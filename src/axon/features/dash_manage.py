"""Server side of the Sessions and Usage controls of the dashboard.

Routes live under ``/api/manage/`` and run after the Host, cookie and (for POST) Origin checks of the HTTP layer.

* Sessions: a paged, filtered, sorted list that stays quick with thousands of chats; rename, star, tag; export;
  delete (to Axon's trash, never destroyed) with restore and an explicit delete-forever.
* A session in use is never deleted: another terminal holding its lease, a running turn here, or the terminal
  session this page is attached to all refuse, and the answer says who has it.
* Usage: the overview plus breakdowns by day, session and project, CSV export, clearing the usage log (with a
  backup first) and the session budget (the same logic as ``/budget``).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon import storage
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer, SessionRef

# A session id is a file name: letters, digits, dot, dash and underscore, never starting with a dot, never "..".
SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,199}")
NAME_RE = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9_.\-]{0,259}")       # a trash folder name or a project key (those start with "-")
MAX_BATCH = 5000
SORTS = ("newest", "oldest", "cost", "length", "name")
USAGE_TABLES = ("days", "models", "sources", "tools", "sessions", "projects", "turns", "calls")
CLEAR_ALL_PHRASE = "clear all usage"


def _s(body: dict[str, Any], key: str, limit: int = 400) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v))) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def check_sid(sid: Any) -> str:
    """A session id that can only name one folder. Subagent transcripts are not sessions of their own."""
    if not isinstance(sid, str) or not SID_RE.fullmatch(sid) or ".." in sid or "_sub_" in sid:
        raise DashError("invalid session id", 400)
    return sid


def check_name(name: Any, what: str = "name") -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or ".." in name:
        raise DashError(f"invalid {what}", 400)
    return name


def _bust(srv: "DashServer") -> None:
    """Forget the cached session lists so the next read sees what just changed."""
    try:
        with srv.index._lock:
            srv.index._at.clear()
    except Exception:
        pass


def _home_short(path: Any) -> str:
    s = str(path)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


# ─── finding and guarding a session ──────────────────────────────────────────

def _ref(srv: "DashServer", sid: str) -> "SessionRef":
    check_sid(sid)
    return srv.index.lookup(sid)


def _lease_holder(ref: "SessionRef") -> dict[str, Any] | None:
    from axon.features.dashboard import held_by
    folder = ref.path.parent if ref.path.name == storage.SESSION_FILE else None
    return held_by(folder) if folder is not None else None


def _who(info: dict[str, Any]) -> str:
    owner = info.get("owner") or "another Axon"
    host = info.get("host")
    where = "" if not host or host == _hostname() else f" on {host}"
    return f"{owner} (process {info.get('pid')}{where})"


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _own_runtime(srv: "DashServer", sid: str) -> Any:
    with srv.lock:
        rt = srv.rts.get(sid)
        if rt is None:
            rt = next((r for r in srv.rts.values() if r.session_id == sid), None)
        return rt


def refusal(srv: "DashServer", ref: "SessionRef", *, deleting: bool) -> str | None:
    """Why this session must not be changed right now, or None. Writing metadata is allowed while this page
    itself has the chat open; deleting is not allowed while anything is using it."""
    info = _lease_holder(ref)
    if info is not None:
        return f"In use by {_who(info)}. Close it there first."
    if not deleting:
        return None
    if srv.live is not None and ref.id in ("live", srv.live.session_id):
        return "This is the session of the terminal Axon this page is attached to, so it is in use."
    if ref.id in srv.busy:
        return "A reply is being written to this chat right now. Wait for it to finish."
    rt = _own_runtime(srv, ref.id)
    if rt is not None and rt.busy:
        return "A turn is running in this chat. Stop it first."
    return None


def _store_for(srv: "DashServer", ref: "SessionRef") -> Any:
    """The SessionStore that owns *ref*: this project's own store, or one over the other project's folder."""
    from axon.features.dashboard import _project_key
    from axon.session.store import SessionStore
    if ref.project_key == _project_key(srv.workspace):
        return SessionStore(srv.workspace)
    ws = Path(ref.workspace) if ref.workspace else None
    if ws is None or not ws.is_dir():
        raise DashError("That chat belongs to a project whose folder can no longer be found, so it is left alone.", 409)
    sdir = ref.path.parent.parent if ref.path.name == storage.SESSION_FILE else ref.path.parent
    return SessionStore(ws, session_dir=sdir)


# ─── listing ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=20000)
def _clean_title(raw: str) -> str:
    from axon.features.dashboard import redact_text
    return redact_text(raw)[:200]


def _rows(srv: "DashServer", scope: str, fresh: bool) -> list[dict[str, Any]]:
    live = srv.live_session_id() if callable(srv.live_session_id) else srv.live_session_id
    out = []
    for ref in srv.index.refs(scope, fresh=fresh).values():
        m = ref.meta
        out.append({"id": ref.id, "title": _clean_title(m.first_prompt), "model": m.model, "messages": m.message_count,
                    "cost": m.total_cost, "tokens": m.total_tokens, "updated": m.created_at, "tags": list(m.tags),
                    "starred": bool(m.is_starred), "project": ref.project, "project_key": ref.project_key,
                    "live": ref.id == live})
    return out


_FULL_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _full_text(srv: "DashServer", scope: str, q: str) -> dict[str, Any]:
    """{session id: snippet} for sessions whose content mentions *q* (cached for a minute: it reads transcripts)."""
    key = (str(srv.workspace), scope, q.lower())
    hit = _FULL_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    from axon.features.dashboard import redact_text
    from axon.features.session_search import search_sessions
    t0 = time.time()
    res = search_sessions(srv.workspace, q, scope == "all", 300)
    snippets: dict[str, str] = {}
    for h in res.get("hits", []):
        snippets.setdefault(h["session"], redact_text(h.get("snippet", "")))
    out = {"snippets": snippets, "truncated": bool(res.get("truncated")), "files": res.get("files", 0),
           "encrypted": len(res.get("skipped_encrypted", [])), "ms": int((time.time() - t0) * 1000)}
    if len(_FULL_CACHE) > 50:
        _FULL_CACHE.clear()
    _FULL_CACHE[key] = (time.time(), out)
    return out


def list_sessions(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    scope = "all" if qs.get("scope") == "all" else "project"
    rows = _rows(srv, scope, qs.get("fresh") == "1")
    q = (qs.get("q") or "").strip().lower()[:200]
    sort = qs.get("sort") if qs.get("sort") in SORTS else "newest"
    tag, model = qs.get("tag", ""), qs.get("model", "")
    # what you can filter by, counted over the whole scope so the choices do not vanish while you filter
    tags: dict[str, int] = {}
    models: dict[str, int] = {}
    for r in rows:
        for t in r["tags"]:
            tags[t] = tags.get(t, 0) + 1
        models[r["model"]] = models.get(r["model"], 0) + 1
    full: dict[str, Any] | None = None
    snippets: dict[str, str] = {}
    if q and qs.get("full") == "1":
        full = _full_text(srv, scope, q)
        snippets = full["snippets"]
    out = []
    for r in rows:
        if qs.get("starred") == "1" and not r["starred"]:
            continue
        if tag and tag not in r["tags"]:
            continue
        if model and r["model"] != model:
            continue
        if q:
            hay = f"{r['title']} {' '.join(r['tags'])} {r['model']} {r['project']} {r['id']}".lower()
            if q not in hay and r["id"] not in snippets:
                continue
        out.append(r)
    if sort == "oldest":
        out.sort(key=lambda r: r["updated"])
    elif sort == "cost":
        out.sort(key=lambda r: (-r["cost"], -r["updated"]))
    elif sort == "length":
        out.sort(key=lambda r: (-r["messages"], -r["updated"]))
    elif sort == "name":
        out.sort(key=lambda r: (r["title"].casefold(), -r["updated"]))
    else:
        out.sort(key=lambda r: r["updated"], reverse=True)
    offset = _int(qs.get("offset"), 0, 0, 10**7)
    limit = _int(qs.get("limit"), 100, 1, 500)
    page = [dict(r, snippet=snippets.get(r["id"], "")) if snippets else r for r in out[offset:offset + limit]]
    res: dict[str, Any] = {"rows": page, "total": len(out), "scope_total": len(rows), "scope": scope, "offset": offset,
                           "tags": sorted(tags.items()), "models": sorted(models.items()),
                           "cost": round(sum(r["cost"] for r in out), 5), "full": {k: v for k, v in (full or {}).items() if k != "snippets"} or None}
    if qs.get("ids") == "1":
        res["ids"] = [r["id"] for r in out][:MAX_BATCH]
    return res


# ─── changing one session ────────────────────────────────────────────────────

def update_session(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    ref = _ref(srv, _s(body, "id", 300))
    why = refusal(srv, ref, deleting=False)
    if why:
        raise DashError(why, 409)
    store = _store_for(srv, ref)
    done: dict[str, Any] = {}
    if "title" in body:
        title = " ".join(_s(body, "title", 400).split())
        if not title or any(ord(c) < 32 for c in title):
            raise DashError("A name cannot be empty.")
        done["title"] = store.rename_session(title, ref.id)
    if "tag" in body:
        tag = _s(body, "tag", 100).strip().lstrip("#")
        if re.search(r"[\x00-\x1f]", tag):
            raise DashError("A tag cannot contain control characters.")
        done["tag"] = store.tag_session(tag or "-", ref.id)
    if "starred" in body:
        if not isinstance(body["starred"], bool):
            raise DashError("'starred' must be true or false")
        if store.is_starred(ref.id) != body["starred"]:
            store.star_session(ref.id)
        done["starred"] = body["starred"]
    if not done:
        raise DashError("nothing to change")
    _bust(srv)
    return {"ok": True, "id": ref.id, **done}


def _delete_one(srv: "DashServer", sid: str) -> dict[str, Any]:
    try:
        ref = _ref(srv, sid)
    except DashError as e:
        return {"id": sid, "ok": False, "reason": "Session not found." if e.status == 404 else str(e)}
    why = refusal(srv, ref, deleting=True)
    if why:
        return {"id": sid, "ok": False, "reason": why}
    try:
        store = _store_for(srv, ref)
    except DashError as e:
        return {"id": sid, "ok": False, "reason": str(e)}
    rt = _own_runtime(srv, sid)
    if rt is not None:                     # an idle chat of this page: let go of it (and its lease) first
        with srv.lock:
            for k in [k for k, v in srv.rts.items() if v is rt]:
                srv.rts.pop(k, None)
            if srv.default_rt is rt:
                srv.default_rt = None
        try:
            rt.close()
        except Exception:
            pass
    title = ref.meta.first_prompt if ref.meta is not None else ""
    store.last_trash = None
    if not store.delete_session(sid):
        return {"id": sid, "ok": False, "reason": "Could not move it to the trash. Nothing was changed or only part moved; check the trash."}
    made = getattr(store, "last_trash", None)           # not a scan of the whole trash: that made a bulk delete slower with every chat
    return {"id": sid, "ok": True, "title": _clean_title(title), "project_key": ref.project_key,
            "name": made.name if made is not None else ""}


def delete_sessions(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise DashError("'ids' must be a list of session ids")
    if len(ids) > MAX_BATCH:
        raise DashError(f"At most {MAX_BATCH} chats at once.", 413)
    ids = list(dict.fromkeys(check_sid(i) for i in ids))
    if len(ids) > 1 and body.get("confirm_count") != len(ids):
        raise DashError(f"Deleting {len(ids)} chats needs confirmation: send confirm_count={len(ids)}.", 400)
    results = [_delete_one(srv, i) for i in ids]
    _bust(srv)
    return {"results": results, "deleted": [r for r in results if r["ok"]], "refused": [r for r in results if not r["ok"]]}


# ─── trash ───────────────────────────────────────────────────────────────────

def _project_dirs(srv: "DashServer", scope: str) -> list[tuple[str, Path]]:
    from axon.features.dashboard import _project_key
    cur = _project_key(srv.workspace)
    root = storage.axon_home() / "projects"
    if scope != "all":
        return [(cur, root / cur)]
    try:
        return [(p.name, p) for p in sorted(root.iterdir()) if p.is_dir()]
    except OSError:
        return []


def _project_name_of(key: str, pdir: Path) -> str:
    from axon.features.analytics import _project_name
    ws = ""
    try:
        ws = str(json.loads((pdir / "project.json").read_text(encoding="utf-8")).get("workspace") or "")
    except (OSError, ValueError, AttributeError):
        pass
    return _project_name(key, ws)


def list_trash(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    scope = "all" if qs.get("scope") == "all" else "project"
    items: list[dict[str, Any]] = []
    for key, pdir in _project_dirs(srv, scope):
        troot = pdir / "trash"
        try:
            dirs = [d for d in troot.iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            continue
        name = _project_name_of(key, pdir) if dirs else ""
        for d in dirs:
            try:
                meta = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    raise ValueError
            except (OSError, ValueError):
                meta = {"session_id": d.name.split("_", 1)[-1], "deleted_at": d.stat().st_mtime, "items": []}
            from axon.features.dashboard import redact_text
            items.append({"project_key": key, "project": name, "name": d.name, "id": str(meta.get("session_id") or d.name),
                          "title": redact_text(str(meta.get("title") or ""))[:200],
                          "deleted_at": float(meta.get("deleted_at") or 0), "files": len(meta.get("items") or [])})
    items.sort(key=lambda m: m["deleted_at"], reverse=True)
    return {"items": items[:1000], "total": len(items), "scope": scope}


def _trash_items(body: dict[str, Any]) -> list[tuple[str, str]]:
    raw = body.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_BATCH:
        raise DashError("'items' must be a non-empty list")
    out = []
    for it in raw:
        if not isinstance(it, dict):
            raise DashError("each item needs project_key and name")
        out.append((check_name(it.get("project_key"), "project"), check_name(it.get("name"), "trash entry")))
    return out


def _trash_dir_of(key: str, name: str) -> tuple[Path, Path]:
    """(project folder, the trash entry), verified to be a real folder directly inside that project's trash."""
    pdir = storage.axon_home() / "projects" / key
    troot = pdir / "trash"
    d = troot / name
    if not d.is_dir() or d.is_symlink() or d.parent.resolve() != troot.resolve():
        raise DashError("That entry is no longer in the trash.", 404)
    return pdir, d


def restore_sessions(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features.dashboard import _project_key
    from axon.session.store import SessionStore
    out: list[dict[str, Any]] = []
    for key, name in _trash_items(body):
        try:
            pdir, d = _trash_dir_of(key, name)
            if key == _project_key(srv.workspace):
                store = SessionStore(srv.workspace)
            else:
                ws = ""
                try:
                    ws = str(json.loads((pdir / "project.json").read_text(encoding="utf-8")).get("workspace") or "")
                except (OSError, ValueError, AttributeError):
                    pass
                if not ws or not Path(ws).is_dir():
                    raise DashError("Its project folder can no longer be found, so it cannot be restored from here.", 409)
                store = SessionStore(Path(ws), session_dir=pdir / "sessions")
            if store.trash_root().resolve() != (pdir / "trash").resolve():
                raise DashError("That entry belongs to a different project folder than it can be restored into.", 409)
            sid = store.restore_session(name)
            if sid is None:
                raise DashError("Could not restore it.", 409)
            out.append({"project_key": key, "name": name, "ok": True, "id": sid, "partial": d.exists()})
        except DashError as e:
            out.append({"project_key": key, "name": name, "ok": False, "reason": str(e)})
    _bust(srv)
    return {"results": out, "restored": [r for r in out if r["ok"]]}


def delete_forever(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    if body.get("confirm") is not True:
        raise DashError("Deleting for good needs confirmation (confirm: true).", 400)
    out: list[dict[str, Any]] = []
    freed = 0
    for key, name in _trash_items(body):
        try:
            _, d = _trash_dir_of(key, name)
        except DashError as e:
            out.append({"project_key": key, "name": name, "ok": False, "reason": str(e)})
            continue
        size = 0
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    size += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)
        ok = not d.exists()
        freed += size if ok else 0
        out.append({"project_key": key, "name": name, "ok": ok, **({} if ok else {"reason": "Some files could not be removed."})})
    return {"results": out, "removed": sum(1 for r in out if r["ok"]), "bytes": freed}


# ─── export ──────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:40]
    return s or "chat"


def export_session(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features.dashboard import load_transcript, redact_text
    ref = _ref(srv, qs.get("id", ""))
    fmt = "json" if qs.get("format") == "json" else "md"
    try:
        tr = load_transcript(ref.path)
    except OSError:
        raise DashError("session not found", 404) from None
    title = redact_text(tr.title or (ref.meta.first_prompt if ref.meta is not None else "") or ref.id)[:200]
    base = f"{_slug(title)}-{ref.id[-8:]}"
    if fmt == "json":
        ents = []
        for e in tr.entries:
            e2 = {k: v for k, v in e.items() if k != "i"}
            ents.append(json.loads(redact_text(json.dumps(e2, ensure_ascii=False, default=str))))
        content = json.dumps({"id": ref.id, "title": title, "model": tr.model, "entries": ents}, ensure_ascii=False, indent=2)
        return {"filename": base + ".json", "mime": "application/json", "content": content}
    lines = [f"# {title}", "", f"Session `{ref.id}`" + (f", model {tr.model}" if tr.model else ""), ""]
    for e in tr.entries:
        when = ""
        if isinstance(e.get("ts"), (int, float)):
            when = " " + datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M")
        if e["kind"] == "user":
            lines += [f"## You{when}", "", redact_text(e["text"]), ""]
        elif e["kind"] == "assistant":
            lines += [f"## Axon{when}", ""]
            if e.get("text"):
                lines += [redact_text(e["text"]), ""]
            for t in e.get("tools", []):
                res = redact_text(t.get("result") or "")
                lines += [f"Tool: `{t['name']}`" + (" (failed)" if t.get("is_error") else ""), "", "```", res[:4000] + (" ..." if len(res) > 4000 else ""), "```", ""]
        else:
            lines += [f"> {redact_text(e.get('text', ''))}", ""]
    return {"filename": base + ".md", "mime": "text/markdown", "content": "\n".join(lines)}


# ─── usage ───────────────────────────────────────────────────────────────────

def _period_scope(srv: "DashServer", qs: dict[str, str]) -> tuple[Any, str, str | None]:
    from axon.features import analytics as an
    from axon.features.dashboard import _project_key
    raw = qs.get("period", "30d") or "30d"
    per = an.parse_period(raw)
    if per is None:           # a mistyped or out-of-range period is said, not quietly replaced by 30 days
        raise DashError("That period is not valid. Use 7d, 30d, all, or two dates like 2026-10-01..2026-10-15 (at most 10 years).", 400)
    scope = "all" if qs.get("scope") == "all" else "project"
    return per, scope, (None if scope == "all" else _project_key(srv.workspace))


def usage_report(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import analytics as an
    from axon.features.dashboard import overview_data, redact_text
    per, scope, key = _period_scope(srv, qs)
    stats = an.collect(per, project=key, scope="all projects" if scope == "all" else f"project {srv.workspace.name}")
    out = overview_data(srv.workspace, per.key, scope, stats=stats)
    out["project_name"] = srv.workspace.name
    out["period_key"] = per.key
    out["by_day"] = [{"name": d.isoformat(), "calls": a.calls, "input": a.input, "output": a.output, "cache_read": a.cache_read,
                      "cache_write": a.cache_write, "cost": a.cost} for d, a in stats.days]
    out["by_session"] = [{"id": s.id, "title": redact_text(s.title) or s.id, "project": an._project_name(s.project, s.workspace),
                          "calls": s.calls, "turns": s.turns, "tokens": s.tokens, "cost": s.cost, "last": s.last_ts,
                          "models": sorted(s.models)} for s in stats.sessions]
    out["sessions_total"] = len(stats.sessions)
    out["by_project"] = [{"key": k, "name": n, "calls": a.calls, "input": a.input, "output": a.output, "cache_read": a.cache_read,
                          "cache_write": a.cache_write, "cost": a.cost} for k, n, a in stats.by_project] if scope == "all" else []
    return out


def _csv_cell(v: Any) -> Any:
    """A spreadsheet runs a cell that starts with = + - or @ as a formula; prefix those so they stay text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def usage_csv(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import analytics as an
    table = qs.get("table", "days")
    if table not in USAGE_TABLES:
        raise DashError("unknown table")
    per, scope, key = _period_scope(srv, qs)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if table == "calls":
        w.writerow(["time", "project", "session", "model", "source", "input", "output", "cache_read", "cache_write", "cost_usd", "seconds"])
        for r in an.load_usage(per.since, key, per.until):
            w.writerow([_csv_cell(x) for x in (datetime.fromtimestamp(r.ts, timezone.utc).isoformat(timespec="seconds"),
                        an._project_name(r.project, r.workspace), r.session, r.model, r.source, r.input, r.output,
                        r.cache_read, r.cache_write, round(r.cost, 6), "" if r.duration_s is None else r.duration_s)])
    else:
        d = usage_report(srv, qs)
        if d.get("empty"):
            d = {**d, "by_model": [], "by_source": [], "tools": [], "turns": []}
        agg = ["calls", "input", "output", "cache_read", "cache_write", "cost"]
        if table == "days":
            w.writerow(["date"] + agg)
            for r in d["by_day"]:
                w.writerow([r["name"]] + [round(r[k], 6) if k == "cost" else r[k] for k in agg])
        elif table in ("models", "sources", "projects"):
            w.writerow([{"models": "model", "sources": "source", "projects": "project"}[table]] + agg)
            for r in d[{"models": "by_model", "sources": "by_source", "projects": "by_project"}[table]]:
                w.writerow([_csv_cell(r["name"])] + [round(r[k], 6) if k == "cost" else r[k] for k in agg])
        elif table == "tools":
            w.writerow(["tool", "calls", "seconds", "errors"])
            for r in d["tools"]:
                w.writerow([_csv_cell(r["name"]), r["calls"], round(r["total_s"], 3), r["errors"]])
        elif table == "turns":
            w.writerow(["prompt", "model", "tokens", "tool_calls", "seconds", "cost_usd"])
            for r in d["turns"]:
                w.writerow([_csv_cell(r["prompt"]), _csv_cell(r["model"]), r["tokens"], r["tools"], round(r["seconds"], 2), round(r["cost"], 6)])
        else:
            w.writerow(["session", "title", "project", "calls", "turns", "tokens", "cost_usd", "last_used"])
            for r in d["by_session"]:
                w.writerow([r["id"], _csv_cell(r["title"]), _csv_cell(r["project"]), r["calls"], r["turns"], r["tokens"], round(r["cost"], 6),
                            datetime.fromtimestamp(r["last"], timezone.utc).isoformat(timespec="seconds") if r["last"] else ""])
    label = "all-projects" if scope == "all" else _slug(srv.workspace.name)
    return {"filename": f"axon-usage-{table}-{label}-{re.sub(r'[^0-9a-zA-Z]+', '_', per.key)}.csv", "mime": "text/csv", "content": buf.getvalue()}


def _usage_file() -> Path:
    return storage.user_file("usage.jsonl")


def _backup_dir() -> Path:
    return storage.user_file("usage-backups")


def _matches(line: str, key: str | None) -> bool:
    """Whether a usage line belongs to the cleared scope (key None = every project). A line that cannot be read is kept."""
    if key is None:
        return True
    try:
        rec = json.loads(line)
    except ValueError:
        return False
    return isinstance(rec, dict) and str(rec.get("project") or "") == key


def usage_clear_preview(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features.dashboard import _project_key
    scope = "all" if qs.get("scope") == "all" else "project"
    key = None if scope == "all" else _project_key(srv.workspace)
    n = 0
    first = last = None
    path = _usage_file()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip() and _matches(line, key):
                    n += 1
                    if '"ts"' in line:
                        try:
                            ts = json.loads(line).get("ts")
                        except ValueError:
                            ts = None
                        first = first or ts
                        last = ts or last
    except OSError:
        pass
    return {"scope": scope, "records": n, "first": first, "last": last, "project": srv.workspace.name,
            "backup_dir": _home_short(_backup_dir()), "phrase": CLEAR_ALL_PHRASE if scope == "all" else ""}


def usage_clear(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    """Remove usage records of this project (or all) from the usage log, after copying the whole log to a backup."""
    from axon.features.dashboard import _project_key
    scope = "all" if body.get("scope") == "all" else "project"
    if body.get("confirm") is not True:
        raise DashError("Clearing usage needs confirmation (confirm: true).", 400)
    if scope == "all" and body.get("phrase") != CLEAR_ALL_PHRASE:
        raise DashError(f"To clear every project, type exactly: {CLEAR_ALL_PHRASE}", 400)
    key = None if scope == "all" else _project_key(srv.workspace)
    path = _usage_file()
    with storage._LOCK:                       # appends from this process wait; another process appending meanwhile is re-checked below
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"ok": True, "removed": 0, "kept": 0, "backup": "", "scope": scope}
        except OSError as e:
            raise DashError(f"Could not read the usage log ({type(e).__name__}).", 500) from None
        text = raw.decode("utf-8", errors="replace")
        keep: list[str] = []
        removed = 0
        for line in text.splitlines(keepends=True):
            if line.strip() and _matches(line, key):
                removed += 1
            else:
                keep.append(line if line.endswith("\n") else line + "\n")
        if not removed:
            return {"ok": True, "removed": 0, "kept": len(keep), "backup": "", "scope": scope}
        bdir = _backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = bdir / f"usage-{stamp}-{'all' if key is None else 'project'}.jsonl"
        n = 2
        while backup.exists():
            backup = bdir / f"usage-{stamp}-{'all' if key is None else 'project'}-{n}.jsonl"
            n += 1
        try:
            backup.write_bytes(raw)
            if backup.stat().st_size != len(raw):
                raise OSError("short backup")
        except OSError as e:
            raise DashError(f"Could not save the backup ({type(e).__name__}), so nothing was cleared.", 500) from None
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text("".join(keep), encoding="utf-8")
            try:
                grown = path.stat().st_size - len(raw)
            except OSError:
                grown = 0
            if grown > 0:          # another process appended after we read: carry those lines over
                with path.open("rb") as f:
                    f.seek(len(raw))
                    tail = f.read()
                with tmp.open("ab") as g:
                    g.write(tail)
            os.replace(tmp, path)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise DashError(f"Could not rewrite the usage log ({type(e).__name__}). Nothing was cleared; the backup is at {_home_short(backup)}.", 500) from None
    return {"ok": True, "removed": removed, "kept": len(keep), "backup": _home_short(backup), "scope": scope}


# ─── budget ──────────────────────────────────────────────────────────────────

def _budget_row(rt: Any) -> dict[str, Any]:
    from decimal import Decimal, InvalidOperation
    from axon.features import analytics as an
    try:
        ceiling = Decimal(str(getattr(rt.agent.settings, "session_cost_ceiling", 0) or 0))
    except (InvalidOperation, ValueError):
        ceiling = Decimal("0")
    spent = an._spent(rt.agent)
    return {"session": rt.session_id, "key": rt.key, "limit": float(ceiling) if ceiling > 0 else 0.0, "has_limit": ceiling > 0,
            "spent": float(spent), "remaining": float(max(Decimal("0"), ceiling - spent)) if ceiling > 0 else None,
            "busy": bool(rt.busy), "live": bool(getattr(rt, "external", False))}


def budgets(srv: "DashServer") -> dict[str, Any]:
    rows = []
    for rt in srv.all_runtimes():
        try:
            row = _budget_row(rt)
        except Exception:
            continue
        title = ""
        try:
            title = _clean_title(srv.index.lookup(rt.session_id).meta.first_prompt)
        except Exception:
            pass
        row["title"] = title or "New chat"
        rows.append(row)
    return {"budgets": rows}


def set_budget(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from decimal import Decimal
    from axon.features import analytics as an
    sid = _s(body, "session", 300)
    rt = srv.runtime(sid, create=False)
    if rt is None:
        raise DashError("Open that chat first: a budget belongs to a chat that is running.", 404)
    raw = body.get("amount")
    if raw is None or raw is False or (isinstance(raw, str) and raw.strip().lower() in ("off", "none", "clear", "")):
        new = Decimal("0")
    else:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise DashError("The amount must be a number of dollars.")
        new = an.parse_amount(str(raw))
        if new is None:
            raise DashError("The amount must be a number of dollars, like 5 or 2.50.")
        if new > Decimal("100000"):
            raise DashError("The limit is too large (most is $100,000).")
    an._apply_settings(rt.agent, session_cost_ceiling=new)
    return {"ok": True, **_budget_row(rt)}


# ─── memory ──────────────────────────────────────────────────────────────────

MEM_BODY_MAX = 20_000


def _mem_row(m: Any) -> dict[str, Any]:
    from axon.features.dashboard import redact_text
    body = str(m.body)
    return {"scope": m.scope, "id": m.id, "file": m.file, "name": redact_text(str(m.name))[:200], "type": m.type,
            "description": redact_text(str(m.description))[:300], "body": redact_text(body[:MEM_BODY_MAX]), "cut": len(body) > MEM_BODY_MAX,
            "updated": m.updated, "path": _home_short(m.path)}


def memory_report(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    """Saved memories of every scope (global, project, repo), filtered by scope and text, plus the instruction files
    the model is given at the start (the same discovery the terminal uses)."""
    from axon.features import memory_files as mf
    scope = qs.get("scope", "")
    scopes = (mf.norm_scope(scope),) if scope in mf.SCOPES else None
    items = mf.list_memories(srv.workspace, scopes)
    counts: dict[str, int] = {}
    for m in items:
        counts[m.scope] = counts.get(m.scope, 0) + 1
    q = (qs.get("q") or "").strip().lower()[:200]
    if q:
        items = [m for m in items if q in f"{m.name} {m.description} {m.body} {m.type} {m.id}".lower()]
    from axon.features.dash_api import instruction_files
    return {"memories": [_mem_row(m) for m in items[:500]], "total": len(items), "counts": counts,
            "files": instruction_files(srv.workspace), "workspace": str(srv.workspace)}


def _mem_body(body: dict[str, Any]) -> str:
    text = _s(body, "body", MEM_BODY_MAX + 1)
    if not text.strip():
        raise DashError("A memory cannot be empty.")
    if len(text) > MEM_BODY_MAX:
        raise DashError(f"A memory can be at most {MEM_BODY_MAX:,} characters.", 413)
    return text.strip()


def _mem_scope(body: dict[str, Any]) -> str:
    from axon.features import memory_files as mf
    try:
        return mf.norm_scope(_s(body, "scope", 20))
    except ValueError as e:
        raise DashError(str(e)) from None


def memory_save(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    """Add a memory as written (no model call). Secrets are removed before it is saved."""
    from axon.features import memory_files as mf
    scope = _mem_scope(body)
    text = _mem_body(body)
    name = " ".join(_s(body, "name", 200).split()) or mf.derive_description(text)[:80]
    typ = body.get("type") if body.get("type") in mf.TYPES else "user" if scope == "global" else "project"
    try:
        res = mf.write_memory(srv.workspace, scope, name=name, body=text, description=_s(body, "description", 300), type_=typ)
    except OSError as e:
        raise DashError(f"Could not save it ({type(e).__name__}).", 500) from None
    return {"ok": True, "action": res.action, "id": res.path.stem, "scope": scope, "notes": res.notes()}


def _mem_find(srv: "DashServer", body: dict[str, Any]) -> Any:
    from axon.features import memory_files as mf
    scope = _mem_scope(body)
    mid = _s(body, "id", 200)
    m = mf.find_memory(srv.workspace, f"{scope}:{mid}", mf.list_memories(srv.workspace, (scope,)))
    if m is None:
        raise DashError("That memory no longer exists.", 404)
    return m


def memory_update(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_files as mf
    m = _mem_find(srv, body)
    text = _mem_body(body)
    try:
        res = mf.replace_body(srv.workspace, m, text)
    except OSError as e:
        raise DashError(f"Could not save it ({type(e).__name__}).", 500) from None
    return {"ok": True, "action": res.action, "id": m.id, "scope": m.scope, "notes": res.notes()}


def memory_delete(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import memory_files as mf
    m = _mem_find(srv, body)
    try:
        mf.delete_memory(srv.workspace, m)
    except OSError as e:
        raise DashError(f"Could not delete it ({type(e).__name__}).", 500) from None
    return {"ok": True, "id": m.id, "scope": m.scope, "kept": "A copy is kept; /memory restore brings it back."}


# ─── dispatch ────────────────────────────────────────────────────────────────

def get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any | None:
    if route == "/api/manage/sessions":
        return list_sessions(srv, qs)
    if route == "/api/manage/trash":
        return list_trash(srv, qs)
    if route == "/api/manage/export":
        return export_session(srv, qs)
    if route == "/api/manage/usage":
        return usage_report(srv, qs)
    if route == "/api/manage/usage/csv":
        return usage_csv(srv, qs)
    if route == "/api/manage/usage/clear":
        return usage_clear_preview(srv, qs)
    if route == "/api/manage/budget":
        return budgets(srv)
    if route == "/api/manage/memory":
        return memory_report(srv, qs)
    raise DashError("not found", 404)


def post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any | None:
    if route == "/api/manage/sessions/update":
        return update_session(srv, body)
    if route == "/api/manage/sessions/delete":
        return delete_sessions(srv, body)
    if route == "/api/manage/trash/restore":
        return restore_sessions(srv, body)
    if route == "/api/manage/trash/delete":
        return delete_forever(srv, body)
    if route == "/api/manage/usage/clear":
        return usage_clear(srv, body)
    if route == "/api/manage/budget":
        return set_budget(srv, body)
    if route == "/api/manage/memory/save":
        return memory_save(srv, body)
    if route == "/api/manage/memory/update":
        return memory_update(srv, body)
    if route == "/api/manage/memory/delete":
        return memory_delete(srv, body)
    raise DashError("not found", 404)
