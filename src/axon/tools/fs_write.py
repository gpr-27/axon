"""
Write, Edit, and MultiEdit tool implementations with atomic disk replacement and staleness guards.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

def _atomic_write(target: Path, content: str) -> None:
    """Atomic write via temporary file and fsync before replacement."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target.with_suffix(target.suffix + ".axon-tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, target)
    finally:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass

class WriteTool(Tool):
    name: ClassVar[str] = "Write"
    description: ClassVar[str] = (
        "Create or overwrite a file in the workspace atomically. "
        "Prefer Edit for modifying existing files."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write to"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        content = args.get("content", "")
        if not raw_path:
            raise ToolError("Write requires 'path'.")

        p = resolve_in_workspace(ctx.workspace, raw_path)
        ctx.file_state.check_writable(p)
        if ctx.checkpoints:
            ctx.checkpoints.capture_before_edit(p)
        _atomic_write(p, content)
        ctx.file_state.record_read(p)
        return f"Successfully wrote {len(content):,} characters to {raw_path}."


class EditTool(Tool):
    name: ClassVar[str] = "Edit"
    description: ClassVar[str] = (
        "Replace an exact string in an existing file. "
        "old_string must occur exactly once unless replace_all=true. "
        "Fails if the file has not been Read in this session or changed on disk."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_string": {"type": "string", "description": "Exact text to replace, including indentation"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        old_str = args.get("old_string", "")
        new_str = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))

        if not raw_path or not old_str:
            raise ToolError("Edit requires 'path' and 'old_string'.")
        if old_str == new_str:
            raise ToolError("Edit failed: 'old_string' and 'new_string' are identical.")

        p = resolve_in_workspace(ctx.workspace, raw_path)
        if not p.exists():
            raise ToolError(f"Cannot edit non-existent file {raw_path}. Use Write to create new files.")

        ctx.file_state.check_writable(p)
        current_content = p.read_text(encoding="utf-8")

        count = current_content.count(old_str)
        if count == 0:
            raise ToolError(
                f"Edit failed: 'old_string' was not found in {raw_path}. "
                f"The file may have changed or line breaks/indentation differed. "
                f"Re-read the file with Read and try again."
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"Edit failed: 'old_string' appears {count} times in {raw_path}. "
                f"Provide more surrounding context in 'old_string' to make it unique, "
                f"or set replace_all=true."
            )

        if replace_all:
            new_content = current_content.replace(old_str, new_str)
        else:
            new_content = current_content.replace(old_str, new_str, 1)

        if ctx.checkpoints:
            ctx.checkpoints.capture_before_edit(p)
        _atomic_write(p, new_content)
        ctx.file_state.record_read(p)
        return f"Successfully applied edit to {raw_path} ({count} occurrence{'s' if count > 1 else ''} replaced)."


class MultiEditTool(Tool):
    name: ClassVar[str] = "MultiEdit"
    description: ClassVar[str] = (
        "Apply multiple sequential edits to a single file atomically. "
        "All edits must succeed or none are applied."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
                "description": "List of edits to apply in sequence",
            }
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        edits = args.get("edits", [])
        if not raw_path or not edits:
            raise ToolError("MultiEdit requires 'path' and non-empty 'edits' list.")

        p = resolve_in_workspace(ctx.workspace, raw_path)
        if not p.exists():
            raise ToolError(f"Cannot edit non-existent file {raw_path}.")

        ctx.file_state.check_writable(p)
        content = p.read_text(encoding="utf-8")

        for idx, ed in enumerate(edits, 1):
            old_str = ed.get("old_string", "")
            new_str = ed.get("new_string", "")
            replace_all = bool(ed.get("replace_all", False))

            if not old_str:
                raise ToolError(f"Edit #{idx} missing 'old_string'.")
            cnt = content.count(old_str)
            if cnt == 0:
                raise ToolError(f"Edit #{idx} failed: 'old_string' not found in intermediate text.")
            if cnt > 1 and not replace_all:
                raise ToolError(f"Edit #{idx} failed: 'old_string' matched {cnt} times without replace_all.")

            content = content.replace(old_str, new_str, -1 if replace_all else 1)

        _atomic_write(p, content)
        ctx.file_state.record_read(p)
        return f"Successfully applied {len(edits)} edits to {raw_path}."
