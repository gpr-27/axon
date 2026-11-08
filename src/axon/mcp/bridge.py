"""
MCP tool bridge: the single runtime path for MCP in Axon.

Owns one live :class:`MCPServerConnection` per configured server, connects them in
parallel with a startup deadline (slow/broken servers never block or crash startup),
wraps their tools as Axon ``Tool`` objects named ``mcp__<server>__<tool>`` and keeps
an attached ``ToolRegistry`` in sync (register on connect / list_changed, unregister
on disconnect). Also exposes resources (as generic tools) and prompts (for slash
commands) through module-level helpers.

Also here: reconnection (a dropped remote server is retried with backoff; a first connection retries
transient errors), OAuth / headersHelper wiring, tool-call time limits, output limits and the shaping of
tool definitions (description caps, root-level schema combinators, invalid tools left out).
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
from urllib.parse import urlsplit

from axon.errors import ToolError
from axon import notices
from axon.waitmarks import waiting
from axon.mcp import health as mcp_health
from axon.mcp import output as mcp_output
from axon.mcp.client import (
    MCPClient, MCPError, MCPTimeout, render_prompt_messages, render_resource_contents,
)
from axon.mcp.config import MCPConfig, ServerSpec
from axon.mcp.headers import DynamicHeaders, HeadersHelperError
from axon.mcp.oauth import OAuthManager, parse_www_authenticate
from axon.mcp.transports import (
    LegacySSETransport, StdioTransport, StreamableHTTPTransport, Transport, TransportError,
    log_path_for,
)
from axon.mcp.websocket import WebSocketTransport
from axon.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

DEFAULT_STARTUP_TIMEOUT = 10.0
CONNECT_TIMEOUT_CAP = 30.0
DEFAULT_MCP_TIMEOUT_MS = 30_000                 # MCP_TIMEOUT: how long one server may take to start
DEFAULT_TOOL_TIMEOUT_S = 100_000.0              # MCP_TOOL_TIMEOUT unset: about 28 hours
IDLE_TIMEOUT_REMOTE_S = 300.0                   # no response or progress for 5 minutes (HTTP, SSE, WS)
IDLE_TIMEOUT_STDIO_S = 1800.0                   # ... 30 minutes for stdio servers
CONNECT_RETRY_DELAYS = (0.5, 1.0, 2.0)          # a first HTTP/SSE connection retries transient errors 3 times
CONNECTING_GRACE_S = 10.0                       # a call waits this long for a server's first connection
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0)   # a dropped remote server: 5 attempts, exponential backoff
LOCAL_RECONNECT_DELAYS = (1.0, 2.0, 4.0)        # a crashed local (stdio) server: 3 attempts, backoff doubling
CRASH_BUDGET = 3                                # ... and at most this many crashes inside CRASH_WINDOW_S
CRASH_WINDOW_S = 120.0
LATE_START_RETRIES = ((2.0, 2.0), (5.0, 4.0))   # a stdio server that missed its connect timeout: (pause s, timeout x)
LATE_START_TIMEOUT_CAP = 120.0
AUTO_CONNECT_BACKOFF = (2.0, 5.0, 15.0, 30.0, 60.0)   # automation.connect_retries: pauses before each retry
FAILURE_STATES = ("failed", "disconnected")             # states that count towards auto-disable
DESC_LIMIT_UPFRONT = 4096                       # tool description loaded upfront / server instructions
DESC_LIMIT_DEFERRED = 16384                     # tool description loaded through ToolSearch
_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_PROP_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _sanitize(part: str) -> str:
    return _NAME_RE.sub("_", part).strip("_") or "x"


def mcp_server_prefix(server: str) -> str:
    """``mcp__<server>``: the permission rule that covers every tool of a server."""
    return f"mcp__{_sanitize(server)}"


def mcp_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` restricted to [a-zA-Z0-9_-]{1,64}."""
    name = f"{mcp_server_prefix(server)}__{_sanitize(tool)}"
    if len(name) > 64:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = name[:55] + "_" + digest
    return name


def _startup_timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("AXON_MCP_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT)))
    except ValueError:
        return DEFAULT_STARTUP_TIMEOUT


def mcp_timeout_s(spec_timeout: float = 60.0) -> float:
    """Per-server startup limit: ``MCP_TIMEOUT`` (milliseconds) when set, else the server's own
    timeout capped at 30 seconds."""
    ms = mcp_output.env_int("MCP_TIMEOUT", "AXON_MCP_TIMEOUT", default=0)
    return ms / 1000.0 if ms > 0 else min(spec_timeout, CONNECT_TIMEOUT_CAP)


def tool_call_limits(spec: ServerSpec) -> tuple[float, float | None]:
    """(wall-clock seconds, idle seconds or None) for one tool call.

    The wall clock is the server's own ``timeout`` when written in its config, else ``MCP_TOOL_TIMEOUT``
    (ms), else about 28 hours; progress notifications never extend it. The idle window
    (``CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`` ms, 0 = off) defaults to 5 minutes for remote servers and
    30 for stdio, and is never shorter than the server's explicit timeout."""
    if spec.timeout_explicit or spec.timeout != 60.0:
        wall = spec.timeout
    else:
        ms = mcp_output.env_int("MCP_TOOL_TIMEOUT", "AXON_MCP_TOOL_TIMEOUT", default=0)
        wall = ms / 1000.0 if ms >= 1000 else DEFAULT_TOOL_TIMEOUT_S
    default_idle = IDLE_TIMEOUT_STDIO_S if spec.type == "stdio" else IDLE_TIMEOUT_REMOTE_S
    raw = os.environ.get("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", os.environ.get("AXON_MCP_TOOL_IDLE_TIMEOUT", ""))
    idle: float | None = default_idle
    if raw.strip():
        try:
            ms = float(raw)
            idle = None if ms <= 0 else ms / 1000.0
        except ValueError:
            pass
    if idle is not None and (spec.timeout_explicit or spec.timeout != 60.0):
        idle = max(idle, spec.timeout)
    return wall, idle


def description_limits() -> tuple[int, int]:
    """(upfront, deferred) description caps; CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH replaces both."""
    n = mcp_output.env_int("CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH", "AXON_MAX_MCP_DESCRIPTION_LENGTH", default=0)
    return (n, n) if n > 0 else (DESC_LIMIT_UPFRONT, DESC_LIMIT_DEFERRED)


def is_ui_resource(res: dict[str, Any]) -> bool:
    """MCP Apps UI resources are pages for a host to render, not content for the model."""
    mime = str(res.get("mimeType") or "").lower().replace(" ", "")
    return str(res.get("uri") or "").startswith("ui://") or mime == "text/html;profile=mcp-app"


def _redact(message: str, spec: ServerSpec, resolved: ServerSpec | None = None) -> str:
    """Failure text for the UI: never the expanded URL (it can carry secrets), no credential-like text."""
    for url in filter(None, {spec.url, resolved.url if resolved else ""}):
        origin = urlsplit(url)
        shown = f"{origin.scheme}://{origin.netloc}" if origin.scheme else "server"
        message = message.replace(url, shown)
    message = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}", r"\1 [REDACTED]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{4,}",
                     r"\1[REDACTED]", message)
    for v in list(spec.headers.values()) + list((resolved.headers if resolved else {}).values()):
        if len(v) >= 8:
            message = message.replace(v, "[REDACTED]")
    return message


# ─── Connection ────────────────────────────────────────────────────────────

class MCPServerConnection:
    """A configured server plus its live client session and discovered tools."""

    def __init__(self, name: str, command: str = "", args: list[str] | None = None,
                 env: dict[str, str] | None = None, *, spec: ServerSpec | None = None,
                 workspace: Path | None = None, http_client: Any = None,
                 on_tools_changed: Callable[["MCPServerConnection"], None] | None = None,
                 oauth_client: Any = None,
                 elicitation_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
                 extra_roots: Callable[[], list[Path]] | None = None,
                 auto_reconnect: bool = True, reconnect_local: bool = False,
                 on_outcome: Callable[["MCPServerConnection"], None] | None = None) -> None:
        self.name = name
        self.spec = spec or ServerSpec(name=name, type="stdio", command=command,
                                       args=list(args or []), env=dict(env or {}))
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self.http_client = http_client
        self.oauth_client = oauth_client            # httpx.Client for the OAuth endpoints (tests)
        self.on_tools_changed = on_tools_changed
        self.elicitation_handler = elicitation_handler
        self.extra_roots = extra_roots
        self.auto_reconnect = auto_reconnect
        self.reconnect_local = reconnect_local      # also reconnect a local (stdio) server that exits
        self.on_outcome = on_outcome                # called when a reconnect cycle ends, good or bad
        self.client: MCPClient | None = None
        self.tools: list[dict[str, Any]] = []
        self.status = "disabled" if self.spec.disabled else "pending"
        self.error: str | None = None
        self.transport_kind = self.spec.type
        self.connected_at: float | None = None
        self.auth_challenge: dict[str, str] = {}
        self.oauth: OAuthManager | None = None
        self.instructions = ""
        self._gen = 0
        self._lock = threading.RLock()
        self._epoch = 0                 # bumped by stop()/start(): cancels a running reconnect loop
        self._timed_out = False         # the last start failed by missing the connect timeout
        self._crashes: list[float] = []

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

    def _headers_for(self, spec: ServerSpec) -> DynamicHeaders | None:
        """Dynamic headers (headersHelper and/or a stored OAuth token) for a remote server."""
        if spec.type == "stdio":
            return None
        oauth = None
        if spec.type in ("http", "sse") and (not spec.has_authorization_header or spec.oauth):
            oauth = self.oauth = OAuthManager(spec, http=self.oauth_client)
            oauth.challenge = self.auth_challenge
        dyn = DynamicHeaders(spec, self.workspace, oauth)
        return dyn if dyn.active else None

    def _make_transport(self, spec: ServerSpec, kind: str, dyn: DynamicHeaders | None = None) -> Transport:
        if kind == "stdio":
            cwd = spec.cwd or str(self.workspace)
            if spec.cwd and not os.path.isabs(spec.cwd):
                cwd = str(self.workspace / spec.cwd)
            return StdioTransport(self.name, spec.command, spec.args, spec.env, cwd,
                                  project_dir=self.workspace)
        provider = dyn if dyn is not None else None
        unauth = dyn.on_unauthorized if dyn is not None else None
        if kind == "ws":
            return WebSocketTransport(spec.url, spec.headers, spec.timeout, name=self.name,
                                      header_provider=provider)
        if kind == "sse":
            return LegacySSETransport(spec.url, spec.headers, spec.timeout, client=self.http_client,
                                      name=self.name, header_provider=provider, on_unauthorized=unauth)
        return StreamableHTTPTransport(spec.url, spec.headers, spec.timeout, client=self.http_client,
                                       name=self.name, header_provider=provider, on_unauthorized=unauth)

    def _open(self, spec: ServerSpec, kind: str, timeout: float, gen: int,
              dyn: DynamicHeaders | None = None) -> MCPClient:
        client = MCPClient(self.name, self._make_transport(spec, kind, dyn), timeout=spec.timeout,
                           workspace=self.workspace, on_tools_changed=self._tools_changed,
                           on_closed=self._closed, elicitation_handler=self.elicitation_handler,
                           extra_roots=self.extra_roots)
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

    # ── policy / approval gates ──────────────────────────────────────────
    def _gate(self) -> str | None:
        """A reason this server may not start (None when it may)."""
        spec = self.spec
        if spec.approval == "pending":
            self.status = "pending-approval"
            return "waiting for your approval (run /mcp approve, or axon in a terminal)"
        if spec.approval == "rejected":
            self.status = "rejected"
            return "rejected (see disabledMcpjsonServers or /mcp reset-project-choices)"
        try:
            from axon.mcp.policy import load_policy
            verdict = load_policy(self.workspace).check(spec)
        except Exception:
            verdict = None
        if verdict:
            self.status = "blocked"
            return f"MCP server {self.name} is blocked by enterprise managed policy"
        return None

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if isinstance(exc, MCPTimeout):
            return True
        cause = exc if isinstance(exc, TransportError) else exc.__cause__
        status = getattr(cause, "status", None)
        return isinstance(cause, TransportError) and (status is None or status >= 500)

    def _auth_error(self, cause: TransportError, spec: ServerSpec, dyn: DynamicHeaders | None) -> str | None:
        """The needs-authentication message for a 401/403 (None when a configured credential was rejected)."""
        configured = spec.has_authorization_header or (dyn is not None and dyn.helper_supplies_authorization)
        self.auth_challenge = parse_www_authenticate((cause.headers or {}).get("www-authenticate"))
        if self.oauth is not None:
            self.oauth.challenge = self.auth_challenge
        if configured:
            return None
        if self.auth_challenge.get("error") == "insufficient_scope":
            scope = self.auth_challenge.get("scope", "")
            return (f"needs additional permissions{f' (scope: {scope})' if scope else ''}; add the scope to "
                    f"oauth.scopes and authenticate again")
        return "needs authentication: run /mcp login " + self.name + " or `axon mcp login " + self.name + "`"

    def start(self, timeout: float | None = None) -> bool:
        """Connect, handshake and discover tools. Never raises; sets status/error.
        The lock is only held for state changes, so stop() can abort a slow handshake."""
        self._epoch += 1
        with waiting("mcp_connect", server=self.name):
            ok = self._start(timeout)
        if (not ok and timeout is None and self._timed_out and self.spec.type == "stdio"
                and self.auto_reconnect and LATE_START_RETRIES):
            threading.Thread(target=self._late_start_loop, args=(self._epoch,), daemon=True,
                             name=f"mcp-late-start-{self.name}").start()
        return ok

    def _late_start_loop(self, epoch: int) -> None:
        """A local server that was merely slow: try again a bounded number of times with a longer limit."""
        base = mcp_timeout_s(self.spec.timeout)
        for pause, factor in LATE_START_RETRIES:
            end = time.monotonic() + pause
            while time.monotonic() < end:
                if self._epoch != epoch:
                    return
                time.sleep(0.05)
            if self._epoch != epoch:
                return
            logger.debug("mcp %s: late start retry (timeout x%.0f)", self.name, factor)
            if self._start(min(base * factor, LATE_START_TIMEOUT_CAP)):
                if self.on_tools_changed:
                    self.on_tools_changed(self)
                self._report_outcome()
                return
            if self._epoch != epoch or not self._timed_out:
                return                      # stopped, or it now fails for another reason: not a slow start

    def _start(self, timeout: float | None = None) -> bool:
        with self._lock:
            self._stop_locked()
            self._gen += 1
            gen = self._gen
            problem = self.spec.validate()
            if problem:
                self.status, self.error = "failed", problem
                return False
            blocked = self._gate()
            if blocked:
                self.error = blocked
                return False
            unresolved = self.spec.unresolved_problems()
            if unresolved:
                self.status, self.error = "needs-config", "; ".join(unresolved)
                return False
            self.status, self.error = "connecting", None
        spec = self.spec.resolved(self.workspace)
        init_timeout = mcp_timeout_s(spec.timeout) if timeout is None else timeout
        logger.debug("mcp %s: starting (%s %s, timeout %.1fs)", self.name, spec.type,
                     getattr(spec, "url", "") or getattr(spec, "command", ""), init_timeout)
        dyn: DynamicHeaders | None = None
        try:
            dyn = self._headers_for(spec)
            if dyn is not None:
                dyn.prime()
        except HeadersHelperError as e:
            with self._lock:
                if gen == self._gen:
                    self.status, self.error = "failed", _redact(str(e), self.spec, spec)
            return False
        self._timed_out = False
        delays = (0.0,) + (CONNECT_RETRY_DELAYS if spec.type in ("http", "sse") else ())
        failure: Exception | None = None
        client: MCPClient | None = None
        kind = spec.type
        try:
            for attempt, delay in enumerate(delays):
                if delay:
                    time.sleep(delay)
                    if gen != self._gen:
                        return False
                try:
                    try:
                        client = self._open(spec, spec.type, init_timeout, gen, dyn)
                        kind = spec.type
                    except MCPError as e:
                        cause = e.__cause__
                        status = getattr(cause, "status", None)
                        # Backwards compatibility: an http URL that rejects POST may be a legacy SSE server.
                        if (spec.type == "http" and isinstance(cause, TransportError)
                                and status in (400, 404, 405) and gen == self._gen):
                            try:
                                client = self._open(spec, "sse", init_timeout, gen, dyn)
                            except (MCPError, TransportError) as e2:
                                c2 = e2 if isinstance(e2, TransportError) else e2.__cause__
                                if isinstance(c2, TransportError) and c2.status in (401, 403):
                                    raise
                                raise e  # the HTTP failure is the informative one
                            kind = "sse"
                        else:
                            raise
                    failure = None
                    break
                except Exception as e:
                    failure = e
                    if not (isinstance(e, (MCPError, TransportError)) and self._transient(e)) \
                            or attempt == len(delays) - 1:
                        break
                    logger.debug("mcp %s: transient failure (%s); retrying", self.name, e)
            if failure is not None:
                raise failure
            assert client is not None
            tools = client.list_tools()
        except Exception as e:  # a broken server must never crash Axon
            with self._lock:
                if gen == self._gen:
                    self._timed_out = isinstance(e, MCPTimeout)
                    cause = e.__cause__
                    code = getattr(cause, "status", None)
                    auth_msg = (self._auth_error(cause, spec, dyn)
                                if isinstance(cause, TransportError) and code in (401, 403) else None)
                    if auth_msg:
                        self.status, self.error = "needs-auth", auth_msg
                    else:
                        msg = str(e) if isinstance(e, (MCPError, TransportError, OSError)) \
                            else f"{type(e).__name__}: {e}"
                        if code == 404 and spec.type != "stdio":
                            msg = f"MCP endpoint not found at {_origin(spec.url)}. Check the URL in your MCP config."
                        self.status, self.error = "failed", _redact(msg, self.spec, spec)
                    self._stop_locked()
            logger.debug("mcp %s: failed to start: %s", self.name, self.error or e)
            return False
        with self._lock:
            if gen != self._gen:
                client.close()
                return False
            self.client, self.tools, self.transport_kind = client, tools, kind
            self.instructions = client.instructions
            self.status = "connected"
            self.error = None
            self.connected_at = time.time()
        logger.debug("mcp %s: connected over %s with %d tool(s): %s", self.name, kind, len(tools),
                     ", ".join(str(t.get("name", "?")) for t in tools[:30] if isinstance(t, dict)))
        return True

    def stop(self) -> None:
        logger.debug("mcp %s: stopping", self.name)
        with self._lock:
            self._gen += 1
            self._epoch += 1
            self._stop_locked()
            if self.status in ("connected", "connecting", "disconnected", "reconnecting"):
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
            logger.debug("mcp %s: disconnected: %s", self.name, reason)
            self.status, self.error = "disconnected", reason
            if not self.spec.remote and self._crashed_too_often():
                self.status = "failed"
                self.error = (f"MCP server \"{self.name}\" crashed {len(self._crashes)} times in "
                              f"{int(CRASH_WINDOW_S)}s ({reason}); not restarting it automatically. "
                              f"Fix it, then /mcp reconnect {self.name}")
                if self.on_tools_changed:
                    self.on_tools_changed(self)
                return
            if self._wants_reconnect():
                self._epoch += 1
                threading.Thread(target=self._reconnect_loop, args=(self._epoch,), daemon=True,
                                 name=f"mcp-reconnect-{self.name}").start()

    def _crashed_too_often(self) -> bool:
        now = time.monotonic()
        self._crashes = [t for t in self._crashes if now - t < CRASH_WINDOW_S] + [now]
        return len(self._crashes) > CRASH_BUDGET

    def _wants_reconnect(self) -> bool:
        # Local (stdio) servers reconnect too, with a shorter backoff and a crash budget (see _closed).
        return bool(self.auto_reconnect and not self.spec.disabled)

    def _reconnect_loop(self, epoch: int) -> None:
        """A remote server dropped mid-session: five attempts, backoff doubling from one second."""
        reason = self.error or "connection lost"
        for delay in (RECONNECT_DELAYS if self.spec.remote else LOCAL_RECONNECT_DELAYS):
            with self._lock:
                if self._epoch != epoch:
                    return
                self.status = "reconnecting"
            time.sleep(delay)
            if self._epoch != epoch:
                return
            if self._start():
                if self.on_tools_changed:
                    self.on_tools_changed(self)
                self._report_outcome()
                return
            if self.status == "needs-auth":
                break
        with self._lock:
            if self._epoch != epoch:
                return
            if self.status != "needs-auth":
                self.status = "failed"
                self.error = f"MCP server \"{self.name}\" disconnected ({reason}); reconnect from /mcp"
        if self.on_tools_changed:
            self.on_tools_changed(self)
        self._report_outcome()

    def _report_outcome(self) -> None:
        if self.on_outcome is not None:
            self.on_outcome(self)

    def _tools_changed(self, client: MCPClient) -> None:
        if client is not self.client:
            return
        try:
            tools = client.list_tools(refresh=True)
        except (MCPError, TransportError):
            return  # keep the tools we had until a later refresh succeeds
        self.tools = tools
        if self.on_tools_changed:
            self.on_tools_changed(self)

    def _require_client(self) -> MCPClient:
        if self.status == "connecting":  # first connection still in progress (e.g. just after a resume)
            deadline = time.monotonic() + CONNECTING_GRACE_S
            while self.status == "connecting" and time.monotonic() < deadline:
                time.sleep(0.05)
        if not self.connected and self.status in ("connected", "disconnected"):
            # The server process died or the session dropped: reconnect once.
            self.start()
        if not self.connected or self.client is None:
            detail = f": {self.error}" if self.error else ""
            raise ToolError(f"MCP server '{self.name}' is not connected ({self.status}{detail}).")
        return self.client

    def call_tool(self, tool_name: str, arguments: dict[str, Any], tool_spec: dict[str, Any] | None = None) -> str:
        client = self._require_client()
        logger.debug("mcp %s: call %s %s", self.name, tool_name, str(arguments)[:500])
        wall, idle = tool_call_limits(self.spec)
        annotations = (tool_spec or {}).get("annotations") or {}
        safe_to_repeat = bool(annotations.get("readOnlyHint") or annotations.get("idempotentHint")) \
            and self.spec.type in ("http", "sse", "ws")
        t0 = time.time()
        client.idle_timeout = idle
        try:
            try:
                with waiting("mcp_call", server=self.name, tool=tool_name):    # shown under the spinner
                    result = client.call_tool(tool_name, arguments, timeout=wall)
            except MCPError as e:
                if not (safe_to_repeat and client.closed and not isinstance(e, MCPTimeout)):
                    raise
                client = self._require_client()  # a call that was in flight when the connection closed
                client.idle_timeout = idle
                with waiting("mcp_call", server=self.name, tool=tool_name):
                    result = client.call_tool(tool_name, arguments, timeout=wall)
        except MCPError as e:
            logger.debug("mcp %s: call %s failed after %.2fs: %s", self.name, tool_name, time.time() - t0, e)
            raise ToolError(f"MCP tool '{tool_name}' on '{self.name}' failed: {e}") from e
        label = f"mcp-{self.name}-{tool_name}"
        text, is_error, has_image = mcp_output.render_for_model(result, workspace=self.workspace, label=label)
        text = mcp_output.limit_text(text, is_error=is_error, has_image=has_image,
                                     own_limit=mcp_output.tool_result_limit(tool_spec), workspace=self.workspace,
                                     label=f"{self.name}/{tool_name}", log=getattr(client, "_log", None))
        logger.debug("mcp %s: call %s %s in %.2fs (%d chars)", self.name, tool_name,
                     "returned an error" if is_error else "ok", time.time() - t0, len(text))
        if is_error:
            raise ToolError(f"MCP tool '{tool_name}' returned an error:\n{text}")
        return text

    def resources(self) -> list[dict[str, Any]]:
        return [r for r in self._require_client().list_resources(refresh=True) if not is_ui_resource(r)]

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

    def warnings(self) -> list[str]:
        return self.spec.config_warnings()


def _origin(url: str) -> str:
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme else url


# ─── Tool wrappers ─────────────────────────────────────────────────────────

def normalize_schema(schema: Any) -> tuple[dict[str, Any], str]:
    """An object schema the model API accepts, plus a sentence for the tool description.

    The API rejects ``anyOf``/``oneOf``/``allOf`` at the schema root, so they are flattened: properties
    from every branch are merged; ``allOf`` branches' ``required`` lists still apply, while for
    ``anyOf``/``oneOf`` they are described in the sentence instead of enforced."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}, ""
    out = dict(schema)
    combos = {k: out.pop(k) for k in ("allOf", "anyOf", "oneOf") if isinstance(out.get(k), list)}
    props = dict(out.get("properties") or {}) if isinstance(out.get("properties"), dict) else {}
    required = [str(r) for r in (out.get("required") or []) if isinstance(r, str)]
    groups: list[list[str]] = []
    for key, branches in combos.items():
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            for k, v in (branch.get("properties") or {}).items():
                props.setdefault(k, v)
            reqs = [str(r) for r in (branch.get("required") or []) if isinstance(r, str)]
            if key == "allOf":
                required += [r for r in reqs if r not in required]
            elif reqs:
                groups.append(reqs)
    out["type"] = "object"
    out["properties"] = props
    if required:
        out["required"] = required
    else:
        out.pop("required", None)
    note = ""
    if groups:
        note = ("Provide one of these parameter groups: "
                + " | ".join("(" + ", ".join(g) + ")" for g in groups) + ". ")
    return out, note


def tool_problem(spec: dict[str, Any]) -> str | None:
    """Why a server tool must be left out (the API would reject its whole request), else None."""
    schema = spec.get("inputSchema")
    if schema is None:
        return None
    if not isinstance(schema, dict):
        return "inputSchema is not an object"
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        return "inputSchema.properties is not an object"
    for pname in (props or {}):
        if not _PROP_RE.match(str(pname)):
            return f"property name {str(pname)[:40]!r} must be 1-64 characters of letters, digits, _ . -"
    if "required" in schema and not (isinstance(schema["required"], list)
                                     and all(isinstance(r, str) for r in schema["required"])):
        return "inputSchema.required must be a list of strings"
    stype = schema.get("type")
    if stype is not None and not (isinstance(stype, str) or isinstance(stype, list)):
        return "inputSchema.type is invalid"
    return None


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
        meta = tool_spec.get("_meta") if isinstance(tool_spec.get("_meta"), dict) else {}
        title = tool_spec.get("title") or annotations.get("title")
        desc = str(tool_spec.get("description") or title or "MCP tool")
        schema, note = normalize_schema(tool_spec.get("inputSchema"))
        upfront, deferred = description_limits()
        full = f"[MCP:{server_name}] {note}{desc}"
        self.name = name or mcp_tool_name(server_name, mcp_name)  # type: ignore[misc]
        self.description = full[:upfront]  # type: ignore[misc]
        self.full_description = full[:deferred]  # shown when ToolSearch loads the tool
        self.schema = schema  # type: ignore[misc]
        self.readonly = bool(annotations.get("readOnlyHint", False))  # type: ignore[misc]
        self.default_permission = "ask"  # type: ignore[misc]
        alw = meta.get("anthropic/alwaysLoad")
        self.always_load: bool | None = alw if isinstance(alw, bool) else None
        # A tool whose own permission prompt is the point (consent / access grants): see the note in
        # docs/extending.md - Axon always asks for these, whatever the permission mode.
        self.requires_user_interaction = meta.get("anthropic/requiresUserInteraction") is True
        self._server_name = server_name
        self._mcp_tool_name = mcp_name
        self._tool_spec = tool_spec
        self._connection = connection

    @property
    def server_name(self) -> str:
        return self._server_name

    def loads_upfront(self) -> bool:
        """alwaysLoad: the server's setting beats the tool's own hint; None = follow tool search."""
        server = self._connection.spec.always_load
        if server is not None:
            return server
        return bool(self.always_load)

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return self._connection.call_tool(self._mcp_tool_name, args, self._tool_spec)

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
    # a ToolSearch-revealed tool is registered as a hidden proxy around the wrapper: drop that too
    if current is not expected and getattr(current, "target", None) is not expected:
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
        self.oauth_client: Any = None                    # httpx.Client for OAuth endpoints (tests)
        self.elicitation_handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
        self.extra_roots: list[Path] = []                # additional working directories (roots/list)
        self.excluded: dict[str, list[tuple[str, str]]] = {}    # server -> [(tool, reason)] left out
        self.context_window: int = 0                     # set by the CLI; used to decide MCP tool deferral
        self.automation: Any = None                      # axon.marketplace.automation.Automation, once loaded

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
            if self._server_tools:
                self._defer_tools()
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
            skipped: list[tuple[str, str]] = []
            for spec in conn.tools:
                problem = tool_problem(spec)
                if problem:  # one malformed schema would make every API request fail
                    skipped.append((str(spec.get("name", "?")), problem))
                    logger.warning("mcp %s: tool %s left out: %s", conn.name, spec.get("name"), problem)
                    continue
                base = mcp_tool_name(conn.name, str(spec.get("name", "")))
                name, i = base, 2
                while name in taken:
                    suffix = f"_{i}"
                    name, i = base[:64 - len(suffix)] + suffix, i + 1
                taken.add(name)
                wrappers.append(MCPToolWrapper(conn.name, spec, conn, name=name))
            self._server_tools[conn.name] = wrappers
            self.excluded[conn.name] = skipped
            if self._registry is not None:
                for w in wrappers:
                    self._registry.register(w)
                self._defer_tools()
            self._sync_generic()
            return wrappers

    def _defer_tools(self) -> None:
        """Apply MCP tool deferral to tools that arrive after startup (late connect, login, reconnect)."""
        try:
            from axon.tools.interactive import defer_mcp_tools
            defer_mcp_tools(self._registry, self.context_window or 200_000)
        except Exception as e:
            logger.warning("mcp: tool deferral failed: %s", e)

    def _unpublish(self, name: str) -> None:
        with self._lock:
            old = self._server_tools.pop(name, [])
            if self._registry is not None:
                for t in old:
                    _registry_unregister(self._registry, t.name, t)
                from axon.tools.interactive import forget_deferred
                forget_deferred(self._registry, [t.name for t in old])

    # ── connecting ──────────────────────────────────────────────────────
    def _elicitation(self) -> Callable[[str, dict[str, Any]], dict[str, Any]] | None:
        if self.elicitation_handler is not None:
            return self.elicitation_handler
        from axon.mcp import state as mcp_state
        if mcp_state.is_interactive():
            from axon.mcp.elicit import terminal_elicitation, with_hooks
            return with_hooks(terminal_elicitation)
        return None

    def _make_connection(self, spec: ServerSpec) -> MCPServerConnection:
        http_client = self.http_client_factory(spec) if self.http_client_factory and spec.type != "stdio" else None
        auto = self.automation
        reconnect = not (auto is not None and auto.enabled and not auto.auto_reconnect)
        return MCPServerConnection(spec.name, spec=spec, workspace=self.workspace,
                                   http_client=http_client, on_tools_changed=self._publish,
                                   oauth_client=self.oauth_client, elicitation_handler=self._elicitation(),
                                   extra_roots=lambda: list(self.extra_roots),
                                   auto_reconnect=reconnect,
                                   reconnect_local=bool(auto is not None and auto.on("auto_reconnect")),
                                   on_outcome=self._record_outcome)

    # ── automation: retries, health ledger, auto-disable ─────────────────
    def _retry_delays(self) -> tuple[float, ...]:
        auto = self.automation
        if auto is None or not auto.on("auto_connect"):
            return ()
        return AUTO_CONNECT_BACKOFF[:auto.connect_retries]

    def _start_with_retries(self, conn: MCPServerConnection) -> None:
        conn.start()
        for delay in self._retry_delays():
            if conn.status != "failed" or conn.spec.validate():
                return
            if not self._sleep_while_current(conn, delay):
                return
            conn.start()

    def _sleep_while_current(self, conn: MCPServerConnection, seconds: float) -> bool:
        """Wait, but give up early (False) when the connection was replaced or stopped."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._connections.get(conn.name) is not conn:
                return False
            time.sleep(0.1)
        return self._connections.get(conn.name) is conn

    def _record_outcome(self, conn: MCPServerConnection, count_failure: bool = True) -> None:
        auto = self.automation
        if auto is None or not auto.enabled:
            return
        if conn.status == "connected":
            mcp_health.record_success(conn.name)
            return
        if not count_failure or conn.status not in FAILURE_STATES:
            return
        entry = mcp_health.record_failure(conn.name, conn.error or conn.status)
        if mcp_health.should_disable(entry.failures, auto.auto_disable_after):
            self._auto_disable(conn, entry.failures)

    def _auto_disable(self, conn: MCPServerConnection, failures: int) -> None:
        if MCPConfig(self.workspace).toggle(conn.name, disabled=True) is None:
            return
        mcp_health.mark_auto_disabled(conn.name)
        self.disconnect_server(conn.name)
        reason = (conn.error or conn.status)[:80]
        notices.post(f"MCP server '{conn.name}' was switched off after {failures} failed connections "
                     f"({reason}). Turn it back on with: /marketplace enable {conn.name}")

    def set_extra_roots(self, dirs: list[Path]) -> None:
        """Additional working directories answer roots/list; servers are told when the set changes."""
        new = [Path(d) for d in dirs]
        if new == self.extra_roots:
            return
        self.extra_roots = new
        for conn in list(self._connections.values()):
            if conn.connected and conn.client is not None:
                conn.client.notify_roots_changed()

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
            self._record_outcome(conn, count_failure=False)
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
                self._start_with_retries(c)
                if self._connections.get(c.name) is c:
                    self._publish(c)
                    self._record_outcome(c)
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

    def pending_servers(self) -> list[str]:
        """Servers still connecting or reconnecting in the background."""
        return sorted(n for n, c in self._connections.items() if c.status in ("connecting", "reconnecting"))

    def wait_for_servers(self, timeout: float = 10.0) -> list[str]:
        """Block until no server is still connecting (or ``timeout``); returns those still pending."""
        deadline = time.monotonic() + timeout
        while self.pending_servers() and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.pending_servers()

    def failed_servers(self) -> list[tuple[str, str, str]]:
        """(name, status, error) of servers that are not usable right now."""
        return [(n, c.status, c.error or "") for n, c in sorted(self._connections.items())
                if c.status in ("failed", "needs-auth", "needs-config", "disconnected", "blocked", "rejected")]

    def reconnect_all(self, specs: dict[str, ServerSpec]) -> list[str]:
        """Reconnect every server that failed or needs authentication (``/mcp reconnect all``)."""
        done = []
        for name, spec in specs.items():
            conn = self._connections.get(name)
            if spec.disabled or (conn is not None and conn.status in ("connected", "connecting")):
                continue
            self.connect_server(spec)
            done.append(name)
        return done

    def instructions_block(self) -> str:
        """Server-provided instructions of connected servers, for the system prompt (each capped)."""
        upfront, _ = description_limits()
        parts = []
        for name, c in sorted(self._connections.items()):
            text = (c.instructions or "").strip()
            if c.connected and text:
                parts.append(f"### {name}\n{text[:upfront]}")
        if not parts:
            return ""
        return "## MCP server instructions\n\nThese come from connected MCP servers.\n\n" + "\n\n".join(parts)

    # ── reporting ───────────────────────────────────────────────────────
    def status_rows(self) -> list[dict[str, Any]]:
        rows = []
        for name, c in sorted(self._connections.items()):
            auth = ""
            if c.spec.remote and c.spec.type != "ws":
                try:
                    auth = (OAuthManager(c.spec.resolved(self.workspace)).status() if c.oauth is None
                            else c.oauth.status())
                except Exception:
                    auth = ""
            rows.append({
                "name": name, "status": c.status, "transport": c.transport_kind,
                "target": c.spec.describe_target(), "tools": len(self._server_tools.get(name, [])),
                "error": c.error, "scope": c.spec.scope,
                "server": (c.client.server_info if c.client else {}),
                "protocol": (c.client.protocol_version if c.client else None),
                "auth": auth, "warnings": c.warnings(), "excluded": list(self.excluded.get(name, [])),
                "advertises_tools": bool(c.client and c.client.has("tools")),
            })
        return rows

    def startup_summary(self) -> str:
        rows = self.status_rows()
        if not rows:
            return ""
        ok = [r for r in rows if r["status"] == "connected"]
        pending = [r for r in rows if r["status"] == "connecting"]
        bad = [r for r in rows if r["status"] in ("failed", "needs-config", "disconnected", "needs-auth")]
        parts = [f"MCP: {len(ok)} connected ({sum(r['tools'] for r in ok)} tools)"]
        if pending:
            parts.append(f"{len(pending)} still connecting")
        if bad:
            parts.append("failed: " + ", ".join(f"{r['name']} ({(r['error'] or r['status'])[:80]})"
                                                for r in bad))
        waiting = [r["name"] for r in rows if r["status"] == "pending-approval"]
        if waiting:
            parts.append("awaiting approval: " + ", ".join(waiting) + " (/mcp approve)")
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
            if c.name == "anthropic-skills":  # that name is reserved for skills synced from claude.ai
                continue
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
                          timeout: float | None = None, extra_roots: list[Path] | None = None) -> list[Tool]:
    """Connect all configured, enabled servers (parallel, bounded by ``timeout`` /
    $AXON_MCP_STARTUP_TIMEOUT, default 10s). Returns tools ready now; when ``registry``
    is given it is kept in sync, including servers that finish connecting later."""
    specs = MCPConfig(workspace).load_specs()
    bridge = get_bridge(workspace)
    timeout = _apply_automation(bridge, workspace, timeout)
    if extra_roots:
        bridge.extra_roots = [Path(d) for d in extra_roots]
    if registry is not None:
        bridge.attach_registry(registry)
    if not specs:
        return []
    from axon.mcp import approval, state
    if state.is_interactive() and approval.pending(specs):
        try:
            approval.review(Path(workspace), specs)
        except Exception:  # a prompt problem must never stop startup
            pass
    return bridge.connect_all(specs, timeout)


def _apply_automation(bridge: MCPToolBridge, workspace: Path, timeout: float | None) -> float | None:
    """Load the automation settings into the bridge and start the background tasks. With automation
    on, startup waits only ``startup_wait`` seconds for servers; the rest connect in the background."""
    try:
        from axon.marketplace import startup
        auto = startup.begin(workspace)
    except Exception:  # noqa: BLE001 - automation must never stop startup
        return timeout
    bridge.automation = auto
    if timeout is None and auto.on("auto_connect"):
        return auto.startup_wait
    return timeout


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
