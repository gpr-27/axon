"""
A real local HTTP server that impersonates an LLM provider for end-to-end tests.

It speaks two wire formats on 127.0.0.1:<random port>:

* OpenAI-compatible: ``POST /v1/chat/completions`` (SSE when ``stream`` is true, JSON otherwise)
  and ``GET /v1/models``.
* Anthropic Messages: ``POST /v1/messages`` (SSE when ``stream`` is true, JSON otherwise).

Responses are scripted. Each incoming completion request pops the next *turn script* from a queue.
A turn script is a list of steps::

    {"text": "hello"}                       # assistant text (streamed in small chunks)
    {"tool": "Write", "args": {...}}        # a tool call (arguments streamed incrementally)
    {"error": 429, "retry_after": 0}        # HTTP error response for this request (whole turn)
    {"delay": 0.2}                          # sleep before continuing the stream
    {"usage": {"input": 10, "output": 5, "cached": 2}}   # override reported usage

A script may also be a dict ``{"steps": [...], "expect": [pred, ...]}`` where every ``pred`` is a
callable ``pred(request: Recorded) -> bool | str`` (``False`` / a string means the expectation
failed; the failure is stored in ``server.failures`` and returned to the client as HTTP 400 so the
test sees it), or a callable ``fn(request) -> script`` for dynamic responses (see
:func:`script_from_rules`). When the queue is empty a default ``{"text": "OK"}`` turn is served.

Every request (method, path, headers, JSON body) is recorded in ``server.requests``.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

Step = dict[str, Any]
Script = Any  # list[Step] | {"steps": [...], "expect": [...]} | Callable[[Recorded], Script]


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    wire: str = ""          # "openai" | "anthropic" | ""
    ts: float = field(default_factory=time.time)

    # ── convenience accessors for assertions ──────────────────────────────
    @property
    def messages(self) -> list[dict[str, Any]]:
        return list((self.body or {}).get("messages") or []) if isinstance(self.body, dict) else []

    @property
    def system_text(self) -> str:
        """System prompt as plain text, for either wire format."""
        if not isinstance(self.body, dict):
            return ""
        parts: list[str] = []
        sys_field = self.body.get("system")
        if isinstance(sys_field, str):
            parts.append(sys_field)
        elif isinstance(sys_field, list):
            parts.extend(b.get("text", "") for b in sys_field if isinstance(b, dict))
        for m in self.messages:
            if m.get("role") in ("system", "developer"):
                parts.append(_content_text(m.get("content")))
        return "\n".join(parts)

    @property
    def tool_names(self) -> list[str]:
        tools = (self.body or {}).get("tools") if isinstance(self.body, dict) else None
        out: list[str] = []
        for t in tools or []:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or (t.get("function") or {}).get("name")
            if name:
                out.append(name)
        return out

    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "user":
                txt = _content_text(m.get("content"), include_tool_results=False)
                if txt:
                    return txt
        return ""

    def tool_results(self) -> list[str]:
        """Contents of every tool result present in the conversation (both wire formats)."""
        out: list[str] = []
        for m in self.messages:
            if m.get("role") == "tool":
                out.append(_content_text(m.get("content")))
            elif m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        out.append(_content_text(b.get("content")))
        return out

    def all_text(self) -> str:
        return json.dumps(self.body, ensure_ascii=False) if self.body is not None else ""


def _content_text(content: Any, include_tool_results: bool = True) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result" and include_tool_results:
                    parts.append(_content_text(b.get("content")))
        return "\n".join(p for p in parts if p)
    return ""


def script_from_rules(fn: Callable[[Recorded], Script]) -> Callable[[Recorded], Script]:
    """Mark ``fn(request) -> script`` as a dynamic turn. It is called with the incoming request
    (use ``request.last_user_text()`` / ``request.tool_results()``) and must return a script."""
    fn.__fake_llm_dynamic__ = True  # type: ignore[attr-defined]
    return fn


def _chunks(text: str, size: int = 7) -> Iterable[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


class FakeLLMServer:
    """Threaded fake provider. Use as a context manager or call start()/stop()."""

    def __init__(self, model_ids: Iterable[str] = ("fake-model",)) -> None:
        self.model_ids = list(model_ids)
        self.requests: list[Recorded] = []
        self.failures: list[str] = []
        self._queue: list[Script] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.default_script: Script = [{"text": "OK"}]

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> "FakeLLMServer":
        server = self

        class Handler(_Handler):
            fake = server

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, name="fake-llm", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server not started"
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """Root URL (no /v1); both wire formats resolve their paths from it."""
        return f"http://127.0.0.1:{self.port}"

    # ── scripting ─────────────────────────────────────────────────────────
    def enqueue(self, *scripts: Script) -> "FakeLLMServer":
        with self._lock:
            self._queue.extend(scripts)
        return self

    def reset(self) -> None:
        with self._lock:
            self._queue.clear()
            self.requests.clear()
            self.failures.clear()

    @property
    def pending(self) -> int:
        return len(self._queue)

    def completion_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.wire in ("openai", "anthropic")]

    def _next_script(self, req: Recorded) -> tuple[list[Step], list[Callable[[Recorded], Any]]]:
        with self._lock:
            script = self._queue.pop(0) if self._queue else self.default_script
        expects: list[Callable[[Recorded], Any]] = []
        for _ in range(5):  # resolve dynamic scripts (may return another dynamic script)
            if callable(script):
                script = script(req)
            else:
                break
        if isinstance(script, dict) and "steps" in script:
            expects = list(script.get("expect") or [])
            script = script["steps"]
        if isinstance(script, dict):
            script = [script]
        if isinstance(script, str):
            script = [{"text": script}]
        return list(script or []), expects

    def _check(self, req: Recorded, expects: list[Callable[[Recorded], Any]]) -> str | None:
        for pred in expects:
            try:
                ok = pred(req)
            except Exception as e:  # pragma: no cover - surfaced to the test
                ok = f"{getattr(pred, '__name__', 'expect')} raised {type(e).__name__}: {e}"
            if ok is True or ok is None:
                continue
            msg = ok if isinstance(ok, str) else f"expectation {getattr(pred, '__name__', repr(pred))} failed"
            self.failures.append(msg)
            return msg
        return None


class _Handler(BaseHTTPRequestHandler):
    fake: FakeLLMServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silence
        pass

    # ── helpers ───────────────────────────────────────────────────────────
    def _read_body(self) -> Any:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _record(self, body: Any, wire: str) -> Recorded:
        rec = Recorded(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body, wire)
        with self.fake._lock:
            self.fake.requests.append(rec)
        return rec

    def _json(self, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _sse_begin(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, data: Any, event: str | None = None) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        msg = (f"event: {event}\n" if event else "") + f"data: {payload}\n\n"
        self.wfile.write(msg.encode("utf-8"))
        self.wfile.flush()

    def _error(self, status: int, step: Step, wire: str) -> None:
        headers = {}
        if "retry_after" in step:
            headers["retry-after"] = str(step["retry_after"])
        message = step.get("message") or {401: "invalid x-api-key", 429: "rate limited", 500: "internal error",
                                           529: "overloaded"}.get(status, "error")
        if wire == "anthropic":
            etype = {401: "authentication_error", 429: "rate_limit_error", 529: "overloaded_error"}.get(status, "api_error")
            body = {"type": "error", "error": {"type": etype, "message": message}}
        else:
            body = {"error": {"message": message, "type": "error", "code": status}}
        self._json(status, body, headers)

    # ── routing ───────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        self._record(None, "")
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "fake", "type": "model", "display_name": m}
                for m in self.fake.model_ids]})
            return
        self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/chat/completions"):
            wire = "openai"
        elif path.endswith("/messages") and not path.endswith("/count_tokens"):
            wire = "anthropic"
        elif path.endswith("/count_tokens"):
            self._record(body, "")
            self._json(200, {"input_tokens": len(json.dumps(body)) // 4})
            return
        else:
            self._record(body, "")
            self._json(404, {"error": {"message": f"no route {self.path}"}})
            return
        req = self._record(body, wire)
        steps, expects = self.fake._next_script(req)
        failure = self.fake._check(req, expects)
        if failure:
            self._json(400, {"error": {"message": f"fake_llm expectation failed: {failure}", "type": "invalid_request_error"}})
            return
        for st in steps:
            if "error" in st:
                self._error(int(st["error"]), st, wire)
                return
        stream = bool(isinstance(body, dict) and body.get("stream"))
        model = (body or {}).get("model", "fake-model") if isinstance(body, dict) else "fake-model"
        try:
            if wire == "openai":
                self._openai(steps, stream, model, body)
            else:
                self._anthropic(steps, stream, model, body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── usage ─────────────────────────────────────────────────────────────
    @staticmethod
    def _usage(steps: list[Step], body: Any) -> tuple[int, int, int]:
        inp = max(1, len(json.dumps(body or {})) // 4)
        out = sum(len(s.get("text", "")) // 4 + 1 for s in steps if "text" in s) + \
            sum(len(json.dumps(s.get("args", {}))) // 4 + 5 for s in steps if "tool" in s)
        cached = 0
        for s in steps:
            if "usage" in s:
                u = s["usage"]
                inp = int(u.get("input", inp))
                out = int(u.get("output", out))
                cached = int(u.get("cached", cached))
        return inp, out, cached

    # ── OpenAI wire ───────────────────────────────────────────────────────
    def _openai(self, steps: list[Step], stream: bool, model: str, body: Any) -> None:
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        inp, out, cached = self._usage(steps, body)
        usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out,
                 "prompt_tokens_details": {"cached_tokens": cached}}
        has_tool = any("tool" in s for s in steps)
        finish = "tool_calls" if has_tool else "stop"

        if not stream:
            text = "".join(s["text"] for s in steps if "text" in s)
            for s in steps:
                if "delay" in s:
                    time.sleep(float(s["delay"]))
            calls = [{"id": s.get("id") or f"call_{uuid.uuid4().hex[:16]}", "type": "function",
                      "function": {"name": s["tool"], "arguments": json.dumps(s.get("args", {}))}}
                     for s in steps if "tool" in s]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            self._json(200, {"id": cid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage})
            return

        self._sse_begin()

        def chunk(delta: dict[str, Any], fr: str | None = None) -> dict[str, Any]:
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}

        self._sse(chunk({"role": "assistant", "content": ""}))
        tool_index = 0
        for s in steps:
            if "delay" in s:
                time.sleep(float(s["delay"]))
            elif "text" in s:
                for piece in _chunks(s["text"]):
                    self._sse(chunk({"content": piece}))
            elif "thinking" in s:
                self._sse(chunk({"reasoning_content": s["thinking"]}))
            elif "tool" in s:
                call_id = s.get("id") or f"call_{uuid.uuid4().hex[:16]}"
                args = json.dumps(s.get("args", {}))
                self._sse(chunk({"tool_calls": [{"index": tool_index, "id": call_id, "type": "function",
                                                 "function": {"name": s["tool"], "arguments": ""}}]}))
                for piece in _chunks(args, 11):
                    self._sse(chunk({"tool_calls": [{"index": tool_index, "function": {"arguments": piece}}]}))
                tool_index += 1
        self._sse(chunk({}, finish))
        self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [], "usage": usage})
        self._sse("[DONE]")

    # ── Anthropic wire ────────────────────────────────────────────────────
    def _anthropic(self, steps: list[Step], stream: bool, model: str, body: Any) -> None:
        mid = f"msg_{uuid.uuid4().hex[:16]}"
        inp, out, cached = self._usage(steps, body)
        has_tool = any("tool" in s for s in steps)
        stop_reason = "tool_use" if has_tool else "end_turn"
        uncached = max(0, inp - cached)

        if not stream:
            content: list[dict[str, Any]] = []
            for s in steps:
                if "delay" in s:
                    time.sleep(float(s["delay"]))
                elif "text" in s:
                    content.append({"type": "text", "text": s["text"]})
                elif "tool" in s:
                    content.append({"type": "tool_use", "id": s.get("id") or f"toolu_{uuid.uuid4().hex[:16]}",
                                    "name": s["tool"], "input": s.get("args", {})})
            self._json(200, {"id": mid, "type": "message", "role": "assistant", "model": model, "content": content,
                             "stop_reason": stop_reason, "stop_sequence": None,
                             "usage": {"input_tokens": uncached, "output_tokens": out,
                                       "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}})
            return

        self._sse_begin()
        self._sse({"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": uncached, "output_tokens": 1, "cache_read_input_tokens": cached,
                      "cache_creation_input_tokens": 0}}}, "message_start")
        idx = 0
        for s in steps:
            if "delay" in s:
                time.sleep(float(s["delay"]))
                continue
            if "text" in s:
                self._sse({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
                          "content_block_start")
                for piece in _chunks(s["text"]):
                    self._sse({"type": "content_block_delta", "index": idx,
                               "delta": {"type": "text_delta", "text": piece}}, "content_block_delta")
            elif "tool" in s:
                tid = s.get("id") or f"toolu_{uuid.uuid4().hex[:16]}"
                self._sse({"type": "content_block_start", "index": idx, "content_block": {
                    "type": "tool_use", "id": tid, "name": s["tool"], "input": {}}}, "content_block_start")
                for piece in _chunks(json.dumps(s.get("args", {})), 11):
                    self._sse({"type": "content_block_delta", "index": idx,
                               "delta": {"type": "input_json_delta", "partial_json": piece}}, "content_block_delta")
            else:
                continue
            self._sse({"type": "content_block_stop", "index": idx}, "content_block_stop")
            idx += 1
        self._sse({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": out}}, "message_delta")
        self._sse({"type": "message_stop"}, "message_stop")
