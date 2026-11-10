"""Steps that happen but were not shown (or were shown wrongly) at the default ``normal`` trace level."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axon.agent.trace import (
    GoalCheckEvent,
    LLMCallEnd,
    PermissionDecisionEvent,
    RetryEvent,
    SubagentEvent,
    SubagentStep,
    ToolRationale,
)
from axon.providers.base import (
    AssistantTurn,
    LLMCallStart,
    TextBlock,
    TextDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseBlock,
    TurnComplete,
    Usage,
)
from axon.ui import agent_panel, render, theme
from axon.ui.render import Renderer
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent

MODEL = "claude-haiku-5-5"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    monkeypatch.setattr(render, "term_width", lambda: 110)
    monkeypatch.setattr(theme, "term_width", lambda: 110)


def _out(capsys) -> str:
    return strip_ansi(capsys.readouterr().out)


def _step(r: Renderer, n: int = 1) -> None:
    r.on_event(LLMCallStart(iteration=n, max_iterations=9, model=MODEL, message_count=n, tokens_in=3300,
                            user_prompt="look around" if n == 1 else "", mode="bypass"))


def _batch(r: Renderer, calls: list[tuple], why: dict[str, tuple[str, str]] | None = None, permission: dict | None = None,
           **batch_kw: Any) -> None:
    r.on_event(ToolBatchStart(total_count=len(calls), tools=[ToolUseBlock(id=c[0], name=c[1], input=c[2]) for c in calls],
                              **batch_kw))
    for cid, name, inp, _o in calls:
        if why and cid in why:
            r.on_event(ToolRationale(id=cid, name=name, text=why[cid][0], source=why[cid][1]))
        r.on_event(ToolExecutionStart(id=cid, name=name, input=inp))
        r.on_event(PermissionDecisionEvent(id=cid, tool=name, **(permission or {"outcome": "allow", "source": "mode",
                                                                                 "reason": "Bypass mode active."})))
    for cid, name, _inp, out in calls:
        r.on_event(ToolExecutionResult(id=cid, name=name, input={}, content=out, is_error=False))


# ── retries held while a full-screen view was open ─────────────────────────

def test_a_retry_held_for_the_monitor_is_shown_when_it_closes(workspace: Path) -> None:
    events: list[Any] = []
    agent = make_agent(FakeProvider([]), workspace, events=events)
    retry = RetryEvent(attempt=1, max_attempts=3, delay_s=0.5, error="HTTP 529 overloaded")
    step = SubagentStep(task_id="sub-1", agent_type="explore", kind="tool", name="Read")
    agent._deliver_after_monitor([retry, step], [], set())
    assert retry in events                      # the retry line reaches the renderer
    assert step not in events                   # the monitor's summary already listed the subagent's steps
    assert "RetryEvent" not in agent_panel._QUIET_EVENTS and "ProviderRetry" not in agent_panel._QUIET_EVENTS


# ── a subagent's own steps without the live monitor ────────────────────────

def _sub(phase: str, n: int, **kw: Any) -> SubagentEvent:
    return SubagentEvent(phase=phase, task_id=f"sub-{n}", agent_type="explore", title=f"job {n}", model=MODEL, **kw)


def _sub_step(n: int, name: str, summary: str) -> SubagentStep:
    return SubagentStep(task_id=f"sub-{n}", agent_type="explore", kind="tool", name=name, summary=summary)


def test_normal_lists_parallel_subagents_steps_grouped_under_each_subagent(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(ToolBatchStart(total_count=2, tools=[ToolUseBlock(id="t1", name="Task", input={"prompt": "a"}),
                                                    ToolUseBlock(id="t2", name="Task", input={"prompt": "b"})]))
    r.on_event(_sub("start", 1))
    r.on_event(_sub("start", 2))
    r.on_event(_sub_step(2, "Grep", "needle_two"))
    r.on_event(_sub_step(1, "Read", "needle_one"))
    r.on_event(_sub_step(2, "Glob", "needle_three"))
    r.on_event(_sub_step(1, "Grep", "needle_four"))
    r.on_event(_sub("end", 2, status="completed", iterations=2))
    r.on_event(_sub("end", 1, status="completed", iterations=2))
    out = _out(capsys)
    for needle in ("needle_one", "needle_two", "needle_three", "needle_four"):
        assert out.count(needle) == 1
    done_2, done_1 = out.index("subagent completed #2"), out.index("subagent completed #1")
    # each subagent's steps sit together under its own 'completed' line, never mixed with the other's
    assert done_2 < out.index("needle_two") < out.index("needle_three") < done_1 < out.index("needle_one") < out.index("needle_four")


def test_normal_shows_a_lone_subagents_steps_as_they_happen_and_compact_stays_short(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id="t1", name="Task", input={"prompt": "a"})]))
    r.on_event(_sub("start", 1))
    r.on_event(_sub_step(1, "Read", "needle_one"))
    live = _out(capsys)
    assert "needle_one" in live and "Read" in live          # nothing to interleave with: shown at once
    r.on_event(_sub("end", 1, status="completed", iterations=1))
    assert "needle_one" not in _out(capsys)                  # and not a second time
    c = Renderer(trace_level="compact")
    _step(c)
    c.on_event(_sub("start", 1))
    c.on_event(_sub_step(1, "Read", "needle_one"))
    c.on_event(_sub("end", 1, status="completed", iterations=1))
    assert "needle_one" not in _out(capsys)


# ── the batch header says how the calls really ran ─────────────────────────

READS = [("a", "Read", {"path": "a.py"}, "     1→x = 1"), ("b", "Glob", {"pattern": "*.py"}, "a.py\nb.py")]


def test_header_follows_how_the_loop_ran_the_batch(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    _batch(r, READS, parallel=False)                 # parallel_tools = 1: read-only calls, run one by one
    assert "▣ TOOLS · 2 calls · one after another" in _out(capsys)
    d = Renderer(trace_level="detailed")
    _step(d)
    _batch(d, READS, parallel=False)
    out = _out(capsys)
    assert "runs 2 tools one after another" in out and "in parallel" not in out
    r = Renderer(trace_level="normal")
    _step(r)
    _batch(r, READS)                                  # no word from the loop: the read-only rule, as before
    assert "▣ TOOLS · 2 calls · in parallel" in _out(capsys)


def _batches(workspace: Path, blocks: list[ToolUseBlock], **settings: Any) -> list[ToolBatchStart]:
    (workspace / "a.py").write_text("x = 1\n")
    events: list[Any] = []
    turns = [AssistantTurn(blocks=blocks, stop_reason="tool_use", usage=Usage(input=10, output=5)),
             AssistantTurn(blocks=[TextBlock(text="done")], stop_reason="end_turn", usage=Usage(input=10, output=5))]
    make_agent(FakeProvider(turns), workspace, events=events, **settings).run_turn("go")
    return [e for e in events if isinstance(e, ToolBatchStart)]


def test_loop_tells_the_batch_event_whether_it_runs_in_parallel(workspace: Path) -> None:
    two = [ToolUseBlock(id="a", name="Read", input={"path": "a.py"}), ToolUseBlock(id="b", name="Glob", input={"pattern": "*.py"})]
    assert _batches(workspace, two, parallel_tools=1)[0].parallel is False
    assert _batches(workspace, two, parallel_tools=4)[0].parallel is True
    mixed = [ToolUseBlock(id="a", name="Read", input={"path": "a.py"}), ToolUseBlock(id="b", name="Bash", input={"command": "true"})]
    assert _batches(workspace, mixed, parallel_tools=4)[0].parallel is False


# ── choose: a reason, not the narration printed right above ────────────────

def test_choose_line_does_not_repeat_the_narration_printed_above_it(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(TextDelta("I'll look at both files now.\n"))
    r.on_event(TurnComplete(stop_reason="tool_use", usage=Usage(input=3300, output=80)))
    _batch(r, READS, why={"a": ("I'll look at both files now.", "model-text")})
    out = _out(capsys)
    assert out.count("I'll look at both files now.") == 1 and "choose" not in out


def test_choose_line_keeps_a_reason_that_is_one_part_of_the_narration(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(TextDelta("Let me check the config. I need the database URL.\n"))
    r.on_event(TurnComplete(stop_reason="tool_use", usage=Usage(input=3300, output=80)))
    _batch(r, [("a", "Read", {"path": "cfg.py"}, "     1→URL = 1"), ("b", "Bash", {"command": "pytest"}, "ok")],
           why={"a": ("I need the database URL.", "model-text")})
    out = _out(capsys)
    choose = [l for l in out.splitlines() if "choose" in l]
    assert len(choose) == 1 and "I need the database URL." in choose[0]


def test_choose_line_from_thinking_stays(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(TextDelta("Checking.\n"))
    _batch(r, READS, why={"a": ("Checking.", "model-thinking")})        # the thought is only previewed above
    assert "choose" in _out(capsys)


def test_history_does_not_repeat_the_narration_as_a_choose_line_either(tmp_path: Path) -> None:
    from axon.session.store import SessionStore
    from axon.ui.history_view import format_session_history
    store = SessionStore(tmp_path, session_dir=tmp_path / "sessions")
    sid = store.open(None)
    store.append("user_message", {"role": "user", "content": "look"})
    store.append("assistant_turn", {
        "stop_reason": "tool_use", "text": "I'll look at both files now.", "thinking": "",
        "tool_uses": [{"id": "a", "name": "Read", "input": {"path": "a.py"}},
                      {"id": "b", "name": "Glob", "input": {"pattern": "*.py"}}],
        "model": MODEL, "usage": {"input": 100, "output": 10}, "call": {"message_count": 1}})
    store.append("tool_results", [{"tool_use_id": "a", "content": "x = 1", "is_error": False},
                                  {"tool_use_id": "b", "content": "a.py", "is_error": False}])
    out = "\n".join(strip_ansi(r) for r in format_session_history(Path(store._file(sid)), width=100))
    assert "▣ TOOLS · 2 calls" in out and out.count("I'll look at both files now.") == 1 and "choose" not in out


# ── permission rule, cache miss and goal check at normal ───────────────────

def test_normal_names_the_rule_that_allowed_a_call(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    _batch(r, [("a", "Bash", {"command": "git status"}, "clean")],
           permission={"outcome": "allow", "source": "rule", "reason": "Matched allow rule: Bash(git *)"})
    assert "allowed: rule Bash(git *)" in _out(capsys)


def test_normal_says_when_the_prompt_cache_missed_and_why(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(LLMCallEnd(iteration=1, model=MODEL, usage=Usage(input=50_000, output=10, cache_read=8000, cache_write=42_000),
                          cost=0.2, duration_s=1.0, stop_reason="end_turn", started_at=0.0, ended_at=1.0,
                          cache_miss_cause="system prompt changed", cache_miss_detail=" · 41k tokens re-sent · +$0.11"))
    assert _out(capsys).count("cache miss: system prompt changed · 41k tokens re-sent · +$0.11") == 1
    c = Renderer(trace_level="compact")
    _step(c)
    c.on_event(LLMCallEnd(iteration=1, model=MODEL, usage=Usage(input=50_000, output=10), cost=0.2, duration_s=1.0,
                          stop_reason="end_turn", started_at=0.0, ended_at=1.0, cache_miss_cause="system prompt changed"))
    assert "cache miss" not in _out(capsys)


def test_normal_says_when_the_goal_check_asks_the_model_again(capsys) -> None:
    r = Renderer(trace_level="normal")
    _step(r)
    r.on_event(GoalCheckEvent(goal="all tests pass", iteration=1))
    out = _out(capsys)
    assert "goal check" in out and "all tests pass" in out
