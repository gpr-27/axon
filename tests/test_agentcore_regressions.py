"""Regression tests found by the agent-core end-to-end round."""
from __future__ import annotations

import pytest

from axon.tools import _http


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8080/x", True),
    ("http://localhost:3000/", True),
    ("http://[::1]:80/", True),
    ("http://127.1.2.3/", True),
    ("http://127.evil.example/", False),     # a host NAME that merely starts with "127."
    ("http://127.0.0.1.nip.io/", False),
    ("http://localhost.evil.example/", False),
    ("http://10.0.0.1/", False),
])
def test_is_loopback_only_for_literal_loopback_addresses(url: str, expected: bool) -> None:
    assert _http._is_loopback(url) is expected


def test_offline_mode_blocks_a_name_that_starts_with_127(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.errors import ToolError
    monkeypatch.setenv("AXON_OFFLINE", "1")
    monkeypatch.setattr(_http, "_TRANSPORT", None)
    with pytest.raises(ToolError, match="offline"):
        _http.ensure_online("WebFetch", "http://127.evil.example/")
    _http.ensure_online("WebFetch", "http://127.0.0.1:9/")   # loopback stays reachable


# ── a stream that ends without the provider's own end marker is an error, not an answer ──

def _cut_stream_server(wire: str):
    """A local provider that streams two text chunks, then closes the connection with no end marker."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits: list[int] = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("content-length") or 0)
            self.rfile.read(n)
            hits.append(1)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True

            def sse(obj, event=None):
                msg = (f"event: {event}\n" if event else "") + "data: " + json.dumps(obj) + "\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()

            if wire == "openai":
                for piece in ("Half of ", "an answer"):
                    sse({"id": "x", "object": "chat.completion.chunk", "model": "m",
                         "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            else:
                sse({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant", "model": "m",
                     "content": [], "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}}}, "message_start")
                sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")
                for piece in ("Half of ", "an answer"):
                    sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}, "content_block_delta")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, hits


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_cut_stream_is_reported_not_returned_as_the_final_answer(tmp_path, wire: str) -> None:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    srv, hits = _cut_stream_server(wire)
    try:
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        home.mkdir()
        ws.mkdir()
        repo = Path(__file__).resolve().parents[1]
        # Windows needs SYSTEMROOT (sockets, DLLs), COMSPEC and PATHEXT (the shell) or Python cannot start or connect.
        env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
               if k in os.environ}
        env.update({"HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home), "AXON_HOME": str(home / ".axon"), "AXON_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
                    "AXON_API_KEY": "sk-test", "AXON_MODEL": "claude-sonnet-4-6" if wire == "anthropic" else "gpt-4o",
                    "AXON_OFFLINE": "1", "AXON_NO_UPDATE_CHECK": "1", "AXON_TITLE_LLM": "0", "AXON_TIPS_LLM": "0",
                    "AXON_PROMPT_SUGGESTIONS": "0", "AXON_NO_NOTIFY": "1", "PYTHONPATH": str(repo / "src"), "NO_COLOR": "1"})
        if wire == "anthropic":
            env["AXON_PROVIDER"] = "anthropic"
        proc = subprocess.run([sys.executable, "-m", "axon", "-p", "--output-format", "stream-json", "hi"], cwd=ws, env=env,
                              capture_output=True, text=True, timeout=120)
        events = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
        result = [e for e in events if e.get("type") == "result"][-1]
        assert result["is_error"] is True and result["stop_reason"] == "error"
        assert "ended before the model finished" in result["result"]
        assert proc.returncode != 0
    finally:
        srv.shutdown()


def test_session_in_use_warning_reaches_stderr_for_scripts(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import SimpleNamespace

    from axon import cli

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    agent = SimpleNamespace(session=SimpleNamespace(conflict={"owner": "terminal", "pid": 4242}))
    muted: list[str] = []
    assert cli._session_in_use(agent, muted.append) is False
    assert muted == []                                   # the json formats mute `say`; the warning must not depend on it
    assert "also open in the terminal (process 4242)" in capsys.readouterr().err


def test_resume_does_not_claim_an_unsaved_tool_call_never_ran() -> None:
    """A session killed mid-batch has the first call's effect on disk but no saved result for it."""
    from axon.agent.state import Conversation

    for assistant in (
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "Write", "input": {}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a1", "type": "function", "function": {"name": "Write", "arguments": "{}"}}]},
    ):
        conv = Conversation([{"role": "user", "content": "hi"}, assistant])
        assert conv.repair() == 1
        text = str(conv.messages[-1]["content"])
        assert "did not run" not in text
        assert "may or may not have run" in text


def _tiny_png(path) -> None:
    import zlib

    def chunk(tag: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + tag + body + zlib.crc32(tag + body).to_bytes(4, "big")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00")
                     + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


def test_a_picture_named_twice_is_attached_once(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`@shot.png` expands to a file block that repeats the name; the model must get one image, not two."""
    from axon.agent.images import compact_image_paths

    _tiny_png(tmp_path / "shot.png")
    monkeypatch.chdir(tmp_path)
    text, atts = compact_image_paths("describe @shot.png\n\n[File: shot.png]\n```\n[Image: shot.png, PNG, 1x1]\n```")
    assert len(atts) == 1
    assert "shot.png" not in text.replace("@shot.png", "")


def test_image_path_with_directory_prefix_is_replaced_whole(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.agent.images import compact_image_paths

    _tiny_png(tmp_path / "shot.png")
    monkeypatch.chdir(tmp_path)
    for named in ("./shot.png", str(tmp_path / "shot.png")):
        text, atts = compact_image_paths(f"look at {named} now")
        assert len(atts) == 1
        assert text == f"look at {atts[0].label} now", text
