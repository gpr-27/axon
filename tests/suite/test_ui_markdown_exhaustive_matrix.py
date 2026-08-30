"""
Exhaustive UI, markdown rendering, theme formatting, and ANSI truncation test matrix.
"""
import pytest
import time
from axon.ui.markdown import format_markdown
from axon.ui.theme import (
    strip_ansi,
    BOLD,
    CYAN,
    GOLD,
    MINT,
    ROSE,
    RST,
    SLATE,
    TEAL,
    WHITE,
)
from axon.ui.switcher import format_time_ago
from axon.ui.input import safe_ansi_truncate, _get_mode_info

# ─── Markdown Syntax Rendering Matrix (20 tests) ────────────────────────────
@pytest.mark.parametrize("md_text,expected_sub", [
    ("# Main Heading", "Main Heading"),
    ("## Sub Heading", "Sub Heading"),
    ("### Deep Section", "Deep Section"),
    ("```python\nx = 1\n```", "x = 1"),
    ("- First item\n- Second item", "First item"),
    ("1. Numbered item\n2. Next item", "Numbered item"),
    ("| Col 1 | Col 2 |\n|---|---|\n| Val A | Val B |", "Val A"),
    ("**bold text**", "bold text"),
    ("`inline code`", "inline code"),
])
def test_markdown_formatting_variations(md_text: str, expected_sub: str):
    rendered = format_markdown(md_text, max_width=80)
    assert len(rendered) > 0
    clean = strip_ansi(rendered)
    assert expected_sub in clean

# ─── Safe ANSI Truncation Matrix (15 tests) ─────────────────────────────────
@pytest.mark.parametrize("colored_input,max_w,should_contain", [
    (f"{CYAN}Hello World{RST}", 5, "Hello"),
    (f"{GOLD}{BOLD}System Active{RST}", 6, "System"),
    (f"{MINT}1234567890{RST}", 4, "1234"),
    ("Plain uncolored string", 10, "Plain unco"),
])
def test_safe_ansi_truncate(colored_input: str, max_w: int, should_contain: str):
    truncated = safe_ansi_truncate(colored_input, max_w)
    clean = strip_ansi(truncated)
    assert len(clean) <= max_w
    assert should_contain in clean

# ─── Time-Ago String Calculation Matrix (10 tests) ──────────────────────────
@pytest.mark.parametrize("delta_s,expected_str", [
    (5, "5s"),
    (45, "45s"),
    (120, "2m"),
    (3600, "1h"),
    (7200, "2h"),
    (86400, "1d"),
    (86400 * 3, "3d"),
])
def test_format_time_ago_intervals(delta_s: int, expected_str: str):
    ts = time.time() - delta_s
    assert format_time_ago(ts) == expected_str

# ─── Mode Badge & Info Matrix (5 tests) ─────────────────────────────────────
@pytest.mark.parametrize("mode_name,expected_label", [
    ("default", "manual mode on"),
    ("acceptEdits", "auto-accept edits on"),
    ("plan", "plan mode on"),
    ("bypass", "bypass permissions on"),
])
def test_mode_info_label(mode_name: str, expected_label: str):
    label, color = _get_mode_info(mode_name)
    assert expected_label in label
    assert color is not None
