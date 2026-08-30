"""
File Checkpoint & Reversion Manager for Axon.
Snapshots files prior to tool edits, allowing exact undo/rewind (`/rewind`, `/undo`).
"""
from __future__ import annotations
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass
class Snapshot:
    turn_id: int
    timestamp: float
    files: dict[Path, str | None]  # Path -> original content string or None if newly created

class CheckpointManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.history: list[Snapshot] = []
        self._pending_files: dict[Path, str | None] = {}

    def clear(self) -> None:
        """Clear all snapshots and pending files."""
        self.history.clear()
        self._pending_files.clear()

    def capture_before_edit(self, p: Path) -> None:
        """Capture original content of a file before an edit modifies it."""
        resolved = p.resolve()
        if resolved in self._pending_files:
            return  # Already captured in current turn

        if resolved.exists() and resolved.is_file():
            try:
                self._pending_files[resolved] = resolved.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                self._pending_files[resolved] = None
        else:
            self._pending_files[resolved] = None

    def commit_turn_snapshot(self, turn_id: int) -> None:
        """Seal the changes made during a turn into a checkpoint snapshot."""
        if self._pending_files:
            self.history.append(Snapshot(
                turn_id=turn_id,
                timestamp=time.time(),
                files=dict(self._pending_files),
            ))
            self._pending_files.clear()

    def rewind_last(self) -> list[str]:
        """Rewind the last turn's file modifications."""
        if not self.history and self._pending_files:
            self.commit_turn_snapshot(0)

        if not self.history:
            return []

        snap = self.history.pop()
        reverted: list[str] = []
        for path, original_content in snap.files.items():
            try:
                if original_content is None:
                    # File was newly created, delete it on rewind
                    if path.exists():
                        path.unlink()
                        reverted.append(f"Deleted newly created {path.name}")
                else:
                    path.write_text(original_content, encoding="utf-8")
                    reverted.append(f"Restored {path.name}")
            except Exception as e:
                reverted.append(f"Failed restoring {path.name}: {e}")

        return reverted
