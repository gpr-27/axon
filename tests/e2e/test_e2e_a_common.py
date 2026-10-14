"""
Shared helpers for the ``test_e2e_a_*`` end-to-end modules (no tests live here).

* :class:`ExtFakeLLM` - the harness fake LLM plus two extra OpenAI-wire steps:
  ``{"raw_tool": name, "raw_args": "<not json>"}`` streams arguments verbatim (malformed JSON) and
  ``{"finish": "length"}`` overrides the finish reason (max_tokens mid tool call).
* :func:`local_http` - a throwaway local HTTP server (WebFetch target / http hook receiver).
* :func:`run_tools` - script a sequence of tool rounds + a final answer, run axon in stream-json mode
  and return ``(result, events)``.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest

from fake_llm import FakeLLMServer, Recorded, _Handler, _chunks
from harness import AxonEnv, Result, create_axon_env

PY = sys.executable
TRACEBACK = "Traceback (most recent call last)"


# ─── extended fake LLM ──────────────────────────────────────────────────────

class _ExtHandler(_Handler):
    def _openai(self, steps, stream, model, body):  # type: ignore[override]
        if not any("raw_tool" in s or "finish" in s for s in steps) or not stream:
            return super()._openai(steps, stream, model, body)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        inp, out, cached = self._usage(steps, body)
        has_tool = any("tool" in s or "raw_tool" in s for s in steps)
        finish = next((s["finish"] for s in steps if "finish" in s), "tool_calls" if has_tool else "stop")
        self._sse_begin()

        def chunk(delta, fr=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}

        self._sse(chunk({"role": "assistant", "content": ""}))
        idx = 0
        for s in steps:
            if "text" in s:
                for piece in _chunks(s["text"]):
                    self._sse(chunk({"content": piece}))
            elif "tool" in s or "raw_tool" in s:
                name = s.get("tool") or s.get("raw_tool")
                args = s["raw_args"] if "raw_args" in s else json.dumps(s.get("args", {}))
                self._sse(chunk({"tool_calls": [{"index": idx, "id": f"call_{uuid.uuid4().hex[:16]}", "type": "function",
                                                 "function": {"name": name, "arguments": ""}}]}))
                for piece in _chunks(args, 11):
                    self._sse(chunk({"tool_calls": [{"index": idx, "function": {"arguments": piece}}]}))
                idx += 1
        self._sse(chunk({}, finish))
        self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [],
                   "usage": {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}})
        self._sse("[DONE]")


class ExtFakeLLM(FakeLLMServer):
    def start(self) -> "ExtFakeLLM":
        server = self

        class Handler(_ExtHandler):
            fake = server

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self


@pytest.fixture
def ext_env(tmp_path: Path):
    server = ExtFakeLLM(model_ids=("gpt-4o", "gpt-4o-mini", "claude-sonnet-4-6")).start()
    try:
        yield create_axon_env(tmp_path, server)
    finally:
        server.stop()


# ─── local http server ──────────────────────────────────────────────────────

class LocalHTTP:
    def __init__(self, handler: Callable[[BaseHTTPRequestHandler, str, bytes], tuple[int, str, str | bytes]]):
        self.requests: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, ctype, payload = handler(self, self.command, body)
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _serve
            do_POST = _serve

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@contextlib.contextmanager
def local_http(handler):
    srv = LocalHTTP(handler)
    try:
        yield srv
    finally:
        srv.stop()


# ─── running scripted tool rounds ──────────────────────────────────────────

Round = Any  # (tool, args) | [(tool, args), ...]


def enqueue_rounds(env: AxonEnv, rounds: Iterable[Round], final: str = "FINAL-ANSWER") -> None:
    for rnd in rounds:
        if isinstance(rnd, tuple):
            rnd = [rnd]
        env.llm.enqueue([{"tool": t, "args": a} for t, a in rnd])
    env.llm.enqueue([{"text": final}])


def run_tools(env: AxonEnv, rounds: Iterable[Round], *extra: str, final: str = "FINAL-ANSWER",
              prompt: str = "go", timeout: float = 60, run_env: dict[str, str] | None = None) -> tuple[Result, list[dict]]:
    enqueue_rounds(env, rounds, final)
    r = env.run(["-p", "--output-format", "stream-json", *extra, prompt], timeout=timeout, env=run_env)
    return r, r.json_lines()


def results(events: list[dict], name: str | None = None) -> list[dict]:
    return [e for e in events if e.get("type") == "tool_result" and (name is None or e.get("name") == name)]


def perms(events: list[dict], tool: str | None = None) -> list[dict]:
    return [e for e in events if e.get("type") == "permission" and (tool is None or e.get("tool") == tool)]


def final(events: list[dict]) -> dict:
    res = [e for e in events if e.get("type") == "result"]
    assert res, f"no result event in {[e.get('type') for e in events]}"
    return res[-1]


def no_traceback(r: Result) -> None:
    assert TRACEBACK not in r.stdout and TRACEBACK not in r.stderr, str(r)


def write_settings(env: AxonEnv, data: dict, where: str = ".axon/settings.json") -> Path:
    p = env.workspace / where
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def py_hook(env: AxonEnv, name: str, body: str, *args: str) -> dict:
    """Write a python hook script under <root>/hooks and return its settings.json hook entry."""
    d = env.root / "hooks"
    d.mkdir(exist_ok=True)
    script = d / name
    script.write_text("import sys, json, os\n" + body, encoding="utf-8")
    quoted = " ".join(f'"{a}"' for a in (PY, str(script), *args))
    return {"type": "command", "command": quoted, "timeout": 30}


MCP_SERVER_SOURCE = r'''
import json, sys
TOOLS = [{"name": "shout", "description": "Upper-case the given text (e2e echo tool)",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
          "annotations": {"readOnlyHint": True}}]
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rid is None:
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
              "capabilities": {"tools": {}}, "serverInfo": {"name": "e2e", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        text = str((params.get("arguments") or {}).get("text", ""))
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "SHOUT:" + text.upper()}]}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''


def mcp_config(env: AxonEnv) -> str:
    """Write the tiny stdio MCP server and return an inline --mcp-config JSON for it."""
    src = env.root / "mcp_server.py"
    src.write_text(MCP_SERVER_SOURCE, encoding="utf-8")
    return json.dumps({"mcpServers": {"e2e": {"command": PY, "args": [str(src)]}}})
