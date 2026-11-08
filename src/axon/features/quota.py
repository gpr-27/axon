"""
Rate-limit data the provider actually reported, and nothing else.

The provider layer feeds ``record_rate_limits(headers)`` with the response headers of every call;
``/usage`` reads ``snapshot()``. Axon never invents a plan quota: with no headers seen there are no
limits to show and ``snapshot()`` is empty.

Parsed (Anthropic names)::

    anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}

``reset`` is an RFC 3339 timestamp. The OpenAI style (``x-ratelimit-{limit,remaining,reset}-
{requests,tokens}``, where reset is a duration such as ``6m0s``) is understood as well.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

FAMILIES = ("requests", "tokens", "input-tokens", "output-tokens")
LABELS = {"requests": "Requests", "tokens": "Tokens", "input-tokens": "Input tokens", "output-tokens": "Output tokens"}


@dataclass(frozen=True)
class RateLimit:
    name: str                  # one of FAMILIES
    limit: int | None
    remaining: int | None
    reset_at: float | None     # epoch seconds
    observed_at: float         # when the headers arrived
    provider: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)

    @property
    def used_fraction(self) -> float | None:
        if not self.limit or self.remaining is None:
            return None
        return min(1.0, max(0.0, (self.limit - self.remaining) / self.limit))

    @property
    def expired(self) -> bool:
        """The window has reset since these headers were read, so the numbers no longer describe it."""
        return self.reset_at is not None and self.reset_at < time.time()


_lock = threading.Lock()
_state: dict[str, RateLimit] = {}


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_reset(value: Any, now: float | None = None) -> float | None:
    """RFC 3339 timestamp, a duration like ``1m30s``, or plain seconds -> epoch seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    now = time.time() if now is None else now
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return now + float(text)
    parts = _DURATION.findall(text)
    if parts and "".join(n + u for n, u in parts) == text:
        unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
        return now + sum(float(n) * unit[u] for n, u in parts)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _lower(headers: Any) -> dict[str, str]:
    try:
        items = headers.items()
    except AttributeError:
        return {}
    return {str(k).lower(): str(v) for k, v in items}


def record_rate_limits(headers: Mapping[str, Any] | None, provider: str = "", now: float | None = None) -> int:
    """Store the rate-limit headers of one response. Returns how many limits were recognised."""
    if not headers:
        return 0
    h = _lower(headers)
    now = time.time() if now is None else now
    found: dict[str, RateLimit] = {}
    for fam in FAMILIES:
        limit = _int(h.get(f"anthropic-ratelimit-{fam}-limit"))
        remaining = _int(h.get(f"anthropic-ratelimit-{fam}-remaining"))
        reset = parse_reset(h.get(f"anthropic-ratelimit-{fam}-reset"), now)
        if fam in ("requests", "tokens") and limit is None and remaining is None:   # OpenAI-style names
            limit = _int(h.get(f"x-ratelimit-limit-{fam}"))
            remaining = _int(h.get(f"x-ratelimit-remaining-{fam}"))
            reset = parse_reset(h.get(f"x-ratelimit-reset-{fam}"), now)
        if limit is None and remaining is None:
            continue
        found[fam] = RateLimit(fam, limit, remaining, reset, now, provider)
    if found:
        with _lock:
            _state.update(found)
    return len(found)


def snapshot(include_expired: bool = False) -> list[RateLimit]:
    """The limits last reported, in the order of ``FAMILIES``."""
    with _lock:
        rows = [_state[f] for f in FAMILIES if f in _state]
    return rows if include_expired else [r for r in rows if not r.expired]


def clear() -> None:
    with _lock:
        _state.clear()


def local_reset_text(reset_at: float | None, now: float | None = None) -> str:
    """'resets 3:45pm' today, 'resets Tue 3:45pm' later, in the local time zone."""
    if reset_at is None:
        return ""
    now = time.time() if now is None else now
    dt, ref = datetime.fromtimestamp(reset_at).astimezone(), datetime.fromtimestamp(now).astimezone()
    clock = dt.strftime("%I:%M%p").lstrip("0").lower()
    zone = dt.tzname() or ""
    if dt.date() == ref.date():
        day = ""
    elif (dt.date() - ref.date()).days == 1:
        day = "tomorrow "
    else:
        day = dt.strftime("%a %b %d ").replace(" 0", " ")
    return f"resets {day}{clock} {zone}".strip()
