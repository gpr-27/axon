"""
Interactive Fuzzy File Finder (Ctrl+P / /find) for Axon.
Provides ultra-fast fuzzy file searching with instant live preview and keyboard navigation.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Any

from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, MINT, ROSE, RST, SLATE, TEAL, WHITE,
    strip_ansi, term_width,
)


def _fuzzy_score(query: str, target: str) -> float:
    """Calculate fuzzy match score for a target path string."""
    q = query.lower()
    t = target.lower()
    if not q:
        return 1.0
    if q in t:
        # Direct substring bonus (higher if near filename)
        name = Path(target).name.lower()
        if q in name:
            return 10.0 + (1.0 / (name.find(q) + 1))
        return 5.0 + (1.0 / (t.find(q) + 1))

    # Subsequence matching
    score = 0.0
    t_idx = 0
    consecutive = 0
    for ch in q:
        found = t.find(ch, t_idx)
        if found == -1:
            return 0.0
        if found == t_idx:
            consecutive += 1
            score += 2.0 * consecutive
        else:
            consecutive = 0
            score += 1.0 / (found - t_idx + 1)
        t_idx = found + 1

    return score


def run_fuzzy_file_finder(workspace: Path) -> str | None:
    """
    Launch interactive fullscreen or popup fuzzy file finder.
    Returns relative path of selected file, or None if cancelled.
    """
    if not sys.stdin.isatty():
        return None

    try:
        import termios
        import tty
    except ImportError:
        return None

    # Discover all workspace files, ignoring build artifacts, venvs, and VCS dirs
    ignore_dirs = {
        ".git", ".axon", "node_modules", "__pycache__", "venv", ".venv",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
        "axon_gpr.egg-info", ".eggs", "target", "out",
    }
    ignore_exts = {".whl", ".tar.gz", ".tgz", ".zip", ".pyc", ".so", ".dylib", ".exe", ".bin"}

    all_files: list[str] = []
    for f in workspace.rglob("*"):
        if f.is_file():
            parts = f.relative_to(workspace).parts
            if not any(p.startswith(".") or p in ignore_dirs for p in parts):
                ext = "".join(f.suffixes).lower()
                if ext not in ignore_exts and f.suffix.lower() not in ignore_exts:
                    all_files.append(f.relative_to(workspace).as_posix())

    if not all_files:
        return None

    all_files.sort()

    query: list[str] = []
    selected_idx: int = 0
    max_visible: int = 10

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)

    def _get_matches() -> list[tuple[float, str]]:
        q_str = "".join(query)
        if not q_str:
            return [(1.0, f) for f in all_files[:40]]
        scored = []
        for f in all_files:
            score = _fuzzy_score(q_str, f)
            if score > 0:
                scored.append((score, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:40]

    def _draw():
        tw = term_width()
        width = max(65, min(90, tw - 6))
        matches = _get_matches()
        nonlocal selected_idx
        if not matches:
            selected_idx = 0
        else:
            selected_idx = max(0, min(selected_idx, len(matches) - 1))

        q_str = "".join(query)
        header = "🔍 Find File · (↑/↓ Navigate · Enter Open · Esc Cancel)"
        
        # Exact width calculation
        inner_w = width - 4
        hdr_display = f" {header} "
        border_r = max(2, inner_w - len(header) - 2)

        lines = [
            f"\r\033[J  {DARK_SLATE}╭──{GOLD}{hdr_display}{DARK_SLATE}{'─' * border_r}╮{RST}",
            f"  {DARK_SLATE}│{RST}  {CYAN}Search:{RST} {WHITE}{BOLD}{q_str}{RST}\033[7m \033[0m{' ' * max(0, inner_w - len(q_str) - 10)}{DARK_SLATE}│{RST}",
            f"  {DARK_SLATE}├──{'─' * inner_w}┤{RST}",
        ]

        if not matches:
            lines.append(f"  {DARK_SLATE}│{RST}  {SLATE}No matching files found.{' ' * max(0, inner_w - 25)}{DARK_SLATE}│{RST}")
        else:
            start_v = max(0, min(selected_idx - max_visible // 2, len(matches) - max_visible))
            end_v = min(len(matches), start_v + max_visible)
            for idx in range(start_v, end_v):
                score, f_path = matches[idx]
                is_sel = (idx == selected_idx)
                prefix = f"{MINT}▶{RST}" if is_sel else " "
                
                # Truncate long paths cleanly
                max_path_len = inner_w - 8
                if len(f_path) > max_path_len:
                    disp = "…" + f_path[-(max_path_len - 1):]
                else:
                    disp = f_path
                pad = max(0, inner_w - len(disp) - 6)
                name_str = f"{WHITE}{BOLD}{disp}{RST}" if is_sel else f"{SLATE}{disp}{RST}"
                lines.append(f"  {DARK_SLATE}│{RST} {prefix} 📄 {name_str}{' ' * pad}{DARK_SLATE}│{RST}")

        lines.append(f"  {DARK_SLATE}╰──{'─' * (inner_w - 2)}╯{RST}")
        sys.stdout.write("\n".join(lines))
        lines_up = len(lines) - 1
        sys.stdout.write(f"\033[{lines_up}A\r")
        sys.stdout.flush()

    try:
        tty.setcbreak(fd)
        _draw()
        while True:
            raw = os.read(fd, 1024)
            if not raw:
                break

            # Enter
            if raw in (b"\r", b"\n"):
                matches = _get_matches()
                if matches and 0 <= selected_idx < len(matches):
                    sys.stdout.write("\033[J\n")
                    sys.stdout.flush()
                    return matches[selected_idx][1]
                sys.stdout.write("\033[J\n")
                sys.stdout.flush()
                return None

            # Esc or Ctrl+C
            if raw in (b"\x1b", b"\x03"):
                sys.stdout.write("\033[J\n")
                sys.stdout.flush()
                return None

            # Up Arrow
            if raw.startswith((b"\x1b[A", b"\x1bOA")):
                selected_idx = max(0, selected_idx - 1)
                _draw()
                continue

            # Down Arrow
            if raw.startswith((b"\x1b[B", b"\x1bOB")):
                matches = _get_matches()
                selected_idx = min(len(matches) - 1, selected_idx + 1)
                _draw()
                continue

            # Backspace
            if raw in (b"\x7f", b"\x08"):
                if query:
                    query.pop()
                    selected_idx = 0
                _draw()
                continue

            # Printable characters
            if len(raw) == 1 and 32 <= raw[0] <= 126:
                query.append(raw.decode("ascii"))
                selected_idx = 0
                _draw()
                continue

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)

    return None
