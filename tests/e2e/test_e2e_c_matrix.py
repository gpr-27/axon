"""
E2E-C: provider matrix. Every catalog preset, driven through the real provider classes against the
local fake LLM: streaming text, a tool call, parallel tools, usage and cache accounting, model listing,
retries on 429 / 529 and error mapping.
"""
from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from fake_llm import FakeLLMServer
from test_e2e_c_support import ExtFakeLLM  # noqa: F401  (fake with the extended wire details)

pytestmark = pytest.mark.e2e

from axon.config import Settings  # noqa: E402
from axon.providers.base import TextDelta, ToolUseComplete  # noqa: E402
from axon.providers.catalog import PROVIDER_PRESETS, get_preset_by_id  # noqa: E402
from axon.providers.endpoints import fetch_models  # noqa: E402
from axon.providers.registry import provider_for  # noqa: E402

PRESETS = [p.id for p in PROVIDER_PRESETS]
SYSTEM = [{"type": "text", "text": "You are Axon."}]
TOOLS = [{"name": "Read", "description": "Read a file.",
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


@pytest.fixture
def llm():
    server = ExtFakeLLM(model_ids=("m-one", "m-two")).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    import os
    for k in list(os.environ):
        if k.endswith("_API_KEY") or k.startswith("AXON_") or k.startswith("AZURE_OPENAI") or k == "BZN_API_KEY":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXON_OFFLINE", "1")


def _provider(pid: str, llm, monkeypatch):
    preset = get_preset_by_id(pid)
    if pid == "azure":
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", llm.url)
    settings = Settings(provider=pid, base_url=llm.url, api_key=SecretStr("key-" + pid), model=preset.default_model)
    return provider_for(preset.default_model, settings, failover=False), preset


def _run(provider, preset, text="hi", tools=True):
    events = list(provider.stream(model=preset.default_model, system=SYSTEM,
                                  messages=[{"role": "user", "content": text}],
                                  tools=list(TOOLS) if tools else [], max_tokens=200, effort=None, thinking=False))
    return events, provider.finalize()


@pytest.mark.parametrize("pid", PRESETS)
def test_matrix_streaming_text_and_usage(pid, llm, monkeypatch):
    provider, preset = _provider(pid, llm, monkeypatch)
    llm.enqueue([{"text": "hello matrix"}, {"usage": {"input": 100, "output": 7, "cached": 30}}])
    events, turn = _run(provider, preset, tools=False)
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "hello matrix"
    assert turn.text == "hello matrix" and turn.stop_reason == "end_turn"
    assert turn.usage.output == 7 and turn.usage.cache_read == 30
    assert llm.completion_requests()[0].wire == preset.api_format


@pytest.mark.parametrize("pid", PRESETS)
def test_matrix_tool_call_and_parallel_tools(pid, llm, monkeypatch):
    provider, preset = _provider(pid, llm, monkeypatch)
    if not preset.native_tools:
        pytest.skip("prompt-tools provider: tools are text, covered by test_audit_providers_prompt_tools")
    llm.enqueue([{"tool": "Read", "args": {"path": "/a"}}, {"tool": "Read", "args": {"path": "/b"}}])
    events, turn = _run(provider, preset)
    done = [e for e in events if isinstance(e, ToolUseComplete)]
    assert len(done) == 2
    uses = turn.tool_uses
    assert [u.input for u in uses] == [{"path": "/a"}, {"path": "/b"}]
    assert len({u.id for u in uses}) == 2 and turn.stop_reason == "tool_use"


@pytest.mark.parametrize("pid", [p for p in PRESETS if get_preset_by_id(p).api_format == "anthropic"] or ["anthropic"])
def test_matrix_anthropic_cache_write_counted(pid, llm, monkeypatch):
    provider, preset = _provider(pid, llm, monkeypatch)
    llm.enqueue([{"text": "x"}, {"usage": {"input": 100, "output": 5, "cached": 20, "cache_write": 10}}])
    _, turn = _run(provider, preset, tools=False)
    assert (turn.usage.cache_read, turn.usage.cache_write, turn.usage.output) == (20, 10, 5)
    assert turn.usage.input == 100  # Usage.input is the total prompt


@pytest.mark.parametrize("pid", [p for p in PRESETS if p not in ("azure",)])
def test_matrix_model_listing(pid, llm):
    preset = get_preset_by_id(pid)
    res = fetch_models(llm.url, "key", preset.api_format)
    assert not res.error and set(res.models) >= {"m-one", "m-two"}


@pytest.mark.parametrize("status", [429, 529])
@pytest.mark.parametrize("pid", PRESETS)
def test_matrix_retry_then_success(pid, status, llm, monkeypatch):
    provider, preset = _provider(pid, llm, monkeypatch)
    llm.enqueue([{"error": status, "retry_after": 0}], [{"text": "recovered"}])
    _, turn = _run(provider, preset, tools=False)
    assert turn.text == "recovered"
    assert len(llm.completion_requests()) == 2


@pytest.mark.parametrize("pid", PRESETS)
def test_matrix_auth_error_is_mapped_and_not_retried(pid, llm, monkeypatch):
    provider, preset = _provider(pid, llm, monkeypatch)
    llm.enqueue([{"error": 401}])
    with pytest.raises(Exception) as ei:
        _run(provider, preset, tools=False)
    assert len(llm.completion_requests()) == 1
    assert "401" in str(ei.value) or "key" in str(ei.value).lower()


# ─── Responses API (gpt-6 class models; settings.wire_api is not a Settings field, see report) ─────────────────────────

class _ResponsesServer:
    """Minimal /v1/responses SSE server; each POST pops the next scripted event list or status."""

    def __init__(self, scripts):
        import http.server
        import threading
        self.scripts, self.requests = list(scripts), []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                outer.requests.append((self.path, body))
                script = outer.scripts.pop(0)
                if isinstance(script, int):
                    data = json.dumps({"error": {"message": "x", "type": "e"}}).encode()
                    self.send_response(script)
                    self.send_header("retry-after", "0")
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for ev in script:
                    self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())

        import http.server as hs
        self.httpd = hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


def _responses_provider(url):
    settings = Settings(provider="openai", base_url=url, api_key=SecretStr("k"), model="gpt-6-astra")
    return provider_for("gpt-6-astra", settings, failover=False)


def _call(provider):
    ev = list(provider.stream(model="gpt-6-astra", system=SYSTEM, messages=[{"role": "user", "content": "hi"}],
                              tools=list(TOOLS), max_tokens=100, effort=None, thinking=False))
    return ev, provider.finalize()


def _completed(usage):
    return {"type": "response.completed", "response": {"status": "completed", "usage": usage}}


@pytest.fixture
def resp_server(monkeypatch):
    servers = []

    def make(*scripts):
        servers.append(_ResponsesServer(scripts))
        return servers[-1]

    yield make
    for s in servers:
        s.httpd.shutdown()


def test_responses_wire_text_usage_and_cache(resp_server):
    srv = resp_server([
        {"type": "response.output_text.delta", "delta": "hel"}, {"type": "response.output_text.delta", "delta": "lo"},
        _completed({"input_tokens": 50, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 1}})])
    _, turn = _call(_responses_provider(srv.url))
    assert srv.requests[0][0].endswith("/responses")
    assert turn.text == "hello" and (turn.usage.input, turn.usage.output, turn.usage.cache_read, turn.usage.reasoning) == (50, 4, 20, 1)


def test_responses_wire_parallel_tool_calls(resp_server):
    def item(i, cid, path):
        return [{"type": "response.output_item.added", "output_index": i, "item": {"type": "function_call", "id": f"fc{i}", "call_id": cid, "name": "Read", "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "output_index": i, "item_id": f"fc{i}", "delta": json.dumps({"path": path})},
                {"type": "response.function_call_arguments.done", "output_index": i, "item_id": f"fc{i}", "arguments": json.dumps({"path": path})}]
    srv = resp_server(item(0, "c0", "/a") + item(1, "c1", "/b") + [_completed({"input_tokens": 5, "output_tokens": 2})])
    _, turn = _call(_responses_provider(srv.url))
    assert [(u.id, u.input) for u in turn.tool_uses] == [("c0", {"path": "/a"}), ("c1", {"path": "/b"})]
    assert turn.stop_reason == "tool_use"


@pytest.mark.parametrize("status", [429, 529])
def test_responses_wire_retries(status, resp_server, monkeypatch):
    srv = resp_server(status, [{"type": "response.output_text.delta", "delta": "ok"}, _completed({"input_tokens": 1, "output_tokens": 1})])
    _, turn = _call(_responses_provider(srv.url))
    assert turn.text == "ok" and len(srv.requests) == 2


# ─── BizNeuro (gateway that drops `tools`: prompt tools) ───────────────────────

def test_bizneuro_entry_and_prompt_tool_round_trip(llm, monkeypatch):
    preset = get_preset_by_id("bizneuro")
    assert preset.native_tools is False and preset.env_vars == ("BZN_API_KEY",) and preset.api_format == "openai"
    assert preset.base_url.endswith("/v1") and preset.default_model in preset.models
    provider, _ = _provider("bizneuro", llm, monkeypatch)
    llm.enqueue([{"text": '<tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>'}])
    _, turn = _run(provider, preset)
    assert [u.input for u in turn.tool_uses] == [{"path": "/x"}]
    sent = llm.completion_requests()[0].body
    assert "tools" not in sent and "Read" in json.dumps(sent["messages"])
    assert llm.completion_requests()[0].headers.get("authorization") == "Bearer key-bizneuro"
