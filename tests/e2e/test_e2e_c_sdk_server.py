"""
E2E-C: the Python SDK (`axon.features.sdk.query/run`) in a separate interpreter and `axon serve`
(real HTTP server on a random port): auth, /health, SSE /v1/query parsed end to end, /v1/usage.
"""
from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from harness import AxonEnv, create_axon_env
from test_e2e_c_support import ExtFakeLLM, ext_llm, money, python_snippet, usage_records, xenv  # noqa: F401

pytestmark = pytest.mark.e2e

from axon.providers.pricing import PROVIDER_PRICING  # noqa: E402

GPT4O = PROVIDER_PRICING["openai"]["gpt-4o"]


def _cost(total_in: int, out: int, cached: int = 0) -> Decimal:
    return money(GPT4O["input"], total_in - cached) + money(GPT4O["cache_read"], cached) + money(GPT4O["output"], out)


# ─── SDK ────────────────────────────────────────────────────────────────────

SDK_QUERY = r"""
import json, os, sys
from axon.features.sdk import AxonOptions, query
opts = AxonOptions(workspace=os.getcwd(), mode=os.environ.get("E2E_MODE", "bypass"))
print(json.dumps(list(query(sys.argv[1], opts)), default=str))
"""


def _sdk_events(env: AxonEnv, prompt: str, mode: str = "bypass") -> list[dict[str, Any]]:
    p = subprocess.run([sys.executable, "-c", SDK_QUERY, prompt], capture_output=True, text=True, encoding="utf-8",
                       env={**env.env, "E2E_MODE": mode}, cwd=str(env.workspace), timeout=60)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_sdk_query_event_order_and_result_cost(xenv: AxonEnv) -> None:
    """query(): system/init first, llm_call_start < text_delta < llm_call_end < result; cost = hand-computed."""
    xenv.llm.enqueue([{"text": "sdk says hello"}, {"usage": {"input": 100_000, "output": 10_000, "cached": 20_000}}])
    ev = _sdk_events(xenv, "hello sdk")
    types = [e["type"] for e in ev]
    assert ev[0]["type"] == "system" and ev[0].get("subtype") == "init", ev[0]
    assert types[-1] == "result"
    i_start, i_text, i_end = types.index("llm_call_start"), types.index("text_delta"), types.index("llm_call_end")
    assert i_start < i_text < i_end < len(types) - 1, types
    assert "".join(e["text"] for e in ev if e["type"] == "text_delta") == "sdk says hello"
    res = ev[-1]
    assert res["result"] == "sdk says hello" and res["is_error"] is False
    expected = _cost(100_000, 10_000, 20_000)
    assert abs(Decimal(str(res["cost_usd"])) - expected) < Decimal("1e-6"), (res["cost_usd"], expected)
    assert res["usage"]["cache_read_tokens"] == 20_000 and res["usage"]["input_tokens"] == 100_000, res["usage"]
    end = ev[i_end]
    assert end["usage"]["input_tokens"] == 100_000 and end["usage"]["output_tokens"] == 10_000, end


def test_sdk_tool_events_and_workspace_effect(xenv: AxonEnv) -> None:
    """A tool turn via the SDK: tool_use precedes tool_result, the file exists, tool_calls == 1."""
    xenv.llm.enqueue([{"tool": "Write", "args": {"path": "sdk.txt", "content": "via sdk"}}], [{"text": "wrote"}])
    ev = _sdk_events(xenv, "write sdk.txt")
    types = [e["type"] for e in ev]
    assert types.index("tool_use") < types.index("tool_result") < types.index("result")
    tu = next(e for e in ev if e["type"] == "tool_use")
    assert tu["name"] == "Write" and tu["input"]["path"] == "sdk.txt"
    assert (xenv.workspace / "sdk.txt").read_text(encoding="utf-8") == "via sdk"
    assert ev[-1]["tool_calls"] == 1 and ev[-1]["iterations"] == 2


def test_sdk_default_mode_denies_writes_without_can_use_tool(xenv: AxonEnv) -> None:
    """mode=default with no can_use_tool: an 'ask' tool (Write) is denied, nothing is written, model is told."""
    xenv.llm.enqueue([{"tool": "Write", "args": {"path": "denied.txt", "content": "x"}}], [{"text": "ok, denied"}])
    ev = _sdk_events(xenv, "write denied.txt", mode="default")
    assert not (xenv.workspace / "denied.txt").exists()
    tr = next(e for e in ev if e["type"] == "tool_result")
    assert tr["is_error"] is True
    assert ev[-1]["result"] == "ok, denied"


def test_sdk_can_use_tool_callback_allows(xenv: AxonEnv) -> None:
    """can_use_tool returning True approves the Write in default mode."""
    code = (
        "import json, os\n"
        "from axon.features.sdk import AxonOptions, run\n"
        "seen=[]\n"
        "def allow(tool, args):\n"
        "    seen.append(tool); return True\n"
        "r = run('write ok.txt', AxonOptions(workspace=os.getcwd(), mode='default', can_use_tool=allow))\n"
        "print(json.dumps({'text': r.text, 'seen': seen, 'cost': r.cost_usd, 'sid': r.session_id}))\n"
    )
    xenv.llm.enqueue([{"tool": "Write", "args": {"path": "ok.txt", "content": "allowed"}}], [{"text": "allowed it"}])
    p = python_snippet(xenv, code)
    assert p.returncode == 0, p.stderr[-2000:]
    data = json.loads(p.stdout.strip().splitlines()[-1])
    assert data["seen"] == ["Write"] and data["text"] == "allowed it" and data["sid"]
    assert (xenv.workspace / "ok.txt").read_text(encoding="utf-8") == "allowed"


def test_sdk_session_keeps_context_across_queries(xenv: AxonEnv) -> None:
    """AxonSession: the second query's request contains the first exchange; usage is logged per call."""
    code = (
        "import json, os\n"
        "from axon.features.sdk import AxonOptions, AxonSession\n"
        "with AxonSession(AxonOptions(workspace=os.getcwd(), mode='bypass')) as s:\n"
        "    a = s.run('remember TOUCAN')\n"
        "    b = s.run('what did I say?')\n"
        "print(json.dumps([a.text, b.text, a.session_id == b.session_id]))\n"
    )
    xenv.llm.enqueue([{"text": "noted TOUCAN"}],
                     {"steps": [{"text": "you said TOUCAN"}],
                      "expect": [lambda q: "remember TOUCAN" in q.all_text() or "history missing"]})
    p = python_snippet(xenv, code)
    assert p.returncode == 0, p.stderr[-2000:]
    assert json.loads(p.stdout.strip().splitlines()[-1]) == ["noted TOUCAN", "you said TOUCAN", True]
    assert not xenv.llm.failures, xenv.llm.failures
    assert len([r for r in usage_records(xenv) if r["source"] == "main"]) == 2


def test_sdk_provider_error_becomes_error_result(xenv: AxonEnv) -> None:
    """A 401 from the provider ends the stream with result.is_error=True (no exception escapes)."""
    xenv.llm.enqueue([{"error": 401}])
    ev = _sdk_events(xenv, "hi")
    assert ev[-1]["type"] == "result" and ev[-1]["is_error"] is True
    assert "401" in (ev[-1].get("error") or "") + ev[-1]["result"]


# ─── axon serve ─────────────────────────────────────────────────────────────

TOKEN = "e2e-serve-token-123"


class Served:
    def __init__(self, env: AxonEnv, proc: subprocess.Popen, port: int) -> None:
        self.env, self.proc, self.port = env, proc, port

    def request(self, method: str, path: str, body: Any = None, token: str | None = TOKEN,
                headers: dict[str, str] | None = None, raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        out = resp.status, {k.lower(): v for k, v in resp.getheaders()}, payload
        conn.close()
        return out

    def json(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        status, _h, payload = self.request(method, path, body, **kw)
        return status, json.loads(payload.decode("utf-8") or "null")


def _parse_sse(payload: bytes) -> list[tuple[str, str, dict[str, Any]]]:
    frames = []
    for block in payload.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        fid = ev = data = ""
        for line in block.splitlines():
            if line.startswith("id: "):
                fid = line[4:]
            elif line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        frames.append((fid, ev, json.loads(data)))
    return frames


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    llm = ExtFakeLLM(model_ids=("gpt-4o",)).start()
    env = create_axon_env(tmp_path_factory.mktemp("serve"), llm)
    proc = subprocess.Popen([sys.executable, "-m", "axon", "serve", "--port", "0", "--token", TOKEN, "--mode", "bypass"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env.env, cwd=str(env.workspace))
    port = None
    deadline = time.time() + 30
    lines = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        m = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
        if m:
            port = int(m.group(1))
            break
    if port is None:
        proc.kill()
        llm.stop()
        pytest.fail("axon serve did not start: " + "".join(lines) + proc.stderr.read())
    try:
        yield Served(env, proc, port)
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        llm.stop()


def test_serve_requires_bearer_token(served: Served) -> None:
    """No token or a wrong token -> 401 with WWW-Authenticate; nothing reaches the LLM."""
    n0 = len(served.env.llm.requests)
    status, headers, _ = served.request("GET", "/health", token=None)
    assert status == 401 and "bearer" in headers.get("www-authenticate", "").lower()
    status2, _h, _b = served.request("POST", "/v1/query", {"prompt": "hi"}, token="wrong-token")
    assert status2 == 401
    assert len(served.env.llm.requests) == n0


def test_serve_health(served: Served) -> None:
    """GET /health with the token -> status ok, axon version, served workspace."""
    import axon
    status, data = served.json("GET", "/health")
    assert status == 200 and data["status"] == "ok"
    assert data["version"] == axon.__version__
    assert Path(data["workspace"]).resolve() == served.env.workspace.resolve()


def test_serve_rejects_non_loopback_host_header(served: Served) -> None:
    """DNS-rebinding protection: a non-loopback Host header is refused with 403 even with a valid token."""
    status, _h, _b = served.request("GET", "/health", headers={"Host": "evil.example.com"})
    assert status == 403


def test_serve_query_sse_stream_parsed_end_to_end(served: Served) -> None:
    """POST /v1/query (stream): SSE frames with sequential ids, event == data.type, text deltas, final result."""
    served.env.llm.enqueue([{"text": "streamed over http"}, {"usage": {"input": 50_000, "output": 5_000}}])
    status, headers, payload = served.request("POST", "/v1/query", {"prompt": "hello server"})
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(payload)
    assert [int(f[0]) for f in frames] == list(range(len(frames)))
    for _fid, ev, data in frames:
        expected_ev = f"system/{data['subtype']}" if data["type"] == "system" and data.get("subtype") else data["type"]
        assert ev == expected_ev
    assert frames[0][1] == "system/init"
    assert "".join(d["text"] for _i, e, d in frames if e == "text_delta") == "streamed over http"
    res = frames[-1][2]
    assert res["type"] == "result" and res["result"] == "streamed over http" and res["is_error"] is False
    assert abs(Decimal(str(res["cost_usd"])) - _cost(50_000, 5_000)) < Decimal("1e-6")


def test_serve_query_non_stream_json(served: Served) -> None:
    """stream=false returns a JSON body with the result event (and events when include_events)."""
    served.env.llm.enqueue([{"text": "plain json"}])
    status, data = served.json("POST", "/v1/query", {"prompt": "hi", "stream": False, "include_events": True})
    assert status == 200
    assert data["result"]["result"] == "plain json"
    assert data["events"][-1]["type"] == "result" and data["events"][0]["type"] == "system"


def test_serve_query_runs_tools_in_workspace(served: Served) -> None:
    """A query whose model calls Write creates the file in the served workspace (mode bypass)."""
    served.env.llm.enqueue([{"tool": "Write", "args": {"path": "served.txt", "content": "from http"}}], [{"text": "done"}])
    status, _h, payload = served.request("POST", "/v1/query", {"prompt": "write served.txt"})
    assert status == 200
    frames = _parse_sse(payload)
    assert any(e == "tool_result" for _i, e, _d in frames)
    assert (served.env.workspace / "served.txt").read_text(encoding="utf-8") == "from http"


def test_serve_usage_totals_match_usage_log_and_hand_cost(served: Served) -> None:
    """GET /v1/usage: totals equal the sum of ~/.axon/usage.jsonl; a scripted call adds exactly its cost."""
    status, before = served.json("GET", "/v1/usage?days=0")
    assert status == 200
    served.env.llm.enqueue([{"text": "metered"}, {"usage": {"input": 200_000, "output": 20_000, "cached": 100_000}}])
    assert served.request("POST", "/v1/query", {"prompt": "meter me"})[0] == 200
    status, after = served.json("GET", "/v1/usage?days=0")
    assert status == 200
    delta = Decimal(str(after["totals"]["cost_usd"])) - Decimal(str(before["totals"]["cost_usd"]))
    assert abs(delta - _cost(200_000, 20_000, 100_000)) < Decimal("2e-6"), delta
    assert after["totals"]["calls"] == before["totals"]["calls"] + 1
    assert after["totals"]["cache_read_tokens"] - before["totals"]["cache_read_tokens"] == 100_000
    recs = usage_records(served.env)
    assert after["totals"]["calls"] == len(recs)
    assert abs(after["totals"]["cost_usd"] - sum(r["cost"] for r in recs)) < 1e-6
    assert "gpt-4o" in after["by_model"] and "main" in after["by_source"]


def test_serve_bad_requests(served: Served) -> None:
    """Empty prompt -> 400, invalid JSON -> 400, unknown route -> 404 (all with a token)."""
    assert served.json("POST", "/v1/query", {"prompt": "  "})[0] == 400
    assert served.request("POST", "/v1/query", raw=b"{not json")[0] == 400
    assert served.request("GET", "/v1/nope")[0] == 404
    assert served.request("POST", "/v1/other", {"prompt": "x"})[0] == 404


def test_serve_sessions_endpoint_lists_queries(served: Served) -> None:
    """GET /v1/sessions lists the sessions created by earlier queries."""
    served.env.llm.enqueue([{"text": "session maker"}])
    assert served.request("POST", "/v1/query", {"prompt": "make a session"})[0] == 200
    status, data = served.json("GET", "/v1/sessions?limit=50")
    assert status == 200 and len(data["sessions"]) >= 1
