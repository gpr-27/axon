"""
Spec-compliant MCP client session (protocol 2025-06-18, negotiates 2025-03-26 / 2024-11-05).

JSON-RPC 2.0 over any :mod:`axon.mcp.transports` transport: monotonically increasing
request ids, pending-request futures resolved by the transport's reader, per-request
timeouts with ``notifications/cancelled``, server-initiated requests (ping, roots/list;
sampling/elicitation rejected) and notifications (list_changed, progress, logging).
"""
from __future__ import annotations

import base64
import itertools
import json
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from axon.mcp.transports import SessionExpired, Transport, TransportError, log_path_for

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_PAGES = 100

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_TIMEOUT = -32001


def _client_version() -> str:
    try:
        from importlib.metadata import version
        return version("axon-terminal")
    except Exception:
        return "0.0.0"


class MCPError(Exception):
    """JSON-RPC error returned by the server (or a local protocol failure)."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class MCPTimeout(MCPError):
    def __init__(self, message: str) -> None:
        super().__init__(message, REQUEST_TIMEOUT)


class MCPClient:
    """One initialized session with one MCP server."""

    def __init__(self, name: str, transport: Transport, *, timeout: float = 60.0,
                 workspace: Path | None = None,
                 on_tools_changed: Callable[["MCPClient"], None] | None = None,
                 on_closed: Callable[["MCPClient", str], None] | None = None) -> None:
        self.name = name
        self.transport = transport
        self.timeout = timeout
        self.workspace = Path(workspace) if workspace else None
        self.on_tools_changed = on_tools_changed
        self.on_closed = on_closed
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""
        self.closed = False
        self.close_reason = ""
        self.progress: dict[Any, dict[str, Any]] = {}
        self.log_path = log_path_for(name)
        self._ids = itertools.count(1)
        self._pending: dict[int | str, Future] = {}
        self._lock = threading.Lock()
        self._tool_cache: list[dict[str, Any]] | None = None
        self._resource_cache: list[dict[str, Any]] | None = None
        self._prompt_cache: list[dict[str, Any]] | None = None

    # ── logging ─────────────────────────────────────────────────────────
    def _log(self, text: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        except OSError:
            pass

    # ── lifecycle ───────────────────────────────────────────────────────
    def connect(self, timeout: float | None = None) -> dict[str, Any]:
        """Start the transport and perform the initialize handshake."""
        self.transport.start(self._on_message, self._on_transport_closed)
        return self.initialize(timeout)

    def initialize(self, timeout: float | None = None) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "axon", "title": "Axon", "version": _client_version()},
        }, timeout=timeout)
        version = str(result.get("protocolVersion") or "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(f"server '{self.name}' uses unsupported MCP protocol version "
                           f"'{version}' (supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)})",
                           INVALID_REQUEST)
        self.protocol_version = version
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self.instructions = str(result.get("instructions") or "")
        self.transport.set_protocol_version(version)
        self.notify("notifications/initialized")
        try:
            self.transport.after_initialize()
        except Exception:
            pass
        return result

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = self.close_reason or "closed by client"
        self._fail_all(MCPError(f"MCP server '{self.name}' connection closed"))
        try:
            self.transport.close()
        except Exception:
            pass

    def _on_transport_closed(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self.close_reason = reason
            self._log(f"connection closed: {reason}")
        self._fail_all(MCPError(f"MCP server '{self.name}' disconnected: {reason}"))
        if self.on_closed:
            try:
                self.on_closed(self, reason)
            except Exception:
                pass

    def _fail_all(self, exc: Exception) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    # ── JSON-RPC core ───────────────────────────────────────────────────
    def request(self, method: str, params: dict[str, Any] | None = None,
                timeout: float | None = None, _retry: bool = True) -> dict[str, Any]:
        if self.closed:
            raise MCPError(f"MCP server '{self.name}' is not connected"
                           + (f" ({self.close_reason})" if self.close_reason else ""))
        req_id = next(self._ids)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        fut: Future = Future()
        with self._lock:
            self._pending[req_id] = fut
        try:
            self.transport.send(msg)
        except SessionExpired:
            with self._lock:
                self._pending.pop(req_id, None)
            if not _retry or method == "initialize":
                raise MCPError(f"MCP session with '{self.name}' expired")
            # Spec: start a new session by re-initializing without the old session id.
            setattr(self.transport, "session_id", None)
            self.initialize(timeout)
            return self.request(method, params, timeout, _retry=False)
        except TransportError as e:
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPError(f"{self.name}: {e}") from e
        wait = self.timeout if timeout is None else timeout
        try:
            result = fut.result(timeout=wait)
        except FutureTimeout:
            with self._lock:
                self._pending.pop(req_id, None)
            if method != "initialize":
                self.notify("notifications/cancelled",
                            {"requestId": req_id, "reason": f"timed out after {wait:g}s"})
            raise MCPTimeout(f"MCP server '{self.name}' did not answer '{method}' within {wait:g}s")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.transport.send(msg)
        except TransportError as e:
            self._log(f"notification {method} not delivered: {e}")

    def _respond(self, req_id: Any, result: dict[str, Any] | None = None,
                 error: tuple[int, str] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = {"code": error[0], "message": error[1]}
        else:
            msg["result"] = result or {}
        try:
            self.transport.send(msg)
        except TransportError as e:
            self._log(f"response to server request {req_id} not delivered: {e}")

    def _on_message(self, msg: dict[str, Any]) -> None:
        if "method" not in msg and "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return  # late reply to a cancelled/timed-out request
            if "error" in msg and msg["error"] is not None:
                err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                fut.set_exception(MCPError(str(err.get("message", "unknown error")),
                                           int(err.get("code", INTERNAL_ERROR) or INTERNAL_ERROR),
                                           err.get("data")))
            else:
                fut.set_result(msg.get("result"))
            return
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if "id" in msg and msg["id"] is not None:
            # Answer server requests off the reader thread so it can keep reading.
            threading.Thread(target=self._handle_server_request,
                             args=(msg["id"], method, msg.get("params") or {}),
                             daemon=True).start()
        else:
            self._handle_notification(method, msg.get("params") or {})

    def _handle_server_request(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        if method == "ping":
            self._respond(req_id, {})
        elif method == "roots/list":
            roots = []
            if self.workspace is not None:
                roots.append({"uri": self.workspace.resolve().as_uri(), "name": self.workspace.name})
            self._respond(req_id, {"roots": roots})
        elif method in ("sampling/createMessage", "elicitation/create"):
            self._respond(req_id, error=(METHOD_NOT_FOUND,
                                         f"Axon does not support '{method}'; the request was declined."))
        else:
            self._respond(req_id, error=(METHOD_NOT_FOUND, f"Method not found: {method}"))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/tools/list_changed":
            self._tool_cache = None
            if self.on_tools_changed:
                threading.Thread(target=self._safe_tools_changed, daemon=True).start()
        elif method == "notifications/resources/list_changed":
            self._resource_cache = None
        elif method == "notifications/prompts/list_changed":
            self._prompt_cache = None
        elif method == "notifications/progress":
            token = params.get("progressToken")
            self.progress[token] = {"progress": params.get("progress"), "total": params.get("total"),
                                    "message": params.get("message"), "time": time.time()}
        elif method == "notifications/message":
            level = params.get("level", "info")
            data = params.get("data")
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            self._log(f"[{level}] {params.get('logger') or self.name}: {text}")
        elif method == "notifications/cancelled":
            self._log(f"server cancelled request {params.get('requestId')}: {params.get('reason', '')}")

    def _safe_tools_changed(self) -> None:
        try:
            assert self.on_tools_changed is not None
            self.on_tools_changed(self)
        except Exception as e:
            self._log(f"tools refresh failed: {e}")

    # ── MCP features ───────────────────────────────────────────────────
    def _paginate(self, method: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            result = self.request(method, {"cursor": cursor} if cursor else {})
            items.extend(i for i in (result.get(key) or []) if isinstance(i, dict))
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return items

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def list_tools(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tool_cache is None or refresh:
            self._tool_cache = self._paginate("tools/list", "tools") if self.has("tools") else []
        return list(self._tool_cache)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None,
                  timeout: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def list_resources(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._resource_cache is None or refresh:
            self._resource_cache = (self._paginate("resources/list", "resources")
                                    if self.has("resources") else [])
        return list(self._resource_cache)

    def list_resource_templates(self) -> list[dict[str, Any]]:
        if not self.has("resources"):
            return []
        try:
            return self._paginate("resources/templates/list", "resourceTemplates")
        except MCPError as e:
            if e.code == METHOD_NOT_FOUND:
                return []
            raise

    def read_resource(self, uri: str) -> dict[str, Any]:
        return self.request("resources/read", {"uri": uri})

    def list_prompts(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._prompt_cache is None or refresh:
            self._prompt_cache = self._paginate("prompts/list", "prompts") if self.has("prompts") else []
        return list(self._prompt_cache)

    def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = {k: str(v) for k, v in arguments.items()}
        return self.request("prompts/get", params)

    def ping(self, timeout: float = 10.0) -> bool:
        try:
            self.request("ping", timeout=timeout)
            return True
        except MCPError:
            return False


# ─── Rendering results for the model ──────────────────────────────────────

def _b64_size(data: str) -> int:
    try:
        return len(base64.b64decode(data, validate=False))
    except Exception:
        return len(data) * 3 // 4


def render_content_block(block: Any) -> str:
    if not isinstance(block, dict):
        return str(block)
    btype = block.get("type")
    if btype == "text":
        return str(block.get("text", ""))
    if btype in ("image", "audio"):
        return (f"[{btype}: {block.get('mimeType', 'unknown')}, "
                f"{_b64_size(str(block.get('data', '')))} bytes]")
    if btype == "resource":
        res = block.get("resource") or {}
        return render_resource_contents(res)
    if btype == "resource_link":
        label = block.get("title") or block.get("name") or ""
        desc = f" - {block['description']}" if block.get("description") else ""
        return f"[resource link: {label} <{block.get('uri', '')}>{desc}]".replace(":  <", ": <")
    return json.dumps(block, ensure_ascii=False)


def render_resource_contents(res: dict[str, Any]) -> str:
    uri = res.get("uri", "")
    if "text" in res:
        return f"--- {uri} ({res.get('mimeType', 'text/plain')}) ---\n{res.get('text', '')}"
    if "blob" in res:
        return f"[binary resource {uri}: {res.get('mimeType', 'application/octet-stream')}, " \
               f"{_b64_size(str(res.get('blob', '')))} bytes]"
    return json.dumps(res, ensure_ascii=False)


def render_tool_result(result: dict[str, Any]) -> tuple[str, bool]:
    """Return (text for the model, is_error)."""
    parts = [render_content_block(b) for b in (result.get("content") or [])]
    text = "\n".join(p for p in parts if p)
    structured = result.get("structuredContent")
    if structured is not None:
        dumped = json.dumps(structured, ensure_ascii=False, indent=2)
        if not text:
            text = dumped
        elif dumped not in text and json.dumps(structured, ensure_ascii=False) not in text:
            text = f"{text}\n\nstructuredContent:\n{dumped}"
    if not text:
        text = "(no content)"
    return text, bool(result.get("isError"))


def render_prompt_messages(result: dict[str, Any]) -> str:
    lines: list[tuple[str, str]] = []
    for m in result.get("messages") or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        blocks = content if isinstance(content, list) else [content]
        body = "\n".join(render_content_block(b) for b in blocks if b is not None)
        lines.append((str(m.get("role") or "user"), body))
    if len(lines) == 1 and lines[0][0] == "user":
        return lines[0][1]
    return "\n\n".join(f"[{role}]\n{body}" for role, body in lines)
