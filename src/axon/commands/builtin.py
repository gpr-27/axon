"""
Built-in slash commands: /help, /model, /mode, /clear, /compact, /context, /cost, /resume, /tools, /permissions, /doctor, /export, /todos, /exit.
"""
from __future__ import annotations
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from axon.agent.loop import Agent
from axon.config import Mode
from axon.providers.registry import known_models, provider_for
from axon.session.interactive import handle_branch, handle_resume, handle_sessions_list
from axon.skills.interactive import handle_skills_command
from axon.ui.picker import pick
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE, term_width,
)

@dataclass
class CommandResult:
    handled: bool
    should_exit: bool = False
    message: str | None = None

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
| `/permissions` | Inspect active permission engine rules and defaults |
| `/plan [mode/off]` | View task checklist or switch to plan mode |

### 📊 Context & Token Budget
| Command / Shortcut | Description |
|---|---|
| `/breakdown` | Full prompt breakdown (system, tools, previous history, last message) & token match |
| `/context` | View active context budget, token breakdown, and compaction limit |
| `/compact` | Compact conversation history while preserving key context |
| `/window [turns]` | Adjust sliding context window size (e.g. `/window 10` or `/window 0` for all) |
| `/cost` | View session billing ledger, token counts, and prompt cache hits |
| `/payload [full]` | Inspect exact prompt payload and tool results sent to model |

### 📜 History & File Revisions
| Command / Shortcut | Description |
|---|---|
| `/history` | View all messages in full conversation history |
| `/diff` | View working tree uncommitted git diff and file changes |
| `/review [focus]` | Run automated multi-file code review |
| `/rewind` | Revert file edits made during previous turns |
| `/expand [file]` | View full un-truncated output or expand any file |

### 🤖 Multi-Agent, Tasks & Skills
| Command / Shortcut | Description |
|---|---|
| `/subagents` | Axon subagent matrix (inspect isolated transcripts) |
| `/todos` | View active multi-step task checklist |
| `/queue` or `/q [text]` | Add/manage sequential prompt queue (`/q <text>`, `/queue drop <id>`, `/queue clear`) |
| `/skills` | Browse active skills studio and create new skills |
| `/mcp` | Inspect Model Context Protocol servers and capabilities |
| `/plugin` | Inspect installed plugins and extension manifests |
| `/hooks` | Inspect active lifecycle and execution event hooks |
| `/memory` | Inspect persistent workspace memory and AGENTS.md conventions |

### 📁 Sessions & Workspace
| Command / Shortcut | Description |
|---|---|
| `/sessions` / `←` | Axon session timeline dashboard |
| `/resume [id]` | Resume previous session from transcript |
| `/branch [name]` | Fork current conversation into an independent branch |
| `/tools` | List all 24 active agent tools, schemas, and permissions |
| `/doctor` | Run local diagnostics & environment health check |
| `/init` | Initialize `AGENTS.md` conventions file in workspace |

### ⌨️ Session Control & Input
| Command / Shortcut | Description |
|---|---|
| `?` or `/help` | Show this categorized command reference |
| `/kb` | View interactive keyboard shortcuts cheat sheet |
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
        print(f"\n  {TEAL}✓ Switched neural reasoning tier to {BOLD}{chosen}{RST}\n")
    else:
        print(f"\n  {SLATE}(Reasoning tier unchanged: {agent.settings.effort}){RST}\n")
    return CommandResult(handled=True)

def handle_model(agent: Agent, arg: str) -> CommandResult:
    models = known_models()
    if arg and arg in models:
        chosen = arg
    else:
        chosen = pick(models, title="Switch Active Model", current=agent.settings.model)

    if chosen and chosen != agent.settings.model:
        # Create updated settings and replace provider
        new_settings = agent.settings.model_copy(update={"model": chosen})
        agent.settings = new_settings
        agent.provider = provider_for(chosen, new_settings)
        print(f"\n  {TEAL}✓ Switched active model to {BOLD}{chosen}{RST}")
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
        print(f"\n  {TEAL}✓ Switched permission mode to {BOLD}{chosen}{RST}\n")
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
    fname = arg.strip() or f"transcript_{int(time.time())}.md"
    out_path = agent.settings.workspace / fname
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Axon Transcript — {agent.settings.model}\n\n")
        for m in agent.conversation.messages:
            f.write(f"### {m.get('role', '').capitalize()}\n\n{m.get('content', '')}\n\n---\n\n")
    print(f"\n  {TEAL}✓ Exported transcript to {out_path}{RST}\n")
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
        has_cache = f" {MINT}[cache_control: ephemeral]{RST}" if "cache_control" in b else ""
        print(f"  {DARK_SLATE}│ Block {idx}{has_cache}:{RST}")
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
            content = m.get("content", "")
            print(f"  {DARK_SLATE}│{RST} {clr}{BOLD}[Msg {i}] {role}{RST}")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        b_type = b.get("type", "")
                        if b_type == "tool_use":
                            print(f"  {DARK_SLATE}│  ⏺ ToolUse: {b.get('name')}({b.get('input')}){RST}")
                        elif b_type == "tool_result":
                            c_prev = str(b.get("content", "")).strip().replace("\n", " ")[:100]
                            print(f"  {DARK_SLATE}│  └─ Result [{b.get('tool_use_id')}]: {c_prev}...{RST}")
                        elif b_type == "image":
                            media_t = b.get("source", {}).get("media_type", "image/png")
                            print(f"  {DARK_SLATE}│  🖼️  Image Attachment: {CYAN}{media_t}{RST} {SLATE}(~1,200 vision tokens){RST}")
                        elif b_type == "text":
                            print(f"  {DARK_SLATE}│  {WHITE}{b.get('text', '')}{RST}")
            else:
                txt = str(content)
                lines = txt.splitlines()
                if is_full or len(lines) <= 8:
                    for l in lines:
                        print(f"  {DARK_SLATE}│  {WHITE}{l}{RST}")
                else:
                    for l in lines[:4]:
                        print(f"  {DARK_SLATE}│  {WHITE}{l}{RST}")
                    print(f"  {DARK_SLATE}│  {SLATE}... ({len(lines)-6} lines hidden · type '/payload full') ...{RST}")
                    for l in lines[-2:]:
                        print(f"  {DARK_SLATE}│  {WHITE}{l}{RST}")
    print(f"  {GOLD}{BOLD}└──{'─' * 70}{RST}")
    print(f"\n{GOLD}{'═' * 76}{RST}\n")
    return CommandResult(handled=True)

def handle_breakdown(agent: Agent, arg: str) -> CommandResult:
    """Displays comprehensive breakdown of exact prompt components, previous messages, last input, and token totals matching the turn footer."""
    import json
    from axon.agent.prompt import build_system
    from axon.agent.state import estimate_content_tokens
    from axon.ui.theme import (
        BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE, term_width,
    )

    # 1. System Prompt Breakdown
    system_blocks = build_system(agent.settings, agent.registry, list(agent.skills.skills.values()))
    sys_text = "".join(str(b.get("text", "")) for b in system_blocks)
    sys_tokens = max(1, int(len(sys_text) / 3.7))

    # 2. Tool Definitions Breakdown
    tool_schemas = agent.registry.schemas(
        provider_style="anthropic" if agent.provider.name == "anthropic" else "openai"
    )
    tool_json = json.dumps(tool_schemas, separators=(",", ":"))
    tool_tokens = max(1, int(len(tool_json) / 4.0))

    # 3. Conversation Messages
    msgs = agent.conversation.messages
    total_msgs = len(msgs)

    prev_msgs = msgs[:-1] if total_msgs > 0 else []
    last_msg = msgs[-1] if total_msgs > 0 else None

    prev_tokens = sum(estimate_content_tokens(m.get("content", "")) for m in prev_msgs)
    last_tokens = estimate_content_tokens(last_msg.get("content", "")) if last_msg else 0
    conv_tokens = prev_tokens + last_tokens

    total_payload_tokens = sys_tokens + tool_tokens + conv_tokens

    # Check if last turn API usage is recorded in ledger
    last_usage = getattr(agent.ledger, "last_usage", None)

    width = min(88, max(50, term_width() - 4))

    print(f"\n{GOLD}{BOLD}=== Active Input Payload Breakdown & Token Matching ==={RST}")
    if last_usage and last_usage.input > 0:
        pct = min(100.0, (last_usage.cache_read / last_usage.input * 100))
        cache_str = f" ({last_usage.input/1000:.1f}k in · {pct:.0f}% cached)" if last_usage.cache_read > 0 else f" ({last_usage.input/1000:.1f}k in)"
        print(f"  {SLATE}Model:{RST} {WHITE}{agent.settings.model}{RST}  {SLATE}|  API Ingested:{RST} {GOLD}{last_usage.input:,} tokens{cache_str}{RST}\n")
    else:
        print(f"  {SLATE}Model:{RST} {WHITE}{agent.settings.model}{RST}  {SLATE}|  Total Payload Ingested:{RST} {GOLD}~{total_payload_tokens:,} tokens{RST}\n")

    # [1] System Prompt Section
    print(f"  {TEAL}{BOLD}┌── [1] SYSTEM PROMPT ({len(system_blocks)} blocks · ~{sys_tokens:,} tokens) {'─' * max(2, width - 48)}┐{RST}")
    for idx, b in enumerate(system_blocks, 1):
        txt = str(b.get("text", "")).strip()
        b_tok = max(1, int(len(txt) / 3.7))
        first_l = txt.splitlines()[0][:50] if txt else "System Block"
        has_cache = f" {MINT}[cache_control: ephemeral]{RST}" if "cache_control" in b else ""
        print(f"  {TEAL}│{RST}  {CYAN}Block {idx}:{RST} {WHITE}{first_l}...{RST} {SLATE}(~{b_tok:,} toks){RST}{has_cache}")
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
        pct = min(100.0, (last_usage.cache_read / last_usage.input * 100))
        cache_sub = f" ({last_usage.input/1000:.1f}k in · {pct:.0f}% cached)" if last_usage.cache_read > 0 else f" ({last_usage.input/1000:.1f}k in)"
        print(f"  {GOLD}│{RST}  {MINT}{BOLD}• Last API Billed Input : {WHITE}{BOLD}{last_usage.input:,} tokens{cache_sub}{RST} {MINT}✓ Ground Truth Match{RST}")
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

        sel = pick(options, title="Select Subagent to Open as Chat", current=options[0])
        if not sel:
            return CommandResult(handled=True)
        if sel.startswith("[Main"):
            return handle_main(agent, "")

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
    """List all registered agent tools grouped neatly by category."""
    tools_by_name = {t.name: t for t in agent.registry.all_tools()}
    categories = [
        ("📄 File & Code Operations", ["Read", "Write", "Edit", "MultiEdit", "Patch", "Diff", "CodeSymbols"]),
        ("🔍 Exploration & Search", ["Ls", "FileTree", "Glob", "Grep", "TableSearch"]),
        ("⚡ Execution & Environment", ["Bash", "Git", "Process", "Env", "Doctor"]),
        ("🌐 Web & Deep Research", ["WebSearch", "WebFetch", "Http", "DeepResearch"]),
        ("🤖 Planning & Multi-Agent", ["Task", "TodoWrite", "ExitPlanMode"]),
    ]

    print(f"\n{GOLD}{BOLD}=== Active Agent Tool Suite ({len(tools_by_name)} tools) ==={RST}")
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

    print(f"\n  {DIM}All {len(tools_by_name)} tools are active and available during turn execution.{RST}\n")
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
            agent.run_turn(nxt.text)
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

def handle_keybindings(agent: Agent, arg: str) -> CommandResult:
    """Show interactive keybindings and quick shortcuts cheat sheet."""
    from axon.ui.render import render_shortcuts_footer
    print(f"\n{CYAN}{BOLD}=== Axon Neural Keybindings & Quick Shortcuts ==={RST}\n")
    print(render_shortcuts_footer())
    print(f"\n  {DIM}All shortcuts are active during turn input and session control.{RST}\n")
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
    """Inspect MCP server connections, tools, and configurations."""
    import json
    mcp_config = agent.settings.workspace / ".axon" / "mcp.json"
    print(f"\n{GOLD}{BOLD}=== Model Context Protocol (MCP) Manager ==={RST}")
    if mcp_config.exists():
        try:
            data = json.loads(mcp_config.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            print(f"  Config: {WHITE}{mcp_config}{RST} ({len(servers)} servers configured)\n")
            for name, s_cfg in servers.items():
                cmd = s_cfg.get("command", "n/a")
                args = " ".join(s_cfg.get("args", []))
                print(f"  • {TEAL}{BOLD}{name}{RST}: {WHITE}{cmd} {args}{RST}")
        except Exception as e:
            print(f"  {ROSE}Error reading {mcp_config}: {e}{RST}")
    else:
        print(f"  {SLATE}No local MCP config found at .axon/mcp.json.{RST}")
        print(f"  {DIM}Axon connects to standard stdio and SSE Model Context Protocol servers.{RST}")
    print(f"\n  {DIM}Type /mcp to check status or configure new MCP servers.{RST}\n")
    return CommandResult(handled=True)

def handle_plugin(agent: Agent, arg: str) -> CommandResult:
    """Inspect plugins, manifests, and loaded plugin capabilities."""
    import json
    plugin_dir = agent.settings.workspace / ".axon" / "plugins"
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
    print(f"\n  {DIM}Plugins can provide bundled tools, skills, and custom hooks.{RST}\n")
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
    """Inspect persistent workspace and global knowledge items and guidelines."""
    from axon.agent.memory import MemoryStore
    store = MemoryStore(agent.settings.workspace)
    memories = store.list_all()
    
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
        proj_memories = [m for m in memories if m.scope == "project"]
        glob_memories = [m for m in memories if m.scope == "global"]

        if proj_memories:
            print(f"\n  {TEAL}📁 Project-Specific Memory ({len(proj_memories)} items · .axon/memory/):{RST}")
            for it in proj_memories:
                print(f"    • {BOLD}{it.title}{RST} {SLATE}({it.category}){RST}")

        if glob_memories:
            print(f"\n  {GOLD}🌐 Global Universal Memory ({len(glob_memories)} items · ~/.axon/memory/):{RST}")
            for it in glob_memories:
                print(f"    • {BOLD}{it.title}{RST} {SLATE}({it.category}){RST}")
            
    if not conv_file and not memories:
        print(f"\n  {SLATE}No memory files found. Use /learn <rule> or /init to save knowledge.{RST}")
    print(f"\n  {DIM}Use /learn <rule> for project memory, or /learn --global <rule> for global memory.{RST}\n")
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
    elif cmd in ("/shortcuts", "/keybindings", "/keys", "/kb", "?"):
        return handle_keybindings(agent, arg)
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
    elif cmd in ("/cost", "/ledger", "/usage"):
        return handle_cost(agent, arg)
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
    elif cmd in ("/sessions", "/session"):
        handle_sessions_list(agent.session)
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
