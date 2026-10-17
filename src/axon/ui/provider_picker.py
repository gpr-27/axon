"""
"Connect a provider" picker: choose a preset, supply/verify its API key, pick a model, apply it.

Also hosts the single implementation of "apply provider + model to the agent and persist it",
shared with axon.ui.model_picker.
"""
from __future__ import annotations

import getpass
import sys
import os
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from axon import platform as plat
from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, is_provider_linked
from axon.ui.picker import SelectItem, Selector, pick
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE

if TYPE_CHECKING:
    from axon.agent.loop import Agent

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag

CUSTOM_MODEL_OPTION = "✏️  Enter custom model name..."
RANDOM_MODEL_OPTION = "🎲  Random model (from this provider)"


# ── shared helpers ───────────────────────────────────────────────────────────

def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def preset_env_vars(preset: ProviderPreset) -> list[str]:
    names = list(getattr(preset, "env_vars", ()) or ())
    if preset.env_var and preset.env_var not in names:
        names.insert(0, preset.env_var)
    if not names:
        names = ["AXON_API_KEY" if preset.id == "agentrouter" else f"{preset.id.upper()}_API_KEY"]
    return names


def find_key(preset: ProviderPreset) -> str | None:
    try:
        from axon.providers.catalog import find_api_key
        return find_api_key(preset_env_vars(preset))
    except (ImportError, AttributeError):
        for var in preset_env_vars(preset):
            val = os.environ.get(var, "").strip()
            if val and not val.startswith("/") and val.lower() not in ("none", "null", "local", "skip"):
                return val
        return None


def store_key(env_var: str, value: str) -> Path | None:
    """Persist an API key to ~/.axon/.env (chmod 600). Returns the file or None on failure."""
    try:
        from axon.providers.catalog import save_api_key
        return save_api_key(env_var, value)
    except (ImportError, AttributeError):
        pass
    except Exception:
        return None
    from axon.storage import user_file
    path = user_file(".env")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [ln for ln in (path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else [])
                 if ln.split("=", 1)[0].strip().removeprefix("export ").strip() != env_var]
        lines.append(f'{env_var}="{value}"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        os.environ[env_var] = value
        return path
    except OSError:
        return None


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def save_provider_choice(preset: ProviderPreset, model: str) -> Path | None:
    """Record base_url/model (and custom models) in ~/.axon/config.toml."""
    from axon.storage import user_file
    cfg_file = user_file("config.toml")
    try:
        import tomli_w
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_toml(cfg_file) if cfg_file.exists() else {}
        cfg["base_url"] = preset.base_url
        cfg["model"] = model
        if model not in preset.models:
            customs = [c for c in cfg.get("custom_models", []) if isinstance(c, dict)]
            if not any(c.get("model") == model and c.get("provider") == preset.id for c in customs):
                customs.append({"model": model, "provider": preset.id})
            cfg["custom_models"] = customs
        with open(cfg_file, "wb") as f:
            tomli_w.dump(cfg, f)
        return cfg_file
    except Exception:
        return None


def same_endpoint(a: str, b: str) -> bool:
    x, y = (a or "").rstrip("/").lower(), (b or "").rstrip("/").lower()
    return bool(x and y) and (x == y or x in y or y in x)


def apply_provider(agent: Agent, preset: ProviderPreset, model: str, api_key: str | None) -> bool:
    """Switch the running agent to preset+model, persist the choice and print a summary."""
    from axon.providers.registry import provider_for
    key = api_key or "local"
    new_settings = agent.settings.model_copy(update={"model": model, "base_url": preset.base_url, "api_key": SecretStr(key)})
    agent.settings = new_settings
    agent.provider = provider_for(model, new_settings)
    if getattr(agent, "permissions", None) is not None and hasattr(agent.permissions, "settings"):
        try:
            agent.permissions.settings = new_settings
        except Exception:
            pass
    cfg = save_provider_choice(preset, model)
    print(f"\n  {MINT}{BOLD}✓ Configured active provider: {preset.name}!{RST}")
    print(f"  {SLATE}Active Model: {BOLD}{WHITE}{model}{RST} · {SLATE}Endpoint: {preset.base_url}{RST}")
    if cfg:
        print(f"  {DARK_SLATE}Saved as default in {cfg}{RST}")
    if preset.id == "ollama":
        try:
            from axon.providers.catalog import fetch_local_ollama_models
            if not fetch_local_ollama_models(preset.base_url):
                print(f"  {GOLD}⚠ Ollama is not answering on {preset.base_url}. Start it with {WHITE}ollama serve{RST}")
        except Exception:
            pass
    print()
    return True


def choose_model_option(preset: ProviderPreset, option: str) -> str | None:
    """Resolve the special custom/random options into a concrete model name."""
    if option.startswith("✏️"):
        if not _stdin_is_tty():
            return None
        try:
            typed = input(f"  {BOLD}{WHITE}Model name for {preset.name}: {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            typed = ""
        return typed or None
    if option.startswith("🎲"):
        avail = [m for m in preset.models if m]
        chosen = random.choice(avail) if avail else preset.default_model
        print(f"  {GOLD}🎲 Random model: {BOLD}{chosen}{RST}")
        return chosen
    return option


# ── picker ──────────────────────────────────────────────────────────────────

def _provider_items(agent: Agent) -> list[SelectItem]:
    items: list[SelectItem] = []
    for p in PROVIDER_PRESETS:
        try:
            linked = is_provider_linked(p, agent)
        except Exception:
            linked = False
        active = same_endpoint(agent.settings.base_url, p.base_url)
        badge = f"{MINT}✓{RST}" if linked else f"{DARK_SLATE}○{RST}"
        hint = ("active · " if active else ("linked · " if linked else "")) + p.description
        items.append(SelectItem(label=p.name, value=p, hint=hint, group=p.category, badge=badge,
                                search=" ".join([p.id, p.category, *p.models])))
    return items


def run_provider_picker(agent: Agent) -> bool:
    """Interactive provider connector. Returns True if the provider was switched."""
    if not plat.is_interactive():
        if not _stdin_is_tty():
            # Nothing to choose with: never switch providers (or prompt for keys) blindly.
            print(f"  {SLATE}The provider picker needs an interactive terminal. Configure a provider with "
                  f"/model <name> and its API key environment variable instead.{RST}")
            return False
        return _fallback_provider_picker(agent)
    items = _provider_items(agent)
    start = next((i for i, it in enumerate(items) if same_endpoint(agent.settings.base_url, it.value.base_url)), 0)
    chosen = Selector(items, title="Connect a provider", index=start, filterable=True,
                      placeholder="type to search providers and models", max_visible=14).run()
    if not isinstance(chosen, SelectItem):
        return False
    return _configure_and_apply_preset(agent, chosen.value)


def _existing_key_menu(preset: ProviderPreset, env_var: str, key: str) -> str | None:
    """A key is already set: use it, test it, or replace it. Returns the key to use ('' = enter a new
    one) or None when cancelled."""
    try:
        from axon.providers import keystore as ks
        masked = ks.mask_key(key)
        where = ks.key_status(env_var).source_label or "environment"
    except Exception:
        ks, masked, where = None, "set", "environment"
    from axon.ui.picker import choose
    while True:
        action = choose([("Use the existing key", "use", f"{env_var} {masked} · {where}"),
                         ("Test it", "test", "live 5-token request"),
                         ("Replace it with a new key", "replace", "hidden input, verified before saving"),
                         ("Cancel", None, "")], title=f"{preset.name}: API key", index=0)
        if action is None:
            return None
        if action == "use":
            return key
        if action == "replace":
            return ""
        if ks is not None:
            res = ks.test_key(preset, key)
            ks.record_check(env_var, key, res.ok, res.latency_s)
            mark = f"{MINT}✓ works{RST} {SLATE}· {res.summary()}" if res.ok else f"{ROSE}✗ {res.message}"
            print(f"  {mark}{RST}")


def _configure_and_apply_preset(agent: Agent, preset: ProviderPreset) -> bool:
    """Connect to *preset*: API key -> pick a model (from the provider's live list when it has one) -> verify the
    key with that model -> save the key and switch. The key is checked against the model you will actually use."""
    print(f"\n  {GOLD}◆{RST} {BOLD}Connecting to {preset.name}{RST}  {SLATE}{preset.base_url}{RST}")
    api_key: str | None = None
    new_key = False
    env_var = preset_env_vars(preset)[0] if preset.requires_key else ""
    if preset.requires_key:
        api_key = find_key(preset)
        if api_key and plat.is_interactive():
            api_key = _existing_key_menu(preset, env_var, api_key)
            if api_key is None:
                print(f"\n  {SLATE}Connection cancelled.{RST}\n")
                return False
        if api_key:
            print(f"  {MINT}✓ Using existing API key ({env_var}){RST}")
        else:
            if getattr(preset, "docs_url", ""):
                print(f"  {SLATE}Get a key at {preset.docs_url}{RST}")
            if not _stdin_is_tty():
                print(f"  {ROSE}No terminal to read the API key from.{RST} {SLATE}Set {env_var} "
                      f"(or run /keys {preset.id} <key>) and try again.{RST}\n")
                return False
            try:
                entered = getpass.getpass(f"  {BOLD}{WHITE}API key for {preset.name} ({env_var}, hidden): {RST}").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {SLATE}Connection cancelled.{RST}\n")
                return False
            if not entered:
                print(f"  {ROSE}An API key is required for {preset.name}.{RST}\n")
                return False
            api_key, new_key = entered, True

    models = _models_for(preset, api_key)
    if models is None:
        return False
    while True:
        selection = pick(models + [CUSTOM_MODEL_OPTION, RANDOM_MODEL_OPTION], title=f"Model for {preset.name}",
                         current=preset.default_model if preset.default_model in models else (models[0] if models else None))
        if not selection:
            print(f"\n  {SLATE}Connection cancelled.{RST}\n")
            return False
        model = choose_model_option(preset, selection) or preset.default_model
        if not new_key:
            break
        ok, msg = _verify(preset, api_key or "", model)
        if ok:
            print(f"  {MINT}✓ API key verified with {model}{RST}")
            saved = store_key(env_var, api_key or "")
            if saved:
                print(f"  {MINT}✓ Saved {env_var} to {saved} (owner-only){RST}")
            break
        print(f"  {ROSE}✗ Check with {model} failed:{RST} {WHITE}{msg}{RST}")
        if not _is_model_problem(msg):
            print()
            return False
        print(f"  {SLATE}The key works, but this model is not available to it. Pick another model.{RST}")
    return apply_provider(agent, preset, model, api_key)


def _models_for(preset: ProviderPreset, api_key: str | None) -> list[str] | None:
    """Models to offer: the provider's live list for this key, else the built-in list. None = key rejected."""
    models: list[str] = []
    if preset.id == "ollama":
        try:
            from axon.providers.catalog import fetch_local_ollama_models
            models = list(fetch_local_ollama_models(preset.base_url))
        except Exception:
            models = []
    elif api_key or not preset.requires_key:
        from axon.providers.catalog import fetch_provider_models
        print(f"  {SLATE}Loading the models this key can use…{RST}", end="", flush=True)
        live, status = fetch_provider_models(preset, api_key or "")
        print("\r\033[K", end="")
        if status in (401, 403):
            print(f"  {ROSE}✗ {preset.name} rejected the API key (HTTP {status}).{RST} "
                  f"{SLATE}Check it at {preset.docs_url or preset.base_url}{RST}\n")
            return None
        if live:
            print(f"  {MINT}✓ {len(live)} chat model{'s' if len(live) != 1 else ''} available{RST}")
            return live
    for m in preset.models:
        if m not in models:
            models.append(m)
    return models


def _verify(preset: ProviderPreset, key: str, model: str) -> tuple[bool, str]:
    try:
        from axon.providers.verifier import verify_api_key
        print(f"  {SLATE}Verifying key with {model}…{RST}", end="", flush=True)
        ok, msg = verify_api_key(preset, key, model=model)
    except Exception as exc:  # verifier unavailable or network failure: report, don't save
        ok, msg = False, str(exc)
    print("\r\033[K", end="")
    return ok, msg


def _is_model_problem(msg: str) -> bool:
    m = msg.lower()
    return "http 404" in m or "does not exist" in m or "model_not_found" in m or "not found" in m and "model" in m


def _fallback_provider_picker(agent: Agent) -> bool:
    options = []
    for p in PROVIDER_PRESETS:
        try:
            linked = is_provider_linked(p, agent)
        except Exception:
            linked = False
        tag = " [Active]" if same_endpoint(agent.settings.base_url, p.base_url) else (" [Linked]" if linked else "")
        options.append(f"{'✓' if linked else '○'} {p.name}{tag} ({p.category}) - {p.default_model}")
    chosen = pick(options, title="Connect a Provider")
    if not chosen:
        return False
    return _configure_and_apply_preset(agent, PROVIDER_PRESETS[options.index(chosen)])
