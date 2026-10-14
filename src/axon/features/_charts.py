"""
Terminal chart primitives (stdlib only): sparklines, horizontal bars, stacked bars and
ANSI-aware aligned tables. Unicode block glyphs degrade to ASCII on consoles that can't
encode them (cp1252, ``AXON_ASCII=1``); colours come from :mod:`axon.ui.theme` when present.
"""
from __future__ import annotations

import os
import re
from typing import Any, Sequence

try:  # theme helpers are optional: charts must work even if the UI layer is unavailable
    from axon.ui import theme as _theme
except Exception:  # pragma: no cover - exercised only when the UI package is broken
    _theme = None  # type: ignore[assignment]

_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")

SPARK_UNICODE = "▁▂▃▄▅▆▇█"
SPARK_ASCII = "_.-:=+*#"
_EIGHTHS = " ▏▎▍▌▋▊▉"
# Distinct fill per stacked segment so the mix stays readable without colour.
STACK_UNICODE = ("█", "▓", "▒", "░", "▚", "▞")
STACK_ASCII = ("#", "=", "+", ".", "o", "~")


def style(name: str) -> str:
    """Theme colour/style escape by name ('' when colours are off or the theme is missing)."""
    if _theme is None:
        return ""
    return str(getattr(_theme, name, "") or "")


def rst() -> str:
    return style("RST")


def paint(text: str, *styles: str) -> str:
    """Wrap *text* in theme styles (names like 'PRIMARY', 'BOLD'); no-op without colour."""
    prefix = "".join(style(s) for s in styles)
    return f"{prefix}{text}{rst()}" if prefix else text


def unicode_ok() -> bool:
    if os.environ.get("AXON_ASCII"):
        return False
    if _theme is not None:
        try:
            return _theme.glyph("block") == "█"
        except Exception:
            pass
    return False


def glyph(name: str, fallback: str) -> str:
    if _theme is not None:
        try:
            return str(_theme.glyph(name))
        except Exception:
            pass
    return fallback


def display_width(s: str) -> int:
    if _theme is not None:
        try:
            return int(_theme.display_width(s))
        except Exception:
            pass
    return len(_ANSI_RE.sub("", s))


def pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - display_width(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if display_width(s) <= width:
        return s
    if _theme is not None:
        try:
            return str(_theme.truncate(s, width))
        except Exception:
            pass
    plain = _ANSI_RE.sub("", s)
    return plain[: max(0, width - 3)] + "..." if width > 3 else plain[:width]


def term_width(default: int = 100) -> int:
    if _theme is not None:
        try:
            return int(_theme.term_width())
        except Exception:
            pass
    try:
        return os.get_terminal_size().columns or default
    except OSError:
        return default


# ─── Sparkline ─────────────────────────────────────────────────────────────

def resample(values: Sequence[float], width: int) -> list[float]:
    """Shrink *values* to *width* buckets by summing neighbours (keeps totals meaningful)."""
    vals = [float(v or 0) for v in values]
    if width <= 0 or len(vals) <= width:
        return vals
    out = [0.0] * width
    for i, v in enumerate(vals):
        out[min(width - 1, i * width // len(vals))] += v
    return out


def sparkline(values: Sequence[float], width: int | None = None, ascii_only: bool | None = None) -> str:
    """One-line trend: zeros render as the lowest tick; the max as a full block."""
    vals = resample(values, width) if width else [float(v or 0) for v in values]
    if not vals:
        return ""
    ticks = SPARK_ASCII if (ascii_only if ascii_only is not None else not unicode_ok()) else SPARK_UNICODE
    hi = max(vals)
    lo = min(0.0, min(vals))
    if hi <= lo:
        return ticks[0] * len(vals)
    span = hi - lo
    n = len(ticks) - 1
    return "".join(ticks[0] if v <= 0 else ticks[max(1, round((v - lo) / span * n))] for v in vals)


# ─── Bars ──────────────────────────────────────────────────────────────────

def hbar(value: float, maximum: float, width: int, color: str = "PRIMARY", ascii_only: bool | None = None) -> str:
    """A horizontal bar of up to *width* cells (eighth-cell precision in Unicode mode)."""
    width = max(1, int(width))
    frac = 0.0 if maximum <= 0 else max(0.0, min(1.0, float(value) / float(maximum)))
    uni = unicode_ok() if ascii_only is None else not ascii_only
    if uni:
        eighths = int(round(frac * width * 8))
        full, rem = divmod(eighths, 8)
        body = "█" * full + (_EIGHTHS[rem] if rem else "")
        if not body and value > 0:
            body = "▏"
    else:
        full = int(round(frac * width))
        body = "#" * full
        if not body and value > 0:
            body = "|"
    return paint(body, color) + " " * (width - display_width(body))


def bar_chart(rows: Sequence[tuple[str, float, str]], width: int = 60, color: str = "PRIMARY",
              indent: int = 2) -> list[str]:
    """Labelled horizontal bars: rows of (label, value, value_text)."""
    if not rows:
        return []
    label_w = min(28, max(display_width(r[0]) for r in rows))
    text_w = max(display_width(r[2]) for r in rows)
    bar_w = max(8, width - indent - label_w - text_w - 4)
    hi = max(float(r[1]) for r in rows)
    out = []
    for label, value, text in rows:
        out.append(" " * indent + pad(truncate(label, label_w), label_w) + "  "
                   + hbar(value, hi, bar_w, color) + "  " + pad(text, text_w, "right"))
    return out


def stacked_bar(parts: Sequence[tuple[float, str]], width: int, total: float | None = None,
                ascii_only: bool | None = None) -> str:
    """Segments (value, colour-name) laid end to end; each segment also gets its own fill glyph."""
    width = max(1, int(width))
    vals = [max(0.0, float(v)) for v, _ in parts]
    tot = float(total) if total else sum(vals)
    if tot <= 0:
        return " " * width
    uni = unicode_ok() if ascii_only is None else not ascii_only
    fills = STACK_UNICODE if uni else STACK_ASCII
    # Largest-remainder rounding so the segments add up exactly to the filled width.
    filled = min(width, int(round(sum(vals) / tot * width)))
    raw = [v / sum(vals) * filled if sum(vals) else 0 for v in vals]
    cells = [int(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)
    for i in order[: filled - sum(cells)]:
        cells[i] += 1
    for i, v in enumerate(vals):  # a non-zero segment always shows at least one cell
        if v > 0 and cells[i] == 0 and filled:
            j = max(range(len(cells)), key=lambda k: cells[k])
            if cells[j] > 1:
                cells[j] -= 1
                cells[i] = 1
    out = "".join(paint(fills[i % len(fills)] * c, parts[i][1]) for i, c in enumerate(cells) if c)
    return out + " " * (width - filled)


def legend(items: Sequence[tuple[str, str]], ascii_only: bool | None = None) -> str:
    """Legend matching :func:`stacked_bar` fills: items of (label, colour-name)."""
    uni = unicode_ok() if ascii_only is None else not ascii_only
    fills = STACK_UNICODE if uni else STACK_ASCII
    return "   ".join(f"{paint(fills[i % len(fills)] * 2, c)} {label}" for i, (label, c) in enumerate(items))


# ─── Tables ────────────────────────────────────────────────────────────────

def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], aligns: str = "", indent: int = 2,
          max_width: int | None = None, gap: int = 2) -> list[str]:
    """Column-aligned table (ANSI and wide-char aware). ``aligns`` is a string of l/r/c per column.
    The first column shrinks (truncated with an ellipsis) when the table exceeds ``max_width``."""
    cols = len(headers)
    cells = [[str(c) for c in r] + [""] * (cols - len(r)) for r in rows]
    widths = [display_width(h) for h in headers]
    for r in cells:
        for i in range(cols):
            widths[i] = max(widths[i], display_width(r[i]))
    if max_width:
        excess = indent + sum(widths) + gap * (cols - 1) - max_width
        if excess > 0:
            widths[0] = max(8, widths[0] - excess)
    al = (aligns or "l" + "r" * (cols - 1)).ljust(cols, "r")
    mode = {"l": "left", "r": "right", "c": "center"}
    sep = " " * gap

    def line(vals: Sequence[str], hdr: bool = False) -> str:
        parts = [pad(truncate(v, widths[i]), widths[i], mode.get(al[i], "left")) for i, v in enumerate(vals)]
        text = sep.join(parts).rstrip()
        return " " * indent + (paint(text, "MUTED") if hdr else text)

    rule = glyph("h", "-")
    out = [line(list(headers), hdr=True),
           " " * indent + paint(sep.join(rule * w for w in widths), "BORDER")]
    out.extend(line(r) for r in cells)
    return out


__all__ = [
    "sparkline", "resample", "hbar", "bar_chart", "stacked_bar", "legend", "table", "paint", "style",
    "display_width", "pad", "truncate", "term_width", "unicode_ok",
]
