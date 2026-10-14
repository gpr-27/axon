"""Tests for axon.features.server (`axon serve`): auth, SSE framing, JSON mode, data endpoints."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from axon.features import server as srv
from axon.features.sdk import AxonOptions
from fakes import FakeProvider, scripted


@pytest.fixture(autouse=True)
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon_home"
    monkeypatch.setenv("AXON_HOME", str(home))
    return home


class _Provider:
    """Hands each query a fresh scripted FakeProvider (options are copied per request)."""

    def __init__(self) -> None:
        self.script: list[Any] = []


@pytest.fixture
def live(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    holder = _Provider()
    import axon.features.sdk as sdk

    real_build = sdk.build_agent

    def build(options: AxonOptions | None = None, *, on_event=None):
        options.provider_instance = FakeProvider(list(holder.script))
        return real_build(options, on_event=on_event)

    monkeypatch.setattr(sdk, "build_agent", build)
    server = srv.make_server("127.0.0.1", 0, "s3cret", AxonOptions(workspace=workspace, model="claude-sonnet-4-5"))
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield server, holder
    server.shutdown()
    server.server_close()


def _req(server: srv.AxonServer, path: str, body: dict | None = None, token: str | None = "s3cret",
         headers: dict | None = None) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(server.url + path, data=data, method="POST" if data else "GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    out = []
    for frame in raw.decode("utf-8").split("\n\n"):
        if not frame.strip():
            continue
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_auth_required(live) -> None:
    server, _ = live
    for token in (None, "wrong"):
        status, headers, body = _req(server, "/health", token=token)
        assert status == 401 and "Bearer" in headers.get("WWW-Authenticate", "")
        assert json.loads(body)["error"]["status"] == 401
    status, _, body = _req(server, "/health")
    assert status == 200 and json.loads(body)["status"] == "ok"
    status, _, _ = _req(server, "/v1/query", {"prompt": "x"}, token="wrong")
    assert status == 401


def test_dns_rebinding_host_rejected(live) -> None:
    server, _ = live
    status, _, _ = _req(server, "/health", headers={"Host": "evil.example.com"})
    assert status == 403


def test_query_sse_stream(live) -> None:
    server, holder = live
    holder.script = scripted("Hello from the server")
    status, headers, raw = _req(server, "/v1/query", {"prompt": "hi"})
    assert status == 200 and headers["Content-Type"].startswith("text/event-stream")
    events = _parse_sse(raw)
    names = [n for n, _ in events]
    assert names[0] == "system/init" and names[-1] == "result"
    assert "llm_call_start" in names and "llm_call_end" in names and "text_delta" in names
    assert all(n == d["type"] or n == "system/init" for n, d in events)
    assert events[-1][1]["result"] == "Hello from the server"
    assert raw.decode("utf-8").startswith("id: 0\nevent: system/init\ndata: {")


def test_query_json_mode_and_usage(live) -> None:
    server, holder = live
    holder.script = scripted("json answer")
    status, headers, raw = _req(server, "/v1/query", {"prompt": "hi", "stream": False, "include_events": True,
                                                      "options": {"maxTurns": 3}})
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    payload = json.loads(raw)
    assert payload["result"]["result"] == "json answer" and payload["result"]["type"] == "result"
    assert payload["events"][0]["subtype"] == "init" and payload["events"][0]["max_turns"] == 3
    sid = payload["result"]["session_id"]

    status, _, raw = _req(server, "/v1/usage?days=1")
    usage = json.loads(raw)
    assert status == 200 and usage["totals"]["calls"] >= 1 and usage["totals"]["input_tokens"] >= 100
    assert "claude-sonnet-4-5" in usage["by_model"]

    status, _, raw = _req(server, "/v1/sessions")
    sessions = json.loads(raw)["sessions"]
    assert status == 200 and any(s["session_id"] == sid for s in sessions)


def test_bad_requests(live) -> None:
    server, _ = live
    assert _req(server, "/v1/query", {"prompt": ""})[0] == 400
    assert _req(server, "/v1/query", {"prompt": "x", "options": {"provider_instance": 1}})[0] == 400
    assert _req(server, "/v1/query", {"prompt": "x", "options": {"mode": "yolo"}})[0] == 400
    assert _req(server, "/nope")[0] == 404
    assert _req(server, "/v1/nope", {"prompt": "x"})[0] == 404


def test_refuses_non_loopback_without_flag() -> None:
    assert srv.is_loopback("127.0.0.1") and srv.is_loopback("::1") and srv.is_loopback("localhost")
    assert not srv.is_loopback("0.0.0.0") and not srv.is_loopback("192.168.1.10")
    with pytest.raises(ValueError):
        srv.make_server("0.0.0.0", 0, "t")
    assert srv.serve_main(["--host", "0.0.0.0", "--port", "0"]) == 2


def test_sse_frame_and_feature_registration() -> None:
    frame = srv.sse_frame({"type": "text_delta", "text": "a\nb"}, 3)
    assert frame == 'id: 3\nevent: text_delta\ndata: {"type": "text_delta", "text": "a\\nb"}\n\n'
    assert srv.CLI_SUBCOMMANDS["serve"] is srv.serve_main
    assert srv.COMMANDS and srv.COMMANDS[0].name == "/serve"


def test_serve_slash_command_prints(capsys: pytest.CaptureFixture[str], workspace: Path) -> None:
    class A:
        class settings:
            pass
    A.settings.workspace = workspace
    srv.COMMANDS[0].handler(A(), "9000")
    out = capsys.readouterr().out
    assert "axon serve --port 9000" in out and "/v1/query" in out
