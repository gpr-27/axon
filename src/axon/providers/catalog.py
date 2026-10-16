"""
Multi-provider catalog: official presets (base URL, auth style, key env vars, default model, models)
plus API-key discovery/persistence helpers shared by the pickers and the router.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

AuthStyle = Literal["bearer", "x-api-key", "api-key", "none"]


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
    env_vars: tuple[str, ...] = ()
    auth_style: AuthStyle = "bearer"
    # Model ids on this provider are namespaced ("org/model"), so a "<provider>/" prefix is NOT a route hint.
    namespaced_models: bool = False
    # Provider rejects non-standard message fields (reasoning_content, cache_control) in requests.
    strict_messages: bool = False
    docs_url: str = ""
    api_version: str | None = None

    def __post_init__(self) -> None:
        if not self.env_vars and self.env_var:
            self.env_vars = (self.env_var,)
        if self.env_vars and not self.env_var:
            self.env_var = self.env_vars[0]
        if not self.requires_key:
            self.auth_style = "none" if self.auth_style == "bearer" else self.auth_style


def _local_models(url: str, path: str, key: str, name_key: str, timeout: float) -> list[str]:
    try:
        clean = url.rstrip("/").removesuffix("/v1")
        req = urllib.request.Request(f"{clean}{path}", headers={"User-Agent": "axon"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get(key, []) if isinstance(data, dict) else []
        return [str(m.get(name_key)) for m in items if isinstance(m, dict) and m.get(name_key)]
    except Exception:
        return []


def fetch_local_ollama_models(base_url: str = "http://localhost:11434", timeout: float = 0.6) -> list[str]:
    """Query the local Ollama daemon (/api/tags) for installed models."""
    return _local_models(base_url, "/api/tags", "models", "name", timeout)


def fetch_local_lmstudio_models(base_url: str = "http://localhost:1234/v1", timeout: float = 0.6) -> list[str]:
    """Query a running LM Studio server (/v1/models) for loaded models."""
    return _local_models(base_url, "/v1/models", "data", "id", timeout)


PROVIDER_PRESETS: list[ProviderPreset] = [
    # AgentRouter (primary default)
    ProviderPreset(
        id="agentrouter",
        name="AgentRouter",
        description="High performance proxy · DeepSeek V4, Claude Opus 5.5, GPT-6",
        category="Popular Cloud",
        base_url="https://agentrouter.org",
        default_model="deepseek-v4-flash",
        # The models AgentRouter serves today (its /api/pricing feed, 2026-10-16).
        models=["deepseek-v4-flash", "claude-opus-5-5", "gpt-6.1-sol", "gpt-6-astra"],
        env_var="AXON_API_KEY",
        env_vars=("AXON_API_KEY", "AGENTROUTER_API_KEY"),
        docs_url="https://agentrouter.org/console",
    ),
    ProviderPreset(
        id="ollama",
        name="Ollama (Local)",
        description="Local inference engine · 100% free & private (localhost:11434)",
        category="Local (Offline & Free)",
        base_url="http://localhost:11434/v1",
        default_model="qwen2.5-coder:7b",
        models=["qwen2.5-coder:7b", "deepseek-r1:8b", "llama3.1:8b", "qwen2.5-coder:1.5b"],
        requires_key=False,
        namespaced_models=True,
        docs_url="https://ollama.com",
    ),
    ProviderPreset(
        id="lmstudio",
        name="LM Studio (Local)",
        description="Local OpenAI-compatible server · free & private (localhost:1234)",
        category="Local (Offline & Free)",
        base_url="http://localhost:1234/v1",
        default_model="qwen/qwen3-coder-30b",
        models=["qwen/qwen3-coder-30b", "openai/gpt-oss-20b", "mistralai/devstral-small-2507"],
        requires_key=False,
        namespaced_models=True,
        docs_url="https://lmstudio.ai/docs/app/api",
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        description="Google AI Studio Gemini 2.5 Pro / Flash (OpenAI-compatible endpoint)",
        category="Popular Cloud",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.5-flash",
        models=["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"],
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        strict_messages=True,
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        description="Unified gateway to 300+ models",
        category="Popular Cloud",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-4.5",
        models=[
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-3.7-sonnet",
            "deepseek/deepseek-r1",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
            "google/gemini-2.0-flash-001",
            "x-ai/grok-4",
            "meta-llama/llama-3.3-70b-instruct",
        ],
        env_vars=("OPENROUTER_API_KEY",),
        namespaced_models=True,
        docs_url="https://openrouter.ai/docs",
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        description="Direct GPT-5, GPT-4.1, GPT-4o, o3 and o4-mini",
        category="Popular Cloud",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5",
        models=["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini", "o3-mini"],
        env_vars=("OPENAI_API_KEY",),
        strict_messages=True,
        docs_url="https://platform.openai.com/docs/api-reference/chat",
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        description="Direct Claude Opus, Sonnet and Haiku (native Messages API)",
        category="Popular Cloud",
        base_url="https://api.anthropic.com",
        default_model="claude-opus-5-5",
        models=[
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        ],
        env_vars=("ANTHROPIC_API_KEY",),
        api_format="anthropic",
        auth_style="x-api-key",
        docs_url="https://docs.anthropic.com/en/api/messages",
    ),
    ProviderPreset(
        id="xai",
        name="xAI Grok",
        description="Grok 4 and Grok Code Fast",
        category="Popular Cloud",
        base_url="https://api.x.ai/v1",
        default_model="grok-4",
        models=["grok-4", "grok-4-fast-reasoning", "grok-code-fast-1", "grok-3-mini"],
        env_vars=("XAI_API_KEY",),
        strict_messages=True,
        docs_url="https://docs.x.ai/docs/api-reference",
    ),
    ProviderPreset(
        id="groq",
        name="Groq",
        description="Ultra-fast LPU inference · Llama, GPT-OSS, Kimi K2",
        category="Popular Cloud",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b"],
        env_vars=("GROQ_API_KEY",),
        namespaced_models=True,
        strict_messages=True,
        docs_url="https://console.groq.com/docs/openai",
    ),
    ProviderPreset(
        id="mistral",
        name="Mistral AI",
        description="Mistral Large / Medium, Codestral and Devstral",
        category="Popular Cloud",
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-large-latest",
        models=["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest", "codestral-latest", "devstral-medium-latest"],
        env_vars=("MISTRAL_API_KEY",),
        strict_messages=True,
        docs_url="https://docs.mistral.ai/api/",
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        description="DeepSeek official API · deepseek-chat and deepseek-reasoner",
        category="Popular Cloud",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        models=["deepseek-chat", "deepseek-reasoner"],
        env_vars=("DEEPSEEK_API_KEY",),
        docs_url="https://api-docs.deepseek.com",
    ),
    ProviderPreset(
        id="together",
        name="Together AI",
        description="Open models at scale · Llama, Qwen Coder, DeepSeek",
        category="Popular Cloud",
        base_url="https://api.together.xyz/v1",
        default_model="Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
        models=[
            "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        ],
        env_vars=("TOGETHER_API_KEY",),
        namespaced_models=True,
        docs_url="https://docs.together.ai/docs/openai-api-compatibility",
    ),
    ProviderPreset(
        id="fireworks",
        name="Fireworks AI",
        description="Fast open-model serving · Qwen3 Coder, DeepSeek, Llama",
        category="Popular Cloud",
        base_url="https://api.fireworks.ai/inference/v1",
        default_model="accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
        models=[
            "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
        ],
        env_vars=("FIREWORKS_API_KEY",),
        namespaced_models=True,
        docs_url="https://docs.fireworks.ai/tools-sdks/openai-compatibility",
    ),
    ProviderPreset(
        id="cerebras",
        name="Cerebras",
        description="Wafer-scale inference · Llama 3.3, Qwen 3 Coder, GPT-OSS",
        category="Popular Cloud",
        base_url="https://api.cerebras.ai/v1",
        default_model="qwen-3-coder-480b",
        models=["qwen-3-coder-480b", "llama-3.3-70b", "llama3.1-8b", "gpt-oss-120b"],
        env_vars=("CEREBRAS_API_KEY",),
        strict_messages=True,
        docs_url="https://inference-docs.cerebras.ai",
    ),
    ProviderPreset(
        id="azure",
        name="Azure OpenAI",
        description="Your Azure OpenAI deployment (set AZURE_OPENAI_ENDPOINT; model = deployment name)",
        category="Custom",
        base_url="https://YOUR-RESOURCE.openai.azure.com",
        default_model="gpt-4o",
        models=["gpt-4o", "gpt-4.1", "gpt-5", "o4-mini"],
        env_vars=("AZURE_OPENAI_API_KEY",),
        auth_style="api-key",
        strict_messages=True,
        docs_url="https://learn.microsoft.com/azure/ai-services/openai/reference",
        api_version="2024-10-21",
    ),
]


# ─── API key discovery / persistence ───────────────────────────────────────
_INVALID_KEYS = {"", "none", "null", "local", "skip", "sk-placeholder", "your_api_key_here", "your-api-key-here", "replace_me"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _user_file(name: str) -> Path:
    from axon.storage import user_file
    return user_file(name)


def axon_env_file() -> Path:
    """User-level key file (storage.user_file(".env"), i.e. ~/.axon/.env unless AXON_HOME is set)."""
    return _user_file(".env")


def axon_config_file() -> Path:
    return _user_file("config.toml")


def _clean_value(raw: str) -> str:
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v.strip()


def _valid_key(v: str | None) -> bool:
    return bool(v) and not v.startswith("/") and v.lower() not in _INVALID_KEYS  # type: ignore[union-attr]


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Parse a dotenv file (KEY=value, optional `export`, quotes, comments). Missing file -> {}."""
    p = path or axon_env_file()
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        k, v = s.split("=", 1)
        k = k.strip()
        if _ENV_NAME.match(k):
            out[k] = _clean_value(v)
    return out


def find_api_key(env_vars: list[str] | tuple[str, ...]) -> str | None:
    """First usable key among `env_vars`: process environment first, then ~/.axon/.env (exact names)."""
    names = [v for v in env_vars if v]
    for var in names:
        val = os.environ.get(var, "").strip()
        if _valid_key(val):
            return val
    file_vals = read_env_file()
    for var in names:
        val = file_vals.get(var, "")
        if _valid_key(val):
            return val
    return None


def save_api_key(env_var: str, value: str) -> Path:
    """Persist `env_var=value` into ~/.axon/.env atomically (chmod 600 where supported).

    Replaces only the line whose key is exactly `env_var`; also exports it to os.environ.
    """
    if not _ENV_NAME.match(env_var or ""):
        raise ValueError(f"Invalid environment variable name: {env_var!r}")
    clean = _clean_value(value or "")
    if not clean or "\n" in clean or "\r" in clean:
        raise ValueError("API key must be a non-empty single line")
    path = axon_env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    replaced = False
    try:
        existing = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        existing = []
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(env_var)}\s*=")
    for line in existing:
        if pat.match(line):
            if not replaced:
                lines.append(f'{env_var}="{clean}"')
                replaced = True
            continue
        lines.append(line)
    if not replaced:
        lines.append(f'{env_var}="{clean}"')
    payload = "\n".join(lines).strip("\n") + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ[env_var] = clean
    return path


def effective_base_url(preset: ProviderPreset) -> str:
    """Preset base URL, with Azure's endpoint taken from AZURE_OPENAI_ENDPOINT when set."""
    if preset.id == "azure":
        ep = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or read_env_file().get("AZURE_OPENAI_ENDPOINT", "")
        if ep:
            return ep.rstrip("/")
    return preset.base_url


# ─── Linkage / model listing ───────────────────────────────────────────────
def is_provider_linked(preset: ProviderPreset, agent: Any = None) -> bool:
    """True if a provider is configured: active with a key, has its own key, or a local daemon answers."""
    if agent is not None and getattr(agent, "settings", None):
        settings = agent.settings
        active = find_preset_by_url(getattr(settings, "base_url", "") or "")
        if active is not None and active.id == preset.id:
            key = getattr(settings, "api_key", None)
            secret = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
            if not preset.requires_key or _valid_key(secret):
                return True

    if preset.requires_key:
        if find_api_key(list(preset.env_vars)) is None:
            return False
        if preset.id == "azure":
            return effective_base_url(preset) != preset.base_url
        return True

    if preset.id == "ollama":
        if fetch_local_ollama_models(preset.base_url):
            return True
        return _config_points_at(":11434")
    if preset.id == "lmstudio":
        if fetch_local_lmstudio_models(preset.base_url):
            return True
        return _config_points_at(":1234")
    return True


def _config_points_at(fragment: str) -> bool:
    cfg_file = axon_config_file()
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return False
    try:
        with open(cfg_file, "rb") as f_cfg:
            return fragment in str(tomllib.load(f_cfg).get("base_url", ""))
    except Exception:
        return False


def _load_custom_models() -> list[Any]:
    cfg_file = axon_config_file()
    if not cfg_file.exists():
        return []
    try:
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]
        with open(cfg_file, "rb") as f_cfg:
            cm = tomllib.load(f_cfg).get("custom_models", [])
        return cm if isinstance(cm, list) else []
    except Exception:
        return []


def get_linked_providers(agent: Any = None) -> list[ProviderPreset]:
    """ProviderPresets that are actively linked/configured (AgentRouter always offered as fallback)."""
    linked = [p for p in PROVIDER_PRESETS if is_provider_linked(p, agent)]
    if not linked:
        ar = get_preset_by_id("agentrouter")
        if ar:
            linked.append(ar)
    return linked


def get_models_for_provider(preset: ProviderPreset, agent: Any = None) -> list[str]:
    """Models for one provider: live local models, user-saved custom models, then preset models."""
    models: list[str] = []
    seen: set[str] = set()

    def add(m: str) -> None:
        if m and m not in seen:
            seen.add(m)
            models.append(m)

    if preset.id == "ollama":
        for m in fetch_local_ollama_models(preset.base_url):
            add(m)
    elif preset.id == "lmstudio":
        for m in fetch_local_lmstudio_models(preset.base_url):
            add(m)

    for cm in _load_custom_models():
        if isinstance(cm, dict) and cm.get("provider") == preset.id:
            add(str(cm.get("model", "")).strip())
        elif isinstance(cm, str) and preset.id == "agentrouter":
            add(cm.strip())

    for m in preset.models:
        add(m)
    return models


def get_curated_model_choices(active_base_url: str = "") -> list[tuple[str, str, str]]:
    """(model_id, provider_label, display) across linked providers, active provider first."""
    active_preset = find_preset_by_url(active_base_url) if active_base_url else None
    custom_prov_ids = {
        str(cm.get("provider")).lower() for cm in _load_custom_models() if isinstance(cm, dict) and cm.get("provider")
    }

    linked_presets = [p for p in PROVIDER_PRESETS if is_provider_linked(p) or p.id.lower() in custom_prov_ids]
    if active_preset and active_preset not in linked_presets:
        linked_presets.append(active_preset)
    if not linked_presets:
        ar = get_preset_by_id("agentrouter")
        linked_presets = [ar] if ar else PROVIDER_PRESETS[:1]
    if active_preset and active_preset in linked_presets:
        linked_presets.remove(active_preset)
        linked_presets.insert(0, active_preset)

    choices: list[tuple[str, str, str]] = []
    seen_models: set[str] = set()
    for p in linked_presets:
        p_label = p.name.split(" (")[0] if "(" in p.name else p.name.split(" ")[0]
        for m in get_models_for_provider(p):
            if m not in seen_models:
                seen_models.add(m)
                choices.append((m, p_label, f"[{p_label:<13}]  {m}"))
    return choices


# ─── Model aliases ─────────────────────────────────────────────────────────
# Family aliases resolve to the newest model of that family; "opusplan" = Opus in plan mode, else Sonnet.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-1",
    "best": "claude-fable-5-1",
    "default": "claude-opus-5-5",
}
_ALIAS_FAMILY = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku", "fable": "fable", "best": "fable", "default": "opus"}
ONE_M_SUFFIX = "[1m]"


def split_context_suffix(model: str) -> tuple[str, bool]:
    """'claude-sonnet-4-5[1m]' -> ('claude-sonnet-4-5', True)."""
    m = (model or "").strip()
    if m.lower().endswith(ONE_M_SUFFIX):
        return m[: -len(ONE_M_SUFFIX)], True
    return m, False


def is_model_alias(model: str) -> bool:
    base, _ = split_context_suffix(model)
    return base.lower() in MODEL_ALIASES or base.lower() == "opusplan"


def resolve_model_alias(model: str, *, preset: ProviderPreset | None = None, mode: str | None = None) -> str:
    """Resolve family aliases (opus / sonnet / haiku / fable / opusplan, optional "[1m]") to a model id.

    When `preset` hosts a model of that family (e.g. AgentRouter's claude-opus-5) that id wins, so the
    alias stays on the current provider. Non-alias ids are returned unchanged (minus nothing).
    """
    base, one_m = split_context_suffix(model)
    key = base.lower()
    if key == "opusplan":
        key = "opus" if (mode or "") == "plan" else "sonnet"
    if key not in MODEL_ALIASES:
        return model
    family = _ALIAS_FAMILY[key]
    resolved = MODEL_ALIASES[key]
    if preset is not None and preset.id != "anthropic":
        hosted = [m for m in preset.models if family in m.lower().rsplit("/", 1)[-1] and "claude" in m.lower()]
        if hosted:
            resolved = hosted[0]
    return resolved + (ONE_M_SUFFIX if one_m else "")


# ─── Lookup ─────────────────────────────────────────────────────────────────
def get_preset_by_id(preset_id: str) -> ProviderPreset | None:
    pid = (preset_id or "").strip().lower()
    for p in PROVIDER_PRESETS:
        if p.id == pid:
            return p
    aliases = {"google": "gemini", "grok": "xai", "x-ai": "xai", "lm-studio": "lmstudio", "lm_studio": "lmstudio",
               "azure-openai": "azure", "azure_openai": "azure", "claude": "anthropic"}
    if pid in aliases:
        return get_preset_by_id(aliases[pid])
    return None


def split_provider_prefix(model_name: str) -> tuple[ProviderPreset | None, str]:
    """'anthropic/claude-sonnet-4-5' -> (anthropic preset, 'claude-sonnet-4-5'); otherwise (None, model)."""
    m = (model_name or "").strip()
    if "/" not in m:
        return None, m
    head, rest = m.split("/", 1)
    preset = get_preset_by_id(head)
    if preset is None or not rest:
        return None, m
    return preset, rest


def find_preset_for_model(model_name: str) -> ProviderPreset | None:
    """Find the provider preset that naturally hosts this model id."""
    m_clean = (model_name or "").strip()
    low = m_clean.lower()

    for p in PROVIDER_PRESETS:
        if m_clean in p.models or m_clean == p.default_model:
            return p

    prefixed, rest = split_provider_prefix(m_clean)
    if prefixed is not None and prefixed.id != "openrouter":
        if rest in prefixed.models or prefixed.id in ("ollama", "lmstudio", "agentrouter", "azure"):
            return prefixed
        family = find_preset_for_model(rest) if "/" not in rest else None
        if family is not None and family.id == prefixed.id:
            return prefixed

    if "/" in m_clean:
        return get_preset_by_id("openrouter")
    if ":" in m_clean and not m_clean.startswith("http"):
        return get_preset_by_id("ollama")
    if low.startswith("claude-"):
        return get_preset_by_id("anthropic")
    if low.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
        return get_preset_by_id("openai")
    if low.startswith(("deepseek-chat", "deepseek-reasoner")):
        return get_preset_by_id("deepseek")
    if low.startswith(("gemini-", "gemma-")):
        return get_preset_by_id("gemini")
    if low.startswith("grok-"):
        return get_preset_by_id("xai")
    if low.startswith(("mistral-", "codestral", "devstral", "magistral", "open-mistral", "open-mixtral", "pixtral")):
        return get_preset_by_id("mistral")
    return None


def find_preset_by_url(base_url: str) -> ProviderPreset | None:
    """Find the provider preset matching a base URL (by host, then port for local servers)."""
    from axon.providers.transport import host_of, is_agentrouter, is_azure, port_of

    if not base_url:
        return None
    if is_agentrouter(base_url):
        return get_preset_by_id("agentrouter")
    if is_azure(base_url):
        return get_preset_by_id("azure")
    host = host_of(base_url)
    port = port_of(base_url)
    for p in PROVIDER_PRESETS:
        if p.id == "azure":
            continue
        if host_of(p.base_url) == host and (p.requires_key or port_of(p.base_url) == port):
            return p
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        if port == 11434:
            return get_preset_by_id("ollama")
        if port == 1234:
            return get_preset_by_id("lmstudio")
    return None
