"""Regressions: failed turns are kept but never replayed, /retry and /output survive a resume,
MCP debug logging, /tools exact match, JSON startup errors."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore, iter_records, restore_retry_state
from axon.tools import create_default_registry
from fakes import FakeProvider


def _text(t: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=t)], stop_reason="end_turn", usage=Usage(input=10, output=5))


class _Failing:
    name = "failing"

    def __init__(self, then: list[AssistantTurn] | None = None, fails: int = 1) -> None:
        self.fails = fails
        self.inner = FakeProvider(then or [_text("fine")])

    def stream(self, **kw: Any):
        if self.fails > 0:
            self.fails -= 1
            raise RuntimeError("HTTP 500 boom")
        return self.inner.stream(**kw)

    def finalize(self) -> AssistantTurn:
        return self.inner.finalize()

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.inner.requests


def _agent(ws: Path, provider: Any, **kw: Any) -> Agent:
    s = Settings(workspace=ws, mode=kw.pop("mode", "bypass"), **kw)
    a = Agent(provider=provider, tools=create_default_registry(), permissions=PermissionEngine(s),
              context=ContextManager(s), session=SessionStore(ws), ledger=Ledger(), settings=s)
    a.retry_backoff_s = 0.0
    return a


# ─── 1. failed first turn ───────────────────────────────────────────────────

def test_failed_first_turn_is_recorded_but_not_replayed(workspace: Path) -> None:
    a = _agent(workspace, _Failing())
    res = a.run_turn("first q")
    assert res.stop_reason == "error" and a.conversation.messages == []
    recs = list(iter_records(a.session.active_file))
    failed = [r for r in recs if r["type"] == "turn_failed"]
    assert failed and failed[0]["data"]["prompt"] == "first q" and failed[0]["data"]["rolled_back"]
    assert not any(r["type"] == "assistant_turn" for r in recs)   # no synthetic "[Provider error]" message
    conv = SessionStore(workspace).read_conversation(a.session.active_session_id)
    assert conv.messages == []
    # a later prompt in the same session replays only that prompt
    a.run_turn("second")
    conv = SessionStore(workspace).read_conversation(a.session.active_session_id)
    assert [m["content"] for m in conv.messages if m["role"] == "user"] == ["second"]


def test_failed_turn_after_tool_calls_keeps_progress(workspace: Path) -> None:
    (workspace / "f.txt").write_text("x\n", encoding="utf-8")

    class _ToolThenFail(FakeProvider):
        def __init__(self) -> None:
            super().__init__([AssistantTurn(blocks=[ToolUseBlock(id="t1", name="Read", input={"path": "f.txt"})],
                                            stop_reason="tool_use", usage=Usage(input=1, output=1))])
            self.calls = 0

        def stream(self, **kw: Any):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("HTTP 500 later")
            return super().stream(**kw)

    a = _agent(workspace, _ToolThenFail())
    res = a.run_turn("read it")
    assert res.stop_reason == "error"
    conv = SessionStore(workspace).read_conversation(a.session.active_session_id)
    roles = [m["role"] for m in conv.messages]
    assert roles[0] == "user" and "assistant" in roles and len(conv.messages) == len(a.conversation.messages)
    assert "Provider error" not in json.dumps(conv.messages)
    assert SessionStore(workspace).last_failed_turn(a.session.active_session_id) == {"prompt": "read it", "resume": True}


def test_legacy_synthetic_error_messages_are_skipped(workspace: Path) -> None:
    store = SessionStore(workspace)
    store.append_user("old failed q")
    store.append("assistant_turn", {"stop_reason": "end_turn", "text": "[Provider error: 500]", "tool_uses": [],
                                    "native": None, "usage": None})
    assert store.read_conversation(store.active_session_id).messages == []
    assert store.last_failed_turn() == {"prompt": "old failed q", "resume": False}
    store.append_user("next")
    store.append_turn(_text("answer"))
    assert [m["role"] for m in store.read_conversation(store.active_session_id).messages] == ["user", "assistant"]
    assert store.last_failed_turn() is None


# ─── 2. /retry after a resume ───────────────────────────────────────────────

def test_retry_works_in_a_resumed_session(workspace: Path, capsys) -> None:
    a = _agent(workspace, _Failing())
    a.run_turn("please answer")
    sid = a.session.active_session_id
    b = _agent(workspace, FakeProvider([_text("answered now")]))
    from axon.cli import _resume
    assert _resume(b, sid, lambda m: None, restore_mode=False)
    assert b.last_failed_turn == {"prompt": "please answer", "resume": False}
    dispatch_command("/retry", b)
    assert "Nothing to retry" not in capsys.readouterr().out
    assert b.conversation.messages[0]["content"] == "please answer"
    assert b.conversation.messages[-1]["role"] == "assistant"
    restore_retry_state(b, sid)
    assert b.last_failed_turn is None   # the session no longer ends on a failure


# ─── 3. /output after a resume ──────────────────────────────────────────────

def test_output_falls_back_to_recorded_tool_results(workspace: Path, capsys, monkeypatch) -> None:
    big = "".join(f"row {i:05d}\n" for i in range(4000))
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    (workspace / "small.txt").write_text("tiny-content\n", encoding="utf-8")
    turns = [AssistantTurn(blocks=[ToolUseBlock(id="t1", name="Bash", input={"command": "cat big.txt"})],
                           stop_reason="tool_use", usage=Usage(input=1, output=1)),
             AssistantTurn(blocks=[ToolUseBlock(id="t2", name="Read", input={"path": "small.txt"})],
                           stop_reason="tool_use", usage=Usage(input=1, output=1)),
             _text("done")]
    a = _agent(workspace, FakeProvider(turns), tool_output_cap=3000)
    a.run_turn("look")
    sid = a.session.active_session_id
    import axon.ui.render as render
    monkeypatch.setattr(render, "_TOOL_OUTPUTS", {})          # a new process after --continue
    monkeypatch.setattr(render, "_LAST_TOOL_OUTPUT", "")
    b = _agent(workspace, FakeProvider([]))
    b.session.open(sid)
    rows = b.session.recorded_tool_outputs()
    assert [r["n"] for r in rows] == [1, 2] and rows[0]["name"] == "Bash" and rows[1]["target"] == "small.txt"
    assert "row 03999" in rows[0]["content"] and "row 02000" in rows[0]["content"]   # complete, from the sidecar
    capsys.readouterr()
    dispatch_command("/output list", b)
    out = capsys.readouterr().out
    assert "Bash" in out and "small.txt" in out
    dispatch_command("/output 1", b)
    assert "row 02000" in capsys.readouterr().out
    dispatch_command("/output", b)
    assert "tiny-content" in capsys.readouterr().out


# ─── 4. MCP debug logging ───────────────────────────────────────────────────

def test_mcp_tool_calls_are_logged(caplog) -> None:
    from axon.mcp.bridge import MCPServerConnection

    class _Client:
        closed = False

        def call_tool(self, name: str, args: dict, timeout: float = 0) -> dict:
            return {"content": [{"type": "text", "text": f"echo {args['t']}"}]}

    conn = MCPServerConnection("srv", command="x")
    conn.client, conn.status = _Client(), "connected"
    with caplog.at_level(logging.DEBUG, logger="axon.mcp"):
        assert conn.call_tool("echo", {"t": "hi"}) == "echo hi"
        conn.stop()
    text = caplog.text
    assert "mcp srv: call echo" in text and "ok" in text and "mcp srv: stopping" in text


def test_debug_logging_is_set_up_before_the_agent_is_built(monkeypatch, tmp_path: Path, workspace: Path) -> None:
    import axon.cli as cli
    order: list[str] = []
    real = cli.setup_debug_logging
    monkeypatch.setattr(cli, "setup_debug_logging", lambda *a, **k: (order.append("debug"), real(*a, **k))[1])

    def fake_build(*a: Any, **k: Any) -> Any:
        order.append("build")
        raise cli.AxonError("stop here")

    monkeypatch.setattr(cli, "build_agent", fake_build)
    monkeypatch.setenv("AXON_API_KEY", "sk-test-1234567890")
    monkeypatch.chdir(workspace)
    code = cli.main(["-p", "--debug-file", str(tmp_path / "d.log"), "hi"])
    assert code == 2 and order[:2] == ["debug", "build"]
    logging.getLogger("axon").handlers.clear()


# ─── 5. /tools exact match ──────────────────────────────────────────────────

def test_tools_exact_match_first(workspace: Path, capsys) -> None:
    a = _agent(workspace, FakeProvider([]))
    capsys.readouterr()
    dispatch_command("/tools Bash", a)
    heads = [ln.split()[0] for ln in capsys.readouterr().out.splitlines() if "default" in ln and ln.startswith("  ")
             and not ln.startswith("   ")]
    assert heads == ["Bash"]
    dispatch_command("/tools bashout", a)
    assert "BashOutput" in capsys.readouterr().out


# ─── 6. JSON errors before the agent runs ──────────────────────────────────

@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_startup_error_is_json_for_json_formats(fmt: str, capsys) -> None:
    from axon.cli import _startup_error
    assert _startup_error(SimpleNamespace(output_format=fmt), "No API key configured", 2, "config_error") == 2
    cap = capsys.readouterr()
    data = json.loads(cap.out)
    assert data == {"type": "error", "subtype": "config_error", "is_error": True,
                    "error": "No API key configured", "exit_code": 2}
    assert "No API key" in cap.err
    _startup_error(SimpleNamespace(output_format="text"), "plain", 2)
    cap = capsys.readouterr()
    assert cap.out == "" and "plain" in cap.err
