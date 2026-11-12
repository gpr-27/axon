"""Scheduled-run upgrades kept apart from scheduler.py: cron + timezone, the spend-cap gate, the run log vocabulary.

* ``parse_cron`` / ``cron_next``: standard 5-field cron (minute hour day-of-month month day-of-week; ``*``, lists,
  ranges, ``/step``, month and weekday names). Like cron, when both day fields are restricted a day matches if either does.
* ``check_tz`` / ``zone``: IANA timezone names (``Asia/Kolkata``); empty means the computer's local time.
* ``cap_problem``: the fail-closed rule. A run needs a cap that can be enforced: a dollar cap needs a known price for
  the model (any provider); with no known price a token cap (``max_tokens``) is the alternative; with neither, or with
  the cap not explicitly switched off, the run is skipped and the reason logged. Local models cost nothing and are fine.
* ``outcome`` / ``failure_of``: the run log's status words (succeeded, failed, budget_exceeded, skipped, cancelled,
  missed) and typed failure reasons, derived from the scheduler's internal statuses.
* ``retain``: log retention by age and count.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

RETAIN_DAYS = 90
RETAIN_COUNT = 1000
SKIP_OVERLAP = "skipped: previous run still active"
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}   # cron: 0 = Sunday


class CronError(ValueError):
    """The expression or timezone is not valid; the message is plain words."""


# ─── timezone ────────────────────────────────────────────────────────────────

def zone(name: str | None) -> tzinfo | None:
    """The tzinfo for an IANA name, or None for local time."""
    name = (name or "").strip()
    if not name:
        return None
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        raise CronError(f"'{name}' is not a timezone name. Use an IANA name such as Europe/London or Asia/Kolkata.") from None


def check_tz(name: Any) -> str:
    name = str(name or "").strip()
    zone(name)
    return name


# ─── cron ────────────────────────────────────────────────────────────────────

def _field(text: str, lo: int, hi: int, names: dict[str, int], what: str) -> tuple[set[int], bool]:
    """(allowed values, whether the field was a bare ``*``)."""
    out: set[int] = set()
    star = text == "*"

    def num(tok: str) -> int:
        t = tok.lower()
        if t in names:
            return names[t]
        if not re.fullmatch(r"\d+", t):
            raise CronError(f"Cron {what} '{tok}' is not valid.")
        return int(t)

    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not re.fullmatch(r"\d+", s) or int(s) < 1:
                raise CronError(f"Cron {what} step '{s}' is not valid.")
            step = int(s)
        if part in ("*", ""):
            if part == "" :
                raise CronError(f"Cron {what} has an empty part.")
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = num(x), num(y)
        else:
            a = num(part)
            b = hi if step > 1 else a
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise CronError(f"Cron {what} '{text}' is out of range ({lo}-{hi}).")
        out.update(range(a, b + 1, step))
    return out, star


def parse_cron(expr: str) -> dict[str, Any]:
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise CronError("A cron schedule has 5 fields: minute hour day-of-month month day-of-week (for example 30 9 * * 1-5).")
    mi, _ = _field(parts[0], 0, 59, {}, "minute")
    ho, _ = _field(parts[1], 0, 23, {}, "hour")
    dom, dstar = _field(parts[2], 1, 31, {}, "day of month")
    mo, _ = _field(parts[3], 1, 12, _MONTHS, "month")
    dow, wstar = _field(parts[4], 0, 7, _DAYS, "day of week")
    dow = {0 if d == 7 else d for d in dow}
    return {"minute": mi, "hour": ho, "dom": dom, "month": mo, "dow": dow, "dom_star": dstar, "dow_star": wstar}


def _day_ok(c: dict[str, Any], d: datetime) -> bool:
    if d.month not in c["month"]:
        return False
    in_dom, in_dow = d.day in c["dom"], ((d.weekday() + 1) % 7) in c["dow"]
    if c["dom_star"] and c["dow_star"]:
        return True
    if c["dom_star"]:
        return in_dow
    if c["dow_star"]:
        return in_dom
    return in_dom or in_dow


def cron_next(expr: str, after: float, tz_name: str | None = None) -> float | None:
    """The first matching minute strictly after *after* (epoch seconds), in the timezone's wall clock."""
    c = parse_cron(expr)
    tz = zone(tz_name)
    start = (datetime.fromtimestamp(after, tz) if tz else datetime.fromtimestamp(after)).replace(second=0, microsecond=0)
    day = start.replace(hour=0, minute=0)
    for i in range(0, 366 * 5):
        d = day + timedelta(days=i)
        if not _day_ok(c, d):
            continue
        for h in sorted(c["hour"]):
            for m in sorted(c["minute"]):
                cand = d.replace(hour=h, minute=m)
                if tz is not None:
                    cand = cand.replace(tzinfo=tz)
                ts = cand.timestamp()
                if ts > after:
                    return ts
    return None


def next_fires(task: dict[str, Any], after: float, n: int = 3) -> list[float]:
    """A preview of the next *n* fire times (uses the scheduler's own rule)."""
    from axon.features import scheduler as sch
    out: list[float] = []
    cur = after
    for _ in range(n):
        nxt = sch.next_after(task, cur)
        if nxt is None:
            break
        out.append(nxt)
        cur = nxt
    return out


def local_wall(ts: float, tz_name: str | None = None) -> str:
    tz = zone(tz_name)
    d = datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)
    return d.strftime("%a %d %b %H:%M") + (f" {tz_name}" if tz_name else "")


# ─── the spend cap, fail closed ──────────────────────────────────────────────

def cap_problem(task: dict[str, Any], model: str, provider: str | None = None) -> str | None:
    """None when the run may start; otherwise the plain reason it is skipped.

    Works for any provider and model: the dollar cap uses providers/pricing.py; a model without a known price can be
    capped by tokens (``max_tokens``); only when neither exists is the run refused, unless the cap is switched off."""
    if task.get("cap_off"):
        return None
    try:
        from axon.providers import pricing
        priced = pricing.is_priced(model, provider)
    except Exception:
        priced = False
    if priced:
        return None
    if int(task.get("max_tokens") or 0) > 0:
        return None
    return (f"No price is known for {model or 'this model'}, so its spending cannot be capped in dollars. Set a token cap "
            f"(--max-tokens N) or switch the cap off (--max-cost off) to let it run.")


# ─── run log vocabulary ──────────────────────────────────────────────────────

_OUTCOME = {"ok": "succeeded", "failed": "failed", "interrupted": "failed", "budget": "budget_exceeded", "stopped": "cancelled",
            "timeout": "cancelled", "skipped": "skipped", "missed": "missed", "running": "running"}


def outcome(status: Any) -> str:
    return _OUTCOME.get(str(status), str(status or ""))


def failure_of(rec: dict[str, Any]) -> str:
    """A typed reason for anything that did not succeed (``""`` for a success)."""
    if rec.get("failure"):
        return str(rec["failure"])
    return {"failed": "error", "interrupted": "interrupted", "budget": "cap_reached", "stopped": "cancelled", "timeout": "timeout",
            "skipped": "skipped", "missed": "app_closed"}.get(str(rec.get("status")), "")


def view(rec: dict[str, Any]) -> dict[str, Any]:
    """A record as the log shows it: canonical status, trigger, failure, duration."""
    out = dict(rec)
    out["outcome"] = outcome(rec.get("status"))
    out["trigger"] = "manual" if rec.get("reason") == "manual" else "scheduled"
    out["failure"] = failure_of(rec)
    s, e = rec.get("started"), rec.get("ended")
    out["duration_s"] = round(float(e) - float(s), 1) if isinstance(s, (int, float)) and isinstance(e, (int, float)) else None
    return out


def retain(records: list[dict[str, Any]], now: float, days: int = RETAIN_DAYS, keep: int = RETAIN_COUNT) -> list[dict[str, Any]]:
    """Drop runs older than *days* (a run still going is kept), then keep the newest *keep*."""
    cut = now - days * 86400
    fresh = [r for r in records if r.get("status") == "running" or float(r.get("ended") or r.get("started") or now) >= cut]
    return fresh[-keep:]
