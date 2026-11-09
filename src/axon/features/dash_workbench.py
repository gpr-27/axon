"""Server side of the dashboard workbench (right panel, side panels, browser preview).

Plain functions over the live session and the project folder:

* ``context_breakdown``: where the context window goes, by category, with per-item lists. Counts are
  estimates (characters / 3.7) unless a figure says otherwise; the headline uses the provider-reported
  figure when the last call gave one.
* ``changes``: git diff --numstat of the working tree (untracked files counted by line) plus the
  facts the Create PR button needs.
* ``branches`` / ``checkout``, ``ports`` (loopback listeners owned by processes working in this
  project), ``connectors`` (MCP servers), ``usage_limits`` (real provider rate-limit data only).
* ``create_pr``: runs ``gh pr create`` only after ``confirm`` is true, and pushes only after ``push`` is true.

Every handler runs behind the HTTP layer's Host, cookie and Origin checks (see dash_api).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features import dash_fs
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer

from axon.features.workbench_data import connectors, context_breakdown, usage_limits  # noqa: E402,F401  (shared with the terminal screens)
from axon.features import worktree_view  # noqa: E402

_LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1", "*", "0.0.0.0", "[::]")


# ─── git ────────────────────────────────────────────────────────────────────

def _git(ws: Path, *args: str, timeout: float = 10.0) -> str | None:
    r = dash_fs._git(ws, *args, timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def changes(ws: Path, mode: str = "working", ref: str = "") -> dict[str, Any]:
    """worktree_view.changes (the same figures as the terminal) plus what the Create PR button needs."""
    out = worktree_view.changes(ws, mode, ref)
    out["gh"] = bool(shutil.which("gh"))
    out["upstream"], out["ahead"] = "", 0
    if out["repo"]:
        up = _git(ws, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if up:
            out["upstream"] = up.strip()
            ah = _git(ws, "rev-list", "--count", "@{u}..HEAD")
            out["ahead"] = int(ah.strip()) if ah and ah.strip().isdigit() else 0
    return out


def branches(ws: Path) -> dict[str, Any]:
    info = dash_fs.git_info(ws)
    if not info["repo"]:
        return {"repo": False, "current": "", "branches": []}
    txt = _git(ws, "for-each-ref", "refs/heads", "--format=%(refname:short)\t%(HEAD)\t%(committerdate:relative)") or ""
    rows = []
    for line in txt.splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            rows.append({"name": parts[0], "current": len(parts) > 1 and parts[1] == "*", "when": parts[2] if len(parts) > 2 else ""})
    return {"repo": True, "current": info["branch"], "branches": rows}


def checkout(ws: Path, name: str) -> dict[str, Any]:
    if name not in {b["name"] for b in branches(ws)["branches"]}:
        raise DashError("no such local branch", 404)
    r = dash_fs._git(ws, "checkout", name, "--", timeout=30)
    if r is None or r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip() if r is not None else "git is not available"
        raise DashError("git refused the switch: " + msg[:400], 409)
    dash_fs.git_info(ws, fresh=True)
    worktree_view._COUNT_CACHE.clear()
    return {"ok": True, "branch": name}


def pr_plan(ws: Path) -> dict[str, Any]:
    ch = changes(ws)
    base = ""
    ref = _git(ws, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        base = ref.strip().split("/", 1)[-1]
    return {"repo": ch["repo"], "gh": ch["gh"], "branch": ch["branch"], "upstream": ch["upstream"], "ahead": ch["ahead"], "base": base,
            "needs_push": bool(ch["repo"] and (not ch["upstream"] or ch["ahead"])), "dirty": len(ch["files"])}


def create_pr(ws: Path, body: dict[str, Any]) -> dict[str, Any]:
    plan = pr_plan(ws)
    if not plan["repo"]:
        raise DashError("This folder is not a git repository.", 409)
    if not plan["gh"]:
        raise DashError("The GitHub CLI (gh) is not installed.", 409)
    if body.get("confirm") is not True:
        raise DashError("Creating a pull request needs your confirmation.", 400)
    if plan["base"] and plan["branch"] == plan["base"]:
        raise DashError(f"You are on {plan['base']}, the branch a pull request would go into. Switch to another branch first.", 409)
    if plan["needs_push"]:
        if body.get("push") is not True:
            raise DashError("The branch has commits that are not on the remote. Confirm the push to continue.", 409)
        r = dash_fs._git(ws, "push", "-u", "origin", plan["branch"], timeout=180)
        if r is None or r.returncode != 0:
            raise DashError("git push failed: " + ((r.stderr or r.stdout).decode("utf-8", "replace").strip()[:400] if r else "git unavailable"), 502)
    title = str(body.get("title") or "").strip()[:200]
    args = ["gh", "pr", "create"] + (["--title", title, "--body", str(body.get("body") or "")[:20000]] if title else ["--fill"])
    try:
        r2 = subprocess.run(args, cwd=str(ws), capture_output=True, timeout=120, stdin=subprocess.DEVNULL, env={**os.environ, "GH_PROMPT_DISABLED": "1"})
    except (OSError, subprocess.SubprocessError) as e:
        raise DashError(f"gh failed: {type(e).__name__}", 502) from None
    text = (r2.stdout or b"").decode("utf-8", "replace").strip()
    if r2.returncode != 0:
        raise DashError("gh could not create the pull request: " + (r2.stderr or b"").decode("utf-8", "replace").strip()[:400], 502)
    m = re.search(r"https://\S+", text)
    return {"ok": True, "url": m.group(0) if m else ""}


# ─── browser: ports this project has opened ─────────────────────────────────

_PORT_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


def ports(ws: Path, own_port: int = 0) -> dict[str, Any]:
    """Loopback TCP listeners whose process works inside this project folder (lsof, so macOS and Linux)."""
    global _PORT_CACHE
    if time.time() - _PORT_CACHE[0] < 3:
        return {"ports": _PORT_CACHE[1]}
    lsof = shutil.which("lsof")
    found: list[dict[str, Any]] = []
    if lsof:
        try:
            r = subprocess.run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
            pid, cmd = 0, ""
            seen: set[int] = set()
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                if line[:1] == "p":
                    pid = int(line[1:] or 0)
                elif line[:1] == "c":
                    cmd = line[1:]
                elif line[:1] == "n":
                    host, _, port = line[1:].rpartition(":")
                    if not port.isdigit() or host not in _LOOPBACK or int(port) in seen or int(port) == own_port:
                        continue
                    if _cwd_inside(lsof, pid, ws):
                        seen.add(int(port))
                        found.append({"port": int(port), "command": cmd[:40], "pid": pid})
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    found.sort(key=lambda x: x["port"])
    _PORT_CACHE = (time.time(), found)
    return {"ports": found}


def _cwd_inside(lsof: str, pid: int, ws: Path) -> bool:
    try:
        r = subprocess.run([lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, timeout=3, stdin=subprocess.DEVNULL)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("n"):
                p = Path(line[1:]).resolve()
                w = ws.resolve()
                return p == w or w in p.parents
    except (OSError, subprocess.SubprocessError):
        pass
    return False


_LOOPBACK_URL = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})(/[^\s]*)?$")


def check_preview_url(url: str) -> dict[str, Any]:
    """Whether the Browser panel may frame this address. Only http on a loopback host with a port."""
    m = _LOOPBACK_URL.match(url.strip())
    ok = bool(m) and 0 < int(m.group(2)) < 65536
    return {"frameable": ok, "reason": "" if ok else "Only http://localhost or http://127.0.0.1 with a port can be previewed here. Other sites usually refuse to be framed, so open them in a new tab."}


# ─── connectors (MCP) ───────────────────────────────────────────────────────

def connector_action(ws: Path, name: str, action: str) -> dict[str, Any]:
    from axon.mcp.config import MCPConfig
    cfg = MCPConfig(ws)
    spec = cfg.load_specs().get(name)
    if spec is None:
        raise DashError("no such MCP server", 404)
    if action in ("enable", "disable"):
        where = cfg.toggle(name, action == "disable")
        if where is None:
            raise DashError("This server is managed by your organization and cannot be switched here.", 403)
        if action == "disable":
            try:
                from axon.mcp.bridge import get_bridge
                get_bridge(ws).disconnect_server(name)
            except Exception:
                pass
        else:
            now = cfg.load_specs().get(name)
            if now is not None and now.disabled:
                raise DashError(f"{name} is switched off inside the shared MCP file itself (\"disabled\": true). "
                                "Remove that line from the file to turn it on.", 409)
            _reconnect(ws, cfg, name)
        return {"ok": True}
    if action == "reconnect":
        _reconnect(ws, cfg, name)
        return {"ok": True}
    raise DashError("unknown action", 400)


def _reconnect(ws: Path, cfg: Any, name: str) -> None:
    from axon.mcp.bridge import get_bridge
    spec = cfg.load_specs().get(name)
    if spec is None or spec.disabled:
        return
    try:
        b = get_bridge(ws)
        b.disconnect_server(name)
        b.connect_server(spec)
    except Exception as e:
        raise DashError(f"could not reconnect: {type(e).__name__}", 502) from None



# ─── per-file diff, stage, unstage, discard ─────────────────────────────────

def file_diff(ws: Path, mode: str, ref: str, rel: str, code: str) -> dict[str, Any]:
    from axon.features.dashboard import redact_text
    d = worktree_view.file_diff(ws, rel, mode, ref, code)
    rows = [r for r in d["rows"] if r["kind"] in ("add", "del", "ctx")]
    for r, text in zip(rows, dash_fs.redact_lines([r["text"] for r in rows], dash_fs.is_secret_file(Path(rel).name))):
        r["text"] = text
    for r in d["rows"]:
        if r["kind"] not in ("add", "del", "ctx"):
            r["text"] = redact_text(r["text"])
    return d


def stage_action(ws: Path, body: dict[str, Any]) -> dict[str, Any]:
    action, rel, code = str(body.get("action") or ""), str(body.get("path") or ""), str(body.get("code") or "")
    if action == "stage":
        ok, msg = worktree_view.stage(ws, rel)
    elif action == "unstage":
        ok, msg = worktree_view.unstage(ws, rel)
    elif action == "discard":
        if body.get("confirm") is not True:
            raise DashError("Discarding changes needs your confirmation.", 400)
        ok, msg = worktree_view.discard(ws, rel, code)
    else:
        raise DashError("unknown action", 400)
    if not ok:
        raise DashError(msg[:300], 409)
    dash_fs.git_info(ws, fresh=True)
    worktree_view._COUNT_CACHE.clear()
    return {"ok": True, "message": msg}


# ─── worktrees (reuses agent.worktree for creating) ─────────────────────────

def worktrees(ws: Path) -> dict[str, Any]:
    rows = worktree_view.list_worktrees(ws)
    return {"repo": bool(rows), "worktrees": [{**w, "changes": w["files"], "missing": not Path(w["path"]).is_dir()} for w in rows]}


def worktree_create(ws: Path, name: str) -> dict[str, Any]:
    ok, msg, path = worktree_view.create_worktree(ws, name)
    if not ok:
        raise DashError(msg, 409)
    return {"ok": True, "path": path, "branch": msg.removeprefix("Created ")}


def worktree_remove(ws: Path, path: str) -> dict[str, Any]:
    ok, msg = worktree_view.remove_worktree(ws, path, force=False)   # never forced from the page
    if not ok:
        raise DashError(msg, 409)
    return {"ok": True}


# ─── extra panels with real data ────────────────────────────────────────────

def hooks_list(srv: "DashServer", session: str) -> dict[str, Any]:
    from axon.features.dashboard import redact_text
    rt = srv.runtime(session, create=False)
    agent = rt.agent if rt else None
    hm = getattr(agent, "hooks", None) if agent is not None else None
    if hm is None or not hasattr(hm, "describe"):
        from axon.hooks.manager import HookManager
        hm = HookManager(getattr(agent, "settings", None) or srv.settings_obj(), srv.workspace)
    return {"hooks": [{"event": h["event"], "matcher": h["matcher"], "type": h["type"], "source": redact_text(str(h["source"])),
                       "command": redact_text(str(h["command"]))[:200], "timeout": h.get("timeout"), "if": redact_text(str(h.get("if") or "")),
                       "async": bool(h.get("async")), "statusMessage": redact_text(str(h.get("statusMessage") or ""))[:120]}
                      for h in hm.describe()]}


# ─── the chat title menu: rename and delete (delete moves to the project trash; /restore undoes it) ──

def session_rename(srv: "DashServer", sid: str, title: str) -> dict[str, Any]:
    rt = srv.runtime(sid, create=False)
    if rt is None:
        raise DashError("no such session", 404)
    clean = " ".join(title.split())[:64]
    if not clean:
        raise DashError("The name cannot be empty.", 400)
    return {"ok": True, "title": rt.agent.session.rename_session(clean, rt.session_id)}


def session_delete(srv: "DashServer", sid: str, confirm: bool) -> dict[str, Any]:
    rt = srv.runtime(sid, create=False)
    if rt is None:
        raise DashError("no such session", 404)
    if confirm is not True:
        raise DashError("Deleting a chat needs your confirmation.", 400)
    if rt.busy:
        raise DashError("Stop the running turn before deleting this chat.", 409)
    if not rt.agent.session.delete_session(rt.session_id):
        raise DashError("This chat has nothing saved yet, or could not be moved to the trash.", 409)
    return {"ok": True, "trash": True}

# ─── routing, called from dash_api ──────────────────────────────────────────

def handle_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any | None:
    ws = srv.workspace
    if not route.startswith("/api/workbench/"):
        return None
    name = route[len("/api/workbench/"):]
    if name == "changes":
        return changes(ws, qs.get("mode", "working"), qs.get("ref", ""))
    if name == "diff":
        p = dash_fs.jail(ws, srv.extra_dirs(), qs.get("path", ""))
        return file_diff(ws, qs.get("mode", "working"), qs.get("ref", ""), dash_fs.rel_to(ws, p), qs.get("code", ""))
    if name == "worktrees":
        return worktrees(ws)
    if name == "hooks":
        return hooks_list(srv, qs.get("session", ""))
    if name == "branches":
        return branches(ws)
    if name == "pr":
        return pr_plan(ws)
    if name == "ports":
        return ports(ws, int(getattr(srv, "port", 0) or 0))
    if name == "preview_check":
        return check_preview_url(qs.get("url", ""))
    if name == "connectors":
        return connectors(ws)
    if name in ("context", "usage"):
        rt = srv.runtime(qs.get("session", ""), create=False)
        if name == "usage":
            return usage_limits(rt.agent if rt else None)
        if rt is None:
            return {"none": True}
        return context_breakdown(rt.agent, int((rt.context or {}).get("used") or 0) or None)
    raise DashError("unknown workbench view", 404)


def handle_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any | None:
    ws = srv.workspace
    if not route.startswith("/api/workbench/"):
        return None
    name = route[len("/api/workbench/"):]
    if name == "checkout":
        return checkout(ws, str(body.get("branch") or ""))
    if name == "pr":
        return create_pr(ws, body)
    if name == "session_rename":
        return session_rename(srv, str(body.get("session") or ""), str(body.get("title") or ""))
    if name == "session_delete":
        return session_delete(srv, str(body.get("session") or ""), body.get("confirm") is True)
    if name == "stage":
        dash_fs.jail(ws, srv.extra_dirs(), str(body.get("path") or ""))
        return stage_action(ws, body)
    if name == "worktree_create":
        return worktree_create(ws, str(body.get("name") or "")[:60])
    if name == "worktree_remove":
        return worktree_remove(ws, str(body.get("path") or ""))
    if name == "connector":
        return connector_action(ws, str(body.get("name") or ""), str(body.get("action") or ""))
    raise DashError("unknown workbench action", 404)
