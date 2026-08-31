"""
Multi-Provider Catalog and Preset Definitions for Local and Cloud AI engines.
"""
from __future__ import annotations
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

PROVIDER_PRESETS: list[ProviderPreset] = [
    # 🏠 Local Providers (Zero API Key, Private & Offline)
    ProviderPreset(
        id="ollama",
        name="Ollama (Local)",
        description="Local inference engine · 100% free & private (localhost:11434)",
        category="Local (Offline & Free)",
        base_url="http://localhost:11434/v1",
        default_model="qwen2.5-coder:32b",
        models=[
            "qwen2.5-coder:32b",
            "qwen2.5-coder:14b",
            "qwen2.5-coder:7b",
            "deepseek-r1:14b",
            "deepseek-r1:8b",
            "deepseek-r1:32b",
            "llama3.3:70b",
            "mistral-nemo:12b",
            "codellama:34b",
        ],
        env_var=None,
        requires_key=False,
        api_format="openai",
    ),
    ProviderPreset(
        id="lmstudio",
        name="LM Studio / vLLM (Local)",
        description="Local GUI & vLLM server · Zero key required (localhost:1234)",
        category="Local (Offline & Free)",
        base_url="http://localhost:1234/v1",
        default_model="local-model",
        models=["local-model", "qwen2.5-coder", "deepseek-r1", "llama-3.3"],
        env_var=None,
        requires_key=False,
        api_format="openai",
    ),

    # ☁️ Popular Cloud Providers
    ProviderPreset(
        id="agentrouter",
        name="AgentRouter (Recommended Default)",
        description="Low latency proxy · DeepSeek, Claude Opus, GPT-5",
        category="Popular Cloud",
        base_url="https://agentrouter.org",
        default_model="deepseek-v4-flash",
        models=["deepseek-v4-flash", "gpt-5.6-sol", "claude-opus-5", "claude-opus-4-8", "glm-5.3"],
        env_var="AXON_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        description="Unified gateway to 200+ top open & proprietary models",
        category="Popular Cloud",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-3.7-sonnet",
        models=[
            "anthropic/claude-3.7-sonnet",
            "deepseek/deepseek-r1",
            "openai/gpt-4o",
            "google/gemini-2.0-flash-001",
            "meta-llama/llama-3.3-70b-instruct",
            "qwen/qwen-2.5-coder-32b-instruct",
        ],
        env_var="OPENROUTER_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic (Official API)",
        description="Direct Claude 3.7 Sonnet, 3.5 Sonnet, and Opus",
        category="Popular Cloud",
        base_url="https://api.anthropic.com",
        default_model="claude-3-7-sonnet-20250219",
        models=[
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-5-haiku-20241022",
        ],
        env_var="ANTHROPIC_API_KEY",
        requires_key=True,
        api_format="anthropic",
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI (Official API)",
        description="Direct GPT-4o, o1, o3-mini, and GPT-4.5",
        category="Popular Cloud",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        models=["gpt-4o", "gpt-4o-mini", "o1", "o3-mini", "gpt-4.5-preview"],
        env_var="OPENAI_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek (Official API)",
        description="DeepSeek-V3 Chat and DeepSeek-R1 Reasoner direct",
        category="Popular Cloud",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=["deepseek-chat", "deepseek-reasoner"],
        env_var="DEEPSEEK_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        description="Google AI Studio Gemini 2.0 Flash and Pro models",
        category="Popular Cloud",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.0-flash",
        models=["gemini-2.0-flash", "gemini-2.0-pro-exp-02-05", "gemini-1.5-pro", "gemini-1.5-flash"],
        env_var="GEMINI_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="groq",
        name="Groq Cloud",
        description="LPU-powered ultra-fast inference (<50ms TTFT)",
        category="Popular Cloud",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        models=[
            "llama-3.3-70b-versatile",
            "deepseek-r1-distill-llama-70b",
            "mixtral-8x7b-32768",
            "qwen-2.5-coder-32b",
        ],
        env_var="GROQ_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
    ProviderPreset(
        id="together",
        name="Together AI",
        description="Cloud endpoint for open-source Llama, Qwen, and DeepSeek",
        category="Popular Cloud",
        base_url="https://api.together.xyz/v1",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        models=[
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        ],
        env_var="TOGETHER_API_KEY",
        requires_key=True,
        api_format="openai",
    ),
]

def get_preset_by_id(preset_id: str) -> ProviderPreset | None:
    for p in PROVIDER_PRESETS:
        if p.id.lower() == preset_id.lower():
            return p
    return None
