"""
OpenAI-compatible provider (OpenAI, Azure OpenAI, Gemini, OpenRouter, xAI, Groq, Mistral, DeepSeek,
Together, Fireworks, Cerebras, Ollama, LM Studio, AgentRouter) over raw httpx streaming.

Speaks both Chat Completions (`/chat/completions`) and the Responses API (`/responses`, used for
gpt-6 class models or when `settings.wire_api == "responses"`).
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Iterator

try:
    import httpx
except ImportError:  # pragma: no cover - anthropic>=1 ships httpx2
    import httpx2 as httpx  # type: ignore[no-redef]

from axon.errors import ProviderError
from axon.providers.base import (
    StreamCanceller,
    retry_event,
    AssistantTurn,
    Block,
    StopReason,
    StreamEvent,
    StreamState,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolArgsDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
    Usage,
    parse_tool_arguments,
)
from axon.providers.transport import (
    azure_chat_url,
    backoff_delay,
    chat_completions_url,
    classify_error,
    connection_error,
    default_headers,
    extract_error_detail,
    is_agentrouter,
    is_azure,
    is_official_openai,
    is_retryable_exception,
    max_retries_for,
    openai_reasoning_effort,
    parse_retry_after,
    request_timeout_for,
    responses_url,
    retry_budget,
    should_retry_status,
)
from axon.providers import transport as _transport

# Message fields that strict OpenAI-compatible servers reject (Groq, Mistral, OpenAI, ...).
_NON_STANDARD_MESSAGE_KEYS = ("reasoning_content", "reasoning", "reasoning_details")

# Thinking-mode APIs that require `reasoning_content` on every assistant message carrying tool_calls
# (DeepSeek: "The `reasoning_content` in the thinking mode must be passed back to the API"). The model
# sometimes emits a tool call with no reasoning at all; an empty string counts as missing, so such
# turns are replayed with this placeholder instead.
REASONING_PLACEHOLDER = "(no reasoning)"
_PASSBACK_MODEL_PREFIXES = ("deepseek-v4", "deepseek-v3.2", "deepseek-reasoner")


def needs_reasoning_passback(model: str, messages: list[dict[str, Any]]) -> bool:
    """True when tool-call turns must carry reasoning_content: a known thinking-mode model family, or a
    conversation that already round-trips reasoning_content (the endpoint returned it, we send it back)."""
    if _bare(model).startswith(_PASSBACK_MODEL_PREFIXES):
        return True
    return any(isinstance(m, dict) and m.get("role") == "assistant" and m.get("reasoning_content") for m in messages)


def _with_reasoning(m: dict[str, Any]) -> dict[str, Any]:
    """*m* with a non-empty reasoning_content when it is an assistant tool_calls message (copy if changed)."""
    if m.get("role") != "assistant" or not m.get("tool_calls"):
        return m
    rc = m.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return m
    return {**m, "reasoning_content": REASONING_PLACEHOLDER}


def _copy_content(c: Any) -> Any:
    return [dict(b) if isinstance(b, dict) else b for b in c] if isinstance(c, list) else c


def _strip_cache_control(content: Any) -> Any:
    if isinstance(content, list):
        return [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b for b in content]
    return content


def sanitize_openai_messages(messages: list[dict[str, Any]], *, strict: bool = False,
                             reasoning_passback: bool = False) -> list[dict[str, Any]]:
    """
    Normalize a mixed Anthropic/OpenAI conversation into valid OpenAI chat messages:
    1. Unpack accidental nested message dicts; content is never None.
    2. Assistant block lists -> text + tool_calls (thinking blocks dropped).
    3. User tool_result blocks -> role="tool" messages; image blocks -> image_url parts.
    4. Every assistant tool_calls message is followed by one tool message per id (synthesized if missing).
    5. Orphaned tool messages and empty assistant turns are dropped; consecutive user turns are merged.
    6. strict=True strips non-standard fields (reasoning_content, cache_control) for picky servers.
    7. reasoning_passback=True (ignored when strict) gives every assistant tool_calls message a
       non-empty reasoning_content, as thinking-mode APIs (DeepSeek) reject the request otherwise.
    Input messages are never mutated.
    """
    if not messages:
        return []

    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        m = dict(msg)
        if isinstance(m.get("content"), dict) and "role" in m["content"]:
            m = dict(m["content"])
        elif isinstance(m.get("content"), dict):
            m["content"] = json.dumps(m["content"])
        if m.get("content") is None:
            m["content"] = ""
        m["content"] = _copy_content(m["content"])
        role = m.get("role")

        if role == "assistant" and isinstance(m.get("content"), list):
            t_calls: list[dict[str, Any]] = list(m.get("tool_calls") or [])
            t_texts: list[str] = []
            for b in m["content"]:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    t_calls.append({
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {"name": b.get("name", ""), "arguments": inp if isinstance(inp, str) else json.dumps(inp)},
                    })
                elif b.get("type") == "text":
                    t_texts.append(b.get("text", ""))
            m["content"] = "\n".join(t for t in t_texts if t)
            if t_calls:
                m["tool_calls"] = t_calls

        if role == "user" and isinstance(m.get("content"), list):
            has_tool_res = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
            if has_tool_res:
                user_texts: list[str] = []
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        raw_c = b.get("content", "")
                        if isinstance(raw_c, list):
                            parts = [x.get("text", "") for x in raw_c if isinstance(x, dict) and x.get("type") == "text"]
                            c_str = "\n".join(parts) if parts else json.dumps(raw_c)
                        else:
                            c_str = raw_c if isinstance(raw_c, str) else json.dumps(raw_c)
                        if b.get("is_error") and not c_str.startswith("Error"):
                            c_str = f"Error: {c_str}"
                        cleaned.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": c_str})
                    elif isinstance(b, dict) and b.get("type") == "text":
                        user_texts.append(b.get("text", ""))
                if user_texts:
                    cleaned.append({"role": "user", "content": "\n".join(user_texts)})
                continue
            converted: list[dict[str, Any]] = []
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "image":
                    src = b.get("source", {}) or {}
                    if src.get("type") == "url" and src.get("url"):
                        url = src["url"]
                    else:
                        url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                    converted.append({"type": "image_url", "image_url": {"url": url}})
                elif isinstance(b, dict):
                    converted.append(dict(b))
            m["content"] = converted

        if strict:
            for k in _NON_STANDARD_MESSAGE_KEYS:
                m.pop(k, None)
            m["content"] = _strip_cache_control(m.get("content"))
        elif reasoning_passback:
            m = _with_reasoning(m)
        cleaned.append(m)

    sanitized: list[dict[str, Any]] = []
    i = 0
    while i < len(cleaned):
        m = cleaned[i]
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            expected_ids = [tc["id"] for tc in m["tool_calls"] if isinstance(tc, dict) and tc.get("id")]
            sanitized.append(m)
            i += 1
            found_ids: set[str] = set()
            while i < len(cleaned) and cleaned[i].get("role") == "tool":
                tm = cleaned[i]
                tid = tm.get("tool_call_id")
                if tid in expected_ids and tid not in found_ids:
                    sanitized.append(tm)
                    found_ids.add(tid)
                i += 1
            for tid in expected_ids:
                if tid not in found_ids:
                    sanitized.append({"role": "tool", "tool_call_id": tid, "content": "[Tool execution completed or interrupted]"})
        elif role == "tool":
            i += 1  # orphaned tool message
        elif role == "assistant" and not m.get("content") and not m.get("tool_calls"):
            i += 1  # empty assistant turn -> upstream invalid_request_error
        elif role == "user" and sanitized and sanitized[-1].get("role") == "user":
            prev = sanitized[-1]
            prev_c, curr_c = prev.get("content", ""), m.get("content", "")
            if isinstance(prev_c, str) and isinstance(curr_c, str):
                prev["content"] = f"{prev_c}\n\n{curr_c}".strip()
            else:
                as_list = lambda c: c if isinstance(c, list) else ([{"type": "text", "text": c}] if c else [])  # noqa: E731
                prev["content"] = list(as_list(prev_c)) + list(as_list(curr_c))
            i += 1
        else:
            sanitized.append(m)
            i += 1
    return sanitized


def normalize_tools_for_chat(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept OpenAI function tools or Anthropic {name, input_schema} tools; return OpenAI format."""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = dict(t["function"])
        elif "name" in t:
            fn = {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema") or t.get("parameters") or {}}
        else:
            continue
        params = dict(fn.get("parameters") or {})
        params.setdefault("type", "object")
        if params.get("properties") is None:
            params["properties"] = {}
        fn["parameters"] = params
        out.append({"type": "function", "function": fn})
    return out


def chat_tools_to_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in normalize_tools_for_chat(tools):
        fn = t["function"]
        out.append({"type": "function", "name": fn.get("name", ""), "description": fn.get("description", ""), "parameters": fn.get("parameters", {})})
    return out


def chat_messages_to_responses_input(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert sanitized chat messages into (instructions, Responses API input items)."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role in ("system", "developer"):
            instructions.append(content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict)))
        elif role == "user":
            if isinstance(content, str):
                items.append({"role": "user", "content": content})
            else:
                parts: list[dict[str, Any]] = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append({"type": "input_text", "text": b.get("text", "")})
                    elif b.get("type") == "image_url":
                        iu = b.get("image_url")
                        parts.append({"type": "input_image", "image_url": iu.get("url", "") if isinstance(iu, dict) else str(iu)})
                items.append({"role": "user", "content": parts})
        elif role == "assistant":
            text = content if isinstance(content, str) else ""
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                items.append({"type": "function_call", "call_id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": fn.get("arguments", "") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""), "output": content if isinstance(content, str) else json.dumps(content)})
    return "\n\n".join(i for i in instructions if i), items


_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6", "gemini-2.5", "gemini-3", "grok-3-mini", "gpt-oss", "openai/gpt-oss")


def _bare(model: str) -> str:
    return model.lower().rsplit("/", 1)[-1]


def is_reasoning_model(model: str) -> bool:
    m = _bare(model)
    return m.startswith(_REASONING_PREFIXES) or "astra" in m


class _ToolAcc:
    __slots__ = ("id", "item_id", "name", "arguments", "started")

    def __init__(self, id: str = "", name: str = "", item_id: str = "") -> None:
        self.id = id
        self.item_id = item_id
        self.name = name
        self.arguments = ""
        self.started = False


class _Outcome:
    """Result of one HTTP attempt (kept outside `with` so mocked context managers can't swallow it)."""
    __slots__ = ("status", "text", "headers", "events_yielded", "stream_error", "transport_exc")

    def __init__(self) -> None:
        self.status: int | None = None
        self.text = ""
        self.headers: Any = None
        self.events_yielded = False
        self.stream_error: Any = None
        self.transport_exc: BaseException | None = None


class OpenAICompatProvider:
    name: str = "openai_compat"

    def __init__(self, settings: Any, *, model_prefix: str = "", preset: Any = None, http_client: Any = None,
                 aliases: dict[str, str] | None = None) -> None:
        self.settings = settings
        self._model_prefix = model_prefix or ""
        self._aliases = dict(aliases or {})
        if preset is None:
            try:
                from axon.providers.catalog import find_preset_by_url
                preset = find_preset_by_url(settings.base_url)
            except Exception:
                preset = None
        self._preset = preset
        self._client = http_client
        base = str(settings.base_url)
        self._azure = is_azure(base) or getattr(preset, "id", None) == "azure"
        self._url = chat_completions_url(base)
        self._responses_url = responses_url(base)
        self._local = threading.local()
        # Request features this endpoint rejected (per model), learned so we stop re-sending them.
        self._rejected_features: dict[str, set[str]] = {}
        self._rejected_lock = threading.Lock()
        self._canceller = StreamCanceller()

    def cancel(self, thread_ident: int | None = None) -> bool:
        """Abort the in-flight stream (of *thread_ident*, default all) from another thread."""
        return self._canceller.cancel(thread_ident)

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def _last_turn(self) -> AssistantTurn | None:  # backwards-compatible accessor
        st = getattr(self._local, "state", None)
        return st.turn if st else None

    def _wire_model(self, model: str) -> str:
        """Model id as sent on the wire: alias resolved, "[1m]" suffix and route prefix stripped."""
        m = self._aliases.get(model, model)
        if m.lower().endswith("[1m]"):
            m = m[:-4]
        if self._model_prefix and m.startswith(self._model_prefix):
            m = m[len(self._model_prefix):]
        return m

    def endpoint_label(self) -> str:
        """Human label for messages: preset name, else the endpoint host ("api.example.com")."""
        name = getattr(self._preset, "name", None)
        if name:
            return str(name)
        try:
            from urllib.parse import urlparse
            host = urlparse(str(self.settings.base_url)).netloc
        except Exception:
            host = ""
        return host or self.name

    def _api_key(self) -> str:
        key = getattr(self.settings, "api_key", None)
        val = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
        return "" if (val or "").strip().lower() in ("", "local", "none", "null") else val.strip()

    def _headers(self) -> dict[str, str]:
        headers = default_headers(str(self.settings.base_url))
        key = self._api_key()
        style = getattr(self._preset, "auth_style", "bearer") if self._preset else "bearer"
        if self._azure:
            style = "api-key"
        if key:
            if style == "api-key":
                headers["api-key"] = key
            else:
                headers["authorization"] = f"Bearer {key}"
        if self._preset is not None and self._preset.id == "openrouter":
            headers.setdefault("http-referer", "https://github.com/axon-terminal")
            headers.setdefault("x-title", "Axon")
        return headers

    def _strict(self) -> bool:
        return bool(getattr(self._preset, "strict_messages", False)) or self._azure

    def _rejected(self, model: str) -> set[str]:
        with self._rejected_lock:
            return set(self._rejected_features.get(model, set()))

    def _learn(self, model: str, feature: str) -> None:
        with self._rejected_lock:
            self._rejected_features.setdefault(model, set()).add(feature)

    def _uses_max_completion_tokens(self) -> bool:
        return self._azure or is_official_openai(self.settings.base_url)

    def _chat_url_for(self, wire_model: str) -> str:
        if self._azure:
            import os
            ver = getattr(self.settings, "azure_api_version", None) or os.environ.get("AZURE_OPENAI_API_VERSION") or getattr(self._preset, "api_version", None)
            deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or wire_model
            return azure_chat_url(str(self.settings.base_url), deployment, ver)
        return self._url

    def _open(self, url: str, body: dict[str, Any]) -> Any:
        timeout = request_timeout_for(self.settings)
        t = httpx.Timeout(timeout, connect=min(10.0, timeout)) if hasattr(httpx, "Timeout") else timeout
        if self._client is not None:
            return self._client.stream("POST", url, headers=self._headers(), json=body, timeout=t)
        return httpx.stream("POST", url, headers=self._headers(), json=body, timeout=t)

    # ── request bodies ───────────────────────────────────────────────────
    def _clamp_tokens(self, model: str, max_tokens: int) -> int:
        try:
            from axon.providers.registry import known_max_output
            cap = known_max_output(model)
        except Exception:
            cap = None
        return max(1, min(int(max_tokens), cap)) if cap else max(1, int(max_tokens))

    def _chat_body(self, model: str, wire_model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int, effort: str | None) -> dict[str, Any]:
        rejected = self._rejected(model)
        body: dict[str, Any] = {"model": wire_model, "messages": messages, "stream": True}
        if "stream_options" not in rejected and getattr(self._preset, "id", None) != "mistral":
            body["stream_options"] = {"include_usage": True}
        limit = self._clamp_tokens(model, max_tokens)
        use_mct = (self._uses_max_completion_tokens() or "use_max_completion_tokens" in rejected) and "use_max_tokens" not in rejected
        body["max_completion_tokens" if use_mct else "max_tokens"] = limit
        if tools:
            body["tools"] = normalize_tools_for_chat(tools)
        reasoning = openai_reasoning_effort(effort)
        if reasoning and is_reasoning_model(model) and "reasoning" not in rejected:
            bare = _bare(model)
            ar_restricted = is_agentrouter(self.settings.base_url) and bare.startswith(("gpt-5", "gpt-6")) or "gpt-6" in bare or "astra" in bare
            if not (tools and ar_restricted):
                if bare.startswith("grok-3-mini") and reasoning == "medium":
                    reasoning = "low"
                body["reasoning_effort"] = reasoning
        return body

    def _responses_body(self, wire_model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int, effort: str | None, model: str) -> dict[str, Any]:
        instructions, items = chat_messages_to_responses_input(messages)
        body: dict[str, Any] = {
            "model": wire_model,
            "input": items,
            "stream": True,
            "store": False,
            "max_output_tokens": self._clamp_tokens(model, max_tokens),
        }
        if instructions:
            body["instructions"] = instructions
        reasoning = openai_reasoning_effort(effort)
        if reasoning and "reasoning" not in self._rejected(model):
            body["reasoning"] = {"effort": reasoning}
        if tools:
            body["tools"] = chat_tools_to_responses(tools)
        return body

    def _wants_responses(self, model: str) -> bool:
        if self._azure:
            return False
        wire = getattr(self.settings, "wire_api", None) or getattr(self.settings, "api_format", None)
        if wire == "responses":
            return True
        if wire == "chat":
            return False
        if "use_chat" in self._rejected(model):
            return False
        return "gpt-6" in _bare(model)

    # ── streaming ────────────────────────────────────────────────────────
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
        state = StreamState()
        self._local.state = state
        self._canceller.begin()
        wire_model = self._wire_model(model)
        model = wire_model  # capability checks use the concrete id

        system_text = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("text"))
        raw_messages: list[dict[str, Any]] = []
        if system_text:
            raw_messages.append({"role": "system", "content": system_text})
        raw_messages.extend(messages)
        strict = self._strict()
        passback = not strict and ("reasoning_passback" in self._rejected(model) or needs_reasoning_passback(model, messages))
        chat_messages = sanitize_openai_messages(raw_messages, strict=strict, reasoning_passback=passback)

        use_responses = self._wants_responses(model)
        tried: set[str] = set()
        images_stripped = False

        try:
            while True:
                if use_responses:
                    url = self._responses_url
                    body = self._responses_body(wire_model, chat_messages, tools, max_tokens, effort, model)
                else:
                    url = self._chat_url_for(wire_model)
                    body = self._chat_body(model, wire_model, chat_messages, tools, max_tokens, effort)
                for feature in tried:
                    if feature == "no_reasoning":
                        body.pop("reasoning", None)
                        body.pop("reasoning_effort", None)
                    elif feature == "no_stream_options":
                        body.pop("stream_options", None)

                acc: dict[int, _ToolAcc] = {}
                stop: list[StopReason] = ["end_turn"]
                outcome = _Outcome()
                yield from self._attempt_with_retries(url, body, outcome, state, acc, stop, thinking, responses=use_responses)

                if outcome.status is not None and 200 <= outcome.status < 300 and outcome.stream_error is None and outcome.transport_exc is None:
                    break
                if outcome.events_yielded or outcome.stream_error is not None:
                    detail = outcome.stream_error if outcome.stream_error is not None else outcome.transport_exc
                    if isinstance(detail, BaseException):
                        raise connection_error(detail, str(self.settings.base_url))
                    raise classify_error(status=_status_of_stream_error(detail), body=detail, base_url=str(self.settings.base_url), model=model, url=url)
                if outcome.transport_exc is not None:
                    raise outcome.transport_exc

                err = (outcome.text or "").lower()
                status = outcome.status or 0
                reasoning_sent = "reasoning" in body or "reasoning_effort" in body
                # Recovery ladder: each adjustment is tried once, then remembered for this host/model.
                if not passback and not strict and "reasoning_content" in err and ("passed back" in err or "missing" in err):
                    passback = True  # thinking-mode API wants reasoning_content on tool-call turns
                    self._learn(model, "reasoning_passback")
                    chat_messages = [_with_reasoning(m) for m in chat_messages]
                    continue
                if reasoning_sent and "no_reasoning" not in tried and (
                    "reasoning_effort" in err or "reasoning" in err and "not supported" in err
                    or "does not support thinking" in err or "does not support reasoning" in err
                    or ("thinking" in err and status == 400)
                ):
                    if not use_responses and "reasoning_effort" in err and "tool" in err and self._responses_url != url and not self._azure:
                        use_responses = True
                        continue
                    tried.add("no_reasoning")
                    self._learn(model, "reasoning")
                    continue
                if status == 404 and use_responses and "use_chat" not in tried:
                    tried.add("use_chat")
                    self._learn(model, "use_chat")
                    use_responses = False
                    continue
                if not use_responses and "stream_options" in body and ("stream_options" in err or "extra_forbidden" in err or "extra inputs" in err) and "no_stream_options" not in tried:
                    tried.add("no_stream_options")
                    self._learn(model, "stream_options")
                    continue
                if not use_responses and "max_tokens" in body and "max_completion_tokens" in err and "mct" not in tried:
                    tried.add("mct")
                    self._learn(model, "use_max_completion_tokens")
                    continue
                if not use_responses and "max_completion_tokens" in body and "max_completion_tokens" in err and "unsupported" in err and "mt" not in tried:
                    tried.add("mt")
                    self._learn(model, "use_max_tokens")
                    continue
                if "cache_control" in err and "no_cache_control" not in tried:
                    tried.add("no_cache_control")
                    chat_messages = [{**m, "content": _strip_cache_control(m.get("content"))} for m in chat_messages]
                    continue
                if not images_stripped and ("does not support image" in err or "invalid_image" in err or "image input" in err and "not" in err):
                    images_stripped = True
                    chat_messages = _strip_images(chat_messages)
                    continue
                raise classify_error(status=outcome.status, body=outcome.text, base_url=str(self.settings.base_url), model=model, url=url)

            turn = self._assemble(state, acc, stop[0])
            for tu in turn.tool_uses:
                yield ToolUseComplete(id=tu.id)
            turn.model = wire_model
            state.turn = turn
            yield TurnComplete(stop_reason=turn.stop_reason, usage=turn.usage)
        except ProviderError:
            raise
        except Exception as e:
            if is_retryable_exception(e):
                raise connection_error(e, str(self.settings.base_url)) from e
            raise classify_error(status=getattr(e, "status_code", None), base_url=str(self.settings.base_url), model=model, url=self._url, exc=e) from e

    def _attempt_with_retries(self, url: str, body: dict[str, Any], outcome: _Outcome, state: StreamState,
                              acc: dict[int, _ToolAcc], stop: list[StopReason], thinking: bool, *, responses: bool) -> Iterator[StreamEvent]:
        retries = max_retries_for(self.settings)
        base_url = str(self.settings.base_url)
        attempt = -1
        while attempt < retries:
            attempt += 1
            outcome.status = None
            outcome.transport_exc = None
            delay: float | None = None
            reason = ""
            try:
                self._canceller.check()
                with self._open(url, body) as resp, self._canceller.track(resp):
                    status = int(getattr(resp, "status_code", 0) or 0)
                    outcome.status = status
                    outcome.headers = getattr(resp, "headers", None)
                    if 200 <= status < 300:
                        parser = self._parse_responses if responses else self._parse_chat
                        for ev in parser(resp, state, acc, stop, thinking, outcome):
                            outcome.events_yielded = True
                            yield ev
                        if outcome.stream_error is not None and not outcome.events_yielded:
                            # An error sent in place of the first chunk (OpenRouter / gateways answer 200, then
                            # `{"error": {"code": 502}}`): nothing was shown yet, so it is retried like a 5xx.
                            err_status = _status_of_stream_error(outcome.stream_error)
                            budget = retry_budget(retries, err_status, outcome.stream_error, base_url)
                            if err_status is not None and attempt < budget and should_retry_status(
                                    err_status, outcome.stream_error, attempt, base_url):
                                retries = budget
                                detail = extract_error_detail(outcome.stream_error)
                                outcome.stream_error = None
                                _reset_stream(state, acc, stop)
                                delay = backoff_delay(min(attempt, 4))
                                reason = f"HTTP {err_status} from {self.endpoint_label()}" + (f": {detail[:160]}" if detail else "")
                    else:
                        try:
                            resp.read()
                        except Exception:
                            pass
                        txt = getattr(resp, "text", "")
                        outcome.text = txt if isinstance(txt, str) else str(txt)
                        budget = retry_budget(retries, status, outcome.text, base_url)
                        if attempt < budget and should_retry_status(status, outcome.text, attempt, base_url):
                            retries = budget
                            delay = backoff_delay(min(attempt, 4), parse_retry_after(outcome.headers))
                            detail = extract_error_detail(outcome.text)
                            reason = f"HTTP {status} from {self.endpoint_label()}" + (f": {detail[:160]}" if detail else "")
            except Exception as e:
                self._canceller.check()  # an aborted read surfaces as a transport error: report Esc/Ctrl+C
                if outcome.events_yielded or not is_retryable_exception(e) or attempt >= retries:
                    outcome.transport_exc = e
                    return
                delay = backoff_delay(attempt)
                reason = f"{type(e).__name__}: {e}"
            self._canceller.check()
            if delay is None:
                return
            yield retry_event(attempt + 1, retries, delay, reason)
            _transport._sleep(delay)

    # ── SSE parsing ──────────────────────────────────────────────────────
    @staticmethod
    def _sse(resp: Any) -> Iterator[tuple[str, Any]]:
        event = ""
        for line in resp.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            if not line:
                event = ""
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                return
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue
            yield event, chunk

    @staticmethod
    def _usage_from(u: dict[str, Any]) -> Usage:
        prompt_t = u.get("prompt_tokens") or u.get("input_tokens") or 0
        details_in = u.get("prompt_tokens_details") or u.get("input_tokens_details") or u.get("input_token_details") or {}
        cache_hit = u.get("prompt_cache_hit_tokens") or (details_in.get("cached_tokens") if isinstance(details_in, dict) else 0) or u.get("cache_read_tokens") or 0
        details_out = u.get("completion_tokens_details") or u.get("output_tokens_details") or u.get("output_token_details") or {}
        reasoning = details_out.get("reasoning_tokens", 0) if isinstance(details_out, dict) else 0
        out_t = u.get("completion_tokens") or u.get("output_tokens") or 0
        total_in = prompt_t if prompt_t >= cache_hit else prompt_t + cache_hit
        return Usage(input=int(total_in or 0), output=int(out_t or 0), cache_read=int(cache_hit or 0), reasoning=int(reasoning or 0))

    def _start_tool(self, tc: _ToolAcc) -> Iterator[StreamEvent]:
        if not tc.started and tc.name:
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:24]}"
            tc.started = True
            yield ToolUseStart(id=tc.id, name=tc.name)

    def _parse_chat(self, resp: Any, state: StreamState, acc: dict[int, _ToolAcc], stop: list[StopReason], thinking: bool, outcome: _Outcome) -> Iterator[StreamEvent]:
        for _event, chunk in self._sse(resp):
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                outcome.stream_error = chunk
                return
            u = chunk.get("usage")
            if isinstance(u, dict) and u:
                state.usage = self._usage_from(u)
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            choice = choices[0]
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            if not delta and isinstance(choice.get("message"), dict):
                delta = choice["message"]  # some servers send a final non-delta message

            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if isinstance(reasoning, str) and reasoning:
                state.thinking_parts.append(reasoning)
                if thinking:
                    yield ThinkingDelta(text=reasoning)
            content = delta.get("content") or ""
            if isinstance(content, str) and content:
                state.text_parts.append(content)
                yield TextDelta(text=content)

            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for tc in raw_calls:
                    if not isinstance(tc, dict):
                        continue
                    idx = tc.get("index")
                    tc_id = tc.get("id") or ""
                    if not isinstance(idx, int):
                        match = next((i for i, a in acc.items() if tc_id and a.id == tc_id), None)
                        idx = match if match is not None else (len(acc) if tc_id or not acc else max(acc))
                    entry = acc.get(idx)
                    if entry is None:
                        entry = acc[idx] = _ToolAcc(id=tc_id)
                    elif tc_id and not entry.id:
                        entry.id = tc_id
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name")
                    if isinstance(name, str) and name and not entry.started:
                        # Fragments concatenate ("Wri" + "te"); servers that resend the full name each delta
                        # repeat or extend what we already have.
                        if not entry.name or name.startswith(entry.name):
                            entry.name = name
                        elif not entry.name.endswith(name):
                            entry.name += name
                    frag = fn.get("arguments")
                    if isinstance(frag, dict):
                        frag = json.dumps(frag)
                    if frag:
                        yield from self._start_tool(entry)  # the name is complete once arguments flow
                        entry.arguments += frag
                        if entry.started:
                            yield ToolArgsDelta(id=entry.id, fragment=frag)

            fr = choice.get("finish_reason")
            if fr:
                if fr in ("tool_calls", "function_call"):
                    stop[0] = "tool_use"
                elif fr == "length":
                    stop[0] = "max_tokens"
                elif fr == "content_filter":
                    stop[0] = "refusal"
                else:
                    stop[0] = "end_turn"
        for entry in list(acc.values()):  # calls whose arguments never arrived (e.g. "{}" omitted)
            yield from self._start_tool(entry)

    def _parse_responses(self, resp: Any, state: StreamState, acc: dict[int, _ToolAcc], stop: list[StopReason], thinking: bool, outcome: _Outcome) -> Iterator[StreamEvent]:
        def find(chunk: dict[str, Any]) -> _ToolAcc | None:
            item_id = chunk.get("item_id") or ""
            call_id = chunk.get("call_id") or ""
            for a in acc.values():
                if (item_id and a.item_id == item_id) or (call_id and a.id == call_id) or (item_id and a.id == item_id):
                    return a
            oi = chunk.get("output_index")
            if isinstance(oi, int) and oi in acc:
                return acc[oi]
            return None

        for event, chunk in self._sse(resp):
            if not isinstance(chunk, dict):
                continue
            et = chunk.get("type") or event
            if et == "error" or (et == "" and chunk.get("error")):
                outcome.stream_error = chunk
                return
            resp_obj = chunk.get("response") if isinstance(chunk.get("response"), dict) else None
            u = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else (resp_obj.get("usage") if resp_obj and isinstance(resp_obj.get("usage"), dict) else None)
            if u:
                state.usage = self._usage_from(u)

            if et in ("response.output_text.delta", "response.text.delta"):
                d = chunk.get("delta") or chunk.get("text") or ""
                if isinstance(d, str) and d:
                    state.text_parts.append(d)
                    yield TextDelta(text=d)
            elif et in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta", "response.reasoning.delta", "response.thought.delta"):
                d = chunk.get("delta") or chunk.get("text") or ""
                if isinstance(d, str) and d:
                    state.thinking_parts.append(d)
                    if thinking:
                        yield ThinkingDelta(text=d)
            elif et in ("response.output_item.added", "response.output_item.done"):
                item = chunk.get("item") if isinstance(chunk.get("item"), dict) else {}
                if item.get("type") != "function_call":
                    continue
                call_id = item.get("call_id") or item.get("id") or ""
                entry = None
                for a in acc.values():
                    if (call_id and a.id == call_id) or (item.get("id") and a.item_id == item.get("id")):
                        entry = a
                        break
                if entry is None:
                    oi = chunk.get("output_index")
                    idx = oi if isinstance(oi, int) and oi not in acc else (max(acc) + 1 if acc else 0)
                    entry = acc[idx] = _ToolAcc(id=call_id, name=item.get("name", ""), item_id=item.get("id", ""))
                if item.get("name"):
                    entry.name = item["name"]
                yield from self._start_tool(entry)
                args = item.get("arguments")
                if et == "response.output_item.done" and isinstance(args, str) and args:
                    entry.arguments = args
            elif et == "response.function_call_arguments.delta":
                entry = find(chunk)
                if entry is None:
                    entry = acc[max(acc) + 1 if acc else 0] = _ToolAcc(id=chunk.get("call_id") or "", item_id=chunk.get("item_id") or "")
                frag = chunk.get("delta") or ""
                if isinstance(frag, str) and frag:
                    entry.arguments += frag
                    if entry.started:
                        yield ToolArgsDelta(id=entry.id, fragment=frag)
            elif et == "response.function_call_arguments.done":
                entry = find(chunk)
                if entry is not None and isinstance(chunk.get("arguments"), str):
                    entry.arguments = chunk["arguments"]
            elif et in ("response.completed", "response.done", "response.incomplete"):
                st = resp_obj.get("status", "") if resp_obj else ""
                if et == "response.incomplete" or st == "incomplete":
                    stop[0] = "max_tokens"
                else:
                    stop[0] = "tool_use" if acc else "end_turn"
            elif et == "response.failed":
                outcome.stream_error = (resp_obj or {}).get("error") or chunk
                return

    # ── turn assembly ────────────────────────────────────────────────────
    def _assemble(self, state: StreamState, acc: dict[int, _ToolAcc], stop_reason: StopReason) -> AssistantTurn:
        blocks: list[Block] = []
        if state.thinking:
            blocks.append(ThinkingBlock(text=state.thinking))
        if state.text:
            blocks.append(TextBlock(text=state.text))

        kept: list[tuple[_ToolAcc, ToolUseBlock]] = []
        for idx in sorted(acc):
            tc = acc[idx]
            if not tc.name:
                continue  # unnamed call can't be executed or replayed
            if not tc.id:
                tc.id = f"call_{uuid.uuid4().hex[:24]}"
            parsed, perr = parse_tool_arguments(tc.arguments)
            if perr and stop_reason == "max_tokens":
                continue  # truncated mid-arguments: drop rather than leave a dangling call
            kept.append((tc, ToolUseBlock(id=tc.id, name=tc.name, input=parsed, parse_error=perr)))

        if kept and stop_reason in ("end_turn", "max_tokens", "stop_sequence"):
            stop_reason = "tool_use"  # e.g. finish_reason "stop" with tool calls (Gemini, Ollama)
        if not kept and stop_reason == "tool_use":
            stop_reason = "end_turn"
        blocks.extend(b for _, b in kept)

        native: dict[str, Any] = {"role": "assistant", "content": state.text or ""}
        if state.thinking:
            native["reasoning_content"] = state.thinking
        if kept:
            native["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments or "{}"}}
                for tc, _ in kept
            ]
        return AssistantTurn(blocks=blocks, stop_reason=stop_reason, usage=state.usage, native=native)

    def finalize(self) -> AssistantTurn:
        state: StreamState | None = getattr(self._local, "state", None)
        self._local.state = None
        if state is None:
            return AssistantTurn(blocks=[], stop_reason="end_turn", usage=Usage())
        if state.turn is not None:
            return state.turn
        return state.partial_turn("openai")

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        # OpenAI requires one role="tool" message per result.
        out = []
        for r in results:
            content = r.content
            if r.is_error and not content.startswith("Error"):
                content = f"Error: {content}"
            out.append({"role": "tool", "tool_call_id": r.tool_use_id, "content": content})
        return out

    def supports(self, feature: str) -> bool:
        if feature in ("tools", "streaming"):
            return True
        if feature in ("reasoning", "thinking"):
            return True
        if feature == "responses_api":
            return not self._azure
        return False


def _status_of_stream_error(err: Any) -> int | None:
    if isinstance(err, dict):
        e = err.get("error", err)
        if isinstance(e, dict):
            for k in ("status", "code", "status_code"):
                v = e.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)
            t = str(e.get("type") or e.get("code") or "")
            if "rate_limit" in t:
                return 429
            if "overloaded" in t or "server_error" in t:
                return 503
    return None


def _reset_stream(state: StreamState, acc: dict[int, _ToolAcc], stop: list[StopReason]) -> None:
    """Forget what a failed attempt accumulated without showing it, before the request is sent again."""
    state.text_parts.clear()
    state.thinking_parts.clear()
    state.usage = Usage()
    acc.clear()
    stop[0] = "end_turn"


def _strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            parts = []
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif isinstance(b, dict) and b.get("type") == "image_url":
                    parts.append("[Attached image omitted: this model does not accept images]")
            m["content"] = "\n".join(parts)
        out.append(m)
    return out


__all__ = [
    "OpenAICompatProvider",
    "sanitize_openai_messages",
    "normalize_tools_for_chat",
    "chat_messages_to_responses_input",
    "chat_tools_to_responses",
    "is_reasoning_model",
]
