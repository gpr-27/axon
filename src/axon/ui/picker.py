"""
Interactive arrow key menu selector.
Cross-platform support for macOS, Linux, and Windows with 100% flicker-free line management.
"""
from __future__ import annotations
import os
import sys
from axon.ui.theme import BOLD, DARK_SLATE, DIM, GOLD, MINT, RST, SLATE, TEAL, WHITE, strip_ansi, term_width

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


def pick(options: list[str], title: str = "Select Option", current: str | None = None) -> str | None:
    if not options:
        return None
    if not sys.stdin.isatty():
        return current or options[0]

    idx = options.index(current) if (current and current in options) else 0
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

    # POSIX (macOS & Linux) clean cbreak loop
    if _HAS_TERMIOS and termios is not None and tty is not None:
        fd = sys.stdin.fileno()
        old_attr = termios.tcgetattr(fd)
        rendered_lines_count = 0

        def render_posix() -> None:
            nonlocal rendered_lines_count
            tw = term_width()
            max_opt_len = max(20, tw - 8)
            max_visible = 10

            lines: list[str] = []
            lines.append(f"  {GOLD}{BOLD}{title}{RST}  {DIM}({idx + 1}/{n} · ↑ ↓ Navigate · Enter Select · Esc Cancel){RST}")
            lines.append("")

            start_idx = max(0, min(idx - max_visible // 2, n - max_visible))
            end_idx = min(n, start_idx + max_visible)

            for i in range(start_idx, end_idx):
                opt = options[i]
                opt_clean = " ".join(opt.split())
                if len(opt_clean) > max_opt_len:
                    opt_clean = opt_clean[:max_opt_len - 3] + "..."

                if i == idx:
                    lines.append(f"  {MINT}{BOLD}▶ {WHITE}{opt_clean}{RST}")
                else:
                    lines.append(f"    {SLATE}{opt_clean}{RST}")

            if n > max_visible:
                more_below = n - end_idx
                more_above = start_idx
                scroll_hint = []
                if more_above > 0:
                    scroll_hint.append(f"↑ {more_above} more above")
                if more_below > 0:
                    scroll_hint.append(f"↓ {more_below} more below")
                lines.append(f"    {DARK_SLATE}... ({', '.join(scroll_hint)}){RST}")

            # Clear previously rendered lines cleanly
            if rendered_lines_count > 0:
                sys.stdout.write(f"\033[{rendered_lines_count}A\r")
                for _ in range(rendered_lines_count):
                    sys.stdout.write("\033[2K\n")
                sys.stdout.write(f"\033[{rendered_lines_count}A\r")

            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            rendered_lines_count = len(lines)

        try:
            tty.setcbreak(fd)
            render_posix()

            while True:
                raw_bytes = os.read(fd, 1024)
                if not raw_bytes:
                    break

                # Enter -> Select
                if raw_bytes in (b"\r", b"\n", b"\r\n"):
                    return options[idx]

                # Esc / Ctrl+C / q -> Cancel
                if raw_bytes in (b"\x1b", b"\x03", b"q", b"Q"):
                    return None

                # Down Arrow
                if raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                    idx = (idx + 1) % n
                    render_posix()
                    continue

                # Up Arrow
                if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                    idx = (idx - 1) % n
                    render_posix()
                    continue

                # Home / Top
                if raw_bytes.startswith((b"\x1b[H", b"\x1b[1~")):
                    idx = 0
                    render_posix()
                    continue

                # End / Bottom
                if raw_bytes.startswith((b"\x1b[F", b"\x1b[4~")):
                    idx = n - 1
                    render_posix()
                    continue

        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            except Exception:
                pass

    # Windows (msvcrt)
    elif _HAS_MSVCRT and msvcrt is not None:
        rendered_lines_count = 0

        def render_win() -> None:
            nonlocal rendered_lines_count
            tw = term_width()
            max_opt_len = max(20, tw - 8)
            max_visible = 10

            lines: list[str] = []
            lines.append(f"  {GOLD}{BOLD}{title}{RST}  {DIM}({idx + 1}/{n} · ↑ ↓ Navigate · Enter Select · Esc Cancel){RST}")
            lines.append("")

            start_idx = max(0, min(idx - max_visible // 2, n - max_visible))
            end_idx = min(n, start_idx + max_visible)

            for i in range(start_idx, end_idx):
                opt = options[i]
                opt_clean = " ".join(opt.split())
                if len(opt_clean) > max_opt_len:
                    opt_clean = opt_clean[:max_opt_len - 3] + "..."

                if i == idx:
                    lines.append(f"  {MINT}{BOLD}▶ {WHITE}{opt_clean}{RST}")
                else:
                    lines.append(f"    {SLATE}{opt_clean}{RST}")

            if n > max_visible:
                more_below = n - end_idx
                more_above = start_idx
                scroll_hint = []
                if more_above > 0:
                    scroll_hint.append(f"↑ {more_above} more above")
                if more_below > 0:
                    scroll_hint.append(f"↓ {more_below} more below")
                lines.append(f"    {DARK_SLATE}... ({', '.join(scroll_hint)}){RST}")

            if rendered_lines_count > 0:
                sys.stdout.write(f"\033[{rendered_lines_count}A\r")
                for _ in range(rendered_lines_count):
                    sys.stdout.write("\033[2K\n")
                sys.stdout.write(f"\033[{rendered_lines_count}A\r")

            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
            rendered_lines_count = len(lines)

        render_win()
        while True:
            try:
                ch = msvcrt.getch()
            except Exception:
                return options[idx]

            if ch in (b"\r", b"\n"):
                return options[idx]
            if ch in (b"\x1b", b"\x03", b"q", b"Q"):
                return None
            if ch in (b"\x00", b"\xe0"):
                ext = msvcrt.getch()
                if ext == b"H":  # Up arrow
                    idx = (idx - 1) % n
                    render_win()
                elif ext == b"P":  # Down arrow
                    idx = (idx + 1) % n
                    render_win()
                elif ext == b"G":  # Home
                    idx = 0
                    render_win()
                elif ext == b"O":  # End
                    idx = n - 1
                    render_win()

    return options[idx]
