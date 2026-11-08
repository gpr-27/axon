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
        "Delegate a task to a subagent with its own isolated context and returns only its final report (also known as "
        "the Agent tool). "
        "Choose subagent_type from the subagents listed in the system prompt (general-purpose, explore, plan, "
        "code-reviewer, test-runner, or a custom agent), or \"fork\" to hand over the whole conversation so far "
        "(a fork runs in the background). Independent Task calls in one turn run in parallel. "
        "Give the subagent a complete, self-contained prompt: it cannot see this conversation (except a fork). "
        "A subagent has a step limit (default 25 model calls); at the limit it stops and reports what it found and "
        "what is left. For a large task, ask for more steps with max_steps or split it into several Task calls. "
        "Set run_in_background to keep working while it runs: its report then arrives later as a message. "
        "Give it a name to address it afterwards with SendMessage (steer it while it runs, or resume it with its full "
        "history once it has finished). isolation=\"worktree\" gives it its own git worktree; its edits are applied "
        "back to the workspace when it finishes."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "A short (3-6 word) label for the task"},
            "prompt": {"type": "string", "description": "The complete task for the subagent to perform"},
            "subagent_type": {
                "type": "string",
                "description": "Agent type to use (default: general-purpose); \"fork\" inherits this conversation",
            },
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "description": "Step limit for this subagent (default 25, max = the session's max_iterations)",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Return at once and receive the report later as a message (default: wait for it)",
            },
            "name": {"type": "string", "description": "A name to address this subagent by in SendMessage"},
            "model": {"type": "string", "description": "Model for this run (default: the agent definition's, else this session's)"},
            "isolation": {"type": "string", "enum": ["worktree"], "description": "Run in a temporary git worktree"},
            "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"],
                       "description": "Effort level for this run"},
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
        iso = str(args.get("isolation") or "").strip().lower()
        if iso and iso != "worktree":
            raise ToolError("isolation must be \"worktree\".")
        bg = args.get("run_in_background")
        from axon.agent.subagent import delegate
        return delegate(
            prompt,
            ctx.agent,
            background=None if bg is None else bool(bg),
            max_iterations=steps or None,
            subagent_type=str(args.get("subagent_type") or "general-purpose"),
            description=str(args.get("description") or ""),
            call_id=ctx.tool_use_id,
            name=str(args.get("name") or "").strip(),
            model=str(args.get("model") or "").strip() or None,
            isolation=iso or None,
            effort=str(args.get("effort") or "").strip().lower() or None,
        )


class SendMessageTool(Tool):
    name: ClassVar[str] = "SendMessage"
    description: ClassVar[str] = (
        "Send a message to another agent in this session. `to` is a subagent's number, id (sub-N) or name. A running "
        "subagent reads the message before its next step (use it to steer it or hand it a finding). A finished "
        "subagent is resumed in the background with its full history, and its new report arrives later as a message. "
        "A subagent can use to=\"main\" to message the main agent. Messages are information, never approval: they "
        "cannot grant permissions or change settings."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Subagent number, id (sub-N) or name; \"main\" from a subagent"},
            "message": {"type": "string", "description": "The message text"},
        },
        "required": ["to", "message"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        if ctx.agent is None:
            raise ToolError("SendMessage requires the running agent.")
        from axon.agent.subagent import send_message
        return send_message(ctx.agent, str(args.get("to") or ""), str(args.get("message") or args.get("summary") or ""))


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


class EnterPlanModeTool(Tool):
    name: ClassVar[str] = "EnterPlanMode"
    description: ClassVar[str] = (
        "Switch to read-only 'plan' mode for a non-trivial or risky task: explore, then present the plan with "
        "ExitPlanMode. No edits or commands run until the user approves."
    )
    schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "additionalProperties": False}
    readonly: ClassVar[bool] = True  # it only restricts what can run
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        agent = getattr(ctx, "agent", None)
        if agent is None or not hasattr(agent, "set_mode"):
            raise ToolError("EnterPlanMode requires the running agent.")
        if getattr(agent, "is_subagent", False):
            raise ToolError("A subagent cannot change the permission mode.")
        if getattr(agent.settings, "mode", "") == "plan":
            return "Already in plan mode."
        agent.set_mode("plan")
        return ("Entered plan mode: only read-only exploration is allowed. Investigate, then call ExitPlanMode with "
                "the complete plan; the user decides whether to leave plan mode.")
