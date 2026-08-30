"""
OpenAI-Compatible Provider using raw httpx2 and Stainless fingerprint headers.
"""
from __future__ import annotations
import json
import secrets
from typing import Any, Iterator
import httpx2 as httpx
from axon.config import Settings
from axon.errors import ProviderError
from axon.providers.base import (
    AssistantTurn,
    Block,
    Provider,
    StopReason,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    TextDelta,
    ToolArgsDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
    Usage,
)

_FINGERPRINT = {
    "user-agent": "Anthropic/Python 1.0.0",
    "x-stainless-lang": "python",
    "x-stainless-os": "MacOS",
    "x-stainless-arch": "arm64",
    "x-stainless-runtime": "CPython",
}

def sanitize_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sanitizes conversation messages for OpenAI/DeepSeek API:
    1. Unpacks any nested message dicts where content is accidentally a dict.
    2. Ensures every message's content is strictly a string or list, never None/null.
    3. Converts any Anthropic-style tool_result blocks in user messages to OpenAI tool messages.
    4. Ensures every assistant message with tool_calls is strictly followed by tool messages for each tool_call_id.
    5. Synthesizes fallback tool response messages for any missing tool_call_id.
    6. Drops any orphaned tool messages whose tool_call_id does not match the preceding assistant message.
    """
    if not messages:
        return []

    # First pass: clean types and unnest accidental dicts
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = dict(msg)

        # Unpack accidental nested message dict in content
        if isinstance(m.get("content"), dict) and "role" in m["content"]:
            m = dict(m["content"])
        elif isinstance(m.get("content"), dict):
            m["content"] = json.dumps(m["content"])

        # DeepSeek and OpenAI Rust deserializers reject content: null
        if m.get("content") is None:
            m["content"] = ""

        # Convert Anthropic tool_result blocks in user message to OpenAI tool messages
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            has_tool_res = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
            if has_tool_res:
                user_texts: list[str] = []
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        raw_c = b.get("content", "")
                        c_str = raw_c if isinstance(raw_c, str) else json.dumps(raw_c)
                        cleaned.append({
                            "role": "tool",
                            "tool_call_id": b.get("tool_use_id", ""),
                            "content": c_str,
                        })
                    elif isinstance(b, dict) and b.get("type") == "text":
                        user_texts.append(b.get("text", ""))
                if user_texts:
                    cleaned.append({"role": "user", "content": "\n".join(user_texts)})
                continue

        cleaned.append(m)

    sanitized: list[dict[str, Any]] = []
    i = 0
    while i < len(cleaned):
        m = cleaned[i]
        role = m.get("role")

        if role == "assistant" and m.get("tool_calls"):
            tool_calls = m.get("tool_calls", [])
            expected_ids = [tc["id"] for tc in tool_calls if isinstance(tc, dict) and "id" in tc]

            sanitized.append(m)
            i += 1

            # Gather following tool messages
            found_tool_msgs: list[dict[str, Any]] = []
            found_ids: set[str] = set()
            while i < len(cleaned) and cleaned[i].get("role") == "tool":
                tm = cleaned[i]
                tid = tm.get("tool_call_id")
                if tid in expected_ids:
                    found_tool_msgs.append(tm)
                    found_ids.add(tid)
                i += 1

            sanitized.extend(found_tool_msgs)

            # Synthesize fallback tool responses for any missing expected_ids
            for tid in expected_ids:
                if tid not in found_ids:
                    sanitized.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "[Tool execution completed or interrupted]",
                    })
        elif role == "tool":
            # Orphaned tool message with no preceding assistant tool_calls -> skip
            i += 1
        else:
            sanitized.append(m)
            i += 1

    return sanitized


class OpenAICompatProvider:
    name: str = "openai_compat"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._url = f"{settings.base_url.rstrip('/')}/v1/chat/completions"
        self._last_turn: AssistantTurn | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
            "content-type": "application/json",
            **_FINGERPRINT,
        }

    def stream(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str | None = None,
        thinking: bool = True,
    ) -> Iterator[StreamEvent]:
        system_text = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        raw_messages: list[dict[str, Any]] = []
        if system_text:
            raw_messages.append({"role": "system", "content": system_text})
        raw_messages.extend(messages)

        openai_messages = sanitize_openai_messages(raw_messages)

        body: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if effort:
            e_str = str(effort).lower()
            if e_str in ("reflex", "low"):
                body["reasoning_effort"] = "low"
            elif e_str in ("balanced", "medium"):
                body["reasoning_effort"] = "medium"
            elif e_str in ("synapse", "quantum", "high", "xhigh", "max", "hyper"):
                body["reasoning_effort"] = "high"
            else:
                body["reasoning_effort"] = e_str
        if tools:
            body["tools"] = tools

        full_text = ""
        full_reasoning = ""
        # Accumulate tool calls keyed by index (ADR & spec)
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        stop_reason: StopReason = "end_turn"
        usage = Usage()

        try:
            with httpx.stream("POST", self._url, headers=self._headers(), json=body, timeout=120) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise ProviderError(f"HTTP {resp.status_code}: {resp.text}", status=resp.status_code, body=resp.text)

                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except Exception:
                        continue

                    # Parse Usage
                    if "usage" in chunk and chunk["usage"]:
                        u = chunk["usage"]
                        usage = Usage(
                            input=u.get("prompt_tokens", 0) or 0,
                            output=u.get("completion_tokens", 0) or 0,
                            cache_read=u.get("prompt_cache_hit_tokens", 0) or 0,
                            reasoning=u.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0,
                        )

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    finish = choice.get("finish_reason")
                    if finish:
                        if finish == "tool_calls":
                            stop_reason = "tool_use"
                        elif finish == "length":
                            stop_reason = "max_tokens"
                        else:
                            stop_reason = "end_turn"

                    delta = choice.get("delta", {})

                    # Reasoning content (thinking)
                    rc = delta.get("reasoning_content", "")
                    if rc:
                        full_reasoning += rc
                        yield ThinkingDelta(text=rc)

                    # Text content
                    ct = delta.get("content", "")
                    if ct:
                        full_text += ct
                        yield TextDelta(text=ct)

                    # Tool call deltas
                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tc_id = tc_delta.get("id") or f"call_{secrets.token_hex(4)}"
                                fn_name = tc_delta.get("function", {}).get("name", "")
                                tool_calls_acc[idx] = {
                                    "id": tc_id,
                                    "name": fn_name,
                                    "arguments": "",
                                }
                                yield ToolUseStart(id=tc_id, name=fn_name)

                            frag = tc_delta.get("function", {}).get("arguments", "")
                            if frag:
                                tool_calls_acc[idx]["arguments"] += frag
                                yield ToolArgsDelta(id=tool_calls_acc[idx]["id"], fragment=frag)

                # Assemble blocks
                blocks: list[Block] = []
                if full_reasoning:
                    blocks.append(ThinkingBlock(text=full_reasoning))
                if full_text:
                    blocks.append(TextBlock(text=full_text))

                for idx in sorted(tool_calls_acc.keys()):
                    t_item = tool_calls_acc[idx]
                    parsed_input = {}
                    if t_item["arguments"].strip():
                        try:
                            parsed_input = json.loads(t_item["arguments"])
                        except Exception:
                            parsed_input = {"raw": t_item["arguments"]}
                    blocks.append(ToolUseBlock(id=t_item["id"], name=t_item["name"], input=parsed_input))
                    yield ToolUseComplete(id=t_item["id"])

                # Build native assistant representation
                native_dict: dict[str, Any] = {
                    "role": "assistant",
                    "content": full_text or "",
                }
                if full_reasoning:
                    native_dict["reasoning_content"] = full_reasoning

                if tool_calls_acc:
                    native_dict["tool_calls"] = [
                        {
                            "id": t_item["id"],
                            "type": "function",
                            "function": {
                                "name": t_item["name"],
                                "arguments": t_item["arguments"],
                            }
                        }
                        for t_item in tool_calls_acc.values()
                    ]

                self._last_turn = AssistantTurn(
                    blocks=blocks,
                    stop_reason=stop_reason,
                    usage=usage,
                    native=native_dict,
                )
                yield TurnComplete(stop_reason=stop_reason, usage=usage)

        except Exception as e:
            err_str = str(e)
            if "sensitive_words_detected" in err_str or "sensitive words" in err_str.lower():
                raise ProviderError(
                    "Content filter triggered (sensitive_words_detected).\n"
                    "  The model's content filter flagged this conversation context.\n"
                    "  Fix: try /clear to reset context, shorten your prompt, or switch model with /model."
                ) from e
            raise ProviderError(f"OpenAI streaming failed: {e}") from e

    def finalize(self) -> AssistantTurn:
        if self._last_turn is None:
            return AssistantTurn(blocks=[], stop_reason="end_turn", usage=Usage())
        turn = self._last_turn
        self._last_turn = None
        return turn

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        # OpenAI requires N separate messages, one per result with role='tool' (Asymmetry Table)
        messages = []
        for r in results:
            content = r.content
            if r.is_error and not content.startswith("Error:"):
                content = f"Error: {content}"
            messages.append({
                "role": "tool",
                "tool_call_id": r.tool_use_id,
                "content": content,
            })
        return messages

    def supports(self, feature: str) -> bool:
        return feature in ("tools", "streaming")
