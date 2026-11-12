"""Server-side context editing, exact token counts and the Anthropic version/beta registry (all against fakes)."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from axon.agent import context_edit as ce
from axon.agent import exact_count
from axon.agent.trace import ContextEditEvent
from axon.config import Settings
from axon.providers import anthropic_wire as wire
from axon.providers.base import AssistantTurn, TextBlock, TextDelta, TurnComplete, Usage
from tests.fakes import FakeProvider

OFFICIAL = "https://api.anthropic.com"


# ─── wire constants ────────────────────────────────────────────────────────

def test_api_version_is_new_only_on_the_official_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXON_ANTHROPIC_VERSION", raising=False)
    assert wire.api_version(OFFICIAL) == "2026-10-21"
    assert wire.api_version("https://agentrouter.org") == "2023-06-01"
    assert wire.api_version("http://localhost:11434") == "2023-06-01"
    assert wire.api_version("") == "2023-06-01"
    monkeypatch.setenv("AXON_ANTHROPIC_VERSION", "2023-06-01")
    assert wire.api_version(OFFICIAL) == "2023-06-01"


def test_no_call_site_spells_the_version_itself() -> None:
    root = Path(wire.__file__).resolve().parents[1]
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p.name != "anthropic_wire.py" and "2023-06-01" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_stale_1m_beta_header_is_gone() -> None:
    src = (Path(wire.__file__).parent / "anthropic.py").read_text(encoding="utf-8")
    assert "context-1m-2025-08-07" not in src.replace("(the context-1m-2025-08-07 beta is gone", "")


def test_beta_registry_merges_without_duplicates() -> None:
    kw: dict[str, Any] = {"extra_headers": {"anthropic-beta": "task-budgets-2026-03-13", "x-other": "1"}}
    wire.add_beta(kw, wire.BETA_CONTEXT_MANAGEMENT)
    wire.add_beta(kw, wire.BETA_CONTEXT_MANAGEMENT)
    assert kw["extra_headers"]["anthropic-beta"] == "task-budgets-2026-03-13,context-management-2025-06-27"
    wire.drop_beta(kw, wire.BETA_CONTEXT_MANAGEMENT)
    assert kw["extra_headers"] == {"anthropic-beta": "task-budgets-2026-03-13", "x-other": "1"}
    only: dict[str, Any] = {}
    wire.add_beta(only, "a")
    wire.drop_beta(only, "a")
    assert only == {}


def test_official_sdk_client_sends_the_new_version_and_gateways_keep_the_old() -> None:
    from axon.providers.anthropic import AnthropicProvider
    official = AnthropicProvider(Settings(api_key="k", base_url=OFFICIAL))
    assert official._official and official._client.default_headers["anthropic-version"] == "2026-10-21"
    gateway = AnthropicProvider(Settings(api_key="k", base_url="https://agentrouter.org"))
    assert gateway._client.default_headers["anthropic-version"] == "2023-06-01"


def test_models_ping_and_verifier_headers_use_the_central_version() -> None:
    from axon.providers.verifier import build_ping_request
    _url, headers, _body = build_ping_request(None, "k", OFFICIAL, "claude-opus-5-5")
    assert headers["anthropic-version"] == "2026-10-21"
    _url, headers, _body = build_ping_request(None, "k", "https://agentrouter.org/v1", "m")
    assert headers.get("anthropic-version", "2023-06-01") == "2023-06-01"


# ─── context editing: policy ───────────────────────────────────────────────

def test_supported_model_gate() -> None:
    for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2", "claude-mythos-5-1",
              "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929", "claude-opus-4-8", "claude-sonnet-4-6[1m]",
              "claude-opus-4-5-20251101"):
        assert ce.supports_context_editing(m), m
    for m in ("claude-sonnet-4-20250514", "claude-opus-4-1", "claude-3-7-sonnet", "deepseek-v4-flash", "gpt-5", ""):
        assert not ce.supports_context_editing(m), m


def test_edit_shape_follows_the_docs() -> None:
    edit = ce.build_edit()["edits"][0]
    assert edit["type"] == "clear_tool_uses_20250919"
    assert edit["trigger"] == {"type": "input_tokens", "value": 100_000}
    assert edit["keep"] == {"type": "tool_uses", "value": 6}
    assert edit["clear_at_least"] == {"type": "input_tokens", "value": 25_000}
    assert {"Edit", "Write", "MultiEdit", "Task"} <= set(edit["exclude_tools"])
    assert "clear_thinking_20251015" not in str(ce.build_edit())        # thinking blocks stay as the model default keeps them
    small = ce.build_edit(trigger=1, keep=0, clear_at_least=10**9)["edits"][0]
    assert small["trigger"]["value"] == ce.MIN_TRIGGER and small["keep"]["value"] == 1
    assert small["clear_at_least"]["value"] <= small["trigger"]["value"] // 2


class _Prov:
    def __init__(self, official: bool = True) -> None:
        self._official = official
        self.context_edit: Any = "unset"
        self.last_context_edit: Any = None

    def _wire_model(self, m: str) -> str:
        return m


def _agent(model: str, official: bool = True, **settings: Any) -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(settings=Settings(api_key="k", model=model, **settings), provider=_Prov(official),
                           context=SimpleNamespace(pins={}))


def test_config_for_only_where_it_cannot_400() -> None:
    assert ce.config_for(_agent("claude-opus-5-5")) is not None
    assert ce.config_for(_agent("claude-opus-5-5", official=False)) is None          # gateway / other provider
    assert ce.config_for(_agent("claude-sonnet-4-20250514")) is None                 # model without the feature
    assert ce.config_for(_agent("claude-opus-5-5", context_editing=False)) is None   # switched off
    pinned = _agent("claude-opus-5-5")
    pinned.context.pins = {"toolu_1": {"label": "x", "tokens": 5}}
    assert ce.config_for(pinned) is None                                              # pinned results stay verbatim
    cfg = ce.config_for(_agent("claude-opus-5-5", context_edit_trigger=60_000, context_edit_keep=4, context_edit_clear_at_least=10_000))
    assert cfg["edits"][0]["trigger"]["value"] == 60_000 and cfg["edits"][0]["keep"]["value"] == 4


def test_arm_sets_and_clears_the_provider_field() -> None:
    a = _agent("claude-opus-5-5")
    ce.arm(a)
    assert a.provider.context_edit["edits"][0]["type"] == ce.EDIT_TYPE
    a.settings = a.settings.model_copy(update={"context_editing": False})
    ce.arm(a)
    assert a.provider.context_edit is None


def test_report_summary_and_one_line_event() -> None:
    applied = [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 12, "cleared_input_tokens": 38_200},
               {"type": "clear_thinking_20251015", "cleared_thinking_turns": 3, "cleared_input_tokens": 999}]
    assert ce.summarize(applied) == (12, 38_200)
    assert ce.describe(12, 38_200) == "cleared 12 old tool results · saved ~38k tokens"
    assert ce.describe(1, 800) == "cleared 1 old tool result · saved ~800 tokens"
    a = _agent("claude-opus-5-5")
    a.provider.last_context_edit = {"applied_edits": applied}
    ev = ce.collect(a)
    assert isinstance(ev, ContextEditEvent) and ev.message.startswith("cleared 12 old tool results")
    assert a.provider.last_context_edit is None and ce.collect(a) is None
    a.provider.last_context_edit = {"applied_edits": []}
    assert ce.collect(a) is None                                  # nothing cleared: no line


def test_event_reaches_dashboard_and_sdk_as_context_edit() -> None:
    from axon.features.sdk import event_to_dict
    d = event_to_dict(ContextEditEvent(cleared_tool_uses=2, cleared_input_tokens=5000, message="m"))
    assert d is not None and d["type"] == "context_edit" and d["message"] == "m"
    js = (Path(wire.__file__).parents[1] / "features" / "dash_js_chat.py").read_text(encoding="utf-8")
    assert "case 'context_edit'" in js


# ─── provider wire behaviour ───────────────────────────────────────────────

def _provider(base: str = OFFICIAL) -> Any:
    from axon.providers.anthropic import AnthropicProvider
    return AnthropicProvider(Settings(api_key="k", base_url=base))


def _capture(prov: Any, monkeypatch: pytest.MonkeyPatch, fail_first: Exception | None = None) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(kwargs: dict[str, Any], state: Any, emitted: list[bool], thinking: bool, wire_model: str = "") -> Any:
        seen.append({k: (dict(v) if isinstance(v, dict) else v) for k, v in kwargs.items()})
        if fail_first is not None and len(seen) == 1:
            raise fail_first
        state.turn = AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn", usage=Usage())
        yield TurnComplete("end_turn", Usage())

    monkeypatch.setattr(prov, "_run", fake_run)
    return seen


def _go(prov: Any, model: str = "claude-opus-5-5") -> None:
    list(prov.stream(model=model, system=[{"type": "text", "text": "s"}], messages=[{"role": "user", "content": "hi"}],
                     tools=[], max_tokens=1000))


def test_provider_sends_context_management_and_beta_header(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _provider()
    seen = _capture(prov, monkeypatch)
    prov.context_edit = ce.build_edit()
    _go(prov)
    assert seen[0]["extra_body"]["context_management"]["edits"][0]["type"] == ce.EDIT_TYPE
    assert seen[0]["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"
    assert "thinking" not in seen[0]["extra_body"]["context_management"]["edits"][0]


def test_provider_sends_nothing_without_config_or_on_gateways(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _provider()
    seen = _capture(prov, monkeypatch)
    _go(prov)
    assert "context_management" not in str(seen[0].get("extra_body", {})) and "context-management" not in str(seen[0].get("extra_headers", {}))
    gate = _provider("https://agentrouter.org")
    seen = _capture(gate, monkeypatch)
    gate.context_edit = ce.build_edit()
    _go(gate)
    assert "context_management" not in str(seen[0].get("extra_body", {})) and "extra_headers" not in seen[0]


def test_a_400_about_context_management_retries_plain_and_stops_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bad(Exception):
        status_code = 400
        body = {"error": {"message": "context_management: Extra inputs are not permitted"}}

    prov = _provider()
    seen = _capture(prov, monkeypatch, fail_first=Bad("context_management: Extra inputs are not permitted"))
    prov.context_edit = ce.build_edit()
    _go(prov)
    assert len(seen) == 2
    assert "context_management" in seen[0]["extra_body"]
    assert "context_management" not in seen[1].get("extra_body", {}) and "extra_headers" not in seen[1]
    assert prov._context_edit_off is True
    _go(prov)                                                    # the next call does not try again
    assert "context_management" not in seen[2].get("extra_body", {}) and "extra_headers" not in seen[2]


def test_provider_reads_applied_edits_from_message_delta_and_final_message() -> None:
    from axon.providers.anthropic import _object_field
    from anthropic.types import RawMessageDeltaEvent
    ev = RawMessageDeltaEvent.model_validate({
        "type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3},
        "context_management": {"applied_edits": [{"type": ce.EDIT_TYPE, "cleared_tool_uses": 2, "cleared_input_tokens": 500}]}})
    got = _object_field(ev, "context_management")
    assert ce.summarize(got["applied_edits"]) == (2, 500)


def test_count_tokens_builds_the_same_request_and_asks_with_a_hard_timeout() -> None:
    prov = _provider()
    calls: list[dict[str, Any]] = []

    class Msgs:
        def count_tokens(self, **kw: Any) -> Any:
            calls.append(kw)
            return type("R", (), {"input_tokens": 4321})()

    class Client:
        def with_options(self, **opts: Any) -> Any:
            calls.append(opts)
            return type("C", (), {"messages": Msgs()})()

    prov._client = Client()
    n = prov.count_tokens(model="claude-opus-5-5", system=[{"type": "text", "text": "s"}],
                          messages=[{"role": "user", "content": "hi"}], tools=[], context_management=ce.build_edit(), timeout=2.0)
    assert n == 4321
    assert calls[0] == {"timeout": 2.0, "max_retries": 0}
    assert calls[1]["model"] == "claude-opus-5-5" and calls[1]["messages"][0]["role"] == "user"
    assert calls[1]["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"
    assert prov.supports("count_tokens")
    with pytest.raises(RuntimeError):
        _provider("https://agentrouter.org").count_tokens(model="m", system=[], messages=[], tools=[])
    assert not _provider("https://agentrouter.org").supports("count_tokens")


# ─── exact counts ──────────────────────────────────────────────────────────

class CountingProvider(FakeProvider):
    """Official-looking provider whose count_tokens is scripted."""

    def __init__(self, turns: list[AssistantTurn], answers: list[Any] | None = None, delay: float = 0.0) -> None:
        super().__init__(turns)
        self._official = True
        self.context_edit = None
        self.last_context_edit = None
        self.answers = answers if answers is not None else [123_456]
        self.delay = delay
        self.count_calls: list[dict[str, Any]] = []

    def _wire_model(self, m: str) -> str:
        return m

    def count_tokens(self, **kw: Any) -> int:
        self.count_calls.append(kw)
        if self.delay:
            time.sleep(self.delay)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return int(a)


class StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _big_agent(workspace: Path, provider: Any, **kw: Any) -> Any:
    from tests.test_core_helpers import make_agent as real
    agent = real(provider, workspace, model="claude-opus-5-5", context_limit=100_000, **kw)
    agent.conversation.append_user("x " * 4)
    # ~ 70% of the 75k threshold in estimated tokens: near, not over
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "y" * 220_000}]})
    agent.conversation.messages.append({"role": "user", "content": "go on"})
    return agent


def _refine(agent: Any, first: bool = True) -> bool:
    return agent.context.exact.refine(agent, [{"type": "text", "text": "sys"}], [], first_iteration=first)


def test_counts_once_when_near_the_threshold_and_serves_the_exact_number(workspace: Path) -> None:
    prov = CountingProvider([], answers=[61_234])
    agent = _big_agent(workspace, prov)
    est = agent.context.estimated_tokens(agent.conversation, [{"type": "text", "text": "sys"}], [])
    assert 0.6 * agent.context.threshold("claude-opus-5-5") <= est
    assert _refine(agent) is True
    assert len(prov.count_calls) == 1
    assert prov.count_calls[0]["timeout"] == 2.0
    assert agent.context.exact.value_for(agent.conversation, "claude-opus-5-5") == 61_234
    assert agent.context.projected_tokens(agent.conversation, [], []) == 61_234          # decisions and gauges read it
    assert agent.context.projected_reading(agent.conversation, [], []) == (61_234, True)
    assert _refine(agent, first=False) is True and len(prov.count_calls) == 1             # same conversation: no second request
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "more"}]})
    assert agent.context.exact.value_for(agent.conversation, "claude-opus-5-5") is None   # stale: the estimate shows again
    assert agent.context.projected_reading(agent.conversation, [], [])[1] is False
    assert _refine(agent, first=False) is False and len(prov.count_calls) == 1            # once per turn
    assert _refine(agent, first=True) is True and len(prov.count_calls) == 2              # next turn counts again


def test_exact_count_drives_the_compact_decision(workspace: Path) -> None:
    prov = CountingProvider([], answers=[10_000])
    agent = _big_agent(workspace, prov)
    sys_blocks = [{"type": "text", "text": "sys"}]
    big = agent.context.estimated_tokens(agent.conversation, sys_blocks, [])
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "z" * 120_000}]})
    agent.conversation.messages.append({"role": "user", "content": "k"})
    assert agent.context.needs_compaction(agent.conversation, sys_blocks, [], "claude-opus-5-5")   # estimate says compact
    prov.answers = [20_000]
    assert _refine(agent) is True
    assert not agent.context.needs_compaction(agent.conversation, sys_blocks, [], "claude-opus-5-5")  # exact says no
    assert big > 0


def test_context_management_is_part_of_the_count_when_editing_is_on(workspace: Path) -> None:
    prov = CountingProvider([], answers=[50_000])
    agent = _big_agent(workspace, prov)
    _refine(agent)
    assert prov.count_calls[0]["context_management"]["edits"][0]["type"] == ce.EDIT_TYPE
    prov2 = CountingProvider([], answers=[50_000])
    agent2 = _big_agent(workspace, prov2, context_editing=False)
    _refine(agent2)
    assert prov2.count_calls[0]["context_management"] is None


def test_below_the_near_zone_nothing_is_asked(workspace: Path) -> None:
    prov = CountingProvider([])
    from tests.test_core_helpers import make_agent as real
    agent = real(prov, workspace, model="claude-opus-5-5", context_limit=100_000)
    agent.conversation.append_user("short")
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    assert _refine(agent) is False and prov.count_calls == []


def test_gateway_provider_or_setting_off_keeps_the_estimate(workspace: Path) -> None:
    prov = CountingProvider([])
    prov._official = False
    assert _refine(_big_agent(workspace, prov)) is False and prov.count_calls == []
    prov = CountingProvider([])
    assert _refine(_big_agent(workspace, prov, exact_token_count=False)) is False and prov.count_calls == []
    plain = FakeProvider([])                                         # a provider without count_tokens at all
    assert _refine(_big_agent(workspace, plain)) is False


def test_rate_limit_backs_off_and_unsupported_disables(workspace: Path) -> None:
    prov = CountingProvider([], answers=[StatusError(429)])
    agent = _big_agent(workspace, prov)
    assert _refine(agent) is False
    assert agent.context.exact.off_until > time.monotonic() and not agent.context.exact.disabled
    assert _refine(agent) is False and len(prov.count_calls) == 1       # backing off: no new request
    prov = CountingProvider([], answers=[StatusError(400)])
    agent = _big_agent(workspace, prov)
    assert _refine(agent) is False and agent.context.exact.disabled
    n = len(prov.count_calls)                                            # the 400 retried once without context_management
    assert n == 2
    assert _refine(agent, first=True) is False and len(prov.count_calls) == n


def test_a_400_with_context_management_retries_the_plain_count(workspace: Path) -> None:
    prov = CountingProvider([], answers=[StatusError(400), 40_000])
    agent = _big_agent(workspace, prov)
    assert _refine(agent) is True
    assert [c["context_management"] is None for c in prov.count_calls] == [False, True]
    assert not agent.context.exact.disabled


def test_a_slow_endpoint_never_holds_the_turn_past_the_hard_limit(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exact_count, "HARD_TIMEOUT", 0.15)
    prov = CountingProvider([], answers=[70_000], delay=0.8)
    agent = _big_agent(workspace, prov)
    t0 = time.monotonic()
    assert _refine(agent) is False                                     # late: the turn goes on with the estimate
    assert time.monotonic() - t0 < 0.7
    assert agent.context.exact.off_until > time.monotonic()
    time.sleep(0.9)                                                    # ... and the late answer still lands in the cache
    assert agent.context.exact.value_for(agent.conversation, "claude-opus-5-5") == 70_000


def test_loop_counts_before_compaction_and_emits_the_context_edit_line(workspace: Path) -> None:
    events: list[Any] = []
    turn = AssistantTurn(blocks=[TextBlock(text="done")], stop_reason="end_turn", usage=Usage(input=10, output=5))
    prov = CountingProvider([turn], answers=[30_000])

    def stream(**kw: Any) -> Any:
        prov.seen_edit = prov.context_edit
        prov.last_context_edit = {"applied_edits": [{"type": ce.EDIT_TYPE, "cleared_tool_uses": 12, "cleared_input_tokens": 38_000}]}
        yield TextDelta("done")
        yield TurnComplete("end_turn", Usage(input=10, output=5))
        prov._last = turn

    prov.stream = stream  # type: ignore[assignment]
    prov.finalize = lambda: turn  # type: ignore[method-assign]
    from tests.test_core_helpers import make_agent as real
    agent = real(prov, workspace, events=events, model="claude-opus-5-5", context_limit=100_000)
    agent.conversation.messages.append({"role": "user", "content": "a"})
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "y" * 220_000}]})
    agent.conversation.messages.append({"role": "user", "content": "b"})
    agent.conversation.messages.append({"role": "assistant", "content": [{"type": "text", "text": "w"}]})
    res = agent.run_turn("continue")
    assert res.stop_reason == "end_turn"
    assert len(prov.count_calls) == 1                                  # exact size asked once, before the compact decision
    assert prov.seen_edit["edits"][0]["type"] == ce.EDIT_TYPE          # armed for the call
    lines = [e.message for e in events if isinstance(e, ContextEditEvent)]
    assert lines == ["cleared 12 old tool results · saved ~38k tokens"]
    assert not any(type(e).__name__ == "CompactionEvent" for e in events)   # the exact count showed no need to compact
    assert agent.conversation.messages[1]["content"][0]["text"] == "y" * 220_000   # Axon's own history is untouched


# ─── display ───────────────────────────────────────────────────────────────

def test_status_bar_marks_estimates_only() -> None:
    from axon.ui.statusbar import StatusBar
    est = StatusBar.format_bar("m", "high", "bypass", context_tokens=61_000, context_capacity=200_000, context_estimated=True)
    exact = StatusBar.format_bar("m", "high", "bypass", context_tokens=61_000, context_capacity=200_000)
    assert "~61.0k/200.0k" in est and "~61.0k" not in exact and "61.0k/200.0k" in exact


def test_context_breakdown_is_calibrated_and_exact_with_a_count(workspace: Path) -> None:
    from axon.features.workbench_data import context_breakdown
    prov = CountingProvider([], answers=[88_888])
    agent = _big_agent(workspace, prov)
    before = context_breakdown(agent)
    assert before["exact"] is False
    assert _refine(agent) is True
    after = context_breakdown(agent)
    assert after["exact"] is True and after["calibrated"] is True and after["used"] == 88_888 or after["used"] >= 88_000
    from axon.features.context_inspect import inspect_section
    assert inspect_section(agent, "overview")["estimate"] is False


def test_settings_defaults_and_config_panel() -> None:
    s = Settings(api_key="k")
    assert s.context_editing is True and s.exact_token_count is True
    assert (s.context_edit_trigger, s.context_edit_keep, s.context_edit_clear_at_least) == (100_000, 6, 25_000)
    from axon.commands.system_cmds import _CONFIG_KEYS
    from axon.config import map_json_settings
    for key in ("context_editing", "context_edit_trigger", "context_edit_keep", "context_edit_clear_at_least", "exact_token_count"):
        assert key in _CONFIG_KEYS
    assert map_json_settings({"contextEditing": False, "contextEditTrigger": 50_000})["context_edit_trigger"] == 50_000
    assert threading.active_count() >= 1
