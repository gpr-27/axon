"""
Cross-platform terminal and process layer for Axon.

Everything OS-specific used by the interactive UI and the shell tools lives here so the rest of
the code base never touches ``termios``, ``tty``, ``msvcrt`` or Win32 consoles directly:

* ``setup_console()``  – UTF-8 streams, VT (ANSI) output on Windows (re-callable before prompts).
* ``read_key()``       – one normalized key name from the keyboard, with optional timeout.
* ``raw_terminal()``   – context manager for raw keyboard mode (always restored, also atexit).
* ``cooked_terminal()``– temporarily return to normal line mode inside a raw section.
* ``default_shell()`` / ``shell_name()`` / ``shell_command_argv()`` – which shell runs commands.
* ``kill_process_tree()`` / ``new_process_group_kwargs()`` – reliable timeouts.
* ``str_width()`` / ``truncate_ansi()`` – display-width aware text helpers (CJK, emoji).

Key names returned by ``read_key``: "up", "down", "left", "right", "ctrl-left", "ctrl-right",
"home", "end", "delete", "insert", "pageup", "pagedown", "enter", "shift-enter", "alt-enter",
"ctrl-j", "backspace", "alt-backspace", "tab", "shift-tab", "esc", "ctrl-<letter>" (e.g.
"ctrl-c", "ctrl-d"), "ctrl-_", "alt-<char>", "resize", "paste:<text>" (bracketed or detected
paste, newlines normalized to "\\n"), "mouse:<button>:<col>:<row>:<M|m>" (an SGR mouse report,
only while mouse reporting is on; see ``parse_mouse()``), "cpr:<row>:<col>" (a cursor position
report, see ``query_cursor_position()``), or a single literal character.
"""
from __future__ import annotations

import atexit
import base64
import codecs
import collections
import contextlib
import functools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterator

IS_WINDOWS: bool = os.name == "nt"
IS_MAC: bool = sys.platform == "darwin"

try:  # POSIX terminal control
    import termios  # type: ignore
    import tty  # noqa: F401  (kept for feature detection)
    import select as _select
    HAS_TERMIOS = True
except ImportError:  # Windows
    termios = None  # type: ignore
    _select = None  # type: ignore
    HAS_TERMIOS = False

try:
    import msvcrt  # type: ignore
    HAS_MSVCRT = True
except ImportError:
    msvcrt = None  # type: ignore
    HAS_MSVCRT = False

PASTE_PREFIX = "paste:"
MOUSE_PREFIX = "mouse:"
CPR_PREFIX = "cpr:"
# SGR mouse reporting (press/release + wheel, SGR-encoded coordinates). Only the idle prompt and
# full-screen views turn it on, and always off again in a finally block (and atexit).
ENABLE_MOUSE = "\033[?1000h\033[?1006h"
DISABLE_MOUSE = "\033[?1006l\033[?1000l"
_mouse_on = False


def set_mouse_reporting(on: bool) -> None:
    """Turn SGR mouse reporting on/off (tracked so an exit while it is on still turns it off)."""
    global _mouse_on
    if IS_WINDOWS and on:
        return  # keyboard only: console mouse input is not decoded as VT sequences here
    if on == _mouse_on:
        return
    _mouse_on = on
    _write_raw(ENABLE_MOUSE if on else DISABLE_MOUSE)


def mouse_reporting() -> bool:
    return _mouse_on
ESC_TIMEOUT = 0.035  # seconds to wait for the rest of an escape sequence

ENTER_ALT_SCREEN = "\033[?1049h\033[H"
EXIT_ALT_SCREEN = "\033[?1049l"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"

_ANSI_RE = re.compile(r"\x1b\]8;;.*?(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")

# ─────────────────────────────────────────────────────────────────────────────
# Console setup (UTF-8 + VT processing)
# ─────────────────────────────────────────────────────────────────────────────

VT_ENABLED: bool = not IS_WINDOWS
_console_lock = threading.RLock()
_setup_done = False
_saved_output_cp: int | None = None

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_DISABLE_NEWLINE_AUTO_RETURN = 0x0008


def _kernel32() -> Any:
    import ctypes
    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _get_console_mode(handle: Any) -> int | None:
    import ctypes
    mode = ctypes.c_uint32()
    if _kernel32().GetConsoleMode(handle, ctypes.byref(mode)):
        return int(mode.value)
    return None


def enable_vt_mode() -> bool:
    """Enable ANSI/VT processing on the Windows console (stdout and stderr).

    Child processes may reset the console mode, so this is cheap and safe to call before every
    prompt or redraw. Only ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x4) is added; 0x8
    (DISABLE_NEWLINE_AUTO_RETURN) is explicitly cleared because it stair-steps child output.
    Returns True when VT sequences will be interpreted.
    """
    global VT_ENABLED
    if not IS_WINDOWS:
        return True
    ok = False
    try:
        k32 = _kernel32()
        for std in (_STD_OUTPUT_HANDLE, _STD_ERROR_HANDLE):
            h = k32.GetStdHandle(std)
            mode = _get_console_mode(h)
            if mode is None:
                continue
            new_mode = (mode | _ENABLE_PROCESSED_OUTPUT | _ENABLE_VIRTUAL_TERMINAL_PROCESSING) & ~_DISABLE_NEWLINE_AUTO_RETURN
            if new_mode == mode or k32.SetConsoleMode(h, new_mode):
                ok = True
    except Exception:
        ok = False
    if not ok:
        # Legacy conhost (pre Windows 10 1511) or no console: colorama translates basic SGR.
        try:
            import colorama  # type: ignore
            fix = getattr(colorama, "just_fix_windows_console", None)
            if fix is not None:
                fix()
            else:
                colorama.init()
        except Exception:
            pass
    VT_ENABLED = ok
    return ok


def _explicit_io_encoding() -> str:
    """The encoding the user forced with PYTHONIOENCODING (``"cp1252"``, ``"ascii:replace"``...), or ''."""
    return (os.environ.get("PYTHONIOENCODING", "") or "").split(":", 1)[0].strip()


def _reconfigure_streams() -> None:
    # An explicit PYTHONIOENCODING is the user's (or the consuming pipe's) choice: keep that encoding
    # and only make errors non-fatal, so glyph() detects it and switches to its ASCII fallback.
    keep_encoding = bool(_explicit_io_encoding())
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if keep_encoding:
                if getattr(stream, "errors", "strict") == "strict":
                    reconf(errors="replace")
            elif enc != "utf8":
                reconf(encoding="utf-8", errors="replace")
            elif getattr(stream, "errors", "strict") == "strict":
                reconf(errors="replace")
        except Exception:
            pass
    stdin = getattr(sys, "stdin", None)
    reconf_in = getattr(stdin, "reconfigure", None)
    if reconf_in is not None:
        try:
            reconf_in(errors="replace")
        except Exception:
            pass


def setup_console() -> None:
    """Prepare the console: UTF-8 streams (errors='replace') and VT mode on Windows. Idempotent."""
    global _setup_done, _saved_output_cp
    with _console_lock:
        if IS_WINDOWS:
            enable_vt_mode()
        if _setup_done:
            return
        _setup_done = True
        _reconfigure_streams()
        if IS_WINDOWS and not _explicit_io_encoding():
            try:
                k32 = _kernel32()
                cp = int(k32.GetConsoleOutputCP())
                if cp and cp != 65001 and k32.SetConsoleOutputCP(65001):
                    _saved_output_cp = cp
            except Exception:
                pass
        atexit.register(_atexit_restore)


def _atexit_restore() -> None:
    _restore_all_raw()
    try:
        if sys.stdout.isatty():
            sys.stdout.write((DISABLE_MOUSE if _mouse_on else "") + SHOW_CURSOR)
            sys.stdout.flush()
    except Exception:
        pass
    if IS_WINDOWS and _saved_output_cp:
        try:
            _kernel32().SetConsoleOutputCP(_saved_output_cp)
        except Exception:
            pass


def is_interactive() -> bool:
    """True when both stdin and stdout are terminals (needed for raw-key UIs)."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def supports_alt_screen() -> bool:
    return (not IS_WINDOWS) or VT_ENABLED


def term_size() -> tuple[int, int]:
    """(columns, lines) of the real window. Asked of the terminal first: shutil prefers $COLUMNS/$LINES,
    which a shell sets once and never updates, so resizes would go unnoticed."""
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            sz = os.get_terminal_size(stream.fileno())
            if sz.columns > 0 and sz.lines > 0:
                return max(20, sz.columns), max(5, sz.lines)
        except (AttributeError, OSError, ValueError):
            continue
    try:
        sz = shutil.get_terminal_size(fallback=(88, 24))
        return max(20, sz.columns), max(5, sz.lines)
    except Exception:
        return 88, 24


def eof_hint() -> str:
    """How the user sends end-of-file in plain line input on this platform."""
    return "Ctrl+Z then Enter" if IS_WINDOWS else "Ctrl+D"


def editor_command() -> list[str]:
    """Return argv for the user's text editor ($VISUAL/$EDITOR, else a sensible default)."""
    import shlex
    env = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if env:
        try:
            parts = shlex.split(env, posix=not IS_WINDOWS)
            if parts:
                return parts
        except ValueError:
            return [env]
    if IS_WINDOWS:
        return ["notepad"]
    for cand in ("nano", "vim", "vi"):
        if shutil.which(cand):
            return [cand]
    return ["vi"]


# ─────────────────────────────────────────────────────────────────────────────
# Display width helpers
# ─────────────────────────────────────────────────────────────────────────────

def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@functools.lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Terminal cell width of a single code point (0, 1 or 2)."""
    if not ch:
        return 0
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    if o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if 0x1F300 <= o <= 0x1FAFF or 0x1F000 <= o <= 0x1F02F:
        return 2
    return 1


def str_width(text: str) -> int:
    """Visible cell width of text, ignoring ANSI escape sequences."""
    plain = strip_ansi(text)
    if plain.isascii():
        return sum(1 for c in plain if " " <= c <= "~")
    return sum(char_width(c) for c in plain)


def truncate_ansi(text: str, max_w: int, ellipsis: str = "…") -> str:
    """Cut text to at most max_w visible cells, keeping escape codes intact (no colour bleed)."""
    if max_w <= 0:
        return ""
    if str_width(text) <= max_w:
        return text
    ell_w = str_width(ellipsis)
    limit = max(0, max_w - ell_w)
    out: list[str] = []
    width = 0
    pos = 0
    had_codes = False
    for m in _ANSI_RE.finditer(text):
        seg = text[pos:m.start()]
        for ch in seg:
            w = char_width(ch)
            if width + w > limit:
                out.append(ellipsis)
                return "".join(out) + ("\033[0m" if had_codes else "")
            out.append(ch)
            width += w
        out.append(m.group(0))
        had_codes = True
        pos = m.end()
    for ch in text[pos:]:
        w = char_width(ch)
        if width + w > limit:
            break
        out.append(ch)
        width += w
    out.append(ellipsis)
    return "".join(out) + ("\033[0m" if had_codes else "")


def pad_to(text: str, width: int) -> str:
    """Right-pad text with spaces to the given visible width."""
    return text + " " * max(0, width - str_width(text))


# ─────────────────────────────────────────────────────────────────────────────
# Raw terminal mode
# ─────────────────────────────────────────────────────────────────────────────

_raw_lock = threading.RLock()
_raw_stack: list[tuple[str, Any, Any]] = []  # (kind, handle/fd, saved state)


def _stdin_fd() -> int | None:
    try:
        return sys.stdin.fileno()
    except Exception:
        return None


def _restore_all_raw() -> None:
    with _raw_lock:
        while _raw_stack:
            kind, handle, saved = _raw_stack.pop()
            _apply_saved(kind, handle, saved)


def _apply_saved(kind: str, handle: Any, saved: Any) -> None:
    try:
        if kind == "posix" and termios is not None:
            termios.tcsetattr(handle, termios.TCSADRAIN, saved)
        elif kind == "win":
            _kernel32().SetConsoleMode(handle, saved)
    except Exception:
        pass


def _posix_raw_attrs(fd: int, isig: bool) -> Any:
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.IGNCR)
    lflags = termios.ECHO | termios.ICANON | termios.IEXTEN
    if not isig:
        lflags |= termios.ISIG
    attrs[3] &= ~lflags
    attrs[6] = list(attrs[6])
    attrs[6][termios.VMIN] = 1
    attrs[6][termios.VTIME] = 0
    return attrs


@contextlib.contextmanager
def raw_terminal(isig: bool = False, bracketed_paste: bool = False) -> Iterator[bool]:
    """Put the keyboard in raw mode for the duration of the block.

    isig=False delivers Ctrl+C / Ctrl+Z as keys ("ctrl-c"/"ctrl-z"); isig=True keeps them as
    signals (KeyboardInterrupt). The previous mode is always restored, even on exceptions, and
    an atexit hook restores it if the interpreter dies inside the block. Yields True when raw
    mode is active (False when stdin is not a terminal; the block still runs).
    """
    entry: tuple[str, Any, Any] | None = None
    with _raw_lock:
        try:
            if IS_WINDOWS:
                h = _kernel32().GetStdHandle(_STD_INPUT_HANDLE)
                mode = _get_console_mode(h)
                if mode is not None:
                    new_mode = (mode & ~(_ENABLE_LINE_INPUT | _ENABLE_ECHO_INPUT | _ENABLE_VIRTUAL_TERMINAL_INPUT
                                         | (0 if isig else _ENABLE_PROCESSED_INPUT))) | _ENABLE_WINDOW_INPUT
                    if _kernel32().SetConsoleMode(h, new_mode):
                        entry = ("win", h, mode)
            elif HAS_TERMIOS:
                fd = _stdin_fd()
                if fd is not None and os.isatty(fd):
                    saved = termios.tcgetattr(fd)
                    termios.tcsetattr(fd, termios.TCSADRAIN, _posix_raw_attrs(fd, isig))
                    entry = ("posix", fd, saved)
        except Exception:
            entry = None
        if entry is not None:
            _raw_stack.append(entry)
    if entry is not None and bracketed_paste and not IS_WINDOWS:
        _write_raw("\033[?2004h")
    try:
        yield entry is not None
    finally:
        if entry is not None:
            if bracketed_paste and not IS_WINDOWS:
                _write_raw("\033[?2004l")
            with _raw_lock:
                if entry in _raw_stack:
                    idx = _raw_stack.index(entry)
                    # restore this level and anything nested above it
                    for kind, handle, saved in reversed(_raw_stack[idx:]):
                        _apply_saved(kind, handle, saved)
                    del _raw_stack[idx:]


@contextlib.contextmanager
def cooked_terminal() -> Iterator[None]:
    """Temporarily return to normal (echo, line-buffered) mode inside raw_terminal()."""
    with _raw_lock:
        stack = list(_raw_stack)
        current: list[tuple[str, Any, Any]] = []
        for kind, handle, _saved in stack:
            try:
                if kind == "posix":
                    current.append((kind, handle, termios.tcgetattr(handle)))
                else:
                    current.append((kind, handle, _get_console_mode(handle)))
            except Exception:
                pass
        if stack:
            kind, handle, saved = stack[0]
            _apply_saved(kind, handle, saved)
    try:
        yield
    finally:
        with _raw_lock:
            if current:
                kind, handle, state = current[-1]
                if state is not None:
                    _apply_saved(kind, handle, state)


def _write_raw(s: str) -> None:
    try:
        sys.stdout.write(s)
        sys.stdout.flush()
    except Exception:
        pass


def flush_input() -> None:
    """Discard pending (type-ahead) keyboard input."""
    _pending_chars.clear()
    _events.clear()
    try:
        if IS_WINDOWS:
            _kernel32().FlushConsoleInputBuffer(_kernel32().GetStdHandle(_STD_INPUT_HANDLE))
        elif HAS_TERMIOS:
            fd = _stdin_fd()
            if fd is not None and os.isatty(fd):
                termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Key decoding (shared between POSIX bytes, Windows VT and msvcrt)
# ─────────────────────────────────────────────────────────────────────────────

_pending_chars: collections.deque[str] = collections.deque()
_events: collections.deque[str] = collections.deque()
_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
_last_size: tuple[int, int] | None = None
_stdin_eof = False

_SS3 = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
        "M": "enter", "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
_CSI_LETTER = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab"}
_CSI_TILDE = {1: "home", 7: "home", 4: "end", 8: "end", 2: "insert", 3: "delete", 5: "pageup", 6: "pagedown"}
_MSVCRT_SCAN = {"H": "up", "P": "down", "K": "left", "M": "right", "G": "home", "O": "end", "S": "delete",
                "R": "insert", "I": "pageup", "Q": "pagedown", "s": "ctrl-left", "t": "ctrl-right",
                "\x0f": "shift-tab", "\x94": "tab", "\x93": "delete"}


def ctrl_key_name(ch: str) -> str:
    """Name for a C0 control character (single char)."""
    o = ord(ch)
    if ch == "\r":
        return "enter"
    if ch == "\n":
        return "ctrl-j"
    if ch == "\t":
        return "tab"
    if ch in ("\x7f", "\x08"):
        return "backspace"
    if ch == "\x1b":
        return "esc"
    if ch == "\x1f":
        return "ctrl-_"
    if 1 <= o <= 26:
        return "ctrl-" + chr(o + 96)
    return ""


def _decode_sequence(first: str, getc: Callable[[float], str | None]) -> str:
    """Decode one key starting with `first`, pulling continuation chars via getc(timeout)."""
    if first == "\x1b":
        nxt = getc(ESC_TIMEOUT)
        if nxt is None:
            return "esc"
        if nxt == "[":
            return _decode_csi(getc)
        if nxt == "O":
            fin = getc(ESC_TIMEOUT)
            return _SS3.get(fin or "", "")
        if nxt == "\x1b":
            _pending_chars.appendleft("\x1b")
            return "esc"
        if nxt in ("\r", "\n"):
            return "alt-enter"
        if nxt in ("\x7f", "\x08"):
            return "alt-backspace"
        if nxt == "b":
            return "ctrl-left"
        if nxt == "f":
            return "ctrl-right"
        return "alt-" + nxt
    if ord(first) < 32 or first == "\x7f":
        return ctrl_key_name(first)
    return first


def _decode_csi(getc: Callable[[float], str | None]) -> str:
    params = ""
    final = ""
    for _ in range(40):
        c = getc(ESC_TIMEOUT)
        if c is None:
            return ""
        if "\x40" <= c <= "\x7e":
            final = c
            break
        params += c
    if not final:
        return ""
    if final == "~" and params == "200":
        return PASTE_PREFIX + _read_bracketed_paste(getc)
    if final == "R" and params.count(";") == 1 and not params.startswith(("<", "?", ">")):
        row, _sep, col = params.partition(";")  # cursor position report (answer to CSI 6n)
        if row.isdigit() and col.isdigit():
            return f"{CPR_PREFIX}{row}:{col}"
        return ""
    if params.startswith("<") and final in ("M", "m"):  # SGR mouse report: CSI < b ; x ; y M|m
        parts = params[1:].split(";")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return f"{MOUSE_PREFIX}{parts[0]}:{parts[1]}:{parts[2]}:{final}"
        return ""
    nums: list[int] = []
    for part in params.lstrip("?<>=").split(";"):
        try:
            nums.append(int(part.split(":")[0]) if part else 1)
        except ValueError:
            nums.append(1)
    mod = nums[1] if len(nums) > 1 else 1
    if final in _CSI_LETTER:
        name = _CSI_LETTER[final]
        if name in ("left", "right") and mod in (3, 5, 7, 9):
            return "ctrl-" + name
        return name
    if final == "~":
        code = nums[0] if nums else 0
        if code == 27 and len(nums) >= 3:  # xterm modifyOtherKeys: CSI 27;mod;key ~
            return _keycode_name(nums[2], nums[1])
        if code == 13:
            return "shift-enter"
        return _CSI_TILDE.get(code, "")
    if final == "u":  # kitty / fixterms: CSI key;mod u
        return _keycode_name(nums[0] if nums else 0, mod)
    return ""


def _keycode_name(code: int, mod: int) -> str:
    shift = bool((mod - 1) & 1) if mod > 1 else False
    alt = bool((mod - 1) & 2) if mod > 1 else False
    ctrl = bool((mod - 1) & 4) if mod > 1 else False
    if code in (13, 10):
        if shift or ctrl:
            return "shift-enter"
        return "alt-enter" if alt else "enter"
    if code == 9:
        return "shift-tab" if shift else "tab"
    if code == 27:
        return "esc"
    if code in (127, 8):
        return "alt-backspace" if (alt or ctrl) else "backspace"
    if 32 <= code < 0x110000:
        ch = chr(code)
        if ctrl and ch.isalpha():
            return "ctrl-" + ch.lower()
        if alt:
            return "alt-" + ch
        return ch.upper() if shift and ch.isalpha() else ch
    return ""


def _read_bracketed_paste(getc: Callable[[float], str | None]) -> str:
    buf: list[str] = []
    end = "\x1b[201~"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        c = getc(0.5)
        if c is None:
            break
        buf.append(c)
        if c == "~" and "".join(buf[-6:]) == end:
            del buf[-6:]
            break
    return normalize_newlines("".join(buf))


@dataclass(frozen=True)
class MouseEvent:
    """A decoded SGR mouse report. ``x``/``y`` are 1-based terminal columns/rows.

    kind: "press" | "release" | "drag" | "wheel-up" | "wheel-down"; ``button`` 0 left, 1 middle,
    2 right (for wheel events 0 = up, 1 = down)."""
    kind: str
    button: int
    x: int
    y: int
    shift: bool = False
    alt: bool = False
    ctrl: bool = False


def parse_mouse(key: str | None) -> MouseEvent | None:
    """Turn a ``"mouse:<b>:<x>:<y>:<M|m>"`` key from :func:`read_key` into a :class:`MouseEvent`."""
    if not key or not key.startswith(MOUSE_PREFIX):
        return None
    try:
        b_s, x_s, y_s, fin = key[len(MOUSE_PREFIX):].split(":")
        b, x, y = int(b_s), int(x_s), int(y_s)
    except ValueError:
        return None
    mods = {"shift": bool(b & 4), "alt": bool(b & 8), "ctrl": bool(b & 16)}
    button = b & 3
    if b & 64:
        kind = ("wheel-up", "wheel-down", "wheel-left", "wheel-right")[button]
        return MouseEvent(kind, button, x, y, **mods)
    if b & 32:
        return MouseEvent("drag", button, x, y, **mods)
    return MouseEvent("press" if fin == "M" else "release", button, x, y, **mods)


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _check_resize() -> bool:
    global _last_size
    size = term_size()
    if _last_size is None:
        _last_size = size
        return False
    if size != _last_size:
        _last_size = size
        return True
    return False


# ---- POSIX backend ---------------------------------------------------------------------------

_PASTE_MIN_CHARS = 200


def _looks_like_paste(text: str) -> bool:
    """Heuristic for a *non-bracketed* paste read in one chunk.

    Type-ahead (keys typed while Axon was busy, e.g. ``/exit`` + Enter) also arrives as one chunk,
    so a single line followed by Enter is *typed input*: it must submit.  Only content with a line
    break before its end, or a chunk too long to have been typed, is treated as a paste.
    """
    if len(text) < 2 or "\x1b" in text or not ("\r" in text or "\n" in text):
        return False
    body = text.rstrip("\r\n")
    if not body.strip():
        return False
    return "\r" in body or "\n" in body or len(text) >= _PASTE_MIN_CHARS


def _posix_fill(timeout: float) -> bool:
    fd = _stdin_fd()
    if fd is None or _select is None:
        time.sleep(timeout)
        return False
    try:
        ready, _, _ = _select.select([fd], [], [], max(0.0, timeout))
    except (OSError, ValueError):
        return False
    if not ready:
        return False
    try:
        data = os.read(fd, 65536)
    except OSError:
        return False
    if not data:
        global _stdin_eof
        _stdin_eof = True
        return False
    text = _decoder.decode(data)
    if not text:
        return True  # incomplete utf-8 sequence; wait for the rest
    if _looks_like_paste(text):
        # Non-bracketed multi-line chunk: treat as a paste so it does not submit line by line.
        _events.append(PASTE_PREFIX + normalize_newlines(text))
        return True
    if len(text) > 1 and text.endswith("\n") and "\x1b" not in text:
        # Type-ahead completed while the tty was still in cooked mode (ICRNL turned Enter into
        # LF): it is an Enter, not Ctrl+J.
        text = text[:-1] + "\r"
    _pending_chars.extend(text)
    return True


def _posix_getc(timeout: float) -> str | None:
    if _pending_chars:
        return _pending_chars.popleft()
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining < 0:
            return None
        _posix_fill(remaining)
        if _pending_chars:
            return _pending_chars.popleft()
        if _events:
            return None
        if time.monotonic() >= end:
            return None


def _posix_read(slice_s: float) -> str | None:
    c = _posix_getc(slice_s)
    if c is None:
        return _events.popleft() if _events else None
    return _decode_sequence(c, _posix_getc)


# ---- Windows backends ------------------------------------------------------------------------

_win_structs: Any = None
_win_failed = False
_win_high_surrogate: str = ""


def _win_types() -> Any:
    global _win_structs
    if _win_structs is not None:
        return _win_structs
    import ctypes
    from ctypes import wintypes

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                    ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                    ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_raw", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]

    _win_structs = (ctypes, wintypes, INPUT_RECORD)
    return _win_structs


_VK_NAMES = {0x26: "up", 0x28: "down", 0x25: "left", 0x27: "right", 0x24: "home", 0x23: "end",
             0x2E: "delete", 0x2D: "insert", 0x21: "pageup", 0x22: "pagedown", 0x1B: "esc"}
_IGNORED_VK = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def win_key_units(vk: int, ch: str, state: int, key_down: bool) -> list[str]:
    """Translate one Win32 KEY_EVENT into key names / characters (public for testing)."""
    global _win_high_surrogate
    ctrl = bool(state & 0x0C)
    alt = bool(state & 0x03)
    shift = bool(state & 0x10)
    if not key_down:
        if vk == 0x12 and ch and ch != "\x00":  # Alt+Numpad composed character
            return [ch]
        return []
    if vk in _IGNORED_VK:
        return []
    if vk == 0x0D:
        if shift or ctrl:
            return ["shift-enter"]
        return ["alt-enter" if alt else "enter"]
    if vk == 0x09:
        return ["shift-tab" if shift else "tab"]
    if vk == 0x08:
        return ["alt-backspace" if (ctrl or alt) else "backspace"]
    if vk in _VK_NAMES:
        name = _VK_NAMES[vk]
        if ctrl and name in ("left", "right"):
            name = "ctrl-" + name
        return [name]
    if not ch or ch == "\x00":
        return []
    o = ord(ch)
    if 0xD800 <= o < 0xDC00:
        _win_high_surrogate = ch
        return []
    if 0xDC00 <= o < 0xE000 and _win_high_surrogate:
        ch = (_win_high_surrogate + ch).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        _win_high_surrogate = ""
    if o < 32 or o == 0x7F:
        name = ctrl_key_name(ch)
        return [name] if name else []
    if alt and not ctrl:  # AltGr reports ctrl+alt and must stay a character
        return ["alt-" + ch]
    return [ch]


def coalesce_paste(units: list[str]) -> list[str]:
    """If a burst of units looks like pasted text (several chars incl. newline), merge it."""
    if len(units) < 3:
        return units
    if not any(u in ("enter", "shift-enter", "ctrl-j") for u in units):
        return units
    text_units = {"enter": "\n", "shift-enter": "\n", "ctrl-j": "\n", "tab": "\t"}
    if all(len(u) == 1 or u in text_units for u in units):
        chars = sum(1 for u in units if len(u) == 1)
        if chars >= 2:
            return [PASTE_PREFIX + "".join(text_units.get(u, u) for u in units)]
    return units


def _win_console_read(slice_s: float) -> str | None:
    ctypes, wintypes, INPUT_RECORD = _win_types()
    k32 = _kernel32()
    h = k32.GetStdHandle(_STD_INPUT_HANDLE)
    if k32.WaitForSingleObject(h, int(max(0.0, slice_s) * 1000)) != 0:
        return None
    units: list[str] = []
    records = (INPUT_RECORD * 128)()
    count = wintypes.DWORD()
    for _ in range(4096):  # drain bursts (pastes) completely before classifying
        if not k32.ReadConsoleInputW(h, records, 128, ctypes.byref(count)):
            raise OSError("ReadConsoleInputW failed")
        for i in range(count.value):
            rec = records[i]
            if rec.EventType == 0x0001:
                ke = rec.Event.KeyEvent
                unit = win_key_units(int(ke.wVirtualKeyCode), ke.uChar, int(ke.dwControlKeyState), bool(ke.bKeyDown))
                units.extend(unit * max(1, int(ke.wRepeatCount)) if unit and len(unit[0]) == 1 else unit)
            elif rec.EventType == 0x0004:
                units.append("resize")
        pending = wintypes.DWORD()
        if not k32.GetNumberOfConsoleInputEvents(h, ctypes.byref(pending)) or pending.value == 0:
            break
    if not units:
        return ""
    units = coalesce_paste(units)
    first, rest = units[0], units[1:]
    _events.extend(rest)
    return first


def _msvcrt_getc(timeout: float) -> str | None:
    if _pending_chars:
        return _pending_chars.popleft()
    end = time.monotonic() + timeout
    while not msvcrt.kbhit():
        if time.monotonic() >= end:
            return None
        time.sleep(0.005)
    return msvcrt.getwch()


def _msvcrt_read(slice_s: float) -> str | None:
    c = _msvcrt_getc(slice_s)
    if c is None:
        return None
    if c in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            code = msvcrt.getwch()
            name = _MSVCRT_SCAN.get(code)
            if name:
                return name
            if c == "\xe0":  # literal 'à' followed by another key
                _pending_chars.appendleft(code)
                return c
            return ""
        return "" if c == "\x00" else c
    if c == "\x03":
        return "ctrl-c"
    return _decode_sequence(c, _msvcrt_getc)


def _backend_read(slice_s: float) -> str | None:
    global _win_failed
    if IS_WINDOWS:
        if not _win_failed:
            try:
                return _win_console_read(slice_s)
            except Exception:
                _win_failed = True
        if HAS_MSVCRT:
            return _msvcrt_read(slice_s)
        time.sleep(slice_s)
        return None
    return _posix_read(slice_s)


def parse_cpr(key: str | None) -> tuple[int, int] | None:
    """``"cpr:<row>:<col>"`` (from :func:`read_key`) -> (row, col), 1-based."""
    if not key or not key.startswith(CPR_PREFIX):
        return None
    row, _sep, col = key[len(CPR_PREFIX):].partition(":")
    return (int(row), int(col)) if row.isdigit() and col.isdigit() else None


def query_cursor_position(timeout: float = 0.15) -> tuple[int, int] | None:
    """Ask the terminal where the cursor is (CSI 6n). Call inside raw_terminal(). Keys typed while
    waiting are put back in order; returns None when the terminal does not answer in time (a late
    answer later arrives as a harmless ``"cpr:…"`` key)."""
    if IS_WINDOWS or not is_interactive():
        return None
    _write_raw("\033[6n")
    deadline = time.monotonic() + max(0.0, timeout)
    stash: list[str] = []
    pos = None
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            key = read_key(timeout=left, report_resize=False)
            if key is None:
                break
            pos = parse_cpr(key)
            if pos is not None:
                break
            stash.append(key)
    except EOFError:
        pass
    _events.extendleft(reversed(stash))
    return pos


def read_key(timeout: float | None = None, report_resize: bool = True) -> str | None:
    """Read one key and return its normalized name (see module docstring).

    Returns None when `timeout` (seconds) expires without input. Call inside raw_terminal()
    for correct behaviour; outside it, the terminal's line discipline applies.
    """
    if _events:
        return _events.popleft()
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        if _stdin_eof and not _pending_chars:
            raise EOFError("stdin closed")
        if report_resize and _check_resize():
            return "resize"
        if deadline is None:
            slice_s = 0.1
        else:
            slice_s = min(0.1, max(0.0, deadline - time.monotonic()))
        key = _backend_read(slice_s)
        if key == "resize" and not report_resize:
            key = ""
        if key:
            return key
        if _events:
            return _events.popleft()
        if deadline is not None and time.monotonic() >= deadline:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Shell selection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ShellInfo:
    kind: str   # bash | zsh | sh | pwsh | powershell | cmd
    path: str
    name: str   # display name, e.g. "PowerShell 7.4", "Windows PowerShell 5.1", "bash"

    @property
    def is_powershell(self) -> bool:
        return self.kind in ("pwsh", "powershell")


def _is_wsl_launcher(path: str) -> bool:
    if not IS_WINDOWS:
        return False
    sysroot = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
    p = os.path.normcase(os.path.abspath(path))
    return p.startswith(sysroot.rstrip("\\") + "\\") or "\\windowsapps\\" in p


def find_git_bash() -> str | None:
    """Locate Git for Windows' bash.exe (never the WSL launcher in System32)."""
    cands: list[str] = []
    found = shutil.which("bash")
    if found and not _is_wsl_launcher(found):
        cands.append(found)
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
        cands += [os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
    for c in cands:
        if c and os.path.isfile(c) and not _is_wsl_launcher(c):
            return c
    return None


@functools.lru_cache(maxsize=8)
def _pwsh_version(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "$PSVersionTable.PSVersion.Major.ToString() + '.' + $PSVersionTable.PSVersion.Minor.ToString()"],
            capture_output=True, timeout=8, stdin=subprocess.DEVNULL,
        )
        ver = out.stdout.decode("utf-8", "replace").strip()
        if re.fullmatch(r"\d+\.\d+", ver):
            return ver
    except Exception:
        pass
    return "7"


def _shell_from_spec(spec: str) -> ShellInfo | None:
    s = spec.strip().strip('"')
    low = os.path.basename(s).lower()
    low = low[:-4] if low.endswith(".exe") else low
    if low in ("pwsh", "powershell"):
        path = shutil.which(s) or shutil.which(low)
        if path:
            if low == "pwsh":
                return ShellInfo("pwsh", path, f"PowerShell {_pwsh_version(path)}")
            return ShellInfo("powershell", path, "Windows PowerShell 5.1")
        return None
    if low == "cmd":
        path = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe"
        return ShellInfo("cmd", path, "cmd.exe")
    if low in ("bash", "zsh", "sh", "fish", "dash", "ksh"):
        if os.path.sep in s or (os.altsep and os.altsep in s):
            path = s if os.path.isfile(s) else None
        elif IS_WINDOWS and low == "bash":
            path = find_git_bash()
        else:
            path = shutil.which(low)
        if path:
            kind = "sh" if low in ("dash", "ksh", "fish") else low
            name = "Git Bash" if IS_WINDOWS and low == "bash" else low
            return ShellInfo(kind, path, name)
    return None


@functools.lru_cache(maxsize=4)
def _detect_shell(override: str, windows: bool) -> ShellInfo:
    if override:
        info = _shell_from_spec(override)
        if info:
            return info
    if windows:
        for exe in ("pwsh", "powershell"):
            info = _shell_from_spec(exe)
            if info:
                return info
        return _shell_from_spec("cmd")  # type: ignore[return-value]
    for exe in ("bash", "sh"):
        path = shutil.which(exe)
        if path:
            return ShellInfo(exe, path, exe)
    for path in ("/bin/bash", "/bin/sh"):
        if os.path.exists(path):
            return ShellInfo(os.path.basename(path), path, os.path.basename(path))
    return ShellInfo("sh", "sh", "sh")


def shell_info() -> ShellInfo:
    """The shell used for tool commands. AXON_SHELL overrides (bash|pwsh|powershell|cmd|<path>)."""
    return _detect_shell(os.environ.get("AXON_SHELL", "").strip(), IS_WINDOWS)


def shell_name() -> str:
    """Human-readable active shell, e.g. "PowerShell 7.4", "Windows PowerShell 5.1", "bash"."""
    return shell_info().name


def default_shell() -> list[str]:
    """argv prefix that runs a command string with the active shell (command appended last)."""
    info = shell_info()
    if info.is_powershell:
        return [info.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    if info.kind == "cmd":
        return [info.path, "/d", "/s", "/c"]
    return [info.path, "-c"]


_PS_PRELUDE = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "$OutputEncoding=[System.Text.Encoding]::UTF8;"
               "$ProgressPreference='SilentlyContinue';")


def powershell_encoded(script: str) -> str:
    return base64.b64encode((_PS_PRELUDE + "\n" + script).encode("utf-16-le")).decode("ascii")


def shell_command_argv(command: str, info: ShellInfo | None = None) -> list[str] | str:
    """Full argv (or, for cmd.exe, a command line string) that runs `command` robustly.

    PowerShell gets -EncodedCommand (no quote mangling) with UTF-8 output; cmd gets code page
    65001. Pass the result directly to subprocess.Popen.
    """
    info = info or shell_info()
    if info.is_powershell:
        argv = [info.path, "-NoLogo", "-NoProfile", "-NonInteractive"]
        if IS_WINDOWS:
            argv += ["-ExecutionPolicy", "Bypass"]
        return argv + ["-EncodedCommand", powershell_encoded(command)]
    if info.kind == "cmd":
        one_line = " & ".join(line for line in command.splitlines() if line.strip()) or command
        return f'"{info.path}" /d /s /c "chcp 65001>nul & {one_line}"'
    return [info.path, "-c", command]


# ─────────────────────────────────────────────────────────────────────────────
# Processes
# ─────────────────────────────────────────────────────────────────────────────

def new_process_group_kwargs() -> dict[str, Any]:
    """Popen kwargs that put the child in its own process group (so the tree can be killed)."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen[Any], grace: float = 1.0) -> None:
    """Terminate a process and all of its descendants."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=10,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return
    import signal
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is not None:
            killpg(pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace)
    except Exception:
        pass
    try:
        if killpg is not None:
            killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


@functools.lru_cache(maxsize=1)
def _oem_codec() -> str | None:
    if not IS_WINDOWS:
        return None
    try:
        cp = int(_kernel32().GetOEMCP())
        codecs.lookup(f"cp{cp}")
        return f"cp{cp}"
    except Exception:
        return None


def decode_output(data: bytes) -> str:
    """Decode subprocess output: UTF-8 first, then the Windows OEM code page, else replace."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        oem = _oem_codec()
        if oem:
            try:
                return data.decode(oem)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")


def run_text(argv: list[str] | str, timeout: float = 10.0, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """subprocess.run returning decoded text safely on every OS (stdin closed, never raises
    UnicodeDecodeError). Raises FileNotFoundError / TimeoutExpired like subprocess.run."""
    res = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
    return subprocess.CompletedProcess(res.args, res.returncode, decode_output(res.stdout), decode_output(res.stderr))
