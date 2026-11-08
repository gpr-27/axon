"""
Folder trust: project hooks, project plugins and project MCP servers run programs, so a folder you have
not trusted must not start any of them.

What counts as executable in a folder (everything a ``git clone`` could have delivered):

* ``hooks`` in ``<ws>/.claude|.axon/settings[.local].json`` and ``<ws>/.axon/config.toml``;
* hooks and MCP servers of every enabled plugin under ``<ws>/.axon/plugins``;
* servers in ``<ws>/.axon/mcp.json`` (``<ws>/.mcp.json`` keeps its own per-server approval prompt).

The decision lives in ``$AXON_HOME/trusted-folders.json`` as ``{folder: {"hash": ...}}``. The hash covers
the full list of commands, so a ``git pull`` that adds or edits one makes the folder untrusted again
until you look at the new list. User-scope config (``~/.axon``) is never gated.

Modes (set once at startup by the CLI via :func:`configure`):

* interactive REPL: untrusted means "do not load"; :func:`prompt_if_needed` asks once and lists everything;
* ``-p`` / SDK (default): running in a folder non-interactively is an explicit choice, so it loads as before;
  ``trust.requireForNonInteractive: true`` in ``~/.axon/settings.json`` (or ``AXON_TRUST_REQUIRE=1``)
  refuses until the folder is trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

STORE_NAME = "trusted-folders.json"
_PROJECT_SETTINGS = (".claude/settings.json", ".axon/settings.json",
                     ".claude/settings.local.json", ".axon/settings.local.json")
_PROJECT_MCP = (".axon/mcp.json",)     # <ws>/.mcp.json has its own per-server approval (axon.mcp.approval)

_state: dict[str, Any] = {"interactive": False, "require": None}
_session_granted: dict[str, str] = {}      # folder -> hash granted for this process only


@dataclass(frozen=True)
class Item:
    kind: str       # hook | mcp | plugin-hook | plugin-mcp
    source: str     # file (relative to the folder) or plugin name
    detail: str     # the full command / URL

    def line(self) -> str:
        return f"{self.kind:<11} {self.source}: {self.detail}"


# ─── configuration ───────────────────────────────────────────────────────────

def configure(interactive: bool, require_non_interactive: bool | None = None) -> None:
    _state["interactive"] = bool(interactive)
    _state["require"] = require_non_interactive


def is_interactive() -> bool:
    return bool(_state["interactive"])


def require_for_non_interactive() -> bool:
    """``trust.requireForNonInteractive`` from the user's own settings (a repository cannot loosen it)."""
    if _state["require"] is not None:
        return bool(_state["require"])
    if os.environ.get("AXON_TRUST_REQUIRE", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        from axon import storage
        data = json.loads(storage.user_file("settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    trust = data.get("trust") if isinstance(data, dict) else None
    if isinstance(trust, dict):
        return trust.get("requireForNonInteractive") is True
    return bool(isinstance(data, dict) and data.get("trust.requireForNonInteractive") is True)


def reset_session() -> None:
    _session_granted.clear()


# ─── what would run ──────────────────────────────────────────────────────────

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _describe_handler(h: dict[str, Any]) -> str:
    htype = str(h.get("type", "command") or "command")
    if htype == "command":
        cmd = str(h.get("command") or "").strip()
        args = h.get("args")
        if isinstance(args, list) and args:
            cmd += " " + " ".join(str(a) for a in args)
        return cmd
    if htype == "http":
        return f"POST {h.get('url', '')}"
    if htype == "mcp_tool":
        return f"mcp tool {h.get('server', '')}:{h.get('tool', '')}"
    return f"{htype} hook: {str(h.get('prompt') or '')[:200]}"


def hook_items(section: Any, source: str, kind: str = "hook") -> list[Item]:
    from axon.hooks.manager import _iter_groups, normalize_event
    out: list[Item] = []
    if not isinstance(section, dict):
        return out
    for raw_event, event_cfg in section.items():
        event = normalize_event(str(raw_event))
        for matcher, handlers in _iter_groups(event_cfg):
            for h in handlers:
                text = _describe_handler(h)
                if text:
                    m = f"[{matcher}] " if matcher else ""
                    out.append(Item(kind, source, f"{event} {m}{text}"))
    return out


def _mcp_items(servers: Any, source: str, kind: str = "mcp") -> list[Item]:
    out: list[Item] = []
    if not isinstance(servers, dict):
        return out
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        if raw.get("url"):
            target = f"{raw.get('type') or 'http'} {raw['url']}"
        else:
            target = " ".join([str(raw.get("command", ""))] + [str(a) for a in raw.get("args") or []]).strip()
        out.append(Item(kind, source, f"server {name}: {target}"))
    return out


def collect(workspace: Path | str) -> list[Item]:
    """Everything in the folder that would start a program or contact a server, with the full text."""
    ws = Path(workspace)
    items: list[Item] = []
    for rel in _PROJECT_SETTINGS:
        data = _read_json(ws / rel)
        if isinstance(data, dict):
            items += hook_items(data.get("hooks"), rel)
    toml_path = ws / ".axon" / "config.toml"
    if toml_path.is_file():
        try:
            import tomllib
            tdata = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            items += hook_items(tdata.get("hooks"), ".axon/config.toml")
        except Exception:  # noqa: BLE001 - an unreadable file defines nothing
            pass
    for rel in _PROJECT_MCP:
        data = _read_json(ws / rel)
        if isinstance(data, dict):
            servers = data.get("mcpServers") if "mcpServers" in data else data.get("servers")
            items += _mcp_items(servers, rel)
    items += _project_plugin_items(ws)
    return items


def _project_plugin_items(ws: Path) -> list[Item]:
    if not (ws / ".axon" / "plugins").is_dir():
        return []
    try:
        from axon.commands import plugins as pl
        found = [p for p in pl.load_plugins(ws) if p.scope == "project" and p.enabled]
    except Exception:  # noqa: BLE001
        return []
    items: list[Item] = []
    for plug in found:
        try:
            items += hook_items(pl.plugin_hooks(plug, ws), f"plugin {plug.name}", "plugin-hook")
            items += _mcp_items(pl.plugin_mcp_servers(plug, ws), f"plugin {plug.name}", "plugin-mcp")
        except Exception:  # noqa: BLE001
            continue
    return items


def fingerprint(items: list[Item]) -> str:
    rows = sorted([i.kind, i.source, i.detail] for i in items)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


# ─── the store ───────────────────────────────────────────────────────────────

def store_path() -> Path:
    from axon import storage
    return storage.user_file(STORE_NAME)


def _key(workspace: Path | str) -> str:
    try:
        return str(Path(workspace).resolve())
    except OSError:
        return str(workspace)


def load_store() -> dict[str, dict[str, Any]]:
    data = _read_json(store_path())
    folders = data.get("folders") if isinstance(data, dict) else None
    return {str(k): v for k, v in folders.items() if isinstance(v, dict)} if isinstance(folders, dict) else {}


def _save_store(folders: dict[str, dict[str, Any]]) -> None:
    from axon.session.store import atomic_write_text
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({"version": 1, "folders": folders}, indent=2, sort_keys=True) + "\n")


def status(workspace: Path | str, items: list[Item] | None = None) -> str:
    """'none' (nothing executable), 'trusted', 'changed' (was trusted, the list differs) or 'untrusted'."""
    items = collect(workspace) if items is None else items
    if not items:
        return "none"
    key, fp = _key(workspace), fingerprint(items)
    if _session_granted.get(key) == fp:
        return "trusted"
    entry = load_store().get(key)
    if entry is None:
        return "untrusted"
    return "trusted" if entry.get("hash") == fp else "changed"


def trust(workspace: Path | str, items: list[Item] | None = None, *, persist: bool = True) -> str:
    items = collect(workspace) if items is None else items
    key, fp = _key(workspace), fingerprint(items)
    if persist:
        folders = load_store()
        folders[key] = {"hash": fp, "trusted_at": int(time.time()), "items": len(items)}
        _save_store(folders)
    else:
        _session_granted[key] = fp
    return fp


def revoke(workspace: Path | str) -> bool:
    key = _key(workspace)
    _session_granted.pop(key, None)
    folders = load_store()
    if key not in folders:
        return False
    del folders[key]
    _save_store(folders)
    return True


def trusted_folders() -> dict[str, dict[str, Any]]:
    return load_store()


# ─── the gate used by the loaders ────────────────────────────────────────────

def allowed(workspace: Path | str) -> bool:
    """May project hooks, project plugins' hooks/servers and project MCP servers load for this folder?"""
    items = collect(workspace)
    if not items:
        return True
    if not is_interactive() and not require_for_non_interactive():
        return True
    return status(workspace, items) == "trusted"


def blocked_line(workspace: Path | str) -> str:
    """One line for the user when something is being held back; '' when nothing is."""
    items = collect(workspace)
    if not items or allowed(workspace):
        return ""
    state = status(workspace, items)
    why = "its hooks/plugins/MCP servers changed since you trusted it" if state == "changed" else "it is not trusted"
    return (f"Project hooks, plugins and MCP servers ({len(items)} item(s)) are not loaded because {why}. "
            f"Run /trust to review and allow.")


def render_items(items: list[Item]) -> list[str]:
    return [f"    {i.line()}" for i in items]


def prompt_if_needed(workspace: Path | str, *, ask: Callable[[str], str] | None = None,
                     out: Callable[[str], None] | None = None) -> str:
    """Interactive start-up check. Returns 'none' | 'trusted' | 'granted' | 'declined'."""
    items = collect(workspace)
    state = status(workspace, items)
    if state in ("none", "trusted"):
        return state
    ask = ask or input
    out = out or print
    out("")
    if state == "changed":
        out("  The executable project configuration of this folder changed since you trusted it.")
    out(f"  Axon will not run anything from {workspace} until you trust it. This folder would run:")
    for line in render_items(items):
        out(line)
    try:
        answer = ask("  Trust this folder and run all of the above? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer in ("y", "yes"):
        trust(workspace, items)
        return "granted"
    out("  Not trusted: project hooks, plugins and MCP servers stay off. Axon works as usual; /trust changes this.")
    return "declined"


@contextmanager
def keep_trust(workspace: Path | str) -> Iterator[None]:
    """Around an explicit install into the project: if the folder was fine before (trusted, or nothing
    executable), the user's own install does not make it untrusted."""
    was_ok = status(workspace) in ("none", "trusted")
    yield
    if was_ok and collect(workspace):
        trust(workspace)


# ─── /trust ──────────────────────────────────────────────────────────────────

USAGE = """/trust                 this folder: what would run and whether it is trusted
/trust yes             trust this folder (lists what would run, then asks)
/trust revoke [path]   stop trusting this folder (or another one)
/trust list            every trusted folder"""


def run_command(arg: str, workspace: Path | str, *, ask: Callable[[str], str] | None = None,
                out: Callable[[str], None] = print) -> bool:
    """Handle ``/trust``. Returns True when the trust state changed (the caller reloads hooks)."""
    words = arg.split()
    verb = words[0].lower() if words else "show"
    if verb in ("list", "ls"):
        folders = load_store()
        if not folders:
            out("  No trusted folders.")
        for path, entry in sorted(folders.items()):
            when = time.strftime("%Y-%m-%d", time.localtime(float(entry.get("trusted_at", 0) or 0)))
            out(f"  {path}  ({entry.get('items', '?')} item(s), trusted {when})")
        return False
    if verb in ("revoke", "remove", "forget", "no"):
        target = Path(" ".join(words[1:])).expanduser() if len(words) > 1 else Path(workspace)
        if revoke(target):
            out(f"  Revoked trust for {target}. Its project hooks, plugins and MCP servers load no more (restart to unload MCP servers).")
            return True
        out(f"  {target} was not trusted.")
        return False
    items = collect(workspace)
    state = status(workspace, items)
    if verb in ("yes", "grant", "trust", "allow"):
        if state == "none":
            out("  Nothing in this folder runs programs, so there is nothing to trust.")
            return False
        if state == "trusted":
            out("  Already trusted.")
            return False
        out("  This folder would run:")
        for line in render_items(items):
            out(line)
        try:
            answer = (ask or input)("  Trust this folder and run all of the above? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer in ("y", "yes"):
            trust(workspace, items)
            out("  Trusted. Project hooks are active now; restart Axon to start project MCP servers.")
            return True
        out("  Not trusted.")
        return False
    label = {"none": "nothing executable in this folder", "trusted": "trusted", "untrusted": "NOT trusted",
             "changed": "NOT trusted (the list changed since you trusted it)"}[state]
    out(f"  {workspace}: {label}")
    if items:
        for line in render_items(items):
            out(line)
    if state in ("untrusted", "changed"):
        out("  /trust yes   trust it")
    elif state == "trusted":
        out("  /trust revoke   stop trusting it")
    return False
