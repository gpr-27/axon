"""
Unit tests for multi-provider catalog, local provider zero-key bypass, and preset resolution.
"""
from __future__ import annotations
import os
from pathlib import Path
from pydantic import SecretStr
from axon.config import Settings
from axon.providers.catalog import PROVIDER_PRESETS, get_preset_by_id
from axon.providers.registry import provider_for
from axon.providers.anthropic import AnthropicProvider
from axon.providers.openai_compat import OpenAICompatProvider

def test_provider_presets_catalog():
    preset_ids = [p.id for p in PROVIDER_PRESETS]
    assert "ollama" in preset_ids
    assert "lmstudio" in preset_ids
    assert "agentrouter" in preset_ids
    assert "openrouter" in preset_ids
    assert "anthropic" in preset_ids
    assert "openai" in preset_ids
    assert "deepseek" in preset_ids
    assert "gemini" in preset_ids
    assert "groq" in preset_ids

def test_local_ollama_zero_key_bypass():
    ollama_preset = get_preset_by_id("ollama")
    assert ollama_preset is not None
    assert ollama_preset.requires_key is False
    assert ollama_preset.base_url == "http://localhost:11434/v1"

    # Loading settings with localhost base_url should not raise ConfigError even with empty API key
    settings = Settings(
        base_url=ollama_preset.base_url,
        model=ollama_preset.default_model,
        api_key=SecretStr(""),
    )
    # Validate provider resolution
    prov = provider_for(settings.model, settings)
    assert isinstance(prov, OpenAICompatProvider)
    assert prov.settings.base_url == "http://localhost:11434/v1"

def test_anthropic_provider_resolution():
    anthropic_preset = get_preset_by_id("anthropic")
    assert anthropic_preset is not None
    
    settings = Settings(
        base_url=anthropic_preset.base_url,
        model="claude-3-7-sonnet-20250219",
        api_key=SecretStr("sk-ant-test"),
    )
    prov = provider_for(settings.model, settings)
    assert isinstance(prov, AnthropicProvider)

def test_openrouter_provider_resolution():
    openrouter_preset = get_preset_by_id("openrouter")
    assert openrouter_preset is not None
    
    settings = Settings(
        base_url=openrouter_preset.base_url,
        model=openrouter_preset.default_model,
        api_key=SecretStr("sk-or-test"),
    )
    prov = provider_for(settings.model, settings)
    assert isinstance(prov, OpenAICompatProvider)
    assert prov.settings.base_url == "https://openrouter.ai/api/v1"
