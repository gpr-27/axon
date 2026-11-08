"""stdio MCP servers: a slow first start recovers late; a crashed server reconnects in the background."""
from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from axon.mcp import bridge
from axon.mcp.config import ServerSpec

SERVER = textwrap.dedent('''
    import json, os, sys, time
    delay = float(os.environ.get("SLOW_START", "0"))
    marker = os.environ.get("RUN_MARKER", "")
    if marker:
        with open(marker, "a") as f:
            f.write("start\\n")
    time.sleep(delay)
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": msg["params"].get("protocolVersion"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "t", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": [{"name": "echo", "description": "d", "inputSchema": {"type": "object"}},
                             {"name": "die", "description": "d", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            if msg["params"]["name"] == "die":
                os._exit(1)
            res = {"content": [{"type": "text", "text": "ok"}]}
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
        sys.stdout.flush()
''')


def spec_for(tmp: Path, **env) -> ServerSpec:
    script = tmp / "srv.py"
    script.write_text(SERVER)
    return ServerSpec.from_dict("slow", {"command": sys.executable, "args": [str(script)], "env": env, "timeout": 1.0})


def wait_for(cond, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_slow_start_missing_the_timeout_recovers_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LATE_START_RETRIES", ((0.2, 3.0),), raising=False)
    b = bridge.MCPToolBridge(tmp_path)
    try:
        conn = b.connect_server(spec_for(tmp_path, SLOW_START="1.6"))
        assert conn.status == "failed"                              # missed the 1 s connect timeout
        assert wait_for(lambda: conn.status == "connected"), (conn.status, conn.error)
        assert any(t["name"] == "echo" for t in conn.tools)
    finally:
        b.disconnect_all()


def test_a_server_that_never_starts_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LATE_START_RETRIES", ((0.1, 1.0), (0.1, 1.0)), raising=False)
    marker = tmp_path / "runs.txt"
    b = bridge.MCPToolBridge(tmp_path)
    try:
        conn = b.connect_server(spec_for(tmp_path, SLOW_START="6", RUN_MARKER=str(marker)))
        assert conn.status == "failed"
        time.sleep(1.0)
        assert wait_for(lambda: conn.status == "failed" and marker.read_text().count("start") == 3, 20)
        time.sleep(2.0)
        assert conn.status == "failed"
        assert marker.read_text().count("start") == 3               # first try + two retries, then it stops
    finally:
        b.disconnect_all()


def test_crashed_stdio_server_reconnects_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LOCAL_RECONNECT_DELAYS", (0.1, 0.2, 0.4), raising=False)
    marker = tmp_path / "runs.txt"
    b = bridge.MCPToolBridge(tmp_path)
    try:
        conn = b.connect_server(spec_for(tmp_path, RUN_MARKER=str(marker)))
        assert conn.status == "connected"
        try:
            conn.call_tool("die", {})
        except Exception:
            pass
        assert wait_for(lambda: marker.read_text().count("start") >= 2 and conn.status == "connected"), conn.status
    finally:
        b.disconnect_all()


def test_a_server_that_keeps_crashing_is_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LOCAL_RECONNECT_DELAYS", (0.05, 0.05, 0.05), raising=False)
    monkeypatch.setattr(bridge, "CRASH_BUDGET", 2, raising=False)
    marker = tmp_path / "runs.txt"
    b = bridge.MCPToolBridge(tmp_path)
    try:
        conn = b.connect_server(spec_for(tmp_path, RUN_MARKER=str(marker)))
        for _ in range(5):
            if conn.status != "connected":
                wait_for(lambda: conn.status in ("connected", "failed"), 5)
            try:
                conn.call_tool("die", {})
            except Exception:
                pass
            time.sleep(0.5)
        assert wait_for(lambda: conn.status == "failed", 8)
        assert "crash" in (conn.error or "").lower()
    finally:
        b.disconnect_all()


def test_unauthenticated_message_names_the_documented_command(tmp_path):
    from axon.mcp.transports import TransportError
    conn = bridge.MCPServerConnection("remote", spec=ServerSpec.from_dict("remote", {"type": "http", "url": "https://x.example/mcp"}))
    msg = conn._auth_error(TransportError("401", status=401, headers={}), conn.spec, None)
    assert "/mcp login remote" in msg and "authenticate" not in msg
