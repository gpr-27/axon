"""
``/usage``: one tabbed screen (Status / Config / Usage / Stats / Context) and its plain-text twin.

Every number here has a source: the session ledger, the turn traces, ``~/.axon/usage.jsonl`` (through
``analytics.collect``), the skill/tool/memory token estimators the other reports use, and the rate-limit
headers the provider reported (``features.quota``). When a number has no source the row is left out
or says so; nothing is estimated silently or invented.

The data functions return plain values; the ``*_lines(agent, width)`` functions turn them into rows
that are never wider than *width* columns (measured with ``display_width``).
"""
from __future__ import annotations

import contextlib
import io
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from axon import platform as plat
from axon.ui.picker import Selector
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, GOLD, GRAY_BG, MINT, RST, ROSE, SLATE, TEAL, WHITE,
    display_width, glyph, middle_ellipsis, pad, truncate, wrap_ansi,
)

TABS = ["Status", "Config", "Usage", "Stats", "Context"]
APPROX = "approximate"


# ─── small formatting helpers ──────────────────────────────────────────────

def usd(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "$0"
    return f"${f:,.2f}" if abs(f) >= 10 else f"${f:.4f}"


def tok(n: Any) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


def dur(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(round(seconds))
    if s < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def bar(frac: float, cells: int, color: str = MINT) -> str:
    frac = min(1.0, max(0.0, frac))
    full = int(round(frac * cells))
    return f"{color}{'█' * full}{DARK_SLATE}{'░' * (cells - full)}{RST}"


def _fit(text: str, width: int) -> str:
    return truncate(text, width) if display_width(text) > width else text


def table_rows(headers: list[str], rows: list[list[str]], width: int, indent: int = 2,
               drop: tuple[int, ...] = ()) -> list[str]:
    """A table whose first column flexes: it is shortened (middle ellipsis) to fit *width*; the columns
    listed in *drop* (last first) disappear when even that is not enough. Numbers align right."""
    cols = list(range(len(headers)))
    drop_order = list(drop)
    while True:
        widths = {c: max([display_width(headers[c])] + [display_width(r[c]) for r in rows]) for c in cols}
        fixed = sum(widths[c] for c in cols[1:]) + 2 * (len(cols) - 1) + indent
        room = width - fixed
        if room >= min(widths[0], 12) or not drop_order:
            break
        cols.remove(drop_order.pop())
    first_w = max(6, min(widths[0], room))
    out = []

    def cell(c: int, text: str, head: bool = False) -> str:
        w = first_w if c == cols[0] else widths[c]
        if c == cols[0]:
            plain = text if display_width(text) <= w else middle_ellipsis(text, w) if "\x1b" not in text else truncate(text, w)
            return pad(plain, w)
        return pad(text, w, "right")

    out.append(" " * indent + "  ".join(f"{DARK_SLATE}{cell(c, headers[c], True)}{RST}" for c in cols))
    for r in rows:
        out.append(" " * indent + "  ".join(cell(c, r[c]) for c in cols))
    return [_fit(x, width) for x in out]


def section_title(text: str, width: int, note: str = "") -> list[str]:
    head = f"  {GOLD}{BOLD}{text}{RST}"
    if note:
        head += f"  {DARK_SLATE}{note}{RST}"
    return ["", _fit(head, width)]


# ─── data: session ─────────────────────────────────────────────────────────

@dataclass
class ModelRow:
    model: str
    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: Decimal = Decimal("0")
    priced: bool = True


@dataclass
class SessionUsage:
    total_cost: Decimal | None = None
    calls: int = 0
    api_s: float | None = None         # time spent waiting for the provider (main agent, from the traces)
    wall_s: float | None = None        # first request to now
    added: int | None = None           # lines added / removed by the edit tools (None: no edit recorded)
    removed: int | None = None
    edited_files: int = 0
    models: list[ModelRow] = field(default_factory=list)


def _is_ledger(obj: Any) -> bool:
    return isinstance(getattr(obj, "entries", None), list)


def session_ledger(agent: Any) -> Any:
    ledger = getattr(agent, "ledger", None)
    if _is_ledger(ledger) and ledger.entries:
        return ledger
    session = getattr(agent, "session", None)
    sid = getattr(session, "active_session_id", "")
    if isinstance(sid, str) and sid:
        try:
            loaded = session.load_ledger(sid.rsplit("_sub_", 1)[0], str(agent.settings.model), include_subagents=True)
            if _is_ledger(loaded) and loaded.entries:
                return loaded
        except Exception:
            pass
    return ledger if _is_ledger(ledger) else None


def _edit_counts(name: str, inp: dict[str, Any]) -> tuple[int, int, str] | None:
    from axon.agent.filechanges import count_lines, diff_counts
    path = str(inp.get("path") or inp.get("file_path") or "")
    if name in ("Edit", "StrReplace"):
        a, r = diff_counts(str(inp.get("old_string") or ""), str(inp.get("new_string") or ""))
        return a, r, path
    if name == "MultiEdit":
        a = r = 0
        for e in inp.get("edits") or []:
            if isinstance(e, dict):
                da, dr = diff_counts(str(e.get("old_string") or ""), str(e.get("new_string") or ""))
                a, r = a + da, r + dr
        return a, r, path
    if name == "Write":
        return count_lines(str(inp.get("content") or "")), 0, path      # an overwrite's removed lines are unknown
    if name == "Patch":
        text = str(inp.get("patch") or inp.get("diff") or "")
        a = sum(1 for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        r = sum(1 for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        return a, r, "patch"
    return None


def session_usage(agent: Any, now: float | None = None) -> SessionUsage:
    now = time.time() if now is None else now
    out = SessionUsage()
    ledger = session_ledger(agent)
    if ledger is not None and ledger.entries:
        out.total_cost = Decimal(str(ledger.total()))
        out.calls = len(ledger.entries)
        rows: dict[str, ModelRow] = {}
        for e in ledger.entries:
            m = rows.setdefault(e.model, ModelRow(e.model))
            m.calls += 1
            m.input += e.usage.input
            m.output += e.usage.output
            m.cache_read += e.usage.cache_read
            m.cache_write += e.usage.cache_write
            m.cost += e.cost
            m.priced = m.priced and e.priced
        out.models = sorted(rows.values(), key=lambda r: r.cost, reverse=True)
    traces = list(getattr(agent, "traces", None) or [])
    api = 0.0
    seen_llm = False
    files: set[str] = set()
    added = removed = 0
    edits = False
    first = [float(t.started_at) for t in traces if getattr(t, "started_at", 0)]
    for t in traces:
        for st in getattr(t, "steps", []) or []:
            d = st.data if isinstance(st.data, dict) else {}
            if st.kind == "llm" and st.ended_at:
                api += max(0.0, st.ended_at - st.started_at)
                seen_llm = True
            elif st.kind == "tool" and not d.get("is_error") and st.ended_at:
                try:
                    got = _edit_counts(str(d.get("name") or ""), d.get("input") if isinstance(d.get("input"), dict) else {})
                except Exception:
                    got = None
                if got is not None:
                    edits = True
                    added, removed = added + got[0], removed + got[1]
                    files.add(got[2])
    out.api_s = api if seen_llm else None
    if edits:
        out.added, out.removed, out.edited_files = added, removed, len(files)
    start = getattr(agent, "started_at", None)
    if not isinstance(start, (int, float)) or start <= 0:
        stamps = first + [float(e.timestamp) for e in (ledger.entries if ledger is not None else []) if e.timestamp]
        start = min(stamps) if stamps else None
    out.wall_s = max(0.0, now - float(start)) if start else None
    return out


# ─── data: footprint ───────────────────────────────────────────────────────

@dataclass
class FootRow:
    group: str       # System prompt | Tools | MCP | Skills | Memory
    name: str
    detail: str
    tokens: int


def footprint(agent: Any) -> list[FootRow]:
    """Tokens every turn carries before the conversation itself, per source (chars/4-style estimates)."""
    from axon.commands.common import estimate_tokens
    from axon.commands.context_cmds import _block_kind, _schema_name, _system_blocks, _tool_schemas
    rows: list[FootRow] = []
    blocks = _system_blocks(agent)
    parts = {"system": 0, "agents": 0, "memory": 0, "skills": 0}
    counts = dict.fromkeys(parts, 0)
    for b in blocks:
        text = str(b.get("text", ""))
        kind = _block_kind(text)
        parts[kind] += estimate_tokens(text)
        counts[kind] += 1
    sys_tok = parts["system"] + parts["agents"]
    rows.append(FootRow("System prompt", "System prompt", f"{counts['system'] + counts['agents']} blocks", sys_tok))
    schemas = _tool_schemas(agent)
    mcp: dict[str, list[dict[str, Any]]] = {}
    builtin = []
    for s in schemas:
        n = _schema_name(s)
        if n.startswith("mcp__"):
            mcp.setdefault(n.split("__")[1] if n.count("__") >= 2 else "mcp", []).append(s)
        else:
            builtin.append(s)
    rows.append(FootRow("Tools", "Built-in tools", f"{len(builtin)} schemas",
                        estimate_tokens(json.dumps(builtin, separators=(",", ":"))) if builtin else 0))
    for server, items in sorted(mcp.items()):
        rows.append(FootRow("MCP servers", f"MCP {server}", f"{len(items)} tools",
                            estimate_tokens(json.dumps(items, separators=(",", ":")))))
    listed = 0
    try:
        from axon.commands.insight_cmds import skill_report
        by_name = dict(getattr(getattr(agent, "skills", None), "skills", {}) or {})
        groups: dict[str, list[int]] = {}
        for r in skill_report(agent):
            if not r["model_invocable"] or not r["listing_tokens"]:
                continue
            sk = by_name.get(r["name"])
            plugin = str(getattr(sk, "plugin", "") or "")
            scope = str(r.get("scope") or "")
            label = f"Plugin {plugin}" if plugin else {"bundled": "Bundled skills", "personal": "Your skills",
                                                        "project": "Project skills"}.get(scope, f"{scope.title()} skills")
            groups.setdefault(label, []).append(int(r["listing_tokens"]))
        for label, vals in sorted(groups.items(), key=lambda kv: -sum(kv[1])):
            rows.append(FootRow("Skills", label, f"{len(vals)} skills", sum(vals)))
            listed += sum(vals)
    except Exception:
        pass
    if parts["skills"] > listed:
        rows.append(FootRow("Skills", "Skill listing header", "", parts["skills"] - listed))
    rows.append(FootRow("Memory", "Memory files", f"{counts['memory']} block" + ("" if counts["memory"] == 1 else "s"),
                        parts["memory"]))
    return rows


# ─── data: limits ──────────────────────────────────────────────────────────

@dataclass
class Spend:
    today: float = 0.0
    day24: float = 0.0
    week: float = 0.0


def budget_settings(agent: Any) -> dict[str, float]:
    s = getattr(agent, "settings", None)
    out: dict[str, float] = {}
    for key, attr in (("session", "session_cost_ceiling"), ("session (guard)", "budget_session_usd"), ("turn", "budget_turn_usd")):
        try:
            v = float(getattr(s, attr, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            out[key] = v
    return out


# ─── data: what's contributing ─────────────────────────────────────────────

@dataclass
class Contrib:
    """Cost (and tool activity) for the last 24 hours and the last 7 days."""
    by_project: dict[str, tuple[float, float]] = field(default_factory=dict)
    by_source: dict[str, tuple[float, float]] = field(default_factory=dict)
    by_model: dict[str, tuple[float, float]] = field(default_factory=dict)
    tools: dict[str, dict[str, float]] = field(default_factory=dict)
    cost24: float = 0.0
    cost7: float = 0.0
    calls24: int = 0
    calls7: int = 0
    note: str = ""


_contrib_cache: tuple[float, Contrib] | None = None


def contributors(now: float | None = None, refresh: bool = False) -> Contrib:
    global _contrib_cache
    now = time.time() if now is None else now
    if not refresh and _contrib_cache and now - _contrib_cache[0] < 30:
        return _contrib_cache[1]
    from axon.features import analytics
    c = Contrib()
    try:
        day = analytics.Period("24h", "last 24 hours", now - 86400, 1)
        week = analytics.Period("7d", "last 7 days", now - 7 * 86400, 7)
        s24, s7 = analytics.collect(day), analytics.collect(week)
        c.cost24, c.cost7 = s24.totals.cost, s7.totals.cost
        c.calls24, c.calls7 = s24.totals.calls, s7.totals.calls

        def two(a: list[tuple[str, Any]], b: list[tuple[str, Any]]) -> dict[str, tuple[float, float]]:
            d: dict[str, list[float]] = {}
            for name, agg in a:
                d.setdefault(name, [0.0, 0.0])[0] = agg.cost
            for name, agg in b:
                d.setdefault(name, [0.0, 0.0])[1] = agg.cost
            return {k: (v[0], v[1]) for k, v in sorted(d.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))}

        def proj(st: Any) -> list[tuple[str, Any]]:
            agg: dict[str, analytics.Agg] = {}
            for srow in st.sessions:
                name = Path(srow.workspace).name if srow.workspace else analytics._project_name(srow.project)
                a = agg.setdefault(name or srow.project or "?", analytics.Agg())
                a.cost += srow.cost
                a.calls += srow.calls
            return list(agg.items())

        c.by_project = two(proj(s24), proj(s7))
        c.by_source = two([(analytics._source_label(k), v) for k, v in s24.by_source],
                          [(analytics._source_label(k), v) for k, v in s7.by_source])
        c.by_model = two(s24.by_model, s7.by_model)
        for t in analytics.load_traces(now - 7 * 86400):
            for st in t.get("steps") or ():
                if not isinstance(st, dict) or st.get("kind") != "tool":
                    continue
                d = st.get("data") if isinstance(st.get("data"), dict) else {}
                name = str(d.get("name") or "?")
                started = analytics._float(st.get("started_at"))
                row = c.tools.setdefault(name, {"calls24": 0, "calls7": 0, "secs7": 0.0, "tokens7": 0})
                row["calls7"] += 1
                row["secs7"] += analytics._float(d.get("duration_s"))
                row["tokens7"] += analytics._int(d.get("est_tokens"))
                if started >= now - 86400:
                    row["calls24"] += 1
    except Exception as e:                              # a damaged log must not break the screen
        c.note = f"usage log could not be read ({type(e).__name__})"
    _contrib_cache = (now, c)
    return c


# ─── tabs ──────────────────────────────────────────────────────────────────

def _warning_lines(agent: Any, width: int) -> list[str]:
    from axon.ui.statusbar import agent_autocompact_warning
    text = agent_autocompact_warning(agent)
    if not text:
        return []
    rows = wrap_ansi(f"{GOLD}! {text}{RST}", max(10, width - 4))
    return [f"  {r}" if i == 0 else f"    {r}" for i, r in enumerate(rows)]


def usage_lines(agent: Any, width: int) -> list[str]:
    """Body of the Usage tab (without the autocompact warning, which the screen pins above it)."""
    out: list[str] = []
    su = session_usage(agent)
    out += section_title("Session", width)
    if su.total_cost is None:
        out.append(f"    {SLATE}No API calls yet in this session.{RST}")
    else:
        def kv(label: str, value: str) -> None:
            out.append(_fit(f"    {SLATE}{pad(label, 15)}{RST} {value}", width))
        kv("Total cost", f"{WHITE}{BOLD}{usd(su.total_cost)}{RST}  {SLATE}{su.calls} API calls{RST}")
        wall = dur(su.wall_s)
        kv("API duration", f"{WHITE}{dur(su.api_s)}{RST}  {SLATE}provider wait{RST}"
           if su.api_s is not None else f"{SLATE}n/a (no timed call recorded){RST}")
        kv("Wall duration", f"{WHITE}{wall}{RST}  {SLATE}since first call{RST}" if su.wall_s is not None
           else f"{SLATE}n/a{RST}")
        if su.added is None:
            kv("Code changes", f"{SLATE}none by the edit tools{RST}")
        else:
            kv("Code changes", f"{MINT}+{su.added}{RST} {ROSE}-{su.removed}{RST} lines in {su.edited_files} file"
               f"{'' if su.edited_files == 1 else 's'}  {SLATE}edit tools{RST}")
        out.append("")
        heads = ["Model", "Input", "Output", "Cache read", "Cache write", "Cost"]
        trows = [[m.model, tok(m.input), tok(m.output), tok(m.cache_read), tok(m.cache_write),
                  usd(m.cost) + ("" if m.priced else "*")] for m in su.models]
        out += table_rows(heads, trows, width, indent=4, drop=(3, 4))
        if any(not m.priced for m in su.models):
            out.append(f"    {DARK_SLATE}* no price known for this model: the cost is not counted{RST}")

    # footprint
    fp = footprint(agent)
    total = sum(r.tokens for r in fp)
    out += section_title("Footprint every turn", width, f"{APPROX} · estimated from text size, before the conversation")
    heads = ["Source", "Items", "Tokens", "Share"]
    peak = max([r.tokens for r in fp] + [1])
    frows = [[f"{r.name}", r.detail, tok(r.tokens), f"{r.tokens / total * 100:.0f}%" if total else "0%"] for r in fp]
    out += table_rows(heads, frows, width, indent=4, drop=(3, 1))
    try:
        from axon.ui.statusline import context_usage
        _, window = context_usage(agent)
    except Exception:
        window = 0
    share = f"  {SLATE}{total / window * 100:.1f}% of the {tok(window)} window{RST}" if window else ""
    out.append(_fit(f"    {WHITE}{BOLD}~{tok(total)} tokens per turn{RST} {DARK_SLATE}({APPROX}){RST}{share}", width))
    _ = peak

    # limits
    out += section_title("Limits", width)
    cells = max(8, min(30, width - 40))
    spent = float(su.total_cost or 0)
    budgets = budget_settings(agent)
    if not budgets:
        out.append(_fit(f"    {SLATE}Session budget{RST}  no limit set  {DARK_SLATE}(/budget <amount>){RST}", width))
    for label, limit in budgets.items():
        frac = spent / limit
        color = MINT if frac < 0.75 else (GOLD if frac < 1 else ROSE)
        out.append(_fit(f"    {SLATE}{pad(label.capitalize() + ' budget', 22)}{RST} {bar(frac, cells, color)} "
                        f"{color}{frac * 100:.0f}%{RST} {SLATE}{usd(spent)} of {usd(limit)}{RST}", width))
    c = contributors()
    out.append(_fit(f"    {SLATE}{pad('Last 24 hours', 22)}{RST} {WHITE}{usd(c.cost24)}{RST} {DARK_SLATE}spent · no daily limit "
                    f"exists in /budget{RST}", width))
    out.append(_fit(f"    {SLATE}{pad('Last 7 days', 22)}{RST} {WHITE}{usd(c.cost7)}{RST} {DARK_SLATE}spent · no weekly limit "
                    f"exists in /budget{RST}", width))
    from axon.features import quota
    from axon.features.quota import local_reset_text
    limits = quota.snapshot()
    if not limits:
        out.append(f"    {SLATE}Provider rate limits{RST}  no limit data from this provider")
    for lim in limits:
        frac = lim.used_fraction
        reset = local_reset_text(lim.reset_at)
        if frac is None:
            out.append(_fit(f"    {SLATE}{pad(lim.label, 22)}{RST} {SLATE}{lim.remaining} left{RST} {DARK_SLATE}{reset}{RST}", width))
            continue
        color = MINT if frac < 0.75 else (GOLD if frac < 0.9 else ROSE)
        out.append(_fit(f"    {SLATE}{pad(lim.label, 22)}{RST} {bar(frac, cells, color)} {color}{frac * 100:.0f}%{RST} "
                        f"{SLATE}{tok(lim.limit)} limit{RST} {DARK_SLATE}{reset}{RST}", width))
        if width >= 100 and lim.provider:
            pass

    # contributors
    out += section_title("What's contributing to your usage?", width, "all projects, from ~/.axon/usage.jsonl")
    if c.note:
        out.append(f"    {ROSE}{c.note}{RST}")
    elif not c.calls7:
        out.append(f"    {SLATE}Nothing recorded in the last 7 days.{RST}")
    else:
        out.append(_fit(f"    {WHITE}{usd(c.cost24)}{RST} {SLATE}in 24h ({c.calls24} calls) ·{RST} "
                        f"{WHITE}{usd(c.cost7)}{RST} {SLATE}in 7 days ({c.calls7} calls){RST}", width))
        for title, data in (("Project", c.by_project), ("Source", c.by_source), ("Model", c.by_model)):
            out.append("")
            rows = [[k, usd(a), usd(b), f"{b / c.cost7 * 100:.0f}%" if c.cost7 else "0%"] for k, (a, b) in list(data.items())[:5]]
            out += table_rows([title, "24h", "7 days", "Share"], rows, width, indent=4, drop=(3,))
        if c.tools:
            out.append("")
            top = sorted(c.tools.items(), key=lambda kv: (-kv[1]["tokens7"], -kv[1]["calls7"]))[:6]
            rows = [[k, str(int(v["calls24"])), str(int(v["calls7"])), dur(v["secs7"]), tok(v["tokens7"])] for k, v in top]
            out += table_rows(["Tool", "Calls 24h", "Calls 7d", "Time 7d", "Fed back 7d"], rows, width, indent=4,
                              drop=(3, 1))
            out.append(f"    {DARK_SLATE}tools are not billed; \"fed back\" is result tokens sent to the model "
                       f"({APPROX}){RST}"[: max(20, width * 3)])
    return [_fit(x, width) for x in out]


class _Capture(io.StringIO):
    """Collects a report's output but looks like the real terminal (encoding, tty), so the report picks
    the same glyphs and colours it would print directly."""

    def __init__(self) -> None:
        super().__init__()
        import sys
        self._enc = getattr(sys.__stdout__, "encoding", None) or "utf-8"
        self._tty = bool(getattr(sys.__stdout__, "isatty", lambda: False)())

    @property
    def encoding(self) -> str:                     # type: ignore[override]
        return self._enc

    def isatty(self) -> bool:
        return self._tty


def _capture(fn: Any, agent: Any, arg: str = "") -> list[str]:
    buf = _Capture()
    with contextlib.redirect_stdout(buf):
        try:
            fn(agent, arg)
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
    lines = buf.getvalue().split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def status_lines(agent: Any, width: int) -> list[str]:
    from axon.commands.system_cmds import handle_status
    return [_fit(x, width) for x in _capture(handle_status, agent)]


def stats_lines(agent: Any, width: int) -> list[str]:
    from axon.features import analytics
    try:
        parsed = analytics.parse_stats_args("", analytics._workspace(agent))
        if isinstance(parsed, str):
            return [parsed]
        period, key, scope = parsed
        return [_fit(x, width) for x in analytics.render_stats(analytics.collect(period, key, scope), width=width)]
    except Exception as e:
        return [f"  {ROSE}Stats unavailable: {type(e).__name__}: {e}{RST}"]


def context_lines(agent: Any, width: int) -> list[str]:
    from axon.commands.context_cmds import handle_context
    return [_fit(x, width) for x in _capture(handle_context, agent)]


def config_lines(agent: Any, width: int) -> list[str]:
    from axon.commands.system_cmds import _setting_sources
    from axon.ui.theme import current_theme, kv_lines
    s = agent.settings
    rows: list[tuple[str, str]] = [
        ("Model", f"{s.model}"), ("Effort", str(s.effort)), ("Thinking", "on" if s.thinking else "off"),
        ("Mode", str(s.mode)), ("Endpoint", str(s.base_url)),
        ("Auto-compact at", f"{float(s.compact_at) * 100:.0f}% of the context window"),
        ("History window", f"{s.max_history_turns} turns" if s.max_history_turns else "unlimited"),
        ("Max output", f"{tok(s.max_tokens)} tokens"),
        ("Session budget", usd(s.session_cost_ceiling) if float(s.session_cost_ceiling or 0) > 0 else "none"),
        ("Turn budget", usd(s.budget_turn_usd) if s.budget_turn_usd else "none"),
        ("Theme", current_theme()),
        ("Workspace", str(s.workspace)),
        ("Setting files", ", ".join(_setting_sources(agent)) or "defaults only"),
    ]
    out = [""]
    for k, v in rows:
        out += kv_lines(f"{SLATE}{k}{RST}", f"{WHITE}{v}{RST}", width, label_w=16, indent=2)
    out += ["", f"  {DARK_SLATE}Change with /config <key> <value>, or /settings for the full panel.{RST}"]
    return [_fit(x, width) for x in out]


def tab_lines(agent: Any, tab: str, width: int) -> list[str]:
    fn = {"Status": status_lines, "Config": config_lines, "Usage": usage_lines, "Stats": stats_lines,
          "Context": context_lines}[tab]
    return fn(agent, width)


def pinned_lines(agent: Any, tab: str, width: int) -> list[str]:
    return _warning_lines(agent, width) if tab in ("Usage", "Context") else []


def plain_text(agent: Any, width: int = 100, tabs: list[str] | None = None) -> str:
    """Every tab as text, for a pipe, a file or ``-p``."""
    from axon.ui.theme import strip_ansi
    out: list[str] = []
    for tab in tabs or TABS:
        out.append(f"== {tab} ==")
        out += pinned_lines(agent, tab, width)
        out += tab_lines(agent, tab, width)
        out.append("")
    return strip_ansi("\n".join(out))


# ─── the screen ────────────────────────────────────────────────────────────

class UsageScreen(Selector):
    """Full-screen tabbed viewer built on the shared picker: ←/→ (or Tab) switch tabs, ↑/↓ scroll, Esc closes."""

    def __init__(self, agent: Any, start: int = 0) -> None:
        super().__init__([], title="Usage", subtitle="", tabs=list(TABS), tab_index=start, fullscreen=True)
        self.agent = agent
        self.scroll = 0
        self._cache: dict[tuple[str, int], list[str]] = {}

    def switch_tab(self, delta: int) -> None:
        super().switch_tab(delta)
        self.scroll = 0

    def _body(self, width: int) -> list[str]:
        tab = self.tabs[self.tab_index]
        key = (tab, width)
        if key not in self._cache:
            try:
                self._cache[key] = tab_lines(self.agent, tab, width)
            except Exception as e:
                self._cache[key] = [f"  {ROSE}{tab} unavailable: {type(e).__name__}: {e}{RST}"]
        return self._cache[key]

    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, rows = plat.term_size()
        width = max(20, cols - 2)
        tab = self.tabs[self.tab_index]
        head = f"  {GOLD}◆{RST} {BOLD}{WHITE}Usage{RST}"
        lines = [truncate(head, width)]
        chips = [f"{GRAY_BG}{BOLD}{WHITE} {n} {RST}" if i == self.tab_index else f"{SLATE} {n} {RST}"
                 for i, n in enumerate(self.tabs)]
        lines.append(truncate("  " + f"{DARK_SLATE}‹{RST}" + "".join(chips) + f"{DARK_SLATE}›{RST}", width))
        lines.append(f"  {DARK_SLATE}{'─' * max(1, width - 2)}{RST}")
        lines += pinned_lines(self.agent, tab, width)
        body = self._body(width)
        room = max(3, rows - len(lines) - 3)
        self.scroll = max(0, min(self.scroll, max(0, len(body) - room)))
        view = body[self.scroll:self.scroll + room]
        above, below = self.scroll, max(0, len(body) - self.scroll - room)
        if above and view:
            view[0] = f"  {DARK_SLATE}↑ {above} more{RST}"
        if below and view:
            view[-1] = f"  {DARK_SLATE}↓ {below} more{RST}"
        lines += view
        lines += [""] * (room - len(view))
        lines.append(f"  {DARK_SLATE}{'─' * max(1, width - 2)}{RST}")
        lines.append(truncate(f"  {DARK_SLATE}←/→ tab · ↑/↓ scroll · r refresh · esc close{RST}", width))
        return lines[:max(1, rows - 1)], None, 0

    def _default_key(self, key: str) -> None:
        cols, rows = plat.term_size()
        page = max(3, rows - 8)
        if key in ("up", "k", "ctrl-p"):
            self.scroll = max(0, self.scroll - 1)
        elif key in ("down", "j", "ctrl-n"):
            self.scroll += 1
        elif key == "pageup":
            self.scroll = max(0, self.scroll - page)
        elif key == "pagedown":
            self.scroll += page
        elif key == "home":
            self.scroll = 0
        elif key == "end":
            self.scroll = 10_000
        elif key == "r":
            self._cache.clear()
            contributors(refresh=True)
        else:
            super()._default_key(key)


def run_usage_screen(agent: Any, tab: str = "") -> None:
    start = next((i for i, t in enumerate(TABS) if t.lower() == tab.strip().lower()), 2 if not tab.strip() else 0)
    UsageScreen(agent, start).run()
    _ = glyph, TEAL, CYAN
