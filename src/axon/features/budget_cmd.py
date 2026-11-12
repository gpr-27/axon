"""The pause-and-resume words of ``/budget`` (the older words, a bare amount, ``session`` and ``day``/``month``, stay in
axon.features.analytics):

    /budget                      caps, spent and what is left (the older lines, plus turn cap and task budget)
    /budget turn <usd|off>       cap what one turn may spend
    /budget raise <usd>          add room above what is spent and continue the paused work
    /budget off                  remove the caps; paused work continues
    /budget task <tokens|off>    Messages API task budget (an advisory countdown the model paces itself against)
"""
from __future__ import annotations

from typing import Any

from axon.agent import budget_pause as bp
from axon.providers.task_budget import MIN_TOTAL, supports_task_budget

_OFF = ("off", "none", "unlimited", "disable", "clear", "0")


def _say(kind: str, text: str) -> None:
    from axon.commands import common
    getattr(common, kind)(text)


def _amount(text: str) -> float | None:
    from axon.features.analytics import parse_amount
    v = parse_amount(text)
    return float(v) if v is not None and v > 0 else None


def resume(agent: Any, prompt: str | None) -> None:
    """Re-run the pending step of paused work on this thread (like /retry does)."""
    if not prompt:
        return
    _say("info", "Continuing where it paused.")
    agent.run_turn(prompt)


def handle(agent: Any, arg: str) -> bool:
    """True when the words were handled here."""
    if getattr(agent, "settings", None) is None:
        return False
    words = arg.strip().lower().split()
    head, rest = (words[0] if words else ""), " ".join(words[1:])
    if head == "raise":
        usd = _amount(rest)
        if usd is None:
            _say("err", "Usage: /budget raise <usd>   (how many dollars more to allow, for example /budget raise 5)")
            return True
        if bp.pending(agent) is None and bp.session_limit(agent.settings) <= 0 and not float(getattr(agent.settings, "budget_turn_usd", 0) or 0):
            _say("info", "No spend cap is set. /budget session <usd> sets one.")
            return True
        was = bp.pending(agent)
        out = bp.raise_cap(agent, usd)
        limits = ", ".join(f"${v:.2f}" for v in out["limits"].values()) or "no cap"
        _say("ok", f"Budget raised by ${usd:.2f} (now {limits}).")
        if was is not None:
            resume(agent, out["prompt"])
        return True
    if head == "turn":
        if not rest:
            cur = float(getattr(agent.settings, "budget_turn_usd", 0) or 0)
            _say("info", f"Turn budget: {'$%.2f' % cur if cur else 'none'}. /budget turn <usd|off> changes it.")
            return True
        if rest in _OFF:
            bp.set_cap(agent, "turn", None)
            _say("ok", "Turn budget removed.")
            return True
        usd = _amount(rest)
        if usd is None:
            _say("err", "Usage: /budget turn <usd|off>")
            return True
        bp.set_cap(agent, "turn", usd)
        _say("ok", f"Turn budget: ${usd:.2f}")
        return True
    if head == "task":
        return _task(agent, rest)
    if head in _OFF and (bp.pending(agent) is not None or float(getattr(agent.settings, "budget_session_usd", 0) or 0) > 0):
        was = bp.pending(agent)
        from axon.commands.common import update_settings
        from decimal import Decimal
        changes: dict[str, Any] = {"budget_session_usd": None, "session_cost_ceiling": Decimal("0")}
        if was is not None and was["scope"] == "turn":
            changes["budget_turn_usd"] = None
        update_settings(agent, **changes)
        prompt = bp.resume_prompt(agent)
        bp.clear(agent)
        _say("ok", "Spend cap removed.")
        if was is not None:
            resume(agent, prompt)
        return True
    return False


def _task(agent: Any, rest: str) -> bool:
    from axon.commands.common import update_settings
    cur = getattr(agent.settings, "task_budget_tokens", None)
    if not rest:
        _say("info", f"Task budget: {f'{cur:,} tokens' if cur else 'off'}. /budget task <tokens|off> changes it "
                     f"(at least {MIN_TOTAL:,}); the model is told what is left and paces itself; Axon stops the task at the limit when the API has no budget of its own.")
        return True
    if rest in _OFF:
        update_settings(agent, task_budget_tokens=None)
        _say("ok", "Task budget off.")
        return True
    try:
        n = int(float(rest.replace(",", "").replace("_", "").rstrip("k")) * (1000 if rest.endswith("k") else 1))
    except ValueError:
        _say("err", "Usage: /budget task <tokens|off>   (for example /budget task 100000 or 100k)")
        return True
    if n < MIN_TOTAL:
        _say("err", f"A task budget is at least {MIN_TOTAL:,} tokens (the API refuses less).")
        return True
    update_settings(agent, task_budget_tokens=n)
    model = getattr(agent.settings, "model", "")
    note = "" if supports_task_budget(model) else f" (enforced by Axon for {model}: it is told what is left and stopped at the limit)"
    _say("ok", f"Task budget: {n:,} tokens per task{note}.")
    return True


def extra_lines(agent: Any) -> list[str]:
    """Lines added under the older ``/budget`` display: the guard caps and the task budget, only when they are set."""
    s = getattr(agent, "settings", None)
    out: list[str] = []
    try:
        guard = float(getattr(s, "budget_session_usd", 0) or 0)
        turn = float(getattr(s, "budget_turn_usd", 0) or 0)
        task = getattr(s, "task_budget_tokens", None)
        spent = bp.session_spent(agent)
        if guard > 0:
            out.append(f"  Session cap (guard): ${spent:.2f} of ${guard:.2f}, ${max(0.0, guard - spent):.2f} left")
        if turn > 0:
            out.append(f"  Turn cap: ${turn:.2f}")
        if task:
            used = bp.task_used(agent)
            out.append(f"  Task budget: {used:,} of {int(task):,} tokens (the model paces itself against it)")
        p = bp.pending(agent)
        if p:
            out.append("  Paused: " + p["message"])
    except Exception:
        return []
    return out
