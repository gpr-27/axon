"""
TodoWrite tool for persistent externalized plan tracking across compaction.

The list is persisted per session to ``axon.storage.todos_dir(ws)/<session_id>.json`` (see
``TodoState.bind``) so ``--continue`` / ``--resume`` restore it.
"""
from __future__ import annotations
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

def _bind_persistence(ctx: ToolContext) -> None:
    """Attach session persistence to the todo list (no-op when already bound or no session)."""
    todos = ctx.todos
    if getattr(todos, "bound", True) or not hasattr(todos, "bind"):
        return
    session = getattr(getattr(ctx, "agent", None), "session", None)
    if session is None or not hasattr(session, "active_session_id"):
        return
    todos.bind(ctx.workspace, lambda: getattr(session, "active_session_id", "") or "")


class TodoWriteTool(Tool):
    name: ClassVar[str] = "TodoWrite"
    description: ClassVar[str] = (
        "Create/update the visible task list for multi-step work (3+ steps, or when the user gives several tasks). "
        "Send the COMPLETE list every time (it replaces the previous one). Status is 'pending', 'in_progress' or "
        "'completed'; keep exactly one item in_progress while working and mark items completed as soon as they are "
        "done. Skip it for trivial single-step requests."
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
                        "content": {"type": "string", "minLength": 1},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "activeForm": {"type": "string"},
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
        todos = args.get("todos")
        if not isinstance(todos, list):
            raise ToolError("TodoWrite requires 'todos': a list of {content, status} objects.")
        valid = {"pending", "in_progress", "completed"}
        clean = []
        for i, it in enumerate(todos, 1):
            if not isinstance(it, dict):
                raise ToolError(f"Todo #{i} must be an object with 'content' and 'status'.")
            content = str(it.get("content") or "").strip()
            status = str(it.get("status") or "pending").strip().lower()
            if not content:
                raise ToolError(f"Todo #{i} has empty 'content'.")
            if status not in valid:
                raise ToolError(f"Todo #{i} has invalid status '{status}'. Use pending, in_progress or completed.")
            entry = {"id": str(it.get("id") or i), "content": content, "status": status}
            active = str(it.get("activeForm") or "").strip()
            if active:
                entry["activeForm"] = active
            clean.append(entry)
        todos = clean
        _bind_persistence(ctx)
        try:
            ctx.todos.replace(todos)
        except ValueError as e:
            raise ToolError(f"TodoWrite failed: {e}") from e
        return f"Updated todos ({len(ctx.todos.items)} items):\n{ctx.todos.render()}"

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return f"error: {output.splitlines()[0] if output else ''}"
        items = args.get("todos") or []
        done = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "completed")
        active = sum(1 for t in items if isinstance(t, dict) and t.get("status") == "in_progress")
        return f"{done}/{len(items)} completed" + (f" · {active} in progress" if active else "")
