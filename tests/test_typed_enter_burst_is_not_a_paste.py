"""Enter, text, Enter arriving in one read is type-ahead, not a paste: the first Enter must stay an Enter.
A real paste (a line break between text, or a long chunk) is still one."""
import sys

import pytest

from axon import platform as plat


@pytest.mark.parametrize("chunk", ["\rhello\r", "\r\rhello\r", "\nhello\n", "\r/exit\r", "\r\nhello\r\n"])
def test_enter_text_enter_is_typed(chunk):
    assert not plat._looks_like_paste(chunk)


@pytest.mark.parametrize("chunk", ["line one\rline two\r", "line one\nline two\n", "\rline one\rline two\r", "a\r\rb", "x" * 300 + "\r",
                                   "\r" + "x" * 300 + "\r"])
def test_pastes_are_still_pastes(chunk):
    assert plat._looks_like_paste(chunk)


def test_the_windows_console_burst_is_typed_too():
    """Windows reads key records, not bytes: Enter, text, Enter in one read must stay keys, a real paste a paste."""
    typed = ["enter", "h", "e", "l", "l", "o", "enter"]
    assert plat.coalesce_paste(typed) == typed
    assert plat.coalesce_paste(["enter", "enter", "h", "i", "enter"]) == ["enter", "enter", "h", "i", "enter"]
    assert plat.coalesce_paste(["a", "b", "enter", "c", "d", "enter"]) == [plat.PASTE_PREFIX + "ab\ncd\n"]
    assert plat.coalesce_paste(["enter"] + ["x"] * 300 + ["enter"])[0].startswith(plat.PASTE_PREFIX)


@pytest.mark.skipif(sys.platform == "win32", reason="reads a pipe with select(); the Windows console path is covered above")
def test_the_burst_reaches_the_editor_as_two_enters_and_a_line(monkeypatch):
    """Through the real reader: the chunk is queued as keys, not as one paste event."""
    monkeypatch.setattr(plat, "_events", __import__("collections").deque())
    monkeypatch.setattr(plat, "_pending_chars", __import__("collections").deque())
    monkeypatch.setattr(plat, "_inside_bracketed_paste", False)
    import os
    r, w = os.pipe()
    monkeypatch.setattr(plat, "_stdin_fd", lambda: r)
    os.write(w, b"\rhello\r")
    try:
        assert plat._posix_fill(0.5)
        assert not plat._events and "".join(plat._pending_chars) == "\rhello\r"
    finally:
        os.close(r)
        os.close(w)
