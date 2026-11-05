"""
"Prompt tools": tool calling for endpoints that silently drop the ``tools`` request field.

Some OpenAI-compatible gateways accept ``tools`` and then ignore it, so the model answers "I have no
tools". For such an endpoint (``native_tools = false``) the tools are described inside the system
message instead, and the model is asked to write each call as text::

    <tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>

Three small steps, all pure functions:

* ``protocol_text``    the instructions + tool list that are added to the system message
* ``messages_for_wire`` chat messages with tool calls / results rewritten as plain text
* ``parse_reply``      the model's reply split into clean text and ``ToolUseBlock`` calls
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from axon.providers.base import ToolUseBlock, parse_tool_arguments

OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"

# A call runs to its closing tag; a reply that ends inside a call (cut off, or the model forgot the
# closing tag) counts as a call too, and is reported as invalid JSON when it is incomplete.
_CALL_RE = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')

_PROTOCOL_HEAD = """\
# Tool calling protocol

You have tools, but this endpoint has no native tool support: you call them by writing text.
To call a tool, output a block exactly like this (one JSON object, valid JSON, double quotes):

<tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>

Rules:
- "name" is one of the tools below; "arguments" is a JSON object that follows the tool's parameters.
- You may write several <tool_call> blocks in one reply when the calls are independent.
- After the last <tool_call> block, STOP and wait. Do not guess the result and do not write <tool_result> yourself.
- The results come back in the next message as <tool_result name="..." id="...">...</tool_result>.
- When you do not need a tool, answer in plain text without any <tool_call> block.
- Never say that you have no tools: use the ones listed here.

Available tools:"""


# ─── Request side ──────────────────────────────────────────────────────────

def tool_line(tool: dict[str, Any]) -> str:
    """One tool as ``- name: first line of the description\\n  parameters: {compact JSON schema}``."""
    fn = tool.get("function") or {}
    description = str(fn.get("description") or "").strip().split("\n", 1)[0]
    params = json.dumps(fn.get("parameters") or {}, separators=(",", ":"), ensure_ascii=False)
    return f"- {fn.get('name', '')}: {description}\n  parameters: {params}"


def protocol_text(tools: list[dict[str, Any]]) -> str:
    """The instruction block for *tools* (OpenAI function format, see ``normalize_tools_for_chat``)."""
    return _PROTOCOL_HEAD + "\n" + "\n".join(tool_line(t) for t in tools)


def format_tool_call(name: str, arguments: Any) -> str:
    args, _error = parse_tool_arguments(arguments)
    return f"{OPEN_TAG}{json.dumps({'name': name, 'arguments': args}, ensure_ascii=False)}{CLOSE_TAG}"


def format_tool_result(name: str, call_id: str, content: str) -> str:
    return f'<tool_result name="{name}" id="{call_id}">\n{content}\n</tool_result>'


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _with_protocol(messages: list[dict[str, Any]], protocol: str) -> list[dict[str, Any]]:
    """Messages with *protocol* appended to the first system message (one is added when missing)."""
    if messages and messages[0].get("role") == "system":
        first = dict(messages[0])
        existing = _text_of(first.get("content"))
        first["content"] = f"{existing}\n\n{protocol}" if existing else protocol
        return [first, *messages[1:]]
    return [{"role": "system", "content": protocol}, *messages]


def _assistant_as_text(message: dict[str, Any]) -> dict[str, Any]:
    parts = [_text_of(message.get("content"))]
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.append(format_tool_call(fn.get("name", ""), fn.get("arguments")))
    return {"role": "assistant", "content": "\n".join(p for p in parts if p)}


def _merge_user(previous: dict[str, Any], new: dict[str, Any]) -> None:
    """Fold *new* into the *previous* user message (servers dislike two user turns in a row)."""
    old, add = previous.get("content", ""), new.get("content", "")
    if isinstance(old, str) and isinstance(add, str):
        previous["content"] = f"{old}\n\n{add}".strip()
        return
    as_parts = lambda c: c if isinstance(c, list) else ([{"type": "text", "text": c}] if c else [])  # noqa: E731
    previous["content"] = as_parts(old) + as_parts(add)


def messages_for_wire(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite sanitized chat *messages* for an endpoint without native tools.

    Assistant ``tool_calls`` become ``<tool_call>`` text, ``role="tool"`` messages become user messages
    holding ``<tool_result>`` blocks (ids unchanged), and the protocol is appended to the system message.
    """
    names_by_id: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                names_by_id[call.get("id", "")] = (call.get("function") or {}).get("name", "")
            out.append(_assistant_as_text(message))
            continue
        if role == "tool":
            call_id = message.get("tool_call_id", "")
            text = format_tool_result(names_by_id.get(call_id, ""), call_id, _text_of(message.get("content")))
            message = {"role": "user", "content": text}
            role = "user"
        else:
            message = dict(message)
        if role == "user" and out and out[-1].get("role") == "user":
            _merge_user(out[-1], message)
            continue
        out.append(message)
    return _with_protocol(out, protocol_text(tools))


# ─── Response side ─────────────────────────────────────────────────────────

def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _strip_fence(body: str) -> str:
    body = body.strip()
    match = _FENCE_RE.match(body)
    return match.group(1).strip() if match else body


def _invalid_call(body: str, reason: str) -> ToolUseBlock:
    """A call whose JSON could not be read: the agent loop answers it with an error result."""
    named = _NAME_RE.search(body)
    name = named.group(1) if named else "invalid_tool_call"
    return ToolUseBlock(id=new_call_id(), name=name, input={"raw": body}, parse_error=reason)


def parse_call(body: str) -> ToolUseBlock:
    """One ``<tool_call>`` body -> ToolUseBlock (``parse_error`` set when it is not usable)."""
    body = _strip_fence(body)
    try:
        data = json.loads(body)
    except ValueError as e:
        return _invalid_call(body, f"Tool call is not valid JSON ({e})")
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
        return _invalid_call(body, 'Tool call must be a JSON object with a "name" string and an "arguments" object')
    raw_args = data.get("arguments", data.get("input", data.get("parameters")))
    args, error = parse_tool_arguments(raw_args)
    return ToolUseBlock(id=new_call_id(), name=data["name"].strip(), input=args, parse_error=error)


# Claude models sometimes answer in the format they were trained on instead of <tool_call>:
#   <function_calls><invoke name="Grep"><parameter name="pattern">x</parameter></invoke></function_calls>
_BLOCK_RE = re.compile(r"<(?:\w+:)?function_calls>(.*?)(?:</(?:\w+:)?function_calls>|\Z)", re.DOTALL)
_INVOKE_RE = re.compile(r'<(?:\w+:)?invoke name="([^"]+)">(.*?)(?:</(?:\w+:)?invoke>|\Z)', re.DOTALL)
_PARAM_RE = re.compile(r'<(?:\w+:)?parameter name="([^"]+)">(.*?)(?:</(?:\w+:)?parameter>|\Z)', re.DOTALL)


def _parameter_value(raw: str) -> Any:
    """A parameter's value: JSON when it parses (numbers, lists, objects), otherwise the plain text."""
    value = raw.strip()
    try:
        return json.loads(value)
    except ValueError:
        return value


def _parse_invoke_blocks(text: str) -> list[ToolUseBlock]:
    calls = []
    for block in _BLOCK_RE.finditer(text):
        for name, body in _INVOKE_RE.findall(block.group(1)):
            arguments = {key: _parameter_value(value) for key, value in _PARAM_RE.findall(body)}
            calls.append(ToolUseBlock(id=new_call_id(), name=name, input=arguments))
    return calls


def parse_reply(text: str) -> tuple[str, list[ToolUseBlock]]:
    """Split the model's full reply into (text without call blocks, tool calls in order)."""
    calls = [parse_call(match.group(1)) for match in _CALL_RE.finditer(text)]
    clean = _CALL_RE.sub("", text)
    if not calls:
        calls = _parse_invoke_blocks(clean)
        clean = _BLOCK_RE.sub("", clean)
    return clean.strip(), calls


def native_tool_calls(calls: list[ToolUseBlock]) -> list[dict[str, Any]]:
    """The calls as OpenAI ``tool_calls`` (the shape stored in the conversation and replayed later)."""
    out = []
    for call in calls:
        raw = call.input.get("raw") if call.parse_error else None
        arguments = raw if isinstance(raw, str) else json.dumps(call.input, ensure_ascii=False)
        out.append({"id": call.id, "type": "function", "function": {"name": call.name, "arguments": arguments}})
    return out


__all__ = [
    "format_tool_call", "format_tool_result", "messages_for_wire", "native_tool_calls", "new_call_id",
    "parse_call", "parse_reply", "protocol_text",
]
