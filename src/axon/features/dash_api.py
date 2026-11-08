"""App API of the dashboard (workspace, files, chat, commands, shell, attachments, memory).

Every handler runs after the Host, cookie and (for POST) Origin checks done by the HTTP layer."""
from __future__ import annotations

import dataclasses
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features import dash_fs
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v))) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _s(body: dict[str, Any], key: str, limit: int = 100_000) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


# ─── GET ────────────────────────────────────────────────────────────────────

def api_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any | None:
    """JSON-able payload for *route*, or None when the route is not an app route."""
    ws = srv.workspace
    if route.startswith("/api/manage/"):          # Sessions and Usage controls (dash_manage.py)
        from axon.features import dash_manage
        return dash_manage.get(srv, route, qs)
    if route == "/api/ws":
        info = dash_fs.git_info(ws)
        return {"workspace": str(ws), "name": ws.name, "home": str(Path.home()), "recent": dash_fs.recent_projects(),
                "git": {"repo": info["repo"], "branch": info["branch"], "changed": len(info["files"])},
                "live": srv.live is not None, "can_switch": srv.live is None,
                "extra_dirs": [str(p) for p in srv.extra_dirs()]}
    if route == "/api/fs/dirs":
        return dash_fs.list_subdirs(qs.get("path", ""), qs.get("hidden") == "1")
    if route == "/api/tree":
        return dash_fs.list_dir(ws, srv.extra_dirs(), qs.get("path", ""), qs.get("ignored") == "1")
    if route == "/api/files/search":
        return {"files": dash_fs.search_files(ws, qs.get("q", ""))}
    if route == "/api/files/grep":
        return dash_fs.grep_files(ws, qs.get("q", ""))
    if route == "/api/file/preview":
        p = dash_fs.jail(ws, srv.extra_dirs(), qs.get("path", ""))
        if not p.is_file() or p.stat().st_size > 2_000_000:
            raise DashError("file not found or too large to preview", 404)
        from axon.features.dashboard import redact_text, render_markdown
        return {"html": render_markdown(redact_text(p.read_text(encoding="utf-8", errors="replace"))), "path": dash_fs.rel_to(ws, p)}
    if route == "/api/file/extract":
        return dash_fs.extract_file(ws, srv.extra_dirs(), qs.get("path", ""), qs.get("pages"), qs.get("sheet"),
                                    _int(qs.get("offset"), 0, 0, 10**9), _int(qs.get("limit"), 500, 1, 5000))
    if route == "/api/file":
        return dash_fs.read_file(ws, srv.extra_dirs(), qs.get("path", ""), _int(qs.get("offset"), 0, 0, 10**9),
                                 _int(qs.get("limit"), 1200, 1, dash_fs.MAX_PAGE_LINES), as_text=qs.get("as_text") == "1")
    if route == "/api/git/diff":
        p = dash_fs.jail(ws, srv.extra_dirs(), qs.get("path", ""))
        return dash_fs.git_diff(ws, p)
    if route == "/api/git/status":
        info = dash_fs.git_info(ws, fresh=True)
        return {"repo": info["repo"], "branch": info["branch"], "files": info["files"]}
    if route.startswith("/api/workbench/"):
        from axon.features import dash_workbench
        return dash_workbench.handle_get(srv, route, qs)
    if route == "/api/chat/state":
        rt = srv.runtime(qs.get("session", ""), create=qs.get("create") != "0")
        return rt.state() if rt else {"key": "", "busy": False, "none": True}
    m = re.fullmatch(r"/api/session/([^/]+)/(events|blob)", route)
    if m:
        return session_events(srv, urllib.parse.unquote(m.group(1)), m.group(2), qs)
    if route == "/api/term/list":
        ok, why = srv.pty.available()
        return {"available": ok, "reason": why, "terminals": [t.summary() for t in srv.pty.sessions.values()], "cwd": str(ws)}
    m2 = re.fullmatch(r"/api/insights/([a-z_]+)", route)
    if m2:
        return insights_get(srv, m2.group(1), qs)
    if route == "/api/providers":
        from axon.features import dash_providers as dp
        return dp.providers_view(srv)
    if route == "/api/provider/ready":
        from axon.features import dash_providers as dp
        ready, why = dp.provider_ready(srv, srv.runtime(qs.get("session", ""), create=False))
        return {"ready": ready, "reason": why, "current": dp.current_choice(srv) if ready or True else {}}
    if route == "/api/provider/models":
        from axon.features import dash_providers as dp
        return dp.list_models(qs.get("preset", ""), qs.get("base_url", ""))
    if route == "/api/settings/panel":
        from axon.features import dash_providers as dp
        return dp.settings_panel(srv, srv.runtime(qs.get("session", ""), create=False))
    if route == "/api/subagents":
        from axon.features import dash_subagents
        return dash_subagents.roster(srv, qs.get("session", ""))
    if route == "/api/subagent/detail":
        from axon.features import dash_subagents
        try:
            num = int(qs.get("n", ""))
        except ValueError:
            raise DashError("n must be a subagent number") from None
        return dash_subagents.detail(srv, qs.get("session", ""), num)
    if route == "/api/models":
        from axon.providers.registry import known_models
        rt = srv.runtime(qs.get("session", ""), create=False)
        cur = rt.agent.settings.model if rt else srv.settings_obj().model
        return {"models": sorted(set(known_models()) | {cur}), "current": cur}
    if route == "/api/cmdcard":                  # structured command cards (dash_cmdcards.py)
        from axon.features import dash_cmdcards
        return dash_cmdcards.get(srv, qs)
    if route == "/api/commands":
        rt = srv.runtime(qs.get("session", ""), create=False)
        return {"commands": srv.commands.list_commands(rt)}
    if route == "/api/command/job":
        job = srv.commands.jobs.get(qs.get("id", ""))
        if job is None:
            raise DashError("no such command job", 404)
        return {"done": job.done.is_set(), **(job.result if job.done.is_set() else {})}
    if route == "/api/shell/history":
        jobs = [j.summary() for j in srv.shell.jobs.values()]
        return {"history": srv.shell.history[-60:], "jobs": jobs, "cwd": str(srv.shell.current_cwd(ws))}
    if route == "/api/shell/background":
        return {"shells": _background(qs.get("tail", "") == "1")}
    if route == "/api/memory":
        return memory_list(srv)
    if route == "/api/marketplace":
        return marketplace(qs.get("tab", "connectors"), qs.get("q", ""))
    if route == "/api/permissions":
        return permissions_view(srv, qs.get("session", ""))
    return None


def _background(tail: bool) -> list[dict[str, Any]]:
    try:
        from axon.tools import shell as shell_mod
    except Exception:
        return []
    rows = []
    for b in shell_mod.BACKGROUND.all():
        row = {"id": b.id, "command": b.command[:400], "status": b.status, "pid": b.proc.pid, "started": b.started}
        with b.stream.lock:
            text = b.stream.buf[-60_000:] if tail else ""
        from axon.features.dash_ansi import ansi_to_html
        row["html"] = ansi_to_html(text)
        rows.append(row)
    return rows


def memory_list(srv: "DashServer") -> dict[str, Any]:
    ws = srv.workspace
    files = []
    for base, label in ((ws, "project"), (Path.home(), "user")):
        for name in ("AXON.md", "CLAUDE.md", "AGENTS.md"):
            p = base / name
            if p.is_file():
                inside = label == "project"
                files.append({"name": name, "scope": label, "path": str(p), "rel": name if inside else "",
                              "size": p.stat().st_size, "editable": inside})
    notes: list[dict[str, Any]] = []
    try:
        from axon.features import memory_files as mf
        for m in mf.list_memories(ws):
            notes.append({"name": getattr(m, "name", ""), "scope": getattr(m, "scope", ""), "type": getattr(m, "type", ""),
                          "description": getattr(m, "description", ""), "body": str(getattr(m, "body", ""))[:6000]})
    except Exception:
        pass
    return {"files": files, "memories": notes, "workspace": str(ws)}


def marketplace(tab: str, q: str) -> dict[str, Any]:
    from axon.marketplace import catalog
    if tab not in ("connectors", "skills", "plugins"):
        raise DashError("tab must be connectors, skills or plugins")
    try:
        entries = catalog.filter_entries(catalog.load_tab(tab), q)
    except Exception as e:
        raise DashError(f"could not load the catalog: {type(e).__name__}", 500) from None
    rows = []
    for e in entries[:300]:
        d = dataclasses.asdict(e)
        rows.append({k: d.get(k) for k in ("id", "name", "publisher", "description", "category", "verified", "free", "docs_url",
                                           "transport", "kind", "license") if k in d})
    return {"tab": tab, "entries": rows, "total": len(entries)}


def permissions_view(srv: "DashServer", session: str) -> dict[str, Any]:
    rt = srv.runtime(session, create=False)
    s = rt.agent.settings if rt else srv.settings_obj()
    perms = s.permissions
    return {"mode": s.mode, "allow": list(perms.allow), "deny": list(perms.deny), "ask": list(getattr(perms, "ask", [])),
            "additional_dirs": [str(p) for p in srv.extra_dirs()], "sandbox": bool(getattr(s, "sandbox_enabled", False))}


# ─── POST ───────────────────────────────────────────────────────────────────

def api_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any | None:
    ws = srv.workspace
    if route.startswith("/api/manage/"):          # Sessions and Usage controls (dash_manage.py)
        from axon.features import dash_manage
        return dash_manage.post(srv, route, body)
    if route == "/api/ws/set":
        return srv.set_workspace(_s(body, "path", 4096))
    if route == "/api/file/save":
        res = dash_fs.save_file(ws, srv.extra_dirs(), _s(body, "path", 4096), _s(body, "content", 3_000_000),
                                _s(body, "sha", 128) if body.get("sha") else "", create=body.get("create") is True,
                                lines=body["lines"] if isinstance(body.get("lines"), int) and not isinstance(body.get("lines"), bool) else None)
        for rt in srv.all_runtimes():           # the agent must re-read a file that changed under it
            try:
                rt.agent.file_state.invalidate(dash_fs.jail(ws, srv.extra_dirs(), body["path"]))
            except Exception:
                pass
        return res
    if route.startswith("/api/workbench/"):
        from axon.features import dash_workbench
        return dash_workbench.handle_post(srv, route, body)
    if route == "/api/chat/send":
        return chat_send(srv, body)
    if route == "/api/chat/new":
        rt = srv.new_runtime()
        return rt.state()
    if route == "/api/chat/stop":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        if rt is None:
            raise DashError("no running session", 404)
        stopped = rt.cancel()
        rt.hub.emit("stop_requested", {"stopped": stopped})
        return {"ok": True, "stopped": stopped}
    if route == "/api/chat/approval":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        if rt is None:
            raise DashError("no such session", 404)
        rt.answer(_s(body, "id", 64), _s(body, "choice", 16))
        return {"ok": True}
    if route == "/api/chat/answer":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        if rt is None:
            raise DashError("no such session", 404)
        labels = body.get("labels")
        if labels is not None and not (isinstance(labels, list) and all(isinstance(x, str) for x in labels)):
            raise DashError("labels must be a list of strings or null")
        rt.answer_question(_s(body, "id", 64), labels)
        return {"ok": True}
    if route == "/api/subagent/stop":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        mgr = getattr(rt.agent, "subagents", None) if rt else None
        if mgr is None:
            raise DashError("no running subagents in this session", 404)
        return {"ok": bool(mgr.cancel(_s(body, "task", 40)))}
    if route == "/api/subagent/tell":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        mgr = getattr(rt.agent, "subagents", None) if rt else None
        if mgr is None:
            raise DashError("no running subagents in this session", 404)
        task = mgr.send(_s(body, "task", 40), _s(body, "text", 20_000))
        if task is None:
            raise DashError("that subagent is not running, so it cannot be told anything", 409)
        return {"ok": True}
    if route == "/api/chat/fork":
        return chat_fork(srv, _s(body, "session", 300), str(body.get("name") or "")[:40])
    if route == "/api/term/start":
        kind = "axon" if body.get("kind") == "axon" else "shell"
        cwd = dash_fs.jail(ws, srv.extra_dirs(), str(body.get("cwd") or "."))
        sess = srv.pty.start(kind, cwd, _int(body.get("cols"), 100, 20, 400), _int(body.get("rows"), 30, 5, 200), str(body.get("initial") or "")[:2000], str(body.get("theme") or "dark"))
        return sess.summary()
    if route == "/api/term/input":
        data = _s(body, "data", 262144)
        srv.pty.get(_s(body, "id", 32)).write(data.encode("utf-8"))
        return {"ok": True}
    if route == "/api/term/resize":
        srv.pty.get(_s(body, "id", 32)).resize(_int(body.get("cols"), 80, 20, 400), _int(body.get("rows"), 24, 5, 200))
        return {"ok": True}
    if route == "/api/term/close":
        srv.pty.close(_s(body, "id", 32))
        return {"ok": True}
    m2 = re.fullmatch(r"/api/insights/([a-z_]+)", route)
    if m2:
        return insights_post(srv, m2.group(1), body)
    if route == "/api/provider/key":
        from axon.features import dash_providers as dp
        return dp.save_key(_s(body, "preset", 80), _s(body, "key", 4000))
    if route == "/api/provider/test":
        from axon.features import dash_providers as dp
        return dp.test_key(_s(body, "preset", 80), str(body.get("key") or ""), str(body.get("base_url") or ""), str(body.get("model") or ""))
    if route == "/api/provider/apply":
        from axon.features import dash_providers as dp
        return dp.apply_choice(srv, body)
    if route == "/api/settings/set":
        from axon.features import dash_providers as dp
        return dp.set_setting(srv, srv.runtime(str(body.get("session") or ""), create=False), _s(body, "key", 60), body.get("value"))
    if route == "/api/file/reveal":
        return dash_fs.reveal(ws, srv.extra_dirs(), _s(body, "path", 4096))
    if route == "/api/chat/queue/remove":
        rt = srv.runtime(_s(body, "session", 300), create=False)
        if rt is None:
            raise DashError("no such session", 404)
        return {"ok": bool(rt.agent.message_queue.remove(_int(body.get("id"), -1, -1, 10**9)))}
    if route.startswith("/api/cmdcard/"):
        from axon.features import dash_cmdcards
        return dash_cmdcards.post(srv, route, body)
    if route == "/api/commands/run":
        rt = srv.runtime(str(body.get("session") or ""), create=True)
        job = srv.commands.start(rt, _s(body, "line", 20_000), cards=body.get("cards") is True)
        job.done.wait(25)
        out = {"id": job.id, "done": job.done.is_set()}
        if job.done.is_set():
            out.update(job.result)
        out["state"] = rt.state()
        return out
    if route == "/api/shell/run":
        cmd = _s(body, "command", 20_000)
        # With no folder asked for, carry on where the last command ended, which `cd` may have left outside the project.
        cwd = dash_fs.jail(ws, srv.extra_dirs(), str(body["cwd"])) if body.get("cwd") else srv.shell.current_cwd(ws)
        if not cwd.is_dir():
            raise DashError("the working folder does not exist", 404)
        job = srv.shell.start(cmd, cwd, ws, srv.settings_obj(), on_done=srv.record_shell)
        return job.summary()
    if route == "/api/shell/kill":
        return {"ok": srv.shell.kill(_s(body, "id", 32))}
    if route == "/api/shell/bg/kill":
        from axon.tools import shell as shell_mod
        try:
            shell_mod.BACKGROUND.kill(_s(body, "id", 32))
        except Exception as e:
            raise DashError(str(e), 404) from None
        return {"ok": True}
    if route == "/api/attach/text":
        text = _s(body, "text", 80_000_000)
        return srv.attach.add_text(text)
    if route == "/api/attach/paths":
        from axon.features import dash_attach
        return {"items": dash_attach.attach_paths(ws, srv.extra_dirs(), body.get("paths"))}
    if route == "/api/attach/folder":
        from axon.features import dash_attach
        return dash_attach.attach_dropped_folder(body.get("name"), body.get("entries"), body.get("truncated"))
    return None


def chat_send(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import dash_providers as dp
    plain = not str(body.get("text") or "").lstrip().startswith("/")
    if plain:
        ready, why = dp.provider_ready(srv, srv.runtime(str(body.get("session") or ""), create=False))
        if not ready:
            raise DashError(why, 412)
    rt = srv.runtime(str(body.get("session") or ""), create=True)
    text = _s(body, "text", 1_000_000).strip()
    if not text:
        raise DashError("message is empty")
    if text.startswith("/") and not text.startswith("//"):
        first = text.split(None, 1)[0]
        looks_like_path = first.count("/") > 1 or first[1:2] in (".", "~")
        if not looks_like_path:
            if rt.busy:
                from axon.ui.in_flight import is_instant_command
                if not is_instant_command(text):
                    raise DashError("A turn is running. Stop it first, or wait; only read-only commands run now.", 409)
            job = srv.commands.start(rt, text, cards=body.get("cards") is True)
            job.done.wait(25)
            out: dict[str, Any] = {"command": True, "id": job.id, "done": job.done.is_set()}
            if job.done.is_set():
                out.update(job.result)
            out["state"] = rt.state()
            return out
    from axon.features import dash_providers as dp
    ready, why = dp.provider_ready(srv, rt)
    if not ready:
        raise DashError(why, 412)
    line = srv.attach.expand(text)
    res = rt.send(line, display=text, steer=body.get("steer") is not False)
    res["session"] = rt.session_id
    return res


def subagents_view(srv: "DashServer", sid: str) -> dict[str, Any]:
    """Every subagent of a session, live and finished, with what is (and is not) recorded for each."""
    import re as _re
    from axon.features.dashboard import load_transcript
    rt = srv.runtime(sid, create=False)
    tasks: dict[int, dict[str, Any]] = {}
    notes: list[str] = []
    if rt is not None and getattr(rt.agent, "subagents", None) is not None:
        try:
            for t in rt.agent.subagents.all_tasks():
                tasks[t.index] = {"index": t.index, "task": t.id, "title": t.title, "type": t.agent_type, "model": t.model,
                                  "status": t.status, "steps": t.steps, "max_steps": t.max_steps, "elapsed": round(t.elapsed_s, 1),
                                  "tokens": t.tokens_consumed, "tool_calls": t.tool_calls, "activity": t.activity,
                                  "name": t.name, "background": t.background, "stopped": t.stopped, "live": t.status == "running",
                                  "error": t.error_msg or "", "prompt": (t.prompt or "")[:600], "source": "live"}
        except Exception:
            notes.append("The live subagent roster could not be read.")
    try:
        ref = srv.index.lookup(sid) if sid else None
    except DashError:
        ref = None
    if ref is None and rt is not None:
        try:
            class _R:
                path = rt.agent.session._file(rt.session_id)
            ref = _R()  # type: ignore[assignment]
        except Exception:
            ref = None
    files: dict[int, Path] = {}
    if ref is not None:
        folder = Path(ref.path).parent
        for f in (folder / "subagents").glob("*.jsonl"):
            m = _re.fullmatch(r"(\d+)", f.stem)
            if m and not f.name.endswith(".trace.jsonl"):
                files[int(m.group(1))] = f
    for n, f in sorted(files.items()):
        info = tasks.setdefault(n, {"index": n, "task": f"sub-{n}", "title": f"Subagent {n}", "type": "", "model": "", "status": "finished",
                                    "steps": 0, "elapsed": 0, "tokens": 0, "tool_calls": 0, "activity": "", "live": False,
                                    "source": "transcript"})
        try:
            tr = load_transcript(f)
        except Exception:
            notes.append(f"Subagent {n}: its transcript could not be read.")
            continue
        a = [e for e in tr.entries if e["kind"] == "assistant"]
        info["entries"] = len(tr.entries)
        info["cost"] = round(sum(float(e.get("cost") or 0) for e in a), 5)
        info["model"] = info.get("model") or tr.model
        if not info.get("tokens"):
            info["tokens"] = sum(int((e.get("usage") or {}).get("input", 0)) + int((e.get("usage") or {}).get("output", 0)) for e in a)
        info["tool_calls"] = info.get("tool_calls") or sum(len(e["tools"]) for e in a)
        first = next((e["text"] for e in tr.entries if e["kind"] == "user"), "")
        if first and not info.get("prompt"):
            info["prompt"] = first[:600]
        if info["title"].startswith("Subagent ") and first:
            info["title"] = " ".join(first.split())[:60]
        info["has_transcript"] = True
        info["mtime"] = f.stat().st_mtime
    for n, info in tasks.items():
        info.setdefault("has_transcript", False)
        if not info["has_transcript"] and not info.get("live"):
            notes.append(f"Subagent {n} ({info['title']}) has no transcript file on disk, so only its summary is shown.")
        info["parent"] = sid
        info["session_id"] = f"{sid}_sub_{n}" if info["has_transcript"] else ""
    if tasks and not any(i.get("source") == "live" for i in tasks.values()):
        notes.append("Type, status and step counts are only known for subagents started while this server was watching; "
                     "older ones show what their transcript recorded (model, cost, tokens, messages).")
    return {"session": sid, "subagents": [tasks[k] for k in sorted(tasks)], "notes": notes}


def chat_fork(srv: "DashServer", sid: str, name: str) -> dict[str, Any]:
    """Copy a session someone else holds into a new session (the original is never written)."""
    import contextlib
    import io
    ref = srv.index.lookup(sid)
    ws = Path(ref.workspace) if ref.workspace and Path(ref.workspace).is_dir() else srv.workspace
    rt = srv._build_runtime(ws, None)
    from axon.session.interactive import handle_branch
    from axon.session.store import SessionStore
    try:
        conv = SessionStore(ws, session_dir=ref.path.parent.parent).read_conversation(sid)
    except Exception as e:
        rt.close()
        raise DashError(f"could not read that session: {type(e).__name__}", 500) from None
    rt.agent.conversation = conv
    with contextlib.redirect_stdout(io.StringIO()):
        handle_branch(rt.agent, name)
    rt.key = rt.session_id
    with srv.lock:
        srv.rts[rt.key] = rt
        srv.default_rt = rt
    return rt.state()


def session_events(srv: "DashServer", sid: str, what: str, qs: dict[str, str]) -> dict[str, Any]:
    """The ordered event log of a session (or one spilled body of it)."""
    from axon.features.dash_runtime import EventLog
    rt = srv.runtime(sid, create=False)
    log = rt.event_log() if rt is not None else None
    if log is None:
        ref = srv.index.lookup(sid)
        log = EventLog(ref.path.parent)
    if what == "blob":
        return log.blob(qs.get("id", ""), _int(qs.get("offset"), 0, 0, 10**10), _int(qs.get("limit"), 400_000, 1, 1_000_000))
    evs = log.read(_int(qs.get("after"), 0, 0, 10**12), _int(qs.get("limit"), 4000, 1, 20_000))
    return {"events": evs, "last": log.seq, "first_ts": evs[0]["ts"] if evs else None, "exists": log.path.exists()}


def _agent(srv: "DashServer", session: str) -> Any:
    rt = srv.runtime(session, create=False)
    if rt is None:
        raise DashError("Open or start a chat first: these views read the live session.", 404)
    return rt.agent


def _jsonable(obj: Any) -> Any:
    from axon.features.sdk import _jsonable as j
    return j(obj)


_EMPTY_VIEWS: dict[str, dict[str, Any]] = {
    "review": {"hunks": []}, "trail": {"calls": [], "trail": []}, "timeline": {"turns": 0, "timeline": None},
    "ctxmap": {}, "ctxdiff": {}, "whatif": {}, "pins": {"pins": []}, "compaction": {"receipt": None}, "receipts": {"receipts": []},
}


def insights_get(srv: "DashServer", name: str, qs: dict[str, str]) -> dict[str, Any]:
    """Read-only views over the live session (review, decision trail, timeline, context map, pins, what-if ...)."""
    from axon.features.dashboard import redact_text
    ws = srv.workspace
    if name == "search":
        from axon.features.session_search import search_sessions
        res = search_sessions(ws, qs.get("q", ""), qs.get("scope") == "all", _int(qs.get("limit"), 30, 1, 200))
        for h in res.get("hits", []):
            h["snippet"] = redact_text(h.get("snippet", ""))
            h.pop("path", None)                          # a session id is the handle; no filesystem paths leave the server
        return res
    if not (qs.get("session") or "").strip() and srv.runtime("", create=False) is None:
        empty = _EMPTY_VIEWS.get(name)            # no current session yet: an empty answer, not an error
        if empty is not None:
            return dict(empty)
    agent = _agent(srv, qs.get("session", ""))
    if name == "review":
        from axon.features import review
        hunks = review.list_hunks(agent.checkpoints)
        return {"hunks": [{**h.to_dict(), "path": h.rel,
                           "added": [redact_text(x) for x in h.added], "removed": [redact_text(x) for x in h.removed]} for h in hunks]}
    if name == "trail":
        from axon.features import trail
        n = _int(qs.get("n"), 0, 0, 10**6)
        return {"calls": trail.list_calls(agent.traces), "trail": _jsonable(trail.trail_for(agent.traces, n or None))}
    if name == "timeline":
        from axon.features.timeline import turn_timeline
        if not agent.traces:
            return {"turns": 0, "timeline": None}
        t = _int(qs.get("turn"), 0, 0, 10**6)
        tr = next((x for x in agent.traces if x.turn == t), agent.traces[-1]) if t else agent.traces[-1]
        return {"turns": len(agent.traces), "timeline": _jsonable(turn_timeline(tr))}
    if name == "ctxmap":
        from axon.features.ctxmap import ctxmap_data
        return _jsonable(ctxmap_data(agent))
    if name == "ctxdiff":
        from axon.features.insights import ctxdiff_data
        return _jsonable(ctxdiff_data(agent, _int(qs.get("n"), 0, 0, 10**6) or None))
    if name == "whatif":
        from axon.features.insights import whatif_data
        return _jsonable(whatif_data(agent, qs.get("models", "")))
    if name == "pins":
        from axon.features.insights import pins_data
        return _jsonable(pins_data(agent))
    if name == "compaction":
        from axon.features.insights import last_compaction
        return {"receipt": _jsonable(last_compaction(agent))}
    if name == "receipts":
        from axon.features.insights import receipts_data
        return _jsonable(receipts_data(agent))
    raise DashError("unknown view", 404)


def insights_post(srv: "DashServer", name: str, body: dict[str, Any]) -> dict[str, Any]:
    agent = _agent(srv, _s(body, "session", 300))
    if name == "review_accept" or name == "review_reject":
        from axon.features import review
        fn = review.accept_hunk if name == "review_accept" else review.reject_hunk
        res = fn(agent.checkpoints, _s(body, "id", 80))
        try:
            agent.file_state.clear()                     # the agent must re-read what a rejection changed on disk
        except Exception:
            pass
        return {"ok": res.ok, "message": res.message}
    if name == "ctxmap_drop":
        from axon.features.ctxmap import drop_result
        return _jsonable(drop_result(agent, _s(body, "id", 120)))
    if name == "pin":
        from axon.features.insights import pin_add
        return _jsonable(pin_add(agent, _s(body, "target", 400)))
    if name == "unpin":
        from axon.features.insights import pin_remove
        return _jsonable(pin_remove(agent, str(body.get("target") or "")))
    raise DashError("unknown action", 404)
