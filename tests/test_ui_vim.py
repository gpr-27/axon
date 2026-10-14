"""Vim editing mode: scripted key sequences through the real PromptEditor / read_input paths."""
from __future__ import annotations

import contextlib
from collections import deque

import pytest

from axon import platform as plat
from axon.ui import input as ui_input
from axon.ui.input import EditorState, PromptEditor, PromptChrome, build_frame, read_input
from axon.ui.keybindings import KeyResolver, default_bindings
from axon.ui.theme import strip_ansi
from axon.ui.vim import NORMAL, INSERT, VimState, word_back, word_end, word_forward


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    monkeypatch.setattr(ui_input, "match_files", lambda q, cwd=None, limit=50: [])
    ui_input.set_vim_mode(False)
    yield
    ui_input.set_vim_mode(False)


def vim_editor(text: str = "", normal: bool = True, history: list[str] | None = None) -> PromptEditor:
    st = EditorState(history=list(history or []))
    st.vim = VimState()
    if text:
        st.set_text(text)
    ed = PromptEditor(st, KeyResolver(default_bindings()))
    if normal:
        ed.handle("esc")
    return ed


def press(ed: PromptEditor, keys) -> list:
    return [ed.handle(k) for k in keys]


def test_word_motion_helpers():
    s = "foo.bar  baz"
    assert word_forward(s, 0) == 3          # 'foo' -> '.'
    assert word_forward(s, 3) == 4          # '.' -> 'bar'
    assert word_forward(s, 4) == 9          # 'bar' + spaces -> 'baz'
    assert word_end(s, 0) == 2 and word_end(s, 2) == 3 and word_end(s, 4) == 6
    assert word_back(s, 9) == 4 and word_back(s, 4) == 3 and word_back(s, 1) == 0


def test_esc_enters_normal_and_moves_left():
    ed = vim_editor("hello world")
    assert ed.st.vim.mode == NORMAL
    assert ed.st.pos == 10                  # on the 'd'
    assert ed.st.vim.label == "-- NORMAL --"


def test_motions_hjkl_w_b_e_0_dollar_caret():
    ed = vim_editor("  hello big world")
    st = ed.st
    press(ed, ["0"]); assert st.pos == 0
    press(ed, ["^"]); assert st.pos == 2
    press(ed, ["w"]); assert st.pos == 8
    press(ed, ["e"]); assert st.pos == 10
    press(ed, ["b"]); assert st.pos == 8
    press(ed, ["$"]); assert st.pos == len(st.buf) - 1
    press(ed, ["h", "h"]); assert st.pos == len(st.buf) - 3
    press(ed, ["l"]); assert st.pos == len(st.buf) - 2
    press(ed, ["l", "l", "l"]); assert st.pos == len(st.buf) - 1   # l never passes the last char
    press(ed, ["0", "3", "l"]); assert st.pos == 3
    press(ed, ["0", "2", "w"]); assert st.pos == 8


def test_j_k_move_between_lines_and_walk_history_at_edges():
    ed = vim_editor("ab\ncd", history=["older prompt"])
    st = ed.st
    assert st.pos == 4                      # 'd'
    press(ed, ["k"]); assert st.pos == 1
    press(ed, ["j"]); assert st.pos == 4
    press(ed, ["k", "k"])                   # top line: older history
    assert st.buf == "older prompt"


def test_x_counts_dw_d2w_2dw_D():
    ed = vim_editor("one two three four")
    press(ed, ["0", "x"]); assert ed.st.buf == "ne two three four"
    press(ed, ["3", "x"]); assert ed.st.buf == "two three four"
    press(ed, ["d", "w"]); assert ed.st.buf == "three four"
    ed2 = vim_editor("a b c d")
    press(ed2, ["0", "d", "2", "w"]); assert ed2.st.buf == "c d"
    ed3 = vim_editor("a b c d")
    press(ed3, ["0", "2", "d", "w"]); assert ed3.st.buf == "c d"
    ed4 = vim_editor("keep this; drop that")
    press(ed4, ["0", "w", "w", "w", "D"])
    assert ed4.st.buf == "keep this; "
    assert ed4.st.vim.register == ("drop that", False)


def test_dw_does_not_join_lines():
    ed = vim_editor("first\nsecond")
    press(ed, ["k", "0", "d", "w"])
    assert ed.st.buf == "\nsecond"


def test_cw_changes_word_and_single_undo():
    ed = vim_editor("foo bar")
    press(ed, ["0", "c", "w"])
    assert ed.st.vim.mode == INSERT and ed.st.buf == " bar"
    press(ed, list("xyz") + ["esc"])
    assert ed.st.buf == "xyz bar"
    press(ed, ["u"])
    assert ed.st.buf == "foo bar"           # one undo reverts the whole change


def test_C_and_cc():
    ed = vim_editor("alpha beta")
    press(ed, ["0", "w", "C"] + list("gamma") + ["esc"])
    assert ed.st.buf == "alpha gamma"
    ed2 = vim_editor("l1\nl2\nl3")
    press(ed2, ["k", "c", "c"] + list("new") + ["esc"])
    assert ed2.st.buf == "l1\nnew\nl3"


def test_dd_yy_p_P_linewise():
    ed = vim_editor("a\nb\nc")
    press(ed, ["k", "d", "d"])
    assert ed.st.buf == "a\nc"
    assert ed.st.buf[ed.st.pos] == "c"
    press(ed, ["p"])
    assert ed.st.buf == "a\nc\nb"
    press(ed, ["y", "y", "P"])
    assert ed.st.buf == "a\nc\nb\nb"
    ed2 = vim_editor("x\ny\nz")
    press(ed2, ["2", "d", "d"])             # last line + count past the end
    assert ed2.st.buf == "x\ny"
    ed3 = vim_editor("1\n2\n3")
    press(ed3, ["k", "k", "2", "d", "d"])
    assert ed3.st.buf == "3"


def test_charwise_yank_and_put():
    ed = vim_editor("ab cd")
    press(ed, ["0", "y", "w", "$", "p"])
    assert ed.st.buf == "ab cdab "


def test_insert_commands_i_a_I_A_o_O():
    ed = vim_editor("  mid")
    press(ed, ["I", "["]); assert ed.st.buf == "  [mid"
    press(ed, ["esc", "A", "]"]); assert ed.st.buf == "  [mid]"
    press(ed, ["esc", "0", "a", "-"]); assert ed.st.buf == " - [mid]"
    press(ed, ["esc", "i", "+"]); assert ed.st.buf == " +- [mid]"
    press(ed, ["esc", "o"] + list("below")); assert ed.st.buf == " +- [mid]\nbelow"
    press(ed, ["esc", "O"] + list("above")); assert ed.st.buf == " +- [mid]\nabove\nbelow"
    press(ed, ["esc", "u"]); assert ed.st.buf == " +- [mid]\nbelow"


def test_r_s_X_and_counts():
    ed = vim_editor("abcd")
    press(ed, ["0", "r", "Z"]); assert ed.st.buf == "Zbcd"
    press(ed, ["$", "X"]); assert ed.st.buf == "Zbd"
    press(ed, ["0", "s", "Q", "esc"]); assert ed.st.buf == "Qbd"


def test_esc_clears_pending_operator_and_unknown_keys_do_not_insert():
    ed = vim_editor("text")
    press(ed, ["d", "esc"])
    assert ed.st.vim.op is None and ed.st.buf == "text"
    press(ed, ["Z", "!", "?"])
    assert ed.st.buf == "text"


def test_enter_submits_from_normal_mode_and_ctrl_keys_still_work():
    ed = vim_editor("go")
    assert ed.handle("enter") == ("submit", None)
    ed2 = vim_editor("abc def")
    press(ed2, ["ctrl-a"])                  # readline keys pass through in NORMAL mode
    assert ed2.st.pos == 0
    press(ed2, ["ctrl-k"])
    assert ed2.st.buf == ""


def test_paste_in_normal_mode_inserts_text():
    ed = vim_editor("ab")
    ed.handle(plat.PASTE_PREFIX + "XY")
    assert "XY" in ed.st.buf and ed.st.vim.mode == INSERT


def test_indicator_in_frame(monkeypatch):
    monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    ed = vim_editor("hi", normal=False)
    lines, _ = build_frame(ed.st, PromptChrome())
    assert any("-- INSERT --" in strip_ansi(l) for l in lines)
    ed.handle("esc")
    ed.handle("d")
    lines, _ = build_frame(ed.st, PromptChrome())
    assert any("-- NORMAL -- d" in strip_ansi(l) for l in lines)


@pytest.fixture
def keys(monkeypatch):
    def install(seq):
        q = deque(seq)

        def fake_read_key(timeout=None, report_resize=True):
            if not q:
                raise AssertionError("ran out of scripted keys")
            return q.popleft()
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    return install


def test_read_input_end_to_end_with_vim(keys):
    ui_input.set_vim_mode(True)
    keys(list("helo") + ["esc", "i", "l", "esc", "0", "x", "i", "H", "enter"])
    assert read_input()[0] == "Hello"


def test_vim_mode_override_kwarg_and_toggle_api(keys):
    keys(list("ab") + ["esc", "x", "enter"])
    assert read_input(vim_mode=True)[0] == "a"
    assert ui_input.set_vim_mode(True) is True
    assert ui_input.vim_mode_enabled() and ui_input.vim_mode_label() == "insert"
    assert ui_input.set_vim_mode(False) is False and ui_input.vim_mode_label() == ""


def test_vim_toggle_action_mid_prompt(keys, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "keybindings.json").write_text('{"bindings": {"app:vimToggle": "alt-i"}}', encoding="utf-8")
    keys(list("xy") + ["alt-i", "esc", "x", "enter"])
    assert read_input()[0] == "x"
    assert ui_input.vim_mode_enabled()
