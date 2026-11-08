"""Spending data shared by the terminal and the web app: the cost forecast before a turn, budget notices, the
project's daily and monthly budgets, and the today / week / month spending summary.

Plain data only (no web or terminal imports). Everything comes from real records: the usage log
(``usage.jsonl``, one line per model call, compare runs included with source ``compare``), the session
transcript, and the model price table. A figure that cannot be known is returned as ``None``, never guessed.

Forecast: input is the context the next request really carries (what the budget guard projects) plus the
words typed; output and the number of model calls per turn come from the last turns of the same chat. The
result is a range, marked as an estimate by every caller, and it never blocks anything.

Budgets: a session budget is the smaller of ``session_cost_ceiling`` and ``budget_session_usd``; the project
budgets (per day, per month) live in ``state/budgets-<project>.json`` and are measured against the usage log.
A notice is raised once at each of 50%, 80% and 100%.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from axon import storage

NOTICE_STEPS = (50, 80, 100)
DEFAULT_FORECAST_USD = 0.25
MAX_BUDGET_USD = 100_000.0
RECENT_TURNS = 6
CHARS_PER_TOKEN = 3.7
SCOPES = ("day", "month")
SCOPE_WORDS = {"session": "Session budget", "day": "Daily budget", "month": "Monthly budget"}


def _f(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and abs(x) != float("inf") else 0.0


def money(v: float) -> str:
    v = float(v or 0)
    if v == 0:
        return "$0.00"
    if abs(v) < 0.01:
        return f"${v:.4f}"
    if abs(v) < 100:
        return f"${v:.2f}"
    return f"${v:,.0f}"


def tokens_text(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k"
    return str(int(n))


# ─── periods and spending ────────────────────────────────────────────────────

def period_starts(now: float | None = None) -> dict[str, float]:
    """Local-time starts of today, this week (Monday) and this month."""
    d = datetime.fromtimestamp(time.time() if now is None else now)
    midnight = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"day": midnight.timestamp(), "week": (midnight - timedelta(days=midnight.weekday())).timestamp(),
            "month": midnight.replace(day=1).timestamp()}


_rec_lock = threading.Lock()
_rec_cache: dict[str, Any] = {}


def _usage_records(since: float, project: str | None) -> list[Any]:
    """Usage log records since *since* (re-read only when the file changed)."""
    from axon.features import analytics
    path = storage.user_file("usage.jsonl")
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    key = f"{path}|{project}|{int(since)}"
    with _rec_lock:
        hit = _rec_cache.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    recs = analytics.load_usage(since, project)
    with _rec_lock:
        if len(_rec_cache) > 16:
            _rec_cache.clear()
        _rec_cache[key] = (sig, recs)
    return recs


def _sum_since(recs: list[Any], starts: dict[str, float]) -> dict[str, dict[str, float]]:
    out = {k: {"cost": 0.0, "calls": 0, "compare": 0.0} for k in ("today", "week", "month")}
    names = {"today": "day", "week": "week", "month": "month"}
    for r in recs:
        for k, sk in names.items():
            if r.ts >= starts[sk]:
                out[k]["cost"] += r.cost
                out[k]["calls"] += 1
                if r.source == "compare":
                    out[k]["compare"] += r.cost
    return out


def spend_summary(workspace: Path | str | None, now: float | None = None) -> dict[str, Any]:
    """Spending today, this week and this month from the usage log: ``project`` (this folder's project) and ``all``
    projects, each as ``{today, week, month}`` of ``{cost, calls, compare}``; plus the project's budgets."""
    now = time.time() if now is None else now
    starts = period_starts(now)
    since = min(starts.values())
    key = storage.project_key(Path(workspace)) if workspace else None
    project = _sum_since(_usage_records(since, key), starts) if key else None
    everything = _sum_since(_usage_records(since, None), starts)
    return {"project": project, "all": everything, "week_from": "Monday", "budgets": budget_status(workspace, now) if workspace else {},
            "generated": now}


# ─── project budgets (per day, per month) ───────────────────────────────────

def _state_name(workspace: Path | str) -> str:
    return "budgets-" + storage.project_key(Path(workspace))[-120:]


def _period_id(scope: str, now: float) -> str:
    d = datetime.fromtimestamp(now)
    return d.strftime("%Y-%m-%d") if scope == "day" else d.strftime("%Y-%m")


def load_budgets(workspace: Path | str) -> dict[str, Any]:
    """``{"day_usd": float|None, "month_usd": float|None, "warned": {...}}`` (a corrupt file reads as empty)."""
    raw = storage.read_state(_state_name(workspace), {})
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"day_usd": None, "month_usd": None, "warned": {}}
    for k in ("day_usd", "month_usd"):
        v = _f(raw.get(k))
        out[k] = v if 0 < v <= MAX_BUDGET_USD else None
    w = raw.get("warned")
    if isinstance(w, dict):
        out["warned"] = {str(k): [int(x) for x in v if isinstance(x, int)] for k, v in w.items() if isinstance(v, list)}
    return out


def _save(workspace: Path | str, data: dict[str, Any], now: float) -> None:
    live = {f"{s}:{_period_id(s, now)}" for s in SCOPES}
    data["warned"] = {k: v for k, v in data.get("warned", {}).items() if k in live}      # old periods are forgotten
    if not storage.write_state(_state_name(workspace), data):
        raise OSError("could not write the budget file")


def set_project_budget(workspace: Path | str, scope: str, amount: Any, now: float | None = None) -> dict[str, Any]:
    """Set (or with ``None`` / "off" clear) the daily or monthly budget of the project. Raises ValueError."""
    if scope not in SCOPES:
        raise ValueError("The budget is either daily or monthly.")
    now = time.time() if now is None else now
    data = load_budgets(workspace)
    if amount is None or (isinstance(amount, str) and amount.strip().lower() in ("", "off", "none", "clear", "0")):
        data[scope + "_usd"] = None
    else:
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            raise ValueError("The amount must be a number of dollars, like 5 or 2.50.")
        try:
            v = float(str(amount).strip().lstrip("$").replace(",", ""))
        except ValueError:
            raise ValueError("The amount must be a number of dollars, like 5 or 2.50.") from None
        if not (v == v) or v < 0 or v == float("inf"):
            raise ValueError("The amount must be a number of dollars, like 5 or 2.50.")
        if v > MAX_BUDGET_USD:
            raise ValueError("The limit is too large (most is $100,000).")
        data[scope + "_usd"] = v if v > 0 else None
    data["warned"].pop(f"{scope}:{_period_id(scope, now)}", None)       # a new limit starts its notices over
    _save(workspace, data, now)
    return budget_status(workspace, now)


def budget_status(workspace: Path | str, now: float | None = None) -> dict[str, Any]:
    """``{"day": {limit, spent, frac, remaining} | None, "month": ...}`` measured against the project's usage log."""
    now = time.time() if now is None else now
    data = load_budgets(workspace)
    starts = period_starts(now)
    recs = _usage_records(min(starts["day"], starts["month"]), storage.project_key(Path(workspace)))
    spent = {"day": sum(r.cost for r in recs if r.ts >= starts["day"]), "month": sum(r.cost for r in recs if r.ts >= starts["month"])}
    out: dict[str, Any] = {}
    for s in SCOPES:
        lim = data.get(s + "_usd")
        out[s] = None if not lim else {"limit": lim, "spent": spent[s], "frac": spent[s] / lim,
                                       "remaining": max(0.0, lim - spent[s])}
    return out


def session_limit(settings: Any) -> float:
    """The session budget that applies: the smaller of the hard ceiling and the guard, 0 when neither is set."""
    vals = []
    for attr in ("session_cost_ceiling", "budget_session_usd"):
        v = _f(getattr(settings, attr, 0) or 0)
        if v > 0:
            vals.append(v)
    return min(vals) if vals else 0.0


def period_limit_hit(workspace: Path | str, now: float | None = None) -> dict[str, Any] | None:
    """The first project budget (day, then month) already used up, as ``{scope, limit, spent}``, else None."""
    try:
        st = budget_status(workspace, now)
    except Exception:
        return None
    for s in SCOPES:
        b = st.get(s)
        if b and b["spent"] >= b["limit"]:
            return {"scope": s, "limit": b["limit"], "spent": b["spent"]}
    return None


def limit_message(hit: dict[str, Any]) -> str:
    word = "daily" if hit["scope"] == "day" else "monthly"
    return (f"The {word} budget for this project is used up ({money(hit['spent'])} of {money(hit['limit'])}). "
            f"Raise it in Usage, or with /budget {hit['scope']} <amount>, or turn it off with /budget {hit['scope']} off.")


# ─── notices at 50%, 80%, 100% ──────────────────────────────────────────────

def notice_step(frac: float, done: set[int] | list[int]) -> int | None:
    """The highest of 50 / 80 / 100 that *frac* has reached and *done* does not hold yet. Lower steps are marked done too."""
    pct = frac * 100
    reached = [s for s in NOTICE_STEPS if pct >= s]
    if not reached or reached[-1] in done:
        return None
    return reached[-1]


def notice_text(scope: str, step: int, spent: float, limit: float) -> str:
    base = f"{SCOPE_WORDS.get(scope, 'Budget')}: {money(spent)} of {money(limit)}"
    if step >= 100:
        return base + (". New turns will not start." if scope != "session" else ". New turns will not start in this chat.")
    return base + f", {step}% used."


def due_notices(settings: Any, workspace: Path | str, session_spent: float, marks: set[str], now: float | None = None) -> list[dict[str, Any]]:
    """Notices that became due: ``[{scope, step, spent, limit, message}]``. *marks* is the chat's own memory of what it
    already said about the session; the project budgets remember it in their file, so a restart does not repeat them."""
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    lim = session_limit(settings)
    if lim > 0:
        done = {int(m.split(":", 1)[1]) for m in marks if m.startswith("session:")}
        step = notice_step(session_spent / lim, done)
        if step is not None:
            marks.update(f"session:{s}" for s in NOTICE_STEPS if s <= step)
            out.append({"scope": "session", "step": step, "spent": session_spent, "limit": lim,
                        "message": notice_text("session", step, session_spent, lim)})
    try:
        data = load_budgets(workspace)
        if data["day_usd"] or data["month_usd"]:
            st = budget_status(workspace, now)
            changed = False
            for s in SCOPES:
                b = st.get(s)
                if not b:
                    continue
                key = f"{s}:{_period_id(s, now)}"
                done_set = set(data["warned"].get(key, []))
                step = notice_step(b["frac"], done_set)
                if step is not None:
                    data["warned"][key] = sorted(done_set | {x for x in NOTICE_STEPS if x <= step})
                    changed = True
                    out.append({"scope": s, "step": step, "spent": b["spent"], "limit": b["limit"],
                                "message": notice_text(s, step, b["spent"], b["limit"])})
            if changed:
                _save(workspace, data, now)
    except Exception:
        pass
    return out


# ─── forecast ────────────────────────────────────────────────────────────────

@dataclass
class Turns:
    """What the last turns of a chat looked like: model calls, output tokens, and how much each extra call grew the input."""
    count: int = 0
    calls: tuple[int, int] = (1, 1)
    output: tuple[int, int] | None = None
    growth: int = 0
    cache_share: float = 0.0


_turn_cache: dict[str, Any] = {}


def recent_turns(transcript: Path | str | None, limit: int = RECENT_TURNS) -> Turns:
    """Calls, output tokens and cache share of the last *limit* turns, read from the chat's transcript."""
    if not transcript:
        return Turns()
    path = Path(transcript)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return Turns()
    hit = _turn_cache.get(str(path))
    if hit and hit[0] == sig:
        return hit[1]
    from axon.session.store import iter_records
    turns: list[list[dict[str, Any]]] = []
    try:
        for rec in iter_records(path):
            t, d = rec.get("type"), rec.get("data")
            if t == "user_message":
                turns.append([])
            elif t == "assistant_turn" and isinstance(d, dict) and turns:
                u = d.get("usage")
                if isinstance(u, dict):
                    turns[-1].append(u)
    except Exception:
        return Turns()
    done = [t for t in turns if t][-limit:]
    if not done:
        res = Turns()
    else:
        calls = [len(t) for t in done]
        outs = [sum(int(_f(u.get("output"))) for u in t) for t in done]
        grows = [(int(_f(t[-1].get("input"))) - int(_f(t[0].get("input")))) // max(1, len(t) - 1) for t in done if len(t) > 1]
        grows = sorted(g for g in grows if g > 0)
        inp = sum(int(_f(u.get("input"))) for t in done for u in t)
        cached = sum(int(_f(u.get("cache_read"))) for t in done for u in t)
        res = Turns(count=len(done), calls=(min(calls), max(calls)), output=(min(outs), max(outs)),
                    growth=grows[len(grows) // 2] if grows else 0, cache_share=min(1.0, cached / inp) if inp else 0.0)
    if len(_turn_cache) > 32:
        _turn_cache.clear()
    _turn_cache[str(path)] = (sig, res)
    return res


def threshold_usd(settings: Any) -> float:
    """The cost above which a forecast shows under the box; 0 means forecasts are off."""
    raw = getattr(settings, "forecast_threshold_usd", DEFAULT_FORECAST_USD)
    return max(0.0, _f(raw)) if raw is not None else 0.0


def estimate(model: str, context_tokens: int, prompt_tokens: int, turns: Turns, threshold: float,
             provider: str | None = None) -> dict[str, Any] | None:
    """The range one more turn is likely to cost, or None when the model has no known price (nothing is guessed).

    ``show`` is true only when the top of the range reaches *threshold*."""
    try:
        from axon.providers.pricing import price_for
        q = price_for(model, provider)
    except Exception:
        return None
    if not getattr(q, "priced", False) or getattr(q, "local", False):
        return None
    r_in, r_out, r_read = _f(q.input) / 1e6, _f(q.output) / 1e6, _f(q.cache_read) / 1e6
    if r_in <= 0 and r_out <= 0:
        return None
    base = max(0, int(context_tokens)) + max(0, int(prompt_tokens))
    share = turns.cache_share
    first_hi = base * r_in
    first_lo = base * (share * r_read + (1 - share) * r_in)
    extra_rate_lo = r_read if share > 0 else r_in
    extra_rate_hi = r_read if share > 0 else r_in
    c_lo, c_hi = turns.calls
    low = first_lo + sum((base) * extra_rate_lo for _ in range(c_lo - 1))
    high = first_hi + sum((base + k * turns.growth) * extra_rate_hi for k in range(1, c_hi))
    if turns.output is not None:
        low += turns.output[0] * r_out
        high += turns.output[1] * r_out
    low, high = min(low, high), max(low, high)
    price = f"${_f(q.input):g}/M in" + (f", ${_f(q.output):g}/M out" if turns.output is not None else "")
    parts = [f"about {tokens_text(base)} tokens of context", price]
    if turns.count:
        calls = f"{c_lo}" if c_lo == c_hi else f"{c_lo} to {c_hi}"
        outs = turns.output
        parts.append(f"last {turns.count} turn{'s' if turns.count != 1 else ''} here: {calls} model call{'s' if c_hi != 1 else ''}"
                     + (f", {tokens_text(outs[0])} to {tokens_text(outs[1])} output tokens" if outs and outs[1] > 0 else ""))
    else:
        parts.append("no earlier turns in this chat, so the reply and any tool calls are not counted")
    return {"model": model, "low": low, "high": high, "context_tokens": base, "calls": [c_lo, c_hi],
            "output": list(turns.output) if turns.output else None, "threshold": threshold,
            "show": threshold > 0 and high >= threshold, "basis": " · ".join(parts),
            "text": f"Estimate {money(low)} to {money(high)}" if money(low) != money(high) else f"Estimate about {money(high)}"}


def agent_context_tokens(agent: Any) -> int:
    """Tokens the next request would carry (conversation, system prompt, tool schemas): the figure the budget guard uses."""
    from axon.commands.context_cmds import _system_blocks, _tool_schemas
    return int(agent.context.projected_tokens(agent.conversation, _system_blocks(agent), _tool_schemas(agent)))


def forecast_for_agent(agent: Any, draft_chars: int = 0, threshold: float | None = None) -> dict[str, Any] | None:
    """The forecast for the chat's next turn, or None (forecasts off, price unknown, or the context is unreadable)."""
    s = agent.settings
    thr = threshold_usd(s) if threshold is None else threshold
    if thr <= 0:
        return None
    try:
        ctx = agent_context_tokens(agent)
    except Exception:
        return None
    try:
        transcript = agent.session._file(agent.session.active_session_id)
    except Exception:
        transcript = None
    return estimate(str(s.model), ctx, int(max(0, draft_chars) / CHARS_PER_TOKEN) + 1, recent_turns(transcript), thr,
                    getattr(s, "provider", None))

