"""/fast and automatic per-message effort, for every provider.

/fast
  * Claude API models with fast mode: ``speed: "fast"`` on the request (providers/speed_wire.py); the cost meter
    adds the premium; a 429 falls back to standard speed (the provider notes the cache miss).
  * every other provider: switch to that provider's ``fast_model`` (settings ``fast_models``, else the small model,
    else the smallest-looking model of its catalog, else ask once) and back on ``/fast off``.

auto_effort (setting, default off)
  * Claude models with per-message effort: one effort-only system message per new user turn, so the cached prefix
    still matches (docs: effort-per-message). It takes effect from the next user turn with new input, so a long
    tool loop keeps the level it started with. Skipped on ``between_tools`` / ``disabled`` thinking (a changed level
    is a 400 there) and on Claude models without the feature (a top-level change would restart the cache).
  * providers that take a reasoning-effort parameter on each request: first step at the configured effort, routine
    tool follow-up steps at ``low``.
  * providers or models with no effort parameter: skipped, with one line saying so.
"""
from __future__ import annotations

import re
from typing import Any

_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
_PLANNING = re.compile(r"\b(plan|design|architect\w*|refactor\w*|investigate|debug|why|migrate|implement|analy[sz]e|review|think)\b", re.I)
_SMALL = re.compile(r"(flash|mini|haiku|nano|lite|small|instant|turbo|-8b|-7b|scout)", re.I)


# ─── automatic effort ────────────────────────────────────────────────────
def routine_prompt(text: str) -> bool:
    """A short message with no planning words ('yes', 'run the tests', 'continue')."""
    t = (text or "").strip()
    return 0 < len(t) <= 120 and not _PLANNING.search(t)


def choose_level(base: str, *, iteration: int, text: str, per_step: bool) -> str:
    """Canonical level (low/medium/high/xhigh) for this call. *per_step*: the provider can vary it on every step."""
    if base not in _RANK or base == "low":
        return base
    if per_step:
        return base if iteration <= 1 else "low"
    return "low" if iteration <= 1 and routine_prompt(text) else base


def _is_anthropic(provider: Any) -> bool:
    return getattr(provider, "name", "") == "anthropic"


def prepare(agent: Any, iteration: int, user_input: str = "") -> str:
    """Set the provider's per-request speed / effort state and return the effort to pass to ``provider.stream``."""
    from axon.providers import model_caps
    from axon.providers.transport import canonical_effort
    s = agent.settings
    base = str(s.effort)
    prov = agent.provider
    try:
        prov.fast_speed = bool(getattr(agent, "fast_speed", False))
    except Exception:
        pass
    agent._effort_now = ""
    if not bool(getattr(s, "auto_effort", False)):
        try:
            prov.turn_effort = None
        except Exception:
            pass
        return base
    canon = canonical_effort(base) or "high"
    model = str(s.model)
    if not model_caps.effort_levels(model):
        if not getattr(agent, "_auto_effort_noted", False):
            agent._auto_effort_noted = True
            agent._effort_note = f"auto effort: {model} has no effort setting, skipped"
        return base
    if _is_anthropic(prov):
        if iteration <= 1:
            level = choose_level(canon, iteration=1, text=user_input or _last_user_text(agent), per_step=False)
            prov.turn_effort = (int(getattr(agent, "_turn_counter", 0)), level)
        agent._effort_now = ""    # decided and shown by the provider's marks; nothing to say per step
        return base
    level = choose_level(canon, iteration=iteration, text=user_input, per_step=True)
    agent._effort_now = level if level != canon else ""
    return level if level != canon else base


def _last_user_text(agent: Any) -> str:
    for m in reversed(list(getattr(getattr(agent, "conversation", None), "messages", []) or [])):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(str(b.get("text", "")) for b in c if isinstance(b, dict))
    return ""


def take_note(agent: Any) -> str:
    """A pending one-line note (shown once), '' when none."""
    note = getattr(agent, "_effort_note", "")
    agent._effort_note = ""
    return note


# ─── fast mode ───────────────────────────────────────────────────────────
def _provider_id(agent: Any) -> str:
    s = agent.settings
    if getattr(s, "provider", None):
        return str(s.provider)
    try:
        from axon.providers.catalog import find_preset_by_url
        p = find_preset_by_url(s.base_url)
        return p.id if p else ""
    except Exception:
        return ""


def api_fast_supported(agent: Any) -> bool:
    from axon.providers import speed_wire
    try:
        from axon.providers.transport import is_official_anthropic
        official = is_official_anthropic(agent.settings.base_url)
    except Exception:
        official = False
    return speed_wire.supports_fast(str(agent.settings.model), official=official)


def _interactive() -> bool:
    import sys
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def fast_model_for(agent: Any, *, ask: bool = False) -> str | None:
    """The model /fast switches to on this provider: configured, the small model, a small-looking catalog model."""
    s = agent.settings
    cur = str(s.model)
    pid = _provider_id(agent)
    configured = (getattr(s, "fast_models", None) or {}).get(pid)
    if configured and configured != cur:
        return configured
    small = getattr(s, "small_model", None)
    if small and small != cur:
        return small
    models: list[str] = []
    try:
        from axon.providers.catalog import get_models_for_provider, get_preset_by_id
        preset = get_preset_by_id(pid) if pid else None
        if preset is not None:
            models = [m for m in get_models_for_provider(preset, agent) if m != cur]
    except Exception:
        models = []
    for m in models:
        if _SMALL.search(m):
            return m
    if ask and models and _interactive():       # never auto-pick: a non-interactive picker returns its first entry (an expensive model)
        from axon.ui.picker import pick
        chosen = pick(models, title="Fast model for this provider (used by /fast)", current=models[0])
        if chosen:
            remember_fast_model(agent, chosen)
            return chosen
    return None


def remember_fast_model(agent: Any, model: str) -> None:
    from axon.commands.common import update_global_config, update_settings
    pid = _provider_id(agent) or "default"
    merged = {**(getattr(agent.settings, "fast_models", None) or {}), pid: model}
    update_settings(agent, fast_models=merged)
    update_global_config({"fast_models": merged})


def status_line(agent: Any) -> str:
    """What /fast actually does right now, plain text."""
    cur = str(agent.settings.model)
    if getattr(agent, "fast_speed", False):
        from axon.providers.pricing import price_for
        from axon.providers import speed_wire
        prem = speed_wire.premium(cur, price_for(cur))
        extra = f", output price x{prem}" if prem else ""
        return f"fast: API fast mode on {cur}{extra}"
    if getattr(agent, "fast_mode", False):
        return f"fast: using {cur} (was {getattr(agent, '_fast_prev_model', '') or '?'})"
    if api_fast_supported(agent):
        return f"fast: off (API fast mode is available on {cur})"
    alt = fast_model_for(agent)
    return f"fast: off ({'/fast on switches to ' + alt if alt else 'no fast model set; /fast model <id>'})"


def status_tag(agent: Any) -> str:
    """The short footer segment: 'fast', 'fast ×1.6' (API fast, output price premium), 'auto low' or ''."""
    bits = []
    if getattr(agent, "fast_speed", False):
        from axon.providers import speed_wire
        from axon.providers.pricing import price_for
        cur = str(agent.settings.model)
        prem = speed_wire.premium(cur, price_for(cur))
        bits.append(f"fast ×{prem.normalize():f}" if prem else "fast")
    elif getattr(agent, "fast_mode", False):
        bits.append("fast")     # the model name is already in the status line
    now = getattr(agent, "_effort_now", "")
    if now:
        bits.append(f"auto {now}")
    elif bool(getattr(agent.settings, "auto_effort", False)):
        bits.append("auto")
    return " · ".join(bits)


def run_fast(agent: Any, arg: str) -> None:
    """The /fast command body (output through the shared command helpers)."""
    from axon.commands.common import info, ok, usage, warn
    from axon.commands.model_cmds import switch_model
    from axon.commands.prefs_cmds import _connection_of, _restore_connection
    words = arg.strip().lower().split()
    sub = words[0] if words else "toggle"
    force_model = "model" in words[1:] or sub == "small"
    if sub == "status":
        info(status_line(agent))
        return
    if sub == "model":
        if len(words) < 2:
            warn("Usage: /fast model <model id>   (the model /fast switches to on this provider)")
            return
        remember_fast_model(agent, arg.strip().split(None, 1)[1].strip())
        ok(f"Fast model for {_provider_id(agent) or 'this provider'}: {arg.strip().split(None, 1)[1].strip()}")
        return
    if sub not in ("on", "off", "toggle", "true", "false", "1", "0", "small"):
        usage("/fast [on|off|status|model <id>]")
        return
    active = bool(getattr(agent, "fast_speed", False) or getattr(agent, "fast_mode", False))
    want = True if sub in ("on", "true", "1", "small") else False if sub in ("off", "false", "0") else not active
    if not want:
        if getattr(agent, "fast_speed", False):
            agent.fast_speed = False
            ok(f"Fast mode off: standard speed on {agent.settings.model}")
        if getattr(agent, "fast_mode", False):
            prev = getattr(agent, "_fast_prev_model", None) or agent.settings.model
            _restore_connection(agent, prev)
            agent.fast_mode = False
            ok(f"Fast mode off: back to {prev}")
        if not active:
            info("Fast mode is already off.")
        return
    if active:
        info(status_line(agent))
        return
    if not force_model and api_fast_supported(agent):
        agent.fast_speed = True
        ok(status_line(agent) + ". A 429 falls back to standard speed (prompt cache miss).")
        return
    small = fast_model_for(agent, ask=True)
    if not small:
        warn("No fast model for this provider. Set one with /fast model <id>.")
        return
    prev = agent.settings.model
    agent._fast_prev_connection = _connection_of(agent.settings)
    if switch_model(agent, small, persist=False) is False:
        return
    agent.fast_mode = True
    agent._fast_prev_model = prev
    ok(f"fast: using {small} (was {prev}; /fast off returns)")
