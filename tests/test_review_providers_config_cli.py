"""Regression tests from the providers / config / cli review."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from axon import cli
from axon.config import PermissionConfig, Settings
from axon.providers.anthropic import AnthropicProvider


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AXON_API_KEY", "AXON_BASE_URL", "AXON_MODEL", "AXON_PROVIDER", "OPENROUTER_API_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
              "GEMINI_API_KEY", "TOGETHER_API_KEY", "AGENTROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AXON_HOME", "")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ─── config: a project file must not redirect the user's key ───────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://evil.example.com/localhost",
    "https://localhost.evil.example.com/v1",
    "https://127.0.0.1.evil.example.com",
    "https://evil.example.com/?next=127.0.0.1",
])
def test_project_base_url_that_only_mentions_localhost_is_still_ignored(workspace: Path, monkeypatch, url: str) -> None:
    monkeypatch.setenv("AXON_API_KEY", "user-secret")
    _write(workspace / ".axon" / "config.toml", f'base_url = "{url}"\n')
    assert "evil" not in Settings.load({"workspace": workspace}).base_url


def test_project_base_url_on_this_machine_is_still_allowed(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("AXON_API_KEY", "user-secret")
    _write(workspace / ".axon" / "config.toml", 'base_url = "http://localhost:11434/v1"\n')
    assert Settings.load({"workspace": workspace}).base_url == "http://localhost:11434/v1"
    _write(workspace / ".axon" / "config.toml", 'base_url = "http://[::1]:8080/v1"\n')
    assert Settings.load({"workspace": workspace}).base_url == "http://[::1]:8080/v1"


# ─── config: a key is never borrowed from another vendor ─────────────────────────────────────────

def test_other_vendors_key_is_not_used_for_a_known_provider(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    s = Settings.load({"workspace": workspace, "base_url": "https://api.openai.com/v1"}, require_api_key=False)
    assert s.api_key.get_secret_value() == ""
    from axon.providers.registry import resolve_route
    assert resolve_route("gpt-5", s).api_key == ""


def test_own_providers_key_is_still_found(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-own")
    s = Settings.load({"workspace": workspace, "base_url": "https://api.openai.com/v1"}, require_api_key=False)
    assert s.api_key.get_secret_value() == "sk-openai-own"


def test_unknown_endpoint_may_still_borrow_a_key_from_the_environment(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
    s = Settings.load({"workspace": workspace, "base_url": "https://my-proxy.example.com/v1"}, require_api_key=False)
    assert s.api_key.get_secret_value() == "sk-or-1"


def test_keyless_custom_endpoint_gets_no_key(workspace: Path, monkeypatch) -> None:
    from axon.providers.endpoints import apply_endpoint_overrides
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    _write(Path.home() / ".axon" / "config.toml", '[endpoints.corp]\nbase_url = "https://llm.corp.example/v1"\n')
    try:
        s = Settings.load({"workspace": workspace, "base_url": "https://llm.corp.example/v1", "provider": "corp"},
                          require_api_key=False)
        assert s.api_key.get_secret_value() == "local"
    finally:
        apply_endpoint_overrides({})


# ─── anthropic provider: no shell credentials to third-party hosts, /v1 base URLs ───────────────

def _anthropic(base_url: str, key: str = "") -> AnthropicProvider:
    from pydantic import SecretStr
    return AnthropicProvider(SimpleNamespace(api_key=SecretStr(key), base_url=base_url, max_retries=0))


def test_gateway_without_a_key_does_not_receive_the_shells_anthropic_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-token")
    client = _anthropic("https://gateway.example.com", "")._client
    assert "sk-ant-real" not in str(client.auth_headers)
    assert "real-token" not in str(client.auth_headers)


def test_official_endpoint_still_reads_the_anthropic_environment(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    assert _anthropic("https://api.anthropic.com", "")._client.api_key == "sk-ant-real"


@pytest.mark.parametrize("base", ["https://gw.example.com/v1", "https://gw.example.com/v1/", "https://gw.example.com/v1/messages"])
def test_base_url_ending_in_v1_does_not_double_the_version(base: str) -> None:
    client = _anthropic(base, "k")._client
    assert str(client.base_url).rstrip("/") == "https://gw.example.com"


def test_base_url_with_a_path_keeps_it() -> None:
    assert str(_anthropic("https://gw.example.com/anthropic/v1", "k")._client.base_url).rstrip("/") == "https://gw.example.com/anthropic"


# ─── cli ──────────────────────────────────────────────────────────────────────────────────────

def test_allowed_tools_keep_the_configured_ask_rules() -> None:
    settings = Settings(permissions=PermissionConfig(allow=["Read"], deny=["Bash(rm *)"], ask=["Bash(git push*)"]))
    out = cli._apply_tool_rules(settings, ["Grep"], ["WebFetch(*)"])
    assert out.permissions.ask == ["Bash(git push*)"]
    assert out.permissions.allow == ["Read", "Grep"]
    assert out.permissions.deny == ["Bash(rm *)", "WebFetch(*)"]


class _Queue(list):
    def push(self, text: str):
        self.append(text)
        return SimpleNamespace(id=len(self), text=text)


def test_double_semicolons_inside_a_mentioned_file_are_not_follow_ups(workspace: Path, monkeypatch) -> None:
    (workspace / "case.sh").write_text('case "$1" in\n  a) echo a ;;\n  b) echo b ;;\nesac\n', encoding="utf-8")
    sent: list[str] = []
    monkeypatch.setattr(cli, "_run_agent_turn", lambda agent, renderer, text, label: sent.append(text))
    monkeypatch.setattr(cli, "_journal", lambda *a, **k: None)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=workspace), message_queue=_Queue(), busy=False)
    assert cli.process_line(agent, MagicMock(), "explain @case.sh") is True
    assert len(sent) == 1 and "echo b ;;" in sent[0]
    assert list(agent.message_queue) == []


def test_double_semicolons_you_type_still_queue_follow_ups(workspace: Path, monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(cli, "_run_agent_turn", lambda agent, renderer, text, label: sent.append(text))
    monkeypatch.setattr(cli, "_journal", lambda *a, **k: None)
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=workspace), message_queue=_Queue(), busy=False)
    cli.process_line(agent, MagicMock(), "first ;; second ;; third")
    assert sent == ["first"]
    assert list(agent.message_queue) == ["second", "third"]


# ─── openai-compatible provider: the reasoning/responses fallback ladder ─────────────────────────

def test_reasoning_fallback_does_not_bounce_between_chat_and_responses(workspace: Path) -> None:
    from axon.providers.openai_compat import OpenAICompatProvider

    settings = Settings(workspace=workspace, base_url="https://gateway.example.com/v1", api_key="k")
    provider = OpenAICompatProvider(settings)
    calls: list[tuple[str, dict]] = []

    def fake_stream(method, url, **kwargs):
        body = kwargs["json"]
        calls.append((url, body))
        resp = MagicMock()
        if url.endswith("/responses"):
            resp.status_code = 404
            resp.text = "not found"
        elif "reasoning_effort" in body:
            resp.status_code = 400
            resp.text = '{"error": {"message": "reasoning_effort is not supported together with tools"}}'
        else:
            resp.status_code = 200
            resp.iter_lines.return_value = ['data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}', "data: [DONE]"]
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        return ctx

    tool = {"type": "function", "function": {"name": "Bash", "parameters": {}}}
    with patch("httpx.stream", side_effect=fake_stream):
        list(provider.stream(model="gpt-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[tool],
                             max_tokens=1000, effort="high"))
    assert provider.finalize().text == "ok"
    assert "reasoning_effort" not in calls[-1][1] and calls[-1][0].endswith("/chat/completions")


# ─── anthropic provider: usage of a stream that is cut short ────────────────────────────────────

class _CutStream:
    """A stream that delivers message_start and some text, then loses the connection."""

    def __iter__(self):
        usage = SimpleNamespace(input_tokens=100, cache_read_input_tokens=50, cache_creation_input_tokens=10,
                                cache_creation=None, output_tokens=1)
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=usage))
        yield SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text", text=""))
        yield SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="partial"))
        raise ConnectionResetError("connection reset")


def test_interrupted_anthropic_stream_still_reports_the_prompt_tokens() -> None:
    from axon.errors import ProviderError
    provider = _anthropic("https://api.anthropic.com", "k")
    ctx = MagicMock()
    ctx.__enter__.return_value = _CutStream()
    provider._client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: ctx))
    with pytest.raises(ProviderError):
        list(provider.stream(model="claude-sonnet-4-5", system=[], messages=[{"role": "user", "content": "hi"}],
                             tools=[], max_tokens=1000))
    turn = provider.finalize()
    assert turn.text == "partial"
    assert (turn.usage.input, turn.usage.cache_read, turn.usage.cache_write) == (160, 50, 10)


def test_usage_update_keeps_the_uncached_prompt_tokens_when_a_delta_omits_them() -> None:
    from axon.providers.base import Usage
    started = SimpleNamespace(input_tokens=100, cache_read_input_tokens=50, cache_creation_input_tokens=10, output_tokens=1)
    start = AnthropicProvider._usage(started, Usage())
    delta = AnthropicProvider._usage(SimpleNamespace(output_tokens=40), start)
    assert (delta.input, delta.output, delta.cache_read, delta.cache_write) == (160, 40, 50, 10)
