"""
MCP tool bridge: the single runtime path for MCP in Axon.

Owns one live :class:`MCPServerConnection` per configured server, connects them in
parallel with a startup deadline (slow/broken servers never block or crash startup),
wraps their tools as Axon ``Tool`` objects named ``mcp__<server>__<tool>`` and keeps
an attached ``ToolRegistry`` in sync (register on connect / list_changed, unregister
on disconnect). Also exposes resources (as generic tools) and prompts (for slash
commands) through module-level helpers.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar

from axon.errors import ToolError
from axon.mcp.client import (
    MCPClient, MCPError, render_prompt_messages, render_resource_contents, render_tool_result,
)
from axon.mcp.config import MCPConfig, ServerSpec
from axon.mcp.transports import (
    LegacySSETransport, StdioTransport, StreamableHTTPTransport, Transport, TransportError,
    log_path_for,
)
from axon.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_TIMEOUT = 10.0
CONNECT_TIMEOUT_CAP = 30.0
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(part: str) -> str:
    return _NAME_RE.sub("_", part).strip("_") or "x"


def mcp_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` restricted to [a-zA-Z0-9_-]{1,64}."""
    name = f"mcp__{_sanitize(server)}__{_sanitize(tool)}"
    if len(name) > 64:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = name[:55] + "_" + digest
    return name


def _startup_timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("AXON_MCP_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT)))
    except ValueError:
        return DEFAULT_STARTUP_TIMEOUT


# ─── Connection ────────────────────────────────────────────────────────────

class MCPServerConnection:
    """A configured server plus its live client session and discovered tools."""

    def __init__(self, name: str, command: str = "", args: list[str] | None = None,
                 env: dict[str, str] | None = None, *, spec: ServerSpec | None = None,
                 workspace: Path | None = None, http_client: Any = None,
                 on_tools_changed: Callable[["MCPServerConnection"], None] | None = None) -> None:
        self.name = name
        self.spec = spec or ServerSpec(name=name, type="stdio", command=command,
                                       args=list(args or []), env=dict(env or {}))
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.http_client = http_client
        self.on_tools_changed = on_tools_changed
        self.client: MCPClient | None = None
        self.tools: list[dict[str, Any]] = []
        self.status = "disabled" if self.spec.disabled else "pending"
        self.error: str | None = None
        self.transport_kind = self.spec.type
        self.connected_at: float | None = None
        self._gen = 0
        self._lock = threading.RLock()

    # Backward-compatible attributes
    @property
    def command(self) -> str:
        return self.spec.command

    @property
    def args(self) -> list[str]:
        return self.spec.args

    @property
    def env(self) -> dict[str, str]:
        return self.spec.env

    @property
    def connected(self) -> bool:
        return self.status == "connected" and self.client is not None and not self.client.closed

    @property
    def log_path(self) -> Path:
        return log_path_for(self.name)

    def _make_transport(self, spec: ServerSpec, kind: str) -> Transport:
        if kind == "stdio":
            cwd = spec.cwd or str(self.workspace)
            if spec.cwd and not os.path.isabs(spec.cwd):
                cwd = str(self.workspace / spec.cwd)
            return StdioTransport(self.name, spec.command, spec.args, spec.env, cwd)
        if kind == "sse":
            return LegacySSETransport(spec.url, spec.headers, spec.timeout, client=self.http_client,
                                      name=self.name)
        return StreamableHTTPTransport(spec.url, spec.headers, spec.timeout, client=self.http_client,
                                       name=self.name)

    def _open(self, spec: ServerSpec, kind: str, timeout: float, gen: int) -> MCPClient:
        client = MCPClient(self.name, self._make_transport(spec, kind), timeout=spec.timeout,
                           workspace=self.workspace, on_tools_changed=self._tools_changed,
                           on_closed=self._closed)
        with self._lock:
            if gen != self._gen:
                raise MCPError("connection attempt superseded")
            self.client = client  # visible to stop() so it can abort the handshake
        try:
            client.connect(timeout=timeout)
        except Exception:
            client.close()
            raise
        return client

    def start(self, timeout: float | None = None) -> bool:
        """Connect, handshake and discover tools. Never raises; sets status/error.
        The lock is only held for state changes, so stop() can abort a slow handshake."""
        with self._lock:
            self._stop_locked()
            self._gen += 1
            gen = self._gen
            problem = self.spec.validate()
            if problem:
                self.status, self.error = "failed", problem
                return False
            unresolved = self.spec.unresolved_problems()
            if unresolved:
                self.status, self.error = "needs-config", "; ".join(unresolved)
                return False
            self.status, self.error = "connecting", None
        spec = self.spec.resolved()
        init_timeout = min(spec.timeout, CONNECT_TIMEOUT_CAP) if timeout is None else timeout
        try:
            try:
                client = self._open(spec, spec.type, init_timeout, gen)
                kind = spec.type
            except MCPError as e:
                cause = e.__cause__
                status = getattr(cause, "status", None)
                # Backwards compatibility: an http URL that rejects POST may be a legacy SSE server.
                if (spec.type == "http" and isinstance(cause, TransportError)
                        and status in (400, 404, 405) and gen == self._gen):
                    client = self._open(spec, "sse", init_timeout, gen)
                    kind = "sse"
                else:
                    raise
            tools = client.list_tools()
        except Exception as e:  # a broken server must never crash Axon
            with self._lock:
                if gen == self._gen:
                    msg = str(e) if isinstance(e, (MCPError, TransportError, OSError)) \
                        else f"{type(e).__name__}: {e}"
                    self.status, self.error = "failed", msg
                    self._stop_locked()
            return False
        with self._lock:
            if gen != self._gen:
                client.close()
                return False
            self.client, self.tools, self.transport_kind = client, tools, kind
            self.status = "connected"
            self.connected_at = time.time()
        return True

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._stop_locked()
            if self.status in ("connected", "connecting", "disconnected"):
                self.status = "stopped"

    def _stop_locked(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _closed(self, client: MCPClient, reason: str) -> None:
        if client is self.client and self.status == "connected":
            self.status, self.error = "disconnected", reason

    def _tools_changed(self, client: MCPClient) -> None:
        if client is not self.client:
            return
        self.tools = client.list_tools(refresh=True)
        if self.on_tools_changed:
            self.on_tools_changed(self)

    def _require_client(self) -> MCPClient:
        if not self.connected and self.status in ("connected", "disconnected"):
            # The server process died or the session dropped: reconnect once.
            self.start()
        if not self.connected or self.client is None:
            detail = f": {self.error}" if self.error else ""
            raise ToolError(f"MCP server '{self.name}' is not connected ({self.status}{detail}).")
        return self.client

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        client = self._require_client()
        try:
            result = client.call_tool(tool_name, arguments, timeout=self.spec.timeout)
        except MCPError as e:
            raise ToolError(f"MCP tool '{tool_name}' on '{self.name}' failed: {e}") from e
        text, is_error = render_tool_result(result)
        if is_error:
            raise ToolError(f"MCP tool '{tool_name}' returned an error:\n{text}")
        return text

    def resources(self) -> list[dict[str, Any]]:
        return self._require_client().list_resources(refresh=True)

    def resource_templates(self) -> list[dict[str, Any]]:
        return self._require_client().list_resource_templates()

    def read_resource(self, uri: str) -> str:
        try:
            result = self._require_client().read_resource(uri)
        except MCPError as e:
            raise ToolError(f"Reading '{uri}' from '{self.name}' failed: {e}") from e
        contents = result.get("contents") or []
        return "\n\n".join(render_resource_contents(c) for c in contents if isinstance(c, dict)) \
            or "(empty resource)"

    def prompts(self) -> list[dict[str, Any]]:
        return self._require_client().list_prompts()

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> str:
        try:
            return render_prompt_messages(self._require_client().get_prompt(name, arguments))
        except MCPError as e:
            raise ToolError(f"Prompt '{name}' from '{self.name}' failed: {e}") from e


# ─── Tool wrappers ─────────────────────────────────────────────────────────

class MCPToolWrapper(Tool):
    """One MCP server tool exposed as an Axon tool."""
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    schema: ClassVar[dict[str, Any]] = {}
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def __init__(self, server_name: str, tool_spec: dict[str, Any], connection: MCPServerConnection,
                 name: str | None = None) -> None:
        mcp_name = str(tool_spec.get("name", "unknown"))
        annotations = tool_spec.get("annotations") or {}
        title = tool_spec.get("title") or annotations.get("title")
        desc = str(tool_spec.get("description") or title or "MCP tool")
        schema = tool_spec.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        elif schema.get("type") != "object":
            schema = {**schema, "type": "object"}
        self.name = name or mcp_tool_name(server_name, mcp_name)  # type: ignore[misc]
        self.description = f"[MCP:{server_name}] {desc}"[:2048]  # type: ignore[misc]
        self.schema = schema  # type: ignore[misc]
        self.readonly = bool(annotations.get("readOnlyHint", False))  # type: ignore[misc]
        self.default_permission = "ask"  # type: ignore[misc]
        self._server_name = server_name
        self._mcp_tool_name = mcp_name
        self._connection = connection

    @property
    def server_name(self) -> str:
        return self._server_name

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return self._connection.call_tool(self._mcp_tool_name, args)

    def render_call(self, args: dict[str, Any]) -> str:
        short = " ".join(f'{k}:"{v}"' if isinstance(v, str) else f"{k}:{v}"
                         for k, v in list(args.items())[:3])
        return f"MCP:{self._server_name}/{self._mcp_tool_name} {short}".strip()


class _BridgeTool(Tool):
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def __init__(self, bridge: "MCPToolBridge") -> None:
        self._bridge = bridge


class MCPListResourcesTool(_BridgeTool):
    name: ClassVar[str] = "mcp__list_resources"
    description: ClassVar[str] = (
        "List resources (and resource templates) exposed by connected MCP servers. "
        "Use mcp__read_resource to read one.")
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"server": {"type": "string", "description": "Only this MCP server (optional)"}},
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return self._bridge.describe_resources(args.get("server") or None)


class MCPReadResourceTool(_BridgeTool):
    name: ClassVar[str] = "mcp__read_resource"
    description: ClassVar[str] = "Read a resource by URI from a connected MCP server."
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "MCP server name"},
            "uri": {"type": "string", "description": "Resource URI (from mcp__list_resources)"},
        },
        "required": ["server", "uri"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        server, uri = str(args.get("server") or ""), str(args.get("uri") or "")
        if not uri:
            raise ToolError("'uri' is required")
        return self._bridge.get_connection(server).read_resource(uri)


def _registry_unregister(registry: Any, name: str, expected: Any) -> None:
    """Remove ``name`` only if it still maps to our tool object."""
    try:
        current = registry.tools.get(name) if hasattr(registry, "tools") else None
    except Exception:
        current = None
    if current is not expected:
        return
    fn = getattr(registry, "unregister", None)
    if callable(fn):
        try:
            fn(name)
            return
        except Exception:
            pass
    for attr in ("tools", "_tools"):
        d = getattr(registry, attr, None)
        if isinstance(d, dict):
            d.pop(name, None)
            return


# ─── Bridge (hub) ──────────────────────────────────────────────────────────

class MCPToolBridge:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self._connections: dict[str, MCPServerConnection] = {}
        self._server_tools: dict[str, list[MCPToolWrapper]] = {}
        self._generic: list[Tool] = [MCPListResourcesTool(self), MCPReadResourceTool(self)]
        self._generic_registered = False
        self._registry: Any = None
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self.http_client_factory: Callable[[ServerSpec], Any] | None = None

    # ── views ──────────────────────────────────────────────────────────
    @property
    def connections(self) -> dict[str, MCPServerConnection]:
        return self._connections

    @property
    def active_tools(self) -> list[Tool]:
        with self._lock:
            tools: list[Tool] = [t for ts in self._server_tools.values() for t in ts]
            if self._wants_generic():
                tools.extend(self._generic)
            return tools

    def get_connection(self, name: str) -> MCPServerConnection:
        conn = self._connections.get(name)
        if conn is None:
            known = ", ".join(sorted(self._connections)) or "none"
            raise ToolError(f"Unknown MCP server '{name}'. Connected servers: {known}")
        return conn

    def _wants_generic(self) -> bool:
        return any(c.connected and c.client is not None and c.client.has("resources")
                   for c in self._connections.values())

    # ── registry sync ───────────────────────────────────────────────────
    def attach_registry(self, registry: Any) -> None:
        with self._lock:
            self._registry = registry
            for tools in self._server_tools.values():
                for t in tools:
                    registry.register(t)
            self._sync_generic()

    def _sync_generic(self) -> None:
        reg = self._registry
        if reg is None:
            return
        want = self._wants_generic()
        for t in self._generic:
            if want:
                reg.register(t)
            else:
                _registry_unregister(reg, t.name, t)

    def _publish(self, conn: MCPServerConnection) -> list[MCPToolWrapper]:
        """(Re)build wrappers for a server and swap them into the registry."""
        with self._lock:
            self._unpublish(conn.name)
            if not conn.connected:
                self._sync_generic()
                return []
            taken = {t.name for n, ts in self._server_tools.items() if n != conn.name for t in ts}
            wrappers: list[MCPToolWrapper] = []
            for spec in conn.tools:
                base = mcp_tool_name(conn.name, str(spec.get("name", "")))
                name, i = base, 2
                while name in taken:
                    suffix = f"_{i}"
                    name, i = base[:64 - len(suffix)] + suffix, i + 1
                taken.add(name)
                wrappers.append(MCPToolWrapper(conn.name, spec, conn, name=name))
            self._server_tools[conn.name] = wrappers
            if self._registry is not None:
                for w in wrappers:
                    self._registry.register(w)
            self._sync_generic()
            return wrappers

    def _unpublish(self, name: str) -> None:
        with self._lock:
            old = self._server_tools.pop(name, [])
            if self._registry is not None:
                for t in old:
                    _registry_unregister(self._registry, t.name, t)

    # ── connecting ──────────────────────────────────────────────────────
    def _make_connection(self, spec: ServerSpec) -> MCPServerConnection:
        http_client = self.http_client_factory(spec) if self.http_client_factory and spec.type != "stdio" else None
        return MCPServerConnection(spec.name, spec=spec, workspace=self.workspace,
                                   http_client=http_client, on_tools_changed=self._publish)

    def connect_server(self, spec: ServerSpec, timeout: float | None = None) -> MCPServerConnection:
        """Synchronously (re)connect one server, replacing any previous connection."""
        self.disconnect_server(spec.name)
        conn = self._make_connection(spec)
        with self._lock:
            self._connections[spec.name] = conn
        if not spec.disabled:
            conn.start(timeout)
        if self._connections.get(spec.name) is conn:
            self._publish(conn)
        return conn

    def connect_all(self, servers: dict[str, Any], timeout: float | None = None) -> list[Tool]:
        """Connect every enabled server in parallel. Waits at most ``timeout`` seconds in
        total; servers still connecting afterwards finish in the background and register
        their tools into the attached registry when ready. Already-connected servers with
        an unchanged spec are kept (no duplicate processes)."""
        deadline = time.monotonic() + (_startup_timeout() if timeout is None else timeout)
        threads: list[threading.Thread] = []
        for name, raw in servers.items():
            spec = raw if isinstance(raw, ServerSpec) else ServerSpec.from_dict(name, raw)
            existing = self._connections.get(name)
            if existing is not None and existing.spec.to_dict() == spec.to_dict() and \
                    existing.status in ("connected", "connecting"):
                continue
            self.disconnect_server(name)
            conn = self._make_connection(spec)
            with self._lock:
                self._connections[name] = conn
            if spec.disabled:
                continue
            conn.status = "connecting"

            def _run(c: MCPServerConnection = conn) -> None:
                c.start()
                if self._connections.get(c.name) is c:
                    self._publish(c)
                else:
                    c.stop()

            t = threading.Thread(target=_run, name=f"mcp-connect-{name}", daemon=True)
            self._threads[name] = t
            t.start()
            threads.append(t)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return self.active_tools

    def connect_single(self, name: str, spec: dict[str, Any]) -> list[MCPToolWrapper]:
        conn = self.connect_server(ServerSpec.from_dict(name, spec))
        return list(self._server_tools.get(conn.name, []))

    def disconnect_server(self, name: str) -> bool:
        with self._lock:
            conn = self._connections.pop(name, None)
            self._unpublish(name)
            self._sync_generic()
        if conn is None:
            return False
        conn.stop()
        return True

    def disconnect_all(self, timeout: float = 5.0) -> None:
        """Stop every server in parallel (each stdio stop may wait ~1.5s for a clean exit)."""
        threads = [threading.Thread(target=self.disconnect_server, args=(n,), daemon=True)
                   for n in list(self._connections)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def wait_until_settled(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for t in list(self._threads.values()):
            t.join(max(0.0, deadline - time.monotonic()))

    # ── reporting ───────────────────────────────────────────────────────
    def status_rows(self) -> list[dict[str, Any]]:
        rows = []
        for name, c in sorted(self._connections.items()):
            rows.append({
                "name": name, "status": c.status, "transport": c.transport_kind,
                "target": c.spec.describe_target(), "tools": len(self._server_tools.get(name, [])),
                "error": c.error, "scope": c.spec.scope,
                "server": (c.client.server_info if c.client else {}),
                "protocol": (c.client.protocol_version if c.client else None),
            })
        return rows

    def startup_summary(self) -> str:
        rows = self.status_rows()
        if not rows:
            return ""
        ok = [r for r in rows if r["status"] == "connected"]
        pending = [r for r in rows if r["status"] == "connecting"]
        bad = [r for r in rows if r["status"] in ("failed", "needs-config", "disconnected")]
        parts = [f"MCP: {len(ok)} connected ({sum(r['tools'] for r in ok)} tools)"]
        if pending:
            parts.append(f"{len(pending)} still connecting")
        if bad:
            parts.append("failed: " + ", ".join(f"{r['name']} ({(r['error'] or r['status'])[:80]})"
                                                for r in bad))
        return " · ".join(parts)

    def get_status_report(self) -> str:
        if not self._connections:
            return "No active MCP server connections."
        lines = [f"MCP servers ({len(self._connections)})", ""]
        for r in self.status_rows():
            lines.append(f"  {r['name']}: {r['status']} [{r['transport']}] {r['tools']} tool(s)"
                         + (f" - {r['error']}" if r["error"] else ""))
            for t in self._server_tools.get(r["name"], []):
                lines.append(f"    - {t.name}: {t.description[:70]}")
        lines.append("")
        lines.append(f"Total MCP tools available: {len(self.active_tools)}")
        return "\n".join(lines)

    # ── resources & prompts ─────────────────────────────────────────────
    def _connected(self, server: str | None) -> list[MCPServerConnection]:
        if server:
            return [self.get_connection(server)]
        return [c for c in self._connections.values() if c.connected]

    def describe_resources(self, server: str | None = None) -> str:
        lines: list[str] = []
        for c in self._connected(server):
            if not (c.client and c.client.has("resources")):
                continue
            try:
                res, tmpl = c.resources(), c.resource_templates()
            except (MCPError, ToolError) as e:
                lines.append(f"[{c.name}] error: {e}")
                continue
            for r in res:
                desc = f" - {r['description']}" if r.get("description") else ""
                lines.append(f"[{c.name}] {r.get('uri')} ({r.get('name', '')}"
                             f"{', ' + r['mimeType'] if r.get('mimeType') else ''}){desc}")
            for t in tmpl:
                lines.append(f"[{c.name}] template {t.get('uriTemplate')} ({t.get('name', '')})")
        return "\n".join(lines) or "No MCP resources available."

    def list_prompts(self) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
        out: list[tuple[str, str, str, list[dict[str, Any]]]] = []
        for c in list(self._connections.values()):
            if not (c.connected and c.client and c.client.has("prompts")):
                continue
            try:
                for p in c.prompts():
                    out.append((c.name, str(p.get("name", "")), str(p.get("description") or ""),
                                list(p.get("arguments") or [])))
            except (MCPError, ToolError):
                continue
        return out

    def get_prompt(self, server: str, name: str, args: dict[str, str] | None = None) -> str:
        return self.get_connection(server).get_prompt(name, args)


# ─── Module-level API ──────────────────────────────────────────────────────

_bridge: MCPToolBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge(workspace: Path | None = None) -> MCPToolBridge:
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = MCPToolBridge(workspace)
        elif workspace is not None and not _bridge.connections:
            _bridge.workspace = Path(workspace)
        return _bridge


def initialize_mcp_bridge(workspace: Path, registry: Any = None,
                          timeout: float | None = None) -> list[Tool]:
    """Connect all configured, enabled servers (parallel, bounded by ``timeout`` /
    $AXON_MCP_STARTUP_TIMEOUT, default 10s). Returns tools ready now; when ``registry``
    is given it is kept in sync, including servers that finish connecting later."""
    specs = MCPConfig(workspace).load_specs()
    bridge = get_bridge(workspace)
    if registry is not None:
        bridge.attach_registry(registry)
    if not specs:
        return []
    return bridge.connect_all(specs, timeout)


def shutdown_mcp_bridge() -> None:
    global _bridge
    with _bridge_lock:
        bridge, _bridge = _bridge, None
    if bridge:
        bridge.disconnect_all()


def list_prompts() -> list[tuple[str, str, str, list[dict[str, Any]]]]:
    """(server, prompt_name, description, arguments) for every connected server's prompts.
    ``arguments`` items are MCP PromptArgument dicts: {name, description?, required?}."""
    return get_bridge().list_prompts() if _bridge is not None else []


def get_prompt(server: str, name: str, args: dict[str, str] | None = None) -> str:
    """Render an MCP prompt (prompts/get) to plain text suitable as a user message."""
    return get_bridge().get_prompt(server, name, args)


def prompt_command_name(server: str, prompt: str) -> str:
    """Slash-command name for an MCP prompt: ``mcp__<server>__<prompt>``."""
    return mcp_tool_name(server, prompt)


__all__ = [
    "MCPServerConnection", "MCPToolWrapper", "MCPToolBridge", "MCPListResourcesTool",
    "MCPReadResourceTool", "get_bridge", "initialize_mcp_bridge", "shutdown_mcp_bridge",
    "list_prompts", "get_prompt", "mcp_tool_name", "prompt_command_name",
]
