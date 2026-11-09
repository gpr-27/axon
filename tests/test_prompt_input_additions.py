"""Prompt behaviour people notice: word keys, wrapped rows, stash, cleared drafts, command menu, emoji, spelling, vim."""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from axon import platform as plat
from axon.ui import input as ui_input
from axon.ui import keybindings as kb
from axon.ui.input import EditorState, PromptChrome, PromptEditor, build_frame
from axon.ui.keybindings import KeyResolver, default_bindings
from axon.ui.vim import NORMAL, VimState


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ui_input, "_STASH", None)
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "_CLEARED_DRAFTS", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    monkeypatch.setattr(plat, "term_size", lambda: (40, 24))


def editor(text: str = "", pos: int | None = None) -> tuple[EditorState, PromptEditor]:
    st = EditorState()
    st.set_text(text, pos)
    return st, PromptEditor(st, KeyResolver(default_bindings()))


def test_word_keys_stop_at_punctuation_and_ctrl_w_at_whitespace() -> None:
    st, ed = editor("run src/utils/foo.ts")
    ed.handle("alt-b")
    assert st.buf[st.pos:] == "ts"
    ed.handle("alt-b")
    assert st.buf[st.pos:] == "foo.ts"
    st, ed = editor("run src/utils/foo.ts")
    ed.handle("ctrl-w")
    assert st.buf == "run "
    st, ed = editor("run src/utils/foo.ts")
    ed.handle("alt-backspace")
    assert st.buf == "run src/utils/foo."


def test_ctrl_u_repeats_across_lines() -> None:
    st, ed = editor("one\ntwo")
    ed.handle("ctrl-u")
    assert st.buf == "one\n"
    ed.handle("ctrl-u")
    assert st.buf == "one"


def test_up_moves_through_wrapped_rows_before_history() -> None:
    st, ed = editor("x" * 90)
    st.history = ["x" * 90 + " older"]            # history recall matches what was typed so far
    build_frame(st, PromptChrome())
    assert st.text_width and 90 > st.text_width
    ed.handle("up")
    assert st.buf == "x" * 90 and st.pos < 90
    for _ in range(5):
        ed.handle("up")
    assert st.buf == "x" * 90 + " older"


def test_stash_outlives_the_prompt_and_keeps_cursor_and_chips() -> None:
    st, ed = editor("[Pasted text #1 +3 lines] hello", 5)
    st.pastes = {1: "a\nb\nc"}
    ed.handle("ctrl-s")
    assert st.buf == ""
    st2, ed2 = editor()                     # the next prompt
    ed2.handle("ctrl-s")
    assert st2.buf.endswith("hello") and st2.pos == 5 and st2.pastes == {1: "a\nb\nc"}


def test_cleared_draft_stays_recallable_in_later_prompts_but_is_capped() -> None:
    st, ed = editor("my long draft")
    ed.handle("ctrl-c")
    assert st.buf == ""
    assert "my long draft" in ui_input._SESSION_HISTORY
    for i in range(12):
        ui_input.remember_cleared_draft(f"draft {i}")
    assert len(ui_input._CLEARED_DRAFTS) == ui_input.MAX_CLEARED_DRAFTS
    assert "my long draft" not in ui_input._SESSION_HISTORY


def test_panel_shortcuts_keep_the_draft() -> None:
    st, ed = editor("half typed")
    assert ed.handle("ctrl-o") == ("keep", "/transcript")
    assert st.buf == "half typed"


def test_menu_highlights_names_not_typos() -> None:
    st, _ed = editor("/adddir")
    pop = st.popup()
    assert pop is not None and pop.items[0][0] == "/add-dir" and pop.highlighted
    st, ed = editor("/comapct")
    pop = st.popup()
    assert pop is not None and not pop.highlighted and "/compact" in [c[0] for c in pop.items]
    assert ed.handle("enter") == ("submit", None)
    assert st.buf == "/comapct"                # goes as typed, so it reports Unknown command


def test_no_match_message() -> None:
    st, _ed = editor("/zzzzqqq")
    assert st.popup() is None
    assert any('No commands match "/zzzzqqq"' in row for row in build_frame(st, PromptChrome())[0])


def test_mid_prompt_enter_sends_and_tab_completes() -> None:
    st, ed = editor("run the tests, then /compa")
    assert st.popup() is None and st.mid_prompt_ghost()[0] == "ct"
    assert ed.handle("enter") == ("submit", None)
    st, ed = editor("run the tests, then /compa")
    ed.handle("tab")                                # several commands start with these letters: Tab lists them
    pop = st.popup()
    assert pop is not None and pop.items[0][0] == "/compact"
    ed.handle("tab")
    assert st.buf.startswith("run the tests, then /compact")
    st, ed = editor("run the tests, then /compac")
    ed.handle("tab")                                # only one match: Tab completes it
    assert st.buf.startswith("run the tests, then /compact")
    st, _ed = editor("see /tmp/notes.md")
    assert st.popup() is None and st.mid_prompt_ghost() == ("", 0)


def test_emoji_shortcodes() -> None:
    st, ed = editor()
    for ch in ":heart:":
        ed.handle(ch)
    assert st.buf == "❤️"
    st, ed = editor("see http://x")
    ed.handle(":")
    assert st.buf == "see http://x:"
    st, ed = editor(":hea")
    pop = st.popup()
    assert pop is not None and pop.kind == "emoji"
    ed.handle("tab")
    assert st.buf == "❤️"
    st, _ed = editor(":hea")
    st.emoji_enabled = False
    assert st.popup() is None


def test_shell_mode_lists_paths(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    st, ed = editor("! cat ./src/a")
    st.cwd = tmp_path
    pop = st.popup()
    assert pop is not None and pop.items == ["./src/app.py"]
    ed.handle("tab")
    assert st.buf == "! cat ./src/app.py "


def test_vim_motions_operators_and_visual() -> None:
    def run(text: str, keys: str, pos: int = 0) -> str:
        st = EditorState()
        st.vim = VimState(NORMAL)
        st.set_text(text, pos)
        for k in keys:
            assert st.vim.handle(st, k)
        return st.buf
    assert run("foo(bar) end", "d%") == " end"
    assert run('say "hi" ok', 'di"') == 'say "" ok'
    assert run("hello world", "fwx") == "hello orld"
    assert run("one two three", "dw.") == "three"
    assert run("l1\nl2\nl3", "dG", 3) == "l1"
    assert run("l1\nl2", "J") == "l1 l2"
    assert run("hello", "vlld") == "lo"
    assert run("l1\nl2\nl3", "Vjd") == "l3"
    assert run("hello", "vl~") == "HEllo"


def test_keybinding_contexts_commands_and_validation(tmp_path, monkeypatch) -> None:
    path = tmp_path / "keybindings.json"
    monkeypatch.setattr(kb, "keybindings_path", lambda: path)
    path.write_text(json.dumps({"bindings": [
        {"context": "Select", "bindings": {"ctrl+q": "select:cancel", "j": None}},
        {"context": "Chat", "bindings": {"meta+g": "command:compact", "ctrl+c": "chat:submit"}},
        {"context": "Nope", "bindings": {"x": "chat:submit"}},
        {"context": "Agents", "bindings": {"ctrl+y": "agents:find"}},
    ]}), encoding="utf-8")
    os.utime(path, (time.time(), time.time() + 7))
    assert kb.translate("Select", "ctrl-q") == "esc"
    assert kb.translate("MessageSelector", "ctrl-q") == "esc"
    assert kb.translate("Select", "j") == ""
    assert kb.translate("Select", "x") == "x"
    assert "alt-g" in kb.effective_keybindings()["command:compact"]
    errors = " ".join(kb.keybinding_errors())
    assert "cannot be rebound" in errors and "unknown context" in errors and "agents:find" not in errors
    assert kb.KeyResolver({"command:compact": ["alt-g"]}).feed("alt-g")[0] == "command:compact"
    kb._cache.update(map=None, mtime=None, path=None, errors=[], contexts={})


def test_chord_expires() -> None:
    r = KeyResolver({"chat:externalEditor": ["ctrl-x ctrl-e"]})
    assert r.feed("ctrl-x") == (None, [])
    assert r.expire(time.monotonic() + 4) and not r.pending


@pytest.mark.skipif(os.name == "nt", reason="uses a shell script as the checker")
def test_spellcheck_underlines_with_the_users_checker(tmp_path, monkeypatch) -> None:
    from axon.ui import spellcheck as sc
    script = tmp_path / "aspell"
    script.write_text('#!/bin/sh\necho "@(#) fake"\nwhile read -r w; do w=${w#^}; '
                      'case "$w" in teh) echo "& teh 1 0: the";; *) echo "*";; esac; echo; done\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert sc.parse_config({"enabled": False}) is None
    assert [w for _s, _e, w in sc.candidate_words("see teh src/foo --flag camelCase wor")] == ["see", "teh"]
    checker = sc.SpellChecker(sc.parse_config({"enabled": True, "color": "#ff0000"}))
    checker.misspelled("teh")
    checker.misspelled("see")
    deadline = time.monotonic() + 5
    while len(checker.known) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert checker.known == {"teh": True, "see": False}
    assert "\033[4;38;2;255;0;0mteh" in sc.underline_row("see teh now", "see teh now", checker)
