"""/timeline: each step of a turn with duration, tokens, cost and cache-miss cause."""
from __future__ import annotations

from axon.agent.trace import Step, TurnTrace
from axon.features.timeline import render_timeline, turn_timeline
from axon.ui.theme import strip_ansi


def _trace() -> TurnTrace:
    t = TurnTrace(turn=3, prompt="p", model="m", mode="default", started_at=100.0)
    t.steps = [
        Step(1, "llm", 100.0, 102.0, {"iteration": 1, "usage": {"input": 1000, "output": 50, "cache_read": 0}, "cost": 0.0123,
                                      "cache_miss_cause": "system prompt changed", "cache": {"hit_pct": 0.0, "miss": True, "cause": "system prompt changed"}}),
        Step(2, "tool", 102.0, 106.0, {"name": "Bash", "input": {"command": "pytest"}, "duration_s": 4.0, "est_tokens": 300}),
        Step(3, "llm", 106.0, 107.0, {"iteration": 2, "usage": {"input": 1400, "output": 20, "cache_read": 1000}, "cost": 0.004,
                                      "cache": {"hit_pct": 71.4, "miss": False, "cause": ""}}),
    ]
    t.ended_at = 107.0
    return t


def test_rows_carry_only_recorded_numbers():
    d = turn_timeline(_trace())
    assert d["turn"] == 3 and abs(d["total_s"] - 7.0) < 1e-9
    llm1, tool, llm2 = d["rows"]
    assert (llm1["tokens_in"], llm1["tokens_out"], llm1["cost"]) == (1000, 50, 0.0123)
    assert llm1["cache_miss_cause"] == "system prompt changed"
    assert tool["label"].startswith("Bash") and tool["tokens_in"] is None and tool["cost"] is None and tool["duration_s"] == 4.0
    assert llm2["cache_miss_cause"] == "" and llm2["cache_hit_pct"] == 71.4


def test_render_is_a_compact_bar_chart_with_the_miss_cause():
    out = strip_ansi("\n".join(render_timeline(turn_timeline(_trace()), width=80)))
    assert "Bash" in out and "cache miss: system prompt changed" in out and "seconds per step" in out
    assert max(len(l) for l in out.splitlines()) <= 80


def test_empty_trace():
    t = TurnTrace(turn=1, prompt="p", model="m", mode="default")
    assert turn_timeline(t)["rows"] == []
