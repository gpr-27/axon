"""
Task subagent tool and ExitPlanMode tool.
"""
from __future__ import annotations
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class TaskTool(Tool):
    name: ClassVar[str] = "Task"
    description: ClassVar[str] = (
        "Delegate a task to a subagent with its own isolated context and returns only its final report. "
        "Choose subagent_type from the subagents listed in the system prompt (general-purpose, explore, plan, "
        "code-reviewer, test-runner, or a custom agent). Independent Task calls in one turn run in parallel. "
        "Give the subagent a complete, self-contained prompt: it cannot see this conversation."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "A short (3-6 word) label for the task"},
            "prompt": {"type": "string", "description": "The complete task for the subagent to perform"},
            "subagent_type": {
                "type": "string",
                "description": "Agent type to use (default: general-purpose)",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }
    # Not read-only: subagents may edit files. The agent loop decides explicitly that
    # Task calls may run in parallel, and each subagent tool call is permission-checked.
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = str(args.get("prompt") or args.get("subtask") or "").strip()
        if not prompt:
            raise ToolError("Task tool requires 'prompt'.")
        if ctx.agent is None:
            raise ToolError("Subagent execution requires parent agent context.")

        from axon.agent.subagent import run_subagent
        return run_subagent(
            prompt,
            parent=ctx.agent,
            subagent_type=str(args.get("subagent_type") or "general-purpose"),
            description=str(args.get("description") or ""),
        )


class ExitPlanModeTool(Tool):
    name: ClassVar[str] = "ExitPlanMode"
    description: ClassVar[str] = (
        "Propose a concrete plan and exit 'plan' mode. "
        "Presents the finalized implementation plan for human review."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "plan": {"type": "string", "description": "The complete structured implementation plan"},
        },
        "required": ["plan"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        plan = args.get("plan", "")
        if not plan:
            raise ToolError("ExitPlanMode requires 'plan'.")
        return f"Plan proposed for approval:\n\n{plan}"
