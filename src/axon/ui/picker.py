"""
Interactive arrow key menu selector.
Cross-platform support for macOS, Linux, and Windows.
"""
from __future__ import annotations
import sys
from axon.ui.theme import BOLD, DIM, GOLD, RST, SLATE, TEAL

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ModuleNotFoundError:
    termios = None  # type: ignore
    tty = None      # type: ignore
    _HAS_TERMIOS = False

try:
    import msvcrt
    _HAS_MSVCRT = True
except ModuleNotFoundError:
    msvcrt = None  # type: ignore
    _HAS_MSVCRT = False


def _getch() -> str:
    if _HAS_TERMIOS and termios is not None and tty is not None and sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch
    elif _HAS_MSVCRT and msvcrt is not None:
        try:
            b = msvcrt.getch()
            return b.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    else:
        try:
            return sys.stdin.read(1)
        except Exception:
            return ""


def pick(options: list[str], title: str = "Select Option", current: str | None = None) -> str | None:
    if not options:
        return None
    if not sys.stdin.isatty():
        return current or options[0]

    idx = options.index(current) if current in options else 0
    n = len(options)

    # If neither termios nor msvcrt is available, use numbered fallback
    if not _HAS_TERMIOS and not _HAS_MSVCRT:
        print(f"\n  {GOLD}{BOLD}{title}{RST}")
        for i, opt in enumerate(options, 1):
            marker = "▶ " if (opt == current) else "  "
            print(f"  {marker}[{i}] {opt}")
        try:
            choice = input(f"\n  Select [1-{n}] (Enter for default): ").strip()
            if not choice:
                return current or options[0]
            num = int(choice)
            if 1 <= num <= n:
                return options[num - 1]
        except Exception:
            pass
        return current or options[0]

    rendered = [False]

    def render() -> None:
        if rendered[0]:
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
        # Windows extended keys: \x00 or \xe0 prefix
        if _HAS_MSVCRT and ch in ("\x00", "\xe0"):
            ext = _getch()
            if ext == "H":  # Up arrow
                idx = (idx - 1) % n
                render()
            elif ext == "P":  # Down arrow
                idx = (idx + 1) % n
                render()
            continue
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

