"""Renderer timeline: trace levels, glass-box events, diffs, footer, spinner and trace replay."""
from __future__ import annotations

import io
import sys
import threading

import pytest

from axon.providers.base import (
    LLMCallStart,
    TextDelta,
    ThinkingDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseBlock,
    TurnComplete,
    Usage,
)
from axon.ui.render import (
    Renderer,
    Spinner,
    format_trace,
    render_status_line,
    render_subagent_result_card,
    tool_target,
)
from axon.ui.theme import strip_ansi


def _turn(r: Renderer, prompt: str = "do it") -> None:
    r.on_event(LLMCallStart(iteration=1, max_iterations=5, model="claude-opus-5", message_count=1,
                            tokens_in=12_400, user_prompt=prompt, mode="bypass"))


def test_every_stream_event_type_is_handled():
    from axon.providers import base

    handled = set(Renderer._HANDLERS)
    for name in ("TextDelta", "ThinkingDelta", "LLMCallStart", "ToolUseStart", "ToolArgsDelta", "ToolUseComplete",
                 "ToolBatchStart", "ToolExecutionStart", "ToolExecutionResult", "TurnComplete"):
        assert getattr(base, name) in handled


def test_trace_level_cycle_and_names():
    r = Renderer()
    assert r.trace_level == "normal"
    assert [r.cycle_trace_level() for _ in range(4)] == ["detailed", "debug", "compact", "normal"]
    assert r.set_trace_level("verbose") == "detailed"
    assert r.set_trace_level("nonsense") == "normal"
    assert Renderer(verbose=True).trace_level == "detailed"


def test_normal_timeline_with_stats_and_answer(capsys):
    r = Renderer()
    _turn(r, "fix the failing test")
    r.on_event(ThinkingDelta("I need to read the file. Then edit."))
    r.on_event(TextDelta("All **good**.\n"))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage(input=12_400, output=412, cache_read=9_800)))
    r.turn_footer(0, Usage(input=12_400, output=412), 0.0031, 1.9, 1)
    out = strip_ansi(capsys.readouterr().out)
    assert '┌ Turn 1 · "fix the failing test"' in out
    assert "◆ Step 1  LLM call · claude-opus-5" in out
    assert "thought for" in out and "I need to read the file." in out
    assert "▎ All good." in out                 # response rows carry the accent bar
    assert "12.4k in (79% cached) · 412 out" in out
    assert "└ ✓ Done" in out


def test_glass_box_events(capsys):
    trace = pytest.importorskip("axon.agent.trace")
    r = Renderer()
    _turn(r)
    r.on_event(TurnComplete(stop_reason="tool_use", usage=Usage(input=100, output=10)))
    r.on_event(trace.LLMCallEnd(iteration=1, model="claude-opus-5", usage=Usage(input=100, output=10), cost=0.5,
                                duration_s=1.25, stop_reason="tool_use", started_at=0.0, ended_at=1.25))
    r.on_event(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id="t1", name="Bash", input={"command": "rm -rf x"})]))
    r.on_event(ToolExecutionStart(id="t1", name="Bash", input={"command": "rm -rf x"}))
    r.on_event(trace.PermissionDecisionEvent(id="t1", tool="Bash", outcome="deny", reason="Bash(rm:*)", source="rule"))
    r.on_event(trace.HookEvent(event="PreToolUse", ran=1, blocked=True, message="nope", tool_use_id="t1", tool_name="Bash"))
    r.on_event(ToolExecutionResult(id="t1", name="Bash", input={}, content="Denied", is_error=True))
    r.on_event(trace.RetryEvent(attempt=1, max_attempts=3, delay_s=0.8, error="529 overloaded"))
    r.on_event(trace.CompactionEvent(trigger="auto", messages_before=40, messages_after=8, tokens_before=150_000, tokens_after=20_000))
    r.on_event(trace.SubagentEvent(phase="end", task_id="s", agent_type="explore", title="map db", model="m", cost=0.002, status="completed"))
    r.on_event(object())  # unknown events are ignored
    out = strip_ansi(capsys.readouterr().out)
    assert "replied in 1.2s · 100 in · 10 out · $0.5000" in out   # LLMCallEnd figures, printed once
    assert out.count("100 in") == 1
    assert "runs 1 tool" not in out           # single-tool plan line only at detailed
    assert "denied by rule: Bash(rm:*)" in out
    assert "hook PreToolUse blocked" in out
    assert "529 overloaded · retrying in 0.8s (retry 1 of 3)" in out
    assert "context compacted (auto) 150k → 20.0k tokens" in out
    assert "subagent completed map db (explore)" in out


def test_edit_deletion_and_multiedit_diffs(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    r = Renderer(trace_level="compact")
    r.on_event(ToolExecutionStart(id="e1", name="Edit", input={"path": "f.py", "old_string": "gone\n", "new_string": ""}))
    r.on_event(ToolExecutionResult(id="e1", name="Edit", input={}, content="ok"))
    out = strip_ansi(capsys.readouterr().out)
    assert "✎ Edited f.py · +0 −1" in out and "- gone" not in out     # compact: the card header row only
    r = Renderer(trace_level="normal")
    r.on_event(ToolExecutionStart(id="e1", name="Edit", input={"path": "f.py", "old_string": "gone\n", "new_string": ""}))
    r.on_event(ToolExecutionResult(id="e1", name="Edit", input={}, content="ok"))
    out = strip_ansi(capsys.readouterr().out)
    assert "Edited f.py · +0 −1" in out and "- gone" in out
    r.on_event(ToolExecutionStart(id="e2", name="MultiEdit", input={"path": "f.py", "edits": [
        {"old_string": "x", "new_string": "three"}, {"old_string": "y\nz", "new_string": "four"}]}))
    r.on_event(ToolExecutionResult(id="e2", name="MultiEdit", input={}, content="ok"))
    out = strip_ansi(capsys.readouterr().out)
    assert "Edited f.py · +2 −3 · 2 edits" in out
    assert "3 + three" in out          # line number located in the edited file
    assert "4 + four" in out


def test_tool_targets():
    assert tool_target("Read", {"path": "a.py", "offset": 10, "limit": 5}) == "a.py · lines 10-14"
    assert tool_target("Grep", {"pattern": "x", "path": "src"}) == '"x" in src'
    assert tool_target("Bash", {"command": "ls\n-la"}) == "ls -la"
    assert tool_target("Unknown", {"foo": "bar"}) == "bar"


def test_subagent_status_not_inferred_from_body():
    ok = strip_ansi(render_subagent_result_card("[Subagent #1 Result (Find bugs) | 1,000 tokens]:\nFound an error handler."))
    assert "Completed" in ok and "Failed" not in ok
    bad = strip_ansi(render_subagent_result_card("[Subagent #2 failed: timeout]"))
    assert "Failed" in bad and "timeout" in bad
    limit = strip_ansi(render_subagent_result_card("[Subagent #3 (x) | 9 tokens]: reached maximum iteration ceiling (5 steps) without concluding."))
    assert "Step limit" in limit


def test_footer_narrow_and_tip_once(capsys, monkeypatch):
    monkeypatch.setattr("axon.ui.render.term_width", lambda: 30)
    monkeypatch.setattr("axon.ui.theme.term_width", lambda: 30)
    Renderer._tip_shown = False
    r = Renderer()
    r.turn_footer(1, Usage(input=6835, output=52), 0.0166, 2.8, 1)
    first = strip_ansi(capsys.readouterr().out)
    assert "6.8k in" in first and "52 out" in first and "Tip:" in first
    assert all(len(l) <= 30 for l in first.splitlines())
    r.turn_footer(1, Usage(), 0.0, 1.0, 1)
    assert "Tip:" not in strip_ansi(capsys.readouterr().out)


def test_banner_adapts_to_width(capsys, monkeypatch):
    r = Renderer()
    for w in (40, 100):
        monkeypatch.setattr("axon.ui.render.term_width", lambda w=w: w)
        r.print_banner("1.2.3", "claude-opus-5", "high", "/tmp/ws", "plan", "http://localhost:11434")
        out = strip_ansi(capsys.readouterr().out)
        assert "Axon v1.2.3" in out and "claude-opus-5" in out and "local inference" in out
        assert all(len(l) <= w for l in out.splitlines())


def test_status_line():
    s = strip_ansi(render_status_line("m", "plan", 80, True, 125, 0.5, "debug", width=120))
    assert s.strip() == "m · plan · ctx 80% · cache warm 2:05 · $0.5000 · trace debug"


def test_format_trace_from_real_turn_trace():
    trace = pytest.importorskip("axon.agent.trace")
    t = trace.TurnTrace(turn=3, prompt="fix", model="m", mode="bypass")
    t.record(LLMCallStart(iteration=1, max_iterations=5, model="m", message_count=2))
    t.record(trace.LLMCallEnd(iteration=1, model="m", usage=Usage(input=1000, output=20, cache_read=500), cost=0.01,
                              duration_s=1.0, stop_reason="tool_use", started_at=0.0, ended_at=1.0))
    t.record(ToolExecutionStart(id="a", name="Read", input={"path": "x.py"}))
    t.record(ToolExecutionResult(id="a", name="Read", input={"path": "x.py"}, content="     1→hi"))
    t.finish("Final **answer**", "end_turn")
    text = strip_ansi(format_trace(t))
    assert '┌ Turn 3 · "fix"' in text
    assert "Step 1  LLM call · m · reads 2 messages" in text and "1.0k in (50% cached)" in text
    assert "● Read  x.py" in text and '"path": "x.py"' in text
    assert "Answer" in text and "Final answer" in text
    assert "└ ✓ Done" in text
    assert strip_ansi(format_trace(t.to_dict())) == text
    assert "No trace" in strip_ansi(format_trace(None))


def test_render_errors_never_propagate(capsys):
    r = Renderer()
    r.on_event(ToolExecutionResult(id="x", name="Read", input=None, content=None))  # type: ignore[arg-type]
    assert isinstance(capsys.readouterr().out, str)


def test_spinner_proxy_erases_line_before_foreign_writes(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    fake = Tty()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.delenv("AXON_NO_SPINNER", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("axon.ui.render.colors_enabled", lambda: True)  # Windows CI consoles lack VT mode
    sp = Spinner(interval=0.01)
    sp.start("Working")
    assert sys.stdout is not fake
    sp._visible = True  # pretend a frame is on screen

    def writer() -> None:
        for _ in range(50):
            print("line from another thread")

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sp.stop()
    assert sys.stdout is fake
    text = fake.getvalue()
    assert text.count("line from another thread") == 200
    for line in text.split("\n"):
        if "line from another thread" in line:
            assert line.rsplit("\r\x1b[2K", 1)[-1] == "line from another thread"


def test_turn_numbers_restart_after_clear(capsys):
    """Turn labels follow the agent's own turn history, so /clear (traces reset) starts at Turn 1 again."""
    from types import SimpleNamespace
    agent = SimpleNamespace(traces=[object()] * 21, registry=None, settings=None)
    r = Renderer()
    r.attach(agent)
    agent.traces.append(object())
    _turn(r)
    assert "┌ Turn 22" in strip_ansi(capsys.readouterr().out)
    r.turn_footer(0, Usage(), 0.0, 0.1, 1)
    agent.traces = [object()]                      # what reset_for_new_session leaves after the first new turn
    _turn(r)
    out = strip_ansi(capsys.readouterr().out)
    assert "┌ Turn 1 " in out and "Turn 22" not in out


def test_restored_conversation_replay_shows_tools_results_and_answers():
    from axon.ui.render import format_conversation_replay
    msgs = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "I should run ls."},
                                          {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.py\nb.py"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "There are **two** files."}]},
    ]
    out = strip_ansi("\n".join(format_conversation_replay(msgs, width=80)))
    assert "› list files" in out and "Turn 1 (restored)" in out
    assert "● Bash  ls" in out and "runs a shell command" in out
    assert "a.py" in out and "b.py" in out                 # the result is shown, not just the call
    assert "thought › I should run ls." in out
    assert "▎ There are two files." in out
    assert out.count("Turn 1") == 1                      # the tool_result message does not open a new turn


def test_tool_rows_say_what_the_tool_does_and_why(capsys):
    trace = pytest.importorskip("axon.agent.trace")
    r = Renderer()
    _turn(r)
    r.on_event(ToolExecutionStart(id="g", name="Grep", input={"pattern": "x"}, rationale="find the handler"))
    r.on_event(ToolExecutionResult(id="g", name="Grep", input={"pattern": "x"}, content="a.py:1:x\nb.py:2:x"))
    out = strip_ansi(capsys.readouterr().out)
    assert "searches inside files · why: find the handler" in out
    assert "a.py · 1 match" in out and "1: x" in out     # search results are grouped by file under the summary


def test_spinner_live_timer_counts_the_current_activity(monkeypatch):
    import axon.ui.render as render_mod
    now = [1000.0]
    monkeypatch.setattr(render_mod.time, "time", lambda: now[0])
    s = Spinner()
    s.started_at = 990.0
    s.set_label("Running Bash")
    now[0] += 3.0
    assert "Running Bash… 3.0s (turn 13s)" in strip_ansi(s.render_line())
    s.set_label("Running Bash")                      # same activity: the clock keeps running
    now[0] += 1.0
    assert "4.0s" in strip_ansi(s.render_line())
    s.set_label("Step 2 · thinking")                 # new activity: its own clock
    assert "thinking… 0.0s" in strip_ansi(s.render_line())
