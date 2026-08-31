"""
MCP Tool Bridge: Live connection to Model Context Protocol servers.
Spawns stdio-based MCP servers, discovers their tools via JSON-RPC,
and registers them as dynamic Axon Tool instances in the ToolRegistry.
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, TYPE_CHECKING

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from axon.mcp.manager import MCPManager

logger = logging.getLogger(__name__)

# ─── JSON-RPC Helpers ──────────────────────────────────────────────────────

def _jsonrpc_request(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request payload."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _send_jsonrpc(proc: subprocess.Popen, message: dict[str, Any]) -> dict[str, Any] | None:
    """Send a JSON-RPC message to a subprocess via stdin and read the response from stdout."""
    if proc.stdin is None or proc.stdout is None:
        return None

    payload = json.dumps(message)
    try:
        proc.stdin.write(payload + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        return None

    # Read response line (with timeout via thread)
    result_container: list[str] = []

    def _read_line() -> None:
        try:
            line = proc.stdout.readline()
            if line:
                result_container.append(line.strip())
        except Exception:
            pass

    reader = threading.Thread(target=_read_line, daemon=True)
    reader.start()
    reader.join(timeout=15.0)

    if not result_container:
        return None

    try:
        return json.loads(result_container[0])
    except (json.JSONDecodeError, IndexError):
        return None


# ─── MCP Server Connection ─────────────────────────────────────────────────

@dataclass
class MCPServerConnection:
    """Represents a live connection to a running MCP server subprocess."""
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    process: subprocess.Popen | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    connected: bool = False
    error: str | None = None

    def start(self) -> bool:
        """Spawn the MCP server subprocess and perform initialization handshake."""
        full_env = dict(os.environ)
        full_env.update(self.env)

        try:
            cmd = [self.command] + self.args
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=full_env,
                cwd=str(Path.cwd()),
            )
        except FileNotFoundError:
            self.error = f"Command not found: {self.command}"
            return False
        except Exception as e:
            self.error = f"Failed to start: {e}"
            return False

        # Allow server startup time
        time.sleep(1.5)

        # Check if process died immediately
        if self.process.poll() is not None:
            stderr_out = ""
            try:
                stderr_out = self.process.stderr.read()[:500] if self.process.stderr else ""
            except Exception:
                pass
            self.error = f"Server exited immediately (code {self.process.returncode}): {stderr_out}"
            self.process = None
            return False

        # 1. Send initialize request
        init_resp = _send_jsonrpc(self.process, _jsonrpc_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "axon", "version": "0.27.7"},
        }, req_id=1))

        if not init_resp or "result" not in init_resp:
            self.error = "MCP server did not respond to initialize"
            self.stop()
            return False

        # 2. Send initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            if self.process.stdin:
                self.process.stdin.write(json.dumps(notif) + "\n")
                self.process.stdin.flush()
        except Exception:
            pass

        time.sleep(0.3)

        # 3. Discover tools via tools/list
        tools_resp = _send_jsonrpc(self.process, _jsonrpc_request("tools/list", req_id=2))
        if tools_resp and "result" in tools_resp:
            self.tools = tools_resp["result"].get("tools", [])

        self.connected = True
        return True

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool on the running MCP server and return the result text."""
        if not self.process or not self.connected:
            raise ToolError(f"MCP server '{self.name}' is not connected.")

        resp = _send_jsonrpc(self.process, _jsonrpc_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        }, req_id=int(time.time() * 1000) % 1_000_000))

        if resp is None:
            raise ToolError(f"MCP server '{self.name}' did not respond to tool call '{tool_name}'.")

        if "error" in resp:
            err = resp["error"]
            raise ToolError(f"MCP tool error: {err.get('message', str(err))}")

        result = resp.get("result", {})
        # MCP tool results have a "content" array of content blocks
        content_blocks = result.get("content", [])
        if isinstance(content_blocks, list):
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "resource":
                        text_parts.append(f"[Resource: {block.get('uri', 'unknown')}]")
                    else:
                        text_parts.append(json.dumps(block, indent=2))
                else:
                    text_parts.append(str(block))
            return "\n".join(text_parts) if text_parts else json.dumps(result, indent=2)

        return json.dumps(result, indent=2)

    def stop(self) -> None:
        """Gracefully terminate the MCP server subprocess."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            self.connected = False


# ─── Dynamic MCP Tool Wrapper ──────────────────────────────────────────────

class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as an Axon Tool instance."""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    schema: ClassVar[dict[str, Any]] = {}
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "ask"

    def __init__(self, server_name: str, tool_spec: dict[str, Any], connection: MCPServerConnection) -> None:
        mcp_name = tool_spec.get("name", "unknown")
        mcp_desc = tool_spec.get("description", "MCP tool")
        input_schema = tool_spec.get("inputSchema", {"type": "object", "properties": {}})

        # Create unique instance attributes (override ClassVar defaults)
        self.name = f"mcp_{server_name}_{mcp_name}"  # type: ignore[assignment]
        self.description = f"[MCP:{server_name}] {mcp_desc}"  # type: ignore[assignment]
        self.schema = input_schema  # type: ignore[assignment]
        self.readonly = True  # type: ignore[assignment]
        self.default_permission = "ask"  # type: ignore[assignment]

        self._server_name = server_name
        self._mcp_tool_name = mcp_name
        self._connection = connection

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return self._connection.call_tool(self._mcp_tool_name, args)

    def render_call(self, args: dict[str, Any]) -> str:
        short_args = " ".join(f'{k}:"{v}"' if isinstance(v, str) else f"{k}:{v}" for k, v in list(args.items())[:3])
        return f"MCP:{self._server_name}/{self._mcp_tool_name} {short_args}".strip()


# ─── MCP Tool Bridge ──────────────────────────────────────────────────────

class MCPToolBridge:
    """
    Connects to configured MCP servers, discovers their tools,
    and registers them as dynamic Axon Tool instances.
    """

    def __init__(self) -> None:
        self._connections: dict[str, MCPServerConnection] = {}
        self._tools: list[MCPToolWrapper] = []

    @property
    def connections(self) -> dict[str, MCPServerConnection]:
        return self._connections

    @property
    def active_tools(self) -> list[MCPToolWrapper]:
        return self._tools

    def connect_all(self, servers: dict[str, dict[str, Any]]) -> list[MCPToolWrapper]:
        """
        Connect to all configured MCP servers and discover their tools.

        Args:
            servers: Dict of server_name -> server_spec from MCPManager.get_all_servers()

        Returns:
            List of MCPToolWrapper instances ready for registration.
        """
        self._tools.clear()

        for name, spec in servers.items():
            command = spec.get("command", "")
            args = spec.get("args", [])
            env = spec.get("env", {})

            if not command:
                continue

            # Skip servers with placeholder tokens
            has_placeholder = any(
                "your_" in str(v).lower() or v in ("xoxb-...", "T...")
                for v in env.values()
            )
            if has_placeholder:
                continue

            conn = MCPServerConnection(
                name=name,
                command=command,
                args=args,
                env=env,
            )

            if conn.start():
                self._connections[name] = conn
                # Wrap each discovered tool
                for tool_spec in conn.tools:
                    wrapper = MCPToolWrapper(name, tool_spec, conn)
                    self._tools.append(wrapper)
            else:
                logger.debug(f"MCP server '{name}' failed to start: {conn.error}")

        return self._tools

    def connect_single(self, name: str, spec: dict[str, Any]) -> list[MCPToolWrapper]:
        """Connect to a single MCP server and return its tools."""
        command = spec.get("command", "")
        args = spec.get("args", [])
        env = spec.get("env", {})

        if not command:
            return []

        conn = MCPServerConnection(name=name, command=command, args=args, env=env)
        new_tools: list[MCPToolWrapper] = []

        if conn.start():
            self._connections[name] = conn
            for tool_spec in conn.tools:
                wrapper = MCPToolWrapper(name, tool_spec, conn)
                new_tools.append(wrapper)
                self._tools.append(wrapper)

        return new_tools

    def disconnect_all(self) -> None:
        """Gracefully shutdown all MCP server connections."""
        for conn in self._connections.values():
            conn.stop()
        self._connections.clear()
        self._tools.clear()

    def disconnect_server(self, name: str) -> bool:
        """Disconnect a single MCP server."""
        if name in self._connections:
            self._connections[name].stop()
            del self._connections[name]
            self._tools = [t for t in self._tools if t._server_name != name]
            return True
        return False

    def get_status_report(self) -> str:
        """Return a formatted status report of all MCP connections and their tools."""
        if not self._connections:
            return "No active MCP server connections."

        lines = [f"=== MCP Tool Bridge — {len(self._connections)} Active Server(s) ===", ""]
        for name, conn in self._connections.items():
            status = "🟢 Connected" if conn.connected else f"🔴 Error: {conn.error}"
            lines.append(f"  {name}: {status}")
            for tool_spec in conn.tools:
                t_name = tool_spec.get("name", "?")
                t_desc = tool_spec.get("description", "")[:60]
                lines.append(f"    • {t_name} — {t_desc}")
            lines.append("")

        lines.append(f"Total MCP tools available: {len(self._tools)}")
        return "\n".join(lines)


# ─── Module-Level Singleton ────────────────────────────────────────────────

_bridge: MCPToolBridge | None = None

def get_bridge() -> MCPToolBridge:
    """Return the global MCPToolBridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = MCPToolBridge()
    return _bridge

def initialize_mcp_bridge(workspace: Path) -> list[MCPToolWrapper]:
    """
    Initialize the MCP bridge by connecting to all configured servers.
    Returns the list of discovered tool wrappers ready for registration.
    """
    from axon.mcp.manager import MCPManager
    manager = MCPManager(workspace)
    servers = manager.get_all_servers()

    if not servers:
        return []

    bridge = get_bridge()
    return bridge.connect_all(servers)

def shutdown_mcp_bridge() -> None:
    """Gracefully shutdown all MCP connections on exit."""
    global _bridge
    if _bridge:
        _bridge.disconnect_all()
        _bridge = None
