"""
Guided first-run setup: ``axon setup`` and ``/setup``.

Steps: welcome -> environment check (doctor) -> provider (env keys and local Ollama / LM Studio
servers are detected and marked) -> API key (hidden input, live verification with latency) ->
model -> default permission mode -> trace level -> write ``~/.axon/config.toml`` (load-modify-dump,
other keys preserved) and the key via ``save_api_key`` -> "try these" card.

Scripted use (CI, Docker, no TTY)::

    axon setup --provider ollama --model qwen2.5-coder:7b --mode default --trace normal

uses keys already present in the environment / ``~/.axon/.env``. Without a TTY and without
``--provider`` the command prints manual instructions and exits 1.

``needs_setup(settings_or_none)`` tells the CLI whether to suggest ``axon setup``.
"""
from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from axon.features import FeatureCommand
from axon.ui.theme import BOLD, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE, glyph, safe_text

PERMISSION_MODES: tuple[tuple[str, str], ...] = (
    ("default", "Ask before edits and shell commands (safest)"),
    ("acceptEdits", "Auto-approve file edits, ask before shell commands"),
    ("plan", "Read-only planning: no edits or commands until you approve a plan"),
    ("bypass", "Never ask (fastest; only for trusted repos / sandboxes)"),
)
TRACE_LEVELS: tuple[tuple[str, str], ...] = (
    ("compact", "One line per tool call + a footer"),
    ("normal", "Step timeline: LLM calls, tools, permissions, cost (recommended)"),
    ("detailed", "Also full tool inputs, result previews, prompt token breakdown"),
    ("debug", "Also raw provider request summaries and usage"),
)
LOCAL_PORTS: dict[str, int] = {"ollama": 11434, "lmstudio": 1234}
_PLACEHOLDER_KEYS = {"", "local", "none", "null", "skip", "sk-placeholder", "your_api_key_here",
                     "your-api-key-here", "replace_me"}


# ─── Output helpers ────────────────────────────────────────────────────────

def _say(text: str = "") -> None:
    try:
        print(safe_text(text), flush=True)
    except Exception:
        pass


def _ok(text: str) -> None:
    _say(f"  {MINT}{glyph('ok')}{RST} {text}")


def _warn(text: str) -> None:
    _say(f"  {GOLD}{glyph('warn')}{RST} {text}")


def _err(text: str) -> None:
    _say(f"  {ROSE}{glyph('fail')}{RST} {text}")


def _heading(step: int, total: int, title: str) -> None:
    _say(f"\n  {TEAL}{BOLD}[{step}/{total}]{RST} {BOLD}{WHITE}{title}{RST}")


# ─── Detection ─────────────────────────────────────────────────────────────

def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_local_providers(timeout: float = 0.3) -> dict[str, list[str]]:
    """{preset_id: installed models} for local servers answering on their default port."""
    found: dict[str, list[str]] = {}
    try:
        from axon.providers.catalog import fetch_local_lmstudio_models, fetch_local_ollama_models
    except (ImportError, AttributeError):
        return found
    for pid, port in LOCAL_PORTS.items():
        if not _port_open(port, timeout=timeout):
            continue
        fetch = fetch_local_ollama_models if pid == "ollama" else fetch_local_lmstudio_models
        try:
            found[pid] = list(fetch(timeout=max(timeout, 0.6)))
        except Exception:
            found[pid] = []
    return found


def _usable(v: str | None) -> bool:
    return bool(v) and str(v).strip().strip("'\"").lower() not in _PLACEHOLDER_KEYS and not str(v).startswith("/")


def detect_env_keys() -> dict[str, tuple[str, str]]:
    """{preset_id: (env var, where)} for presets whose key is already set (where: "env" | ".env")."""
    out: dict[str, tuple[str, str]] = {}
    try:
        from axon.providers.catalog import PROVIDER_PRESETS, read_env_file
        file_vals = read_env_file()
    except (ImportError, AttributeError):
        return out
    except Exception:
        file_vals = {}
    for p in PROVIDER_PRESETS:
        if not p.requires_key:
            continue
        for var in p.env_vars:
            if _usable(os.environ.get(var)):
                out[p.id] = (var, "env")
                break
            if _usable(file_vals.get(var)):
                out[p.id] = (var, ".env")
                break
    return out


def needs_setup(settings: Any = None) -> bool:
    """True when no usable API key exists for the configured provider (local endpoints never need one).

    Accepts a Settings object or None (then the user's config is loaded without prompting)."""
    if settings is None:
        try:
            from axon.config import Settings
            settings = Settings.load(require_api_key=False)
        except Exception:
            return True
    base_url = str(getattr(settings, "base_url", "") or "")
    if any(h in base_url.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")):
        return False
    key = getattr(settings, "api_key", None)
    secret = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
    if _usable(secret):
        return False
    try:
        from axon.providers.catalog import find_api_key, find_preset_by_url, get_preset_by_id
        pid = getattr(settings, "provider", None)
        preset = (get_preset_by_id(str(pid)) if pid else None) or find_preset_by_url(base_url)
        if preset is not None:
            if not preset.requires_key:
                return False
            return find_api_key(list(preset.env_vars)) is None
    except Exception:
        pass
    return True


# ─── Config persistence ────────────────────────────────────────────────────

def user_config_path() -> Path:
    from axon.storage import user_file
    return user_file("config.toml")


def write_user_config(updates: dict[str, Any], path: Path | None = None) -> Path:
    """Merge `updates` into ~/.axon/config.toml atomically. Other keys are preserved.

    Raises ValueError (and leaves the file untouched) when the existing file is malformed."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    import tomli_w

    cfg = Path(path) if path is not None else user_config_path()
    data: dict[str, Any] = {}
    if cfg.is_file():
        raw = cfg.read_bytes()
        try:
            try:
                from axon.config import decode_config_bytes
                text = decode_config_bytes(raw)
            except (ImportError, AttributeError):
                text = raw.decode("utf-8-sig", errors="replace")
            data = tomllib.loads(text)
        except Exception as e:
            raise ValueError(f"{cfg} is not valid TOML ({e}); fix or remove it, then run `axon setup` again") from e
    for k, v in updates.items():
        if v is not None:
            data[k] = v
    cfg.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=str(cfg.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(tomli_w.dumps(data))
        os.replace(tmp, cfg)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cfg


# ─── Interaction layer (Selector on a TTY, numbered prompts otherwise) ────

@dataclass
class Choice:
    label: str
    value: Any
    hint: str = ""
    badge: str = ""
    group: str = ""


class SetupIO:
    """All user interaction of the wizard; replaceable in tests."""

    def __init__(self, input_fn: Callable[[str], str] | None = None,
                 secret_fn: Callable[[str], str] | None = None, use_selector: bool | None = None) -> None:
        self.input_fn = input_fn or input
        self.secret_fn = secret_fn or getpass.getpass
        if use_selector is None:
            try:
                from axon import platform as plat
                use_selector = input_fn is None and plat.is_interactive()
            except Exception:
                use_selector = False
        self.use_selector = bool(use_selector)

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" {DIM}[{default}]{RST}" if default else ""
        try:
            val = self.input_fn(safe_text(f"  {prompt}{suffix}: ")).strip()
        except (EOFError, KeyboardInterrupt):
            raise SetupCancelled() from None
        return val or default

    def secret(self, prompt: str) -> str:
        try:
            return self.secret_fn(safe_text(f"  {prompt}: ")).strip().strip("'\"")
        except (EOFError, KeyboardInterrupt):
            raise SetupCancelled() from None

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        ans = self.ask(f"{question} ({hint})").lower()
        if not ans:
            return default
        return ans in ("y", "yes")

    def choose(self, title: str, choices: Sequence[Choice], default: int = 0) -> Any:
        if not choices:
            raise SetupCancelled()
        default = max(0, min(default, len(choices) - 1))
        if self.use_selector:
            try:
                from axon.ui.picker import SelectItem, Selector
                items = [SelectItem(label=c.label, value=c.value, hint=c.hint, badge=c.badge, group=c.group,
                                    search=str(c.value)) for c in choices]
                picked = Selector(items, title=title, index=default, filterable=len(items) > 9).run()
                if picked is None:
                    raise SetupCancelled()
                return picked.value
            except SetupCancelled:
                raise
            except Exception:
                pass  # fall through to the numbered menu
        _say(f"  {GOLD}{BOLD}{title}{RST}")
        for i, c in enumerate(choices, 1):
            mark = f"{MINT}{glyph('prompt')}{RST}" if i - 1 == default else " "
            badge = f" {c.badge}" if c.badge else ""
            hint = f"  {DIM}{c.hint}{RST}" if c.hint else ""
            _say(f"  {mark} {DIM}{i:>2}.{RST} {c.label}{badge}{hint}")
        while True:
            ans = self.ask(f"Select 1-{len(choices)}", str(default + 1))
            if ans.isdigit() and 1 <= int(ans) <= len(choices):
                return choices[int(ans) - 1].value
            for c in choices:  # also accept the value / label typed out
                if ans.lower() in (str(c.value).lower(), c.label.lower()):
                    return c.value
            _warn(f"Please enter a number between 1 and {len(choices)}.")


class SetupCancelled(Exception):
    pass


# ─── Wizard ────────────────────────────────────────────────────────────────

@dataclass
class SetupResult:
    provider: str
    model: str
    mode: str
    trace_level: str
    base_url: str
    config_path: Path | None = None
    key_saved_to: Path | None = None
    verified: bool = False
    latency_s: float = 0.0
    api_key: str = ""


def _welcome() -> None:
    bar = glyph("h") * 54
    _say(f"\n  {TEAL}{bar}{RST}")
    _say(f"  {BOLD}{WHITE}Welcome to Axon{RST}  {SLATE}terminal coding assistant{RST}")
    _say(f"  {SLATE}This takes about a minute: pick a provider, add a key (or use a local{RST}")
    _say(f"  {SLATE}model), choose defaults. Re-run any time with {WHITE}axon setup{SLATE} or {WHITE}/setup{RST}.")
    _say(f"  {TEAL}{bar}{RST}")


def environment_summary(settings: Any = None, workspace: Path | None = None) -> tuple[int, list[str]]:
    """(number of warnings, the warning/missing lines) from the doctor report."""
    try:
        from axon.tools.doctor import MISS, WARN, run_diagnostics
    except (ImportError, AttributeError):
        return 0, []
    ws = Path(workspace or Path.cwd())
    try:
        report = run_diagnostics(settings, ws)
    except Exception as e:
        return 1, [f"diagnostics failed: {e}"]
    lines = [ln.strip() for ln in report.splitlines() if WARN in ln or (MISS in ln and "not found" in ln)]
    return sum(1 for ln in lines if WARN in ln), lines


def _provider_choices(env_keys: dict[str, tuple[str, str]], local: dict[str, list[str]]) -> tuple[list[Choice], int]:
    from axon.providers.catalog import PROVIDER_PRESETS
    choices: list[Choice] = []
    default = -1
    for p in PROVIDER_PRESETS:
        badge, hint = "", p.description
        if p.id in env_keys:
            var, where = env_keys[p.id]
            badge = f"{MINT}{glyph('ok')} key found{RST}"
            hint = f"{var} in {'environment' if where == 'env' else '~/.axon/.env'} · {p.description}"
            if default < 0:
                default = len(choices)
        elif p.id in local:
            n = len(local[p.id])
            badge = f"{MINT}{glyph('ok')} running{RST}"
            hint = f"{n} model{'s' if n != 1 else ''} installed · {p.description}"
            if default < 0:
                default = len(choices)
        elif not p.requires_key:
            hint = f"not running · {p.description}"
        choices.append(Choice(label=p.name, value=p.id, hint=hint, badge=badge, group=p.category))
    return choices, max(default, 0)


def _verify(verify_fn: Callable[..., tuple[bool, str]], preset: Any, key: str, base_url: str, model: str) -> tuple[bool, str, float]:
    _say(f"  {SLATE}Verifying with a 5-token request to {base_url} ...{RST}")
    t0 = time.perf_counter()
    try:
        ok, msg = verify_fn(preset, key, base_url=base_url, model=model)
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    return ok, msg, time.perf_counter() - t0


def _try_card(result: SetupResult) -> None:
    rows = [
        ("axon", "start an interactive session in the current folder"),
        ('axon "explain this repo"', "one-shot question, then exit"),
        ("/help", "every command, grouped by purpose"),
        ("/init", "create AXON.md project memory for this repo"),
        ("/new python-cli demo", "scaffold a runnable starter project"),
        ("/model  /mode  /trace", "switch model, permission mode, trace level"),
        ("/next", "suggested next steps after any answer"),
        ("/doctor", "re-check your environment"),
    ]
    _say(f"\n  {MINT}{BOLD}{glyph('ok')} Axon is ready{RST}  {SLATE}{result.provider} · {result.model} · mode {result.mode} · trace {result.trace_level}{RST}")
    if result.config_path:
        _say(f"  {SLATE}config  {result.config_path}{RST}")
    if result.key_saved_to:
        _say(f"  {SLATE}api key {result.key_saved_to}{RST}")
    _say(f"\n  {GOLD}{BOLD}Try these{RST}")
    for cmd, desc in rows:
        _say(f"    {WHITE}{cmd:<26}{RST} {SLATE}{desc}{RST}")
    _say("")


def manual_instructions() -> str:
    cfg = user_config_path()
    env = cfg.with_name(".env")
    win = os.name == "nt"
    set_cmd = '$env:ANTHROPIC_API_KEY = "sk-..."' if win else 'export ANTHROPIC_API_KEY="sk-..."'
    try:
        from axon.providers.catalog import get_preset_by_id
        anth = get_preset_by_id("anthropic")
        ollama = get_preset_by_id("ollama")
        anth_model = getattr(anth, "default_model", "") or "<model>"
        local_model = getattr(ollama, "default_model", "") or "<model>"
    except Exception:
        anth_model = local_model = "<model>"
    return "\n".join([
        "axon setup needs an interactive terminal. To configure Axon manually:",
        "",
        f"  1. Put your provider key in your environment ({set_cmd})",
        f"     or in {env} as a line like  ANTHROPIC_API_KEY=\"sk-...\"",
        f"  2. Create {cfg} with for example:",
        '       provider = "anthropic"',
        '       base_url = "https://api.anthropic.com"',
        f'       model = "{anth_model}"',
        '       mode = "default"          # default | acceptEdits | plan | bypass',
        '       trace_level = "normal"    # compact | normal | detailed | debug',
        "     Local models need no key:  base_url = \"http://localhost:11434/v1\"  (Ollama)",
        "  3. Or run non-interactively:",
        f"       axon setup --provider ollama --model {local_model} --mode default",
        "",
        "Run `axon doctor` to check the result.",
    ])


def run_setup(
    argv: Sequence[str] | None = None,
    *,
    io: SetupIO | None = None,
    verify_fn: Callable[..., tuple[bool, str]] | None = None,
    interactive: bool | None = None,
    settings: Any = None,
    detect_local: Callable[[], dict[str, list[str]]] | None = None,
) -> tuple[int, SetupResult | None]:
    """Run the wizard. Returns (exit code, result). Everything interactive goes through `io`."""
    parser = argparse.ArgumentParser(prog="axon setup", description="Guided Axon configuration")
    parser.add_argument("--provider", help="provider preset id (anthropic, openai, ollama, ...)")
    parser.add_argument("--model", help="default model id")
    parser.add_argument("--mode", choices=[m for m, _ in PERMISSION_MODES], help="default permission mode")
    parser.add_argument("--trace", choices=[t for t, _ in TRACE_LEVELS], help="default trace level")
    parser.add_argument("--skip-verify", action="store_true", help="do not test the API key")
    try:
        args = parser.parse_args(list(argv or []))
    except SystemExit as e:
        return int(e.code or 0), None

    if interactive is None:
        try:
            from axon import platform as plat
            interactive = io is not None or plat.is_interactive()
        except Exception:
            interactive = io is not None
    if not interactive and not args.provider:
        sys.stderr.write(manual_instructions() + "\n")
        return 1, None
    io = io or SetupIO()
    verify_fn = verify_fn or _default_verifier()
    try:
        return _wizard(args, io, verify_fn, bool(interactive), settings, detect_local or detect_local_providers)
    except SetupCancelled:
        _say(f"\n  {SLATE}Setup cancelled - nothing was changed.{RST}")
        return 130, None


def _default_verifier() -> Callable[..., tuple[bool, str]]:
    try:
        from axon.providers.verifier import verify_api_key
        return verify_api_key
    except (ImportError, AttributeError):
        return lambda *a, **k: (False, "verifier unavailable")


def _current(settings: Any, name: str, default: str) -> str:
    val = getattr(settings, name, None) if settings is not None else None
    return str(val) if val else default


def _wizard(args: argparse.Namespace, io: SetupIO, verify_fn: Callable[..., tuple[bool, str]],
            interactive: bool, settings: Any, detect_local: Callable[[], dict[str, list[str]]]) -> tuple[int, SetupResult | None]:
    from axon.providers.catalog import effective_base_url, find_api_key, get_preset_by_id, save_api_key

    total = 6
    if interactive:
        _welcome()
        # 1. environment
        _heading(1, total, "Environment check")
        n_warn, lines = environment_summary(settings, Path(getattr(settings, "workspace", None) or Path.cwd()))
        for ln in lines[:8]:
            _say(f"    {SLATE}{ln}{RST}")
        if n_warn:
            _warn(f"{n_warn} warning(s). Axon still works; run {WHITE}axon doctor{RST} for details.")
        else:
            _ok(f"Python {sys.version.split()[0]} on {sys.platform} - environment looks good")

    # 2. provider
    env_keys = detect_env_keys()
    local = detect_local() if (interactive or (args.provider or "") in LOCAL_PORTS) else {}
    if args.provider:
        preset = get_preset_by_id(args.provider)
        if preset is None:
            _err(f"Unknown provider '{args.provider}'.")
            return 2, None
    else:
        _heading(2, total, "Choose a provider")
        if local:
            _ok("Local model server detected: " + ", ".join(sorted(local)))
        if env_keys:
            _ok("API keys already configured for: " + ", ".join(sorted(env_keys)))
        choices, default = _provider_choices(env_keys, local)
        preset = get_preset_by_id(io.choose("Provider", choices, default))
        if preset is None:
            raise SetupCancelled()
    _ok(f"Provider: {preset.name}")

    # 3. API key (+ Azure endpoint)
    base_url = effective_base_url(preset)
    key = ""
    key_saved_to: Path | None = None
    new_key = False
    verified, latency = False, 0.0
    model_for_check = args.model or preset.default_model
    if preset.id == "azure" and base_url == preset.base_url:
        if not interactive:
            _err("Azure needs AZURE_OPENAI_ENDPOINT set (https://<resource>.openai.azure.com).")
            return 2, None
        endpoint = io.ask("Azure endpoint (https://<resource>.openai.azure.com)").rstrip("/")
        if not endpoint.startswith("https://"):
            _err("The endpoint must start with https://")
            return 2, None
        save_api_key("AZURE_OPENAI_ENDPOINT", endpoint)
        base_url = endpoint
    if preset.requires_key:
        if interactive:
            _heading(3, total, "API key")
        existing = find_api_key(list(preset.env_vars))
        if existing and (not interactive or io.confirm(f"Use the existing {env_keys.get(preset.id, (preset.env_var, ''))[0]} key?", True)):
            key = existing
        elif not interactive:
            _err(f"No API key found. Set {preset.env_var} in your environment or ~/.axon/.env.")
            return 2, None
        else:
            if preset.docs_url:
                _say(f"  {SLATE}Get a key: {preset.docs_url}{RST}")
            for _attempt in range(3):
                key = io.secret(f"Paste your {preset.name} API key (input hidden)")
                if not _usable(key):
                    _warn("The key is empty.")
                    continue
                new_key = True
                break
            if not new_key:
                raise SetupCancelled()
        while not args.skip_verify:
            verified, msg, latency = _verify(verify_fn, preset, key, base_url, model_for_check)
            if verified:
                _ok(f"Key works {glyph('dot')} {latency * 1000:.0f} ms round trip")
                break
            _err(f"Verification failed after {latency:.1f}s: {msg}")
            if not interactive:
                return 3, None
            action = io.choose("What now?", [
                Choice("Enter a different key", "retry"),
                Choice("Save it anyway (e.g. offline right now)", "save"),
                Choice("Cancel setup", "cancel"),
            ])
            if action == "cancel":
                raise SetupCancelled()
            if action == "save":
                break
            key = io.secret(f"Paste your {preset.name} API key (input hidden)")
            if not _usable(key):
                raise SetupCancelled()
            new_key = True
        if new_key:
            key_saved_to = save_api_key(preset.env_var or "AXON_API_KEY", key)
            _ok(f"Saved {preset.env_var} to {key_saved_to}")
    else:
        models = local.get(preset.id)
        if models is None and preset.id in LOCAL_PORTS and not _port_open(LOCAL_PORTS[preset.id]):
            hint = "ollama serve" if preset.id == "ollama" else "start the server in LM Studio (Developer tab)"
            _warn(f"{preset.name} is not answering on port {LOCAL_PORTS[preset.id]} - start it with: {hint}")
        else:
            verified = True
            _ok(f"{preset.name} needs no API key")

    # 4. model
    if args.model:
        model = args.model
    elif not interactive:
        model = (local.get(preset.id) or [preset.default_model])[0]
    else:
        _heading(4, total, "Default model")
        installed = local.get(preset.id) or []
        names: list[str] = []
        for m in [*installed, preset.default_model, *preset.models]:
            if m and m not in names:
                names.append(m)
        choices = [Choice(m, m, hint=("installed" if m in installed else ("default" if m == preset.default_model else ""))) for m in names]
        choices.append(Choice("Other (type a model id)", "__custom__"))
        picked = io.choose("Model", choices, 0)
        model = io.ask("Model id") if picked == "__custom__" else str(picked)
        if not model:
            raise SetupCancelled()
    _ok(f"Model: {model}")

    # 5. permission mode / trace level
    if args.mode:
        mode = args.mode
    elif interactive:
        _heading(5, total, "Defaults")
        cur = _current(settings, "mode", "default")
        modes = [m for m, _ in PERMISSION_MODES]
        mode = io.choose("Permission mode", [Choice(m, m, d) for m, d in PERMISSION_MODES],
                         modes.index(cur) if cur in modes else 0)
    else:
        mode = _current(settings, "mode", "default")
    if args.trace:
        trace = args.trace
    elif interactive:
        levels = [t for t, _ in TRACE_LEVELS]
        cur_t = _current(settings, "trace_level", "normal")
        trace = io.choose("Trace level (how much of each turn Axon shows)", [Choice(t, t, d) for t, d in TRACE_LEVELS],
                          levels.index(cur_t) if cur_t in levels else 1)
    else:
        trace = _current(settings, "trace_level", "normal")

    if interactive:
        _ok(f"Permission mode: {mode} {glyph('dot')} trace level: {trace}")

    # 6. write config
    if interactive:
        _heading(6, total, "Save")
    try:
        cfg = write_user_config({"provider": preset.id, "base_url": base_url, "model": model,
                                 "mode": mode, "trace_level": trace})
    except (ValueError, OSError) as e:
        _err(str(e))
        return 4, None
    _ok(f"Wrote {cfg}")
    result = SetupResult(provider=preset.id, model=model, mode=mode, trace_level=trace, base_url=base_url,
                         config_path=cfg, key_saved_to=key_saved_to, verified=verified, latency_s=latency,
                         api_key=key)
    _try_card(result)
    return 0, result


# ─── Entry points ──────────────────────────────────────────────────────────

def setup_main(argv: list[str]) -> int:
    """`axon setup [--provider P --model M --mode X --trace L --skip-verify]`."""
    try:
        from axon import platform as plat
        plat.setup_console()
    except Exception:
        pass
    code, _ = run_setup(argv)
    return code


def _apply_to_agent(agent: Any, result: SetupResult) -> bool:
    """Switch a running agent to the new provider, model, permission mode and trace level."""
    settings = getattr(agent, "settings", None)
    if settings is None or not hasattr(settings, "model_copy"):
        return False
    update: dict[str, Any] = {"provider": result.provider, "base_url": result.base_url, "model": result.model,
                              "mode": result.mode, "trace_level": result.trace_level}
    try:
        from pydantic import SecretStr
        if result.api_key:
            update["api_key"] = SecretStr(result.api_key)
        from axon.providers.registry import provider_for
        provider = provider_for(result.model, settings.model_copy(update=update))
    except Exception as e:
        _warn(f"Saved, but could not switch this session ({e}). Restart axon to use the new settings.")
        return False
    try:
        from axon.commands.common import update_settings
        update_settings(agent, **update)
    except (ImportError, AttributeError):
        agent.settings = settings.model_copy(update=update)
    agent.provider = provider
    renderer = getattr(agent, "renderer", None)
    if renderer is not None and hasattr(renderer, "set_trace_level"):
        try:
            renderer.set_trace_level(result.trace_level)
        except Exception:
            pass
    return True


def _setup_command(agent: Any, arg: str) -> None:
    argv = arg.split() if arg and arg.strip() else []
    code, result = run_setup(argv, settings=getattr(agent, "settings", None))
    if code == 0 and result is not None and _apply_to_agent(agent, result):
        _ok(f"This session now uses {result.model} ({result.provider}).")


COMMANDS = [
    FeatureCommand(name="/setup", handler=_setup_command, category="Getting started", aliases=("/onboard",),
                   description="Guided setup: provider, API key (verified), model, permission mode, trace level",
                   usage="/setup [--provider P] [--model M] [--mode X] [--trace L]"),
]
CLI_SUBCOMMANDS = {"setup": setup_main}

__all__ = [
    "needs_setup", "run_setup", "setup_main", "detect_local_providers", "detect_env_keys", "write_user_config",
    "user_config_path", "environment_summary", "manual_instructions", "SetupIO", "SetupResult", "Choice",
    "SetupCancelled", "PERMISSION_MODES", "TRACE_LEVELS", "COMMANDS", "CLI_SUBCOMMANDS",
]
