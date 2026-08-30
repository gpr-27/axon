# 04 — The Provider Layer

Axon speaks to four models over two incompatible wire protocols, through a proxy that
authenticates on client identity. This layer absorbs all of that so nothing above it
has to know.

## Verified probe results

Measured against `agentrouter.org` on **2026-08-27 16:15 IST**, before any of this
design was committed. The plan rests on these facts, so they were checked rather than
assumed.

| Transport | Client | Result |
|-----------|--------|--------|
| `POST /v1/messages` · `claude-opus-5` | official `anthropic` SDK | ✅ 200 · `stop_reason=tool_use` · `input` arrives as a parsed object · usage includes cache fields |
| `POST /v1/chat/completions` · `gpt-5.6-sol` | official `openai` SDK | ❌ **401 `unauthorized_client_error`** |
| `POST /v1/chat/completions` · `gpt-5.6-sol` | raw `httpx2` + Anthropic UA headers | ✅ 200 · `finish_reason=tool_calls` |
| `POST /v1/chat/completions` · `deepseek-v4-flash` | raw `httpx2` + Anthropic UA headers | ✅ 200 · reports `prompt_cache_hit_tokens` and `reasoning_tokens` |
| either endpoint, no fingerprint headers | plain `httpx` | ❌ 401 on all four models |

### What this establishes

**1. Tool calling works on both protocols.** This was the open question the whole design
depended on. Both `stop_reason: "tool_use"` and `finish_reason: "tool_calls"` come back
correctly with well-formed arguments, so the agentic loop is buildable on either path.

**2. The proxy authenticates the client, not just the key.** The error is
`unauthorized_client_error` — "unauthorized client detected" — not an invalid-key error,
and it fires with a valid key. Requests must carry:

```python
FINGERPRINT = {
    "user-agent":          "Anthropic/Python 1.0.0",
    "x-stainless-lang":    "python",
    "x-stainless-os":      "MacOS",
    "x-stainless-arch":    "arm64",
    "x-stainless-runtime": "CPython",
}
```

The `anthropic` SDK emits these natively, which is why the Anthropic path can use it
directly. The `openai` SDK emits `User-Agent: OpenAI/Python …` and is rejected — so the
OpenAI-compatible path is hand-rolled over `httpx2` with the header set carried over
from `agentrouter_chat.py`. This is the reasoning behind
[ADR-002](01-ARCHITECTURE.md#adr-002--official-anthropic-sdk-for-claude-raw-httpx2-for-openai-compat).

**3. `anthropic` 1.x rides on `httpx2`, not `httpx`.** Both are installed. Passing an
`httpx` client object into the SDK is rejected. `import httpx2 as httpx` throughout.

**4. Re-probe on failure, do not assume.** The gate is a proxy implementation detail and
can change. `axon doctor` runs exactly these probes and reports which paths are live, so
a future 401 is diagnosed in one command instead of being mistaken for a bad key.

## The asymmetry table

The two protocols differ in almost every detail that matters to an agent loop. This
table is the specification for the normalization layer.

| Concern | Anthropic `/v1/messages` | OpenAI `/v1/chat/completions` |
|---------|--------------------------|-------------------------------|
| Tool declaration | `tools: [{name, description, input_schema}]` | `tools: [{type: "function", function: {name, description, parameters}}]` |
| Model requests a tool | content block `{type: "tool_use", id, name, input: {…}}` | `message.tool_calls: [{id, function: {name, arguments}}]` |
| **Argument type** | already a JSON **object** | a JSON **string** needing `json.loads` |
| Stop signal | `stop_reason: "tool_use"` | `finish_reason: "tool_calls"` |
| **Returning results** | **one** `user` message holding **all** `tool_result` blocks | **N** separate `{role: "tool", tool_call_id, content}` messages |
| Error signalling | `tool_result.is_error: true` | no flag — encode in the content string |
| System prompt | top-level `system` parameter | first message, `{role: "system"}` |
| Reasoning output | `thinking` blocks, with a `signature` | `delta.reasoning_content`, no signature |
| Streaming tool args | `input_json_delta.partial_json` fragments | `delta.tool_calls[i].function.arguments` fragments |
| Usage field names | `input_tokens`, `output_tokens`, `cache_read_input_tokens` | `prompt_tokens`, `completion_tokens`, `prompt_cache_hit_tokens` |
| Caching | explicit `cache_control` breakpoints, max 4 | implicit; DeepSeek reported hits unprompted |
| Multiple parallel calls | multiple `tool_use` blocks in one message | multiple entries in `tool_calls` |

The **returning results** row is the one that bites. Anthropic requires batching into a
single message; OpenAI requires one message per result. An implementation that does not
notice this either 400s on Anthropic or silently degrades parallel tool use. Above the
provider layer, the loop always calls
`conversation.append_tool_results(results)` with the whole batch, and each provider
performs its own translation on the way out.

## The Protocol

```python
class Provider(Protocol):
    name: str
    def stream(self, *, model, system, messages, tools,
               max_tokens, effort, thinking) -> Iterator[StreamEvent]: ...
    def finalize(self) -> AssistantTurn: ...
    def supports(self, feature: str) -> bool: ...
```

`StreamEvent` is a small closed union that the renderer consumes:

```python
TextDelta(text)          ThinkingDelta(text)      ToolUseStart(id, name)
ToolArgsDelta(id, frag)  ToolUseComplete(id)      TurnComplete(stop_reason, usage)
```

Neither the renderer nor the loop can tell which provider produced them.

## AnthropicProvider

```python
from anthropic import Anthropic

class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings):
        self._client = Anthropic(
            auth_token=settings.api_key.get_secret_value(),   # NOT api_key= — proxy uses Bearer
            base_url=settings.base_url,                        # https://agentrouter.org
            max_retries=3,
        )
```

`auth_token=` rather than `api_key=` because the proxy expects
`Authorization: Bearer …` rather than `x-api-key`. The SDK sets the fingerprint headers
itself.

Request construction, with the current-API details that a stale prior gets wrong:

```python
kwargs = {
    "model": model,
    "max_tokens": max_tokens,          # 64k safe while streaming; 16k non-streaming
    "system": system_blocks,           # list, so cache_control can be attached
    "messages": messages,
    "tools": tool_specs,
}
if thinking:
    kwargs["thinking"] = {
        "type": "adaptive",            # NOT {"type":"enabled","budget_tokens":N} —
                                       # budget_tokens is a 400 on the Opus 5 family
        "display": "summarized",       # defaults to "omitted"; without this, no
                                       # reasoning is streamed at all
    }
if effort:
    kwargs["output_config"] = {"effort": effort}   # "xhigh" for coding/agentic work
```

Three traps in that block, all of which produce confusing symptoms:

- `budget_tokens` is **rejected with a 400** on Opus 5 / 4.8 / 4.7 and Sonnet 5 / Fable 5.
  Adaptive thinking replaces it.
- Thinking `display` defaults to `"omitted"`, so a correct-looking implementation shows
  no reasoning and looks broken. `"summarized"` is required to surface it.
- Assistant-message prefill returns 400 on these models. Constrain output through the
  system prompt or a tool schema instead.

The SSE accumulator maps events to internal blocks:

```
message_start            → capture input usage
content_block_start      → open a block; for tool_use, remember (index → id, name)
content_block_delta
    text_delta           → TextDelta
    thinking_delta       → ThinkingDelta
    signature_delta      → append to the open thinking block's signature
    input_json_delta     → append partial_json to that index's buffer
content_block_stop       → close; for tool_use, json.loads the buffer
message_delta            → stop_reason, output usage
message_stop             → finalize
```

Tool arguments arrive as concatenated `partial_json` fragments that are only valid JSON
once complete. Buffer per content-block index, parse at `content_block_stop`. A parse
failure becomes a `ToolResultBlock(is_error=True)` telling the model its arguments were
malformed — the loop keeps going.

## OpenAICompatProvider

Raw `httpx2`, because the `openai` SDK cannot get past the gate.

```python
import httpx2 as httpx

class OpenAICompatProvider:
    name = "openai-compat"
    URL = "https://agentrouter.org/v1/chat/completions"

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self._key}",
                "content-type": "application/json",
                **FINGERPRINT}
```

Streaming needs `"stream_options": {"include_usage": true}` or the final chunk carries
no usage and cost accounting is silently zero.

Tool-call fragments are indexed rather than keyed by id, and the id itself may arrive
only in the first fragment:

```python
# delta.tool_calls[i] carries `index`; id/name appear in the first fragment only
for tc in delta.get("tool_calls", []):
    slot = self._calls.setdefault(tc["index"], {"id": None, "name": None, "args": ""})
    if tc.get("id"):
        slot["id"] = tc["id"]
    if fn := tc.get("function"):
        if fn.get("name"):
            slot["name"] = fn["name"]
        slot["args"] += fn.get("arguments", "")     # fragments; concatenate
```

Result translation on the way out inverts Law 2 — the batch is expanded into one message
per result:

```python
def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict]:
    return [
        {"role": "tool", "tool_call_id": r.tool_use_id,
         "content": (f"ERROR: {r.content}" if r.is_error else r.content)}
        for r in results
    ]
```

`is_error` has no wire representation here, so it is folded into the string. Less
precise than the Anthropic flag, and enough for the model to notice.

## Routing and pricing

```python
PROVIDER_FOR = {
    "claude-opus-5":     AnthropicProvider,
    "claude-opus-4-8":   AnthropicProvider,
    "gpt-5.6-sol":       OpenAICompatProvider,
    "deepseek-v4-flash": OpenAICompatProvider,
}

# USD per 1M tokens, as billed by agentrouter (resale — differs from first-party rates)
PRICING = {
    "claude-opus-5":     {"input": 6.00, "output": 30.00, "cache_read": 0.60},
    "claude-opus-4-8":   {"input": 6.00, "output": 30.00, "cache_read": 0.60},
    "gpt-5.6-sol":       {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    "deepseek-v4-flash": {"input": 2.00, "output":  6.00, "cache_read": 0.20},
}
```

Model selection is a per-session setting changeable mid-session with `/model`. Because
the conversation is stored in normalized form plus `native`, switching providers
mid-session works — the new provider re-encodes the normalized history. Thinking-block
signatures do not survive a cross-provider switch, so the switch drops thinking blocks
from replayed history and notes it in the transcript.

**Recommended defaults.** `claude-opus-5` at `effort: "xhigh"` for the main loop;
`deepseek-v4-flash` for `WebFetch` summarization and other cheap sub-tasks. A mixed
strategy is worth several times its complexity in saved cost.

## Capability probing

Betas may or may not survive the proxy. Rather than assume, probe once and cache:

```python
FEATURES = {
    "compaction":     ("compact-2026-01-12",             {"edits": [{"type": "compact_20260112"}]}),
    "context_edit":   ("context-management-2025-06-27",  {"edits": [{"type": "clear_tool_uses_20250919"}]}),
    "task_budget":    ("task-budgets-2026-03-13",        None),
    "prompt_caching": (None,                              None),
    "count_tokens":   (None,                              None),
}
```

Each probe is a minimal request with `max_tokens: 1`. A 200 means supported; a 400
naming the beta means not. Results are cached in `~/.axon/capabilities.json` keyed by
`(base_url, model)` with a 7-day TTL, and `axon doctor --refresh` re-runs them.

Everything degrades:

| Feature | Available | Unavailable |
|---------|-----------|-------------|
| Server compaction | `compact_20260112` | Client-side summarize-and-replace |
| Context editing | `clear_tool_uses_20250919` | Manual eviction of old tool results |
| Token counting | `messages.count_tokens` | `len(text) / 3.7` heuristic |
| Prompt caching | `cache_control` breakpoints | Omit them; log the lost saving |
| Task budgets | server-enforced | client-side iteration and token caps |

The degraded path is never *broken*, only more expensive or less precise. That is what
makes it safe to point Axon at an endpoint whose capabilities are unknown.

---

Next: [`05-CONTEXT-AND-COST.md`](05-CONTEXT-AND-COST.md) — making long sessions survivable and affordable.
