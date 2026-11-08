"""Paste chips: size notice, expand-in-place, whole-chip delete, per-chip spill at submission."""
from __future__ import annotations

from pathlib import Path

from axon import platform as plat
from axon.agent import large_input
from axon.ui import input as inp
from axon.ui.input import EditorState, PromptEditor


def _lines(n: int, tag: str = "row") -> str:
    return "\n".join(f"{tag} {i} some ordinary content" for i in range(n))


def test_chip_creation_shows_size_and_tokens():
    ed = PromptEditor(EditorState())
    ed.handle(plat.PASTE_PREFIX + _lines(300))
    assert "[Pasted text #1 +300 lines]" in ed.st.buf
    assert "tokens" in ed.st.notice and "KB" in ed.st.notice


def test_huge_chip_notice_says_only_start_and_end_are_sent():
    st = EditorState()
    st.window_tokens = 200_000
    st.insert_paste(_lines(400_000))
    assert "only the start and end will be sent" in st.notice
    assert "M tokens" in st.notice


def test_small_chip_notice_has_no_truncation_warning():
    st = EditorState()
    st.insert_paste(_lines(30))
    assert "tokens" in st.notice and "only the start" not in st.notice


def test_expand_key_replaces_chip_with_full_text():
    ed = PromptEditor(EditorState())
    ed.handle(plat.PASTE_PREFIX + _lines(40))
    assert ed.handle("alt-e") is None
    assert "[Pasted text" not in ed.st.buf and ed.st.buf == _lines(40)


def test_expand_key_on_large_chip_opens_editor_and_keeps_chip():
    ed = PromptEditor(EditorState())
    ed.handle(plat.PASTE_PREFIX + _lines(5000))
    assert ed.handle("alt-e") == ("ui", "pasteedit")
    assert "[Pasted text #1" in ed.st.buf


def test_expand_key_without_chip_says_so():
    ed = PromptEditor(EditorState())
    ed.st.insert("hello")
    ed.handle("alt-e")
    assert ed.st.buf == "hello" and "no pasted text" in ed.st.notice


def test_edited_chip_text_updates_the_paste(monkeypatch):
    st = EditorState()
    st.insert_paste(_lines(5000))
    monkeypatch.setattr(inp, "_edit_externally", lambda text: "edited\nbody")
    inp._edit_chip(st, 1)
    assert st.pastes[1] == "edited\nbody" and "+2 lines" in st.buf


def test_backspace_next_to_chip_removes_whole_chip():
    ed = PromptEditor(EditorState())
    ed.st.insert("look: ")
    ed.handle(plat.PASTE_PREFIX + _lines(40))
    ed.handle("backspace")
    assert ed.st.buf == "look: "


def test_slash_command_at_start_of_collapsed_paste_still_dispatches():
    st = EditorState()
    st.insert_paste("/model gpt-4o\n" + _lines(40))
    assert st.buf.startswith("[Pasted text")
    assert st.expanded().startswith("/model gpt-4o\n")


def test_each_oversized_chip_is_spilled_and_typed_text_stays_whole(workspace: Path):
    st = EditorState()
    st.insert_paste(_lines(60_000, "alpha"))
    st.insert(" MIDDLE-INSTRUCTION: compare the two logs carefully ")
    st.insert_paste(_lines(60_000, "beta") + "\n")  # a fresh chip: typed text separates them
    assert st.buf.count("[Pasted text #") == 2
    text = inp.expand_for_send(st.buf, st.pastes, limit_tokens=20_000)
    assert "MIDDLE-INSTRUCTION: compare the two logs carefully" in text
    assert text.count("saved at") == 2
    assert large_input.estimate_tokens(text) < 20_000


def test_old_fit_would_lose_the_middle_text():
    big = _lines(60_000, "alpha") + " MIDDLE-INSTRUCTION " + _lines(60_000, "beta")
    fitted, _ = large_input.fit(big, 20_000)
    assert "MIDDLE-INSTRUCTION" not in fitted


def test_small_chips_are_expanded_whole():
    st = EditorState()
    st.insert_paste(_lines(40))
    assert inp.expand_for_send(st.buf, st.pastes, limit_tokens=20_000) == _lines(40)
