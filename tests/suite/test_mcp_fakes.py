"""
Shared fixtures for the MCP tests: a tiny fake MCP stdio server (written to tmp_path)
and helpers. Imported by the other tests/suite/test_mcp_*.py modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

FAKE_SERVER_SOURCE = r'''"""A tiny line-delimited JSON-RPC MCP server used by the MCP tests (stdio)."""
from __future__ import annotations

import json
import os
import sys
import time

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
FORCE_VERSION = os.environ.get("FAKE_MCP_VERSION")

TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "fail", "description": "Always fails", "inputSchema": {"type": "object"}},
    {"name": "image", "description": "Returns an image", "inputSchema": {"type": "object"}},
    {"name": "structured", "description": "Structured output", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleeps", "inputSchema": {"type": "object"}},
    {"name": "roots", "description": "Asks the client for roots", "inputSchema": {"type": "object"}},
    {"name": "grow", "description": "Adds a tool and notifies", "inputSchema": {"type": "object"}},
]
EXTRA = {"name": "late tool!", "description": "Added later", "inputSchema": {"type": "object"}}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def result(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


def main():
    print("fake server starting", file=sys.stderr, flush=True)
    sys.stdout.write("this is not json noise\n")
    sys.stdout.flush()
    tools = list(TOOLS)
    while True:
        msg = read()
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if rid is None:
            if method == "notifications/cancelled":
                print(f"cancelled {params.get('requestId')}", file=sys.stderr, flush=True)
            continue
        if method == "initialize":
            req = params.get("protocolVersion")
            version = FORCE_VERSION or (req if req in SUPPORTED else SUPPORTED[0])
            result(rid, {"protocolVersion": version,
                         "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {},
                                          "logging": {}},
                         "serverInfo": {"name": "fake", "version": "1.0"},
                         "instructions": "be nice"})
        elif method == "ping":
            result(rid, {})
        elif method == "tools/list":
            # Two pages: first 3 tools, then the rest.
            if params.get("cursor") == "page2":
                result(rid, {"tools": tools[3:]})
            else:
                result(rid, {"tools": tools[:3], "nextCursor": "page2"})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "info", "data": "echo called"}})
                result(rid, {"content": [{"type": "text", "text": "echo: " + args.get("text", "")}]})
            elif name == "fail":
                result(rid, {"content": [{"type": "text", "text": "boom"}], "isError": True})
            elif name == "image":
                result(rid, {"content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]})
            elif name == "structured":
                result(rid, {"content": [], "structuredContent": {"temp": 21}})
            elif name == "slow":
                time.sleep(float(args.get("seconds", 1.5)))
                result(rid, {"content": [{"type": "text", "text": "slow done"}]})
            elif name == "roots":
                send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
                send({"jsonrpc": "2.0", "id": "srv-2", "method": "sampling/createMessage", "params": {}})
                answers = {}
                while len(answers) < 2:
                    m = read()
                    if m.get("id") in ("srv-1", "srv-2"):
                        answers[m["id"]] = m
                roots = answers["srv-1"]["result"]["roots"]
                rejected = "error" in answers["srv-2"]
                result(rid, {"content": [{"type": "text",
                                          "text": json.dumps({"roots": roots, "sampling_rejected": rejected})}]})
            elif name == "grow":
                tools.append(EXTRA)
                result(rid, {"content": [{"type": "text", "text": "grown"}]})
                send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool {name}"}})
        elif method == "resources/list":
            result(rid, {"resources": [{"uri": "file:///readme.md", "name": "readme", "mimeType": "text/markdown"}]})
        elif method == "resources/templates/list":
            result(rid, {"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "files"}]})
        elif method == "resources/read":
            result(rid, {"contents": [{"uri": params["uri"], "mimeType": "text/markdown", "text": "# Hello"}]})
        elif method == "prompts/list":
            result(rid, {"prompts": [{"name": "review", "description": "Review code",
                                      "arguments": [{"name": "lang", "required": True}]}]})
        elif method == "prompts/get":
            lang = (params.get("arguments") or {}).get("lang", "?")
            result(rid, {"messages": [{"role": "user", "content": {"type": "text", "text": f"Review this {lang} code"}}]})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()
'''


def write_fake_server(directory: Path) -> Path:
    path = directory / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER_SOURCE, encoding="utf-8")
    return path


@pytest.fixture
def fake_server(tmp_path: Path) -> Path:
    return write_fake_server(tmp_path)


def stdio_spec(name: str, script: Path, **extra):
    from axon.mcp.config import ServerSpec
    return ServerSpec(name=name, type="stdio", command=sys.executable, args=[str(script)], **extra)


def test_fake_server_source_is_valid_python():
    compile(FAKE_SERVER_SOURCE, "fake_mcp_server.py", "exec")
