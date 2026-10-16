"""
Terminal charts for markdown answers: ```chart / ```bar / ```sparkline fenced blocks.

The block body may be

* ``label: value`` lines (an optional ``title: ...`` line names the chart),
* CSV (``label,value``; a non-numeric first row is a header; the first numeric column is plotted),
* a markdown table (first text column = labels, first numeric column = values),
* JSON: ``{"labels": [...], "values": [...]}`` (optionally ``"title"``), ``{"label": value, ...}``
  or ``[["label", value], ...]``.

Bars use Unicode eighth blocks (ASCII ``#`` fallback), sparklines the ``▁▂▃▄▅▆▇█`` ramp.  Every row
fits the given width; nothing here raises on malformed input (``render_chart`` returns ``None``
so the caller can show the block as plain code instead).
"""
from __future__ import annotations

import json
import math
import re

from axon.ui.theme import (
    ACCENT,
    BOLD,
    BORDER,
    ERROR,
    MUTED,
    PRIMARY,
    RST,
    SUBTLE,
    TEXT,
    display_width,
    glyph,
    pad,
    truncate,
)

CHART_LANGS = {"chart", "bar", "bars", "barchart", "bar-chart", "hbar", "sparkline", "spark", "sparklines"}
_NUM_RE = re.compile(r"^\s*[-+]?[$€£¥]?\s*[-+]?(?:\d{1,3}(?:[,_]\d{3})+|\d+)?(?:\.\d+)?\s*(?:%|[kKmMbB]|ms|s|x)?\s*$")


def parse_number(text: str) -> float | None:
    """'1,234.5' / '$12' / '42%' / '3.2k' / '-7' -> float; None when *text* is not a number."""
    s = str(text).strip().strip("*`_")
    if not s or not _NUM_RE.match(s) or not re.search(r"\d", s):
        return None
    mult = 1.0
    low = s.lower()
    if low.endswith("ms"):
        s = s[:-2]
    elif low.endswith(("k", "m", "b")) and not low.endswith("%"):
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[low[-1]]
        s = s[:-1]
    elif low.endswith(("s", "x", "%")):
        s = s[:-1]
    s = re.sub(r"[$€£¥,_\s]", "", s)
    try:
        return float(s) * mult
    except ValueError:
        return None


def _from_json(body: str) -> tuple[str, list[str], list[float]] | None:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    title = ""
    if isinstance(data, dict):
        title = str(data.get("title") or "")
        if "values" in data or "data" in data:
            vals = data.get("values", data.get("data"))
            if isinstance(vals, dict):
                return title, [str(k) for k in vals], [float(v) for v in vals.values() if parse_number(str(v)) is not None]
            labels = data.get("labels") or [str(i + 1) for i in range(len(vals or []))]
            nums = [parse_number(str(v)) for v in (vals or [])]
            if nums and all(n is not None for n in nums):
                return title, [str(x) for x in labels][: len(nums)], [float(n) for n in nums if n is not None]
            return None
        pairs = [(str(k), parse_number(str(v))) for k, v in data.items() if k != "title"]
        if pairs and all(v is not None for _, v in pairs):
            return title, [k for k, _ in pairs], [float(v) for _, v in pairs if v is not None]
        return None
    if isinstance(data, list) and data:
        if all(isinstance(x, (int, float)) for x in data):
            return "", [str(i + 1) for i in range(len(data))], [float(x) for x in data]
        labels, values = [], []
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                n = parse_number(str(item[1]))
                if n is None:
                    return None
                labels.append(str(item[0]))
                values.append(n)
            elif isinstance(item, dict) and len(item) >= 2:
                vals = list(item.values())
                n = next((parse_number(str(v)) for v in vals[1:] if parse_number(str(v)) is not None), None)
                if n is None:
                    return None
                labels.append(str(vals[0]))
                values.append(n)
            else:
                return None
        return "", labels, values
    return None


def _from_rows(rows: list[list[str]]) -> tuple[str, list[str], list[float]] | None:
    """Labels + the first numeric column of a table-like row list (an optional header row)."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    title = ""
    header = None
    if len(rows) > 1 and all(parse_number(c) is None for c in rows[0]):
        header, rows = rows[0], rows[1:]
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    if ncols == 1:
        nums = [parse_number(r[0]) for r in rows]
        if all(n is not None for n in nums):
            return (header[0] if header else ""), [str(i + 1) for i in range(len(nums))], [float(n) for n in nums if n is not None]
        return None
    col = next((c for c in range(ncols)
                if all(c < len(r) and parse_number(r[c]) is not None for r in rows)), None)
    if col is None:
        return None
    label_col = next((c for c in range(ncols) if c != col), 0)
    if header and col < len(header):
        title = header[col].strip()
        if label_col < len(header) and header[label_col].strip():
            title = f"{title} by {header[label_col].strip()}" if title else ""
    labels = [r[label_col].strip() if label_col < len(r) else "" for r in rows]
    values = [float(parse_number(r[col]) or 0.0) for r in rows]
    return title, labels, values


def parse_chart(body: str) -> tuple[str, list[str], list[float]] | None:
    """(title, labels, values) from a chart block body, or None when it holds no plottable data."""
    text = (body or "").strip()
    if not text:
        return None
    if text[:1] in "[{":
        res = _from_json(text)
        if res:
            return res
    lines = [ln for ln in text.splitlines() if ln.strip()]
    title = ""
    if lines and re.match(r"^\s*(title|caption)\s*[:=]\s*", lines[0], re.I):
        title = re.sub(r"^\s*(title|caption)\s*[:=]\s*", "", lines[0], flags=re.I).strip()
        lines = lines[1:]
    if not lines:
        return None
    flat = re.split(r"[,;\s]+", " ".join(lines).strip())
    if len(flat) > 1 and all(parse_number(x) is not None for x in flat if x):
        nums = [float(parse_number(x) or 0.0) for x in flat if x]
        return title, [str(i + 1) for i in range(len(nums))], nums
    if all(ln.strip().startswith("|") for ln in lines):
        rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines
                if not re.match(r"^\s*\|?\s*:?-{2,}", ln)]
        res = _from_rows(rows)
    elif all(re.match(r"^[^,;\t]*[:=]\s*\S", ln) for ln in lines) and not any("," in ln.split(":", 1)[0] for ln in lines):
        labels, values = [], []
        for ln in lines:
            k, _, v = re.split(r"([:=])", ln, maxsplit=1)
            n = parse_number(v)
            if n is None:
                return None
            labels.append(k.strip().strip("-*• "))
            values.append(n)
        res = ("", labels, values)
    else:
        sep = "\t" if all("\t" in ln for ln in lines) else (";" if all(";" in ln for ln in lines) else ",")
        rows = [[c.strip().strip('"') for c in ln.split(sep)] for ln in lines]
        res = _from_rows(rows)
    if not res or not res[2]:
        return None
    return (title or res[0]), res[1], res[2]


def fmt_value(v: float) -> str:
    """Compact, readable number: 1,234 · 12.5 · 0.031 · 1.2M."""
    if not math.isfinite(v):
        return str(v)
    a = abs(v)
    if a >= 1e7:
        for div, suf in ((1e9, "B"), (1e6, "M")):
            if a >= div:
                return f"{v / div:.1f}".rstrip("0").rstrip(".") + suf
    if float(v).is_integer():
        return f"{int(v):,}"
    if a >= 100:
        return f"{v:,.1f}"
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3g}"


def _bar(value: float, vmax: float, cells: int) -> str:
    """Horizontal bar of *value* scaled to *cells* columns (eighth-block resolution)."""
    if vmax <= 0 or cells <= 0 or value <= 0:
        return ""
    eighths = glyph("eighths")
    units = round(min(1.0, value / vmax) * cells * 8)
    full, part = divmod(units, 8)
    unicode_ramp = eighths != "########"
    s = eighths[-1] * full
    if part and unicode_ramp:
        s += eighths[part - 1]
    elif part >= 4 and not unicode_ramp:
        s += "#"
    return s or (eighths[0] if unicode_ramp else "#")


def bar_chart(labels: list[str], values: list[float], width: int, title: str = "", indent: int = 2) -> list[str]:
    """Horizontal bar chart rows (label │ bar  value); every row fits *width* columns."""
    width = max(20, width)
    sp = " " * indent
    out: list[str] = []
    if title:
        out.append(f"{sp}{BOLD}{TEXT}{truncate(title, width - indent)}{RST}")
    if not values:
        return out
    vals = [v for v in values]
    vmax = max([abs(v) for v in vals] + [0.0])
    texts = [fmt_value(v) for v in vals]
    val_w = max(display_width(t) for t in texts)
    label_w = min(max(display_width(str(lb)) for lb in labels) if labels else 1, max(6, (width - indent) // 3))
    bar_w = max(4, width - indent - label_w - val_w - 4)
    top = max(range(len(vals)), key=lambda i: vals[i])
    for i, (lb, v, t) in enumerate(zip(labels, vals, texts)):
        color = ERROR if v < 0 else (ACCENT if i == top else PRIMARY)
        bar = _bar(abs(v), vmax, bar_w)
        label = pad(truncate(str(lb), label_w), label_w)
        row = f"{sp}{TEXT}{label}{RST} {SUBTLE}{glyph('v')}{RST}{color}{bar}{RST}{' ' * (bar_w - display_width(bar))} {BOLD if i == top else ''}{pad(t, val_w, 'right')}{RST}"
        out.append(truncate(row, width))
    return out


def chart_stats(values: list[float]) -> str:
    """'4 items · max 1,204.5 · total 2,356.5'."""
    d = f" {glyph('dot')} "
    return f"{len(values)} items{d}max {fmt_value(max(values))}{d}total {fmt_value(sum(values))}"


def sparkline(values: list[float], width: int | None = None) -> str:
    """'▁▂▄▇█▆▃' for *values* (resampled to *width* cells when longer)."""
    if not values:
        return ""
    vals = list(values)
    if width and len(vals) > width > 0:
        step = len(vals) / width
        vals = [sum(vals[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]) /
                max(1, len(vals[int(i * step):max(int(i * step) + 1, int((i + 1) * step))])) for i in range(width)]
    ramp = glyph("spark")
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        return ramp[len(ramp) // 2] * len(vals)
    return "".join(ramp[min(len(ramp) - 1, int((v - lo) / span * (len(ramp) - 1) + 0.5))] for v in vals)


def sparkline_rows(labels: list[str], values: list[float], width: int, title: str = "", indent: int = 2) -> list[str]:
    sp = " " * indent
    out: list[str] = []
    if title:
        out.append(f"{sp}{BOLD}{TEXT}{truncate(title, width - indent)}{RST}")
    stats = (f"min {fmt_value(min(values))} {glyph('dot')} max {fmt_value(max(values))} "
             f"{glyph('dot')} last {fmt_value(values[-1])}")
    room = max(4, width - indent - display_width(stats) - 2)
    line = sparkline(values, room)
    if display_width(line) + display_width(stats) + 2 > width - indent:
        out.append(f"{sp}{PRIMARY}{sparkline(values, max(4, width - indent))}{RST}")
        out.append(truncate(f"{sp}{MUTED}{stats}{RST}", width))
    else:
        out.append(f"{sp}{PRIMARY}{line}{RST}  {MUTED}{stats}{RST}")
    if labels and len(labels) >= 2 and not all(lb == str(i + 1) for i, lb in enumerate(labels)):
        first, last = str(labels[0]), str(labels[-1])
        gap = display_width(line) - display_width(first) - display_width(last)
        if gap >= 1:
            out.append(f"{sp}{SUBTLE}{first}{' ' * gap}{last}{RST}")
    return out


def render_chart(kind: str, body: str, width: int, indent: int = 2) -> list[str] | None:
    """Rows for a chart fence (*kind* is the fence language); None when the body is not chart data."""
    try:
        parsed = parse_chart(body)
    except Exception:
        return None
    if not parsed:
        return None
    title, labels, values = parsed
    kind = (kind or "").strip().lower().split()[0] if (kind or "").strip() else "chart"
    spark = kind.startswith("spark") or (len(values) > 24 and all(lb == str(i + 1) for i, lb in enumerate(labels)))
    sp = " " * indent
    corner = f"{sp}{BORDER}{glyph('tl')}{glyph('h') * 2}{RST} "
    head = corner + (f"{BOLD}{TEXT}{title}{RST}" if title else f"{MUTED}{'sparkline' if spark else 'chart'}{RST}")
    bar = f"{sp}{BORDER}{glyph('v')}{RST} "
    inner = width - indent - 2
    if spark:
        body = sparkline_rows(labels, values, inner, "", 0)
    else:
        body = bar_chart(labels, values, inner, "", 0)
    foot = f"{sp}{BORDER}{glyph('bl')}{glyph('h') * 2}{RST}" + ("" if spark else f" {MUTED}{chart_stats(values)}{RST}")
    return [truncate(head, width)] + [bar + r for r in body] + [truncate(foot, width)]
