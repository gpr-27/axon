"""
Interactive session management: /sessions listing, deletion, /resume, /branch and
rendering of a restored transcript.
"""
from __future__ import annotations

import datetime
import re
import sys
from typing import TYPE_CHECKING, Any

from axon.session.store import SessionStore
from axon.ui.picker import pick
from axon.ui.theme import BOLD, DARK_SLATE, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE, term_width

if TYPE_CHECKING:
    from axon.agent.loop import Agent


def _fmt_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def handle_sessions_list(store: SessionStore) -> None:
    recent = store.list_recent(limit=None)
    if not recent:
        print(f"\n  {SLATE}No saved sessions for this workspace (store: {store.session_dir}).{RST}\n")
        return

    from axon.ui.markdown import format_markdown
    table_lines = [
        "### Saved Sessions",
        "",
        "| Session | Updated | Title | Msgs | Tokens | Cost | |",
        "|---|---|---|---|---|---|---|",
    ]
    for meta in recent:
        flags = []
        if meta.session_id == store.active_session_id:
            flags.append("active")
        if meta.is_starred:
            flags.append("★")
        summary = meta.first_prompt.replace("|", "/")
        if meta.tags:
            summary += f" `#{meta.tags[0]}`"
        table_lines.append(
            f"| `{meta.session_id}` | {_fmt_ts(meta.created_at)} | {summary} | {meta.message_count} | "
            f"{meta.total_tokens:,} | ${meta.total_cost:.4f} | {' '.join(flags)} |"
        )
    total_spend = sum(m.total_cost for m in recent)
    total_tokens = sum(m.total_tokens for m in recent)
    table_lines += [
        "",
        f"**Total:** `${total_spend:.4f}` across `{len(recent)}` sessions (`{total_tokens:,}` tokens)",
        "",
        "> `/resume <id>` to switch · `/sessions delete <id>` to remove · `←` opens the switcher",
    ]
    print(format_markdown("\n".join(table_lines)))


def _match_session(ids: list[str], arg: str) -> tuple[str | None, list[str]]:
    if arg in ids:
        return arg, []
    matches = [s for s in ids if s.startswith(arg)] or [s for s in ids if arg in s]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def handle_session_delete(store: SessionStore, session_id: str = "") -> None:
    """Delete one session (id, unique prefix, or picker) or all with --all."""
    recent = store.list_recent(limit=None)
    if not recent:
        print(f"\n  {SLATE}No saved sessions found to delete.{RST}\n")
        return

    clean_arg = session_id.strip()
    if clean_arg in ("--all", "-a", "all", "clear"):
        count = store.delete_all_sessions()
        print(f"\n  {MINT}✓ Deleted {count} session(s) for this workspace.{RST}\n")
        return

    ids = [m.session_id for m in recent]
    chosen_id: str | None = None
    if clean_arg:
        chosen_id, candidates = _match_session(ids, clean_arg)
        if not chosen_id:
            if candidates:
                print(f"\n  {GOLD}'{clean_arg}' matches {len(candidates)} sessions - be more specific:{RST}")
                for c in candidates[:8]:
                    print(f"    {SLATE}{c}{RST}")
                print()
            else:
                print(f"\n  {ROSE}✗ Session not found: {clean_arg}{RST}\n")
            return
    else:
        picker_options = [f"{m.session_id}  (${m.total_cost:.4f} · {m.first_prompt[:25]})" for m in recent]
        chosen = pick(picker_options, title="Select Session to Delete")
        if not chosen:
            print(f"\n  {SLATE}(Delete cancelled){RST}\n")
            return
        chosen_id = chosen.split()[0]

    if store.delete_session(chosen_id):
        print(f"\n  {MINT}✓ Deleted session {BOLD}{chosen_id}{RST}\n")
    else:
        print(f"\n  {ROSE}✗ Failed to delete session {chosen_id}{RST}\n")


def render_restored_conversation(conversation: Any, session_id: str, ledger: Any = None) -> None:
    """Render a restored transcript with tool badges and its recorded token/cost usage."""
    from axon.ui.markdown import format_markdown

    msgs = getattr(conversation, "messages", [])
    if not msgs:
        print(f"\n  {SLATE}(Restored session is empty){RST}\n")
        return

    width = max(40, term_width() - 4)
    in_tok = int(getattr(ledger, "total_input_tokens", 0) or 0) if ledger is not None else 0
    out_tok = int(getattr(ledger, "total_output_tokens", 0) or 0) if ledger is not None else 0
    tot_tok = in_tok + out_tok
    cost_val = float(getattr(ledger, "total_cost", 0.0) or 0.0) if ledger is not None else 0.0
    hdr_metrics = f" · {tot_tok:,} tokens · ${cost_val:.5f}" if tot_tok > 0 else ""

    fill = max(2, width - len(session_id) - len(hdr_metrics) - 45)
    print(f"\n  {DARK_SLATE}╭── {TEAL}Restored Chat Session:{RST} {BOLD}{WHITE}{session_id}{RST} "
          f"{SLATE}({len(msgs)} messages{hdr_metrics}){DARK_SLATE} {'─' * fill}╮{RST}\n")

    for m in msgs:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            if isinstance(content, str):
                try:
                    from axon.ui.input import format_user_prompt_banner
                    sys.stdout.write(f"{format_user_prompt_banner(content.strip(), max_w=width)}\n\n")
                except Exception:
                    print(f"  {TEAL}›{RST} {content.strip()}\n")
            elif isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        lines = str(blk.get("content", "")).strip().splitlines()
                        icon = f"{ROSE}✗" if blk.get("is_error") else f"{MINT}✓"
                        print(f"    {icon} {SLATE}{(lines[0][:80] if lines else 'Completed')}{RST}")
                        if len(lines) > 1:
                            print(f"      {DIM}... (+{len(lines) - 1} lines of output){RST}")
                    elif isinstance(blk, dict) and blk.get("type") == "text":
                        print(f"  {TEAL}›{RST} {blk.get('text', '')}")
                print()
        elif role == "tool":
            lines = str(content or "").strip().splitlines()
            print(f"    {MINT}✓ {SLATE}{(lines[0][:80] if lines else 'Completed')}{RST}")
        elif role == "assistant":
            blocks = content if isinstance(content, list) else []
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function", {}) if isinstance(tc, dict) else {}
                print(f"  {MINT}⚡ Tool:{RST} {GOLD}{BOLD}{fn.get('name', 'tool')}{RST}")
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    inp = blk.get("input", {}) or {}
                    arg = inp.get("command") or inp.get("path") or inp.get("pattern") or inp.get("query") or "" if isinstance(inp, dict) else ""
                    print(f"  {MINT}⚡ Tool:{RST} {GOLD}{BOLD}{blk.get('name', 'tool')}{RST} {WHITE}{arg}{RST}")
                elif blk.get("type") == "text" and str(blk.get("text", "")).strip():
                    print(f"{format_markdown(str(blk['text']).strip(), max_width=width)}\n")
            if isinstance(content, str) and content.strip():
                print(f"{format_markdown(content.strip(), max_width=width)}\n")

    if ledger is not None:
        reason_tok = int(getattr(ledger, "total_reasoning_tokens", 0) or 0)
        reason_info = f" · reasoning: {reason_tok:,}" if reason_tok > 0 else ""
        usage_txt = f"{WHITE}{BOLD}{tot_tok:,}{RST} {SLATE}tokens (in: {in_tok:,} · out: {out_tok:,}{reason_info}){RST}" if tot_tok else f"{SLATE}no usage recorded{RST}"
        print(f"  {DARK_SLATE}├── {TEAL}{BOLD}Session usage:{RST} {usage_txt} {DARK_SLATE}·{RST} {GOLD}{BOLD}Total Cost: ${cost_val:.5f}{RST}")
    print(f"  {DARK_SLATE}╰──{'─' * max(2, width - 4)}╯{RST}\n")


def pick_session(recent: list[Any], active_id: str = "") -> str | None:
    """Searchable session picker (type to filter by title, id, tag or model). Non-TTY: numbered menu."""
    from axon import platform as plat
    if not plat.is_interactive():
        labels = {f"{m.session_id}  · {m.first_prompt[:40]}": m.session_id for m in recent}
        sel = pick(list(labels), title="Select Session to Resume")
        return labels.get(sel) if sel else None
    from axon.ui.picker import SelectItem, Selector
    items = []
    for m in recent:
        star = f"{GOLD}★{RST}" if getattr(m, "is_starred", False) else " "
        tags = " ".join(f"#{t}" for t in getattr(m, "tags", []) or [])
        hint = f"{_fmt_ts(m.created_at)} · {m.message_count} msgs · ${float(getattr(m, 'total_cost', 0.0)):.4f}"
        items.append(SelectItem(label=(m.first_prompt or m.session_id)[:70], value=m.session_id, hint=hint, badge=star,
                                search=f"{m.session_id} {tags} {m.model}"))
    chosen = Selector(items, title="Resume session", subtitle=f"{len(recent)} saved · type to search",
                      filterable=True, placeholder="search title, id, #tag or model", max_visible=12).run()
    value = getattr(chosen, "value", None)
    return str(value) if value else None


def handle_resume(agent: Agent, session_id: str = "") -> None:
    store = agent.session
    recent = store.list_recent(limit=None)
    if not recent:
        print(f"\n  {SLATE}No past sessions found to resume.{RST}\n")
        return

    ids = [m.session_id for m in recent]
    arg = session_id.strip()
    chosen: str | None
    if arg:
        chosen, candidates = _match_session(ids, arg)
        if not chosen and candidates:
            print(f"\n  {GOLD}'{arg}' matches {len(candidates)} sessions - be more specific.{RST}\n")
            return
        if not chosen:
            print(f"\n  {ROSE}✗ Session not found: {arg}{RST}\n")
            return
    else:
        chosen = pick_session(recent, store.active_session_id)

    if not chosen:
        print(f"\n  {SLATE}(Session unchanged: {store.active_session_id}){RST}\n")
        return
    agent.conversation = store.load(chosen)
    agent.ledger = store.load_ledger(chosen, agent.settings.model)
    cp = getattr(agent, "checkpoints", None)
    if cp is not None and hasattr(cp, "set_session"):
        try:
            cp.set_session(chosen)  # /rewind keeps working on the resumed session's file snapshots
        except Exception:
            pass
    try:
        from axon.agent.subagent import sync_subagents_for_session
        sync_subagents_for_session(agent)
    except Exception:
        pass
    render_restored_conversation(agent.conversation, chosen, ledger=agent.ledger)


def handle_branch(agent: Agent, name: str = "") -> None:
    """Fork the current conversation into a new session (the original is untouched)."""
    store = agent.session
    old_id = store.active_session_id
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-")[:40]
    if clean:
        new_id = f"{old_id.split('_sub_')[0]}_branch_{clean}"
    else:
        new_id = f"{old_id}_branch_{int(datetime.datetime.now().timestamp())}"
    n = 2
    base = new_id
    while (store.session_dir / f"{new_id}.jsonl").exists():
        new_id = f"{base}-{n}"
        n += 1
    store.open(new_id)
    for m in agent.conversation.messages:
        role = m.get("role")
        if role == "user":
            store.append("user_message", {"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            store.append("assistant_turn", {"native": dict(m), "text": "", "tool_uses": [], "usage": None})
        elif role == "tool":
            store.append("tool_results", [dict(m)])
    if clean:
        store.rename_session(name.strip())
    print(f"\n  {TEAL}✓ Branched into {BOLD}{new_id}{RST}{TEAL} · original {old_id} preserved.{RST}\n")
