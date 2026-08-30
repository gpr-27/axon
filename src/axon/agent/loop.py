"""
The ReAct Agent Loop: reason -> act -> observe -> iterate.
Enforces the 5 Invariants (Pairing, Batching, Verbatim Replay, Errors as Data, Interrupt Safety).
"""
from __future__ import annotations
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable
from axon.config import Settings
from axon.errors import ToolError
from axon.agent.state import Conversation, FileState, TodoState
from axon.agent.prompt import build_system
from axon.agent.context import ContextManager
from axon.tools.base import ToolContext
from axon.tools.registry import ToolRegistry
from axon.permissions.engine import PermissionEngine
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.providers.base import (
    AssistantTurn,
    LLMCallStart,
    Provider,
    StreamEvent,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

from axon.skills.manager import SkillManager

from axon.session.checkpoint import CheckpointManager

@dataclass
class TurnResult:
    final_text: str
    stop_reason: str
    iterations: int
    tool_calls_count: int
    usage: Usage

class Agent:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        permissions: PermissionEngine,
        context: ContextManager,
        session: SessionStore,
        ledger: Ledger,
        settings: Settings,
        skills: SkillManager | None = None,
        checkpoints: CheckpointManager | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        on_approval: Callable[[str, dict[str, Any], Any], str] | None = None,
    ) -> None:
        self.provider = provider
        self.registry = tools
        self.permissions = permissions
        self.context = context
        self.session = session
        self.ledger = ledger
        self.settings = settings
        self.skills = skills or SkillManager(settings.workspace)
        self.checkpoints = checkpoints or CheckpointManager(settings.workspace)
        self.on_event = on_event
        self.on_approval = on_approval

        self.conversation = Conversation()
        self.file_state = FileState()
        self.todos = TodoState()
        from axon.agent.state import MessageQueue
        self.message_queue: MessageQueue = MessageQueue()
        self.renderer: Any = None
        from axon.agent.subagent import SubagentManager
        self.subagents: SubagentManager = SubagentManager()

    def reset_for_new_session(self, session_id: str | None = None) -> str:
        """Completely refresh and restart everything from zero for a fresh session."""
        self.conversation = Conversation([])
        if hasattr(self, "ledger") and self.ledger is not None:
            self.ledger.clear()
        else:
            self.ledger = Ledger()
        if hasattr(self, "file_state") and self.file_state is not None:
            self.file_state.clear()
        if hasattr(self, "todos") and self.todos is not None:
            self.todos.clear()
        if hasattr(self, "message_queue") and self.message_queue is not None:
            self.message_queue.clear()
        if hasattr(self, "subagents") and self.subagents is not None:
            self.subagents.clear()
        if hasattr(self, "checkpoints") and self.checkpoints is not None:
            self.checkpoints.clear()

        # Open fresh session ID in store
        new_id = self.session.open(session_id)
        return new_id

    def run_turn(self, user_input: str) -> TurnResult:
        """Run complete ReAct loop until end_turn or iteration exhaustion."""
        self.conversation.append_user(user_input)
        self.session.append_user(user_input)

        total_tool_calls = 0
        total_usage = Usage()
        final_text = ""
        iterations = 0

        for iteration in range(self.settings.max_iterations):
            iterations += 1
            system_blocks = build_system(self.settings, self.registry, list(self.skills.skills.values()))
            tool_schemas = self.registry.schemas(
                provider_style="anthropic" if self.provider.name == "anthropic" else "openai"
            )

            # 1. Prepare context & apply cache markers
            self.context.prepare(self.conversation, system_blocks, tool_schemas, model=self.settings.model)

            # Calculate full payload token projection (system prompt + tools + messages)
            sys_chars = sum(len(str(b.get("text", ""))) for b in system_blocks)
            tool_chars = sum(len(str(s)) for s in tool_schemas)
            conv_tokens = self.conversation.token_estimate()
            total_prompt_tokens = conv_tokens + int((sys_chars + tool_chars) / 3.7)

            # Emit LLM step start event
            if self.on_event:
                self.on_event(
                    LLMCallStart(
                        iteration=iterations,
                        max_iterations=self.settings.max_iterations,
                        model=self.settings.model,
                        message_count=len(self.conversation.messages),
                        tokens_in=total_prompt_tokens,
                    )
                )

            # 2. Reason + Act via Provider Stream
            stream = self.provider.stream(
                model=self.settings.model,
                system=system_blocks,
                messages=self.conversation.messages,
                tools=tool_schemas,
                max_tokens=self.settings.max_tokens,
                effort=self.settings.effort,
                thinking=self.settings.thinking,
            )

            for event in stream:
                if self.on_event:
                    self.on_event(event)

            turn = self.provider.finalize()
            total_usage = total_usage + turn.usage
            self.ledger.record(self.settings.model, turn.usage)

            # 3. Record assistant turn verbatim (Law 3 / ADR-003)
            self.conversation.append_assistant(turn)
            self.session.append_turn(turn)
            final_text = turn.text

            # 4. Terminate if turn is not requesting tools
            if turn.stop_reason != "tool_use":
                return TurnResult(
                    final_text=final_text,
                    stop_reason=turn.stop_reason,
                    iterations=iterations,
                    tool_calls_count=total_tool_calls,
                    usage=total_usage,
                )

            # 5. Observe & Execute Tool Uses
            tool_uses = turn.tool_uses
            total_tool_calls += len(tool_uses)

            try:
                results = self._execute_batch(tool_uses)
            except KeyboardInterrupt:
                # Law 5: Interrupt still closes the turn
                results = [
                    ToolResultBlock(
                        tool_use_id=tu.id,
                        content="Interrupted by user before completion.",
                        is_error=True,
                    )
                    for tu in tool_uses
                ]
                self.conversation.append_tool_results(
                    results,
                    provider_encoder=self.provider.encode_tool_results if self.provider.name != "anthropic" else None,
                )
                self.session.append_results(results)
                raise
            except Exception as e:
                # Ensure turn state remains valid even on unexpected tool batch failure
                results = [
                    ToolResultBlock(
                        tool_use_id=tu.id,
                        content=f"Tool execution failed unexpectedly: {e}",
                        is_error=True,
                    )
                    for tu in tool_uses
                ]
                self.conversation.append_tool_results(
                    results,
                    provider_encoder=self.provider.encode_tool_results if self.provider.name != "anthropic" else None,
                )
                self.session.append_results(results)
                raise

            # 6. Feed back all results in ONE message (Law 2 / Batching)
            self.conversation.append_tool_results(
                results,
                provider_encoder=self.provider.encode_tool_results if self.provider.name != "anthropic" else None,
            )
            self.session.append_results(results)

        return TurnResult(
            final_text=final_text + "\n\n[Axon reached maximum iteration ceiling without concluding.]",
            stop_reason="max_iterations",
            iterations=iterations,
            tool_calls_count=total_tool_calls,
            usage=total_usage,
        )

    def _execute_batch(self, tool_uses: list[ToolUseBlock]) -> list[ToolResultBlock]:
        """
        Executes tool uses. Law 1: pre-allocates slots so every tool_use is guaranteed a result.
        Runs concurrently if all tools in batch are readonly.
        """
        if len(tool_uses) > 1 and self.on_event:
            self.on_event(ToolBatchStart(total_count=len(tool_uses), tools=tool_uses))

        results: list[ToolResultBlock | None] = [None] * len(tool_uses)

        all_readonly = False
        try:
            all_readonly = all(self.registry.get(tu.name).readonly for tu in tool_uses)
        except Exception:
            all_readonly = False

        has_tasks = any(tu.name == "Task" for tu in tool_uses)
        if all_readonly and (len(tool_uses) > 1 or has_tasks):
            # Parallel/asynchronous execution for read-only batches and subagents
            with ThreadPoolExecutor(max_workers=min(6, len(tool_uses))) as pool:
                future_map = {pool.submit(self._run_one, tu): idx for idx, tu in enumerate(tool_uses)}
                if has_tasks and hasattr(self, "subagents") and sys.stdin.isatty():
                    from axon.ui.subagent_monitor import run_live_subagent_monitor
                    run_live_subagent_monitor(future_map, self.subagents, agent=self)
                for fut in as_completed(future_map):
                    idx = future_map[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:
                        err_res = ToolResultBlock(
                            tool_use_id=tool_uses[idx].id,
                            content=f"Tool error: {e}",
                            is_error=True,
                        )
                        results[idx] = err_res
                        if self.on_event:
                            self.on_event(
                                ToolExecutionResult(
                                    id=tool_uses[idx].id,
                                    name=tool_uses[idx].name,
                                    input=tool_uses[idx].input,
                                    content=err_res.content,
                                    is_error=True,
                                )
                            )
        else:
            # Serial execution in model's order
            for idx, tu in enumerate(tool_uses):
                results[idx] = self._run_one(tu)

        # Guarantee Law 1
        final_results: list[ToolResultBlock] = []
        for r, tu in zip(results, tool_uses):
            if r is not None:
                final_results.append(r)
            else:
                fallback_err = ToolResultBlock(
                    tool_use_id=tu.id,
                    content="Tool execution did not produce a result.",
                    is_error=True,
                )
                final_results.append(fallback_err)
                if self.on_event:
                    self.on_event(
                        ToolExecutionResult(
                            id=tu.id,
                            name=tu.name,
                            input=tu.input,
                            content=fallback_err.content,
                            is_error=True,
                        )
                    )
        has_tasks = any(tu.name == "Task" for tu in tool_uses)
        if has_tasks and hasattr(self, "subagents") and self.subagents.all_tasks():
            from axon.ui.render import render_subagent_dashboard
            render_subagent_dashboard(self.subagents.all_tasks())

        return final_results

    def _run_one(self, block: ToolUseBlock) -> ToolResultBlock:
        """Pipeline: Permission Check -> Tool Execution -> Error Transformation (Law 4)."""
        if self.on_event:
            self.on_event(ToolExecutionStart(id=block.id, name=block.name, input=block.input))

        try:
            tool = self.registry.get(block.name)
        except ToolError as e:
            res = ToolResultBlock(tool_use_id=block.id, content=str(e), is_error=True)
            if self.on_event:
                self.on_event(ToolExecutionResult(id=block.id, name=block.name, input=block.input, content=res.content, is_error=True))
            return res

        # Permission check
        decision = self.permissions.check(tool, block.input, self.settings.mode)
        if decision.outcome == "deny":
            res = ToolResultBlock(tool_use_id=block.id, content=f"Permission Denied: {decision.reason}", is_error=True)
            if self.on_event:
                self.on_event(ToolExecutionResult(id=block.id, name=block.name, input=block.input, content=res.content, is_error=True))
            return res

        if decision.outcome == "ask" and self.on_approval:
            approval = self.on_approval(tool.name, block.input, decision)
            if approval == "deny":
                res = ToolResultBlock(tool_use_id=block.id, content="User declined this action.", is_error=True)
                if self.on_event:
                    self.on_event(ToolExecutionResult(id=block.id, name=block.name, input=block.input, content=res.content, is_error=True))
                return res
            elif approval == "always":
                from axon.permissions.rules import Rule
                self.permissions.grant_persistent(Rule(tool=tool.name, pattern="*"), self.settings.workspace)

        ctx = ToolContext(
            workspace=self.settings.workspace,
            file_state=self.file_state,
            todos=self.todos,
            settings=self.settings,
            ledger=self.ledger,
            agent=self,
            checkpoints=self.checkpoints,
        )

        try:
            out = tool.run(block.input, ctx)
            res = ToolResultBlock(tool_use_id=block.id, content=str(out), is_error=False)
        except ToolError as e:
            # Law 4: Errors are data
            res = ToolResultBlock(tool_use_id=block.id, content=str(e), is_error=True)
        except Exception as e:
            res = ToolResultBlock(tool_use_id=block.id, content=f"Unexpected {type(e).__name__}: {e}", is_error=True)

        if self.on_event:
            self.on_event(ToolExecutionResult(id=block.id, name=block.name, input=block.input, content=res.content, is_error=res.is_error))
        return res
