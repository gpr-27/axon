"""Version history and per-session pins for agent definition files.

Every time an agent file is loaded (or written by a command) whose content hash differs from the last recorded
one, a version is stored under ``<axon home>/agents-history/<scope>/<name>/vNNNN.json``:
``{"v", "ts", "sha256", "text", "path", "who"}`` with ``who`` = ``user`` (seen on disk), ``edit-command``
(``/agents create`` / ``rollback``) or ``plugin``. Scopes: ``user``, ``project-<hash of the folder>``,
``plugin-<name>``. Bounded: at most ``MAX_VERSIONS`` per agent (oldest dropped) and files over ``MAX_TEXT`` bytes
are not recorded.

A session may pin a version (``agent_pins.json`` in the session folder, so ``/resume`` keeps it): a pinned agent
keeps using that text even when the file changes on disk. Nothing here is provider specific.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

MAX_VERSIONS = 50
MAX_TEXT = 256 * 1024
_LAST: dict[str, tuple[int, str]] = {}      # history key -> (latest version, its sha) seen in this process


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:80] or "_"


def scope_for(source: str, path: Path | None) -> str | None:
    """History scope of a definition, or None for built-in / session / unknown sources."""
    if source.startswith("plugin:"):
        return "plugin-" + _safe(source.split(":", 1)[1])
    if source == "user":
        return "user"
    if source == "project" and path is not None:
        root = path.parent
        for p in [path.parent, *path.parent.parents]:
            if p.name == "agents" and p.parent.name in (".claude", ".axon"):
                root = p.parent.parent
                break
        return "project-" + hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:8]
    return None


def history_dir(scope: str, name: str) -> Path:
    from axon.storage import axon_home
    return axon_home() / "agents-history" / _safe(scope) / _safe(name.lower())


def _vfile(d: Path, n: int) -> Path:
    return d / f"v{n:04d}.json"


def _numbers(d: Path) -> list[int]:
    out = []
    try:
        for f in d.glob("v*.json"):
            m = re.fullmatch(r"v(\d+)\.json", f.name)
            if m:
                out.append(int(m.group(1)))
    except OSError:
        pass
    return sorted(out)


def get_version(scope: str, name: str, n: int) -> dict[str, Any] | None:
    try:
        return json.loads(_vfile(history_dir(scope, name), n).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def versions(scope: str, name: str) -> list[dict[str, Any]]:
    d = history_dir(scope, name)
    out = [v for n in _numbers(d) if (v := get_version(scope, name, n))]
    return out


def latest(scope: str, name: str) -> dict[str, Any] | None:
    nums = _numbers(history_dir(scope, name))
    return get_version(scope, name, nums[-1]) if nums else None


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record(scope: str, name: str, text: str, path: Path | str | None, who: str) -> int:
    """Store *text* as a new version unless it equals the latest one. Returns the version number of the text (0 on failure)."""
    if len(text.encode("utf-8")) > MAX_TEXT:
        return 0
    sha = sha_of(text)
    key = f"{scope}/{name.lower()}"
    cached = _LAST.get(key)
    if cached and cached[1] == sha:
        return cached[0]
    try:
        d = history_dir(scope, name)
        cur = latest(scope, name)
        if cur and cur.get("sha256") == sha:
            _LAST[key] = (int(cur["v"]), sha)
            return int(cur["v"])
        n = (int(cur["v"]) if cur else 0) + 1
        d.mkdir(parents=True, exist_ok=True)
        rec = {"v": n, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sha256": sha, "text": text,
               "path": str(path or ""), "who": who}
        tmp = d / f".v{n:04d}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_vfile(d, n))
        for old in _numbers(d)[:-MAX_VERSIONS]:
            try:
                _vfile(d, old).unlink()
            except OSError:
                pass
        _LAST[key] = (n, sha)
        return n
    except OSError:
        return 0


def observe(source: str, name: str, text: str, path: Path | None) -> int:
    """Called when a definition file is loaded: record it when it changed. Returns its version (0: not tracked)."""
    try:
        scope = scope_for(source, path)
        if scope is None:
            return 0
        return record(scope, name, text, path, "plugin" if source.startswith("plugin:") else "user")
    except Exception:  # noqa: BLE001 - history must never break loading
        return 0


# ── diff ────────────────────────────────────────────────────────────────────

def diff_text(a: str, b: str, la: str, lb: str, limit: int = 120) -> list[str]:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), la, lb, lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more diff lines"]
    return lines


# ── pins (per session) ──────────────────────────────────────────────────────

def _pins_file(workspace: Path) -> Path:
    from axon.storage import session_dir
    return session_dir(workspace, "", create=False) / "agent_pins.json"


def load_pins(workspace: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_pins_file(Path(workspace)).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_pin(workspace: Path, name: str, scope: str, version: int | None) -> None:
    pins = load_pins(workspace)
    if version is None:
        pins.pop(name.lower(), None)
    else:
        pins[name.lower()] = {"scope": scope, "v": int(version)}
    f = _pins_file(Path(workspace))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(pins), encoding="utf-8")


def pinned_definition(workspace: Path, d: Any) -> Any:
    """The definition built from the pinned text when this session pins *d*'s agent; else *d* unchanged."""
    try:
        if not getattr(d, "path", None):
            return d
        scope = scope_for(str(d.source), Path(d.path))
        pin = load_pins(workspace).get(d.name.lower())
        if scope is None or not pin or pin.get("scope") != scope:
            return d
        rec = get_version(scope, d.name, int(pin["v"]))
        if not rec:
            return d
        from axon.agent.subagent import _definition_from_meta, parse_agent_text
        meta, body, error = parse_agent_text(rec["text"], Path(d.path).stem)
        if error:
            return d
        pd = _definition_from_meta(meta, body, d.name, str(d.source), Path(d.path), d.description)
        pd.version = int(pin["v"])
        return pd
    except Exception:  # noqa: BLE001
        return d


def pin_note(workspace: Path, d: Any) -> str:
    """One line when the session pins *d* to text that differs from the file on disk ('' otherwise)."""
    try:
        scope = scope_for(str(d.source), Path(d.path)) if getattr(d, "path", None) else None
        pin = load_pins(workspace).get(d.name.lower())
        if not scope or not pin or pin.get("scope") != scope:
            return ""
        cur = latest(scope, d.name)
        if cur and int(cur["v"]) != int(pin["v"]):
            return f"{d.name} is pinned to v{pin['v']}; the file on disk is v{cur['v']} (/agents diff {d.name} {pin['v']} {cur['v']})"
    except Exception:  # noqa: BLE001
        pass
    return ""


def version_label(d: Any) -> str:
    """``name@v3`` for a tracked file-based definition, else the plain name."""
    v = int(getattr(d, "version", 0) or 0)
    return f"{d.name}@v{v}" if v else d.name


def base_name(label: str) -> str:
    """Strip a ``@vN`` suffix from an agent type label."""
    return re.sub(r"@v\d+$", "", label or "")


# ── rollback (shared by /agents rollback and the dashboard) ─────────────────

def find_definition(workspace: Path, name: str, extra_dirs: Any = None) -> tuple[Any, str] | None:
    """The file-based definition called *name* (loading records new versions) and its history scope."""
    from axon.agent.subagent import load_agent_definitions
    d = next((x for x in load_agent_definitions(Path(workspace), extra_dirs=extra_dirs)
              if x.name.lower() == base_name(name).lower()), None)
    scope = scope_for(str(d.source), Path(d.path)) if d is not None and d.path else None
    return (d, scope) if d is not None and scope else None


def rollback(workspace: Path, d: Any, scope: str, n: int) -> int:
    """Write version *n* back to the agent's file; the current text stays as its own version. Returns the new version.
    Raises ValueError for a plugin-managed file or an unknown version."""
    if str(d.source).startswith("plugin:") or scope.startswith("plugin-"):
        raise ValueError(f"'{d.name}' is managed by a plugin ({d.source}); change it through the plugin, not here.")
    rec = get_version(scope, d.name, n)
    if rec is None:
        raise ValueError(f"Version v{n} not found.")
    path = Path(d.path)
    try:
        cur = path.read_text(encoding="utf-8")
    except OSError:
        cur = ""
    if cur:
        record(scope, d.name, cur, path, "user")
    from axon.session.store import atomic_write_text
    atomic_write_text(path, rec["text"])
    try:
        from axon.session.library import record_saved
        record_saved(path, action="edited", source="/agents rollback", workspace=workspace)
    except Exception:  # noqa: BLE001
        pass
    return record(scope, d.name, rec["text"], path, "edit-command")
