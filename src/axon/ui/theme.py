"""
Axon terminal theme: one coherent palette, capability detection and text-measurement helpers.

* Colour depth: truecolor when ``COLORTERM`` is ``truecolor``/``24bit``, otherwise a 256-colour
  fallback.  ``NO_COLOR`` (any value), ``TERM=dumb`` or a non-TTY stdout turn every code into ``""``.
  ``FORCE_COLOR`` / ``AXON_FORCE_COLOR`` force colours on (``AXON_FORCE_COLOR=truecolor`` forces 24-bit).
* Glyphs: ``glyph(name)`` returns a Unicode glyph when the current stdout encoding can encode it
  (and emoji only on terminals that render them), else an ASCII fallback (Windows cp1252 / conhost).
* Measurement: ``display_width`` (ANSI/OSC-8 aware, East-Asian wide, emoji, combining marks),
  ``truncate`` and ``wrap_ansi`` (keeps styles and hyperlinks across wrapped rows).

Every legacy constant name (GOLD, MINT, SLATE, …) is preserved and mapped onto the palette.
"""
from __future__ import annotations

import functools
import os
import re
import sys
import threading
import unicodedata

# Every component that writes to the terminal from more than one thread (renderer, spinner,
# in-flight input listener, background notifiers) must hold this lock while writing.
OUTPUT_LOCK = threading.RLock()

# Console setup (VT processing / UTF-8 on Windows) lives in axon.platform.
try:  # pragma: no cover - depends on platform module availability
    from axon.platform import setup_console as _setup_console

    _setup_console()
except Exception:  # ImportError before the platform module exists, or any console error
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            import colorama  # declared dependency on win32

            colorama.just_fix_windows_console()
        except Exception:
            pass


# ─── Capability detection ──────────────────────────────────────────────────

def _stream_isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:
        return False


def detect_color_level(env: dict[str, str] | None = None, stream: object | None = None) -> int:
    """Return 0 (no colour), 1 (256 colours) or 2 (truecolor)."""
    env = dict(os.environ) if env is None else env
    stream = sys.stdout if stream is None else stream
    force = (env.get("AXON_FORCE_COLOR") or env.get("FORCE_COLOR") or "").strip().lower()
    if "NO_COLOR" in env and not force:
        return 0
    truecolor = env.get("COLORTERM", "").lower() in ("truecolor", "24bit") or force in ("truecolor", "24bit", "3")
    if force and force not in ("0", "false", "no"):
        return 2 if truecolor else 1
    if env.get("TERM", "") == "dumb":
        return 0
    if not _stream_isatty(stream):
        return 0
    if truecolor:
        return 2
    if os.name == "nt" and env.get("WT_SESSION"):
        return 2  # Windows Terminal supports 24-bit colour
    return 1


COLOR_LEVEL = detect_color_level()
COLOR_ENABLED = COLOR_LEVEL > 0


def colors_enabled() -> bool:
    return COLOR_ENABLED


def _sgr(code: str) -> str:
    return f"\033[{code}m" if COLOR_ENABLED else ""


def _fg(rgb: tuple[int, int, int], c256: int) -> str:
    if not COLOR_ENABLED:
        return ""
    if COLOR_LEVEL >= 2:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[38;5;{c256}m"


def _bg(rgb: tuple[int, int, int], c256: int) -> str:
    if not COLOR_ENABLED:
        return ""
    if COLOR_LEVEL >= 2:
        return f"\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"\033[48;5;{c256}m"


# ─── Styles ────────────────────────────────────────────────────────────────
RST    = _sgr("0")
BOLD   = _sgr("1")
DIM    = _sgr("2")
ITALIC = _sgr("3")
UNDER  = _sgr("4")
STRIKE = _sgr("9")

# ─── Palettes ──────────────────────────────────────────────────────────────
# name -> ((r, g, b), xterm-256 index). "dark" is the default "Axon dusk" palette (teal primary,
# violet accent); other themes override some entries. Backgrounds end in _BG.
_DARK: dict[str, tuple[tuple[int, int, int], int]] = {
    "PRIMARY": ((94, 210, 200), 80), "ACCENT": ((180, 160, 255), 183), "SUCCESS": ((134, 222, 140), 114),
    "WARNING": ((246, 196, 92), 221), "ERROR": ((255, 120, 120), 210), "INFO": ((122, 182, 255), 111),
    "TEXT": ((228, 230, 236), 254), "MUTED": ((140, 146, 160), 245), "SUBTLE": ((100, 106, 120), 241),
    "BORDER": ((72, 78, 92), 239), "CODE_FG": ((240, 190, 140), 216), "CODE_BG": ((38, 41, 50), 236),
    "DIFF_ADD": ((134, 222, 140), 114), "DIFF_DEL": ((255, 120, 120), 210),
    "DIFF_ADD_BG": ((24, 58, 36), 22), "DIFF_DEL_BG": ((70, 28, 32), 52), "LINK": ((122, 182, 255), 111),
    "CYAN": ((110, 220, 240), 117), "TERRACOTTA": ((232, 140, 100), 209), "ORANGE": ((255, 176, 110), 215),
    "AMBER": ((255, 180, 70), 214), "EMERALD": ((80, 220, 150), 48), "CORAL": ((255, 128, 150), 204),
    "LAVENDER": ((210, 200, 255), 189), "INDIGO": ((160, 130, 255), 141), "SKY": ((130, 200, 255), 117),
    "BLUE_PILL_BG": ((30, 70, 120), 24), "GREEN_PILL_BG": ((30, 100, 50), 28), "GOLD_PILL_BG": ((100, 80, 20), 58),
    "GRAY_PILL_BG": ((58, 60, 68), 237), "GRAY_PILL_FG": ((200, 200, 205), 250),
    # cards: intra-line diff words, search-match highlight
    "DIFF_ADD_WORD_BG": ((44, 110, 62), 28), "DIFF_DEL_WORD_BG": ((128, 44, 52), 88), "MATCH_BG": ((92, 74, 18), 58),
}
_LIGHT = {
    "PRIMARY": ((0, 128, 120), 30), "ACCENT": ((110, 70, 200), 98), "SUCCESS": ((20, 130, 50), 28),
    "WARNING": ((165, 105, 0), 136), "ERROR": ((190, 40, 40), 160), "INFO": ((20, 90, 200), 26),
    "TEXT": ((30, 32, 40), 235), "MUTED": ((88, 94, 108), 242), "SUBTLE": ((125, 130, 145), 245),
    "BORDER": ((180, 184, 192), 250), "CODE_FG": ((150, 70, 20), 130), "CODE_BG": ((236, 238, 242), 255),
    "DIFF_ADD": ((20, 130, 50), 28), "DIFF_DEL": ((190, 40, 40), 160),
    "DIFF_ADD_BG": ((218, 244, 222), 194), "DIFF_DEL_BG": ((250, 224, 224), 224), "LINK": ((20, 90, 200), 26),
    "CYAN": ((0, 120, 150), 31), "TERRACOTTA": ((180, 80, 40), 130), "ORANGE": ((190, 100, 20), 166),
    "AMBER": ((175, 105, 0), 136), "EMERALD": ((0, 140, 80), 29), "CORAL": ((200, 50, 90), 161),
    "LAVENDER": ((100, 80, 180), 61), "INDIGO": ((90, 60, 200), 62), "SKY": ((30, 110, 190), 25),
    "GRAY_PILL_BG": ((220, 222, 228), 253), "GRAY_PILL_FG": ((50, 52, 60), 238),
    "DIFF_ADD_WORD_BG": ((160, 228, 172), 151), "DIFF_DEL_WORD_BG": ((246, 178, 178), 217), "MATCH_BG": ((255, 230, 140), 229),
}
_HIGH_CONTRAST = {
    "PRIMARY": ((0, 255, 255), 51), "ACCENT": ((255, 135, 255), 213), "SUCCESS": ((0, 255, 95), 47),
    "WARNING": ((255, 255, 0), 226), "ERROR": ((255, 70, 70), 203), "INFO": ((95, 175, 255), 75),
    "TEXT": ((255, 255, 255), 231), "MUTED": ((215, 215, 215), 252), "SUBTLE": ((180, 180, 180), 249),
    "BORDER": ((160, 160, 160), 247), "CODE_FG": ((255, 215, 0), 220), "CODE_BG": ((0, 0, 0), 16),
    "DIFF_ADD": ((0, 255, 95), 47), "DIFF_DEL": ((255, 70, 70), 203),
    "DIFF_ADD_BG": ((0, 95, 0), 22), "DIFF_DEL_BG": ((115, 0, 0), 52), "LINK": ((95, 175, 255), 75),
    "CYAN": ((0, 255, 255), 51), "AMBER": ((255, 175, 0), 214), "SKY": ((95, 215, 255), 81),
    "DIFF_ADD_WORD_BG": ((0, 150, 0), 28), "DIFF_DEL_WORD_BG": ((175, 0, 0), 124), "MATCH_BG": ((135, 95, 0), 94),
}
THEMES: tuple[str, ...] = ("auto", "dark", "light", "high-contrast", "mono")
_THEME_SPECS = {"dark": {}, "light": _LIGHT, "high-contrast": _HIGH_CONTRAST}
_ALIASES = {
    "WHITE": "TEXT", "LBLUE": "INFO", "MINT": "SUCCESS", "GOLD": "WARNING", "ROSE": "ERROR", "SLATE": "MUTED",
    "PURPLE": "ACCENT", "TEAL": "PRIMARY", "GREEN": "DIFF_ADD", "RED": "DIFF_DEL", "GRAY_BG": "CODE_BG",
    "DARK_SLATE": "SUBTLE", "DARK_BORDER": "BORDER",
}
_PALETTE_NAMES: tuple[str, ...] = ()
_current_theme = "dark"


def _palette_values(theme: str) -> dict[str, str]:
    """Every exported colour constant for *theme* (already resolved: 'auto' must not be passed)."""
    mono = theme == "mono"
    spec = dict(_DARK)
    spec.update(_THEME_SPECS.get(theme, {}))
    vals: dict[str, str] = {}
    for name, (rgb, c256) in spec.items():
        if name.endswith("_BG") and name.endswith("PILL_BG"):
            continue
        if mono:
            vals[name] = ""
        elif name.endswith("_BG"):
            vals[name] = _bg(rgb, c256)
        else:
            vals[name] = _fg(rgb, c256)
    for alias, target in _ALIASES.items():
        vals[alias] = vals[target]
    if mono:
        rev = _sgr("7")
        vals.update(BLUE_PILL=rev, GREEN_PILL=rev, GOLD_PILL=rev, GRAY_PILL=rev)
    else:
        white = _sgr("97")
        for pill in ("BLUE", "GREEN", "GOLD"):
            rgb, c = spec[f"{pill}_PILL_BG"]
            vals[f"{pill}_PILL"] = _bg(rgb, c) + white
        rgb, c = spec["GRAY_PILL_BG"]
        frgb, fc = spec["GRAY_PILL_FG"]
        vals["GRAY_PILL"] = _bg(rgb, c) + _fg(frgb, fc)
    vals.pop("GRAY_PILL_FG", None)
    return vals


def detect_background(env: dict[str, str] | None = None) -> str:
    """'light' or 'dark' from $COLORFGBG ("fg;bg", set by iTerm2, rxvt, Konsole...); default dark."""
    env = dict(os.environ) if env is None else env
    raw = env.get("COLORFGBG", "")
    try:
        bg = int(raw.split(";")[-1])
    except (ValueError, IndexError):
        return "dark"
    return "light" if bg in (7, 15) or 9 <= bg <= 15 and bg != 8 else "dark"


def resolve_theme(name: str | None) -> str:
    n = str(name or "auto").strip().lower().replace("_", "-")
    n = {"highcontrast": "high-contrast", "contrast": "high-contrast", "hc": "high-contrast",
         "monochrome": "mono", "none": "mono", "default": "dark"}.get(n, n)
    if n not in THEMES:
        raise ValueError(f"unknown theme {name!r}; choose one of: {', '.join(THEMES)}")
    return detect_background() if n == "auto" else n


def current_theme() -> str:
    return _current_theme


def set_theme(name: str | None) -> str:
    """Switch the palette live: rebinds the colour constants here and in every loaded axon module
    that imported them by name. Returns the resolved theme ("auto" resolves to dark/light)."""
    global _current_theme
    resolved = resolve_theme(name)
    g = globals()
    new = _palette_values(resolved)
    old = {k: g.get(k) for k in new}
    g.update(new)
    _current_theme = resolved
    if old != new:
        for mod_name, mod in list(sys.modules.items()):
            if mod is None or not (mod_name == "axon" or mod_name.startswith("axon.")) or mod_name == __name__:
                continue
            md = getattr(mod, "__dict__", None)
            if not isinstance(md, dict):
                continue
            for k, v in new.items():
                if k in md and isinstance(md[k], str) and md[k] == old[k]:
                    md[k] = v
    return resolved


globals().update(_palette_values("dark"))
_PALETTE_NAMES = tuple(_palette_values("dark"))
if os.environ.get("AXON_THEME"):
    try:
        set_theme(os.environ["AXON_THEME"])
    except ValueError:
        pass


# ─── ANSI handling & width measurement ─────────────────────────────────────
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ESC_TOKEN_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]")


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences and OSC sequences (e.g. OSC 8 hyperlinks)."""
    return _CSI_RE.sub("", _OSC_RE.sub("", text))


def sanitize(text: str) -> str:
    """Remove terminal control characters (incl. raw ESC) from untrusted text; keeps \\n and \\t."""
    return _CONTROL_RE.sub("", text.replace("\r\n", "\n"))


def _is_zero_width(ch: str) -> bool:
    o = ord(ch)
    if o in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return True
    return unicodedata.category(ch) in ("Mn", "Me", "Cf")


@functools.lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Terminal cell width of a single character (0, 1 or 2)."""
    if not ch:
        return 0
    o = ord(ch)
    if o < 32 or o == 0x7F:
        return 0
    if o < 0x300:
        return 1
    if _is_zero_width(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return 2
    if 0x1F000 <= o <= 0x1FAFF:  # pictographs / emoji blocks
        return 2
    return 1


def display_width(s: str) -> int:
    """Visible terminal column width of *s* (ignores ANSI/OSC, handles wide chars and emoji)."""
    if not s:
        return 0
    t = strip_ansi(s)
    if t.isascii():
        return sum(1 for c in t if " " <= c <= "~")
    w = 0
    prev = ""
    after_zwj = False
    for ch in t:
        if ch == "\ufe0f":
            if prev and not prev.isascii() and char_width(prev) == 1:
                w += 1  # VS16 emoji presentation widens a text-style symbol (e.g. ⚙️)
        elif ch == "\u200d":
            after_zwj = True
            prev = ch
            continue
        elif after_zwj and char_width(ch) == 2:
            pass  # joined into the previous emoji (ZWJ sequence) - occupies no extra cells
        else:
            w += char_width(ch)
        after_zwj = False
        prev = ch
    return w


def _tokens(s: str):
    """Yield (is_escape, text) tokens; non-escape tokens are single grapheme-ish chars."""
    pos = 0
    for m in _ESC_TOKEN_RE.finditer(s):
        for ch in s[pos:m.start()]:
            yield False, ch
        yield True, m.group()
        pos = m.end()
    for ch in s[pos:]:
        yield False, ch


def truncate(s: str, width: int, ellipsis: str | None = None) -> str:
    """Truncate *s* to at most *width* columns, keeping escape codes intact and closing styles."""
    if width <= 0:
        return ""
    if display_width(s) <= width:
        return s
    ell = glyph("ellipsis") if ellipsis is None else ellipsis
    ell_w = display_width(ell)
    budget = max(0, width - ell_w)
    out: list[str] = []
    used = 0
    saw_escape = False
    link_open = False
    for is_esc, tok in _tokens(s):
        if is_esc:
            saw_escape = True
            if tok.startswith("\x1b]8;"):
                link_open = not tok.startswith("\x1b]8;;\x1b") and not tok.startswith("\x1b]8;;\x07")
            out.append(tok)
            continue
        w = char_width(tok)
        if used + w > budget:
            break
        out.append(tok)
        used += w
    if link_open:
        out.append("\x1b]8;;\x1b\\")
    out.append(ell if used + ell_w <= width else "")
    if saw_escape:
        out.append(RST or "\033[0m")
    return "".join(out)


def pad(s: str, width: int, align: str = "left") -> str:
    """Pad *s* with spaces to *width* columns (left/right/center)."""
    gap = max(0, width - display_width(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def wrap_ansi(s: str, width: int) -> list[str]:
    """Word-wrap a styled string to *width* columns.

    Active SGR styles and OSC 8 hyperlinks are closed at the end of each row and re-opened on the
    next one, so every row is self-contained.  Words longer than *width* are hard-broken.
    """
    width = max(1, width)
    if display_width(s) <= width and "\n" not in s:
        return [s]
    rows: list[str] = []
    for para in s.split("\n"):
        rows.extend(_wrap_one(para, width))
    return rows


def hard_wrap_ansi(s: str, width: int, first_width: int | None = None) -> list[str]:
    """Break a styled single-line string every *width* columns (no word boundaries, e.g. code).

    *first_width* (default *width*) limits the first row.  Like :func:`wrap_ansi`, SGR styles and
    OSC 8 links are closed at each row end and re-opened on the next row.
    """
    width = max(1, width)
    limit = max(1, first_width if first_width is not None else width)
    if display_width(s) <= limit:
        return [s]
    rows: list[str] = []
    styles: list[str] = []
    link = ""
    cur: list[str] = []
    cur_w = 0
    for is_esc, tok in _tokens(s):
        if is_esc:
            cur.append(tok)
            if tok.startswith("\x1b]8;"):
                link = "" if tok in ("\x1b]8;;\x1b\\", "\x1b]8;;\x07") else tok
            elif tok.startswith("\x1b[") and tok.endswith("m"):
                if tok in ("\x1b[0m", "\x1b[m"):
                    styles.clear()
                else:
                    styles.append(tok)
            continue
        w = char_width(tok)
        if cur_w + w > limit and cur_w > 0:
            row = "".join(cur) + ("\x1b]8;;\x1b\\" if link else "") + ("\033[0m" if styles else "")
            rows.append(row)
            cur = list(styles) + ([link] if link else [])
            cur_w = 0
            limit = width
        cur.append(tok)
        cur_w += w
    rows.append("".join(cur))
    return rows


def _wrap_one(s: str, width: int) -> list[str]:
    rows: list[str] = []
    styles: list[str] = []   # active SGR codes
    link: str = ""           # active OSC 8 opener
    cur: list[str] = []
    cur_w = 0
    word: list[tuple[bool, str]] = []
    word_w = 0

    def reopen() -> list[str]:
        return list(styles) + ([link] if link else [])

    def close_row() -> None:
        nonlocal cur, cur_w
        row = "".join(cur).rstrip(" ")
        if link:
            row += "\x1b]8;;\x1b\\"
        if styles:
            row += "\033[0m"
        rows.append(row)
        cur = reopen()
        cur_w = 0

    def apply_escape(tok: str) -> None:
        nonlocal link
        if tok.startswith("\x1b]8;"):
            link = "" if tok in ("\x1b]8;;\x1b\\", "\x1b]8;;\x07") else tok
        elif tok.endswith("m") and tok.startswith("\x1b["):
            if tok in ("\x1b[0m", "\x1b[m"):
                styles.clear()
            else:
                styles.append(tok)

    def flush_word() -> None:
        nonlocal word, word_w, cur_w
        if not word:
            return
        if cur_w + word_w > width and cur_w > 0:
            close_row()
        for is_esc, tok in word:
            if is_esc:
                cur.append(tok)
                apply_escape(tok)
                continue
            w = char_width(tok)
            if cur_w + w > width and cur_w > 0:
                close_row()
            cur.append(tok)
            cur_w += w
        word = []
        word_w = 0

    for is_esc, tok in _tokens(s):
        if is_esc:
            word.append((True, tok))
        elif tok == " ":
            flush_word()
            if cur_w + 1 <= width:
                cur.append(" ")
                cur_w += 1
            else:
                close_row()
        else:
            word.append((False, tok))
            word_w += char_width(tok)
    flush_word()
    row = "".join(cur).rstrip(" ")
    if link:
        row += "\x1b]8;;\x1b\\"
    if styles:
        row += "\033[0m"
    rows.append(row)
    return rows


# ─── Terminal geometry ─────────────────────────────────────────────────────

def term_width() -> int:
    try:
        cols = os.get_terminal_size().columns
        return cols if cols > 0 else 88
    except Exception:
        try:
            return max(20, int(os.environ.get("COLUMNS", "88")))
        except ValueError:
            return 88


_MAX_LAYOUT_WIDTH = 0     # 0 = use the whole terminal width (setting max_width / AXON_MAX_WIDTH)


def set_max_layout_width(cols: int | None) -> None:
    global _MAX_LAYOUT_WIDTH
    try:
        _MAX_LAYOUT_WIDTH = max(0, int(cols or 0))
    except (TypeError, ValueError):
        _MAX_LAYOUT_WIDTH = 0


def layout_width(margin: int = 2) -> int:
    """Columns available to Axon's output: the full window (minus a small margin, so the terminal never
    has to re-wrap a row), optionally capped by the max_width setting."""
    cols = term_width() - margin
    cap = _MAX_LAYOUT_WIDTH
    env = os.environ.get("AXON_MAX_WIDTH", "")
    if env.isdigit() and int(env) > 0:
        cap = int(env)
    return max(20, min(cols, cap) if cap > 0 else cols)


def term_height() -> int:
    try:
        lines = os.get_terminal_size().lines
        return lines if lines > 0 else 24
    except Exception:
        return 24


# ─── Glyphs with ASCII fallback ────────────────────────────────────────────
# name -> (unicode, ascii)
GLYPHS: dict[str, tuple[str, str]] = {
    "tool": ("●", "*"),
    "result": ("⎿", "L"),
    "ok": ("✓", "+"),
    "fail": ("✗", "x"),
    "warn": ("⚠", "!"),
    "info": ("ℹ", "i"),
    "arrow": ("→", "->"),
    "down": ("↓", "v"),
    "enter": ("⏎", "Enter"),
    "flow": ("➔", "->"),
    "wrap": ("↪", ">"),
    "chevron": ("›", ">"),
    "prompt": ("❯", ">"),
    "ellipsis": ("…", "..."),
    "dot": ("·", "-"),
    "bullet": ("•", "-"),
    "bullet2": ("◦", "-"),
    "bullet3": ("▪", "-"),
    "star": ("✻", "*"),
    "diamond": ("◆", "#"),
    "circle": ("○", "o"),
    "play": ("▶", ">"),
    "block": ("█", "#"),
    "shade": ("░", "."),
    "quote": ("▎", "|"),
    "task_done": ("☑", "[x]"),
    "task_todo": ("☐", "[ ]"),
    "h": ("─", "-"),
    "hh": ("━", "="),
    "v": ("│", "|"),
    "tl": ("╭", "+"),
    "tr": ("╮", "+"),
    "bl": ("╰", "+"),
    "br": ("╯", "+"),
    "sq_tl": ("┌", "+"),
    "sq_tr": ("┐", "+"),
    "sq_bl": ("└", "+"),
    "sq_br": ("┘", "+"),
    "t_down": ("┬", "+"),
    "t_up": ("┴", "+"),
    "t_right": ("├", "+"),
    "t_left": ("┤", "+"),
    "cross": ("┼", "+"),
    "branch": ("├─", "|-"),
    "branch_end": ("└─", "`-"),
    "git": ("⎇", "git:"),
    "thinking_bar": ("┊", ":"),
    "logo_top": ("▲█▲", "/^\\"),
    "logo_mid": ("█⚡█", "|A|"),
    "logo_bot": ("▼█▼", "\\_/"),
    "spinner": ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "|/-\\"),
    # cards (tool results, code blocks, charts)
    "created": ("✚", "+"),
    "edited": ("✎", "~"),
    "overwrote": ("⟳", "*"),
    "deleted": ("✖", "x"),
    "renamed": ("➜", ">"),
    "minus": ("−", "-"),
    "chip": ("■", ""),
    "hunk": ("┈", "-"),
    "fail_cell": ("▒", "x"),
    "eighths": ("▏▎▍▌▋▊▉█", "########"),
    "spark": ("▁▂▃▄▅▆▇█", "_.-:=+*#"),
    "dir_mark": ("▸", ">"),
}

# Emoji glyphs: only on terminals known to render them (falls back to plain text otherwise).
EMOJI: dict[str, tuple[str, str]] = {
    "python": ("🐍", ""),
    "config": ("⚙️", ""),
    "native": ("⚡", ""),
    "doc": ("📄", ""),
    "web": ("🌐", ""),
    "script": ("📜", ""),
    "folder": ("📁", ""),
    "search": ("🔍", ""),
    "brain": ("🧠", ""),
    "chart": ("📊", ""),
    "inbox": ("📥", ""),
    "outbox": ("📤", ""),
    "chat": ("💬", ""),
    "bulb": ("💡", ""),
    "robot": ("🤖", ""),
    "target": ("🎯", ""),
    "pin": ("📍", ""),
    "link": ("🔗", ""),
    "lock": ("🔒", ""),
    "question": ("❓", ""),
    "clipboard": ("📋", ""),
    "tools": ("🛠️", ""),
    "gear": ("⚙️", ""),
}


def _encoding() -> str:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    return enc.lower()


@functools.lru_cache(maxsize=1024)
def _can_encode(text: str, encoding: str) -> bool:
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def supports_unicode() -> bool:
    return _can_encode("╭─●⎿✓…", _encoding())


@functools.lru_cache(maxsize=8)
def _emoji_ok(encoding: str, ascii_env: str) -> bool:
    if ascii_env:
        return False
    if not _can_encode("🐍📁", encoding):
        return False
    if os.name == "nt":
        # Legacy conhost renders emoji as tofu; Windows Terminal / VS Code / ConEmu render them.
        env = os.environ
        return bool(env.get("WT_SESSION") or env.get("TERM_PROGRAM") or env.get("ConEmuANSI") == "ON")
    return True


def supports_emoji() -> bool:
    return _emoji_ok(_encoding(), os.environ.get("AXON_ASCII", ""))


def glyph(name: str) -> str:
    """Return the Unicode glyph *name*, or its ASCII fallback when stdout can't encode it."""
    if name in EMOJI:
        uni, asc = EMOJI[name]
        return uni if supports_emoji() else asc
    pair = GLYPHS.get(name)
    if pair is None:
        return name
    uni, asc = pair
    if os.environ.get("AXON_ASCII"):
        return asc
    return uni if _can_encode(uni, _encoding()) else asc


def emoji(name: str, fallback: str = "") -> str:
    """Return emoji *name* (with trailing space) when supported, else *fallback*."""
    e = glyph(name)
    return f"{e} " if e else fallback


def safe_text(text: str) -> str:
    """Make *text* encodable by the current stdout (replaces unencodable characters)."""
    enc = _encoding()
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        try:
            return text.encode(enc, errors="replace").decode(enc, errors="replace")
        except LookupError:
            return text.encode("ascii", errors="replace").decode("ascii")


# ─── Hyperlinks ────────────────────────────────────────────────────────────

def supports_hyperlinks() -> bool:
    """Best-effort OSC 8 support detection (disabled without colour / on legacy consoles)."""
    if os.environ.get("AXON_NO_HYPERLINKS"):
        return False
    if os.environ.get("AXON_HYPERLINKS"):
        return True
    if not COLOR_ENABLED:
        return False
    if os.name == "nt" and not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")):
        return False
    return os.environ.get("TERM", "") not in ("dumb", "linux")


def hyperlink(target: str, label: str) -> str:
    """Wrap *label* in an OSC 8 hyperlink to *target* when supported."""
    if not target or not supports_hyperlinks():
        return label
    return f"\x1b]8;;{target}\x1b\\{label}\x1b]8;;\x1b\\"
