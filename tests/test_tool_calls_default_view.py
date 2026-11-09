"""The quiet terminal view (the compact trace level, chosen with /trace compact or Alt+L): the answer, one tidy row
per tool call with a short preview, failures with their error, a short summary. Nothing is lost: the transcript
and /output keep it all. The default level is normal, which shows every step."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.trace import PermissionDecisionEvent, ToolRationale
from axon.config import Settings
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
from axon.ui import live_status, render, theme, tool_cards
from axon.ui.history_view import format_session_history
from axon.ui.render import Renderer, get_tool_output, get_transcript, tool_output_details
from axon.ui.theme import display_width, strip_ansi

MODEL = "gpt-4o"
LONG = "\n".join(f"line {i}" for i in range(40))


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    _columns(monkeypatch, 100)


def _columns(monkeypatch, n: int) -> None:
    monkeypatch.setattr(render, "term_width", lambda: n)
    monkeypatch.setattr(theme, "term_width", lambda: n)


def _turn(r: Renderer, calls: list[tuple], text: str = "Let me look.") -> None:
    r.on_event(LLMCallStart(iteration=1, max_iterations=9, model=MODEL, message_count=1, tokens_in=8500,
                            user_prompt="hello", mode="bypass"))
    r.on_event(TextDelta(text=text))
    r.on_event(TurnComplete(stop_reason="tool_use", usage=Usage(input=8500, output=40)))
    r.on_event(ToolBatchStart(total_count=len(calls), tools=[ToolUseBlock(id=c[0], name=c[1], input=c[2]) for c in calls]))
    for cid, name, inp, _out, _err in calls:
        r.on_event(ToolRationale(id=cid, name=name, text=f"Because of {cid}", source="model-text"))
        r.on_event(ToolExecutionStart(id=cid, name=name, input=inp))
        r.on_event(PermissionDecisionEvent(id=cid, tool=name, outcome="allow", reason="Bypass mode active.", source="mode"))
    for cid, name, _inp, out, err in calls:
        r.on_event(ToolExecutionResult(id=cid, name=name, input={}, content=out, is_error=err))
    r.on_event(LLMCallStart(iteration=2, max_iterations=9, model=MODEL, message_count=4, tokens_in=8600))
    r.on_event(TextDelta(text="All done."))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage(input=8600, output=20)))
    r.turn_footer(len(calls), Usage(input=17100, output=60), 0.0425, 4.4, llm_calls=2)


def _rows(name: str, content: str, inp: dict | None = None, *, error: bool = False, width: int = 80,
          duration: float | None = 0.27, **extra) -> list[str]:
    call = tool_cards.CardCall(number=1, name=name, inp=inp or {}, content=content, is_error=error, duration=duration,
                               hint="/output 7", **extra)
    rows = tool_cards.compact_rows(call, width)
    assert all(display_width(r) <= width for r in rows)
    return [strip_ansi(r) for r in rows]


def test_normal_is_the_default_level_and_every_level_is_one_keypress_away() -> None:
    assert Settings().trace_level == "normal"
    r = Renderer()
    assert r.trace_level == "normal"
    assert [r.cycle_trace_level() for _ in range(4)] == ["detailed", "debug", "compact", "normal"]


def test_default_turn_has_no_gutter_labels_or_accounting(capsys) -> None:
    _turn(Renderer(trace_level="compact"), [("t1", "Bash", {"command": "echo hi && ls"}, "hi\nb\nc\nd\ne", False),
                       ("t2", "Read", {"file_path": "notes.md"}, "1→# notes\n2→two", False)])
    out = strip_ansi(capsys.readouterr().out)
    for heavy in ("ANSWER", "TOOLS", "Step ", "Turn ", "one after another", "go to step", "tokens", "LLM call", " in ", "┌", "└", "│", "▎"):
        if heavy == " in ":      # 'Done in 4.4s' says how long; '17.1k in' is what must go
            assert "k in" not in out
            continue
        assert heavy not in out, heavy
    rows = out.splitlines()
    bash = next(r for r in rows if "Bash" in r)
    assert bash.startswith(f"  {theme.glyph('ok')} Bash  echo hi && ls  ")
    assert bash.endswith("s")     # the time trails the row
    assert any("+3 more lines" in r and "/output" in r for r in rows)
    assert any("Read  notes.md  2 lines" in r for r in rows)
    assert "# notes" not in out   # a file's text stays out of the default view
    done = next(r for r in rows if "Done" in r)
    assert done.strip().startswith(f"{theme.glyph('ok')} Done in 4.4s")
    assert "2 tools" in done and "$0.04" in done


def test_a_failure_always_shows_its_error(capsys) -> None:
    _turn(Renderer(trace_level="compact"), [("t1", "Bash", {"command": "ls /nope"}, "Command exited with non-zero code 2.\nls: /nope: No such file or directory", True),
                       ("t2", "Read", {"file_path": "gone.txt"}, "File not found: gone.txt", True)])
    out = strip_ansi(capsys.readouterr().out)
    fail = theme.glyph("fail")
    assert f"{fail} Bash  ls /nope" in out and "exit 2" in out
    assert "ls: /nope: No such file or directory" in out
    assert f"{fail} Read  gone.txt" in out and "File not found: gone.txt" in out


@pytest.mark.parametrize("width", [30, 50, 80, 130])
def test_error_text_survives_every_width(width: int) -> None:
    text = "Command exited with non-zero code 1.\nfatal: not a git repository"
    rows = _rows("Bash", text, {"command": "git status --porcelain " + "x" * 200}, error=True, width=width)
    assert rows[0].startswith(theme.glyph("fail"))
    assert "failed" in rows[0]
    assert "fatal" in " ".join(rows[1:])


def test_a_denied_call_says_blocked_and_why() -> None:
    rows = _rows("Bash", "Permission denied by rule: no rm", {"command": "rm -rf x"}, error=True,
                 allowed="denied by rule: no rm")
    assert "blocked" in rows[0] and "denied by rule: no rm" in " ".join(rows[1:])


def test_a_blocking_hook_note_shows_and_a_routine_one_does_not() -> None:
    rows = _rows("Bash", "ok", {"command": "ls"}, notes=["hook PreToolUse blocked (1 command)", "hook PostToolUse ran (1 command)"])
    assert any("blocked" in r for r in rows) and not any("ran (1" in r for r in rows)


def test_a_subagent_preview_skips_its_token_banner() -> None:
    rows = _rows("Task", "[Subagent #1 Result (x) | 7,491 tokens (in: 7,484)]:\nFound the bug.", {"description": "Look"})
    assert "Found the bug." in " ".join(rows) and "tokens" not in " ".join(rows)


def test_a_long_path_keeps_its_file_name() -> None:
    rows = _rows("Read", "1→x", {"file_path": "a/" * 60 + "target.py"}, width=50)
    assert "target.py" in rows[0]


def test_a_stopped_call_says_so() -> None:
    call = tool_cards.CardCall(number=1, name="Bash", inp={"command": "sleep 99"}, state="stopped", duration=3.0)
    row = strip_ansi(tool_cards.compact_rows(call, 80)[0])
    assert row.startswith(theme.glyph("fail")) and "stopped" in row


def test_a_multi_line_command_shows_its_first_line_and_a_count() -> None:
    rows = _rows("Bash", "ok", {"command": "cd a\nmake\nmake test"})
    assert "cd a +2 lines" in rows[0]


def test_an_edit_keeps_its_diff_and_stat(tmp_path: Path) -> None:
    rows = _rows("Edit", "ok", {"file_path": str(tmp_path / "n.md"), "old_string": "one", "new_string": "two"})
    assert "+1" in rows[0]
    assert any("- one" in r for r in rows[1:]) and any("+ two" in r for r in rows[1:])


def test_a_big_diff_is_cut_with_a_pointer(tmp_path: Path) -> None:
    rows = _rows("Write", "ok", {"file_path": str(tmp_path / "new.py"), "content": LONG})
    assert len(rows) <= 1 + tool_cards.COMPACT_DIFF_ROWS + 1
    assert "more lines" in rows[-1] and "/output 7" in rows[-1]


def test_parallel_results_print_in_call_order(capsys) -> None:
    r = Renderer(trace_level="compact")
    calls = [("a", "Read", {"file_path": "a.md"}, "1→x", False), ("b", "Read", {"file_path": "b.md"}, "1→y", False)]
    r.on_event(LLMCallStart(iteration=1, max_iterations=9, model=MODEL, message_count=1, tokens_in=1, user_prompt="p", mode="bypass"))
    r.on_event(ToolBatchStart(total_count=2, tools=[ToolUseBlock(id=c[0], name=c[1], input=c[2]) for c in calls]))
    for cid, name, inp, _o, _e in calls:
        r.on_event(ToolExecutionStart(id=cid, name=name, input=inp))
    for cid, name, _i, out, err in reversed(calls):
        r.on_event(ToolExecutionResult(id=cid, name=name, input={}, content=out, is_error=err))
    out = strip_ansi(capsys.readouterr().out)
    assert out.index("a.md") < out.index("b.md")


def test_the_detail_is_still_recorded_at_the_default_level() -> None:
    _turn(Renderer(trace_level="compact"), [("t77", "Bash", {"command": "seq 40"}, LONG, False)])
    entry = next(e for e in reversed(get_transcript()) if e.kind == "tool" and e.id == "t77")
    assert entry.body == LONG                      # the whole output
    assert entry.why == "Because of t77"           # why the model made the call
    assert "bypass" in entry.allowed               # the permission outcome
    assert entry.then and entry.duration is not None
    assert get_tool_output(entry.output_no)[2] == LONG
    details = "\n".join(strip_ansi(row) for row in tool_output_details(entry.output_no, 100))
    assert "Because of t77" in details and "bypass" in details


def test_the_detailed_card_is_one_keypress_away(capsys) -> None:
    r = Renderer(trace_level="compact")
    r.cycle_trace_level()
    _turn(r, [("t1", "Bash", {"command": "echo hi"}, "hi", False)])
    out = strip_ansi(capsys.readouterr().out)
    assert "TOOLS" in out and "ANSWER" in out and "Step 1" in out and "goes to step 2" in out


def test_turn_without_tools_is_one_short_row(capsys) -> None:
    r = Renderer(trace_level="compact")
    r.on_event(LLMCallStart(iteration=1, max_iterations=9, model=MODEL, message_count=1, tokens_in=1, user_prompt="hi", mode="bypass"))
    r.on_event(TextDelta(text="Hello."))
    r.on_event(TurnComplete(stop_reason="end_turn", usage=Usage()))
    r.turn_footer(0, Usage(), 0.0, 1.2, llm_calls=1)
    out = strip_ansi(capsys.readouterr().out)
    assert f"{theme.glyph('ok')} Done in 1.2s\n" in out and "tool" not in out


def test_a_failed_turn_says_why_at_the_default_level(capsys) -> None:
    r = Renderer(trace_level="compact")
    r.on_event(LLMCallStart(iteration=1, max_iterations=9, model=MODEL, message_count=1, tokens_in=1, user_prompt="hi", mode="bypass"))
    r.turn_footer(0, Usage(), 0.0, 1.2, llm_calls=1, stop_reason="error", error="HTTP 500 from the provider")
    out = strip_ansi(capsys.readouterr().out)
    assert "Failed" in out and "HTTP 500 from the provider" in out


# ── the running turn: one calm row and at most a tip ──────────────────────

class _Live:
    def __init__(self, now: str, tip: str = "") -> None:
        self.answer = live_status.Rows(now, tip)

    def enabled(self) -> bool:
        return True

    def rows(self, width: int, show_tip: bool = True, now: float | None = None) -> live_status.Rows:
        return self.answer


def _spinner(live: _Live, monkeypatch) -> render.Spinner:
    monkeypatch.setattr(render, "term_height", lambda: 40)
    s = render.Spinner()
    s.live = live
    return s


def test_spinner_is_one_row_with_the_live_text(monkeypatch) -> None:
    s = _spinner(_Live("running Bash: List files · 4s"), monkeypatch)
    text, count = s.frame_text("x")
    assert count == 1
    row = strip_ansi(text)
    assert "running Bash: List files · 4s" in row and "interrupt" in row
    assert "Now:" not in row and "Step" not in row


def test_a_tip_is_the_only_extra_row(monkeypatch) -> None:
    s = _spinner(_Live("waiting", "Switch model with /model"), monkeypatch)
    text, count = s.frame_text("x")
    assert count == 2
    assert strip_ansi(text.split("\n")[-1]).strip().endswith("Tip: Switch model with /model")


def test_live_text_drops_step_and_tokens_at_the_default_level() -> None:
    quiet = live_status.View(step=3, model=MODEL, phase="thinking", phase_t0=0.0, out_chars=4000, detail=False)
    text = live_status.now_candidates(quiet, 2.0)[0]
    assert "tokens" not in text and "thinking" in text
    tool = live_status.ToolView(id="1", name="Bash", t0=0.0, what="List files")
    assert "step" not in live_status.now_candidates(live_status.View(step=3, tools=(tool,), detail=False), 2.0)[0]
    assert "step 3" in live_status.now_candidates(live_status.View(step=3, tools=(tool,)), 2.0)[0]


# ── a restored session looks like the live view ───────────────────────────

def test_restored_session_at_compact_has_no_rail(tmp_path: Path) -> None:
    import json
    path = tmp_path / "s.jsonl"
    records = [
        {"type": "user_message", "data": {"content": "hello"}},
        {"type": "assistant_turn", "data": {"model": MODEL, "text": "Let me look.", "stop_reason": "tool_use", "cost": 0.01,
                                            "usage": {"input": 10, "output": 2},
                                            "tool_uses": [{"id": "t1", "name": "Bash", "input": {"command": "ls /nope"}}]}},
        {"type": "tool_results", "data": [{"tool_use_id": "t1", "content": "Command exited with non-zero code 2.\nls: /nope: No such file", "is_error": True}]},
        {"type": "assistant_turn", "data": {"model": MODEL, "text": "Done now.", "stop_reason": "end_turn", "tool_uses": []}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    out = "\n".join(strip_ansi(r) for r in format_session_history(path, width=100, trace_level="compact"))
    for heavy in ("Turn 1", "Step 1", "ANSWER", "TOOLS", "go to step", "│", "└"):
        assert heavy not in out, heavy
    assert "Let me look." in out and "Done now." in out
    assert "ls: /nope: No such file" in out and f"{theme.glyph('fail')} Bash  ls /nope" in out
    assert "1 tool" in out
    normal = "\n".join(strip_ansi(r) for r in format_session_history(path, width=100, trace_level="normal"))
    assert "TOOLS" in normal and "Step 1" in normal
