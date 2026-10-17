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
      logs/                                               axon.log, mcp-<server>.log
      cache/  paste-cache/  images/                       scratch data
      README.md                                           generated guide to this folder
      backup/<timestamp>/                                 originals kept by migrations
      projects/index.json                                 every project: path, name, last opened
      projects/<project-key>/
        project.json  README.md                           workspace path, name, git, counts
        activity.jsonl                                    append-only journal of everything
        sessions/<session-id>.jsonl                       full transcript: messages,
                                                          tool calls, results, usage, cost
        outputs/<session-id>/                             full tool / shell outputs that were
                                                          truncated for the model
        traces/<session-id>.jsonl                         per-turn traces
        checkpoints/<session-id>/                         file snapshots for /rewind
                                   (rewound/)             snapshots undone by /rewind (kept)
        memory/MEMORY.md (+ topic files)                  auto memory for this project
        todos/<session-id>.json                           todo list state
        trash/<stamp>_<session-id>/                       deleted sessions (/restore brings
                                                          them back)
        images/  pastes/  exports/  plans/  debug/        pasted images, large pastes, exports

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


def sessions_dir(workspace: Path) -> Path:
    return ensure_dir(project_dir(workspace) / "sessions")


def traces_dir(workspace: Path) -> Path:
    return ensure_dir(project_dir(workspace) / "traces")


def checkpoints_dir(workspace: Path, session_id: str | None = None) -> Path:
    d = project_dir(workspace) / "checkpoints"
    if session_id:
        d = d / session_id
    return ensure_dir(d)


def memory_dir(workspace: Path) -> Path:
    override = os.environ.get("AXON_AUTO_MEMORY_DIR", "").strip()
    if override:
        return ensure_dir(Path(os.path.expanduser(override)))
    return ensure_dir(project_dir(workspace) / "memory")


def todos_dir(workspace: Path) -> Path:
    return ensure_dir(project_dir(workspace) / "todos")


def project_subdir(workspace: Path | None, name: str) -> Path:
    """``projects/<key>/<name>`` for ``workspace`` (the current directory when None)."""
    ws = Path(workspace) if workspace is not None else Path.cwd()
    return ensure_dir(project_dir(ws) / name)


def outputs_dir(workspace: Path, session_id: str = "") -> Path:
    """Full tool/shell outputs that were truncated before reaching the model."""
    d = project_subdir(workspace, "outputs")
    return ensure_dir(d / _safe_name(session_id)) if session_id else d


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
    """Store a complete tool/shell output under ``outputs/<session>/<name>.txt`` (unique name,
    fsync'ed). Returns the path, or None when it could not be written."""
    try:
        d = outputs_dir(Path(workspace), session_id or "no-session")
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


def record_history(workspace: Path, text: str, session_id: str = "") -> None:
    """Global prompt history (~/.axon/history.jsonl), tagged with project."""
    if not text.strip():
        return
    append_jsonl(user_file("history.jsonl"), {
        "ts": _now(), "project": str(workspace), "session": session_id, "display": text,
    })


def storage_summary() -> list[dict[str, Any]]:
    """Sizes of the main storage areas for /status, /doctor and /stats."""
    home = axon_home()
    rows: list[dict[str, Any]] = []
    for name in ("projects", "logs", "cache", "usage.jsonl", "history.jsonl"):
        p = home / name
        size = 0
        count = 0
        if p.is_file():
            size, count = p.stat().st_size, 1
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        size += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
        rows.append({"name": name, "path": str(p), "bytes": size, "files": count})
    return rows


def prune_old(days: int = 30) -> int:
    """Delete sessions/traces/todos/checkpoints/paste-cache older than ``days`` (Claude-Code
    cleanupPeriodDays). ``days <= 0`` keeps everything. Returns the number of files and
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
        for pattern in ("*/sessions/*.jsonl", "*/traces/*.jsonl", "*/todos/*.json"):
            for f in root.glob(pattern):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
        for d in root.glob("*/checkpoints/*"):
            try:
                if not d.is_dir():
                    continue
                newest = max((x.stat().st_mtime for x in d.rglob("*")), default=d.stat().st_mtime)
                if newest < cutoff:
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
    "traces_dir", "checkpoints_dir", "memory_dir", "todos_dir", "logs_dir", "cache_dir",
    "user_file", "user_ext_dir", "append_jsonl", "append_line", "read_jsonl", "record_usage",
    "project_subdir", "outputs_dir", "trash_dir", "activity_path", "save_full_output",
    "record_history", "storage_summary", "prune_old", "prune_on_startup",
]
