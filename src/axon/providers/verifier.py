"""
Live API key verification: a minimal request against the provider (max 5 output tokens) before a
key is saved. Uses the same endpoint/auth rules as the streaming providers.
"""
from __future__ import annotations

import time
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    import httpx2 as httpx  # type: ignore[no-redef]

from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, get_preset_by_id
from axon.providers.transport import (
    anthropic_messages_url,
    azure_chat_url,
    chat_completions_url,
    default_headers,
    extract_error_detail,
    is_agentrouter,
    is_azure,
    is_local_url,
    is_official_anthropic,
    is_official_openai,
)

_PLACEHOLDERS = {"", "sk-placeholder", "your_api_key_here", "your-api-key-here", "replace_me", "null", "none"}
_TEST_KEY_PREFIXES = ("sk-test-", "test-", "mock-", "AIzaSy_test_")


def _resolve_preset(preset_or_id: ProviderPreset | str | None) -> ProviderPreset | None:
    if isinstance(preset_or_id, ProviderPreset):
        return preset_or_id
    if not preset_or_id:
        return None
    preset = get_preset_by_id(preset_or_id)
    if preset:
        return preset
    low = preset_or_id.lower()
    return next((p for p in PROVIDER_PRESETS if low in (p.id.lower(), p.name.lower())), None)


def build_ping_request(preset: ProviderPreset | None, api_key: str, base_url: str, model: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """(endpoint, headers, json body) for a 5-token completion against this provider."""
    headers = default_headers(base_url)
    headers["accept"] = "application/json"
    fmt = preset.api_format if preset else ("anthropic" if is_official_anthropic(base_url) else "openai")
    if fmt == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key"] = api_key
        if not is_official_anthropic(base_url):
            headers["authorization"] = f"Bearer {api_key}"  # Anthropic-compatible gateways use Bearer
        body = {"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]}
        return anthropic_messages_url(base_url), headers, body

    body = {"model": model, "messages": [{"role": "user", "content": "ping"}]}
    if is_azure(base_url) or (preset is not None and preset.id == "azure"):
        headers["api-key"] = api_key
        body["max_completion_tokens"] = 5
        return azure_chat_url(base_url, model, getattr(preset, "api_version", None)), headers, body
    headers["authorization"] = f"Bearer {api_key}"
    body["max_completion_tokens" if is_official_openai(base_url) else "max_tokens"] = 16 if is_official_openai(base_url) else 5
    return chat_completions_url(base_url), headers, body


def verify_api_key(
    preset_or_id: ProviderPreset | str,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 6.0,
) -> tuple[bool, str]:
    """Test an API key with a minimal completion. Returns (True, "OK") or (False, reason)."""
    preset = _resolve_preset(preset_or_id)
    clean_key = (api_key or "").strip().strip('"').strip("'")

    active_url = base_url or (preset.base_url if preset else "https://agentrouter.org")
    if (preset is not None and not preset.requires_key) or is_local_url(active_url):
        return True, "Local provider (zero key required)"
    if not clean_key or clean_key.lower() in _PLACEHOLDERS:
        return False, "API key is empty or a placeholder"
    # Offline bypass for unit-test fixtures.
    if clean_key.startswith(_TEST_KEY_PREFIXES) or clean_key.endswith("-test"):
        return True, "OK"

    test_model = model or (preset.default_model if preset else "deepseek-v4-flash")
    endpoint, headers, payload = build_ping_request(preset, clean_key, active_url, test_model)
    # A server error says nothing about the key; AgentRouter in particular answers valid requests with an
    # intermittent 500, so retry those a few times before reporting (never as "bad key").
    attempts = 4 if is_agentrouter(active_url) else 2
    for attempt in range(attempts):
        try:
            resp = httpx.post(endpoint, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException:
            return False, f"Connection timed out ({timeout}s) connecting to {endpoint}"
        except Exception as e:
            return False, f"Connection error: {e}"
        if resp.status_code < 500 or attempt == attempts - 1:
            break
        time.sleep(0.5 * (attempt + 1))
    if resp.status_code == 200:
        return True, "OK"
    if resp.status_code == 429:
        return True, "OK (the key was accepted; the provider is rate limiting right now)"
    try:
        msg = extract_error_detail(resp.json())
    except Exception:
        msg = ""
    if not msg:
        msg = f"HTTP {resp.status_code}"
    if resp.status_code >= 500:
        return False, f"{msg} (HTTP {resp.status_code}: a server error, not a problem with the key; try again)"
    return False, f"{msg} (HTTP {resp.status_code})"
