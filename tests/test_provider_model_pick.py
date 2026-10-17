"""Connecting to a provider: API key -> pick from the models that key can use -> verify with that model."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from axon.providers import catalog
from axon.ui import provider_picker as pp

GROQ = next(p for p in catalog.PROVIDER_PRESETS if p.id == "groq")


def _setup(monkeypatch: Any, live: Any, picks: list[str], verify: list[tuple[bool, str]]) -> dict[str, Any]:
    seen: dict[str, Any] = {"verified": [], "saved": None, "applied": None, "offered": None}
    monkeypatch.setattr(pp, "find_key", lambda p: None)
    monkeypatch.setattr(pp, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(pp.getpass, "getpass", lambda *a, **k: "gsk_live_key")
    monkeypatch.setattr(catalog, "fetch_provider_models", lambda preset, key, **k: live)

    def fake_pick(items: list[str], **k: Any) -> str:
        seen["offered"] = list(items)
        return picks.pop(0) if picks else ""
    monkeypatch.setattr(pp, "pick", fake_pick)

    def fake_verify(preset: Any, key: str, model: str) -> tuple[bool, str]:
        seen["verified"].append(model)
        return verify.pop(0)
    monkeypatch.setattr(pp, "_verify", fake_verify)
    monkeypatch.setattr(pp, "store_key", lambda env, key: seen.__setitem__("saved", key) or "/tmp/keys")
    monkeypatch.setattr(pp, "apply_provider", lambda agent, preset, model, key: seen.__setitem__("applied", model) or True)
    return seen


def test_models_come_from_the_key_before_verification(monkeypatch: Any) -> None:
    live = (["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"], 200)
    seen = _setup(monkeypatch, live, ["qwen/qwen3.8-27b"], [(True, "OK")])
    assert pp._configure_and_apply_preset(SimpleNamespace(), GROQ) is True
    assert seen["offered"][:3] == live[0]
    assert seen["verified"] == ["qwen/qwen3.8-27b"]                 # verified with the model the user chose
    assert seen["saved"] == "gsk_live_key" and seen["applied"] == "qwen/qwen3.8-27b"


def test_a_model_the_key_cannot_use_lets_the_user_pick_again(monkeypatch: Any) -> None:
    seen = _setup(monkeypatch, (None, None), ["openai/gpt-oss-20b", "openai/gpt-oss-120b"],
                  [(False, "The model `openai/gpt-oss-20b` does not exist (HTTP 404)"), (True, "OK")])
    assert pp._configure_and_apply_preset(SimpleNamespace(), GROQ) is True
    assert seen["verified"] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert seen["applied"] == "openai/gpt-oss-120b"


def test_a_rejected_key_stops_before_the_model_list(monkeypatch: Any, capsys: Any) -> None:
    seen = _setup(monkeypatch, (None, 401), [], [])
    assert pp._configure_and_apply_preset(SimpleNamespace(), GROQ) is False
    assert seen["offered"] is None and seen["saved"] is None
    assert "rejected the API key (HTTP 401)" in capsys.readouterr().out


def test_bad_key_on_verification_is_not_saved(monkeypatch: Any) -> None:
    seen = _setup(monkeypatch, (None, None), ["openai/gpt-oss-120b"], [(False, "Invalid API Key (HTTP 401)")])
    assert pp._configure_and_apply_preset(SimpleNamespace(), GROQ) is False
    assert seen["saved"] is None and seen["applied"] is None


def test_fetch_provider_models_keeps_chat_models_only(monkeypatch: Any) -> None:
    data = {"data": [{"id": i} for i in ("whisper-large-v3", "openai/gpt-oss-20b", "canopylabs/orpheus-v1-english",
                                         "meta-llama/llama-prompt-guard-2-86m", "openai/gpt-oss-safeguard-20b",
                                         "qwen/qwen3.8-27b", "openai/gpt-oss-120b")]}
    calls: list[str] = []
    monkeypatch.setattr(httpx, "get", lambda url, **k: calls.append(url) or httpx.Response(200, json=data))
    models, status = catalog.fetch_provider_models(GROQ, "gsk_x")
    assert status == 200 and calls == ["https://api.groq.com/openai/v1/models"]
    assert models == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]   # preset order first


def test_fetch_provider_models_reports_a_bad_key(monkeypatch: Any) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(401, json={"error": {"message": "bad"}}))
    assert catalog.fetch_provider_models(GROQ, "gsk_bad") == (None, 401)


def test_groq_preset_lists_current_models() -> None:
    assert GROQ.default_model == "openai/gpt-oss-120b"
    assert "llama-3.3-70b-versatile" not in GROQ.models


def test_verify_treats_rate_limit_as_a_working_key_and_retries_server_errors(monkeypatch: Any) -> None:
    from axon.providers import verifier
    monkeypatch.setattr(verifier.time, "sleep", lambda s: None)
    answers = [httpx.Response(500, json={"error": {"message": "Upstream rejected the request as invalid"}}),
               httpx.Response(200, json={})]
    monkeypatch.setattr(httpx, "post", lambda *a, **k: answers.pop(0))
    ar = next(p for p in catalog.PROVIDER_PRESETS if p.id == "agentrouter")
    assert verifier.verify_api_key(ar, "sk-real-looking-key", model="deepseek-v4-flash") == (True, "OK")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(429, json={"error": {"message": "slow down"}}))
    ok, msg = verifier.verify_api_key(GROQ, "gsk_real_looking", model="openai/gpt-oss-120b")
    assert ok and "rate limiting" in msg
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503, json={"error": {"message": "down"}}))
    ok, msg = verifier.verify_api_key(GROQ, "gsk_real_looking", model="openai/gpt-oss-120b")
    assert not ok and "not a problem with the key" in msg


def test_env_file_ignores_bom_and_inline_comments(tmp_path: Any) -> None:
    f = tmp_path / ".env"
    f.write_bytes("﻿GROQ_API_KEY=gsk_abc  # work account\nOTHER=\"x\" # note\n".encode("utf-8"))
    assert catalog.read_env_file(f) == {"GROQ_API_KEY": "gsk_abc", "OTHER": "x"}
