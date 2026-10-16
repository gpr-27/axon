"""
Axon typed Settings configuration.

Precedence (lowest -> highest):
  defaults -> ~/.axon/config.toml -> <ws>/.axon/config.toml -> .env files
  (<ws>/.env, ~/.axon/.env; never parent directories) -> real environment
  variables (AXON_*) -> CLI overrides.

Security: a project-level file (``<ws>/.axon/config.toml`` or ``<ws>/.env``) may
not redirect the user's own API key to another host. If a project file sets
``base_url`` while the API key comes from user-level config or the environment,
the project ``base_url`` is ignored and a warning is printed.
"""
from __future__ import annotations
import os
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from axon.errors import ConfigError

Mode = Literal["default", "acceptEdits", "plan", "bypass"]
TraceLevel = Literal["compact", "normal", "detailed", "debug"]
Effort = Literal["reflex", "balanced", "synapse", "quantum", "low", "medium", "high", "xhigh", "max", "hyper"]

PROVIDER_KEY_ENV_VARS: tuple[str, ...] = (
    "AXON_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY",
)
INVALID_KEY_PLACEHOLDERS = {"", "your_api_key_here", "your-api-key-here", "sk-placeholder", "replace_me"}
_LOCAL_HOST_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


class PermissionConfig(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)   # an ask rule beats allow (Claude-Code permissions.ask)


class HookSpec(BaseModel):
    """One hook entry. Accepts both the Claude-Code group form
    ``{"matcher": "Bash", "hooks": [{"type": "command", "command": "..."}]}``
    and the legacy flat form ``{"tool": "Bash", "command": "..."}``."""
    model_config = ConfigDict(extra="allow")
    event: str = ""
    command: str = ""
    tool: str | None = None
    matcher: str | None = None
    type: str = "command"
    timeout: int | None = None
    hooks: list[dict[str, Any]] = Field(default_factory=list)


HookConfig = HookSpec


# Keys a Claude Code .claude/settings*.json may not set for Axon (model choice belongs to Axon's config).
_CLAUDE_MODEL_KEYS = ("model", "small_model", "fallback_model", "effort")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AXON_",
        frozen=True,
        extra="ignore",
    )

    api_key: SecretStr = Field(default=SecretStr(""))
    base_url: str = "https://agentrouter.org"
    provider: str | None = None          # explicit provider preset id (anthropic, openai, openrouter, ollama, ...)
    model: str = "deepseek-v4-flash"
    small_model: str | None = None       # cheap model for compaction / background tasks (defaults to `model`)
    effort: Effort = "quantum"
    thinking: bool = True
    mode: Mode = "bypass"                # owner decision: bypass by default; use --mode to change
    workspace: Path = Field(default_factory=Path.cwd)
    max_tokens: int = 128_000
    max_iterations: int = 50
    max_retries: int = 3                 # provider retries on 429/5xx/connection errors
    turn_token_budget: int = 2_000_000
    max_history_turns: int = 0  # Sliding context window in turns (0 = unlimited)
    session_cost_ceiling: Decimal = Decimal("50.00")
    compact_at: float = 0.75            # auto-compact at this share of the context limit
    parallel_tools: int = 6
    bash_timeout_s: int = 180
    tool_output_cap: int = 150_000
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    hooks: dict[str, list[HookSpec]] = Field(default_factory=dict)
    append_system_prompt: str | None = None
    # When true the session starts in bypass mode regardless of the configured mode.
    dangerously_skip_permissions: bool = False
    isolate_worktrees: bool = False
    verbose: bool = False
    trace_level: TraceLevel = "normal"
    # Live budget guard (USD). The projected cost of the next request is checked before sending.
    budget_turn_usd: float | None = None
    budget_session_usd: float | None = None
    budget_action: Literal["warn", "stop", "downgrade"] = "stop"
    # Instruction files: axon-md-or-agents-md | axon-md-and-agents-md | axon-md | managed-only
    instruction_files: str = "axon-md-or-agents-md"
    auto_memory: bool = True
    memory_excludes: list[str] = Field(default_factory=list)
    # Failover chain: comma list or list of models tried when the primary keeps failing (429/5xx/overload).
    fallback_model: str | list[str] | None = None
    routing: dict[str, Any] | None = None
    # Extra directories the session may read/edit (--add-dir, permissions.additionalDirectories).
    additional_dirs: list[Path] = Field(default_factory=list)
    # Replaces the core system prompt (identity + operating rules); env/memory/tools layers stay.
    system_prompt_override: str | None = None
    output_style: str = "default"
    status_line: dict[str, Any] | None = None
    vim_mode: bool = False
    theme: str = "auto"
    cleanup_period_days: int = 0         # 0 = keep everything forever; N > 0 prunes sessions/traces/todos older than N days
    verify_commands: list[Any] = Field(default_factory=list)
    verify_timeout: int = 900
    repo_map: bool = False
    repo_map_tokens: int = 1500
    suggestions: bool = True
    # Usage tips (spinner line / after turns). AXON_NO_TIPS=1 turns them off regardless.
    tips: bool = True
    tips_llm: bool = True                # model-written contextual tips (False: derived from the command registry)
    tips_refresh_turns: int = 5          # min turns between LLM tip refreshes
    context_limit: int = 128_000         # tokens of the model's window Axon uses (128k, 256k, 1M; 0 = model max)
    max_width: int = 0                   # cap output width in columns (0 = use the whole terminal)
    screen_log: bool = True              # keep a plain-text copy of everything shown (projects/<p>/screen/)
    mouse_click_expand: bool = False     # capture clicks at the prompt to expand blocks (wheel/drag then go to Axon)
    notifications: str = "important"     # desktop notifications: important | all | off
    notify_after_seconds: float = 30.0   # "important": notify a finished turn only if it ran this long
    tips_refresh_minutes: float = 15.0   # min minutes between LLM tip refreshes
    auto_title: bool = True
    # `!cmd` output is added to the conversation; when true the model also replies to it.
    respond_to_bash_commands: bool = True     # `!cmd`: the model responds to the output
    disable_all_hooks: bool = False
    prompt_color: str | None = None     # /color, settings.json promptColor
    goal: str | None = None             # /goal: session goal (prompt section + one bounded Stop check)

    @field_validator("cleanup_period_days", mode="before")
    @classmethod
    def _min_cleanup(cls, v: Any) -> int:
        """0 (or negative / invalid) keeps every session forever; N > 0 opts in to pruning."""
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    @field_validator("additional_dirs", mode="before")
    @classmethod
    def _norm_dirs(cls, v: Any) -> list[Path]:
        if v is None:
            return []
        if isinstance(v, (str, Path)):
            v = [v]
        out: list[Path] = []
        for item in v:
            try:
                p = Path(os.path.expanduser(str(item))).resolve()
            except (OSError, RuntimeError):
                continue
            if p not in out:
                out.append(p)
        return out

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, v: Any) -> str:
        if isinstance(v, str):
            v_clean = v.strip().strip('"').strip("'")
            if v_clean:
                return v_clean
        return str(cls.model_fields["model"].default)

    @field_validator("effort", mode="before")
    @classmethod
    def normalize_effort(cls, v: Any) -> str:
        if isinstance(v, str):
            v_low = v.lower().strip()
            if v_low in ("xhigh", "max", "hyper"):
                return "quantum"
            if v_low == "high":
                return "synapse"
            if v_low == "medium":
                return "balanced"
            if v_low == "low":
                return "reflex"
            if v_low in ("reflex", "balanced", "synapse", "quantum"):
                return v_low
        return "quantum"

    @classmethod
    def load(cls, cli_overrides: dict[str, Any] | None = None, require_api_key: bool = True) -> Settings:
        """Load settings honoring the documented precedence (see module docstring).

        With ``require_api_key=False`` a missing key is tolerated (no prompt, no error)."""
        cli_overrides = {k: v for k, v in (cli_overrides or {}).items() if v is not None}  # None = flag not given
        extra_settings = cli_overrides.pop("settings_file", None)
        merged: dict[str, Any] = {}
        warnings: list[str] = []

        # 1. User global config ~/.axon/config.toml
        user_cfg = _axon_home() / "config.toml"
        user_data = _load_toml(user_cfg, fatal=True)
        merged.update(user_data)
        # 1a. [endpoints.*] (user config only): preset base-URL overrides and custom endpoints.
        try:
            from axon.providers.endpoints import apply_endpoint_overrides
            endpoints_table = user_data.get("endpoints")
            apply_endpoint_overrides(endpoints_table if isinstance(endpoints_table, dict) else {})
        except Exception:
            pass

        # 2. Workspace project config <ws>/.axon/config.toml (non-fatal)
        ws = Path(cli_overrides.get("workspace") or Path.cwd()).resolve()
        proj_cfg = ws / ".axon" / "config.toml"
        proj_data: dict[str, Any] = {}
        if proj_cfg.is_file() and proj_cfg != user_cfg:
            try:
                proj_data = _load_toml(proj_cfg, fatal=True)
            except ConfigError as e:
                warnings.append(f"Ignoring malformed project config: {e}")
                proj_data = {}
        if proj_data:
            proj_perms = proj_data.get("permissions")
            if isinstance(proj_perms, dict):
                base_perms = merged.get("permissions") if isinstance(merged.get("permissions"), dict) else {}
                merged["permissions"] = {
                    key: _dedupe(list(base_perms.get(key, [])) + list(proj_perms.get(key, [])))
                    for key in ("allow", "deny", "ask")
                }
            proj_hooks = proj_data.get("hooks")
            if isinstance(proj_hooks, dict):
                hooks = dict(merged.get("hooks") or {})
                for ev, items in proj_hooks.items():
                    hooks[ev] = list(hooks.get(ev, [])) + list(items or [])
                merged["hooks"] = hooks
            merged.update({k: v for k, v in proj_data.items() if k not in ("permissions", "hooks")})

        # 2b. JSON settings files (Claude-Code style), lowest -> highest:
        #     ~/.axon/settings.json, <ws>/.claude/settings.json, <ws>/.axon/settings.json,
        #     <ws>/.claude/settings.local.json, <ws>/.axon/settings.local.json, --settings <file|json>
        json_layers: list[tuple[str, Any]] = [("user", _axon_home() / "settings.json")]

        def _same_dir(a: Path, b: Path) -> bool:
            try:
                return a.resolve() == b.resolve()
            except OSError:
                return False

        # Opened in the home folder: ~/.claude is Claude Code's own user config and ~/.axon is Axon's,
        # neither is a project's settings folder.
        claude_is_home = _same_dir(ws / ".claude", Path.home() / ".claude")
        axon_is_home = _same_dir(ws / ".axon", _axon_home())
        for name in ("settings.json", "settings.local.json"):
            if not claude_is_home:
                json_layers.append(("project-claude", ws / ".claude" / name))
            if not axon_is_home:
                json_layers.append(("project", ws / ".axon" / name))
        if extra_settings:
            json_layers.append(("cli", extra_settings))
        for level, src in json_layers:
            data = _load_json_settings(src, warnings)
            if not data:
                continue
            mapped = map_json_settings(data)
            if level == "project-claude":
                # Claude Code's model names ("opus", "sonnet") pick Anthropic models; they must never switch
                # the model chosen in Axon's own config (that silently moved a DeepSeek user onto Opus).
                for key in _CLAUDE_MODEL_KEYS:
                    if key in mapped:
                        warnings.append(f"Ignoring '{key}' = {mapped[key]!r} from Claude Code settings {src}; "
                                        "Axon's model comes from ~/.axon/config.toml or /model.")
                        mapped.pop(key)
                level = "project"
            if level == "project":
                for blocked in ("api_key", "base_url"):
                    if blocked in mapped:
                        warnings.append(f"Ignoring '{blocked}' from project settings file {src} (set it in user config).")
                        mapped.pop(blocked)
                proj_data.update({k: v for k, v in mapped.items() if k not in ("permissions",)})
            _merge_layer(merged, mapped)

        # 3. .env files: workspace root and ~/.axon only (never parent directories).
        project_env_keys: set[str] = set()
        user_env = _axon_home() / ".env"
        key_vars = set(PROVIDER_KEY_ENV_VARS) | set(preset_key_env_vars())
        for dotenv, is_project in ((ws / ".env", True), (user_env, False)):
            if dotenv.is_file():
                for k, v in _parse_dotenv(dotenv).items():
                    if (k.startswith("AXON_") or k in key_vars) and k not in os.environ:
                        os.environ[k] = v
                        if is_project:
                            project_env_keys.add(k)

        # 4. Real environment variables beat TOML values (pydantic reads env for missing keys).
        for name in cls.model_fields:
            if f"AXON_{name.upper()}" in os.environ and name not in cli_overrides:
                merged.pop(name, None)

        # 5. Security: a project file must not redirect a user-level API key.
        base_from_project = ("base_url" in proj_data and "base_url" in merged) or (
            "AXON_BASE_URL" in project_env_keys and "base_url" not in merged
        )
        key_from_project = ("api_key" in proj_data and "api_key" in merged) or "AXON_API_KEY" in project_env_keys
        if base_from_project and not key_from_project and cli_overrides.get("base_url") is None:
            proj_url = str(proj_data.get("base_url") or os.environ.get("AXON_BASE_URL", ""))
            if not any(h in proj_url.lower() for h in _LOCAL_HOST_MARKERS):
                warnings.append(
                    f"Ignoring project-level base_url '{proj_url}': it would send your user-level API key to "
                    "another host. Set base_url in ~/.axon/config.toml or pass --base-url to use it."
                )
                if "base_url" in proj_data:
                    merged["base_url"] = user_data.get("base_url", cls.model_fields["base_url"].default)
                if "AXON_BASE_URL" in project_env_keys:
                    os.environ.pop("AXON_BASE_URL", None)
                    merged.setdefault("base_url", user_data.get("base_url", cls.model_fields["base_url"].default))

        # 6. CLI overrides (non-null)
        clean_overrides = {k: v for k, v in cli_overrides.items() if v is not None}
        if "workspace" not in clean_overrides and "workspace" not in merged:
            clean_overrides["workspace"] = ws
        merged.update(clean_overrides)

        if isinstance(merged.get("budget"), dict):
            merged.update(map_json_settings({"budget": merged.pop("budget")}))
        try:
            settings = cls(**merged)
        except Exception as e:  # pydantic ValidationError -> friendly ConfigError
            raise ConfigError(f"Invalid configuration: {e}") from e

        for w in warnings:
            _warn(w)

        if settings.dangerously_skip_permissions and settings.mode != "bypass":
            settings = settings.model_copy(update={"mode": "bypass"})

        return _resolve_api_key(settings, interactive=require_api_key)


_JSON_KEY_MAP = {
    "model": "model", "smallModel": "small_model", "small_model": "small_model",
    "effortLevel": "effort", "effort": "effort", "baseUrl": "base_url", "base_url": "base_url",
    "provider": "provider", "traceLevel": "trace_level", "trace_level": "trace_level",
    "maxTurns": "max_iterations", "maxRetries": "max_retries", "max_retries": "max_retries", "parallelTools": "parallel_tools", "compactAt": "compact_at",
    "autoMemoryEnabled": "auto_memory", "instructionFiles": "instruction_files",
    "axonMdExcludes": "memory_excludes", "claudeMdExcludes": "memory_excludes",
    "appendSystemPrompt": "append_system_prompt", "isolateWorktrees": "isolate_worktrees",
    "verbose": "verbose", "alwaysThinkingEnabled": "thinking",
    "fallbackModel": "fallback_model", "fallback_model": "fallback_model", "routing": "routing",
    "outputStyle": "output_style", "output_style": "output_style",
    "statusLine": "status_line", "status_line": "status_line", "theme": "theme",
    "cleanupPeriodDays": "cleanup_period_days", "cleanup_period_days": "cleanup_period_days",
    "verifyCommands": "verify_commands", "verify_commands": "verify_commands",
    "verifyTimeout": "verify_timeout", "verify_timeout": "verify_timeout",
    "repoMap": "repo_map", "repo_map": "repo_map", "repoMapTokens": "repo_map_tokens", "repo_map_tokens": "repo_map_tokens",
    "suggestions": "suggestions", "promptSuggestionEnabled": "suggestions",
    "contextLimit": "context_limit", "context_limit": "context_limit",
    "notifications": "notifications", "preferredNotifChannel": "notifications",
    "notifyAfterSeconds": "notify_after_seconds", "notify_after_seconds": "notify_after_seconds",
    "tips": "tips", "spinnerTipsEnabled": "tips", "tipsLlm": "tips_llm", "tips_llm": "tips_llm",
    "tipsRefreshTurns": "tips_refresh_turns", "tips_refresh_turns": "tips_refresh_turns",
    "tipsRefreshMinutes": "tips_refresh_minutes", "tips_refresh_minutes": "tips_refresh_minutes",
    "autoTitle": "auto_title", "auto_title": "auto_title",
    "respondToBashCommands": "respond_to_bash_commands", "disableAllHooks": "disable_all_hooks",
    "vimMode": "vim_mode", "vim_mode": "vim_mode", "additionalDirectories": "additional_dirs",
    "systemPrompt": "system_prompt_override",
    "promptColor": "prompt_color", "prompt_color": "prompt_color",
    "mouseClickExpand": "mouse_click_expand", "mouse_click_expand": "mouse_click_expand",
}
_MODE_MAP = {"bypassPermissions": "bypass", "bypass": "bypass", "acceptEdits": "acceptEdits", "plan": "plan",
             "default": "default", "manual": "default"}


def normalize_mode(value: Any) -> str | None:
    """Map Claude-Code permission-mode spellings to Axon's (None when unknown)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v in _MODE_MAP:
        return _MODE_MAP[v]
    low = {k.lower(): m for k, m in _MODE_MAP.items()}
    return low.get(v.lower())


def map_json_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Translate a Claude-Code-style settings.json object into Settings fields.
    (``hooks`` are read by :class:`axon.hooks.HookManager` directly.)"""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _JSON_KEY_MAP:
            out[_JSON_KEY_MAP[k]] = v
    if isinstance(data.get("editorMode"), str):
        out["vim_mode"] = data["editorMode"].strip().lower() == "vim"
    perms = data.get("permissions")
    if isinstance(perms, dict):
        out["permissions"] = {
            "allow": [str(x) for x in perms.get("allow", []) or []],
            "deny": [str(x) for x in perms.get("deny", []) or []],
            "ask": [str(x) for x in perms.get("ask", []) or []],
        }
        mode = normalize_mode(perms.get("defaultMode"))
        if mode:
            out["mode"] = mode
        dirs = perms.get("additionalDirectories")
        if isinstance(dirs, list):
            out["additional_dirs"] = [str(x) for x in dirs if isinstance(x, str) and x.strip()]
    budget = data.get("budget")
    if isinstance(budget, dict):
        if "turn_usd" in budget or "turnUsd" in budget:
            out["budget_turn_usd"] = budget.get("turn_usd", budget.get("turnUsd"))
        if "session_usd" in budget or "sessionUsd" in budget:
            out["budget_session_usd"] = budget.get("session_usd", budget.get("sessionUsd"))
        if "action" in budget:
            out["budget_action"] = budget["action"]
    env = data.get("env")
    if isinstance(env, dict):
        for ek, ev in env.items():
            if isinstance(ek, str) and ek not in os.environ and ek.upper() not in ("AXON_API_KEY", "AXON_BASE_URL"):
                os.environ[ek] = str(ev)
    return out


def _load_json_settings(src: Any, warnings: list[str]) -> dict[str, Any]:
    import json as _json
    text = ""
    if isinstance(src, Path) or (isinstance(src, str) and not src.lstrip().startswith("{")):
        path = Path(os.path.expanduser(str(src)))
        if not path.is_file():
            if not isinstance(src, Path):
                warnings.append(f"--settings file not found: {src}")
            return {}
        try:
            text = decode_config_bytes(path.read_bytes())
        except OSError:
            return {}
    else:
        text = str(src)
    if not text.strip():
        return {}
    try:
        data = _json.loads(text)
    except ValueError as e:
        warnings.append(f"Ignoring malformed settings JSON ({src}): {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _merge_layer(merged: dict[str, Any], layer: dict[str, Any]) -> None:
    """Scalars: higher layer wins. Permission lists and memory_excludes: merged across layers."""
    for k, v in layer.items():
        if k == "permissions" and isinstance(v, dict):
            base = merged.get("permissions") if isinstance(merged.get("permissions"), dict) else {}
            merged["permissions"] = {
                key: _dedupe(list(base.get(key, [])) + list(v.get(key, [])))
                for key in ("allow", "deny", "ask")
            }
        elif k in ("memory_excludes", "additional_dirs") and isinstance(v, list):
            merged[k] = _dedupe(list(merged.get(k, [])) + v)
        else:
            merged[k] = v


def _axon_home() -> Path:
    from axon.storage import axon_home
    return axon_home()


def _dedupe(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def _warn(msg: str) -> None:
    try:
        sys.stderr.write(f"  axon: warning: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def decode_config_bytes(raw: bytes) -> str:
    """Decode a config file written by any common editor/shell (UTF-8, UTF-8 BOM, UTF-16 LE/BE)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    # Heuristic: UTF-16 without BOM has many NUL bytes.
    if raw and raw.count(b"\x00") > len(raw) // 4:
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _load_toml(path: Path, fatal: bool) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = decode_config_bytes(path.read_bytes())
        return tomllib.loads(text)
    except Exception as e:
        if fatal:
            raise ConfigError(f"Malformed config at {path}: {e}") from e
        return {}


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = decode_config_bytes(path.read_bytes())
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        k = k.strip().lstrip("﻿")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def preset_key_env_vars() -> list[str]:
    """Every API-key env var known to the provider catalog (XAI_API_KEY, GROQ_API_KEY, ...)."""
    try:
        from axon.providers.catalog import PROVIDER_PRESETS
    except Exception:
        return []
    out: list[str] = []
    for p in PROVIDER_PRESETS:
        for v in getattr(p, "env_vars", ()) or ():
            if v and v not in out:
                out.append(v)
    return out


def _preset_api_key(settings: Settings) -> str | None:
    """Key for the configured provider preset (settings.provider, else the preset owning base_url):
    process environment first, then ~/.axon/.env."""
    try:
        from axon.providers.catalog import find_api_key, find_preset_by_url, get_preset_by_id
        preset = (get_preset_by_id(str(settings.provider)) if settings.provider else None) or find_preset_by_url(settings.base_url)
        if preset is None or not getattr(preset, "env_vars", None):
            return None
        val = find_api_key(list(preset.env_vars))
    except Exception:
        return None
    return val if val and val not in INVALID_KEY_PLACEHOLDERS else None


def missing_key_message(settings: Any = None) -> str:
    """Friendly missing-key error; points at `axon setup` when onboarding says setup is needed."""
    base = ("Missing API key. Set AXON_API_KEY in your environment or in ~/.axon/.env "
            "(or use a local provider such as Ollama via --base-url http://localhost:11434/v1).")
    try:
        from axon.features.onboarding import needs_setup
        if needs_setup(settings):
            return "No API key is configured. Run `axon setup` to pick a provider and save a key.\n  " + base
    except Exception:
        pass
    return base


def _resolve_api_key(settings: Settings, interactive: bool = True) -> Settings:
    val = settings.api_key.get_secret_value()
    base_lower = settings.base_url.lower()
    if any(h in base_lower for h in _LOCAL_HOST_MARKERS):
        if not val or val in INVALID_KEY_PLACEHOLDERS:
            return settings.model_copy(update={"api_key": SecretStr("local")})
        return settings

    if val and val not in INVALID_KEY_PLACEHOLDERS:
        return settings

    preset_key = _preset_api_key(settings)
    if preset_key:
        return settings.model_copy(update={"api_key": SecretStr(preset_key)})

    for ck in PROVIDER_KEY_ENV_VARS:
        candidate_val = os.environ.get(ck)
        if candidate_val and candidate_val not in INVALID_KEY_PLACEHOLDERS:
            return settings.model_copy(update={"api_key": SecretStr(candidate_val)})

    if not interactive:
        return settings
    if not _stdin_is_tty():
        raise ConfigError(missing_key_message(settings))
    entered = _prompt_for_key(settings)
    return settings.model_copy(update={"api_key": SecretStr(entered)})


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _prompt_for_key(settings: Settings) -> str:
    """Interactive first-run key prompt (works on Windows consoles via getpass/msvcrt)."""
    import getpass
    from axon.ui.theme import BOLD, GOLD, MINT, ROSE, RST, SLATE, WHITE

    print(f"\n  {GOLD}Axon Provider Setup{RST}")
    print(f"  {SLATE}No API key found for {settings.base_url}.{RST}")
    print(f"  {SLATE}Tip: run `axon setup` for a guided provider + key setup, or paste a key below.{RST}")
    try:
        entered_key = getpass.getpass(f"  {BOLD}{WHITE}Enter your API key (input hidden, Enter to exit): {RST}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        entered_key = ""
    except Exception:
        try:
            entered_key = input("  Enter your API key (or Enter to exit): ").strip()
        except Exception:
            entered_key = ""
    entered_key = entered_key.strip().strip('"').strip("'")
    if not entered_key or entered_key in INVALID_KEY_PLACEHOLDERS:
        raise ConfigError(
            "Missing API key. Set AXON_API_KEY in ~/.axon/.env or your environment, or run with a local provider."
        )

    try:
        from axon.providers.verifier import verify_api_key
        print(f"  {SLATE}Verifying API key...{RST}", flush=True)
        ok, msg = verify_api_key(settings.provider or "agentrouter", entered_key, base_url=settings.base_url, model=settings.model)
    except Exception as e:
        ok, msg = False, str(e)
    if not ok:
        print(f"  {ROSE}API key test failed:{RST} {WHITE}{msg}{RST}")
        print(f"  {SLATE}The key is not working and was not saved.{RST}\n")
        raise ConfigError(f"Provided API key is not working: {msg}")
    print(f"  {MINT}API key verified.{RST}")

    global_axon = _axon_home()
    try:
        global_axon.mkdir(parents=True, exist_ok=True)
        env_file = global_axon / ".env"
        with open(env_file, "a", encoding="utf-8") as f_env:
            f_env.write(f'\nAXON_API_KEY="{entered_key}"\n')
        if sys.platform != "win32":
            try:
                os.chmod(env_file, 0o600)
            except OSError:
                pass
        print(f"  {MINT}Saved key to {env_file}{RST}\n")
    except Exception:
        pass
    return entered_key
