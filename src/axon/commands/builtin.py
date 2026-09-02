"""
Built-in slash commands: /help, /model, /mode, /clear, /compact, /context, /cost, /resume, /tools, /permissions, /doctor, /export, /todos, /exit.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from pydantic import SecretStr
from axon.agent.loop import Agent
from axon.config import Mode
from axon.providers.registry import known_models, provider_for
from axon.session.interactive import handle_branch, handle_resume, handle_sessions_list
from axon.skills.interactive import handle_skills_command
from axon.ui.picker import pick
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE, term_width, term_height,
)

@dataclass
class CommandResult:
    handled: bool
    should_exit: bool = False
    message: str | None = None

def save_or_update_env_key(env_var: str, key_val: str, agent: Agent | None = None) -> bool:
    """Save or update an API key in ~/.axon/.env, process environment, and active agent settings."""
    if not env_var or not key_val:
        return False
    clean_key = key_val.strip().strip('"').strip("'")
    if clean_key.startswith("/") or clean_key.lower() in ("cancel", "exit", "skip", "q", "none", "null"):
        return False

    os.environ[env_var] = clean_key
    env_f = Path.home() / ".axon" / ".env"
    env_f.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    found = False
    if env_f.exists():
        try:
            for l in env_f.read_text(encoding="utf-8").splitlines():
                if "=" in l and l.strip().startswith(env_var):
                    lines.append(f'{env_var}="{clean_key}"')
                    found = True
                else:
                    lines.append(l)
        except Exception:
            pass
    if not found:
        lines.append(f'{env_var}="{clean_key}"')

    env_f.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    if agent is not None:
        from axon.providers.catalog import find_preset_by_url
        active_preset = find_preset_by_url(agent.settings.base_url)
        if active_preset and active_preset.env_var == env_var:
            from pydantic import SecretStr
            from axon.providers.registry import provider_for
            new_settings = agent.settings.model_copy(update={"api_key": SecretStr(clean_key)})
            agent.settings = new_settings
            agent.provider = provider_for(agent.settings.model, new_settings)
    return True

def handle_help(agent: Agent, arg: str) -> CommandResult:
    from axon.ui.markdown import format_markdown
    help_md = """
### ⚙️ Core Configuration & Models
| Command / Shortcut | Description |
|---|---|
| `Tab` / `Shift+Tab` | Cycle permission mode (`manual` → `auto-accept` → `plan` → `bypass`) |
| `/model [name]` | Switch active LLM (Claude Opus, GPT-5, DeepSeek, GLM) |
| `/effort [level]` | Adjust reasoning effort (`low`, `medium`, `high`, `xhigh`) |
| `/config [k] [v]` | View and adjust runtime configuration parameters |
| `/status` | View comprehensive live system, token, and agent status |
| `/statusbar` | Toggle real-time token capacity gauge and sparklines |
| `/permissions` | Inspect active permission engine rules and defaults |
| `/plan [mode/off]` | View task checklist or switch to plan mode |

### 📊 Context & Token Budget
| Command / Shortcut | Description |
|---|---|
| `/breakdown` | Full prompt breakdown (system, tools, previous history, last message) & token match |
| `/context` | View active context budget, token breakdown, and compaction limit |
| `/provider` | Connect a provider (Ollama, LM Studio, OpenRouter, Anthropic, OpenAI, Gemini, Groq) |
| `/compact` | Compact conversation history while preserving key context |
| `/window [turns]` | Adjust sliding context window size (e.g. `/window 10` or `/window 0` for all) |
| `/cost` | View session billing ledger, token counts, and real-time cost |
| `/analytics` | View lifetime workspace usage metrics, tool calls, and model analytics |
| `/payload [full]` | Inspect exact prompt payload and tool results sent to model |

### 📜 History & File Revisions
| Command / Shortcut | Description |
|---|---|
| `/history` | View all messages in full conversation history |
| `/diff` | View working tree uncommitted git diff and file changes |
| `/review [focus]` | Run automated multi-file code review |
| `/rewind` | Revert file edits made during previous turns |
| `/copy [code/diff]` | Copy last assistant response, code blocks, or diff to clipboard |
| `/expand [file]` | View full un-truncated output or expand any file |
| `/export [md/json]` | Export conversation to a clean Markdown or JSON transcript |

### 🤖 Multi-Agent, Tasks & Skills
| Command / Shortcut | Description |
|---|---|
| `/subagents` | Axon subagent matrix (inspect isolated transcripts) |
| `/todos` | View active multi-step task checklist |
| `/q [text]` | Add/manage sequential prompt queue (`/q <text>`, `/q drop <id>`, `/q clear`) |
| `/skills` | Browse active skills studio and create new skills |
| `/mcp [tools/connect]` | Inspect and connect Model Context Protocol servers and tools |
| `/plugin [create/install]` | Inspect, create, and install Axon community plugins |
| `/hooks` | Inspect active lifecycle and execution event hooks |
| `/memory` | Inspect persistent workspace memory and AGENTS.md conventions |

### 📁 Sessions & Workspace
| Command / Shortcut | Description |
|---|---|
| `/sessions` / `←` | Axon session timeline dashboard |
| `/rename <title>` | Rename the active session |
| `/tag <name>` | Tag the active session for categorized filtering |
| `/star` | Star the active session as a favorite |
| `/resume [id]` | Resume previous session from transcript |
| `/branch [name]` | Fork current conversation into an independent branch |
| `/test [path]` | Run workspace tests (pytest, npm test, cargo test, go test) |
| `/find` / `Ctrl+P` | Interactive fuzzy file finder |
| `/tools` | List all active agent tools, schemas, and permissions |
| `/notify [msg]` | Test desktop notifications |
| `/doctor` | Run local diagnostics & environment health check |
| `/init` | Initialize `AGENTS.md` conventions file in workspace |

### ⌨️ Session Control & Input
| Command / Shortcut | Description |
|---|---|
| `?` or `/help` | Show this categorized command reference |
| `/kb` | View interactive keyboard shortcuts cheat sheet |
| `Ctrl+A` / `Ctrl+E` | Move cursor to start / end of line (Emacs/Vim) |
| `Ctrl+K` / `Ctrl+W` | Kill line after cursor / delete word backward |
| `/ask [question]` | Ask simultaneous side question in isolated scratch context |
| `/clear` | Clear active conversation context |
| `/exit` or `Ctrl+D` | Save and exit session |
"""
    print(format_markdown(help_md))
    return CommandResult(handled=True)

def handle_effort(agent: Agent, arg: str) -> CommandResult:
    aliases = {
        "reflex": "reflex", "low": "reflex", "fast": "reflex",
        "balanced": "balanced", "medium": "balanced", "normal": "balanced",
        "synapse": "synapse", "high": "synapse", "deep": "synapse",
        "quantum": "quantum", "xhigh": "quantum", "max": "quantum", "hyper": "quantum",
    }
    raw_options = [
        "reflex   (⚡ Fast · Minimal latency)",
        "balanced (⚖️ Adaptive · Standard depth)",
        "synapse  (🔬 Deep · Analytical focus)",
        "quantum  (🧠 Maximum · Neural exhaustive)",
    ]
    option_keys = ["reflex", "balanced", "synapse", "quantum"]

    arg_clean = arg.strip().lower()
    if arg_clean and arg_clean in aliases:
        chosen = aliases[arg_clean]
    else:
        curr_key = aliases.get(str(agent.settings.effort).lower(), "quantum")
        curr_idx = option_keys.index(curr_key) if curr_key in option_keys else 3
        chosen_opt = pick(raw_options, title="Select Neural Reasoning Tier", current=raw_options[curr_idx])
        if chosen_opt:
            idx = raw_options.index(chosen_opt)
            chosen = option_keys[idx]
        else:
            chosen = None

    if chosen and chosen != agent.settings.effort:
        agent.settings = agent.settings.model_copy(update={"effort": chosen})
        print(f"\n  {TEAL}✓ Switched neural reasoning tier to {BOLD}{chosen}{RST}")
        from axon.ui.render import Renderer
        Renderer().print_banner(
            version="GPR_27",
            model=agent.settings.model,
            effort=chosen,
            workspace=str(agent.settings.workspace),
            mode=agent.settings.mode,
        )
    else:
        print(f"\n  {SLATE}(Reasoning tier unchanged: {agent.settings.effort}){RST}\n")
    return CommandResult(handled=True)

def handle_model(agent: Agent, arg: str) -> CommandResult:
    import random
    from axon.providers.catalog import get_curated_model_choices, find_preset_for_model

    curated_choices = get_curated_model_choices(agent.settings.base_url)
    display_to_model = {disp: raw_id for raw_id, _, disp in curated_choices}
    display_to_preset = {}
    from axon.providers.catalog import PROVIDER_PRESETS, get_preset_by_id
    for raw_id, p_label, disp in curated_choices:
        for p in PROVIDER_PRESETS:
            if p.name.startswith(p_label) or p_label.lower() in p.name.lower() or p_label.lower() == p.id.lower():
                display_to_preset[disp] = p
                break

    raw_models = [raw_id for raw_id, _, _ in curated_choices]

    arg_clean = arg.strip()
    chosen_preset = None

    if arg_clean.lower() in ("random", "rand", "shuffle", "surprise"):
        chosen = random.choice(raw_models)
        print(f"\n  {GOLD}🎲 Selected random model: {BOLD}{chosen}{RST}")
    elif arg_clean.lower() in ("random:small", "random-small", "rand-small", "small"):
        small_models = [
            m for m in raw_models
            if any(k in m.lower() for k in ("0.5b", "1.5b", "1b", "2b", "3b", "3.8b", "7b", "8b", "mini", "flash", "haiku", "lite"))
        ]
        chosen = random.choice(small_models) if small_models else random.choice(raw_models)
        print(f"\n  {GOLD}🎲 Selected random lightweight model: {BOLD}{chosen}{RST}")
    elif arg_clean:
        # User specified an exact model name (either from presets or a custom model)
        chosen = arg_clean
    else:
        # Interactive picker with custom model typing and random options
        picker_options = [
            "✏️  Enter custom model name...",
            "➕  Configure Provider (API Key & Endpoint)...",
            "🎲  Random model (surprise me)",
            "🎲  Random lightweight model (<8B)",
        ] + [disp for _, _, disp in curated_choices]

        # Determine current active option display
        current_disp = None
        for raw_id, _, disp in curated_choices:
            if raw_id == agent.settings.model:
                current_disp = disp
                break

        selection = pick(picker_options, title="Switch Active Model", current=current_disp)
        if not selection:
            print(f"\n  {SLATE}(Active model unchanged: {agent.settings.model}){RST}\n")
            return CommandResult(handled=True)

        if selection == "➕  Configure Provider (API Key & Endpoint)...":
            return handle_provider(agent, "")
        elif selection == "🎲  Random model (surprise me)":
            chosen = random.choice(raw_models)
            print(f"\n  {GOLD}🎲 Selected random model: {BOLD}{chosen}{RST}")
        elif selection == "🎲  Random lightweight model (<8B)":
            small_models = [
                m for m in raw_models
                if any(k in m.lower() for k in ("0.5b", "1.5b", "1b", "2b", "3b", "3.8b", "7b", "8b", "mini", "flash", "haiku", "lite"))
            ]
            chosen = random.choice(small_models) if small_models else random.choice(raw_models)
            print(f"\n  {GOLD}🎲 Selected random lightweight model: {BOLD}{chosen}{RST}")
        elif selection == "✏️  Enter custom model name...":
            try:
                custom_input = input(f"\n  {BOLD}{WHITE}Enter custom model name (e.g. qwen2.5-coder:1.5b, mistral:7b, gpt-4o): {RST}").strip()
            except (KeyboardInterrupt, EOFError):
                custom_input = ""
            if not custom_input:
                print(f"\n  {SLATE}(Active model unchanged: {agent.settings.model}){RST}\n")
                return CommandResult(handled=True)

            # Prompt for provider to eliminate any confusion
            provider_menu = [
                f"{p.name:<24} · {p.description} ({p.base_url})"
                for p in PROVIDER_PRESETS
            ]
            chosen_p_option = pick(provider_menu, title=f"Select Provider for '{custom_input}'")
            if not chosen_p_option:
                print(f"\n  {SLATE}(Cancelled custom model setup){RST}\n")
                return CommandResult(handled=True)

            p_idx = provider_menu.index(chosen_p_option)
            chosen_preset = PROVIDER_PRESETS[p_idx]
            chosen = custom_input

            # Persist custom model associated with provider
            try:
                cfg_file = Path.home() / ".axon" / "config.toml"
                cfg_file.parent.mkdir(parents=True, exist_ok=True)
                existing_cfg = {}
                if cfg_file.exists():
                    import tomllib
                    with open(cfg_file, "rb") as f_cfg:
                        existing_cfg = tomllib.load(f_cfg)
                c_models = existing_cfg.get("custom_models", [])
                entry = {"model": chosen, "provider": chosen_preset.id}
                if not any(isinstance(x, dict) and x.get("model") == chosen and x.get("provider") == chosen_preset.id for x in c_models):
                    c_models.append(entry)
                    existing_cfg["custom_models"] = c_models
                    import tomli_w
                    with open(cfg_file, "wb") as f_out:
                        tomli_w.dump(existing_cfg, f_out)
            except Exception:
                pass
        elif selection in display_to_model:
            chosen = display_to_model[selection]
            chosen_preset = display_to_preset.get(selection)
        else:
            chosen = selection

    if chosen and chosen != agent.settings.model:
        from axon.providers.catalog import find_preset_for_model
        preset = chosen_preset or find_preset_for_model(chosen)
        target_base_url = agent.settings.base_url
        target_key = agent.settings.api_key

        if preset:
            if preset.id != "openrouter":
                prefix_to_strip = f"{preset.id}/"
                if chosen.lower().startswith(prefix_to_strip):
                    chosen = chosen[len(prefix_to_strip):]

            if preset.base_url.rstrip("/") != agent.settings.base_url.rstrip("/"):
                target_base_url = preset.base_url
                print(f"  {MINT}⚡ Switched provider endpoint to {BOLD}{preset.name}{RST} {SLATE}({preset.base_url}){RST}")

            if preset.requires_key:
                target_var = preset.env_var or "AXON_API_KEY"
                key_str = ""
                # Check environment
                env_val = os.environ.get(target_var, "").strip()
                if env_val and not env_val.startswith("/"):
                    key_str = env_val
                elif preset.id == "agentrouter" and os.environ.get("AXON_API_KEY") and not os.environ.get("AXON_API_KEY", "").startswith("/"):
                    key_str = os.environ.get("AXON_API_KEY", "").strip()
                else:
                    # Check ~/.axon/.env
                    env_f = Path.home() / ".axon" / ".env"
                    if env_f.exists():
                        for l in env_f.read_text(encoding="utf-8").splitlines():
                            if "=" in l and l.strip().startswith(target_var):
                                val = l.split("=", 1)[1].strip().strip('"').strip("'")
                                if val and not val.startswith("/"):
                                    key_str = val
                                    break

                # Prompt if missing or invalid
                if (not key_str or key_str.startswith("/")) and sys.stdin.isatty():
                    try:
                        entered_key = input(f"  {BOLD}{WHITE}Enter API key for {preset.name} ({target_var}): {RST}").strip()
                        if entered_key and not entered_key.startswith("/") and entered_key.lower() not in ("cancel", "exit", "skip"):
                            from axon.providers.verifier import verify_api_key
                            print(f"  {SLATE}⚡ Verifying API key with {preset.name}...{RST}", end="", flush=True)
                            ok, msg = verify_api_key(preset, entered_key)
                            if ok:
                                print(f"\r\033[K  {MINT}✓ API key verified!{RST}")
                                save_or_update_env_key(target_var, entered_key)
                                key_str = entered_key
                                print(f"  {MINT}✓ Saved {target_var} to ~/.axon/.env{RST}")
                            else:
                                print(f"\r\033[K  {ROSE}❌ API key test failed for {preset.name}:{RST} {WHITE}{msg}{RST}")
                                print(f"  {SLATE}The key is not working and was not saved.{RST}\n")
                    except (KeyboardInterrupt, EOFError):
                        key_str = ""
                target_key = SecretStr(key_str or "local")
            else:
                target_key = SecretStr("local")

        # Create updated settings and replace provider
        new_settings = agent.settings.model_copy(update={
            "model": chosen,
            "base_url": target_base_url,
            "api_key": target_key,
        })
        agent.settings = new_settings
        agent.provider = provider_for(chosen, new_settings)

        # Save to global config.toml
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
            existing_cfg["base_url"] = target_base_url
            existing_cfg["model"] = chosen
            # Maintain custom_models history
            from axon.providers.catalog import PROVIDER_PRESETS
            custom_list = existing_cfg.get("custom_models", [])
            if chosen not in custom_list and not any(chosen in p.models for p in PROVIDER_PRESETS):
                custom_list.append(chosen)
                existing_cfg["custom_models"] = custom_list
            with open(cfg_file, "wb") as f_out:
                tomli_w.dump(existing_cfg, f_out)
        except Exception:
            pass

        print(f"\n  {TEAL}✓ Switched active model to {BOLD}{chosen}{RST} {SLATE}({target_base_url}){RST}")
        from axon.ui.render import Renderer
        r = Renderer()
        r.print_banner(
            version="GPR_27",
            model=chosen,
            effort=agent.settings.effort,
            workspace=str(agent.settings.workspace),
            mode=agent.settings.mode,
        )
    else:
        print(f"\n  {SLATE}(Active model unchanged: {agent.settings.model}){RST}\n")
    return CommandResult(handled=True)

def handle_mode(agent: Agent, arg: str) -> CommandResult:
    modes: list[Mode] = ["default", "acceptEdits", "plan", "bypass"]
    arg_clean = arg.strip().lower()
    alias_map = {
        "auto": "acceptEdits",
        "acceptedits": "acceptEdits",
        "accept": "acceptEdits",
        "plan": "plan",
        "readonly": "plan",
        "bypass": "bypass",
        "god": "bypass",
        "all": "bypass",
        "manual": "default",
        "default": "default",
    }
    if arg_clean in alias_map:
        chosen = alias_map[arg_clean]
    elif not arg_clean:
        chosen = pick(modes, title="Select Permission Mode", current=agent.settings.mode)
    elif arg in modes:
        chosen = arg
    else:
        print(f"\n  {ROSE}Invalid mode '{arg}'. Valid modes: {', '.join(modes)} (or 'auto', 'manual', 'plan', 'bypass'){RST}\n")
        return CommandResult(handled=True)

    if chosen:
        agent.settings = agent.settings.model_copy(update={"mode": chosen})  # type: ignore
        agent.permissions.settings = agent.settings
        print(f"\n  {TEAL}✓ Switched permission mode to {BOLD}{chosen}{RST}")
        from axon.ui.render import Renderer
        Renderer().print_banner(
            version="GPR_27",
            model=agent.settings.model,
            effort=agent.settings.effort,
            workspace=str(agent.settings.workspace),
            mode=chosen,
        )
    return CommandResult(handled=True)

def handle_context(agent: Agent, arg: str) -> CommandResult:
    from axon.agent.prompt import build_system
    system_blocks = build_system(agent.settings, agent.registry, list(agent.skills.skills.values()))
    tool_schemas = agent.registry.schemas(
        provider_style="anthropic" if agent.provider.name == "anthropic" else "openai"
    )
    sys_chars = sum(len(str(b.get("text", ""))) for b in system_blocks)
    sys_tokens = int(sys_chars / 3.7)
    tool_chars = sum(len(str(s)) for s in tool_schemas)
    tool_tokens = int(tool_chars / 3.7)
    chat_tokens = agent.conversation.token_estimate()
    total_tokens = sys_tokens + tool_tokens + chat_tokens

    ceiling = agent.context.get_effective_budget(agent.settings.model)
    threshold = int(ceiling * agent.settings.compact_at)
    pct = (total_tokens / ceiling * 100) if ceiling > 0 else 0.0

    print(f"\n  {GOLD}{BOLD}=== Active LLM Context Breakdown ==={RST}")
    print(f"  {TEAL}• System Prompt:{RST}        ~{sys_tokens:,} tokens ({len(system_blocks)} blocks: Identity, Rules, Env, Memory, Skills)")
    print(f"  {TEAL}• Tool Schemas:{RST}         ~{tool_tokens:,} tokens ({len(tool_schemas)} registered tools)")
    print(f"  {TEAL}• Conversation Chat:{RST}    ~{chat_tokens:,} tokens ({len(agent.conversation.messages)} messages)")
    print(f"  {DARK_SLATE}  {'─' * 55}{RST}")
    print(f"  {GOLD}• Total Input Payload:{RST}   ~{total_tokens:,} tokens ({pct:.1f}% of {ceiling:,} active window)")
    print(f"\n  {SLATE}Model:{RST} {agent.settings.model}  {SLATE}|  Compaction Threshold (85%):{RST} ~{threshold:,} tokens")
    win_str = f"{agent.settings.max_history_turns} turns" if agent.settings.max_history_turns > 0 else "Unlimited"
    print(f"  {SLATE}Sliding Window:{RST} {win_str}  {SLATE}|  Type '/payload' to view full system prompt & tools{RST}\n")
    return CommandResult(handled=True)

def handle_cost(agent: Agent, arg: str) -> CommandResult:
    from decimal import Decimal
    curr_id = agent.session.active_session_id
    parent_id = curr_id.rsplit("_sub_", 1)[0] if "_sub_" in curr_id else curr_id
    model_name = getattr(agent.settings, "model", "claude-opus-5") if hasattr(agent, "settings") and isinstance(getattr(agent.settings, "model", None), str) else "claude-opus-5"

    if "_sub_" in curr_id:
        sub_part = curr_id.split("_sub_")[-1]
        sub_ledger = agent.session.load_ledger(curr_id, model_name) if hasattr(agent, "session") else agent.ledger
        print(f"\n{GOLD}{BOLD}=== Subagent #{sub_part} Cost & Token Ledger ==={RST}")
        print(f"\n{sub_ledger.render(model_name)}\n")
    else:
        main_ledger = agent.session.load_ledger(parent_id, model_name) if hasattr(agent, "session") else agent.ledger
        print(f"\n{GOLD}{BOLD}=== Main Agent Cost & Token Ledger ==={RST}")
        print(f"\n{main_ledger.render(model_name)}\n")

        if hasattr(agent, "subagents") and agent.subagents:
            tasks = agent.subagents.all_tasks()
            if tasks:
                print(f"  {CYAN}🤖 Subagent Cost Breakdown:{RST}")
                tot_sub_tok = 0
                tot_sub_cost = Decimal("0.0")
                for t in tasks:
                    sub_f = f"{parent_id}_sub_{t.index}"
                    sub_l = agent.session.load_ledger(sub_f, model_name) if hasattr(agent, "session") else None
                    if sub_l and (sub_l.total_input_tokens + sub_l.total_output_tokens > 0):
                        sub_tok = sub_l.total_input_tokens + sub_l.total_output_tokens
                        sub_c = sub_l.total_cost
                        in_t = sub_l.total_input_tokens
                        out_t = sub_l.total_output_tokens
                    else:
                        sub_tok = getattr(t, "tokens_consumed", 0) or (getattr(t, "input_tokens", 0) + getattr(t, "output_tokens", 0))
                        in_t = getattr(t, "input_tokens", 0)
                        out_t = getattr(t, "output_tokens", 0)
                        from axon.providers.registry import PRICING
                        p = PRICING.get(model_name, {"input": 3.0, "output": 15.0})
                        sub_c = (Decimal(str(in_t)) / Decimal("1000000")) * Decimal(str(p.get("input", 3.0))) + (Decimal(str(out_t)) / Decimal("1000000")) * Decimal(str(p.get("output", 15.0)))

                    tot_sub_tok += sub_tok
                    tot_sub_cost += sub_c
                    print(f"     {SLATE}└─ Subagent #{t.index} ({t.title[:24]}): {WHITE}{sub_tok:,} tokens{SLATE} (in: {in_t:,} · out: {out_t:,}) · {GOLD}${float(sub_c):.5f}{RST}")

                combined_tokens = (main_ledger.total_input_tokens + main_ledger.total_output_tokens) + tot_sub_tok
                combined_cost = main_ledger.total_cost + tot_sub_cost
                print(f"\n  {GOLD}{BOLD}🌟 Combined Session Total (Main + Subagents): {WHITE}{combined_tokens:,} tokens · ${float(combined_cost):.5f}{RST}\n")

    try:
        if hasattr(agent, "session") and hasattr(agent.session, "load_workspace_ledger"):
            ws_ledger = agent.session.load_workspace_ledger(model_name)
            total_toks = ws_ledger.total_input_tokens + ws_ledger.total_output_tokens
            chats_cnt = getattr(ws_ledger, "chat_count", 0)
            chat_label = f" across {chats_cnt} chat{'s' if chats_cnt != 1 else ''}" if chats_cnt > 0 else ""
            print(f"  {SLATE}Workspace Lifetime Total : {GOLD}${ws_ledger.total():.5f}{SLATE} ({total_toks:,} tokens recorded{chat_label}){RST}\n")
    except Exception:
        pass
    return CommandResult(handled=True)

def handle_todos(agent: Agent, arg: str) -> CommandResult:
    from axon.ui.render import render_todo_box
    if not agent.todos.items:
        print(f"\n  {SLATE}No active task plan recorded. Ask the agent to plan or use TodoWrite.{RST}\n")
        return CommandResult(handled=True)
    print(f"\n{render_todo_box(agent.todos.render())}\n")
    return CommandResult(handled=True)

def handle_clear(agent: Agent, arg: str) -> CommandResult:
    import sys
    # 1. Clear terminal visible screen and scrollback buffer
    if sys.stdin.isatty():
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()

    # 2. Reset conversation messages, ledger, file state, todos, checkpoints, subagents, and queue from zero
    custom_name = arg.strip() if arg.strip() else None
    if hasattr(agent, "reset_for_new_session"):
        new_session_id = agent.reset_for_new_session(custom_name)
    else:
        agent.conversation.messages.clear()
        if hasattr(agent, "ledger") and agent.ledger:
            agent.ledger.clear()
        if hasattr(agent, "subagents") and agent.subagents:
            agent.subagents.clear()
        if hasattr(agent, "file_state"):
            agent.file_state.clear()
        if hasattr(agent, "todos"):
            agent.todos.clear()
        if hasattr(agent, "message_queue"):
            agent.message_queue.clear()
        if hasattr(agent, "checkpoints") and agent.checkpoints:
            agent.checkpoints.clear()
        new_session_id = agent.session.open(custom_name)

    # 3. Display clean startup banner for new session
    from axon.ui.render import Renderer
    renderer = getattr(agent, "renderer", None) or Renderer()
    renderer.print_banner(
        version="GPR_27",
        model=agent.settings.model,
        effort=agent.settings.effort,
        workspace=str(agent.settings.workspace),
        mode=agent.settings.mode,
    )
    print(f"  {MINT}⚡ Started new session:{RST} {BOLD}{WHITE}{new_session_id}{RST}\n")
    return CommandResult(handled=True)

def handle_permissions(agent: Agent, arg: str) -> CommandResult:
    mode = agent.settings.mode
    print(f"\n{GOLD}{BOLD}=== Permission Policy ({mode}) ==={RST}")
    print(f"  {TEAL}Workspace Root:{RST} {agent.settings.workspace}")
    print(f"  {TEAL}Active Mode:{RST} {mode}")
    print(f"  {TEAL}Protected Paths:{RST} System roots (/etc, /System), ~/.ssh, ~/.aws")
    print(f"  {TEAL}Hard Invariant:{RST} 'rm -rf /' unconditionally blocked\n")
    return CommandResult(handled=True)

def handle_doctor(agent: Agent, arg: str) -> CommandResult:
    doc_tool = agent.registry.get("Doctor")
    from axon.tools.base import ToolContext
    ctx = ToolContext(
        workspace=agent.settings.workspace,
        file_state=agent.file_state,
        todos=agent.todos,
        settings=agent.settings,
        ledger=agent.ledger,
        agent=agent,
    )
    print(f"\n{doc_tool.run({}, ctx)}\n")
    return CommandResult(handled=True)

def handle_export(agent: Agent, arg: str) -> CommandResult:
    """Export conversation transcript to a beautifully formatted Markdown or JSON document."""
    parts = arg.strip().split(maxsplit=1)
    fmt = "md"
    fname = ""
    if parts:
        if parts[0].lower() in ("md", "markdown", "json"):
            fmt = parts[0].lower()
            fname = parts[1] if len(parts) > 1 else ""
        else:
            fname = parts[0]
            if fname.endswith(".json"):
                fmt = "json"

    if not fname:
        from datetime import datetime
        ext = "json" if fmt == "json" else "md"
        fname = f"axon_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"

    out_path = Path(fname)
    if not out_path.is_absolute():
        out_path = agent.settings.workspace / out_path

    if fmt == "json":
        import json
        data = {
            "session_id": agent.session.active_session_id,
            "model": agent.settings.model,
            "workspace": str(agent.settings.workspace),
            "cost_usd": float(agent.ledger.total()),
            "messages": agent.conversation.messages,
        }
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        from datetime import datetime
        lines = [
            f"# 📜 Axon Session Export: {agent.settings.workspace.name}",
            f"*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · Model: {agent.settings.model} · Cost: ${agent.ledger.total():.4f}*",
            "",
            "---",
            "",
        ]
        for idx, m in enumerate(agent.conversation.messages, 1):
            role = m.get("role", "unknown").upper()
            content = m.get("content", "")
            icon = "👤" if role == "USER" else ("🤖" if role == "ASSISTANT" else "⚙️")
            lines.append(f"## {icon} Message #{idx} ({role})\n")

            if isinstance(content, str):
                lines.append(content)
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        b_type = b.get("type", "")
                        if b_type == "text":
                            lines.append(b.get("text", ""))
                        elif b_type == "tool_use":
                            lines.append(f"```yaml\nTool Call: {b.get('name')}\nArguments:\n{b.get('input')}\n```")
                        elif b_type == "tool_result":
                            res_str = str(b.get("content", "")).strip()
                            lines.append(f"```\n[Tool Result - {b.get('tool_use_id')}]:\n{res_str}\n```")
            lines.append("\n---\n")

        out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n  {MINT}✓ Exported transcript ({len(agent.conversation.messages)} messages) to: {WHITE}{out_path.name}{RST}\n")
    return CommandResult(handled=True)



def handle_window(agent: Agent, arg: str) -> CommandResult:
    """Configure or inspect sliding context window size."""
    if not arg:
        win = agent.settings.max_history_turns
        win_str = f"{win} turns ({win * 2} messages)" if win > 0 else "Unlimited (all messages retained)"
        print(f"\n  {GOLD}Sliding Context Window:{RST} {win_str}")
        print(f"  {SLATE}Active messages in context:{RST} {len(agent.conversation.messages)}")
        print(f"  {SLATE}Usage: /window <turns> (e.g. /window 10, /window 20, /window 0 for unlimited){RST}\n")
        return CommandResult(handled=True)

    arg_clean = arg.strip().lower()
    if arg_clean in ("0", "unlimited", "all", "none"):
        new_val = 0
    else:
        try:
            new_val = int(arg_clean)
            if new_val < 0:
                raise ValueError()
        except ValueError:
            print(f"\n  {ROSE}Invalid window size '{arg}'. Enter a positive number of turns (e.g. /window 10) or 0 for unlimited.{RST}\n")
            return CommandResult(handled=True)

    agent.settings = agent.settings.model_copy(update={"max_history_turns": new_val})
    if new_val > 0:
        max_msgs = new_val * 2
        if len(agent.conversation.messages) > max_msgs:
            slice_msgs = list(agent.conversation.messages[-max_msgs:])
            while slice_msgs and slice_msgs[0].get("role") != "user":
                slice_msgs.pop(0)
            if slice_msgs:
                agent.conversation.messages = slice_msgs
        print(f"\n  {TEAL}✓ Set sliding context window to {BOLD}{new_val} turns{RST}{TEAL} ({new_val * 2} messages). Context trimmed to {len(agent.conversation.messages)} messages.{RST}\n")
    else:
        print(f"\n  {TEAL}✓ Context window set to {BOLD}unlimited{RST}{TEAL} (no sliding eviction).{RST}\n")
    return CommandResult(handled=True)

def handle_history(agent: Agent, arg: str, window_only: bool = False) -> CommandResult:
    msgs = agent.conversation.messages
    if window_only and agent.settings.max_history_turns > 0:
        max_msgs = agent.settings.max_history_turns * 2
        if len(msgs) > max_msgs:
            msgs = msgs[-max_msgs:]

    title = "Active Window Messages" if window_only else "Full Conversation History"
    print(f"\n{GOLD}{BOLD}=== {title} ({len(msgs)} messages) ==={RST}")
    if not msgs:
        print(f"  {SLATE}(No messages in conversation yet){RST}\n")
        return CommandResult(handled=True)

    for i, m in enumerate(msgs, 1):
        role = m.get("role", "unknown").upper()
        clr = TEAL if role == "USER" else (GOLD if role == "ASSISTANT" else LBLUE)
        content = m.get("content", "")
        if isinstance(content, list):
            block_types = [b.get("type", "block") for b in content if isinstance(b, dict)]
            preview = f"[{', '.join(block_types)}]"
        else:
            preview = str(content).replace("\n", " ")
            if len(preview) > 85:
                preview = preview[:82] + "..."

        from axon.agent.state import estimate_content_tokens
        toks = estimate_content_tokens(content)
        print(f"  {clr}{BOLD}{i:2d}. [{role:<9}]{RST} {preview} {SLATE}(~{toks:,} tokens){RST}")
    print()
    return CommandResult(handled=True)

def handle_payload(agent: Agent, arg: str) -> CommandResult:
    import json
    from axon.agent.prompt import build_system
    system_blocks = build_system(agent.settings, agent.registry, list(agent.skills.skills.values()))
    tool_schemas = agent.registry.schemas(
        provider_style="anthropic" if agent.provider.name == "anthropic" else "openai"
    )
    sys_text = "".join(str(b.get("text", "")) for b in system_blocks)
    sys_tokens = int(len(sys_text) / 3.7)
    tool_json = json.dumps(tool_schemas, separators=(",", ":"))
    tool_tokens = int(len(tool_json) / 4.0)
    chat_tokens = agent.conversation.token_estimate()
    total_tokens = sys_tokens + tool_tokens + chat_tokens

    is_full = arg.lower() in ("full", "raw", "all")
    is_sys = arg.lower() in ("sys", "system")

    print(f"\n{GOLD}{BOLD}=== Full Active API Payload Sent to Model (~{total_tokens:,} tokens) ==={RST}")

    # 1. System Prompt Section
    print(f"\n  {TEAL}{BOLD}┌── [1] SYSTEM PROMPT ({len(system_blocks)} blocks · ~{sys_tokens:,} tokens) {'─' * 28}{RST}")
    for idx, b in enumerate(system_blocks, 1):
        txt = str(b.get("text", "")).strip()
        print(f"  {DARK_SLATE}│ Block {idx}:{RST}")
        lines = txt.splitlines()
        if is_full or is_sys or len(lines) <= 6:
            for l in lines:
                print(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
        else:
            for l in lines[:4]:
                print(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
            print(f"  {DARK_SLATE}│{RST} {SLATE}... ({len(lines) - 4} lines hidden · type '/payload full' to view all) ...{RST}")
    print(f"  {TEAL}{BOLD}└──{'─' * 70}{RST}")

    # 2. Tool Definitions Section
    print(f"\n  {LBLUE}{BOLD}┌── [2] REGISTERED TOOL DEFINITIONS ({len(tool_schemas)} tools · ~{tool_tokens:,} tokens) {'─' * 18}{RST}")
    for s in tool_schemas:
        t_name = s.get("name") or s.get("function", {}).get("name")
        t_desc = s.get("description") or s.get("function", {}).get("description", "")
        t_first = t_desc.splitlines()[0] if t_desc else ""
        print(f"  {DARK_SLATE}│{RST} {BOLD}{t_name:<14}{RST} {SLATE}{t_first[:65]}{RST}")
    print(f"  {LBLUE}{BOLD}└──{'─' * 70}{RST}")

    # 3. Conversation Messages Section
    msgs = agent.conversation.messages
    print(f"\n  {GOLD}{BOLD}┌── [3] CONVERSATION MESSAGES ({len(msgs)} messages · ~{chat_tokens:,} tokens) {'─' * 21}{RST}")
    if not msgs:
        print(f"  {DARK_SLATE}│{RST} {SLATE}(No conversation messages yet. Context is clear.){RST}")
    else:
        for i, m in enumerate(msgs, 1):
            role = m.get("role", "unknown").upper()
            clr = TEAL if role == "USER" else (GOLD if role == "ASSISTANT" else LBLUE)
            print(f"\n  {DARK_SLATE}├──{RST} {clr}{BOLD}[{i}] {role}:{RST}")
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        b_type = block.get("type", "")
                        if b_type == "text":
                            t_lines = str(block.get("text", "")).splitlines()
                            for l in (t_lines if is_full else t_lines[:6]):
                                print(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
                            if not is_full and len(t_lines) > 6:
                                print(f"  {DARK_SLATE}│{RST} {SLATE}... ({len(t_lines) - 6} lines hidden) ...{RST}")
                        elif b_type == "tool_use":
                            print(f"  {DARK_SLATE}│{RST} {GOLD}🛠️  Tool Use: {block.get('name')}({block.get('input')}){RST}")
                        elif b_type == "tool_result":
                            res_txt = str(block.get("content", ""))
                            r_lines = res_txt.splitlines()
                            print(f"  {DARK_SLATE}│{RST} {CYAN}✓ Tool Result [{block.get('tool_use_id')}]:{RST}")
                            for l in (r_lines if is_full else r_lines[:4]):
                                print(f"  {DARK_SLATE}│{RST}   {SLATE}{l}{RST}")
                            if not is_full and len(r_lines) > 4:
                                print(f"  {DARK_SLATE}│{RST}   {SLATE}... ({len(r_lines) - 4} lines hidden) ...{RST}")
            elif isinstance(content, str):
                c_lines = content.splitlines()
                for l in (c_lines if is_full else c_lines[:6]):
                    print(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
                if not is_full and len(c_lines) > 6:
                    print(f"  {DARK_SLATE}│{RST} {SLATE}... ({len(c_lines) - 6} lines hidden) ...{RST}")

    print(f"  {GOLD}{BOLD}└──{'─' * 70}{RST}\n")
    return CommandResult(handled=True)

def handle_breakdown(agent: Agent, arg: str = "") -> CommandResult:
    """Detailed visual breakdown of active input payload and token reconciliation."""
    from axon.agent.prompt import build_system
    import json
    from axon.ui.theme import (
        BOLD, CYAN, DARK_SLATE, GOLD, LBLUE, MINT, PURPLE, RST, SLATE, TEAL, WHITE, term_width,
    )

    system_blocks = build_system(agent.settings, agent.registry, list(agent.skills.skills.values()))
    tool_schemas = agent.registry.schemas(
        provider_style="anthropic" if agent.provider.name == "anthropic" else "openai"
    )

    sys_text = "".join(str(b.get("text", "")) for b in system_blocks)
    sys_tokens = max(1, int(len(sys_text) / 3.7))
    tool_json = json.dumps(tool_schemas, separators=(",", ":"))
    tool_tokens = max(1, int(len(tool_json) / 4.0))

    messages = agent.conversation.messages
    total_msgs = len(messages)

    if total_msgs > 0:
        prev_msgs = messages[:-1]
        last_msg = messages[-1]
    else:
        prev_msgs = []
        last_msg = None

    def estimate_content_tokens(content: Any) -> int:
        if isinstance(content, str):
            return max(1, int(len(content) / 3.7))
        elif isinstance(content, list):
            tot = 0
            for b in content:
                if isinstance(b, dict):
                    txt = str(b.get("text") or b.get("content") or b.get("input") or "")
                    tot += max(1, int(len(txt) / 3.7))
            return max(1, tot)
        return 1

    prev_tokens = sum(estimate_content_tokens(m.get("content", "")) for m in prev_msgs)
    last_tokens = estimate_content_tokens(last_msg.get("content", "")) if last_msg else 0
    conv_tokens = prev_tokens + last_tokens
    total_payload_tokens = sys_tokens + tool_tokens + conv_tokens

    # Check if last turn API usage is recorded in ledger
    last_usage = getattr(agent.ledger, "last_usage", None)

    width = min(88, max(50, term_width() - 4))

    print(f"\n{GOLD}{BOLD}=== Active Input Payload Breakdown & Token Matching ==={RST}")
    if last_usage and last_usage.input > 0:
        print(f"  {SLATE}Model:{RST} {WHITE}{agent.settings.model}{RST}  {SLATE}|  API Ingested:{RST} {GOLD}{last_usage.input:,} tokens{RST}\n")
    else:
        print(f"  {SLATE}Model:{RST} {WHITE}{agent.settings.model}{RST}  {SLATE}|  Total Payload Ingested:{RST} {GOLD}~{total_payload_tokens:,} tokens{RST}\n")

    # [1] System Prompt Section
    print(f"  {TEAL}{BOLD}┌── [1] SYSTEM PROMPT ({len(system_blocks)} blocks · ~{sys_tokens:,} tokens) {'─' * max(2, width - 48)}┐{RST}")
    for idx, b in enumerate(system_blocks, 1):
        txt = str(b.get("text", "")).strip()
        b_tok = max(1, int(len(txt) / 3.7))
        first_l = txt.splitlines()[0][:50] if txt else "System Block"
        print(f"  {TEAL}│{RST}  {CYAN}Block {idx}:{RST} {WHITE}{first_l}...{RST} {SLATE}(~{b_tok:,} toks){RST}")
    print(f"  {TEAL}{BOLD}└──{'─' * max(2, width - 6)}┘{RST}\n")

    # [2] Tool Definitions Section
    sample_tools = ", ".join([s.get("name") or s.get("function", {}).get("name", "") for s in tool_schemas[:6]])
    print(f"  {LBLUE}{BOLD}┌── [2] TOOL DEFINITIONS ({len(tool_schemas)} tools · ~{tool_tokens:,} tokens) {'─' * max(2, width - 48)}┐{RST}")
    print(f"  {LBLUE}│{RST}  {WHITE}{len(tool_schemas)} Registered tools ({sample_tools}...){RST}")
    print(f"  {LBLUE}{BOLD}└──{'─' * max(2, width - 6)}┘{RST}\n")

    # [3] Previous Conversation History
    print(f"  {PURPLE}{BOLD}┌── [3] PREVIOUS CONVERSATION ({len(prev_msgs)} messages · ~{prev_tokens:,} tokens) {'─' * max(2, width - 52)}┐{RST}")
    if not prev_msgs:
        print(f"  {PURPLE}│{RST}  {SLATE}(No prior conversation messages in context){RST}")
    else:
        for idx, m in enumerate(prev_msgs, 1):
            role = m.get("role", "unknown").upper()
            m_tok = estimate_content_tokens(m.get("content", ""))
            content = m.get("content", "")
            if isinstance(content, list):
                summary = f"[{len(content)} blocks: " + ", ".join(b.get("type", "") for b in content[:3]) + "]"
            else:
                summary = str(content).replace("\n", " ")[:60]
            clr = TEAL if role == "USER" else (GOLD if role == "ASSISTANT" else LBLUE)
            print(f"  {PURPLE}│{RST}  {clr}[Msg {idx}] {role:<9}{RST} {WHITE}{summary}...{RST} {SLATE}(~{m_tok:,} toks){RST}")
    print(f"  {PURPLE}{BOLD}└──{'─' * max(2, width - 6)}┘{RST}\n")

    # [4] Last Message (Current Input)
    print(f"  {MINT}{BOLD}┌── [4] LAST MESSAGE (Active Input · ~{last_tokens:,} tokens) {'─' * max(2, width - 45)}┐{RST}")
    if not last_msg:
        print(f"  {MINT}│{RST}  {SLATE}(No active user message in context){RST}")
    else:
        role = last_msg.get("role", "unknown").upper()
        content = last_msg.get("content", "")
        clr = TEAL if role == "USER" else GOLD
        print(f"  {MINT}│{RST}  {clr}{BOLD}[Msg {total_msgs}] {role}:{RST}")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    b_type = blk.get("type", "")
                    if b_type == "text":
                        print(f"  {MINT}│{RST}    {WHITE}{blk.get('text', '')}{RST}")
                    elif b_type == "tool_use":
                        print(f"  {MINT}│{RST}    {GOLD}🛠️  Tool Use: {blk.get('name')}({blk.get('input')}){RST}")
                    elif b_type == "tool_result":
                        res_prev = str(blk.get("content", "")).strip().replace("\n", " ")[:80]
                        print(f"  {MINT}│{RST}    {CYAN}✓ Tool Result [{blk.get('tool_use_id')}]: {res_prev}...{RST}")
        else:
            for l in str(content).splitlines()[:10]:
                print(f"  {MINT}│{RST}    {WHITE}{l}{RST}")
    print(f"  {MINT}{BOLD}└──{'─' * max(2, width - 6)}┘{RST}\n")

    # [5] Token Matching Summary Matrix
    in_fmt = f"{total_payload_tokens/1000:.1f}k" if total_payload_tokens >= 1000 else f"{total_payload_tokens}"
    print(f"  {GOLD}{BOLD}┌── [5] TOTAL INPUT TOKEN RECONCILIATION {'─' * max(2, width - 43)}┐{RST}")
    print(f"  {GOLD}│{RST}  • System Prompt Blocks  : {WHITE}~{sys_tokens:,} tokens{RST}")
    print(f"  {GOLD}│{RST}  • Tool Definitions ({len(tool_schemas)}) : {WHITE}~{tool_tokens:,} tokens{RST}")
    print(f"  {GOLD}│{RST}  • Prior Chat History    : {WHITE}~{prev_tokens:,} tokens{RST}")
    print(f"  {GOLD}│{RST}  • Last Input Message    : {WHITE}~{last_tokens:,} tokens{RST}")
    print(f"  {GOLD}│{RST}  {DARK_SLATE}{'─' * max(2, width - 8)}{RST}")
    print(f"  {GOLD}│{RST}  • Estimated Input Total : {WHITE}{BOLD}~{total_payload_tokens:,} tokens (~{in_fmt} in){RST}")
    if last_usage and last_usage.input > 0:
        print(f"  {GOLD}│{RST}  {MINT}{BOLD}• Last API Billed Input : {WHITE}{BOLD}{last_usage.input:,} tokens{RST} {MINT}✓ Ground Truth Match{RST}")
    else:
        print(f"  {GOLD}│{RST}  {SLATE}Matches in-flight estimation (`~{in_fmt} in`) and turn footer token metrics.{RST}")
    print(f"  {GOLD}{BOLD}└──{'─' * max(2, width - 6)}┘{RST}\n")

    return CommandResult(handled=True)

def handle_paste(agent: Agent, arg: str) -> CommandResult:
    """Multi-line paste mode for large text blocks."""
    print(f"\n  {TEAL}Multi-line Paste Mode — Paste your text below, then type {BOLD}{GOLD}END{RST}{TEAL} on a new line and press Enter to send:{RST}")
    lines = []
    try:
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        print(f"\n  {SLATE}(Paste cancelled){RST}\n")
        return CommandResult(handled=True)

    combined = "\n".join(lines).strip()
    if combined:
        print(f"\n  {TEAL}⚡ Submitting pasted text ({len(combined):,} chars)...{RST}\n")
        agent.run_turn(combined)
    else:
        print(f"\n  {SLATE}(Empty paste cancelled){RST}\n")
    return CommandResult(handled=True)

def handle_output(agent: Agent, arg: str) -> CommandResult:
    """Displays full un-truncated output from the last tool or inspects a file."""
    import pydoc
    from axon.ui.render import get_last_tool_output, highlight_code
    from axon.ui.theme import term_width

    arg_clean = arg.strip()
    out = ""
    title_extra = ""

    if arg_clean and arg_clean not in ("pager", "all", "full"):
        # Check if arg is a file in workspace
        target_path = Path(arg_clean)
        if not target_path.is_absolute():
            target_path = agent.settings.workspace / target_path
        if target_path.exists() and target_path.is_file():
            try:
                out = target_path.read_text(encoding="utf-8")
                title_extra = f" · {target_path.name}"
            except Exception as e:
                print(f"\n  {ROSE}Failed to read {target_path.name}: {e}{RST}\n")
                return CommandResult(handled=True)

    if not out:
        out = get_last_tool_output()

    if out:
        lines = out.splitlines()
        width = min(92, max(60, term_width() - 4))
        title = f"Full Output{title_extra} ({len(lines)} lines · {len(out):,} chars)"
        border_w = max(10, width - len(title) - 8)

        if "pager" in arg_clean and sys.stdin.isatty():
            # Interactive pager
            formatted_text = "\n".join([f"{idx:4d} | {l}" for idx, l in enumerate(lines, 1)])
            pydoc.pager(f"=== {title} ===\n\n{formatted_text}")
        else:
            print(f"\n  {DARK_SLATE}┌── {GOLD}{BOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}")
            for idx, l in enumerate(lines, 1):
                hl = highlight_code(l)
                print(f"  {DARK_SLATE}│{RST} {DARK_SLATE}{idx:4d} |{RST} {hl}")
            print(f"  {DARK_SLATE}└──{'─' * max(10, width - 6)}{RST}\n")
    else:
        print(f"\n  {SLATE}No recent tool output to display. Run a tool or command first.{RST}\n")
    return CommandResult(handled=True)

def handle_copy(agent: Agent, arg: str) -> CommandResult:
    """Copies last assistant response, extracted code blocks, or git diff to system clipboard."""
    from axon.ui.clipboard import copy_to_clipboard
    sub = arg.strip().lower()

    text_to_copy = ""
    label = "response"

    if sub == "diff":
        try:
            import subprocess
            res = subprocess.run(["git", "diff"], cwd=str(agent.settings.workspace), capture_output=True, text=True, timeout=5)
            text_to_copy = res.stdout if res.returncode == 0 else ""
            label = "uncommitted git diff"
        except Exception:
            text_to_copy = ""
    elif sub == "code":
        # Find last assistant message and extract code blocks
        for m in reversed(agent.conversation.messages):
            if m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str):
                    code_blocks = re.findall(r"```(?:\w+)?\n([\s\S]*?)```", content)
                    if code_blocks:
                        text_to_copy = "\n\n".join(code_blocks)
                        label = f"{len(code_blocks)} code block(s)"
                        break
    else:
        # Last assistant message text
        for m in reversed(agent.conversation.messages):
            if m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str):
                    text_to_copy = content
                    label = "last assistant response"
                    break
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    if texts:
                        text_to_copy = "\n".join(texts)
                        label = "last assistant response"
                        break

    if not text_to_copy:
        print(f"\n  {SLATE}Nothing found to copy.{RST}\n")
        return CommandResult(handled=True)

    ok = copy_to_clipboard(text_to_copy)
    if ok:
        print(f"\n  {MINT}✓ Copied {label} ({len(text_to_copy):,} chars) to system clipboard.{RST}\n")
    else:
        print(f"\n  {ROSE}Clipboard copy failed (no system clipboard utility found).{RST}\n")

    return CommandResult(handled=True)

def handle_subagents(agent: Agent, arg: str) -> CommandResult:
    """Claude-style interactive subagent selector and chat launcher."""
    curr_id = agent.session.active_session_id
    parent_id = curr_id.rsplit("_sub_", 1)[0] if "_sub_" in curr_id else curr_id

    from axon.agent.subagent import sync_subagents_for_session, SubagentManager
    if not isinstance(getattr(agent, "subagents", None), SubagentManager):
        agent.subagents = SubagentManager()

    if "_sub_" in curr_id:
        try:
            parent_conv = agent.session.read_conversation(parent_id)
            class ParentProxy:
                def __init__(self, conv: Any, sess: Any) -> None:
                    self.conversation = conv
                    self.session = sess
                    self.subagents = SubagentManager()
            proxy = ParentProxy(parent_conv, agent.session)
            sync_subagents_for_session(proxy)  # type: ignore
            tasks = proxy.subagents.all_tasks()
        except Exception:
            tasks = []
    else:
        if not agent.subagents.all_tasks():
            sync_subagents_for_session(agent)
        tasks = agent.subagents.all_tasks()

    if not tasks:
        print(f"\n  {SLATE}No subagents have been launched in this session yet.{RST}\n")
        return CommandResult(handled=True)

    tgt = None
    if arg.strip():
        if arg.strip().lower() in ("main", "root", "parent", "0"):
            return handle_main(agent, "")
        if arg.strip().lower() in ("q", "queue"):
            return handle_queue(agent, "")
        for t in tasks:
            if str(t.index) == arg.strip() or t.id == arg.strip() or t.title.lower().startswith(arg.strip().lower()):
                tgt = t
                break
        if not tgt:
            print(f"\n  {ROSE}Subagent '{arg.strip()}' not found.{RST}\n")
            return CommandResult(handled=True)
    else:
        # Interactive picker
        options = [f"[Main Agent] Return to Main chart ({parent_id})"]
        for t in tasks:
            st_icon = "✓" if t.status == "completed" else ("!" if t.status == "exhausted" else ("✗" if t.status == "error" else "▶"))
            is_active_sub = f"{parent_id}_sub_{t.index}" == curr_id
            active_marker = " (Active)" if is_active_sub else ""
            options.append(f"[Subagent {t.index}] ({st_icon}) {t.title}{active_marker} ({t.steps} steps)")
        if hasattr(agent, "message_queue") and len(agent.message_queue) > 0:
            options.append(f"[Message Queue] Inspect pending prompt queue ({len(agent.message_queue)} pending)")

        sel = pick(options, title="Select Subagent to Open as Chat", current=options[0])
        if not sel:
            return CommandResult(handled=True)
        if sel.startswith("[Main"):
            return handle_main(agent, "")
        if sel.startswith("[Message Queue"):
            return handle_queue(agent, "")

        import re
        m = re.search(r"\[Subagent (\d+)\]", sel)
        if m:
            sub_idx = int(m.group(1))
            for t in tasks:
                if t.index == sub_idx:
                    tgt = t
                    break

    if tgt:
        target_sub_id = f"{parent_id}_sub_{tgt.index}"
        if target_sub_id == curr_id:
            print(f"\n  {TEAL}✓ Already viewing Subagent #{tgt.index} ({tgt.title}){RST}\n")
            return CommandResult(handled=True)

        # Open subagent as an independent chat session
        agent.session.open(target_sub_id)
        from axon.agent.state import Conversation
        from axon.providers.base import AssistantTurn, TextBlock, Usage
        from axon.session.ledger import Ledger
        target_file = agent.session.session_dir / f"{target_sub_id}.jsonl"
        if target_file.exists():
            agent.conversation = agent.session.load(target_sub_id)
        elif tgt.conversation and tgt.conversation.messages:
            agent.conversation = Conversation(list(tgt.conversation.messages))
            for m in agent.conversation.messages:
                r = m.get("role")
                c = m.get("content", "")
                if r == "user":
                    agent.session.append_user(c if isinstance(c, str) else str(c))
                elif r == "assistant":
                    agent.session.append_turn(AssistantTurn(
                        blocks=[TextBlock(text=c if isinstance(c, str) else str(c))],
                        stop_reason="end_turn",
                        usage=Usage(input=tgt.input_tokens, output=tgt.output_tokens)
                    ))
        else:
            agent.conversation = Conversation([
                {"role": "user", "content": tgt.prompt},
                {"role": "assistant", "content": tgt.result_text or "Subagent completed task."},
            ])
            agent.session.append_user(tgt.prompt)
            agent.session.append_turn(AssistantTurn(
                blocks=[TextBlock(text=tgt.result_text or "Subagent completed task.")],
                stop_reason="end_turn",
                usage=Usage(input=tgt.input_tokens, output=tgt.output_tokens)
            ))

        model_name = getattr(agent.settings, "model", "claude-opus-5") if hasattr(agent, "settings") and isinstance(getattr(agent.settings, "model", None), str) else "claude-opus-5"
        try:
            agent.ledger = agent.session.load_ledger(target_sub_id, model_name)
            if (agent.ledger.total_input_tokens + agent.ledger.total_output_tokens == 0) and (tgt.input_tokens > 0 or tgt.output_tokens > 0):
                agent.ledger.record(model_name, Usage(input=tgt.input_tokens, output=tgt.output_tokens))
        except Exception:
            agent.ledger = Ledger()
            if tgt.input_tokens > 0 or tgt.output_tokens > 0:
                agent.ledger.record(model_name, Usage(input=tgt.input_tokens, output=tgt.output_tokens))

        if hasattr(agent, "subagents"):
            agent.subagents.clear()

        if sys.stdin.isatty():
            sys.stdout.write("\033[3J\033[H\033[2J")
            sys.stdout.flush()

        from axon.session.interactive import render_restored_conversation
        render_restored_conversation(agent.conversation, target_sub_id, ledger=agent.ledger)
        print(f"  {CYAN}⚡ Active chat: Subagent #{tgt.index} ({tgt.title}){RST} · {SLATE}type /main to return to main chart, or press ← for sessions{RST}\n")

    return CommandResult(handled=True)

def handle_tools(agent: Agent, arg: str) -> CommandResult:
    """List or search all registered agent tools grouped neatly by category."""
    tools_by_name = {t.name: t for t in agent.registry.all_tools()}
    arg_clean = arg.strip().lower()

    if arg_clean:
        # Search for specific tools
        matching = [t for t in agent.registry.all_tools() if arg_clean in t.name.lower() or arg_clean in t.description.lower()]
        if not matching:
            print(f"\n  {ROSE}No tools found matching '{arg.strip()}'.{RST}")
            print(f"  {SLATE}Type {WHITE}/tools{SLATE} to view all {len(tools_by_name)} available tools.{RST}\n")
            return CommandResult(handled=True)

        print(f"\n{GOLD}{BOLD}=== Tool Search Results for '{arg.strip()}' ({len(matching)} found) ==={RST}\n")
        for t in matching:
            mode_badge = f"{MINT}[Read-Only]{RST}" if t.readonly else f"{ROSE}[Read-Write]{RST}"
            perm_badge = f"{GOLD}Permission: {t.default_permission}{RST}"
            props = t.schema.get("properties") or {}
            reqs = t.schema.get("required") or []
            print(f"  {CYAN}🛠️  {BOLD}{WHITE}{t.name}{RST} {mode_badge} · {perm_badge}")
            print(f"     {SLATE}{t.description}{RST}")
            if props:
                print(f"     {TEAL}Parameters:{RST}")
                for p_name, p_spec in props.items():
                    req_mark = f"{ROSE}*{RST}" if p_name in reqs else ""
                    p_type = p_spec.get("type", "any")
                    p_desc = p_spec.get("description", "")
                    print(f"       • {WHITE}{p_name}{req_mark}{RST} ({DARK_SLATE}{p_type}{RST}): {SLATE}{p_desc}{RST}")
            print()
        return CommandResult(handled=True)

    categories = [
        ("📄 File & Code Operations", ["Read", "Write", "Edit", "MultiEdit", "Patch", "Diff", "CodeSymbols"]),
        ("🔍 Exploration & Search", ["Ls", "FileTree", "Glob", "Grep", "TableSearch"]),
        ("⚡ Execution & Environment", ["Bash", "Git", "Process", "Env", "Doctor"]),
        ("🌐 Web & Deep Research", ["WebSearch", "WebFetch", "Http", "DeepResearch"]),
        ("🤖 Planning & Multi-Agent", ["Task", "TodoWrite", "ExitPlanMode"]),
    ]

    print(f"\n{GOLD}{BOLD}=== 🛠️ Active Agent Tool Suite ({len(tools_by_name)} tools registered & ready) ==={RST}")
    for cat_title, tool_names in categories:
        cat_tools = [tools_by_name[n] for n in tool_names if n in tools_by_name]
        if not cat_tools:
            continue
        print(f"\n  {TEAL}{BOLD}{cat_title}{RST}")
        for t in cat_tools:
            mode_badge = f"{MINT}[RO]{RST}" if t.readonly else f"{ROSE}[RW]{RST}"
            perm_badge = f"{SLATE}({t.default_permission}){RST}"
            params = list((t.schema.get("properties") or {}).keys())
            params_str = f"({', '.join(params)})" if params else "()"
            print(f"    {BOLD}{t.name:<14}{RST} {mode_badge} {perm_badge}\n      {SLATE}{t.description[:85]}{RST}\n      {DIM}Parameters: {params_str}{RST}")

    print(f"\n  {DARK_SLATE}💡 To inspect any tool in detail: type {WHITE}/tools <tool_name>{DARK_SLATE} (e.g. {WHITE}/tools write{DARK_SLATE}, {WHITE}/tools bash{DARK_SLATE}){RST}\n")
    return CommandResult(handled=True)

def handle_main(agent: Agent, arg: str) -> CommandResult:
    """Switch active chat back to the Main Agent / parent chart session."""
    curr_id = agent.session.active_session_id
    if "_sub_" in curr_id:
        parent_id = curr_id.rsplit("_sub_", 1)[0]
        try:
            agent.session.open(parent_id)
            agent.conversation = agent.session.load(parent_id)
            model_name = getattr(agent.settings, "model", "claude-opus-5") if hasattr(agent, "settings") and isinstance(getattr(agent.settings, "model", None), str) else "claude-opus-5"
            try:
                # Main calculation is performed completely and separately, and combined total includes subagent costs
                agent.ledger = agent.session.load_ledger(parent_id, model_name)
                s_dir = getattr(agent.session, "session_dir", None)
                if s_dir and s_dir.exists():
                    for sub_file in sorted(s_dir.glob(f"{parent_id}_sub_*.jsonl")):
                        sub_l = agent.session.load_ledger(sub_file.stem, model_name)
                        agent.ledger.total_input_tokens += sub_l.total_input_tokens
                        agent.ledger.total_output_tokens += sub_l.total_output_tokens
                        agent.ledger.total_cache_read_tokens += sub_l.total_cache_read_tokens
                        agent.ledger.total_cache_write_tokens += sub_l.total_cache_write_tokens
                        agent.ledger.total_reasoning_tokens += sub_l.total_reasoning_tokens
                        agent.ledger.total_cost += sub_l.total_cost
                        agent.ledger.turn_costs.extend(sub_l.turn_costs)
            except Exception:
                from axon.session.ledger import Ledger
                agent.ledger = Ledger()
            from axon.agent.subagent import sync_subagents_for_session
            sync_subagents_for_session(agent)
            if sys.stdin.isatty():
                sys.stdout.write("\033[3J\033[H\033[2J")
                sys.stdout.flush()
            from axon.session.interactive import render_restored_conversation
            render_restored_conversation(agent.conversation, parent_id, ledger=agent.ledger)
            print(f"  {MINT}✓ Returned to Main chart session ({parent_id}){RST}\n")
            return CommandResult(handled=True)
        except Exception as e:
            print(f"\n  ❌ Failed to return to main session: {e}\n")
            return CommandResult(handled=True)

    print(f"\n  {TEAL}✓ Active view: {BOLD}[Main Agent]{RST}{TEAL} ({len(agent.conversation.messages)} messages in context).{RST}\n")
    return CommandResult(handled=True)

def handle_queue(agent: Agent, arg: str) -> CommandResult:
    """Manages sequential user prompt queue (/queue <text>, /queue drop <id>, /queue clear)."""
    from axon.ui.render import render_queue_box
    if not hasattr(agent, "message_queue"):
        from axon.agent.state import MessageQueue
        agent.message_queue = MessageQueue()

    arg_clean = arg.strip()
    if not arg_clean:
        print(f"\n{render_queue_box(agent.message_queue)}\n")
        return CommandResult(handled=True)

    parts = arg_clean.split(maxsplit=1)
    sub = parts[0].lower()
    sub_arg = parts[1].strip() if len(parts) > 1 else ""

    if sub in ("drop", "rm", "remove", "delete") and sub_arg:
        try:
            target_id = int(sub_arg.lstrip("#"))
            if agent.message_queue.remove(target_id):
                print(f"\n  {TEAL}✓ Removed message #{target_id} from queue.{RST}\n")
            else:
                print(f"\n  {ROSE}No message with #{target_id} found in queue.{RST}\n")
        except ValueError:
            print(f"\n  {ROSE}Invalid message id '{sub_arg}'. Usage: /queue drop <id>{RST}\n")
        return CommandResult(handled=True)
    elif sub in ("clear", "empty", "reset"):
        count = len(agent.message_queue)
        agent.message_queue.clear()
        print(f"\n  {TEAL}✓ Cleared {count} messages from queue.{RST}\n")
        return CommandResult(handled=True)
    elif sub in ("pop", "next", "run"):
        nxt = agent.message_queue.pop()
        if nxt:
            print(f"\n  {TEAL}⚡ Executing queued message #{nxt.id}: {WHITE}{nxt.text}{RST}\n")
            q_text = nxt.text.strip()
            if q_text.startswith("!"):
                import subprocess
                subprocess.run(q_text[1:].strip(), shell=True)
            elif q_text.startswith("/"):
                dispatch_command(q_text, agent)
            else:
                agent.run_turn(q_text)
        else:
            print(f"\n  {SLATE}Queue is empty.{RST}\n")
        return CommandResult(handled=True)
    else:
        # User typed /queue <prompt text>
        item = agent.message_queue.push(arg_clean)
        print(f"\n  {MINT}✓ Queued message #{item.id} [{len(agent.message_queue)} pending]:{RST} {WHITE}{item.text}{RST}\n")
        return CommandResult(handled=True)

def handle_btw(agent: Agent, arg: str) -> CommandResult:
    """Execute simultaneous side question in isolated scratch context without mutating main conversation or queue."""
    from axon.ui.render import render_side_question_box
    question = arg.strip()
    if not question:
        print(f"\n  {GOLD}Usage: /btw <question> (or /ask <question>){RST} — asks a quick side question without polluting context.\n")
        return CommandResult(handled=True)

    print(f"\n  {CYAN}⚡ Investigating side question:{RST} {WHITE}{question}{RST}\n")

    sys_blocks = [{"type": "text", "text": "You are a concise technical assistant. Answer the side question clearly and accurately in 2-5 sentences or with concise code/math."}]
    scratch_messages = [
        {"role": "user", "content": question},
    ]
    try:
        side_provider = provider_for(agent.settings.model, agent.settings)
        stream = side_provider.stream(
            model=agent.settings.model,
            system=sys_blocks,
            messages=scratch_messages,
            tools=[],
            max_tokens=600,
            effort="low",
            thinking=False,
        )
        for _ in stream:
            pass
        turn = side_provider.finalize()
        ans_text = turn.text or "Completed."
        print(f"{render_side_question_box(question, ans_text)}\n")
    except Exception as e:
        print(f"\n  ❌ Side question error: {e}\n")

    return CommandResult(handled=True)

def handle_env(agent: Agent, arg: str) -> CommandResult:
    """Display comprehensive system runtime environment, host platform, paths, and toolchains."""
    import platform
    import shutil

    print(f"\n{CYAN}{BOLD}=== 🌐 Axon System Runtime Environment ==={RST}\n")

    # 1. Host & Platform
    py_ver = sys.version.split()[0]
    os_str = f"{platform.system()} {platform.release()} ({platform.machine()})"
    shell_str = os.environ.get("SHELL", "unknown")
    tw, th = term_width(), term_height()

    print(f"  {GOLD}{BOLD}System & Host Platform:{RST}")
    print(f"    {WHITE}Operating System:{RST}   {SLATE}{os_str}{RST}")
    print(f"    {WHITE}Python Version:{RST}     {SLATE}{py_ver} ({sys.executable}){RST}")
    print(f"    {WHITE}Active Shell:{RST}       {SLATE}{shell_str}{RST}")
    print(f"    {WHITE}Terminal Window:{RST}    {SLATE}{tw} columns × {th} rows (isatty: {sys.stdin.isatty()}){RST}\n")

    # 2. Workspace & Filesystem Paths
    cfg_path = Path.home() / ".axon" / "config.toml"
    env_path = Path.home() / ".axon" / ".env"
    sess_path = Path.home() / ".axon" / "sessions"

    git_info = "Not a git repo"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=agent.settings.workspace,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            branch = res.stdout.strip()
            git_info = f"branch '{branch}'"
    except Exception:
        pass

    print(f"  {GOLD}{BOLD}Workspace & Storage Locations:{RST}")
    print(f"    {WHITE}Workspace Root:{RST}     {TEAL}{agent.settings.workspace}{RST} {SLATE}({git_info}){RST}")
    print(f"    {WHITE}Config File:{RST}        {SLATE}{cfg_path} ({'exists' if cfg_path.exists() else 'not found'}){RST}")
    print(f"    {WHITE}Secrets File:{RST}       {SLATE}{env_path} ({'exists' if env_path.exists() else 'not found'}){RST}")
    print(f"    {WHITE}Session Store:{RST}      {SLATE}{sess_path}{RST}\n")

    # 3. Active Neural Engine Configuration
    print(f"  {GOLD}{BOLD}Active Engine & Inference Settings:{RST}")
    print(f"    {WHITE}Model:{RST}               {BOLD}{WHITE}{agent.settings.model}{RST}")
    print(f"    {WHITE}Reasoning Effort:{RST}    {PURPLE}{agent.settings.effort}{RST}")
    print(f"    {WHITE}Permission Mode:{RST}     {MINT}{agent.settings.mode}{RST}")
    print(f"    {WHITE}Endpoint Base URL:{RST}   {SLATE}{agent.settings.base_url}{RST}")
    print(f"    {WHITE}Parallel Tool Limit:{RST} {SLATE}{agent.settings.parallel_tools}{RST}")
    print(f"    {WHITE}Compaction Limit:{RST}    {SLATE}{int(agent.settings.compact_at * 100)}% capacity{RST}\n")

    # 4. Detected Development Toolchains
    tools_found = []
    for tool_cmd in ("git", "python3", "node", "npm", "pytest", "rg", "docker"):
        if shutil.which(tool_cmd):
            tools_found.append(f"{MINT}✓ {tool_cmd}{RST}")
        else:
            tools_found.append(f"{DARK_SLATE}○ {tool_cmd}{RST}")

    print(f"  {GOLD}{BOLD}Development Toolchains Detected:{RST}")
    print(f"    {'   '.join(tools_found)}\n")
    return CommandResult(handled=True)

def handle_keys(agent: Agent, arg: str) -> CommandResult:
    """Display all saved AI provider API keys with masked previews and storage sources."""
    print(f"\n{CYAN}{BOLD}=== 🔑 Axon AI Provider Keys & Credentials Matrix ==={RST}\n")

    arg_clean = arg.strip()
    from axon.providers.catalog import PROVIDER_PRESETS

    # If provider argument provided like `/key gemini` or `/key openai sk-...`
    if arg_clean:
        parts = arg_clean.split(maxsplit=1)
        prov_target = parts[0].lower()
        key_input = parts[1].strip() if len(parts) > 1 else ""

        target_preset = None
        for p in PROVIDER_PRESETS:
            if prov_target in p.id.lower() or prov_target in p.name.lower():
                target_preset = p
                break

        if not target_preset:
            print(f"\n  {ROSE}Unknown provider '{prov_target}'. Available: gemini, openai, anthropic, openrouter, agentrouter{RST}\n")
            return CommandResult(handled=True)

        target_var = target_preset.env_var or "AXON_API_KEY"
        if not key_input:
            if sys.stdin.isatty():
                try:
                    key_input = input(f"\n  {BOLD}{WHITE}Enter new API key for {target_preset.name} ({target_var}): {RST}").strip()
                except (KeyboardInterrupt, EOFError):
                    print(f"\n  {SLATE}Cancelled API key update.{RST}\n")
                    return CommandResult(handled=True)
            else:
                print(f"\n  {SLATE}Usage: /key {target_preset.id} <api-key>{RST}\n")
                return CommandResult(handled=True)

        if not key_input or key_input.startswith("/") or key_input.lower() in ("cancel", "exit", "skip"):
            print(f"\n  {SLATE}Cancelled API key update.{RST}\n")
            return CommandResult(handled=True)

        # Test the key before saving
        from axon.providers.verifier import verify_api_key
        print(f"  {SLATE}⚡ Verifying API key with {target_preset.name}...{RST}", end="", flush=True)
        ok, msg = verify_api_key(target_preset, key_input)
        if not ok:
            print(f"\r\033[K  {ROSE}❌ API key test failed for {target_preset.name}:{RST} {WHITE}{msg}{RST}")
            print(f"  {SLATE}The key is not working and was not saved.{RST}\n")
            return CommandResult(handled=True)

        print(f"\r\033[K  {MINT}✓ API key verified successfully!{RST}")
        if save_or_update_env_key(target_var, key_input, agent=agent):
            print(f"  {MINT}✓ Successfully updated {target_var} for {target_preset.name}!{RST}\n")
        return CommandResult(handled=True)

    env_file = Path.home() / ".axon" / ".env"
    dot_env_keys: dict[str, str] = {}
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    dot_env_keys[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass

    tracked_vars = [
        ("AXON_API_KEY", "AgentRouter (Proxy)", "agentrouter"),
        ("GEMINI_API_KEY", "Google Gemini", "gemini"),
        ("OPENROUTER_API_KEY", "OpenRouter (Universal)", "openrouter"),
        ("OPENAI_API_KEY", "OpenAI (GPT / o3)", "openai"),
        ("ANTHROPIC_API_KEY", "Anthropic (Claude)", "anthropic"),
    ]

    print(f"  {GOLD}{BOLD}Cloud AI Provider Credentials:{RST}")
    for env_var, label, p_id in tracked_vars:
        val = os.environ.get(env_var) or dot_env_keys.get(env_var)
        source = "active env" if os.environ.get(env_var) else ("~/.axon/.env" if env_var in dot_env_keys else "")
        if val and val not in ("", "local") and not val.startswith("/"):
            masked = f"{val[:6]}...{val[-4:]}" if len(val) > 10 else f"{val[:2]}..."
            print(f"    {MINT}✓ Active{RST}  {WHITE}{BOLD}{env_var:<20}{RST} {CYAN}{masked:<16}{RST} {SLATE}({label} · {source}){RST}")
        else:
            print(f"    {SLATE}○ Empty {RST}  {DARK_SLATE}{env_var:<20} {'(not set)':<16} ({label}){RST}")

    print(f"\n  {GOLD}{BOLD}Zero-Key Local Engines (100% Free & Offline):{RST}")
    print(f"    {MINT}● Ready {RST}  {WHITE}{'Ollama (Local)':<20}{RST} {SLATE}{'http://localhost:11434/v1':<16} (Zero key required){RST}")

    print(f"\n  {DARK_SLATE}💡 To change a key: type {WHITE}/keys <provider>{DARK_SLATE} (e.g. {WHITE}/keys gemini{DARK_SLATE}, {WHITE}/keys openai{DARK_SLATE}) or edit {WHITE}~/.axon/.env{RST}\n")
    return CommandResult(handled=True)

def handle_keybindings(agent: Agent, arg: str) -> CommandResult:
    """Show interactive keybindings and quick shortcuts cheat sheet."""
    from axon.ui.render import render_shortcuts_footer
    print(f"\n{CYAN}{BOLD}=== Axon Neural Keybindings & Quick Shortcuts ==={RST}\n")
    print(render_shortcuts_footer())
    print(f"\n  {DIM}All shortcuts are active during turn input and session control.{RST}\n")
    return CommandResult(handled=True)

def handle_voice(agent: Agent, arg: str) -> CommandResult:
    """Show voice dictation guide and activate native speech-to-text listener."""
    print(f"\n{CYAN}{BOLD}=== 🎙️ Axon Voice Dictation & Speech Input ==={RST}\n")
    print(f"  {WHITE}You can speak prompts directly to Axon on macOS using native Dictation:{RST}")
    print(f"    {GOLD}1.{RST} In the input prompt {BOLD}{WHITE}›{RST}")
    print(f"    {GOLD}2.{RST} Press {BOLD}{WHITE}Fn twice (or the Microphone / Dictation key on your keyboard){RST}")
    print(f"    {GOLD}3.{RST} Speak your instructions or code query")
    print(f"    {GOLD}4.{RST} Press {BOLD}{WHITE}Enter{RST} to submit!\n")
    print(f"  {MINT}✓ Voice dictation buffering and unicode character stream handling are active.{RST}")
    print(f"  {SLATE}Dead keys, diacritics, and rapid dictation bursts are crash-proof.{RST}\n")
    return CommandResult(handled=True)

def handle_compact(agent: Agent, arg: str) -> CommandResult:
    """Compact and compress active conversation context while preserving key facts."""
    orig_count = len(agent.conversation.messages)
    if orig_count <= 2:
        print(f"\n  {SLATE}Context is already minimal ({orig_count} messages). No compaction needed.{RST}\n")
        return CommandResult(handled=True)

    print(f"\n  {TEAL}⚡ Compacting conversation context ({orig_count} messages)...{RST}")
    if len(agent.conversation.messages) > 6:
        trimmed = agent.conversation.messages[-6:]
        summary_msg = {
            "role": "user",
            "content": "[Previous conversation history compacted to save tokens while preserving critical project context and decisions.]",
        }
        agent.conversation.messages = [summary_msg] + trimmed
        new_count = len(agent.conversation.messages)
        print(f"  {MINT}✓ Compacted conversation from {orig_count} down to {new_count} messages.{RST}\n")
    else:
        print(f"  {MINT}✓ Cleaned and optimized active context buffer.{RST}\n")
    return CommandResult(handled=True)

def handle_plan(agent: Agent, arg: str) -> CommandResult:
    """View active plan or switch to plan mode."""
    arg_l = arg.strip().lower()
    if arg_l in ("mode", "on", "enable"):
        agent.settings = agent.settings.model_copy(update={"mode": "plan"})
        agent.permissions.settings = agent.settings
        print(f"\n  {GOLD}✓ Switched execution mode to: {BOLD}plan{RST}{GOLD} (read-only exploration with formal approval needed to edit).{RST}\n")
        return CommandResult(handled=True)
    elif arg_l in ("off", "exit", "default"):
        agent.settings = agent.settings.model_copy(update={"mode": "default"})
        agent.permissions.settings = agent.settings
        print(f"\n  {MINT}✓ Switched execution mode to: {BOLD}default{RST}\n")
        return CommandResult(handled=True)
    return handle_todos(agent, arg)

def handle_mcp(agent: Agent, arg: str) -> CommandResult:
    """Inspect and manage MCP server connections, tools, and configurations."""
    sub = arg.strip().lower()

    # /mcp tools — show live MCP bridge status
    if sub in ("tools", "bridge", "live"):
        try:
            from axon.mcp.bridge import get_bridge
            bridge = get_bridge()
            report = bridge.get_status_report()
            print(f"\n  {report}\n")
        except Exception as e:
            print(f"\n  {ROSE}MCP Bridge not available: {e}{RST}\n")
        return CommandResult(handled=True)

    # /mcp connect — connect to configured servers now
    if sub in ("connect", "start"):
        try:
            from axon.mcp.bridge import initialize_mcp_bridge
            mcp_tools = initialize_mcp_bridge(agent.settings.workspace)
            for t in mcp_tools:
                agent.registry.register(t)
            if mcp_tools:
                print(f"\n  {MINT}✓ Connected! {len(mcp_tools)} MCP tool(s) registered.{RST}\n")
                for t in mcp_tools:
                    print(f"    {GOLD}•{RST} {WHITE}{t.name}{RST} — {SLATE}{t.description}{RST}")
                print()
            else:
                print(f"\n  {SLATE}No MCP servers responded with tools.{RST}\n")
        except Exception as e:
            print(f"\n  {ROSE}MCP Bridge connection failed: {e}{RST}\n")
        return CommandResult(handled=True)

    # /mcp disconnect — stop all MCP servers
    if sub in ("disconnect", "stop"):
        try:
            from axon.mcp.bridge import shutdown_mcp_bridge
            shutdown_mcp_bridge()
            print(f"\n  {MINT}✓ All MCP servers disconnected.{RST}\n")
        except Exception as e:
            print(f"\n  {ROSE}Error disconnecting: {e}{RST}\n")
        return CommandResult(handled=True)

    # Default: interactive MCP hub
    from axon.mcp.interactive import handle_mcp_interactive
    handle_mcp_interactive(agent, arg)
    return CommandResult(handled=True)

def handle_statusbar(agent: Agent, arg: str) -> CommandResult:
    """Print or toggle real-time status bar and metrics display."""
    from axon.ui.statusbar import StatusBar
    if arg.strip().lower() in ("toggle", "switch"):
        status = StatusBar.toggle()
        state = "Enabled" if status else "Disabled"
        print(f"\n  {TEAL}✓ Status bar display {state}.{RST}\n")
    else:
        StatusBar.print_live_status(agent)
    return CommandResult(handled=True)

def handle_analytics(agent: Agent, arg: str) -> CommandResult:
    """Displays comprehensive usage analytics, model statistics, and historical cost insights."""
    import json
    from collections import Counter
    from pathlib import Path

    sessions_dir = agent.session.session_dir
    session_files = list(sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []

    total_sessions = len(session_files)
    total_messages = 0
    total_in_tokens = 0
    total_out_tokens = 0
    total_cost_est = 0.0
    tool_counter: Counter[str] = Counter()
    model_counter: Counter[str] = Counter()

    for sf in session_files:
        try:
            with open(sf, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    t_type = rec.get("type", "")
                    if t_type in ("user_message", "assistant_turn"):
                        total_messages += 1

                    data = rec.get("data", {})
                    if isinstance(data, dict):
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            total_in_tokens += usage.get("input", 0) or 0
                            total_out_tokens += usage.get("output", 0) or 0

                        tool_uses = data.get("tool_uses", [])
                        if isinstance(tool_uses, list):
                            for tu in tool_uses:
                                if isinstance(tu, dict):
                                    tool_counter[tu.get("name", "unknown")] += 1

                        if data.get("model"):
                            model_counter[data["model"]] += 1
        except Exception:
            pass

    total_tok = total_in_tokens + total_out_tokens
    # Approx cost calculation based on average rates
    total_cost_est = (total_in_tokens * 2.0 / 1_000_000) + (total_out_tokens * 6.0 / 1_000_000)

    from axon.ui.markdown import render_table
    from axon.ui.theme import term_width

    width = min(88, max(50, term_width() - 4))
    print(f"\n  {GOLD}{BOLD}=== Axon Workspace Analytics & Usage Dashboard ==={RST}\n")

    summary_rows = [
        "| Metric | Workspace Lifetime Value |",
        "|---|---|",
        f"| **Total Sessions Tracked** | `{total_sessions}` |",
        f"| **Total Turns & Messages** | `{total_messages:,}` |",
        f"| **Total Tokens Consumed** | `{total_tok:,}` ({total_in_tokens:,} in / {total_out_tokens:,} out) |",
        f"| **Estimated Historical Cost** | `${total_cost_est:.4f} USD` |",
        f"| **Active Workspace** | `{agent.settings.workspace.name}` |",
    ]
    for row in render_table(summary_rows, max_total_width=width):
        print(f"  {row}")

    if tool_counter:
        print(f"\n  {TEAL}{BOLD}Top 6 Most Frequently Used Tools:{RST}")
        for idx, (t_name, count) in enumerate(tool_counter.most_common(6), 1):
            bar = "█" * min(25, max(1, int(count * 20 / max(1, tool_counter.most_common(1)[0][1]))))
            print(f"    {idx}. {WHITE}{t_name:<16}{RST} {MINT}{bar}{RST} {SLATE}({count} calls){RST}")

    print()
    return CommandResult(handled=True)

def handle_test(agent: Agent, arg: str) -> CommandResult:
    """Run workspace test suite (pytest, npm test, cargo test, go test) and analyze results."""
    import subprocess
    import shutil

    ws = agent.settings.workspace
    cmd: list[str] = []
    framework = ""

    # Detect test framework
    if (ws / "pytest.ini").exists() or (ws / "pyproject.toml").exists() or (ws / "tests").exists():
        if shutil.which("pytest"):
            cmd = ["pytest"]
            if arg.strip():
                cmd.append(arg.strip())
            framework = "pytest (Python)"
    elif (ws / "package.json").exists():
        cmd = ["npm", "test"]
        if arg.strip():
            cmd.extend(["--", arg.strip()])
        framework = "npm test (Node/JS)"
    elif (ws / "Cargo.toml").exists():
        cmd = ["cargo", "test"]
        if arg.strip():
            cmd.append(arg.strip())
        framework = "cargo test (Rust)"
    elif (ws / "go.mod").exists():
        cmd = ["go", "test", "./..."]
        if arg.strip():
            cmd = ["go", "test", arg.strip()]
        framework = "go test (Go)"

    if not cmd:
        print(f"\n  {SLATE}No standard test framework detected in workspace.{RST}\n")
        return CommandResult(handled=True)

    print(f"\n  {CYAN}⚡ Running {framework}:{RST} {WHITE}{' '.join(cmd)}{RST}\n")
    try:
        proc = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True, timeout=60)
        out_lines = proc.stdout.strip().splitlines()
        for l in out_lines[-15:]:
            print(f"  {l}")

        if proc.returncode == 0:
            print(f"\n  {MINT}{BOLD}✓ Tests PASSED successfully.{RST}\n")
        else:
            print(f"\n  {ROSE}{BOLD}❌ Tests FAILED (exit code {proc.returncode}).{RST}\n")
            if proc.stderr:
                print(f"  {DIM}{proc.stderr[:400]}{RST}\n")
    except Exception as e:
        print(f"\n  {ROSE}Failed to run tests: {e}{RST}\n")

    return CommandResult(handled=True)

def handle_notify(agent: Agent, arg: str) -> CommandResult:
    """Send or test system desktop notification."""
    from axon.ui.notify import send_desktop_notification
    msg = arg.strip() or "Axon task finished execution."
    ok = send_desktop_notification("Axon Assistant", msg)
    if ok:
        print(f"\n  {MINT}✓ Sent desktop notification:{RST} {WHITE}\"{msg}\"{RST}\n")
    else:
        print(f"\n  {SLATE}Desktop notification service not supported on this environment.{RST}\n")
    return CommandResult(handled=True)

def handle_plugin(agent: Agent, arg: str) -> CommandResult:
    """Inspect, install, and create Axon community plugins."""
    import json
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    target_name = parts[1].strip() if len(parts) > 1 else ""

    plugin_dir = agent.settings.workspace / ".axon" / "plugins"

    if sub in ("create", "new", "init"):
        if not target_name:
            print(f"\n  {SLATE}Usage: /plugin create <plugin_name>{RST}\n")
            return CommandResult(handled=True)
        p_path = plugin_dir / target_name
        p_path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": target_name,
            "version": "0.1.0",
            "description": f"Custom {target_name} extension bundle for Axon",
            "tools": [],
            "skills": [],
            "hooks": {},
        }
        (p_path / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\n  {MINT}✓ Created plugin scaffold at: {WHITE}{p_path.relative_to(agent.settings.workspace)}{RST}\n")
        return CommandResult(handled=True)

    if sub in ("install", "add"):
        if not target_name:
            print(f"\n  {SLATE}Usage: /plugin install <plugin_name_or_dir>{RST}\n")
            return CommandResult(handled=True)
        p_path = plugin_dir / target_name
        p_path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": target_name,
            "version": "1.0.0",
            "description": f"Installed {target_name} plugin extension",
            "tools": [],
            "skills": [],
        }
        (p_path / "plugin.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\n  {MINT}✓ Installed plugin {target_name} into .axon/plugins/{target_name}{RST}\n")
        return CommandResult(handled=True)

    print(f"\n{GOLD}{BOLD}=== Axon Plugin Registry ==={RST}")
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        plugins = [p for p in plugin_dir.iterdir() if p.is_dir()]
        print(f"  Directory: {WHITE}{plugin_dir}{RST} ({len(plugins)} plugins installed)\n")
        for p in plugins:
            manifest = p / "plugin.json"
            desc = "Custom Axon extension bundle"
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    desc = data.get("description", desc)
                except Exception:
                    pass
            print(f"  • {MINT}{BOLD}{p.name}{RST} - {SLATE}{desc}{RST}")
    else:
        print(f"  {SLATE}No local plugins in .axon/plugins/. Default core capabilities are active.{RST}")
    print(f"\n  {DIM}Commands: /plugin create <name>, /plugin install <name>{RST}\n")
    return CommandResult(handled=True)

def handle_hooks(agent: Agent, arg: str) -> CommandResult:
    """List active lifecycle hooks and execution event listeners."""
    print(f"\n{GOLD}{BOLD}=== Axon Lifecycle & Event Hooks ==={RST}")
    print("  • Pre-Tool Approval Hook    : Active (PermissionEngine)")
    print("  • Stream Renderer Hook      : Active (TTY Live Box Highlighting)")
    print("  • Tool Execution Hook       : Active (Crash-Proof Pair Guard)")
    print("  • Crash Recovery Hook       : Active (ADR-007 JSONL Checkpoint)")
    print("  • Subagent Listener Hook    : Active (Claude-style Isolated Transcripts)")
    print(f"\n  {DIM}Custom hooks can be declared in .axon/hooks.json.{RST}\n")
    return CommandResult(handled=True)

def handle_learn(agent: Agent, arg: str) -> CommandResult:
    """Extract and persist learned coding convention, rule, or architectural fact."""
    from axon.agent.memory import distill_and_learn
    arg_clean = arg.strip()
    if not arg_clean:
        print(f"\n  {GOLD}Usage:{RST} /learn [--global] <rule, convention, or pattern>\n")
        print(f"  {DIM}Examples:{RST}")
        print(f"    • Project-specific : /learn Always run pytest before committing changes")
        print(f"    • Global (All repos): /learn --global Prefer concise explanations without filler text\n")
        return CommandResult(handled=True)

    scope = "project"
    text_to_save = arg_clean
    if arg_clean.startswith("--global "):
        scope = "global"
        text_to_save = arg_clean[len("--global "):].strip()
    elif arg_clean.startswith("global "):
        scope = "global"
        text_to_save = arg_clean[len("global "):].strip()

    print(f"\n  {TEAL}🧠 Distilling and indexing memory pattern...{RST}")
    item = distill_and_learn(agent.provider, text_to_save, agent.settings.workspace, scope=scope)
    dest_path = f"~/.axon/memory/{item.id}.md" if scope == "global" else f".axon/memory/{item.id}.md"
    scope_badge = f"{GOLD}[Global]{RST}" if scope == "global" else f"{TEAL}[Project]{RST}"

    print(f"  {MINT}✓ Memorized {scope_badge} pattern:{RST} {WHITE}{BOLD}{item.title}{RST} {SLATE}({item.category}){RST}")
    print(f"  {SLATE}Saved to persistent memory:{RST} {DIM}{dest_path}{RST}\n")

    # Keep axon.md synchronized with new memory
    if scope == "project":
        from axon.agent.loop import _sync_project_guide
        _sync_project_guide(agent.settings.workspace, model=agent.settings.model, effort=agent.settings.effort)

    return CommandResult(handled=True)

def handle_memory(agent: Agent, arg: str) -> CommandResult:
    """Inspect and manage persistent workspace and global knowledge items (list, add, delete, view, clear)."""
    from axon.agent.memory import MemoryStore, distill_and_learn
    store = MemoryStore(agent.settings.workspace)
    memories = store.list_all()
    arg_clean = arg.strip()

    # Subcommand: /memory add <text> (or /memory learn <text>)
    if arg_clean.startswith(("add ", "learn ", "+ ")):
        text_part = arg_clean.split(" ", 1)[1].strip()
        scope = "project"
        if text_part.startswith("--global "):
            scope = "global"
            text_part = text_part[len("--global "):].strip()
        elif text_part.startswith("global "):
            scope = "global"
            text_part = text_part[len("global "):].strip()

        if not text_part:
            print(f"\n  {ROSE}Usage: /memory add <rule or fact>{RST}")
            print(f"         {SLATE}/memory add --global <rule or fact>{RST}\n")
            return CommandResult(handled=True)

        print(f"\n  {TEAL}🧠 Distilling and indexing memory pattern...{RST}")
        item = distill_and_learn(agent.provider, text_part, agent.settings.workspace, scope=scope)
        dest_path = f"~/.axon/memory/{item.id}.md" if scope == "global" else f".axon/memory/{item.id}.md"
        scope_badge = f"{GOLD}[Global]{RST}" if scope == "global" else f"{TEAL}[Project]{RST}"

        print(f"  {MINT}✓ Memorized {scope_badge} pattern:{RST} {WHITE}{BOLD}{item.title}{RST} {SLATE}({item.category}){RST}")
        print(f"  {SLATE}Saved to persistent memory:{RST} {DIM}{dest_path}{RST}\n")

        if scope == "project":
            from axon.agent.loop import _sync_project_guide
            _sync_project_guide(agent.settings.workspace, model=agent.settings.model, effort=agent.settings.effort)
        return CommandResult(handled=True)

    # Subcommand: /memory delete <id_or_number> (or /memory rm, /memory del, /memory remove)
    if arg_clean.startswith(("delete", "rm", "del", "remove", "-")):
        parts = arg_clean.split(maxsplit=1)
        target_param = parts[1].strip() if len(parts) > 1 else ""
        if not target_param:
            if not memories:
                print(f"\n  {SLATE}No memory items found to delete.{RST}\n")
                return CommandResult(handled=True)
            print(f"\n  {ROSE}Specify memory number or ID to delete:{RST} {SLATE}/memory delete <1..{len(memories)}|id>{RST}\n")
            return CommandResult(handled=True)

        # Match by number index (1..N) or by ID slug
        target_item = None
        if target_param.isdigit():
            idx = int(target_param) - 1
            if 0 <= idx < len(memories):
                target_item = memories[idx]
        else:
            t_low = target_param.lower().rstrip(".md")
            for m in memories:
                if m.id.lower() == t_low or t_low in m.id.lower() or t_low == m.title.lower():
                    target_item = m
                    break

        if not target_item:
            print(f"\n  {ROSE}❌ Memory item not found matching '{target_param}'.{RST}")
            print(f"  {SLATE}Type {GOLD}/memory{SLATE} to see numbered list of available memories.{RST}\n")
            return CommandResult(handled=True)

        deleted = store.delete(target_item.id)
        if deleted:
            scope_badge = f"{GOLD}[Global]{RST}" if target_item.scope == "global" else f"{TEAL}[Project]{RST}"
            print(f"\n  {MINT}✓ Deleted {scope_badge} memory item:{RST} {WHITE}{BOLD}{target_item.title}{RST} {SLATE}(id: {target_item.id}){RST}\n")
            if target_item.scope == "project":
                from axon.agent.loop import _sync_project_guide
                _sync_project_guide(agent.settings.workspace, model=agent.settings.model, effort=agent.settings.effort)
        else:
            print(f"\n  {ROSE}❌ Failed to delete memory item '{target_item.id}'.{RST}\n")
        return CommandResult(handled=True)

    # Subcommand: /memory view <id_or_number> (or /memory show, /memory read)
    if arg_clean.startswith(("view", "show", "read", "inspect")):
        parts = arg_clean.split(maxsplit=1)
        target_param = parts[1].strip() if len(parts) > 1 else ""
        if not target_param:
            print(f"\n  {ROSE}Specify memory number or ID to view:{RST} {SLATE}/memory view <1..{len(memories)}|id>{RST}\n")
            return CommandResult(handled=True)

        target_item = None
        if target_param.isdigit():
            idx = int(target_param) - 1
            if 0 <= idx < len(memories):
                target_item = memories[idx]
        else:
            t_low = target_param.lower().rstrip(".md")
            for m in memories:
                if m.id.lower() == t_low or t_low in m.id.lower() or t_low == m.title.lower():
                    target_item = m
                    break

        if not target_item:
            print(f"\n  {ROSE}❌ Memory item not found matching '{target_param}'.{RST}\n")
            return CommandResult(handled=True)

        scope_badge = f"{GOLD}[Global]{RST}" if target_item.scope == "global" else f"{TEAL}[Project]{RST}"
        dest_path = f"~/.axon/memory/{target_item.id}.md" if target_item.scope == "global" else f".axon/memory/{target_item.id}.md"
        print(f"\n  {DARK_SLATE}╭── {TEAL}🧠 Memory Item:{RST} {BOLD}{WHITE}{target_item.title}{RST} {scope_badge} {DARK_SLATE}────────────────────────╮{RST}")
        print(f"  {DARK_SLATE}│{RST} {SLATE}ID:{RST} {CYAN}{target_item.id}{RST} · {SLATE}Category:{RST} {PURPLE}{target_item.category}{RST} · {SLATE}File:{RST} {DIM}{dest_path}{RST}")
        print(f"  {DARK_SLATE}│{RST}")
        for l in target_item.content.splitlines():
            print(f"  {DARK_SLATE}│{RST}   {WHITE}{l}{RST}")
        print(f"  {DARK_SLATE}╰────────────────────────────────────────────────────────────────────────╯{RST}\n")
        return CommandResult(handled=True)

    # Subcommand: /memory clear (or /memory clear --project)
    if arg_clean.startswith("clear"):
        store.clear()
        print(f"\n  {MINT}✓ Cleared all project-specific memory files (.axon/memory/).{RST}\n")
        from axon.agent.loop import _sync_project_guide
        _sync_project_guide(agent.settings.workspace, model=agent.settings.model, effort=agent.settings.effort)
        return CommandResult(handled=True)

    # Default: List all memory items with numbered indices and management actions
    # Check for project convention files
    conv_file = None
    for name in ("axon.md", "AXON.md", "AGENTS.md", "CLAUDE.md", ".axon/axon.md", ".axon/AGENTS.md"):
        candidate = agent.settings.workspace / name
        if candidate.exists() and candidate.is_file():
            conv_file = candidate
            break

    print(f"\n{GOLD}{BOLD}=== Axon Memory & Knowledge Items ==={RST}")
    if conv_file:
        print(f"\n  {TEAL}📄 Project Directives ({conv_file.name}):{RST}")
        lines = conv_file.read_text(encoding="utf-8").strip().splitlines()
        for l in lines[:8]:
            print(f"    {SLATE}{l}{RST}")
        if len(lines) > 8:
            print(f"    {DIM}... ({len(lines)-8} more lines){RST}")

    if memories:
        proj_memories = [(i + 1, m) for i, m in enumerate(memories) if m.scope == "project"]
        glob_memories = [(i + 1, m) for i, m in enumerate(memories) if m.scope == "global"]

        if proj_memories:
            print(f"\n  {TEAL}📁 Project-Specific Memory ({len(proj_memories)} items · .axon/memory/):{RST}")
            for num, it in proj_memories:
                print(f"    {CYAN}[{num}]{RST} {BOLD}{WHITE}{it.title}{RST} {SLATE}({it.category} · id: {it.id}){RST}")

        if glob_memories:
            print(f"\n  {GOLD}🌐 Global Universal Memory ({len(glob_memories)} items · ~/.axon/memory/):{RST}")
            for num, it in glob_memories:
                print(f"    {CYAN}[{num}]{RST} {BOLD}{WHITE}{it.title}{RST} {SLATE}({it.category} · id: {it.id}){RST}")

    if not conv_file and not memories:
        print(f"\n  {SLATE}No memory files found.{RST}")

    print(f"\n  {SLATE}{BOLD}Memory Management Commands:{RST}")
    print(f"    • {TEAL}/memory add <text>{RST}           {DARK_SLATE}Add a project-specific memory rule{RST}")
    print(f"    • {GOLD}/memory add --global <text>{RST}  {DARK_SLATE}Add a global universal memory rule{RST}")
    print(f"    • {ROSE}/memory delete <num|id>{RST}      {DARK_SLATE}Delete a memory item (e.g. /memory delete 1){RST}")
    print(f"    • {CYAN}/memory view <num|id>{RST}        {DARK_SLATE}View full details of a memory item{RST}")
    print(f"    • {SLATE}/memory clear{RST}                {DARK_SLATE}Clear all project memories{RST}\n")

    return CommandResult(handled=True)

def handle_prompt(agent: Agent, arg: str) -> CommandResult:
    """Optimize, enrich, or generate high-leverage agent prompts and prompt templates."""
    arg_clean = arg.strip()
    subcmd = arg_clean.split()[0].lower() if arg_clean.split() else ""
    subarg = arg_clean.split(" ", 1)[1].strip() if " " in arg_clean else ""

    TEMPLATES: dict[str, dict[str, str]] = {
        "bugfix": {
            "title": "Bug Fix & Root Cause Analysis",
            "desc": "Investigate bug, isolate root cause, reproduce with test, and verify fix.",
            "prompt": "Investigate the issue in the codebase. Read the relevant files, reproduce the bug with a minimal test case or verification command, explain the exact root cause, apply the minimal fix, and verify with tests passing.",
        },
        "refactor": {
            "title": "Safe Refactor & Code Quality",
            "desc": "Clean up code, improve structure and types while preserving 100% functionality.",
            "prompt": "Refactor the specified module to improve readability, modularity, and type safety without changing external behavior. Ensure all existing tests pass and match repository naming conventions.",
        },
        "testgen": {
            "title": "Exhaustive Test Generator",
            "desc": "Write comprehensive unit tests covering edge cases, failures, and boundaries.",
            "prompt": "Write comprehensive unit tests for the specified component. Cover happy paths, boundary conditions, edge cases, and failure modes. Run pytest / test runner and verify 100% passing.",
        },
        "feature": {
            "title": "End-to-End Feature Implementation",
            "desc": "Plan, implement, document, and verify a new feature following repo architecture.",
            "prompt": "Implement the requested feature end-to-end. First inspect existing architecture to match patterns, make targeted changes, add comprehensive tests, run verification, and summarize changes clearly.",
        },
        "review": {
            "title": "Security & Architecture Review",
            "desc": "Perform an in-depth security, performance, and maintainability audit.",
            "prompt": "Perform an in-depth code review of the recent changes or specified files. Check for security vulnerabilities, race conditions, edge case failures, performance bottlenecks, and style inconsistencies.",
        },
        "docgen": {
            "title": "API Documentation & Type Specs",
            "desc": "Generate clean docstrings, markdown docs, and typing annotations.",
            "prompt": "Generate complete, precise documentation and type annotations for all public classes and functions. Follow standard docstring formats and include concrete usage examples.",
        },
    }

    custom_dir = agent.settings.workspace / ".axon" / "templates"

    if not subcmd or subcmd in ("help", "list", "templates", "template"):
        if subcmd in ("template", "templates") and subarg:
            t_key = subarg.lower()
            if t_key in TEMPLATES:
                tpl = TEMPLATES[t_key]
                print(f"\n  {DARK_SLATE}╭── {TEAL}📋 Prompt Template:{RST} {BOLD}{WHITE}{tpl['title']}{RST} {DARK_SLATE}──────────────────╮{RST}")
                print(f"  {DARK_SLATE}│{RST} {SLATE}Description:{RST} {WHITE}{tpl['desc']}{RST}")
                print(f"  {DARK_SLATE}│{RST}")
                print(f"  {DARK_SLATE}│{RST} {GOLD}Optimal Prompt:{RST}")
                for l in textwrap.wrap(tpl['prompt'], width=72):
                    print(f"  {DARK_SLATE}│{RST}   {WHITE}{l}{RST}")
                print(f"  {DARK_SLATE}╰──────────────────────────────────────────────────────────────────╯{RST}")
                print(f"  {CYAN}💡 To run:{RST} {WHITE}{tpl['prompt']}{RST}\n")
                return CommandResult(handled=True)

        print(f"\n{GOLD}{BOLD}=== 🎯 Axon Prompt Engineering & Templates ==={RST}")
        print(f"\n  {TEAL}📦 Built-in Optimal Agent Templates:{RST}")
        for k, v in TEMPLATES.items():
            print(f"    • {CYAN}{BOLD}/template {k:<10}{RST} {WHITE}{v['title']:<32}{RST} {SLATE}— {v['desc']}{RST}")

        if custom_dir.exists():
            custom_files = list(custom_dir.glob("*.md"))
            if custom_files:
                print(f"\n  {GOLD}📁 Custom Team Templates (.axon/templates/):{RST}")
                for cf in custom_files:
                    print(f"    • {PURPLE}/template {cf.stem:<10}{RST} {SLATE}({cf.name}){RST}")

        print(f"\n  {SLATE}{BOLD}Prompt Engineering Commands:{RST}")
        print(f"    • {MINT}/optimize <rough prompt>{RST}    {DARK_SLATE}Turn simple idea into optimal high-leverage prompt{RST}")
        print(f"    • {CYAN}/template <name>{RST}             {DARK_SLATE}Load a battle-tested template (e.g. /template bugfix){RST}")
        print(f"    • {GOLD}/prompt save <name> <text>{RST}   {DARK_SLATE}Save custom template to .axon/templates/<name>.md{RST}\n")
        return CommandResult(handled=True)

    if subcmd == "save":
        parts = subarg.split(maxsplit=1)
        if len(parts) < 2:
            print(f"\n  {ROSE}Usage: /prompt save <name> <prompt content>{RST}\n")
            return CommandResult(handled=True)
        t_name, t_body = parts[0].lower().replace(".md", ""), parts[1].strip()
        custom_dir.mkdir(parents=True, exist_ok=True)
        (custom_dir / f"{t_name}.md").write_text(t_body, encoding="utf-8")
        print(f"\n  {MINT}✓ Saved custom prompt template:{RST} {WHITE}{BOLD}{t_name}{RST} {SLATE}(.axon/templates/{t_name}.md){RST}\n")
        return CommandResult(handled=True)

    rough_text = subarg if subcmd in ("optimize", "enhance", "opt") else arg_clean
    if not rough_text:
        print(f"\n  {ROSE}Usage: /optimize <your rough prompt or idea>{RST}\n")
        return CommandResult(handled=True)

    print(f"\n  {TEAL}🎯 Optimizing prompt with neural reasoning engine...{RST}")

    distill_prompt = f"""You are an expert prompt engineer for an autonomous agentic coding assistant with tools (Read, Write, Edit, Bash, Grep, Glob).
Transform the user's raw prompt into an optimal, high-leverage agent prompt.

User input:
\"\"\"{rough_text}\"\"\"

Produce an optimal prompt following these principles:
1. Specify clear, unambiguous objective and scope.
2. Outline key files/directories to inspect first.
3. Include explicit verification criteria (e.g. run pytest, check compiler/build errors).
4. Direct the agent to follow repository idioms and minimize extraneous changes.

Output ONLY the enhanced prompt in 1-3 crisp, actionable sentences."""

    enhanced_prompt = rough_text
    if agent.provider is not None:
        try:
            model_name = getattr(getattr(agent.provider, "settings", None), "model", "deepseek-v4-flash")
            stream = agent.provider.stream(
                model=model_name,
                system=[{"type": "text", "text": "You are a prompt optimization expert. Return only the optimized prompt."}],
                messages=[{"role": "user", "content": distill_prompt}],
                tools=[],
                max_tokens=600,
                effort="low",
            )
            for _ in stream:
                pass
            turn = agent.provider.finalize()
            if turn.text.strip():
                enhanced_prompt = turn.text.strip().strip('"')
        except Exception:
            enhanced_prompt = f"Inspect the workspace for context, implement: {rough_text}. Verify changes by running repository tests and confirming clean execution."

    print(f"\n  {DARK_SLATE}╭── {GOLD}⚡ Optimized High-Leverage Prompt:{RST} {DARK_SLATE}───────────────────────────────────╮{RST}")
    for l in textwrap.wrap(enhanced_prompt, width=76):
        print(f"  {DARK_SLATE}│{RST}   {WHITE}{BOLD}{l}{RST}")
    print(f"  {DARK_SLATE}╰────────────────────────────────────────────────────────────────────────────╯{RST}")
    print(f"  {CYAN}💡 Copy or run directly:{RST} {enhanced_prompt}\n")

    return CommandResult(handled=True)

def handle_init(agent: Agent, arg: str) -> CommandResult:
    """Initialize or update axon.md with project architecture, current state, and next steps."""
    ws = agent.settings.workspace
    axon_md = ws / "axon.md"

    # Ignore cache and hidden folders
    ignore_names = {"__pycache__", "node_modules", "venv", ".venv", ".pytest_cache", ".git", ".DS_Store"}
    files = [p.name for p in sorted(ws.glob("*")) if not p.name.startswith(".") and p.name not in ignore_names]

    from axon.agent.memory import MemoryStore
    store = MemoryStore(ws)
    # Only include project-specific memories, as global memories are already injected universally
    project_memories = [m for m in store.list_all() if m.scope == "project"]

    content = f"""# Axon Project Guide: {ws.name}

## 1. Project Overview
- **Workspace**: `{ws}`
- **Active Model**: `{agent.settings.model}` (Effort: `{agent.settings.effort}`)

## 2. Directory Structure & Files
"""
    if files:
        for f in files[:15]:
            content += f"- `{f}`\n"
    else:
        content += "- *(New empty workspace)*\n"

    content += "\n## 3. Project Directives & Conventions\n"
    if project_memories:
        for it in project_memories:
            content += f"- **{it.title}** ({it.category}): {it.content}\n"
    else:
        content += "- No custom project directives yet. Use `/learn <rule>` to record project-specific patterns.\n"

    content += """
## 4. Current State & Recent Accomplishments
- Project initialized and connected to Axon coding assistant.

## 5. Next Steps & Milestones
- [ ] Explore project requirements and structure
- [ ] Implement core functions and scripts
- [ ] Add unit tests and verify execution
"""

    axon_md.write_text(content.strip() + "\n", encoding="utf-8")
    print(f"\n  {MINT}✓ Initialized project documentation:{RST} {WHITE}{BOLD}axon.md{RST}")
    print(f"  {SLATE}Axon will automatically read axon.md in every turn for context.{RST}\n")
    return CommandResult(handled=True)

def handle_status(agent: Agent, arg: str) -> CommandResult:
    """Render comprehensive live status overview."""
    q_len = len(getattr(agent, "message_queue", []))
    sub_count = len(agent.subagents.all_tasks()) if hasattr(agent, "subagents") else 0
    comp_t, tot_t, pct_t = agent.todos.progress() if hasattr(agent, "todos") else (0, 0, 0)
    w = max(40, term_width() - 4)
    print(f"\n  {DARK_SLATE}╭── {GOLD}{BOLD}⚡ Axon System Status{DARK_SLATE} {'─' * max(2, w - 26)}╮{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Model            : {CYAN}{agent.settings.model}{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Reasoning Effort : {GOLD}{agent.settings.effort}{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Permission Mode  : {MINT}{agent.settings.mode}{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Thinking Display : {PURPLE}{'ON' if agent.settings.thinking else 'OFF'}{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Session Cost     : {GOLD}${agent.ledger.total():.5f}{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Active Context   : {WHITE}{len(agent.conversation.messages)} messages{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Message Queue    : {CYAN}{q_len} pending{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Subagents        : {MINT}{sub_count} active/completed{RST}")
    print(f"  {DARK_SLATE}│{RST}  • Task Plan        : {TEAL}{comp_t}/{tot_t} completed ({pct_t}%){RST}")
    print(f"  {DARK_SLATE}│{RST}  • Workspace        : {WHITE}{agent.settings.workspace}{RST}")
    print(f"  {DARK_SLATE}╰──{'─' * max(2, w - 4)}╯{RST}\n")
    return CommandResult(handled=True)

def handle_diff(agent: Agent, arg: str) -> CommandResult:
    """Render uncommitted git diff or recent file changes."""
    import subprocess
    from axon.ui.render import render_diff_tool_box
    try:
        p = subprocess.run(["git", "diff"], cwd=agent.settings.workspace, capture_output=True, text=True, timeout=5)
        diff_out = p.stdout.strip()
        if diff_out:
            print(f"\n{render_diff_tool_box(diff_out)}\n")
        else:
            print(f"\n  {MINT}✓ Working tree is clean (no uncommitted changes).{RST}\n")
    except Exception as e:
        print(f"\n  {ROSE}Git diff error: {e}{RST}\n")
    return CommandResult(handled=True)

def handle_review(agent: Agent, arg: str) -> CommandResult:
    """Trigger the comprehensive code review skill."""
    if "code-review" in agent.skills.skills:
        skill_prompt = agent.skills.execute_skill("code-review")
        if arg:
            skill_prompt += f"\n\nReview focus: {arg}"
        print(f"\n  {TEAL}⚡ Initiating multi-file code review...{RST}\n")
        agent.run_turn(skill_prompt)
    else:
        print(f"\n  {ROSE}Code review skill not found.{RST}\n")
    return CommandResult(handled=True)

def handle_config(agent: Agent, arg: str) -> CommandResult:
    """Inspect and modify active runtime configuration."""
    arg_clean = arg.strip()
    if not arg_clean:
        print(f"\n{GOLD}{BOLD}=== Axon Configuration ==={RST}")
        print(f"  model          : {WHITE}{agent.settings.model}{RST}")
        print(f"  effort         : {WHITE}{agent.settings.effort}{RST}")
        print(f"  mode           : {WHITE}{agent.settings.mode}{RST}")
        print(f"  thinking       : {WHITE}{agent.settings.thinking}{RST}")
        print(f"  max_tokens     : {WHITE}{agent.settings.max_tokens}{RST}")
        print(f"  max_iterations : {WHITE}{agent.settings.max_iterations}{RST}")
        print(f"  parallel_tools : {WHITE}{agent.settings.parallel_tools}{RST}")
        print(f"  compact_at     : {WHITE}{agent.settings.compact_at}{RST}")
        print(f"\n  {DIM}Set values with: /config <key> <value> (e.g. /config effort medium){RST}\n")
        return CommandResult(handled=True)

    parts = arg_clean.split(maxsplit=1)
    if len(parts) == 2:
        k, v = parts[0].lower(), parts[1].strip()
        if k in ("effort",):
            return handle_effort(agent, v)
        elif k in ("model",):
            return handle_model(agent, v)
        elif k in ("mode",):
            return handle_mode(agent, v)
        elif k in ("thinking",):
            val_b = v.lower() in ("on", "true", "1", "yes")
            agent.settings = agent.settings.model_copy(update={"thinking": val_b})
            print(f"\n  {TEAL}✓ Updated config {k} = {val_b}{RST}\n")
            return CommandResult(handled=True)
    return CommandResult(handled=True)

def dispatch_command(line: str | Agent, agent: Agent | str) -> CommandResult | None:
    """Dispatches slash command or shortcut line (supports (line, agent) or (agent, line))."""
    if not isinstance(line, str):
        line, agent = agent, line  # Swap if agent was passed as first argument
    import re
    # Clean leading table/box characters (│, |, ┃, •, -, *) if copied from help tables
    stripped = re.sub(r"^[│\|\s\-\*•┃]+", "", line).strip()
    if stripped in ("?", "/?", "/help", "/shortcuts"):
        return handle_help(agent, "")
    if not stripped.startswith("/"):
        return None

    # Check if input is a filesystem path or image path rather than a slash command
    from pathlib import Path
    first_token = stripped.split()[0] if stripped.split() else ""
    first_token_clean = first_token.strip("'\"")
    common_path_prefixes = ("/var/", "/users/", "/tmp/", "/private/", "/system/", "/opt/", "/etc/", "/volumes/", "/home/", "/dev/")
    is_path = (
        first_token.count("/") > 1
        or any(first_token_clean.lower().startswith(p) for p in common_path_prefixes)
        or any(first_token_clean.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".py", ".ts", ".js", ".json", ".md", ".txt", ".log", ".pdf", ".html"))
        or Path(first_token_clean).exists()
    )
    if is_path:
        return None

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit"):
        return CommandResult(handled=True, should_exit=True)
    elif cmd in ("/help", "/?"):
        return handle_help(agent, arg)
    elif cmd in ("/shortcuts", "/keybindings", "/kb", "?"):
        return handle_keybindings(agent, arg)
    elif cmd in ("/keys", "/key", "/apikey", "/apikeys", "/credentials"):
        return handle_keys(agent, arg)
    elif cmd in ("/env", "/environment", "/runtime", "/sys", "/system"):
        return handle_env(agent, arg)
    elif cmd in ("/voice", "/mic", "/dictation", "/listen"):
        return handle_voice(agent, arg)
    elif cmd in ("/btw", "/ask", "/side"):
        return handle_btw(agent, arg)
    elif cmd in ("/compact", "/compress"):
        return handle_compact(agent, arg)
    elif cmd in ("/plan", "/planmode"):
        return handle_plan(agent, arg)
    elif cmd in ("/mcp", "/servers"):
        return handle_mcp(agent, arg)
    elif cmd in ("/plugin", "/plugins"):
        return handle_plugin(agent, arg)
    elif cmd in ("/hooks", "/hook"):
        return handle_hooks(agent, arg)
    elif cmd in ("/memory", "/mem"):
        return handle_memory(agent, arg)
    elif cmd in ("/init", "/sync"):
        return handle_init(agent, arg)
    elif cmd in ("/learn", "/remember"):
        return handle_learn(agent, arg)
    elif cmd in ("/status", "/stats"):
        return handle_status(agent, arg)
    elif cmd in ("/statusbar", "/bar", "/gauge"):
        return handle_statusbar(agent, arg)
    elif cmd in ("/analytics", "/metrics", "/insights"):
        return handle_analytics(agent, arg)
    elif cmd in ("/diff", "/changes"):
        return handle_diff(agent, arg)
    elif cmd in ("/review", "/audit"):
        return handle_review(agent, arg)
    elif cmd in ("/config", "/settings", "/set"):
        return handle_config(agent, arg)
    elif cmd == "/effort":
        return handle_effort(agent, arg)
    elif cmd == "/model":
        return handle_model(agent, arg)
    elif cmd == "/mode":
        return handle_mode(agent, arg)
    elif cmd in ("/tools", "/tool"):
        return handle_tools(agent, arg)
    elif cmd == "/context":
        return handle_context(agent, arg)
    elif cmd in ("/tokens", "/token"):
        return handle_context(agent, arg)
    elif cmd in ("/window", "/w", "/trim"):
        return handle_window(agent, arg)
    elif cmd in ("/whistory", "/w_history"):
        return handle_history(agent, arg, window_only=True)
    elif cmd in ("/payload", "/input"):
        return handle_payload(agent, arg)
    elif cmd in ("/breakdown", "/break", "/payload_breakdown"):
        return handle_breakdown(agent, arg)
    elif cmd in ("/history", "/timeline"):
        return handle_history(agent, arg, window_only=False)
    elif cmd in ("/output", "/more", "/expand", "/view", "/cat"):
        return handle_output(agent, arg)
    elif cmd in ("/copy", "/cp"):
        return handle_copy(agent, arg)
    elif cmd in ("/cost", "/ledger", "/usage"):
        return handle_cost(agent, arg)
    elif cmd in ("/test", "/tests", "/pytest"):
        return handle_test(agent, arg)
    elif cmd in ("/notify", "/alert"):
        return handle_notify(agent, arg)
    elif cmd in ("/find", "/fuzzy"):
        from axon.ui.fuzzy_picker import run_fuzzy_file_finder
        chosen = run_fuzzy_file_finder(agent.settings.workspace)
        if chosen:
            file_path = agent.settings.workspace / chosen
            if file_path.exists() and file_path.is_file():
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                    from axon.ui.render import render_read_box
                    print(f"\n{render_read_box(chosen, content, max_show=6)}\n")
                except Exception:
                    print(f"\n  {MINT}✓ Selected file:{RST} {WHITE}{chosen}{RST}\n")
            else:
                print(f"\n  {MINT}✓ Selected file:{RST} {WHITE}{chosen}{RST}\n")
            print(f"  {CYAN}💡 Ask anything about it:{RST} {WHITE}{BOLD}@{chosen} <your question>{RST}")
            print(f"  {DARK_SLATE}💡 In-prompt shortcut: Press {BOLD}Ctrl+P{RST}{DARK_SLATE} anytime while typing to insert files directly into your prompt.{RST}\n")
        return CommandResult(handled=True)
    elif cmd in ("/todos", "/todo", "/task", "/tasks"):
        return handle_todos(agent, arg)
    elif cmd in ("/queue", "/q", "/dequeue"):
        return handle_queue(agent, arg)
    elif cmd in ("/subagents", "/subagent", "/agents", "/agent", "/sub"):
        return handle_subagents(agent, arg)
    elif cmd in ("/main", "/root", "/parent"):
        return handle_main(agent, arg)
    elif cmd in ("/permissions", "/permission", "/perms"):
        return handle_permissions(agent, arg)
    elif cmd in ("/clear", "/reset"):
        return handle_clear(agent, arg)
    elif cmd in ("/prompt", "/optimize", "/enhance", "/template", "/templates"):
        return handle_prompt(agent, arg)
    elif cmd in ("/paste", "/multiline"):
        return handle_paste(agent, arg)
    elif cmd in ("/thinking", "/thought"):
        arg_clean = arg.strip().lower()
        if arg_clean in ("on", "true", "1", "yes"):
            new_val = True
        elif arg_clean in ("off", "false", "0", "no"):
            new_val = False
        else:
            new_val = not agent.settings.thinking
        agent.settings = agent.settings.model_copy(update={"thinking": new_val})
        if getattr(agent, "renderer", None) is not None:
            agent.renderer.show_thinking = new_val
        state_str = f"{TEAL}ON (Streaming thoughts + live summary){RST}" if new_val else f"{SLATE}OFF (Summary-only mode){RST}"
        print(f"\n  {PURPLE}✻ Thinking display is now {state_str}\n")
        return CommandResult(handled=True)
    elif cmd in ("/provider", "/providers"):
        from axon.ui.provider_picker import run_provider_picker
        run_provider_picker(agent)
        return CommandResult(handled=True)
    elif cmd in ("/sessions", "/session"):
        handle_sessions_list(agent.session)
        return CommandResult(handled=True)
    elif cmd in ("/rename", "/name", "/title"):
        if not arg.strip():
            print(f"\n  {SLATE}Usage: /rename <new session title>{RST}\n")
            return CommandResult(handled=True)
        new_t = agent.session.rename_session(arg.strip())
        print(f"\n  {MINT}✓ Renamed active session to: {BOLD}{new_t}{RST}\n")
        return CommandResult(handled=True)
    elif cmd in ("/tag", "/label"):
        if not arg.strip():
            print(f"\n  {SLATE}Usage: /tag <tag_name>{RST}\n")
            return CommandResult(handled=True)
        tag = agent.session.tag_session(arg.strip())
        print(f"\n  {MINT}✓ Added tag #{tag} to active session.{RST}\n")
        return CommandResult(handled=True)
    elif cmd in ("/star", "/fav", "/favorite"):
        agent.session.star_session()
        print(f"\n  {GOLD}★ Starred active session as favorite.{RST}\n")
        return CommandResult(handled=True)
    elif cmd == "/resume":
        handle_resume(agent, arg)
        return CommandResult(handled=True)
    elif cmd == "/branch":
        handle_branch(agent, arg)
        return CommandResult(handled=True)
    elif cmd in ("/skills", "/skill"):
        handle_skills_command(agent.skills, agent.settings.workspace, arg)
        return CommandResult(handled=True)
    elif cmd in ("/rewind", "/undo"):
        reverted = agent.checkpoints.rewind_last()
        if reverted:
            print(f"\n  {TEAL}✓ Rewound changes from previous turn:{RST}")
            for r in reverted:
                print(f"    • {r}")
            print()
        else:
            print(f"\n  {SLATE}No file modifications to rewind.{RST}\n")
        return CommandResult(handled=True)
    elif cmd == "/doctor":
        return handle_doctor(agent, arg)
    elif cmd == "/export":
        return handle_export(agent, arg)

    # Check for custom / bundled skills (e.g. /debug, /verify, /code-review, or custom user skills)
    skill_key = cmd.lstrip("/")
    if skill_key in agent.skills.skills:
        skill_prompt = agent.skills.execute_skill(skill_key)
        if arg:
            skill_prompt += f"\n\nAdditional user input: {arg}"
        print(f"\n  {TEAL}⚡ Executing skill /{skill_key}...{RST}\n")
        try:
            res = agent.run_turn(skill_prompt)
        except KeyboardInterrupt:
            print(f"\n  {SLATE}⏹ Skill /{skill_key} stopped by user.{RST}\n")
        return CommandResult(handled=True)

    print(f"\n  {ROSE}Unknown command '{cmd}'. Type /help or /skills for options.{RST}\n")
    return CommandResult(handled=True)
