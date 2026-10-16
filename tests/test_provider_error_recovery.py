"""
Regression tests for a failed AgentRouter / deepseek-v4-flash turn:

* thinking-mode APIs reject an assistant tool_calls message without reasoning_content
  ("The `reasoning_content` in the thinking mode must be passed back to the API"; AgentRouter
  relays it as HTTP 500 "Upstream rejected the request as invalid"),
* such a 5xx is reported honestly and does not burn the full retry budget,
* a provider failure after tools already ran keeps the turn's progress and points at continue / /retry,
* the Bash tool resolves `python` to the project's interpreter,
* /context reports the same context window as the per-step gauge.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from axon.config import Settings
from axon.errors import ProviderError
from axon.providers import transport
from axon.providers.base import AssistantTurn, ToolUseBlock
from axon.providers.openai_compat import (
    REASONING_PLACEHOLDER,
    OpenAICompatProvider,
    needs_reasoning_passback,
    sanitize_openai_messages,
)
from axon.tools import shell as shell_tool
from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

AGENTROUTER = "https://agentrouter.org"
INVALID_500 = {"error": {"message": "Upstream rejected the request as invalid (request id: 2026101623291121)",
                         "type": "invalid_request_error", "param": "", "code": "invalid_request_error"}}
PASSBACK_400 = {"error": {"message": "The `reasoning_content` in the thinking mode must be passed back to the API.",
                          "type": "invalid_request_error", "param": "", "code": None}}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_sleep", lambda s: None)


def sse(*chunks: object) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


OK_STREAM = sse({"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]})


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("unexpected extra request")
        return self.queue.pop(0)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def provider(rec: Recorder, base_url: str = AGENTROUTER) -> OpenAICompatProvider:
    settings = Settings(base_url=base_url, api_key=SecretStr("sk-test"))
    return OpenAICompatProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(rec)))


def run(prov: OpenAICompatProvider, messages: list[dict[str, Any]], model: str = "deepseek-v4-flash") -> list[Any]:
    events = list(prov.stream(model=model, system=[{"type": "text", "text": "sys"}], messages=messages, tools=[],
                              max_tokens=1000, effort=None, thinking=True))
    prov.finalize()
    return events


def tool_call(call_id: str, reasoning: str | None = None, content: str = "") -> dict[str, Any]:
    m: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]}
    if reasoning is not None:
        m["reasoning_content"] = reasoning
    return m


def turn_with_silent_tool_call() -> list[dict[str, Any]]:
    """The failing session's shape: one tool call of the current turn came back with no reasoning."""
    return [
        {"role": "user", "content": "Can you test any tool calling?"},
        tool_call("call_00_a", reasoning="Let me look."),
        {"role": "tool", "tool_call_id": "call_00_a", "content": "ok"},
        tool_call("call_00_b"),  # the model emitted this call without any reasoning_content
        {"role": "tool", "tool_call_id": "call_00_b", "content": "ok"},
        tool_call("call_00_c", reasoning="Ask the user.", content="Tool calling works."),
        {"role": "tool", "tool_call_id": "call_00_c", "content": "The user answered: Report only"},
    ]


def assistants(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body["messages"] if m["role"] == "assistant"]


# ─── 1a. reasoning_content passback ─────────────────────────────────────────
def test_tool_call_without_reasoning_is_replayed_with_placeholder() -> None:
    rec = Recorder(httpx.Response(200, content=OK_STREAM))
    run(provider(rec), turn_with_silent_tool_call())
    sent = assistants(rec.body())
    assert [m["reasoning_content"] for m in sent] == ["Let me look.", REASONING_PLACEHOLDER, "Ask the user."]


def test_whitespace_reasoning_counts_as_missing() -> None:
    out = sanitize_openai_messages([{"role": "user", "content": "x"}, tool_call("c1", reasoning="\n"),
                                    {"role": "tool", "tool_call_id": "c1", "content": "r"}], reasoning_passback=True)
    assert out[1]["reasoning_content"] == REASONING_PLACEHOLDER


def test_passback_detection() -> None:
    plain = [{"role": "user", "content": "x"}, tool_call("c1")]
    assert needs_reasoning_passback("deepseek-v4-flash", plain)
    assert needs_reasoning_passback("deepseek/deepseek-reasoner", plain)
    assert not needs_reasoning_passback("gpt-4o", plain)
    assert needs_reasoning_passback("some-model", plain + [tool_call("c2", reasoning="thought")])


def test_no_reasoning_content_added_for_models_without_thinking_history() -> None:
    rec = Recorder(httpx.Response(200, content=OK_STREAM))
    run(provider(rec, "https://api.openai.com/v1"), [{"role": "user", "content": "x"}, tool_call("c1"),
                                                     {"role": "tool", "tool_call_id": "c1", "content": "r"}],
        model="gpt-4o")
    assert all("reasoning_content" not in m for m in assistants(rec.body()))


def test_strict_presets_never_receive_reasoning_content() -> None:
    rec = Recorder(httpx.Response(200, content=OK_STREAM))
    run(provider(rec, "https://api.groq.com/openai/v1"), turn_with_silent_tool_call(), model="deepseek-v4-flash")
    assert all("reasoning_content" not in m for m in assistants(rec.body()))


def test_explicit_passback_rejection_is_repaired_once_and_remembered() -> None:
    msgs = [{"role": "user", "content": "x"}, tool_call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "r"}]
    rec = Recorder(httpx.Response(400, json=PASSBACK_400), httpx.Response(200, content=OK_STREAM),
                   httpx.Response(200, content=OK_STREAM))
    prov = provider(rec, "https://api.example.com/v1")
    run(prov, msgs, model="thinker-1")
    assert "reasoning_content" not in assistants(rec.body(0))[0]
    assert assistants(rec.body(1))[0]["reasoning_content"] == REASONING_PLACEHOLDER
    run(prov, msgs, model="thinker-1")  # learned: no failed round-trip the second time
    assert len(rec.requests) == 3 and assistants(rec.body(2))[0]["reasoning_content"] == REASONING_PLACEHOLDER


# ─── 1b. invalid-request 5xx classification ─────────────────────────────────
def test_invalid_request_500_is_reported_honestly_with_reduced_retries() -> None:
    rec = Recorder(*[httpx.Response(500, json=INVALID_500) for _ in range(6)])
    with pytest.raises(ProviderError) as exc:
        run(provider(rec), [{"role": "user", "content": "hi"}])
    msg = str(exc.value)
    assert len(rec.requests) == 1 + transport.MAX_INVALID_5XX_RETRIES < 1 + 3
    assert "rejected the request as invalid (HTTP 500)" in msg and "temporarily unavailable" not in msg
    assert "/compact" in msg and "request id: 2026101623291121" in msg
    assert exc.value.status == 500 and getattr(exc.value, "retryable", None) is False


def test_invalid_request_500_still_recovers_when_a_retry_succeeds() -> None:
    rec = Recorder(httpx.Response(500, json=INVALID_500), httpx.Response(200, content=OK_STREAM))
    events = run(provider(rec), [{"role": "user", "content": "hi"}])
    retries = [e for e in events if type(e).__name__ in ("RetryEvent", "ProviderRetry")]
    assert len(retries) == 1 and "Upstream rejected the request as invalid" in retries[0].error


def test_plain_500_keeps_full_retry_budget_and_outage_wording() -> None:
    rec = Recorder(*[httpx.Response(500, json={"error": {"message": "internal error"}}) for _ in range(4)])
    with pytest.raises(ProviderError) as exc:
        run(provider(rec), [{"role": "user", "content": "hi"}])
    assert len(rec.requests) == 4 and "temporarily unavailable" in str(exc.value)


def test_should_retry_status_rules() -> None:
    assert transport.should_retry_status(500, "internal error", 2)
    assert transport.should_retry_status(500, INVALID_500, 0)
    assert not transport.should_retry_status(500, json.dumps(INVALID_500), transport.MAX_INVALID_5XX_RETRIES)
    assert not transport.should_retry_status(400, PASSBACK_400, 0)
    assert transport.should_retry_status(429, "invalid request rate", 2)  # only 5xx is capped


# ─── 1c. keep the turn's progress on a mid-turn provider failure ───────────
class FailAfter(FakeProvider):
    """Serves scripted turns, then raises on call number *fail_at* (1-based) once."""

    def __init__(self, turns: list[AssistantTurn], fail_at: int) -> None:
        super().__init__(turns)
        self.fail_at, self.calls = fail_at, 0

    def stream(self, **kw: Any):
        self.calls += 1
        if self.calls == self.fail_at:
            self.requests.append(kw)
            raise ProviderError("AgentRouter rejected the request as invalid (HTTP 500).", status=400)
        yield from super().stream(**kw)


def test_mid_turn_failure_keeps_tool_results_and_points_to_continue(workspace: Path) -> None:
    (workspace / "a.txt").write_text("alpha\n")
    turns = [AssistantTurn(blocks=[ToolUseBlock(id="t0", name="Read", input={"path": "a.txt"})], stop_reason="tool_use")]
    prov = FailAfter(turns + scripted("picked up where I left off"), fail_at=2)
    agent = make_agent(prov, workspace)
    res = agent.run_turn("read a.txt")
    assert res.stop_reason == "error" and res.tool_calls_count == 1
    assert "type 'continue' or /retry to resume" in res.final_text and "/retry" in (res.error or "")
    assert agent.last_failed_turn == {"prompt": "read a.txt", "resume": True}
    flat = json.dumps(agent.conversation.messages)
    assert "read a.txt" in flat and '"t0"' in flat and "alpha" in flat  # tool result is kept

    from axon.commands import dispatch_command
    dispatch_command("/retry", agent)
    last = prov.requests[-1]["messages"]
    assert "alpha" in json.dumps(last) and "continue" in json.dumps(last[-1]).lower()
    assert agent.last_failed_turn is None


def test_first_call_failure_rolls_back_and_retry_resends_the_prompt(workspace: Path) -> None:
    prov = FailAfter(scripted("answer"), fail_at=1)
    agent = make_agent(prov, workspace)
    res = agent.run_turn("what is 2+2?")
    assert res.stop_reason == "error" and agent.conversation.messages == []
    assert "/retry sends it again" in res.final_text
    from axon.commands import dispatch_command
    dispatch_command("/retry", agent)
    assert prov.requests[-1]["messages"][-1]["content"] == "what is 2+2?"
    assert [m["role"] for m in agent.conversation.messages] == ["user", "assistant"]


def test_retry_without_a_failed_turn_does_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prov = FakeProvider(scripted("hi"))
    agent = make_agent(prov, workspace)
    from axon.commands import dispatch_command
    dispatch_command("/retry", agent)
    assert prov.requests == [] and "Nothing to retry" in capsys.readouterr().out


# ─── 2. Bash tool finds the project's python ────────────────────────────────
def _ctx(workspace: Path) -> Any:
    from axon.agent.state import FileState, TodoState
    from axon.tools.base import ToolContext
    settings = Settings(workspace=workspace, mode="bypass", api_key="test-key-12345")
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=settings)


def _make_venv(root: Path) -> Path:
    bindir = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bindir.mkdir(parents=True)
    exe = bindir / ("python.exe" if os.name == "nt" else "python")
    exe.write_text("#!/bin/sh\necho fake-venv-python\n")
    exe.chmod(0o755)
    return bindir


def _env_with(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for k, v in values.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_workspace_venv_is_put_first_on_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = _make_venv(workspace)
    _env_with(monkeypatch, VIRTUAL_ENV=None, CONDA_PREFIX=None, PATH=os.pathsep.join(["/usr/bin", "/bin"]))
    env = shell_tool._child_env(workspace)
    assert env["PATH"].split(os.pathsep)[0] == str(bindir)
    assert env["VIRTUAL_ENV"] == str(workspace / ".venv")


def test_stale_virtual_env_is_dropped(workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The reported case: the venv was moved, so VIRTUAL_ENV and the first PATH entry point nowhere."""
    bindir = _make_venv(workspace)
    gone = tmp_path / "old-location" / ".venv"
    _env_with(monkeypatch, VIRTUAL_ENV=str(gone), CONDA_PREFIX=None,
              PATH=os.pathsep.join([str(gone / "bin"), "/usr/bin", "/bin"]))
    env = shell_tool._child_env(workspace)
    entries = env["PATH"].split(os.pathsep)
    assert entries[0] == str(bindir) and str(gone / "bin") not in entries
    assert env["VIRTUAL_ENV"] == str(workspace / ".venv")


def test_stale_virtual_env_without_workspace_venv(workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    _env_with(monkeypatch, VIRTUAL_ENV=str(gone), CONDA_PREFIX=None, PATH=os.pathsep.join([str(gone / "bin"), "/bin"]))
    env = shell_tool._child_env(workspace)
    assert "VIRTUAL_ENV" not in env and env["PATH"] == "/bin"


@pytest.mark.parametrize("var", ["VIRTUAL_ENV", "CONDA_PREFIX"])
def test_user_activated_environment_wins(workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str) -> None:
    _make_venv(workspace)
    active = tmp_path / "active-env"
    (active / "bin").mkdir(parents=True)
    path = os.pathsep.join([str(active / "bin"), "/usr/bin"])
    _env_with(monkeypatch, VIRTUAL_ENV=None, CONDA_PREFIX=None, PATH=path)
    monkeypatch.setenv(var, str(active))
    env = shell_tool._child_env(workspace)
    assert env["PATH"] == path and env[var] == str(active)


def test_no_venv_and_no_activation_leaves_path_alone(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env_with(monkeypatch, VIRTUAL_ENV=None, CONDA_PREFIX=None, PATH="/usr/bin:/bin")
    env = shell_tool._child_env(workspace)
    assert env["PATH"] == "/usr/bin:/bin" and "VIRTUAL_ENV" not in env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_bash_tool_resolves_python_from_workspace_venv(workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _make_venv(workspace)
    gone = tmp_path / "moved" / ".venv"
    _env_with(monkeypatch, VIRTUAL_ENV=str(gone), CONDA_PREFIX=None, AXON_SHELL=None,
              PATH=os.pathsep.join([str(gone / "bin"), "/usr/bin", "/bin"]))
    out = shell_tool.BashTool().run({"command": "python"}, _ctx(workspace))
    assert "fake-venv-python" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_python_not_found_gets_a_hint(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env_with(monkeypatch, VIRTUAL_ENV=None, CONDA_PREFIX=None, AXON_SHELL=None, PATH="/usr/bin:/bin")
    out = shell_tool.BashTool().run({"command": "echo '/bin/bash: python: command not found'"}, _ctx(workspace))
    assert "(note:" in out and "python" in out.split("(note:")[1]


# ─── 3. /context and the step gauge agree on the window ─────────────────────
def test_context_command_reports_full_window_and_reply_reserve(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands import dispatch_command
    from axon.commands.context_cmds import _budget
    agent = make_agent(FakeProvider([]), workspace, model="deepseek-v4-flash", context_limit=0)   # model's full window
    window, threshold, reserve = _budget(agent)
    assert window == agent.context.context_window("deepseek-v4-flash") == 1_000_000  # what the gauge shows
    assert reserve == window - agent.context.get_effective_budget("deepseek-v4-flash") > 0
    assert threshold == agent.context.threshold("deepseek-v4-flash")
    dispatch_command("/context", agent)
    out = capsys.readouterr().out
    assert "window 1.00M" in out and "872.0k" not in out.split("window", 1)[1].splitlines()[0]
    assert "Reply reserve" in out


def test_context_limit_default_and_command(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands import dispatch_command
    agent = make_agent(FakeProvider([]), workspace, model="deepseek-v4-flash")
    assert agent.context.context_window("deepseek-v4-flash") == 128_000      # default limit on a 1M model
    dispatch_command("/context", agent)
    out = capsys.readouterr().out
    assert "window 128.0k (limit; model max 1.00M" in out
    dispatch_command("/context limit 256k", agent)
    assert agent.settings.context_limit == 256_000 and agent.context.context_window("deepseek-v4-flash") == 256_000
    assert "auto-compact at 192.0k (75%)" in capsys.readouterr().out
    dispatch_command("/context limit max", agent)
    assert agent.context.context_window("deepseek-v4-flash") == 1_000_000
