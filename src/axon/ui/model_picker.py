"""
Interactive Tabbed Model Picker TUI.
Allows switching models across linked providers with clean provider tabs (Tab/Arrows).
Only linked providers show their models, each displayed separately under its provider tab.
"""
from __future__ import annotations
import os
import random
import sys
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

from axon.providers.catalog import (
    PROVIDER_PRESETS,
    ProviderPreset,
    get_linked_providers,
    get_models_for_provider,
    get_preset_by_id,
    is_provider_linked,
)
from axon.providers.registry import PRICING, provider_for
from axon.ui.picker import pick
from axon.ui.provider_picker import run_provider_picker
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, LBLUE, MINT, RST, ROSE, SLATE, TEAL, UNDER, WHITE,
    strip_ansi, term_width,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent


def run_model_picker(agent: Agent) -> bool:
    """
    Renders interactive tabbed model picker.
    User cycles through linked provider tabs (Tab / Left / Right) and selects models (Up / Down / Enter).
    Returns True if model/provider was changed, False if cancelled.
    """
    if not sys.stdin.isatty() or not _HAS_TERMIOS or termios is None:
        return _fallback_model_picker(agent)

    linked_presets = get_linked_providers(agent)
    if not linked_presets:
        ar = get_preset_by_id("agentrouter")
        if ar:
            linked_presets = [ar]

    # Find initial tab matching current agent base_url
    active_tab_idx = 0
    clean_active_url = agent.settings.base_url.rstrip("/").lower()
    for idx, p in enumerate(linked_presets):
        if p.base_url.rstrip("/").lower() in clean_active_url or clean_active_url in p.base_url.rstrip("/").lower():
            active_tab_idx = idx
            break

    # Total tabs: linked providers + 1 "[+ Connect Provider]" tab
    total_tabs = len(linked_presets) + 1
    selected_model_idx = 0
    rendered_lines = 0

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)

    # Initial screen clear
    sys.stdout.write("\033[3J\033[H\033[2J")
    sys.stdout.flush()

    def get_current_tab_models(tab_idx: int) -> list[str]:
        if tab_idx >= len(linked_presets):
            return ["➕ Connect a New Provider (API Key & Endpoint)..."]
        p = linked_presets[tab_idx]
        base_models = get_models_for_provider(p, agent)
        options = list(base_models)
        options.append("✏️  Enter custom model name...")
        options.append("🎲  Random model (from this provider)")
        return options

    # Position selected_model_idx on current active model if present
    curr_models = get_current_tab_models(active_tab_idx)
    if agent.settings.model in curr_models:
        selected_model_idx = curr_models.index(agent.settings.model)

    def draw():
        nonlocal rendered_lines
        tw = term_width()
        width = max(58, min(82, tw - 4))
        lines_out: list[str] = []

        # Header banner
        lines_out.append("")
        header_title = f"{BOLD}{WHITE}Switch Active Model{RST}"
        esc_hint = f"{SLATE}esc back{RST}"
        pad_top = max(2, width - len(strip_ansi(header_title)) - len(strip_ansi(esc_hint)))
        lines_out.append(f"  {header_title}{' ' * pad_top}{esc_hint}")
        lines_out.append("")

        # Tab bar
        tab_chips: list[str] = []
        for idx, p in enumerate(linked_presets):
            is_active_p = (p.base_url.rstrip("/").lower() in clean_active_url or clean_active_url in p.base_url.rstrip("/").lower())
            tab_label = f"✓ {p.name}"
            if idx == active_tab_idx:
                if is_active_p:
                    tab_chips.append(f"{MINT}{BOLD}{UNDER}[{tab_label} ●]{RST}")
                else:
                    tab_chips.append(f"{CYAN}{BOLD}{UNDER}[{tab_label}]{RST}")
            else:
                if is_active_p:
                    tab_chips.append(f"{GOLD}[{tab_label} ●]{RST}")
                else:
                    tab_chips.append(f"{SLATE}[{tab_label}]{RST}")

        # Add [+ Connect] tab
        connect_tab_idx = len(linked_presets)
        if active_tab_idx == connect_tab_idx:
            tab_chips.append(f"{GOLD}{BOLD}{UNDER}[➕ Connect]{RST}")
        else:
            tab_chips.append(f"{DARK_SLATE}[➕ Connect]{RST}")

        tabs_line = "  ".join(tab_chips)
        lines_out.append(f"  {SLATE}Provider Tabs (Tab/← → switch · ↑ ↓ choose model):{RST}")
        lines_out.append(f"  {tabs_line}")
        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")

        # Current tab details
        if active_tab_idx < len(linked_presets):
            p = linked_presets[active_tab_idx]
            models = get_current_tab_models(active_tab_idx)
            lines_out.append(f"  {GOLD}{BOLD}{p.name}{RST} {SLATE}({p.base_url}){RST}")
            lines_out.append("")

            # Scroll window
            max_visible = 9
            num_m = len(models)
            start_idx = max(0, min(selected_model_idx - max_visible // 2, num_m - max_visible))
            end_idx = min(num_m, start_idx + max_visible)

            for i in range(start_idx, end_idx):
                m = models[i]
                is_sel = (i == selected_model_idx)
                cursor = f"{MINT}▶{RST}" if is_sel else " "
                is_curr = (m == agent.settings.model and (p.base_url.rstrip("/").lower() in clean_active_url or clean_active_url in p.base_url.rstrip("/").lower()))
                curr_dot = f"{MINT}●{RST}" if is_curr else " "

                # Model name formatting
                if m.startswith("✏️") or m.startswith("🎲"):
                    m_label = f"{WHITE}{m}{RST}"
                    price_info = ""
                else:
                    pricing = PRICING.get(m)
                    if pricing:
                        cache_s = f" · ${pricing['cache_read']:.2f} cache" if "cache_read" in pricing else ""
                        price_info = f"{SLATE}${pricing['input']:.2f}/1M in · ${pricing['output']:.2f}/1M out{cache_s}{RST}"
                    else:
                        price_info = f"{SLATE}{p.name} model{RST}"

                    if is_curr:
                        price_info += f" {GOLD}[Active]{RST}"

                    if is_sel:
                        m_label = f"{MINT}{BOLD}{m:<28}{RST}"
                    elif is_curr:
                        m_label = f"{WHITE}{BOLD}{m:<28}{RST}"
                    else:
                        m_label = f"{WHITE}{m:<28}{RST}"

                if is_sel:
                    lines_out.append(f"  {cursor} {curr_dot} {m_label}  {price_info}")
                else:
                    lines_out.append(f"    {curr_dot} {m_label}  {price_info}")

            if num_m > max_visible:
                more_b = num_m - end_idx
                more_a = start_idx
                hints = []
                if more_a > 0:
                    hints.append(f"↑ {more_a} above")
                if more_b > 0:
                    hints.append(f"↓ {more_b} below")
                lines_out.append(f"    {DARK_SLATE}... ({', '.join(hints)}){RST}")

        else:
            # [+ Connect Provider] tab view
            lines_out.append(f"  {GOLD}{BOLD}Connect a New Provider Preset{RST}")
            lines_out.append(f"  {SLATE}Configure API credentials for Gemini, OpenRouter, Anthropic, Ollama, etc.{RST}")
            lines_out.append("")
            lines_out.append(f"  {MINT}▶ ➕  Open Provider Connector (Enter to launch)...{RST}")
            lines_out.append("")

        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        lines_out.append(f"  {SLATE}Tab/Shift-Tab cycle tabs · ↑/↓ navigate · Enter select · Esc cancel{RST}")

        sys.stdout.write("\033[H")
        output_str = "\n".join([f"\033[2K\r{l}" for l in lines_out])
        sys.stdout.write(output_str + "\n\033[J")
        sys.stdout.flush()
        rendered_lines = len(lines_out)

    action_connect = False
    chosen_selection = None

    try:
        tty.setcbreak(fd)
        draw()

        while True:
            raw_bytes = os.read(fd, 1024)
            if not raw_bytes:
                break

            models = get_current_tab_models(active_tab_idx)
            num_models = len(models)

            # Esc (0x1b) or Ctrl+C (0x03)
            if raw_bytes in (b"\x1b", b"\x03"):
                return False

            # Tab (0x09) -> Next provider tab
            if raw_bytes == b"\t":
                active_tab_idx = (active_tab_idx + 1) % total_tabs
                selected_model_idx = 0
                draw()
                continue

            # Shift+Tab variations -> Previous provider tab
            if raw_bytes in (b"\x1b[Z", b"\x1b\t", b"\x1b[1;2Z"):
                active_tab_idx = (active_tab_idx - 1) % total_tabs
                selected_model_idx = 0
                draw()
                continue

            # Right Arrow -> Next tab
            if raw_bytes.startswith((b"\x1b[C", b"\x1bOC")):
                active_tab_idx = (active_tab_idx + 1) % total_tabs
                selected_model_idx = 0
                draw()
                continue

            # Left Arrow -> Previous tab
            if raw_bytes.startswith((b"\x1b[D", b"\x1bOD")):
                active_tab_idx = (active_tab_idx - 1) % total_tabs
                selected_model_idx = 0
                draw()
                continue

            # Down Arrow -> Next model in active tab
            if raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                if num_models > 0:
                    selected_model_idx = (selected_model_idx + 1) % num_models
                    draw()
                continue

            # Up Arrow -> Previous model in active tab
            if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                if num_models > 0:
                    selected_model_idx = (selected_model_idx - 1) % num_models
                    draw()
                continue

            # Enter
            if raw_bytes in (b"\r", b"\n", b"\r\n"):
                if active_tab_idx >= len(linked_presets):
                    # Connect provider tab
                    action_connect = True
                    break

                chosen_preset = linked_presets[active_tab_idx]
                chosen_opt = models[selected_model_idx] if selected_model_idx < len(models) else chosen_preset.default_model
                chosen_selection = (chosen_preset, chosen_opt)
                break

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

    if action_connect:
        return run_provider_picker(agent)
    if chosen_selection:
        c_preset, c_opt = chosen_selection
        return _apply_model_selection(agent, c_preset, c_opt)

    return False


def _apply_model_selection(agent: Agent, preset: ProviderPreset, selected_option: str) -> bool:
    """Apply the chosen provider preset and model to the active agent."""
    chosen_model = selected_option

    # Handle special options
    if selected_option.startswith("✏️"):
        try:
            custom_typed = input(f"\n  {BOLD}{WHITE}Enter model name for {preset.name} (e.g. gpt-4o, claude-opus-5, qwen2.5-coder:1.5b): {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            custom_typed = ""
        if not custom_typed:
            print(f"\n  {SLATE}(Active model unchanged: {agent.settings.model}){RST}\n")
            return False
        chosen_model = custom_typed
    elif selected_option.startswith("🎲"):
        avail = [m for m in preset.models if not m.startswith(("✏️", "🎲"))]
        chosen_model = random.choice(avail) if avail else preset.default_model
        print(f"\n  {GOLD}🎲 Selected random model: {BOLD}{chosen_model}{RST}")

    # Extract API key for preset
    key_str = ""
    if preset.requires_key:
        target_var = preset.env_var or ("AXON_API_KEY" if preset.id == "agentrouter" else f"{preset.id.upper()}_API_KEY")
        # Check environment
        env_val = os.environ.get(target_var, "").strip()
        if env_val and not env_val.startswith("/"):
            key_str = env_val
        elif preset.id == "agentrouter":
            for alt_var in ("AXON_API_KEY", "AGENTROUTER_API_KEY"):
                val = os.environ.get(alt_var, "").strip()
                if val and not val.startswith("/"):
                    key_str = val
                    break

        # Check ~/.axon/.env
        if not key_str:
            env_f = Path.home() / ".axon" / ".env"
            if env_f.exists():
                try:
                    for line in env_f.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if "=" in line and line.strip().startswith(target_var):
                            val = line.split("=", 1)[1].strip().strip('"').strip("'")
                            if val and not val.startswith("/"):
                                key_str = val
                                break
                except Exception:
                    pass

        # If agent is already using this preset's URL, retain current key
        if not key_str and hasattr(agent.settings, "api_key") and agent.settings.api_key:
            curr_url = agent.settings.base_url.rstrip("/").lower()
            if preset.base_url.rstrip("/").lower() in curr_url or curr_url in preset.base_url.rstrip("/").lower():
                key_str = agent.settings.api_key.get_secret_value() if hasattr(agent.settings.api_key, "get_secret_value") else str(agent.settings.api_key)

        target_key = SecretStr(key_str or "local")
    else:
        target_key = SecretStr("local")

    # Update agent settings and instantiate provider
    new_settings = agent.settings.model_copy(
        update={
            "model": chosen_model,
            "base_url": preset.base_url,
            "api_key": target_key,
        }
    )
    agent.settings = new_settings
    agent.provider = provider_for(chosen_model, new_settings)

    # Save to ~/.axon/config.toml
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

        # Record custom model if not standard
        if chosen_model not in preset.models:
            c_models = existing_cfg.get("custom_models", [])
            entry = {"model": chosen_model, "provider": preset.id}
            if not any(isinstance(x, dict) and x.get("model") == chosen_model and x.get("provider") == preset.id for x in c_models):
                c_models.append(entry)
                existing_cfg["custom_models"] = c_models

        with open(cfg_file, "wb") as f_out:
            tomli_w.dump(existing_cfg, f_out)
        if not sys.stdin.isatty():
            print(f"\n  {MINT}✓ Saved default provider to {cfg_file}{RST}")
    except Exception:
        pass

    if sys.stdin.isatty():
        sys.stdout.write("\033[2J\033[H\n")
        sys.stdout.flush()
    else:
        # Submission confirmation displaying ONLY the connector provider in non-TTY mode
        print(f"\n  {MINT}{BOLD}✓ Configured active provider: {preset.name}!{RST}")
        print(f"  {SLATE}Active Model: {BOLD}{WHITE}{chosen_model}{RST} · {SLATE}Endpoint: {preset.base_url}{RST}\n")

    from axon.ui.render import Renderer
    import axon
    Renderer().print_banner(
        version=getattr(axon, "__version__", ""),
        model=chosen_model,
        effort=agent.settings.effort,
        workspace=str(agent.settings.workspace),
        mode=agent.settings.mode,
        base_url=preset.base_url,
    )
    return True


def _fallback_model_picker(agent: Agent) -> bool:
    """Non-TTY fallback: step 1 select linked provider, step 2 select model."""
    linked = get_linked_providers(agent)
    if not linked:
        ar = get_preset_by_id("agentrouter")
        linked = [ar] if ar else []

    curr_url = agent.settings.base_url.rstrip("/").lower()
    p_options: list[str] = []
    for p in linked:
        is_active = (p.base_url.rstrip("/").lower() in curr_url or curr_url in p.base_url.rstrip("/").lower())
        active_tag = " [Active]" if is_active else ""
        p_options.append(f"✓ {p.name} ({p.base_url}){active_tag}")
    p_options.append("➕ Connect a New Provider...")

    chosen_p_str = pick(p_options, title="Switch Active Model · Select Provider")
    if not chosen_p_str:
        return False

    if chosen_p_str == "➕ Connect a New Provider...":
        return run_provider_picker(agent)

    p_idx = p_options.index(chosen_p_str)
    preset = linked[p_idx]

    # Step 2: models for chosen provider only
    m_options = get_models_for_provider(preset, agent) + [
        "✏️  Enter custom model name...",
        "🎲  Random model (from this provider)",
    ]
    chosen_m = pick(m_options, title=f"Select Model for {preset.name}", current=agent.settings.model if agent.settings.model in m_options else None)
    if not chosen_m:
        return False

    return _apply_model_selection(agent, preset, chosen_m)
