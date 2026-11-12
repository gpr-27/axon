"""Request-level tool streaming and parallelism switches for the Anthropic Messages API.

* ``eager_input_streaming: true`` on a tool definition (docs: Fine-grained tool streaming) makes the API send
  that tool's input as it is generated instead of buffering and validating it. Axon turns it on for the
  file-writing tools only, on a model the docs list and the first-party API, so the file content can be shown
  while it is written. The input may then be partial or invalid JSON: the provider never executes such a call
  (it gets an INVALID_JSON error result, see ``invalid_json_content``).
* ``tool_choice: {"type": "auto", "disable_parallel_tool_use": true}`` (docs: Parallel tool use) makes the
  model call at most one tool per reply. Sent only when the user turned parallel tools off (``parallel_tools``
  1 or less). The local executor serializes ordering-sensitive calls whatever the request said.
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Any

# Tools whose input is large text worth streaming. Bash is left out: its command is short and a cut-off
# command must never run, so buffered validation is the better trade there.
EAGER_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

_FIELDS = ("eager_input_streaming", "disable_parallel_tool_use")

# Per-request hints set by the agent loop just before it streams (the provider object is shared and its
# settings snapshot can be stale). A ContextVar so a subagent thread never sees its parent's value.
_hints: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("axon_request_hints", default={})


def set_hints(**hints: Any) -> None:
    _hints.set(dict(hints))


def get_hints() -> dict[str, Any]:
    return dict(_hints.get())


_MODEL = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")


def supports_eager_streaming(model: str) -> bool:
    """True for the models the docs list (Claude 4.5 and newer; not Claude 3.x, Sonnet 4, Opus 4 / 4.1)."""
    m = _MODEL.search((model or "").lower())
    if not m:
        return False
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if family in ("fable", "mythos"):
        return True
    if major >= 5:
        return True
    return major == 4 and minor >= 5


def apply(kwargs: dict[str, Any], model: str, *, official: bool) -> list[str]:
    """Add the switches to a request in place. Returns what was added (for tests and diagnostics)."""
    added: list[str] = []
    tools = kwargs.get("tools")
    if not tools:
        return added
    if official and supports_eager_streaming(model):
        for t in tools:
            if isinstance(t, dict) and t.get("name") in EAGER_TOOLS and "eager_input_streaming" not in t:
                t["eager_input_streaming"] = True
                added.append(f"eager:{t['name']}")
    if get_hints().get("serial_tools") and "tool_choice" not in kwargs:
        kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        added.append("disable_parallel_tool_use")
    return added


def strip(kwargs: dict[str, Any], error_text: str) -> bool:
    """After a 400 that names one of the fields (a gateway that does not know it): drop it from the request.
    True when something was removed, so the caller retries once."""
    text = (error_text or "").lower()
    changed = False
    if "eager_input_streaming" in text or "eager" in text:
        for t in kwargs.get("tools") or []:
            if isinstance(t, dict) and t.pop("eager_input_streaming", None) is not None:
                changed = True
    if ("disable_parallel_tool_use" in text or "tool_choice" in text) and isinstance(kwargs.get("tool_choice"), dict):
        kwargs.pop("tool_choice", None)
        changed = True
    return changed


def invalid_json_content(raw: str, reason: str, limit: int = 1500) -> str:
    """The tool_result text for input that could not be parsed: the docs' INVALID_JSON wrapper (built with a
    JSON library), with the raw text cut short so a half-written file is not sent back whole."""
    raw = raw or ""
    shown = raw if len(raw) <= limit else raw[:limit // 2] + f"...[{len(raw) - limit} chars omitted]..." + raw[-limit // 2:]
    return json.dumps({"INVALID_JSON": shown, "error": reason})


# ── OpenAI-compatible wire: ``parallel_tool_calls: false`` ───────────────────────────────────────────────
OPENAI_PARALLEL_FIELD = "parallel_tool_calls"


def apply_openai(body: dict[str, Any], rejected: set[str], base_url: str) -> bool:
    """Ask an OpenAI-style endpoint for at most one tool call per reply (only when the user turned parallel tools
    off). Local inference servers and a host that already refused the field are left alone; the local serial
    executor applies to every provider regardless. True when the field was added."""
    if not body.get("tools") or not get_hints().get("serial_tools") or OPENAI_PARALLEL_FIELD in rejected:
        return False
    try:
        from axon.providers.transport import is_local_url
        if is_local_url(base_url):
            return False
    except Exception:
        pass
    body[OPENAI_PARALLEL_FIELD] = False
    return True


def rejected_openai(status: Any, error_text: str) -> bool:
    """True when a 400/422 names the field, so it is dropped and the request retried once."""
    return status in (400, 422) and OPENAI_PARALLEL_FIELD in (error_text or "").lower()
