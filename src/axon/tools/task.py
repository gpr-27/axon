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
        "Give the subagent a complete, self-contained prompt: it cannot see this conversation. "
        "A subagent has a step limit (default 25 model calls); at the limit it stops and reports what it found and "
        "what is left. For a large task, ask for more steps with max_steps or split it into several Task calls."
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
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "description": "Step limit for this subagent (default 25, max = the session's max_iterations)",
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

        steps = args.get("max_steps")
        if steps not in (None, ""):
            try:
                steps = int(steps)
            except (TypeError, ValueError):
                raise ToolError(f"max_steps must be a whole number of steps, got {steps!r}.") from None
        from axon.agent.subagent import run_subagent
        return run_subagent(
            prompt,
            parent=ctx.agent,
            max_iterations=steps or None,
            subagent_type=str(args.get("subagent_type") or "general-purpose"),
            description=str(args.get("description") or ""),
            call_id=ctx.tool_use_id,
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
        try:
            import time as _time
            from axon import storage
            from axon.session.library import record_run
            agent = getattr(ctx, "agent", None)
            path = storage.project_subdir(ctx.workspace, "plans") / f"{_time.strftime('%Y%m%d-%H%M%S')}-plan.md"
            path.write_text(plan + "\n", encoding="utf-8")
            record_run("plan", {"action": "approved", "source": "ExitPlanMode", "path": str(path), "plan": plan},
                       agent=agent, workspace=ctx.workspace)
        except Exception:
            pass
        return f"Plan proposed for approval:\n\n{plan}"
