"""Theme: colour detection, NO_COLOR, widths, truncation, wrapping and ASCII glyph fallback."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from axon.ui import theme
from axon.ui.theme import (
    char_width,
    detect_color_level,
    display_width,
    glyph,
    pad,
    sanitize,
    strip_ansi,
    truncate,
    wrap_ansi,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def test_color_level_detection():
    tty = _Tty()
    assert detect_color_level({"NO_COLOR": "1"}, tty) == 0
    assert detect_color_level({"TERM": "dumb"}, tty) == 0
    assert detect_color_level({}, io.StringIO()) == 0  # not a TTY
    assert detect_color_level({"TERM": "xterm-256color"}, tty) == 1
    assert detect_color_level({"COLORTERM": "truecolor"}, tty) == 2
    assert detect_color_level({"COLORTERM": "24bit"}, tty) == 2
    assert detect_color_level({"FORCE_COLOR": "1"}, io.StringIO()) == 1
    assert detect_color_level({"FORCE_COLOR": "1", "COLORTERM": "truecolor"}, io.StringIO()) == 2


def _run(code: str, **env: str) -> str:
    full_env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "AXON_FORCE_COLOR", "COLORTERM")}
    full_env.update(env)
    full_env["PYTHONPATH"] = SRC
    full_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                          env=full_env, timeout=60).stdout


def test_no_color_and_forced_color_in_subprocess():
    code = "from axon.ui.markdown import format_markdown; print(format_markdown('# Hi\\n**bold** `code`'))"
    plain = _run(code, NO_COLOR="1")
    assert "\x1b[" not in plain and "Hi" in plain and "bold" in plain
    forced = _run(code, FORCE_COLOR="1")
    assert "\x1b[38;5;" in forced
    truecolor = _run(code, FORCE_COLOR="1", COLORTERM="truecolor")
    assert "\x1b[38;2;" in truecolor


def test_legacy_constants_exist():
    for name in ("RST", "BOLD", "DIM", "ITALIC", "UNDER", "WHITE", "LBLUE", "MINT", "GOLD", "ROSE", "SLATE",
                 "PURPLE", "TEAL", "CYAN", "GREEN", "RED", "TERRACOTTA", "ORANGE", "AMBER", "GRAY_BG",
                 "DARK_SLATE", "DARK_BORDER", "EMERALD", "CORAL", "LAVENDER", "INDIGO", "SKY", "BLUE_PILL",
                 "GREEN_PILL", "GOLD_PILL", "GRAY_PILL", "PRIMARY", "ACCENT", "SUCCESS", "WARNING", "ERROR",
                 "MUTED", "BORDER", "CODE_BG", "DIFF_ADD", "DIFF_DEL", "OUTPUT_LOCK"):
        assert hasattr(theme, name), name


@pytest.mark.parametrize("text,width", [
    ("a", 1), ("✓", 1), ("─", 1), ("✅", 2), ("中文", 4), ("⚙️", 2), ("é", 1),
    ("👨‍👩‍👧", 2), ("\x1b[1mhi\x1b[0m", 2), ("\x1b]8;;file:///x\x1b\\link\x1b]8;;\x1b\\", 4), ("", 0),
])
def test_display_width(text: str, width: int):
    assert display_width(text) == width


def test_char_width_basics():
    assert char_width("a") == 1
    assert char_width("́") == 0
    assert char_width("界") == 2


def test_truncate_keeps_styles_and_width():
    s = "\x1b[1mhello world\x1b[0m"
    t = truncate(s, 6)
    assert display_width(t) <= 6
    assert strip_ansi(t).startswith("hello")
    assert t.endswith("\x1b[0m")
    assert truncate("中文中文", 5) and display_width(truncate("中文中文", 5)) <= 5
    assert truncate("short", 10) == "short"
    assert truncate("anything", 0) == ""


def test_wrap_ansi_reopens_styles():
    rows = wrap_ansi("\x1b[1mone two three four five\x1b[0m six", 9)
    assert all(display_width(r) <= 9 for r in rows)
    assert all(r.startswith("\x1b[1m") for r in rows[:2])
    assert strip_ansi(" ".join(rows)).split() == "one two three four five six".split()
    long = wrap_ansi("x" * 25, 10)
    assert [display_width(r) for r in long] == [10, 10, 5]


def test_pad_and_sanitize():
    assert display_width(pad("中", 4)) == 4
    assert pad("ab", 4, "right") == "  ab"
    assert sanitize("ok\x1b[2Jbad\x07") == "ok[2Jbad"


def test_ascii_glyph_fallback_on_cp1252(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    assert glyph("tool") == "*"
    assert glyph("tl") == "+"
    assert glyph("h") == "-"
    assert glyph("spinner") == "|/-\\"
    assert glyph("python") == ""  # emoji never on cp1252
    from axon.ui.render import box, render_diff_box
    from axon.ui.markdown import format_markdown

    text = box("Title", ["row ✓"], 40) + format_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n- item") + render_diff_box("a", "b", "x.py")
    from axon.ui.theme import safe_text

    stream.write(safe_text(text))  # must not raise
    stream.flush()


def test_ascii_env_override(monkeypatch):
    monkeypatch.setenv("AXON_ASCII", "1")
    assert glyph("tool") == "*"
    assert glyph("result") == "L"
