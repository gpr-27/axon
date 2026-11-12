"""Client-side context editing for every provider without the Anthropic ``context_management`` beta.

Where the API cannot clear old tool results itself (OpenAI-compatible hosts, gateways, local servers, Claude models
on a proxy), Axon does the same to the *request copy*: the bodies of OLD tool results (older than the last N tool
uses) become one short stub, and the model reads that instead. The transcript on disk, /context, resume and the
dashboard still hold everything; only what is sent changes.

Rules (the same as the server-side edit in ``context_edit``):
  * trigger: the prompt estimate reaches ``context_edit_trigger`` (default 100k; on a small window it is capped at
    60% of the auto-compact threshold so it fires before compaction);
  * keep the newest ``context_edit_keep`` (6) tool results; never clear Edit/Write/Task-like results or a /pin;
  * a clear must free at least ``context_edit_clear_at_least`` tokens, so the prefix cache of a provider that caches
    prefixes is rewritten rarely and for a good reason;
  * batches, stable stubs: once a result is cleared it stays cleared with the same stub text on every later call
    (the request prefix does not change again until the next batch);
  * pairs are never split: only the *content* of a tool result changes, never a tool_use, a tool_call, a result's
    id or its position.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axon.agent.context_edit import (DEFAULT_CLEAR_AT_LEAST, DEFAULT_KEEP, DEFAULT_TRIGGER, EXCLUDE_TOOLS, MIN_TRIGGER,
                                     config_for, describe)

MIN_RESULT_TOKENS = 60       # a result smaller than its stub is left alone
_STUB_NUMBERED = "[old result cleared; full text in /output {n}]"
_STUB_PLAIN = "[old result cleared to save context; run the tool again if you need it]"
STUB_TOKENS = 18


@dataclass
class _Slot:
    msg: int
    block: int | None       # index in the message's content list, None for an OpenAI ``tool`` message
    id: str
    name: str
    tokens: int
    stubbed: bool


def _tokens(content: Any) -> int:
    from axon.agent.state import estimate_content_tokens
    try:
        return int(estimate_content_tokens(content or ""))
    except Exception:
        return len(str(content or "")) // 4


def _is_stub(content: Any) -> bool:
    return isinstance(content, str) and content.startswith("[old result cleared")


def _names(messages: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                out[str(tc.get("id") or "")] = str(fn.get("name") or tc.get("name") or "")
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    out[str(b.get("id") or "")] = str(b.get("name") or "")
    return out


def _slots(messages: list[dict[str, Any]]) -> list[_Slot]:
    names = _names(messages)
    slots: list[_Slot] = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if m.get("role") == "tool":
            tid = str(m.get("tool_call_id") or "")
            slots.append(_Slot(i, None, tid, names.get(tid, ""), _tokens(content), _is_stub(content)))
        elif isinstance(content, list):
            for j, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = str(b.get("tool_use_id") or "")
                    inner = b.get("content", "")
                    slots.append(_Slot(i, j, tid, names.get(tid, ""), _tokens(inner), _is_stub(inner)))
    return slots


def _state(agent: Any) -> dict[str, Any]:
    st = getattr(agent, "_client_ctx", None)
    if st is None:
        st = {"cleared": {}, "saved": 0, "stubs": {}}
        try:
            agent._client_ctx = st
        except Exception:
            pass
    return st


def enabled(agent: Any) -> bool:
    """True when this provider needs the client-side path: editing is on and the server is not doing it."""
    s = agent.settings
    if not bool(getattr(s, "context_editing", True)):
        return False
    provider = getattr(agent, "provider", None)
    try:
        if config_for(agent) is not None and not getattr(provider, "_context_edit_off", False):
            return False            # the API clears by itself (native accelerator)
    except Exception:
        pass
    return True


def stub_for(agent: Any, tool_use_id: str) -> str:
    try:
        from axon.ui import render
        n = render.output_number_for(tool_use_id)
    except Exception:
        n = 0
    return _STUB_NUMBERED.format(n=n) if n else _STUB_PLAIN


def _limits(agent: Any) -> tuple[int, int, int]:
    s = agent.settings
    trigger = max(MIN_TRIGGER, int(getattr(s, "context_edit_trigger", DEFAULT_TRIGGER) or DEFAULT_TRIGGER))
    try:
        trigger = min(trigger, max(MIN_TRIGGER, int(0.6 * agent.context.threshold(s.model))))
    except Exception:
        pass
    keep = max(1, int(getattr(s, "context_edit_keep", DEFAULT_KEEP) or DEFAULT_KEEP))
    least = int(getattr(s, "context_edit_clear_at_least", DEFAULT_CLEAR_AT_LEAST) or 0)
    return trigger, keep, max(0, min(least, trigger // 2))


def _apply(messages: list[dict[str, Any]], slots: list[_Slot], stubs: dict[str, str]) -> list[dict[str, Any]]:
    """A copy of *messages* where every slot named in *stubs* carries its stub (other messages are shared)."""
    edits: dict[int, list[_Slot]] = {}
    for sl in slots:
        if sl.id in stubs and not sl.stubbed:
            edits.setdefault(sl.msg, []).append(sl)
    if not edits:
        return messages
    out = list(messages)
    for i, group in edits.items():
        m = dict(messages[i])
        if m.get("role") == "tool":
            m["content"] = stubs[group[0].id]
            m.pop("images", None)
        else:
            blocks = list(m["content"])
            for sl in group:
                b = dict(blocks[sl.block])
                b["content"] = stubs[sl.id]
                b.pop("images", None)
                blocks[sl.block] = b
            m["content"] = blocks
        out[i] = m
    return out


def view(agent: Any, system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
    """(messages to send, ContextEditEvent or None). With nothing to clear the live list itself is returned."""
    conv = agent.conversation
    messages = conv.messages
    try:
        if not enabled(agent) or len(messages) < 4:
            agent.context.exact.saved = 0
            return messages, None
        st = _state(agent)
        slots = _slots(messages)
        live = {sl.id for sl in slots}
        st["cleared"] = {k: v for k, v in st["cleared"].items() if k in live}       # compaction removed some
        cleared: dict[str, int] = st["cleared"]
        trigger, keep, least = _limits(agent)
        pins = getattr(getattr(agent, "context", None), "pins", None) or {}
        eligible = [sl for sl in slots[:-keep] if not sl.stubbed and sl.id and sl.id not in pins
                    and sl.name not in EXCLUDE_TOOLS and sl.tokens >= MIN_RESULT_TOKENS]
        event = None
        fresh = [sl for sl in eligible if sl.id not in cleared]
        if fresh:
            prompt = agent.context.projected_tokens(conv, system, tools) - int(sum(cleared.values()))
            freed = sum(max(0, sl.tokens - STUB_TOKENS) for sl in fresh)
            if prompt >= trigger and freed >= least:
                for sl in fresh:
                    cleared[sl.id] = max(0, sl.tokens - STUB_TOKENS)
                from axon.agent.trace import ContextEditEvent
                event = ContextEditEvent(cleared_tool_uses=len(fresh), cleared_input_tokens=freed, message=describe(len(fresh), freed))
        st["saved"] = int(sum(cleared.values()))
        agent.context.exact.saved = st["saved"]       # the context meter and the calibration count the prompt as sent
        if not cleared:
            return messages, None
        cache = st.setdefault("stubs", {})
        stubs = {tid: cache.setdefault(tid, stub_for(agent, tid)) for tid in cleared}
        return _apply(messages, slots, stubs), event
    except Exception:
        return messages, None


def saved_tokens(agent: Any) -> int:
    """Tokens the clearing keeps out of the prompt right now (used by the context meter)."""
    st = getattr(agent, "_client_ctx", None)
    return int(st.get("saved", 0)) if isinstance(st, dict) else 0
