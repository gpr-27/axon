"""
E2E-C: provider HTTP errors, retries and the failover chain, through the real CLI.

Non-retryable statuses (400/401/402/403/404/413) are tried exactly once and produce an actionable
message; retryable ones (429/500/529) are retried 3 times; --fallback-model hops to the next model.
"""
from __future__ import annotations

import json

import pytest

from harness import AxonEnv
from test_e2e_c_support import ext_llm, no_traceback, usage_records, xenv  # noqa: F401

pytestmark = pytest.mark.e2e

# status -> phrases the user-facing message must contain (case-insensitive)
NON_RETRYABLE = {
    400: ("400", "bad request"),
    401: ("401", "api key"),
    402: ("402", "payment required"),
    403: ("403", "forbidden"),
    404: ("404", "not found"),
    413: ("413", "too large"),
}
RETRYABLE = {
    429: ("429", "rate limit"),
    500: ("500", "temporarily unavailable"),
    529: ("529", "temporarily unavailable"),
}


def _fail_json(env: AxonEnv, extra: dict | None = None):
    r = env.run(["-p", "--output-format", "json", "hi"], env=extra or {}, timeout=40)
    no_traceback(r)
    return r


@pytest.mark.parametrize("status", sorted(NON_RETRYABLE))
def test_openai_wire_non_retryable_error_is_friendly_and_not_retried(xenv: AxonEnv, status: int) -> None:
    """HTTP 4xx (non-retryable) -> exit 1, is_error, friendly message naming the cause, exactly one request."""
    xenv.llm.enqueue([{"error": status, "message": f"upstream says {status}"}])
    r = _fail_json(xenv)
    assert r.code == 1, str(r)
    data = r.json()
    assert data["is_error"] is True and data["stop_reason"] == "error"
    low = data["result"].lower()
    for phrase in NON_RETRYABLE[status]:
        assert phrase in low, (status, data["result"])
    assert f"upstream says {status}" in data["result"]  # provider detail is preserved
    assert len(xenv.llm.completion_requests()) == 1


@pytest.mark.parametrize("status", sorted(RETRYABLE))
def test_openai_wire_retryable_error_exhausts_three_retries(xenv: AxonEnv, status: int) -> None:
    """429/500/529 -> 1 try + 3 retries, then a friendly 'retries exhausted' style error."""
    xenv.llm.enqueue(*[[{"error": status, "retry_after": 0}]] * 4)
    r = _fail_json(xenv)
    assert r.code == 1, str(r)
    low = r.json()["result"].lower()
    for phrase in RETRYABLE[status]:
        assert phrase in low, (status, r.json()["result"])
    assert len(xenv.llm.completion_requests()) == 4


@pytest.mark.parametrize("status", [401, 400, 403])
def test_anthropic_wire_non_retryable_errors_are_friendly(xenv: AxonEnv, status: int) -> None:
    """Same classification on the Anthropic Messages wire (SDK exceptions mapped to friendly errors)."""
    xenv.llm.enqueue([{"error": status, "message": f"anthropic detail {status}"}])
    r = _fail_json(xenv, {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"})
    assert r.code == 1, str(r)
    res = r.json()["result"]
    assert str(status) in res and f"anthropic detail {status}" in res, res
    assert len(xenv.llm.completion_requests()) == 1


def test_anthropic_wire_529_overloaded_is_retried_then_succeeds(xenv: AxonEnv) -> None:
    """529 overloaded_error from Anthropic is retried and the turn succeeds."""
    xenv.llm.enqueue([{"error": 529, "retry_after": 0}], [{"error": 529, "retry_after": 0}], [{"text": "back online"}])
    r = xenv.run(["-p", "hi"], env={"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"}, timeout=40)
    assert r.code == 0, str(r)
    assert "back online" in r.output
    assert len(xenv.llm.completion_requests()) == 3


def test_retry_events_are_visible_in_stream_json(xenv: AxonEnv) -> None:
    """Each retry is emitted as a `retry` event before the successful answer."""
    xenv.llm.enqueue([{"error": 500, "retry_after": 0}], [{"error": 503, "retry_after": 0}], [{"text": "third time"}])
    r = xenv.run(["-p", "--output-format", "stream-json", "hi"], timeout=40)
    assert r.code == 0, str(r)
    ev = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    retries = [e for e in ev if e.get("type") == "retry"]
    assert len(retries) == 2, [e.get("type") for e in ev]
    assert ev[-1]["result"].strip() == "third time"


def test_retry_after_header_is_honoured(xenv: AxonEnv) -> None:
    """A 429 with Retry-After: 1 delays the retry by about a second."""
    xenv.llm.enqueue([{"error": 429, "retry_after": 1}], [{"text": "waited"}])
    r = xenv.run(["-p", "hi"], timeout=40)
    assert r.code == 0, str(r)
    reqs = xenv.llm.completion_requests()
    assert len(reqs) == 2
    assert reqs[1].ts - reqs[0].ts >= 0.9


def test_context_length_400_suggests_compact(xenv: AxonEnv) -> None:
    """A 400 'maximum context length' error points the user at /compact."""
    xenv.llm.enqueue([{"error": 400, "message": "This model's maximum context length is 128000 tokens"}])
    r = _fail_json(xenv)
    assert r.code == 1
    assert "/compact" in r.json()["result"] and "context window" in r.json()["result"]


def test_text_mode_error_goes_to_stderr_without_traceback(xenv: AxonEnv) -> None:
    """Text mode: the friendly error is printed, exit 1, no Python traceback."""
    xenv.llm.enqueue([{"error": 403, "message": "region blocked"}])
    r = xenv.run(["-p", "hi"], timeout=30)
    assert r.code == 1, str(r)
    no_traceback(r)
    assert "403" in r.output and "region blocked" in r.output


def test_connection_refused_is_friendly(xenv: AxonEnv) -> None:
    """Nothing listening on the base_url port: a friendly local-endpoint message, no traceback."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = xenv.run(["-p", "--output-format", "json", "hi"], env={"AXON_BASE_URL": f"http://127.0.0.1:{port}"}, timeout=60)
    no_traceback(r)
    assert r.code == 1, str(r)
    assert "connect" in r.json()["result"].lower()


# ─── failover chain ─────────────────────────────────────────────────────────

def test_failover_to_fallback_model_after_retries(xenv: AxonEnv) -> None:
    """Primary keeps returning 500 -> after its retries the --fallback-model answers the turn."""
    xenv.llm.enqueue(*[[{"error": 500, "retry_after": 0}]] * 4,
                     [{"text": "answered by fallback"}, {"usage": {"input": 1000, "output": 100}}])
    r = xenv.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=40)
    assert r.code == 0, str(r)
    assert r.json()["result"].strip() == "answered by fallback"
    models = [q.body["model"] for q in xenv.llm.completion_requests()]
    assert models == ["gpt-4o"] * 4 + ["gpt-4o-mini"], models


def test_failover_call_is_billed_at_fallback_model_price(xenv: AxonEnv) -> None:
    """The successful fallback call is recorded (ledger + usage.jsonl) under the fallback model's rates."""
    from axon.providers.pricing import PROVIDER_PRICING
    xenv.llm.enqueue(*[[{"error": 529, "retry_after": 0}]] * 4,
                     [{"text": "fb"}, {"usage": {"input": 200000, "output": 100000}}])
    r = xenv.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=40)
    assert r.code == 0, str(r)
    rate = PROVIDER_PRICING["openai"]["gpt-4o-mini"]
    expected = 200000 * rate["input"] / 1e6 + 100000 * rate["output"] / 1e6
    assert abs(r.json()["cost_usd"] - expected) < 1e-6, (r.json()["cost_usd"], expected)
    recs = [x for x in usage_records(xenv) if x["source"] == "main"]
    assert [x["model"] for x in recs] == ["gpt-4o-mini"]
    assert abs(recs[0]["cost"] - expected) < 1e-6


def test_failover_chain_of_two_fallbacks(xenv: AxonEnv) -> None:
    """Chain gpt-4o -> gpt-4.1 -> gpt-4o-mini: the second fallback answers after the first also fails."""
    xenv.llm.enqueue(*[[{"error": 500, "retry_after": 0}]] * 8, [{"text": "third model"}])
    r = xenv.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4.1,gpt-4o-mini", "hi"], timeout=60)
    assert r.code == 0, str(r)
    models = [q.body["model"] for q in xenv.llm.completion_requests()]
    assert models == ["gpt-4o"] * 4 + ["gpt-4.1"] * 4 + ["gpt-4o-mini"], models


def test_no_failover_on_auth_error(xenv: AxonEnv) -> None:
    """A 401 is not a failover reason: one request, no fallback hop."""
    xenv.llm.enqueue([{"error": 401}], [{"text": "should not be used"}])
    r = xenv.run(["-p", "--output-format", "json", "--fallback-model", "gpt-4o-mini", "hi"], timeout=30)
    assert r.code == 1, str(r)
    assert [q.body["model"] for q in xenv.llm.completion_requests()] == ["gpt-4o"]


def test_failover_from_settings_file_fallback_model(xenv: AxonEnv) -> None:
    """fallback_model in ~/.axon/config.toml is honoured like the CLI flag."""
    (xenv.axon_home / "config.toml").write_text('fallback_model = "gpt-4o-mini"\n', encoding="utf-8")
    xenv.llm.enqueue(*[[{"error": 503, "retry_after": 0}]] * 4, [{"text": "config fallback"}])
    r = xenv.run(["-p", "hi"], timeout=40)
    assert r.code == 0, str(r)
    assert "config fallback" in r.output
    assert xenv.llm.completion_requests()[-1].body["model"] == "gpt-4o-mini"


@pytest.mark.parametrize("status", [402, 404, 413])
def test_anthropic_wire_more_non_retryable_statuses(xenv: AxonEnv, status: int) -> None:
    """402/404/413 on the Anthropic wire map to the same friendly messages, one request each."""
    xenv.llm.enqueue([{"error": status, "message": f"anth {status} detail"}])
    r = _fail_json(xenv, {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"})
    assert r.code == 1, str(r)
    low = r.json()["result"].lower()
    for phrase in NON_RETRYABLE[status]:
        assert phrase in low, (status, r.json()["result"])
    assert len(xenv.llm.completion_requests()) == 1


@pytest.mark.parametrize("status", [429, 500])
def test_anthropic_wire_retryable_statuses_exhaust_retries(xenv: AxonEnv, status: int) -> None:
    """429/500 on the Anthropic wire: 1 + 3 retries, then the friendly exhausted message."""
    xenv.llm.enqueue(*[[{"error": status, "retry_after": 0}]] * 4)
    r = _fail_json(xenv, {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"})
    assert r.code == 1, str(r)
    low = r.json()["result"].lower()
    for phrase in RETRYABLE[status]:
        assert phrase in low, (status, r.json()["result"])
    assert len(xenv.llm.completion_requests()) == 4
