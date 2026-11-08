"""Esc must stop a running turn quickly in every phase, not only while the model streams.

Esc raises KeyboardInterrupt in the turn's thread (Agent.cancel_current_turn). Python delivers it only
between bytecodes, so any wait that sits in one long C call (time.sleep, Future.result, a socket read,
Popen.communicate) used to hold it until the call returned. These tests interrupt each such wait the
same way and check that it ends within a second.
"""
from __future__ import annotations

import ctypes
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
from pydantic import SecretStr

from axon import interruptible
from axon.config import Settings
from axon.hooks.manager import HookManager
from axon.providers.openai_compat import OpenAICompatProvider
from axon.tools import _http
from tests.fakes import FakeProvider, scripted
from tests.suite.test_mcp_fakes import fake_server  # noqa: F401
from tests.test_core_helpers import make_agent, make_settings

QUICK = 1.5   # seconds an interrupted wait may still take (the slices are 0.1 s)


def _interrupt_after(delay: float, fn: Callable[[], Any], ready: Callable[[], bool] | None = None) -> tuple[float, str]:
    """Run fn on a worker thread, raise KeyboardInterrupt in it after `delay` (what Esc does), and
    return (seconds from the interrupt to the end, name of the exception fn ended with)."""
    out: dict[str, Any] = {"error": ""}
    finished = threading.Event()

    def work() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            out["error"] = type(e).__name__
        finally:
            out["end"] = time.monotonic()
            finished.set()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    time.sleep(delay)
    if ready is not None:                  # wait for the work to really be under way (a slow CI start-up) before Esc
        limit = time.monotonic() + 20
        while not ready() and time.monotonic() < limit and not finished.is_set():
            time.sleep(0.05)
    sent = time.monotonic()
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(worker.ident), ctypes.py_object(KeyboardInterrupt))
    finished.wait(10)
    return out.get("end", time.monotonic()) - sent, out["error"]


def _cancel_later(agent: Any, delay: float) -> None:
    def fire() -> None:
        time.sleep(delay)
        agent.cancel_current_turn()
    threading.Thread(target=fire, daemon=True).start()


def _alive(pid: int) -> bool:
    if os.name == "nt":                      # os.kill(pid, 0) is not a liveness probe on Windows
        from axon.session.lease import _pid_alive
        return _pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_file(path: Path, seconds: float = 5.0) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} was not written")


# ── waiting for the model ───────────────────────────────────────────────────

def test_interruptible_sleep_stops_at_once() -> None:
    took, error = _interrupt_after(0.2, lambda: interruptible.sleep(30))
    assert error == "KeyboardInterrupt" and took < QUICK


def test_esc_during_rate_limit_backoff_ends_the_turn(workspace: Path) -> None:
    """HTTP 429 with Retry-After: 30 made the provider sleep 30 s in one time.sleep call."""
    def always_busy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "30"}, json={"error": {"message": "slow down"}})

    settings = Settings(base_url="https://api.x.ai/v1", api_key=SecretStr("sk-test-key"))
    provider = OpenAICompatProvider(settings, http_client=httpx.Client(transport=httpx.MockTransport(always_busy)))
    agent = make_agent(provider, workspace, base_url="https://api.x.ai/v1", model="grok-4")
    _cancel_later(agent, 0.4)
    started = time.monotonic()
    res = agent.run_turn_threaded("hello")
    assert time.monotonic() - started < 0.4 + QUICK
    assert res is not None and res.stop_reason == "interrupted"
    agent.conversation.validate()


def test_compaction_provider_stream_is_aborted_too(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    cancelled: list[Any] = []
    agent.provider.cancel = lambda ident=None: cancelled.append(("main", ident))  # type: ignore[attr-defined]
    agent._side_provider = SimpleNamespace(cancel=lambda ident=None: cancelled.append(("side", ident)))
    agent._cancel_provider_stream(7)
    assert cancelled == [("main", 7), ("side", 7)]


# ── tools ───────────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="uses sh and kill -0")
def test_esc_kills_a_long_bash_command(workspace: Path) -> None:
    pid_file = workspace / "bash.pid"
    command = f"echo $$ > '{pid_file}'; sleep 30"
    agent = make_agent(FakeProvider(scripted(("Bash", {"command": command, "description": "wait"}), "x")), workspace)
    _cancel_later(agent, 0.8)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn_threaded("wait")
    assert time.monotonic() - started < 0.8 + QUICK + 1.0      # + the SIGTERM grace of kill_process_tree
    assert not _alive(int(_wait_for_file(pid_file)))
    agent.conversation.validate()                               # the Bash call got an "Interrupted" result
    last = agent.conversation.messages[-1]["content"][0]
    assert last["is_error"] and "Interrupted" in str(last["content"])


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(10)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"late")
        except OSError:
            pass

    def log_message(self, *args: Any) -> None:
        pass


def test_esc_stops_a_web_fetch_waiting_on_a_slow_server() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/page"
    try:
        took, error = _interrupt_after(0.3, lambda: _http.fetch(url, timeout=30))
        assert error == "KeyboardInterrupt" and took < QUICK
    finally:
        server.shutdown()


def test_esc_stops_waiting_for_an_mcp_tool(fake_server: Path, tmp_path: Path) -> None:  # noqa: F811
    from axon.mcp.client import MCPClient
    from axon.mcp.transports import StdioTransport
    client = MCPClient("never", StdioTransport("never", sys.executable, [str(fake_server)], {}, tmp_path),
                       timeout=60, workspace=tmp_path)
    client.connect()
    try:
        took, error = _interrupt_after(0.3, lambda: client.call_tool("slow", {"seconds": 5}, timeout=60))
        assert error == "KeyboardInterrupt" and took < QUICK
        assert not client._pending                 # the abandoned request is forgotten
    finally:
        client.close()


# ── hooks ───────────────────────────────────────────────────────────────────

def _sleeping_hook(tmp: Path, name: str) -> tuple[str, Path]:
    pid_file = tmp / f"{name}.pid"
    script = tmp / f"{name}.py"
    script.write_text(f"import os, time\nopen(r'{pid_file}', 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
                      encoding="utf-8")
    return f'"{sys.executable}" "{script}"', pid_file


def _hook_settings(ws: Path, commands: list[str]) -> None:
    d = ws / ".axon"
    d.mkdir(parents=True, exist_ok=True)
    hooks = {"PreToolUse": [{"hooks": [{"type": "command", "command": c, "timeout": 60} for c in commands]}]}
    (d / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


@pytest.mark.parametrize("count", [1, 2])
def test_esc_kills_running_hook_commands(workspace: Path, tmp_path: Path, count: int) -> None:
    hooks = [_sleeping_hook(tmp_path, f"hook{i}") for i in range(count)]
    _hook_settings(workspace, [cmd for cmd, _ in hooks])
    manager = HookManager(make_settings(workspace), workspace)
    pid_files = [pid for _, pid in hooks]

    def run() -> None:
        manager.run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})

    took, error = _interrupt_after(1.0, run, ready=lambda: all(p.exists() and p.read_text().strip() for p in pid_files))
    assert error == "KeyboardInterrupt" and took < QUICK + 1.0      # + the SIGTERM grace
    for pid_file in pid_files:
        pid = int(_wait_for_file(pid_file))
        end = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < end:
            time.sleep(0.05)
        assert not _alive(pid)


# ── subagents ───────────────────────────────────────────────────────────────

def test_cancelling_a_subagent_aborts_its_model_stream() -> None:
    from axon.agent.subagent import SubagentManager
    manager = SubagentManager()
    task = manager.register("look around")
    aborted: list[int] = []
    task.live_agent = SimpleNamespace(_cancel_provider_stream=lambda ident: aborted.append(ident))
    stop = threading.Event()
    interrupted: list[bool] = []

    def child() -> None:
        while not stop.is_set():
            try:
                time.sleep(0.01)
            except KeyboardInterrupt:
                interrupted.append(True)
                return
    t = threading.Thread(target=child, daemon=True)
    t.start()
    manager.bind_thread(task.id, t.ident)
    assert manager.cancel(task.id)
    t.join(2)
    stop.set()
    assert interrupted == [True] and aborted == [t.ident]


def test_esc_on_the_subagent_monitor_overview_stops_the_turn() -> None:
    from axon.ui.subagent_monitor import MonitorKeys
    keys = MonitorKeys(None)
    keys.view = 1
    assert keys.handle("esc", []) is None and keys.view == 0     # first Esc: back to the overview
    assert keys.handle("esc", []) == "interrupt"                  # on the overview: stop
    keys.buf = "half typed"
    assert keys.handle("esc", []) is None and keys.buf == ""      # Esc clears a typed note first


# ── the key listener and a second Esc ───────────────────────────────────────

def test_second_esc_does_not_print_interrupting_twice(capsys: pytest.CaptureFixture[str]) -> None:
    from axon.ui.in_flight import InFlightInputListener
    calls: list[int] = []
    listener = InFlightInputListener(SimpleNamespace(cancel_current_turn=lambda: calls.append(1)))
    listener.handle_key("esc")
    listener.handle_key("esc")
    assert len(calls) == 2
    assert capsys.readouterr().out.count("Interrupting") == 1


def test_a_failing_key_does_not_stop_the_listener(capsys: pytest.CaptureFixture[str]) -> None:
    from axon.ui.in_flight import InFlightInputListener
    calls: list[int] = []
    listener = InFlightInputListener(SimpleNamespace(cancel_current_turn=lambda: calls.append(1)))

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("queue is broken")
    listener._submit_line = broken  # type: ignore[method-assign]
    for key in ("h", "i", "enter", "esc"):
        listener._handle_key_safely(key)
    assert calls == [1]
    assert "queue is broken" in capsys.readouterr().out


def test_repeated_cancel_raises_only_one_interrupt(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    hits: list[int] = []
    stop = threading.Event()

    def turn() -> None:
        while not stop.is_set():
            try:
                time.sleep(0.01)
            except KeyboardInterrupt:
                hits.append(1)
    t = threading.Thread(target=turn, daemon=True)
    t.start()
    agent._turn_thread = t
    agent.cancel_current_turn()
    agent.cancel_current_turn()                  # Esc pressed twice
    time.sleep(0.3)
    stop.set()
    t.join(2)
    assert hits == [1]


# ── after the interrupt: the end-of-turn summary ────────────────────────────

def test_interrupted_tool_turn_prints_the_stopped_summary(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon import cli
    from axon.tools import create_default_registry
    from axon.tools.base import Tool, ToolContext

    class Spin(Tool):
        name = "Spin"
        description = "spins"
        schema = {"type": "object", "properties": {}}
        readonly = False
        default_permission = "allow"

        def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
            interruptible.sleep(30)
            return "never"

    registry = create_default_registry()
    registry.register(Spin())
    agent = make_agent(FakeProvider(scripted(("Spin", {}), "x")), workspace, registry=registry)
    footers: list[dict[str, Any]] = []
    renderer = SimpleNamespace(turn_footer=lambda **kw: footers.append(kw))
    monkeypatch.setattr(cli, "_use_turn_thread", lambda _agent: True)
    _cancel_later(agent, 0.4)
    started = time.monotonic()
    cli._run_agent_turn(agent, renderer, "spin", "Turn")
    assert time.monotonic() - started < 0.4 + QUICK
    assert len(footers) == 1
    assert footers[0]["stop_reason"] == "interrupted" and footers[0]["llm_calls"] == 1
    agent.conversation.validate()
