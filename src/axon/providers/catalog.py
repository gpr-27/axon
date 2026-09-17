"""
Multi-Provider Catalog and Preset Definitions for Local and Cloud AI engines.
"""
from __future__ import annotations
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class ProviderPreset:
    id: str
    name: str
    description: str
    category: Literal["Local (Offline & Free)", "Popular Cloud", "Custom"]
    base_url: str
    default_model: str
    models: list[str] = field(default_factory=list)
    env_var: str | None = None
    requires_key: bool = True
    api_format: Literal["openai", "anthropic"] = "openai"

def fetch_local_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Dynamically query local Ollama daemon for installed models (timeout 0.6s)."""
    try:
        clean_url = base_url.rstrip("/")
        if clean_url.endswith("/v1"):
            clean_url = clean_url[:-3]
        tags_url = f"{clean_url}/api/tags"
        req = urllib.request.Request(tags_url, headers={"User-Agent": "Axon-Client"})
        with urllib.request.urlopen(req, timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            return models
    except Exception:
        return []

PROVIDER_PRESETS: list[ProviderPreset] = [
    # ☁️ AgentRouter (Primary Recommended Default)
    ProviderPreset(
        id="agentrouter",
        name="AgentRouter",
        description="High performance proxy · DeepSeek, Claude Opus, GPT-5",
        category="Popular Cloud",
        base_url="https://agentrouter.org",
        default_model="deepseek-v4-flash",
        models=[
            "deepseek-v4-flash",
            "gpt-5.6-sol",
            "gpt-6-astra",
            "claude-opus-5",
            "claude-opus-4-8",
        ],
        env_var="AXON_API_KEY",
        requires_key=True,
        api_format="openai",
    ),

    # 🏠 Local Ollama
    ProviderPreset(
        id="ollama",
        name="Ollama (Local)",
        description="Local inference engine · 100% free & private (localhost:11434)",
        category="Local (Offline & Free)",
        base_url="http://localhost:11434/v1",
        default_model="qwen2.5-coder:7b",
        models=[
            "qwen2.5-coder:7b",
            "deepseek-r1:8b",
            "llama3.1:8b",
            "qwen2.5-coder:1.5b",
        ],
        env_var=None,
        requires_key=False,
        api_format="openai",
    ),

    # ✨ Google Gemini
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        description="Google AI Studio Gemini 2.0 Flash and Pro models",
        category="Popular Cloud",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.0-flash",
        models=[
            "gemini-2.0-flash",
            "gemini-2.0-pro-exp-02-05",
            "gemini-1.5-flash",
        ],
        env_var="GEMINI_API_KEY",
        requires_key=True,
        api_format="openai",
    ),

    # 🌐 OpenRouter
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        description="Unified gateway to 200+ models",
        category="Popular Cloud",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-3.7-sonnet",
        models=[
            "anthropic/claude-3.7-sonnet",
            "deepseek/deepseek-r1",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-001",
        ],
        env_var="OPENROUTER_API_KEY",
        requires_key=True,
        api_format="openai",
    ),

    # 🧠 OpenAI
    ProviderPreset(
        id="openai",
        name="OpenAI",
        description="Direct GPT-4o, o3-mini, and o1",
        category="Popular Cloud",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        models=[
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
            "o1",
        ],
        env_var="OPENAI_API_KEY",
        requires_key=True,
        api_format="openai",
    ),

    # 🤖 Anthropic
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        description="Direct Claude 3.7 Sonnet, 3.5 Sonnet, and Haiku",
        category="Popular Cloud",
        base_url="https://api.anthropic.com",
        default_model="claude-3-7-sonnet-20250219",
        models=[
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ],
        env_var="ANTHROPIC_API_KEY",
        requires_key=True,
        api_format="anthropic",
    ),
]

def is_provider_linked(preset: ProviderPreset, agent: Any = None) -> bool:
    """Check if a provider is already linked / configured (has valid API key or reachable local daemon)."""
    import os
    from pathlib import Path

    # 1. If currently active in agent settings
    if agent is not None and hasattr(agent, "settings") and agent.settings:
        active_url = agent.settings.base_url.rstrip("/").lower()
        preset_url = preset.base_url.rstrip("/").lower()
        if (preset_url in active_url or active_url in preset_url) and agent.settings.api_key:
            secret = agent.settings.api_key.get_secret_value() if hasattr(agent.settings.api_key, "get_secret_value") else str(agent.settings.api_key)
            if secret and secret not in ("", "local", "none", "null"):
                return True

    # 2. If requires key, check process environment and ~/.axon/.env for preset.env_var
    if preset.requires_key:
        target_vars = [preset.env_var] if preset.env_var else []
        if preset.id == "agentrouter":
            if "AXON_API_KEY" not in target_vars:
                target_vars.append("AXON_API_KEY")
            if "AGENTROUTER_API_KEY" not in target_vars:
                target_vars.append("AGENTROUTER_API_KEY")

        for var in target_vars:
            val = os.environ.get(var, "").strip()
            if val and not val.startswith("/") and val.lower() not in ("none", "null", "local", "skip"):
                return True

            env_file = Path.home() / ".axon" / ".env"
            if env_file.exists():
                try:
                    for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if "=" in line and line.strip().startswith(var):
                            k_val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if k_val and not k_val.startswith("/") and k_val.lower() not in ("none", "null", "local", "skip"):
                                return True
                except Exception:
                    pass
        return False
    else:
        # Local daemon without key (e.g. Ollama): check if reachable or has models
        if preset.id == "ollama":
            mods = fetch_local_ollama_models(preset.base_url)
            if mods:
                return True
            cfg_file = Path.home() / ".axon" / "config.toml"
            if cfg_file.exists():
                try:
                    import tomllib
                    with open(cfg_file, "rb") as f_cfg:
                        c = tomllib.load(f_cfg)
                        if "11434" in c.get("base_url", ""):
                            return True
                except Exception:
                    pass
            return False
        return True

def get_linked_providers(agent: Any = None) -> list[ProviderPreset]:
    """Returns list of ProviderPresets that are actively linked/configured."""
    linked = [p for p in PROVIDER_PRESETS if is_provider_linked(p, agent)]
    if not linked:
        # Always ensure agentrouter is available as default
        ar = get_preset_by_id("agentrouter")
        if ar:
            linked.append(ar)
    return linked

def get_models_for_provider(preset: ProviderPreset, agent: Any = None) -> list[str]:
    """Returns models available for a specific provider preset."""
    models: list[str] = []
    seen: set[str] = set()

    # Dynamic local models if Ollama
    if preset.id == "ollama":
        live_m = fetch_local_ollama_models(preset.base_url)
        for m in live_m:
            if m not in seen:
                seen.add(m)
                models.append(m)

    # Saved custom models for this provider in ~/.axon/config.toml
    try:
        from pathlib import Path
        cfg_file = Path.home() / ".axon" / "config.toml"
        if cfg_file.exists():
            import tomllib
            with open(cfg_file, "rb") as f_cfg:
                cfg = tomllib.load(f_cfg)
                for cm in cfg.get("custom_models", []):
                    if isinstance(cm, dict) and cm.get("provider") == preset.id:
                        m_name = cm.get("model", "").strip()
                        if m_name and m_name not in seen:
                            seen.add(m_name)
                            models.append(m_name)
                    elif isinstance(cm, str) and preset.id == "agentrouter":
                        if cm not in seen:
                            seen.add(cm)
                            models.append(cm)
    except Exception:
        pass

    # Preset standard models
    for m in preset.models:
        if m not in seen:
            seen.add(m)
            models.append(m)

    return models

def get_curated_model_choices(active_base_url: str = "") -> list[tuple[str, str, str]]:
    """
    Builds a clean, non-overwhelming list of curated models across ONLY LINKED providers.
    Returns list of tuples: (raw_model_id, provider_name, formatted_display_string).
    """
    # Identify active preset
    active_preset = find_preset_by_url(active_base_url) if active_base_url else None

    # Load custom_models from config.toml to check for user-saved provider models
    custom_prov_ids: set[str] = set()
    try:
        from pathlib import Path
        cfg_file = Path.home() / ".axon" / "config.toml"
        if cfg_file.exists():
            import tomllib
            with open(cfg_file, "rb") as f_cfg:
                cfg = tomllib.load(f_cfg)
                for cm in cfg.get("custom_models", []):
                    if isinstance(cm, dict) and cm.get("provider"):
                        custom_prov_ids.add(cm.get("provider").lower())
    except Exception:
        pass

    # Filter to only linked providers, active preset, or providers with saved custom models
    linked_presets = [p for p in PROVIDER_PRESETS if is_provider_linked(p) or p.id.lower() in custom_prov_ids]
    if active_preset and active_preset not in linked_presets:
        linked_presets.append(active_preset)
    if not linked_presets:
        ar = get_preset_by_id("agentrouter")
        linked_presets = [ar] if ar else PROVIDER_PRESETS[:1]

    # Active preset first
    if active_preset and active_preset in linked_presets:
        linked_presets.remove(active_preset)
        linked_presets.insert(0, active_preset)

    choices: list[tuple[str, str, str]] = []
    seen_models: set[str] = set()

    for p in linked_presets:
        p_label = p.name.split(" ")[0] if not p.name.startswith("Ollama") else "Ollama"
        for m in get_models_for_provider(p):
            if m not in seen_models:
                seen_models.add(m)
                disp = f"[{p_label:<13}]  {m}"
                choices.append((m, p_label, disp))

    return choices

def get_preset_by_id(preset_id: str) -> ProviderPreset | None:
    for p in PROVIDER_PRESETS:
        if p.id.lower() == preset_id.lower():
            return p
    return None

def find_preset_for_model(model_name: str) -> ProviderPreset | None:
    """Find the provider preset that naturally hosts this model."""
    m_clean = model_name.strip()

    # 1. Exact match in preset models (e.g. OpenRouter's "openai/gpt-4o", "anthropic/claude-3.7-sonnet")
    for p in PROVIDER_PRESETS:
        if m_clean in p.models or m_clean == p.default_model:
            return p

    # Explicit provider overrides
    if m_clean.lower().startswith("ollama/"):
        return get_preset_by_id("ollama")
    if m_clean.lower().startswith("gemini/"):
        return get_preset_by_id("gemini")
    if m_clean.lower().startswith("agentrouter/"):
        return get_preset_by_id("agentrouter")

    # 2. Any model identifier with "/" is an OpenRouter namespace model (e.g. "meta-llama/...", "openai/...", "anthropic/...")
    if "/" in m_clean:
        return get_preset_by_id("openrouter")

    # 3. Ollama model tags (e.g. "name:tag" like "qwen2.5-coder:1.5b", "mistral:7b")
    if ":" in m_clean and not m_clean.startswith("http"):
        return get_preset_by_id("ollama")

    # 4. Anthropic models
    if m_clean.startswith("claude-") and not any(m_clean in p.models for p in PROVIDER_PRESETS if p.id == "agentrouter"):
        return get_preset_by_id("anthropic")

    # 5. OpenAI models
    if m_clean.startswith(("gpt-", "o1", "o3")):
        agentrouter_preset = get_preset_by_id("agentrouter")
        if agentrouter_preset and m_clean in agentrouter_preset.models:
            return agentrouter_preset
        return get_preset_by_id("openai")

    # 6. DeepSeek official
    if m_clean.startswith("deepseek-chat") or m_clean.startswith("deepseek-reasoner"):
        return get_preset_by_id("deepseek")

    # 7. Gemini official
    if m_clean.startswith("gemini-"):
        return get_preset_by_id("gemini")

    return None

def find_preset_by_url(base_url: str) -> ProviderPreset | None:
    """Find the provider preset matching a base URL."""
    clean_u = base_url.rstrip("/").lower()
    for p in PROVIDER_PRESETS:
        if p.base_url.rstrip("/").lower() in clean_u or clean_u in p.base_url.rstrip("/").lower():
            return p
    return None

