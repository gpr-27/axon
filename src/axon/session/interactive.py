"""
Interactive /resume, /sessions, /history, and /branch session management.
"""
from __future__ import annotations
import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from axon.session.store import SessionStore
from axon.ui.picker import pick
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, LBLUE, MINT, RST, ROSE, SLATE, TEAL, WHITE, term_width,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent

def handle_sessions_list(store: SessionStore) -> None:
    recent = store.list_recent(limit=None)
    if not recent:
        print(f"\n  {SLATE}No saved sessions found in {store.session_dir}.{RST}\n")
        return

    table_lines = [
        "### Saved Axon Sessions",
        "",
        "| Session ID | Timestamp | First Task / Summary | Entries | Tokens | Status |",
        "|---|---|---|---|---|---|",
    ]

    for meta in recent:
        dt = datetime.datetime.fromtimestamp(meta.created_at).strftime("%Y-%m-%d %H:%M")
        is_active = meta.session_id == store.active_session_id
        status = "🟢 Active" if is_active else "Saved"
        summary = meta.first_prompt.replace("|", "/")
        tok_str = f"{meta.total_tokens:,}" if meta.total_tokens > 0 else "0"
        table_lines.append(f"| `{meta.session_id}` | {dt} | {summary} | {meta.message_count} | {tok_str} | {status} |")

    table_lines.append("")
    table_lines.append("> Use `/resume <session_id>` to switch or restore any session.")
    print(format_markdown("\n".join(table_lines)))

def handle_history(agent: Agent) -> None:
    """Display chronological task history of the current active session."""
    msgs = agent.conversation.messages
    if not msgs:
        print(f"\n  {SLATE}Current session is empty. No tasks executed yet.{RST}\n")
        return

    print(f"\n{GOLD}{BOLD}=== Active Session Task Timeline ({len(msgs)} messages) ==={RST}\n")
    turn_num = 1
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            if isinstance(content, list):
                # Tool result blocks
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        c_text = blk.get("content", "").splitlines()
                        summary = c_text[0][:60] if c_text else "Done"
                        is_err = "❌" if blk.get("is_error") else "✔"
                        print(f"    {SLATE}└─ {is_err} Result: {summary}{RST}")
            else:
                print(f"  {TEAL}{BOLD}[Turn {turn_num}]{RST} {WHITE}{BOLD}User Request:{RST}")
                for line in str(content).splitlines()[:4]:
                    print(f"    {MINT}› {line}{RST}")
                turn_num += 1
        elif role == "assistant":
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        t_name = blk.get("name", "tool")
                        t_inp = blk.get("input", {})
                        arg_summary = t_inp.get("command") or t_inp.get("path") or t_inp.get("pattern") or ""
                        print(f"    {LBLUE}⏺ Action:{RST} {BOLD}{t_name}{RST} {WHITE}{arg_summary}{RST}")
                    elif isinstance(blk, dict) and blk.get("type") == "text":
                        txt = blk.get("text", "").strip().splitlines()
                        if txt:
                            print(f"    {SLATE}💬 Response: {txt[0][:70]}...{RST}")
            elif isinstance(content, str) and content.strip():
                txt = content.strip().splitlines()
                print(f"    {SLATE}💬 Response: {txt[0][:70]}...{RST}")
    print()

def render_restored_conversation(conversation: Any, session_id: str, ledger: Any = None) -> None:
    """Renders the full restored chat history with syntax highlighting, action badges, and token/cost metrics."""
    import sys
    from axon.ui.theme import (
        BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, LBLUE, MINT, RST, ROSE, SLATE, TEAL, WHITE, term_width,
    )
    from axon.ui.markdown import format_markdown
    msgs = getattr(conversation, "messages", [])
    if not msgs:
        print(f"\n  {SLATE}(Restored session is empty){RST}\n")
        return

    width = max(40, term_width() - 4)

    # Metrics summary for header
    tot_tok = (getattr(ledger, "total_input_tokens", 0) or 0) + (getattr(ledger, "total_output_tokens", 0) or 0) if ledger else 0
    cost_val = float(getattr(ledger, "total_cost", 0.0) or 0.0) if ledger else 0.0

    if ledger is not None and tot_tok == 0 and len(msgs) > 0:
        from axon.agent.state import estimate_content_tokens
        est_in = 0
        est_out = 0
        for m in msgs:
            c = m.get("content", "")
            r = m.get("role")
            if r == "user":
                est_in += estimate_content_tokens(c)
            elif r == "assistant":
                est_out += estimate_content_tokens(c)
        if est_in > 0 or est_out > 0:
            from axon.providers.base import Usage
            ledger.record("claude-opus-5", Usage(input=max(100, est_in), output=max(20, est_out)))
            tot_tok = ledger.total_input_tokens + ledger.total_output_tokens
            cost_val = float(ledger.total_cost)

    hdr_metrics = f" · {tot_tok:,} tokens · ${cost_val:.5f}" if ledger and tot_tok > 0 else ""

    print(f"\n  {DARK_SLATE}╭── {TEAL}📂 Restored Chat Session:{RST} {BOLD}{WHITE}{session_id}{RST} {SLATE}({len(msgs)} messages{hdr_metrics}){DARK_SLATE} {'─' * max(2, width - len(session_id) - len(hdr_metrics) - 45)}╮{RST}\n")

    for m in msgs:
        role = m.get("role")
        content = m.get("content")

        if role == "user":
            if isinstance(content, str):
                clean_text = content.strip()
                if len(clean_text) > width - 6 or "\n" in clean_text:
                    sys.stdout.write(f"  {BOLD}{CYAN}›{RST} {WHITE}{clean_text[:500]}{RST}\n\n")
                else:
                    pad_len = max(0, width - len(clean_text) - 4)
                    sys.stdout.write(f"  {GRAY_BG}{BOLD}{WHITE} › {clean_text}{' ' * pad_len} {RST}\n\n")
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        c_text = str(blk.get("content", "")).strip()
                        lines = c_text.splitlines()
                        is_err = blk.get("is_error")
                        icon = f"{ROSE}❌" if is_err else f"{MINT}✓"
                        preview = lines[0][:80] if lines else "Completed"
                        print(f"    {icon} {SLATE}{preview}{RST}")
                        if len(lines) > 1:
                            print(f"      {DIM}... (+{len(lines)-1} lines of output){RST}")
                print()

        elif role == "assistant":
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        b_type = blk.get("type")
                        if b_type == "tool_use":
                            t_name = blk.get("name", "tool")
                            t_inp = blk.get("input", {})
                            arg_summary = t_inp.get("command") or t_inp.get("path") or t_inp.get("pattern") or t_inp.get("query") or ""
                            print(f"  {MINT}⚡ Tool Action:{RST} {GOLD}{BOLD}🛠️ {t_name}{RST} {WHITE}{arg_summary}{RST}")
                        elif b_type == "text":
                            txt = blk.get("text", "")
                            if txt.strip():
                                rendered = format_markdown(txt.strip(), max_width=width)
                                print(f"{rendered}\n")
            elif isinstance(content, str) and content.strip():
                rendered = format_markdown(content.strip(), max_width=width)
                print(f"{rendered}\n")

    # Render prominent cost & token usage summary
    if ledger is not None:
        in_tok = getattr(ledger, "total_input_tokens", 0) or 0
        out_tok = getattr(ledger, "total_output_tokens", 0) or 0
        cache_tok = getattr(ledger, "total_cache_read_tokens", 0) or 0
        reason_tok = getattr(ledger, "total_reasoning_tokens", 0) or 0
        cache_info = f" · cache: {cache_tok:,}" if cache_tok > 0 else ""
        reason_info = f" · reasoning: {reason_tok:,}" if reason_tok > 0 else ""
        
        print(
            f"  {DARK_SLATE}├── {TEAL}{BOLD}📊 Restored Session Usage:{RST} "
            f"{WHITE}{BOLD}{tot_tok:,}{RST} {SLATE}tokens{RST} "
            f"{SLATE}(in: {in_tok:,} · out: {out_tok:,}{cache_info}{reason_info}){RST} "
            f"{DARK_SLATE}·{RST} {GOLD}{BOLD}Total Cost: ${cost_val:.5f}{RST}"
        )

    print(f"  {DARK_SLATE}╰──{'─' * max(2, width - 4)}╯{RST}\n")

def handle_resume(agent: Agent, session_id: str = "") -> None:
    recent = agent.session.list_recent(limit=None)
    if not recent:
        print(f"\n  {SLATE}No past sessions found to resume.{RST}\n")
        return

    options = [m.session_id for m in recent]

    if session_id and session_id in options:
        chosen = session_id
    elif session_id:
        matches = [s for s in options if s.startswith(session_id)]
        chosen = matches[0] if matches else None
    else:
        chosen = pick(options, title="Select Session to Resume", current=agent.session.active_session_id)

    if chosen:
        agent.session.open(chosen)
        agent.conversation = agent.session.load(chosen)
        agent.ledger = agent.session.load_ledger(chosen, agent.settings.model)
        if hasattr(agent, "subagents"):
            from axon.agent.subagent import sync_subagents_for_session
            sync_subagents_for_session(agent)
        render_restored_conversation(agent.conversation, chosen, ledger=agent.ledger)
    else:
        print(f"\n  {SLATE}(Session unchanged: {agent.session.active_session_id}){RST}\n")

def handle_branch(agent: Agent, name: str = "") -> None:
    old_id = agent.session.active_session_id
    new_id = f"{old_id}_branch_{int(datetime.datetime.now().timestamp())}" if not name else f"session_{name}"
    agent.session.open(new_id)

    for m in agent.conversation.messages:
        role = m.get("role")
        if role == "user":
            agent.session.append("user_message", m)
        elif role == "assistant":
            agent.session.append("assistant_turn", {"native": m.get("content")})
        elif role == "tool":
            agent.session.append("tool_results", [m])

    print(f"\n  {TEAL}✓ Branched session into {BOLD}{new_id}{RST}{TEAL}. Original {old_id} preserved.{RST}\n")
