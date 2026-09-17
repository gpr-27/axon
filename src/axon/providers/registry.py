"""
Provider router and verified pricing registry.
"""
from __future__ import annotations
from typing import Type
from axon.config import Settings
from axon.providers.base import Provider
from axon.providers.anthropic import AnthropicProvider
from axon.providers.openai_compat import OpenAICompatProvider

from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, get_preset_by_id

PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 4.00, "output": 12.00, "cache_read": 0.80, "cache_write": 0.0},
    "gpt-5.6-sol":       {"input": 3.00, "output": 15.00, "cache_read": 0.60},
    "gpt-6-astra":       {"input": 3.00, "output": 15.00, "cache_read": 0.60},
    "claude-opus-5":     {"input": 6.00, "output": 30.00, "cache_read": 1.20, "cache_write": 7.50},
    "claude-opus-4-8":   {"input": 8.00, "output": 40.00, "cache_read": 1.60, "cache_write": 10.00},
    "claude-3-7-sonnet-20250219": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "gpt-4o":            {"input": 2.50, "output": 10.00, "cache_read": 1.25},
    "deepseek-chat":     {"input": 0.27, "output": 1.10, "cache_read": 0.07},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19, "cache_read": 0.14},
    "gemini-2.0-flash":  {"input": 0.10, "output": 0.40, "cache_read": 0.025},
}

_AGENTROUTER_SYNCED = False

def sync_agentrouter_pricing(base_url: str = "https://agentrouter.org") -> bool:
    """Dynamically query AgentRouter /api/pricing to update exact live pricing ratios."""
    global _AGENTROUTER_SYNCED
    try:
        import urllib.request
        import json
        clean = base_url.rstrip("/")
        if clean.endswith("/v1"):
            clean = clean[:-3]
        if not ("agentrouter.org" in clean or "air-outer.com" in clean):
            return False
        pricing_url = f"{clean}/api/pricing"
        req = urllib.request.Request(pricing_url, headers={"User-Agent": "Axon-Client/1.0"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            for item in items:
                name = item.get("model_name")
                if not name:
                    continue
                m_ratio = float(item.get("model_ratio", 1.0))
                c_ratio = float(item.get("completion_ratio", 1.0))
                in_p = m_ratio * 2.0
                out_p = in_p * c_ratio
                cache_read_p = in_p * 0.2
                cache_write_p = 0.0 if "deepseek" in name.lower() else in_p * 1.25
                PRICING[name] = {
                    "input": round(in_p, 4),
                    "output": round(out_p, 4),
                    "cache_read": round(cache_read_p, 4),
                    "cache_write": round(cache_write_p, 4),
                }
            _AGENTROUTER_SYNCED = True
            return True
    except Exception:
        return False

# Verified Model Context Windows & Max Output Limits
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-5":     1_000_000,    # 1 Million token context window
    "claude-opus-4-8":   1_000_000,    # 1 Million token context window
    "deepseek-v4-flash": 1_000_000,    # 1 Million token context window
    "gpt-5.6-sol":       1_000_000,    # 1 Million token context window
    "gpt-6-astra":       1_000_000,    # 1 Million token context window
}

MODEL_MAX_OUTPUT: dict[str, int] = {
    "claude-opus-5":     128_000,
    "claude-opus-4-8":   128_000,
    "deepseek-v4-flash": 128_000,
    "gpt-5.6-sol":       128_000,
    "gpt-6-astra":       128_000,
}

ANTHROPIC_MODELS = {
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
}

def get_context_window(model: str, default: int = 1_000_000) -> int:
    """Return context window capacity for the given model."""
    return MODEL_CONTEXT_LIMITS.get(model, default)

def get_max_output(model: str, default: int = 64_000) -> int:
    """Return max completion tokens for the given model."""
    return MODEL_MAX_OUTPUT.get(model, default)

def provider_for(model: str, settings: Settings) -> Provider:
    """Resolve and instantiate the appropriate provider for the requested model."""
    base_lower = settings.base_url.lower().rstrip("/")
    if not _AGENTROUTER_SYNCED and ("agentrouter" in base_lower or "air-outer" in base_lower):
        sync_agentrouter_pricing(settings.base_url)
    if "api.anthropic.com" in base_lower:
        return AnthropicProvider(settings)
    if model in ANTHROPIC_MODELS and "agentrouter" not in base_lower and "openrouter" not in base_lower:
        return AnthropicProvider(settings)
    return OpenAICompatProvider(settings)

def known_models() -> list[str]:
    models: list[str] = []
    for p in PROVIDER_PRESETS:
        for m in p.models:
            if m not in models:
                models.append(m)
    return models

