"""Agent loop correctness: pairing on interrupts/errors, limits, budgets, compaction, tracing."""
from __future__ import annotations
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axon.agent.loop import tool_rationales
from axon.agent.state import Conversation
from axon.agent.trace import (
    BudgetEvent, CacheStatus, CompactionEvent, LLMCallEnd, PermissionDecisionEvent, ToolTiming,
    load_traces,
)
from axon.errors import ProviderError
from axon.providers.base import (
    AssistantTurn, TextBlock, TextDelta, ThinkingBlock, ToolUseBlock, ToolUseStart, Usage,
)
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _valid_alternation(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        if m.get("role") == "assistant" and not m.get("content") and not m.get("tool_calls"):
            return False
    conv = Conversation(messages)
    conv.validate()
    return True


class InterruptingProvider(FakeProvider):
    """Streams some text and a partial tool call, then the user hits Ctrl+C."""

    def stream(self, **kw: Any):
        self.requests.append(kw)
        if len(self.requests) == 1:
            yield TextDelta("Partial ans")
            yield ToolUseStart("t_partial", "Write")
            raise KeyboardInterrupt()
        yield from super().stream(**kw)

    def finalize(self) -> AssistantTurn:
        if self._last is None:
            raise RuntimeError("no turn")
        return self._last


def test_ctrl_c_mid_stream_keeps_conversation_valid(workspace: Path) -> None:
    prov = InterruptingProvider(scripted("second answer"))
    agent = make_agent(prov, workspace)
    res = agent.run_turn("first")
    assert res.stop_reason == "interrupted"
    assert "Partial ans" in res.final_text
    last = agent.conversation.messages[-1]
    assert last["role"] == "assistant" and last["content"]
    # The partial tool call must never be replayed.
    assert not any(b.get("type") == "tool_use" for b in last["content"])
    assert _valid_alternation(agent.conversation.messages)
    res2 = agent.run_turn("again")
    assert res2.final_text == "second answer"


class FailingProvider(FakeProvider):
    def __init__(self, turns: list[AssistantTurn], fail_times: int, status: int | None) -> None:
        super().__init__(turns)
        self.fail_times = fail_times
        self.status = status

    def stream(self, **kw: Any):
        if self.fail_times > 0:
            self.fail_times -= 1
            self.requests.append(kw)
            raise ProviderError("boom", status=self.status)
        yield from super().stream(**kw)


def test_provider_error_rolls_back_prompt_and_surfaces_result(workspace: Path) -> None:
    prov = FailingProvider(scripted("ok now"), fail_times=1, status=400)
    agent = make_agent(prov, workspace)
    res = agent.run_turn("hello")
    assert res.stop_reason == "error" and "boom" in res.final_text
    assert agent.conversation.messages == []  # no dangling user message
    assert agent.run_turn("hello again").final_text == "ok now"


def test_transient_provider_error_is_retried(workspace: Path) -> None:
    prov = FailingProvider(scripted("recovered"), fail_times=2, status=529)
    events: list[Any] = []
    agent = make_agent(prov, workspace, events=events)
    res = agent.run_turn("hi")
    assert res.final_text == "recovered"
    assert sum(1 for e in events if type(e).__name__ == "RetryEvent") == 2


def test_max_tokens_with_tool_use_gets_error_results(workspace: Path) -> None:
    truncated = AssistantTurn(blocks=[ToolUseBlock(id="tw", name="Write", input={"path": "a.txt"})], stop_reason="max_tokens")
    prov = FakeProvider([truncated] + scripted("done"))
    agent = make_agent(prov, workspace)
    res = agent.run_turn("write")
    assert res.final_text == "done"
    results = prov.requests[1]["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "tw" and results[0]["is_error"]
    assert not (workspace / "a.txt").exists()


def test_max_iterations_enforced(workspace: Path) -> None:
    turns = [AssistantTurn(blocks=[ToolUseBlock(id=f"c{i}", name="Ls", input={"path": "."})], stop_reason="tool_use") for i in range(10)]
    agent = make_agent(FakeProvider(turns), workspace, max_iterations=3)
    res = agent.run_turn("loop")
    assert res.stop_reason == "max_iterations" and res.iterations == 3
    assert _valid_alternation(agent.conversation.messages)


def test_turn_token_budget_enforced(workspace: Path) -> None:
    turns = [AssistantTurn(blocks=[ToolUseBlock(id=f"c{i}", name="Ls", input={"path": "."})], stop_reason="tool_use",
                           usage=Usage(input=600, output=100)) for i in range(10)]
    agent = make_agent(FakeProvider(turns), workspace, turn_token_budget=1000)
    res = agent.run_turn("loop")
    assert res.stop_reason == "token_budget"
    assert res.iterations == 2


def test_session_cost_ceiling_enforced(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace, session_cost_ceiling=Decimal("0.01"))
    agent.ledger.record("claude-opus-5", Usage(input=10_000_000, output=0))
    res = agent.run_turn("anything")
    assert res.stop_reason == "budget_exceeded"
    assert agent.conversation.messages == []


def test_budget_guard_stops_before_expensive_request(workspace: Path) -> None:
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted("x")), workspace, events=events, model="claude-opus-5",
                       budget_session_usd=0.000001)
    res = agent.run_turn("hi")
    assert res.stop_reason == "budget_exceeded"
    assert any(isinstance(e, BudgetEvent) and e.action == "stop" for e in events)


def test_budget_guard_downgrades_to_small_model(workspace: Path) -> None:
    events: list[Any] = []
    prov = FakeProvider(scripted("cheap answer"))
    agent = make_agent(prov, workspace, events=events, model="claude-opus-5", small_model="cheap-local",
                       budget_session_usd=0.000001, budget_action="downgrade")
    res = agent.run_turn("hi")
    assert res.final_text == "cheap answer"
    assert agent.settings.model == "cheap-local"
    assert prov.requests[0]["model"] == "cheap-local"
    assert any(isinstance(e, BudgetEvent) and e.action == "downgrade" for e in events)


def test_ask_without_approver_is_denied(workspace: Path) -> None:
    prov = FakeProvider(scripted(("Write", {"path": "x.txt", "content": "hi"}), "ok"))
    agent = make_agent(prov, workspace, mode="default")
    agent.run_turn("write it")
    result = prov.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "approver" in result["content"]
    assert not (workspace / "x.txt").exists()


def test_approval_flow_and_permission_events(workspace: Path) -> None:
    events: list[Any] = []
    seen: list[str] = []
    prov = FakeProvider(scripted(("Write", {"path": "y.txt", "content": "hi"}), "ok"))
    agent = make_agent(prov, workspace, mode="default", events=events,
                       on_approval=lambda tool, args, dec: seen.append(tool) or "once")
    agent.run_turn("write")
    assert seen == ["Write"]
    assert (workspace / "y.txt").read_text(encoding="utf-8") == "hi"
    perms = [e for e in events if isinstance(e, PermissionDecisionEvent)]
    assert perms and perms[-1].source == "user" and perms[-1].approval == "once"


def test_parallel_tools_setting_one_runs_serially(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.agent.loop as loop_mod
    created: list[int] = []
    real = loop_mod.ThreadPoolExecutor

    def spy(*a: Any, **k: Any) -> Any:
        created.append(1)
        return real(*a, **k)

    monkeypatch.setattr(loop_mod, "ThreadPoolExecutor", spy)
    batch = [("Glob", {"pattern": "*"}), ("Ls", {"path": "."})]
    make_agent(FakeProvider(scripted(batch, "done")), workspace, parallel_tools=1).run_turn("x")
    assert created == []
    make_agent(FakeProvider(scripted(batch, "done")), workspace, parallel_tools=4).run_turn("x")
    assert created == [1]


def test_repair_closes_dangling_tool_uses() -> None:
    conv = Conversation([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "Ls", "input": {}}]},
        {"role": "assistant", "content": []},
    ])
    fixes = conv.repair()
    assert fixes == 2
    conv.validate()
    assert conv.messages[-1]["content"][0]["tool_use_id"] == "a1"


def test_auto_compaction_uses_small_model_and_emits_event(workspace: Path) -> None:
    events: list[Any] = []
    summary_turn = AssistantTurn(blocks=[TextBlock(text="SUMMARY: user wants X")], stop_reason="end_turn", usage=Usage(input=50, output=20))
    prov = FakeProvider([summary_turn] + scripted("final"))
    agent = make_agent(prov, workspace, events=events, small_model="tiny-model", compact_at=0.5)
    agent.context.context_window = lambda model=None: 3000  # type: ignore[method-assign]
    for i in range(6):
        agent.conversation.append_user(f"question {i} " + "lorem ipsum " * 60)
        agent.conversation.append_assistant(AssistantTurn(blocks=[TextBlock(text=f"answer {i} " + "dolor " * 60)]))
    res = agent.run_turn("next")
    assert res.final_text == "final"
    assert prov.requests[0]["model"] == "tiny-model"  # summariser ran on the cheap model
    comp = [e for e in events if isinstance(e, CompactionEvent) and not e.preview]
    assert comp and comp[0].messages_after < comp[0].messages_before and "SUMMARY" in comp[0].summary
    assert "SUMMARY: user wants X" in json.dumps(agent.conversation.messages)
    assert _valid_alternation(agent.conversation.messages)


def test_compact_preview_does_not_modify_history(workspace: Path) -> None:
    prov = FakeProvider([AssistantTurn(blocks=[TextBlock(text="preview summary")], stop_reason="end_turn")])
    agent = make_agent(prov, workspace)
    for i in range(4):
        agent.conversation.append_user(f"q{i}")
        agent.conversation.append_assistant(AssistantTurn(blocks=[TextBlock(text=f"a{i}")]))
    before = list(agent.conversation.messages)
    assert agent.compact_preview() == "preview summary"
    assert agent.conversation.messages == before
    assert agent.compact(summary="preview summary") is True
    assert "preview summary" in json.dumps(agent.conversation.messages)


def test_init_and_turns_never_write_project_guide(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted(("Ls", {"path": "."}), "done")), workspace)
    agent.run_turn("look around")
    assert not (workspace / "axon.md").exists()
    assert not (workspace / "AXON.md").exists()


def test_traces_record_steps_and_persist(workspace: Path) -> None:
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Ls", {"path": "."}), "done")), workspace, events=events)
    agent.session.open("sess_trace")
    agent.run_turn("list")
    trace = agent.last_trace
    assert trace is not None and trace.llm_calls == 2 and trace.tool_calls == 1
    kinds = [s.kind for s in trace.steps]
    assert kinds.count("llm") == 2 and "tool" in kinds
    tool_step = next(s for s in trace.steps if s.kind == "tool")
    assert "permission" in tool_step.data and "duration_s" in tool_step.data
    assert any(isinstance(e, LLMCallEnd) for e in events) and any(isinstance(e, ToolTiming) for e in events)
    loaded = load_traces(workspace, "sess_trace")
    assert loaded and loaded[-1].tool_calls == 1


def test_tool_rationale_comes_from_model_text_only() -> None:
    turn = AssistantTurn(blocks=[
        TextBlock(text="Let me check the config. I need the database URL."),
        ToolUseBlock(id="a", name="Read", input={"path": "cfg.py"}),
        ToolUseBlock(id="b", name="Bash", input={"command": "pytest"}),
    ])
    out = tool_rationales(turn)
    assert out["a"] == ("I need the database URL.", "model-text")
    assert "b" not in out  # nothing said before it and no heuristic applies without a registry
    turn2 = AssistantTurn(blocks=[ThinkingBlock(text="The tests should pass now."), ToolUseBlock(id="c", name="Bash", input={"command": "pytest"})])
    assert tool_rationales(turn2)["c"][1] == "model-thinking"
    turn3 = AssistantTurn(blocks=[ToolUseBlock(id="d", name="Bash", input={"command": "pytest"})])
    prev = [ToolUseBlock(id="e", name="Edit", input={"path": "a.py"})]
    assert tool_rationales(turn3, previous_tools=prev)["d"] == ("verifies previous edit", "heuristic")


def test_cache_miss_cause_detects_tool_change(workspace: Path) -> None:
    events: list[Any] = []
    turns = [
        AssistantTurn(blocks=[TextBlock(text="one")], usage=Usage(input=1000, output=10, cache_read=800)),
        AssistantTurn(blocks=[TextBlock(text="two")], usage=Usage(input=1200, output=10, cache_read=0)),
    ]
    agent = make_agent(FakeProvider(turns), workspace, events=events)
    agent.run_turn("first")
    agent.registry.unregister("WebSearch")  # tool set changes between requests
    agent.run_turn("second")
    statuses = [e for e in events if isinstance(e, CacheStatus)]
    assert statuses[0].miss is False and statuses[0].hit_pct == 80.0
    assert statuses[1].miss is True and statuses[1].cause == "tool definitions changed"
    ends = [e for e in events if isinstance(e, LLMCallEnd)]
    assert ends[1].cache_miss_cause == "tool definitions changed" and ends[1].context_window > 0
