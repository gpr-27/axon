"""Regression tests for the defects found in the src/axon/ui review (ASCII mode, narrow widths, untrusted text)."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from axon.platform import str_width
from axon.ui import keybindings, menus, render, statusbar, tool_cards, transcript_view
from axon.ui.detail import BodyLine, wrap_body
from axon.ui.input import EditorState, PromptChrome, PromptEditor, _paste_text, build_frame
from axon.ui.theme import strip_ansi

ESC = "\x1b"


# ─── AXON_ASCII: tool-supplied " · " separators ────────────────────────────

CHOSEN = "chosen by gpt-4o (local) in step 1 · call 1 of 1 · from 22 tools"


def test_chosen_row_uses_the_encoding_aware_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AXON_ASCII", raising=False)
    assert "·" in render.tool_chosen_row(CHOSEN)
    monkeypatch.setenv("AXON_ASCII", "1")
    row = render.tool_chosen_row(CHOSEN)
    assert row.isascii() and "step 1 - call 1 of 1 - from 22 tools" in row


def test_tool_summary_dot_is_ascii_under_axon_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tool:
        def summarize(self, args: dict, output: str, is_error: bool) -> str:
            return "1 line · 9 B"

    registry = SimpleNamespace(get=lambda name: Tool())
    monkeypatch.setenv("AXON_ASCII", "1")
    assert render._tool_summary(registry, "Write", {}, "x", False) == "1 line - 9 B"
    monkeypatch.delenv("AXON_ASCII")
    assert render._tool_summary(registry, "Write", {}, "x", False) == "1 line · 9 B"


def test_transcript_titles_are_ascii_under_axon_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [{"role": "user", "content": "fix the test"},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    monkeypatch.setenv("AXON_ASCII", "1")
    rows = transcript_view.plain_dump(transcript_view.entries_from_messages(messages), width=100)
    assert rows and all(row.isascii() for row in rows), rows
    assert 'Turn 1 - "fix the test"' in rows[0]


def test_spinner_hint_is_ascii_under_axon_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_ASCII", "1")
    monkeypatch.delenv("AXON_NO_TIPS", raising=False)
    monkeypatch.setattr(render, "term_height", lambda: 40)
    monkeypatch.setattr(render.Spinner, "draft_provider", lambda: "")
    spinner = render.Spinner()
    spinner.started_at = time.time()
    assert "queue it" in spinner.hint_line() and spinner.hint_line().isascii()


def test_run_with_spinner_writes_only_ascii_under_axon_ascii(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("AXON_ASCII", "1")
    monkeypatch.setattr(menus, "menus_enabled", lambda: True)
    assert menus.run_with_spinner("loading", lambda: (time.sleep(0.35), 7)[1]) == 7
    assert capsys.readouterr().out.isascii()


# ─── modules ───────────────────────────────────────────────────────────────

def test_every_name_in_tool_cards_all_exists() -> None:
    assert [name for name in tool_cards.__all__ if not hasattr(tool_cards, name)] == []


# ─── keybindings.json with a list / object value ───────────────────────────

@pytest.mark.parametrize("value", [["chat:submit"], {"a": 1}, 5])
def test_keybinding_file_with_a_non_string_action_is_reported_not_raised(tmp_path: Any, value: Any) -> None:
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps({"ctrl+e": value}), encoding="utf-8")
    bindings, errors = keybindings.load_bindings(path)
    assert any("ctrl+e" in e for e in errors)
    assert bindings == keybindings.default_bindings()


# ─── detail screen text ────────────────────────────────────────────────────

def test_detail_body_lines_never_hold_a_newline_or_tab() -> None:
    rows = wrap_body([BodyLine("default: first\nsecond\tthird", 5), BodyLine("a\r\nb"), BodyLine("\n")], 40)
    assert all("\n" not in r and "\t" not in r and "\r" not in r for r in rows)
    assert len(rows) == 5          # two lines, two lines, one blank line


def test_detail_body_drops_terminal_escapes() -> None:
    (row,) = wrap_body([BodyLine(f"x{ESC}[2Jy")], 40)
    assert ESC not in strip_ansi(row)


# ─── /statusbar box fits the window ────────────────────────────────────────

@pytest.mark.parametrize("cols", [50, 80, 120])
def test_status_bar_never_outgrows_the_window(monkeypatch: pytest.MonkeyPatch, cols: int) -> None:
    monkeypatch.setattr(statusbar, "term_width", lambda: cols)
    bar = statusbar.StatusBar.format_bar("claude-sonnet-4-5", "high", "acceptEdits", context_tokens=42_000,
                                         context_capacity=200_000, session_cost=1.2345, tool_count=22,
                                         subagent_count=2, trace_level="normal")
    widths = [str_width(line) for line in bar.strip("\n").split("\n")]
    assert max(widths) <= cols - 1, widths
    assert len(set(widths)) == 1                      # the box stays a box
    assert "claude-sonnet-4-5" in strip_ansi(bar)


# ─── pasted text can not drive the terminal ────────────────────────────────

def test_pasted_escape_sequences_are_removed() -> None:
    assert _paste_text(f"a{ESC}]52;c;QUJD\x07b{ESC}[2Jc\r\nd\te\x9bf") == "a]52;c;QUJDb[2Jc\nd\tef"


def test_prompt_frame_never_contains_a_pasted_escape() -> None:
    from axon import platform as plat
    st = EditorState()
    PromptEditor(st).handle(plat.PASTE_PREFIX + f"hello {ESC}[2J{ESC}]0;title\x07 world")
    lines, _cursor = build_frame(st, PromptChrome())
    frame = "\n".join(lines)
    assert "hello [2J]0;title world" in strip_ansi(frame)
    assert "\x07" not in frame and ESC + "[2J" not in frame and ESC + "]0" not in frame


def test_type_ahead_paste_drops_escapes() -> None:
    from axon import platform as plat
    from axon.ui.in_flight import InFlightInputListener
    listener = InFlightInputListener(SimpleNamespace())
    listener.handle_key(plat.PASTE_PREFIX + f"ab{ESC}[31mcd")
    assert listener.draft() == "ab[31mcd"


# ─── untrusted subagent text in the rosters ────────────────────────────────

def _task(**kw: Any) -> SimpleNamespace:
    base = dict(id="sub-1", index=1, status="running", agent_type="explorer", title="t", activity="",
                start_time=time.time(), elapsed_s=0.0, tokens_consumed=10, steps=1, max_steps=5, tool_calls=1,
                timeline=[], result_text="", live_logs=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_agent_panel_rows_strip_escapes_from_activity() -> None:
    from axon.ui.statusline import agent_panel_rows
    task = _task(activity=f"Read {ESC}]52;c;QUJD\x07{ESC}[2J secrets\nnext")
    agent = SimpleNamespace(subagents=SimpleNamespace(all_tasks=lambda: [task]))
    text = "\n".join(agent_panel_rows(agent, 80))
    assert "\x07" not in text and f"{ESC}]52" not in text and f"{ESC}[2J" not in text
    assert "Read" in text and "secrets next" in text


def test_pinned_footer_peek_rows_strip_escapes() -> None:
    from axon.ui.footer import PinnedFooter
    task = _task(timeline=[{"kind": "result", "text": f"ok {ESC}[2J{ESC}]52;c;QUJD\x07 done", "is_error": False}],
                 status="completed", result_text=f"{ESC}[H report")
    footer = PinnedFooter(SimpleNamespace(), lambda: "")
    text = "\n".join(footer._peek_rows(task, 80))
    assert "\x07" not in text and f"{ESC}]52" not in text and f"{ESC}[2J" not in text and f"{ESC}[H" not in text


def test_subagent_monitor_strips_escapes_from_model_text() -> None:
    from axon import platform as plat
    from axon.ui.subagent_monitor import _one_line, _paste_text as monitor_paste, build_monitor_frame
    assert _one_line(f"a{ESC}[2Jb\nc\x07") == "ab c"
    assert monitor_paste(plat.PASTE_PREFIX + f"x{ESC}[2Jy") == "x[2Jy"
    task = _task(title=f"{ESC}[2Jtitle", activity=f"{ESC}]52;c;QUJD\x07doing")
    frame = "\n".join(build_monitor_frame(0, [task], None, "", 80))
    assert "\x07" not in frame and f"{ESC}]52" not in frame and f"{ESC}[2J" not in frame


# ─── ASCII: echoed prompt banner and paste preview ─────────────────────────

def test_prompt_banner_and_paste_preview_are_ascii_under_axon_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.ui.input import format_user_prompt_banner, paste_preview
    monkeypatch.setenv("AXON_ASCII", "1")
    assert format_user_prompt_banner("x " * 100, max_w=40).isascii()          # long: truncated with the ASCII ellipsis
    assert format_user_prompt_banner("hi", subagent_label="explorer").isascii()
    assert all(row.isascii() for row in paste_preview("\n".join(str(i) for i in range(20))))
    monkeypatch.delenv("AXON_ASCII")
    assert "›" in format_user_prompt_banner("hi")
    assert "… 14 lines hidden …" in paste_preview("\n".join(str(i) for i in range(20)))


# ─── str.isdigit() accepts "²" but int("²") raises ─────────────────────────

def test_superscript_digits_are_not_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.ui import picker, theme
    monkeypatch.setattr("builtins.input", lambda prompt="": "²")
    assert picker._numbered_pick(["a", "b"], "t", 0) is None                  # was ValueError
    monkeypatch.setenv("AXON_MAX_WIDTH", "²")
    assert theme.layout_width() >= 20                                         # was ValueError on every render
    assert render.render_read_box("a.py", "²→x\n   1→y")                       # was ValueError


# ─── streamed reasoning without spaces (CJK, long URLs) stays inside the window ─

def test_streamed_thinking_without_spaces_is_cut_at_the_row_end(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from axon.providers.base import ThinkingDelta
    from axon.ui import theme
    monkeypatch.setattr(theme, "term_width", lambda: 50)
    r = render.Renderer(show_thinking=True, trace_level="detailed")
    r._turn_open = True
    r.on_event(ThinkingDelta(text="日本語のテキスト" * 12 + " then " + "q" * 120 + "\nnext"))
    r._end_thinking()
    rows = [strip_ansi(line) for line in capsys.readouterr().out.split("\n") if line]
    assert max(str_width(row) for row in rows) <= 49, rows
    assert "".join(rows).count("日") == 12 and "".join(rows).count("q") == 120      # nothing lost by the cut


# ─── restored conversation at detailed trace level fits a 50-column window ───

@pytest.mark.parametrize("detail", [2, 3])
def test_restored_conversation_rows_fit_a_narrow_window(monkeypatch: pytest.MonkeyPatch, detail: int) -> None:
    from axon.ui import theme
    monkeypatch.setattr(theme, "term_width", lambda: 50)
    messages = [
        {"role": "user", "content": "fix the test"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "echo hi && ls " + "x" * 60 + " " + "y z " * 20, "description": "x " * 30}},
            {"type": "tool_use", "id": "t3", "name": "Grep", "input": {"pattern": "p" * 70}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "hi\nfoo.py\n" + "z" * 100},
            {"type": "tool_result", "tool_use_id": "t3", "content": "a.py:1:" + "m" * 100}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    rows = render.format_conversation_replay(messages, detail=detail)
    too_wide = [(str_width(r), strip_ansi(r)) for r in rows if str_width(r) > 49]
    assert not too_wide, too_wide


def test_hang_prefixes_count_toward_the_width() -> None:
    rows = render._hang("word " * 30, 20, "", "    ")
    assert max(str_width(r) for r in rows) <= 20
