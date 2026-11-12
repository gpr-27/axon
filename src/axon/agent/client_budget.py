"""Client-side task budget for providers without the Anthropic ``task_budget`` field.

``/budget task N`` (and a subagent's ``taskbudget``) sets a token budget for one task. On the Claude API with a
supported model the API shows the model a countdown. Everywhere else Axon does both halves itself:

  * every request carries one short, stable note in the system prompt: what share of the budget is left. The share
    is rounded to 10% steps, so the wording changes at most ten times per task and a provider that caches the
    prompt prefix keeps its cache in between;
  * at the budget the loop stops gracefully with one clear line (the work so far stays in the conversation).

What counts is what the model newly consumed in this task: output plus the input that grew since the first request
(the resent history is not billed against the budget twice), the same measure the native countdown uses.
"""
from __future__ import annotations

from typing import Any


def total(agent: Any) -> int:
    from axon.providers.task_budget import clamp_total
    return clamp_total(getattr(agent.settings, "task_budget_tokens", None)) or 0


def native(agent: Any) -> bool:
    """True when the API itself enforces the countdown (official host, supported model, not refused)."""
    from axon.providers.task_budget import supports_task_budget
    p = getattr(agent, "provider", None)
    if not getattr(p, "_official", False) or getattr(p, "_task_budget_off", False):
        return False
    try:
        wire = getattr(p, "_wire_model", lambda m: m)(agent.settings.model)
    except Exception:
        wire = agent.settings.model
    return supports_task_budget(wire)


def active(agent: Any) -> bool:
    return total(agent) > 0 and not native(agent)


def used(agent: Any) -> int:
    t = getattr(agent, "_task_tracker", None)
    return int(getattr(t, "used", 0) or 0) if t is not None else 0


def note(agent: Any) -> str:
    """The system note for the next request ('' when the budget is off or the API shows its own countdown)."""
    if not active(agent):
        return ""
    n = total(agent)
    left = max(0, n - used(agent))
    pct = min(100, (left * 100 // n) // 10 * 10)
    line = f"Task budget: about {pct}% of {n:,} tokens left for this task. Plan to finish within it"
    if pct <= 10:
        line += "; wrap up now and give your final answer."
    else:
        line += "; prefer fewer, targeted tool calls as it shrinks."
    return line


def with_note(agent: Any, system: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*system* plus the note as a last block (a copy; the agent's own blocks are untouched)."""
    text = note(agent)
    if not text:
        return system
    return list(system) + [{"type": "text", "text": text}]


def exhausted(agent: Any) -> str:
    """The stop line when the budget is used up (before the next request), else ''."""
    if not active(agent):
        return ""
    n = total(agent)
    if used(agent) >= n:
        return f"[Stopped: task budget of {n:,} tokens reached.]"
    return ""
