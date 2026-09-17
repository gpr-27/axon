"""
Tests for provider linkage detection, tick mark indicators, and tabbed model picker behavior.
"""
import os
from unittest.mock import MagicMock, patch
import pytest
from pydantic import SecretStr

from axon.providers.catalog import (
    PROVIDER_PRESETS,
    get_preset_by_id,
    is_provider_linked,
    get_linked_providers,
    get_models_for_provider,
    get_curated_model_choices,
)
from axon.commands.builtin import handle_provider, handle_model, dispatch_command
from axon.config import Settings
from axon.ui.model_picker import _fallback_model_picker, _apply_model_selection


def test_is_provider_linked_checks_credentials(monkeypatch):
    """Test that is_provider_linked accurately detects existing credentials."""
    agent = MagicMock()
    agent.settings = Settings(api_key=SecretStr("sk-agentrouter"), base_url="https://agentrouter.org")

    ar_preset = get_preset_by_id("agentrouter")
    assert ar_preset is not None
    assert is_provider_linked(ar_preset, agent) is True

    openai_preset = get_preset_by_id("openai")
    assert openai_preset is not None

    # When OPENAI_API_KEY is not set
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Ensure it doesn't mistakenly grab AXON_API_KEY
    assert is_provider_linked(openai_preset, agent=None) is False

    # When OPENAI_API_KEY is set
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test12345")
    assert is_provider_linked(openai_preset, agent=None) is True


def test_get_linked_providers_only_returns_configured_providers(monkeypatch):
    """Verify get_linked_providers returns only presets with verified credentials or active state."""
    # Mock environment to have only AgentRouter and OpenRouter
    monkeypatch.setenv("AXON_API_KEY", "sk-axon-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    linked = get_linked_providers()
    linked_ids = [p.id for p in linked]
    assert "agentrouter" in linked_ids
    assert "openrouter" in linked_ids
    assert "openai" not in linked_ids
    assert "anthropic" not in linked_ids


def test_get_models_for_provider_separates_models_cleanly():
    """Verify get_models_for_provider returns only models belonging to that specific provider."""
    ar = get_preset_by_id("agentrouter")
    assert ar is not None
    ar_models = get_models_for_provider(ar)
    assert "deepseek-v4-flash" in ar_models
    assert "gpt-5.6-sol" in ar_models
    assert "claude-opus-5" in ar_models
    # Shouldn't contain other providers' models
    assert "gpt-4o" not in ar_models
    assert "gemini-2.0-flash" not in ar_models

    gemini = get_preset_by_id("gemini")
    assert gemini is not None
    gemini_models = get_models_for_provider(gemini)
    assert "gemini-2.0-flash" in gemini_models
    assert "deepseek-v4-flash" not in gemini_models


def test_get_curated_model_choices_only_shows_linked_providers(monkeypatch):
    """Verify curated choices does not include unlinked provider models."""
    monkeypatch.setenv("AXON_API_KEY", "sk-axon-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    choices = get_curated_model_choices("https://agentrouter.org")
    provider_labels = {p_label for _, p_label, _ in choices}
    assert "AgentRouter" in provider_labels
    assert "Anthropic" not in provider_labels


def test_handle_provider_defined_and_callable():
    """Verify handle_provider is defined and executes without NameError."""
    agent = MagicMock()
    agent.settings = Settings(api_key=SecretStr("sk-test"), base_url="https://agentrouter.org")
    with patch("axon.ui.provider_picker.run_provider_picker", return_value=True):
        res = handle_provider(agent, "")
        assert res.handled is True

    # Dispatch via slash command
    with patch("axon.ui.provider_picker.run_provider_picker", return_value=True):
        res2 = dispatch_command(agent, "/provider")
        assert res2.handled is True


def test_fallback_model_picker_configures_selected_provider_and_model(capsys):
    """Verify selecting a provider tab and model configures only that connector provider."""
    agent = MagicMock()
    agent.settings = Settings(api_key=SecretStr("sk-test"), base_url="https://agentrouter.org", model="deepseek-v4-flash")

    ar_preset = get_preset_by_id("agentrouter")
    assert ar_preset is not None

    with patch("axon.ui.model_picker.pick") as mock_pick:
        # Step 1: select AgentRouter
        # Step 2: select claude-opus-5
        mock_pick.side_effect = [
            f"✓ {ar_preset.name} ({ar_preset.base_url}) [Active]",
            "claude-opus-5",
        ]
        res = _fallback_model_picker(agent)
        assert res is True
        assert agent.settings.model == "claude-opus-5"
        assert agent.settings.base_url == "https://agentrouter.org"

        from axon.ui.theme import strip_ansi
        out = capsys.readouterr().out
        plain_out = strip_ansi(out)
        assert "Configured active provider: AgentRouter!" in plain_out
        assert "Active Model: claude-opus-5" in plain_out
        assert "Endpoint: https://agentrouter.org" in plain_out
