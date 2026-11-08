"""
Per-hunk review of the files Axon changed in this session. Plain functions returning data; the
terminal view lives in axon.commands.review_cmds and a web app can call these directly.

The baseline of a file is its bytes before the first edit of the session (the earliest checkpoint
snapshot that holds it, see axon.session.checkpoint); the current bytes are what is on disk now.
``list_hunks`` splits the difference into hunks; ``reject_hunk`` reverts exactly one of them and
refuses when the file changed since it was listed (the hunk id embeds a digest of the file).
"""
from __future__ import annotations

import difflib
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Hunk:
    id: str                      # "<file tag>-<n>-<file digest>": valid only while the file is unchanged
    path: Path
    rel: str
    kind: str                    # modified | created | deleted | binary
    old_start: int = 0           # 1-based line in the baseline
    new_start: int = 0           # 1-based line in the current file
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    accepted: bool = False
    key: str = ""                # content key, stable across digests (used for accepted marks)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": str(self.path), "file": self.rel, "kind": self.kind,
                "old_start": self.old_start, "new_start": self.new_start, "removed": self.removed,
                "added": self.added, "accepted": self.accepted}


@dataclass
class ReviewResult:
    ok: bool
    message: str


def _tag(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()[:4]


def _digest(data: bytes | None) -> str:
    return hashlib.sha1(data if data is not None else b"\0missing").hexdigest()[:6]


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def baselines(manager: Any) -> dict[Path, bytes | None]:
    """File -> bytes before Axon's first edit this session (None = the file did not exist)."""
    out: dict[Path, bytes | None] = {}
    for snap in manager.list_snapshots():
        for path, data in snap.files.items():
            out.setdefault(path, data)
    return out


def _accepted(manager: Any) -> set[str]:
    marks = getattr(manager, "_review_accepted", None)
    if marks is None:
        marks = set()
        setattr(manager, "_review_accepted", marks)
    return marks


def _key(path: Path, removed: list[str], added: list[str], kind: str) -> str:
    return hashlib.sha1("\0".join([str(path), kind, "".join(removed), "\1", "".join(added)]).encode("utf-8", "replace")).hexdigest()[:12]


def _rel(manager: Any, path: Path) -> str:
    try:
        return str(path.relative_to(manager.workspace))
    except ValueError:
        return str(path)


def _lines(data: bytes) -> list[str] | None:
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _file_hunks(manager: Any, path: Path, base: bytes | None, cur: bytes | None) -> list[tuple[Hunk, tuple[int, int, int, int] | None]]:
    if base == cur:
        return []
    digest = _digest(cur)
    rel = _rel(manager, path)
    blines = _lines(base) if base is not None else []
    clines = _lines(cur) if cur is not None else []
    if base is None or cur is None or blines is None or clines is None:
        kind = "created" if base is None else "deleted" if cur is None else "binary"
        removed = [] if base is None or blines is None else [l.rstrip("\r\n") for l in blines]
        added = [] if cur is None or clines is None else [l.rstrip("\r\n") for l in clines]
        h = Hunk(id=f"{_tag(path)}-1-{digest}", path=path, rel=rel, kind=kind, old_start=1, new_start=1, removed=removed,
                 added=added, key=_key(path, removed, added, kind))
        return [(h, None)]
    out = []
    matcher = difflib.SequenceMatcher(None, blines, clines, autojunk=False)
    n = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        n += 1
        removed = [l.rstrip("\r\n") for l in blines[i1:i2]]
        added = [l.rstrip("\r\n") for l in clines[j1:j2]]
        h = Hunk(id=f"{_tag(path)}-{n}-{digest}", path=path, rel=rel, kind="modified", old_start=i1 + 1, new_start=j1 + 1,
                 removed=removed, added=added, key=_key(path, removed, added, "modified"))
        out.append((h, (i1, i2, j1, j2)))
    return out


def list_hunks(manager: Any, include_accepted: bool = True) -> list[Hunk]:
    """Every hunk Axon changed this session, grouped by file in edit order."""
    marks = _accepted(manager)
    hunks: list[Hunk] = []
    for path, base in baselines(manager).items():
        for h, _ in _file_hunks(manager, path, base, _read(path)):
            h.accepted = h.key in marks
            if include_accepted or not h.accepted:
                hunks.append(h)
    return hunks


def _find(manager: Any, hunk_id: str) -> tuple[Hunk | None, Any, ReviewResult | None]:
    """The hunk with this id, plus (baseline, current, span); or a refusal explaining why not."""
    parts = hunk_id.strip().split("-")
    if len(parts) == 3:
        tag, _n, digest = parts
        for path, base in baselines(manager).items():
            if _tag(path) != tag:
                continue
            cur = _read(path)
            if _digest(cur) != digest:
                return None, None, ReviewResult(
                    False, f"Refused: {_rel(manager, path)} changed since this hunk was listed. "
                           "List the hunks again with /review hunks.")
            for h, span in _file_hunks(manager, path, base, cur):
                if h.id == hunk_id.strip():
                    return h, (base, cur, span), None
    return None, None, ReviewResult(False, f"No hunk {hunk_id.strip()!r}. List the hunks with /review hunks.")


def accept_hunk(manager: Any, hunk_id: str) -> ReviewResult:
    """Mark a hunk reviewed. Nothing on disk changes."""
    h, _ctx, refusal = _find(manager, hunk_id)
    if h is None:
        return refusal or ReviewResult(False, "No such hunk.")
    _accepted(manager).add(h.key)
    return ReviewResult(True, f"Accepted {h.rel} hunk {h.id}.")


def _stash(manager: Any, h: Hunk) -> None:
    """Keep the rejected text on disk so a rejection is never a loss."""
    base = getattr(manager, "_dir", None)
    if base is None:
        return
    try:
        d = Path(base) / "rejected"
        d.mkdir(parents=True, exist_ok=True)
        body = "".join(f"- {l}\n" for l in h.removed) + "".join(f"+ {l}\n" for l in h.added)
        (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{h.id}.diff").write_text(f"{h.rel}\n{body}", encoding="utf-8")
    except OSError:
        pass


def reject_hunk(manager: Any, hunk_id: str) -> ReviewResult:
    """Revert exactly one hunk. Refuses, writing nothing, when the file changed since it was listed."""
    h, ctx, refusal = _find(manager, hunk_id)
    if h is None:
        return refusal or ReviewResult(False, "No such hunk.")
    base, cur, span = ctx
    path = h.path
    try:
        if h.kind == "created":
            path.unlink()
        elif h.kind in ("deleted", "binary"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base)
        else:
            i1, i2, j1, j2 = span
            blines, clines = _lines(base), _lines(cur)
            new = clines[:j1] + blines[i1:i2] + clines[j2:]
            path.write_bytes("".join(new).encode("utf-8"))
    except OSError as e:
        return ReviewResult(False, f"Could not revert {h.rel}: {e}")
    _stash(manager, h)
    return ReviewResult(True, f"Reverted {h.rel} hunk {h.id}.")
