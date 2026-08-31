"""
Provider router and verified pricing registry.
"""
from __future__ import annotations
from typing import Type
from axon.config import Settings
from axon.providers.base import Provider
from axon.providers.anthropic import AnthropicProvider
from axon.providers.openai_compat import OpenAICompatProvider

PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 2.00, "output": 6.00},
    "gpt-5.6-sol":       {"input": 3.00, "output": 15.00},
    "claude-opus-5":     {"input": 8.00, "output": 40.00},
    "claude-opus-4-8":   {"input": 8.00, "output": 40.00},
    "glm-5.3":           {"input": 3.00, "output": 12.00},
}

# Verified Model Context Windows & Max Output Limits
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-5":     1_000_000,    # 1 Million token context window
    "claude-opus-4-8":   1_000_000,    # 1 Million token context window
    "deepseek-v4-flash": 1_000_000,    # 1 Million token context window
    "gpt-5.6-sol":       1_000_000,    # 1 Million token context window
    "glm-5.3":           1_000_000,    # 1 Million token context window
}

MODEL_MAX_OUTPUT: dict[str, int] = {
    "claude-opus-5":     128_000,
    "claude-opus-4-8":   128_000,
    "deepseek-v4-flash": 128_000,
    "gpt-5.6-sol":       128_000,
    "glm-5.3":           128_000,
}

ANTHROPIC_MODELS = {"claude-opus-5", "claude-opus-4-8"}

def get_context_window(model: str, default: int = 1_000_000) -> int:
    """Return context window capacity for the given model."""
    return MODEL_CONTEXT_LIMITS.get(model, default)

def get_max_output(model: str, default: int = 64_000) -> int:
    """Return max completion tokens for the given model."""
    return MODEL_MAX_OUTPUT.get(model, default)

def provider_for(model: str, settings: Settings) -> Provider:
    """Resolve and instantiate the appropriate provider for the requested model."""
    if model in ANTHROPIC_MODELS:
        return AnthropicProvider(settings)
    return OpenAICompatProvider(settings)

def known_models() -> list[str]:
    return list(PRICING.keys())
