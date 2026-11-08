"""Cache autopsy: why the prompt cache missed, how many tokens were re-sent and what that cost."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from axon.agent.trace import CacheStatus, CacheTracker, LLMCallEnd
from axon.features import insights
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


def _turn(read: int, write: int, total: int, *, tool: bool, diagnostics: Any = None) -> AssistantTurn:
    blocks: list[Any] = [TextBlock(text="x")]
    if tool:
        blocks = [ToolUseBlock(id=f"t{read}{write}", name="Ls", input={"path": "."})]
    t = AssistantTurn(blocks=blocks, stop_reason="tool_use" if tool else "end_turn",
                      usage=Usage(input=total, output=10, cache_read=read, cache_write=write))
    if diagnostics is not None:
        t.diagnostics = diagnostics
    return t


def _run(workspace: Path, turns: list[AssistantTurn]) -> list[Any]:
    events: list[Any] = []
    agent = make_agent(FakeProvider(turns), workspace, events=events, model="claude-sonnet-4-5")
    agent.run_turn("go")
    return events


def test_sharp_drop_reports_tokens_resent(workspace: Path) -> None:
    ev = _run(workspace, [_turn(0, 50_000, 50_000, tool=True), _turn(50_000, 100, 50_100, tool=True),
                          _turn(8_000, 42_000, 50_300, tool=False)])
    status = [e for e in ev if isinstance(e, CacheStatus)]
    assert [s.miss for s in status] == [True, False, True]      # first call is cold; the third dropped sharply
    assert status[2].resent_tokens == 42_000 and status[2].source == "local"
    end = [e for e in ev if isinstance(e, LLMCallEnd)][2]
    assert "42k tokens re-sent" in end.cache_miss_detail and "+$" in end.cache_miss_detail


def test_steady_cache_hits_say_nothing(workspace: Path) -> None:
    ev = _run(workspace, [_turn(0, 50_000, 50_000, tool=True), _turn(50_000, 100, 50_100, tool=True),
                          _turn(50_100, 50, 50_200, tool=False)])
    ends = [e for e in ev if isinstance(e, LLMCallEnd)]
    assert not ends[1].cache_miss_detail and not ends[2].cache_miss_detail
    assert not ends[1].cache_miss_cause and not ends[2].cache_miss_cause


def test_api_diagnostics_win_over_the_local_guess(workspace: Path) -> None:
    diag = {"cache_miss_reason": {"type": "system_changed"}, "cache_missed_input_tokens": 41_000}
    ev = _run(workspace, [_turn(0, 50_000, 50_000, tool=True), _turn(50_000, 100, 50_100, tool=True),
                          _turn(9_000, 41_000, 50_200, tool=False, diagnostics=diag)])
    s = [e for e in ev if isinstance(e, CacheStatus)][2]
    assert s.cause == "system prompt changed" and s.resent_tokens == 41_000 and s.source == "api"


def test_unavailable_diagnostics_fall_back_to_the_local_cause(workspace: Path) -> None:
    diag = {"cache_miss_reason": {"type": "unavailable"}}
    ev = _run(workspace, [_turn(0, 50_000, 50_000, tool=True), _turn(50_000, 100, 50_100, tool=True),
                          _turn(8_000, 42_000, 50_300, tool=False, diagnostics=diag)])
    s = [e for e in ev if isinstance(e, CacheStatus)][2]
    assert s.source == "local" and s.resent_tokens == 42_000


def test_describe_miss_formats_unknown_price_without_a_dollar_figure() -> None:
    line = insights.miss_detail(resent=41_200, extra_cost=None)
    assert line == " · 41k tokens re-sent"
    assert insights.miss_detail(resent=0, extra_cost=0.11) == ""
    assert insights.miss_detail(resent=41_200, extra_cost=0.11) == " · 41k tokens re-sent · +$0.11"


def test_extra_cost_uses_the_write_rate_over_the_read_rate() -> None:
    cost = insights.extra_cost("claude-sonnet-4-5", 1_000_000, Usage(input=1_000_000, cache_write=1_000_000))
    assert cost is not None and abs(cost - (3.75 - 0.30)) < 1e-6
    assert insights.extra_cost("no-such-model-xyz", 1000, Usage(input=1000)) is None


def test_tracker_remembers_previous_read() -> None:
    tr = CacheTracker()
    fp = tr.fingerprint("m", "", [], [], [])
    tr.observe(fp, Usage(input=5000, cache_read=4000))
    assert tr.last_prev_read == 0 and tr.prev_read == 4000
    tr.observe(tr.fingerprint("m", "", [], [], []), Usage(input=5000, cache_read=500, cache_write=4500))
    assert tr.last_prev_read == 4000



def test_renderer_prints_one_muted_line_with_the_detail(capsys: Any) -> None:
    from axon.ui.render import Renderer
    from axon.ui.theme import strip_ansi
    from axon.providers.base import LLMCallStart
    r = Renderer(trace_level="detailed")
    r.on_event(LLMCallStart(iteration=1, max_iterations=20, model="claude-sonnet-4-5", message_count=1, tokens_in=50_000))
    r.on_event(LLMCallEnd(iteration=1, model="claude-sonnet-4-5", usage=Usage(input=50_000, output=10, cache_read=8000, cache_write=42_000),
                          cost=0.2, duration_s=1.0, stop_reason="end_turn", started_at=0.0, ended_at=1.0,
                          cache_miss_cause="system prompt changed", cache_miss_detail=" · 41k tokens re-sent · +$0.11"))
    out = strip_ansi(capsys.readouterr().out)
    assert out.count("cache miss: system prompt changed · 41k tokens re-sent · +$0.11") == 1
