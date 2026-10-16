"""Model & mode commands: /model /provider /effort /mode /plan /thinking /trace (/keys lives in keys_cmds)."""
from __future__ import annotations

import os
import random
from typing import Any

from pydantic import SecretStr

from axon.commands.common import (
    err, header, hint, info, is_cancel_word, is_tty, kv, lookup_key, ok, save_env_key,
    update_global_config, update_settings, usage, warn,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, GOLD, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Model & Modes"
_SMALL_HINTS = ("0.5b", "1.5b", "1b", "2b", "3b", "3.8b", "7b", "8b", "mini", "flash", "haiku", "lite", "nano")


def _pick(options: list[str], title: str, current: str | None = None) -> str | None:
    from axon.ui.picker import pick
    return pick(options, title=title, current=current)


def save_or_update_env_key(env_var: str, key_val: str, agent: Any = None) -> bool:
    """Persist an API key and, if it belongs to the active provider, apply it now."""
    if not save_env_key(env_var, key_val):
        return False
    if agent is not None:
        try:
            from axon.providers.catalog import find_preset_by_url
            from axon.providers.registry import provider_for
            preset = find_preset_by_url(agent.settings.base_url)
            if preset is not None and (preset.env_var or "AXON_API_KEY") == env_var:
                update_settings(agent, api_key=SecretStr(os.environ[env_var]))
                agent.provider = provider_for(agent.settings.model, agent.settings)
        except Exception:
            pass
    return True


def _key_for_preset(preset: Any) -> str:
    var = getattr(preset, "env_var", None) or "AXON_API_KEY"
    key = lookup_key(var)
    if not key and getattr(preset, "id", "") == "agentrouter":
        key = lookup_key("AXON_API_KEY")
    return key


def _verify_and_save(preset: Any, env_var: str, key: str, agent: Any = None) -> bool:
    try:
        from axon.providers.verifier import verify_api_key
        print(f"  {SLATE}Verifying key with {preset.name}…{RST}", end="", flush=True)
        good, msg = verify_api_key(preset, key)
    except Exception as e:
        good, msg = False, str(e)
    if not good:
        print(f"\r\033[K  {ROSE}✗ Key rejected by {preset.name}:{RST} {WHITE}{msg}{RST}\n  {SLATE}Not saved.{RST}")
        return False
    print(f"\r\033[K  {MINT}✓ Key verified{RST}")
    save_or_update_env_key(env_var, key, agent=agent)
    print(f"  {MINT}✓ Saved {env_var} to ~/.axon/.env{RST}")
    return True


def switch_model(agent: Any, chosen: str, preset: Any = None, persist: bool = True) -> bool:
    """Switch model (and endpoint/key when the model belongs to another provider).

    ``persist=False`` keeps the change to this session (used by /fast)."""
    from axon.providers.registry import provider_for
    try:
        from axon.providers.catalog import find_preset_for_model
        preset = preset or find_preset_for_model(chosen)
    except Exception:
        preset = preset or None
    base_url = agent.settings.base_url
    key: SecretStr = agent.settings.api_key
    if preset is not None:
        if preset.id != "openrouter" and chosen.lower().startswith(f"{preset.id}/"):
            chosen = chosen[len(preset.id) + 1:]
        if preset.base_url.rstrip("/") != str(agent.settings.base_url).rstrip("/"):
            base_url = preset.base_url
        if getattr(preset, "requires_key", True):
            var = preset.env_var or "AXON_API_KEY"
            k = _key_for_preset(preset)
            if not k and is_tty():
                from axon.commands.keys_cmds import ask_secret
                entered = ask_secret(f"API key for {preset.name} ({var}, input hidden, Enter skips):")
                if not is_cancel_word(entered) and _verify_and_save(preset, var, entered):
                    k = entered
            if not k:
                warn(f"No API key for {preset.name}; set {var} or run /keys {preset.id}")
            key = SecretStr(k or "local")
        else:
            key = SecretStr("local")
    update_settings(agent, model=chosen, base_url=base_url, api_key=key)
    try:
        agent.provider = provider_for(chosen, agent.settings)
    except Exception as e:
        err(f"Could not create provider for {chosen}: {e}")
        return False
    try:
        agent.session.model = chosen
    except Exception:
        pass
    if persist:
        update_global_config({"model": chosen, "base_url": base_url})
    via = f" {SLATE}via {preset.name} ({base_url}){RST}" if preset is not None else f" {SLATE}({base_url}){RST}"
    print(f"\n  {TEAL}✓ Model:{RST} {BOLD}{WHITE}{chosen}{RST}{via}\n")
    return True


def _curated(agent: Any) -> list[tuple[str, str, str]]:
    try:
        from axon.providers.catalog import get_curated_model_choices
        return list(get_curated_model_choices(agent.settings.base_url))
    except Exception:
        return []


@command("model", category=CAT, description="Switch model (picker, name, 'random' or 'random:small')",
         usage="/model                 interactive picker\n/model <name>          switch (endpoint follows the model's provider)\n/model random[:small]  surprise me")
def handle_model(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    if not a:
        try:
            from axon.ui.model_picker import run_model_picker
            run_model_picker(agent)
        except Exception as e:
            kv("Current model", agent.settings.model)
            hint(f"    (picker unavailable: {e}) use /model <name>")
            print()
        return CommandResult(handled=True)
    low = a.lower()
    if low in ("random", "rand", "shuffle", "surprise", "random:small", "random-small", "small"):
        pool = [m for m, _, _ in _curated(agent)]
        if low in ("random:small", "random-small", "small"):
            pool = [m for m in pool if any(k in m.lower() for k in _SMALL_HINTS)] or pool
        if not pool:
            err("No models available from linked providers. Use /provider to connect one.")
            return CommandResult(handled=True)
        a = random.choice(pool)
        print(f"\n  {GOLD}🎲 Picked {BOLD}{a}{RST}")
    if a == agent.settings.model:
        info(f"Already using {a}.")
        return CommandResult(handled=True)
    switch_model(agent, a)
    return CommandResult(handled=True)


@command("provider", category=CAT, description="Connect or switch provider (API key + endpoint)", aliases=("providers",))
def handle_provider(agent: Any, arg: str = "") -> CommandResult:
    from axon.ui.provider_picker import run_provider_picker
    run_provider_picker(agent)
    return CommandResult(handled=True)


_EFFORTS = ["reflex", "balanced", "synapse", "quantum"]
_EFFORT_ALIASES = {
    "reflex": "reflex", "low": "reflex", "fast": "reflex",
    "balanced": "balanced", "medium": "balanced", "normal": "balanced",
    "synapse": "synapse", "high": "synapse", "deep": "synapse",
    "quantum": "quantum", "xhigh": "quantum", "max": "quantum", "hyper": "quantum",
}
_EFFORT_DESC = {"reflex": "fast, minimal reasoning", "balanced": "standard depth", "synapse": "deep analysis", "quantum": "maximum reasoning"}


@command("effort", category=CAT, description="Reasoning effort: reflex | balanced | synapse | quantum",
         usage="/effort [reflex|balanced|synapse|quantum]   (aliases: low, medium, high, max)\n/effort auto      back to the default effort\n/effort status    show the current effort")
def handle_effort(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if a in ("status", "show", "current"):
        cur = str(agent.settings.effort)
        info(f"Effort: {BOLD}{cur}{RST} {SLATE}({_EFFORT_DESC.get(cur, '')}){RST}")
        return CommandResult(handled=True)
    if a in ("auto", "default", "reset"):
        a = str(type(agent.settings).model_fields["effort"].default)
    chosen = _EFFORT_ALIASES.get(a) if a else None
    if a and not chosen:
        usage("/effort [reflex|balanced|synapse|quantum]")
        return CommandResult(handled=True)
    if not chosen:
        opts = [f"{e:<9} {_EFFORT_DESC[e]}" for e in _EFFORTS]
        cur = _EFFORT_ALIASES.get(str(agent.settings.effort).lower(), "quantum")
        sel = _pick(opts, "Reasoning effort", current=opts[_EFFORTS.index(cur)])
        chosen = sel.split()[0] if sel else None
    if not chosen or chosen == agent.settings.effort:
        info(f"Effort unchanged: {agent.settings.effort}")
        return CommandResult(handled=True)
    update_settings(agent, effort=chosen)
    ok(f"Effort set to {BOLD}{chosen}{RST} {SLATE}({_EFFORT_DESC[chosen]}){RST}")
    return CommandResult(handled=True)


_MODES = ["default", "acceptEdits", "plan", "bypass"]
_MODE_ALIASES = {
    "default": "default", "manual": "default", "ask": "default", "normal": "default",
    "acceptedits": "acceptEdits", "accept": "acceptEdits", "auto": "acceptEdits", "edits": "acceptEdits",
    "plan": "plan", "readonly": "plan",
    "bypass": "bypass", "bypasspermissions": "bypass", "yolo": "bypass",
}
_MODE_DESC = {
    "default": "ask before edits and commands",
    "acceptEdits": "auto-approve file edits in the workspace",
    "plan": "read-only exploration, no changes",
    "bypass": "approve everything (deny rules still apply)",
}


def set_mode(agent: Any, mode: str) -> None:
    update_settings(agent, mode=mode)
    color = {"default": SLATE, "acceptEdits": MINT, "plan": GOLD, "bypass": ROSE}.get(mode, WHITE)
    print(f"\n  {TEAL}✓ Permission mode:{RST} {color}{BOLD}{mode}{RST} {SLATE}— {_MODE_DESC[mode]}{RST}\n")


@command("mode", category=CAT, description="Permission mode: default | acceptEdits | plan | bypass",
         usage="/mode [default|acceptEdits|plan|bypass]   (Shift+Tab cycles)")
def handle_mode(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    chosen = _MODE_ALIASES.get(a.lower()) if a else None
    if a and not chosen:
        err(f"Unknown mode '{a}'. Valid: {', '.join(_MODES)}")
        return CommandResult(handled=True)
    if not chosen:
        opts = [f"{m:<12} {_MODE_DESC[m]}" for m in _MODES]
        cur = agent.settings.mode if agent.settings.mode in _MODES else "default"
        sel = _pick(opts, "Permission mode", current=opts[_MODES.index(cur)])
        chosen = sel.split()[0] if sel else None
    if chosen:
        set_mode(agent, chosen)
    return CommandResult(handled=True)


@command("plan", category=CAT, description="Enter plan mode (read-only); '/plan off' leaves it",
         usage="/plan              enter plan mode\n/plan <task>       enter plan mode and start planning the task\n/plan off          back to default mode")
def handle_plan(agent: Any, arg: str) -> CommandResult:
    a = arg.strip()
    if a.lower() in ("off", "exit", "stop", "default"):
        set_mode(agent, "default")
        return CommandResult(handled=True)
    if agent.settings.mode != "plan":
        set_mode(agent, "plan")
    elif not a:
        info("Plan mode is already on. '/plan off' to leave it.")
    if a and a.lower() not in ("on", "mode", "enable"):
        agent.run_turn(a)
    return CommandResult(handled=True)


@command("thinking", category=CAT, description="Toggle streaming of model reasoning", aliases=("thought",),
         usage="/thinking [on|off]")
def handle_thinking(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if a in ("on", "true", "1", "yes"):
        val = True
    elif a in ("off", "false", "0", "no"):
        val = False
    else:
        val = not bool(agent.settings.thinking)
    update_settings(agent, thinking=val)
    print(f"\n  {PURPLE}✻ Thinking display {'ON' if val else 'OFF'}{RST} {SLATE}{'(streaming thoughts)' if val else '(summary only)'}{RST}\n")
    return CommandResult(handled=True)


TRACE_LEVELS = ("compact", "normal", "detailed", "debug")


def current_trace_level(agent: Any) -> str:
    r = getattr(agent, "renderer", None)
    for obj, attr in ((r, "trace_level"), (agent.settings, "trace_level")):
        v = getattr(obj, attr, None) if obj is not None else None
        if isinstance(v, str) and v:
            return v
    return "normal"


@command("trace", category=CAT, description="Trace detail: compact | normal | detailed | debug",
         usage="/trace [compact|normal|detailed|debug]\n/trace last       same as /inspect")
def handle_trace(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if a in ("last", "inspect"):
        from axon.commands.agent_cmds import handle_inspect
        return handle_inspect(agent, "")
    if not a:
        cur = current_trace_level(agent)
        header("Trace level", cur)
        for lv, d in (("compact", "one line per tool + footer"), ("normal", "step timeline with args, results, permissions"),
                      ("detailed", "+ full tool input, result heads, prompt breakdown"), ("debug", "+ raw request and usage")):
            mark = f"{MINT}●{RST}" if lv == cur else " "
            print(f"    {mark} {WHITE}{lv:<9}{RST} {SLATE}{d}{RST}")
        print()
        return CommandResult(handled=True)
    if a not in TRACE_LEVELS:
        usage("/trace [compact|normal|detailed|debug]")
        return CommandResult(handled=True)
    r = getattr(agent, "renderer", None)
    setter = getattr(r, "set_trace_level", None)
    if callable(setter):
        setter(a)
    elif r is not None:
        try:
            r.trace_level = a
        except Exception:
            pass
    if "trace_level" in type(agent.settings).model_fields:
        update_settings(agent, trace_level=a)
    ok(f"Trace level: {BOLD}{a}{RST}")
    return CommandResult(handled=True)
