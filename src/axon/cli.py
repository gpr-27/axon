"""
Axon Top-Level CLI: Argument Parsing, Print Mode, and Interactive REPL.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from axon.config import Settings
from axon.agent.loop import Agent
from axon.agent.context import ContextManager
from axon.tools import create_default_registry
from axon.permissions.engine import PermissionEngine
from axon.agent.state import Conversation
from axon.providers.base import AssistantTurn
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.providers.registry import provider_for
from axon.ui.approve import ask_approval
from axon.ui.input import read_input
from axon.ui.live_turn import run_interactive_turn
from axon.ui.render import Renderer
from axon.ui.markdown import format_markdown
from axon.commands.builtin import dispatch_command
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE,
    term_height, term_width,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axon",
        description="A terminal-native agentic coding assistant.",
    )
    parser.add_argument("-v", "--version", action="version", version="%(prog)s vGPR_27", help="Show version and exit")
    parser.add_argument("prompt", nargs="?", default="", help="Prompt to execute")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="One-shot print mode")
    parser.add_argument("--output-format", choices=["text", "json"], default="text", help="Output format for print mode")
    parser.add_argument("--model", help="LLM Model to use (claude-opus-5, gpt-5.6-sol, deepseek-v4-flash, glm-5.3)")
    parser.add_argument("--mode", choices=["default", "acceptEdits", "plan", "bypass"], help="Permission mode")
    parser.add_argument("--effort", choices=["reflex", "balanced", "synapse", "quantum", "low", "medium", "high", "xhigh", "max"], help="Reasoning effort tier")
    parser.add_argument("--workspace", help="Workspace root directory")
    parser.add_argument("--continue", dest="continue_session", action="store_true", help="Resume latest session")
    parser.add_argument("--resume", dest="resume_id", help="Resume specific session ID")
    parser.add_argument("--no-thinking", action="store_true", help="Disable extended reasoning blocks")
    parser.add_argument("--dangerously-skip-permissions", action="store_true", help="Allow bypass mode without prompting")
    return parser

def run_print_mode(agent: Agent, prompt: str, fmt: str, renderer: Renderer | None = None) -> int:
    """Headless or single-shot one-pass execution."""
    if prompt.startswith("/"):
        res = dispatch_command(prompt, agent)
        if res is not None:
            return 0

    t0 = time.time()
    result = agent.run_turn(prompt)
    elapsed = time.time() - t0

    if fmt == "json":
        data = {
            "result": result.final_text,
            "cost_usd": float(agent.ledger.total()),
            "stop_reason": result.stop_reason,
            "iterations": result.iterations,
            "tool_calls": result.tool_calls_count,
            "usage": {
                "input_tokens": result.usage.input,
                "output_tokens": result.usage.output,
                "cache_read_tokens": result.usage.cache_read,
            },
            "elapsed_seconds": elapsed,
        }
        print(json.dumps(data, indent=2))
    else:
        if renderer:
            renderer.turn_footer(
                tool_count=result.tool_calls_count,
                usage=result.usage,
                cost=float(agent.ledger.total()),
                elapsed=elapsed,
            )
        else:
            rendered = format_markdown(result.final_text)
            print(rendered)

    return 0 if result.stop_reason == "end_turn" else 1

def run_repl(agent: Agent, renderer: Renderer) -> int:
    """Interactive terminal REPL loop."""
    if sys.stdin.isatty():
        sys.stdout.write("\033[2J\033[H\n")
        sys.stdout.flush()

    renderer.print_banner(
        version="GPR_27",
        model=agent.settings.model,
        effort=agent.settings.effort,
        workspace=str(agent.settings.workspace),
        mode=agent.settings.mode,
    )

    if hasattr(agent, "subagents"):
        from axon.agent.subagent import sync_subagents_for_session
        sync_subagents_for_session(agent)

    while True:
        try:
            plan_summary = None
            if hasattr(agent, "todos") and agent.todos.items:
                comp, tot, pct = agent.todos.progress()
                if tot > 0:
                    plan_summary = f"📋 Plan: {comp}/{tot} ({pct}%)"

            queue_summary = None
            if hasattr(agent, "message_queue") and len(agent.message_queue) > 0:
                queue_summary = f"📥 Queue: {len(agent.message_queue)} pending"

            subagent_label = None
            curr_id = agent.session.active_session_id
            if "_sub_" in curr_id:
                sub_part = curr_id.split("_sub_")[-1]
                subagent_label = f"Subagent #{sub_part}"

            res_input = read_input(
                mode=agent.settings.mode,
                effort=agent.settings.effort,
                plan_summary=plan_summary,
                queue_summary=queue_summary,
                subagent_label=subagent_label,
            )
            if len(res_input) == 3:
                line, toggled_mode, _ = res_input
            else:
                line, toggled_mode = res_input

            if toggled_mode:
                agent.settings = agent.settings.model_copy(update={"mode": toggled_mode})
                agent.permissions.settings = agent.settings
            line = line.strip()
        except KeyboardInterrupt:
            print(f"\n  {SLATE}(Input cleared · Press Ctrl+D or type /exit to quit){RST}\n")
            continue
        except EOFError:
            print(f"\n\n  {TEAL}Session closed. Total cost: {GOLD}${agent.ledger.total():.5f}{RST}\n")
            break

        if not line:
            # If empty Enter on main prompt and queue has pending items, run the next one
            if hasattr(agent, "message_queue") and len(agent.message_queue) > 0:
                nxt = agent.message_queue.pop()
                if nxt:
                    line = nxt.text
                else:
                    continue
            else:
                continue

        # Session Switcher Dashboard trigger (Left Arrow on empty prompt or /sessions)
        if line == "__SWITCH_SESSION__" or line.lower() in ("/sessions", "/dashboard", "/switcher", "/chats"):
            from axon.ui.switcher import run_session_dashboard
            selected_target = run_session_dashboard(agent)
            if selected_target:
                if sys.stdin.isatty():
                    sys.stdout.write("\033[3J\033[H\033[2J")
                    sys.stdout.flush()

                renderer.print_banner(
                    version="GPR_27",
                    model=agent.settings.model,
                    effort=agent.settings.effort,
                    workspace=str(agent.settings.workspace),
                    mode=agent.settings.mode,
                )

                if selected_target.startswith("__NEW_SESSION__:"):
                    prompt_buf = selected_target.split(":", 1)[1].strip()
                    new_id = agent.reset_for_new_session()
                    if prompt_buf:
                        line = prompt_buf
                    else:
                        print(f"  {MINT}⚡ Started fresh session ({new_id}){RST}\n")
                        continue
                else:
                    try:
                        agent.session.open(selected_target)
                        agent.conversation = agent.session.load(selected_target)
                        agent.ledger = agent.session.load_ledger(selected_target, agent.settings.model)
                        if hasattr(agent, "subagents"):
                            from axon.agent.subagent import sync_subagents_for_session
                            sync_subagents_for_session(agent)
                        from axon.session.interactive import render_restored_conversation
                        render_restored_conversation(agent.conversation, selected_target, ledger=agent.ledger)
                    except Exception as e:
                        print(f"\n  ❌ Failed to switch session: {e}\n")
                    continue
            else:
                if sys.stdin.isatty():
                    sys.stdout.write("\033[3J\033[H\033[2J")
                    sys.stdout.flush()
                renderer.print_banner(
                    version="GPR_27",
                    model=agent.settings.model,
                    effort=agent.settings.effort,
                    workspace=str(agent.settings.workspace),
                    mode=agent.settings.mode,
                )
                if agent.conversation.messages:
                    from axon.session.interactive import render_restored_conversation
                    render_restored_conversation(agent.conversation, agent.session.active_session_id, ledger=agent.ledger)
                continue

        # Direct shell execution shortcut with '!' prefix
        if line.startswith("!"):
            cmd_shell = line[1:].strip()
            if cmd_shell:
                print(f"\n  {GOLD}⚡ Shell:{RST} {WHITE}{cmd_shell}{RST}\n")
                if cmd_shell == "cd" or cmd_shell.startswith("cd "):
                    target_dir = cmd_shell[2:].strip() or str(Path.home())
                    target_path = Path(target_dir).expanduser()
                    if not target_path.is_absolute():
                        target_path = Path.cwd() / target_path
                    if target_path.exists() and target_path.is_dir():
                        os.chdir(target_path)
                        print(f"  {MINT}✓ Changed directory to {target_path}{RST}\n")
                    else:
                        print(f"  {ROSE}Directory not found: {target_dir}{RST}\n")
                else:
                    try:
                        subprocess.run(cmd_shell, shell=True)
                    except Exception as e:
                        print(f"  {ROSE}Command execution failed: {e}{RST}\n")
                print()
            continue

        try:
            cmd_res = dispatch_command(line, agent)
            if cmd_res is not None:
                if cmd_res.should_exit:
                    print(f"\n  {TEAL}Goodbye! Total cost: {GOLD}${agent.ledger.total():.5f}{RST}\n")
                    break
                continue

            display_line = line

            # Expand @file mentions into prompt context
            if "@" in line and not line.startswith("/"):
                import re
                matches = re.findall(r"@([a-zA-Z0-9_\-\./\\]+)", line)
                for m in matches:
                    p = agent.settings.workspace / m
                    if p.exists() and p.is_file():
                        try:
                            content_snippet = p.read_text(encoding="utf-8", errors="replace")[:4000]
                            line = line.replace(f"@{m}", f"\n\n[File: {m}]\n```\n{content_snippet}\n```\n")
                        except Exception:
                            pass

            # Support multi-question batch queuing separated by ';;'
            if ";;" in line and not line.startswith("/"):
                raw_parts = [p.strip() for p in line.split(";;") if p.strip()]
                if len(raw_parts) > 1:
                    line = raw_parts[0]
                    for extra_q in raw_parts[1:]:
                        item = agent.message_queue.push(extra_q)
                    print(f"\n  {MINT}✓ Queued {len(raw_parts)-1} follow-up question{'s' if len(raw_parts) > 2 else ''} (Total in queue: {len(agent.message_queue)}){RST}\n")

            # Ingest and compact any attached images
            from axon.agent.images import compact_image_paths
            line, attachments = compact_image_paths(line)
            if attachments:
                print(f"  {CYAN}🖼️  Attached Image{'s' if len(attachments) != 1 else ''} ({len(attachments)} pending):{RST}")
                for att in attachments:
                    kb_size = att.size_bytes / 1024
                    dim_str = f" · {att.width}x{att.height}" if att.width and att.height else ""
                    print(f"    {GOLD}• {att.label}{RST} {WHITE}{BOLD}{Path(att.original_path).name}{RST} {SLATE}({kb_size:.1f} KB{dim_str}){RST}")
                print()

            # Render clean user message bubble (without expanded context dump)
            renderer.render_user_message(display_line)
            turn_input = line

            # Execute agent turn cleanly
            t0 = time.time()
            try:
                res = agent.run_turn(turn_input)
                elapsed = time.time() - t0
                renderer.turn_footer(
                    tool_count=res.tool_calls_count,
                    usage=res.usage,
                    cost=float(agent.ledger.total()),
                    elapsed=elapsed,
                    llm_calls=res.iterations,
                )
            except Exception as e:
                print(f"\n  {ROSE}❌ Error during execution: {e}{RST}\n")

            # Auto-process queued messages sequentially
            while hasattr(agent, "message_queue") and len(agent.message_queue) > 0:
                nxt = agent.message_queue.pop()
                if not nxt:
                    break
                print(f"\n  {CYAN}📥 [Processing Queued #{nxt.id} · {len(agent.message_queue)} remaining]:{RST} {WHITE}{BOLD}{nxt.text}{RST}\n")
                renderer.render_user_message(nxt.text)
                t_q0 = time.time()
                res = agent.run_turn(nxt.text)
                elapsed = time.time() - t_q0
                renderer.turn_footer(
                    tool_count=res.tool_calls_count,
                    usage=res.usage,
                    cost=float(agent.ledger.total()),
                    elapsed=elapsed,
                    llm_calls=res.iterations,
                )
        except KeyboardInterrupt:
            print(f"\n\n  {TEAL}⏹ Stopped turn by user.{RST}\n")
        except Exception as e:
            print(f"\n  ❌ Turn execution error: {e}\n")

    return 0

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Gather stdin if piped
    stdin_content = ""
    if not sys.stdin.isatty():
        try:
            import select
            r, _, _ = select.select([sys.stdin], [], [], 0.0)
            if r:
                stdin_content = sys.stdin.read().strip()
        except Exception:
            pass

    prompt = args.prompt
    if stdin_content:
        prompt = f"{stdin_content}\n\n{prompt}".strip() if prompt else stdin_content

    # Load settings with CLI overrides
    overrides = {
        "model": args.model,
        "mode": args.mode,
        "effort": args.effort,
        "workspace": Path(args.workspace).resolve() if args.workspace else None,
        "thinking": False if args.no_thinking else None,
        "dangerously_skip_permissions": args.dangerously_skip_permissions or None,
    }
    settings = Settings.load(overrides)

    # Initialize subsystems
    provider = provider_for(settings.model, settings)
    tools = create_default_registry()
    permissions = PermissionEngine(settings)
    context = ContextManager(settings)
    session = SessionStore(workspace=settings.workspace)
    ledger = Ledger()
    renderer = Renderer(show_thinking=settings.thinking)

    # Create agent
    agent = Agent(
        provider=provider,
        tools=tools,
        permissions=permissions,
        context=context,
        session=session,
        ledger=ledger,
        settings=settings,
        on_event=renderer.on_event,
        on_approval=lambda tool, targs, dec: ask_approval(tool, targs, dec.reason),
    )
    agent.renderer = renderer

    # Handle session continuation
    if args.continue_session:
        latest = session.latest()
        if latest:
            agent.conversation = session.load(latest)
            agent.ledger = session.load_ledger(latest, agent.settings.model)
            tot_tok = agent.ledger.total_input_tokens + agent.ledger.total_output_tokens
            print(f"  {TEAL}✓ Resumed latest session: {latest} ({len(agent.conversation.messages)} msgs · {tot_tok:,} tokens · ${agent.ledger.total():.5f}){RST}")
    elif args.resume_id:
        agent.conversation = session.load(args.resume_id)
        agent.ledger = session.load_ledger(args.resume_id, agent.settings.model)
        tot_tok = agent.ledger.total_input_tokens + agent.ledger.total_output_tokens
        print(f"  {TEAL}✓ Resumed session: {args.resume_id} ({len(agent.conversation.messages)} msgs · {tot_tok:,} tokens · ${agent.ledger.total():.5f}){RST}")

    if args.print_mode or prompt:
        return run_print_mode(agent, prompt, args.output_format, renderer)
    else:
        return run_repl(agent, renderer)

if __name__ == "__main__":
    sys.exit(main())
