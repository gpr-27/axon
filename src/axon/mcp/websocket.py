"""
A small RFC 6455 WebSocket client for MCP servers that hold a persistent connection (``"type": "ws"``).

Each text frame carries one JSON-RPC message. Authentication is header-only (static ``headers`` or a
``headersHelper``). No third-party dependency: sockets, ``ssl`` and ``hashlib`` from the standard library.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
from typing import Any
from urllib.parse import urlsplit

from axon.mcp.transports import (
    HeaderProvider, OnClose, OnMessage, Transport, TransportError, _dispatch,
)

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    if n < 126:
        head += bytes([flag | n])
    elif n < 65536:
        head += bytes([flag | 126]) + struct.pack("!H", n)
    else:
        head += bytes([flag | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return head + key + masked


class WebSocketTransport(Transport):
    kind = "ws"

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 60.0,
                 name: str = "server", header_provider: HeaderProvider | None = None,
                 connect_timeout: float = 15.0, max_message: int = MAX_MESSAGE_BYTES) -> None:
        self.url = url
        self.name = name
        self.headers = dict(headers or {})
        self.header_provider = header_provider
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_message = max_message
        self._sock: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._closed = False
        self._on_close: OnClose | None = None
        self._pending = b""

    # ── connection ──────────────────────────────────────────────────────
    def _handshake(self, sock: socket.socket, parts: Any) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        extra = dict(self.headers)
        if self.header_provider is not None:
            extra.update(self.header_provider())
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "Sec-WebSocket-Protocol: mcp"]
        lines += [f"{k}: {v}" for k, v in extra.items()
                  if k.lower() not in ("host", "upgrade", "connection", "sec-websocket-key")]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                raise TransportError("WebSocket handshake failed: connection closed")
            raw += chunk
            if len(raw) > 65536:
                raise TransportError("WebSocket handshake failed: oversized response")
        head, _, rest = raw.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            raise TransportError(f"WebSocket handshake failed: bad status line {status_line!r}") from None
        resp_headers = {}
        for line in header_lines:
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()
        if status != 101:
            raise TransportError(f"HTTP {status} from {self.url}", status=status, headers=resp_headers)
        if resp_headers.get("sec-websocket-accept") != accept_key(key):
            raise TransportError("WebSocket handshake failed: bad Sec-WebSocket-Accept")
        self._pending = rest

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise TransportError(f"not a WebSocket URL: {self.url}")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        self._on_close = on_close
        try:
            sock = socket.create_connection((parts.hostname, port), timeout=self.connect_timeout)
            if parts.scheme == "wss":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
            sock.settimeout(self.connect_timeout)
            self._pending = b""
            self._handshake(sock, parts)
            sock.settimeout(None)
        except TransportError:
            raise
        except (OSError, ssl.SSLError) as e:
            raise TransportError(f"WebSocket connect failed: {e}") from e
        self._sock = sock
        threading.Thread(target=self._read_loop, args=(on_message,), name=f"mcp-{self.name}-ws",
                         daemon=True).start()

    # ── reading ─────────────────────────────────────────────────────────
    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = self._pending[:n]
        self._pending = self._pending[n:]
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("connection closed")
            buf += chunk
        return buf

    def _read_loop(self, on_message: OnMessage) -> None:
        reason = "WebSocket closed"
        message = b""
        try:
            while not self._closed:
                b1, b2 = self._recv_exact(2)
                opcode, masked, n = b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._recv_exact(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._recv_exact(8))[0]
                if n > self.max_message or len(message) + n > self.max_message:
                    reason = "WebSocket message over 16 MiB; connection closed"
                    break
                key = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(n) if n else b""
                if masked:
                    payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                elif opcode == OP_CLOSE:
                    self._send_frame(OP_CLOSE, payload[:2])
                    reason = "WebSocket closed by server"
                    break
                elif opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                    message += payload
                    if b1 & 0x80:  # FIN
                        data, message = message, b""
                        try:
                            _dispatch(json.loads(data.decode("utf-8", "replace")), on_message)
                        except ValueError:
                            continue
        except (EOFError, OSError, ssl.SSLError) as e:
            reason = f"WebSocket closed: {e}"
        finally:
            self._shutdown_socket()
            if not self._closed and self._on_close:
                self._on_close(reason)

    # ── writing / closing ───────────────────────────────────────────────
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise TransportError("WebSocket is not connected")
        with self._write_lock:
            try:
                sock.sendall(encode_frame(opcode, payload))
            except (OSError, ssl.SSLError) as e:
                raise TransportError(f"write failed: {e}") from e

    def send(self, message: dict[str, Any]) -> None:
        if self._closed:
            raise TransportError("transport closed")
        self._send_frame(OP_TEXT, json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def _shutdown_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except TransportError:
            pass
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._shutdown_socket()
