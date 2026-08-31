"""
Unit tests for live API key verification engine and command integration.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from pydantic import SecretStr

from axon.config import Settings
from axon.providers.catalog import get_preset_by_id
from axon.providers.verifier import verify_api_key
from axon.commands.builtin import handle_keys, dispatch_command
from axon.agent.loop import Agent
from axon.agent.context import ContextManager
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.permissions.engine import PermissionEngine
from axon.tools.registry import create_default_registry


def test_verify_api_key_local_provider_bypass():
    # Ollama or localhost provider requires zero keys
    ok, msg = verify_api_key("ollama", "")
    assert ok is True
    assert "zero key" in msg.lower() or "local" in msg.lower()

    ok2, msg2 = verify_api_key("agentrouter", "", base_url="http://localhost:11434/v1")
    assert ok2 is True


def test_verify_api_key_placeholder_rejection():
    # Placeholder keys must fail immediately
    for ph in ("", "sk-placeholder", "your_api_key_here", "replace_me"):
        ok, msg = verify_api_key("gemini", ph)
        assert ok is False
        assert "placeholder" in msg.lower() or "empty" in msg.lower()


def test_verify_api_key_mock_test_key():
    # Known test keys bypass network
    ok, msg = verify_api_key("gemini", "AIzaSy_test_12345")
    assert ok is True
    assert msg == "OK"


def test_verify_api_key_mock_200_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.post", return_value=mock_resp):
        ok, msg = verify_api_key("openai", "sk-actual-working-key-999")
        assert ok is True
        assert msg == "OK"


def test_verify_api_key_mock_401_failure():
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": {"message": "Incorrect API key provided"}}

    with patch("httpx.post", return_value=mock_resp):
        ok, msg = verify_api_key("openai", "sk-bogus-invalid-key")
        assert ok is False
        assert "Incorrect API key" in msg or "401" in msg


def test_handle_keys_rejects_invalid_key_and_does_not_save(tmp_path: Path, monkeypatch):
    settings = Settings(workspace=tmp_path)
    agent = Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {"error": {"message": "API_KEY_INVALID"}}

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with patch("httpx.post", return_value=mock_resp):
        res = handle_keys(agent, "gemini hloooo_bad_key")
        assert res.handled is True
        # GEMINI_API_KEY must NOT be set in os.environ
        assert os.environ.get("GEMINI_API_KEY") != "hloooo_bad_key"


def test_handle_keys_accepts_valid_key_and_updates_env(tmp_path: Path):
    settings = Settings(workspace=tmp_path)
    agent = Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.post", return_value=mock_resp):
        res = handle_keys(agent, "gemini AIzaSy_valid_working_key_123")
        assert res.handled is True
        assert os.environ.get("GEMINI_API_KEY") == "AIzaSy_valid_working_key_123"
