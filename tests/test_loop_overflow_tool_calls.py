"""A reply cut by a full context window while it was writing tool calls: the calls are not run, the model is told the
true reason (not "max_tokens"), the context is shrunk before the next call, and PostToolBatch does not fire."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage


def _cut(reason: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text="writing"), ToolUseBlock(id="t1", name="Read", input={"file_path": "a.txt"})],
                         stop_reason=reason, usage=Usage())


def _result_text(provider: FakeProvider) -> str:
    return str(provider.requests[-1]["messages"])


def test_context_full_with_tool_calls_says_so_and_recovers(workspace: Path, monkeypatch: Any) -> None:
    provider = FakeProvider([_cut("model_context_window_exceeded")] + scripted("finished"))
    agent = make_agent(provider, workspace)
    recovered: list[int] = []
    monkeypatch.setattr(agent, "_recover_from_overflow", lambda attempt: recovered.append(attempt) or True)
    hooks: list[str] = []
    real_hook = agent._hook
    monkeypatch.setattr(agent, "_hook", lambda event, payload=None, *a, **k: hooks.append(event) or real_hook(event, payload, *a, **k))
    result = agent.run_turn("do it")
    assert result.final_text == "finished"
    sent = _result_text(provider)
    assert "context window filled" in sent and "max_tokens" not in sent
    assert recovered == [1]
    assert "PostToolBatch" not in hooks


def test_output_limit_with_tool_calls_keeps_its_own_text(workspace: Path) -> None:
    provider = FakeProvider([_cut("max_tokens")] + scripted("finished"))
    agent = make_agent(provider, workspace)
    assert agent.run_turn("do it").final_text == "finished"
    assert "hit the max_tokens limit" in _result_text(provider)
