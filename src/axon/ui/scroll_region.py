"""
Fixed Bottom Bar — Inline coordination engine for a persistent input bar.

Instead of using ANSI scroll regions (which conflict with streaming renderers),
this uses an inline coordination approach: the bar renders at the current cursor
position and gets temporarily cleared before each output write, then redrawn
after — keeping it always at the visual bottom without fragmentation.

This is the same approach used by Claude Code and similar CLI tools.
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, RST, SLATE, WHITE, strip_ansi, term_width


# Global reference to the active bar for renderer coordination
_active_bar: FixedBottomBar | None = None
_bar_lock = threading.RLock()


def get_active_bar() -> FixedBottomBar | None:
    """Return the currently active FixedBottomBar, if any."""
    return _active_bar


class FixedBottomBar:
    """
    Inline input bar that stays at the visual bottom of terminal output.

    Works by coordinating with the renderer: before any output write, the bar
    is cleared (cursor moved up, line erased); after the write, the bar is
    redrawn at the new cursor position.

    Usage::

        with FixedBottomBar() as bar:
            bar.set_content("📥 Queue: 3 pending", "❯ typing here...")
            # ... agent output goes to stdout
            # The bar auto-clears and redraws around writes via write_output()
    """

    def __init__(self, queue_summary: str = "") -> None:
        self._queue_summary = queue_summary
        self._lock = threading.RLock()
        self._active = False
        self._status_text = ""
        self._prompt_text = ""
        self._bar_visible = False  # Whether bar lines are currently rendered
        self._bar_lines = 0       # Number of lines the bar occupies

    def __enter__(self) -> FixedBottomBar:
        global _active_bar
        self._active = True
        with _bar_lock:
            _active_bar = self
        self._draw()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        global _active_bar
        self._clear()
        self._active = False
        with _bar_lock:
            if _active_bar is self:
                _active_bar = None

    @property
    def is_active(self) -> bool:
        return self._active

    def set_content(self, status_text: str = "", prompt_text: str = "") -> None:
        """Update bar content and redraw."""
        with self._lock:
            self._status_text = status_text
            self._prompt_text = prompt_text
            if self._active:
                self._clear()
                self._draw()

    def update_queue_summary(self, summary: str) -> None:
        """Update the queue badge."""
        self._queue_summary = summary

    def temporarily_clear(self) -> None:
        """Clear the bar before renderer output. Called by write_output()."""
        with self._lock:
            self._clear()

    def restore(self) -> None:
        """Redraw the bar after renderer output. Called by write_output()."""
        with self._lock:
            if self._active:
                self._draw()

    def _clear(self) -> None:
        """Erase bar lines from terminal and move cursor back up."""
        if not self._bar_visible or self._bar_lines == 0:
            return
        # Move up bar_lines, clear each line
        for _ in range(self._bar_lines):
            sys.stdout.write("\033[A\r\033[2K")
        sys.stdout.flush()
        self._bar_visible = False

    def _draw(self) -> None:
        """Draw bar at the current cursor position."""
        if not self._active:
            return

        width = max(40, term_width() - 4)
        lines: list[str] = []

        # Status line with separator and queue badge
        if self._queue_summary:
            badge = f"  {DARK_SLATE}{'─' * 3}{RST} {CYAN}{self._queue_summary}{RST} {DARK_SLATE}{'─' * max(4, width - len(strip_ansi(self._queue_summary)) - 8)}{RST}"
            lines.append(badge)
        else:
            lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")

        # Prompt line
        if self._prompt_text:
            lines.append(self._prompt_text)
        else:
            lines.append(f"  {BOLD}{CYAN}❯{RST} {DARK_SLATE}Type to queue follow-ups, /q to manage, /btw for side questions...{RST}")

        self._bar_lines = len(lines)
        output = "\n".join(lines)
        sys.stdout.write(f"\n{output}")
        sys.stdout.flush()
        self._bar_visible = True


def write_output(text: str, flush: bool = True) -> None:
    """
    Write text to stdout, coordinating with the active FixedBottomBar.

    If a bar is active, it's temporarily cleared before the write and
    redrawn after, so the bar always stays at the visual bottom.
    """
    bar = get_active_bar()
    if bar is not None and bar.is_active:
        bar.temporarily_clear()
        sys.stdout.write(text)
        if flush:
            sys.stdout.flush()
        bar.restore()
    else:
        sys.stdout.write(text)
        if flush:
            sys.stdout.flush()
