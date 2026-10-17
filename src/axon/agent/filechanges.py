"""
Which files a shell command created, modified or deleted in the workspace.

``take_snapshot`` records every workspace file's size and mtime: in a git repository the tracked and
untracked files plus the files inside ignored folders (git lists those folders without descending),
otherwise a walk; both skip dependency and cache folders (``SKIP_DIRS``). It keeps the text of the
files whose old content could not be recovered later: in a git repository the files that differ from
the index or are not in it, otherwise every small text file. After the command, ``changes_since``
compares a fresh listing against it and returns one ``FileChange`` per created / modified / deleted /
renamed file, with line counts and both texts for the renderer's diff.

Snapshots are best effort: a workspace that is too large, or a listing that is too slow, yields
``None`` and the command simply runs untracked.
"""
from __future__ import annotations

import difflib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".next", ".nuxt", ".gradle", ".idea", ".cache", "target", "dist", "build",
    ".eggs", "site-packages", ".terraform", ".dart_tool", "Pods", "vendor",
})
MAX_FILES = 20_000
TIME_BUDGET_S = 1.0              # listing + stat; a slower workspace is not tracked
TEXT_BUDGET_S = 0.3              # reading the old texts kept for diffs
TEXT_LIMIT = 256 * 1024          # texts kept for diffs (old and new)
CACHE_BUDGET = 4 * 1024 * 1024   # total old text kept per snapshot
MAX_CHANGES = 200
_SECRET_NAMES = (".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")


def is_secret(path: str) -> bool:
    """Files whose contents are never shown in a diff (only that they changed)."""
    name = path.rsplit("/", 1)[-1].lower()
    return (name.startswith(".env") or name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)
            or "secret" in name)


@dataclass(frozen=True)
class FileChange:
    path: str                    # workspace-relative, forward slashes (the new path of a rename)
    kind: str                    # "created" | "modified" | "deleted" | "renamed"
    added: int | None            # lines added (None: unknown or not text)
    removed: int | None          # lines removed (None: unknown or not text)
    old: str | None = None       # text before (modified / deleted / renamed), when known, small, not secret
    new: str | None = None       # text after (created / modified / renamed), when small, not secret
    old_path: str | None = None  # renamed: where the file was


@dataclass
class Snapshot:
    root: Path
    git: bool
    stats: dict[str, tuple[int, int]]                      # path -> (mtime_ns, size)
    texts: dict[str, str | None] = field(default_factory=dict)  # old text (None: binary / too large)


def _git(root: Path, *args: str) -> bytes | None:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=TIME_BUDGET_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _skipped(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in rel.split("/")[:-1])


def _list_files(root: Path, git: bool, deadline: float) -> list[str] | None:
    files: list[str] = []
    walk_from = [root]
    if git:
        out = _git(root, "ls-files", "-z", "-co", "--exclude-standard")
        ignored = _git(root, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory")
        if out is None:
            return None
        files = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p and not _skipped(p)]
        walk_from = []
        for p in (ignored or b"").decode("utf-8", "surrogateescape").split("\0"):
            if not p or _skipped(p.rstrip("/") + "/x"):
                continue
            if p.endswith("/"):
                walk_from.append(root / p)       # an ignored folder: its files are still tracked here
            else:
                files.append(p)                  # an ignored file (e.g. .env)
    for top in walk_from:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            base = Path(dirpath).relative_to(root).as_posix()
            for name in filenames:
                files.append(name if base == "." else f"{base}/{name}")
            if len(files) > MAX_FILES or time.monotonic() > deadline:
                return None
    return files if len(files) <= MAX_FILES else None


def _stats(root: Path, files: list[str], deadline: float) -> dict[str, tuple[int, int]] | None:
    stats: dict[str, tuple[int, int]] = {}
    for i, rel in enumerate(files):
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        if not os.path.isfile(root / rel):
            continue
        stats[rel] = (st.st_mtime_ns, st.st_size)
        if i % 512 == 0 and time.monotonic() > deadline:
            return None
    return stats


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _read_text(path: Path, limit: int = TEXT_LIMIT) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return _decode(path.read_bytes())
    except OSError:
        return None


def _is_git_repo(root: Path) -> bool:
    out = _git(root, "rev-parse", "--is-inside-work-tree")
    return out is not None and out.strip() == b"true"


def take_snapshot(root: Path) -> Snapshot | None:
    """The workspace's files right now, or None when it is too large or slow to track."""
    deadline = time.monotonic() + TIME_BUDGET_S
    root = Path(root)
    git = _is_git_repo(root)
    files = _list_files(root, git, deadline)
    if files is None:
        return None
    stats = _stats(root, files, deadline)
    if stats is None:
        return None
    snap = Snapshot(root, git, stats)
    if git:   # the index has every other file's old content
        dirty = _git(root, "diff", "--name-only", "-z") or b""
        in_index = _git(root, "ls-files", "-z") or b""
        indexed = set(in_index.decode("utf-8", "surrogateescape").split("\0"))
        keep = {p for p in dirty.decode("utf-8", "surrogateescape").split("\0") if p in stats}
        keep |= {p for p in stats if p not in indexed}          # untracked and ignored files
    else:
        keep = set(stats)
    budget, text_deadline = CACHE_BUDGET, time.monotonic() + TEXT_BUDGET_S
    for rel in sorted(keep, key=lambda p: stats[p][1]):         # small files first: most diffs per ms
        size = stats[rel][1]
        if size > TEXT_LIMIT or size > budget or time.monotonic() > text_deadline:
            snap.texts[rel] = None
            continue
        snap.texts[rel] = _read_text(root / rel)
        budget -= size
    return snap


def _old_text(snap: Snapshot, rel: str) -> str | None:
    if rel in snap.texts:
        return snap.texts[rel]
    if snap.git:
        data = _git(snap.root, "show", f":{rel}")
        if data is not None and len(data) <= TEXT_LIMIT:
            return _decode(data)
    return None


def count_lines(text: str) -> int:
    return len(text.splitlines())


def diff_counts(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    a, b = old.splitlines(), new.splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def changes_since(snap: Snapshot, root: Path | None = None) -> list[FileChange]:
    """Files created, modified or deleted since *snap* was taken (sorted by path)."""
    root = Path(root or snap.root)
    deadline = time.monotonic() + TIME_BUDGET_S
    files = _list_files(root, snap.git, deadline)
    if files is None:
        return []
    now = _stats(root, files, deadline)
    if now is None:   # too slow to stat this time: report nothing rather than every file as deleted
        return []
    changes: list[FileChange] = []
    for rel in sorted(set(now) | set(snap.stats)):
        if len(changes) >= MAX_CHANGES:
            break
        before, after = snap.stats.get(rel), now.get(rel)
        if before == after:
            continue
        if before is None:
            new = _read_text(root / rel)
            changes.append(FileChange(rel, "created", count_lines(new) if new is not None else None, None, None, new))
        elif after is None:
            old = _old_text(snap, rel)
            changes.append(FileChange(rel, "deleted", None, count_lines(old) if old is not None else None, old, None))
        else:
            old, new = _old_text(snap, rel), _read_text(root / rel)
            if old is not None and new is not None:
                if old == new:
                    continue        # touched, not changed
                a, r = diff_counts(old, new)
                changes.append(FileChange(rel, "modified", a, r, old, new))
            else:
                changes.append(FileChange(rel, "modified", None, None, old, new))
    return _hide_secrets(_pair_renames(changes, snap, now))


def _pair_renames(changes: list[FileChange], snap: Snapshot, now: dict[str, tuple[int, int]]) -> list[FileChange]:
    """A deleted file and a created file with the same content are one rename."""
    created = [c for c in changes if c.kind == "created"]
    out: list[FileChange] = []
    used: set[str] = set()
    for c in changes:
        if c.kind != "deleted":
            continue
        size = snap.stats[c.path][1]
        match = next((n for n in created if n.path not in used and now.get(n.path, (0, -1))[1] == size
                      and (c.old is None or n.new is None or c.old == n.new)), None)
        if match is not None:
            used.add(match.path)
            out.append(FileChange(match.path, "renamed", None, None, c.old, match.new, old_path=c.path))
    gone = {r.old_path for r in out}
    rest = [c for c in changes if c.path not in used and c.path not in gone]
    return sorted(rest + out, key=lambda c: c.path)


def _hide_secrets(changes: list[FileChange]) -> list[FileChange]:
    return [FileChange(c.path, c.kind, c.added, c.removed, None, None, c.old_path) if is_secret(c.path) else c
            for c in changes]


def _counts(c: FileChange) -> str:
    parts = [f"+{c.added}" if c.added is not None else "", f"−{c.removed}" if c.removed is not None else ""]
    shown = " ".join(p for p in parts if p)
    return f" ({shown})" if shown else ""


def summary_line(changes: list[FileChange], limit: int = 20) -> str:
    """'[Files changed by this command: created a.md (+3), modified b.py (+1 −1), deleted c.txt (−2)]'."""
    items = [f"renamed {c.old_path} → {c.path}" if c.kind == "renamed" else f"{c.kind} {c.path}{_counts(c)}"
             for c in changes[:limit]]
    if len(changes) > limit:
        items.append(f"and {len(changes) - limit} more")
    return "[Files changed by this command: " + ", ".join(items) + "]"
