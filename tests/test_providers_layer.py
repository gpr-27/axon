"""
Provider layer tests: streaming parsers for Chat Completions, Responses and Anthropic Messages over
fake HTTP (httpx MockTransport), retries/backoff, error classification, cancellation, thread
isolation, routing / per-provider keys, aliases, failover, pricing and the ledger. No network.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from axon.config import Settings
from axon.errors import ProviderError
from axon.providers import pricing, transport
from axon.providers.anthropic import (
    AnthropicProvider,
    build_thinking_params,
    limit_cache_breakpoints,
    thinking_mode,
    to_anthropic_messages,
)
from axon.providers.base import (
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolUseBlock,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
    Usage,
)
from axon.providers.catalog import (
    PROVIDER_PRESETS,
    find_api_key,
    find_preset_by_url,
    find_preset_for_model,
    get_preset_by_id,
    resolve_model_alias,
    save_api_key,
)
from axon.providers.openai_compat import (
    OpenAICompatProvider,
    chat_messages_to_responses_input,
    sanitize_openai_messages,
)
from axon.providers.registry import (
    PRICING,
    fallback_chain,
    get_context_window,
    provider_for,
    resolve_api_key,
    resolve_route,
)
from axon.providers.verifier import build_ping_request
from axon.session.ledger import Ledger

PROVIDER_ENV = [v for p in PROVIDER_PRESETS for v in p.env_vars] + ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in PROVIDER_ENV:
        monkeypatch.delenv(var, raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(transport, "_sleep", lambda s: sleeps.append(s))
    return sleeps


def sse(*chunks: object, done: bool = True) -> bytes:
    lines = []
    for c in chunks:
        if isinstance(c, tuple):  # (event, data)
            lines.append(f"event: {c[0]}\ndata: {json.dumps(c[1])}\n\n")
        else:
            lines.append(f"data: {json.dumps(c)}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


class Recorder:
    """MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("unexpected extra request")
        return self.queue.pop(0)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def openai_provider(base_url: str, rec: Recorder, key: str = "sk-test-key", **kw) -> OpenAICompatProvider:
    settings = Settings(base_url=base_url, api_key=SecretStr(key))
    client = httpx.Client(transport=httpx.MockTransport(rec))
    return OpenAICompatProvider(settings, http_client=client, **kw)


def run(provider, **kw):
    params = dict(model="gpt-4o", system=[{"type": "text", "text": "sys"}], messages=[{"role": "user", "content": "hi"}],
                  tools=[], max_tokens=1000, effort=None, thinking=True)
    params.update(kw)
    events = list(provider.stream(**params))
    return events, provider.finalize()


# ─── Chat Completions ──────────────────────────────────────────────────────
def test_chat_stream_text_reasoning_and_tool_call_deltas():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "plan "}}]},
        {"choices": [{"index": 0, "delta": {"content": "Running "}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "Bash", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\": "}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"ls\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "Read", "arguments": "{\"path\": \"a.py\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 100},
                                   "completion_tokens_details": {"reasoning_tokens": 7}}},
    ), headers={"content-type": "text/event-stream"}))
    prov = openai_provider("https://api.openai.com/v1", rec)
    events, turn = run(prov, model="gpt-4o")

    kinds = [type(e).__name__ for e in events]
    assert kinds.index("ToolUseStart") < kinds.index("ToolArgsDelta")
    starts = [e for e in events if isinstance(e, ToolUseStart)]
    assert [(s.id, s.name) for s in starts] == [("call_a", "Bash"), ("call_b", "Read")]
    assert "".join(e.fragment for e in events if isinstance(e, ToolArgsDelta) and e.id == "call_a") == '{"command": "ls"}'
    assert [e.id for e in events if isinstance(e, ToolUseComplete)] == ["call_a", "call_b"]
    assert isinstance(events[-1], TurnComplete)

    assert turn.stop_reason == "tool_use"
    assert turn.text == "Running " and turn.thinking == "plan "
    assert [(t.name, t.input) for t in turn.tool_uses] == [("Bash", {"command": "ls"}), ("Read", {"path": "a.py"})]
    assert turn.usage == Usage(input=120, output=30, cache_read=100, reasoning=7)
    assert [tc["id"] for tc in turn.native["tool_calls"]] == ["call_a", "call_b"]

    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-key"
    assert not req.headers["user-agent"].startswith("Anthropic")
    body = rec.body()
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 1000 and "max_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_chat_finish_stop_with_tool_calls_keeps_them():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "c1", "function": {"name": "Ls", "arguments": "{}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    )))
    _, turn = run(openai_provider("https://generativelanguage.googleapis.com/v1beta/openai/", rec, key="AIza-k"), model="gemini-2.5-flash")
    assert turn.stop_reason == "tool_use" and turn.tool_uses[0].name == "Ls"
    assert str(rec.requests[0].url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def test_chat_malformed_tool_json_is_flagged_not_crashing():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Bash", "arguments": "{\"command\": \"ls"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )))
    _, turn = run(openai_provider("https://api.groq.com/openai/v1", rec), model="llama-3.3-70b-versatile")
    tu = turn.tool_uses[0]
    assert tu.parse_error and "not valid JSON" in tu.parse_error
    assert tu.input == {"raw": '{"command": "ls'}
    assert str(rec.requests[0].url) == "https://api.groq.com/openai/v1/chat/completions"


def test_chat_truncated_tool_call_at_length_is_dropped():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"content": "Let me"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Write", "arguments": "{\"path\": \"x"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
    )))
    _, turn = run(openai_provider("https://api.mistral.ai/v1", rec), model="mistral-large-latest")
    assert turn.stop_reason == "max_tokens"
    assert turn.tool_uses == [] and "tool_calls" not in turn.native
    assert "stream_options" not in rec.body()  # Mistral rejects stream_options


def test_chat_midstream_error_chunk_raises():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"content": "partial"}}]},
        {"error": {"message": "upstream exploded", "type": "server_error"}},
    )))
    prov = openai_provider("https://api.together.xyz/v1", rec)
    with pytest.raises(ProviderError) as exc:
        run(prov, model="deepseek-ai/DeepSeek-V3")
    assert "upstream exploded" in str(exc.value)


def test_reasoning_models_get_reasoning_effort_and_strict_strip():
    rec = Recorder(httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})))
    prov = openai_provider("https://api.openai.com/v1", rec)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": "a", "reasoning_content": "secret"},
        {"role": "user", "content": "next"},
    ]
    run(prov, model="gpt-5", messages=history, effort="quantum", tools=[{"name": "Ls", "description": "d", "input_schema": {"type": "object"}}])
    body = rec.body()
    assert body["reasoning_effort"] == "high"
    assert body["tools"][0]["function"]["name"] == "Ls"  # Anthropic-style tool converted
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert "reasoning_content" not in assistant
    assert "cache_control" not in json.dumps(body["messages"])
    assert history[1]["reasoning_content"] == "secret"  # caller history untouched


def test_max_tokens_clamped_to_model_limit():
    rec = Recorder(httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})))
    run(openai_provider("https://api.deepseek.com", rec), model="deepseek-chat", max_tokens=128_000)
    assert rec.body()["max_tokens"] == 8_192
    assert str(rec.requests[0].url) == "https://api.deepseek.com/v1/chat/completions"


def test_retry_on_429_honours_retry_after(_clean_env):
    sleeps = _clean_env
    rec = Recorder(
        httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}}),
        httpx.Response(503, json={"error": {"message": "busy"}}),
        httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]})),
    )
    events, turn = run(openai_provider("https://api.x.ai/v1", rec), model="grok-4")
    assert turn.text == "done" and len(rec.requests) == 3
    assert sleeps[0] == 2.0 and 0 < sleeps[1] <= 1.0
    retries = [e for e in events if type(e).__name__ in ("RetryEvent", "ProviderRetry")]
    assert len(retries) == 2 and "429" in retries[0].error


def test_401_is_not_retried_and_names_the_env_var():
    rec = Recorder(httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(ProviderError) as exc:
        run(openai_provider("https://api.cerebras.ai/v1", rec), model="llama-3.3-70b")
    assert exc.value.status == 401 and len(rec.requests) == 1
    assert "CEREBRAS_API_KEY" in str(exc.value) and "Incorrect API key" in str(exc.value)


@pytest.mark.parametrize("status,needle", [(403, "403 Forbidden"), (404, "404 Not Found"), (500, "temporarily unavailable")])
def test_status_errors_are_classified(status, needle):
    responses = [httpx.Response(status, json={"error": {"message": "nope"}}) for _ in range(5)]
    with pytest.raises(ProviderError) as exc:
        run(openai_provider("https://api.fireworks.ai/inference/v1", Recorder(*responses)), model="accounts/fireworks/models/deepseek-v3")
    assert needle in str(exc.value) and exc.value.status == status


def test_azure_uses_deployment_url_and_api_key_header():
    rec = Recorder(httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})))
    prov = openai_provider("https://myres.openai.azure.com", rec, key="az-key")
    run(prov, model="gpt-4o-prod")
    req = rec.requests[0]
    assert str(req.url).startswith("https://myres.openai.azure.com/openai/deployments/gpt-4o-prod/chat/completions?api-version=")
    assert req.headers["api-key"] == "az-key" and "authorization" not in req.headers


def test_local_provider_sends_no_auth_header():
    rec = Recorder(httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})))
    run(openai_provider("http://localhost:1234/v1", rec, key="local"), model="qwen/qwen3-coder-30b")
    assert "authorization" not in rec.requests[0].headers


@pytest.mark.parametrize("base,expected", [
    ("https://openrouter.ai", "https://openrouter.ai/api/v1/chat/completions"),
    ("https://openrouter.ai/api/v1/", "https://openrouter.ai/api/v1/chat/completions"),
    ("https://agentrouter.org", "https://agentrouter.org/v1/chat/completions"),
    ("https://api.groq.com", "https://api.groq.com/openai/v1/chat/completions"),
    ("https://api.fireworks.ai/inference/v1", "https://api.fireworks.ai/inference/v1/chat/completions"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
    ("https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
])
def test_endpoint_normalization(base, expected):
    assert transport.chat_completions_url(base) == expected


# ─── Responses API ─────────────────────────────────────────────────────────
def test_responses_api_official_events_and_body():
    rec = Recorder(httpx.Response(200, content=sse(
        ("response.created", {"type": "response.created", "response": {"status": "in_progress"}}),
        ("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "delta": "think"}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hi"}),
        ("response.output_item.added", {"type": "response.output_item.added", "output_index": 2,
                                        "item": {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "Grep", "arguments": ""}}),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 2, "delta": "{\"pattern\":"}),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 2, "delta": "\"x\"}"}),
        ("response.completed", {"type": "response.completed", "response": {"status": "completed", "usage": {
            "input_tokens": 50, "input_tokens_details": {"cached_tokens": 20}, "output_tokens": 9,
            "output_tokens_details": {"reasoning_tokens": 4}}}}),
        done=False,
    )))
    prov = openai_provider("https://api.openai.com/v1", rec)
    prov.settings = prov.settings.model_copy(update={"wire_api": "responses"})
    history = [
        {"role": "user", "content": "find x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a.py"},
    ]
    events, turn = run(prov, model="gpt-5", messages=history, effort="balanced",
                       tools=[{"type": "function", "function": {"name": "Grep", "description": "d", "parameters": {"type": "object"}}}])
    assert turn.text == "Hi" and turn.thinking == "think"
    assert [(t.id, t.name, t.input) for t in turn.tool_uses] == [("call_9", "Grep", {"pattern": "x"})]
    assert turn.stop_reason == "tool_use"
    assert turn.usage == Usage(input=50, output=9, cache_read=20, reasoning=4)
    body = rec.body()
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/responses"
    assert body["instructions"] == "sys" and "messages" not in body and "max_tokens" not in body
    assert body["max_output_tokens"] == 1000 and body["reasoning"] == {"effort": "medium"}
    assert body["tools"] == [{"type": "function", "name": "Grep", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    assert {"type": "function_call", "call_id": "call_1", "name": "Ls", "arguments": "{}"} in body["input"]
    assert {"type": "function_call_output", "call_id": "call_1", "output": "a.py"} in body["input"]


def test_responses_404_falls_back_to_chat_completions():
    rec = Recorder(
        httpx.Response(404, json={"error": {"message": "no such route"}}),
        httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "chat ok"}, "finish_reason": "stop"}]})),
    )
    _, turn = run(openai_provider("https://agentrouter.org/v1", rec), model="gpt-6-astra")
    assert turn.text == "chat ok"
    assert [r.url.path for r in rec.requests] == ["/v1/responses", "/v1/chat/completions"]


def test_responses_input_conversion_images():
    _, items = chat_messages_to_responses_input([{"role": "user", "content": [{"type": "text", "text": "see"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}])
    assert items[0]["content"] == [{"type": "input_text", "text": "see"}, {"type": "input_image", "image_url": "data:image/png;base64,AA"}]


# ─── Cancellation & concurrency ─────────────────────────────────────────────
def test_interrupt_midstream_finalize_returns_consistent_partial_turn():
    rec = Recorder(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"content": "Hello "}}]},
        {"choices": [{"index": 0, "delta": {"content": "wor"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "Bash", "arguments": "{\"comm"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )))
    prov = openai_provider("https://api.openai.com/v1", rec)
    gen = prov.stream(model="gpt-4o", system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=100)
    seen = []
    with pytest.raises(KeyboardInterrupt):
        for ev in gen:
            seen.append(ev)
            if isinstance(ev, ToolUseStart):
                raise KeyboardInterrupt
    gen.close()
    turn = prov.finalize()
    assert turn.text == "Hello wor"
    assert turn.tool_uses == [] and turn.stop_reason == "end_turn"
    assert turn.native == {"role": "assistant", "content": "Hello wor"}
    assert prov.finalize().blocks == []  # state consumed


def test_concurrent_streams_are_isolated_per_thread():
    def handler(request: httpx.Request) -> httpx.Response:
        word = json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": word}, "finish_reason": "stop"}]}))

    settings = Settings(base_url="https://api.openai.com/v1", api_key=SecretStr("k"))
    prov = OpenAICompatProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    results: dict[str, str] = {}
    barrier = threading.Barrier(4)

    def worker(word: str) -> None:
        barrier.wait()
        list(prov.stream(model="gpt-4o", system=[], messages=[{"role": "user", "content": word}], tools=[], max_tokens=10))
        results[word] = prov.finalize().text

    threads = [threading.Thread(target=worker, args=(w,)) for w in ("alpha", "beta", "gamma", "delta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {w: w for w in ("alpha", "beta", "gamma", "delta")}


def test_sanitize_does_not_mutate_input_and_pairs_tools():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "t"}, {"type": "tool_use", "id": "t1", "name": "Ls", "input": {}}]},
    ]
    before = json.dumps(msgs)
    out = sanitize_openai_messages(msgs)
    assert json.dumps(msgs) == before
    assert out[0]["content"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert out[1]["tool_calls"][0]["id"] == "t1" and out[1]["content"] == ""
    assert out[2] == {"role": "tool", "tool_call_id": "t1", "content": "[Tool execution completed or interrupted]"}


# ─── Anthropic Messages API ─────────────────────────────────────────────────
def _anthropic_httpx():
    import anthropic._base_client as bc
    return getattr(bc, "httpx2", None) or getattr(bc, "httpx")


def _anthropic_sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


ANTHROPIC_STREAM = [
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
                                          "stop_reason": None, "stop_sequence": None,
                                          "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100,
                                                    "cache_creation": {"ephemeral_5m_input_tokens": 60, "ephemeral_1h_input_tokens": 40}}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me look."}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Listing."}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Ls", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": "}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\".\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


def anthropic_provider(handler, base_url="https://api.anthropic.com", key="sk-ant-real", **kw) -> AnthropicProvider:
    hx = _anthropic_httpx()
    from anthropic import DefaultHttpxClient
    client = DefaultHttpxClient(transport=hx.MockTransport(handler))
    return AnthropicProvider(Settings(base_url=base_url, api_key=SecretStr(key)), http_client=client, **kw)


def test_anthropic_stream_thinking_text_tool_use_and_cache_usage():
    hx = _anthropic_httpx()
    seen: list = []

    def handler(request):
        seen.append(request)
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(ANTHROPIC_STREAM))

    prov = anthropic_provider(handler)
    system = [{"type": "text", "text": "You are Axon."}]
    msgs = []
    for i in range(6):  # six markers from an over-eager context manager
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": f"m{i}", "cache_control": {"type": "ephemeral"}}]})
    events = list(prov.stream(model="claude-opus-5-5", system=system, messages=msgs + [{"role": "user", "content": "go"}],
                              tools=[{"type": "function", "function": {"name": "Ls", "description": "list", "parameters": {"type": "object"}}}],
                              max_tokens=200_000, effort="quantum", thinking=True))
    turn = prov.finalize()

    assert [type(e) for e in events[:2]] == [ThinkingDelta, TextDelta]
    assert any(isinstance(e, ToolUseStart) and e.id == "toolu_1" for e in events)
    assert "".join(e.fragment for e in events if isinstance(e, ToolArgsDelta)) == '{"path": "."}'
    assert turn.stop_reason == "tool_use"
    assert turn.tool_uses == [ToolUseBlock(id="toolu_1", name="Ls", input={"path": "."})]
    assert turn.thinking == "Let me look." and turn.blocks[0].signature == "sig123"
    assert turn.usage.input == 1010 and turn.usage.cache_read == 900 and turn.usage.cache_write == 100
    assert turn.usage.cache_write_1h == 40 and turn.usage.output == 42
    assert turn.native[0]["type"] == "thinking" and turn.native[0]["signature"] == "sig123"
    assert turn.model == "claude-opus-5-5"

    req = seen[0]
    assert req.headers["x-api-key"] == "sk-ant-real" and "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["max_tokens"] == 128_000  # clamped to the model's output cap
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "xhigh"}
    assert body["tools"][0]["name"] == "Ls" and "input_schema" in body["tools"][0]
    markers = json.dumps(body).count("cache_control")
    assert markers <= 4


def test_anthropic_retries_overloaded_then_succeeds(_clean_env):
    hx = _anthropic_httpx()
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return hx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(ANTHROPIC_STREAM))

    events = list(anthropic_provider(handler).stream(model="claude-sonnet-4-5", system=[], messages=[{"role": "user", "content": "x"}],
                                                       tools=[], max_tokens=4000, effort="synapse", thinking=True))
    assert len(calls) == 2 and _clean_env  # slept once
    assert any(type(e).__name__ in ("RetryEvent", "ProviderRetry") and "529" in e.error for e in events)


def test_anthropic_401_classified():
    hx = _anthropic_httpx()
    prov = anthropic_provider(lambda r: hx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-haiku-4-5", system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=100))
    assert exc.value.status == 401 and "ANTHROPIC_API_KEY" in str(exc.value)


def test_anthropic_gateway_uses_bearer_and_prefix_route():
    hx = _anthropic_httpx()
    seen = []

    def handler(request):
        seen.append(request)
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(ANTHROPIC_STREAM))

    prov = anthropic_provider(handler, base_url="https://gateway.example.com", key="gw-key", model_prefix="anthropic/")
    list(prov.stream(model="anthropic/claude-opus-4-8", system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=100))
    assert seen[0].headers["authorization"] == "Bearer gw-key"
    assert json.loads(seen[0].content)["model"] == "claude-opus-4-8"


@pytest.mark.parametrize("model,mode", [
    ("claude-opus-5-5", "always_on"), ("claude-fable-5-1", "always_on"), ("claude-opus-4-8", "adaptive"),
    ("claude-sonnet-4-6", "adaptive"), ("claude-sonnet-4-5-20250929", "budget"), ("claude-haiku-4-5", "budget"),
    ("claude-3-7-sonnet-20250219", "budget"), ("claude-3-5-haiku-20241022", "none"), ("anthropic/claude-sonnet-4.5", "budget"),
])
def test_thinking_mode_families(model, mode):
    assert thinking_mode(model) == mode


def test_thinking_params_per_family():
    assert build_thinking_params("claude-sonnet-4-5", thinking=True, effort="balanced", max_tokens=32_000) == {
        "thinking": {"type": "enabled", "budget_tokens": 6_000}}
    assert build_thinking_params("claude-haiku-4-5", thinking=True, effort="quantum", max_tokens=4_000)["thinking"]["budget_tokens"] == 2_976
    assert build_thinking_params("claude-opus-4-6", thinking=False, effort="quantum", max_tokens=8_000) == {"output_config": {"effort": "max"}}
    assert build_thinking_params("claude-opus-4-5", thinking=False, effort="quantum", max_tokens=8_000) == {"output_config": {"effort": "high"}}
    assert build_thinking_params("claude-3-5-haiku-20241022", thinking=True, effort="high", max_tokens=8_000) == {}
    assert build_thinking_params("claude-opus-5-5", thinking=False, effort="reflex", max_tokens=8_000) == {"output_config": {"effort": "low"}}


def test_to_anthropic_messages_converts_openai_history_and_stubs_dangling_tools():
    out = to_anthropic_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok", "reasoning_content": "x",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Ls", "arguments": "{\"path\": \".\"}"}},
                        {"id": "c2", "type": "function", "function": {"name": "Ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
        {"role": "assistant", "content": ""},
    ])
    assert out[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "Ls", "input": {"path": "."}}
    results = out[2]["content"]
    assert {b["tool_use_id"] for b in results} == {"c1", "c2"}
    assert any(b.get("is_error") for b in results if b["tool_use_id"] == "c2")
    assert len(out) == 3  # empty assistant dropped


def test_limit_cache_breakpoints_adds_system_marker_when_missing():
    system, tools, msgs = limit_cache_breakpoints([{"type": "text", "text": "s"}], [], [{"role": "user", "content": "x"}])
    assert system[0]["cache_control"] == {"type": "ephemeral"}


# ─── Routing, keys, aliases, failover ───────────────────────────────────────
def test_resolve_api_key_prefers_provider_env_and_never_leaks(monkeypatch):
    settings = Settings(base_url="https://agentrouter.org", api_key=SecretStr("ar-key"))
    assert resolve_api_key("agentrouter", settings) == "ar-key"
    assert resolve_api_key("openai", settings) == ""  # AgentRouter key is not sent to OpenAI
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert resolve_api_key("https://api.openai.com/v1", settings) == "sk-openai"
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert resolve_api_key(get_preset_by_id("gemini"), settings) == "g-key"
    custom = Settings(base_url="https://llm.internal.example/v1", api_key=SecretStr("c-key"))
    assert resolve_api_key(None, custom) == "c-key"


def test_provider_for_explicit_prefix_routes_to_vendor(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    settings = Settings(base_url="https://agentrouter.org", api_key=SecretStr("ar-key"))
    a = provider_for("anthropic/claude-sonnet-4-5", settings)
    assert isinstance(a, AnthropicProvider) and a.settings.base_url == "https://api.anthropic.com"
    assert a.settings.api_key.get_secret_value() == "sk-ant" and a._wire_model("anthropic/claude-sonnet-4-5") == "claude-sonnet-4-5"
    o = provider_for("openai/gpt-5", settings)
    assert isinstance(o, OpenAICompatProvider) and o._url == "https://api.openai.com/v1/chat/completions"
    assert o._wire_model("openai/gpt-5") == "gpt-5"
    # OpenRouter ids keep their namespace
    orr = provider_for("openai/gpt-4o", Settings(base_url="https://openrouter.ai/api/v1", api_key=SecretStr("or")))
    assert orr._wire_model("openai/gpt-4o") == "openai/gpt-4o" and "openrouter.ai" in orr._url
    # Groq hosts "openai/gpt-oss-120b" natively
    g = provider_for("openai/gpt-oss-120b", Settings(base_url="https://api.groq.com/openai/v1", api_key=SecretStr("gq")))
    assert g._wire_model("openai/gpt-oss-120b") == "openai/gpt-oss-120b"


def test_explicit_settings_provider_wins(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gq")
    settings = Settings(base_url="https://agentrouter.org", api_key=SecretStr("ar")).model_copy(update={"provider": "groq"})
    route = resolve_route("llama-3.3-70b-versatile", settings)
    assert route.provider_id == "groq" and route.base_url == "https://api.groq.com/openai/v1" and route.api_key == "gq"


def test_model_aliases_resolve_per_provider(monkeypatch):
    ar = Settings(base_url="https://agentrouter.org", api_key=SecretStr("ar"))
    p = provider_for("opus", ar)
    assert p._wire_model("opus") == "claude-opus-5"  # AgentRouter's Opus
    assert pricing.price_for("opus").input == Decimal("6.0")
    s = provider_for("sonnet", ar)
    assert isinstance(s, AnthropicProvider) and s._wire_model("sonnet") == "claude-sonnet-5-5"
    assert resolve_model_alias("opusplan", mode="plan") == "claude-opus-5-5"
    assert resolve_model_alias("opusplan", mode="default") == "claude-sonnet-5-5"
    assert get_context_window("claude-sonnet-4-5[1m]") == 1_000_000
    assert get_context_window("claude-sonnet-4-5") == 200_000
    assert get_context_window("totally-unknown") == 128_000


def test_find_preset_for_model_families():
    assert find_preset_for_model("anthropic/claude-sonnet-4-5").id == "anthropic"
    assert find_preset_for_model("grok-4").id == "xai"
    assert find_preset_for_model("codestral-latest").id == "mistral"
    assert find_preset_for_model("deepseek-reasoner").id == "deepseek"
    assert find_preset_for_model("meta-llama/llama-3.3-70b-instruct").id == "openrouter"
    assert find_preset_by_url("https://foo.openai.azure.com").id == "azure"
    assert find_preset_by_url("http://127.0.0.1:1234/v1").id == "lmstudio"


def test_fallback_chain_parsing():
    s = Settings().model_copy(update={"fallback_model": "gpt-5, anthropic:claude-haiku-4-5", "routing": {"main": ["ollama:qwen2.5-coder:7b", "gpt-5"]}})
    assert fallback_chain(s, primary="deepseek-v4-flash") == ["gpt-5", "anthropic/claude-haiku-4-5", "ollama/qwen2.5-coder:7b"]


def test_failover_switches_model_on_overload(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "deepseek-v4-flash":
            return httpx.Response(529, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": f"from {model}"}, "finish_reason": "stop"}],
                                                "usage": {"prompt_tokens": 5, "completion_tokens": 2}}))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "stream", client.stream)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    settings = Settings(base_url="https://agentrouter.org", api_key=SecretStr("ar")).model_copy(update={"fallback_model": "openai/gpt-5"})
    prov = provider_for("deepseek-v4-flash", settings)
    assert prov.name == "openai_compat"
    events = list(prov.stream(model="deepseek-v4-flash", system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=50))
    turn = prov.finalize()
    assert turn.text == "from gpt-5" and turn.model == "gpt-5"
    assert calls.count("deepseek-v4-flash") == 4 and calls[-1] == "gpt-5"
    hops = [e for e in events if type(e).__name__ in ("RetryEvent", "ProviderRetry") and "failover" in e.error]
    assert len(hops) == 1 and "529" in hops[0].error


# ─── Pricing ────────────────────────────────────────────────────────────────
def test_cost_for_accepts_usage_or_dict_and_matches_formula():
    u = Usage(input=39365, output=4886, cache_read=39168)
    expected = Decimal("0.0907544")
    assert pricing.cost_for("deepseek-v4-flash", u) == expected
    assert pricing.cost_for("deepseek-v4-flash", {"input": 39365, "output": 4886, "cache_read": 39168, "cache_write": 0}) == expected


def test_pricing_families_local_unknown_and_cache_writes():
    assert pricing.price_for("claude-sonnet-4-5-20250929", "anthropic").input == Decimal("3.0")
    assert pricing.price_for("gpt-4o-mini-2024-07-18").output == Decimal("0.6")
    assert pricing.price_for("o4-mini").cache_read == Decimal("0.275")
    local = pricing.cost_details("qwen2.5-coder:7b", Usage(input=10_000, output=1_000))
    assert local.cost == 0 and local.priced and local.quote.local
    unknown = pricing.cost_details("nobody-knows-v9", Usage(input=1_000_000, output=1_000_000))
    assert unknown.cost == 0 and unknown.priced is False
    # 1M uncached + 1M 5m writes + 1M 1h writes on Sonnet 4.5: 3 + 3.75 + 6
    res = pricing.cost_details("claude-sonnet-4-5", {"input": 3_000_000, "cache_write": 2_000_000, "cache_write_1h": 1_000_000}, provider="anthropic")
    assert res.cost == Decimal("12.75")
    assert res.parts()["cache_write"] == Decimal("9.75") and res.parts()["input"] == Decimal("3")
    assert pricing.cost_for("claude-opus-5", Usage(input=1_000_000), provider="anthropic") == Decimal("5")
    assert pricing.cost_for("claude-opus-5", Usage(input=1_000_000)) == Decimal("6")  # AgentRouter default


def test_agentrouter_feed_never_overrides_verified_rates(monkeypatch):
    feed = {"data": [{"model_name": "gpt-6-astra", "model_ratio": 3, "completion_ratio": 5},
                     {"model_name": "brand-new-model", "model_ratio": 0.5, "completion_ratio": 4}]}
    assert pricing.apply_agentrouter_pricing_feed(feed) == 1
    assert PRICING["gpt-6-astra"]["input"] == 3.0
    assert pricing.price_for("brand-new-model").input == Decimal("1.0")
    assert pricing.price_for("brand-new-model").output == Decimal("4.0")


def test_pricing_sync_is_disabled_under_pytest(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert pricing.sync_agentrouter_pricing("https://agentrouter.org") is False
    provider_for("deepseek-v4-flash", Settings(base_url="https://agentrouter.org", api_key=SecretStr("k")))


# ─── Ledger ─────────────────────────────────────────────────────────────────
def test_ledger_breakdown_contract_and_roundtrip():
    led = Ledger()
    led.record("claude-opus-5", Usage(input=1000, output=100, cache_read=500), source="main")
    led.record("claude-opus-5", Usage(input=200, output=20), "main")
    led.record("deepseek-v4-flash", Usage(input=50, output=5), source="subagent:explorer")
    led.record("mystery", Usage(input=10, output=1), tag="side_question")
    rows = led.breakdown()
    keys = {"source", "model", "calls", "input", "output", "cache_read", "cache_write", "cost"}
    assert all(keys <= set(r) for r in rows)
    main = next(r for r in rows if r["source"] == "main")
    assert main["calls"] == 2 and main["input"] == 1200 and main["cache_read"] == 500
    assert next(r for r in rows if r["model"] == "mystery")["priced"] is False
    assert "Subagent explorer" in led.render("claude-opus-5") and "Unpriced models" in led.render()
    clone = Ledger.from_dict(json.loads(json.dumps(led.to_dict())))
    assert clone.total_cost == led.total_cost and len(clone.entries) == 4
    assert clone.breakdown() == led.breakdown()
    assert led.totals()["unpriced_models"] == ["mystery"]


def test_ledger_savings_pct_never_none_and_thread_safe():
    led = Ledger()
    assert led.savings_pct("unknown-model") == 0.0
    led.total_input_tokens = 100  # aggregated totals with no entries (workspace ledger)
    assert isinstance(led.savings_pct("claude-opus-5"), float)

    led2 = Ledger()

    def work():
        for _ in range(200):
            led2.record("gpt-4o", Usage(input=10, output=1), source="subagent_1")

    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(led2.entries) == 1600 and led2.total_input_tokens == 16000
    assert sorted(e.id for e in led2.entries) == list(range(1, 1601))


# ─── Keys on disk & verifier ────────────────────────────────────────────────
def test_find_and_save_api_key_exact_names(monkeypatch):
    from axon.providers.catalog import axon_env_file
    env_file = axon_env_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text('OPENAI_API_KEY_OLD="stale"\nexport GROQ_API_KEY=gq-file\n', encoding="utf-8")
    assert find_api_key(["OPENAI_API_KEY"]) is None  # prefix must not match
    assert find_api_key(["GROQ_API_KEY"]) == "gq-file"
    monkeypatch.setenv("GROQ_API_KEY", "gq-env")
    assert find_api_key(["GROQ_API_KEY"]) == "gq-env"
    path = save_api_key("OPENAI_API_KEY", "sk-new")
    text = path.read_text(encoding="utf-8")
    assert 'OPENAI_API_KEY="sk-new"' in text and 'OPENAI_API_KEY_OLD="stale"' in text
    save_api_key("OPENAI_API_KEY", "sk-newer")
    assert path.read_text(encoding="utf-8").count("OPENAI_API_KEY=") == 1
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(ValueError):
        save_api_key("BAD NAME", "x")


def test_verifier_builds_vendor_specific_requests():
    url, headers, body = build_ping_request(get_preset_by_id("anthropic"), "k", "https://api.anthropic.com", "claude-haiku-4-5")
    assert url == "https://api.anthropic.com/v1/messages" and headers["x-api-key"] == "k" and "authorization" not in headers
    url, headers, body = build_ping_request(get_preset_by_id("azure"), "k", "https://r.openai.azure.com", "dep")
    assert "/openai/deployments/dep/chat/completions" in url and headers["api-key"] == "k"
    url, headers, body = build_ping_request(get_preset_by_id("openai"), "k", "https://api.openai.com/v1", "gpt-5")
    assert "max_completion_tokens" in body and headers["authorization"] == "Bearer k"


def test_retry_after_parsing():
    assert transport.parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert transport.parse_retry_after({"retry-after": "3"}) == 3.0
    assert transport.parse_retry_after({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0
    assert transport.parse_retry_after({}) is None
    assert transport.backoff_delay(0, 120.0) == transport.MAX_RETRY_AFTER_S
    assert 0.25 <= transport.backoff_delay(1) <= 1.0


def test_anthropic_gateway_cache_control_rejection_moves_marker_to_last_message():
    hx = _anthropic_httpx()
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return hx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "cache_control not allowed here"}})
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(ANTHROPIC_STREAM))

    prov = anthropic_provider(handler, base_url="https://gateway.example.com")
    list(prov.stream(model="claude-opus-4-8", system=[{"type": "text", "text": "s"}], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=100))
    second = bodies[1]
    assert "cache_control" not in json.dumps(second["system"])
    assert second["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
