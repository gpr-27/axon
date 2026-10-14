"""
Shared helpers for the ``test_e2e_c_*`` suites (no tests in this module).

``ExtFakeLLM`` extends :class:`fake_llm.FakeLLMServer` with wire details the base fake does not
emit, so provider parsing is exercised against byte-level SSE streams:

* ``{"thinking": "...", "signature": "sig"}``  Anthropic thinking block (thinking_delta + signature_delta)
* ``{"usage": {"input": N, "output": M, "cached": R, "cache_write": W}}``  Anthropic usage incl.
  ``cache_creation_input_tokens`` (``input`` is the TOTAL prompt; uncached = input - cached - write)
* ``{"finish": "stop"}``  override the OpenAI ``finish_reason`` (e.g. "stop" although tools were called)
* ``{"split_tool_header": True}``  OpenAI: send a tool call's id, name and arguments in separate chunks
* ``{"raw_openai": [delta, ...]}``  OpenAI: stream these ``delta`` dicts verbatim (one chunk each)
* ``{"raw_chunks": [chunk, ...]}``  OpenAI: stream these whole chunk objects verbatim
* ``{"usage_raw": {...}}``  OpenAI: send this dict as the final chunk's ``usage`` (e.g. DeepSeek cache fields)

Everything else is delegated to the base fake.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fake_llm import FakeLLMServer, Recorded, _chunks, _Handler
from harness import SRC, AxonEnv, create_axon_env

_EXT_KEYS = ("thinking", "finish", "split_tool_header", "raw_openai", "raw_chunks", "usage_raw")


def _has_ext(steps: list[dict[str, Any]], wire: str) -> bool:
    for s in steps:
        if any(k in s for k in _EXT_KEYS):
            return True
        if wire == "anthropic" and "usage" in s and "cache_write" in s["usage"]:
            return True
    return False


class _ExtHandler(_Handler):
    def _openai(self, steps, stream, model, body):  # type: ignore[override]
        if not stream or not _has_ext(steps, "openai"):
            return super()._openai(steps, stream, model, body)
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        inp, out, cached = self._usage(steps, body)
        usage = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out,
                 "prompt_tokens_details": {"cached_tokens": cached}}
        has_tool = any("tool" in s for s in steps) or any("raw_openai" in s and any("tool_calls" in d for d in s["raw_openai"]) for s in steps)
        finish = "tool_calls" if has_tool else "stop"
        split = any(s.get("split_tool_header") for s in steps)
        for s in steps:
            if "finish" in s:
                finish = s["finish"]
            if "usage_raw" in s:
                usage = s["usage_raw"]
        self._sse_begin()

        def chunk(delta, fr=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}

        self._sse(chunk({"role": "assistant", "content": ""}))
        idx = 0
        for s in steps:
            if "delay" in s:
                time.sleep(float(s["delay"]))
            elif "raw_openai" in s:
                for d in s["raw_openai"]:
                    self._sse(chunk(d))
            elif "raw_chunks" in s:
                for c in s["raw_chunks"]:
                    self._sse(c)
                if s.get("stop_after"):
                    return
            elif "text" in s:
                for piece in _chunks(s["text"]):
                    self._sse(chunk({"content": piece}))
            elif "thinking" in s:
                for piece in _chunks(s["thinking"], 5):
                    self._sse(chunk({"reasoning_content": piece}))
            elif "tool" in s:
                call_id = s.get("id") or f"call_{uuid.uuid4().hex[:16]}"
                args = json.dumps(s.get("args", {}))
                if split:
                    self._sse(chunk({"tool_calls": [{"index": idx, "id": call_id, "type": "function"}]}))
                    self._sse(chunk({"tool_calls": [{"index": idx, "function": {"name": s["tool"]}}]}))
                else:
                    self._sse(chunk({"tool_calls": [{"index": idx, "id": call_id, "type": "function",
                                                     "function": {"name": s["tool"], "arguments": ""}}]}))
                for piece in _chunks(args, 3 if split else 11):
                    self._sse(chunk({"tool_calls": [{"index": idx, "function": {"arguments": piece}}]}))
                idx += 1
        self._sse(chunk({}, finish))
        self._sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [], "usage": usage})
        self._sse("[DONE]")

    def _anthropic(self, steps, stream, model, body):  # type: ignore[override]
        if not stream or not _has_ext(steps, "anthropic"):
            return super()._anthropic(steps, stream, model, body)
        mid = f"msg_{uuid.uuid4().hex[:16]}"
        inp, out, cached = self._usage(steps, body)
        write = 0
        for s in steps:
            if "usage" in s:
                write = int(s["usage"].get("cache_write", write))
        uncached = max(0, inp - cached - write)
        has_tool = any("tool" in s for s in steps)
        stop_reason = "tool_use" if has_tool else "end_turn"
        self._sse_begin()
        self._sse({"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": uncached, "output_tokens": 1, "cache_read_input_tokens": cached,
                      "cache_creation_input_tokens": write}}}, "message_start")
        idx = 0
        for s in steps:
            if "delay" in s:
                time.sleep(float(s["delay"]))
                continue
            if "thinking" in s:
                self._sse({"type": "content_block_start", "index": idx,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}}, "content_block_start")
                for piece in _chunks(s["thinking"], 6):
                    self._sse({"type": "content_block_delta", "index": idx,
                               "delta": {"type": "thinking_delta", "thinking": piece}}, "content_block_delta")
                self._sse({"type": "content_block_delta", "index": idx,
                           "delta": {"type": "signature_delta", "signature": s.get("signature", "sig-e2e")}},
                          "content_block_delta")
            elif "text" in s:
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


class ExtFakeLLM(FakeLLMServer):
    def start(self) -> "ExtFakeLLM":
        import threading
        from http.server import ThreadingHTTPServer

        server = self

        class Handler(_ExtHandler):
            fake = server

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="ext-fake-llm", daemon=True)
        self._thread.start()
        return self


@pytest.fixture
def ext_llm():
    server = ExtFakeLLM(model_ids=("gpt-4o", "claude-sonnet-4-6", "fake-model")).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def xenv(tmp_path: Path, ext_llm: ExtFakeLLM) -> AxonEnv:
    """AxonEnv wired to the extended fake (plain workspace)."""
    return create_axon_env(tmp_path, ext_llm)


@pytest.fixture
def xgit_env(tmp_path: Path, ext_llm: ExtFakeLLM) -> AxonEnv:
    """AxonEnv wired to the extended fake with ``git init`` in the workspace."""
    return create_axon_env(tmp_path, ext_llm, git=True)


# ─── small assertion helpers ────────────────────────────────────────────────
TRACEBACK = "Traceback (most recent call last)"


def no_traceback(r: Any) -> None:
    assert TRACEBACK not in r.stdout and TRACEBACK not in r.stderr, str(r)


def has_tool_result(substr: str = ""):
    def pred(req: Recorded):
        res = req.tool_results()
        if not res:
            return "no tool result in request"
        if substr and not any(substr in t for t in res):
            return f"no tool result containing {substr!r}: {res!r}"
        return True
    pred.__name__ = f"has_tool_result({substr!r})"
    return pred


def usage_records(env: AxonEnv) -> list[dict[str, Any]]:
    p = env.axon_home / "usage.jsonl"
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def money(rate: float, tokens: int) -> Decimal:
    """tokens * $rate / 1M as an exact Decimal."""
    return Decimal(str(rate)) * Decimal(tokens) / Decimal(1_000_000)


def env_without_keys(env: AxonEnv, **extra: str) -> dict[str, str]:
    """A copy of env.env with every *_API_KEY removed (AXON_API_KEY too) plus `extra`."""
    out = {k: v for k, v in env.env.items() if not k.endswith("_API_KEY")}
    out.update(extra)
    return out


def run_raw(env_vars: dict[str, str], args: list[str], cwd: Path, timeout: float = 60, input: str = "") -> Any:
    """Run `python -m axon` with exactly `env_vars` (no merging with AxonEnv.env)."""
    from harness import run_axon
    return run_axon(args, input=input, timeout=timeout, env=env_vars, cwd=cwd)


def python_snippet(env: AxonEnv, code: str, timeout: float = 60, extra_env: dict[str, str] | None = None,
                   cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a python -c snippet in a subprocess with the e2e environment (PYTHONPATH=src)."""
    e = {**env.env, **(extra_env or {})}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=e, cwd=str(cwd or env.workspace))


def venv_path_env(env: AxonEnv) -> dict[str, str]:
    """PATH with this interpreter's bin dir first (so `python3 -m pytest` resolves to a python with pytest)."""
    bindir = str(Path(sys.executable).parent)
    return {"PATH": bindir + os.pathsep + env.env.get("PATH", "")}


def no_gh_path(tmp_path: Path) -> str:
    """A PATH that has git (and the shell basics) but no `gh`."""
    b = tmp_path / "nogh-bin"
    b.mkdir(exist_ok=True)
    git = shutil.which("git")
    if git:
        link = b / "git"
        if not link.exists():
            link.symlink_to(git)
    return os.pathsep.join([str(b), str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


__all__ = ["ExtFakeLLM", "ext_llm", "xenv", "xgit_env", "no_traceback", "has_tool_result", "usage_records",
           "money", "env_without_keys", "run_raw", "python_snippet", "venv_path_env", "no_gh_path", "SRC"]
