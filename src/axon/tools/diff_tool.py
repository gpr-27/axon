"""
Diff tool: compare two files or text contents and produce structured unified diffs with statistics.
"""
from __future__ import annotations
import difflib
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

class DiffTool(Tool):
    name: ClassVar[str] = "Diff"
    description: ClassVar[str] = (
        "Compare two files in the workspace and generate a formatted unified diff with addition/deletion statistics."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path_a": {"type": "string", "description": "Path to the first (original) file"},
            "path_b": {"type": "string", "description": "Path to the second (modified) file"},
        },
        "required": ["path_a", "path_b"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_a = args.get("path_a", "")
        raw_b = args.get("path_b", "")

        if not raw_a or not raw_b:
            raise ToolError("Diff requires 'path_a' and 'path_b'.")

        pa = resolve_in_workspace(ctx.workspace, raw_a, allow_git_read=True)
        pb = resolve_in_workspace(ctx.workspace, raw_b, allow_git_read=True)

        if not pa.exists():
            raise ToolError(f"File not found: {raw_a}")
        if not pb.exists():
            raise ToolError(f"File not found: {raw_b}")

        lines_a = pa.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines_b = pb.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

        diff = list(difflib.unified_diff(lines_a, lines_b, fromfile=raw_a, tofile=raw_b))
        if not diff:
            return f"Files '{raw_a}' and '{raw_b}' are identical (no differences)."

        additions = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))

        header = f"=== Diff: {raw_a} ↔ {raw_b} (+{additions} / -{deletions} lines) ===\n"
        return header + "".join(diff[:120])
