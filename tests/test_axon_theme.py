"""The Axon theme: default in every terminal, same colours in the browser, mark aligned everywhere."""
from __future__ import annotations

import pytest

from axon.ui import brand, theme


@pytest.fixture
def colors(monkeypatch):
    monkeypatch.setattr(theme, "COLOR_ENABLED", True)
    monkeypatch.setattr(theme, "COLOR_LEVEL", 2)
    theme.set_theme("axon")
    yield
    monkeypatch.setattr(theme, "COLOR_ENABLED", False)
    theme.set_theme("axon")


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        vals = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, bl = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("env,expected", [("15;0", "axon"), ("0;15", "axon-light"), ("", "axon")])
def test_auto_follows_the_terminal_background(monkeypatch, env: str, expected: str):
    monkeypatch.setenv("COLORFGBG", env)
    assert theme.resolve_theme("auto") == expected


def test_default_name_is_axon():
    assert theme.resolve_theme("default") == "axon"


@pytest.mark.parametrize("name", ["dark", "light", "high-contrast", "mono"])
def test_existing_themes_still_work(name: str):
    assert theme.resolve_theme(name) == name


def test_axon_palette_is_its_own(colors):
    assert theme.PRIMARY == "\033[38;2;56;232;198m"
    assert theme.ACCENT == "\033[38;2;255;111;181m"
    assert theme.TEAL == theme.PRIMARY


def test_every_theme_defines_the_same_tokens():
    assert set(brand.palette_hex("axon")) == set(brand.palette_hex("axon-light"))


@pytest.mark.parametrize("name,page", [("axon", "#0B1117"), ("axon-light", "#FFFFFF")])
@pytest.mark.parametrize("token,minimum", [("TEXT", 7.0), ("PRIMARY", 4.5), ("ACCENT", 4.5), ("SUCCESS", 4.5),
                                           ("WARNING", 4.5), ("ERROR", 4.5), ("INFO", 4.5), ("LINK", 4.5), ("MUTED", 4.5)])
def test_text_colours_are_readable(name: str, page: str, token: str, minimum: float):
    assert _contrast(brand.palette_hex(name)[token], page) >= minimum


@pytest.mark.parametrize("unicode_ok", [True, False])
def test_logo_rows_align(monkeypatch, unicode_ok: bool):
    monkeypatch.setattr(theme, "supports_unicode", lambda: unicode_ok)
    rows = brand.logo_rows()
    assert len(rows) == 3
    assert [theme.display_width(r) for r in rows] == [brand.LOGO_WIDTH] * 3


def test_ascii_logo_is_ascii(monkeypatch):
    monkeypatch.setattr(theme, "supports_unicode", lambda: False)
    assert all(theme.strip_ansi(r).isascii() for r in brand.logo_rows())


def test_logo_follows_the_live_theme(colors):
    dark = brand.logo_rows()
    theme.set_theme("axon-light")
    assert brand.logo_rows() != dark


def test_browser_css_carries_the_same_colours():
    css = brand.browser_css_vars("axon")
    assert "--axon-primary:#38E8C6;" in css and "--axon-page:#0B1117;" in css


def test_svg_uses_the_theme_variables():
    assert "var(--axon-primary" in brand.LOGO_SVG and "var(--axon-accent" in brand.LOGO_SVG
    assert "<script" not in brand.LOGO_SVG


def test_axon_ascii_also_switches_the_logo(monkeypatch):
    monkeypatch.setenv("AXON_ASCII", "1")
    assert theme.supports_unicode() is False
    assert all(theme.strip_ansi(r).isascii() for r in brand.logo_rows())
