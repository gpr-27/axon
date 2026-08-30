"""
Interactive arrow key menu selector.
"""
from __future__ import annotations
import sys
import termios
import tty
from axon.ui.theme import BOLD, DIM, GOLD, RST, SLATE, TEAL

def _getch() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch

def pick(options: list[str], title: str = "Select Option", current: str | None = None) -> str | None:
    if not sys.stdin.isatty():
        return current or (options[0] if options else None)

    idx = options.index(current) if current in options else 0
    n = len(options)
    rendered = [False]  # track whether first render has happened

    def render() -> None:
        if rendered[0]:
            # Move up to the top of the previously drawn menu and erase it
            sys.stdout.write(f"\033[{n + 3}A\033[J")
        sys.stdout.write(f"\n  {GOLD}{BOLD}{title}  {DIM}(↑ ↓ Navigate · Enter Select · Esc Cancel){RST}\n\n")
        for i, opt in enumerate(options):
            if i == idx:
                sys.stdout.write(f"  {TEAL}{BOLD}▶ {opt}{RST}\n")
            else:
                sys.stdout.write(f"    {SLATE}{opt}{RST}\n")
        sys.stdout.flush()
        rendered[0] = True

    render()

    while True:
        ch = _getch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            return options[idx]
        if ch == "\x1b":
            nxt = _getch()
            if nxt == "[":
                arrow = _getch()
                if arrow == "A":
                    idx = (idx - 1) % n
                    render()
                if arrow == "B":
                    idx = (idx + 1) % n
                    render()
            else:
                sys.stdout.write("\n")
                return None
        if ch in ("q", "Q"):
            sys.stdout.write("\n")
            return None
