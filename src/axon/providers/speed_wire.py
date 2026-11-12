"""Anthropic fast mode (``speed: "fast"``) and per-message effort (a ``system`` message carrying ``output_config``).

Fast mode (docs: build-with-claude/fast-mode): set ``speed: "fast"`` on a Messages request, send the
``fast-mode-2026-02-01`` beta header on Opus 5 and Opus 4.8; priced at a premium over standard rates (caching
multipliers stack on top); a 429 / 529 is returned instead of queueing, and the fallback is to resend without
``speed`` (a prompt-cache miss: speeds do not share cached prefixes). ``usage.speed`` says which speed ran.

Per-message effort (docs: build-with-claude/effort-per-message): beta header ``mid-conversation-output-config-2026-07-01``;
a ``{"role": "system", "content": [], "output_config": {"effort": X}}`` message right after a user turn with new
input (a user turn with only tool_result blocks is not new input) sets the level from that turn on, the top-level
value stays and the cached prefix still matches. Keep the message in place on every later request. Sonnet 5.5 with
``between_tools`` and Haiku 5.5 with ``disabled`` thinking reject a changed level (400), so they are skipped.
"""
from __future__ import annotations

import dataclasses
import re
from decimal import Decimal
from typing import Any

BETA_FAST = "fast-mode-2026-02-01"
BETA_MID_EFFORT = "mid-conversation-output-config-2026-07-01"

# model prefix -> (fast input $/MTok, fast output $/MTok, needs the beta header). From the fast mode pricing table.
FAST_MODELS: tuple[tuple[str, float, float, bool], ...] = (
    ("claude-fable-5-2", 16.0, 80.0, False),
    ("claude-mythos-5-2", 16.0, 80.0, False),
    ("claude-opus-5-5", 8.0, 40.0, False),
    ("claude-opus-5", 10.0, 50.0, True),
    ("claude-opus-4-8", 10.0, 50.0, True),
)
# Models taking per-message effort (docs availability list).
MID_EFFORT_MODELS = ("claude-fable-5-2", "claude-mythos-5-2", "claude-fable-5-1", "claude-mythos-5-1",
                     "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5")


def _bare(model: str) -> str:
    return re.sub(r"(?<=\d)\.(?=\d)", "-", (model or "").lower().rsplit("/", 1)[-1])


def _entry(model: str) -> tuple[str, float, float, bool] | None:
    m = _bare(model)
    for row in FAST_MODELS:     # longest prefix wins: opus-5-5 before opus-5
        if m == row[0] or m.startswith(row[0] + "-") or m.startswith(row[0] + "@") or m.startswith(row[0] + ":"):
            if row[0] == "claude-opus-5" and m.startswith("claude-opus-5-5"):
                continue
            return row
    return None


def supports_fast(model: str, *, official: bool = True) -> bool:
    """API fast mode exists on these models of the Claude API (not on gateways or other providers)."""
    return bool(official) and _entry(model) is not None


def apply_fast(kwargs: dict[str, Any], model: str) -> bool:
    from axon.providers.anthropic_wire import add_beta
    row = _entry(model)
    if row is None:
        return False
    body = dict(kwargs.get("extra_body") or {})
    body["speed"] = "fast"
    kwargs["extra_body"] = body
    if row[3]:
        add_beta(kwargs, BETA_FAST)
    return True


def strip_fast(kwargs: dict[str, Any]) -> bool:
    """Resend at standard speed. True when something was removed."""
    from axon.providers.anthropic_wire import drop_beta
    body = dict(kwargs.get("extra_body") or {})
    had = body.pop("speed", None) is not None
    if body:
        kwargs["extra_body"] = body
    else:
        kwargs.pop("extra_body", None)
    drop_beta(kwargs, BETA_FAST)
    return had


def is_fast_request(kwargs: dict[str, Any]) -> bool:
    return (kwargs.get("extra_body") or {}).get("speed") == "fast"


def fast_quote(quote: Any) -> Any:
    """The PriceQuote at fast-mode rates: every rate scaled by fast input / standard input (the caching multipliers
    stack on top of the fast price, which this keeps). An unpriced or unknown model is returned unchanged."""
    row = _entry(getattr(quote, "model", ""))
    if row is None or not getattr(quote, "priced", False) or not quote.input or not quote.output:
        return quote
    ratio_in = Decimal(str(row[1])) / quote.input
    ratio_out = Decimal(str(row[2])) / quote.output
    return dataclasses.replace(quote, input=quote.input * ratio_in, output=quote.output * ratio_out,
                               cache_read=quote.cache_read * ratio_in, cache_write=quote.cache_write * ratio_in,
                               cache_write_1h=quote.cache_write_1h * ratio_in)


def premium(model: str, quote: Any) -> Decimal | None:
    """Fast-mode output price over standard (e.g. 1.6 for Opus 5.5), or None when unknown."""
    q = fast_quote(quote)
    if q is quote or not quote.output:
        return None
    return (q.output / quote.output).quantize(Decimal("0.01"))


# ─── per-message effort ───────────────────────────────────────────────────
def supports_mid_effort(model: str, *, official: bool = True) -> bool:
    m = _bare(model)
    return bool(official) and any(m == p or m.startswith(p + "-") or m.startswith(p + "@") for p in MID_EFFORT_MODELS)


def _only_tool_results(msg: dict[str, Any]) -> bool:
    c = msg.get("content")
    return isinstance(c, list) and bool(c) and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def new_input_turns(msgs: list[dict[str, Any]]) -> list[int]:
    """Indexes of the user turns that carry new input (not just tool results)."""
    return [i for i, m in enumerate(msgs) if m.get("role") == "user" and not _only_tool_results(m)]


def inject_effort_marks(msgs: list[dict[str, Any]], marks: list[tuple[int, str]]) -> tuple[list[dict[str, Any]], int]:
    """Insert an effort-only system message after the n-th new-input user turn for every (n, level) mark.
    Returns (messages, marks applied); a mark pointing past the history is ignored."""
    turns = new_input_turns(msgs)
    after: dict[int, str] = {}
    for n, level in marks:
        if 0 <= n < len(turns):
            after[turns[n]] = level
    if not after:
        return msgs, 0
    out: list[dict[str, Any]] = []
    for i, m in enumerate(msgs):
        out.append(m)
        if i in after:
            out.append({"role": "system", "content": [], "output_config": {"effort": after[i]}})
    return out, len(after)


def strip_effort_marks(kwargs: dict[str, Any]) -> bool:
    from axon.providers.anthropic_wire import drop_beta
    msgs = kwargs.get("messages") or []
    kept = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
    drop_beta(kwargs, BETA_MID_EFFORT)
    changed = len(kept) != len(msgs)
    kwargs["messages"] = kept
    return changed


def mark_rejected(status: Any, text: str) -> bool:
    return status == 400 and any(k in text for k in ("output_config", "per-turn effort", "mid-conversation", "extra inputs"))


def update_marks(state: dict[str, Any], turn_effort: tuple[int, str] | None, msgs: list[dict[str, Any]],
                 base_api: str | None, model: str) -> list[tuple[int, str]]:
    """Record the effort for a new user turn once and return every mark to replay on this request.

    *turn_effort* is (turn id, canonical level) from the agent; a level equal to the one in effect adds no mark.
    Marks pointing past a history that shrank (compaction) are dropped."""
    from axon.providers import model_caps
    marks: list[tuple[int, str]] = state.setdefault("marks", [])
    seen: set[int] = state.setdefault("seen", set())
    turns = new_input_turns(msgs)
    marks[:] = [m for m in marks if m[0] < len(turns)]
    if turn_effort and turn_effort[0] not in seen and turns:
        seen.add(turn_effort[0])
        level = model_caps.pick_level(model, turn_effort[1])
        cur = marks[-1][1] if marks else base_api
        if level and level != cur:
            marks.append((len(turns) - 1, level))
    return list(marks)
