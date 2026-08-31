"""
OpenAI-Compatible Provider using raw httpx2 and Stainless fingerprint headers.
"""
from __future__ import annotations
import json
from typing import Any, Iterator
try:
    import httpx
except ImportError:
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

        # Convert Anthropic tool_result blocks in user message to OpenAI tool messages, and format multimodal image blocks
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
            else:
                converted_blocks: list[dict[str, Any]] = []
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "image":
                        src = b.get("source", {})
                        media_type = src.get("media_type", "image/png")
                        data_b64 = src.get("data", "")
                        converted_blocks.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{data_b64}"
                            }
                        })
                    else:
                        converted_blocks.append(b)
                cleaned.append({"role": "user", "content": converted_blocks})
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
        base = settings.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            self._url = base
        elif "googleapis.com" in base:
            clean = base.rstrip("/")
            if not clean.endswith("/openai"):
                clean = f"{clean}/openai"
            self._url = f"{clean}/chat/completions"
        elif "openrouter.ai" in base:
            clean = base.rstrip("/")
            if not clean.endswith("/api/v1"):
                clean = f"{clean.rstrip('/api').rstrip('/v1')}/api/v1"
            self._url = f"{clean}/chat/completions"
        elif base.endswith("/v1"):
            self._url = f"{base}/chat/completions"
        else:
            self._url = f"{base}/v1/chat/completions"
        self._last_turn: AssistantTurn | None = None

    def _headers(self) -> dict[str, str]:
        key_val = self.settings.api_key.get_secret_value() if self.settings.api_key else ""
        headers = {
            "content-type": "application/json",
            **_FINGERPRINT,
        }
        if key_val and key_val != "local":
            headers["authorization"] = f"Bearer {key_val}"
        return headers

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
        is_reasoning_model = any(
            k in model.lower()
            for k in ("o1", "o3", "o4", "deepseek-reasoner", "deepseek-r1", "r1:", "r1-", "reasoning", "qwq")
        )
        if effort and is_reasoning_model:
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

        def _parse_stream(resp: httpx.Response) -> Iterator[StreamEvent]:
            nonlocal full_text, full_reasoning, tool_calls_acc, stop_reason, usage
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
                    prompt_t = u.get("prompt_tokens", 0) or 0
                    cache_hit_t = (
                        u.get("prompt_cache_hit_tokens", 0)
                        or u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                        or 0
                    )
                    # DeepSeek/OpenAI report prompt_tokens as total input (cached + uncached);
                    # if a gateway only returns uncached prompt_tokens, add cache_hit_t.
                    if prompt_t >= cache_hit_t:
                        total_input = prompt_t
                    else:
                        total_input = prompt_t + cache_hit_t
                    usage = Usage(
                        input=total_input,
                        output=u.get("completion_tokens", 0) or 0,
                        cache_read=cache_hit_t,
                        reasoning=u.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0,
                    )

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})

                # DeepSeek and GLM reasoning chunk
                reasoning_chunk = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if reasoning_chunk:
                    full_reasoning += reasoning_chunk
                    if thinking:
                        yield ThinkingDelta(text=reasoning_chunk)

                # Regular assistant response text chunk
                content_chunk = delta.get("content") or ""
                if content_chunk:
                    full_text += content_chunk
                    yield TextDelta(text=content_chunk)

                # Tool call deltas
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": tc.get("id", ""),
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": "",
                            }
                        if tc.get("id"):
                            tool_calls_acc[idx]["id"] = tc["id"]
                        if tc.get("function", {}).get("name"):
                            tool_calls_acc[idx]["name"] = tc["function"]["name"]
                        if tc.get("function", {}).get("arguments"):
                            frag = tc["function"]["arguments"]
                            tool_calls_acc[idx]["arguments"] += frag
                            yield ToolArgsDelta(id=tool_calls_acc[idx]["id"], fragment=frag)

                if choice.get("finish_reason"):
                    fr = choice["finish_reason"]
                    if fr in ("tool_calls", "function_call"):
                        stop_reason = "tool_use"
                    elif fr == "length":
                        stop_reason = "max_tokens"
                    else:
                        stop_reason = "end_turn"

        try:
            with httpx.stream("POST", self._url, headers=self._headers(), json=body, timeout=120) as resp:
                if resp.status_code != 200:
                    resp.read()
                    err_text = resp.text
                    # Check if thinking/reasoning_effort is rejected
                    if "does not support thinking" in err_text.lower() or "reasoning_effort" in err_text.lower() or "does not support reasoning" in err_text.lower() or "thinking" in err_text.lower():
                        body.pop("reasoning_effort", None)
                        with httpx.stream("POST", self._url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code == 200:
                                yield from _parse_stream(retry_resp)
                                return
                            else:
                                retry_resp.read()
                                err_text = retry_resp.text

                    # Check if stream_options is rejected by legacy local server
                    if "stream_options" in err_text.lower() or "extra_forbidden" in err_text.lower():
                        body.pop("stream_options", None)
                        with httpx.stream("POST", self._url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code == 200:
                                yield from _parse_stream(retry_resp)
                                return
                            else:
                                retry_resp.read()
                                err_text = retry_resp.text

                    if "does not support image" in err_text.lower() or "invalid_image" in err_text.lower():
                        # Fallback: Strip image_url blocks and retry with text placeholder
                        for m in openai_messages:
                            if isinstance(m.get("content"), list):
                                text_only = []
                                for b in m["content"]:
                                    if isinstance(b, dict) and b.get("type") == "text":
                                        text_only.append(b.get("text", ""))
                                    elif isinstance(b, dict) and b.get("type") == "image_url":
                                        text_only.append("[Attached User Screenshot / Image]")
                                m["content"] = "\n".join(text_only)
                        body["messages"] = openai_messages
                        with httpx.stream("POST", self._url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code != 200:
                                retry_resp.read()
                                raise ProviderError(f"HTTP {retry_resp.status_code}: {retry_resp.text}", status=retry_resp.status_code, body=retry_resp.text)
                            yield from _parse_stream(retry_resp)
                    else:
                        raise ProviderError(f"HTTP {resp.status_code}: {err_text}", status=resp.status_code, body=err_text)
                else:
                    yield from _parse_stream(resp)

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
            if "<!doctype html>" in err_str.lower() or "not found | openrouter" in err_str.lower() or "http 404" in err_str.lower():
                raise ProviderError(
                    f"HTTP 404 Not Found from endpoint '{self._url}'.\n"
                    f"  💡 The model '{self.settings.model}' or path is not available on this provider.\n"
                    f"  Fix: Run `/model` to select an active model or `/provider` to re-configure the provider endpoint."
                ) from e
            if "connection refused" in err_str.lower() or "[errno 61]" in err_str.lower() or "[errno 111]" in err_str.lower() or "winerror 10061" in err_str.lower():
                base_u = self.settings.base_url
                if "11434" in base_u or "ollama" in base_u.lower():
                    raise ProviderError(
                        f"Cannot connect to Ollama at {base_u} (Connection refused).\n"
                        f"  💡 How to fix:\n"
                        f"     1. Start Ollama by running: `ollama serve` (or open the Ollama application)\n"
                        f"     2. Or switch back to cloud AI anytime by typing: `/provider` and choosing AgentRouter."
                    ) from e
                elif "1234" in base_u or "lmstudio" in base_u.lower():
                    raise ProviderError(
                        f"Cannot connect to LM Studio at {base_u} (Connection refused).\n"
                        f"  💡 How to fix:\n"
                        f"     1. Open LM Studio -> 'Local Server' tab -> 'Start Server' (port 1234)\n"
                        f"     2. Or switch back to cloud AI anytime by typing: `/provider`."
                    ) from e
                else:
                    raise ProviderError(
                        f"Cannot connect to local AI endpoint at {base_u} (Connection refused).\n"
                        f"  💡 Ensure your local server is running, or run `/provider` to switch to cloud."
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
