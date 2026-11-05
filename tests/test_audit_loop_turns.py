"""Agent loop regressions: prompt rollback, tool-batch pairing, cancel races, hook callbacks, compaction fallback."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from axon.agent.loop import TurnResult
from axon.providers.base import AssistantTurn, TextBlock, ThinkingBlock, ToolUseBlock, Usage
from axon.providers.openai_compat import sanitize_openai_messages
from axon.providers.anthropic import to_anthropic_messages
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


class FailingProvider:
    name = "fake"

    def stream(self, **kwargs: Any) -> Iterator[Any]:
        raise RuntimeError("model unavailable")
        yield  # pragma: no cover

    def finalize(self) -> AssistantTurn:  # pragma: no cover
        raise RuntimeError("model unavailable")

    def encode_tool_results(self, results: list[Any]) -> list[dict[str, Any]]:  # pragma: no cover
        return []


def _history(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for i in range(turns):
        messages.append({"role": "user", "content": f"question {i}"})
        messages.append({"role": "assistant", "content": [{"type": "text", "text": f"answer {i}"}]})
    return messages


def test_failed_first_call_removes_the_prompt_even_after_compaction(workspace: Path, monkeypatch) -> None:
    agent = make_agent(FailingProvider(), workspace)
    agent.conversation.messages = _history(8)
    asked = []
    monkeypatch.setattr(agent.context, "needs_compaction", lambda *a, **k: not asked.append(1) and len(asked) == 1)
    monkeypatch.setattr(agent.context, "generate_summary", lambda *a, **k: ("earlier work", None))

    result = agent.run_turn("the new prompt")

    assert result.stop_reason == "error"
    assert len(agent.conversation.messages) < 17            # compaction really shortened the history
    assert "the new prompt" not in str(agent.conversation.messages)
    assert agent.last_failed_turn == {"prompt": "the new prompt", "resume": False}


def test_sequential_batch_keeps_results_of_tools_that_already_ran(workspace: Path, monkeypatch) -> None:
    (workspace / "a.txt").write_text("alpha-content\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta-content\n", encoding="utf-8")
    prov = FakeProvider(scripted([("Read", {"path": str(workspace / "a.txt")}),
                                  ("Read", {"path": str(workspace / "b.txt")})], "done"))
    agent = make_agent(prov, workspace, parallel_tools=1)
    check = agent.permissions.check

    def flaky_check(tool: Any, tool_input: dict[str, Any], mode: str) -> Any:
        if str(tool_input.get("path", "")).endswith("b.txt"):
            raise RuntimeError("permission engine crashed")
        return check(tool, tool_input, mode)

    monkeypatch.setattr(agent.permissions, "check", flaky_check)

    agent.run_turn("read both")

    blocks = agent.conversation.messages[2]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t0", "t1"]
    assert "alpha-content" in blocks[0]["content"] and not blocks[0].get("is_error")
    assert blocks[1]["is_error"] and "permission engine crashed" in blocks[1]["content"]


def test_threaded_turn_returns_when_the_worker_dies_before_signalling(workspace: Path) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    finished = TurnResult(final_text="ok", stop_reason="end_turn", iterations=1, tool_calls_count=0, usage=Usage())

    def run_turn(_prompt: str) -> TurnResult:
        agent._active_bg = SimpleNamespace(thread=threading.current_thread())
        return finished

    def finish_background(*_args: Any) -> None:
        agent._active_bg = None
        raise KeyboardInterrupt()          # a cancel landing in the worker's tail

    agent.run_turn = run_turn                       # type: ignore[method-assign]
    agent._finish_background = finish_background    # type: ignore[method-assign]
    outcome: list[Any] = []
    waiter = threading.Thread(target=lambda: outcome.append(agent.run_turn_threaded("go")), daemon=True)
    waiter.start()
    waiter.join(timeout=5)

    assert not waiter.is_alive(), "run_turn_threaded hangs forever"
    assert outcome == [finished]


def test_subagent_does_not_take_over_the_parents_hook_callbacks(workspace: Path) -> None:
    parent = make_agent(FakeProvider([]), workspace)
    assert parent.hooks is not None
    child = make_agent(FakeProvider([]), workspace, hooks=parent.hooks, agent_name="explore")

    assert child.hooks is parent.hooks
    assert parent.hooks.llm == parent._hook_llm
    assert parent.hooks.mcp_call == parent._hook_mcp_call


def test_summary_falls_back_to_the_main_model_when_the_small_model_fails(workspace: Path, monkeypatch) -> None:
    main = FakeProvider(scripted("SUMMARY OF EARLIER WORK"))
    agent = make_agent(main, workspace)
    agent.conversation.messages = _history(5)
    monkeypatch.setattr(agent, "_compaction_provider", lambda: (FailingProvider(), "small-model-of-another-provider"))

    summary, _usage, model = agent._generate_summary("")

    assert summary == "SUMMARY OF EARLIER WORK"
    assert model == agent.settings.model


class OpenAIStyleFake(FakeProvider):
    name = "openai"

    def encode_tool_results(self, results: list[Any]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content} for r in results]


def test_history_stays_valid_for_both_wire_formats_after_switching_provider(workspace: Path) -> None:
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    anthropic_turn = AssistantTurn(
        blocks=[ThinkingBlock(text="hmm"), ToolUseBlock(id="toolu_1", name="Read", input={"path": str(workspace / "a.txt")})],
        stop_reason="tool_use",
        native={"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": str(workspace / "a.txt")}}]})
    first = FakeProvider([anthropic_turn] + scripted("first done"))
    first.name = "anthropic"
    agent = make_agent(first, workspace)
    agent.run_turn("read a.txt")

    second = OpenAIStyleFake(scripted(("Read", {"path": str(workspace / "a.txt")}), "second done"))
    agent.provider = second
    agent.run_turn("and again")

    sent = second.requests[-1]["messages"]
    openai_messages = sanitize_openai_messages([{"role": "system", "content": "s"}] + sent)
    assert "thinking" not in str(openai_messages)
    for i, m in enumerate(openai_messages):
        if m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = [x["tool_call_id"] for x in openai_messages[i + 1:i + 1 + len(ids)]]
            assert following == ids
    anthropic_messages = to_anthropic_messages(agent.conversation.messages)
    uses = [b["id"] for m in anthropic_messages if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    results = [b["tool_use_id"] for m in anthropic_messages if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b["type"] == "tool_result"]
    assert sorted(uses) == sorted(results) and len(uses) == 2
