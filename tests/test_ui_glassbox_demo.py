"""Glass-box timeline for a realistic multi-step turn (read/grep, subagent with nested steps, steer,
edit + hook, final answer), rendered live and replayed from the stored TurnTrace."""
from __future__ import annotations

import io
import sys
import time

import pytest

from axon.agent.trace import (HookEvent, LLMCallEnd, PermissionDecisionEvent, SteerEvent, SubagentEvent,
                              SubagentStep, ToolTiming, TurnTrace)
from axon.providers.base import (LLMCallStart, TextDelta, ThinkingDelta, ToolBatchStart, ToolExecutionResult,
                                 ToolExecutionStart, ToolUseBlock, TurnComplete, Usage)
from axon.ui import render
from axon.ui.render import Renderer, format_trace
from axon.ui.theme import display_width, strip_ansi

WIDTH = 100


class _Utf8(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return False


def script(emit) -> None:
        now = time.time()
        emit(LLMCallStart(iteration=1, max_iterations=20, model="claude-opus-5", message_count=1,
                                tokens_in=12_400, user_prompt="fix the failing test in test_app.py", mode="default"))
        emit(ThinkingDelta("The test fails on handle(); I should read app.py and search for the handler. "))
        emit(TextDelta("Let me look at the code first.\n"))
        u1 = Usage(input=12_400, output=412, cache_read=9_800)
        emit(TurnComplete(stop_reason="tool_use", usage=u1))
        emit(LLMCallEnd(iteration=1, model="claude-opus-5", usage=u1, cost=0.0031, duration_s=1.9,
                              stop_reason="tool_use", started_at=now, ended_at=now + 1.9, tool_use_count=2,
                              context_used=12_812, context_window=200_000))
        calls = [ToolUseBlock("t1", "Read", {"file_path": "src/app.py"}),
                 ToolUseBlock("t2", "Grep", {"pattern": "def handle", "path": "src/", "output_mode": "content"})]
        emit(ToolBatchStart(total_count=2, tools=calls))
        for c, content in zip(calls, ["\n".join(f"{i:>6}→code line {i}" for i in range(1, 201)),
                                      "src/app.py:12:def handle(x):\nsrc/util.py:3:def handle_all():"]):
            emit(ToolExecutionStart(c.id, c.name, c.input, rationale="Need to see the handler implementation"))
            emit(PermissionDecisionEvent(id=c.id, tool=c.name, outcome="allow", reason="read-only", source="mode"))
            emit(ToolExecutionResult(c.id, c.name, c.input, content))
            emit(ToolTiming(c.id, c.name, now, now + 0.02, 0.02, 200, 6100, 1600, False, True))
        emit(LLMCallStart(iteration=2, max_iterations=20, model="claude-opus-5", message_count=4,
                                tokens_in=14_100, tools_ingested=[{"name": "Read", "bytes": 6100, "tokens": 1600, "lines": 200},
                                                                  {"name": "Grep", "bytes": 80, "tokens": 20, "lines": 2}]))
        u2 = Usage(input=14_100, output=220, cache_read=12_400)
        emit(TurnComplete(stop_reason="tool_use", usage=u2))
        emit(LLMCallEnd(iteration=2, model="claude-opus-5", usage=u2, cost=0.0024, duration_s=1.2,
                              stop_reason="tool_use", started_at=now, ended_at=now + 1.2, tool_use_count=1,
                              context_used=14_320, context_window=200_000))
        task = ToolUseBlock("t3", "Task", {"description": "run the test suite", "prompt": "run pytest", "subagent_type": "general"})
        emit(ToolBatchStart(total_count=1, tools=[task]))
        emit(ToolExecutionStart(task.id, task.name, task.input))
        emit(PermissionDecisionEvent(id="t3", tool="Task", outcome="allow", reason="", source="user", approval="once"))
        emit(SubagentEvent(phase="start", task_id="s1", agent_type="general", title="run the test suite", model="claude-haiku-4"))
        emit(SubagentStep(task_id="s1", agent_type="general", kind="llm", name="claude-haiku-4",
                                summary="tool_use · 1 tool call(s)", duration_s=0.8, cost=0.0004, input_tokens=3200, output_tokens=80))
        emit(SubagentStep(task_id="s1", agent_type="general", kind="tool", name="Bash",
                                summary="pytest -q -> 12 lines · ~90 tok", duration_s=3.1))
        emit(SteerEvent(count=1, texts=("also check the edge case with None",), iteration=2))
        emit(SubagentEvent(phase="end", task_id="s1", agent_type="general", title="run the test suite", model="claude-haiku-4",
                                 cost=0.0009, input_tokens=6400, output_tokens=310, status="completed", duration_s=4.2, iterations=2))
        emit(ToolExecutionResult(task.id, task.name, task.input, "1 failed, 11 passed: test_handle_none"))
        emit(LLMCallStart(iteration=3, max_iterations=20, model="claude-opus-5", message_count=6, tokens_in=15_000))
        edit = ToolUseBlock("t4", "Edit", {"file_path": "src/app.py", "old_string": "    return x.value\n",
                                           "new_string": "    if x is None:\n        return None\n    return x.value\n"})
        u3 = Usage(input=15_000, output=180, cache_read=14_100)
        emit(TurnComplete(stop_reason="tool_use", usage=u3))
        emit(LLMCallEnd(iteration=3, model="claude-opus-5", usage=u3, cost=0.0021, duration_s=1.0,
                              stop_reason="tool_use", started_at=now, ended_at=now + 1.0, context_used=150_000, context_window=200_000))
        emit(ToolBatchStart(total_count=1, tools=[edit]))
        emit(ToolExecutionStart(edit.id, edit.name, edit.input))
        emit(HookEvent(event="PreToolUse", ran=1, tool_name="Edit", tool_use_id="t4"))
        emit(ToolExecutionResult(edit.id, edit.name, edit.input, "Edited src/app.py"))
        emit(LLMCallStart(iteration=4, max_iterations=20, model="claude-opus-5", message_count=8, tokens_in=15_600))
        emit(TextDelta("Fixed: `handle()` now returns **None** for a missing value.\n"))
        u4 = Usage(input=15_600, output=60, cache_read=15_000)
        emit(TurnComplete(stop_reason="end_turn", usage=u4))
        emit(LLMCallEnd(iteration=4, model="claude-opus-5", usage=u4, cost=0.0012, duration_s=0.7,
                              stop_reason="end_turn", started_at=now, ended_at=now + 0.7, context_used=15_660, context_window=200_000))


@pytest.fixture
def out(monkeypatch):
    monkeypatch.setattr(render, "term_width", lambda: WIDTH)

    def run(level: str = "normal") -> tuple[str, TurnTrace]:
        monkeypatch.setattr(sys, "stdout", _Utf8())
        r = Renderer(trace_level=level)
        trace = TurnTrace(turn=1, prompt="fix the failing test in test_app.py", model="claude-opus-5", mode="default")

        def emit(e):
            r.on_event(e)
            trace.record(e)
        script(emit)
        r.turn_footer(4, Usage(input=57_100, output=872), 0.12, 9.3, llm_calls=4)
        text = sys.stdout.getvalue()
        monkeypatch.undo()
        monkeypatch.setattr(render, "term_width", lambda: WIDTH)
        return text, trace
    return run


def test_live_timeline_structure(out):
    text, _ = out("normal")
    plain = strip_ansi(text)
    rows = plain.splitlines()
    assert rows[0].startswith("┌ Turn 1") and rows[0].rstrip().endswith("claude-opus-5 · default mode")
    assert all(display_width(r) <= WIDTH for r in rows)
    for n in (1, 2, 3, 4):
        assert f"◆ Step {n}  LLM call · claude-opus-5 · reads" in plain
    # per-step context gauge from LLMCallEnd.context_used / context_window
    assert "context █░░░░░░░ 6% of 200k" in plain and "context ██████░░ 75% of 200k" in plain
    # timeline rows keep the rail; only response bodies (markdown) and the footer leave it
    body_rows = [r for r in rows[1:] if r.strip() and not r.startswith("  ") and not r.startswith("└")]
    assert all(r.startswith("│") for r in body_rows), body_rows
    # a subagent's own steps stay out of the normal timeline (the monitor and /subagents show them) ...
    start = next(i for i, r in enumerate(rows) if "subagent started" in r)
    end = next(i for i, r in enumerate(rows) if "subagent completed" in r)
    assert not [r for r in rows[start + 1:end] if "┊" in r]
    # ... and appear, labelled with the subagent, between its start and end lines at the detailed level
    drows = strip_ansi(out("detailed")[0]).splitlines()
    start = next(i for i, r in enumerate(drows) if "subagent started" in r)
    end = next(i for i, r in enumerate(drows) if "subagent completed" in r)
    nested = [r for r in drows[start + 1:end] if "┊" in r]
    assert len(nested) == 2 and "LLM claude-haiku-4" in nested[0] and "Bash" in nested[1]
    assert "1 message you typed during the turn sent to the model" in plain
    assert "took " in plain                           # timing under the subagent card, not glued to it
    assert "auto-allowed" not in plain and "approved by user (once)" in plain
    assert "hook PreToolUse ran" in plain
    assert plain.count("▎ ") == 2                    # both responses carry the accent bar
    assert rows[-3].startswith("└ ✓ Done") or any(r.startswith("└ ✓ Done") for r in rows)


def test_compact_level_is_one_line_per_tool(out):
    text, _ = out("compact")
    plain = strip_ansi(text)
    assert "◆ Step" not in plain and "┊" not in plain
    assert "● Read(src/app.py)" in plain and "✓ Done" in plain


def test_stored_trace_replay_has_nested_steps_and_gauges(out, monkeypatch):
    _, trace = out("normal")
    replay = strip_ansi(format_trace(trace, "normal"))
    assert "subagent run the test suite completed · 2 steps · 4.2s · $0.0009" in replay
    assert replay.count("┊") == 2
    assert "context ██████░░ 75% of 200k" in replay
    assert "you typed during the turn" in replay
    assert all(display_width(r) <= WIDTH for r in replay.splitlines())
