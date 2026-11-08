"""Long pastes: a pause inside the paste must not end it, and split pieces merge into one chip."""
import os

import pytest

from axon import platform as plat
from axon.ui.input import EditorState


def _getc_from(chunks):
    """A fake terminal: returns characters, and None once per pause marker (a pause shorter than the idle limit)."""
    stream = list("".join(chunks))

    def getc(timeout):
        return stream.pop(0) if stream else None
    return getc


def test_paste_is_read_up_to_the_end_marker():
    text = "line1\nline2\n" * 200
    getc = _getc_from([text, plat.PASTE_END])
    assert plat._read_bracketed_paste(getc) == plat.normalize_newlines(text)


def test_short_pause_does_not_end_the_paste(monkeypatch):
    calls = {"n": 0}
    data = list("first part\n") + [None] + list("second part\n" + plat.PASTE_END)

    def getc(timeout):
        assert timeout >= 1.0, "idle limit must be generous"
        calls["n"] += 1
        item = data.pop(0) if data else None
        return item if item is not None else (data.pop(0) if data else None)
    out = plat._read_bracketed_paste(getc)
    assert "second part" in out


def test_split_pastes_merge_into_one_chip():
    ed = EditorState()
    big = "x = 1\n" * 40
    ed.insert_paste(big)
    ed.insert_paste(big)
    assert ed.buf.count("[Pasted text #") == 1
    assert ed.expanded() == big + big


def test_separate_pastes_typed_apart_stay_separate():
    ed = EditorState()
    big = "y = 2\n" * 40
    ed.insert_paste(big)
    ed.insert(" and ")
    ed.insert_paste(big)
    assert ed.buf.count("[Pasted text #") == 2


@pytest.mark.skipif(os.name == "nt", reason="feeds the POSIX reader through a pipe; Windows reads the console through msvcrt")
def test_paste_larger_than_one_read_chunk_stays_one_paste(monkeypatch):
    """A 200 KB bracketed paste arrives in several chunks; none of them may become typed keys or a second paste."""
    import os
    import threading

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(plat, "_stdin_fd", lambda: read_fd)
    monkeypatch.setattr(plat, "_stdin_eof", False)
    plat._pending_chars.clear()
    plat._events.clear()
    body = "".join(f"line {i} = value_{i}  # some text to make it longer\n" for i in range(4000))
    assert len(body) > 150_000

    def feed():
        os.write(write_fd, b"\x1b[200~")
        data = body.encode()
        for start in range(0, len(data), 70_000):
            os.write(write_fd, data[start:start + 70_000])
        os.write(write_fd, b"\x1b[201~")

    threading.Thread(target=feed, daemon=True).start()
    first = plat.read_key(timeout=10)
    assert first == plat.PASTE_PREFIX + plat.normalize_newlines(body)
    assert plat.read_key(timeout=0.3) is None        # nothing is left over to be typed as Enter
    os.close(read_fd)
    os.close(write_fd)


@pytest.mark.skipif(os.name == "nt", reason="feeds the POSIX reader through a pipe; Windows reads the console through msvcrt")
def test_crlf_paste_split_between_cr_and_lf_gets_no_extra_blank_line(monkeypatch):
    """A read chunk of a Windows-style paste can end right after the LF of a CRLF. The type-ahead rule that turns a
    trailing LF into Enter must not run inside a paste: it made `\\r\\n` into `\\r\\r` (a blank line in the pasted code)."""
    import os
    import threading
    import time

    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(plat, "_stdin_fd", lambda: read_fd)
    monkeypatch.setattr(plat, "_stdin_eof", False)
    plat._pending_chars.clear()
    plat._events.clear()
    first, middle, last = "alpha\r\nbeta", "\r\ngamma\r\n", "delta"

    def feed():
        os.write(write_fd, b"\x1b[200~" + first.encode())
        time.sleep(0.2)                                  # each chunk is a separate read
        os.write(write_fd, middle.encode())              # ends in LF, holds no escape: looks like type-ahead
        time.sleep(0.2)
        os.write(write_fd, last.encode() + b"\x1b[201~")

    threading.Thread(target=feed, daemon=True).start()
    got = plat.read_key(timeout=10)
    assert got == plat.PASTE_PREFIX + "alpha\nbeta\ngamma\ndelta"
    os.close(read_fd)
    os.close(write_fd)
