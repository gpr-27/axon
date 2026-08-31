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
    assert "agentrouter" in preset_ids
    assert "openrouter" in preset_ids
    assert "anthropic" in preset_ids
    assert "openai" in preset_ids
    assert "gemini" in preset_ids

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


def test_small_lightweight_models_catalog():
    ollama_preset = get_preset_by_id("ollama")
    assert ollama_preset is not None
    # Check that compact / small models are present in preset
    assert "qwen2.5-coder:1.5b" in ollama_preset.models
    assert "qwen2.5-coder:7b" in ollama_preset.models
    assert "llama3.1:8b" in ollama_preset.models


def test_handle_model_custom_and_random(tmp_path: Path):
    from axon.agent.loop import Agent
    from axon.agent.context import ContextManager
    from axon.commands.builtin import handle_model
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.permissions.engine import PermissionEngine
    from axon.tools.registry import create_default_registry

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

    # 1. Custom model name
    handle_model(agent, "my-custom-finetuned-llama:latest")
    assert agent.settings.model == "my-custom-finetuned-llama:latest"

    # 2. Random model selection
    handle_model(agent, "random")
    assert agent.settings.model != ""

    # 3. Random small model selection
    handle_model(agent, "random:small")
    assert agent.settings.model != ""

def test_handle_env_command(tmp_path: Path, monkeypatch):
    from axon.agent.loop import Agent
    from axon.agent.context import ContextManager
    from axon.commands.builtin import handle_env, handle_keys, dispatch_command
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.permissions.engine import PermissionEngine
    from axon.tools.registry import create_default_registry

    monkeypatch.setenv("AXON_API_KEY", "sk-agentrouter-test-key-12345")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-groq-test-key-67890")

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

    res = handle_env(agent, "")
    assert res.handled is True

    res_keys = handle_keys(agent, "")
    assert res_keys.handled is True

    res2 = dispatch_command(agent, "/keys")
    assert res2.handled is True

    res3 = dispatch_command(agent, "/env")
    assert res3.handled is True

def test_one_step_model_switch_endpoint_sync(tmp_path: Path, monkeypatch):
    from axon.agent.loop import Agent
    from axon.agent.context import ContextManager
    from axon.commands.builtin import handle_model
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.permissions.engine import PermissionEngine
    from axon.tools.registry import create_default_registry

    monkeypatch.setenv("AXON_API_KEY", "sk-agentrouter-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")

    settings = Settings(workspace=tmp_path, base_url="https://agentrouter.org", model="deepseek-v4-flash")
    agent = Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )

    # 1. Switch to Ollama local model -> Endpoint must update to localhost:11434/v1
    handle_model(agent, "qwen2.5-coder:7b")
    assert agent.settings.model == "qwen2.5-coder:7b"
    assert "11434" in agent.settings.base_url
    assert agent.settings.api_key.get_secret_value() == "local"

    # 2. Switch to AgentRouter model -> Endpoint must update to agentrouter.org
    handle_model(agent, "claude-opus-5")
    assert agent.settings.model == "claude-opus-5"
    assert "agentrouter.org" in agent.settings.base_url
    assert agent.settings.api_key.get_secret_value() == "sk-agentrouter-test"

    # 3. Switch to OpenRouter model (e.g. openai/gpt-4o) -> Must route to OpenRouter with OPENROUTER_API_KEY
    handle_model(agent, "openai/gpt-4o")
    assert agent.settings.model == "openai/gpt-4o"
    assert "openrouter.ai" in agent.settings.base_url
    assert agent.settings.api_key.get_secret_value() == "sk-or-test-key"

    # 4. Switch to direct OpenAI model (gpt-4o) -> Endpoint must update to OpenAI and model to gpt-4o
    handle_model(agent, "gpt-4o")
    assert agent.settings.model == "gpt-4o"
    assert "api.openai.com" in agent.settings.base_url
    assert agent.settings.api_key.get_secret_value() == "sk-openai-test"

def test_handle_key_command_direct_update(tmp_path: Path, monkeypatch):
    from axon.agent.loop import Agent
    from axon.agent.context import ContextManager
    from axon.commands.builtin import handle_keys, dispatch_command
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.permissions.engine import PermissionEngine
    from axon.tools.registry import create_default_registry

    settings = Settings(workspace=tmp_path, base_url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-2.0-flash")
    agent = Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )

    # Update Gemini key via `/key gemini AIzaSy_test_12345`
    res = dispatch_command(agent, "/key gemini AIzaSy_test_12345")
    assert res.handled is True
    assert os.environ.get("GEMINI_API_KEY") == "AIzaSy_test_12345"
    assert agent.settings.api_key.get_secret_value() == "AIzaSy_test_12345"


def test_custom_models_provider_specific(tmp_path: Path, monkeypatch):
    import tomli_w
    from axon.providers.catalog import get_curated_model_choices

    cfg_dir = tmp_path / ".axon"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.toml"

    custom_entries = [
        {"model": "my-custom-or-model", "provider": "openrouter"},
        {"model": "my-custom-ollama-model", "provider": "ollama"},
    ]
    with open(cfg_file, "wb") as f:
        tomli_w.dump({"custom_models": custom_entries}, f)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    choices = get_curated_model_choices("https://openrouter.ai/api/v1")
    disp_map = {m: (p, d) for m, p, d in choices}

    assert "my-custom-or-model" in disp_map
    assert "OpenRouter" in disp_map["my-custom-or-model"][0]
    assert "my-custom-ollama-model" in disp_map
    assert "Ollama" in disp_map["my-custom-ollama-model"][0]


