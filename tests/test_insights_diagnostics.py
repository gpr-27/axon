"""Anthropic cache diagnostics and refusal details: sent only to the official API, dropped when rejected."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests.test_providers_layer import ANTHROPIC_STREAM, _anthropic_httpx, _anthropic_sse, anthropic_provider


def _stream(msg_id: str, *, diagnostics: Any = None, stop_details: Any = None, stop: str = "end_turn") -> list[dict]:
    ev = copy.deepcopy(ANTHROPIC_STREAM)
    ev = [e for e in ev if e["type"] in ("message_start", "message_delta", "message_stop")]
    ev[0]["message"]["id"] = msg_id
    if diagnostics is not None:
        ev[0]["message"]["diagnostics"] = diagnostics
    ev[1]["delta"]["stop_reason"] = stop
    if stop_details is not None:
        ev[1]["delta"]["stop_details"] = stop_details
    return ev


def _call(prov: Any) -> Any:
    list(prov.stream(model="claude-sonnet-4-5", system=[{"type": "text", "text": "s"}], tools=[], max_tokens=100,
                     messages=[{"role": "user", "content": "x"}], thinking=False))
    return prov.finalize()


def test_previous_message_id_is_sent_and_diagnostics_are_read(_clean_env: Any) -> None:
    hx = _anthropic_httpx()
    bodies: list[dict] = []
    diag = {"cache_miss_reason": {"type": "tools_changed"}, "cache_missed_input_tokens": 1234}
    replies = [_stream("msg_a"), _stream("msg_b", diagnostics=diag)]

    def handler(request: Any) -> Any:
        bodies.append(json.loads(request.content))
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(replies[len(bodies) - 1]))

    prov = anthropic_provider(handler)
    first = _call(prov)
    second = _call(prov)
    assert bodies[0]["diagnostics"] == {"previous_message_id": None}   # opts in; nothing earlier to compare with
    assert bodies[1]["diagnostics"] == {"previous_message_id": "msg_a"}
    assert first.diagnostics is None and second.diagnostics == diag


def test_rejected_diagnostics_are_dropped_for_the_session(_clean_env: Any) -> None:
    hx = _anthropic_httpx()
    bodies: list[dict] = []

    def handler(request: Any) -> Any:
        body = json.loads(request.content)
        bodies.append(body)
        if "diagnostics" in body:
            return hx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": "diagnostics: Extra inputs are not permitted"}})
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(_stream(f"msg_{len(bodies)}")))

    prov = anthropic_provider(handler)
    _call(prov)
    turn = _call(prov)                                          # the first call opts in, is rejected and retried without it
    _call(prov)                                                 # the field is not tried again
    sent = ["diagnostics" in b for b in bodies]
    assert sent == [True, False, False, False] and turn.text is not None


def test_gateways_never_get_the_field(_clean_env: Any) -> None:
    hx = _anthropic_httpx()
    bodies: list[dict] = []

    def handler(request: Any) -> Any:
        bodies.append(json.loads(request.content))
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=_anthropic_sse(_stream(f"msg_{len(bodies)}")))

    prov = anthropic_provider(handler, base_url="https://gateway.example.com")
    _call(prov)
    _call(prov)
    assert all("diagnostics" not in b for b in bodies)


def test_stop_details_are_kept_on_the_turn(_clean_env: Any) -> None:
    hx = _anthropic_httpx()
    details = {"category": "cyber", "explanation": "Looks like malware work.", "recommended_model": "claude-sonnet-4-5"}
    prov = anthropic_provider(lambda r: hx.Response(200, headers={"content-type": "text/event-stream"},
                                                    content=_anthropic_sse(_stream("m1", stop="refusal", stop_details=details))))
    turn = _call(prov)
    assert turn.stop_reason == "refusal" and turn.stop_details == details


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    from axon.providers import transport
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(transport, "_sleep", lambda s: None)
    return True


# ─── refusal explainer ───────────────────────────────────────────────────────

def _refusal_line(capsys: Any, **extra: Any) -> str:
    from axon.providers.base import TurnComplete, Usage
    from axon.ui.render import Renderer
    from axon.ui.theme import strip_ansi
    r = Renderer(trace_level="normal")
    r.on_event(TurnComplete(stop_reason="refusal", usage=Usage(input=1, output=1), **extra))
    return strip_ansi(capsys.readouterr().out)


def test_refusal_without_details_is_unchanged(capsys: Any) -> None:
    out = _refusal_line(capsys)
    assert "the model declined to answer" in out and "category" not in out
    assert "next:" in out and "refusal_fallback_model" in out        # one next-step line when there is no fallback model


def test_refusal_with_details_names_category_explanation_model_and_hint(capsys: Any) -> None:
    out = _refusal_line(capsys, stop_details={"category": "cyber", "explanation": "Looks like malware work.",
                                              "recommended_model": "claude-sonnet-4-5"})
    assert "the model declined to answer" in out
    assert "cyber" in out and "Looks like malware work." in out and "claude-sonnet-4-5" in out and "/model" in out


def test_provider_hands_the_details_to_the_renderer_event(_clean_env: Any) -> None:
    hx = _anthropic_httpx()
    details = {"category": "cyber"}
    prov = anthropic_provider(lambda r: hx.Response(200, headers={"content-type": "text/event-stream"},
                                                    content=_anthropic_sse(_stream("m1", stop="refusal", stop_details=details))))
    events = list(prov.stream(model="claude-sonnet-4-5", system=[], tools=[], max_tokens=100,
                              messages=[{"role": "user", "content": "x"}], thinking=False))
    done = [e for e in events if type(e).__name__ == "TurnComplete"][0]
    assert done.stop_details == details
