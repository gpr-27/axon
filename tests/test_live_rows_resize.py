"""A window that gets narrower while a reply streams must not leave live rows behind (VS Code / xterm.js re-wraps
every line and moves the cursor down by the rows its pinned footer gained; the live rows erase themselves by
counting rows up from the cursor)."""
from __future__ import annotations

from axon.ui import footer as footer_mod
from axon.ui import render as render_mod


class _Footer:
    """Just what wrap_drift reads from a PinnedFooter."""
    def __init__(self, widths, cols):
        self._height = len(widths)
        self._widths = widths
        self._size = (cols, 36)
        self._pending_drift = 0


def _spinner_with_visible_frame(cols_drawn: int, widths: list[int]):
    sp = render_mod.Spinner()
    sp._real = object()
    sp._visible = True
    sp._rows = len(widths)
    sp._row_widths = widths
    sp._drawn_cols = cols_drawn
    return sp


def test_footer_drift_matches_how_xterm_js_rewraps_its_rows(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(footer_mod, "_ACTIVE", _Footer([153, 70, 153, 130], 154))
    assert footer_mod.wrap_drift(120) == 3          # 153 -> 2 rows, 70 -> 1, 153 -> 2, 130 -> 2
    assert footer_mod.wrap_drift(60) == 7
    assert footer_mod.wrap_drift(154) == 0           # not narrower: nothing re-wraps


def test_other_terminals_get_no_drift_correction(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setattr(footer_mod, "_ACTIVE", _Footer([153, 153], 154))
    assert footer_mod.wrap_drift(100) == 0


def test_spinner_erase_after_narrowing_goes_back_up_first_and_counts_wrapped_rows(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(footer_mod, "_ACTIVE", _Footer([153, 70, 153, 130], 154))
    monkeypatch.setattr(render_mod, "term_width", lambda: 120)
    sp = _spinner_with_visible_frame(154, [150, 60, 66])       # a long preview row wraps to two at 120 columns
    seq = sp._erase_str_locked()
    assert seq.startswith("\x1b[3A\r\x1b[2K")                    # cursor drift undone first
    assert seq.count("\x1b[A") == 3                              # 4 rows after re-wrapping: 3 moves up between 4 clears
    assert seq.count("\x1b[2K") == 4


def test_spinner_erase_unchanged_when_the_window_did_not_shrink(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(footer_mod, "_ACTIVE", _Footer([153, 153], 154))
    monkeypatch.setattr(render_mod, "term_width", lambda: 154)
    sp = _spinner_with_visible_frame(154, [60, 60, 60])
    assert sp._erase_str_locked() == "\r\x1b[2K" + "\x1b[A\r\x1b[2K" * 2
