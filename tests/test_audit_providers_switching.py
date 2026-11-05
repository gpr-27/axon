"""Provider switching: thinking-block replay, one-step provider/endpoint changes, custom endpoints and models."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from axon.config import Settings
from axon.providers import endpoints as ep
from axon.providers.anthropic import AnthropicProvider, strip_thinking, to_anthropic_messages
from axon.providers.base import TurnComplete, Usage
from axon.providers.catalog import PROVIDER_PRESETS, find_preset_for_model, get_models_for_provider, get_preset_by_id
from axon.providers.registry import preset_for_model_switch, provider_for, resolve_route


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "axon_home"
    home_dir.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home_dir))
    for preset in PROVIDER_PRESETS:
        for var in preset.env_vars:
            monkeypatch.setenv(var, "")
    monkeypatch.setenv("MY_GW_API_KEY", "gw-key-123456789")
    ep.apply_endpoint_overrides({})
    yield home_dir
    ep.apply_endpoint_overrides({})


def config_text(home_dir: Path) -> str:
    return (home_dir / "config.toml").read_text(encoding="utf-8")


def add_gateway(api_format: str = "openai") -> None:
    ep.save_endpoint("my-gw", "https://gw.example.com/v1", env_var="MY_GW_API_KEY", model="qwen3", api_format=api_format)


# ─── thinking blocks ───────────────────────────────────────────────────────

def assistant_with_thinking(text: str = "answer") -> dict[str, Any]:
    return {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
        {"type": "redacted_thinking", "data": "xxx"},
        {"type": "text", "text": text},
    ]}


def test_strip_thinking_removes_blocks_from_old_turns() -> None:
    history = [{"role": "user", "content": "hi"}, assistant_with_thinking(), {"role": "user", "content": "more"}]
    result = strip_thinking(history, keep_current_turn=False)
    assert result[1]["content"] == [{"type": "text", "text": "answer"}]
    assert "thinking" in json.dumps(history)  # input untouched


def test_strip_thinking_keeps_open_tool_loop() -> None:
    old = assistant_with_thinking("old")
    open_loop = {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "plan", "signature": "s2"},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
    ]}
    history = [{"role": "user", "content": "q1"}, old, {"role": "user", "content": "q2"}, open_loop,
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
    result = strip_thinking(history, keep_current_turn=True)
    assert [b["type"] for b in result[1]["content"]] == ["text"]
    assert result[3]["content"][0]["type"] == "thinking"


def test_strip_thinking_never_leaves_empty_assistant_message() -> None:
    only = {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]}
    result = strip_thinking([{"role": "user", "content": "a"}, only, {"role": "user", "content": "b"}], keep_current_turn=False)
    assert result[1]["content"] == [{"type": "text", "text": "[thinking omitted]"}]


class FakeBadRequest(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.body = {"error": {"message": message}}


def test_anthropic_retries_once_without_thinking_on_signature_400(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(base_url="https://api.anthropic.com", api_key=SecretStr("sk-ant-test"), model="claude-sonnet-4-5")
    provider = AnthropicProvider(settings)
    sent: list[list[dict[str, Any]]] = []

    def fake_run(self: Any, kwargs: dict[str, Any], state: Any, emitted: list[bool], thinking: bool, wire_model: str = "") -> Any:
        sent.append(kwargs["messages"])
        if len(sent) == 1:
            raise FakeBadRequest("messages.1.content.0: Invalid `signature` in `thinking` block")
        yield TurnComplete(stop_reason="end_turn", usage=Usage())

    monkeypatch.setattr(AnthropicProvider, "_run", fake_run)
    history = [{"role": "user", "content": "hi"}, assistant_with_thinking(), {"role": "user", "content": "again"}]
    events = list(provider.stream(model="claude-sonnet-4-5", system=[], messages=history, tools=[], max_tokens=1000, thinking=False))
    assert isinstance(events[-1], TurnComplete)
    assert len(sent) == 2
    assert all(b["type"] != "thinking" for m in sent[1] for b in m["content"] if isinstance(b, dict))


def test_anthropic_does_not_loop_on_repeated_signature_400(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(base_url="https://api.anthropic.com", api_key=SecretStr("sk-ant-test"), model="claude-sonnet-4-5")
    provider = AnthropicProvider(settings)
    calls = []

    def always_fail(self: Any, kwargs: dict[str, Any], state: Any, emitted: list[bool], thinking: bool, wire_model: str = "") -> Any:
        calls.append(1)
        raise FakeBadRequest("messages.1.content.0: Invalid `signature` in `thinking` block")
        yield  # pragma: no cover

    monkeypatch.setattr(AnthropicProvider, "_run", always_fail)
    with pytest.raises(Exception) as info:
        list(provider.stream(model="claude-sonnet-4-5", system=[], messages=[{"role": "user", "content": "x"}],
                             tools=[], max_tokens=1000, thinking=False))
    assert len(calls) == 2
    assert "thinking" in str(info.value).lower()
    assert "How to fix" in str(info.value)


def test_openai_history_never_carries_anthropic_thinking_blocks() -> None:
    from axon.providers.openai_compat import sanitize_openai_messages
    history = [{"role": "user", "content": "hi"}, assistant_with_thinking(), {"role": "user", "content": "again"}]
    out = json.dumps(sanitize_openai_messages(history, strict=True))
    assert "thinking" not in out and "signature" not in out


def test_anthropic_tool_ids_from_other_providers_are_made_valid() -> None:
    history = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call.1:x", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call.1:x", "content": "ok"},
    ]
    msgs = to_anthropic_messages(history)
    use_id = msgs[1]["content"][0]["id"]
    assert use_id == "call_1_x"
    assert msgs[2]["content"][0]["tool_use_id"] == use_id


# ─── one switch is enough ──────────────────────────────────────────────────

def agent_for(**kw: Any) -> Any:
    settings = Settings(api_key=SecretStr("k"), **kw)
    return SimpleNamespace(settings=settings, provider=None, session=SimpleNamespace(model=""), permissions=None)


def test_switch_model_moves_provider_and_base_url_together(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.commands import model_cmds
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-1234567890")
    monkeypatch.setattr(model_cmds, "update_global_config", lambda updates=None, **k: True)
    monkeypatch.setattr(model_cmds, "update_settings", lambda agent, **ch: setattr(agent, "settings", agent.settings.model_copy(update=ch)))
    agent = agent_for(provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5")
    assert model_cmds.switch_model(agent, "gpt-5")
    assert agent.settings.provider == "openai"
    route = resolve_route("gpt-5", agent.settings)
    assert route.preset.id == "openai" and "api.openai.com" in route.base_url
    assert route.api_key == "sk-openai-1234567890"


def test_switch_model_persists_provider(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.commands import model_cmds
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-1234567890")
    monkeypatch.setattr(model_cmds, "update_settings", lambda agent, **ch: setattr(agent, "settings", agent.settings.model_copy(update=ch)))
    agent = agent_for(provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5")
    model_cmds.switch_model(agent, "gpt-5")
    text = config_text(home)
    assert 'provider = "openai"' in text and "api.openai.com" in text


def test_apply_provider_sets_provider_and_session_model() -> None:
    from axon.ui.provider_picker import apply_provider
    agent = agent_for(provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5")
    apply_provider(agent, get_preset_by_id("openai"), "gpt-5", "sk-x")
    assert agent.settings.provider == "openai"
    assert agent.session.model == "gpt-5"
    assert resolve_route("gpt-5", agent.settings).preset.id == "openai"


def test_picker_active_tab_follows_provider_not_url() -> None:
    from axon.ui.provider_picker import is_active_preset
    settings = Settings(provider="openrouter", base_url="https://openrouter.ai/api/v1")
    assert is_active_preset(settings, get_preset_by_id("openrouter"))
    assert not is_active_preset(settings, get_preset_by_id("openai"))


def test_failover_fallback_model_goes_to_its_own_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.providers.registry import settings_for_model
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-1234567890")
    settings = Settings(provider="anthropic", base_url="https://api.anthropic.com", api_key=SecretStr("sk-ant"))
    changed = settings_for_model("openai/gpt-5", settings)
    route = resolve_route("openai/gpt-5", changed)
    assert route.preset.id == "openai" and route.api_key == "sk-openai-1234567890"
    assert route.model_prefix == "openai/"
    assert settings_for_model("claude-haiku-4-5", settings) is settings


# ─── custom endpoints and models ───────────────────────────────────────────

def test_custom_endpoint_routes_with_its_key_and_format() -> None:
    add_gateway("anthropic")
    settings = Settings(provider="my-gw", base_url="https://gw.example.com/v1", api_key=SecretStr(""), model="qwen3")
    route = resolve_route("qwen3", settings)
    assert route.preset.id == "my-gw"
    assert route.api_format == "anthropic"
    assert route.api_key == "gw-key-123456789"


def test_custom_endpoint_prefix_is_stripped_from_wire_model() -> None:
    add_gateway()
    settings = Settings(provider="my-gw", base_url="https://gw.example.com/v1", api_key=SecretStr(""))
    route = resolve_route("my-gw/qwen3", settings)
    assert route.model_prefix == "my-gw/"
    assert find_preset_for_model("my-gw/qwen3").id == "my-gw"


def test_custom_models_are_found_and_offered(home: Path) -> None:
    add_gateway()
    ep.add_models("my-gw", ["claude-fancy", "org/llama-9"])
    assert find_preset_for_model("claude-fancy").id == "my-gw"
    assert find_preset_for_model("org/llama-9").id == "my-gw"
    offered = get_models_for_provider(get_preset_by_id("my-gw"))
    assert "claude-fancy" in offered and "qwen3" in offered
    assert ep.add_models("my-gw", ["claude-fancy"]) == []  # no duplicates


def test_active_custom_endpoint_keeps_claude_and_gpt_ids() -> None:
    add_gateway()
    settings = Settings(provider="my-gw", base_url="https://gw.example.com/v1")
    assert preset_for_model_switch("gpt-5", settings).id == "my-gw"
    assert preset_for_model_switch("claude-sonnet-4-5", settings).id == "my-gw"
    assert preset_for_model_switch("openai/gpt-5", settings).id == "openai"


def test_switch_model_to_saved_custom_model_changes_endpoint(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.commands import model_cmds
    add_gateway()
    ep.add_models("my-gw", ["claude-fancy"])
    monkeypatch.setattr(model_cmds, "update_settings", lambda agent, **ch: setattr(agent, "settings", agent.settings.model_copy(update=ch)))
    agent = agent_for(provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5")
    assert model_cmds.switch_model(agent, "claude-fancy")
    assert agent.settings.provider == "my-gw"
    assert agent.settings.base_url == "https://gw.example.com/v1"
    assert agent.settings.api_key.get_secret_value() == "gw-key-123456789"
    assert provider_for("claude-fancy", agent.settings, failover=False) is not None


def test_removing_custom_endpoint_forgets_models_and_provider(home: Path) -> None:
    add_gateway()
    ep.add_models("my-gw", ["a", "b"])
    ep.save_endpoint("my-gw", "https://gw.example.com/v1")
    ep.reset_endpoint("my-gw")
    assert ep.saved_models("my-gw") == []


# ─── importing a model list ────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def test_parse_model_ids_handles_common_shapes() -> None:
    assert ep.parse_model_ids({"data": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert ep.parse_model_ids({"models": [{"name": "x"}, {"model": "y"}]}) == ["x", "y"]
    assert ep.parse_model_ids(["p", "q", "p"]) == ["p", "q"]
    assert ep.parse_model_ids({"oops": 1}) == []


def test_fetch_and_import_models_from_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(url: str, headers: dict[str, str], timeout: float) -> FakeResponse:
        seen.update(url=url, headers=headers)
        return FakeResponse(200, {"data": [{"id": "m-one"}, {"id": "m-two"}, {"id": "text-embedding-3-small"}]})

    monkeypatch.setattr(ep, "http_get", fake_get)
    add_gateway()
    result = ep.fetch_models("https://gw.example.com/v1", "gw-key-123456789")
    assert seen["url"] == "https://gw.example.com/v1/models"
    assert seen["headers"]["authorization"] == "Bearer gw-key-123456789"
    assert result.models == ["m-one", "m-two"]
    assert ep.add_models("my-gw", result.models) == ["m-one", "m-two"]
    assert find_preset_for_model("m-two").id == "my-gw"


def test_fetch_models_reports_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: FakeResponse(401, {}))
    assert "rejected the API key" in ep.fetch_models("https://gw.example.com/v1").error
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: FakeResponse(404, {}))
    assert "HTTP 404" in ep.fetch_models("https://gw.example.com/v1").error

    def boom(*a: Any, **k: Any) -> Any:
        raise ConnectionError("refused")

    monkeypatch.setattr(ep, "http_get", boom)
    assert "cannot reach" in ep.fetch_models("https://gw.example.com/v1").error


def test_endpoint_models_import_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.endpoint_cmds import run_endpoint
    monkeypatch.setattr(ep, "http_get", lambda *a, **k: FakeResponse(200, {"data": [{"id": "m-one"}]}))
    add_gateway()
    assert run_endpoint("models my-gw import") == 0
    assert ep.saved_models("my-gw") == ["m-one"]
    assert run_endpoint("models my-gw add extra-model") == 0
    assert "extra-model" in ep.saved_models("my-gw")
    assert run_endpoint("models my-gw") == 0
    assert "extra-model" in capsys.readouterr().out
    assert run_endpoint("models my-gw clear") == 0
    assert ep.saved_models("my-gw") == []


def test_endpoint_use_remembers_unlisted_model(home: Path) -> None:
    from axon.commands.endpoint_cmds import use_endpoint
    add_gateway()
    assert use_endpoint(None, "my-gw", model="brand-new") == 0
    assert "brand-new" in ep.saved_models("my-gw")
    assert 'provider = "my-gw"' in config_text(home)


def test_import_against_real_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"data": [{"id": "local-a"}, {"id": "local-b"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = ep.fetch_models(f"http://127.0.0.1:{server.server_port}/v1")
    finally:
        server.shutdown()
    assert result.models == ["local-a", "local-b"]


# ─── error messages ────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,needle", [
    ("messages.1.content.0: Invalid `signature` in `thinking` block", "thinking blocks"),
    ("The model `foo` does not exist", "does not know this model"),
    ("thinking.budget_tokens: must be >= 1024", "thinking/reasoning"),
    ("tools not supported by this model", "tool"),
])
def test_400_messages_state_cause_and_next_step(body: str, needle: str) -> None:
    from axon.providers.transport import classify_error
    error = classify_error(status=400, body={"error": {"message": body}}, base_url="https://gw.example.com/v1", model="foo")
    text = str(error)
    assert needle in text
    assert "How to fix" in text
