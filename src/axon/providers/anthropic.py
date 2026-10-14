"""
Anthropic provider: native Messages API through the official SDK (streaming, prompt caching,
adaptive / budgeted extended thinking, effort, tool_use with input_json_delta, cache usage).
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterator

from anthropic import Anthropic

from axon.errors import ProviderError
from axon.providers.base import (
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
from axon.providers import transport as _transport
from axon.providers.base import StreamCanceller, retry_event
from axon.providers.transport import (
    RETRYABLE_STATUS,
    backoff_delay,
    parse_retry_after,
    canonical_effort,
    classify_error,
    connection_error,
    default_headers,
    is_agentrouter,
    is_official_anthropic,
    is_retryable_exception,
    max_retries_for,
    request_timeout_for,
)

MAX_CACHE_BREAKPOINTS = 4

# ─── Model capability families ─────────────────────────────────────────────
# Thinking is always on (explicit disable / budget_tokens are 400s): omit `thinking` to disable.
_ALWAYS_ON = ("claude-fable-", "claude-mythos-", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5")
# Adaptive thinking ({type: adaptive}); budget_tokens removed or deprecated.
_ADAPTIVE = ("claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-sonnet-5")
# Legacy extended thinking ({type: enabled, budget_tokens}).
_BUDGET = ("claude-opus-4-5", "claude-opus-4-1", "claude-opus-4-0", "claude-opus-4-2025", "claude-sonnet-4-5",
           "claude-sonnet-4-0", "claude-sonnet-4-2025", "claude-haiku-4-5", "claude-3-7-sonnet")
# Effort support: xhigh available from Opus 4.7 / 5.x on; Opus 4.6 / Sonnet 4.6 up to high/max; Opus 4.5 low..high.
_EFFORT_XHIGH = _ALWAYS_ON + ("claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")
_EFFORT_BASIC = ("claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-5")
# Families whose effort scale tops out at "max" (no xhigh): Axon's top tier (xhigh) maps there.
_EFFORT_MAX = ("claude-opus-4-6", "claude-sonnet-4-6")

_THINKING_BUDGET = {"low": 2_048, "medium": 6_000, "high": 16_000, "xhigh": 32_000}


def _bare(model: str) -> str:
    m = (model or "").lower().rsplit("/", 1)[-1]
    return re.sub(r"(?<=\d)\.(?=\d)", "-", m)


def _matches(model: str, prefixes: tuple[str, ...]) -> bool:
    m = _bare(model)
    for p in prefixes:
        if m == p or (m.startswith(p) and (p.endswith("-") or m[len(p)] in "-@:")):
            return True
    return False


def thinking_mode(model: str) -> str:
    """'always_on' | 'adaptive' | 'budget' | 'none' for a Claude model id."""
    if _matches(model, _ALWAYS_ON):
        return "always_on"
    if _matches(model, _ADAPTIVE):
        return "adaptive"
    if _matches(model, _BUDGET):
        return "budget"
    m = _bare(model)
    found = re.match(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)", m)
    if found and int(found.group(1)) >= 5:
        return "always_on"  # future 5.x+ ids follow the current API surface
    return "none"


def build_thinking_params(model: str, *, thinking: bool, effort: str | None, max_tokens: int) -> dict[str, Any]:
    """Request fields (thinking / output_config) appropriate for this model."""
    out: dict[str, Any] = {}
    mode = thinking_mode(model)
    level = canonical_effort(effort)
    if thinking:
        if mode in ("always_on", "adaptive"):
            out["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif mode == "budget" and max_tokens > 2_048:
            budget = min(_THINKING_BUDGET.get(level or "high", 16_000), max_tokens - 1_024)
            if budget >= 1_024:
                out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if level:
        if _matches(model, _EFFORT_XHIGH) or (mode == "always_on"):
            out["output_config"] = {"effort": level}
        elif _matches(model, _EFFORT_BASIC):
            top = "max" if _matches(model, _EFFORT_MAX) else "high"
            out["output_config"] = {"effort": top if level == "xhigh" else level}
    return out


# ─── Message conversion (OpenAI-shaped history -> Anthropic) ───────────────
def _image_block(url: str) -> dict[str, Any] | None:
    m = re.match(r"data:([^;]+);base64,(.*)", url or "", re.DOTALL)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize history for the Messages API: convert OpenAI tool_calls / role=tool, drop empty
    turns and non-standard fields, and make every tool_use answered by a tool_result."""
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""),
                     "content": content if isinstance(content, str) else json.dumps(content)}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in out[-1]["content"]
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role in ("system", "developer"):
            text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
            if text:
                out.append({"role": "user", "content": [{"type": "text", "text": f"[System note] {text}"}]})
            continue
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, str):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": content}] if content else []
        elif isinstance(content, list):
            blocks = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "image_url":
                    iu = b.get("image_url")
                    img = _image_block(iu.get("url", "") if isinstance(iu, dict) else str(iu))
                    if img:
                        blocks.append(img)
                elif b.get("type") == "text" and not b.get("text"):
                    continue
                else:
                    blocks.append(dict(b))
        else:
            blocks = []
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                parsed, _err = parse_tool_arguments(fn.get("arguments"))
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": parsed})
        if not blocks:
            continue
        out.append({"role": role, "content": blocks})

    # Pair tool_use with tool_result.
    fixed: list[dict[str, Any]] = []
    i = 0
    while i < len(out):
        m = out[i]
        fixed.append(m)
        i += 1
        if m["role"] != "assistant":
            continue
        ids = [b.get("id") for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
        if not ids:
            continue
        nxt = out[i] if i < len(out) else None
        answered: set[str] = set()
        if nxt is not None and nxt["role"] == "user":
            answered = {b.get("tool_use_id") for b in nxt["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
        missing = [t for t in ids if t not in answered]
        if not missing:
            continue
        stubs = [{"type": "tool_result", "tool_use_id": t, "content": "[Tool execution was interrupted]", "is_error": True} for t in missing]
        if nxt is not None and nxt["role"] == "user":
            nxt["content"] = stubs + list(nxt["content"])
        else:
            fixed.append({"role": "user", "content": stubs})
    return fixed


def limit_cache_breakpoints(system: list[dict[str, Any]], tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> tuple[list, list, list]:
    """Copy request parts keeping at most 4 cache_control markers (oldest message markers dropped);
    adds a system breakpoint when none exists."""
    system = [dict(b) for b in system or [] if isinstance(b, dict)]
    tools = [dict(t) for t in tools or [] if isinstance(t, dict)]
    msgs = [dict(m) for m in messages]
    for m in msgs:
        if isinstance(m.get("content"), list):
            m["content"] = [dict(b) if isinstance(b, dict) else b for b in m["content"]]

    def count() -> int:
        n = sum(1 for b in system if "cache_control" in b) + sum(1 for t in tools if "cache_control" in t)
        for m in msgs:
            if isinstance(m.get("content"), list):
                n += sum(1 for b in m["content"] if isinstance(b, dict) and "cache_control" in b)
        return n

    if system and count() == 0:
        system[-1]["cache_control"] = {"type": "ephemeral"}
    excess = count() - MAX_CACHE_BREAKPOINTS
    for m in msgs:
        if excess <= 0:
            break
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if excess > 0 and isinstance(b, dict) and "cache_control" in b:
                    del b["cache_control"]
                    excess -= 1
    for t in tools:
        if excess > 0 and "cache_control" in t:
            del t["cache_control"]
            excess -= 1
    return system, tools, msgs


def cache_on_last_message_only(system: list[dict[str, Any]], tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> tuple[list, list, list]:
    """Strip every cache_control marker and put one on the last block of the last message."""
    def strip(blocks: list[Any]) -> list[Any]:
        return [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b for b in blocks]

    msgs = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            m["content"] = strip(m["content"])
        msgs.append(m)
    if msgs:
        last = msgs[-1]
        content = last.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}] if content else []
        if content and isinstance(content[-1], dict):
            content = list(content)
            content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
            last["content"] = content
    return strip(system or []), strip(tools or []), msgs


def normalize_tools_for_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append({"name": fn.get("name", ""), "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        else:
            out.append(t)
    return out


class AnthropicProvider:
    name: str = "anthropic"

    def __init__(self, settings: Any, *, model_prefix: str = "", http_client: Any = None,
                 aliases: dict[str, str] | None = None) -> None:
        self.settings = settings
        self._model_prefix = model_prefix or ""
        self._aliases = dict(aliases or {})
        self._max_retries = max_retries_for(settings)
        self._cache_last_only = False  # set when a gateway rejects cache_control placement
        self._canceller = StreamCanceller()
        key_obj = getattr(settings, "api_key", None)
        key = key_obj.get_secret_value() if hasattr(key_obj, "get_secret_value") else str(key_obj or "")
        key = "" if key.strip().lower() in ("local", "none", "null") else key.strip()
        base = str(settings.base_url)
        kwargs: dict[str, Any] = {
            "base_url": base,
            "max_retries": 0,  # retries are done here so each one is visible in the trace
            "timeout": request_timeout_for(settings),
        }
        if is_official_anthropic(base):
            kwargs["api_key"] = key or None
        else:
            # Anthropic-compatible gateways (AgentRouter & co.) authenticate with a Bearer token.
            kwargs["auth_token"] = key or None
            kwargs["api_key"] = key or None
        if is_agentrouter(base):
            kwargs["default_headers"] = {k: v for k, v in default_headers(base).items() if k.startswith(("user-agent", "x-stainless"))}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = Anthropic(**kwargs)
        self._local = threading.local()

    @property
    def _last_turn(self) -> AssistantTurn | None:  # backwards-compatible accessor
        st = getattr(self._local, "state", None)
        return st.turn if st else None

    def _wire_model(self, model: str) -> str:
        """Model id as sent: alias resolved, "[1m]" suffix and route prefix stripped."""
        m = self._aliases.get(model, model)
        if m.lower().endswith("[1m]"):
            m = m[:-4]
        if self._model_prefix and m.startswith(self._model_prefix):
            m = m[len(self._model_prefix):]
        return m

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
        wire_model = self._wire_model(model)
        try:
            from axon.providers.registry import known_max_output
            cap = known_max_output(wire_model)
        except Exception:
            cap = None
        limit = max(1, min(int(max_tokens), cap) if cap else int(max_tokens))

        sys_blocks, tool_defs, msgs = limit_cache_breakpoints(system, normalize_tools_for_anthropic(tools), to_anthropic_messages(messages))
        if self._cache_last_only:
            sys_blocks, tool_defs, msgs = cache_on_last_message_only(sys_blocks, tool_defs, msgs)
        kwargs: dict[str, Any] = {"model": wire_model, "max_tokens": limit, "messages": msgs}
        if sys_blocks:
            kwargs["system"] = sys_blocks
        if tool_defs:
            kwargs["tools"] = tool_defs
        extra = build_thinking_params(wire_model, thinking=thinking, effort=effort, max_tokens=limit)
        if extra:
            kwargs["extra_body"] = extra
        if model.lower().endswith("[1m]") or self._aliases.get(model, "").lower().endswith("[1m]"):
            if _matches(wire_model, ("claude-sonnet-4", "claude-sonnet-4-5")):
                kwargs["extra_headers"] = {"anthropic-beta": "context-1m-2025-08-07"}

        stripped_thinking = False
        retry_no = 0
        self._canceller.begin()
        while True:
            emitted = [False]
            try:
                self._canceller.check()
                yield from self._run(kwargs, state, emitted, thinking, wire_model)
                self._canceller.check()
                return
            except ProviderError:
                self._canceller.check()
                raise
            except Exception as e:
                self._canceller.check()  # an aborted read surfaces as a transport error: report Esc/Ctrl+C
                status = getattr(e, "status_code", None)
                body = getattr(e, "body", None)
                text = f"{getattr(e, 'message', '')} {body or ''} {e}".lower()
                if (not emitted[0] and not stripped_thinking and status == 400 and "extra_body" in kwargs
                        and any(k in text for k in ("thinking", "effort", "output_config", "budget_tokens", "adaptive"))):
                    kwargs.pop("extra_body", None)  # endpoint/model rejects thinking params: retry plain
                    stripped_thinking = True
                    continue
                if not emitted[0] and status == 400 and "cache_control" in text and not self._cache_last_only:
                    # Gateway rejects our breakpoints: keep a single marker on the last message for this session.
                    self._cache_last_only = True
                    s_, t_, m_ = cache_on_last_message_only(kwargs.get("system", []), kwargs.get("tools", []), kwargs["messages"])
                    if s_:
                        kwargs["system"] = s_
                    if t_:
                        kwargs["tools"] = t_
                    kwargs["messages"] = m_
                    continue
                retryable = (status in RETRYABLE_STATUS) if isinstance(status, int) else is_retryable_exception(e)
                if not emitted[0] and retryable and retry_no < self._max_retries:
                    resp = getattr(e, "response", None)
                    delay = backoff_delay(retry_no, parse_retry_after(getattr(resp, "headers", None)))
                    retry_no += 1
                    yield retry_event(retry_no, self._max_retries, delay,
                                      f"HTTP {status} from anthropic" if status else f"{type(e).__name__}: {e}")
                    _transport._sleep(delay)
                    continue
                if status is None and is_retryable_exception(e):
                    raise connection_error(e, str(self.settings.base_url)) from e
                raise classify_error(status=status, body=body if body is not None else getattr(e, "message", None),
                                     base_url=str(self.settings.base_url), model=model, exc=e if status is None else None) from e

    def cancel(self, thread_ident: int | None = None) -> bool:
        """Abort the in-flight stream (of *thread_ident*, default all) from another thread."""
        return self._canceller.cancel(thread_ident)

    def _run(self, kwargs: dict[str, Any], state: StreamState, emitted: list[bool], thinking: bool, wire_model: str = "") -> Iterator[StreamEvent]:
        buffers: dict[int, dict[str, Any]] = {}
        blocks: list[Block] = []
        with self._client.messages.stream(**kwargs) as stream, self._canceller.track(stream):
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_start":
                    cb = event.content_block
                    idx = event.index
                    ctype = getattr(cb, "type", "")
                    if ctype == "thinking":
                        buffers[idx] = {"type": "thinking", "text": getattr(cb, "thinking", "") or "", "signature": getattr(cb, "signature", None)}
                    elif ctype == "text":
                        buffers[idx] = {"type": "text", "text": getattr(cb, "text", "") or ""}
                    elif ctype == "tool_use":
                        buffers[idx] = {"type": "tool_use", "id": cb.id, "name": cb.name, "json": ""}
                        emitted[0] = True
                        yield ToolUseStart(id=cb.id, name=cb.name)
                elif etype == "content_block_delta":
                    d = event.delta
                    buf = buffers.get(event.index)
                    dtype = getattr(d, "type", "")
                    if dtype == "thinking_delta":
                        th = getattr(d, "thinking", "") or ""
                        if buf is not None:
                            buf["text"] += th
                        state.thinking_parts.append(th)
                        if th and thinking:
                            emitted[0] = True
                            yield ThinkingDelta(text=th)
                    elif dtype == "signature_delta":
                        if buf is not None:
                            buf["signature"] = (buf.get("signature") or "") + (getattr(d, "signature", "") or "")
                    elif dtype == "text_delta":
                        txt = getattr(d, "text", "") or ""
                        if buf is not None:
                            buf["text"] += txt
                        state.text_parts.append(txt)
                        if txt:
                            emitted[0] = True
                            yield TextDelta(text=txt)
                    elif dtype == "input_json_delta":
                        frag = getattr(d, "partial_json", "") or ""
                        if buf is not None:
                            buf["json"] += frag
                            if frag:
                                emitted[0] = True
                                yield ToolArgsDelta(id=buf["id"], fragment=frag)
                elif etype == "content_block_stop":
                    buf = buffers.get(event.index)
                    if not buf:
                        continue
                    if buf["type"] == "thinking":
                        blocks.append(ThinkingBlock(text=buf["text"], signature=buf.get("signature")))
                    elif buf["type"] == "text":
                        blocks.append(TextBlock(text=buf["text"]))
                    elif buf["type"] == "tool_use":
                        parsed, perr = parse_tool_arguments(buf["json"])
                        blocks.append(ToolUseBlock(id=buf["id"], name=buf["name"], input=parsed, parse_error=perr))
                elif etype == "message_delta":
                    u = getattr(event, "usage", None)
                    if u is not None:
                        state.usage = self._usage(u, state.usage)
            final_msg = stream.get_final_message()

        stop_reason: StopReason = getattr(final_msg, "stop_reason", None) or "end_turn"  # type: ignore[assignment]
        raw_usage = getattr(final_msg, "usage", None)
        usage = self._usage(raw_usage, Usage()) if raw_usage is not None else state.usage
        state.usage = usage

        native: list[Any] = []
        for b in getattr(final_msg, "content", []) or []:
            native.append(b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b)

        # A tool_use the turn can't complete (truncated JSON at max_tokens) is dropped from both the
        # normalized blocks and the replayed content so history never holds an unanswered tool_use.
        drop = {b.id for b in blocks if isinstance(b, ToolUseBlock) and b.parse_error and stop_reason == "max_tokens"}
        if drop:
            blocks = [b for b in blocks if not (isinstance(b, ToolUseBlock) and b.id in drop)]
            native = [n for n in native if not (isinstance(n, dict) and n.get("type") == "tool_use" and n.get("id") in drop)]
        has_tools = any(isinstance(b, ToolUseBlock) for b in blocks)
        if has_tools and stop_reason in ("max_tokens", "end_turn", "stop_sequence"):
            stop_reason = "tool_use"
        if not native:
            native = [{"type": "text", "text": "".join(b.text for b in blocks if isinstance(b, TextBlock)) or "[empty response]"}]

        for b in blocks:
            if isinstance(b, ToolUseBlock):
                yield ToolUseComplete(id=b.id)
        state.turn = AssistantTurn(blocks=blocks, stop_reason=stop_reason, usage=usage, native=native, model=wire_model or None)
        yield TurnComplete(stop_reason=stop_reason, usage=usage)

    @staticmethod
    def _usage(raw: Any, prev: Usage) -> Usage:
        def g(name: str, fallback: int) -> int:
            v = getattr(raw, name, None)
            return int(v) if isinstance(v, int) else fallback

        uncached = g("input_tokens", 0)
        cache_read = g("cache_read_input_tokens", prev.cache_read)
        cache_write = g("cache_creation_input_tokens", prev.cache_write)
        split = getattr(raw, "cache_creation", None)
        w1h = getattr(split, "ephemeral_1h_input_tokens", None) if split is not None else None
        if w1h is None and isinstance(split, dict):
            w1h = split.get("ephemeral_1h_input_tokens")
        write_1h = int(w1h) if isinstance(w1h, int) else prev.cache_write_1h
        total_in = uncached + cache_read + cache_write if uncached or cache_read or cache_write else prev.input
        return Usage(input=total_in, output=g("output_tokens", prev.output), cache_read=cache_read, cache_write=cache_write,
                     reasoning=prev.reasoning, cache_write_1h=min(write_1h, cache_write))

    def finalize(self) -> AssistantTurn:
        state: StreamState | None = getattr(self._local, "state", None)
        self._local.state = None
        if state is None:
            return AssistantTurn(blocks=[], stop_reason="end_turn", usage=Usage())
        if state.turn is not None:
            return state.turn
        return state.partial_turn("anthropic")

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        # Anthropic requires ALL results in ONE user message.
        content = []
        for r in results:
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": r.tool_use_id, "content": r.content}
            if r.is_error:
                block["is_error"] = True
            content.append(block)
        return [{"role": "user", "content": content}]

    def supports(self, feature: str) -> bool:
        return feature in ("tools", "thinking", "prompt_caching", "streaming")

