"""
ANSI styling and color constants for Axon terminal rendering.
"""
import os
import re
import sys

# Enable ANSI escape processing on Windows 10/11 CMD and PowerShell
if os.name == "nt":
    try:
        os.system("")
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h_stdout = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(h_stdout, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h_stdout, mode.value | 0x0004 | 0x0008)
    except Exception:
        pass


RST    = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
ITALIC = "\033[3m"
UNDER  = "\033[4m"

WHITE  = "\033[97m"
LBLUE  = "\033[94m"
MINT   = "\033[38;5;120m"
GOLD   = "\033[38;5;220m"
ROSE   = "\033[38;5;210m"
SLATE  = "\033[38;5;246m"
PURPLE = "\033[38;5;183m"
TEAL   = "\033[38;5;80m"
CYAN   = "\033[96m"
GREEN  = "\033[32m"
RED    = "\033[31m"
TERRACOTTA = "\033[38;5;209m"
ORANGE = "\033[38;5;215m"
AMBER  = "\033[38;5;214m"
GRAY_BG = "\033[48;5;236m"
DARK_SLATE = "\033[38;5;240m"

def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and OSC 8 hyperlinks."""
    t = re.sub(r"\x1b\]8;;.*?(?:\x1b\\|\x07)", "", text)
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", t)

def term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 88

def term_height() -> int:
    try:
        return os.get_terminal_size().lines
    except Exception:
        return 24
