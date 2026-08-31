"""
Axon typed Settings configuration.
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
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from axon.errors import ConfigError

Mode = Literal["default", "acceptEdits", "plan", "bypass"]
Effort = Literal["reflex", "balanced", "synapse", "quantum", "low", "medium", "high", "xhigh", "max", "hyper"]

class PermissionConfig(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

class HookSpec(BaseModel):
    event: str = ""
    command: str = ""
    tool: str | None = None

HookConfig = HookSpec

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AXON_",
        frozen=True,
        extra="ignore",
    )

    api_key: SecretStr = Field(
        default=SecretStr(""),
    )
    base_url: str = "https://agentrouter.org"
    model: str = "deepseek-v4-flash"
    effort: Effort = "quantum"
    thinking: bool = True
    mode: Mode = "bypass"
    workspace: Path = Field(default_factory=Path.cwd)
    max_tokens: int = 128_000
    max_iterations: int = 50
    turn_token_budget: int = 2_000_000
    max_history_turns: int = 0  # Sliding context window in turns (0 = unlimited)
    session_cost_ceiling: Decimal = Decimal("50.00")
    compact_at: float = 0.85
    parallel_tools: int = 6
    bash_timeout_s: int = 180
    tool_output_cap: int = 150_000
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    hooks: dict[str, list[HookSpec]] = Field(default_factory=dict)
    append_system_prompt: str | None = None
    dangerously_skip_permissions: bool = False
    isolate_worktrees: bool = False

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, v: Any) -> str:
        if isinstance(v, str):
            v_clean = v.strip().strip('"').strip("'")
            if v_clean:
                return v_clean
        return "deepseek-v4-flash"

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
    def load(cls, cli_overrides: dict[str, Any] | None = None) -> Settings:
        """
        Load settings in order:
        defaults -> ~/.axon/config.toml -> ./.axon/config.toml -> env vars -> .env -> CLI overrides
        """
        cli_overrides = cli_overrides or {}
        merged: dict[str, Any] = {}

        # 1. User global config ~/.axon/config.toml
        user_cfg = Path.home() / ".axon" / "config.toml"
        if user_cfg.exists():
            try:
                with open(user_cfg, "rb") as f:
                    merged.update(tomllib.load(f))
            except Exception as e:
                raise ConfigError(f"Malformed config at {user_cfg}: {e}") from e

        # 2. Workspace project config ./.axon/config.toml
        ws = Path(cli_overrides.get("workspace") or Path.cwd()).resolve()
        proj_cfg = ws / ".axon" / "config.toml"
        if proj_cfg.exists():
            try:
                with open(proj_cfg, "rb") as f:
                    proj_data = tomllib.load(f)
                    if "permissions" in proj_data and "permissions" in merged:
                        # Merge permissions lists
                        merged["permissions"]["allow"] = list(set(
                            merged["permissions"].get("allow", []) + proj_data["permissions"].get("allow", [])
                        ))
                        merged["permissions"]["deny"] = list(set(
                            merged["permissions"].get("deny", []) + proj_data["permissions"].get("deny", [])
                        ))
                    merged.update({k: v for k, v in proj_data.items() if k != "permissions"})
            except Exception as e:
                # If project config is malformed, continue with defaults rather than halting
                pass

        # 3. Search for .env across workspace, axon package root, and ~/.axon
        search_dirs: list[Path] = [ws] + list(ws.parents)
        axon_pkg_root = Path(__file__).resolve().parents[2]
        if axon_pkg_root not in search_dirs:
            search_dirs.append(axon_pkg_root)
        user_axon_dir = Path.home() / ".axon"
        if user_axon_dir not in search_dirs:
            search_dirs.append(user_axon_dir)
        if Path.home() not in search_dirs:
            search_dirs.append(Path.home())

        for folder in search_dirs:
            dotenv_candidate = folder / ".env"
            if dotenv_candidate.exists() and dotenv_candidate.is_file():
                try:
                    with open(dotenv_candidate, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                k = k.strip()
                                v = v.strip().strip("'\"")
                                if (k.startswith("AXON_") or k in (
                                    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                                    "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY"
                                )) and k not in os.environ:
                                    os.environ[k] = v
                except Exception:
                    pass

        # 4. Filter non-null CLI overrides
        clean_overrides = {k: v for k, v in cli_overrides.items() if v is not None}
        merged.update(clean_overrides)

        settings = cls(**merged)
        val = settings.api_key.get_secret_value()
        invalid_placeholders = {"", "your_api_key_here", "your-api-key-here", "sk-placeholder", "replace_me"}
        
        # Localhost / Ollama / LM Studio providers require zero API keys
        base_lower = settings.base_url.lower()
        if "localhost" in base_lower or "127.0.0.1" in base_lower or "0.0.0.0" in base_lower:
            if not val or val in invalid_placeholders:
                settings = settings.model_copy(update={"api_key": SecretStr("local")})
                return settings

        if not val or val in invalid_placeholders:
            # Check if set in os.environ across all provider keys
            candidate_keys = (
                "AXON_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY"
            )
            env_key = None
            for ck in candidate_keys:
                candidate_val = os.environ.get(ck)
                if candidate_val and candidate_val not in invalid_placeholders:
                    env_key = candidate_val
                    break

            if env_key:
                settings = settings.model_copy(update={"api_key": SecretStr(env_key)})
            elif sys.stdin.isatty():
                from axon.ui.theme import BOLD, GOLD, MINT, RST, SLATE, WHITE
                print(f"\n  {GOLD}▲█▲ Axon Provider Setup{RST}")
                print(f"  {SLATE}No API key found for {settings.base_url}.{RST}")
                try:
                    entered_key = input(f"  {BOLD}{WHITE}Enter your API key (or Enter to exit): {RST}").strip()
                except Exception:
                    entered_key = ""
                if entered_key and entered_key not in invalid_placeholders:
                    from axon.providers.verifier import verify_api_key
                    print(f"  {SLATE}⚡ Verifying API key...{RST}", end="", flush=True)
                    ok, msg = verify_api_key("agentrouter", entered_key, base_url=settings.base_url, model=settings.model)
                    if not ok:
                        from axon.ui.theme import ROSE
                        print(f"\r\033[K  {ROSE}❌ API key test failed:{RST} {WHITE}{msg}{RST}")
                        print(f"  {SLATE}The key is not working and was not saved.{RST}\n")
                        raise ConfigError(f"Provided API key is not working: {msg}")
                    print(f"\r\033[K  {MINT}✓ API key verified successfully!{RST}")

                    global_axon = Path.home() / ".axon"
                    global_axon.mkdir(parents=True, exist_ok=True)
                    env_file = global_axon / ".env"
                    try:
                        with open(env_file, "a", encoding="utf-8") as f_env:
                            f_env.write(f'\nAXON_API_KEY="{entered_key}"\n')
                        print(f"  {MINT}✓ Saved key permanently to {env_file}{RST}\n")
                    except Exception:
                        pass
                    settings = settings.model_copy(update={"api_key": SecretStr(entered_key)})
                else:
                    raise ConfigError(
                        "Missing API key. Set your API key in ~/.axon/.env, .env, or run /provider to choose a local or cloud engine."
                    )
            else:
                raise ConfigError(
                    "Missing or placeholder API key. Please set your API key in your .env file or environment."
                )

        return settings


