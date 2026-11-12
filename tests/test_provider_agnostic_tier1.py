"""Tier-1 features on a non-Anthropic provider: client-side context clearing, calibrated counts, client-side task
budget, tool-argument delta streaming with truncation safety, parallel_tool_calls. All against fakes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]
from pydantic import SecretStr

from axon.agent import client_budget, client_context
from axon.agent.exact_count import note_call
from axon.config import Settings
from axon.providers import toolflow
from axon.providers.base import AssistantTurn, ToolArgsDelta, ToolUseBlock, ToolUseComplete, Usage
from axon.providers.openai_compat import OpenAICompatProvider
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

BIG = "x" * 9000


def _openai_history(n: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for i in range(n):
        name = "Edit" if i == 1 else "Read"
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": BIG})
    return msgs


def _anthropic_history(n: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": BIG}]})
    return msgs


def _agent(workspace: Path, msgs: list[dict[str, Any]]) -> Any:
    agent = make_agent(FakeProvider([]), workspace, context_edit_clear_at_least=1000, context_edit_trigger=10_000)
    agent.conversation.messages = msgs
    return agent


def test_client_clearing_stubs_old_results_without_splitting_pairs(workspace: Path) -> None:
    for hist, key in ((_openai_history(14), "c"), (_anthropic_history(14), "t")):
        agent = _agent(workspace, hist)
        before = json.dumps(agent.conversation.messages)
        sent, event = client_context.view(agent, [], [])
        assert event is not None and "cleared" in event.message and "saved" in event.message
        assert json.dumps(agent.conversation.messages) == before          # the transcript is untouched
        assert len(sent) == len(hist)
        text = json.dumps(sent)
        assert text.count("[old result cleared") >= 7
        assert f'"{key}13"' in text and text.count(BIG) in (6, 7)           # newest 6 stay verbatim
        # every tool_use / tool_call still has its result
        ids_use = [b["id"] for m in sent if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_use"]
        ids_use += [c["id"] for m in sent for c in m.get("tool_calls") or []]
        ids_res = [b["tool_use_id"] for m in sent if isinstance(m.get("content"), list) for b in m["content"] if b.get("type") == "tool_result"]
        ids_res += [m["tool_call_id"] for m in sent if m.get("role") == "tool"]
        assert ids_use == ids_res
        # stable: the next call sends the same prefix and no second notice
        sent2, event2 = client_context.view(agent, [], [])
        assert event2 is None and json.dumps(sent2) == json.dumps(sent)


def test_client_clearing_spares_edits_and_pins_and_waits_for_the_trigger(workspace: Path) -> None:
    agent = _agent(workspace, _openai_history(14))
    agent.context.pins["c0"] = {"label": "keep", "tokens": 1}
    sent, _ = client_context.view(agent, [], [])
    by_id = {m["tool_call_id"]: m["content"] for m in sent if m.get("role") == "tool"}
    assert by_id["c0"] == BIG and by_id["c1"] == BIG                      # pinned and Edit stay
    assert by_id["c2"].startswith("[old result cleared")
    tiny = _agent(workspace, [{"role": "user", "content": "hi"}] * 5)
    assert client_context.view(tiny, [], [])[1] is None


def test_calibrated_count_converges_and_tilde_only_without_calibration(workspace: Path) -> None:
    agent = _agent(workspace, _openai_history(3))
    conv, model = agent.conversation, agent.settings.model
    assert agent.context.exact.value_for(conv, model) is None             # no response yet: the meter shows '~'
    est = conv.token_estimate()
    real = lambda e: int(e * 1.5) + 300                                     # the provider's tokenizer is 1.5x ours + overhead
    for _ in range(3):
        note_call(agent, AssistantTurn(blocks=[], stop_reason="end_turn", usage=Usage(input=real(conv.token_estimate()), output=10)), [], [])
        conv.messages.append({"role": "user", "content": "y" * 3700})
    v = agent.context.exact.value_for(conv, model)
    truth = real(conv.token_estimate())
    assert v is not None and abs(v - truth) / truth < 0.05, (v, truth)
    assert est > 0


def test_client_task_budget_notes_remaining_and_stops(workspace: Path) -> None:
    (workspace / "a.txt").write_text("hi\n")
    turns = [AssistantTurn(blocks=[ToolUseBlock(id=f"u{i}", name="Read", input={"path": "a.txt"})], stop_reason="tool_use",
                           usage=Usage(input=10_000 + i * 8_000, output=1_000)) for i in range(6)]
    prov = FakeProvider(turns)
    agent = make_agent(prov, workspace, task_budget_tokens=20_000)
    res = agent.run_turn("read it again and again")
    assert res.stop_reason == "token_budget" and "task budget of 20,000 tokens reached" in res.final_text
    assert len(prov.requests) < 6
    notes = [b["text"] for r in prov.requests for b in r["system"] if b["text"].startswith("Task budget:")]
    assert notes and all("of 20,000 tokens left" in n for n in notes)
    assert client_budget.note(agent).endswith("final answer.") or "%" in client_budget.note(agent)


def _sse(*chunks: dict[str, Any]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def _tc(frag: str, first: bool = False) -> dict[str, Any]:
    fn: dict[str, Any] = {"arguments": frag}
    tc: dict[str, Any] = {"index": 0, "function": fn}
    if first:
        tc.update(id="call_1", type="function")
        fn["name"] = "Write"
    return {"choices": [{"index": 0, "delta": {"tool_calls": [tc]}}]}


def _oa(handler: Any, base: str = "https://gw.example.com/v1") -> OpenAICompatProvider:
    s = Settings(base_url=base, api_key=SecretStr("k"))
    return OpenAICompatProvider(s, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _stream(prov: Any) -> tuple[list[Any], AssistantTurn]:
    ev = list(prov.stream(model="m", system=[], messages=[{"role": "user", "content": "x"}],
                          tools=[{"name": "Write", "description": "w", "input_schema": {"type": "object", "properties": {}}}], max_tokens=100))
    return ev, prov.finalize()


def test_openai_tool_arguments_stream_as_deltas_and_truncation_is_never_run() -> None:
    body = _sse(_tc('{"path": "a.py", ', True), _tc('"content": "ab'), _tc('c"}'),
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    ev, turn = _stream(_oa(lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})))
    assert "".join(e.fragment for e in ev if isinstance(e, ToolArgsDelta)) == '{"path": "a.py", "content": "abc"}'
    assert turn.tool_uses[0].input["content"] == "abc" and not turn.tool_uses[0].parse_error
    # cut off by the output limit: dropped, and its live row is closed
    cut = _sse(_tc('{"path": "a.py", ', True), _tc('"content": "half'),
               {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    ev, turn = _stream(_oa(lambda r: httpx.Response(200, content=cut, headers={"content-type": "text/event-stream"})))
    assert not turn.tool_uses and any(isinstance(e, ToolUseComplete) and e.id == "call_1" for e in ev)
    # invalid JSON with a normal finish: kept as an error call (the loop answers INVALID_JSON), replayed as valid JSON
    bad = _sse(_tc('{"path": "a.py", "content": ', True), {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    ev, turn = _stream(_oa(lambda r: httpx.Response(200, content=bad, headers={"content-type": "text/event-stream"})))
    assert turn.tool_uses[0].parse_error
    json.loads(turn.native["tool_calls"][0]["function"]["arguments"])


def test_parallel_tool_calls_false_sent_then_stripped_after_a_400_naming_it() -> None:
    seen: list[dict[str, Any]] = []
    ok = _sse({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        if "parallel_tool_calls" in seen[-1]:
            return httpx.Response(400, json={"error": {"message": "Unknown parameter: parallel_tool_calls"}})
        return httpx.Response(200, content=ok, headers={"content-type": "text/event-stream"})

    prov = _oa(handler)
    toolflow.set_hints(serial_tools=True)
    try:
        _stream(prov)
        assert "parallel_tool_calls" in seen[0] and seen[0]["parallel_tool_calls"] is False
        assert "parallel_tool_calls" not in seen[1] and len(seen) == 2
        _stream(prov)                                                    # learned: not sent again
        assert "parallel_tool_calls" not in seen[2]
        seen.clear()
        _stream(_oa(handler, "http://localhost:11434/v1"))               # local server: never sent
        assert "parallel_tool_calls" not in seen[0]
        toolflow.set_hints(serial_tools=False)
        seen.clear()
        _stream(_oa(handler))
        assert "parallel_tool_calls" not in seen[0]
    finally:
        toolflow.set_hints()
