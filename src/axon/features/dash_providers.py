"""Providers and settings for the dashboard, built on the terminal's own code (``providers.catalog``,
``providers.verifier``, ``ui.provider_picker.apply_provider``, the /config panel).

An API key is write-only here: it goes in through ``save_api_key`` and never comes back out in any response,
event or log. Responses say whether a key is set and where it comes from, nothing more."""
from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer

_AUTH_FAIL = re.compile(r"401|403|unauthori[sz]ed|invalid[ _-]*(api)?[ _-]*key|incorrect api key|authentication|forbidden|api key", re.I)


def _preset(pid: str) -> Any:
    from axon.providers.catalog import get_preset_by_id
    p = get_preset_by_id(pid)
    if p is None:
        raise DashError("unknown provider", 404)
    return p


def _secret(settings: Any) -> str:
    key = getattr(settings, "api_key", None)
    return key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")


def _key_source(preset: Any) -> tuple[bool, str]:
    """(is a usable key set, where it comes from) without ever returning the key."""
    if not preset.requires_key:
        return True, "not needed"
    try:
        from axon.providers import keystore
        for var in preset.env_vars:
            st = keystore.key_status(var)
            if keystore.usable(st.value):
                return True, {"env": "the environment", "user": "~/.axon/.env", "project": "the project .env"}.get(st.source, st.source or "a saved key")
    except Exception:
        pass
    from axon.providers.catalog import find_api_key
    if find_api_key(list(preset.env_vars)):
        return True, "a saved key"
    return False, ""


def current_choice(srv: "DashServer") -> dict[str, Any]:
    from axon.providers.catalog import find_preset_by_url
    s = srv.settings_obj()
    preset = find_preset_by_url(getattr(s, "base_url", "") or "")
    return {"provider": (getattr(s, "provider", None) or (preset.id if preset else "")), "preset": preset.id if preset else "",
            "model": getattr(s, "model", ""), "base_url": getattr(s, "base_url", ""), "key_set": bool(_secret(s).strip() and _secret(s) not in ("", "local"))}


def provider_ready(srv: "DashServer", rt: Any = None) -> tuple[bool, str]:
    """Can the next chat message possibly work? (False, plain reason) when no key is set for the chosen provider."""
    from axon.providers.catalog import _valid_key, find_preset_by_url
    try:
        s = rt.agent.settings if rt is not None else srv.settings_obj()
    except DashError as e:
        return False, str(e)
    base = getattr(s, "base_url", "") or ""
    preset = find_preset_by_url(base)
    if preset is not None and not preset.requires_key:
        return True, ""
    if _valid_key(_secret(s)) or (preset is not None and _key_source(preset)[0]):
        return True, ""
    if preset is None and base:
        from axon.providers.verifier import is_local_url
        if is_local_url(base):
            return True, ""
    name = preset.name if preset is not None else (base or "the chosen provider")
    return False, f"No API key is set for {name}. Add one in Providers, or pick a provider that needs none."


def looks_like_auth_failure(text: str) -> bool:
    return bool(_AUTH_FAIL.search(text or ""))


def providers_view(srv: "DashServer") -> dict[str, Any]:
    from axon.providers.catalog import PROVIDER_PRESETS, endpoint_notice, is_provider_linked
    cur = current_choice(srv)
    rows = []
    for p in PROVIDER_PRESETS:
        has, src = _key_source(p)
        try:
            linked = is_provider_linked(p)
        except Exception:
            linked = False
        rows.append({"id": p.id, "name": p.name, "description": p.description, "category": p.category, "base_url": p.base_url,
                     "default_model": p.default_model, "models": list(p.models)[:60], "env_var": p.env_var or "", "requires_key": p.requires_key,
                     "key_set": has, "key_source": src, "linked": linked, "docs_url": p.docs_url, "notice": endpoint_notice(p),
                     "active": bool(cur["preset"] == p.id), "api_format": p.api_format})
    return {"providers": rows, "current": cur, "custom_notice": endpoint_notice(None, cur["base_url"])}


def save_key(preset_id: str, key: str) -> dict[str, Any]:
    from axon.providers.catalog import save_api_key
    p = _preset(preset_id)
    if not p.requires_key or not p.env_var:
        raise DashError("this provider does not use an API key", 400)
    try:
        save_api_key(p.env_var, key)
    except ValueError as e:
        raise DashError(str(e), 400) from None
    _mask_everywhere(key)
    has, src = _key_source(p)
    return {"ok": True, "key_set": has, "key_source": src, "env_var": p.env_var}


def _mask_everywhere(key: str) -> None:
    """Register the literal key with the redactor so it is masked in every response, event and record from now on."""
    try:
        from axon.session.activity import add_secret
        add_secret(key.strip().strip('"').strip("'"))
    except Exception:
        pass


def _redact(msg: str, *secrets: str) -> str:
    from axon.features.dashboard import redact_text
    for s in secrets:
        if s and len(s) >= 6:
            msg = msg.replace(s, "[REDACTED]")
    return redact_text(msg)[:400]


def test_key(preset_id: str, key: str, base_url: str, model: str) -> dict[str, Any]:
    """A five-token request with the typed key, or the saved one when none is typed."""
    from axon.providers.catalog import find_api_key
    from axon.providers.verifier import verify_api_key
    p = _preset(preset_id)
    k = (key or "").strip() or (find_api_key(list(p.env_vars)) or "")
    if p.requires_key and not k:
        return {"ok": False, "message": "No key to test. Enter one first."}
    ok, msg = verify_api_key(p, k, base_url or None, model or p.default_model)
    return {"ok": bool(ok), "message": _redact(msg or ("The key works." if ok else "The provider refused it."), k)}


def list_models(preset_id: str, base_url: str) -> dict[str, Any]:
    from axon.providers.catalog import fetch_provider_models, find_api_key, get_models_for_provider
    p = _preset(preset_id)
    k = find_api_key(list(p.env_vars)) or ""
    live, status = (None, None)
    if k or not p.requires_key:
        try:
            live, status = fetch_provider_models(p, k, base_url or None)
        except Exception:
            live, status = None, None
    models = list(live) if live else get_models_for_provider(p)
    return {"models": models[:400], "live": bool(live), "status": status, "default": p.default_model}


def apply_choice(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    """Make this provider and model the one the next message uses: saved like the terminal does, applied to
    every running agent, and the failed or stale default runtime is dropped so the next send rebuilds."""
    from axon.providers.catalog import ProviderPreset
    from axon.ui.provider_picker import apply_provider, save_provider_choice
    model = str(body.get("model") or "").strip()
    pid = str(body.get("preset") or "").strip()
    base = str(body.get("base_url") or "").strip()
    key = str(body.get("key") or "").strip()
    if pid == "custom":
        if not re.match(r"^https?://", base):
            raise DashError("A custom endpoint needs a URL that starts with http:// or https://", 400)
        if not model:
            raise DashError("Enter the model name this endpoint serves.", 400)
        preset = ProviderPreset(id="custom", name="Custom endpoint", description="Any OpenAI-compatible endpoint", category="Custom",
                                base_url=base.rstrip("/"), default_model=model, models=[model], env_var="AXON_API_KEY",
                                requires_key=bool(key) or not _is_local(base))
    else:
        preset = _preset(pid)
        if base:
            import dataclasses
            preset = dataclasses.replace(preset, base_url=base.rstrip("/"))
        model = model or preset.default_model
    if key:
        from axon.providers.catalog import save_api_key
        try:
            save_api_key(preset.env_var or "AXON_API_KEY", key)
        except ValueError as e:
            raise DashError(str(e), 400) from None
        _mask_everywhere(key)
    effective_key = key or _saved_key(preset)
    applied = 0
    out = io.StringIO()
    with srv.lock:
        for rt in srv.all_runtimes():
            if rt.busy:
                raise DashError("A turn is running. Stop it before switching providers.", 409)
        for rt in srv.all_runtimes():
            with contextlib.redirect_stdout(out):
                if apply_provider(rt.agent, preset, model, effective_key):
                    applied += 1
    if not applied:
        save_provider_choice(preset, model)
    # Remember it for runtimes this server builds later (a new chat, a new folder): an environment default such as
    # AXON_MODEL must not quietly undo what was chosen here.
    srv.provider_choice = {"model": model, "base_url": preset.base_url, "provider": None if pid == "custom" else preset.id,
                           "api_key": effective_key or None}
    if pid == "custom":
        _fix_custom_provider(srv, preset)
    srv._settings_cache.clear()
    with srv.lock:
        dead = [k for k, r in srv.rts.items() if r is srv.default_rt and not r.session_id]
        for k in dead:
            srv.rts.pop(k, None)
            srv.default_rt = None
    ready, why = provider_ready(srv)
    return {"ok": True, "applied": applied, "provider": preset.name, "model": model, "ready": ready, "message": why or f"Using {preset.name} with {model}."}


def _is_local(url: str) -> bool:
    try:
        from axon.providers.verifier import is_local_url
        return bool(is_local_url(url))
    except Exception:
        return False


def _saved_key(preset: Any) -> str | None:
    from axon.providers.catalog import find_api_key
    return find_api_key(list(preset.env_vars)) if preset.env_vars else None


def _fix_custom_provider(srv: "DashServer", preset: Any) -> None:
    """A custom endpoint has no catalog entry: let the router pick the wire format from the URL instead of an unknown id."""
    for rt in srv.all_runtimes():
        try:
            s = rt.agent.settings
            if getattr(s, "provider", None) == "custom":
                rt.agent.settings = s.model_copy(update={"provider": None})
                if getattr(rt.agent, "permissions", None) is not None and hasattr(rt.agent.permissions, "settings"):
                    rt.agent.permissions.settings = rt.agent.settings
        except Exception:
            pass


# ─── settings (the terminal's /config panel) ───────────────────────────────

def settings_panel(srv: "DashServer", rt: Any = None) -> dict[str, Any]:
    from axon.commands.system_cmds import _panel_keys
    s = rt.agent.settings if rt is not None else srv.settings_obj()
    agent = rt.agent if rt is not None else SimpleAgent(s, srv.workspace)
    rows = []
    for key, label, choices, jkey in _panel_keys(agent):
        val = getattr(s, key, None)
        if key == "mode" and rt is None:
            val = srv.effective_mode()               # the mode a new chat starts in, as the status bar says
        rows.append({"key": key, "label": label, "type": "bool" if choices == "bool" else ("choice" if isinstance(choices, list) else "text"),
                     "choices": choices if isinstance(choices, list) else None, "value": val if not hasattr(val, "get_secret_value") else "", "json_key": jkey})
    return {"settings": rows, "scope": "user", "live": rt is not None}


class SimpleAgent:
    """Just enough of an agent for the panel's key list when no chat has started."""

    def __init__(self, settings: Any, workspace: Path) -> None:
        self.settings = settings
        self.workspace = workspace


def set_setting(srv: "DashServer", rt: Any, key: str, value: Any) -> dict[str, Any]:
    from axon.commands.system_cmds import _json_value, _panel_keys, set_config_value
    if rt is not None:
        try:
            msg = set_config_value(rt.agent, key, value)
        except ValueError as e:
            raise DashError(str(e), 400) from None
        return {"ok": True, "message": msg}
    spec = {k: (choices, jkey) for k, _l, choices, jkey in _panel_keys(SimpleAgent(srv.settings_obj(), srv.workspace))}
    if key not in spec:
        raise DashError("not an editable setting", 400)
    choices, jkey = spec[key]
    if choices == "bool":
        value = bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
    elif isinstance(choices, list) and str(value) not in choices:
        raise DashError("expected one of " + ", ".join(choices), 400)
    from axon.commands.common import update_settings_file
    if update_settings_file({jkey: _json_value(key, value)}) is None:
        raise DashError("could not write settings.json", 500)
    srv._settings_cache.clear()
    return {"ok": True, "message": f"{key} saved. It applies to the next chat."}
