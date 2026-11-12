"""A message typed mid-answer must never poison the next request. These tests run the REAL provider classes (official
Anthropic SDK + httpx, and the OpenAI-compatible client) and the real Agent loop against a local keep-alive server,
over plain HTTP and over TLS (the real APIs are TLS: a full socket shutdown of an aborted TLS stream made the next
request die with ``ReadError [Errno 32] Broken pipe``)."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx  # type: ignore[no-redef]

from axon.agent.loop import STEER_PREFIX
from axon.agent.trace import ContextEditEvent, RetryEvent
from axon.config import Settings
from axon.providers import transport as _transport
from axon.providers.anthropic import AnthropicProvider
from axon.providers.base import refresh_http_pool
from axon.providers import openai_compat as _oc
from axon.providers.openai_compat import OpenAICompatProvider
from tests.fake_anthropic_server import FakeAnthropic
from tests.test_core_helpers import make_agent


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not available")
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "c.pem", d / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return str(cert), str(key)


@pytest.fixture(params=["http", "tls"])
def server(request: pytest.FixtureRequest, tls_files: tuple[str, str]) -> Any:
    srv = FakeAnthropic(tls=tls_files if request.param == "tls" else None)
    srv.is_tls = request.param == "tls"
    yield srv
    srv.close()


@pytest.fixture
def plain_server() -> Any:
    srv = FakeAnthropic()
    srv.is_tls = False
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_transport, "_sleep", lambda s: None)


def _client(srv: Any, module: Any = httpx) -> Any:
    return module.Client(verify=False) if srv.is_tls else None


def anthropic_agent(ws: Path, srv: Any, plans: list[dict[str, Any]]) -> tuple[Any, Any, list[Any]]:
    srv.plans = plans
    prov = AnthropicProvider(Settings(workspace=ws, mode="bypass", api_key="k", base_url=srv.url), http_client=_client(srv))
    events: list[Any] = []
    agent = make_agent(prov, ws, events=events, base_url=srv.url)
    return agent, prov, events


def steer_at(agent: Any, *when: tuple[float, str]) -> threading.Thread:
    def run() -> None:
        t0 = time.monotonic()
        for at, text in when:
            time.sleep(max(0.0, at - (time.monotonic() - t0)))
            agent.message_queue.push(text, steer=True)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def assert_clean_turn(res: Any, events: list[Any], srv: Any, texts: tuple[str, ...]) -> None:
    assert res.stop_reason == "end_turn", res.final_text
    assert res.final_text and "Provider error" not in res.final_text
    assert not [e for e in events if isinstance(e, RetryEvent)]            # no retry warning chain
    assert not [e for e in events if isinstance(e, ContextEditEvent)]      # and the kill switch never had to act
    last = json.dumps(srv.requests[-1]["body"]["messages"])
    for t in texts:
        assert f"{STEER_PREFIX} {t}" in last                                # the user's message was never lost


def test_steer_mid_stream_per_event_path_then_next_request_is_healthy(tmp_path: Path, server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, server, [{"chunks": 25, "gap": 0.1}, {"chunks": 2}])
    steer_at(agent, (0.6, "use b.py"))
    res = agent.run_turn("explain")
    assert_clean_turn(res, ev, server, ("use b.py",))
    assert len(server.requests) == 2                  # exactly one new request carries the correction
    assert not agent._steer_paused


def test_steer_with_a_silent_server_is_aborted_by_the_watcher_and_next_request_is_healthy(tmp_path: Path, server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, server, [{"chunks": 3, "silent": 6.0}, {"chunks": 2}])
    steer_at(agent, (0.3, "never mind"))
    t0 = time.monotonic()
    res = agent.run_turn("explain")
    assert time.monotonic() - t0 < 5.0                # the 6 s silence was cut, not waited out
    assert_clean_turn(res, ev, server, ("never mind",))
    assert len(server.requests) == 2


def test_steer_during_the_connection_phase_waits_for_the_minimum_request_age(tmp_path: Path, server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, server, [{"chunks": 3, "header_delay": 2.5}, {"chunks": 2}])
    cancels: list[float] = []
    real = agent._cancel_provider_stream
    agent._cancel_provider_stream = lambda ident: (cancels.append(time.monotonic()), real(ident))[1]   # type: ignore[method-assign]
    t0 = time.monotonic()
    steer_at(agent, (0.1, "hello again"))
    res = agent.run_turn("explain")
    assert_clean_turn(res, ev, server, ("hello again",))
    assert cancels and cancels[0] - t0 >= agent.STEER_MIN_AGE_S - 0.05     # never in the first moments of a request


def test_two_steers_back_to_back_never_break_a_request(tmp_path: Path, server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, server, [{"chunks": 25, "gap": 0.1}, {"chunks": 25, "gap": 0.1}, {"chunks": 3}])
    steer_at(agent, (0.5, "first fix"), (0.7, "second fix"), (1.2, "third fix"))
    res = agent.run_turn("explain")
    assert_clean_turn(res, ev, server, ("first fix", "second fix", "third fix"))
    assert len(server.requests) <= 4


def test_interrupts_are_rate_limited(tmp_path: Path, plain_server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, plain_server, [{"chunks": 30, "gap": 0.1}] * 3 + [{"chunks": 2}])
    aborts: list[float] = []
    real = agent._steer_interrupt

    def counted(*a: Any) -> bool:
        aborts.append(time.monotonic())
        return real(*a)

    agent._steer_interrupt = counted                  # type: ignore[method-assign]
    steer_at(agent, *[(0.4 + 0.25 * i, f"m{i}") for i in range(8)])
    res = agent.run_turn("explain")
    assert_clean_turn(res, ev, plain_server, tuple(f"m{i}" for i in range(8)))
    assert all(b - a >= agent.STEER_MIN_GAP_S - 0.05 for a, b in zip(aborts, aborts[1:]))


def test_steer_then_esc_then_a_new_turn(tmp_path: Path, server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, server, [{"chunks": 40, "gap": 0.1}, {"chunks": 40, "gap": 0.1}, {"chunks": 3}])
    steer_at(agent, (0.5, "change plan"))
    threading.Timer(2.5, agent.cancel_current_turn).start()
    first = agent.run_turn_threaded("explain")
    assert first is not None and first.stop_reason != "error"
    n = len(server.requests)
    res = agent.run_turn("and now?")                  # the next turn on the same provider and pool
    assert res.stop_reason == "end_turn" and "Provider error" not in res.final_text
    assert len(server.requests) == n + 1
    assert not [e for e in ev if isinstance(e, RetryEvent)]


def test_steer_while_the_model_writes_a_tool_call_or_a_tool_runs_aborts_nothing(tmp_path: Path, plain_server: Any) -> None:
    agent, _, ev = anthropic_agent(tmp_path, plain_server, [{"tool": True, "gap": 0.4}, {"chunks": 2}])
    cancels: list[Any] = []
    real_cancel = agent._cancel_provider_stream
    agent._cancel_provider_stream = lambda ident: (cancels.append(ident), real_cancel(ident))[1]    # type: ignore[method-assign]
    real = agent._execute_batch

    def slow(tool_uses: Any) -> Any:
        agent.message_queue.push("while the tool runs", steer=True)
        time.sleep(0.3)
        return real(tool_uses)

    agent._execute_batch = slow                       # type: ignore[method-assign]
    steer_at(agent, (0.5, "while the call is written"))
    res = agent.run_turn("list")
    assert_clean_turn(res, ev, plain_server, ("while the call is written", "while the tool runs"))
    assert not cancels                                # nothing was aborted
    msgs = plain_server.requests[1]["body"]["messages"]
    assert msgs[1]["role"] == "assistant" and msgs[1]["content"][-1]["type"] == "tool_use"
    assert msgs[2]["role"] == "user" and msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == msgs[1]["content"][-1]["id"]      # pairing untouched


def test_a_connection_error_after_a_steer_heals_and_pauses_interrupts(tmp_path: Path, server: Any) -> None:
    prov0 = AnthropicProvider(Settings(workspace=tmp_path, api_key="k", base_url=server.url))
    drops = prov0._max_retries + 1                    # the provider's own retries all fail too; the loop heals once more
    plans = [{"chunks": 25, "gap": 0.1}] + [{"drop": True}] * drops + [{"chunks": 2}]
    agent, _, ev = anthropic_agent(tmp_path, server, plans)
    steer_at(agent, (0.6, "fix"))
    res = agent.run_turn("explain")
    assert res.stop_reason == "end_turn" and "Provider error" not in res.final_text
    assert agent._steer_paused and not agent.steer_interrupts_on()
    notes = [e.message for e in ev if isinstance(e, ContextEditEvent)]
    assert notes == ["steering: interrupts paused for this session after a connection error; messages will be read at the next step"]
    assert f"{STEER_PREFIX} fix" in json.dumps(server.requests[-1]["body"]["messages"])
    # later messages are read at step boundaries and never abort anything
    server.plans = [{"chunks": 6, "gap": 0.1}, {"chunks": 2}]
    n = len(server.requests)
    steer_at(agent, (0.2, "later"))
    res2 = agent.run_turn("again")
    assert res2.stop_reason == "end_turn" and len(server.requests) >= n + 1


def test_provider_retries_a_dropped_connection_on_a_fresh_one_at_once(tmp_path: Path, server: Any) -> None:
    agent, prov, ev = anthropic_agent(tmp_path, server, [{"drop": True}, {"chunks": 2}])
    res = agent.run_turn("hi")
    assert res.stop_reason == "end_turn" and "Provider error" not in res.final_text
    assert len(server.requests) == 2


def test_transport_error_refreshes_the_client_before_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = AnthropicProvider(Settings(api_key="k", base_url="https://gateway.example"))
    order: list[str] = []
    monkeypatch.setattr(prov, "refresh_client", lambda: order.append("refresh"))
    from axon.providers.base import AssistantTurn, TextBlock, TurnComplete, Usage

    def fake_run(kwargs: Any, state: Any, emitted: list[bool], thinking: bool, wire_model: str = "") -> Any:
        order.append("run")
        if order.count("run") == 1:
            raise httpx.ReadError("[Errno 32] Broken pipe")
        state.turn = AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn", usage=Usage())
        yield TurnComplete("end_turn", Usage())

    monkeypatch.setattr(prov, "_run", fake_run)
    events = list(prov.stream(model="claude-sonnet-5-5", system=[{"type": "text", "text": "s"}],
                              messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=50, effort=None, thinking=False))
    assert order == ["run", "refresh", "run"]
    retry = [e for e in events if type(e).__name__ == "RetryEvent" or hasattr(e, "delay_s")]
    assert retry and getattr(retry[0], "delay_s", 0.0) == 0.0          # the first retry of a transport error is immediate


def test_refresh_http_pool_evicts_pooled_connections_and_the_client_stays_usable(plain_server: Any) -> None:
    prov = AnthropicProvider(Settings(api_key="k", base_url=plain_server.url))
    kw: dict[str, Any] = dict(model="claude-sonnet-5-5", system=[{"type": "text", "text": "s"}],
                              messages=[{"role": "user", "content": "a"}], tools=[], max_tokens=50, effort=None, thinking=False)
    list(prov.stream(**kw))
    prov.refresh_client()
    list(prov.stream(**kw))
    assert len(plain_server.requests) == 2 and len({r["conn"] for r in plain_server.requests}) == 2   # fresh connection
    refresh_http_pool(object())                        # unknown shapes are ignored


def test_openai_compat_refreshes_a_shared_client_on_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = OpenAICompatProvider(Settings(api_key="k", base_url="https://gateway.example/v1"), http_client=httpx.Client())
    closed: list[bool] = []
    monkeypatch.setattr("axon.providers.openai_compat.refresh_http_pool", lambda c: closed.append(c is prov._client))
    prov.refresh_client()
    assert closed == [True]


def test_openai_compat_steer_then_next_request_is_healthy(tmp_path: Path, server: Any) -> None:
    server.plans = [{"chunks": 25, "gap": 0.1}, {"chunks": 2}]
    prov = OpenAICompatProvider(Settings(workspace=tmp_path, mode="bypass", api_key="k", base_url=server.url + "/v1",
                                         model="gpt-test"), http_client=_client(server, _oc.httpx))
    events: list[Any] = []
    agent = make_agent(prov, tmp_path, events=events, base_url=server.url + "/v1", model="gpt-test")
    steer_at(agent, (0.6, "use b.py"))
    res = agent.run_turn("explain")
    assert res.stop_reason == "end_turn" and "Provider error" not in res.final_text, res.final_text
    assert not [e for e in events if isinstance(e, RetryEvent)]
    assert len(server.requests) == 2
    assert f"{STEER_PREFIX} use b.py" in json.dumps(server.requests[-1]["body"]["messages"])
