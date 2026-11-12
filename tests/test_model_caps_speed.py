"""Model capabilities from the models list, fast mode wire shape, per-message effort, auto effort (fakes only)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from axon.agent import speed
from axon.providers import model_caps, speed_wire
from axon.providers.anthropic import build_thinking_params
from axon.providers.base import Usage
from axon.providers.pricing import cost_details, price_for


def _item(mid, *, adaptive=True, enabled=False, disabled=False, between=False, levels=("low", "medium", "high"), **extra):
    sup = lambda v: {"supported": v}
    return {"id": mid, "line": "sonnet", "lifecycle": extra.pop("lifecycle", "active"), "capabilities": {
        "thinking": {"supported": True, "types": {"adaptive": sup(adaptive), "enabled": sup(enabled),
                                                  "disabled": sup(disabled), "between_tools": sup(between)}},
        "effort": {"supported": bool(levels), **{lv: sup(lv in levels) for lv in ("low", "medium", "high", "max")}, "xhigh": sup("xhigh" in levels)},
        "structured_outputs": sup(False), "pdf_input": sup(True), "image_input": sup(True),
        "context_management": {"supported": False, "compact_20260112": None}}, **extra}


@pytest.fixture(autouse=True)
def _clean():
    model_caps.clear()
    yield
    model_caps.clear()


def test_parse_capabilities_and_lifecycle():
    model_caps.record([_item("claude-x-1", levels=("low", "high", "xhigh"), lifecycle="deprecated", retires_at="2027-01-15T00:00:00Z")])
    c = model_caps.live("claude-x-1")
    assert c.effort_levels == ("low", "high", "xhigh") and c.thinking_types["adaptive"] and not c.thinking_types["enabled"]
    assert model_caps.allows("claude-x-1", "structured_outputs") is False and model_caps.allows("claude-x-1", "pdf_input") is True
    assert model_caps.lifecycle_note("claude-x-1") == "deprecated · retires 2027-01-15"
    assert "deprecated" in model_caps.deprecation_warning("claude-x-1")
    assert model_caps.deprecation_warning("unknown-model") == ""


def test_never_sends_an_unsupported_thinking_type_or_level():
    model_caps.record([_item("claude-x-1", adaptive=False, enabled=True, levels=("low", "medium"))])
    out = build_thinking_params("claude-x-1", thinking=True, effort="quantum", max_tokens=20_000)
    assert out["thinking"]["type"] == "enabled" and out["output_config"] == {"effort": "medium"}
    # Sonnet 5.5 style: between_tools only; effort above high is lowered, never sent with it
    model_caps.record([_item("claude-s-55", disabled=False, between=True, levels=("low", "medium", "high", "xhigh"))])
    out = build_thinking_params("claude-s-55", thinking=False, effort="quantum", max_tokens=8_000)
    assert out["thinking"] == {"type": "between_tools"} and out["output_config"] == {"effort": "high"}
    out = build_thinking_params("claude-s-55", thinking=True, effort="quantum", max_tokens=8_000)
    assert out["thinking"]["type"] == "adaptive" and out["output_config"] == {"effort": "xhigh"}
    # a model with no effort and adaptive-only thinking
    model_caps.record([_item("claude-y", levels=())])
    assert "output_config" not in build_thinking_params("claude-y", thinking=True, effort="high", max_tokens=8_000)


def test_fallback_tuples_when_the_list_is_unavailable():
    out = build_thinking_params("claude-opus-4-6", thinking=True, effort="quantum", max_tokens=8_000)
    assert out == {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "max"}}
    assert model_caps.effort_levels("claude-haiku-4-5") == ()
    assert model_caps.lookup("claude-opus-5-5").source == "table"


def test_other_providers_table_and_unknown():
    assert model_caps.offered_efforts("gpt-5.1") == ["reflex", "balanced", "synapse"]
    assert model_caps.offered_efforts("grok-3-mini") == ["reflex", "synapse"]
    assert model_caps.offered_efforts("some-unknown-model") == ["reflex", "balanced", "synapse", "quantum"]  # unknown: nothing hidden
    assert model_caps.effort_levels("some-unknown-model") == ()                                              # ...and nothing sent
    model_caps.record([{"id": "vendor/m", "supported_parameters": ["reasoning", "tools"]}])
    assert model_caps.effort_levels("vendor/m") == ("low", "medium", "high")


def test_offered_efforts_from_live_caps():
    model_caps.record([_item("claude-x-1", levels=("low", "medium", "high"))])
    assert model_caps.offered_efforts("claude-x-1") == ["reflex", "balanced", "synapse"]


def test_fast_request_shape_and_fallback():
    kw = {"model": "claude-opus-5-5", "extra_body": {"output_config": {"effort": "high"}}}
    assert speed_wire.apply_fast(kw, "claude-opus-5-5") and kw["extra_body"]["speed"] == "fast"
    assert "extra_headers" not in kw                      # Opus 5.5 needs no beta header
    k2 = {"extra_body": {}}
    speed_wire.apply_fast(k2, "claude-opus-5")
    assert k2["extra_headers"]["anthropic-beta"] == speed_wire.BETA_FAST
    assert speed_wire.supports_fast("claude-opus-5-5") and not speed_wire.supports_fast("claude-opus-5-5", official=False)
    assert not speed_wire.supports_fast("claude-sonnet-5-5") and not speed_wire.supports_fast("gpt-5")
    assert speed_wire.is_fast_request(kw)
    assert speed_wire.strip_fast(kw) and "speed" not in kw["extra_body"] and kw["extra_body"]["output_config"]
    assert speed_wire.strip_fast(k2) and "extra_body" not in k2 and "extra_headers" not in k2


def test_fast_premium_in_cost():
    q = price_for("claude-opus-5-5")
    if not q.priced:
        pytest.skip("model not in the price table")
    std = cost_details("claude-opus-5-5", Usage(input=1_000_000, output=1_000_000)).cost
    fast = cost_details("claude-opus-5-5", Usage(input=1_000_000, output=1_000_000, speed="fast")).cost
    assert fast == Decimal(8) + Decimal(40) and fast > std
    assert speed_wire.premium("claude-opus-5-5", q) > 1
    assert (Usage(input=5, output=5, speed="fast") + Usage()).speed == "fast"
    assert (Usage(input=5, output=5, speed="fast") + Usage(input=1, output=1)).speed == ""


def _drive(prov, monkeypatch, fail_first=None, messages=None, **stream_kw):
    from axon.providers.base import AssistantTurn, TextBlock, TurnComplete
    seen = []

    def fake_run(kwargs, state, emitted, thinking, wire_model=""):
        seen.append({k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()})
        if fail_first is not None and len(seen) == 1:
            raise fail_first
        state.turn = AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn", usage=Usage())
        yield TurnComplete("end_turn", Usage())

    monkeypatch.setattr(prov, "_run", fake_run)
    monkeypatch.setattr("axon.providers.transport._sleep", lambda *_: None)
    events = list(prov.stream(model="claude-opus-5-5", system=[], messages=messages or [{"role": "user", "content": "hi"}],
                              tools=[], max_tokens=1000, effort="quantum", **stream_kw))
    return seen, events


def _prov():
    from axon.config import Settings
    from axon.providers.anthropic import AnthropicProvider
    return AnthropicProvider(Settings(api_key="k", base_url="https://api.anthropic.com"))


def test_fast_request_goes_out_and_429_falls_back_to_standard_speed(monkeypatch):
    class Err(Exception):
        status_code = 429
        message = "fast mode rate limit"
        body = None

    prov = _prov()
    prov.fast_speed = True
    seen, events = _drive(prov, monkeypatch, fail_first=Err())
    assert seen[0]["extra_body"]["speed"] == "fast"
    assert len(seen) == 2 and "speed" not in seen[1].get("extra_body", {})
    assert "cache miss" in prov.last_speed_note and prov._fast_off is False   # a 429 is capacity: ask again next call
    seen2, _ = _drive(prov, monkeypatch)
    assert seen2[0]["extra_body"]["speed"] == "fast"


def test_fast_is_never_sent_off_the_official_api_or_for_other_models(monkeypatch):
    from axon.config import Settings
    from axon.providers.anthropic import AnthropicProvider
    gw = AnthropicProvider(Settings(api_key="k", base_url="https://agentrouter.org"))
    gw.fast_speed = True
    seen, _ = _drive(gw, monkeypatch)
    assert "speed" not in seen[0].get("extra_body", {})


def test_provider_injects_per_message_effort_with_the_beta(monkeypatch):
    prov = _prov()
    prov.turn_effort = (1, "low")
    msgs = [{"role": "user", "content": "plan"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "yes"}]
    seen, _ = _drive(prov, monkeypatch, messages=msgs)
    sent = seen[0]
    assert sent["messages"][-1]["role"] == "user" and sent["messages"][-2]["role"] == "system" or sent["messages"][-1]["role"] == "system"
    assert any(m.get("role") == "system" and m["output_config"] == {"effort": "low"} for m in sent["messages"])
    assert speed_wire.BETA_MID_EFFORT in sent["extra_headers"]["anthropic-beta"]
    assert sent["extra_body"]["output_config"]["effort"] == "xhigh"        # the top-level value is untouched


def test_per_message_effort_marks():
    msgs = [{"role": "user", "content": "plan it"}, {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
            {"role": "assistant", "content": "done"}, {"role": "user", "content": "yes"}]
    assert speed_wire.new_input_turns(msgs) == [0, 4]      # a tool_result-only user turn is not new input
    out, n = speed_wire.inject_effort_marks(msgs, [(1, "low")])
    assert n == 1 and out[5] == {"role": "system", "content": [], "output_config": {"effort": "low"}} and out[4]["content"] == "yes"
    state = {}
    marks = speed_wire.update_marks(state, (7, "low"), msgs, "high", "claude-opus-5-5")
    assert marks == [(1, "low")]
    assert speed_wire.update_marks(state, (7, "low"), msgs, "high", "claude-opus-5-5") == [(1, "low")]   # recorded once
    assert speed_wire.update_marks({}, (8, "high"), msgs, "high", "claude-opus-5-5") == []              # same as in effect
    assert speed_wire.supports_mid_effort("claude-sonnet-5-5") and not speed_wire.supports_mid_effort("claude-fable-5")
    kw = {"messages": out, "extra_headers": {"anthropic-beta": speed_wire.BETA_MID_EFFORT}}
    assert speed_wire.strip_effort_marks(kw) and len(kw["messages"]) == 5 and "extra_headers" not in kw


def _agent(provider_name, model, effort="quantum", auto=True):
    s = SimpleNamespace(effort=effort, model=model, auto_effort=auto, provider=None, base_url="https://x", fast_models={}, small_model=None)
    return SimpleNamespace(settings=s, provider=SimpleNamespace(name=provider_name), _turn_counter=3,
                           conversation=SimpleNamespace(messages=[{"role": "user", "content": "ok"}]))


def test_auto_effort_per_step_for_openai_style_providers():
    a = _agent("openai", "gpt-5.1")
    assert speed.prepare(a, 1) == "quantum"                      # first step: configured effort
    assert speed.prepare(a, 2) == "low" and a._effort_now == "low"   # routine follow-up step
    assert speed.status_tag(a) == "auto low"
    off = _agent("openai", "gpt-5.1", auto=False)
    assert speed.prepare(off, 2) == "quantum"


def test_auto_effort_skips_models_without_effort_with_a_note():
    a = _agent("openai", "mystery-model")
    assert speed.prepare(a, 2) == "quantum" and "no effort setting" in speed.take_note(a)
    assert speed.take_note(a) == ""


def test_auto_effort_anthropic_is_per_turn_via_provider_marks():
    a = _agent("anthropic", "claude-opus-5-5")
    assert speed.prepare(a, 1) == "quantum" and a.provider.turn_effort == (3, "low")     # short routine message
    a.conversation.messages = [{"role": "user", "content": "Please plan and refactor the whole auth module step by step"}]
    speed.prepare(a, 1)
    assert a.provider.turn_effort == (3, "xhigh")
    speed.prepare(a, 2)                                                                  # follow-up steps never change it
    assert a.provider.turn_effort == (3, "xhigh")


def test_fast_model_resolution_for_other_providers():
    a = _agent("openai", "gpt-5.1")
    a.settings.small_model = "gpt-5.1-mini"
    assert speed.fast_model_for(a) == "gpt-5.1-mini"
    a.settings.small_model = None
    a.settings.fast_models = {"": "x-fast"}
    assert speed.fast_model_for(a) == "x-fast"
    assert not speed.api_fast_supported(a)
