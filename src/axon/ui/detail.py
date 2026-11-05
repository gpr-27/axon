"""
A detail screen: a block of text (scrollable when it is long) above a short list of actions.

Used where one thing is shown in full and the user then picks what to do with it: a tool with all of
its parameters and "Always allow / Ask each time / Deny / ...". Keys: Up/Down choose an action,
PgUp/PgDn (or Ctrl+U / Ctrl+D) scroll the text, Enter runs the action, Esc goes back.
"""
from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Sequence

from axon import platform as plat
from axon.ui import picker
from axon.ui.picker import LiveFrame, POINTER, SelectItem
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, RST, SLATE, WHITE, sanitize

FOOTER = "↑↓ choose · PgUp/PgDn scroll · Enter select · Esc back"
FOOTER_NARROW = "↑↓ choose · PgUp/Dn scroll · Enter · Esc back"


@dataclass
class BodyLine:
    """One paragraph of the text. ``indent`` is the left margin; ``style`` an ANSI prefix."""
    text: str = ""
    indent: int = 0
    style: str = ""


def wrap_body(lines: Sequence[BodyLine], width: int) -> list[str]:
    """The paragraphs as display lines no wider than ``width`` (blank paragraphs stay blank)."""
    out: list[str] = []
    for line in lines:
        room = max(10, width - line.indent)
        # One display row per piece: a newline or tab left inside a piece would add terminal rows the
        # frame does not count (a tool default or enum value can hold several lines).
        for text in (sanitize(line.text).expandtabs(4).splitlines() or [""]) if line.text else [""]:
            pieces = textwrap.wrap(text, room, replace_whitespace=False, drop_whitespace=True) if text else [""]
            for piece in pieces or [""]:
                out.append(f"{' ' * line.indent}{line.style}{piece}{RST}" if piece else "")
    return out


class DetailScreen:
    def __init__(self, items: Sequence[SelectItem], title: str, subtitle: str, body: Sequence[BodyLine],
                 index: int = 0) -> None:
        self.items = list(items)
        self.title = title
        self.subtitle = subtitle
        self.body = list(body)
        self.index = max(0, min(index, len(self.items) - 1)) if self.items else 0
        self.offset = 0
        self._live = LiveFrame()
        self._result: SelectItem | None = None
        self._done = False

    # ── layout ──
    def _sizes(self) -> tuple[int, int]:
        cols, rows = plat.term_size()
        width = max(20, cols - 4)
        # title + 2 separators + footer + the action rows + a little air
        height = max(3, rows - len(self.items) - 7)
        return width, height

    def _frame(self) -> list[str]:
        width, height = self._sizes()
        text = wrap_body(self.body, width - 2)
        height = min(height, len(text)) if text else 0
        self.offset = max(0, min(self.offset, max(0, len(text) - height)))
        head = f"  {GOLD}◆{RST} {BOLD}{WHITE}{self.title}{RST}"
        if self.subtitle:
            head += f"  {SLATE}{self.subtitle}{RST}"
        lines = [head, f"  {DARK_SLATE}{'─' * width}{RST}"]
        if text:
            above, below = self.offset, len(text) - self.offset - height
            if above:
                lines.append(f"  {DARK_SLATE}↑ {above} more line{'s' if above != 1 else ''} (PgUp){RST}")
            lines += [f"  {line}" for line in text[self.offset:self.offset + height]]
            if below:
                lines.append(f"  {DARK_SLATE}↓ {below} more line{'s' if below != 1 else ''} (PgDn){RST}")
            lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        label_width = max((plat.str_width(item.label) for item in self.items), default=0)
        for position, item in enumerate(self.items):
            selected = position == self.index
            mark = f"{MINT}{BOLD}{POINTER}{RST}" if selected else " "
            padded = item.label + " " * (label_width - plat.str_width(item.label))
            label = f"{BOLD}{WHITE}{padded}{RST}" if selected else f"{SLATE}{padded}{RST}"
            row = f"  {mark} {label}"
            if item.hint:
                row += "   " + f"{DARK_SLATE}{item.hint}{RST}"
            lines.append(row)
        lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        lines.append(f"  {DARK_SLATE}{FOOTER if width >= 58 else FOOTER_NARROW}{RST}")
        return lines

    # ── keys ──
    def _page(self) -> int:
        return max(1, self._sizes()[1] - 1)

    def handle(self, key: str) -> None:
        count = len(self.items)
        if key in ("esc", "ctrl-c", "q"):
            self._done = True
        elif key == "enter":
            self._result = self.items[self.index] if self.items else None
            self._done = True
        elif key in ("up", "ctrl-p", "k") and count:
            self.index = (self.index - 1) % count
        elif key in ("down", "ctrl-n", "j") and count:
            self.index = (self.index + 1) % count
        elif key in ("pageup", "ctrl-u"):
            self.offset = max(0, self.offset - self._page())
        elif key in ("pagedown", "ctrl-d"):
            self.offset += self._page()
        elif key == "home":
            self.offset = 0
        elif key == "end":
            self.offset = 10 ** 6
        elif len(key) == 1 and key in "123456789" and int(key) <= count:
            self.index = int(key) - 1
            self._result = self.items[self.index]
            self._done = True

    def run(self) -> SelectItem | None:
        picker.note_menu_started()
        if not plat.is_interactive():
            return self.items[self.index] if self.items else None
        plat.setup_console()
        try:
            with plat.raw_terminal(isig=False):
                self._live.render(self._frame())
                while not self._done:
                    key = plat.read_key()
                    if key is None:
                        continue
                    self.handle(key)
                    if not self._done:
                        self._live.render(self._frame())
        finally:
            self._live.clear()
            sys.stdout.write(plat.SHOW_CURSOR)
            sys.stdout.flush()
        return self._result


def show_detail(items: Sequence[SelectItem], title: str, subtitle: str, body: Sequence[BodyLine],
                index: int = 0) -> Any:
    """Run a detail screen; returns the chosen item or None (Esc)."""
    return DetailScreen(items, title, subtitle, body, index).run()
