"""The terminal 'GOAL' block: one compact block per grader check, per-criterion ok/fail with its one-line evidence or gap.

Shared by the live renderer and the history view. Colours are the neutral palette plus the teal accent; green/red
only mark met/unmet.
"""
from __future__ import annotations

from typing import Any

from axon.ui.theme import BOLD, ERROR, MUTED, PRIMARY, RST, SUBTLE, SUCCESS, WARNING, glyph, truncate, wrap_ansi

_STATUS = {
    "satisfied": ("goal met", SUCCESS),
    "needs_revision": ("not done yet", WARNING),
    "failed_unrecoverable": ("cannot be met as it stands", ERROR),
    "interrupted": ("check cancelled", MUTED),
}


def _g(d: Any, key: str, default: Any = "") -> Any:
    return d.get(key, default) if isinstance(d, dict) else getattr(d, key, default)


def one_line(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: max(1, limit - 1)] + "…"


def goal_block_rows(e: Any, width: int = 100) -> list[str]:
    """Rows for a verdict (or skipped) GoalCheckEvent / trace dict. Empty for the 'start' phase."""
    phase = str(_g(e, "phase", "verdict"))
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    if phase == "skipped":
        return [f"{MUTED}GOAL{RST}{dot}{MUTED}not checked: {_g(e, 'note')}{RST}"]
    if phase != "verdict":
        return []
    verdict = str(_g(e, "verdict"))
    label, color = _STATUS.get(verdict, (verdict.replace("_", " "), MUTED))
    rnd, mx = int(_g(e, "round", 0) or 0), int(_g(e, "max_rounds", 0) or 0)
    head = f"{PRIMARY}{BOLD}GOAL{RST}{dot}{color}{label}{RST}"
    if rnd and verdict != "interrupted":
        head += f"{dot}{MUTED}round {rnd}/{mx}{RST}"
    rows = [head]
    inner = max(30, width - 6)
    for c in _g(e, "criteria", ()) or ():
        met = bool(_g(c, "met", False))
        mark = f"{SUCCESS}{glyph('ok')}{RST}" if met else f"{ERROR}{glyph('fail')}{RST}"
        why = _g(c, "evidence") if met else (_g(c, "gap") or _g(c, "evidence"))
        line = f"{mark} {_g(c, 'criterion')}" + (f" {MUTED}— {why}{RST}" if why else "")
        wrapped = wrap_ansi(line, inner)
        rows.append("  " + wrapped[0])
        rows += ["    " + w for w in wrapped[1:2]]
    note = str(_g(e, "note") or "")
    summary = str(_g(e, "summary") or "")
    if not _g(e, "criteria", ()) and summary:
        rows.append(f"  {MUTED}{one_line(summary, inner)}{RST}")
    if note and note != "goal met" and verdict != "needs_revision":
        rows.append(f"  {MUTED}{one_line(note, inner)}{RST}")
    return [truncate(r, width) if "\x1b" not in r else r for r in rows]


def goal_usage_row(e: Any) -> dict[str, Any] | None:
    """The 'goal check' row of the end-of-turn usage table, or None when the check reported no usage."""
    if str(_g(e, "phase", "")) != "verdict" or _g(e, "usage", None) is None:
        return None
    from axon.ui.render import step_usage
    row = step_usage(0, str(_g(e, "model") or ""), _g(e, "duration_s", None), _g(e, "usage"), _g(e, "cost", None))
    row["step"] = "goal check"
    return row
