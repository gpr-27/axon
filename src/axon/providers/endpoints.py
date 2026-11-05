"""
Endpoint overrides ("redirection") and custom OpenAI-compatible endpoints.

Stored in the user's ``~/.axon/config.toml`` only (a project file may never redirect a user's key)::

    [endpoints.openai]                 # a preset: send its traffic to a proxy / gateway
    base_url = "https://proxy.example.com/v1"

    [endpoints.my-gateway]             # a new provider
    base_url = "https://llm.example.com/v1"
    env_var  = "MY_GATEWAY_API_KEY"    # where its key is read from (empty: no key)
    model    = "qwen3-coder"           # optional default model
    name     = "My gateway"            # optional display name
    api_format = "openai"              # or "anthropic"
    native_tools = false               # optional: the gateway drops `tools`, so describe them in the prompt

``apply_endpoint_overrides`` patches ``PROVIDER_PRESETS`` in place (presets get the new base URL,
custom endpoints become presets in the "Custom" category), so routing, pickers, ``/keys`` and
``find_preset_by_url`` all see them. It is idempotent: defaults are restored before each apply.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, axon_config_file, get_preset_by_id

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
_DEFAULTS: dict[str, tuple[str, tuple[str, ...], str | None, str, bool]] = {}
_CUSTOM_IDS: set[str] = set()


# ─── Validation ────────────────────────────────────────────────────────────

def valid_url(url: str) -> bool:
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc) and " " not in url.strip()


def url_warning(url: str) -> str:
    """A caution for plain-http endpoints that are not on this machine (keys would travel in clear)."""
    from axon.providers.transport import is_local_url
    if url.strip().lower().startswith("http://") and not is_local_url(url):
        return "this endpoint uses plain http: your API key would be sent unencrypted"
    return ""


def norm(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "custom"


def suggest_id(url: str) -> str:
    host = (urlparse(url).hostname or "custom").lower()
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "org", "net", "io", "ai", "dev", "co")]
    return slug(parts[0] if parts else host)


def suggest_env_var(eid: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", eid.upper()).strip("_") + "_API_KEY"


# ─── Persistence (~/.axon/config.toml, user scope only) ────────────────────

def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    from axon.config import decode_config_bytes
    try:
        return tomllib.loads(decode_config_bytes(path.read_bytes()))
    except Exception as e:
        raise ValueError(f"{path} is not valid TOML ({e}); fix it first") from e


def _write_config(path: Path, data: dict[str, Any]) -> None:
    import tomli_w
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(tomli_w.dumps(data))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_endpoints(path: Path | None = None) -> dict[str, dict[str, Any]]:
    try:
        table = _read_config(path or axon_config_file()).get("endpoints")
    except ValueError:
        return {}
    return {str(k): dict(v) for k, v in table.items() if isinstance(v, dict)} if isinstance(table, dict) else {}


# ─── Applying overrides to the preset catalog ──────────────────────────────

def _snapshot() -> None:
    for p in PROVIDER_PRESETS:
        if p.id not in _CUSTOM_IDS and p.id not in _DEFAULTS:
            _DEFAULTS[p.id] = (p.base_url, tuple(p.env_vars), p.env_var, p.default_model, p.native_tools)


def default_url(preset_id: str) -> str | None:
    _snapshot()
    d = _DEFAULTS.get(preset_id)
    return d[0] if d else None


def is_custom(preset_or_id: Any) -> bool:
    pid = getattr(preset_or_id, "id", preset_or_id)
    return str(pid) in _CUSTOM_IDS


def is_overridden(preset: ProviderPreset) -> bool:
    d = default_url(preset.id)
    return d is not None and norm(d) != norm(preset.base_url)


def apply_endpoint_overrides(table: dict[str, Any] | None = None) -> list[str]:
    """Restore the preset defaults, then apply ``table`` (default: the user's config.toml).
    Returns the ids that were changed or added."""
    if table is None:
        table = load_endpoints()
    _snapshot()
    PROVIDER_PRESETS[:] = [p for p in PROVIDER_PRESETS if p.id not in _CUSTOM_IDS]
    _CUSTOM_IDS.clear()
    for p in PROVIDER_PRESETS:
        d = _DEFAULTS.get(p.id)
        if d:
            p.base_url, p.env_vars, p.env_var, p.default_model, p.native_tools = d[0], tuple(d[1]), d[2], d[3], d[4]
    changed: list[str] = []
    for eid, spec in (table or {}).items():
        if not isinstance(spec, dict):
            continue
        url = str(spec.get("base_url") or "").strip()
        if not valid_url(url):
            continue
        env_var = str(spec.get("env_var") or "").strip()
        if env_var and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_var):
            env_var = ""
        model = str(spec.get("model") or "").strip()
        native_tools = spec.get("native_tools")
        preset = get_preset_by_id(str(eid))
        if preset is not None:
            preset.base_url = url
            if env_var:
                preset.env_vars = (env_var, *[v for v in preset.env_vars if v != env_var])
                preset.env_var = env_var
            if model:
                preset.default_model = model
            if isinstance(native_tools, bool):
                preset.native_tools = native_tools
            changed.append(preset.id)
            continue
        pid = str(eid).lower()
        if not ID_RE.match(pid):
            continue
        fmt = str(spec.get("api_format") or "openai").lower()
        PROVIDER_PRESETS.append(ProviderPreset(
            id=pid, name=str(spec.get("name") or pid), description=f"Custom endpoint · {url}",
            category="Custom", base_url=url, default_model=model, models=[model] if model else [],
            env_var=env_var or None, requires_key=bool(env_var),
            api_format="anthropic" if fmt == "anthropic" else "openai", namespaced_models=True,
            native_tools=native_tools if isinstance(native_tools, bool) else True,
        ))
        _CUSTOM_IDS.add(pid)
        changed.append(pid)
    return changed


# ─── Mutations ─────────────────────────────────────────────────────────────

@dataclass
class EndpointChange:
    id: str
    old_url: str
    new_url: str
    path: Path
    active_changed: bool = False   # config.toml's base_url pointed at the old URL and now follows


def _current_url(eid: str) -> str:
    p = get_preset_by_id(eid)
    return p.base_url if p is not None else ""


def save_endpoint(eid: str, base_url: str, *, env_var: str | None = None, model: str | None = None,
                  name: str | None = None, api_format: str | None = None, native_tools: bool | None = None,
                  path: Path | None = None) -> EndpointChange:
    """Set a preset's base URL, or create/update a custom endpoint, in ~/.axon/config.toml.

    ``native_tools=False`` marks an endpoint that drops the ``tools`` field (tools go in the prompt);
    ``None`` leaves the saved value alone."""
    url = (base_url or "").strip().rstrip("/")
    if not valid_url(url):
        raise ValueError(f"not a valid http(s) URL: {base_url!r}")
    preset = get_preset_by_id(eid)
    key = preset.id if preset is not None and not is_custom(preset) else str(eid).strip().lower()
    if preset is None and not ID_RE.match(key):
        raise ValueError(f"invalid endpoint name {eid!r}: use lowercase letters, digits, '-', '_' or '.'")
    if env_var and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_var):
        raise ValueError(f"invalid environment variable name: {env_var!r}")
    if api_format and api_format not in ("openai", "anthropic"):
        raise ValueError("api_format must be 'openai' or 'anthropic'")
    cfg_path = path or axon_config_file()
    data = _read_config(cfg_path)
    old = _current_url(key)
    table = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else {}
    spec = dict(table.get(key) or {})
    spec["base_url"] = url
    for k, v in (("env_var", env_var), ("model", model), ("name", name), ("api_format", api_format)):
        if v is not None:
            if v == "":
                spec.pop(k, None)
            else:
                spec[k] = v
    if native_tools is not None:
        spec["native_tools"] = bool(native_tools)
    table[key] = spec
    data["endpoints"] = table
    active = bool(old) and norm(str(data.get("base_url", ""))) == norm(old)
    if active:
        data["base_url"] = url
    _write_config(cfg_path, data)
    apply_endpoint_overrides(table)
    return EndpointChange(key, old, url, cfg_path, active)


def set_native_tools(eid: str, native_tools: bool, path: Path | None = None) -> EndpointChange:
    """Turn native tool calling on or off for an existing endpoint or preset, keeping its URL."""
    preset = get_preset_by_id(eid)
    if preset is None:
        raise ValueError(f"unknown endpoint {eid!r}")
    return save_endpoint(preset.id, preset.base_url, native_tools=native_tools, path=path)


def reset_endpoint(eid: str, path: Path | None = None) -> EndpointChange | None:
    """Drop the override (preset back to its default URL) or remove a custom endpoint."""
    cfg_path = path or axon_config_file()
    data = _read_config(cfg_path)
    table = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else {}
    preset = get_preset_by_id(eid)
    key = preset.id if preset is not None else str(eid).strip().lower()
    if key not in table:
        return None
    old = _current_url(key)
    table.pop(key)
    if table:
        data["endpoints"] = table
    else:
        data.pop("endpoints", None)
    new = default_url(key) or ""
    active = bool(old) and norm(str(data.get("base_url", ""))) == norm(old)
    if active:
        if new:
            data["base_url"] = new
        else:
            data.pop("base_url", None)
    if not new and str(data.get("provider", "")).lower() == key:
        data.pop("provider", None)  # a removed custom endpoint must not stay the configured provider
    _write_config(cfg_path, data)
    if not default_url(key):
        remove_models(key, cfg_path)
    apply_endpoint_overrides(table)
    return EndpointChange(key, old, new, cfg_path, active)


# ─── Connectivity test ─────────────────────────────────────────────────────

@dataclass
class EndpointTest:
    reachable: bool
    ok: bool
    message: str
    latency_s: float = 0.0
    status: int | None = None
    models: int | None = None


def http_get(url: str, headers: dict[str, str], timeout: float) -> Any:
    """Indirection so tests can replace the network call."""
    import httpx
    return httpx.get(url, headers=headers, timeout=timeout)


def models_url(base_url: str, api_format: str = "openai") -> str:
    from axon.providers.transport import _api_root
    if api_format == "anthropic":
        return f"{base_url.rstrip('/').removesuffix('/v1')}/v1/models"
    return f"{_api_root(base_url)}/models"


def request_headers(base_url: str, key: str, api_format: str) -> dict[str, str]:
    """Headers for a /models request: the endpoint's defaults plus the key in the style its API expects."""
    from axon.providers.transport import default_headers, is_official_anthropic
    headers = default_headers(base_url)
    if not key or key == "local":
        return headers
    if api_format != "anthropic":
        headers["authorization"] = f"Bearer {key}"
        return headers
    headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    if not is_official_anthropic(base_url):
        headers["authorization"] = f"Bearer {key}"
    return headers


def test_endpoint(base_url: str, key: str = "", api_format: str = "openai", timeout: float = 6.0) -> EndpointTest:
    """GET <api root>/models: reachable? key accepted? how many models?"""
    url = models_url(base_url, api_format)
    headers = request_headers(base_url, key, api_format)
    t0 = time.perf_counter()
    try:
        resp = http_get(url, headers, timeout)
    except Exception as e:
        name = type(e).__name__
        msg = f"timed out after {timeout:g}s" if "Timeout" in name else f"{name}: {e}"
        return EndpointTest(False, False, f"cannot reach {url}: {msg}", time.perf_counter() - t0)
    latency = time.perf_counter() - t0
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 200:
        try:
            data = resp.json()
            items = data.get("data", data.get("models")) if isinstance(data, dict) else data
            n = len(items) if isinstance(items, list) else None
        except Exception:
            n = None
        return EndpointTest(True, True, "OK", latency, status, n)
    if status in (401, 403):
        why = "no API key was sent" if not key or key == "local" else "the API key was rejected"
        return EndpointTest(True, False, f"reachable, but {why} (HTTP {status})", latency, status)
    if status == 404:
        return EndpointTest(True, False, f"reachable, but {url} was not found (HTTP 404): check the path "
                                         "(many servers expect it to end in /v1)", latency, status)
    return EndpointTest(True, False, f"reachable, HTTP {status}", latency, status)


test_endpoint.__test__ = False  # type: ignore[attr-defined]


# ─── Tool-support probe ────────────────────────────────────────────────────

@dataclass
class ToolProbe:
    ran: bool            # False when the probe could not be made (no model, error, ...)
    native_tools: bool   # the endpoint returned a tool call
    message: str = ""


def http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> Any:
    """Indirection so tests can replace the network call."""
    import httpx
    return httpx.post(url, headers=headers, json=body, timeout=timeout)


def probe_tool_support(base_url: str, key: str = "", model: str = "", timeout: float = 30.0) -> ToolProbe:
    """One tiny request with a trivial ``echo`` tool: does the endpoint hand back a tool call?

    Only run on request (``/endpoint test``), never during normal chat."""
    if not model:
        return ToolProbe(False, True, "no model to probe with: pass --model <id>")
    from axon.providers.transport import chat_completions_url, default_headers
    headers = default_headers(base_url)
    if key and key != "local":
        headers["authorization"] = f"Bearer {key}"
    echo = {"type": "function", "function": {
        "name": "echo", "description": "Return the given text.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
    body = {"model": model, "max_tokens": 200, "tools": [echo], "tool_choice": "auto",
            "messages": [{"role": "user", "content": "Call the echo tool with the text 'hi'. Do not answer in prose."}]}
    try:
        resp = http_post(chat_completions_url(base_url), headers, body, timeout)
        data = resp.json()
    except Exception as e:
        return ToolProbe(False, True, f"{type(e).__name__}: {e}")
    if int(resp.status_code) != 200:
        return ToolProbe(False, True, f"HTTP {resp.status_code}")
    return ToolProbe(True, _has_tool_call(data))


def _has_tool_call(data: Any) -> bool:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = choices[0].get("message")
    return isinstance(message, dict) and bool(message.get("tool_calls") or message.get("function_call"))


probe_tool_support.__test__ = False  # type: ignore[attr-defined]


# ─── Importing models from an endpoint ─────────────────────────────────────

@dataclass
class RemoteModels:
    models: list[str]
    error: str = ""


def parse_model_ids(payload: Any) -> list[str]:
    """Model ids from an OpenAI/Anthropic ``{"data": [...]}``, an Ollama-style ``{"models": [...]}`` or a bare list."""
    items = payload.get("data", payload.get("models")) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("id") or item.get("name") or item.get("model")
        text = str(item or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def fetch_models(base_url: str, key: str = "", api_format: str = "openai", timeout: float = 8.0) -> RemoteModels:
    """The chat models an endpoint lists at /models, or an error saying why the list is unavailable."""
    from axon.providers.catalog import is_chat_model_id
    url = models_url(base_url, api_format)
    try:
        resp = http_get(url, request_headers(base_url, key, api_format), timeout)
    except Exception as e:
        return RemoteModels([], f"cannot reach {url}: {type(e).__name__}: {e}")
    status = int(getattr(resp, "status_code", 0) or 0)
    if status in (401, 403):
        return RemoteModels([], f"{url} rejected the API key (HTTP {status}); set it with /keys set")
    if status != 200:
        return RemoteModels([], f"{url} answered HTTP {status}; it may not offer a model list. Add models by name instead")
    try:
        ids = parse_model_ids(resp.json())
    except ValueError:
        return RemoteModels([], f"{url} did not return JSON")
    chat_ids = [m for m in ids if is_chat_model_id(m)]
    if not chat_ids:
        return RemoteModels([], f"{url} listed no models")
    return RemoteModels(chat_ids)


def _custom_model_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries = data.get("custom_models")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def saved_models(eid: str, path: Path | None = None) -> list[str]:
    """Models saved for provider *eid* in config.toml ``custom_models``."""
    data = _read_config(path or axon_config_file())
    return [str(e["model"]) for e in _custom_model_entries(data) if e.get("provider") == eid and e.get("model")]


def add_models(eid: str, models: list[str], path: Path | None = None) -> list[str]:
    """Save *models* under provider *eid*; returns the ones that were not saved before."""
    cfg_path = path or axon_config_file()
    data = _read_config(cfg_path)
    entries = _custom_model_entries(data)
    known = {str(e.get("model")) for e in entries if e.get("provider") == eid}
    added: list[str] = []
    for model in models:
        model = model.strip()
        if model and model not in known:
            entries.append({"model": model, "provider": eid})
            known.add(model)
            added.append(model)
    if added:
        data["custom_models"] = entries
        _write_config(cfg_path, data)
    return added


def remove_models(eid: str, path: Path | None = None) -> int:
    """Forget every model saved for provider *eid*; returns how many were removed."""
    cfg_path = path or axon_config_file()
    data = _read_config(cfg_path)
    entries = _custom_model_entries(data)
    kept = [e for e in entries if e.get("provider") != eid]
    removed = len(entries) - len(kept)
    if removed:
        if kept:
            data["custom_models"] = kept
        else:
            data.pop("custom_models", None)
        _write_config(cfg_path, data)
    return removed


__all__ = [
    "EndpointChange", "EndpointTest", "RemoteModels", "ToolProbe", "add_models", "apply_endpoint_overrides", "default_url",
    "fetch_models", "http_get", "http_post", "is_custom", "is_overridden", "load_endpoints", "models_url", "norm",
    "parse_model_ids", "probe_tool_support", "remove_models", "request_headers", "reset_endpoint", "save_endpoint", "saved_models", "set_native_tools",
    "slug", "suggest_env_var", "suggest_id", "test_endpoint", "url_warning", "valid_url",
]
