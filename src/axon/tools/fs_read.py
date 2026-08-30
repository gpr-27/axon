"""
Read tool implementation with line numbers and FileState registration.
"""
from __future__ import annotations
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

class ReadTool(Tool):
    name: ClassVar[str] = "Read"
    description: ClassVar[str] = (
        "Read a file from the workspace. Returns contents with 1-based line numbers. "
        "Every successful read records file identity, making future edits legal. "
        "Use offset and limit for paging large files."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative path"},
            "offset": {"type": "integer", "description": "1-based first line to read (default: 1)"},
            "limit": {"type": "integer", "description": "Max lines to read (default: 10000)"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        if not raw_path:
            raise ToolError("Read requires 'path' argument.")

        p = resolve_in_workspace(ctx.workspace, raw_path, allow_git_read=True)
        if not p.exists():
            raise ToolError(f"File not found: {raw_path}")
        if not p.is_file():
            raise ToolError(f"Path is a directory, not a file: {raw_path}")

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise ToolError(f"Failed to read file {raw_path}: {e}") from e

        # Record into FileState for read-before-edit invariant
        ctx.file_state.record_read(p)

        lines = content.splitlines()
        total_lines = len(lines)
        offset = max(1, int(args.get("offset") or 1))
        limit = min(50000, max(1, int(args.get("limit") or 10000)))

        start_idx = offset - 1
        end_idx = min(total_lines, start_idx + limit)

        if start_idx >= total_lines:
            return f"[File has {total_lines} lines. Offset {offset} is beyond end of file.]"

        numbered_lines = []
        for i in range(start_idx, end_idx):
            line_num = i + 1
            line_text = lines[i]
            # Truncate very long single lines to 2000 chars
            if len(line_text) > 2000:
                line_text = line_text[:1990] + " ...[truncated]"
            numbered_lines.append(f"{line_num:6d}→{line_text}")

        result = "\n".join(numbered_lines)
        if end_idx < total_lines:
            remaining = total_lines - end_idx
            result += f"\n\n[... {remaining} lines remaining. Use offset={end_idx + 1} to continue reading ...]"

        return result
