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
            self._responses_url = base.replace("/chat/completions", "/responses")
        elif "googleapis.com" in base:
            clean = base.rstrip("/")
            if not clean.endswith("/openai"):
                clean = f"{clean}/openai"
            self._url = f"{clean}/chat/completions"
            self._responses_url = f"{clean}/responses"
        elif "openrouter.ai" in base:
            clean = base.rstrip("/")
            if not clean.endswith("/api/v1"):
                clean = f"{clean.rstrip('/api').rstrip('/v1')}/api/v1"
            self._url = f"{clean}/chat/completions"
            self._responses_url = f"{clean}/responses"
        elif base.endswith("/v1"):
            self._url = f"{base}/chat/completions"
            self._responses_url = f"{base}/responses"
        else:
            self._url = f"{base}/v1/chat/completions"
            self._responses_url = f"{base}/v1/responses"
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

        # Detect if model or settings requires Response protocol (OpenAI /v1/responses)
        # As instructed for gpt-6-astra: use the Response protocol to prevent:
        # "Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions"
        is_response_protocol = (
            getattr(self.settings, "wire_api", None) == "responses"
            or getattr(self.settings, "api_format", None) == "responses"
            or ("gpt-6" in model.lower() and getattr(self.settings, "wire_api", None) != "chat")
        )
        target_url = self._responses_url if is_response_protocol else self._url

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
        }

        if is_response_protocol:
            body["input"] = openai_messages
            body["messages"] = openai_messages  # compatibility with proxy forwarders
            if effort:
                e_str = str(effort).lower()
                if e_str in ("reflex", "low"):
                    body["reasoning"] = {"effort": "low"}
                elif e_str in ("balanced", "medium"):
                    body["reasoning"] = {"effort": "medium"}
                elif e_str in ("synapse", "quantum", "high", "xhigh", "max", "hyper"):
                    body["reasoning"] = {"effort": "high"}
                else:
                    body["reasoning"] = {"effort": e_str}
            if tools:
                body["tools"] = tools
        else:
            body["messages"] = openai_messages
            body["stream_options"] = {"include_usage": True}
            is_reasoning_model = any(
                k in model.lower()
                for k in ("o1", "o3", "o4", "gpt-6", "astra", "deepseek-reasoner", "deepseek-r1", "r1:", "r1-", "reasoning", "qwq")
            )
            if effort and is_reasoning_model:
                # In /v1/chat/completions, GPT-6 with tools rejects reasoning_effort
                if not (tools and "gpt-6" in model.lower()):
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
            current_event = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                    continue
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except Exception:
                    continue

                event_type = current_event or chunk.get("type", "")

                # 1. Parse Usage (both Chat Completions and Responses API formats)
                u = chunk.get("usage") or chunk.get("response", {}).get("usage")
                if u and isinstance(u, dict):
                    prompt_t = u.get("prompt_tokens", 0) or u.get("input_tokens", 0) or 0
                    cache_hit_t = (
                        u.get("prompt_cache_hit_tokens", 0)
                        or u.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                        or u.get("input_token_details", {}).get("cached_tokens", 0)
                        or u.get("cache_read_tokens", 0)
                        or 0
                    )
                    out_t = u.get("completion_tokens", 0) or u.get("output_tokens", 0) or 0
                    reasoning_t = (
                        u.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
                        or u.get("output_token_details", {}).get("reasoning_tokens", 0)
                        or 0
                    )
                    if prompt_t >= cache_hit_t:
                        total_input = prompt_t
                    else:
                        total_input = prompt_t + cache_hit_t
                    usage = Usage(
                        input=total_input,
                        output=out_t,
                        cache_read=cache_hit_t,
                        reasoning=reasoning_t,
                    )

                # 2. Parse Response Protocol Events
                if event_type in ("response.text.delta", "response.output_text.delta"):
                    delta_text = chunk.get("delta") or chunk.get("text", "")
                    if delta_text:
                        full_text += delta_text
                        yield TextDelta(text=delta_text)
                    continue

                if event_type in ("response.reasoning.delta", "response.thought.delta"):
                    reasoning_delta = chunk.get("delta") or chunk.get("text", "")
                    if reasoning_delta:
                        full_reasoning += reasoning_delta
                        if thinking:
                            yield ThinkingDelta(text=reasoning_delta)
                    continue

                if event_type == "response.output_item.added":
                    item = chunk.get("item", {})
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id") or item.get("id", "")
                        idx = chunk.get("output_index", len(tool_calls_acc))
                        tool_calls_acc[idx] = {
                            "id": call_id,
                            "name": item.get("name", ""),
                            "arguments": "",
                        }
                    continue

                if event_type == "response.function_call_arguments.delta":
                    call_id = chunk.get("call_id") or chunk.get("item_id", "")
                    frag = chunk.get("delta", "")
                    matched_idx = None
                    for idx, tc in tool_calls_acc.items():
                        if tc.get("id") == call_id:
                            matched_idx = idx
                            break
                    if matched_idx is None:
                        matched_idx = len(tool_calls_acc)
                        tool_calls_acc[matched_idx] = {"id": call_id, "name": "", "arguments": ""}
                    tool_calls_acc[matched_idx]["arguments"] += frag
                    yield ToolArgsDelta(id=tool_calls_acc[matched_idx]["id"], fragment=frag)
                    continue

                if event_type == "response.output_item.done":
                    item = chunk.get("item", {})
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id") or item.get("id", "")
                        name = item.get("name", "")
                        args = item.get("arguments", "")
                        for idx, tc in tool_calls_acc.items():
                            if tc.get("id") == call_id:
                                if name:
                                    tc["name"] = name
                                if args:
                                    tc["arguments"] = args
                                break
                    continue

                if event_type in ("response.done", "response.completed"):
                    if tool_calls_acc:
                        stop_reason = "tool_use"
                    else:
                        resp_obj = chunk.get("response", {})
                        st = resp_obj.get("status", "")
                        stop_reason = "max_tokens" if st == "incomplete" else "end_turn"
                    continue

                # 3. Parse Standard Choices Format (Chat Completions)
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
            with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as resp:
                if resp.status_code != 200:
                    resp.read()
                    err_text = resp.text
                    retry_success = False

                    # Check if tools with reasoning_effort error triggered:
                    # "Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions"
                    if "function tools with reasoning_effort are not supported" in err_text.lower() or ("reasoning_effort" in err_text.lower() and "tool" in err_text.lower()):
                        # Switch to /v1/responses or strip reasoning_effort
                        if target_url == self._url and self._responses_url != self._url:
                            target_url = self._responses_url
                            body.pop("reasoning_effort", None)
                            if effort:
                                body["reasoning"] = {"effort": str(effort).lower()}
                            body["input"] = openai_messages
                            body.pop("stream_options", None)
                            with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                                if retry_resp.status_code == 200:
                                    yield from _parse_stream(retry_resp)
                                    retry_success = True
                                else:
                                    retry_resp.read()
                                    err_text = retry_resp.text
                        else:
                            body.pop("reasoning_effort", None)
                            body.pop("reasoning", None)
                            with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                                if retry_resp.status_code == 200:
                                    yield from _parse_stream(retry_resp)
                                    retry_success = True
                                else:
                                    retry_resp.read()
                                    err_text = retry_resp.text

                    # If /v1/responses returned 404 (endpoint not supported by gateway/mock), fallback to /v1/chat/completions
                    if not retry_success and resp.status_code == 404 and target_url == self._responses_url:
                        target_url = self._url
                        body.pop("input", None)
                        body.pop("reasoning", None)
                        body["messages"] = openai_messages
                        body["stream_options"] = {"include_usage": True}
                        if tools:
                            body.pop("reasoning_effort", None)
                        with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code == 200:
                                yield from _parse_stream(retry_resp)
                                retry_success = True
                            else:
                                retry_resp.read()
                                err_text = retry_resp.text

                    # Check if thinking/reasoning_effort is rejected
                    if not retry_success and ("does not support thinking" in err_text.lower() or "reasoning_effort" in err_text.lower() or "does not support reasoning" in err_text.lower() or "thinking" in err_text.lower()):
                        body.pop("reasoning_effort", None)
                        body.pop("reasoning", None)
                        with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code == 200:
                                yield from _parse_stream(retry_resp)
                                retry_success = True
                            else:
                                retry_resp.read()
                                err_text = retry_resp.text

                    # Check if stream_options is rejected by legacy local server
                    if not retry_success and ("stream_options" in err_text.lower() or "extra_forbidden" in err_text.lower()):
                        body.pop("stream_options", None)
                        with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
                            if retry_resp.status_code == 200:
                                yield from _parse_stream(retry_resp)
                                retry_success = True
                            else:
                                retry_resp.read()
                                err_text = retry_resp.text

                    if not retry_success:
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
                            if "input" in body:
                                body["input"] = openai_messages
                            with httpx.stream("POST", target_url, headers=self._headers(), json=body, timeout=120) as retry_resp:
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
            if "sensitive_words_detected" in err_str or "sensitive words" in err_str.lower() or "sensitive_word" in err_str.lower():
                raise ProviderError(
                    "Content filter triggered (sensitive_words_detected).\n"
                    "  This is AgentRouter's sensitive word detection to prevent abuse.\n"
                    "  💡 How to fix: Run `/clear` to reset context, or start a new session with `/sessions`.\n"
                    "  Run `/faq sensitive` for more details."
                ) from e
            if "402" in err_str or "budget pool quota has been exhausted" in err_str.lower() or "budget pool" in err_str.lower():
                raise ProviderError(
                    "HTTP 402 Budget Pool Quota Exhausted.\n"
                    "  Claude and GPT models on AgentRouter are released in daily batches on a first-come, first-served basis:\n"
                    "    • 00:00 Beijing time (16:00 UTC)\n"
                    "    • 08:00 Beijing time (00:00 UTC)\n"
                    "    • 16:00 Beijing time (08:00 UTC)\n"
                    "  💡 How to fix:\n"
                    "     1. Switch to DeepSeek with `/model deepseek-v4-flash` for uninterrupted use with unlimited quota.\n"
                    "     2. Or wait for the next batch release time slot.\n"
                    "     3. Run `/faq 402` for more details."
                ) from e
            if "content blocked" in err_str.lower() or "400 content blocked" in err_str.lower() or "unsupported language" in err_str.lower():
                raise ProviderError(
                    "HTTP 400 Content Blocked: Language restriction triggered.\n"
                    "  AgentRouter currently only supports Chinese, English, French, German, and Russian.\n"
                    "  💡 How to fix: Modify or translate your request into a supported language and retry.\n"
                    "  Run `/faq 400` for more details."
                ) from e
            if "401" in err_str or "unauthorized" in err_str.lower() or "unauthenticated" in err_str.lower():
                raise ProviderError(
                    "HTTP 401 Unauthorized: API key missing, expired, or client unauthenticated.\n"
                    "  💡 How to fix:\n"
                    "     1. Verify your API token at https://agentrouter.org/console or https://ps.air-outer.com\n"
                    "     2. Run `/keys` or `/provider` to update your credentials.\n"
                    "     3. Supported client documentation: https://ps.air-outer.com/docs/claude-code.html\n"
                    "     4. Run `/faq 401` for more details."
                ) from e
            if "<!doctype html>" in err_str.lower() or "not found | openrouter" in err_str.lower() or "http 404" in err_str.lower():
                raise ProviderError(
                    f"HTTP 404 Not Found from endpoint '{self._url}'.\n"
                    f"  💡 The model '{self.settings.model}' or path is not available on this provider.\n"
                    f"  Fix: Run `/model` to select an active model or `/provider` to re-configure the provider endpoint."
                ) from e
            if "agentrouter.org" in str(self.settings.base_url) and ("connection refused" in err_str.lower() or "[errno 61]" in err_str.lower() or "timeout" in err_str.lower() or "getaddrinfo" in err_str.lower() or "nameresolution" in err_str.lower()):
                raise ProviderError(
                    f"Cannot connect to AgentRouter at {self.settings.base_url}.\n"
                    f"  💡 Backup Domain Available:\n"
                    f"     AgentRouter provides an official alternative domain: https://ps.air-outer.com\n"
                    f"     Switch anytime by running `/provider` or setting base_url to https://ps.air-outer.com\n"
                    f"     Run `/faq domain` for details."
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
