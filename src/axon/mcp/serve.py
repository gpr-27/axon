"""
`axon mcp serve`: Axon's own tools (Read, Write, Edit, Glob, Grep, Bash, ...) as an MCP server over stdio,
for other MCP clients (Claude Desktop, an IDE, another agent).

The server only exposes tools. It asks nobody anything: your MCP client is responsible for confirming
individual tool calls. Tools that need a running agent (Task, AskUserQuestion, Skill, ToolSearch, plan mode,
todo tracking) are not offered. stdout carries the protocol and nothing else.
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import IO, Any

from axon.errors import ToolError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
NEEDS_AGENT = frozenset({"Task", "AskUserQuestion", "Skill", "ToolSearch", "ExitPlanMode", "TodoWrite",
                         "SendMessage", "TaskStop", "TaskOutput", "Monitor", "CronCreate", "CronDelete",
                         "CronList", "EnterPlanMode"})


def _client_version() -> str:
    try:
        from importlib.metadata import version
        return version("axon-terminal")
    except Exception:
        return "0.0.0"


class AxonMCPServer:
    """JSON-RPC handler; :meth:`handle` maps one request/notification to its response (or None)."""

    def __init__(self, workspace: Path) -> None:
        from axon.agent.state import FileState, TodoState
        from axon.config import Settings
        from axon.tools import create_default_registry
        from axon.tools.base import ToolContext
        self.workspace = Path(workspace).resolve()
        settings = Settings.load({"workspace": self.workspace}, require_api_key=False)
        self.registry = create_default_registry()
        self.ctx = ToolContext(workspace=self.workspace, file_state=FileState(), todos=TodoState(),
                               settings=settings)

    def tools(self) -> list[Any]:
        return [t for t in self.registry.all_tools()
                if t.name not in NEEDS_AGENT and not getattr(t, "hidden", False)
                and not str(t.name).startswith("mcp__")]

    def tool_list(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tools():
            out.append({"name": t.name, "description": str(t.description),
                        "inputSchema": t.schema if isinstance(t.schema, dict) else {"type": "object"},
                        "annotations": {"readOnlyHint": bool(t.readonly), "title": t.name}})
        return out

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = next((t for t in self.tools() if t.name == name), None)
        if tool is None:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        try:
            with contextlib.redirect_stdout(sys.stderr):  # stray prints must not corrupt the protocol
                text = tool.run(args or {}, self.ctx)
            return {"content": [{"type": "text", "text": str(text)}], "isError": False}
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # noqa: BLE001 - report, never crash the server
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, rid = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if rid is None:  # notification
            return None

        def ok(result: dict[str, Any]) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def err(code: int, text: str) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}

        if method == "initialize":
            wanted = str(params.get("protocolVersion") or "")
            version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return ok({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "axon", "title": "Axon", "version": _client_version()}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tool_list()})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return err(-32602, "tools/call needs a tool name")
            return ok(self.call(name, params.get("arguments") if isinstance(params.get("arguments"), dict) else {}))
        return err(-32601, f"Method not found: {method}")


def serve_stdio(workspace: Path, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Run until stdin closes. Newline-delimited JSON-RPC, one message per line."""
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    server = AxonMCPServer(workspace)
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            out.flush()
            continue
        for item in (msg if isinstance(msg, list) else [msg]):
            if not isinstance(item, dict):
                continue
            reply = server.handle(item)
            if reply is not None:
                out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                out.flush()
    return 0
