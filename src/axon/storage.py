"""
Central on-disk layout for everything Axon persists.

All state lives under one home directory (default ``~/.axon``, override with
``AXON_HOME``), never inside the user's project unless they explicitly ask
(e.g. ``/init`` or a project-scoped memory/setting).

    ~/.axon/
      settings.json  config.toml  .env  mcp.json  AXON.md   user-level config & memory
      agents/  commands/  skills/  plugins/  rules/  output-styles/  user extensions
      history.jsonl                                       prompt history (all projects)
      usage.jsonl                                         every LLM call: tokens + cost
      mcp-auth.json  mcp-state.json  keybindings.json     MCP OAuth tokens (0600), MCP state, key bindings
      agent-memory/  plugin-data/  bin/  prompts/         subagent memory, plugin data, helpers, prompts
      logs/                                               axon.log, mcp-<server>.log
      cache/                                              regenerable scratch (reports, key checks)
      state/                                              small remembered choices (read_state/write_state)
      README.md                                           generated guide to this folder
      backup/<timestamp>/                                 originals kept by migrations
      projects/index.json                                 every project: path, name, last opened
      projects/<project-key>/
        project.json  README.md  tips.json                workspace path, name, git, counts
        activity.jsonl                                    append-only journal of everything
        memory/MEMORY.md (+ topic files)                  auto memory for this project
        plans/                                            plan-mode files
        library/                                          every version of skills, agents, ...
        trash/<stamp>_<session-id>/                       deleted sessions (/restore brings
                                                          them back)
        sessions/<session-id>/                            EVERYTHING of one chat session:
          session.jsonl                                   full transcript: messages, tool calls,
                                                          results, usage, cost
          subagents/<n>.jsonl  (<n>.trace.jsonl)          subagent transcripts (and traces)
          screen.log                                      plain-text copy of the terminal
          trace.jsonl                                     per-turn traces
          images/  pastes/                                pasted images, large pastes
          outputs/                                        full tool / shell outputs that were
                                                          truncated for the model, background
                                                          shell logs
          checkpoints/<n>/  (rewound/)                    file snapshots for /rewind
          todos.json  exports/  debug.log                 todo list, /export files, debug log
        sessions/_unattached/                             fallback for artifacts written while
                                                          no session is active (images, pastes)

A session never spreads over several folders. Older layouts (flat ``sessions/<id>.jsonl`` plus
``traces/ outputs/ checkpoints/ todos/ screen/ exports/ debug/`` next to it) are still read and are
moved into the session folder once (see :mod:`axon.session.relayout`).

Nothing is deleted automatically: ``cleanup_period_days`` defaults to 0 (keep forever).

``project_key`` is the resolved git root (or workspace) with every
non-alphanumeric character replaced by ``-`` (Claude-Code style), so it is
stable, human-readable and safe on Windows, macOS and Linux.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


def axon_home() -> Path:
    """Root directory for all Axon state (``AXON_HOME`` overrides ``~/.axon``)."""
    override = os.environ.get("AXON_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".axon"


def ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def find_git_root(start: Path) -> Path | None:
    try:
        cur = Path(start).resolve()
    except OSError:
        cur = Path(start)
    for d in (cur, *cur.parents):
        if (d / ".git").exists():
            return d
    return None


def project_key(workspace: Path) -> str:
    """Stable directory name for a workspace (git root if inside a repo)."""
    root = find_git_root(workspace) or Path(workspace)
    try:
        root = root.resolve()
    except OSError:
        pass
    key = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    if len(key) > 200:
        key = key[:200] + "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return key


def project_dir(workspace: Path, create: bool = True) -> Path:
    d = axon_home() / "projects" / project_key(workspace)
    if create:
        ensure_dir(d)
        meta = d / "project.json"
        if not meta.exists():
            try:
                meta.write_text(
                    json.dumps({"workspace": str(Path(workspace).resolve()),
                                "created": _now()}, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
    return d


def sessions_dir(workspace: Path, create: bool = True) -> Path:
    """Container of the per-session folders (``projects/<key>/sessions``)."""
    d = project_dir(workspace, create=create) / "sessions"
    return ensure_dir(d) if create else d


SESSION_FILE = "session.jsonl"
UNATTACHED = "_unattached"  # sessions/<this>/ holds artifacts written while no session is active
_SUB_RE = re.compile(r"^(?P<parent>.+)_sub_(?P<n>\d+)$")
_SID_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def split_subagent_id(session_id: str) -> tuple[str, int | None]:
    """``("<parent>", <n>)`` for a subagent id ``<parent>_sub_<n>``, else ``(session_id, None)``."""
    m = _SUB_RE.match(str(session_id or ""))
    return (m.group("parent"), int(m.group("n"))) if m else (str(session_id or ""), None)


def _sid_dirname(session_id: str) -> str:
    sid = str(session_id or "")
    return sid if _SID_RE.fullmatch(sid) and sid not in (".", "..") else _safe_name(sid or UNATTACHED)


def transcript_path(sessions_root: Path, session_id: str) -> Path:
    """Pure path of a transcript under a ``sessions/`` folder: ``<id>/session.jsonl`` or, for a
    subagent id ``<parent>_sub_<n>``, ``<parent>/subagents/<n>.jsonl``."""
    parent, n = split_subagent_id(session_id)
    d = Path(sessions_root) / _sid_dirname(parent)
    return d / "subagents" / f"{n}.jsonl" if n is not None else d / SESSION_FILE


def session_dir(workspace: Path, session_id: str = "", create: bool = True) -> Path:
    """``projects/<key>/sessions/<session-id>/``: the one folder holding everything of a session.
    A subagent id maps to its parent's folder; an empty id to the active session (see
    :func:`set_active_session`), else to the ``_unattached`` fallback folder."""
    sid = split_subagent_id(session_id)[0] if session_id else ""
    if not sid:
        sid = active_session_id()
    d = sessions_dir(workspace, create=create) / _sid_dirname(sid or UNATTACHED)
    return ensure_dir(d) if create else d


def session_subdir(workspace: Path, session_id: str, name: str) -> Path:
    """``<session folder>/<name>/`` (created): images, pastes, outputs, checkpoints, exports."""
    return ensure_dir(session_dir(workspace, session_id) / name)


def session_file(workspace: Path, session_id: str, name: str) -> Path:
    """``<session folder>/<name>``: screen.log, trace.jsonl, todos.json, debug.log. Nothing is created
    (reading must not litter empty folders); writers create the parent."""
    return session_dir(workspace, session_id, create=False) / name


_ACTIVE: dict[str, str] = {"workspace": "", "session": ""}


def set_active_session(workspace: Path | str, session_id: str) -> None:
    """Remember the chat session artifacts without an explicit id (pasted images, large pastes)
    belong to. Called by ``SessionStore`` whenever a session is opened."""
    with _LOCK:
        _ACTIVE["workspace"] = str(workspace)
        _ACTIVE["session"] = split_subagent_id(session_id)[0]


def active_session_id() -> str:
    return _ACTIVE["session"]


def active_session_subdir(name: str) -> Path:
    """``<active session folder>/<name>/`` for artifacts created without a session id (falls back
    to ``sessions/_unattached/<name>`` of the current directory's project)."""
    ws = Path(_ACTIVE["workspace"]) if _ACTIVE["workspace"] else Path.cwd()
    return session_subdir(ws, _ACTIVE["session"], name)


def trace_path(workspace: Path, session_id: str) -> Path:
    """Per-turn trace file: ``<session>/trace.jsonl`` (subagents: ``<session>/subagents/<n>.trace.jsonl``)."""
    n = split_subagent_id(session_id)[1]
    if n is None:
        return session_file(workspace, session_id, "trace.jsonl")
    return session_file(workspace, session_id, f"subagents/{n}.trace.jsonl")


def checkpoints_dir(workspace: Path, session_id: str = "") -> Path:
    return session_subdir(workspace, session_id, "checkpoints")


def todos_path(workspace: Path, session_id: str) -> Path:
    """Todo list of a session: ``<session>/todos.json`` (subagents: ``<session>/subagents/<n>.todos.json``)."""
    n = split_subagent_id(session_id)[1]
    if n is None:
        return session_file(workspace, session_id, "todos.json")
    return session_file(workspace, session_id, f"subagents/{n}.todos.json")


def memory_dir(workspace: Path) -> Path:
    override = os.environ.get("AXON_AUTO_MEMORY_DIR", "").strip()
    if override:
        return ensure_dir(Path(os.path.expanduser(override)))
    return ensure_dir(project_dir(workspace) / "memory")


def project_subdir(workspace: Path | None, name: str) -> Path:
    """``projects/<key>/<name>`` for ``workspace`` (the current directory when None)."""
    ws = Path(workspace) if workspace is not None else Path.cwd()
    return ensure_dir(project_dir(ws) / name)


def outputs_dir(workspace: Path, session_id: str = "") -> Path:
    """Full tool/shell outputs that were truncated before reaching the model, and background
    shell logs: ``<session folder>/outputs``."""
    return session_subdir(workspace, session_id, "outputs")


def trash_dir(workspace: Path) -> Path:
    """Deleted sessions (moved here, never destroyed; /restore brings them back)."""
    return project_subdir(workspace, "trash")


def activity_path(workspace: Path) -> Path:
    """Append-only activity journal of a project (see axon.session.activity)."""
    return project_dir(Path(workspace)) / "activity.jsonl"


def _safe_name(name: str, limit: int = 120) -> str:
    out = re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(name or "")).strip("._") or "item"
    return out[:limit]


def save_full_output(workspace: Path, session_id: str, name: str, text: str) -> Path | None:
    """Store a complete tool/shell output under ``sessions/<session>/outputs/<name>.txt`` (unique name,
    fsync'ed). Returns the path, or None when it could not be written."""
    try:
        d = outputs_dir(Path(workspace), session_id)
        base = _safe_name(name)
        path = d / f"{base}.txt"
        n = 2
        while path.exists():
            path = d / f"{base}-{n}.txt"
            n += 1
        with open(path, "w", encoding="utf-8", errors="replace", newline="") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        return path
    except OSError:
        return None


def relocate_recorded_path(path: str | Path) -> Path:
    """A path recorded in an old transcript (``.../outputs/<session>/<file>``) that has since been
    moved into its session folder (``.../sessions/<session>/outputs/<file>``); ``path`` itself when
    it still exists or is not such a path."""
    p = Path(path)
    if p.exists():
        return p
    parts = p.parts
    for i in range(len(parts) - 3, 0, -1):  # ... / outputs / <session> / <file...>
        if parts[i] == "outputs":
            cand = Path(*parts[:i]) / "sessions" / split_subagent_id(parts[i + 1])[0] / "outputs" / Path(*parts[i + 2:])
            if cand.exists():
                return cand
    return p


def logs_dir() -> Path:
    return ensure_dir(axon_home() / "logs")


def cache_dir(name: str = "") -> Path:
    d = axon_home() / "cache"
    return ensure_dir(d / name if name else d)


def user_file(name: str) -> Path:
    """A file directly under the Axon home (settings.json, .env, mcp.json, ...)."""
    return axon_home() / name


def user_ext_dir(kind: str) -> Path:
    """User-level extension dirs: agents, commands, skills, plugins, rules, output-styles."""
    return axon_home() / kind


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_line(path: Path, line: str, *, fsync: bool = True) -> None:
    """Append one line in append mode (safe with concurrent writers) and flush it.

    If the file does not end with a newline (a writer crashed mid-line), a newline is written
    first so the torn line stays isolated and this record is not glued onto it. Raises OSError."""
    data = line.rstrip("\n").encode("utf-8", errors="replace") + b"\n"
    with open(path, "a+b") as f:
        try:
            end = f.seek(0, os.SEEK_END)
            if end > 0:
                f.seek(end - 1)
                if f.read(1) != b"\n":
                    data = b"\n" + data
        except OSError:
            pass
        f.write(data)
        f.flush()
        if fsync:
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Thread-safe, crash-tolerant JSONL append (one fsync'd line per record)."""
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        ensure_dir(path.parent)
        try:
            append_line(path, line)
        except OSError:
            pass


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Read JSONL, skipping corrupt lines. ``limit`` keeps only the last N records."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out[-limit:] if limit else out


def record_usage(
    *,
    workspace: Path,
    session_id: str,
    model: str,
    source: str,
    usage: dict[str, int],
    cost: float,
    duration_s: float | None = None,
) -> None:
    """Global per-call usage log (~/.axon/usage.jsonl) used by /stats and /cost --all."""
    append_jsonl(user_file("usage.jsonl"), {
        "ts": _now(),
        "project": project_key(workspace),
        "workspace": str(workspace),
        "session": session_id,
        "model": model,
        "source": source,
        "usage": usage,
        "cost": round(float(cost), 8),
        "duration_s": duration_s,
    })


def history_project(path: Path | str) -> Path:
    """The folder a prompt belongs to: its git root, else the folder itself. Prompts typed in any chat
    of a project, from the root or from a subfolder, share one history.

    A git repository that sits in your home folder (or above it) is not a project: every folder below it
    would share one history. Such folders count as their own project."""
    start = Path(path)
    root = find_git_root(start)
    if root is None or _is_home_or_above(root):
        return start
    return root


def _is_home_or_above(folder: Path) -> bool:
    try:
        home = Path.home().resolve()
        return folder.resolve() in (home, *home.parents)
    except OSError:
        return False


def store_paste(text: str) -> str:
    """Keep a large pasted text once, by content, under cache/pastes/. Returns its id (a hash prefix)."""
    paste_id = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    target = ensure_dir(axon_home() / "cache" / "pastes") / f"{paste_id}.txt"
    if not target.exists():
        try:
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except OSError:
            pass
    return paste_id


def load_paste(paste_id: str) -> str | None:
    """The text stored by :func:`store_paste`, or None when it is gone."""
    if not re.fullmatch(r"[0-9a-f]{16}", paste_id or ""):
        return None
    try:
        return (axon_home() / "cache" / "pastes" / f"{paste_id}.txt").read_text(encoding="utf-8")
    except OSError:
        return None


def record_history(workspace: Path, text: str, session_id: str = "", pastes: dict[int, str] | None = None) -> None:
    """Global prompt history (~/.axon/history.jsonl), tagged with the project root.

    *text* is what was typed, with ``[Pasted text #N +M lines]`` chips; *pastes* maps each chip number to its
    full text, which is stored once under cache/pastes/ and only referenced from the history record."""
    if not text.strip():
        return
    record: dict[str, Any] = {"ts": _now(), "project": str(history_project(workspace)),
                              "session": session_id, "display": text}
    if pastes:
        record["pastes"] = {str(n): store_paste(content) for n, content in pastes.items()}
    append_jsonl(user_file("history.jsonl"), record)


# ---------------------------------------------------------------------------------------------
# What every entry of the home / project / session folders is. Drives the /storage tree, the
# "unknown entry" flags and the secret-file permissions; keep in step with render_home_readme().
# ---------------------------------------------------------------------------------------------

HOME_ENTRIES: dict[str, str] = {
    "README.md": "generated guide to this folder",
    "settings.json": "user settings", "settings.local.json": "user settings, machine-local",
    "config.toml": "provider, model and endpoint config", ".env": "API keys (owner-only)",
    "mcp.json": "MCP servers", "mcp-auth.json": "MCP OAuth tokens (owner-only)",
    "mcp-state.json": "MCP enable/disable state", "keybindings.json": "key bindings",
    "statusline.py": "custom status line", "AXON.md": "user-wide instructions",
    "memory": "user-wide memory", "rules": "user-wide rules", "agents": "user subagents",
    "commands": "user slash commands", "skills": "user skills", "plugins": "installed plugins",
    "plugin-data": "per-plugin data", "output-styles": "output styles", "prompts": "prompt snippets",
    "agent-memory": "subagent memory", "bin": "helper binaries", "history.jsonl": "every prompt typed",
    "usage.jsonl": "every model call: tokens and cost", "logs": "axon and MCP logs",
    "cache": "regenerable scratch data", "state": "small remembered choices",
    "bug-reports": "saved bug reports", "backup": "originals kept by migrations",
    "migrations": "migration markers", "projects": "per-project data",
}
LEGACY_HOME_ENTRIES: dict[str, str] = {
    "sessions": "legacy flat transcripts (moved into projects/ at startup)",
    "history": "legacy plain-text prompt history",
    "images": "legacy pasted images", "paste-cache": "legacy large pastes",
    "exports": "legacy /export files", "debug": "legacy debug logs", "research": "legacy research notes",
    "traces": "legacy traces", "outputs": "legacy tool outputs", "checkpoints": "legacy checkpoints",
}
PROJECT_ENTRIES: dict[str, str] = {
    "project.json": "name, workspace, git", "README.md": "generated guide", "tips.json": "cached tips",
    "activity.jsonl": "append-only journal", "sessions": "one folder per chat session",
    "memory": "auto memory", "plans": "plan-mode files", "trash": "deleted sessions (restorable)",
    "library": "every version of skills, agents, commands, memory",
}
SESSION_ENTRIES: dict[str, str] = {
    "session.jsonl": "full transcript", "subagents": "subagent transcripts and traces",
    "screen.log": "terminal copy", "trace.jsonl": "per-turn traces", "outputs": "full tool/shell outputs",
    "checkpoints": "file snapshots for /rewind", "images": "pasted images", "pastes": "large pastes",
    "todos.json": "todo list", "exports": "/export files", "debug.log": "--debug log",
    "legacy": "files kept by relayout when names clashed",
}
SECRET_HOME_FILES = (".env", "mcp-auth.json", "settings.local.json")


def state_dir() -> Path:
    """``~/.axon/state``: small remembered choices (last model, dismissed hints, ...)."""
    return ensure_dir(axon_home() / "state")


def read_state(name: str, default: Any = None) -> Any:
    """JSON value remembered under ``state/<name>.json`` (``default`` when missing or unreadable)."""
    try:
        return json.loads((axon_home() / "state" / f"{_safe_name(name)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_state(name: str, value: Any) -> bool:
    """Remember ``value`` under ``state/<name>.json`` atomically. False when it could not be written."""
    path = state_dir() / f"{_safe_name(name)}.json"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        tmp.unlink(missing_ok=True)
        return False


def secure_home() -> list[str]:
    """Owner-only permissions where secrets can live: the home folder 0700 and key/token files
    0600. Returns what was changed. No-op on Windows; never raises."""
    changed: list[str] = []
    if os.name == "nt":
        return changed
    home = axon_home()
    targets: list[tuple[Path, int]] = [(home, 0o700)]
    targets += [(home / n, 0o600) for n in SECRET_HOME_FILES]
    for p, mode in targets:
        try:
            if p.exists() and p.stat().st_mode & 0o077:  # only ever tighten, never loosen
                os.chmod(p, mode)
                changed.append(str(p))
        except OSError:
            pass
    return changed


def ensure_home_layout() -> None:
    """Create the always-present folders (logs, cache, state) and tighten secret permissions."""
    for sub in ("logs", "cache", "state"):
        ensure_dir(axon_home() / sub)
    secure_home()


def _tree_size(p: Path) -> tuple[int, int]:
    if p.is_symlink():
        return 0, 0
    try:
        if p.is_file():
            return p.stat().st_size, 1
    except OSError:
        return 0, 0
    size = count = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    size += f.stat().st_size
                    count += 1
            except OSError:
                pass
    except OSError:
        pass
    return size, count


def _classify(name: str, known: dict[str, str], legacy: dict[str, str] | None) -> tuple[str, str]:
    """Return (status, note) for one folder entry: known, legacy, temp or unknown."""
    if name.endswith(".tmp") or name.endswith(".migrating"):
        return "temp", "leftover temporary file"
    if name in known:
        return "known", known[name]
    if legacy and name in legacy:
        return "legacy", legacy[name]
    if name.startswith("_"):
        return "known", "internal folder"
    return "unknown", "not created by Axon"


def _entries(d: Path, known: dict[str, str], legacy: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One row per child of ``d`` with its size and classification."""
    try:
        children = sorted(d.iterdir(), key=lambda x: x.name.lower())
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for child in children:
        status, note = _classify(child.name, known, legacy)
        size, files = _tree_size(child)
        rows.append({"name": child.name, "path": str(child), "dir": child.is_dir(), "bytes": size,
                     "files": files, "status": status, "note": note})
    return rows


def _session_orphans(sessions_root: Path) -> list[dict[str, Any]]:
    """Unknown or temp entries inside the session folders of one project."""
    orphans: list[dict[str, Any]] = []
    if not sessions_root.is_dir():
        return orphans
    for session in sorted(x for x in sessions_root.iterdir() if x.is_dir()):
        for row in _entries(session, SESSION_ENTRIES):
            if row["status"] in ("unknown", "temp"):
                orphans.append(dict(row, session=session.name))
    return orphans


def _project_row(project: Path) -> dict[str, Any]:
    size, files = _tree_size(project)
    return {"key": project.name, "path": str(project), "bytes": size, "files": files,
            "entries": _entries(project, PROJECT_ENTRIES),
            "session_orphans": _session_orphans(project / "sessions")}


def _exposed_secrets(home: Path) -> list[str]:
    """Secret files that other users can read (always empty on Windows)."""
    exposed: list[str] = []
    if os.name == "nt":
        return exposed
    for name in SECRET_HOME_FILES:
        path = home / name
        try:
            if path.is_file() and path.stat().st_mode & 0o077:
                exposed.append(str(path))
        except OSError:
            pass
    return exposed


def storage_tree() -> dict[str, Any]:
    """Everything under the Axon home with sizes; each entry is marked known, legacy, temp or
    unknown (an orphan). Also lists secret files readable by others."""
    home = axon_home()
    top = _entries(home, HOME_ENTRIES, LEGACY_HOME_ENTRIES)
    projects_root = home / "projects"
    project_dirs: list[Path] = []
    if projects_root.is_dir():
        project_dirs = sorted(x for x in projects_root.iterdir() if x.is_dir())
    return {"home": str(home), "entries": top, "projects": [_project_row(p) for p in project_dirs],
            "exposed_secrets": _exposed_secrets(home), "bytes": sum(r["bytes"] for r in top)}


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


_STATUS_FLAGS = {"unknown": "  [UNKNOWN - not created by Axon]", "legacy": "  [legacy]", "temp": "  [temp leftover]"}


def _entry_line(row: dict[str, Any], indent: str, width: int) -> str:
    name = row["name"] + ("/" if row["dir"] else "")
    flag = _STATUS_FLAGS.get(row["status"], "")
    return f"{indent}{name:<{width}} {_fmt_bytes(row['bytes']):>9}  {row['note']}{flag}"


def render_storage_tree(tree: dict[str, Any] | None = None) -> str:
    """Plain-text tree for ``/storage`` and ``/doctor``: sizes, notes and flags for orphans."""
    t = tree if tree is not None else storage_tree()
    lines = [f"{t['home']}  ({_fmt_bytes(t['bytes'])})"]
    for row in t["entries"]:
        lines.append(_entry_line(row, "  ", 22))
    for project in t["projects"]:
        lines.append(f"  projects/{project['key']}  ({_fmt_bytes(project['bytes'])}, {project['files']} files)")
        for row in project["entries"]:
            lines.append(_entry_line(row, "    ", 20))
        for row in project["session_orphans"]:
            flag = _STATUS_FLAGS.get(row["status"], "")
            lines.append(f"    sessions/{row['session']}/{row['name']}  {_fmt_bytes(row['bytes'])}{flag}")
    for path in t["exposed_secrets"]:
        lines.append(f"! {path} is readable by other users - run: chmod 600 {path}")
    return "\n".join(lines)


_SUMMARY_ALWAYS = ("projects", "logs", "cache", "usage.jsonl", "history.jsonl")


def storage_summary() -> list[dict[str, Any]]:
    """Sizes of every top-level entry of the Axon home (for /status, /doctor and /stats). The main
    areas are listed even when they do not exist yet."""
    home = axon_home()
    rows = _entries(home, HOME_ENTRIES, LEGACY_HOME_ENTRIES)
    present = {r["name"] for r in rows}
    for name in _SUMMARY_ALWAYS:
        if name not in present:
            rows.append({"name": name, "path": str(home / name), "dir": False, "bytes": 0, "files": 0,
                         "status": "known", "note": HOME_ENTRIES[name]})
    return rows


def _newest_mtime(d: Path) -> float:
    try:
        return max((x.stat().st_mtime for x in d.rglob("*")), default=d.stat().st_mtime)
    except OSError:
        return time.time()


def prune_old(days: int = 30) -> int:
    """Delete whole session folders (and leftovers of the older flat layout) older than ``days``
    (Claude-Code cleanupPeriodDays). ``days <= 0`` keeps everything. Returns the number of files and
    directories removed. Only runs when the user opted in (cleanup_period_days > 0) or
    explicitly; the activity journal, memory and trash are never pruned."""
    import shutil
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    root = axon_home() / "projects"
    if root.is_dir():
        for d in root.glob("*/sessions/*"):
            try:
                # a session is as old as its newest file (the transcript is appended on every turn)
                if d.is_dir() and not d.name.startswith("_") and _newest_mtime(d) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        for pattern in ("*/sessions/*.jsonl", "*/traces/*.jsonl", "*/todos/*.json"):  # flat layout
            for f in root.glob(pattern):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        for d in root.glob("*/checkpoints/*"):
            try:
                if d.is_dir() and _newest_mtime(d) < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
    paste = axon_home() / "paste-cache"
    if paste.is_dir():
        for f in paste.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


_PRUNE_STAMP = "last-cleanup"


def prune_on_startup(days: int = 0, min_interval_s: float = 86400.0) -> int:
    """Run :func:`prune_old` at most once per ``min_interval_s`` (stamp file in the cache dir).
    ``days <= 0`` (the default) never deletes anything."""
    try:
        if int(days or 0) <= 0:
            return 0
    except (TypeError, ValueError):
        return 0
    stamp = cache_dir() / _PRUNE_STAMP
    try:
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < min_interval_s:
            return 0
    except OSError:
        pass
    removed = prune_old(days)
    try:
        stamp.write_text(_now(), encoding="utf-8")
    except OSError:
        pass
    return removed


__all__ = [
    "axon_home", "ensure_dir", "find_git_root", "project_key", "project_dir", "sessions_dir",
    "session_dir", "session_subdir", "session_file", "transcript_path", "split_subagent_id",
    "set_active_session", "active_session_id", "active_session_subdir", "trace_path", "todos_path",
    "SESSION_FILE", "UNATTACHED", "checkpoints_dir", "memory_dir", "logs_dir", "cache_dir",
    "user_file", "user_ext_dir", "append_jsonl", "append_line", "read_jsonl", "record_usage",
    "project_subdir", "outputs_dir", "trash_dir", "activity_path", "save_full_output",
    "record_history", "storage_summary", "prune_old", "prune_on_startup",
    "HOME_ENTRIES", "LEGACY_HOME_ENTRIES", "PROJECT_ENTRIES", "SESSION_ENTRIES", "SECRET_HOME_FILES",
    "state_dir", "read_state", "write_state", "secure_home", "ensure_home_layout", "storage_tree",
    "render_storage_tree",
]
