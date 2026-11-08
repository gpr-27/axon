"""The Axon identity, shared by the terminal and the browser.

The mark is a firing neuron: dendrites, a cell body, a myelinated axon and its synapse. Signal aqua
carries the impulse and synapse pink marks where it lands. Colours live in the theme palettes, so
every surface reads the same values from here.
"""
from __future__ import annotations

from axon.ui import theme

LOGO_WIDTH = 13

# (dendrites and axon, cell body and synapse), split so each part takes its own colour.
_UNICODE_ROWS = (
    ((" ╲│╱", ""), ("", "        ╱")),
    (("──", "●"), ("━━╸━━╸━━╸", "─")),
    ((" ╱│╲", ""), ("", "        ╲")),
)
_ASCII_ROWS = (
    ((" \\|/", ""), ("", "        /")),
    (("--", "O"), ("==-==-==-", "-")),
    ((" /|\\", ""), ("", "        \\")),
)

# Page backgrounds for the browser; a terminal keeps its own background.
BROWSER_BG = {
    "axon": {"page": "#0B1117", "surface": "#121A22", "raised": "#1A242E"},
    "axon-light": {"page": "#F6F8F9", "surface": "#FFFFFF", "raised": "#EAF0F2"},
}

LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 48" role="img" aria-label="Axon">'
    '<title>Axon</title>'
    '<g fill="none" stroke="var(--axon-primary, #38E8C6)" stroke-width="3" stroke-linecap="round">'
    '<path d="M6 24H16M9 9L18 19M18 5V17M29 9L22 19M9 39L18 29M18 43V31M29 39L22 29"/>'
    '<path d="M32 24H40M45 24H55M60 24H70M75 24H81" stroke-width="5"/>'
    '</g>'
    '<g fill="none" stroke="var(--axon-accent, #FF6FB5)" stroke-width="3" stroke-linecap="round">'
    '<path d="M81 24L91 13M81 24L92 24M81 24L91 35"/>'
    '</g>'
    '<circle cx="19" cy="24" r="7" fill="var(--axon-accent, #FF6FB5)"/>'
    '</svg>'
)


def logo_rows() -> list[str]:
    """The three rows of the terminal mark, coloured with the live theme, each LOGO_WIDTH columns wide."""
    rows = _UNICODE_ROWS if theme.supports_unicode() else _ASCII_ROWS
    out = []
    for (left, soma), (axon, synapse) in rows:
        plain = left + soma + axon + synapse
        out.append(
            f"{theme.PRIMARY}{left}{theme.RST}"
            f"{theme.ACCENT}{theme.BOLD}{soma}{theme.RST}"
            f"{theme.PRIMARY}{axon}{theme.RST}"
            f"{theme.ACCENT}{synapse}{theme.RST}"
            + " " * (LOGO_WIDTH - len(plain))
        )
    return out


def palette_hex(name: str = "axon") -> dict[str, str]:
    """Every colour token of a theme as ``{TOKEN: "#rrggbb"}``, for the browser and shared exports."""
    spec = dict(theme._DARK)
    spec.update(theme._THEME_SPECS.get(name, {}))
    return {token: "#{:02X}{:02X}{:02X}".format(*rgb) for token, (rgb, _c256) in spec.items()}


def browser_css_vars(name: str = "axon") -> str:
    """CSS custom properties for a theme: ``--axon-primary``, ``--axon-page``, and so on."""
    tokens = {**{k.lower().replace("_", "-"): v for k, v in palette_hex(name).items()}, **BROWSER_BG[name]}
    return "".join(f"--axon-{k}:{v};" for k, v in sorted(tokens.items()))
