"""
Interactive 'Connect a provider' TUI Modal (Matching Claude Code / OpenCode UI).
Allows seamless live switching between Local (Ollama, LM Studio) and Cloud engines.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from pydantic import SecretStr

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ModuleNotFoundError:
    termios = None  # type: ignore
    tty = None      # type: ignore
    _HAS_TERMIOS = False

from axon.providers.catalog import PROVIDER_PRESETS, ProviderPreset, get_preset_by_id
from axon.providers.registry import provider_for
from axon.ui.picker import pick
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, LBLUE, MINT, RST, ROSE, SLATE, TEAL, UNDER, WHITE,
    strip_ansi, term_width,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent

def run_provider_picker(agent: Agent) -> bool:
    """
    Renders interactive full-viewport provider connector matching OpenCode / Claude Code.
    Returns True if provider was switched, False if cancelled.
    """
    if not sys.stdin.isatty() or not _HAS_TERMIOS or termios is None:
        # Fallback to simple picker
        return _fallback_provider_picker(agent)

    presets = PROVIDER_PRESETS
    selected_idx = 0
    query: list[str] = []
    rendered_lines = 0

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)

    # Clean screen
    sys.stdout.write("\033[3J\033[H\033[2J")
    sys.stdout.flush()

    def get_filtered_items() -> list[ProviderPreset]:
        q_str = "".join(query).strip().lower()
        if not q_str:
            return presets
        return [
            p for p in presets
            if q_str in p.name.lower() or q_str in p.description.lower() or q_str in p.category.lower() or any(q_str in m.lower() for m in p.models)
        ]

    def draw():
        nonlocal rendered_lines
        tw = term_width()
        width = max(52, min(76, tw - 4))
        
        filtered = get_filtered_items()
        lines_out: list[str] = []
        
        # Header banner
        lines_out.append("")
        header_title = f"{BOLD}{WHITE}Connect a provider{RST}"
        esc_hint = f"{SLATE}esc{RST}"
        pad_top = max(2, width - len(strip_ansi(header_title)) - len(strip_ansi(esc_hint)))
        lines_out.append(f"  {header_title}{' ' * pad_top}{esc_hint}")
        lines_out.append("")

        # Search bar
        q_display = "".join(query)
        if q_display:
            lines_out.append(f"  {GOLD}Search:{RST} {WHITE}{BOLD}{q_display}{RST}{MINT}█{RST}")
        else:
            lines_out.append(f"  {SLATE}Search (type to filter)...{RST}")
        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")

        if not filtered:
            lines_out.append(f"  {ROSE}No matching providers found for '{q_display}'.{RST}")
            lines_out.append("")
        else:
            current_category: str | None = None
            for idx, p in enumerate(filtered):
                if p.category != current_category:
                    current_category = p.category
                    lines_out.append(f"  {GOLD}{BOLD}{current_category}{RST}")

                is_sel = (idx == selected_idx)
                cursor = f"{MINT}▶{RST}" if is_sel else " "
                is_active = (agent.settings.base_url.rstrip("/") == p.base_url.rstrip("/"))
                star = f"{MINT}●{RST}" if is_active else " "

                name_str = f"{p.name:<28}"
                if is_sel:
                    p_name = f"{MINT}{BOLD}{UNDER}{name_str}{RST}"
                elif is_active:
                    p_name = f"{MINT}{BOLD}{name_str}{RST}"
                else:
                    p_name = f"{WHITE}{name_str}{RST}"

                desc_max = max(8, width - 36)
                desc = p.description[:desc_max]
                pad_line = max(1, width - 32 - len(desc))

                if is_sel:
                    lines_out.append(f"  {cursor} {star} {p_name} {SLATE}{desc}{RST}")
                else:
                    lines_out.append(f"    {star} {p_name} {SLATE}{desc}{RST}")

        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        lines_out.append(f"  {SLATE}↑/↓ navigate · Enter connect · Type to search · Esc back{RST}")

        sys.stdout.write("\033[H")
        output_str = "\n".join([f"\033[2K\r{l}" for l in lines_out])
        sys.stdout.write(output_str + "\n\033[J")
        sys.stdout.flush()
        rendered_lines = len(lines_out)

    try:
        tty.setcbreak(fd)
        draw()

        while True:
            raw_bytes = os.read(fd, 1024)
            if not raw_bytes:
                break

            filtered = get_filtered_items()
            num_items = len(filtered)

            # Esc / Ctrl+C
            if raw_bytes in (b"\x1b", b"\x03"):
                return False

            # Down Arrow
            if raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                if num_items > 0:
                    selected_idx = (selected_idx + 1) % num_items
                    draw()
                continue

            # Up Arrow
            if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                if num_items > 0:
                    selected_idx = (selected_idx - 1) % num_items
                    draw()
                continue

            # Backspace
            if raw_bytes in (b"\x7f", b"\x08"):
                if query:
                    query.pop()
                    selected_idx = 0
                    draw()
                continue

            # Enter
            if raw_bytes in (b"\r", b"\n", b"\r\n"):
                if filtered and selected_idx < len(filtered):
                    chosen_preset = filtered[selected_idx]
                    break
                return False

            # Skip escape sequences
            if raw_bytes.startswith(b"\x1b"):
                continue

            # Printable characters
            try:
                decoded = raw_bytes.decode("utf-8")
                for ch in decoded:
                    if ord(ch) >= 32:
                        query.append(ch)
                        selected_idx = 0
                draw()
            except Exception:
                pass

    finally:
        if termios is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            except Exception:
                pass
        if rendered_lines > 0:
            sys.stdout.write(f"\033[{rendered_lines}A\r")
            for _ in range(rendered_lines):
                sys.stdout.write("\033[2K\n")
            sys.stdout.write(f"\033[{rendered_lines}A\r")
            sys.stdout.flush()

    if chosen_preset:
        return _configure_and_apply_preset(agent, chosen_preset)
    return False

def _configure_and_apply_preset(agent: Agent, preset: ProviderPreset) -> bool:
    """Interactively configure credentials, model, and apply provider preset."""
    print(f"\n  {GOLD}▲█▲ Connecting to {preset.name}{RST}")
    print(f"  {SLATE}Base URL: {preset.base_url}{RST}")

    # 1. API Key handling
    api_key_str = "local"
    if preset.requires_key:
        existing_key = None
        if preset.env_var:
            existing_key = os.environ.get(preset.env_var)
        if not existing_key:
            existing_key = os.environ.get("AXON_API_KEY")

        if existing_key and existing_key not in ("", "local"):
            print(f"  {MINT}✓ Found existing API key in environment ({preset.env_var or 'AXON_API_KEY'}){RST}")
            api_key_str = existing_key
        else:
            try:
                entered = input(f"  {BOLD}{WHITE}Enter your API key for {preset.name}: {RST}").strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {SLATE}Connection cancelled.{RST}\n")
                return False
            if not entered:
                print(f"\n  {ROSE}API key is required for {preset.name}.{RST}\n")
                return False
            api_key_str = entered
            # Persist key to ~/.axon/.env
            try:
                env_file = Path.home() / ".axon" / ".env"
                env_file.parent.mkdir(parents=True, exist_ok=True)
                with open(env_file, "a", encoding="utf-8") as f:
                    env_k = preset.env_var or "AXON_API_KEY"
                    f.write(f'\n{env_k}="{api_key_str}"\n')
                print(f"  {MINT}✓ Saved {env_k} to {env_file}{RST}")
            except Exception:
                pass

    # 2. Select Model
    print(f"\n  {BOLD}{WHITE}Select default model for {preset.name}:{RST}")
    chosen_model = pick(preset.models, title=f"Available Models on {preset.name}", current=preset.default_model)
    if not chosen_model:
        chosen_model = preset.default_model

    # 3. Update Settings and Provider
    new_settings = agent.settings.model_copy(
        update={
            "base_url": preset.base_url,
            "model": chosen_model,
            "api_key": SecretStr(api_key_str),
        }
    )
    agent.settings = new_settings
    agent.provider = provider_for(chosen_model, new_settings)

    # 4. Save to global config.toml
    try:
        cfg_file = Path.home() / ".axon" / "config.toml"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        import tomli_w
        existing_cfg = {}
        if cfg_file.exists():
            try:
                import tomllib
                with open(cfg_file, "rb") as f_cfg:
                    existing_cfg = tomllib.load(f_cfg)
            except Exception:
                pass
        existing_cfg["base_url"] = preset.base_url
        existing_cfg["model"] = chosen_model
        with open(cfg_file, "wb") as f_out:
            tomli_w.dump(existing_cfg, f_out)
        print(f"  {MINT}✓ Saved default provider to {cfg_file}{RST}")
    except Exception:
        pass

    print(f"\n  {MINT}{BOLD}✓ Successfully connected to {preset.name}!{RST}")
    print(f"  {SLATE}Active Model: {BOLD}{WHITE}{chosen_model}{RST} · {SLATE}Endpoint: {preset.base_url}{RST}\n")
    return True

def _fallback_provider_picker(agent: Agent) -> bool:
    """Non-raw terminal fallback picker."""
    options = [f"{p.name} ({p.category}) - {p.default_model}" for p in PROVIDER_PRESETS]
    chosen_str = pick(options, title="Connect a Provider")
    if not chosen_str:
        return False
    idx = options.index(chosen_str)
    return _configure_and_apply_preset(agent, PROVIDER_PRESETS[idx])
