"""End-to-end tests of the MCP client over stdio against a fake server subprocess."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from axon.errors import ToolError
from axon.mcp.bridge import MCPServerConnection, MCPToolBridge, mcp_tool_name
from axon.mcp.client import MCPClient, MCPError, MCPTimeout
from axon.mcp.transports import StdioTransport, log_path_for
from axon.tools.registry import ToolRegistry
from tests.suite.test_mcp_fakes import fake_server, stdio_spec  # noqa: F401


def _client(script: Path, ws: Path, name: str = "fake", env=None, timeout: float = 10.0) -> MCPClient:
    t = StdioTransport(name, sys.executable, [str(script)], env or {}, ws)
    c = MCPClient(name, t, timeout=timeout, workspace=ws)
    c.connect()
    return c


def test_handshake_pagination_and_calls(fake_server: Path, tmp_path: Path):
    c = _client(fake_server, tmp_path)
    try:
        assert c.protocol_version == "2025-06-18"
        assert c.server_info["name"] == "fake"
        assert c.instructions == "be nice"
        tools = c.list_tools()
        assert [t["name"] for t in tools][:3] == ["echo", "fail", "image"]
        assert len(tools) == 7  # both pages fetched
        res = c.call_tool("echo", {"text": "hi"})
        assert res["content"][0]["text"] == "echo: hi"
        with pytest.raises(MCPError) as ei:
            c.call_tool("nope", {})
        assert ei.value.code == -32602
        assert c.ping()
    finally:
        c.close()
    log = log_path_for("fake").read_text(encoding="utf-8")
    assert "fake server starting" in log          # stderr captured to the log file
    assert "non-JSON stdout" in log               # stdout noise tolerated
    assert "echo called" in log                   # notifications/message logged


def test_version_negotiation_down(fake_server: Path, tmp_path: Path):
    c = _client(fake_server, tmp_path, env={"FAKE_MCP_VERSION": "2024-11-05"})
    try:
        assert c.protocol_version == "2024-11-05"
    finally:
        c.close()


def test_unsupported_version_rejected(fake_server: Path, tmp_path: Path):
    t = StdioTransport("old", sys.executable, [str(fake_server)], {"FAKE_MCP_VERSION": "1999-01-01"}, tmp_path)
    c = MCPClient("old", t, timeout=10, workspace=tmp_path)
    with pytest.raises(MCPError, match="unsupported"):
        c.connect()
    c.close()


def test_timeout_sends_cancel_and_ids_stay_matched(fake_server: Path, tmp_path: Path):
    c = _client(fake_server, tmp_path, name="slowpoke")
    try:
        with pytest.raises(MCPTimeout):
            c.call_tool("slow", {"seconds": 1.0}, timeout=0.2)
        # The late reply must not be delivered to the next request.
        res = c.call_tool("echo", {"text": "after"})
        assert res["content"][0]["text"] == "echo: after"
    finally:
        c.close()
    # initialize was id 1, the slow call id 2: the server saw notifications/cancelled for it
    assert "cancelled 2" in log_path_for("slowpoke").read_text(encoding="utf-8")


def test_server_requests_roots_and_sampling_rejected(fake_server: Path, tmp_path: Path):
    c = _client(fake_server, tmp_path)
    try:
        res = c.call_tool("roots", {})
        payload = json.loads(res["content"][0]["text"])
        assert payload["roots"][0]["uri"] == tmp_path.resolve().as_uri()
        assert payload["sampling_rejected"] is True
    finally:
        c.close()


def test_resources_and_prompts(fake_server: Path, tmp_path: Path):
    c = _client(fake_server, tmp_path)
    try:
        assert c.list_resources()[0]["uri"] == "file:///readme.md"
        assert c.list_resource_templates()[0]["uriTemplate"] == "file:///{path}"
        assert c.read_resource("file:///readme.md")["contents"][0]["text"] == "# Hello"
        assert c.list_prompts()[0]["name"] == "review"
        msgs = c.get_prompt("review", {"lang": "go"})["messages"]
        assert msgs[0]["content"]["text"] == "Review this go code"
    finally:
        c.close()


def test_process_exit_fails_pending(tmp_path: Path):
    script = tmp_path / "dies.py"
    script.write_text("import sys\nsys.stdin.readline()\nsys.exit(3)\n", encoding="utf-8")
    t = StdioTransport("dies", sys.executable, [str(script)], {}, tmp_path)
    c = MCPClient("dies", t, timeout=5, workspace=tmp_path)
    with pytest.raises(MCPError):
        c.connect()
    assert c.closed
    c.close()


def test_missing_command_reports_error(tmp_path: Path):
    from axon.mcp.config import ServerSpec
    conn = MCPServerConnection("ghost", spec=ServerSpec(name="ghost", command="definitely-not-a-cmd-xyz"),
                               workspace=tmp_path)
    assert conn.start() is False
    assert conn.status == "failed" and "not found" in (conn.error or "").lower()


def test_bridge_registers_tools_and_renders(fake_server: Path, tmp_path: Path):
    bridge = MCPToolBridge(tmp_path)
    reg = ToolRegistry([])
    bridge.attach_registry(reg)
    try:
        conn = bridge.connect_server(stdio_spec("fake", fake_server))
        assert conn.connected, conn.error
        names = set(reg.tools)
        assert "mcp__fake__echo" in names
        assert "mcp__list_resources" in names and "mcp__read_resource" in names
        echo = reg.get("mcp__fake__echo")
        assert echo.readonly is True                         # annotations.readOnlyHint
        assert reg.get("mcp__fake__fail").readonly is False
        assert echo.run({"text": "yo"}, None) == "echo: yo"  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="boom"):
            reg.get("mcp__fake__fail").run({}, None)  # type: ignore[arg-type]
        assert reg.get("mcp__fake__image").run({}, None) == "[image: image/png, 5 bytes]"  # type: ignore[arg-type]
        assert '"temp": 21' in reg.get("mcp__fake__structured").run({}, None)  # type: ignore[arg-type]
        assert "file:///readme.md" in reg.get("mcp__list_resources").run({}, None)  # type: ignore[arg-type]
        assert "# Hello" in reg.get("mcp__read_resource").run(  # type: ignore[arg-type]
            {"server": "fake", "uri": "file:///readme.md"}, None)
        assert bridge.list_prompts() == [("fake", "review", "Review code", [{"name": "lang", "required": True}])]
        assert bridge.get_prompt("fake", "review", {"lang": "py"}) == "Review this py code"

        # tools/list_changed -> refreshed and re-registered live
        reg.get("mcp__fake__grow").run({}, None)  # type: ignore[arg-type]
        deadline = time.time() + 5
        while "mcp__fake__late_tool" not in reg.tools and time.time() < deadline:
            time.sleep(0.05)
        assert "mcp__fake__late_tool" in reg.tools

        # no duplicate connections; disconnect unregisters
        bridge.connect_all({"fake": stdio_spec("fake", fake_server)}, timeout=5)
        assert len(bridge.connections) == 1 and bridge.connections["fake"] is conn
        assert bridge.disconnect_server("fake")
        assert not any(n.startswith("mcp__") for n in reg.tools)
    finally:
        bridge.disconnect_all()


def test_connect_all_parallel_with_deadline(fake_server: Path, tmp_path: Path):
    hang = tmp_path / "hang.py"
    hang.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    bridge = MCPToolBridge(tmp_path)
    reg = ToolRegistry([])
    bridge.attach_registry(reg)
    try:
        start = time.monotonic()
        bridge.connect_all({
            "good": stdio_spec("good", fake_server),
            "hang": stdio_spec("hang", hang, timeout=20),
            "broken": stdio_spec("broken", tmp_path / "missing.py"),
        }, timeout=3)
        assert time.monotonic() - start < 6
        st = {r["name"]: r["status"] for r in bridge.status_rows()}
        assert st["good"] == "connected"
        assert st["hang"] == "connecting"
        assert st["broken"] == "failed"
        assert "mcp__good__echo" in reg.tools
        assert "failed: broken" in bridge.startup_summary()
        t0 = time.monotonic()
        bridge.disconnect_all()
        assert time.monotonic() - t0 < 5  # aborting a hanging handshake is fast
    finally:
        bridge.disconnect_all()


def test_tool_name_sanitizing():
    assert mcp_tool_name("my server", "do.thing/now") == "mcp__my_server__do_thing_now"
    long = mcp_tool_name("s" * 40, "t" * 40)
    assert len(long) == 64
    import re
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", long)


def test_resolve_command_uses_path(tmp_path: Path):
    import os
    import stat
    from axon.mcp.transports import build_argv, resolve_command
    name = "fakenpx.cmd" if os.name == "nt" else "fakenpx"
    exe = tmp_path / name
    exe.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
    assert Path(resolve_command("fakenpx", env)).resolve() == exe.resolve()
    assert build_argv("fakenpx", ["-y", "pkg"], env)[1:] == ["-y", "pkg"]
    assert resolve_command("not-on-path-xyz", env) == "not-on-path-xyz"
