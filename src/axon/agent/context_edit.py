"""Server-side context editing (Anthropic ``context_management``, beta ``context-management-2025-06-27``).

In a long session the API drops the OLDEST tool results from what the model sees (``clear_tool_uses_20250919``)
once the prompt passes a trigger, keeping the newest few and every result of a tool whose output is the record
of a decision (edits, writes, tasks, todos, ...). Axon's own transcript is never touched: the API edits a copy
before the model reads it, so history, /context, resume and the dashboard still show everything.

Defaults (a coding agent):
  * trigger 100k input tokens, the API default: below that nothing is cleared and the prompt cache is untouched;
  * keep the last 6 tool results (a read-edit-test loop stays whole);
  * clear_at_least 25k tokens, so a clear that would break the cache for a small gain is skipped by the API;
  * no thinking edit: the per-model default keeps thinking blocks valid (on Fable/Mythos/Opus 5.5/Sonnet 5.5/Haiku 5.5
    server-side clearing never invalidates them).

Only where it cannot be a 400: the official Anthropic host and a model the docs list as supported. Any other
provider, gateway or model is left exactly as before. Results the user pinned (/pin) must stay verbatim and the
API cannot exempt one result, so editing pauses while any pin exists.
"""
from __future__ import annotations

from typing import Any

EDIT_TYPE = "clear_tool_uses_20250919"

# Tool results that are never cleared (name is what the API matches on).
EXCLUDE_TOOLS = ("Edit", "Write", "MultiEdit", "Patch", "NotebookEdit", "Task", "TodoWrite", "SendMessage",
                 "AskUserQuestion", "Skill", "ToolSearch", "CheckpointUndo", "EnterPlanMode", "ExitPlanMode")

# Models the docs list as supporting context editing (a dated suffix such as -20251001 also matches).
SUPPORTED_MODELS = (
    "claude-fable-5", "claude-mythos-5", "claude-mythos-preview", "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5",
    "claude-haiku-4-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-4-6", "claude-sonnet-4-5",
)

DEFAULT_TRIGGER = 100_000
DEFAULT_KEEP = 6
DEFAULT_CLEAR_AT_LEAST = 25_000
MIN_TRIGGER = 10_000


def supports_context_editing(model: str) -> bool:
    m = str(model or "").lower().split("/")[-1]
    if m.endswith("[1m]"):
        m = m[:-4]
    return any(m == p or m.startswith(p + "-") for p in SUPPORTED_MODELS)


def build_edit(trigger: int = DEFAULT_TRIGGER, keep: int = DEFAULT_KEEP, clear_at_least: int = DEFAULT_CLEAR_AT_LEAST) -> dict[str, Any]:
    """The ``context_management`` request field."""
    trigger = max(MIN_TRIGGER, int(trigger))
    return {"edits": [{
        "type": EDIT_TYPE,
        "trigger": {"type": "input_tokens", "value": trigger},
        "keep": {"type": "tool_uses", "value": max(1, int(keep))},
        # clearing must free something worth the cache rewrite, and never ask for more than the trigger holds
        "clear_at_least": {"type": "input_tokens", "value": max(0, min(int(clear_at_least), trigger // 2))},
        "exclude_tools": list(EXCLUDE_TOOLS),
    }]}


def config_for(agent: Any) -> dict[str, Any] | None:
    """The ``context_management`` value for the next call (clearing edit and, when asked for, the server-side
    compaction edit after it), or None when nothing must be sent."""
    clear = _clear_config(agent)
    try:
        from axon.agent import compaction
        compact = compaction.server_edit(agent)
    except Exception:
        compact = None
    if compact is None:
        return clear
    return {"edits": [*((clear or {}).get("edits") or []), compact]}


def _clear_config(agent: Any) -> dict[str, Any] | None:
    """The tool-result clearing config, or None when editing must not be sent."""
    s = agent.settings
    if not bool(getattr(s, "context_editing", True)):
        return None
    provider = getattr(agent, "provider", None)
    if not getattr(provider, "_official", False):          # only the official host takes the beta (see module doc)
        return None
    if not supports_context_editing(getattr(provider, "_wire_model", lambda m: m)(s.model)):
        return None
    if getattr(getattr(agent, "context", None), "pins", None):
        return None
    return build_edit(getattr(s, "context_edit_trigger", DEFAULT_TRIGGER), getattr(s, "context_edit_keep", DEFAULT_KEEP),
                      getattr(s, "context_edit_clear_at_least", DEFAULT_CLEAR_AT_LEAST))


def arm(agent: Any) -> None:
    """Tell the provider what to send with the next call (called right before streaming)."""
    provider = getattr(agent, "provider", None)
    if provider is not None and hasattr(provider, "context_edit"):
        provider.context_edit = config_for(agent)


def summarize(applied: Any) -> tuple[int, int]:
    """(tool results cleared, input tokens saved) from a response's ``applied_edits`` list."""
    cleared = saved = 0
    for edit in applied or []:
        if isinstance(edit, dict) and edit.get("type") == EDIT_TYPE:
            cleared += int(edit.get("cleared_tool_uses") or 0)
            saved += int(edit.get("cleared_input_tokens") or 0)
    return cleared, saved


def _short(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else (f"{round(n / 1000)}k" if n >= 1000 else str(n))


def describe(cleared: int, saved: int) -> str:
    """The one line shown in the turn view and the dashboard."""
    noun = "tool result" if cleared == 1 else "tool results"
    return f"cleared {cleared} old {noun} · saved ~{_short(saved)} tokens"


def collect(agent: Any) -> Any:
    """After a call: a ContextEditEvent when the API cleared something (else None)."""
    provider = getattr(agent, "provider", None)
    info = getattr(provider, "last_context_edit", None)
    if provider is not None and hasattr(provider, "last_context_edit"):
        provider.last_context_edit = None
    if not info:
        return None
    cleared, saved = summarize(info.get("applied_edits"))
    if cleared <= 0 and saved <= 0:
        return None
    from axon.agent.trace import ContextEditEvent
    return ContextEditEvent(cleared_tool_uses=cleared, cleared_input_tokens=saved, message=describe(cleared, saved))
