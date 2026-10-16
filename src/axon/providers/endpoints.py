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
_DEFAULTS: dict[str, tuple[str, tuple[str, ...], str | None, str]] = {}
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
            _DEFAULTS[p.id] = (p.base_url, tuple(p.env_vars), p.env_var, p.default_model)


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
            p.base_url, p.env_vars, p.env_var, p.default_model = d[0], tuple(d[1]), d[2], d[3]
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
        preset = get_preset_by_id(str(eid))
        if preset is not None:
            preset.base_url = url
            if env_var:
                preset.env_vars = (env_var, *[v for v in preset.env_vars if v != env_var])
                preset.env_var = env_var
            if model:
                preset.default_model = model
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
                  name: str | None = None, api_format: str | None = None, path: Path | None = None) -> EndpointChange:
    """Set a preset's base URL, or create/update a custom endpoint, in ~/.axon/config.toml."""
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
    table[key] = spec
    data["endpoints"] = table
    active = bool(old) and norm(str(data.get("base_url", ""))) == norm(old)
    if active:
        data["base_url"] = url
    _write_config(cfg_path, data)
    apply_endpoint_overrides(table)
    return EndpointChange(key, old, url, cfg_path, active)


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
            if str(data.get("provider", "")).lower() == key:
                data.pop("provider", None)
    _write_config(cfg_path, data)
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


def test_endpoint(base_url: str, key: str = "", api_format: str = "openai", timeout: float = 6.0) -> EndpointTest:
    """GET <api root>/models: reachable? key accepted? how many models?"""
    from axon.providers.transport import default_headers, is_official_anthropic
    url = models_url(base_url, api_format)
    headers = default_headers(base_url)
    if key and key != "local":
        if api_format == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            if not is_official_anthropic(base_url):
                headers["authorization"] = f"Bearer {key}"
        else:
            headers["authorization"] = f"Bearer {key}"
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


__all__ = [
    "EndpointChange", "EndpointTest", "apply_endpoint_overrides", "default_url", "http_get", "is_custom",
    "is_overridden", "load_endpoints", "models_url", "norm", "reset_endpoint", "save_endpoint", "slug",
    "suggest_env_var", "suggest_id", "test_endpoint", "url_warning", "valid_url",
]
