"""
TodoWrite tool for persistent externalized plan tracking across compaction.
"""
from __future__ import annotations
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class TodoWriteTool(Tool):
    name: ClassVar[str] = "TodoWrite"
    description: ClassVar[str] = (
        "Maintain the visible task plan. "
        "Accepts a full list of todos with status 'pending', 'in_progress', or 'completed'. "
        "Enforces at most one item in_progress."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
                "description": "The complete replacement list of todos",
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        todos = args.get("todos", [])
        try:
            ctx.todos.replace(todos)
        except ValueError as e:
            raise ToolError(f"TodoWrite failed: {e}") from e
        return f"Updated todos ({len(ctx.todos.items)} items):\n{ctx.todos.render()}"
