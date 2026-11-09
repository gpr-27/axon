"""The transcript keys in a real REPL: Ctrl+O opens it (Claude Code's key), { and } move between prompts, v opens the
editor, and Alt+L is where the detail level went."""
from __future__ import annotations

import stat
from pathlib import Path

from fake_llm import FakeLLMServer
from harness import create_axon_env
from test_e2e_d_helpers import open_repl, posix_only

pytestmark = posix_only
ALT_SCREEN = "\x1b[?1049h"


def _session(tmp_path: Path, extra: dict | None = None):
    llm = FakeLLMServer(model_ids=("gpt-4o",))
    llm.start()
    return llm, open_repl(create_axon_env(tmp_path, llm), extra_env=extra or {})


def test_ctrl_o_opens_the_transcript_and_closes_it(tmp_path) -> None:
    llm, s = _session(tmp_path)
    try:
        llm.enqueue([{"text": "FIRST-ANSWER"}], [{"text": "SECOND-ANSWER"}])
        s.turn("first question")
        s.turn("second question")
        m, shown = len(s._raw), s.mark()
        s.send("\x0f")
        s.expect("prompts", timeout=10, since=shown)          # the viewer's key help names { }; a tip may say it too
        assert ALT_SCREEN in s.raw_since(m)
        m = len(s._raw)
        s.send("\x0f")
        for _ in range(100):
            s._pump(0.05)
            if "\x1b[?1049l" in s.raw_since(m):
                break
        assert "\x1b[?1049l" in s.raw_since(m)
    finally:
        s.close()
        llm.stop()


def test_alt_l_cycles_the_detail_level(tmp_path) -> None:
    llm, s = _session(tmp_path)
    try:
        m = s.mark()
        s.send("\x1bl")
        s.expect("Trace level", timeout=10, since=m)
        assert "detailed" in s.screen[m:]
    finally:
        s.close()
        llm.stop()


def test_ctrl_x_l_cycles_the_detail_level_without_an_alt_key(tmp_path) -> None:
    """On a Mac the Option key sends Alt only when the terminal is set up for it; Ctrl+X then L needs nothing."""
    llm, s = _session(tmp_path)
    try:
        m = s.mark()
        s.send("\x18")
        s.drain(0.2)
        s.send("l")
        s.expect("Trace level", timeout=10, since=m)
        assert "detailed" in s.screen[m:]
    finally:
        s.close()
        llm.stop()


def test_v_opens_the_conversation_in_the_editor_and_returns(tmp_path) -> None:
    seen = tmp_path / "editor-saw.txt"
    editor = tmp_path / "fake-editor"
    editor.write_text(f'#!/bin/sh\ncp "$1" "{seen}"\n', encoding="utf-8")
    editor.chmod(editor.stat().st_mode | stat.S_IEXEC)
    llm, s = _session(tmp_path, {"EDITOR": str(editor), "VISUAL": str(editor)})
    try:
        llm.enqueue([{"text": "ANSWER-FOR-THE-EDITOR"}])
        s.turn("a question")
        s.send("\x0f")
        s.expect("prompts", timeout=10)
        s.drain(0.2)
        m = len(s._raw)
        s.send("v")
        for _ in range(100):
            s._pump(0.05)
            if seen.exists():
                break
        assert seen.exists(), "the editor was not started"
        text = seen.read_text(encoding="utf-8")
        assert "\x1b[" not in text
        assert "a question" in text and "ANSWER-FOR-THE-EDITOR" in text
        s.drain(0.3)
        assert ALT_SCREEN in s.raw_since(m)                    # back in the viewer afterwards
        s.send("q")
        s.expect("Ask anything", timeout=10)
    finally:
        s.close()
        llm.stop()
