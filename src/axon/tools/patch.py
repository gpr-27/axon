"""
Patch tool: apply standard unified diff patches (multi-hunk diffs) to workspace files with rollback protection.
"""
from __future__ import annotations
import difflib
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext
from axon.tools.fs_write import _atomic_write

class PatchTool(Tool):
    name: ClassVar[str] = "Patch"
    description: ClassVar[str] = (
        "Apply a unified diff patch to a file in the workspace. "
        "Supports standard diff hunks (with '@@ -start,count +start,count @@', '+' and '-' lines)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to apply patch to"},
            "patch": {"type": "string", "description": "The unified diff patch string"},
        },
        "required": ["path", "patch"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        patch_text = args.get("patch", "")

        if not raw_path or not patch_text:
            raise ToolError("Patch requires 'path' and 'patch' arguments.")

        p = resolve_in_workspace(ctx.workspace, raw_path)
        if not p.exists():
            raise ToolError(f"Cannot patch non-existent file: {raw_path}")

        ctx.file_state.check_writable(p)
        original = p.read_text(encoding="utf-8")
        orig_lines = original.splitlines(keepends=True)

        # Parse hunks from patch_text
        hunk_lines = [l for l in patch_text.splitlines() if not l.startswith(("---", "+++"))]
        
        # Simple robust patch application
        try:
            applied = self._apply_hunks(orig_lines, hunk_lines)
        except Exception as e:
            raise ToolError(f"Failed to apply patch to {raw_path}: {e}")

        if ctx.checkpoints:
            ctx.checkpoints.capture_before_edit(p)

        _atomic_write(p, applied)
        ctx.file_state.record_read(p)
        return f"Successfully applied patch to {raw_path}."

    def _apply_hunks(self, orig_lines: list[str], patch_lines: list[str]) -> str:
        # Reconstruct by extracting added/removed/context lines
        result_lines = []
        orig_idx = 0

        # Group into hunks
        current_old = []
        current_new = []
        in_hunk = False

        for line in patch_lines:
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue

            if line.startswith("+"):
                current_new.append(line[1:] + ("\n" if not line[1:].endswith("\n") else ""))
            elif line.startswith("-"):
                current_old.append(line[1:] + ("\n" if not line[1:].endswith("\n") else ""))
            elif line.startswith(" ") or not line:
                val = line[1:] if line.startswith(" ") else ""
                current_old.append(val + ("\n" if not val.endswith("\n") else ""))
                current_new.append(val + ("\n" if not val.endswith("\n") else ""))

        old_block = "".join(current_old)
        new_block = "".join(current_new)

        orig_text = "".join(orig_lines)
        if old_block and old_block in orig_text:
            return orig_text.replace(old_block, new_block, 1)
        elif old_block.strip() and old_block.strip() in orig_text:
            return orig_text.replace(old_block.strip(), new_block.strip(), 1)
        else:
            raise ValueError("Target hunk context not found in original file.")
