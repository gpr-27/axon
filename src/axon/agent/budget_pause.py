"""A spend cap that pauses the work and resumes it, instead of ending it.

When the session (or turn) cap is reached, the loop stops at a safe boundary (before the next model request, so a
tool call and its result are never split), keeps the whole conversation, and records *why* in ``agent.budget_pause``.
``/budget raise <usd>`` (or the web page's button) lifts the cap and re-runs the pending step: the prompt itself when
nothing had been answered yet, otherwise a short "continue" for the history that is already there. Tools that already
ran are in the history as results, so nothing runs twice.
"""
from __future__ import annotations

import math
import time
from decimal import Decimal
from typing import Any

RESUME_PROMPT = "continue (the spend limit was raised; pick up where you left off using the tool results above)"
SUGGESTED_RAISE = 5

# the settings that cap a scope
SESSION_SETTINGS = ("session_cost_ceiling", "budget_session_usd")
TURN_SETTINGS = ("budget_turn_usd",)


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def session_spent(agent: Any) -> float:
    """Spend of the whole session: this agent's ledger plus its parents' (a subagent has its own ledger that is
    folded into the parent's only when the run ends, so it adds its running cost to what the parent has spent)."""
    total = 0.0
    seen: set[int] = set()
    cur = agent
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        ledger = getattr(cur, "ledger", None)
        try:
            total += float(ledger.total()) if ledger is not None else 0.0
        except Exception:
            pass
        cur = getattr(cur, "parent_agent", None)
    return total


def session_limit(settings: Any) -> float:
    """The smallest session cap that is set (0 when there is none)."""
    caps = [_num(getattr(settings, k, 0)) for k in SESSION_SETTINGS]
    caps = [c for c in caps if c > 0]
    return min(caps) if caps else 0.0


def reached_message(scope: str, spent: float, limit: float) -> str:
    return (f"{scope} budget reached (${spent:.2f} of ${limit:.2f}) — "
            f"/budget raise {SUGGESTED_RAISE} to continue, or /budget off")


def pause(agent: Any, *, setting: str, scope: str, spent: float, limit: float, prompt: str, resume: bool) -> str:
    """Record the pause. *resume* is True when the history already holds work to continue from."""
    msg = reached_message(scope, spent, limit)
    agent.budget_pause = {"setting": setting, "scope": scope, "spent": round(spent, 6), "limit": round(limit, 6),
                          "prompt": prompt, "resume": bool(resume), "at": time.time(), "message": msg}
    try:
        from axon.features import notify
        notify.emit_for_agent(agent, "budget_reached", msg)
    except Exception:
        pass
    return msg


def finalize(agent: Any, prompt_message: Any, raw_prompt: str) -> None:
    """Complete a pause recorded inside the loop. When no model reply followed the prompt yet, the prompt is taken
    back out of the history (like a failed first request) and resumed by sending it again; otherwise the history
    stays exactly as it is and a short "continue" resumes it."""
    p = pending(agent)
    if p is None:
        return
    msgs = agent.conversation.messages
    resume = bool(msgs) and msgs[-1] is not prompt_message
    p["prompt"], p["resume"] = raw_prompt, resume
    if not resume:
        try:
            agent._drop_from(prompt_message)
            agent.session.append("turn_failed", {"prompt": raw_prompt, "resume": False, "rolled_back": True,
                                                 "error": p.get("message", "budget reached"), "tool_calls": 0})
        except Exception:
            pass


def clear(agent: Any) -> None:
    agent.budget_pause = None


def pending(agent: Any) -> dict[str, Any] | None:
    p = getattr(agent, "budget_pause", None)
    return p if isinstance(p, dict) else None


def resume_prompt(agent: Any) -> str | None:
    """What to run to continue the paused work (None when nothing is paused)."""
    p = pending(agent)
    if p is None:
        return None
    return RESUME_PROMPT if p.get("resume") else str(p.get("prompt") or "")


def _apply(agent: Any, **changes: Any) -> None:
    from axon.commands.common import update_settings
    update_settings(agent, **changes)


def set_cap(agent: Any, which: str, value: float | None) -> str:
    """``/budget session|turn``: set (or, with None, remove) a cap for this session. Returns the field changed."""
    if which == "turn":
        _apply(agent, budget_turn_usd=value)
        return "budget_turn_usd"
    changes: dict[str, Any] = {"budget_session_usd": value}
    if _num(getattr(agent.settings, "session_cost_ceiling", 0)) > 0 or value is None:
        changes["session_cost_ceiling"] = Decimal(str(value)) if value is not None else Decimal("0")
    _apply(agent, **changes)
    return "budget_session_usd"


def raise_cap(agent: Any, usd: float) -> dict[str, Any]:
    """Lift the caps that stopped the work by *usd* above what is spent. Returns {"raised": [...], "prompt": str|None}.
    The pending prompt is returned (not run) so the terminal and the page can run it their own way."""
    if not (isinstance(usd, (int, float)) and math.isfinite(usd) and usd > 0):
        raise ValueError("the amount to add must be more than zero")
    p = pending(agent)
    spent = session_spent(agent)
    s = agent.settings
    changes: dict[str, Any] = {}
    raised: list[str] = []
    scope = p["scope"] if p else "session"
    if scope == "turn":
        cur = _num(getattr(s, "budget_turn_usd", 0))
        spent_turn = float(getattr(getattr(agent, "_trace", None), "cost", 0.0) or 0.0)
        if p:
            spent_turn = max(spent_turn, float(p.get("spent") or 0))
        if cur > 0:
            changes["budget_turn_usd"] = round(max(cur, spent_turn) + usd, 6)
            raised.append("turn")
    else:
        for key in SESSION_SETTINGS:
            cur = _num(getattr(s, key, 0))
            if cur > 0:
                new = round(max(cur, spent) + usd, 6)
                changes[key] = Decimal(str(new)) if key == "session_cost_ceiling" else new
        if changes:
            raised.append("session")
    if changes:
        _apply(agent, **changes)
    prompt = resume_prompt(agent)
    clear(agent)
    return {"raised": raised, "prompt": prompt, "limits": {k: float(v) for k, v in changes.items()}}


def describe(agent: Any) -> dict[str, Any]:
    """Caps, spend and what is left, for ``/budget`` and the web page."""
    s = agent.settings
    spent = session_spent(agent)
    sess = session_limit(s)
    turn = _num(getattr(s, "budget_turn_usd", 0))
    turn_spent = float(getattr(getattr(agent, "_trace", None), "cost", 0.0) or 0.0)
    p = pending(agent)
    return {"spent": spent, "session_limit": sess or None, "session_remaining": max(0.0, sess - spent) if sess else None,
            "turn_limit": turn or None, "turn_spent": turn_spent,
            "paused": bool(p), "scope": p["scope"] if p else None, "message": p["message"] if p else "",
            "task_budget": getattr(s, "task_budget_tokens", None),
            "task_used": task_used(agent)}


def task_used(agent: Any) -> int:
    t = getattr(agent, "_task_tracker", None)
    return int(getattr(t, "used", 0) or 0) if t is not None else 0


def chip(agent: Any) -> str:
    """One short chip for the status area: ``$2.10 left`` / ``paused`` (empty when no cap is set)."""
    d = describe(agent)
    if d["paused"]:
        return "budget paused"
    if d["session_remaining"] is not None:
        return f"${d['session_remaining']:.2f} left"
    return ""
