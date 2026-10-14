"""
A tiny but real MCP server for end-to-end tests (no dependencies).

* stdio mode (default): ``python fake_mcp_server.py [--name N] [--fail] [--log FILE]`` - JSON-RPC 2.0,
  one message per line on stdin/stdout.
* HTTP mode, in-process: :func:`start_http_server` starts a threaded Streamable-HTTP endpoint
  (``POST /mcp`` answers JSON or, when ``sse=True``, a one-shot ``text/event-stream``; it issues an
  ``Mcp-Session-Id`` on initialize and requires it afterwards; ``GET`` -> 405, ``DELETE`` -> 200).

Capabilities: tools (paginated ``tools/list``: page 1 = echo, add; page 2 = env_secret, boom),
resources (``mem://notes``), prompts (``review`` with an optional ``topic`` argument).
Every received method is appended to ``--log`` / ``log_path`` (one JSON line per message).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROTOCOL = "2025-06-18"

TOOLS_PAGE_1 = [
    {"name": "echo", "description": "Echo the given text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two integers",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
]
TOOLS_PAGE_2 = [
    {"name": "env_secret", "description": "Return the MCP_SECRET environment variable of the server process",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "Always fails", "inputSchema": {"type": "object", "properties": {}}},
]
RESOURCES = [{"uri": "mem://notes", "name": "notes", "description": "Team notes", "mimeType": "text/plain"}]
RESOURCE_TEXT = "RESOURCE-NOTES-CONTENT: the launch code is 4242"
PROMPTS = [{"name": "review", "description": "Review a topic",
            "arguments": [{"name": "topic", "description": "What to review", "required": False}]}]


def handle(msg: dict[str, Any], name: str = "fake") -> dict[str, Any] | None:
    """Process one JSON-RPC message; returns the response (None for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}
    if mid is None:
        return None  # notification

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},
                   "serverInfo": {"name": f"fake-mcp-{name}", "version": "9.9.9"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        if params.get("cursor") == "page-2":
            return ok({"tools": TOOLS_PAGE_2})
        return ok({"tools": TOOLS_PAGE_1, "nextCursor": "page-2"})
    if method == "tools/call":
        tname = params.get("name")
        args = params.get("arguments") or {}
        if tname == "echo":
            return ok({"content": [{"type": "text", "text": f"ECHO:{args.get('text', '')}"}]})
        if tname == "add":
            return ok({"content": [{"type": "text", "text": f"SUM={int(args.get('a', 0)) + int(args.get('b', 0))}"}]})
        if tname == "env_secret":
            return ok({"content": [{"type": "text", "text": f"SECRET={os.environ.get('MCP_SECRET', '<unset>')}"}]})
        if tname == "boom":
            return ok({"content": [{"type": "text", "text": "BOOM-FAILED"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {tname}"}}
    if method == "resources/list":
        return ok({"resources": RESOURCES})
    if method == "resources/templates/list":
        return ok({"resourceTemplates": []})
    if method == "resources/read":
        uri = params.get("uri")
        if uri == "mem://notes":
            return ok({"contents": [{"uri": uri, "mimeType": "text/plain", "text": RESOURCE_TEXT}]})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": f"resource not found {uri}"}}
    if method == "prompts/list":
        return ok({"prompts": PROMPTS})
    if method == "prompts/get":
        topic = (params.get("arguments") or {}).get("topic", "everything")
        return ok({"description": "Review", "messages": [
            {"role": "user", "content": {"type": "text", "text": f"PROMPT-REVIEW-BODY about {topic}"}}]})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _log(path: str | None, msg: Any) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg) + "\n")


# ─── stdio ──────────────────────────────────────────────────────────────────

def main_stdio(argv: list[str]) -> int:
    name, log = "fake", None
    if "--fail" in argv:
        sys.stderr.write("fake mcp: configured to fail at startup\n")
        return 3
    if "--name" in argv:
        name = argv[argv.index("--name") + 1]
    if "--log" in argv:
        log = argv[argv.index("--log") + 1]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        _log(log, msg)
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            resp = handle(m, name)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
    return 0


# ─── streamable HTTP ────────────────────────────────────────────────────────

class _HttpMCP(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], sse: bool, log_path: str | None, required_header: tuple[str, str] | None):
        self.sse = sse
        self.log_path = log_path
        self.required_header = required_header
        self.sessions: set[str] = set()
        self.received: list[dict[str, Any]] = []
        self.headers_seen: list[dict[str, str]] = []
        super().__init__(addr, _HttpHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/mcp"


class _HttpHandler(BaseHTTPRequestHandler):
    server: _HttpMCP
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - no server->client stream
        self._send(405, b"")

    def do_DELETE(self) -> None:  # noqa: N802
        self.server.sessions.discard(self.headers.get("mcp-session-id", ""))
        self._send(200, b"")

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        hdrs = {k.lower(): v for k, v in self.headers.items()}
        self.server.headers_seen.append(hdrs)
        if self.server.required_header:
            k, v = self.server.required_header
            if hdrs.get(k.lower()) != v:
                self._send(401, b'{"error":"unauthorized"}')
                return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, b'{"error":"bad json"}')
            return
        self.server.received.append(msg)
        _log(self.server.log_path, msg)
        extra: dict[str, str] = {}
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            sid = uuid.uuid4().hex
            self.server.sessions.add(sid)
            extra["Mcp-Session-Id"] = sid
        else:
            sid = hdrs.get("mcp-session-id", "")
            if sid not in self.server.sessions:
                self._send(404, b'{"error":"unknown session"}')
                return
        resp = handle(msg, "http") if isinstance(msg, dict) else None
        if resp is None:
            self._send(202, b"", extra=extra)
            return
        if self.server.sse:
            body = f"event: message\ndata: {json.dumps(resp)}\n\n".encode("utf-8")
            self._send(200, body, "text/event-stream", extra)
        else:
            self._send(200, json.dumps(resp).encode("utf-8"), "application/json", extra)


def start_http_server(sse: bool = False, log_path: str | None = None,
                      required_header: tuple[str, str] | None = None) -> _HttpMCP:
    srv = _HttpMCP(("127.0.0.1", 0), sse, log_path, required_header)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


if __name__ == "__main__":
    sys.exit(main_stdio(sys.argv[1:]))
