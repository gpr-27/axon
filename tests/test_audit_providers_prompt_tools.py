"""Prompt tools: tool calling for gateways that silently drop the `tools` request field.

A local fake HTTP server plays the gateway: it ignores `tools` and answers with `<tool_call>` text.
"""
from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from axon.config import Settings
from axon.providers import endpoints as ep
from axon.providers import prompt_tools
from axon.providers.base import TextDelta, ToolUseStart, TurnComplete
from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, get_preset_by_id
from axon.providers.openai_compat import OpenAICompatProvider

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

READ_TOOL = {
    "name": "Read",
    "description": "Read a file from disk.\nSecond line that must not appear.",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
}
CALL_X = '<tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>'


# ─── Fake gateway ───────────────────────────────────────────────────────────

class _Gateway:
    """Answers each request with the next scripted reply (as a streamed chat completion)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                gateway.requests.append(json.loads(self.rfile.read(length)))
                reply = gateway.replies.pop(0) if gateway.replies else "done"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                half = max(1, len(reply) // 2)  # two chunks: a tag may be split across them
                for piece in (reply[:half], reply[half:]):
                    chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    made: list[_Gateway] = []

    def make(*replies: str) -> _Gateway:
        made.append(_Gateway(list(replies)))
        return made[-1]

    yield make
    for g in made:
        g.close()


def _provider(gw: _Gateway, native_tools: bool) -> OpenAICompatProvider:
    preset = ProviderPreset(id="fakegw", name="Fake", description="", category="Custom", base_url=gw.url,
                            default_model="m", requires_key=False, native_tools=native_tools)
    settings = Settings(base_url=gw.url, api_key=SecretStr("local"))
    return OpenAICompatProvider(settings, preset=preset)


def _run(provider: OpenAICompatProvider, messages: list[dict], tools=(READ_TOOL,)):
    events = list(provider.stream(model="m", system=[{"type": "text", "text": "You are Axon."}],
                                  messages=messages, tools=list(tools), max_tokens=500))
    return events, provider.finalize()


# ─── Request and response ──────────────────────────────────────────────────

def test_tools_are_described_in_the_prompt_and_not_sent(gateway):
    gw = gateway("Let me look. " + CALL_X)
    events, turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "read /x"}])
    body = gw.requests[0]
    assert "tools" not in body and "tool_choice" not in body and "functions" not in body
    system = body["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("You are Axon.")
    assert "<tool_call>" in system["content"] and "- Read: Read a file from disk." in system["content"]
    assert "Second line" not in system["content"]  # one-line description only
    assert '"required":["path"]' in system["content"]  # compact schema
    assert [b.name for b in turn.tool_uses] == ["Read"]
    assert turn.tool_uses[0].input == {"path": "/x"} and turn.tool_uses[0].parse_error is None
    assert turn.stop_reason == "tool_use"
    assert turn.text == "Let me look."
    assert turn.native["content"] == "Let me look." and turn.native["tool_calls"][0]["id"] == turn.tool_uses[0].id
    assert json.loads(turn.native["tool_calls"][0]["function"]["arguments"]) == {"path": "/x"}
    assert isinstance(events[-1], TurnComplete) and events[-1].stop_reason == "tool_use"


def test_text_is_buffered_and_emitted_once_without_tags(gateway):
    gw = gateway("Let me look. " + CALL_X)
    events, _turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "read /x"}])
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert texts == ["Let me look."]
    assert [type(e).__name__ for e in events].index("TextDelta") < [type(e).__name__ for e in events].index("ToolUseStart")
    assert any(isinstance(e, ToolUseStart) and e.name == "Read" for e in events)


def test_usage_estimate_includes_the_protocol_block(gateway):
    gw = gateway("plain answer")
    _events, turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "hi"}])
    protocol = prompt_tools.protocol_text([{"type": "function", "function": {"name": "Read", "description": "d", "parameters": {}}}])
    assert turn.usage.input > len(protocol) / 3.7 * 0.9
    assert turn.usage.output > 0


def test_invalid_json_gives_a_tool_use_with_parse_error(gateway):
    gw = gateway('<tool_call>{"name": "Read", "arguments": {path: /x}}</tool_call>')
    _events, turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "read /x"}])
    assert len(turn.tool_uses) == 1
    call = turn.tool_uses[0]
    assert call.name == "Read" and call.parse_error and "not valid JSON" in call.parse_error
    assert call.input == {"raw": '{"name": "Read", "arguments": {path: /x}}'}
    assert turn.stop_reason == "tool_use"


def test_two_calls_with_fences_and_whitespace(gateway):
    reply = ('<tool_call>\n```json\n{"name": "Read", "arguments": {"path": "/a"}}\n```\n</tool_call>\n'
             '  <tool_call>  {"name":"Read","arguments":{"path":"/b"}}  </tool_call>')
    gw = gateway(reply)
    _events, turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "read both"}])
    assert [c.input for c in turn.tool_uses] == [{"path": "/a"}, {"path": "/b"}]
    assert len({c.id for c in turn.tool_uses}) == 2
    assert turn.text == ""
    assert [t["function"]["name"] for t in turn.native["tool_calls"]] == ["Read", "Read"]


def test_round_trip_with_a_tool_result(gateway):
    gw = gateway(CALL_X, "The file says hello.")
    provider = _provider(gw, native_tools=False)
    messages = [{"role": "user", "content": "read /x"}]
    _events, first = _run(provider, messages)
    call = first.tool_uses[0]

    messages.append(first.native)
    messages.extend(provider.encode_tool_results([_result(call.id, "hello")]))
    _events, second = _run(provider, messages)

    wire = gw.requests[1]["messages"]
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert prompt_tools.OPEN_TAG in wire[2]["content"] and '"path": "/x"' in wire[2]["content"]
    assert wire[3]["content"] == f'<tool_result name="Read" id="{call.id}">\nhello\n</tool_result>'
    assert all("tool_calls" not in m and m["role"] != "tool" for m in wire)
    assert "tools" not in gw.requests[1]
    assert second.text == "The file says hello." and not second.tool_uses and second.stop_reason == "end_turn"


def test_results_and_following_user_text_share_one_user_message(gateway):
    gw = gateway("ok")
    provider = _provider(gw, native_tools=False)
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Read", "arguments": '{"path": "/a"}'}},
            {"id": "c2", "type": "function", "function": {"name": "Read", "arguments": '{"path": "/b"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "A"},
        {"role": "tool", "tool_call_id": "c2", "content": "B"},
        {"role": "user", "content": "and then?"},
    ]
    _run(provider, messages)
    wire = gw.requests[0]["messages"]
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "user"]
    assert '<tool_result name="Read" id="c1">' in wire[3]["content"] and 'id="c2"' in wire[3]["content"]
    assert wire[3]["content"].endswith("and then?")


def test_native_tools_true_changes_nothing(gateway):
    gw = gateway("streamed text")
    events, turn = _run(_provider(gw, native_tools=True), [{"role": "user", "content": "hi"}])
    body = gw.requests[0]
    assert body["tools"][0]["function"]["name"] == "Read"
    assert body["messages"][0]["content"] == "You are Axon."  # no protocol block
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["stream", "ed text"]  # live, unbuffered
    assert turn.text == "streamed text"


def test_no_tools_in_the_request_means_no_protocol(gateway):
    gw = gateway("hello")
    _events, turn = _run(_provider(gw, native_tools=False), [{"role": "user", "content": "hi"}], tools=())
    assert gw.requests[0]["messages"][0]["content"] == "You are Axon."
    assert turn.text == "hello"


def _result(call_id: str, text: str):
    from axon.providers.base import ToolResultBlock
    return ToolResultBlock(tool_use_id=call_id, content=text)


# ─── Parsing details ───────────────────────────────────────────────────────

def test_parse_reply_without_calls_keeps_the_text():
    assert prompt_tools.parse_reply("just text") == ("just text", [])


def test_parse_reply_unterminated_block_is_an_invalid_call():
    text, calls = prompt_tools.parse_reply('go <tool_call>{"name": "Bash", "arguments": {"command": "ls')
    assert text == "go" and len(calls) == 1
    assert calls[0].name == "Bash" and calls[0].parse_error


def test_parse_reply_accepts_string_arguments_and_rejects_non_objects():
    _text, calls = prompt_tools.parse_reply(
        '<tool_call>{"name": "Read", "arguments": "{\\"path\\": \\"/s\\"}"}</tool_call><tool_call>[1, 2]</tool_call>')
    assert calls[0].input == {"path": "/s"} and calls[0].parse_error is None
    assert calls[1].parse_error and calls[1].name == "invalid_tool_call"


# ─── Config: native_tools per endpoint ─────────────────────────────────────

@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "axon_home"
    h.mkdir()
    monkeypatch.setenv("AXON_HOME", str(h))
    ep.apply_endpoint_overrides({})
    yield h
    ep.apply_endpoint_overrides({})


def test_native_tools_defaults_to_true_for_every_builtin_preset(home):
    assert all(p.native_tools for p in PROVIDER_PRESETS)
    assert get_preset_by_id("ollama").native_tools is True


def test_custom_endpoint_native_tools_is_read_from_config(home):
    ep.save_endpoint("gw", "https://llm.example.com/v1", env_var="GW_API_KEY", native_tools=False)
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["endpoints"]["gw"]["native_tools"] is False
    assert get_preset_by_id("gw").native_tools is False
    ep.apply_endpoint_overrides(ep.load_endpoints())  # as on startup
    assert get_preset_by_id("gw").native_tools is False
    ep.save_endpoint("gw", "https://llm.example.com/v1")  # leaves the saved value alone
    assert get_preset_by_id("gw").native_tools is False
    ep.set_native_tools("gw", True)
    assert get_preset_by_id("gw").native_tools is True


def test_builtin_preset_override_and_restore(home):
    ep.set_native_tools("ollama", False)
    assert get_preset_by_id("ollama").native_tools is False
    ep.apply_endpoint_overrides({})  # no entry: back to the built-in default
    assert get_preset_by_id("ollama").native_tools is True


def test_provider_reads_the_flag_from_its_preset(home):
    ep.save_endpoint("gw", "https://llm.example.com/v1", native_tools=False)
    from axon.providers.registry import provider_for
    settings = Settings(base_url="https://llm.example.com/v1", api_key=SecretStr("k")).model_copy(update={"provider": "gw"})
    provider = provider_for("some-model", settings, failover=False)
    assert provider._prompt_tools_mode() is True


def test_endpoint_command_sets_flag_and_listing_shows_it(home, capsys):
    from axon.commands.endpoint_cmds import run_endpoint
    assert run_endpoint("add gw https://llm.example.com/v1 --key-env GW_API_KEY --native-tools false") == 0
    assert get_preset_by_id("gw").native_tools is False
    capsys.readouterr()
    run_endpoint("")
    assert "tools in prompt" in capsys.readouterr().out
    assert run_endpoint("native-tools gw true") == 0
    assert get_preset_by_id("gw").native_tools is True
    assert run_endpoint("native-tools gw maybe") == 2
    assert run_endpoint("add other https://o.example.com/v1 --native-tools sometimes") == 2


# ─── Probe (only on request) ───────────────────────────────────────────────

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def test_probe_detects_dropped_tools(monkeypatch):
    sent = {}

    def fake_post(url, headers, body, timeout):
        sent.update(url=url, body=body)
        return _Resp({"choices": [{"message": {"role": "assistant", "content": "I do not have access to any tools"}}]})

    monkeypatch.setattr(ep, "http_post", fake_post)
    probe = ep.probe_tool_support("https://gw.example.com/v1", "k", "m")
    assert probe.ran and not probe.native_tools
    assert sent["url"] == "https://gw.example.com/v1/chat/completions"
    assert sent["body"]["tools"][0]["function"]["name"] == "echo" and sent["body"]["tool_choice"] == "auto"


def test_probe_accepts_a_tool_call(monkeypatch):
    reply = {"choices": [{"message": {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]}}]}
    monkeypatch.setattr(ep, "http_post", lambda *a, **k: _Resp(reply))
    assert ep.probe_tool_support("https://gw.example.com/v1", "k", "m").native_tools is True


def test_probe_errors_are_not_a_verdict(monkeypatch):
    monkeypatch.setattr(ep, "http_post", lambda *a, **k: _Resp({}, status=500))
    probe = ep.probe_tool_support("https://gw.example.com/v1", "k", "m")
    assert not probe.ran and probe.native_tools is True
    assert not ep.probe_tool_support("https://gw.example.com/v1", "k", "").ran


def test_endpoint_test_prints_the_fix_when_tools_are_dropped(home, monkeypatch, capsys):
    from axon.commands.endpoint_cmds import run_endpoint
    run_endpoint("add gw https://llm.example.com/v1 --key-env GW_API_KEY --model m")
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: _Resp({"data": [{"id": "m"}]}))
    monkeypatch.setattr(ep, "http_post", lambda *a, **k: _Resp({"choices": [{"message": {"content": "no tools"}}]}))
    capsys.readouterr()
    assert run_endpoint("test gw") == 0
    out = capsys.readouterr().out
    assert "drops tool definitions" in out and "/endpoint native-tools gw false" in out


def test_endpoint_test_does_not_probe_when_prompt_tools_are_already_on(home, monkeypatch, capsys):
    from axon.commands.endpoint_cmds import run_endpoint
    run_endpoint("add gw https://llm.example.com/v1 --key-env GW_API_KEY --model m --native-tools false")
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: _Resp({"data": []}))
    monkeypatch.setattr(ep, "http_post", lambda *a, **k: pytest.fail("probe must not run"))
    capsys.readouterr()
    assert run_endpoint("test gw") == 0
    assert "described in the prompt" in capsys.readouterr().out


def test_parse_reply_accepts_claude_invoke_format():
    from axon.providers.prompt_tools import parse_reply
    reply = ('Searching. <function_calls>\n<invoke name="Grep">\n<parameter name="pattern">resolve_route</parameter>\n'
             '<parameter name="head_limit">5</parameter>\n</invoke>\n<invoke name="Read">\n'
             '<parameter name="path">/a b/c.py</parameter>\n</invoke>\n</function_calls>')
    text, calls = parse_reply(reply)
    assert text == "Searching."
    assert [c.name for c in calls] == ["Grep", "Read"]
    assert calls[0].input == {"pattern": "resolve_route", "head_limit": 5}
    assert calls[1].input == {"path": "/a b/c.py"}


def test_parse_reply_prefers_tool_call_blocks():
    from axon.providers.prompt_tools import parse_reply
    _, calls = parse_reply('<tool_call>{"name": "Read", "arguments": {"path": "/x"}}</tool_call>')
    assert [(c.name, c.input) for c in calls] == [("Read", {"path": "/x"})]
