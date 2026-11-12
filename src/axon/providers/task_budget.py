"""Messages API task budgets: an advisory token countdown for a whole agentic loop (beta).

Docs ("Task budgets"): ``output_config.task_budget = {"type": "tokens", "total": N}`` plus the beta header
``task-budgets-2026-03-13``. The model sees a running countdown and paces itself; the API never stops it at the
budget, so ``max_tokens`` still caps every single request. The minimum total is 20,000 tokens (smaller returns a 400).
Only some Claude models take it, and only on the Claude API (gateways and other providers would reject it), so the
field is added only when both hold. Every other provider gets the same budget client-side (``agent/client_budget``):
a short note of what is left in each request and a graceful stop at the limit.
"""
from __future__ import annotations

import re
from typing import Any

BETA_HEADER = "task-budgets-2026-03-13"
MIN_TOTAL = 20_000

# Models the docs list as supported (everything else, such as Sonnet 5, Opus 4.6, Sonnet 4.6, Haiku 4.5, is not).
_SUPPORTED = (
    "claude-fable-5-2", "claude-mythos-5-2", "claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5",
    "claude-mythos-preview", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-opus-4-8", "claude-opus-4-7",
)
_PATTERN = re.compile(r"^(?:" + "|".join(re.escape(m) for m in _SUPPORTED) + r")(?:-\d{8})?$")


def supports_task_budget(model: str) -> bool:
    """True for a model id (aliases and a ``[1m]`` suffix tolerated) the docs list as supported."""
    m = re.sub(r"\[.*?\]$", "", str(model or "").strip().lower())
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return bool(_PATTERN.match(m))


def clamp_total(total: Any) -> int | None:
    """A usable total: None when off or not a number, else at least the 20,000-token minimum."""
    try:
        n = int(total)
    except (TypeError, ValueError):
        return None
    return max(MIN_TOTAL, n) if n > 0 else None


def apply(kwargs: dict[str, Any], model: str, total: Any, *, official: bool) -> bool:
    """Add the task budget to *kwargs* (the Messages request: ``extra_body`` / ``extra_headers``) when it is wanted
    and the endpoint and model take it. Returns True when it was added."""
    n = clamp_total(total)
    if n is None or not official or not supports_task_budget(model):
        return False
    body = dict(kwargs.get("extra_body") or {})
    cfg = dict(body.get("output_config") or {})
    cfg["task_budget"] = {"type": "tokens", "total": n}
    body["output_config"] = cfg
    kwargs["extra_body"] = body
    headers = dict(kwargs.get("extra_headers") or {})
    have = [b.strip() for b in str(headers.get("anthropic-beta", "")).split(",") if b.strip()]
    if BETA_HEADER not in have:
        have.append(BETA_HEADER)
    headers["anthropic-beta"] = ",".join(have)
    kwargs["extra_headers"] = headers
    return True


def strip(kwargs: dict[str, Any]) -> bool:
    """Remove the task budget again (the endpoint refused it). Returns True when something was removed."""
    body = dict(kwargs.get("extra_body") or {})
    cfg = dict(body.get("output_config") or {})
    if "task_budget" not in cfg:
        return False
    cfg.pop("task_budget")
    if cfg:
        body["output_config"] = cfg
    else:
        body.pop("output_config", None)
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    headers = dict(kwargs.get("extra_headers") or {})
    have = [b.strip() for b in str(headers.get("anthropic-beta", "")).split(",") if b.strip() and b.strip() != BETA_HEADER]
    if have:
        headers["anthropic-beta"] = ",".join(have)
    else:
        headers.pop("anthropic-beta", None)
    if headers:
        kwargs["extra_headers"] = headers
    else:
        kwargs.pop("extra_headers", None)
    return True


def rejected(status: Any, text: str) -> bool:
    """True when a 400 names the task budget (so only that field is dropped, not effort or thinking)."""
    return status == 400 and ("task_budget" in text or "task-budgets" in text or "task budget" in text)


class Tracker:
    """Client-side estimate of what the model's countdown has used in one turn: the docs say the countdown is only
    visible to the model and counts what Claude sees newly (output plus new tool results), not the resent history.
    used = (input of the last request - input of the first) + output of the last request."""

    def __init__(self) -> None:
        self.first_in: int | None = None
        self.last_in = 0
        self.last_out = 0

    def add(self, usage: Any, inclusive: bool = False) -> None:
        """*inclusive*: ``input`` already contains the cached tokens (OpenAI-style usage), so they are not added again."""
        keys = ("input",) if inclusive else ("input", "cache_read", "cache_write")
        total_in = sum(int(getattr(usage, k, 0) or 0) for k in keys)
        if inclusive:
            total_in = max(total_in, int(getattr(usage, "cache_read", 0) or 0))
        if self.first_in is None:
            self.first_in = total_in
        self.last_in = total_in
        self.last_out = int(getattr(usage, "output", 0) or 0)

    @property
    def used(self) -> int:
        if self.first_in is None:
            return 0
        return max(0, self.last_in - self.first_in) + self.last_out
