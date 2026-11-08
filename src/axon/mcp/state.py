"""
Per-project MCP state that must not live in the (possibly shared) project files.

``~/.axon/mcp-state.json`` remembers, per workspace:

* which servers you switched off for this project (``disabledMcpServers``), so toggling a team's
  ``.mcp.json`` server in ``/mcp`` never edits the committed file;
* which ``.mcp.json`` servers you approved or rejected (Claude Code asks before it launches a program a
  cloned repository names), and whether you approved all of them at once.

It also tracks whether this process may ask you questions (approval prompts, elicitation dialogs):
``axon -p`` and SDK runs never block on a prompt.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_session: dict[str, Any] = {"interactive": None}


# ─── interactive flag ───────────────────────────────────────────────────────

def set_interactive(flag: bool | None) -> None:
    """Called by the CLI: True for the REPL, False for -p / SDK runs, None = not set (treated as False,
    so a library or server embedding Axon never blocks on a prompt)."""
    _session["interactive"] = flag


def is_interactive() -> bool:
    return bool(_session["interactive"])


def set_page_mode(flag: bool) -> None:
    """The web app is the interactive surface (approvals and questions appear on the page): same decisions as
    the REPL, but nothing may wait on the terminal's stdin."""
    _session["page"] = bool(flag)


def can_prompt() -> bool:
    """May a question be asked on the terminal (input())? Only in the REPL, never behind the web app."""
    return bool(_session["interactive"]) and not _session.get("page")


def terminal_attached() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


# ─── state file ─────────────────────────────────────────────────────────────

def state_path() -> Path:
    from axon.storage import user_file
    return user_file("mcp-state.json")


def _key(workspace: Path | str) -> str:
    try:
        return str(Path(workspace).resolve())
    except OSError:
        return str(workspace)


def _read() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def project_state(workspace: Path | str) -> dict[str, Any]:
    with _lock:
        proj = (_read().get("projects") or {}).get(_key(workspace))
        return dict(proj) if isinstance(proj, dict) else {}


def _update(workspace: Path | str, fn: Any) -> None:
    with _lock:
        data = _read()
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
        proj = projects.get(_key(workspace))
        proj = dict(proj) if isinstance(proj, dict) else {}
        fn(proj)
        if proj:
            projects[_key(workspace)] = proj
        else:
            projects.pop(_key(workspace), None)
        data["projects"] = projects
        _write(data)


def _names(proj: dict[str, Any], key: str) -> list[str]:
    value = proj.get(key)
    return [str(x) for x in value] if isinstance(value, list) else []


# ─── disabled servers ───────────────────────────────────────────────────────

def disabled_servers(workspace: Path | str) -> set[str]:
    return set(_names(project_state(workspace), "disabledMcpServers"))


def set_server_disabled(workspace: Path | str, name: str, disabled: bool) -> None:
    def fn(proj: dict[str, Any]) -> None:
        names = [n for n in _names(proj, "disabledMcpServers") if n != name]
        if disabled:
            names.append(name)
        if names:
            proj["disabledMcpServers"] = sorted(names)
        else:
            proj.pop("disabledMcpServers", None)
    _update(workspace, fn)


# ─── .mcp.json approvals ────────────────────────────────────────────────────

def project_choice(workspace: Path | str, name: str) -> str | None:
    """'approved', 'rejected' or None (undecided) for a project-scoped (.mcp.json) server."""
    proj = project_state(workspace)
    if name in _names(proj, "rejectedMcpjsonServers"):
        return "rejected"
    if proj.get("enableAllProjectMcpServers") or name in _names(proj, "approvedMcpjsonServers"):
        return "approved"
    return None


def set_project_choice(workspace: Path | str, name: str, choice: str | None) -> None:
    def fn(proj: dict[str, Any]) -> None:
        for key in ("approvedMcpjsonServers", "rejectedMcpjsonServers"):
            names = [n for n in _names(proj, key) if n != name]
            if (choice == "approved" and key == "approvedMcpjsonServers") or \
                    (choice == "rejected" and key == "rejectedMcpjsonServers"):
                names.append(name)
            if names:
                proj[key] = sorted(names)
            else:
                proj.pop(key, None)
    _update(workspace, fn)


def approve_all(workspace: Path | str, enabled: bool = True) -> None:
    def fn(proj: dict[str, Any]) -> None:
        if enabled:
            proj["enableAllProjectMcpServers"] = True
        else:
            proj.pop("enableAllProjectMcpServers", None)
    _update(workspace, fn)


def reset_project_choices(workspace: Path | str) -> int:
    """Forget every approval and rejection for this project. Returns how many entries were dropped."""
    dropped = 0

    def fn(proj: dict[str, Any]) -> None:
        nonlocal dropped
        for key in ("approvedMcpjsonServers", "rejectedMcpjsonServers"):
            dropped += len(_names(proj, key))
            proj.pop(key, None)
        if proj.pop("enableAllProjectMcpServers", None):
            dropped += 1
    _update(workspace, fn)
    return dropped
