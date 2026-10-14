"""
E2E-C: provider wire formats through the real CLI against the (extended) fake LLM.

OpenAI chat-completions streaming (tool_call deltas split across chunks, parallel tool calls,
DeepSeek-style reasoning_content, finish_reason "stop" with tool calls) and Anthropic Messages
(thinking blocks + signature replay, cache_control breakpoints, auth headers, cache usage).
"""
from __future__ import annotations

import json

import pytest

from fake_llm import Recorded
from harness import AxonEnv
from test_e2e_c_support import ext_llm, has_tool_result, no_traceback, xenv  # noqa: F401

pytestmark = pytest.mark.e2e


def _stream_events(r) -> list[dict]:
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]


# ─── OpenAI wire ────────────────────────────────────────────────────────────

def test_openai_tool_call_header_and_args_split_across_many_chunks(xenv: AxonEnv) -> None:
    """id, name and 3-char argument fragments arrive in separate SSE chunks; the call still executes."""
    xenv.llm.enqueue(
        [{"split_tool_header": True}, {"tool": "Write", "args": {"path": "split.txt", "content": "split-ok\n"}}],
        {"steps": [{"text": "written"}], "expect": [has_tool_result()]},
    )
    r = xenv.run(["-p", "write split.txt"])
    assert r.code == 0, str(r)
    assert (xenv.workspace / "split.txt").read_text(encoding="utf-8") == "split-ok\n"
    assert not xenv.llm.failures, xenv.llm.failures


def test_openai_parallel_tool_calls_all_execute_and_results_match_ids(xenv: AxonEnv) -> None:
    """Three tool_calls (index 0,1,2) in one response: all run, each result carries its tool_call_id."""
    xenv.llm.enqueue(
        [{"tool": "Write", "id": "call_a", "args": {"path": "p/a.txt", "content": "A"}},
         {"tool": "Write", "id": "call_b", "args": {"path": "p/b.txt", "content": "B"}},
         {"tool": "Write", "id": "call_c", "args": {"path": "p/c.txt", "content": "C"}}],
        [{"text": "all three written"}],
    )
    r = xenv.run(["-p", "--output-format", "json", "write three files"])
    assert r.code == 0, str(r)
    for n, c in (("a", "A"), ("b", "B"), ("c", "C")):
        assert (xenv.workspace / "p" / f"{n}.txt").read_text(encoding="utf-8") == c
    assert r.json()["tool_calls"] == 3
    second = xenv.llm.completion_requests()[1]
    asst = [m for m in second.messages if m.get("role") == "assistant"][-1]
    assert [tc["id"] for tc in asst["tool_calls"]] == ["call_a", "call_b", "call_c"]
    tool_msgs = [m for m in second.messages if m.get("role") == "tool"]
    assert sorted(m["tool_call_id"] for m in tool_msgs) == ["call_a", "call_b", "call_c"]


def test_openai_finish_reason_stop_with_tool_calls_still_runs_tools(xenv: AxonEnv) -> None:
    """Gemini/Ollama style: finish_reason "stop" although tool_calls were streamed -> tools still run."""
    xenv.llm.enqueue(
        [{"finish": "stop"}, {"tool": "Write", "args": {"path": "stop.txt", "content": "stop-ok"}}],
        {"steps": [{"text": "done after stop"}], "expect": [has_tool_result()]},
    )
    r = xenv.run(["-p", "--output-format", "json", "go"])
    assert r.code == 0, str(r)
    assert (xenv.workspace / "stop.txt").read_text(encoding="utf-8") == "stop-ok"
    data = r.json()
    assert data["tool_calls"] == 1 and data["iterations"] == 2
    assert data["result"].strip() == "done after stop"


def test_openai_tool_call_deltas_without_index_field(xenv: AxonEnv) -> None:
    """Some servers omit `index` on tool_call deltas; continuation fragments must join the open call."""
    xenv.llm.enqueue(
        [{"raw_openai": [
            {"tool_calls": [{"id": "call_noidx", "type": "function",
                             "function": {"name": "Write", "arguments": "{\"path\": \"noidx.txt\", "}}]},
            {"tool_calls": [{"function": {"arguments": "\"content\": \"no-index\"}"}}]},
        ]}],
        [{"text": "ok"}],
    )
    r = xenv.run(["-p", "go"])
    assert r.code == 0, str(r)
    assert (xenv.workspace / "noidx.txt").read_text(encoding="utf-8") == "no-index"


def test_openai_tool_arguments_sent_as_json_object(xenv: AxonEnv) -> None:
    """`function.arguments` delivered as an object instead of a string is accepted."""
    xenv.llm.enqueue(
        [{"raw_openai": [
            {"tool_calls": [{"index": 0, "id": "call_obj", "type": "function",
                             "function": {"name": "Write", "arguments": {"path": "obj.txt", "content": "obj"}}}]},
        ]}],
        [{"text": "ok"}],
    )
    r = xenv.run(["-p", "go"])
    assert r.code == 0, str(r)
    assert (xenv.workspace / "obj.txt").read_text(encoding="utf-8") == "obj"


def test_deepseek_reasoning_content_streams_as_thinking(xenv: AxonEnv) -> None:
    """DeepSeek `reasoning_content` deltas become thinking_delta events, separate from the answer."""
    xenv.llm.enqueue([{"thinking": "Let me reason about primes carefully."}, {"text": "The answer is 7."}])
    r = xenv.run(["-p", "--output-format", "stream-json", "smallest prime > 5"],
                 env={"AXON_PROVIDER": "deepseek", "AXON_MODEL": "deepseek-reasoner"})
    assert r.code == 0, str(r)
    ev = _stream_events(r)
    thinking = "".join(e.get("text", "") for e in ev if e.get("type") == "thinking_delta")
    assert thinking == "Let me reason about primes carefully.", [e.get("type") for e in ev]
    assert ev[-1]["type"] == "result" and ev[-1]["result"].strip() == "The answer is 7."
    assert "reason about primes" not in ev[-1]["result"]


def test_deepseek_reasoning_not_shown_with_no_thinking(xenv: AxonEnv) -> None:
    """--no-thinking suppresses reasoning_content deltas from the event stream."""
    xenv.llm.enqueue([{"thinking": "hidden chain of thought"}, {"text": "visible answer"}])
    r = xenv.run(["-p", "--no-thinking", "--output-format", "stream-json", "q"],
                 env={"AXON_PROVIDER": "deepseek", "AXON_MODEL": "deepseek-chat"})
    assert r.code == 0, str(r)
    ev = _stream_events(r)
    assert not [e for e in ev if e.get("type") == "thinking_delta"]
    assert ev[-1]["result"].strip() == "visible answer"


def test_strict_provider_strips_reasoning_content_from_replayed_history(xenv: AxonEnv) -> None:
    """For strict presets (OpenAI) the assistant's reasoning_content is not echoed back in the next request."""
    xenv.llm.enqueue(
        [{"thinking": "private reasoning"}, {"tool": "Bash", "args": {"command": "echo hi", "description": "e"}}],
        [{"text": "final"}],
    )
    r = xenv.run(["-p", "go"], env={"AXON_PROVIDER": "openai", "AXON_MODEL": "gpt-4o"})
    assert r.code == 0, str(r)
    second = xenv.llm.completion_requests()[1]
    for m in second.messages:
        assert "reasoning_content" not in m, m
    assert "private reasoning" not in second.all_text()


def test_openai_request_shape_stream_usage_and_tools(xenv: AxonEnv) -> None:
    """Chat request: stream=true, stream_options.include_usage, function tools, system first."""
    xenv.llm.enqueue([{"text": "shape ok"}])
    r = xenv.run(["-p", "hi"])
    assert r.code == 0, str(r)
    req = xenv.llm.completion_requests()[0]
    assert req.path.endswith("/v1/chat/completions")
    assert req.body["stream"] is True
    assert req.body.get("stream_options") == {"include_usage": True}
    assert all(t.get("type") == "function" and "parameters" in t["function"] for t in req.body["tools"])
    assert req.messages[0]["role"] in ("system", "developer")
    assert req.messages[-1] == {"role": "user", "content": req.messages[-1]["content"]}


def test_mistral_preset_omits_stream_options(xenv: AxonEnv) -> None:
    """Mistral rejects stream_options; the mistral preset must not send it."""
    xenv.llm.enqueue([{"text": "bonjour"}])
    r = xenv.run(["-p", "hi"], env={"AXON_PROVIDER": "mistral", "AXON_MODEL": "mistral-large-latest",
                                    "MISTRAL_API_KEY": "mk-1"})
    assert r.code == 0, str(r)
    req = xenv.llm.completion_requests()[0]
    assert "stream_options" not in req.body
    assert req.headers.get("authorization") == "Bearer mk-1"


def test_openai_cached_tokens_reported_as_cache_read(xenv: AxonEnv) -> None:
    """prompt_tokens_details.cached_tokens -> usage.cache_read_tokens in --output-format json."""
    xenv.llm.enqueue([{"text": "c"}, {"usage": {"input": 9000, "output": 12, "cached": 6000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"])
    assert r.code == 0, str(r)
    u = r.json()["usage"]
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_tokens"], u["cache_write_tokens"]) == (9000, 12, 6000, 0)


def test_openai_multibyte_text_split_across_chunks_is_intact(xenv: AxonEnv) -> None:
    """Non-ASCII text (CJK, emoji, accents) chunked by the server is reassembled byte-exact."""
    text = "Grüße 你好世界 🚀✨ café — naïve"
    xenv.llm.enqueue([{"text": text}])
    r = xenv.run(["-p", "--output-format", "json", "hi"])
    assert r.code == 0, str(r)
    assert r.json()["result"].strip() == text


def test_openai_tool_error_result_is_fed_back(xenv: AxonEnv) -> None:
    """A failing tool (Read of a missing file) is returned to the model as an error tool message."""
    xenv.llm.enqueue(
        [{"tool": "Read", "args": {"path": "does/not/exist.txt"}}],
        {"steps": [{"text": "file missing"}], "expect": [has_tool_result()]},
    )
    r = xenv.run(["-p", "read it"])
    assert r.code == 0, str(r)
    second = xenv.llm.completion_requests()[1]
    tool_msg = [m for m in second.messages if m.get("role") == "tool"][0]
    assert "error" in tool_msg["content"].lower() or "not found" in tool_msg["content"].lower() \
        or "no such" in tool_msg["content"].lower(), tool_msg


# ─── Anthropic wire ─────────────────────────────────────────────────────────

ANTH = {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"}


def test_anthropic_thinking_block_streams_as_thinking_delta(xenv: AxonEnv) -> None:
    """thinking_delta events surface as stream-json thinking_delta; the result holds only the text."""
    xenv.llm.enqueue([{"thinking": "Considering the options step by step.", "signature": "sig-1"},
                      {"text": "Pick option B."}])
    r = xenv.run(["-p", "--output-format", "stream-json", "choose"], env=ANTH)
    assert r.code == 0, str(r)
    ev = _stream_events(r)
    thinking = "".join(e.get("text", "") for e in ev if e.get("type") == "thinking_delta")
    assert thinking == "Considering the options step by step."
    assert ev[-1]["result"].strip() == "Pick option B."
    assert xenv.llm.completion_requests()[0].wire == "anthropic"


def test_anthropic_thinking_signature_replayed_with_tool_use(xenv: AxonEnv) -> None:
    """A thinking block preceding tool_use is sent back verbatim (with signature) in the next request."""
    xenv.llm.enqueue(
        [{"thinking": "I should list files.", "signature": "sig-REPLAY-42"},
         {"tool": "Bash", "args": {"command": "echo anth", "description": "echo"}}],
        {"steps": [{"text": "listed"}], "expect": [has_tool_result("anth")]},
    )
    r = xenv.run(["-p", "list"], env=ANTH)
    assert r.code == 0, str(r)
    assert not xenv.llm.failures, xenv.llm.failures
    second = xenv.llm.completion_requests()[1]
    asst = [m for m in second.messages if m.get("role") == "assistant"][-1]
    blocks = asst["content"]
    assert blocks[0]["type"] == "thinking", blocks
    assert blocks[0]["thinking"] == "I should list files." and blocks[0]["signature"] == "sig-REPLAY-42"
    assert any(b["type"] == "tool_use" for b in blocks)


def test_anthropic_cache_control_markers_at_most_four_every_request(xenv: AxonEnv) -> None:
    """Across a 4-request tool loop each request carries 1..4 cache_control breakpoints (API limit 4)."""
    xenv.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo 1", "description": "1"}}],
        [{"tool": "Bash", "args": {"command": "echo 2", "description": "2"}}],
        [{"tool": "Bash", "args": {"command": "echo 3", "description": "3"}}],
        [{"text": "done"}],
    )
    r = xenv.run(["-p", "loop"], env=ANTH)
    assert r.code == 0, str(r)
    reqs = xenv.llm.completion_requests()
    assert len(reqs) == 4
    for q in reqs:
        n = q.all_text().count('"cache_control"')
        assert 1 <= n <= 4, (n, q.path)


def test_anthropic_gateway_uses_bearer_auth(xenv: AxonEnv) -> None:
    """A non-official Anthropic-compatible host (fake server, provider forced) gets Authorization: Bearer."""
    xenv.llm.enqueue([{"text": "gw"}])
    r = xenv.run(["-p", "hi"], env={**ANTH, "ANTHROPIC_API_KEY": "sk-ant-gw-9"})
    assert r.code == 0, str(r)
    h = xenv.llm.completion_requests()[0].headers
    assert h.get("authorization") == "Bearer sk-ant-gw-9", h
    assert h.get("anthropic-version"), h


def test_anthropic_official_host_uses_x_api_key_only() -> None:
    """For api.anthropic.com the SDK client authenticates with x-api-key and no Bearer header."""
    from pydantic import SecretStr

    from axon.config import Settings
    from axon.providers.anthropic import AnthropicProvider
    s = Settings(api_key=SecretStr("sk-ant-official"), base_url="https://api.anthropic.com", model="claude-sonnet-4-6")
    prov = AnthropicProvider(s)
    headers = {k.lower(): v for k, v in prov._client.auth_headers.items()}
    assert headers.get("x-api-key") == "sk-ant-official"
    assert "authorization" not in headers


def test_anthropic_cache_write_and_read_usage_reported(xenv: AxonEnv) -> None:
    """cache_creation_input_tokens and cache_read_input_tokens land in json usage (input = total prompt)."""
    xenv.llm.enqueue([{"text": "cached"}, {"usage": {"input": 30000, "output": 50, "cached": 12000, "cache_write": 8000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=ANTH)
    assert r.code == 0, str(r)
    u = r.json()["usage"]
    assert u == {"input_tokens": 30000, "output_tokens": 50, "cache_read_tokens": 12000, "cache_write_tokens": 8000}, u


def test_anthropic_parallel_tool_results_in_one_user_message(xenv: AxonEnv) -> None:
    """Two tool_use blocks -> both tool_result blocks in a single user message, ids matching."""
    xenv.llm.enqueue(
        [{"tool": "Write", "id": "toolu_one", "args": {"path": "one.txt", "content": "1"}},
         {"tool": "Write", "id": "toolu_two", "args": {"path": "two.txt", "content": "2"}}],
        [{"text": "both"}],
    )
    r = xenv.run(["-p", "two files"], env=ANTH)
    assert r.code == 0, str(r)
    second = xenv.llm.completion_requests()[1]
    last_user = second.messages[-1]
    assert last_user["role"] == "user"
    ids = [b["tool_use_id"] for b in last_user["content"] if b.get("type") == "tool_result"]
    assert sorted(ids) == ["toolu_one", "toolu_two"]


def test_anthropic_no_thinking_flag_omits_thinking_param(xenv: AxonEnv) -> None:
    """--no-thinking: the Messages request carries no `thinking` field."""
    xenv.llm.enqueue([{"text": "plain"}])
    r = xenv.run(["-p", "--no-thinking", "hi"], env=ANTH)
    assert r.code == 0, str(r)
    assert "thinking" not in xenv.llm.completion_requests()[0].body


def test_anthropic_system_is_block_list_and_tools_have_input_schema(xenv: AxonEnv) -> None:
    """Native request shape: system as text blocks, tools with input_schema, no system role in messages."""
    xenv.llm.enqueue([{"text": "shape"}])
    r = xenv.run(["-p", "hi"], env=ANTH)
    assert r.code == 0, str(r)
    body = xenv.llm.completion_requests()[0].body
    assert isinstance(body["system"], list) and body["system"][0]["type"] == "text"
    assert all("input_schema" in t and "name" in t for t in body["tools"])
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])
    assert body["max_tokens"] > 0


# ─── provider-prefixed model ids ────────────────────────────────────────────

def test_anthropic_prefixed_model_id_is_stripped_on_the_wire(xenv: AxonEnv) -> None:
    """--model anthropic/claude-sonnet-4-6 with --provider anthropic sends model 'claude-sonnet-4-6'."""
    xenv.llm.enqueue([{"text": "pref"}])
    r = xenv.run(["-p", "--provider", "anthropic", "--model", "anthropic/claude-sonnet-4-6", "hi"])
    assert r.code == 0, str(r)
    req = xenv.llm.completion_requests()[0]
    assert req.wire == "anthropic" and req.body["model"] == "claude-sonnet-4-6"


def test_openai_prefixed_model_id_is_stripped_on_the_wire(xenv: AxonEnv) -> None:
    """--model openai/gpt-4o with --provider openai sends model 'gpt-4o' on chat/completions."""
    xenv.llm.enqueue([{"text": "pref"}])
    r = xenv.run(["-p", "--provider", "openai", "--model", "openai/gpt-4o", "hi"])
    assert r.code == 0, str(r)
    req = xenv.llm.completion_requests()[0]
    assert req.wire == "openai" and req.body["model"] == "gpt-4o"


def test_openrouter_namespaced_model_id_is_kept(xenv: AxonEnv) -> None:
    """OpenRouter ids are namespaced: 'anthropic/claude-sonnet-4.5' is sent unchanged over the OpenAI wire."""
    xenv.llm.enqueue([{"text": "or"}])
    r = xenv.run(["-p", "hi"], env={"AXON_PROVIDER": "openrouter", "AXON_MODEL": "anthropic/claude-sonnet-4.5",
                                    "OPENROUTER_API_KEY": "or-key"})
    assert r.code == 0, str(r)
    req = xenv.llm.completion_requests()[0]
    assert req.wire == "openai" and req.body["model"] == "anthropic/claude-sonnet-4.5"
    assert req.headers.get("authorization") == "Bearer or-key"
    assert req.headers.get("x-title") == "Axon"


def test_model_alias_sonnet_resolves_on_anthropic_wire(xenv: AxonEnv) -> None:
    """--model sonnet with --provider anthropic resolves to the newest Sonnet id."""
    from axon.providers.catalog import MODEL_ALIASES
    xenv.llm.enqueue([{"text": "alias"}])
    r = xenv.run(["-p", "--provider", "anthropic", "--model", "sonnet", "hi"])
    assert r.code == 0, str(r)
    assert xenv.llm.completion_requests()[0].body["model"] == MODEL_ALIASES["sonnet"]


def test_openai_length_truncated_tool_call_is_dropped_not_executed(xenv: AxonEnv) -> None:
    """finish_reason "length" mid tool-arguments: the half call is dropped, nothing runs, no crash."""
    xenv.llm.enqueue(
        [{"finish": "length"}, {"raw_openai": [
            {"tool_calls": [{"index": 0, "id": "call_trunc", "type": "function",
                             "function": {"name": "Write", "arguments": "{\"path\": \"trunc.txt\", \"cont"}}]}]}],
        [{"text": "recovered"}],
    )
    r = xenv.run(["-p", "--output-format", "json", "go"])
    no_traceback(r)
    assert not (xenv.workspace / "trunc.txt").exists()
    assert r.json()["tool_calls"] == 0


def test_openai_midstream_error_chunk_is_reported(xenv: AxonEnv) -> None:
    """An in-band SSE `{"error": ...}` chunk ends the turn with a readable error, not a traceback."""
    xenv.llm.enqueue([{"raw_chunks": [{"error": {"message": "upstream exploded mid-stream", "code": 500}}],
                       "stop_after": True}])
    r = xenv.run(["-p", "--output-format", "json", "go"], timeout=40)
    no_traceback(r)
    assert r.code == 1, str(r)
    assert "upstream exploded mid-stream" in r.json()["result"], r.json()


def test_openai_tool_name_split_across_chunks(xenv: AxonEnv) -> None:
    """The function name itself arrives in two fragments ("Wri" + "te")."""
    xenv.llm.enqueue(
        [{"raw_openai": [
            {"tool_calls": [{"index": 0, "id": "call_nm", "type": "function", "function": {"name": "Wri"}}]},
            {"tool_calls": [{"index": 0, "function": {"name": "te", "arguments": "{\"path\": \"nm.txt\", \"content\": \"n\"}"}}]},
        ]}],
        [{"text": "ok"}],
    )
    r = xenv.run(["-p", "go"])
    no_traceback(r)
    assert (xenv.workspace / "nm.txt").exists(), str(r)
