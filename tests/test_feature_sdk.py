"""Tests for axon.features.sdk (programmatic API) using the deterministic FakeProvider."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.features import sdk
from axon.features.sdk import AxonOptions, AxonSession, SDKError, event_to_dict, query, run
from axon.providers.base import (
    AssistantTurn, LLMCallStart, TextBlock, TextDelta, ThinkingBlock, ToolArgsDelta, ToolExecutionResult,
    ToolExecutionStart, ToolUseBlock, Usage,
)
from fakes import FakeProvider, scripted


@pytest.fixture(autouse=True)
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    return home


def _opts(workspace: Path, *turns, **kw) -> AxonOptions:
    return AxonOptions(workspace=workspace, model="claude-sonnet-4-5", provider_instance=FakeProvider(list(turns)), **kw)


def test_query_streams_events_and_result(workspace: Path) -> None:
    (workspace / "a.txt").write_text("hello world\n", encoding="utf-8")
    turns = scripted(("Read", {"file_path": str(workspace / "a.txt")}), "The file says hello.")
    turns[0].blocks.insert(0, ThinkingBlock(text="look at the file"))
    events = list(query("what is in a.txt?", _opts(workspace, *turns, mode="bypass")))
    for ev in events:
        json.dumps(ev)  # every event is JSON-serializable
    types = [e["type"] for e in events]
    assert types[0] == "system" and events[0]["subtype"] == "init"
    assert "Read" in events[0]["tools"]
    for t in ("llm_call_start", "thinking_delta", "tool_use", "tool_result", "llm_call_end", "text_delta"):
        assert t in types, t
    assert types[-1] == "result"
    use = next(e for e in events if e["type"] == "tool_use")
    res = next(e for e in events if e["type"] == "tool_result")
    assert use["name"] == "Read" and res["tool_use_id"] == use["id"] and not res["is_error"]
    assert "hello world" in res["content"]
    end = next(e for e in events if e["type"] == "llm_call_end")
    assert end["usage"]["input_tokens"] == 100 and "cost" in end
    final = events[-1]
    assert final["result"] == "The file says hello."
    assert final["is_error"] is False and final["iterations"] == 2 and final["tool_calls"] == 1
    assert final["usage"]["input_tokens"] == 200 and final["usage"]["output_tokens"] == 100
    assert final["session_id"] and final["duration_s"] >= 0
    assert final["cost_usd"] == pytest.approx(sum(e["cost"] for e in events if e["type"] == "llm_call_end"))


def test_run_returns_result(workspace: Path) -> None:
    r = run("hi", _opts(workspace, *scripted("Hello there")))
    assert r.text == "Hello there" and r.stop_reason == "end_turn" and not r.is_error
    assert r.events[-1]["type"] == "result" and r.to_dict()["text"] == "Hello there"


def test_ask_is_denied_without_callback_and_allowed_with_it(workspace: Path) -> None:
    target = workspace / "new.txt"
    spec = ("Write", {"file_path": str(target), "content": "x"})
    r = run("write", _opts(workspace, *scripted(spec, "done"), mode="default"))
    res = next(e for e in r.events if e["type"] == "tool_result")
    assert res["is_error"] and not target.exists()
    perm = next(e for e in r.events if e["type"] == "permission")
    assert perm["outcome"] == "deny"

    seen: list[str] = []

    def allow(name: str, args: dict) -> bool:
        seen.append(name)
        return True

    r2 = run("write", _opts(workspace, *scripted(spec, "done"), mode="default", can_use_tool=allow))
    assert seen == ["Write"] and target.read_text(encoding="utf-8") == "x"
    assert not next(e for e in r2.events if e["type"] == "tool_result")["is_error"]


def test_disallowed_tools_and_system_prompt(workspace: Path) -> None:
    fake = FakeProvider(scripted("ok"))
    opts = AxonOptions(workspace=workspace, provider_instance=fake, disallowed_tools=["Bash"],
                       system_prompt="You are a test bot.", append_system_prompt="Be brief.", max_turns=3)
    with AxonSession(opts) as s:
        assert "Bash" not in s.agent.registry.names()
        assert s.agent.settings.max_iterations == 3
        assert s.run("hi").text == "ok"
    system = fake.requests[0]["system"]
    assert [b["text"] for b in system] == ["You are a test bot.", "Be brief."]
    assert all(t.get("name") != "Bash" for t in fake.requests[0]["tools"])


def test_multi_turn_session_and_resume(workspace: Path) -> None:
    fake = FakeProvider(scripted("first", "second"))
    with AxonSession(AxonOptions(workspace=workspace, provider_instance=fake)) as s:
        sid = s.run("one").session_id
        assert s.run("two").text == "second"
    assert len(fake.requests[1]["messages"]) == 3  # user, assistant, user

    fake2 = FakeProvider(scripted("third"))
    r = run("three", AxonOptions(workspace=workspace, provider_instance=fake2, resume=sid))
    assert r.session_id == sid and r.text == "third"
    assert len(fake2.requests[0]["messages"]) == 5
    with pytest.raises(SDKError):
        list(query("x", AxonOptions(workspace=workspace, provider_instance=FakeProvider(), resume="nope_missing")))


def test_provider_error_becomes_error_result(workspace: Path) -> None:
    class Boom(FakeProvider):
        def stream(self, **kw):
            raise RuntimeError("kaput")
            yield  # pragma: no cover

    s = AxonSession(AxonOptions(workspace=workspace, provider_instance=Boom()))
    s.agent.max_retries = 0
    r = s.run("hi")
    s.close()
    assert r.is_error and r.stop_reason == "error" and "kaput" in r.text


def test_cancel_interrupts_turn(workspace: Path) -> None:
    turn = AssistantTurn(blocks=[TextBlock(text="a" * 10)], usage=Usage(input=1, output=1))
    s = AxonSession(AxonOptions(workspace=workspace, provider_instance=FakeProvider([turn])))
    out = []
    for ev in s.query("go"):
        out.append(ev)
        if ev["type"] == "llm_call_start":
            s.cancel()
    s.close()
    assert out[-1]["type"] == "result" and out[-1]["stop_reason"] == "interrupted"


def test_options_validation(workspace: Path) -> None:
    with pytest.raises(SDKError):
        sdk.build_agent(AxonOptions(workspace=workspace, mode="yolo", provider_instance=FakeProvider()))
    with pytest.raises(SDKError):
        sdk.build_agent(AxonOptions(workspace=workspace / "missing", provider_instance=FakeProvider()))
    with pytest.raises(SDKError):
        AxonOptions.from_dict({"provider_instance": "x"})
    o = AxonOptions.from_dict({"maxTurns": 4, "allowedTools": "Read", "permissionMode": "plan"},
                              base=AxonOptions(model="m"))
    assert (o.max_turns, o.allowed_tools, o.mode, o.model) == (4, ["Read"], "plan", "m")


def test_event_to_dict_shapes() -> None:
    assert event_to_dict(TextDelta("x")) == {"type": "text_delta", "text": "x"}
    assert event_to_dict(ToolArgsDelta("1", "{")) is None
    assert event_to_dict(ToolExecutionStart(id="1", name="Read", input={"p": Path("a")}))["input"] == {"p": "a"}
    r = event_to_dict(ToolExecutionResult(id="1", name="Read", input={}, content="c", is_error=True))
    assert r == {"type": "tool_result", "tool_use_id": "1", "name": "Read", "content": "c", "is_error": True}
    s = event_to_dict(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=2))
    assert s["type"] == "llm_call_start" and s["iteration"] == 1
    from axon.agent.trace import LLMCallEnd, RetryEvent
    e = event_to_dict(LLMCallEnd(iteration=1, model="m", usage=Usage(input=3, output=4), cost=0.5,
                                 duration_s=1.0, stop_reason="end_turn", started_at=0.0, ended_at=1.0))
    assert e["type"] == "llm_call_end" and e["usage"]["input_tokens"] == 3 and e["cost"] == 0.5
    assert event_to_dict(RetryEvent(attempt=1, max_attempts=2, delay_s=1.0, error="x"))["type"] == "retry"
    json.dumps(e)


def test_tool_use_block_unused_import_guard() -> None:
    # ToolUseBlock is part of the scripted turns; ensure tool batches serialize.
    from axon.providers.base import ToolBatchStart
    d = event_to_dict(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id="t", name="Read", input={})]))
    assert d == {"type": "tool_batch", "count": 1, "tools": [{"id": "t", "name": "Read"}]}
