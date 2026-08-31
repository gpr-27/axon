"""
Subagent runner for Task tool with isolated context, Claude-style task tracking, and thread-safe dashboard metrics.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from axon.agent.state import Conversation
from axon.errors import ToolError

if TYPE_CHECKING:
    from axon.agent.loop import Agent
    from axon.providers.base import StreamEvent

@dataclass
class SubagentTask:
    id: str
    index: int
    title: str
    prompt: str
    status: str = "pending"  # pending, running, completed, exhausted, error
    steps: int = 0
    max_steps: int = 15
    start_time: float = 0.0
    elapsed_s: float = 0.0
    conversation: Conversation = field(default_factory=Conversation)
    events: list[StreamEvent] = field(default_factory=list)
    result_text: str = ""
    error_msg: str | None = None
    tokens_consumed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    live_logs: list[str] = field(default_factory=list)

    def add_log(self, msg: str) -> None:
        self.live_logs.append(msg)
        if len(self.live_logs) > 30:
            self.live_logs.pop(0)

class SubagentManager:
    """Thread-safe manager for tracking subagents and rendering Claude-style progress."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: list[SubagentTask] = []
        self._counter: int = 0
        self.on_update: Callable[[list[SubagentTask]], None] | None = None

    def register(self, prompt: str, max_steps: int = 15) -> SubagentTask:
        with self._lock:
            self._counter += 1
            idx = self._counter
            # Extract clean title from prompt
            first_line = prompt.strip().splitlines()[0]
            clean_title = first_line.strip("#* -:").replace("[Subtask]", "").strip()
            if len(clean_title) > 42:
                clean_title = clean_title[:39] + "..."
            if not clean_title:
                clean_title = f"Task #{idx}"

            task = SubagentTask(
                id=f"sub-{idx}",
                index=idx,
                title=clean_title,
                prompt=prompt,
                status="running",
                max_steps=max_steps,
                start_time=time.time(),
            )
            self._tasks.append(task)
            self._notify()
            return task

    def update_progress(self, task_id: str, steps: int) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.steps = steps
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    break
            self._notify()

    def complete(self, task_id: str, result_text: str, conversation: Conversation, status: str = "completed", usage: Any = None) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.status = status
                    t.result_text = result_text
                    t.conversation = conversation
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    if usage:
                        t.input_tokens = getattr(usage, "input", 0) or 0
                        t.output_tokens = getattr(usage, "output", 0) or 0
                        t.tokens_consumed = t.input_tokens + t.output_tokens
                    break
            self._notify()

    def total_tokens(self) -> int:
        with self._lock:
            return sum(t.tokens_consumed for t in self._tasks)

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.status = "error"
                    t.error_msg = error
                    t.elapsed_s = max(0.1, time.time() - t.start_time)
                    break
            self._notify()

    def _notify(self) -> None:
        if self.on_update:
            try:
                self.on_update(list(self._tasks))
            except Exception:
                pass

    def all_tasks(self) -> list[SubagentTask]:
        with self._lock:
            return list(self._tasks)

    def get_task(self, query: str) -> SubagentTask | None:
        with self._lock:
            q = str(query).strip().lower()
            for t in self._tasks:
                if t.id == q or str(t.index) == q or t.title.lower().startswith(q):
                    return t
            return None

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._counter = 0

def run_subagent(
    prompt: str,
    parent: Agent,
    max_iterations: int = 15,
) -> str:
    """
    Spawns an isolated sub-agent loop.
    Excludes Task tool to enforce depth cap 1.
    All events and conversations are isolated in SubagentTask to prevent stdout interleaving.
    """
    if not hasattr(parent, "subagents") or parent.subagents is None:
        parent.subagents = SubagentManager()

    task = parent.subagents.register(prompt, max_steps=max_iterations)

    # Isolated event handler for this subagent (powers live monitor while preventing stdout collision)
    def sub_on_event(event: StreamEvent) -> None:
        task.events.append(event)
        from axon.providers.base import LLMCallStart, ThinkingDelta, ToolExecutionStart, ToolExecutionResult, TextDelta
        if isinstance(event, LLMCallStart):
            parent.subagents.update_progress(task.id, event.iteration)
            task.add_log(f"⚡ Step {event.iteration} LLM call...")
        elif isinstance(event, ToolExecutionStart):
            arg_str = str(event.input)
            if len(arg_str) > 50:
                arg_str = arg_str[:47] + "..."
            task.add_log(f"⏺ {event.name}({arg_str})")
        elif isinstance(event, ToolExecutionResult):
            st = "Error" if event.is_error else "Result"
            c_str = str(event.content).strip().replace("\n", " ")
            if len(c_str) > 55:
                c_str = c_str[:52] + "..."
            task.add_log(f"  └─ {st}: {c_str}")
        elif isinstance(event, ThinkingDelta):
            th = (getattr(event, "text", "") or getattr(event, "delta", "")).strip()
            if th and len(th) > 10:
                task.add_log(f"✻ Thinking: {th[:55]}...")
        elif isinstance(event, TextDelta):
            txt = getattr(event, "text", "").strip()
            if txt and len(txt) > 15:
                task.add_log(f"✍️ Generating: {txt[:55]}...")

    is_plan_mode = getattr(parent.settings, "mode", "default") == "plan"
    sub_registry = parent.registry.subset(
        names=[t.name for t in parent.registry.all_tools() if t.name != "Task"],
        readonly_only=is_plan_mode,
    )

    from axon.agent.loop import Agent
    from axon.session.store import SessionStore
    from axon.agent.worktree import WorktreeManager, WorktreeInfo

    worktree_info: WorktreeInfo | None = None
    sub_ws = parent.settings.workspace
    if getattr(parent.settings, "isolate_worktrees", False):
        try:
            worktree_info = WorktreeManager.create_worktree(parent.settings.workspace, task.id)
            if worktree_info:
                sub_ws = worktree_info.worktree_path
                task.add_log(f"🌿 Git worktree isolated: {sub_ws.name}")
        except Exception:
            worktree_info = None

    parent_sess_id = parent.session.active_session_id.rsplit("_sub_", 1)[0]
    sub_sess_id = f"{parent_sess_id}_sub_{task.index}"
    sub_store = SessionStore(sub_ws, session_dir=parent.session.session_dir)
    sub_store.open(sub_sess_id)

    sub_agent = Agent(
        provider=parent.provider,
        tools=sub_registry,
        permissions=parent.permissions,
        context=parent.context,
        session=sub_store,
        ledger=parent.ledger,
        settings=parent.settings.model_copy(update={"max_iterations": max_iterations, "workspace": sub_ws}),
        on_event=sub_on_event,
    )

    try:
        sub_prompt = (
            f"[Subagent Task #{task.index}]: {prompt}\n\n"
            "Guidelines:\n"
            "• You have full access to workspace tools (Write, Edit, MultiEdit, Patch, Bash, Ls, Grep, etc.). Perform any requested actions, file modifications, or commands directly.\n"
            "• Format your final findings/summary cleanly and neatly for terminal viewing:\n"
            "  - Executive Summary: 1-2 concise sentences of what was accomplished.\n"
            "  - Key Actions & Findings: Bullet points with bold titles, exact file paths, lines, and metrics.\n"
            "  - Structured Data: Use clean Markdown tables (| Item | Details |) when comparing multiple items or components.\n"
            "  - Conclusion: Clear outcome, PASS/FAIL status, or recommended next steps."
        )
        result = sub_agent.run_turn(sub_prompt)
        final_text = result.final_text
        sub_usage = result.usage
        tokens_total = (sub_usage.input or 0) + (sub_usage.output or 0)

        # Merge worktree changes back if isolated
        if worktree_info:
            merged_ok, merge_msg = WorktreeManager.merge_back(worktree_info)
            if merged_ok and "applied" in merge_msg.lower():
                final_text += f"\n\n✓ Worktree changes merged back to workspace."
            WorktreeManager.cleanup(worktree_info)

        # Record subagent tokens and cost into parent ledger so Main session total includes all subagents
        if hasattr(parent, "ledger") and parent.ledger is not None:
            try:
                parent.ledger.record(parent.settings.model, sub_usage)
            except Exception:
                pass

        if not final_text:
            if result.iterations >= max_iterations:
                parent.subagents.complete(task.id, "[Exhausted iteration ceiling]", sub_agent.conversation, status="exhausted", usage=sub_usage)
                return f"[Subagent #{task.index} ({task.title}) | {tokens_total:,} tokens]: reached maximum iteration ceiling ({max_iterations} steps) without concluding."
            else:
                parent.subagents.complete(task.id, "No text output", sub_agent.conversation, status="completed", usage=sub_usage)
                return f"[Subagent #{task.index} ({task.title}) | {tokens_total:,} tokens]: completed ({result.iterations} steps) with no text output."

        parent.subagents.complete(task.id, final_text, sub_agent.conversation, status="completed", usage=sub_usage)
        return f"[Subagent #{task.index} Result ({task.title}) | {tokens_total:,} tokens (in: {sub_usage.input:,} · out: {sub_usage.output:,})]:\n{final_text}"
    except Exception as e:
        if worktree_info:
            try:
                WorktreeManager.cleanup(worktree_info)
            except Exception:
                pass
        parent.subagents.fail(task.id, str(e))
        return f"[Subagent #{task.index} failed: {e}]"

def sync_subagents_for_session(agent: Agent) -> None:
    """Ensure agent.subagents contains all subagent tasks belonging to the active session."""
    import json
    import re
    from axon.agent.subagent import SubagentManager
    if not hasattr(agent, "subagents") or agent.subagents is None or not isinstance(agent.subagents, SubagentManager):
        agent.subagents = SubagentManager()
    else:
        agent.subagents.clear()
    parent_sess_id = agent.session.active_session_id.rsplit("_sub_", 1)[0]
    seen_indices: set[int] = set()

    # 1. Parse Task tool calls from conversation messages (Anthropic + OpenAI formats)
    if hasattr(agent, "conversation") and agent.conversation.messages:
        from axon.providers.base import ToolUseBlock, ToolResultBlock
        task_idx = 0
        for m in agent.conversation.messages:
            role = m.get("role")
            content = m.get("content")
            task_calls: list[tuple[str, dict[str, Any]]] = []  # (tool_use_id, input_dict)

            # Anthropic style: list of blocks in content
            if role == "assistant" and isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") == "Task":
                        task_calls.append((blk.get("id") or "", blk.get("input", {})))
                    elif isinstance(blk, ToolUseBlock) or type(blk).__name__ == "ToolUseBlock":
                        if getattr(blk, "name", "") == "Task":
                            task_calls.append((getattr(blk, "id", "") or "", getattr(blk, "input", {})))

            # OpenAI style: tool_calls array in message
            if role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn_name = ""
                    fn_args = {}
                    if isinstance(tc, dict):
                        fn_obj = tc.get("function", {})
                        fn_name = fn_obj.get("name", "")
                        raw_args = fn_obj.get("arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                fn_args = json.loads(raw_args)
                            except Exception:
                                fn_args = {"prompt": raw_args}
                        elif isinstance(raw_args, dict):
                            fn_args = raw_args
                        tc_id = tc.get("id", "")
                    else:
                        fn_name = getattr(getattr(tc, "function", None), "name", "")
                        raw_args = getattr(getattr(tc, "function", None), "arguments", {})
                        if isinstance(raw_args, str):
                            try:
                                fn_args = json.loads(raw_args)
                            except Exception:
                                fn_args = {"prompt": raw_args}
                        elif isinstance(raw_args, dict):
                            fn_args = raw_args
                        tc_id = getattr(tc, "id", "")
                    if fn_name == "Task":
                        task_calls.append((tc_id, fn_args))

            for tu_id, inp in task_calls:
                task_idx += 1
                seen_indices.add(task_idx)
                prompt_txt = inp.get("prompt") or inp.get("subtask") or f"Subtask {task_idx}"
                task = agent.subagents.register(prompt_txt)
                task.status = "completed"
                if tu_id:
                    for m2 in agent.conversation.messages:
                        m2_content = m2.get("content")
                        if m2.get("role") == "user" and isinstance(m2_content, list):
                            for res_blk in m2_content:
                                res_match = False
                                res_content = ""
                                if isinstance(res_blk, dict):
                                    if res_blk.get("tool_use_id") == tu_id:
                                        res_match = True
                                        res_content = res_blk.get("content", "")
                                elif isinstance(res_blk, ToolResultBlock) or type(res_blk).__name__ == "ToolResultBlock":
                                    if getattr(res_blk, "tool_use_id", None) == tu_id:
                                        res_match = True
                                        res_content = getattr(res_blk, "content", "")

                                if res_match:
                                    task.result_text = res_content
                                    from axon.agent.state import Conversation
                                    sub_file = getattr(agent, "session", None) and (agent.session.session_dir / f"{parent_sess_id}_sub_{task_idx}.jsonl")
                                    if sub_file and sub_file.exists():
                                        try:
                                            task.conversation = agent.session.read_conversation(f"{parent_sess_id}_sub_{task_idx}")
                                        except Exception:
                                            task.conversation = Conversation([
                                                {"role": "user", "content": prompt_txt},
                                                {"role": "assistant", "content": task.result_text},
                                            ])
                                    else:
                                        task.conversation = Conversation([
                                            {"role": "user", "content": prompt_txt},
                                            {"role": "assistant", "content": task.result_text},
                                        ])
                                    break

    # 2. Discover any additional subagent session files on disk (_sub_*.jsonl)
    s_dir = getattr(agent, "session", None) and getattr(agent.session, "session_dir", None)
    if s_dir and s_dir.exists():
        pattern = f"{parent_sess_id}_sub_*.jsonl"
        for sub_path in sorted(s_dir.glob(pattern)):
            stem = sub_path.stem
            m_idx = re.search(r"_sub_(\d+)$", stem)
            if not m_idx:
                continue
            f_idx = int(m_idx.group(1))
            if f_idx in seen_indices:
                continue
            seen_indices.add(f_idx)
            try:
                sub_conv = agent.session.read_conversation(stem)
                p_text = ""
                r_text = ""
                for sm in sub_conv.messages:
                    if sm.get("role") == "user" and not p_text:
                        p_text = sm.get("content", "")
                        if isinstance(p_text, list):
                            p_text = "".join(str(x) for x in p_text)
                    elif sm.get("role") == "assistant":
                        c = sm.get("content", "")
                        if isinstance(c, str) and c:
                            r_text = c
                if not p_text:
                    p_text = f"Subagent Task #{f_idx}"
                task = agent.subagents.register(p_text)
                task.index = f_idx
                task.id = f"sub-{f_idx}"
                task.status = "completed"
                task.result_text = r_text
                task.conversation = sub_conv
            except Exception:
                pass

    # 3. Synchronize token counts and costs for each task
    model_name = getattr(agent.settings, "model", "claude-opus-5") if hasattr(agent, "settings") and isinstance(getattr(agent.settings, "model", None), str) else "claude-opus-5"
    for t in agent.subagents.all_tasks():
        sub_stem = f"{parent_sess_id}_sub_{t.index}"
        if getattr(agent, "session", None) and hasattr(agent.session, "load_ledger"):
            try:
                sub_l = agent.session.load_ledger(sub_stem, model_name)
                if sub_l.total_input_tokens + sub_l.total_output_tokens > 0:
                    t.input_tokens = sub_l.total_input_tokens
                    t.output_tokens = sub_l.total_output_tokens
                    t.tokens_consumed = t.input_tokens + t.output_tokens
            except Exception:
                pass
        if getattr(t, "tokens_consumed", 0) == 0 and t.result_text:
            m_tok = re.search(r"(\d[\d,]*)\s*tokens\s*\(in:\s*(\d[\d,]*)\s*·\s*out:\s*(\d[\d,]*)\)", t.result_text)
            if m_tok:
                t.tokens_consumed = int(m_tok.group(1).replace(",", ""))
                t.input_tokens = int(m_tok.group(2).replace(",", ""))
                t.output_tokens = int(m_tok.group(3).replace(",", ""))

