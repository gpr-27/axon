"""
Usage analytics: ``/stats``, ``/budget``, ``/report`` and ``axon report`` / ``axon stats``.

Everything is computed from real persisted data (see :mod:`axon.storage`):

* ``~/.axon/usage.jsonl``          one record per LLM call (tokens, cost, source, latency)
* ``projects/<key>/traces/*.jsonl`` per-turn glass-box traces (tool calls, timings, errors)
* ``projects/<key>/sessions/*.jsonl`` transcripts (session titles / turn counts as fallback)

The HTML report is a single self-contained file (inline CSS + SVG, no scripts or external
assets) written under ``~/.axon/cache/reports/`` unless ``--out`` says otherwise.
"""
from __future__ import annotations

import argparse
import html
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from axon import storage
from axon.features import FeatureCommand
from axon.features import _charts as ch

DEFAULT_PERIOD = "7d"
DEFAULT_REPORT_DAYS = 30
MAX_ALL_DAYS = 366


# ─── Formatting ────────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    v = float(v or 0)
    if v == 0:
        return "$0.00"
    a = abs(v)
    if a < 0.01:
        return f"${v:.4f}"
    if a < 100:
        return f"${v:.2f}" if a >= 1 else f"${v:.3f}"
    return f"${v:,.0f}"


def fmt_tokens(n: float) -> str:
    n = float(n or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            val = n / div
            return f"{val:.1f}{suf}" if val < 100 else f"{val:.0f}{suf}"
    return f"{int(n)}"


def fmt_dur(s: float) -> str:
    s = float(s or 0)
    if s < 1:
        return f"{s * 1000:.0f}ms"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"


def fmt_pct(p: float) -> str:
    return f"{p:.1f}%"


def _clean(text: Any, limit: int = 0) -> str:
    """Single-line, control-character-free version of untrusted text."""
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\x9b]", "", str(text or ""))
    s = " ".join(s.split())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _source_label(source: str) -> str:
    try:
        from axon.session.ledger import source_label
        return str(source_label(source))
    except Exception:
        return (source or "main").replace("_", " ").title()


# ─── Periods & scope ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Period:
    key: str                 # today | 7d | 30d | all | <n>d
    label: str
    since: float | None      # epoch seconds (local midnight), None = all time
    days: int | None


def _midnight(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp()


def parse_period(token: str | None, today: date | None = None) -> Period | None:
    """'today' | '7d' | '30d' | 'all' | '<n>d' | '<n>' -> Period (None when not a period)."""
    t = (token or DEFAULT_PERIOD).strip().lower()
    today = today or date.today()
    if t in ("today", "1d", "day"):
        return Period("today", "today", _midnight(today), 1)
    if t in ("all", "ever", "all-time", "alltime"):
        return Period("all", "all time", None, None)
    if t in ("week", "w"):
        t = "7d"
    if t in ("month", "m"):
        t = "30d"
    m = re.fullmatch(r"(\d{1,4})d?", t)
    if not m:
        return None
    n = max(1, min(int(m.group(1)), 3650))
    if n == 1:
        return Period("today", "today", _midnight(today), 1)
    return Period(f"{n}d", f"last {n} days", _midnight(today - timedelta(days=n - 1)), n)


def _period_from_days(days: int | None) -> Period:
    if not days or days <= 0:
        return Period("all", "all time", None, None)
    return parse_period(f"{days}d") or Period("all", "all time", None, None)


def _project_key(workspace: Any) -> str | None:
    if not workspace:
        return None
    try:
        return storage.project_key(Path(workspace))
    except Exception:
        return None


def _projects_root() -> Path:
    return storage.axon_home() / "projects"


def _project_name(key: str, workspace: str = "") -> str:
    if workspace:
        name = Path(workspace).name
        if name:
            return name
    parts = [p for p in (key or "").split("-") if p]
    return parts[-1] if parts else (key or "?")


# ─── Data model ────────────────────────────────────────────────────────────

@dataclass
class UsageRec:
    ts: float
    project: str
    workspace: str
    session: str
    model: str
    source: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int
    cost: float
    duration_s: float | None

    @property
    def day(self) -> date:
        return datetime.fromtimestamp(self.ts).date()


@dataclass
class Agg:
    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0
    uncached_cost: float = 0.0     # what these calls would cost with no prompt caching
    list_cost: float = 0.0         # same calls at list rates with caching (for the savings ratio)

    def add(self, r: UsageRec, uncached: float, listed: float) -> None:
        self.calls += 1
        self.input += r.input
        self.output += r.output
        self.cache_read += r.cache_read
        self.cache_write += r.cache_write
        self.cost += r.cost
        self.uncached_cost += uncached
        self.list_cost += listed

    @property
    def uncached_input(self) -> int:
        return max(0, self.input - self.cache_read - self.cache_write)

    @property
    def hit_pct(self) -> float:
        return self.cache_read / self.input * 100 if self.input else 0.0

    @property
    def savings(self) -> float:
        """Net $ saved by prompt caching, scaled to what was actually billed (group ratios)."""
        if self.list_cost <= 0:
            return 0.0
        return (self.uncached_cost - self.list_cost) * (self.cost / self.list_cost)


@dataclass
class ToolAgg:
    name: str
    calls: int = 0
    total_s: float = 0.0
    errors: int = 0


@dataclass
class TurnRow:
    session: str
    project: str
    turn: int
    prompt: str
    model: str
    cost: float
    llm_calls: int
    tool_calls: int
    tokens: int
    duration_s: float
    started_at: float


@dataclass
class SessionRow:
    id: str
    project: str
    workspace: str
    title: str = ""
    first_ts: float = 0.0
    last_ts: float = 0.0
    calls: int = 0
    turns: int = 0
    tokens: int = 0
    cost: float = 0.0
    models: set[str] = field(default_factory=set)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.last_ts - self.first_ts)


@dataclass
class Stats:
    period: Period
    scope: str                                  # human scope label
    project: str | None
    totals: Agg
    days: list[tuple[date, Agg]]
    by_model: list[tuple[str, Agg]]
    by_source: list[tuple[str, Agg]]
    tools: list[ToolAgg]
    turns: list[TurnRow]
    sessions: list[SessionRow]
    latencies: list[float]
    generated_at: float = field(default_factory=time.time)

    @property
    def empty(self) -> bool:
        return self.totals.calls == 0 and not self.tools and not self.turns

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        s = sorted(self.latencies)
        return s[min(len(s) - 1, int(math.ceil(0.95 * len(s))) - 1)]


# ─── Loading ───────────────────────────────────────────────────────────────

def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> float:
    try:
        f = float(v or 0)
        return f if math.isfinite(f) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_usage(since: float | None = None, project: str | None = None) -> list[UsageRec]:
    """Records from ~/.axon/usage.jsonl, filtered by start time and project key."""
    out: list[UsageRec] = []
    for d in storage.read_jsonl(storage.user_file("usage.jsonl")):
        ts = _parse_ts(d.get("ts"))
        if ts is None or (since is not None and ts < since):
            continue
        proj = str(d.get("project") or "")
        if project and proj != project:
            continue
        u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        dur = d.get("duration_s")
        out.append(UsageRec(
            ts=ts, project=proj, workspace=str(d.get("workspace") or ""), session=str(d.get("session") or ""),
            model=str(d.get("model") or "unknown"), source=str(d.get("source") or "main"),
            input=_int(u.get("input")), output=_int(u.get("output")), cache_read=_int(u.get("cache_read")),
            cache_write=_int(u.get("cache_write")), reasoning=_int(u.get("reasoning")),
            cost=_float(d.get("cost")), duration_s=_float(dur) if dur is not None else None,
        ))
    out.sort(key=lambda r: r.ts)
    return out


def _trace_files(project: str | None, since: float | None) -> list[tuple[str, Path]]:
    root = _projects_root()
    if not root.is_dir():
        return []
    dirs = [root / project] if project else [p for p in root.iterdir() if p.is_dir()]
    out: list[tuple[str, Path]] = []
    for d in dirs:
        tdir = d / "traces"
        if not tdir.is_dir():
            continue
        for f in tdir.glob("*.jsonl"):
            try:
                if since is not None and f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            out.append((d.name, f))
    return out


def load_traces(since: float | None = None, project: str | None = None) -> list[dict[str, Any]]:
    """Saved TurnTrace dicts (each tagged with ``_project`` and ``_session``)."""
    out: list[dict[str, Any]] = []
    for proj, path in _trace_files(project, since):
        for rec in storage.read_jsonl(path):
            started = _float(rec.get("started_at"))
            if since is not None and started and started < since:
                continue
            rec["_project"], rec["_session"] = proj, path.stem
            out.append(rec)
    return out


def _transcript_info(project: str, session: str) -> tuple[str, int]:
    """(first user prompt, number of user messages) from a session transcript."""
    path = _projects_root() / project / "sessions" / f"{session}.jsonl"
    if not path.is_file():
        return "", 0
    try:
        from axon.session.store import iter_records
        records: Iterable[dict[str, Any]] = iter_records(path)
    except Exception:
        records = storage.read_jsonl(path)
    title, renamed, n = "", "", 0
    try:
        for rec in records:
            t = rec.get("type")
            data = rec.get("data")
            if t == "user_message":
                n += 1
                if not title and isinstance(data, dict):
                    title = str(data.get("content") or "")
            elif t == "session_rename" and isinstance(data, dict):
                renamed = str(data.get("title") or "")
    except Exception:
        pass
    return renamed or title, n


# ─── Aggregation ───────────────────────────────────────────────────────────

class _Pricer:
    """List-rate costs per call, with one price lookup per model."""

    def __init__(self) -> None:
        self._rates: dict[str, tuple[float, float, float, float] | None] = {}

    def rates(self, model: str) -> tuple[float, float, float, float] | None:
        if model not in self._rates:
            try:
                from axon.providers.pricing import price_for
                q = price_for(model)
                self._rates[model] = (float(q.input), float(q.output), float(q.cache_read), float(q.cache_write)) \
                    if getattr(q, "priced", True) else None
            except Exception:
                self._rates[model] = None
        return self._rates[model]

    def costs(self, r: UsageRec) -> tuple[float, float]:
        """(uncached cost, cached list cost) of one call at the model's list rates."""
        rates = self.rates(r.model)
        if not rates:
            return 0.0, 0.0
        qi, qo, qr, qw = rates
        uncached_in = max(0, r.input - r.cache_read - r.cache_write)
        uncached = (r.input * qi + r.output * qo) / 1e6
        listed = (uncached_in * qi + r.cache_read * qr + r.cache_write * qw + r.output * qo) / 1e6
        return uncached, listed


def _day_range(period: Period, recs: Sequence[UsageRec], today: date) -> list[date]:
    if period.since is not None and period.days:
        start = datetime.fromtimestamp(period.since).date()
    elif recs:
        start = max(recs[0].day, today - timedelta(days=MAX_ALL_DAYS - 1))
    else:
        start = today
    n = (today - start).days + 1
    return [start + timedelta(days=i) for i in range(max(1, n))]


def collect(period: Period, project: str | None = None, scope: str = "", today: date | None = None) -> Stats:
    """Aggregate usage + traces for *period* (optionally one project key)."""
    today = today or date.today()
    recs = load_usage(period.since, project)
    traces = load_traces(period.since, project)
    pricer = _Pricer()

    totals = Agg()
    days = {d: Agg() for d in _day_range(period, recs, today)}
    models: dict[str, Agg] = {}
    sources: dict[str, Agg] = {}
    sessions: dict[str, SessionRow] = {}
    latencies: list[float] = []
    for r in recs:
        unc, listed = pricer.costs(r)
        totals.add(r, unc, listed)
        if r.day in days:
            days[r.day].add(r, unc, listed)
        models.setdefault(r.model, Agg()).add(r, unc, listed)
        sources.setdefault(r.source, Agg()).add(r, unc, listed)
        if r.duration_s is not None and r.duration_s > 0:
            latencies.append(r.duration_s)
        if r.session:
            sid = r.session.rsplit("_sub_", 1)[0]  # subagent calls belong to their parent session (source keeps who)
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = SessionRow(id=sid, project=r.project, workspace=r.workspace,
                                                     first_ts=r.ts, last_ts=r.ts)
            s.first_ts = min(s.first_ts, r.ts - (r.duration_s or 0))
            s.last_ts = max(s.last_ts, r.ts)
            s.calls += 1
            s.tokens += r.input + r.output
            s.cost += r.cost
            s.models.add(r.model)

    tools: dict[str, ToolAgg] = {}
    turns: list[TurnRow] = []
    trace_turns: dict[str, int] = {}
    titles: dict[str, str] = {}
    for t in traces:
        raw_sid = str(t.get("_session") or "")
        sid = raw_sid.rsplit("_sub_", 1)[0]
        is_sub = sid != raw_sid
        if not is_sub:  # a subagent's internal turns are not turns of the parent session
            trace_turns[sid] = trace_turns.get(sid, 0) + 1
        prompt = str(t.get("prompt") or "")
        started, ended = _float(t.get("started_at")), _float(t.get("ended_at"))
        if sid and prompt and not is_sub:
            if _int(t.get("turn")) == 1:
                titles[sid] = prompt
            else:
                titles.setdefault(sid, prompt)
        if sid in sessions and started:
            s = sessions[sid]
            s.first_ts = min(s.first_ts, started)
            s.last_ts = max(s.last_ts, ended or started)
        for step in t.get("steps") or ():
            if not isinstance(step, dict) or step.get("kind") != "tool":
                continue
            data = step.get("data") if isinstance(step.get("data"), dict) else {}
            name = str(data.get("name") or "?")
            ta = tools.setdefault(name, ToolAgg(name))
            ta.calls += 1
            dur = data.get("duration_s")
            if dur is None:
                dur = _float(step.get("ended_at")) - _float(step.get("started_at"))
            ta.total_s += max(0.0, _float(dur))
            if data.get("is_error"):
                ta.errors += 1
        cost = _float(t.get("cost"))
        turns.append(TurnRow(
            session=sid, project=str(t.get("_project") or ""), turn=_int(t.get("turn")), prompt=prompt,
            model=str(t.get("model") or ""), cost=cost, llm_calls=_int(t.get("llm_calls")),
            tool_calls=_int(t.get("tool_calls")),
            tokens=_int(t.get("input_tokens")) + _int(t.get("output_tokens")),
            duration_s=max(0.0, ended - started) if ended and started else 0.0, started_at=started,
        ))
    if not latencies:  # older logs without duration_s: fall back to traced LLM steps
        for t in traces:
            for step in t.get("steps") or ():
                if isinstance(step, dict) and step.get("kind") == "llm":
                    d = _float((step.get("data") or {}).get("duration_s"))
                    if d > 0:
                        latencies.append(d)

    srows = sorted(sessions.values(), key=lambda s: s.last_ts, reverse=True)
    for s in srows[:200]:
        s.turns = trace_turns.get(s.id, 0)
        title = titles.get(s.id, "")
        if not title or not s.turns:
            t_title, n_user = _transcript_info(s.project, s.id)
            title = title or t_title
            s.turns = s.turns or n_user
        s.title = _clean(title, 120)

    if project:
        ws = next((r.workspace for r in recs if r.workspace), "")
        scope = scope or f"project {_project_name(project, ws)}"
    return Stats(
        period=period, scope=scope or "all projects", project=project, totals=totals,
        days=sorted(days.items()),
        by_model=sorted(models.items(), key=lambda kv: (kv[1].cost, kv[1].calls), reverse=True),
        by_source=sorted(sources.items(), key=lambda kv: (kv[1].cost, kv[1].calls), reverse=True),
        tools=sorted(tools.values(), key=lambda a: (a.calls, a.total_s), reverse=True),
        turns=sorted((t for t in turns if t.cost > 0 or t.tokens > 0), key=lambda t: (t.cost, t.tokens), reverse=True),
        sessions=srows, latencies=latencies,
    )


# ─── Terminal rendering ────────────────────────────────────────────────────

def _bucketed_days(stats: Stats) -> list[tuple[str, Agg]]:
    """Per-day rows for short ranges; per-week rows (labelled by week start) beyond two weeks."""
    if len(stats.days) <= 14:
        return [(d.strftime("%a %m-%d"), a) for d, a in stats.days]
    weeks: list[tuple[str, Agg]] = []
    for i in range(0, len(stats.days), 7):
        chunk = stats.days[i:i + 7]
        a = Agg()
        for _, x in chunk:
            for k in ("calls", "input", "output", "cache_read", "cache_write", "cost", "uncached_cost", "list_cost"):
                setattr(a, k, getattr(a, k) + getattr(x, k))
        weeks.append((f"wk {chunk[0][0].strftime('%m-%d')}", a))
    return weeks


def _section(title: str) -> list[str]:
    return ["", "  " + ch.paint(title, "BOLD", "ACCENT")]


def render_stats(stats: Stats, width: int | None = None) -> list[str]:
    """Pretty terminal dashboard lines for a :class:`Stats`."""
    W = max(60, min(width or ch.term_width(), 120))
    dot = ch.paint(f" {ch.glyph('dot', '-')} ", "SUBTLE")
    head = ch.paint("Axon usage", "BOLD", "PRIMARY") + dot + stats.period.label + dot + _clean(stats.scope)
    lines = ["", "  " + head]
    if stats.empty:
        lines += ["", "  " + ch.paint("No usage yet for this period.", "MUTED"),
                  "  " + ch.paint("Usage is logged to " + str(storage.user_file("usage.jsonl"))
                                  + " after each LLM call. Try /stats all or --all-projects.", "SUBTLE"), ""]
        return lines

    t = stats.totals
    lab = lambda s: ch.paint(ch.pad(s, 9), "MUTED")  # noqa: E731
    lines.append("")
    lines.append("  " + lab("Spend") + ch.paint(fmt_usd(t.cost), "BOLD", "SUCCESS") + dot
                 + f"{t.calls:,} LLM calls" + dot + f"{len(stats.sessions):,} sessions"
                 + (dot + f"avg {fmt_dur(stats.avg_latency)}/call (p95 {fmt_dur(stats.p95_latency)})"
                    if stats.latencies else ""))
    lines.append("  " + lab("Tokens") + f"{fmt_tokens(t.input)} in" + dot + f"{fmt_tokens(t.output)} out"
                 + dot + f"cache {fmt_tokens(t.cache_read)} read / {fmt_tokens(t.cache_write)} write")
    cache_line = f"hit {ch.paint(fmt_pct(t.hit_pct), 'BOLD')}"
    if t.list_cost > 0:
        sav = t.savings
        word = "saved" if sav >= 0 else "cost extra"
        cache_line += dot + f"{word} {ch.paint(fmt_usd(abs(sav)), 'SUCCESS' if sav >= 0 else 'WARNING')} vs uncached"
        cache_line += ch.paint(f" ({fmt_usd(t.cost + max(0.0, sav))} {ch.glyph('arrow', '->')} {fmt_usd(t.cost)})", "SUBTLE")
    lines.append("  " + lab("Cache") + cache_line)
    mix = [(t.uncached_input, "INFO"), (t.cache_read, "SUCCESS"), (t.cache_write, "WARNING"), (t.output, "CORAL")]
    if sum(v for v, _ in mix) > 0:
        bar_w = max(20, min(48, W - 14))
        lines.append("  " + lab("Mix") + ch.stacked_bar(mix, bar_w))
        lines.append("  " + " " * 9 + ch.legend([("input", "INFO"), ("cache read", "SUCCESS"),
                                                  ("cache write", "WARNING"), ("output", "CORAL")]))

    # Cost over time
    day_costs = [a.cost for _, a in stats.days]
    if len(stats.days) > 1:
        lines += _section("Cost over time")
        per_day = t.cost / max(1, len(stats.days))
        lines.append("  " + ch.paint(ch.sparkline(day_costs, width=min(len(day_costs), W - 30)), "PRIMARY")
                     + "  " + ch.paint(f"{fmt_usd(per_day)}/day avg · peak {fmt_usd(max(day_costs))}", "MUTED"))
        rows = [(label, a.cost, f"{fmt_usd(a.cost):>9}  {a.calls:>4} calls") for label, a in _bucketed_days(stats)]
        lines += ch.bar_chart(rows, width=min(W, 96), color="PRIMARY", indent=2)

    # Models
    if stats.by_model:
        lines += _section("By model")
        hi = max((a.cost for _, a in stats.by_model), default=0) or 1
        rows = [[_clean(m, 40), f"{a.calls:,}", fmt_tokens(a.input), fmt_tokens(a.output), fmt_pct(a.hit_pct),
                 fmt_usd(a.cost), ch.hbar(a.cost, hi, 14, "ACCENT")] for m, a in stats.by_model[:10]]
        lines += ch.table(["model", "calls", "input", "output", "cache hit", "cost", ""], rows,
                          aligns="lrrrrrl", max_width=W)

    # Sources (main agent vs subagents vs auxiliary calls)
    if stats.by_source:
        lines += _section("By source")
        rows = [[_clean(_source_label(s), 32), f"{a.calls:,}", fmt_tokens(a.input + a.output), fmt_usd(a.cost),
                 fmt_pct(a.cost / t.cost * 100) if t.cost else "-"] for s, a in stats.by_source[:10]]
        lines += ch.table(["source", "calls", "tokens", "cost", "share"], rows, aligns="lrrrr", max_width=W)

    # Tools
    if stats.tools:
        lines += _section("Top tools")
        hi = max(a.calls for a in stats.tools) or 1
        rows = [[_clean(a.name, 32), f"{a.calls:,}", fmt_dur(a.total_s), fmt_dur(a.total_s / a.calls if a.calls else 0),
                 ch.paint(str(a.errors), "ERROR") if a.errors else ch.paint("0", "SUBTLE"),
                 ch.hbar(a.calls, hi, 14, "INFO")] for a in stats.tools[:10]]
        lines += ch.table(["tool", "calls", "time", "avg", "errors", ""], rows, aligns="lrrrrl", max_width=W)

    # Turns
    if stats.turns:
        lines += _section("Most expensive turns")
        rows = [[_clean(x.prompt, 60) or "(no prompt)", fmt_usd(x.cost), fmt_tokens(x.tokens), str(x.llm_calls),
                 str(x.tool_calls), fmt_dur(x.duration_s)] for x in stats.turns[:5]]
        lines += ch.table(["prompt", "cost", "tokens", "llm", "tools", "time"], rows, aligns="lrrrrr", max_width=W)

    if stats.sessions:
        lines += _section("Top sessions")
        top = sorted(stats.sessions, key=lambda s: s.cost, reverse=True)[:5]
        rows = [[s.title or s.id, datetime.fromtimestamp(s.last_ts).strftime("%m-%d %H:%M"), str(s.turns),
                 str(s.calls), fmt_dur(s.duration_s), fmt_usd(s.cost)] for s in top]
        lines += ch.table(["session", "last", "turns", "calls", "span", "cost"], rows, aligns="lrrrrr", max_width=W)

    lines += ["", "  " + ch.paint("/stats today|7d|30d|all [--all-projects] · /report for an HTML dashboard", "SUBTLE"), ""]
    return lines


def _emit(lines: Iterable[str]) -> None:
    text = "\n".join(lines)
    try:
        from axon.ui import theme
        text = theme.safe_text(text)
        lock = getattr(theme, "OUTPUT_LOCK", None)
    except Exception:
        lock = None
    try:
        if lock is not None:
            with lock:
                print(text, flush=True)
        else:
            print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


# ─── /stats ────────────────────────────────────────────────────────────────

def _workspace(agent: Any) -> Path | None:
    ws = getattr(getattr(agent, "settings", None), "workspace", None)
    return Path(ws) if ws else None


def parse_stats_args(arg: str, workspace: Path | None) -> tuple[Period, str | None, str] | str:
    """-> (period, project_key, scope_label) or an error message."""
    period: Period | None = None
    all_projects = False
    for tok in (arg or "").split():
        low = tok.lower()
        if low in ("--all-projects", "--all", "-a", "--global"):
            all_projects = True
        elif low in ("--project", "-p"):
            all_projects = False
        else:
            p = parse_period(low)
            if p is None:
                return f"Unknown argument '{tok}'. Usage: /stats [today|7d|30d|all|<n>d] [--project|--all-projects]"
            period = p
    period = period or parse_period(DEFAULT_PERIOD)
    assert period is not None
    if all_projects or workspace is None:
        return period, None, "all projects"
    key = _project_key(workspace)
    return period, key, f"project {workspace.name or key}"


def cmd_stats(agent: Any, arg: str) -> None:
    parsed = parse_stats_args(arg, _workspace(agent))
    if isinstance(parsed, str):
        _emit(["  " + ch.paint(parsed, "WARNING")])
        return
    period, key, scope = parsed
    _emit(render_stats(collect(period, key, scope)))


# ─── /budget ───────────────────────────────────────────────────────────────

def _apply_settings(agent: Any, **changes: Any) -> None:
    try:
        from axon.commands.common import update_settings
        update_settings(agent, **changes)
        return
    except (ImportError, AttributeError):
        pass
    agent.settings = agent.settings.model_copy(update=changes)
    for dep in ("permissions", "context"):
        obj = getattr(agent, dep, None)
        if obj is not None and hasattr(obj, "settings"):
            try:
                obj.settings = agent.settings
            except Exception:
                pass


def _spent(agent: Any) -> Decimal:
    ledger = getattr(agent, "ledger", None)
    try:
        return Decimal(str(ledger.total())) if ledger is not None else Decimal("0")
    except Exception:
        return Decimal("0")


def parse_amount(text: str) -> Decimal | None:
    s = text.strip().replace("$", "").replace(",", "").replace("usd", "").strip()
    try:
        v = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return v if v.is_finite() and v >= 0 else None


def budget_lines(agent: Any) -> list[str]:
    settings = getattr(agent, "settings", None)
    try:
        ceiling = Decimal(str(getattr(settings, "session_cost_ceiling", 0) or 0))
    except (InvalidOperation, ValueError):
        ceiling = Decimal("0")
    spent = _spent(agent)
    head = "  " + ch.paint("Session budget", "BOLD", "PRIMARY")
    if ceiling <= 0:
        return [head, f"  Spent {ch.paint(fmt_usd(float(spent)), 'BOLD')} this session · "
                + ch.paint("no limit set", "MUTED"),
                "  " + ch.paint("Set one with /budget <amount>, e.g. /budget 5", "SUBTLE")]
    remaining = ceiling - spent
    frac = float(spent / ceiling) if ceiling else 0.0
    color = "SUCCESS" if frac < 0.75 else ("WARNING" if frac < 1 else "ERROR")
    lines = [head,
             f"  {ch.hbar(float(spent), float(ceiling), 30, color)}  {ch.paint(fmt_pct(min(frac, 9.99) * 100), color)}",
             f"  Spent {ch.paint(fmt_usd(float(spent)), 'BOLD')} of {fmt_usd(float(ceiling))} · "
             + (ch.paint(f"{fmt_usd(float(remaining))} remaining", "SUCCESS" if frac < 0.75 else "WARNING")
                if remaining > 0 else ch.paint("limit reached - new turns will not start", "ERROR"))]
    lines.append("  " + ch.paint("New turns stop once spending reaches the limit. /budget off removes it.", "SUBTLE"))
    return lines


def cmd_budget(agent: Any, arg: str) -> None:
    if getattr(agent, "settings", None) is None:
        _emit(["  " + ch.paint("No active session settings.", "WARNING")])
        return
    a = (arg or "").strip().lower()
    if a:
        if a in ("off", "none", "unlimited", "disable", "clear", "0"):
            new = Decimal("0")
        else:
            new = parse_amount(a)
            if new is None:
                _emit(["  " + ch.paint(f"Invalid amount '{arg.strip()}'. Usage: /budget [amount|off]", "WARNING")])
                return
        try:
            _apply_settings(agent, session_cost_ceiling=new)
        except Exception as e:
            _emit(["  " + ch.paint(f"Could not update the budget: {e}", "ERROR")])
            return
    _emit([""] + budget_lines(agent) + [""])


# ─── HTML report ───────────────────────────────────────────────────────────

_PALETTE = ("--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8")

_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--good:#006300;--bad:#d03b3b;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--good:#0ca30c;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--good:#0ca30c;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:32px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px}
.sub{color:var(--ink2);margin:0 0 24px}.muted{color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:16px;min-width:0}
.kpi .v{font-size:24px;font-weight:600;margin-top:2px}.kpi .l{color:var(--ink2);font-size:12px}
.kpi .h{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
.full{margin-bottom:16px}
.grid2.eq{grid-template-columns:1fr 1fr}
@media (max-width:820px){.grid2,.grid2.eq{grid-template-columns:1fr}}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:0 0 8px;padding:0;list-style:none;font-size:12px;color:var(--ink2)}
.legend li{display:flex;align-items:center;gap:6px}.sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.dl{list-style:none;margin:8px 0 0;padding:0;font-size:13px}
.dl li{display:flex;align-items:center;gap:8px;padding:3px 0}.dl .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tw{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--axis);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.t{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mbar{height:8px;border-radius:4px;background:var(--s1);min-width:2px}
.meter{height:10px;border-radius:5px;background:var(--grid);overflow:hidden;margin:8px 0}
.meter>span{display:block;height:100%;background:var(--s3);border-radius:5px}
.err{color:var(--bad);font-weight:600}
.empty{text-align:center;padding:64px 16px;color:var(--ink2)}
footer{color:var(--muted);font-size:12px;margin-top:24px}
"""


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def _x_label_idx(n: int, max_labels: int = 8) -> set[int]:
    if n <= max_labels:
        return set(range(n))
    step = math.ceil(n / max_labels)
    idx = set(range(0, n, step))
    last = max(idx)
    if n - 1 - last < step / 2:  # keep the final label without colliding with its neighbour
        idx.discard(last)
    idx.add(n - 1)
    return idx


def svg_line_chart(points: Sequence[tuple[str, float, str]], fmt=fmt_usd, w: int = 720, h: int = 240) -> str:
    """Single-series line chart: points of (x label, value, tooltip)."""
    if not points:
        return ""
    ml, mr, mt, mb = 56, 12, 12, 28
    pw, ph = w - ml - mr, h - mt - mb
    hi = _nice_max(max(v for _, v, _ in points))
    n = len(points)
    xs = [ml + (pw * i / (n - 1) if n > 1 else pw / 2) for i in range(n)]
    ys = [mt + ph - (v / hi) * ph for _, v, _ in points]
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Cost per day line chart">']
    for k in range(5):
        y = mt + ph - ph * k / 4
        out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--{"axis" if k == 0 else "grid"})" stroke-width="1"/>')
        out.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{_e(fmt(hi * k / 4))}</text>')
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    if n > 1:
        area = path + f" L{xs[-1]:.1f},{mt + ph:.1f} L{xs[0]:.1f},{mt + ph:.1f} Z"
        out.append(f'<path d="{area}" fill="var(--s1)" fill-opacity="0.10" stroke="none"/>')
    out.append(f'<path d="{path}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    labels = _x_label_idx(n)
    slot = pw / max(1, n)
    for i, ((lab, v, tip), x, y) in enumerate(zip(points, xs, ys)):
        out.append(f'<g><rect x="{x - slot / 2:.1f}" y="{mt}" width="{slot:.1f}" height="{ph}" fill="transparent"/>'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{4 if n <= 40 else 2.5}" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"/>'
                   f'<title>{_e(tip)}</title></g>')
        if i in labels:
            out.append(f'<text x="{x:.1f}" y="{h - 8}" text-anchor="middle">{_e(lab)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_stacked_bars(cols: Sequence[tuple[str, Sequence[float], str]], series: Sequence[str],
                     w: int = 720, h: int = 240) -> str:
    """Stacked columns: cols of (x label, values per series, tooltip). Series use palette slots in order."""
    if not cols:
        return ""
    ml, mr, mt, mb = 56, 12, 12, 28
    pw, ph = w - ml - mr, h - mt - mb
    hi = _nice_max(max(sum(v) for _, v, _ in cols))
    n = len(cols)
    slot = pw / n
    bw = max(2.0, min(36.0, slot * 0.7))
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Tokens per day stacked bar chart">']
    for k in range(5):
        y = mt + ph - ph * k / 4
        out.append(f'<line x1="{ml}" x2="{w - mr}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--{"axis" if k == 0 else "grid"})" stroke-width="1"/>')
        out.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{_e(fmt_tokens(hi * k / 4))}</text>')
    labels = _x_label_idx(n)
    for i, (lab, vals, tip) in enumerate(cols):
        x = ml + slot * i + (slot - bw) / 2
        y = mt + ph
        out.append("<g>")
        for j, v in enumerate(vals):
            if v <= 0:
                continue
            hh = v / hi * ph
            y -= hh
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(0.5, hh):.1f}" rx="2" '
                       f'fill="var({_PALETTE[j % len(_PALETTE)]})" stroke="var(--surface)" stroke-width="1"/>')
        out.append(f'<rect x="{ml + slot * i:.1f}" y="{mt}" width="{slot:.1f}" height="{ph}" fill="transparent"/>'
                   f"<title>{_e(tip)}</title></g>")
        if i in labels:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" text-anchor="middle">{_e(lab)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_donut(parts: Sequence[tuple[str, float]], center: str, size: int = 200) -> str:
    total = sum(v for _, v in parts)
    if total <= 0:
        return ""
    c, r_out, r_in = size / 2, size / 2 - 4, size / 2 - 30
    out = [f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="Cost by model donut chart" style="max-width:{size}px;margin:0 auto">']
    a0 = -math.pi / 2
    for j, (label, v) in enumerate(parts):
        frac = v / total
        if frac <= 0:
            continue
        color = f"var({_PALETTE[j % len(_PALETTE)]})"
        tip = f"{label}: {fmt_usd(v)} ({frac * 100:.1f}%)"
        if frac >= 0.9999:
            out.append(f'<circle cx="{c}" cy="{c}" r="{(r_out + r_in) / 2}" fill="none" stroke="{color}" '
                       f'stroke-width="{r_out - r_in}"><title>{_e(tip)}</title></circle>')
            break
        a1 = a0 + frac * 2 * math.pi
        large = 1 if a1 - a0 > math.pi else 0
        p = [(c + r_out * math.cos(a0), c + r_out * math.sin(a0)), (c + r_out * math.cos(a1), c + r_out * math.sin(a1)),
             (c + r_in * math.cos(a1), c + r_in * math.sin(a1)), (c + r_in * math.cos(a0), c + r_in * math.sin(a0))]
        d = (f"M{p[0][0]:.2f},{p[0][1]:.2f} A{r_out},{r_out} 0 {large} 1 {p[1][0]:.2f},{p[1][1]:.2f} "
             f"L{p[2][0]:.2f},{p[2][1]:.2f} A{r_in},{r_in} 0 {large} 0 {p[3][0]:.2f},{p[3][1]:.2f} Z")
        out.append(f'<path d="{d}" fill="{color}" stroke="var(--surface)" stroke-width="2"><title>{_e(tip)}</title></path>')
        a0 = a1
    out.append(f'<text x="{c}" y="{c + 2}" text-anchor="middle" style="fill:var(--ink);font-size:18px;font-weight:600">{_e(center)}</text>')
    out.append(f'<text x="{c}" y="{c + 20}" text-anchor="middle">total</text></svg>')
    return "".join(out)


def _legend_html(items: Sequence[str]) -> str:
    return '<ul class="legend">' + "".join(
        f'<li><span class="sw" style="background:var({_PALETTE[j % len(_PALETTE)]})"></span>{_e(t)}</li>'
        for j, t in enumerate(items)) + "</ul>"


def _table_html(headers: Sequence[tuple[str, str]], rows: Sequence[Sequence[str]]) -> str:
    """headers: (label, css class); row cells are pre-escaped HTML."""
    head = "".join(f'<th class="{c}">{_e(h)}</th>' for h, c in headers)
    body = "".join("<tr>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r)) + "</tr>"
                   for r in rows)
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render_html(stats: Stats) -> str:
    t = stats.totals
    gen = datetime.fromtimestamp(stats.generated_at).strftime("%Y-%m-%d %H:%M")
    title = "Axon usage report"
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>",
             f"<h1>{_e(title)}</h1>",
             f'<p class="sub">{_e(stats.period.label)} · {_e(stats.scope)} · generated {_e(gen)}</p>']
    if stats.empty:
        parts.append('<div class="card empty"><h2>No usage yet</h2><p>Axon logs every LLM call to '
                     f"<code>{_e(storage.user_file('usage.jsonl'))}</code>. Run a few prompts, then regenerate "
                     "this report (or widen it with <code>--days 0</code> / <code>--all-projects</code>).</p></div>")
        parts.append("</main></body></html>")
        return "".join(parts)

    sav = t.savings
    kpis = [
        ("Total cost", fmt_usd(t.cost), f"{fmt_usd(t.cost / max(1, len(stats.days)))} per day"),
        ("LLM calls", f"{t.calls:,}", f"avg {fmt_dur(stats.avg_latency)} · p95 {fmt_dur(stats.p95_latency)}"
         if stats.latencies else "no latency data"),
        ("Sessions", f"{len(stats.sessions):,}", f"{sum(s.turns for s in stats.sessions):,} turns"),
        ("Tokens", f"{fmt_tokens(t.input + t.output)}", f"{fmt_tokens(t.input)} in · {fmt_tokens(t.output)} out"),
        ("Cache hit rate", fmt_pct(t.hit_pct), f"{fmt_tokens(t.cache_read)} read · {fmt_tokens(t.cache_write)} written"),
        ("Cache savings", fmt_usd(sav), "vs. the same calls uncached" if t.list_cost > 0 else "no priced models"),
    ]
    parts.append('<section class="kpis">' + "".join(
        f'<div class="card kpi"><div class="l">{_e(l)}</div><div class="v">{_e(v)}</div><div class="h">{_e(h)}</div></div>'
        for l, v, h in kpis) + "</section>")

    # Cost per day + model donut
    pts = [(d.strftime("%m-%d"), a.cost, f"{d.strftime('%a %Y-%m-%d')}: {fmt_usd(a.cost)} · {a.calls} calls")
           for d, a in stats.days]
    top_models = stats.by_model[:5]
    other = sum(a.cost for _, a in stats.by_model[5:])
    donut_parts = [(m, a.cost) for m, a in top_models] + ([("Other", other)] if other > 0 else [])
    donut = svg_donut(donut_parts, fmt_usd(t.cost))
    model_list = "".join(
        f'<li><span class="sw" style="background:var({_PALETTE[j % len(_PALETTE)]})"></span><span class="n" title="{_e(m)}">{_e(m)}</span>'
        f'<span class="r">{_e(fmt_usd(v))}</span><span class="muted">{v / t.cost * 100 if t.cost else 0:.0f}%</span></li>'
        for j, (m, v) in enumerate(donut_parts))
    parts.append('<section class="grid2">'
                 f'<div class="card"><h2>Cost per day</h2>{svg_line_chart(pts)}</div>'
                 f'<div class="card"><h2>Cost by model</h2>{donut or "<p class=muted>All calls were unpriced ($0).</p>"}'
                 f'<ul class="dl">{model_list}</ul></div></section>')

    # Tokens per day stacked
    series = ["Uncached input", "Output", "Cache read", "Cache write"]
    cols = [(d.strftime("%m-%d"), (a.uncached_input, a.output, a.cache_read, a.cache_write),
             f"{d.strftime('%a %Y-%m-%d')}: {fmt_tokens(a.uncached_input)} uncached in · {fmt_tokens(a.output)} out · "
             f"{fmt_tokens(a.cache_read)} cache read · {fmt_tokens(a.cache_write)} cache write")
            for d, a in stats.days]
    hit_w = max(0.0, min(100.0, t.hit_pct))
    cache_card = (f'<div class="card"><h2>Cache efficiency</h2><div class="kpi"><div class="v">{_e(fmt_pct(t.hit_pct))}</div>'
                  f'<div class="h">of input tokens served from cache</div></div>'
                  f'<div class="meter" role="img" aria-label="cache hit rate {_e(fmt_pct(t.hit_pct))}"><span style="width:{hit_w:.1f}%"></span></div>'
                  + _table_html([("", ""), ("", "r")], [
                      ["Cache read tokens", _e(fmt_tokens(t.cache_read))],
                      ["Cache write tokens", _e(fmt_tokens(t.cache_write))],
                      ["Uncached input tokens", _e(fmt_tokens(t.uncached_input))],
                      ["Billed cost", _e(fmt_usd(t.cost))],
                      ["Without caching", _e(fmt_usd(t.cost + sav)) if t.list_cost > 0 else "-"],
                      ["Net saved", f'<b style="color:var(--good)">{_e(fmt_usd(sav))}</b>' if sav >= 0 else _e(fmt_usd(sav))],
                  ]) + "</div>")
    parts.append('<section class="grid2">'
                 f'<div class="card"><h2>Tokens per day</h2>{_legend_html(series)}{svg_stacked_bars(cols, series)}</div>'
                 f"{cache_card}</section>")

    # Models & sources tables
    mrows = [[f'<span title="{_e(m)}">{_e(m)}</span>', _e(f"{a.calls:,}"), _e(fmt_tokens(a.input)), _e(fmt_tokens(a.output)),
              _e(fmt_pct(a.hit_pct)), _e(fmt_usd(a.cost))] for m, a in stats.by_model]
    srows = [[_e(_source_label(s)), _e(f"{a.calls:,}"), _e(fmt_tokens(a.input + a.output)), _e(fmt_usd(a.cost)),
              _e(fmt_pct(a.cost / t.cost * 100) if t.cost else "-")] for s, a in stats.by_source]
    parts.append('<section class="grid2 eq">'
                 '<div class="card"><h2>Models</h2>' + _table_html(
                     [("Model", "t"), ("Calls", "r"), ("Input", "r"), ("Output", "r"), ("Cache hit", "r"), ("Cost", "r")], mrows)
                 + '</div><div class="card"><h2>Sources</h2>' + _table_html(
                     [("Source", "t"), ("Calls", "r"), ("Tokens", "r"), ("Cost", "r"), ("Share", "r")], srows)
                 + "</div></section>")

    # Tools
    if stats.tools:
        hi = max(a.calls for a in stats.tools) or 1
        trows = [[_e(a.name), _e(f"{a.calls:,}"), _e(fmt_dur(a.total_s)), _e(fmt_dur(a.total_s / a.calls if a.calls else 0)),
                  f'<span class="err">{a.errors}</span>' if a.errors else "0",
                  f'<div class="mbar" style="width:{a.calls / hi * 100:.1f}%"></div>'] for a in stats.tools[:30]]
        parts.append('<section class="card full"><h2>Tool usage</h2>' + _table_html(
            [("Tool", "t"), ("Calls", "r"), ("Total time", "r"), ("Avg", "r"), ("Errors", "r"), ("", "")], trows)
            + "</section>")
    else:
        parts.append('<section class="card full"><h2>Tool usage</h2><p class="muted">No traced tool calls in this period.</p></section>')

    # Expensive turns
    if stats.turns:
        rows = [[_e(_clean(x.prompt, 160) or "(no prompt)"), _e(x.model), _e(fmt_tokens(x.tokens)), _e(str(x.llm_calls)),
                 _e(str(x.tool_calls)), _e(fmt_dur(x.duration_s)), _e(fmt_usd(x.cost))] for x in stats.turns[:15]]
        parts.append('<section class="card full"><h2>Most expensive turns</h2>' + _table_html(
            [("Prompt", "t"), ("Model", ""), ("Tokens", "r"), ("LLM calls", "r"), ("Tools", "r"), ("Time", "r"), ("Cost", "r")], rows)
            + "</section>")

    # Sessions
    if stats.sessions:
        rows = [[_e(datetime.fromtimestamp(s.first_ts).strftime("%Y-%m-%d %H:%M")),
                 f'<span title="{_e(s.id)}">{_e(s.title or s.id)}</span>', _e(_project_name(s.project, s.workspace)),
                 _e(str(s.turns)), _e(f"{s.calls:,}"), _e(fmt_tokens(s.tokens)), _e(fmt_dur(s.duration_s)), _e(fmt_usd(s.cost))]
                for s in stats.sessions[:100]]
        more = f'<p class="muted">Showing 100 of {len(stats.sessions):,} sessions.</p>' if len(stats.sessions) > 100 else ""
        parts.append('<section class="card full"><h2>Sessions</h2>' + _table_html(
            [("Started", ""), ("Session", "t"), ("Project", ""), ("Turns", "r"), ("Calls", "r"), ("Tokens", "r"),
             ("Span", "r"), ("Cost", "r")], rows) + more + "</section>")

    parts.append(f'<footer>Data: {_e(storage.user_file("usage.jsonl"))} and per-project traces under '
                 f'{_e(_projects_root())}. Costs use Axon\'s recorded per-call prices; cache savings compare each call '
                 "with the same tokens billed at the full input rate.</footer></main></body></html>")
    return "".join(parts)


def default_report_path(today: date | None = None) -> Path:
    return storage.cache_dir("reports") / f"axon-report-{(today or date.today()).isoformat()}.html"


def write_report(stats: Stats, out: str | Path | None = None) -> Path:
    if out:
        path = Path(os.path.expanduser(str(out)))
        if path.is_dir() or str(out).endswith(("/", os.sep)):
            path = path / default_report_path().name
    else:
        path = default_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_html(stats), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _link(path: Path) -> str:
    uri = path.resolve().as_uri()
    try:
        from axon.ui.theme import hyperlink
        return hyperlink(uri, uri)
    except Exception:
        return uri


def _report_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Generate a self-contained HTML usage dashboard.")
    p.add_argument("--days", type=int, default=DEFAULT_REPORT_DAYS, help="days to include (0 = all time; default 30)")
    p.add_argument("--out", default=None, help="output file or directory (default ~/.axon/cache/reports/)")
    p.add_argument("--project", nargs="?", const=".", default=None, metavar="PATH",
                   help="only this project (default: current directory)")
    p.add_argument("--all-projects", action="store_true", help="include every project")
    p.add_argument("--open", action="store_true", help="open the report in the default browser")
    return p


def run_report(argv: Sequence[str], default_workspace: Path | None = None, prog: str = "axon report") -> int:
    parser = _report_parser(prog)
    try:
        ns = parser.parse_args(list(argv))
    except SystemExit as e:  # argparse already printed usage / help
        return int(e.code or 0)
    workspace: Path | None = None
    if ns.project is not None:
        workspace = Path(os.path.expanduser(ns.project)).resolve()
    elif default_workspace is not None and not ns.all_projects:
        workspace = default_workspace
    if ns.all_projects:
        workspace = None
    key = _project_key(workspace) if workspace else None
    scope = f"project {workspace.name}" if workspace else "all projects"
    stats = collect(_period_from_days(ns.days), key, scope)
    try:
        path = write_report(stats, ns.out)
    except OSError as e:
        _emit(["  " + ch.paint(f"Could not write the report: {e}", "ERROR")])
        return 1
    note = "no usage yet - the report explains how to get data" if stats.empty else \
        f"{stats.totals.calls:,} calls · {fmt_usd(stats.totals.cost)} · {stats.period.label} · {scope}"
    _emit(["  " + ch.paint(f"{ch.glyph('ok', '+')} Report written", "SUCCESS") + ch.paint(f"  ({note})", "MUTED"),
           "  " + _link(path)])
    if ns.open:
        try:
            import webbrowser
            webbrowser.open(path.resolve().as_uri())
        except Exception:
            pass
    return 0


def _split_args(arg: str) -> list[str]:
    import shlex
    try:
        return shlex.split(arg or "", posix=os.name != "nt")
    except ValueError:
        return (arg or "").split()


def cmd_report(agent: Any, arg: str) -> None:
    run_report(_split_args(arg), default_workspace=_workspace(agent), prog="/report")


def cli_report(argv: list[str]) -> int:
    return run_report(argv)


def cli_stats(argv: list[str]) -> int:
    """`axon stats [period] [--project|--all-projects]` (defaults to the current directory's project)."""
    parsed = parse_stats_args(" ".join(argv), Path.cwd())
    if isinstance(parsed, str):
        sys.stderr.write(parsed.replace("/stats", "axon stats") + "\n")
        return 2
    period, key, scope = parsed
    _emit(render_stats(collect(period, key, scope)))
    return 0


# ─── UsageStats tool (for the model and the analytics subagent) ───────────

GROUP_BY = ("model", "source", "day", "tool", "session")
AGENTS_DIR = Path(__file__).resolve().parent / "agents"


def _plain_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], aligns: str) -> list[str]:
    return [ch._ANSI_RE.sub("", x).rstrip() for x in ch.table(headers, rows, aligns=aligns, indent=0)]


def usage_summary_text(stats: Stats, group_by: str = "model", limit: int = 25) -> str:
    """Compact, ANSI-free text report (totals + one grouped table) for LLM consumption."""
    t = stats.totals
    head = f"Usage {stats.period.label} ({stats.scope})"
    if stats.empty:
        return f"{head}: no usage recorded yet (log: {storage.user_file('usage.jsonl')})."
    lines = [
        head,
        f"cost {fmt_usd(t.cost)} | {t.calls} LLM calls | {len(stats.sessions)} sessions | "
        f"tokens in {t.input:,} (uncached {t.uncached_input:,}) out {t.output:,} | "
        f"cache read {t.cache_read:,} write {t.cache_write:,} hit {fmt_pct(t.hit_pct)} | "
        f"cache savings {fmt_usd(t.savings)}" + (" (no priced models)" if t.list_cost <= 0 else "")
        + (f" | latency avg {fmt_dur(stats.avg_latency)} p95 {fmt_dur(stats.p95_latency)}" if stats.latencies else ""),
        "",
    ]

    def agg_row(name: str, a: Agg) -> list[str]:
        return [name, f"{a.calls:,}", f"{a.input:,}", f"{a.output:,}", f"{a.cache_read:,}", f"{a.cache_write:,}",
                fmt_pct(a.hit_pct), fmt_usd(a.cost), fmt_pct(a.cost / t.cost * 100) if t.cost else "-"]

    agg_head = ["", "calls", "input", "output", "cache_read", "cache_write", "hit", "cost", "share"]
    if group_by == "source":
        rows = [agg_row(f"{_source_label(s)} [{s}]", a) for s, a in stats.by_source[:limit]]
        lines += _plain_table(["source"] + agg_head[1:], rows, "lrrrrrrrr")
    elif group_by == "day":
        rows = [agg_row(d.isoformat(), a) for d, a in stats.days[-limit:]]
        lines += _plain_table(["day"] + agg_head[1:], rows, "lrrrrrrrr")
    elif group_by == "tool":
        if not stats.tools:
            lines.append("No traced tool calls in this period.")
        else:
            rows = [[a.name, f"{a.calls:,}", fmt_dur(a.total_s), fmt_dur(a.total_s / a.calls if a.calls else 0), str(a.errors)]
                    for a in stats.tools[:limit]]
            lines += _plain_table(["tool", "calls", "total_time", "avg", "errors"], rows, "lrrrr")
    elif group_by == "session":
        rows = [[_clean(s.title or "(untitled)", 60), s.id, datetime.fromtimestamp(s.last_ts).strftime("%Y-%m-%d %H:%M"),
                 str(s.turns), f"{s.calls:,}", f"{s.tokens:,}", fmt_dur(s.duration_s), fmt_usd(s.cost)]
                for s in sorted(stats.sessions, key=lambda x: x.cost, reverse=True)[:limit]]
        lines += _plain_table(["session", "id", "last", "turns", "calls", "tokens", "span", "cost"], rows, "lllrrrrr")
    else:
        rows = [agg_row(m, a) for m, a in stats.by_model[:limit]]
        lines += _plain_table(["model"] + agg_head[1:], rows, "lrrrrrrrr")
    if group_by != "tool" and stats.turns:
        lines += ["", "Most expensive turns:"]
        lines += [f"- {fmt_usd(x.cost)} · {x.tokens:,} tok · {x.llm_calls} llm / {x.tool_calls} tools · "
                  f"{_clean(x.prompt, 100) or '(no prompt)'}" for x in stats.turns[:5]]
    return "\n".join(lines)


try:
    from axon.tools.base import Tool as _ToolBase
except Exception:  # pragma: no cover - tools layer unavailable
    _ToolBase = object  # type: ignore[assignment,misc]
try:
    from axon.errors import ToolError as _ToolError
except Exception:  # pragma: no cover
    _ToolError = ValueError  # type: ignore[assignment,misc]


class UsageStatsTool(_ToolBase):  # type: ignore[misc,valid-type]
    """Read-only access to Axon's own usage log, so the model can answer cost questions."""

    name = "UsageStats"
    description = (
        "Report Axon's recorded LLM usage: cost, tokens, prompt-cache reads/writes, hit rate and cache savings, "
        "grouped by model, source (main agent vs each subagent / auxiliary call), day, tool or session. "
        "Use it to answer questions like 'how much did I spend this week and on what?'. Read-only."
    )
    schema = {
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["today", "7d", "30d", "all"],
                       "description": "Time window (default 7d)"},
            "group_by": {"type": "string", "enum": list(GROUP_BY), "description": "Breakdown (default model)"},
            "project_only": {"type": "boolean",
                             "description": "Only the current project (default true); false = all projects"},
        },
        "additionalProperties": False,
    }
    readonly = True
    default_permission = "allow"

    def run(self, args: dict[str, Any], ctx: Any) -> str:
        period = parse_period(str(args.get("period") or DEFAULT_PERIOD))
        if period is None:
            raise _ToolError("period must be one of today, 7d, 30d, all")
        group_by = str(args.get("group_by") or "model").lower()
        if group_by not in GROUP_BY:
            raise _ToolError(f"group_by must be one of {', '.join(GROUP_BY)}")
        project_only = args.get("project_only", True)
        if isinstance(project_only, str):
            project_only = project_only.strip().lower() not in ("false", "0", "no")
        ws = getattr(ctx, "workspace", None)
        key = _project_key(ws) if (project_only and ws) else None
        scope = f"project {Path(ws).name}" if key else "all projects"
        return usage_summary_text(collect(period, key, scope), group_by)

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return (output or "error").splitlines()[0][:120]
        first = (output or "").splitlines()[1:2]
        return first[0][:120] if first else "no usage"

    def render_call(self, args: dict[str, Any]) -> str:
        return (f"UsageStats {args.get('period') or DEFAULT_PERIOD} by {args.get('group_by') or 'model'}"
                + ("" if args.get("project_only", True) not in (False, "false") else " (all projects)"))


TOOLS = [UsageStatsTool()]


# ─── /insights (analytics subagent) ───────────────────────────────────────

def analytics_agent_definition() -> Any | None:
    """The bundled ``analytics`` subagent (features/agents/analytics.md) as an AgentDefinition."""
    path = AGENTS_DIR / "analytics.md"
    try:
        from axon.agent.subagent import parse_agent_file
        return parse_agent_file(path, "builtin")
    except Exception:
        return None


def insights_prompt(period: Period, project: str | None, scope: str) -> str:
    stats = collect(period, project, scope)
    snapshot = "\n\n".join(usage_summary_text(stats, g, limit=12) for g in ("model", "source", "tool"))
    return (
        f"Analyse my Axon usage for {period.label} ({scope}). Explain where the money went, how efficient prompt "
        "caching was, which models and which subagents/sources cost most, any expensive turns or failing tools, and "
        "give 3-6 concrete, quantified saving tips. Call the UsageStats tool (period/group_by/project_only) if you "
        "need other breakdowns such as per day or per session. Here is the current snapshot:\n\n" + snapshot
    )


def _print_markdown(text: str) -> None:
    try:
        from axon.ui.markdown import MarkdownStream
        md = MarkdownStream()
        _emit([md.feed(text + "\n") + md.flush()])
    except Exception:
        _emit([text])


def cmd_insights(agent: Any, arg: str) -> None:
    parsed = parse_stats_args(arg, _workspace(agent))
    if isinstance(parsed, str):
        _emit(["  " + ch.paint(parsed.replace("/stats", "/insights"), "WARNING")])
        return
    period, key, scope = parsed
    prompt = insights_prompt(period, key, scope)
    if getattr(agent, "subagents", None) is not None and getattr(agent, "settings", None) is not None:
        try:
            from axon.agent.subagent import run_subagent
            _emit(["  " + ch.paint(f"{ch.glyph('diamond', '#')} analytics subagent · {period.label} · {scope}", "ACCENT")])
            _print_markdown(str(run_subagent(prompt, parent=agent, subagent_type="analytics",
                                             description="Usage insights")))
            return
        except KeyboardInterrupt:
            _emit(["  " + ch.paint("Interrupted.", "WARNING")])
            return
        except Exception as e:  # analytics agent not registered yet, or the Task path failed
            if "Unknown subagent_type" not in str(e):
                _emit(["  " + ch.paint(f"Analytics subagent failed ({e}); answering in the main session.", "WARNING")])
    run_turn = getattr(agent, "run_turn", None)
    if not callable(run_turn):
        _emit(render_stats(collect(period, key, scope)))
        return
    d = analytics_agent_definition()
    persona = getattr(d, "system_prompt", "") if d is not None else ""
    run_turn((f"<analyst-instructions>\n{persona}\n</analyst-instructions>\n\n" if persona else "") + prompt)


COMMANDS = [
    FeatureCommand(name="/stats", handler=cmd_stats, category="Insight", aliases=("/analytics",),
                   description="Usage analytics: cost, tokens, cache, models, sources, tools, expensive turns",
                   usage="/stats [today|7d|30d|all] [--project|--all-projects]"),
    FeatureCommand(name="/budget", handler=cmd_budget, category="Insight",
                   description="Show or set this session's spending limit",
                   usage="/budget [amount|off]"),
    FeatureCommand(name="/report", handler=cmd_report, category="Insight",
                   description="Write a self-contained HTML usage dashboard",
                   usage="/report [--days N] [--out PATH] [--all-projects] [--open]"),
]

COMMANDS.append(FeatureCommand(
    name="/insights", handler=cmd_insights, category="Insight",
    description="Ask the analytics subagent to explain your spending and suggest savings",
    usage="/insights [today|7d|30d|all] [--project|--all-projects]"))

CLI_SUBCOMMANDS = {"report": cli_report, "stats": cli_stats}

__all__ = [
    "COMMANDS", "CLI_SUBCOMMANDS", "Period", "Stats", "Agg", "parse_period", "parse_stats_args", "collect",
    "load_usage", "load_traces", "render_stats", "render_html", "write_report", "run_report", "cmd_stats",
    "cmd_budget", "cmd_report", "budget_lines", "parse_amount", "fmt_usd", "fmt_tokens", "fmt_dur",
    "svg_line_chart", "svg_stacked_bars", "svg_donut", "default_report_path", "UsageStatsTool", "TOOLS",
    "usage_summary_text", "cmd_insights", "insights_prompt", "analytics_agent_definition", "AGENTS_DIR",
]
