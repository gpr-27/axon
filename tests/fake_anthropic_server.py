"""A tiny keep-alive HTTP/1.1 server speaking the Anthropic Messages SSE wire (POST /v1/messages) and the OpenAI
Chat Completions SSE wire (POST .../chat/completions), for real-HTTP-stack tests (plain or TLS)."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def _sse(event: str, data: dict[str, Any]) -> bytes:
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


class FakeAnthropic:
    """``plans[i]`` configures request i (the last plan repeats): dict(chunks=int, gap=float, header_delay=float,
    silent=float (seconds with no bytes after headers), drop=True (close the connection without answering),
    tool=True (answer with a streamed Ls tool_use instead of text; its arguments stream over `chunks` * `gap`))."""

    def __init__(self, plans: list[dict[str, Any]] | None = None, tls: tuple[str, str] | None = None) -> None:
        self.plans = plans or [{}]
        self.requests: list[dict[str, Any]] = []     # {"n", "conn", "body"}
        self.connections: set[int] = set()
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                pass

            def _openai(self, plan: dict[str, Any], n: int) -> None:
                def chunk(obj: Any) -> bytes:
                    payload = (f"data: {json.dumps(obj)}\n\n" if obj != "[DONE]" else "data: [DONE]\n\n").encode()
                    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"
                try:
                    time.sleep(plan.get("header_delay", 0))
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.send_header("connection", "keep-alive")
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(plan.get("silent", 0))
                    for i in range(plan.get("chunks", 3)):
                        self.wfile.write(chunk({"id": f"c{n}", "model": "m", "choices": [{"index": 0, "delta": {"content": f"c{i} "}}]}))
                        self.wfile.flush()
                        time.sleep(plan.get("gap", 0.0))
                    self.wfile.write(chunk({"id": f"c{n}", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}))
                    self.wfile.write(chunk("[DONE]"))
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                with outer.lock:
                    n = len(outer.requests)
                    outer.requests.append({"n": n, "conn": id(self.connection), "body": body})
                    outer.connections.add(id(self.connection))
                plan = outer.plans[min(n, len(outer.plans) - 1)]
                if plan.get("drop"):
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if self.path.endswith("/chat/completions"):
                    return self._openai(plan, n)
                try:
                    time.sleep(plan.get("header_delay", 0))
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.send_header("connection", "keep-alive")
                    self.end_headers()
                    self.wfile.flush()
                    time.sleep(plan.get("silent", 0))
                    w = self.wfile
                    w.write(_sse("message_start", {"type": "message_start", "message": {
                        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "m", "content": [],
                        "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}))
                    w.write(_sse("content_block_start", {"type": "content_block_start", "index": 0,
                                                         "content_block": {"type": "text", "text": ""}}))
                    if plan.get("tool"):
                        w.write(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
                        w.write(_sse("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                            "type": "tool_use", "id": f"toolu_{n}", "name": "Ls", "input": {}}}))
                        for frag in ('{"pa', 'th":', ' "."', '}'):
                            w.write(_sse("content_block_delta", {"type": "content_block_delta", "index": 1,
                                                                 "delta": {"type": "input_json_delta", "partial_json": frag}}))
                            w.flush()
                            time.sleep(plan.get("gap", 0.0))
                        w.write(_sse("content_block_stop", {"type": "content_block_stop", "index": 1}))
                        w.write(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                                                       "usage": {"output_tokens": 3}}))
                        w.write(_sse("message_stop", {"type": "message_stop"}))
                        w.write(b"0\r\n\r\n")
                        w.flush()
                        return
                    for i in range(plan.get("chunks", 3)):
                        w.write(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                             "delta": {"type": "text_delta", "text": f"c{i} "}}))
                        w.flush()
                        time.sleep(plan.get("gap", 0.0))
                    w.write(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
                    w.write(_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                                   "usage": {"output_tokens": 3}}))
                    w.write(_sse("message_stop", {"type": "message_stop"}))
                    w.write(b"0\r\n\r\n")
                    w.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
