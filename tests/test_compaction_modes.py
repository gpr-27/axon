"""Keep-last-N-turns cut, background compaction swap, server-side compaction accounting, cache diagnostics (fakes only)."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from axon.agent.compaction import (BackgroundCompaction, apply_server_compaction, has_open_tool_call, strip_thinking_blocks,
                                   supports_server_compaction)
from axon.agent.context import ContextManager
from axon.agent.state import Conversation, is_tool_result_message
from axon.config import Settings


def _u(t: str) -> dict:
    return {"role": "user", "content": t}


def _a(t: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": t}]}


def _call(i: int) -> dict:
    return {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "s"},
                                              {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}]}


def _res(i: int) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}


def _cm(**kw: Any) -> ContextManager:
    return ContextManager(Settings(api_key="k", **kw))


def _pairs_intact(msgs: list[dict]) -> bool:
    ids_open: set[str] = set()
    for m in msgs:
        c = m.get("content")
        for b in c if isinstance(c, list) else []:
            if b.get("type") == "tool_use":
                ids_open.add(b["id"])
            if b.get("type") == "tool_result":
                if b["tool_use_id"] not in ids_open:
                    return False
                ids_open.discard(b["tool_use_id"])
    return not ids_open


def _long() -> list[dict]:
    out: list[dict] = []
    for turn in range(5):
        out.append(_u(f"q{turn}"))
        for k in range(3):
            out += [_call(turn * 10 + k), _res(turn * 10 + k)]
        out.append(_a(f"done{turn}"))
    return out


def test_cut_never_splits_a_tool_pair_for_any_keep_setting() -> None:
    for keep in (1, 2, 3, 4, 9):
        cm = _cm(compact_keep_turns=keep)
        conv = Conversation(_long())
        cut = cm.split_point(conv)
        if cut:
            assert not is_tool_result_message(conv.messages[cut])
            assert _pairs_intact(conv.messages[:cut]) and _pairs_intact(conv.messages[cut:])


def test_safe_cut_moves_off_a_tool_result() -> None:
    msgs = [_u("a"), _call(1), _res(1), _a("x")]
    assert ContextManager.safe_cut(msgs, 2) == 1       # the result's call stays with it
    assert ContextManager.safe_cut(msgs, 1) == 1


def test_keep_turns_setting_keeps_the_tail_verbatim_and_summary_goes_first() -> None:
    msgs = _long()
    cm = _cm(compact_keep_turns=2)
    conv = Conversation(list(msgs))
    assert cm._summarize_older_turns(conv, summary_text="S")
    starts = [i for i, m in enumerate(msgs) if m.get("role") == "user" and isinstance(m["content"], str)]
    tail = msgs[starts[-2]:]
    assert conv.messages[-len(tail):] == tail                      # kept turns word for word
    assert "S" in conv.messages[0]["content"] and conv.messages[0]["role"] == "user"
    assert len(conv.messages) == len(tail) + 2                     # summary + ack + tail
    cm3 = _cm(compact_keep_turns=3)
    c3 = Conversation(list(msgs))
    cm3._summarize_older_turns(c3, summary_text="S")
    assert len(c3.messages) > len(conv.messages)


class _Agent:
    """The few things BackgroundCompaction and apply_server_compaction use."""

    def __init__(self, messages: list[dict], **settings: Any) -> None:
        settings.setdefault("model", "claude-sonnet-4-5")
        self.settings = Settings(api_key="k", **settings)
        self.context = ContextManager(self.settings)
        self.conversation = Conversation(messages)
        self.events: list[Any] = []
        self.compacted: list[dict] = []
        self.is_subagent = False
        self.provider = SimpleNamespace(_official=False)

    def _emit(self, e: Any) -> None:
        self.events.append(e)

    def _compact_instructions(self, i: str) -> str:
        return i

    def journal(self, *a: Any, **k: Any) -> None:
        pass

    def compact(self, trigger: str = "", summary: str | None = None, usage_in: Any = None, model_in: str = "", cut: int | None = None, **k: Any) -> bool:
        self.compacted.append({"trigger": trigger, "summary": summary, "cut": cut, "usage": usage_in})
        return self.context._summarize_older_turns(self.conversation, summary_text=summary, cut=cut)


def _near_threshold(agent: _Agent) -> None:
    agent.context.projected_tokens = lambda *a, **k: 10**9      # type: ignore[method-assign]
    agent.context.threshold = lambda *a, **k: 10**9 // 2        # type: ignore[method-assign]
    agent.context.needs_compaction = lambda *a, **k: False      # type: ignore[method-assign]


def test_background_swap_keeps_messages_appended_meanwhile() -> None:
    agent = _Agent(_long(), compact_keep_turns=2)
    _near_threshold(agent)
    gate = threading.Event()
    seen: list[int] = []

    def slow(snapshot: Conversation, instructions: str) -> tuple[str, Any, str]:
        seen.append(len(snapshot.messages))
        gate.wait(5)
        return "BG SUMMARY", "usage", "small"

    bg = BackgroundCompaction(agent, summarize=slow)
    assert bg.step([], []) is False and bg.running
    assert [e.phase for e in agent.events] == ["start"]
    before = list(agent.conversation.messages)
    agent.conversation.messages += [_u("typed while summarizing"), _a("answer")]     # the work goes on
    gate.set()
    bg._thread.join(5)
    assert bg.step([], []) is True
    msgs = agent.conversation.messages
    assert "BG SUMMARY" in msgs[0]["content"]
    assert msgs[-2:] == [_u("typed while summarizing"), _a("answer")]
    cut = agent.compacted[0]["cut"]
    assert msgs[-(len(before) - cut) - 2:-2] == before[cut:]        # the kept tail of the snapshot is intact, nothing duplicated
    assert agent.compacted[0]["usage"] == "usage" and agent.compacted[0]["trigger"] == "background"


def test_background_summary_is_dropped_when_the_history_changed_or_cancelled() -> None:
    agent = _Agent(_long())
    _near_threshold(agent)
    bg = BackgroundCompaction(agent, summarize=lambda s, i: ("X", None, "m"))
    bg.step([], [])
    bg._thread.join(5)
    agent.conversation.messages = [dict(m) for m in agent.conversation.messages]   # rewritten under it (rewind, other compaction)
    assert bg.step([], []) is False and agent.compacted == [] and agent.events[-1].phase == "stale"
    agent2 = _Agent(_long())
    _near_threshold(agent2)
    bg2 = BackgroundCompaction(agent2, summarize=lambda s, i: ("X", None, "m"))
    bg2.step([], [])
    bg2._thread.join(5)
    assert bg2.cancel() is True and bg2.step([], []) is False and agent2.compacted == []
    assert agent2.events[-1].phase in ("cancelled", "start")


def test_background_never_starts_in_a_tool_exchange_or_below_the_threshold() -> None:
    msgs = _long()[:-1] + [_call(99)]
    assert has_open_tool_call(msgs)
    agent = _Agent(msgs)
    _near_threshold(agent)
    bg = BackgroundCompaction(agent, summarize=lambda s, i: ("X", None, "m"))
    assert bg.step([], []) is False and not bg.running
    quiet = _Agent(_long())
    quiet.context.projected_tokens = lambda *a, **k: 10      # type: ignore[method-assign]
    bg2 = BackgroundCompaction(quiet, summarize=lambda s, i: ("X", None, "m"))
    assert bg2.step([], []) is False and not bg2.running


def test_server_compaction_support_and_edit_gate() -> None:
    from axon.agent.compaction import server_active, server_edit
    assert supports_server_compaction("claude-sonnet-4-6") and supports_server_compaction("claude-opus-5-5")
    assert not supports_server_compaction("gpt-5") and not supports_server_compaction("claude-sonnet-4-5")
    agent = _Agent(_long(), compact_server_side=True, model="claude-sonnet-4-6")
    assert not server_active(agent)                                  # not the official host
    agent.provider = SimpleNamespace(_official=True, _wire_model=lambda m: m)
    agent.context.pins = {}
    edit = server_edit(agent)
    assert edit["type"] == "compact_20260112" and edit["pause_after_compaction"] is True and edit["trigger"]["value"] >= 50_000
    assert "Do not call any tools" in edit["instructions"]
    off = _Agent(_long(), compact_server_side=False, model="claude-sonnet-4-6")
    off.provider = agent.provider
    assert server_edit(off) is None


def test_server_compaction_applies_block_keeps_tail_without_thinking() -> None:
    agent = _Agent(_long(), compact_keep_turns=1)
    agent.session = SimpleNamespace(append=lambda *a, **k: None)
    agent.reload_memory = lambda: None
    agent._reread_recent_files = lambda: ""
    block = {"type": "compaction", "content": "THE SUMMARY", "signature": "sig"}
    turn = SimpleNamespace(native=[block], stop_reason="compaction", usage=SimpleNamespace(input=200, output=30))
    assert apply_server_compaction(agent, turn, "claude-sonnet-4-6", 0.5) is True
    msgs = agent.conversation.messages
    assert msgs[0] == {"role": "assistant", "content": [block]}
    assert msgs[1]["role"] == "user" and msgs[1]["content"] == "q4"
    assert all(b.get("type") != "thinking" for m in msgs for b in (m["content"] if isinstance(m["content"], list) else []))
    assert _pairs_intact(msgs)
    assert agent.events[-1].trigger == "server" and agent.events[-1].cost == 0.5
    empty = SimpleNamespace(native=[{"type": "compaction"}], stop_reason="compaction", usage=None)
    assert apply_server_compaction(agent, empty, "m") is False and agent.provider._compact_off is True


def test_strip_thinking_blocks_drops_empty_turns() -> None:
    out = strip_thinking_blocks([{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}, _u("hi")])
    assert out == [_u("hi")]


# ── wire: usage.iterations and the compaction stream ────────────────────────

def test_usage_iterations_are_counted_in_the_turn_usage() -> None:
    from axon.providers.anthropic import AnthropicProvider
    raw = {"input_tokens": 23000, "output_tokens": 1000,
           "iterations": [{"type": "compaction", "input_tokens": 180000, "output_tokens": 3500},
                          {"type": "message", "input_tokens": 23000, "output_tokens": 1000}]}
    obj = SimpleNamespace(**raw)
    u = AnthropicProvider._usage(obj, __import__("axon.providers.base", fromlist=["Usage"]).Usage())
    assert (u.input, u.output) == (203000, 4500)
    from axon.providers.pricing import cost_for
    assert cost_for("claude-sonnet-4-6", u) > cost_for("claude-sonnet-4-6", type(u)(input=23000, output=1000))


def test_compaction_edit_wire_helpers() -> None:
    from axon.providers.anthropic_wire import BETA_COMPACT, BETA_CONTEXT_MANAGEMENT, apply_context_management, strip_compaction_edit
    cfg = {"edits": [{"type": "clear_tool_uses_20250919"}, {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 60000}}]}
    kw: dict = {}
    apply_context_management(kw, cfg)
    assert BETA_COMPACT in kw["extra_headers"]["anthropic-beta"] and BETA_CONTEXT_MANAGEMENT in kw["extra_headers"]["anthropic-beta"]
    assert strip_compaction_edit(kw) is True
    assert kw["extra_body"]["context_management"]["edits"] == [{"type": "clear_tool_uses_20250919"}]
    assert kw["extra_headers"]["anthropic-beta"] == BETA_CONTEXT_MANAGEMENT
    only: dict = {}
    apply_context_management(only, {"edits": [cfg["edits"][1]]})
    assert only["extra_headers"]["anthropic-beta"] == BETA_COMPACT
    strip_compaction_edit(only)
    assert "extra_body" not in only and "extra_headers" not in only


def test_compaction_block_streams_through_and_is_sent_back(monkeypatch: Any) -> None:
    from tests.test_providers_layer import _anthropic_httpx, _anthropic_sse, anthropic_provider
    hx = _anthropic_httpx()
    events = [
        {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "content": [], "model": "claude-sonnet-4-6",
                                              "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "compaction", "content": None}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "compaction_delta", "content": "SUM"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "compaction"},
         "usage": {"input_tokens": 0, "output_tokens": 0,
                   "iterations": [{"type": "compaction", "input_tokens": 90000, "output_tokens": 2000}]}},
        {"type": "message_stop"},
    ]
    bodies: list[dict] = []

    def handler(request: Any) -> Any:
        bodies.append(json.loads(request.content))
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(events))

    prov = anthropic_provider(handler)
    prov.context_edit = {"edits": [{"type": "compact_20260112", "pause_after_compaction": True}]}
    list(prov.stream(model="claude-sonnet-4-6", system=[], tools=[], max_tokens=100, messages=[_u("x")], thinking=False))
    turn = prov.finalize()
    assert turn.stop_reason == "compaction"
    block = [b for b in turn.native if b.get("type") == "compaction"][0]
    assert block["content"] == "SUM"
    assert turn.usage.input == 90000 and turn.usage.output == 2000      # the summarizing call is in the usage, so in cost and the table
    # sent back: kept as a block while armed, as plain text when it is not
    hist = [{"role": "assistant", "content": [block]}, _u("go")]
    list(prov.stream(model="claude-sonnet-4-6", system=[], tools=[], max_tokens=100, messages=hist, thinking=False))
    prov.finalize()
    assert bodies[-1]["messages"][0]["content"][0]["type"] == "compaction"
    prov.context_edit = None
    list(prov.stream(model="claude-sonnet-4-6", system=[], tools=[], max_tokens=100, messages=hist, thinking=False))
    prov.finalize()
    assert bodies[-1]["messages"][0]["content"][0] == {"type": "text", "text": "[Summary of the earlier conversation]\n\nSUM"}


# ── cache diagnostics ───────────────────────────────────────────────────────

def test_cache_diagnostics_opt_in_and_one_line_cause() -> None:
    from axon.features.insights import api_cause
    assert api_cause({"cache_miss_reason": {"type": "system_changed", "cache_missed_input_tokens": 5}, "cache_missed_input_tokens": 41850}) == ("system prompt changed", 41850)
    assert api_cause({"cache_miss_reason": {"type": "unavailable"}}) is None
    assert api_cause(None) is None
