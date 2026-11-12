"""Server-side tool search: the Anthropic accelerator for deferred tool loading (docs: Tool search tool).

The GENERAL path is the client-side one (``tools.interactive.ToolSearchTool``: a compact index of deferred
tool names plus a ``ToolSearch`` tool that loads schemas on demand); it works with every provider. On the official
Anthropic host with a model the docs list, Axon instead sends every deferred tool with ``defer_loading: true`` plus
the server-side ``tool_search_tool_bm25_20251119`` tool, so the API keeps those definitions out of the cached
prefix and runs the search itself (adding or changing deferred tools therefore never invalidates the prompt cache).

* The response carries ``server_tool_use`` / ``tool_search_tool_result`` (with ``tool_reference``) blocks. They are
  kept in the native assistant content and replayed unchanged (the docs: never answer them with a tool_result).
  Other providers ignore blocks they do not know, and :func:`strip_history` removes them when a request is retried
  without the feature.
* A 400 that names ``defer_loading`` / ``tool_search`` is answered by :func:`strip`: the request is retried once with
  every tool fully loaded.
"""
from __future__ import annotations

import copy
from typing import Any

from axon.providers import toolflow

SERVER_TOOL_TYPE = "tool_search_tool_bm25_20251119"
SERVER_TOOL_NAME = "tool_search_tool_bm25"
CLIENT_TOOL_NAME = "ToolSearch"
# Built-in tools that are rarely needed: deferred (not removed) when the server-side search is on.
RARELY_USED = frozenset({"NotebookEdit", "Doctor"})
_SERVER_BLOCKS = ("server_tool_use", "tool_search_tool_result")


def mode(settings: Any) -> str:
    v = str(getattr(settings, "server_tool_search", "auto") or "auto").strip().lower()
    return v if v in ("auto", "on", "off") else "auto"


def supported(model: str) -> bool:
    """The models the docs list: Claude 4.5 and newer (Opus 4.1 and earlier are out)."""
    return toolflow.supports_eager_streaming(model)


def active(settings: Any, model: str, *, official: bool) -> bool:
    return official and mode(settings) != "off" and supported(model)


def deferred_defs(registry: Any) -> list[dict[str, Any]]:
    """Anthropic-style definitions of the tools the client-side ToolSearch currently holds back."""
    try:
        search = registry.tools.get(CLIENT_TOOL_NAME)
        held = dict(getattr(search, "deferred", {}) or {})
    except Exception:
        return []
    out = []
    for name, t in held.items():
        out.append({"name": name, "description": str(getattr(t, "full_description", None) or t.description or ""),
                    "input_schema": copy.deepcopy(t.schema) or {"type": "object", "properties": {}}})
    return out


def reveal_all(registry: Any) -> int:
    """Make every deferred tool callable by name (hidden, never in the schema list) so a call the server-side
    search unlocked can be run. Returns how many were added."""
    from axon.tools.interactive import _Revealed
    n = 0
    try:
        held = dict(getattr(registry.tools.get(CLIENT_TOOL_NAME), "deferred", {}) or {})
    except Exception:
        return 0
    for name, t in held.items():
        if not registry.has(name):
            registry.register(_Revealed(t))
            n += 1
    return n


def apply(kwargs: dict[str, Any], model: str, *, official: bool, settings: Any = None) -> list[str]:
    """Rewrite ``kwargs["tools"]`` for server-side search. Returns what was done (empty: nothing changed)."""
    tools = kwargs.get("tools")
    if not tools or not official or not supported(model) or mode(settings) == "off":
        return []
    extra = [d for d in (toolflow.get_hints().get("deferred_defs") or []) if isinstance(d, dict) and d.get("name")]
    rare = [t for t in tools if isinstance(t, dict) and t.get("name") in RARELY_USED]
    if not extra and not rare:
        return []
    keep: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    marker = False
    have = {t.get("name") for t in tools if isinstance(t, dict)}
    for t in tools:
        if not isinstance(t, dict) or t.get("name") == CLIENT_TOOL_NAME:
            continue
        if t.get("name") in RARELY_USED:
            t = dict(t)
            marker = bool(t.pop("cache_control", None)) or marker
            t["defer_loading"] = True
            deferred.append(t)
        else:
            keep.append(t)
    for d in extra:
        if d["name"] in have:
            continue
        deferred.append({**d, "defer_loading": True})
    if keep and marker and not any("cache_control" in t for t in keep):
        keep[-1] = {**keep[-1], "cache_control": {"type": "ephemeral"}}
    kwargs["tools"] = [*keep, {"type": SERVER_TOOL_TYPE, "name": SERVER_TOOL_NAME}, *deferred]
    return [f"server_tool_search:{len(deferred)}"]


def strip_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages without the server search blocks (the request no longer declares the search tool)."""
    out = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            kept = [b for b in c if not (isinstance(b, dict) and b.get("type") in _SERVER_BLOCKS)]
            if len(kept) != len(c):
                m = {**m, "content": kept or [{"type": "text", "text": "[tool search]"}]}
        out.append(m)
    return out


def strip(kwargs: dict[str, Any], error_text: str) -> bool:
    """After a 400 naming the feature: retry with every tool loaded. True when something was removed."""
    text = (error_text or "").lower()
    if not any(k in text for k in ("defer_loading", "tool_search", "tool search", "tool_reference")):
        return False
    changed = False
    new_tools = []
    for t in kwargs.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == SERVER_TOOL_TYPE:
            changed = True
            continue
        if isinstance(t, dict) and t.pop("defer_loading", None) is not None:
            changed = True
        new_tools.append(t)
    if changed:
        kwargs["tools"] = new_tools
        kwargs["messages"] = strip_history(kwargs.get("messages") or [])
    return changed


def mode_note(agent: Any) -> str:
    """The one /context line: how deferred tools are loaded."""
    try:
        official = bool(getattr(agent.provider, "_official", False))
        on = active(agent.settings, agent.settings.model, official=official)
    except Exception:
        on = False
    return "server-side tool search" if on else "ToolSearch"
