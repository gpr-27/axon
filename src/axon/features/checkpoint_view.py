"""The checkpoint timeline and a safe restore, as plain functions over the real checkpoint manager.

No terminal and no web imports: ``/rewind`` in the terminal and the Changes panel in the app call these.
They read ``axon.session.checkpoint.CheckpointManager`` (the snapshots the CheckpointList and
CheckpointUndo tools and ``/rewind`` use) and do not keep a second copy of anything.

* A checkpoint ``n`` (1-based, the same number ``/rewind list`` and ``/diff T<n>`` show) holds each file
  as it was before that turn's edits. What the turn left behind is the next checkpoint's copy of the file,
  or the file on disk for the newest one.
* ``timeline``: one row per checkpoint with time, the prompt that made it, files and +/- line counts.
* ``checkpoint_diff``: the diff of one checkpoint, as the rows the Changes panel draws.
* ``plan_restore`` / ``restore``: put every file touched by checkpoint ``n`` and later back to how it was
  before ``n``. A file that was changed outside Axon since Axon last wrote it is a conflict: the restore is
  refused unless the caller says to keep those files or to overwrite them. The restore is recorded as a
  new checkpoint holding what was replaced, so restoring that checkpoint undoes it.
"""
from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Any

from axon.session.checkpoint import MAX_SNAPSHOT_BYTES, digest_bytes

MAX_COUNT_BYTES = 1_500_000
MAX_FILES_SHOWN = 60
MAX_DIFF_LINES = 4000
RESTORE_LABEL = "Restored to before checkpoint "


class RestoreError(ValueError):
    """The restore cannot start; the message says why."""


def _snaps(cp: Any) -> list[Any]:
    return list(cp.list_snapshots()) if cp is not None and hasattr(cp, "list_snapshots") else []


def _rel(cp: Any, ws: Path | None, p: Path) -> str:
    base = ws or getattr(cp, "workspace", None)
    try:
        return Path(p).resolve().relative_to(Path(base).resolve()).as_posix() if base else str(p)
    except (ValueError, OSError):
        return str(p)


def _inside(cp: Any, ws: Path | None, p: Path) -> bool:
    base = ws or getattr(cp, "workspace", None)
    if base is None:
        return True
    try:
        Path(p).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


def _after_bytes(snaps: list[Any], i: int, p: Path) -> bytes | None:
    """What checkpoint ``i`` (0-based) left in file ``p``: the next checkpoint's copy, else the file now."""
    for later in snaps[i + 1:]:
        if p in later.files:
            return later.files[p]
    return _read(p)


def _is_binary(b: bytes) -> bool:
    return b"\0" in b[:8000]


def _text(b: bytes | None) -> list[str]:
    return b.decode("utf-8", errors="replace").splitlines(keepends=True) if b else []


def count_lines(before: bytes | None, after: bytes | None) -> tuple[int | None, int | None, bool]:
    """(added, removed, binary). Counts are None for a file too large to compare cheaply."""
    if before == after:
        return 0, 0, False
    if (before and _is_binary(before)) or (after and _is_binary(after)):
        return None, None, True
    if max(len(before or b""), len(after or b"")) > MAX_COUNT_BYTES:
        return None, None, False
    a = r = 0
    for line in difflib.unified_diff(_text(before), _text(after), n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            a += 1
        elif line.startswith("-") and not line.startswith("---"):
            r += 1
    return a, r, False


def _prompt_for(prompts: list[tuple[float, str]] | None, ts: float) -> str:
    best = ""
    for pts, text in prompts or []:
        if pts and pts <= ts:
            best = text
    return " ".join(str(best).split())


def timeline(cp: Any, prompts: list[tuple[float, str]] | None = None, ws: Path | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
    """One row per checkpoint, oldest first. ``prompts`` is [(time sent, text)] oldest first; the prompt
    shown for a checkpoint is the last one sent before it was made."""
    snaps = _snaps(cp)
    rows: list[dict[str, Any]] = []
    first = max(0, len(snaps) - limit)
    for i in range(first, len(snaps)):
        s = snaps[i]
        current = s.turn_id == -1
        files = []
        added = removed = 0
        for p, before in list(s.files.items())[:MAX_FILES_SHOWN]:
            after = _after_bytes(snaps, i, p)
            a, r, binary = count_lines(before, after)
            status = "new" if before is None else "deleted" if after is None else "modified"
            if before == after:
                status = "unchanged"
            files.append({"path": str(p), "rel": _rel(cp, ws, p), "status": status, "added": a, "removed": r, "binary": binary})
            added += a or 0
            removed += r or 0
        label = str(getattr(s, "label", "") or "")
        restore = label.startswith(RESTORE_LABEL)
        rows.append({
            "n": i + 1, "time": s.timestamp, "turn": s.turn_id,
            "kind": "current" if current else "restore" if restore else "turn",
            "label": label if restore else "",
            "prompt": "" if restore else _prompt_for(prompts, s.timestamp),
            "files": files, "file_count": len(s.files) + len(s.skipped), "skipped": len(s.skipped),
            "added": added, "removed": removed,
        })
    return rows


def checkpoint_diff(cp: Any, n: int, ws: Path | None = None, rel: str = "") -> list[dict[str, Any]]:
    """The diff of checkpoint ``n``: [{rel, status, rows, note}], rows as the Changes panel draws them."""
    from axon.features.worktree_view import parse_diff
    snaps = _snaps(cp)
    if not 1 <= n <= len(snaps):
        raise RestoreError(f"There is no checkpoint {n}.")
    s = snaps[n - 1]
    out = []
    for p, before in s.files.items():
        r = _rel(cp, ws, p)
        if rel and r != rel:
            continue
        after = _after_bytes(snaps, n - 1, p)
        status = "new" if before is None else "deleted" if after is None else "modified"
        entry: dict[str, Any] = {"rel": r, "status": status, "rows": [], "note": ""}
        if before == after:
            entry["note"] = "No difference: the file is back to how it was."
        elif (before and _is_binary(before)) or (after and _is_binary(after)):
            entry["note"] = "Binary file."
        elif max(len(before or b""), len(after or b"")) > 2_000_000:
            entry["note"] = "File too large to compare here."
        else:
            text = "".join(difflib.unified_diff(_text(before), _text(after), f"a/{r}", f"b/{r}", n=3))
            rows = parse_diff(text)
            for row in rows:
                row["text"] = str(row.get("text", "")).rstrip("\r")   # a CRLF file must not show stray carriage returns
            if len(rows) > MAX_DIFF_LINES:
                rows = rows[:MAX_DIFF_LINES] + [{"kind": "meta", "old": None, "new": None, "text": "Diff cut here."}]
            entry["rows"] = rows
        out.append(entry)
    for p in s.skipped:
        out.append({"rel": _rel(cp, ws, p), "status": "skipped", "rows": [], "note": "Too large for Axon to keep a copy."})
    return out


# ─── restore ────────────────────────────────────────────────────────────────

def _expected_digest(snaps: list[Any], p: Path) -> tuple[bool, str | None, int]:
    """(known, digest Axon last left in ``p``, checkpoint number that left it)."""
    for i in range(len(snaps) - 1, -1, -1):
        s = snaps[i]
        if p in s.files:
            after = getattr(s, "after", None) or {}
            if p in after:
                return True, after[p], i + 1
            return False, None, i + 1
    return False, None, 0


def plan_restore(cp: Any, n: int, ws: Path | None = None) -> dict[str, Any]:
    """What restoring to before checkpoint ``n`` would do, without touching anything.

    ``files`` rows: {path, rel, action: restore|recreate|delete, added, removed, conflict, why}.
    ``conflicts`` are the rows changed outside Axon since Axon last wrote them, ``unverified`` are files
    from checkpoints made before Axon recorded what it left (nothing to compare with), and ``skipped``
    are files that cannot be restored from here (outside the project, or too large to keep a copy)."""
    snaps = _snaps(cp)
    if not 1 <= n <= len(snaps):
        raise RestoreError(f"There is no checkpoint {n}.")
    if snaps[-1].turn_id == -1:
        raise RestoreError("Axon is still working on a turn. Wait for it to finish, then restore.")
    target: dict[Path, bytes | None] = {}
    for s in snaps[n - 1:]:
        for p, before in s.files.items():
            target.setdefault(p, before)
    skipped_big = [p for s in snaps[n - 1:] for p in s.skipped]
    files, skipped = [], []
    for p, want in target.items():
        rel = _rel(cp, ws, p)
        if not _inside(cp, ws, p):
            skipped.append({"rel": rel, "why": "outside this project"})
            continue
        now = _read(p)
        if now is not None and len(now) > MAX_SNAPSHOT_BYTES:
            skipped.append({"rel": rel, "why": "too large for Axon to keep a copy of what it replaces"})
            continue
        if now == want:
            continue
        action = "delete" if want is None else "recreate" if now is None else "restore"
        a, r, binary = count_lines(now, want)
        known, expect, by = _expected_digest(snaps, p)
        conflict = known and digest_bytes(now) != expect
        row = {"path": str(p), "rel": rel, "action": action, "added": a, "removed": r, "binary": binary,
               "conflict": bool(conflict), "unverified": not known,
               "why": (f"changed outside Axon since checkpoint {by}" if conflict else "")}
        files.append(row)
    for p in skipped_big:
        skipped.append({"rel": _rel(cp, ws, p), "why": "too large to restore"})
    return {"n": n, "checkpoints": len(snaps) - n + 1, "files": files,
            "conflicts": [f for f in files if f["conflict"]], "unverified": [f for f in files if f["unverified"]],
            "skipped": skipped}


def summary_lines(plan: dict[str, Any]) -> list[str]:
    """The confirmation text: exactly which files change, and what each change is."""
    verbs = {"restore": "go back to how it was", "recreate": "be put back (it was deleted)", "delete": "be deleted (Axon created it)"}
    out = []
    for f in plan["files"]:
        nums = ""
        if f.get("added") is not None and f.get("removed") is not None and f["action"] == "restore":
            nums = f"  (+{f['added']} −{f['removed']})"
        out.append(f"{f['rel']}: will {verbs[f['action']]}{nums}" + (f"  CONFLICT: {f['why']}" if f["conflict"] else ""))
    return out


def restore(cp: Any, n: int, ws: Path | None = None, conflicts: str = "refuse") -> dict[str, Any]:
    """Restore to before checkpoint ``n``. ``conflicts``: refuse (default, nothing is written when any file
    conflicts), keep (leave the conflicting files alone) or overwrite (replace them; they are kept in the new
    checkpoint so this can be undone). Returns {ok, refused, restored, deleted, kept, failed, checkpoint}."""
    if conflicts not in ("refuse", "keep", "overwrite"):
        raise RestoreError("conflicts must be refuse, keep or overwrite.")
    plan = plan_restore(cp, n, ws)
    if cp.pending_count():
        raise RestoreError("Axon is still working on a turn. Wait for it to finish, then restore.")
    if plan["conflicts"] and conflicts == "refuse":
        return {"ok": False, "refused": True, "conflicts": plan["conflicts"], "restored": [], "deleted": [], "kept": [],
                "failed": [], "checkpoint": None,
                "message": f"{len(plan['conflicts'])} file(s) changed outside Axon. Nothing was changed."}
    snaps = _snaps(cp)
    target: dict[Path, bytes | None] = {}
    for s in snaps[n - 1:]:
        for p, before in s.files.items():
            target.setdefault(p, before)
    from axon.tools.textio import atomic_write_bytes
    restored, deleted, kept, failed = [], [], [], []
    for row in plan["files"]:
        p = Path(row["path"])
        if row["conflict"] and conflicts == "keep":
            kept.append(row["rel"])
            continue
        try:
            cp.capture_before_edit(p)              # what is here now, so the restore can be undone
            want = target[p]
            if want is None:
                p.unlink()
                deleted.append(row["rel"])
            else:
                atomic_write_bytes(p, want)
                restored.append(row["rel"])
        except Exception as e:
            getattr(cp, "_pending_files", {}).pop(p.resolve(), None)   # nothing changed here, so no copy to keep
            failed.append(f"{row['rel']}: {e}")
    snap = cp.commit_turn_snapshot(label=f"{RESTORE_LABEL}{n}") if cp.pending_count() else None
    new_n = len(_snaps(cp)) if snap is not None else None
    return {"ok": not failed, "refused": False, "restored": restored, "deleted": deleted, "kept": kept, "failed": failed,
            "checkpoint": new_n, "conflicts": plan["conflicts"] if conflicts == "overwrite" else [],
            "message": f"Restored {len(restored)}, deleted {len(deleted)}" + (f", kept {len(kept)} you changed" if kept else "")
            + (f", {len(failed)} failed" if failed else "") + "."}


def when(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
