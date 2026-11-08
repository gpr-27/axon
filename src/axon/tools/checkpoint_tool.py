"""CheckpointList / CheckpointUndo: inspect and undo the agent's own file edits (the snapshots /rewind uses)."""
from __future__ import annotations

import time
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, int_arg, plural


def _manager(ctx: ToolContext):
    cp = getattr(ctx, "checkpoints", None)
    if cp is None:
        raise ToolError("Checkpoints are not available in this session.")
    return cp


class CheckpointListTool(Tool):
    name: ClassVar[str] = "CheckpointList"
    description: ClassVar[str] = (
        "List the file-edit checkpoints of this session, newest first (1 = most recent turn's edits), with the "
        "files each one changed. Use before CheckpointUndo."
    )
    schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "additionalProperties": False}
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cp = _manager(ctx)
        snaps = list(reversed(cp.list_snapshots()))
        if not snaps:
            return "No checkpoints: no files have been changed by Write/Edit/MultiEdit/Patch in this session."
        lines = [f"{plural(len(snaps), 'checkpoint')} (newest first):"]
        for i, s in enumerate(snaps, 1):
            when = time.strftime("%H:%M:%S", time.localtime(s.timestamp))
            names = [cp._rel(p) + (" (new)" if orig is None else "") for p, orig in s.files.items()]
            shown = ", ".join(names[:8]) + (f", +{len(names) - 8} more" if len(names) > 8 else "")
            label = f" {s.label}" if s.label else ""
            lines.append(f"{i}. {when}{label} · {plural(len(names), 'file')}: {shown}")
        return "\n".join(lines)


class CheckpointUndoTool(Tool):
    name: ClassVar[str] = "CheckpointUndo"
    description: ClassVar[str] = (
        "Undo your own file edits: restore the files changed in the newest `steps` checkpoints to their earlier "
        "content (files you created are deleted). Overwrites any later changes to those files, including the "
        "user's. Re-Read a file before editing it again."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"steps": {"type": "integer", "minimum": 1, "description": "Number of checkpoints to undo (default 1)"}},
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cp = _manager(ctx)
        steps = int_arg(args, "steps", 1, lo=1, strict=True)
        snaps = cp.list_snapshots()
        if not snaps:
            raise ToolError("Nothing to undo: no checkpoints in this session.")
        touched = [p for s in snaps[-steps:] for p in list(s.files) + list(s.skipped)]
        done = cp.rewind(steps)
        for p in touched:
            ctx.file_state.invalidate(p)
        return f"Undid {plural(min(steps, len(snaps)), 'checkpoint')}:\n" + "\n".join(done or ["(no files changed)"])
