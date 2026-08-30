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
        "Spawn a sub-agent with isolated context to explore unfamiliar code, search, or answer questions. "
        "Returns only the subagent's final conclusions, saving parent context."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The specific research prompt for the subagent"},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        prompt = args.get("prompt", "")
        if not prompt:
            raise ToolError("Task tool requires 'prompt'.")
        if ctx.agent is None:
            raise ToolError("Subagent execution requires parent agent context.")

        from axon.agent.subagent import run_subagent
        return run_subagent(prompt, parent=ctx.agent)


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
