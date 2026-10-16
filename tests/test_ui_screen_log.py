"""Screen log: keeps what stayed on screen, drops transient redraws and styling."""
from __future__ import annotations

import io

from axon.ui.screen_log import ScreenLog


class _Term(io.StringIO):
    def isatty(self) -> bool:
        return True


def _log(tmp_path, *writes, pause_between=None):
    term = _Term()
    log = ScreenLog(term, tmp_path / "screen" / "s.log")
    for w in writes:
        log.write(w)
    log.close()
    return term.getvalue(), (tmp_path / "screen" / "s.log").read_text(encoding="utf-8").splitlines()


def test_text_passes_through_and_is_recorded_without_colour(tmp_path):
    shown, rows = _log(tmp_path, "\x1b[1m┌ Turn 1\x1b[0m\n", "│ ● Bash  ls\n", "\x1b]8;;file:///x\x1b\\/output 1\x1b]8;;\x1b\\\n")
    assert "\x1b[1m" in shown                     # the terminal still gets the styled text
    assert rows == ["┌ Turn 1", "│ ● Bash  ls", "/output 1"]


def test_spinner_frames_and_the_tip_row_are_not_recorded(tmp_path):
    _, rows = _log(tmp_path,
                   "│ ● Bash  sleep 5\n",
                   "\r\x1b[2K⠋ Running Bash… 0.1s\n\x1b[2K ⎿ Tip: queue a message",     # spinner + tip row
                   "\r\x1b[2K\x1b[A\r\x1b[2K",                                          # erased again
                   "\r\x1b[2K⠙ Running Bash… 0.2s\n\x1b[2K ⎿ Tip: queue a message",
                   "\r\x1b[2K\x1b[A\r\x1b[2K",
                   "│   ⎿  done · 5.01s\n")
    assert rows == ["│ ● Bash  sleep 5", "│   ⎿  done · 5.01s"]


def test_carriage_return_overwrites_like_a_terminal(tmp_path):
    _, rows = _log(tmp_path, "progress 10%", "\rprogress 100%\n")
    assert rows == ["progress 100%"]


def test_alternate_screen_views_are_skipped_and_notes_and_marks_are_kept(tmp_path):
    term = _Term()
    log = ScreenLog(term, tmp_path / "a.log")
    log.mark("session s1")
    log.note("› fix the test")
    log.write("\x1b[?1049hFULLSCREEN VIEWER\n\x1b[?1049l")
    log.pause()
    log.write("prompt editor redraw\n")
    log.resume()
    log.write("answer\n")
    log.close()
    rows = (tmp_path / "a.log").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("──── session s1 ────")
    assert rows[1:] == ["› fix the test", "answer"]
