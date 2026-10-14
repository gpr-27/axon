"""MCP Streamable HTTP and legacy HTTP+SSE transports, tested with httpx.MockTransport."""
from __future__ import annotations

import json
import queue
from pathlib import Path

import httpx
import pytest

from axon.mcp.bridge import MCPToolBridge
from axon.mcp.client import MCPClient, MCPError
from axon.mcp.config import ServerSpec
from axon.mcp.transports import LegacySSETransport, StreamableHTTPTransport, iter_sse
from axon.tools.registry import ToolRegistry

INIT_RESULT = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
               "serverInfo": {"name": "web", "version": "2"}}
TOOLS = [{"name": "search", "description": "Search", "inputSchema": {"type": "object"},
          "annotations": {"readOnlyHint": True}}]


class StreamableServer:
    """Records requests and answers like a Streamable HTTP MCP server."""

    def __init__(self, token: str = "secret", sse_for: tuple[str, ...] = ("tools/list", "tools/call")):
        self.token = token
        self.sse_for = sse_for
        self.requests: list[httpx.Request] = []
        self.deleted = False
        self.expire_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="unauthorized")
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            self.deleted = True
            return httpx.Response(200)
        msg = json.loads(request.content)
        method = msg.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": INIT_RESULT},
                                  headers={"Mcp-Session-Id": "sess-1"})
        assert request.headers.get("mcp-session-id") == "sess-1"
        assert request.headers.get("mcp-protocol-version") == "2025-06-18"
        if "id" not in msg:
            return httpx.Response(202)
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(404)
        if method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": f"found {msg['params']['arguments'].get('q')}"}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"],
                                             "error": {"code": -32601, "message": "nope"}})
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if method in self.sse_for:
            sse = ("event: message\ndata: "
                   + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                                 "params": {"progressToken": 1, "progress": 1}})
                   + f"\n\nevent: message\ndata: {json.dumps(body)}\n\n")
            return httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=body)


def _client(server: StreamableServer) -> MCPClient:
    http = httpx.Client(transport=httpx.MockTransport(server))
    t = StreamableHTTPTransport("https://mcp.example/mcp", {"Authorization": "Bearer secret"},
                                client=http, name="web")
    c = MCPClient("web", t, timeout=5)
    c.connect()
    return c


def test_streamable_http_json_and_sse():
    server = StreamableServer()
    c = _client(server)
    try:
        assert c.protocol_version == "2025-06-18"
        assert c.list_tools()[0]["name"] == "search"            # via SSE response
        assert c.call_tool("search", {"q": "x"})["content"][0]["text"] == "found x"
        with pytest.raises(MCPError):
            c.request("bogus/method")
        initialized = [json.loads(r.content) for r in server.requests
                       if r.method == "POST" and b"notifications/initialized" in r.content]
        assert initialized
        assert all("text/event-stream" in r.headers["accept"] for r in server.requests if r.method == "POST")
    finally:
        c.close()
    assert server.deleted  # session terminated with DELETE


def test_streamable_http_session_expiry_reinitializes():
    server = StreamableServer(sse_for=())
    c = _client(server)
    try:
        server.expire_once = True
        assert c.list_tools()[0]["name"] == "search"
        inits = [r for r in server.requests if r.method == "POST" and b'"initialize"' in r.content]
        assert len(inits) == 2
    finally:
        c.close()


def test_http_auth_failure_is_reported():
    server = StreamableServer(token="other")
    http = httpx.Client(transport=httpx.MockTransport(server))
    spec = ServerSpec(name="web", type="http", url="https://mcp.example/mcp",
                      headers={"Authorization": "Bearer ${WEB_TOKEN:-secret}"})
    bridge = MCPToolBridge(Path.cwd())
    bridge.http_client_factory = lambda s: http
    conn = bridge.connect_server(spec)
    assert conn.status == "failed" and "401" in (conn.error or "")
    bridge.disconnect_all()


def test_bridge_http_registers_tools_with_env_header(monkeypatch):
    monkeypatch.setenv("WEB_TOKEN", "secret")
    server = StreamableServer()
    bridge = MCPToolBridge(Path.cwd())
    bridge.http_client_factory = lambda s: httpx.Client(transport=httpx.MockTransport(server))
    reg = ToolRegistry([])
    bridge.attach_registry(reg)
    conn = bridge.connect_server(ServerSpec(name="web", type="http", url="https://mcp.example/mcp",
                                            headers={"Authorization": "Bearer ${WEB_TOKEN}"}))
    try:
        assert conn.connected, conn.error
        tool = reg.get("mcp__web__search")
        assert tool.readonly is True
        assert tool.run({"q": "cats"}, None) == "found cats"  # type: ignore[arg-type]
    finally:
        bridge.disconnect_all()
    assert "mcp__web__search" not in reg.tools


class LegacyServer:
    """HTTP+SSE (2024-11-05): GET opens the stream, POSTs go to the endpoint."""

    def __init__(self):
        self.q: "queue.Queue[str | None]" = queue.Queue()

    def _stream(self):
        yield b"event: endpoint\ndata: /messages?session=abc\n\n"
        while True:
            item = self.q.get()
            if item is None:
                return
            yield f"event: message\ndata: {item}\n\n".encode()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=self._stream())
        assert request.url.path == "/messages" and request.url.params["session"] == "abc"
        msg = json.loads(request.content)
        if "id" in msg and "method" in msg:
            if msg["method"] == "initialize":
                result = dict(INIT_RESULT, protocolVersion="2024-11-05")
            elif msg["method"] == "tools/list":
                result = {"tools": TOOLS}
            else:
                result = {}
            self.q.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}))
        return httpx.Response(202)


def test_legacy_sse_transport():
    server = LegacyServer()
    http = httpx.Client(transport=httpx.MockTransport(server))
    t = LegacySSETransport("https://old.example/sse", client=http, name="old", connect_timeout=5)
    c = MCPClient("old", t, timeout=5)
    try:
        c.connect()
        assert t.endpoint == "https://old.example/messages?session=abc"
        assert c.protocol_version == "2024-11-05"
        assert c.list_tools()[0]["name"] == "search"
    finally:
        server.q.put(None)
        c.close()


def test_iter_sse_parsing():
    lines = [": comment", "event: endpoint", "data: /a", "", "data: {\"x\":", "data: 1}", "", "id: 7"]
    events = list(iter_sse(lines))
    assert events[0] == ("endpoint", "/a", None)
    assert events[1] == ("message", "{\"x\":\n1}", None)
