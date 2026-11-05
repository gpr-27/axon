"""Claude Code parity for the MCP runtime: time limits, output limits, tool shaping, elicitation, roots,
reconnection, tool search and `axon mcp serve`."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import axon.mcp.bridge as bridge_mod
from axon.errors import ToolError
from axon.mcp import output as mcp_output
from axon.mcp.bridge import (
    MCPServerConnection, MCPToolBridge, MCPToolWrapper, description_limits, is_ui_resource, mcp_timeout_s,
    normalize_schema, tool_call_limits, tool_problem,
)
from axon.mcp.client import MCPClient, MCPError, MCPTimeout
from axon.mcp.config import ServerSpec
from axon.mcp.elicit import terminal_elicitation
from axon.mcp.transports import StdioTransport
from axon.tools.registry import ToolRegistry

SERVER = r'''
import json, os, sys, time

BIG = int(os.environ.get("BIG", "60000"))
TOOLS = [
    {"name": "big", "description": "big text", "inputSchema": {"type": "object"}},
    {"name": "bigmeta", "description": "big text, own limit", "inputSchema": {"type": "object"},
     "_meta": {"anthropic/maxResultSizeChars": 200000}},
    {"name": "err", "description": "long error", "inputSchema": {"type": "object"}},
    {"name": "anyof", "description": "Either a or b.", "inputSchema": {"anyOf": [
        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"properties": {"b": {"type": "string"}}, "required": ["b"]}]}},
    {"name": "allof", "description": "Both.", "inputSchema": {"allOf": [
        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"properties": {"b": {"type": "string"}}}]}},
    {"name": "badprop", "description": "x", "inputSchema": {"type": "object", "properties": {"bad name!": {}}}},
    {"name": "consent", "description": "asks every time", "inputSchema": {"type": "object"},
     "_meta": {"anthropic/requiresUserInteraction": True}},
    {"name": "always", "description": "load me", "inputSchema": {"type": "object"},
     "_meta": {"anthropic/alwaysLoad": True}},
    {"name": "lazy", "description": "x" * 5000, "inputSchema": {"type": "object"}},
    {"name": "progress", "description": "progress", "inputSchema": {"type": "object"}},
    {"name": "silent", "description": "silent", "inputSchema": {"type": "object"}},
    {"name": "ask", "description": "elicit", "inputSchema": {"type": "object"}},
    {"name": "caps", "description": "client caps", "inputSchema": {"type": "object"}},
    {"name": "roots", "description": "roots", "inputSchema": {"type": "object"}},
    {"name": "rootnotes", "description": "roots/list_changed count", "inputSchema": {"type": "object"}},
    {"name": "img", "description": "image", "inputSchema": {"type": "object"}},
]
caps = {}
root_notes = 0

def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()

def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)

def result(rid, r):
    send({"jsonrpc": "2.0", "id": rid, "result": r})

def text(t):
    return {"content": [{"type": "text", "text": t}]}

while True:
    msg = read()
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        if method == "notifications/roots/list_changed":
            root_notes += 1
        continue
    if method == "initialize":
        caps = params.get("capabilities") or {}
        result(rid, {"protocolVersion": "2025-06-18",
                     "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}},
                     "serverInfo": {"name": "fake", "version": "1"}, "instructions": "Prefer the big tool."})
    elif method == "tools/list":
        result(rid, {"tools": TOOLS})
    elif method == "resources/list":
        result(rid, {"resources": [{"uri": "file:///a.txt", "name": "a"}, {"uri": "ui://app/panel", "name": "ui"},
                                    {"uri": "file:///b.html", "name": "b", "mimeType": "text/html;profile=mcp-app"}]})
    elif method == "resources/templates/list":
        result(rid, {"resourceTemplates": []})
    elif method == "tools/call":
        name = params["name"]
        if name == "big":
            result(rid, text("x" * BIG))
        elif name == "bigmeta":
            result(rid, text("y" * 100000))
        elif name == "err":
            result(rid, {"content": [{"type": "text", "text": "E" * 5000 + "MIDDLE" * 2000 + "F" * 5000}], "isError": True})
        elif name == "progress":
            token = (params.get("_meta") or {}).get("progressToken")
            for i in range(6):
                time.sleep(0.2)
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": i + 1, "total": 6}})
            result(rid, text("progress done"))
        elif name == "silent":
            time.sleep(2.5)
            result(rid, text("late"))
        elif name == "ask":
            send({"jsonrpc": "2.0", "id": "e1", "method": "elicitation/create",
                  "params": {"mode": "form", "message": "Your name?",
                             "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                                                 "required": ["name"]}}})
            ans = read()
            result(rid, text(json.dumps(ans.get("result") or ans.get("error"))))
        elif name == "caps":
            result(rid, text(json.dumps(caps)))
        elif name == "roots":
            send({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"})
            ans = read()
            result(rid, text(json.dumps([r["uri"] for r in ans["result"]["roots"]])))
        elif name == "rootnotes":
            result(rid, text(str(root_notes)))
        elif name == "img":
            result(rid, {"content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]})
        else:
            result(rid, text("ok " + name))
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})
'''


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    p = tmp_path / "fake_parity_server.py"
    p.write_text(SERVER, encoding="utf-8")
    return p


def _spec(script: Path, **kw) -> ServerSpec:
    return ServerSpec(name="fk", type="stdio", command=sys.executable, args=[str(script)], **kw)


def _conn(script: Path, workspace: Path, **kw) -> MCPServerConnection:
    conn = MCPServerConnection("fk", spec=_spec(script), workspace=workspace, **kw)
    assert conn.start(timeout=15), conn.error
    return conn


def _tool(conn: MCPServerConnection, name: str) -> dict:
    return next(t for t in conn.tools if t["name"] == name)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("MAX_MCP_OUTPUT_TOKENS", "AXON_MAX_MCP_OUTPUT_TOKENS", "MCP_TIMEOUT", "MCP_TOOL_TIMEOUT",
                "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "AXON_MCP_TOOL_IDLE_TIMEOUT", "ENABLE_TOOL_SEARCH",
                "CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH", "AXON_MAX_MCP_DESCRIPTION_LENGTH"):
        monkeypatch.delenv(var, raising=False)


# ─── time limits ────────────────────────────────────────────────────────────

def test_tool_call_limits_follow_the_documented_rules(monkeypatch: pytest.MonkeyPatch):
    stdio, remote = ServerSpec(name="a", command="x"), ServerSpec(name="b", type="http", url="https://h")
    assert tool_call_limits(stdio) == (bridge_mod.DEFAULT_TOOL_TIMEOUT_S, 1800.0)
    assert tool_call_limits(remote) == (bridge_mod.DEFAULT_TOOL_TIMEOUT_S, 300.0)
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "90000")
    monkeypatch.setenv("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "20000")
    assert tool_call_limits(remote) == (90.0, 20.0)
    explicit = ServerSpec.from_dict("c", {"type": "http", "url": "https://h", "timeout": 600000})
    assert tool_call_limits(explicit) == (600.0, 600.0)         # per-server wins, and floors the idle window
    monkeypatch.setenv("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "0")
    assert tool_call_limits(remote)[1] is None                    # 0 turns the idle check off


def test_mcp_timeout_env_is_milliseconds(monkeypatch: pytest.MonkeyPatch):
    assert mcp_timeout_s(60.0) == 30.0
    monkeypatch.setenv("MCP_TIMEOUT", "60000")
    assert mcp_timeout_s(60.0) == 60.0


def test_progress_keeps_a_call_alive_but_never_extends_the_wall_clock(server_script: Path, tmp_path: Path):
    c = MCPClient("fk", StdioTransport("fk", sys.executable, [str(server_script)], {}, tmp_path), timeout=10)
    c.connect()
    try:
        started = time.time()
        res = c.call_tool("progress", {}, timeout=10, idle_timeout=0.6)    # 1.2s of work, progress every 0.2s
        assert res["content"][0]["text"] == "progress done" and time.time() - started > 1.0
        with pytest.raises(MCPTimeout, match="did not answer"):
            c.call_tool("progress", {}, timeout=0.5, idle_timeout=0.3)      # wall clock still bites
        with pytest.raises(MCPTimeout, match="no response or progress"):
            c.call_tool("silent", {}, timeout=10, idle_timeout=0.5)         # nothing at all: idle abort
        assert not c.progress                                               # tokens are not leaked
    finally:
        c.close()


# ─── output limits ──────────────────────────────────────────────────────────

def test_large_results_are_saved_to_a_file(server_script: Path, workspace: Path):
    conn = _conn(server_script, workspace)
    try:
        out = conn.call_tool("big", {}, _tool(conn, "big"))
        assert out.startswith("[MCP result from fk/big is too large") and "50,000" not in out
        path = Path(out.split("saved to ", 1)[1].split(". Read", 1)[0])
        assert path.read_text() == "x" * 60000
        # a tool's own limit replaces both defaults (100k chars is fine under a 200k annotation)
        assert conn.call_tool("bigmeta", {}, _tool(conn, "bigmeta")) == "y" * 100000
    finally:
        conn.stop()


def test_max_mcp_output_tokens_lowers_the_threshold(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    text = "z" * 4000                                                     # ~1,000 tokens, far under 50k chars
    assert mcp_output.limit_text(text, is_error=False, has_image=False, own_limit=None, workspace=workspace,
                                 label="s/t") == text
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "500")
    saved = mcp_output.limit_text(text, is_error=False, has_image=False, own_limit=None, workspace=workspace, label="s/t")
    assert "too large" in saved and "~1,000 tokens" in saved
    # the 50,000 character rule is independent of the variable
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "1000000")
    assert "too large" in mcp_output.limit_text("q" * 60000, is_error=False, has_image=False, own_limit=None,
                                                workspace=workspace, label="s/t")


def test_error_results_keep_first_and_last_5000(server_script: Path, workspace: Path):
    conn = _conn(server_script, workspace)
    try:
        with pytest.raises(ToolError) as ei:
            conn.call_tool("err", {}, _tool(conn, "err"))
        msg = str(ei.value)
        assert "characters removed" in msg and msg.count("E") >= 5000 and "F" * 5000 in msg and "MIDDLE" not in msg
    finally:
        conn.stop()


def test_tool_result_limit_is_clamped_and_images_are_saved(server_script: Path, workspace: Path):
    assert mcp_output.tool_result_limit({"_meta": {"anthropic/maxResultSizeChars": 9_999_999}}) == 500_000
    assert mcp_output.tool_result_limit({"_meta": {"anthropic/maxResultSizeChars": "no"}}) is None
    conn = _conn(server_script, workspace)
    try:
        out = conn.call_tool("img", {}, _tool(conn, "img"))
        assert "image/png, 5 bytes, saved to" in out
        assert Path(out.split("saved to ", 1)[1][:-1]).read_bytes() == b"hello"
    finally:
        conn.stop()


# ─── shaping tool definitions ───────────────────────────────────────────────

def test_root_level_combinators_are_flattened():
    schema, note = normalize_schema({"anyOf": [{"properties": {"a": {}}, "required": ["a"]},
                                               {"properties": {"b": {}}, "required": ["b"]}]})
    assert schema["type"] == "object" and set(schema["properties"]) == {"a", "b"}
    assert "anyOf" not in schema and "required" not in schema
    assert "(a) | (b)" in note
    schema, note = normalize_schema({"allOf": [{"properties": {"a": {}}, "required": ["a"]}, {"properties": {"b": {}}}]})
    assert schema["required"] == ["a"] and set(schema["properties"]) == {"a", "b"} and note == ""


def test_invalid_tools_are_left_out_and_reported(server_script: Path, workspace: Path):
    assert tool_problem({"inputSchema": {"properties": {"bad name!": {}}}})
    assert tool_problem({"inputSchema": {"properties": {"x" * 65: {}}}})
    assert tool_problem({"inputSchema": {"type": "object", "properties": {"ok.name-1_": {}}}}) is None
    conn = _conn(server_script, workspace)
    bridge = MCPToolBridge(workspace)
    try:
        wrappers = bridge._publish(conn)
        names = {w._mcp_tool_name for w in wrappers}
        assert "badprop" not in names and "anyof" in names
        assert bridge.excluded["fk"][0][0] == "badprop"
        anyof = next(w for w in wrappers if w._mcp_tool_name == "anyof")
        assert "anyOf" not in anyof.schema and "Provide one of these parameter groups" in anyof.description
    finally:
        conn.stop()


def test_description_caps(server_script: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch):
    conn = _conn(server_script, workspace)
    try:
        w = MCPToolWrapper("fk", _tool(conn, "lazy"), conn)
        assert len(w.description) == 4096 and len(w.full_description) <= 16384 and len(w.full_description) > 4096
        monkeypatch.setenv("CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH", "8192")
        assert description_limits() == (8192, 8192)
        assert len(MCPToolWrapper("fk", _tool(conn, "lazy"), conn).description) == 5000 + len("[MCP:fk] ")
    finally:
        conn.stop()


def test_ui_resources_are_hidden(server_script: Path, workspace: Path):
    assert is_ui_resource({"uri": "ui://x"}) and is_ui_resource({"uri": "file:///a", "mimeType": "text/html;profile=mcp-app"})
    conn = _conn(server_script, workspace)
    try:
        assert [r["uri"] for r in conn.resources()] == ["file:///a.txt"]
    finally:
        conn.stop()


def test_requires_user_interaction_always_asks_even_in_bypass_mode(server_script: Path, workspace: Path):
    from axon.config import Settings
    from axon.permissions.engine import PermissionEngine
    conn = _conn(server_script, workspace)
    try:
        consent = MCPToolWrapper("fk", _tool(conn, "consent"), conn)
        plain = MCPToolWrapper("fk", _tool(conn, "lazy"), conn)
        assert consent.requires_user_interaction and not plain.requires_user_interaction
        eng = PermissionEngine(Settings(workspace=workspace, mode="bypass"))
        assert eng.check(consent, {}).outcome == "ask"
        assert eng.check(plain, {}).outcome == "allow"
    finally:
        conn.stop()


# ─── tool search / deferral ─────────────────────────────────────────────────

def _registry_with(server_script: Path, workspace: Path, **spec_kw):
    spec = _spec(server_script, **spec_kw)
    bridge = MCPToolBridge(workspace)
    reg = ToolRegistry()
    bridge.attach_registry(reg)
    conn = bridge.connect_server(spec, timeout=15)
    assert conn.connected, conn.error
    return bridge, reg


def test_always_load_exempts_tools_and_servers_from_deferral(server_script: Path, workspace: Path, monkeypatch):
    from axon.tools.interactive import defer_mcp_tools
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    bridge, reg = _registry_with(server_script, workspace)
    try:
        search = defer_mcp_tools(reg, 200_000)
        assert search is not None
        assert "mcp__fk__always" in reg.visible_names()              # the tool's own alwaysLoad hint
        assert "mcp__fk__lazy" in search.deferred and "mcp__fk__always" not in search.deferred
    finally:
        bridge.disconnect_all()
    bridge, reg = _registry_with(server_script, workspace, always_load=True)
    try:
        search = defer_mcp_tools(reg, 200_000)                         # server alwaysLoad: none of its tools defer
        assert search is None or not any(n.startswith("mcp__fk__") for n in search.deferred)
        assert "mcp__fk__lazy" in reg.visible_names()
    finally:
        bridge.disconnect_all()
    bridge, reg = _registry_with(server_script, workspace, always_load=False)
    try:
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "auto")
        search = defer_mcp_tools(reg, 10_000_000)                      # far under the threshold, still deferred
        assert search is not None and "mcp__fk__always" in search.deferred
    finally:
        bridge.disconnect_all()


def test_enable_tool_search_modes(monkeypatch: pytest.MonkeyPatch):
    from axon.tools.interactive import parse_tool_search_mode
    assert parse_tool_search_mode("") == ("auto", 0.10) and parse_tool_search_mode("false")[0] == "off"
    assert parse_tool_search_mode("true")[0] == "always" and parse_tool_search_mode("auto")[0] == "auto"
    assert parse_tool_search_mode("auto:5") == ("auto", 0.05) and parse_tool_search_mode("auto:0") == ("auto", 0.0)


def test_tool_search_waits_for_connecting_servers_and_reports_failures(workspace: Path, monkeypatch):
    from axon.tools.interactive import ToolSearchTool
    bridge = MCPToolBridge(workspace)
    ok = MCPServerConnection("ok", spec=ServerSpec(name="ok", command="x"), workspace=workspace)
    ok.status = "connecting"
    dead = MCPServerConnection("dead", spec=ServerSpec(name="dead", command="x"), workspace=workspace)
    dead.status, dead.error = "failed", "Command not found: nope"
    bridge._connections.update({"ok": ok, "dead": dead})
    monkeypatch.setattr(bridge_mod, "_bridge", bridge)
    monkeypatch.setattr("axon.tools.interactive._wait_for_mcp", lambda pending, timeout=10.0: setattr(ok, "status", "stopped"))
    out = ToolSearchTool([]).run({"query": "anything"}, SimpleNamespace(agent=None))
    assert "No deferred tool matches" in out and "dead (failed: Command not found: nope)" in out


def test_server_instructions_reach_the_system_prompt(server_script: Path, workspace: Path, monkeypatch):
    from axon.agent.prompt import mcp_instructions_block
    bridge, _reg = _registry_with(server_script, workspace)
    try:
        monkeypatch.setattr(bridge_mod, "_bridge", bridge)
        block = mcp_instructions_block()
        assert "### fk" in block and "Prefer the big tool." in block
    finally:
        bridge.disconnect_all()


# ─── client capabilities: elicitation and roots ─────────────────────────────

def test_elicitation_form_is_answered_and_advertised_only_with_a_handler(server_script: Path, workspace: Path):
    asked = []

    def handler(server: str, params: dict) -> dict:
        asked.append((server, params["message"]))
        return {"action": "accept", "content": {"name": "Ada"}}

    conn = _conn(server_script, workspace, elicitation_handler=handler)
    try:
        caps = json.loads(conn.call_tool("caps", {}))
        assert caps["elicitation"] == {"form": {}, "url": {}} and caps["roots"] == {"listChanged": True}
        out = json.loads(conn.call_tool("ask", {}))
        assert out == {"action": "accept", "content": {"name": "Ada"}} and asked == [("fk", "Your name?")]
    finally:
        conn.stop()
    bare = _conn(server_script, workspace)
    try:
        assert "elicitation" not in json.loads(bare.call_tool("caps", {}))
    finally:
        bare.stop()


def test_terminal_elicitation_form_and_url_modes():
    answers = iter(["", "Ada", "", "y"])                    # blank required field -> asked again; optional skipped
    said: list[str] = []
    res = terminal_elicitation("srv", {"mode": "form", "message": "Who?", "requestedSchema": {
        "type": "object", "required": ["name"], "properties": {"name": {"type": "string"},
                                                              "age": {"type": "integer"}}}},
                               ask=lambda _p: next(answers), out=said.append)
    assert res == {"action": "accept", "content": {"name": "Ada"}} and any("required" in m for m in said)
    script = iter(["Ada", "42", ""])                       # name, age, submit(default yes)
    res = terminal_elicitation("srv", {"mode": "form", "requestedSchema": {
        "type": "object", "required": ["name"], "properties": {"name": {"type": "string"},
                                                              "age": {"type": "integer"}}}},
                               ask=lambda _p: next(script), out=said.append)
    assert res == {"action": "accept", "content": {"name": "Ada", "age": 42}}
    res = terminal_elicitation("srv", {"mode": "form", "requestedSchema": {"properties": {"x": {"type": "string"}}}},
                               ask=lambda _p: "cancel", out=said.append)
    assert res == {"action": "cancel"}
    opened: list[str] = []
    res = terminal_elicitation("srv", {"mode": "url", "message": "Sign in", "url": "https://x.example/auth"},
                               ask=lambda _p: "y", opener=opened.append, out=said.append)
    assert res == {"action": "accept"} and opened == ["https://x.example/auth"]
    assert terminal_elicitation("srv", {"mode": "url", "url": "https://x.example/a"}, ask=lambda _p: "n",
                                out=said.append) == {"action": "decline"}
    assert terminal_elicitation("srv", {"mode": "url", "url": "javascript:alert(1)"}, ask=lambda _p: "y",
                                out=said.append) == {"action": "decline"}
    assert terminal_elicitation("srv", {"mode": "url", "url": "https://x.example/" + "&" * 3000},
                                ask=lambda _p: "y", out=said.append) == {"action": "decline"}


def test_roots_include_additional_directories_and_changes_are_announced(server_script: Path, workspace: Path, tmp_path: Path):
    extra = tmp_path / "extra"
    extra.mkdir()
    bridge = MCPToolBridge(workspace)
    bridge.set_extra_roots([extra])
    conn = bridge.connect_server(_spec(server_script), timeout=15)
    try:
        roots = json.loads(conn.call_tool("roots", {}))
        assert workspace.resolve().as_uri() in roots and extra.resolve().as_uri() in roots
        assert conn.call_tool("rootnotes", {}) == "0"
        bridge.set_extra_roots([])
        deadline = time.time() + 3
        while conn.call_tool("rootnotes", {}) != "1" and time.time() < deadline:
            time.sleep(0.05)
        assert conn.call_tool("rootnotes", {}) == "1"
    finally:
        bridge.disconnect_all()


def test_stdio_servers_get_the_project_dir_in_their_environment(workspace: Path, tmp_path: Path):
    script = tmp_path / "env_server.py"
    script.write_text(SERVER.replace(
        'result(rid, text("ok " + name))',
        'result(rid, text(os.environ["CLAUDE_PROJECT_DIR"] + "|" + os.environ["AXON_PROJECT_DIR"]))'), encoding="utf-8")
    conn = _conn(script, workspace)
    try:
        assert conn.call_tool("lazy", {}) == f"{workspace}|{workspace}"
    finally:
        conn.stop()


# ─── retries and reconnection ───────────────────────────────────────────────

class _NoCloseClient(httpx.Client):
    def close(self) -> None:  # the server object outlives each transport
        pass


class FlakyServer:
    def __init__(self, fail_first: int = 0, status: int = 503, fail_methods: tuple[str, ...] = ("initialize",)) -> None:
        self.fail_first, self.status, self.fail_methods = fail_first, status, fail_methods
        self.calls: dict[str, int] = {}
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        msg = json.loads(request.content)
        method = msg.get("method")
        if self.down:
            return httpx.Response(500)
        self.calls[method] = self.calls.get(method, 0) + 1
        if method in self.fail_methods and self.calls[method] <= self.fail_first:
            return httpx.Response(self.status, text="busy")
        if "id" not in msg:
            return httpx.Response(202)
        result = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "h", "version": "1"}},
                  "tools/list": {"tools": [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]}
                  }.get(method, {})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})


def _http_conn(server: FlakyServer, **kw) -> MCPServerConnection:
    spec = ServerSpec.from_dict("h", {"type": "http", "url": "https://h/mcp", "headers": {"Authorization": "Bearer x"}})
    return MCPServerConnection("h", spec=spec, http_client=_NoCloseClient(transport=httpx.MockTransport(server)), **kw)


def test_first_connection_retries_transient_errors_three_times(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_mod, "CONNECT_RETRY_DELAYS", (0.0, 0.0, 0.0))
    srv = FlakyServer(fail_first=3)
    conn = _http_conn(srv, auto_reconnect=False)
    assert conn.start() is True and srv.calls["initialize"] == 4
    conn.stop()
    srv = FlakyServer(fail_first=4)
    conn = _http_conn(srv, auto_reconnect=False)
    assert conn.start() is False and conn.status == "failed" and srv.calls["initialize"] == 4


def test_auth_and_not_found_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_mod, "CONNECT_RETRY_DELAYS", (0.0, 0.0, 0.0))
    srv = FlakyServer(fail_first=9, status=401)
    conn = _http_conn(srv, auto_reconnect=False)
    conn.start()
    assert srv.calls["initialize"] == 1 and conn.status == "failed"      # configured Authorization rejected
    srv = FlakyServer(fail_first=9, status=404)
    conn = _http_conn(srv, auto_reconnect=False)
    conn.start()
    assert "endpoint not found at https://h" in (conn.error or "") and "/mcp" not in conn.error


def test_discovery_requests_retry_transient_errors(monkeypatch: pytest.MonkeyPatch):
    import axon.mcp.client as client_mod
    monkeypatch.setattr(client_mod, "DISCOVERY_RETRY_DELAYS", (0.0, 0.0, 0.0))
    srv = FlakyServer(fail_first=2, fail_methods=("tools/list",))
    conn = _http_conn(srv, auto_reconnect=False)
    assert conn.start() is True and srv.calls["tools/list"] == 3
    conn.stop()


def test_dropped_remote_server_reconnects_with_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_mod, "RECONNECT_DELAYS", (0.01, 0.01, 0.01, 0.01, 0.01))
    monkeypatch.setattr(bridge_mod, "CONNECT_RETRY_DELAYS", ())
    srv = FlakyServer()
    seen: list[str] = []
    conn = _http_conn(srv)
    conn.on_tools_changed = lambda c: seen.append(c.status)
    assert conn.start()
    conn.client._on_transport_closed("network dropped")
    deadline = time.time() + 5
    while "connected" not in seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen == ["connected"] and conn.connected
    # a server that stays down: five attempts, then failed with a pointer to /mcp
    srv.down = True
    conn.client._on_transport_closed("again")
    deadline = time.time() + 5
    while conn.status != "failed" and time.time() < deadline:
        time.sleep(0.02)
    assert conn.status == "failed" and "reconnect from /mcp" in (conn.error or "")
    conn.stop()


def test_stop_cancels_a_running_reconnect(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_mod, "RECONNECT_DELAYS", (0.2, 0.2))
    srv = FlakyServer()
    conn = _http_conn(srv)
    conn.start()
    conn.client._on_transport_closed("x")
    conn.stop()
    time.sleep(0.6)
    assert conn.status == "stopped"


def test_idempotent_call_is_repeated_once_on_a_new_connection(monkeypatch: pytest.MonkeyPatch):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        msg = json.loads(request.content)
        method = msg.get("method")
        if "id" not in msg:
            return httpx.Response(202)
        if method == "tools/call":
            state["calls"] += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                             "result": {"content": [{"type": "text", "text": f"call {state['calls']}"}]}})
        return FlakyServer()(request)

    spec = ServerSpec.from_dict("h", {"type": "http", "url": "https://h/mcp", "headers": {"Authorization": "Bearer x"}})
    conn = MCPServerConnection("h", spec=spec, http_client=_NoCloseClient(transport=httpx.MockTransport(handler)),
                               auto_reconnect=False)
    conn.start()
    real = conn.client.call_tool
    boom = {"n": 0}

    def flaky(*a, **kw):
        boom["n"] += 1
        if boom["n"] == 1:
            conn.client.closed = True
            raise MCPError("connection closed before the tool call was answered")
        return real(*a, **kw)

    conn.client.call_tool = flaky  # type: ignore[method-assign]
    safe = {"name": "t", "annotations": {"readOnlyHint": True}}
    assert conn.call_tool("t", {}, safe) == "call 1"
    boom["n"] = 0
    conn.client.call_tool = flaky  # type: ignore[method-assign]
    conn.client.closed = False
    with pytest.raises(ToolError):
        conn.call_tool("t", {}, {"name": "t"})              # not marked safe: not repeated
    conn.stop()


# ─── axon mcp serve ─────────────────────────────────────────────────────────

def _rpc(server, method: str, params: dict | None = None, rid: int = 1) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def test_serve_exposes_axon_tools(workspace: Path):
    from axon.mcp.serve import AxonMCPServer
    (workspace / "hello.txt").write_text("hi there\n", encoding="utf-8")
    server = AxonMCPServer(workspace)
    init = _rpc(server, "initialize", {"protocolVersion": "2025-03-26"})
    assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = {t["name"]: t for t in _rpc(server, "tools/list")["result"]["tools"]}
    assert {"Read", "Write", "Edit", "Bash", "Grep", "Glob"} <= set(tools)
    assert not ({"Task", "AskUserQuestion", "ToolSearch", "ExitPlanMode"} & set(tools))
    assert tools["Read"]["annotations"]["readOnlyHint"] is True and tools["Bash"]["annotations"]["readOnlyHint"] is False
    out = _rpc(server, "tools/call", {"name": "Read", "arguments": {"file_path": str(workspace / "hello.txt")}})["result"]
    assert out["isError"] is False and "hi there" in out["content"][0]["text"]
    assert _rpc(server, "tools/call", {"name": "Nope", "arguments": {}})["result"]["isError"] is True
    assert _rpc(server, "tools/call", {"name": "Read", "arguments": {"file_path": "/definitely/not/here"}})["result"]["isError"] is True
    assert _rpc(server, "bogus")["error"]["code"] == -32601


def test_serve_over_stdio_keeps_stdout_clean(workspace: Path):
    from axon.mcp.serve import serve_stdio
    lines = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"})) + "\nnot json\n"
    out = io.StringIO()
    assert serve_stdio(workspace, io.StringIO(lines), out) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None] and replies[2]["error"]["code"] == -32700


def test_axon_mcp_serve_subcommand_speaks_json_rpc(tmp_path: Path):
    code = "import sys; from axon.cli import main; sys.exit(main(['mcp', 'serve']))"
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                          input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n",
                          env={**__import__("os").environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert [json.loads(line) for line in proc.stdout.splitlines()] == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_failure_text_never_shows_the_expanded_url_or_credentials():
    from axon.mcp.bridge import _redact
    spec = ServerSpec.from_dict("x", {"type": "http", "url": "https://h.example/mcp?key=SECRETKEY123",
                                      "headers": {"Authorization": "Bearer abcdefghijkl"}})
    shown = _redact("HTTP 500 from https://h.example/mcp?key=SECRETKEY123: Bearer abcdefghijkl api_key=zzzzzz1234",
                    spec, spec)
    assert shown == "HTTP 500 from https://h.example: Bearer [REDACTED] api_key=[REDACTED]"


def test_embedding_never_prompts_unless_the_cli_says_it_is_interactive():
    from axon.mcp import state
    state.set_interactive(None)
    assert state.is_interactive() is False
    state.set_interactive(True)
    assert state.is_interactive() is True
    state.set_interactive(None)
