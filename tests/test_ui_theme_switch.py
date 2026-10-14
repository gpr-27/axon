"""Theme switching: named palettes, auto detection and live rebinding in importing modules."""
from __future__ import annotations

import pytest

from axon.ui import theme


@pytest.fixture
def colors(monkeypatch):
    """Pretend we are on a truecolor terminal; restore the colourless palette afterwards."""
    monkeypatch.setattr(theme, "COLOR_ENABLED", True)
    monkeypatch.setattr(theme, "COLOR_LEVEL", 2)
    theme.set_theme("dark")
    yield
    theme.set_theme("dark")
    monkeypatch.setattr(theme, "COLOR_ENABLED", False)
    theme.set_theme("dark")


def test_resolve_and_detect(monkeypatch):
    assert theme.resolve_theme("High_Contrast") == "high-contrast"
    assert theme.resolve_theme("monochrome") == "mono"
    with pytest.raises(ValueError):
        theme.resolve_theme("neon")
    assert theme.detect_background({"COLORFGBG": "0;15"}) == "light"
    assert theme.detect_background({"COLORFGBG": "15;0"}) == "dark"
    assert theme.detect_background({}) == "dark"
    monkeypatch.setenv("COLORFGBG", "0;7")
    assert theme.resolve_theme("auto") == "light"


def test_palettes_differ_and_mono_is_plain(colors):
    dark_gold = theme.GOLD
    assert dark_gold.startswith("\033[38;2;")
    assert theme.set_theme("light") == "light" and theme.current_theme() == "light"
    assert theme.GOLD != dark_gold and theme.WARNING == theme.GOLD
    theme.set_theme("high-contrast")
    assert theme.TEXT == "\033[38;2;255;255;255m"
    theme.set_theme("mono")
    assert theme.GOLD == "" and theme.CODE_BG == "" and theme.GRAY_PILL == "\033[7m"


def test_live_rebinding_in_importers(colors):
    import axon.ui.input as ui_input
    import axon.ui.render as render
    old = render.WARNING
    theme.set_theme("light")
    assert render.WARNING == theme.WARNING != old
    assert ui_input.DARK_SLATE == theme.SUBTLE
    theme.set_theme("dark")
    assert render.WARNING == old


def test_no_colour_terminal_switch_is_harmless():
    assert theme.set_theme("light") == "light"
    assert theme.GOLD == "" or theme.COLOR_ENABLED
    theme.set_theme("dark")
