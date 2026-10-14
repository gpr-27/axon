"""
File checkpoints for /rewind: snapshot files before tool edits and restore them.

Contents are captured as raw bytes so binary files, CRLF line endings and
non-UTF-8 encodings round-trip exactly. Each committed snapshot is one agent turn.
When a session is bound (``set_session``), snapshots are also persisted under
``storage.checkpoints_dir(workspace, session_id)/<n>/`` so /rewind survives a resume.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024  # do not hold files bigger than this in memory


@dataclass
class Snapshot:
    turn_id: int
    timestamp: float
    files: dict[Path, bytes | None]  # original bytes, or None if the file did not exist
    label: str = ""
    skipped: list[Path] = field(default_factory=list)


class CheckpointManager:
    def __init__(self, workspace: Path, session_id: str | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.history: list[Snapshot] = []
        self._pending_files: dict[Path, bytes | None] = {}
        self._pending_skipped: list[Path] = []
        self._turn_counter = 0
        self.session_id: str | None = None
        self._dir: Path | None = None
        if session_id:
            self.set_session(session_id)

    # ── persistence ──
    def set_session(self, session_id: str | None) -> None:
        """Bind to a session: persist new snapshots and load the ones saved earlier."""
        self.session_id = session_id
        self._dir = None
        self.history = []
        self._turn_counter = 0
        if not session_id:
            return
        try:
            from axon import storage
            base = storage.project_dir(self.workspace, create=False) / "checkpoints" / session_id
        except Exception:
            return
        self._dir = base
        if not base.is_dir():
            return
        for d in sorted((x for x in base.iterdir() if x.is_dir() and x.name.isdigit()), key=lambda x: int(x.name)):
            snap = self._load_snapshot(d)
            if snap is not None:
                setattr(snap, "_seq", int(d.name))
                self.history.append(snap)
                self._turn_counter = max(self._turn_counter, int(d.name))

    def _load_snapshot(self, d: Path) -> Snapshot | None:
        try:
            meta = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            files: dict[Path, bytes | None] = {}
            for i, entry in enumerate(meta.get("files", [])):
                blob = d / f"{i}.bin"
                files[Path(entry["path"])] = blob.read_bytes() if entry.get("existed") else None
            return Snapshot(turn_id=int(meta.get("turn_id", d.name)), timestamp=float(meta.get("timestamp", 0)),
                            files=files, label=str(meta.get("label", "")),
                            skipped=[Path(p) for p in meta.get("skipped", [])])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _persist(self, snap: Snapshot, seq: int) -> None:
        if self._dir is None:
            return
        d = self._dir / str(seq)
        try:
            d.mkdir(parents=True, exist_ok=True)
            entries = []
            for i, (path, data) in enumerate(snap.files.items()):
                entries.append({"path": str(path), "existed": data is not None})
                if data is not None:
                    (d / f"{i}.bin").write_bytes(data)
            (d / "manifest.json").write_text(json.dumps({
                "turn_id": snap.turn_id, "timestamp": snap.timestamp, "label": snap.label,
                "files": entries, "skipped": [str(p) for p in snap.skipped],
            }, indent=1), encoding="utf-8")
            setattr(snap, "_seq", seq)
        except OSError:
            pass

    def _forget(self, snap: Snapshot) -> None:
        if self._dir is None:
            return
        seq = getattr(snap, "_seq", None)
        if seq is None:
            seq = snap.turn_id
        shutil.rmtree(self._dir / str(seq), ignore_errors=True)

    def clear(self) -> None:
        self.history.clear()
        self._pending_files.clear()
        self._pending_skipped.clear()

    def capture_before_edit(self, p: Path) -> None:
        """Remember a file's current bytes the first time it is touched in this turn."""
        resolved = Path(p).resolve()
        if resolved in self._pending_files or resolved in self._pending_skipped:
            return
        if resolved.is_file():
            try:
                if resolved.stat().st_size > MAX_SNAPSHOT_BYTES:
                    self._pending_skipped.append(resolved)
                    return
                self._pending_files[resolved] = resolved.read_bytes()
            except OSError:
                self._pending_skipped.append(resolved)
        elif not resolved.exists():
            self._pending_files[resolved] = None

    def commit_turn_snapshot(self, turn_id: int | None = None, label: str = "") -> Snapshot | None:
        """Seal the files captured during a turn into one snapshot."""
        if not self._pending_files and not self._pending_skipped:
            return None
        self._turn_counter += 1
        snap = Snapshot(
            turn_id=self._turn_counter if turn_id is None else turn_id,
            timestamp=time.time(),
            files=dict(self._pending_files),
            label=label[:80],
            skipped=list(self._pending_skipped),
        )
        self.history.append(snap)
        self._persist(snap, self._turn_counter)
        self._pending_files.clear()
        self._pending_skipped.clear()
        return snap

    def pending_count(self) -> int:
        return len(self._pending_files)

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace))
        except ValueError:
            return str(path)

    def list_snapshots(self) -> list[Snapshot]:
        """Committed snapshots (oldest first), including uncommitted edits as a final entry."""
        snaps = list(self.history)
        if self._pending_files:
            snaps.append(Snapshot(turn_id=-1, timestamp=time.time(), files=dict(self._pending_files), label="(current turn)"))
        return snaps

    def _restore(self, snap: Snapshot) -> list[str]:
        reverted: list[str] = []
        for path, original in snap.files.items():
            name = self._rel(path)
            try:
                if original is None:
                    if path.exists():
                        path.unlink()
                        reverted.append(f"Deleted newly created {name}")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
                    reverted.append(f"Restored {name}")
            except OSError as e:
                reverted.append(f"Failed restoring {name}: {e}")
        for path in snap.skipped:
            reverted.append(f"Skipped {self._rel(path)} (too large or unreadable to snapshot)")
        return reverted

    def rewind_last(self) -> list[str]:
        """Undo the most recent turn's file changes."""
        return self.rewind(1)

    def rewind(self, steps: int = 1) -> list[str]:
        """Undo the last ``steps`` turns (uncommitted edits count as the newest turn)."""
        if self._pending_files or self._pending_skipped:
            self.commit_turn_snapshot(label="(current turn)")
        reverted: list[str] = []
        for _ in range(max(1, steps)):
            if not self.history:
                break
            snap = self.history.pop()
            reverted.extend(self._restore(snap))
            self._forget(snap)
        return reverted

    def snapshots_since(self, ts: float) -> list[Snapshot]:
        """Committed snapshots taken at or after ``ts`` (plus pending edits), oldest first."""
        return [s for s in self.list_snapshots() if s.turn_id == -1 or s.timestamp >= ts]

    def rewind_to(self, ts: float) -> list[str]:
        """Undo every snapshot taken at or after ``ts`` (newest first), including uncommitted edits."""
        if self._pending_files or self._pending_skipped:
            self.commit_turn_snapshot(label="(current turn)")
        reverted: list[str] = []
        while self.history and self.history[-1].timestamp >= ts:
            snap = self.history.pop()
            reverted.extend(self._restore(snap))
            self._forget(snap)
        return reverted
