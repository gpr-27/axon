"""
One-time move of the older flat storage layout into one folder per chat session.

Before, a session was spread over ``sessions/<id>.jsonl``, ``sessions/<id>_sub_<n>.jsonl``,
``traces/<id>.jsonl``, ``outputs/<id>/``, ``checkpoints/<id>/``, ``todos/<id>.json``,
``screen/<stamp>-<id>.log``, ``exports/<id>.<fmt>`` and ``debug/<id>.log``. Now everything of a
session lives in ``projects/<key>/sessions/<id>/`` (see :mod:`axon.storage`):

    session.jsonl  subagents/<n>.jsonl  subagents/<n>.trace.jsonl  trace.jsonl  screen.log
    outputs/  checkpoints/  todos.json  exports/  debug.log

* :func:`migrate_session`  converts one session (run when it is opened);
* :func:`migrate_project`  converts every session of a project (run at startup).

Files are *moved*, never copied or deleted: a file whose destination already holds different
content goes to ``<session>/legacy/`` instead of overwriting it, and a byte-identical duplicate is
dropped only after the content was compared. Safe to re-run (nothing is left to move the second
time) and safe against two Axon processes migrating at once (a vanished source is skipped).
Images and pasted text of the old flat layout carry no session id and stay where they are.
"""
from __future__ import annotations

import filecmp
import os
import re
import shutil
import time
from pathlib import Path

from axon import storage

# project-level folders of the old layout that hold per-session items
_LEGACY_PROJECT_DIRS = ("traces", "outputs", "checkpoints", "todos", "screen", "exports", "debug")
_SCREEN_RE = re.compile(r"^\d{8}-\d{6}-(?P<sid>.+)\.log$")


def _same(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _unique(path: Path) -> Path:
    n = 2
    cand = path
    while cand.exists():
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return cand


def _move(src: Path, dst: Path, folder: Path) -> int:
    """Move ``src`` to ``dst`` without ever overwriting; returns the number of items moved.

    Directories merge into an existing directory. A name clash with different content parks the
    source under ``<folder>/legacy/``; an identical file is dropped (its twin already is there)."""
    try:
        if not os.path.lexists(src):
            return 0
        if src.is_dir() and not src.is_symlink():
            if dst.exists() and not dst.is_dir():
                dst = _unique(folder / "legacy" / src.name)
            storage.ensure_dir(dst)
            n = sum(_move(c, dst / c.name, folder) for c in sorted(src.iterdir()))
            try:
                src.rmdir()
            except OSError:
                pass
            return n
        if dst.exists():
            if _same(src, dst):
                src.unlink()
                return 1
            dst = _unique(folder / "legacy" / src.name)
        storage.ensure_dir(dst.parent)
        for delay in (0.0, 0.05, 0.2):  # Windows: antivirus / indexer may hold the file briefly
            if delay:
                time.sleep(delay)
            try:
                shutil.move(str(src), str(dst))
                return 1
            except PermissionError:
                continue
        return 0
    except OSError:
        return 0


def _sub_name(name: str, sid: str) -> tuple[int, str] | None:
    """``(n, kind)`` for ``<sid>_sub_<n>[.traces].jsonl`` (kind "" = transcript, ".trace" = trace)."""
    m = re.fullmatch(re.escape(sid) + r"_sub_(\d+)(\.traces)?\.jsonl", name)
    return (int(m.group(1)), ".trace" if m.group(2) else "") if m else None


def migrate_session(sessions_root: Path, session_id: str, pdir: Path | None = None) -> int:
    """Move everything of one session (a parent id, not ``..._sub_<n>``) into
    ``<sessions_root>/<id>/``. ``pdir`` is the project folder holding the old ``traces/``,
    ``outputs/`` ... folders (None when the store uses a custom sessions folder). Returns the number
    of files/folders moved."""
    root = Path(sessions_root)
    sid = storage.split_subagent_id(session_id)[0]
    folder = root / (storage.UNATTACHED if sid == "no-session" else storage._sid_dirname(sid))
    moved = 0

    def mv(src: Path, rel: str) -> None:
        nonlocal moved
        moved += _move(src, folder / rel, folder)

    mv(root / f"{sid}.jsonl", storage.SESSION_FILE)
    mv(root / f"{sid}.traces.jsonl", "trace.jsonl")
    try:
        flat = [e.name for e in os.scandir(root) if e.is_file() and e.name.startswith(f"{sid}_sub_")]
    except OSError:
        flat = []
    for name in sorted(flat):
        hit = _sub_name(name, sid)
        if hit:
            mv(root / name, f"subagents/{hit[0]}{hit[1]}.jsonl")
    if pdir is None:
        return moved

    mv(pdir / "traces" / f"{sid}.jsonl", "trace.jsonl")
    for kind, ext in (("traces", ".jsonl"), ("todos", ".json")):
        d = pdir / kind
        try:
            names = [e.name for e in os.scandir(d) if e.name.startswith(f"{sid}_sub_") and e.name.endswith(ext)]
        except OSError:
            continue
        for name in sorted(names):
            m = re.fullmatch(re.escape(sid) + r"_sub_(\d+)" + re.escape(ext), name)
            if m:
                mv(d / name, f"subagents/{m.group(1)}.{'trace.jsonl' if kind == 'traces' else 'todos.json'}")
    mv(pdir / "todos" / f"{sid}.json", "todos.json")
    mv(pdir / "checkpoints" / sid, "checkpoints")
    mv(pdir / "outputs" / sid, "outputs")
    try:
        for e in sorted(os.scandir(pdir / "outputs"), key=lambda e: e.name):
            if e.is_dir() and e.name.startswith(f"{sid}_sub_"):
                mv(Path(e.path), "outputs")
    except OSError:
        pass
    for e in sorted(Path(pdir / "exports").glob(f"{storage._safe_name(sid)}.*")) if (pdir / "exports").is_dir() else []:
        mv(e, f"exports/{e.name}")
    mv(pdir / "debug" / f"{sid}.log", "debug.log")

    # screen logs: one per REPL start (``<stamp>-<sid>.log``); a resumed session had several
    try:
        logs = sorted(e.name for e in os.scandir(pdir / "screen")
                      if e.is_file() and (m := _SCREEN_RE.match(e.name)) and m.group("sid") == sid)
    except OSError:
        logs = []
    dest = folder / "screen.log"
    created = False
    for i, name in enumerate(logs):
        src = pdir / "screen" / name
        if i == 0:
            created = not dest.exists()
            mv(src, "screen.log")
        elif created:  # later runs of the same session continue the log we just created
            try:
                with open(src, "rb") as a, open(dest, "ab") as b:
                    b.write(f"\n--- continued: {name} ---\n".encode())
                    shutil.copyfileobj(a, b)
                src.unlink()
                moved += 1
            except OSError:
                pass
        else:
            mv(src, f"legacy/{name}")
    return moved


def migrate_for(workspace: Path, session_id: str) -> int:
    """:func:`migrate_session` for a session of ``workspace``'s project in its default folders; never raises."""
    try:
        pdir = storage.project_dir(Path(workspace), create=False)
        if not pdir.is_dir():
            return 0
        return migrate_session(pdir / "sessions", session_id, pdir)
    except Exception:
        return 0


def _project_session_ids(root: Path, pdir: Path) -> set[str]:
    """Parent ids of every session that still has files in the old flat layout."""
    ids: set[str] = set()

    def add(name: str) -> None:
        name = name.removesuffix(".traces")
        if name and not name.startswith("."):
            ids.add(storage.split_subagent_id(name)[0])

    try:
        for e in os.scandir(root):
            if e.is_file() and e.name.endswith(".jsonl"):
                add(e.name[:-6])
    except OSError:
        pass
    for kind in ("traces", "outputs", "checkpoints", "todos"):
        try:
            for e in os.scandir(pdir / kind):
                add(e.name.rsplit(".", 1)[0] if e.is_file() else e.name)
        except OSError:
            pass
    try:
        for e in os.scandir(pdir / "screen"):
            m = _SCREEN_RE.match(e.name)
            if m:
                add(m.group("sid"))
    except OSError:
        pass
    for kind, ext in (("exports", None), ("debug", ".log")):  # only files named after a known session
        try:
            for e in os.scandir(pdir / kind):
                stem = e.name.rsplit(".", 1)[0]
                if e.is_file() and stem in ids and (ext is None or e.name.endswith(ext)):
                    add(stem)
        except OSError:
            pass
    return ids


def migrate_project(pdir: Path, sessions_root: Path | None = None) -> int:
    """Convert every session of the project folder ``pdir``; returns the number of items moved.
    Removes the old per-kind folders once they are empty."""
    pdir = Path(pdir)
    root = Path(sessions_root) if sessions_root is not None else pdir / "sessions"
    if not pdir.is_dir():
        return 0
    moved = 0
    for sid in sorted(_project_session_ids(root, pdir)):
        moved += migrate_session(root, sid, pdir)
    for kind in _LEGACY_PROJECT_DIRS:
        try:
            (pdir / kind).rmdir()  # only succeeds when empty
        except OSError:
            pass
    if moved:
        try:
            import json
            from axon.session import activity
            ws = json.loads((pdir / "project.json").read_text(encoding="utf-8")).get("workspace")
            if ws:
                activity.record("migration", {"layout": "session-folders", "moved": moved, "project": str(pdir)},
                                workspace=Path(ws))
        except Exception:
            pass
    return moved


__all__ = ["migrate_session", "migrate_project", "migrate_for"]
