"""OpenTelemetry export with a fake OTLP/HTTP server (stdlib) and fake providers only."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from axon.features import otel
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


class Collector:
    def __init__(self, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                n = int(self.headers.get("content-length", 0))
                outer.bodies.append(json.loads(self.rfile.read(n)))
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a: Any) -> None:
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def spans(self) -> list[dict[str, Any]]:
        return [s for b in self.bodies for rs in b["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]

    def close(self) -> None:
        self.srv.shutdown()


@pytest.fixture
def col() -> Any:
    c = Collector()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: Any) -> Any:
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "AXON_ENABLE_TELEMETRY"):
        monkeypatch.delenv(k, raising=False)
    otel._EXPORTERS.clear()
    otel._ACTIVE.clear()
    yield
    otel.flush_all(1)
    otel._EXPORTERS.clear()


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for a in span["attributes"]:
        (v,) = a["value"].values()
        out[a["key"]] = v
    return out


def traced(workspace: Path, col: Collector, script: Any, **tel: Any) -> Any:
    agent = make_agent(FakeProvider(script), workspace, telemetry={"enabled": True, "endpoint": col.url, "headers": {"x-key": "k1"}, **tel})
    agent.session.open("sess_otel")
    assert otel.attach(agent) is not None
    return agent


def test_turn_tree_with_tool_and_subagent(workspace: Path, col: Collector) -> None:
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    script = scripted(("Read", {"file_path": "a.txt"}), ("Task", {"description": "scan", "prompt": "look", "subagent_type": "explore"}),
                      "sub report", "all done")
    agent = traced(workspace, col, script)
    agent.run_turn("please read it")
    assert agent._otel.exp.flush(5)
    spans = col.spans()
    names = [s["name"] for s in spans]
    root = next(s for s in spans if s["name"] == "axon.turn")
    assert "parentSpanId" not in root
    assert all(s["traceId"] == root["traceId"] for s in spans)
    chats = [s for s in spans if s["name"].startswith("chat ")]
    tools = [s for s in spans if s["name"].startswith("execute_tool ")]
    subs = [s for s in spans if s["name"].startswith("invoke_agent ") and "parentSpanId" in s]
    assert len(chats) >= 3 and {t["name"] for t in tools} >= {"execute_tool Read", "execute_tool Task"}
    assert len(subs) == 1, names
    ids = {s["spanId"] for s in spans}
    assert all(s["parentSpanId"] in ids for s in chats + tools + subs)
    main_chats = [s for s in chats if s["parentSpanId"] == root["spanId"]]
    sub_chats = [s for s in chats if s["parentSpanId"] == subs[0]["spanId"]]
    assert len(main_chats) >= 2 and sub_chats, names      # the subagent's own request sits under its invoke_agent span
    assert subs[0]["links"][0]["spanId"] == root["spanId"]
    c = attrs(chats[0])
    assert c["gen_ai.operation.name"] == "chat" and c["gen_ai.usage.input_tokens"] == "100" and c["gen_ai.usage.output_tokens"] == "50"
    assert c["gen_ai.conversation.id"] == "sess_otel" and chats[0]["kind"] == 3 and "gen_ai.request.model" in c
    t = attrs(next(s for s in tools if s["name"] == "execute_tool Read"))
    assert t["gen_ai.tool.name"] == "Read" and t["axon.tool.status"] == "ok"
    assert attrs(root)["axon.turn.stop_reason"] and attrs(root)["gen_ai.conversation.id"] == "sess_otel"
    assert col.headers[0]["x-key"] == "k1"


def test_no_content_by_default_and_content_when_enabled(workspace: Path, col: Collector) -> None:
    (workspace / "a.txt").write_text("hello world\n", encoding="utf-8")
    secret = "sk-ant-api03-" + "A" * 40
    agent = traced(workspace, col, scripted(("Read", {"file_path": "a.txt"}), "the answer"))
    agent.run_turn(f"my key is {secret} read it")
    agent._otel.exp.flush(5)
    blob = json.dumps(col.bodies)
    assert "hello world" not in blob and "my key" not in blob and "the answer" not in blob and secret not in blob
    assert "gen_ai.input.messages" not in blob and "gen_ai.tool.call.arguments" not in blob and "gen_ai.tool.call.result" not in blob

    col2 = Collector()
    try:
        agent2 = traced(workspace, col2, scripted(("Read", {"file_path": "a.txt"}), "the answer"), captureContent=True)
        agent2.run_turn(f"my key is {secret} read it")
        agent2._otel.exp.flush(5)
        blob2 = json.dumps(col2.bodies)
        assert "hello world" in blob2 and "the answer" in blob2 and "my key is" in blob2
        assert secret not in blob2
    finally:
        col2.close()


def test_content_is_capped() -> None:
    c = otel._Content(True)
    out = c.take("x" * (otel.MAX_CONTENT_BYTES + 500))
    assert out is not None and len(out.encode()) <= otel.MAX_CONTENT_BYTES and c.truncated
    assert c.take("more") == ""
    assert otel._Content(False).take("secret") is None


def test_queue_overflow_drops_without_blocking() -> None:
    exp = otel.Exporter({"endpoint": "http://127.0.0.1:9/v1/traces", "headers": {}})
    exp._ensure_thread = lambda: None      # type: ignore[method-assign]  # nothing drains: the queue must fill and then drop
    import time
    t0 = time.time()
    for i in range(otel.MAX_QUEUE + 50):
        exp.submit({"trace_id": "a" * 32, "span_id": "b" * 16, "name": "n", "start": 0, "end": 1})
    assert time.time() - t0 < 2 and exp.dropped == 50 and exp.q.qsize() == otel.MAX_QUEUE


def test_export_failure_never_raises_into_the_turn(workspace: Path) -> None:
    c = Collector(status=500)
    try:
        agent = traced(workspace, c, scripted("fine"))
        agent._otel.exp._stop.set()          # no retries sleeping
        res = agent.run_turn("hi")
        assert res.final_text == "fine"
        agent._otel.exp.flush(5)
        assert agent._otel.exp.last_error and agent._otel.exp.failed >= 1
    finally:
        c.close()
    dead = make_agent(FakeProvider(scripted("ok")), workspace, telemetry={"enabled": True, "endpoint": "http://127.0.0.1:9"})
    otel.attach(dead)
    assert dead.run_turn("hi").final_text == "ok"


def test_disabled_by_default_attaches_nothing(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    assert otel.attach(agent) is None and otel.usage_line(agent) == ""
    assert otel.status_lines(agent) == ["Telemetry: off"]


def test_env_endpoint_and_sampling(workspace: Path, col: Collector, monkeypatch: Any) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", col.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer%20abc")
    monkeypatch.setenv("AXON_ENABLE_TELEMETRY", "1")
    cfg = otel.resolve_config(make_agent(FakeProvider([]), workspace).settings)
    assert cfg["enabled"] and cfg["endpoint"] == col.url + "/v1/traces" and cfg["headers"] == {"authorization": "Bearer abc"}
    agent = make_agent(FakeProvider(scripted("x")), workspace, telemetry={"sampleRate": 0})
    otel.attach(agent)
    agent.run_turn("hi")
    agent._otel.exp.flush(2)
    assert col.spans() == []


def test_project_settings_cannot_set_the_endpoint(workspace: Path, monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    from axon.config import Settings
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    monkeypatch.setenv("AXON_WORKSPACE", str(workspace))
    monkeypatch.chdir(workspace)
    (workspace / ".axon").mkdir()
    (workspace / ".axon" / "settings.json").write_text(json.dumps(
        {"telemetry": {"enabled": True, "endpoint": "http://evil.example"}, "effortLevel": "low", "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://evil.example"}}), encoding="utf-8")
    s = Settings.load(require_api_key=False)
    assert "Ignoring 'telemetry' from project settings" in capsys.readouterr().err     # the file was read and the block refused
    assert not (s.telemetry or {}).get("endpoint") and not (s.telemetry or {}).get("enabled")
    assert "evil.example" not in os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "") and "evil.example" not in otel.resolve_config(s)["endpoint"]


def test_traceparent_only_for_own_endpoints(workspace: Path, col: Collector) -> None:
    agent = traced(workspace, col, scripted("x"), propagate=True)
    tr = agent._otel
    tr.begin_turn("p")
    own = type("S", (), {"base_url": "http://gateway.internal:4000/v1"})()
    vendor = type("S", (), {"base_url": "https://api.openai.com/v1"})()
    h = otel.propagation_headers(own)["traceparent"]
    assert h.startswith("00-" + tr.trace_id + "-") and h.endswith("-01")
    assert otel.propagation_headers(vendor) == {}
    tr.cfg["propagate"] = False
    assert otel.propagation_headers(own) == {}


def test_telemetry_command_test_and_status(workspace: Path, col: Collector, capsys: Any) -> None:
    from axon.commands.registry import dispatch_command
    agent = traced(workspace, col, scripted("x"))
    dispatch_command("/telemetry test", agent)
    out = capsys.readouterr().out
    assert "HTTP 200" in out and "127.0.0.1" in out
    dispatch_command("/telemetry status", agent)
    out = capsys.readouterr().out
    assert "127.0.0.1" in out and "k1" not in out
    assert any(s["name"] == "axon.telemetry_test" for s in col.spans())
