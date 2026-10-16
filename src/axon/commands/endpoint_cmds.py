"""/endpoint: view and change provider base URLs ("redirection") and custom OpenAI-compatible endpoints.

    /endpoint                                     menu (list without a terminal)
    /endpoint set <provider> <url>                send a provider's traffic to another URL (proxy, gateway)
    /endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--use]
    /endpoint reset <provider|name>               back to the preset default / delete a custom endpoint
    /endpoint test [provider|url]                 connectivity, latency, model count
    /endpoint use <provider|name> [--model M]     switch this session (and the default) to it

Everything is saved in ~/.axon/config.toml ([endpoints.<id>]); project files can never redirect a key.
The same engine backs ``axon endpoint ...``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from axon.commands.common import ask_text, err, header, hint, info, is_tty, ok, update_settings, usage, warn
from axon.commands.keys_cmds import _prepare_cli, parse_opts, refresh_agent_key, set_key, split_args
from axon.commands.registry import CommandResult, command
from axon.providers import endpoints as ep
from axon.providers import keystore as ks
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE

CAT = "Model & Modes"
USAGE = (
    "/endpoint                                        menu (a list without a terminal)\n"
    "/endpoint set <provider> <url>                   redirect a provider to another base URL (proxy/gateway)\n"
    "/endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--use]\n"
    "/endpoint reset <provider|name>                  preset default URL again / delete a custom endpoint\n"
    "/endpoint test [provider|url]                    connectivity, latency and model count\n"
    "/endpoint use <provider|name> [--model M]        switch to it now (also saved as the default)\n"
    "saved in ~/.axon/config.toml (user scope only)"
)
_VALUED = ("--key-env", "--env", "--env-var", "--model", "--format", "--name")


def _ws(agent: Any, workspace: Path | str | None) -> Path:
    if workspace is not None:
        return Path(workspace)
    return Path(getattr(getattr(agent, "settings", None), "workspace", None) or Path.cwd())


def _presets() -> list[Any]:
    from axon.providers.catalog import PROVIDER_PRESETS
    return list(PROVIDER_PRESETS)


def _find(token: str) -> Any:
    from axon.providers.catalog import get_preset_by_id
    t = (token or "").strip().lower()
    if not t:
        return None
    return get_preset_by_id(t) or next((p for p in _presets() if p.name.lower() == t), None) or \
        next((p for p in _presets() if t in p.id.lower() or t in p.name.lower()), None)


def _configured() -> Any:
    """Provider/base_url from ~/.axon/config.toml (used outside a session, e.g. `axon endpoint`)."""
    from types import SimpleNamespace
    try:
        data = ep._read_config(ep.axon_config_file())
    except (ValueError, OSError):
        data = {}
    return SimpleNamespace(provider=data.get("provider"), base_url=data.get("base_url", ""))


def _active_id(agent: Any) -> str:
    s = getattr(agent, "settings", None) if agent is not None else _configured()
    if s is None:
        return ""
    pid = str(getattr(s, "provider", "") or "")
    if pid and _find(pid) is not None:
        return _find(pid).id
    from axon.providers.catalog import find_preset_by_url
    p = find_preset_by_url(str(getattr(s, "base_url", "") or ""))
    return p.id if p is not None else ""


def _status(p: Any) -> str:
    if ep.is_custom(p):
        bits = ["custom"]
        if p.env_var:
            bits.append(f"key {p.env_var}")
        if p.default_model:
            bits.append(f"model {p.default_model}")
        if p.api_format != "openai":
            bits.append(p.api_format)
        return " · ".join(bits)
    if ep.is_overridden(p):
        return f"custom URL (default {ep.default_url(p.id)})"
    return "default"


def list_endpoints(agent: Any = None) -> int:
    from axon.providers.catalog import axon_config_file
    header("Endpoints", f"saved in {axon_config_file()}")
    active = _active_id(agent)
    s = getattr(agent, "settings", None)
    for p in _presets():
        mark = f"{GOLD}▸{RST}" if p.id == active else " "
        color = MINT if (ep.is_custom(p) or ep.is_overridden(p)) else SLATE
        print(f"  {mark} {WHITE}{p.name[:22]:<22}{RST} {CYAN}{p.base_url:<52}{RST} {color}{_status(p)}{RST}")
    if s is not None and active == "":
        print(f"\n  {GOLD}▸{RST} {SLATE}this session:{RST} {CYAN}{getattr(s, 'base_url', '')}{RST} {SLATE}(not a saved endpoint){RST}")
    print()
    hint("    /endpoint set <provider> <url> · /endpoint add <name> <url> --key-env VAR · /endpoint reset <provider> · "
         "/endpoint test [provider]")
    print()
    return 0


def _sync_agent(agent: Any, change: ep.EndpointChange) -> None:
    s = getattr(agent, "settings", None)
    if s is None:
        return
    on_it = ep.norm(str(getattr(s, "base_url", ""))) == ep.norm(change.old_url) or \
        str(getattr(s, "provider", "") or "").lower() == change.id
    if not on_it:
        return
    if not change.new_url:
        warn("This session still uses that endpoint; pick another one with /provider or /endpoint use <name>.")
        return
    try:
        from axon.providers.registry import provider_for
        update_settings(agent, base_url=change.new_url)
        refresh_agent_key(agent)
        agent.provider = provider_for(agent.settings.model, agent.settings)
        print(f"  {MINT}✓ This session now talks to {change.new_url}{RST}\n")
    except Exception as e:
        warn(f"Saved, but this session could not switch ({e}). Restart Axon to use it.")


def set_url(agent: Any, token: str, url: str, *, env_var: str | None = None, model: str | None = None,
            test: bool = False) -> int:
    preset = _find(token)
    if preset is None:
        err(f"Unknown provider '{token}'. To add a new endpoint: /endpoint add <name> <url>")
        return 2
    if not ep.valid_url(url):
        err(f"Not a valid http(s) URL: {url}")
        return 2
    if w := ep.url_warning(url):
        warn(w)
    try:
        change = ep.save_endpoint(preset.id, url, env_var=env_var, model=model)
    except (OSError, ValueError) as e:
        err(f"Could not save: {e}")
        return 1
    ok(f"{BOLD}{preset.name}{RST} → {CYAN}{change.new_url}{RST} {SLATE}(was {change.old_url}) · saved to {change.path}{RST}")
    if change.active_changed:
        info("Your default endpoint followed the change.")
    _sync_agent(agent, change)
    if test:
        return test_target(agent, preset.id)
    return 0


def reset(agent: Any, token: str) -> int:
    preset = _find(token)
    pid = preset.id if preset is not None else token.lower()
    try:
        change = ep.reset_endpoint(pid)
    except (OSError, ValueError) as e:
        err(f"Could not save: {e}")
        return 1
    if change is None:
        info(f"{token} has no saved endpoint override; it already uses its default.")
        return 0
    if change.new_url:
        ok(f"{preset.name if preset else pid} is back to its default {CYAN}{change.new_url}{RST}")
    else:
        ok(f"Removed the custom endpoint {BOLD}{pid}{RST}")
    _sync_agent(agent, change)
    return 0


def add(agent: Any, ws: Path, name: str, url: str, *, env_var: str | None, model: str | None, fmt: str | None,
        display: str | None, use: bool, interactive: bool) -> int:
    eid = ep.slug(name)
    existing = _find(eid)
    if existing is not None and not ep.is_custom(existing) and existing.id == eid:
        info(f"'{eid}' is a built-in provider; changing its URL instead.")
        return set_url(agent, eid, url, env_var=env_var, model=model)
    if not ep.valid_url(url):
        err(f"Not a valid http(s) URL: {url}")
        return 2
    if w := ep.url_warning(url):
        warn(w)
    from axon.providers.transport import is_local_url
    if env_var is None:
        env_var = "" if is_local_url(url) else ep.suggest_env_var(eid)
    if env_var.lower() in ("-", "none", "no"):
        env_var = ""
    try:
        change = ep.save_endpoint(eid, url, env_var=env_var, model=model or "", name=display or "",
                                  api_format=fmt or None)
    except (OSError, ValueError) as e:
        err(f"Could not save: {e}")
        return 1
    ok(f"Added endpoint {BOLD}{eid}{RST} → {CYAN}{url}{RST} {SLATE}· saved to {change.path}{RST}")
    if env_var:
        st = ks.key_status(env_var, ws)
        print(f"  {SLATE}Its key is read from {WHITE}{env_var}{RST}{SLATE}: "
              f"{(st.masked + ' (' + st.source_label + ')') if st.is_set else 'not set yet'}{RST}")
        if not st.is_set:
            if interactive:
                set_key(agent, ws, env_var, interactive=True)
            else:
                hint(f"    Add it with: /keys set {env_var} <key>   (or axon keys set {env_var})")
    else:
        print(f"  {SLATE}No API key is sent to this endpoint.{RST}")
    if interactive:
        test_target(agent, eid)
        from axon.commands.common import confirm
        use = use or confirm(f"Switch to {eid} now?", default=False)
    if use:
        return use_endpoint(agent, eid, model=model, interactive=interactive)
    hint(f"    Switch to it with /endpoint use {eid}")
    print()
    return 0


def _key_for(preset: Any, agent: Any) -> str:
    if preset is None:
        k = getattr(getattr(agent, "settings", None), "api_key", None)
        return k.get_secret_value() if hasattr(k, "get_secret_value") else ""
    if not preset.requires_key:
        return ""
    return ks.preset_status(preset).value


def test_target(agent: Any, token: str = "") -> int:
    from axon.providers.catalog import find_preset_by_url
    if token and ep.valid_url(token):
        url, preset = token, find_preset_by_url(token)
    elif token:
        preset = _find(token)
        if preset is None:
            err(f"Unknown provider '{token}'.")
            return 2
        url = preset.base_url
    else:
        s = getattr(agent, "settings", None)
        if s is None:
            try:
                from axon.config import Settings
                s = Settings.load(require_api_key=False)
            except Exception:
                s = None
        url = str(getattr(s, "base_url", "") or "")
        preset = _find(_active_id(agent)) if agent is not None else find_preset_by_url(url)
        if not url:
            err("No endpoint configured.")
            return 2
    key = _key_for(preset, agent)
    fmt = getattr(preset, "api_format", "openai") if preset is not None else "openai"
    print(f"  {SLATE}Testing {url} …{RST}", flush=True)
    res = ep.test_endpoint(url, key, fmt)
    bits = [f"{res.latency_s * 1000:.0f} ms"]
    if res.models is not None:
        bits.append(f"{res.models} models")
    if res.ok:
        print(f"  {MINT}✓ Reachable{RST} {SLATE}· {' · '.join(bits)}{RST}\n")
        return 0
    color = GOLD if res.reachable else ROSE
    print(f"  {color}{'!' if res.reachable else '✗'}{RST} {WHITE}{res.message}{RST} {SLATE}· {bits[0]}{RST}")
    if res.status in (401, 403) and preset is not None and preset.requires_key:
        hint(f"    Fix the key with /keys set {preset.id}")
    print()
    return 1


test_target.__test__ = False  # type: ignore[attr-defined]


def use_endpoint(agent: Any, token: str, *, model: str | None = None, interactive: bool = False) -> int:
    preset = _find(token)
    if preset is None:
        err(f"Unknown provider or endpoint '{token}'.")
        return 2
    cur_model = str(getattr(getattr(agent, "settings", None), "model", "") or "")
    chosen = model or preset.default_model or ""
    if not chosen and interactive:
        chosen = ask_text(f"Model to use on {preset.name}{(' [' + cur_model + ']') if cur_model else ''}:") or cur_model
    chosen = chosen or cur_model
    if not chosen:
        err(f"Pass a model: /endpoint use {preset.id} --model <id>")
        return 2
    key = _key_for(preset, agent)
    if preset.requires_key and not key:
        warn(f"No key for {preset.name} yet: /keys set {preset.id}")
    from axon.commands.common import update_global_config
    if not update_global_config({"provider": preset.id, "base_url": preset.base_url, "model": chosen}):
        warn("Could not update ~/.axon/config.toml (malformed?); this session switches anyway.")
    if agent is not None and getattr(agent, "settings", None) is not None:
        try:
            from pydantic import SecretStr

            from axon.providers.registry import provider_for
            update_settings(agent, provider=preset.id, base_url=preset.base_url, model=chosen,
                            api_key=SecretStr(key or "local"))
            agent.provider = provider_for(chosen, agent.settings)
            try:
                agent.session.model = chosen
            except Exception:
                pass
        except Exception as e:
            err(f"Saved, but this session could not switch: {e}")
            return 1
    ok(f"Using {BOLD}{preset.name}{RST} · {WHITE}{chosen}{RST} {SLATE}({preset.base_url}) · saved as default{RST}")
    return 0


# ─── interactive menu ──────────────────────────────────────────────────────

def _menu(agent: Any, ws: Path) -> int:
    from axon.ui.picker import SelectItem, Selector, choose
    index = 0
    while True:
        active = _active_id(agent)
        items = [SelectItem(label="+ Add a custom endpoint…", value=("add", None),
                            hint="any OpenAI- or Anthropic-compatible URL", group="Actions")]
        for p in _presets():
            state = _status(p)
            badge = f"{MINT}●{RST}" if p.id == active else (f"{GOLD}◆{RST}" if state != "default" else f"{DARK_SLATE}○{RST}")
            items.append(SelectItem(label=p.name + ("  (active)" if p.id == active else ""), value=("preset", p),
                                    hint=f"{p.base_url}" + ("" if state == "default" else f" · {state}"),
                                    badge=badge, group=p.category, search=p.id))
        picked = Selector(items, title="Endpoints", subtitle="Enter: change · type to filter · Esc: close",
                          index=min(index, len(items) - 1), filterable=True, max_visible=16).run()
        if not isinstance(picked, SelectItem):
            return 0
        index = items.index(picked) if picked in items else 0
        kind, p = picked.value
        if kind == "add":
            _interactive_add(agent, ws)
            continue
        opts: list[tuple[str, Any, str]] = [("Change base URL…", "url", p.base_url), ("Test connectivity", "test", "")]
        if ep.is_overridden(p):
            opts.append(("Reset to preset default", "reset", ep.default_url(p.id) or ""))
        if ep.is_custom(p):
            opts.append(("Remove this endpoint", "reset", "deletes it from config.toml"))
        if p.requires_key:
            opts.append(("API key…", "key", ks.preset_status(p, ws).masked or "not set"))
        opts += [("Use it now", "use", "switch this session and save as default"), ("Back", None, "")]
        action = choose(opts, title=p.name, subtitle=p.base_url)
        if action == "url":
            new = ask_text(f"New base URL for {p.name} (Enter keeps {p.base_url}):")
            if new and set_url(agent, p.id, new) == 0:
                test_target(agent, p.id)
        elif action == "test":
            test_target(agent, p.id)
        elif action == "reset":
            reset(agent, p.id)
        elif action == "key":
            set_key(agent, ws, p.id, interactive=True)
        elif action == "use":
            use_endpoint(agent, p.id, interactive=True)


def _interactive_add(agent: Any, ws: Path) -> int:
    url = ask_text("Base URL (e.g. https://llm.example.com/v1):")
    if not url:
        return 1
    if not ep.valid_url(url):
        err(f"Not a valid http(s) URL: {url}")
        return 2
    name = ask_text(f"Short name [{ep.suggest_id(url)}]:") or ep.suggest_id(url)
    from axon.providers.transport import is_local_url
    default_var = "" if is_local_url(url) else ep.suggest_env_var(ep.slug(name))
    var = ask_text(f"Environment variable holding its key [{default_var or 'none'}] ('-' for no key):") or default_var
    model = ask_text("Default model id (optional):")
    from axon.ui.picker import choose
    fmt = choose([("OpenAI-compatible (/chat/completions)", "openai"), ("Anthropic-compatible (/v1/messages)", "anthropic")],
                 title="API format", index=0) or "openai"
    return add(agent, ws, name, url, env_var=var, model=model or None, fmt=fmt, display=None, use=False, interactive=True)


# ─── entry points ──────────────────────────────────────────────────────────

def run_endpoint(arg: str | Sequence[str] = "", agent: Any = None, workspace: Path | str | None = None) -> int:
    ws = _ws(agent, workspace)
    words, flags, opts = parse_opts(split_args(arg), valued=_VALUED)
    env_var = opts.get("--key-env", opts.get("--env", opts.get("--env-var")))
    model = opts.get("--model") or None
    fmt = (opts.get("--format") or "").lower() or None
    if fmt and fmt not in ("openai", "anthropic"):
        err("--format must be openai or anthropic")
        return 2
    if "--help" in flags or (words and words[0].lower() in ("help", "-h")):
        usage(USAGE)
        return 0
    if not words:
        return _menu(agent, ws) if is_tty() and "--list" not in flags else list_endpoints(agent)
    sub, rest = words[0].lower(), words[1:]
    if sub in ("list", "ls", "show", "where"):
        return list_endpoints(agent)
    if sub in ("set", "url", "redirect"):
        if len(rest) < 2:
            usage("/endpoint set <provider> <url>")
            return 2
        return set_url(agent, rest[0], rest[1], env_var=env_var, model=model, test="--test" in flags)
    if sub in ("add", "new", "custom"):
        if len(rest) < 2:
            if is_tty() and not rest:
                return _interactive_add(agent, ws)
            usage("/endpoint add <name> <url> [--key-env VAR] [--model M] [--format openai|anthropic] [--use]")
            return 2
        return add(agent, ws, rest[0], rest[1], env_var=env_var, model=model, fmt=fmt, display=opts.get("--name"),
                   use="--use" in flags, interactive=False)
    if sub in ("reset", "default", "remove", "rm", "delete"):
        if not rest:
            usage("/endpoint reset <provider|name>")
            return 2
        return reset(agent, rest[0])
    if sub in ("test", "check", "ping"):
        return test_target(agent, rest[0] if rest else "")
    if sub in ("use", "switch"):
        if not rest:
            usage("/endpoint use <provider|name> [--model M]")
            return 2
        return use_endpoint(agent, rest[0], model=model, interactive=is_tty())
    if len(words) >= 2 and ep.valid_url(words[1]) and _find(words[0]) is not None:
        return set_url(agent, words[0], words[1], env_var=env_var, model=model)
    usage(USAGE)
    return 2


@command("endpoint", category=CAT, description="Provider base URLs: redirect to a proxy, add custom endpoints, test, reset",
         aliases=("endpoints", "base-url", "baseurl"), usage=USAGE)
def handle_endpoint(agent: Any, arg: str) -> CommandResult:
    code = run_endpoint(arg, agent=agent)
    return CommandResult(handled=True, success=code == 0)


def endpoint_main(argv: list[str]) -> int:
    """`axon endpoint ...` (no REPL)."""
    _prepare_cli()
    return run_endpoint(list(argv), agent=None, workspace=Path.cwd())


__all__ = ["USAGE", "add", "endpoint_main", "handle_endpoint", "list_endpoints", "reset", "run_endpoint", "set_url",
           "test_target", "use_endpoint"]
