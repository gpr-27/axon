"""
Provider router: picks the provider implementation, endpoint and API key for a model, and exposes
model capability tables (context windows, max output). Pricing lives in axon.providers.pricing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from axon.providers import pricing as _pricing
from axon.providers.anthropic import AnthropicProvider
from axon.providers.base import Provider
from axon.providers.catalog import (
    PROVIDER_PRESETS,
    ProviderPreset,
    effective_base_url,
    find_api_key,
    find_preset_by_url,
    get_preset_by_id,
    is_model_alias,
    resolve_model_alias,
    split_context_suffix,
    split_provider_prefix,
)
from axon.providers.openai_compat import OpenAICompatProvider
from axon.providers.pricing import PRICING, sync_agentrouter_pricing  # noqa: F401  (re-exported)
from axon.providers.transport import is_azure, is_local_inference_url, is_local_url, is_official_anthropic

__all__ = [
    "PRICING",
    "MODEL_CONTEXT_LIMITS",
    "MODEL_MAX_OUTPUT",
    "ANTHROPIC_MODELS",
    "Route",
    "resolve_route",
    "resolve_api_key",
    "provider_for",
    "fallback_chain",
    "known_models",
    "get_context_window",
    "get_max_output",
    "sync_agentrouter_pricing",
]

# ─── Capability tables (exact ids or family prefixes) ──────────────────────
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # AgentRouter catalogue
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "gpt-5.6-sol": 1_000_000,
    "gpt-6-astra": 1_000_000,
    # Anthropic
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3": 200_000,
    # OpenAI
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    # Google
    "gemini-2.5": 1_048_576,
    "gemini-2.0": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    # Others
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "deepseek-r1": 128_000,
    "deepseek-v3": 128_000,
    "grok-4-fast": 2_000_000,
    "grok-4": 256_000,
    "grok-code-fast": 256_000,
    "grok-3": 131_072,
    "llama-3.3-70b": 128_000,
    "llama-3.1-8b": 128_000,
    "llama3.1-8b": 128_000,
    "gpt-oss": 131_072,
    "kimi-k2": 131_072,
    "qwen-3-coder": 131_072,
    "qwen3-coder": 262_144,
    "mistral-large": 128_000,
    "mistral-medium": 128_000,
    "mistral-small": 128_000,
    "codestral": 256_000,
    "devstral": 128_000,
}

MODEL_MAX_OUTPUT: dict[str, int] = {
    "claude-opus-5": 128_000,
    "claude-opus-4-8": 128_000,
    "deepseek-v4-flash": 128_000,
    "gpt-5.6-sol": 128_000,
    "gpt-6-astra": 128_000,
    "claude-fable-5": 128_000,
    "claude-mythos-5": 128_000,
    "claude-opus-5-5": 128_000,
    "claude-opus-4-7": 128_000,
    "claude-opus-4-6": 128_000,
    "claude-sonnet-5": 128_000,
    "claude-sonnet-4-6": 128_000,
    "claude-haiku-5-5": 128_000,
    "claude-opus-4-5": 64_000,
    "claude-opus-4-1": 32_000,
    "claude-opus-4": 32_000,
    "claude-sonnet-4": 64_000,
    "claude-haiku-4-5": 64_000,
    "claude-3-7-sonnet": 64_000,
    "claude-3-5": 8_192,
    "claude-3": 4_096,
    "gpt-5": 128_000,
    "gpt-4.1": 32_768,
    "gpt-4o": 16_384,
    "o1": 100_000,
    "o3": 100_000,
    "o4-mini": 100_000,
    "gemini-2.5": 65_536,
    "gemini-2.0": 8_192,
    "gemini-1.5": 8_192,
    "deepseek-chat": 8_192,
    "deepseek-reasoner": 65_536,
    "llama-3.3-70b-versatile": 32_768,
    "llama-3.1-8b-instant": 131_072,
    "mistral-large": 32_768,
    "codestral": 32_768,
}

# Kept for importers (agent/context.py). Family detection for Claude ids is is_anthropic_model().
ANTHROPIC_MODELS: set[str] = {
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-1",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022",
}

_BOUNDARY = set("-.:_@/")


def _family_lookup(table: dict[str, int], model: str) -> int | None:
    m = (model or "").strip()
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    m = m.lower()
    if m.startswith("claude"):
        m = re.sub(r"(?<=\d)\.(?=\d)", "-", m)
    if m in table:
        return table[m]
    best = None
    for k in table:
        if m.startswith(k) and len(m) > len(k) and m[len(k)] in _BOUNDARY and (best is None or len(k) > len(best)):
            best = k
    return table[best] if best else None


def _canonical(model: str) -> tuple[str, bool]:
    base, one_m = split_context_suffix(model or "")
    if is_model_alias(base):
        base = resolve_model_alias(base)
    return base, one_m


def get_context_window(model: str, default: int | None = None) -> int:
    """Context window for `model` (family-aware; aliases and "[1m]" variants resolved).
    Unknown models -> `default` or 128k."""
    model, one_m = _canonical(model)
    if one_m:
        return 1_000_000
    val = _family_lookup(MODEL_CONTEXT_LIMITS, model)
    if val is not None:
        return val
    return default if default is not None else 128_000


def get_max_output(model: str, default: int = 64_000) -> int:
    """Max completion tokens for `model` (family-aware)."""
    val = _family_lookup(MODEL_MAX_OUTPUT, _canonical(model)[0])
    return val if val is not None else default


def known_max_output(model: str) -> int | None:
    return _family_lookup(MODEL_MAX_OUTPUT, _canonical(model)[0])


def is_anthropic_model(model: str) -> bool:
    m = (model or "").lower().rsplit("/", 1)[-1]
    return m in ANTHROPIC_MODELS or m.startswith("claude")


# ─── Routing ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Route:
    preset: ProviderPreset | None
    base_url: str
    api_key: str
    api_format: str          # "anthropic" | "openai"
    model_prefix: str = ""   # "<provider>/" stripped from the model id before it is sent
    aliases: tuple[tuple[str, str], ...] = ()  # (alias, resolved id) applied before sending

    @property
    def provider_id(self) -> str | None:
        return self.preset.id if self.preset else None


def _settings_key(settings: Any) -> str:
    key = getattr(settings, "api_key", None)
    if key is None:
        return ""
    val = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return (val or "").strip()


def resolve_api_key(preset_or_base_url: ProviderPreset | str | None, settings: Any = None) -> str:
    """API key for a provider: its own env vars (process env, then ~/.axon/.env), else settings.api_key.

    settings.api_key is only reused when settings.base_url belongs to the same provider (or is a
    custom endpoint), so a key configured for one vendor is never sent to another vendor.
    Local providers return "" when no key is configured.
    """
    if isinstance(preset_or_base_url, ProviderPreset):
        preset = preset_or_base_url
    elif isinstance(preset_or_base_url, str) and preset_or_base_url:
        preset = get_preset_by_id(preset_or_base_url) or find_preset_by_url(preset_or_base_url)
    else:
        preset = None

    if preset is not None and preset.env_vars:
        found = find_api_key(list(preset.env_vars))
        if found:
            return found

    fallback = _settings_key(settings) if settings is not None else ""
    if fallback.lower() in ("local", "none", "null"):
        fallback = ""
    if preset is None:
        return fallback
    settings_url = str(getattr(settings, "base_url", "") or "") if settings is not None else ""
    owner = find_preset_by_url(settings_url) if settings_url else None
    if owner is None or owner.id == preset.id:
        return fallback
    return ""


def resolve_route(model: str, settings: Any) -> Route:
    """Decide preset / endpoint / key for `model`.

    Order: explicit `settings.provider` -> explicit "<provider>/" model prefix (unless the current
    endpoint itself uses namespaced ids, e.g. OpenRouter) -> settings.base_url -> model family.
    """
    base_url = str(getattr(settings, "base_url", "") or "https://agentrouter.org")
    url_preset = find_preset_by_url(base_url)
    explicit = getattr(settings, "provider", None)
    explicit_preset = get_preset_by_id(str(explicit)) if isinstance(explicit, str) and explicit.strip() else None

    aliases: tuple[tuple[str, str], ...] = ()
    if is_model_alias(model):
        host = explicit_preset or url_preset
        resolved = resolve_model_alias(model, preset=host, mode=getattr(settings, "mode", None))
        bare_resolved, _ = split_context_suffix(resolved)
        _pricing.note_alias(model, bare_resolved)
        aliases = ((model, bare_resolved),)
        if explicit_preset is None and (host is None or (host.id != "anthropic" and bare_resolved not in host.models)):
            # The current endpoint doesn't serve this Claude family: send the alias to Anthropic directly.
            explicit_preset = get_preset_by_id("anthropic")
        model = bare_resolved

    prefix = ""
    target: ProviderPreset | None = None
    prefixed, _rest = split_provider_prefix(model)

    if explicit_preset is not None:
        target = explicit_preset
        if prefixed is not None and prefixed.id == explicit_preset.id and not explicit_preset.namespaced_models:
            prefix = f"{model.split('/', 1)[0]}/"
    elif prefixed is not None and not (url_preset is not None and url_preset.namespaced_models) and prefixed.id != (url_preset.id if url_preset else None):
        target = prefixed
        if not prefixed.namespaced_models or prefixed.id in ("ollama", "lmstudio"):
            prefix = f"{model.split('/', 1)[0]}/"
    elif prefixed is not None and url_preset is not None and prefixed.id == url_preset.id and not url_preset.namespaced_models:
        target = url_preset
        prefix = f"{model.split('/', 1)[0]}/"

    if target is not None and (url_preset is None or url_preset.id != target.id):
        # Explicitly routed to a different provider than base_url: use that provider's endpoint,
        # unless base_url is a custom (unknown) endpoint the user chose for this provider.
        if explicit_preset is not None and url_preset is None:
            route_base = base_url
        else:
            route_base = effective_base_url(target)
        preset = target
    else:
        preset = target or url_preset
        route_base = base_url

    if preset is not None and preset.id == "azure":
        route_base = base_url if is_azure(base_url) else effective_base_url(preset)

    if preset is not None:
        api_format = preset.api_format
    elif is_official_anthropic(route_base):
        api_format = "anthropic"
    elif is_anthropic_model(model) and not is_local_url(route_base) and model in ANTHROPIC_MODELS:
        # Unknown custom endpoint serving a Claude id: assume an Anthropic-compatible proxy.
        api_format = "anthropic"
    else:
        api_format = "openai"

    key = resolve_api_key(preset, settings) if preset is not None else resolve_api_key(None, settings)
    return Route(preset=preset, base_url=route_base, api_key=key, api_format=api_format, model_prefix=prefix, aliases=aliases)


def fallback_chain(settings: Any, role: str = "main", primary: str = "") -> list[str]:
    """Ordered failover models: settings.fallback_model / fallback_models, then settings.routing[role].

    Entries may be "model", "provider/model" or "provider:model" (converted to "provider/model").
    """
    raw: list[Any] = []
    for attr in ("fallback_model", "fallback_models", "fallbackModel"):
        val = getattr(settings, attr, None)
        if isinstance(val, str):
            raw.extend(v.strip() for v in val.split(","))
        elif isinstance(val, (list, tuple)):
            raw.extend(val)
    routing = getattr(settings, "routing", None)
    if isinstance(routing, dict):
        chain = routing.get(role) or (routing.get("main") if role != "main" else None)
        if isinstance(chain, str):
            raw.extend(v.strip() for v in chain.split(","))
        elif isinstance(chain, (list, tuple)):
            raw.extend(chain)
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        m = item.strip()
        head, sep, rest = m.partition(":")
        if sep and rest and rest != "*" and get_preset_by_id(head) is not None and "/" not in head:
            m = f"{head}/{rest}"
        if m != primary and m not in out:
            out.append(m)
    return out


def provider_for(model: str, settings: Any, *, role: str = "main", failover: bool = True) -> Provider:
    """Resolve and instantiate the provider for `model` (see resolve_route for the precedence).

    When a fallback chain is configured (settings.fallback_model / routing), the provider is wrapped
    so 429 / 5xx / overload / timeouts that survive retries fail over to the next model.
    """
    primary = _build_provider(model, settings)
    if failover:
        chain = fallback_chain(settings, role=role, primary=model)
        if chain:
            from axon.providers.failover import FailoverProvider
            return FailoverProvider(primary, model=model, settings=settings, chain=chain)  # type: ignore[return-value]
    return primary


def _build_provider(model: str, settings: Any) -> Provider:
    route = resolve_route(model, settings)
    from pydantic import SecretStr

    updates: dict[str, Any] = {}
    if route.base_url != getattr(settings, "base_url", None):
        updates["base_url"] = route.base_url
    current_key = _settings_key(settings)
    if route.api_key and route.api_key != current_key:
        updates["api_key"] = SecretStr(route.api_key)
    elif not route.api_key and current_key and route.preset is not None and _settings_key_belongs_elsewhere(route, settings):
        updates["api_key"] = SecretStr("")
    eff_settings = settings.model_copy(update=updates) if updates and hasattr(settings, "model_copy") else settings

    pid = route.provider_id
    if pid is None and is_local_url(route.base_url):
        # Free only for real local inference servers, or when the model has no known price;
        # a localhost proxy in front of gpt-4o keeps gpt-4o's price.
        if is_local_inference_url(route.base_url) or not _pricing.price_for(model).priced:
            pid = "local"
    _pricing.note_route(model, pid)
    if pid and "/" in model and model.split("/", 1)[0].lower() == pid:
        # "anthropic/claude-opus-4-8" goes on the wire (and into the ledger) as the bare id
        _pricing.note_route(model.split("/", 1)[1], pid)
    if pid == "agentrouter":
        _pricing.sync_agentrouter_pricing_async(route.base_url)

    aliases = dict(route.aliases)
    if route.api_format == "anthropic":
        return AnthropicProvider(eff_settings, model_prefix=route.model_prefix, aliases=aliases)
    return OpenAICompatProvider(eff_settings, model_prefix=route.model_prefix, preset=route.preset, aliases=aliases)


def _settings_key_belongs_elsewhere(route: Route, settings: Any) -> bool:
    owner = find_preset_by_url(str(getattr(settings, "base_url", "") or ""))
    return owner is not None and route.preset is not None and owner.id != route.preset.id


def known_models() -> list[str]:
    models: list[str] = []
    for p in PROVIDER_PRESETS:
        for m in p.models:
            if m not in models:
                models.append(m)
    return models


def env_api_key_for_url(base_url: str) -> str:
    """Convenience: key for whatever provider owns base_url ('' if none configured)."""
    p = find_preset_by_url(base_url)
    if p is None or not p.env_vars:
        return ""
    return find_api_key(list(p.env_vars)) or ""
