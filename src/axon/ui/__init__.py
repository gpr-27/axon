"""
UI package exports. Importing the UI prepares the console (UTF-8 streams, Windows VT mode).
"""
import sys as _sys

from axon import platform as _platform

try:
    if _sys.stdout is not None and _sys.stdout.isatty():
        _platform.setup_console()
except Exception:
    pass

from axon.ui.theme import (
    RST,
    BOLD,
    DIM,
    WHITE,
    LBLUE,
    MINT,
    GOLD,
    ROSE,
    SLATE,
    PURPLE,
    TEAL,
    CYAN,
    strip_ansi,
    term_width,
)
from axon.ui.input import read_input
from axon.ui.picker import pick
from axon.ui.approve import ask_approval
from axon.ui.render import Renderer

__all__ = [
    "RST",
    "BOLD",
    "DIM",
    "WHITE",
    "LBLUE",
    "MINT",
    "GOLD",
    "ROSE",
    "SLATE",
    "PURPLE",
    "TEAL",
    "CYAN",
    "strip_ansi",
    "term_width",
    "read_input",
    "pick",
    "ask_approval",
    "Renderer",
]
