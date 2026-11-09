"""The live trace shows the exact text of every tool call and result, and reports cost / tokens / time
once, in an end-of-turn table, instead of a line after every LLM call."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from axon.agent.trace import LLMCallEnd, PermissionDecisionEvent
from axon.providers.base import (
    LLMCallStart,
    TextDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseBlock,
    TurnComplete,
    Usage,
)
from axon.session.store import SessionStore
from axon.ui import render, theme
from axon.ui.history_view import format_session_history
from axon.ui.render import Renderer, get_tool_output
from axon.ui.theme import strip_ansi

DETAILED_RESULT_LINES = 40     # rows of a result's exact text at the detailed level
QUERY = {"query": "Anthropic Claude tool use API documentation", "num_results": 3}


def _search_output(results: int = 6) -> str:
    lines = ["Search results for: Anthropic Claude tool use API documentation (via DuckDuckGo)", ""]
    for i in range(1, results + 1):
        lines += [f"{i}. Tool use with Claude, page {i}", f"   https://docs.example.com/tool-use-{i}",
                  f"   Snippet number {i}.", ""]
    return "\n".join(lines).rstrip()


def _usage(step: int) -> Usage:
    return Usage(input=32_500 + step, output=800, cache_read=30_900, cache_write=1_400 if step == 1 else 0)


def _step(r: Renderer, n: int, stop: str, *, end: bool = True) -> None:
    """One LLM call: start, (text for the last step), and its end figures."""
    r.on_event(LLMCallStart(iteration=n, max_iterations=10, model="deepseek-v4-flash", message_count=n,
                            tokens_in=32_000, user_prompt="how does tool use work?", mode="bypass"))
    if stop == "end_turn":
        r.on_event(TextDelta("Tool use works by **declaring tools**.\n"))
    else:
        r.on_event(TextDelta("Let me search the docs first.\n"))
    if not end:
        return
    r.on_event(TurnComplete(stop_reason=stop, usage=_usage(n)))
    r.on_event(LLMCallEnd(iteration=n, model="deepseek-v4-flash", usage=_usage(n), cost=0.0009, duration_s=3.7,
                          stop_reason=stop, started_at=0.0, ended_at=3.7, tool_use_count=1 if stop == "tool_use" else 0,
                          context_used=23_000, context_window=128_000))


def _search_call(r: Renderer, content: str) -> None:
    r.on_event(ToolBatchStart(total_count=1, tools=[ToolUseBlock(id="s1", name="WebSearch", input=QUERY)]))
    r.on_event(ToolExecutionStart(id="s1", name="WebSearch", input=QUERY))
    r.on_event(PermissionDecisionEvent(id="s1", tool="WebSearch", outcome="allow", reason="Bypass mode active.",
                                       source="mode"))
    r.on_event(ToolExecutionResult(id="s1", name="WebSearch", input=QUERY, content=content))


def _two_step_turn(level: str = "normal", content: str | None = None) -> None:
    r = Renderer(trace_level=level)
    _step(r, 1, "tool_use")
    _search_call(r, content if content is not None else _search_output())
    _step(r, 2, "end_turn")
    r.turn_footer(1, Usage(input=65_003, output=1_600), 0.0123, 7.9, 2)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    _columns(monkeypatch, 110)


def _columns(monkeypatch, n: int) -> None:
    monkeypatch.setattr(render, "term_width", lambda: n)
    monkeypatch.setattr(theme, "term_width", lambda: n)


def _plain(capsys) -> str:
    return strip_ansi(capsys.readouterr().out)


def _squeezed(text: str) -> list[str]:
    """Rows without the rail / bar and with runs of spaces collapsed (table cells compare easily)."""
    return [" ".join(row.strip("│ ").split()) for row in text.splitlines()]


def test_exact_call_json_is_shown_after_the_decision_lines(capsys):
    _two_step_turn("detailed")
    out = _plain(capsys)
    assert "exact call · WebSearch" in out
    assert '│ {"query": "Anthropic Claude tool use API documentation", "num_results": 3}' in out
    # order: header, why / does, allowed, then the exact call, then the result
    assert out.index("● WebSearch") < out.index("allowed: bypass mode is on") < out.index("exact call") < out.index("⎿")


def test_exact_call_of_arguments_that_were_not_json_shows_the_raw_text(capsys):
    r = Renderer(trace_level="detailed")
    _step(r, 1, "tool_use")
    r.on_event(ToolExecutionStart(id="b1", name="Grep", input={"raw": "{pattern: oops"},
                                  parse_error="Tool arguments are not valid JSON"))
    r.on_event(ToolExecutionResult(id="b1", name="Grep", input={"raw": "{pattern: oops"},
                                   content="Tool arguments are not valid JSON. Re-issue the call.", is_error=True))
    out = _plain(capsys)
    assert "exact call · Grep (not valid JSON: the raw text the model wrote)" in out
    assert "│ {pattern: oops" in out and '"raw"' not in out


def test_normal_level_shows_result_titles_and_points_to_the_rest(capsys):
    content = _search_output(results=6)
    _two_step_turn(content=content)
    out = _plain(capsys)
    assert "6 results via DuckDuckGo" in out                      # the run line keeps the count
    assert "output  Tool use with Claude, page 1" in out and "Tool use with Claude, page 2" in out
    assert "Snippet number" not in out and "exact result sent to the model" not in out
    pointer = re.search(r"… \+(\d+) more results · /output (\d+)", out)
    assert pointer, out
    assert int(pointer.group(1)) == 4
    assert get_tool_output(int(pointer.group(2)))[2] == content        # /output N gives the whole exact text
    assert "→ 1 result (" in out and "goes to step 2" in out


def test_exact_result_is_shown_and_cut_with_an_output_pointer(capsys):
    content = _search_output(results=15)           # 59 lines: more than the detailed level shows
    _two_step_turn("detailed", content=content)
    out = _plain(capsys)
    assert "15 results via DuckDuckGo" in out                     # the summary row stays
    assert f"exact result sent to the model ({len(content.splitlines())} lines)" in out
    exact_rows = [row for row in out.splitlines() if re.match(r"^│ {8}│ ", row)]      # the result's rows
    assert len(exact_rows) == DETAILED_RESULT_LINES + 1           # 40 rows of text + the pointer
    assert "│ 1. Tool use with Claude, page 1" in out and "│    https://docs.example.com/tool-use-1" in out
    assert "Snippet number 15." not in out
    pointer = re.search(r"… \+(\d+) more lines · /output (\d+) to view all", out)
    assert pointer, out
    assert int(pointer.group(1)) == len(content.splitlines()) - DETAILED_RESULT_LINES
    assert get_tool_output(int(pointer.group(2)))[2] == content
    assert "result #1" in out and "goes to the model in step 2" in out


def test_a_preview_that_is_the_exact_text_is_labelled_not_repeated(capsys):
    r = Renderer(trace_level="detailed")
    _step(r, 1, "tool_use")
    r.on_event(ToolExecutionStart(id="b1", name="Bash", input={"command": "echo hi"}))
    r.on_event(ToolExecutionResult(id="b1", name="Bash", input={"command": "echo hi"}, content="hi"))
    out = _plain(capsys)
    assert "hi · exact result sent to the model" in out
    assert out.count("│ hi") == 0                      # not printed a second time


def test_no_cost_line_in_the_middle_of_the_turn_at_normal_level(capsys):
    _two_step_turn()
    out = _plain(capsys)
    assert "replied in" not in out
    before_answer = out[: out.index("Tool use works by")]
    assert "$0.0009" not in before_answer and "cache write" not in before_answer
    assert "context ███" not in before_answer and "% of 128k" not in before_answer


def test_end_of_turn_table_and_totals_follow_the_final_answer(capsys):
    _two_step_turn()
    out = _plain(capsys)
    rows = _squeezed(out)
    answer = out.index("Tool use works by")
    final = out.index("final answer written in step 2 by deepseek-v4-flash")
    table = out.index("usage by step")
    done = out.index("└ ✓ Done in 7.9s")
    assert answer < final < table < done
    assert "step model time in cached cache write out cost" in rows
    assert "1 deepseek-v4-flash 3.7s 32.5k 95% 1.4k 800 $0.0009" in rows
    assert "2 deepseek-v4-flash 3.7s 32.5k 95% - 800 $0.0009" in rows
    assert "2 LLM calls · 1 tool call" in out and "$0.0018 (session $0.01)" in out
    assert out.count("% of 128k") == 1                 # the context gauge, once, after the turn
    assert out.index("% of 128k") > done


def test_stopped_turn_still_shows_what_was_used(capsys):
    r = Renderer(trace_level="normal")
    _step(r, 1, "tool_use")
    _search_call(r, _search_output(1))
    _step(r, 2, "end_turn", end=False)               # interrupted while the model was writing
    r.turn_footer(1, Usage(input=32_501, output=800), 0.01, 5.0, 2, stop_reason="interrupted", error="interrupted")
    out = _plain(capsys)
    rows = _squeezed(out)
    assert "used so far · the turn stopped" in out
    assert "1 deepseek-v4-flash 3.7s 32.5k 95% 1.4k 800 $0.0009" in rows
    assert any(r.startswith("2 deepseek-v4-flash (stopped)") for r in rows), rows
    assert "✗ Interrupted after 5.0s" in out and "final answer written" not in out
    assert out.index("Tool use works by") < out.index("used so far")


def test_compact_level_leaves_the_accounting_out(capsys):
    _two_step_turn("compact")
    out = _plain(capsys)
    assert "WebSearch  " in out and "results via DuckDuckGo" in out
    for absent in ("exact call", "exact result", "usage by step", "replied in", "% of 128k", "Snippet number 1"):
        assert absent not in out


def test_detailed_level_keeps_a_per_step_line_and_the_table(capsys):
    _two_step_turn("detailed")
    out = _plain(capsys)
    assert "replied in 3.7s" in out and "usage by step" in out


def test_narrow_terminal_gets_one_line_per_step(capsys, monkeypatch):
    _columns(monkeypatch, 50)
    _two_step_turn()
    out = _plain(capsys)
    assert all(len(row) <= 50 for row in out.splitlines())
    assert "step 1 · deepseek-v4-flash · 3.7s" in out


def _session(tmp_path: Path) -> Path:
    store = SessionStore(tmp_path, session_dir=tmp_path / "sessions")
    sid = store.open(None)
    store.append("user_message", {"role": "user", "content": "how does tool use work?"})
    usage = {"input": 32_500, "output": 800, "cache_read": 30_900, "cache_write": 1_400}
    store.append("assistant_turn", {"stop_reason": "tool_use", "text": "Let me search the docs first.", "thinking": "",
                                    "tool_uses": [{"id": "s1", "name": "WebSearch", "input": QUERY}],
                                    "model": "deepseek-v4-flash", "cost": 0.0009, "duration_s": 3.7, "usage": usage,
                                    "call": {"context_used": 23_000, "context_window": 128_000}})
    store.append("tool_results", [{"tool_use_id": "s1", "content": _search_output(6), "is_error": False}])
    store.append("assistant_turn", {"stop_reason": "end_turn", "text": "Tool use works by **declaring tools**.",
                                    "thinking": "", "tool_uses": [], "model": "deepseek-v4-flash", "cost": 0.0009,
                                    "duration_s": 3.7, "usage": usage,
                                    "call": {"context_used": 24_000, "context_window": 128_000}})
    return Path(store._file(sid))


def test_restored_view_shows_the_exact_text_and_the_same_summary(tmp_path: Path):
    out = strip_ansi("\n".join(format_session_history(_session(tmp_path), width=100, trace_level="detailed")))
    rows = _squeezed(out)
    assert "exact call · WebSearch" in out
    assert '{"query": "Anthropic Claude tool use API documentation", "num_results": 3}' in out
    assert "exact result sent to the model (25 lines)" in out and "1. Tool use with Claude, page 1" in out
    assert re.search(r"… \+\d+ more lines · /transcript to view all", out)
    assert "replied in" not in out
    assert out.index("Tool use works by") < out.index("final answer written in step 2") < out.index("usage by step")
    assert "1 deepseek-v4-flash 3.7s 32.5k 95% 1.4k 800 $0.0009" in rows
    assert "└ ✓ Done" in out and "2 LLM calls · 1 tool call" in out and "$0.0018" in out
    assert out.count("% of 128k") == 1


def test_restored_normal_view_draws_the_same_cards_as_the_live_view(tmp_path: Path):
    out = strip_ansi("\n".join(format_session_history(_session(tmp_path), width=100)))
    assert "▣ TOOLS · 1 call" in out and "WebSearch  choose" in out and "exact call" not in out
    assert "output  Tool use with Claude, page 1" in out and re.search(r"… \+4 more results · /transcript", out)
    assert "→ 1 result (" in out and "goes to step 2" in out
    assert out.index("▍ ANSWER") < out.index("▣ TOOLS") < out.index("usage by step")      # the model wrote text, then called
