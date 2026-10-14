"""
Interactive session switcher (Left arrow on an empty prompt, or /sessions).

Built on the shared Selector; session data comes from axon.session.store.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from axon import platform as plat
from axon.session.store import DashboardSession, clean_session_topic, load_dashboard_sessions
from axon.ui.picker import SelectItem, Selector
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, RST, SLATE, WHITE

if TYPE_CHECKING:
    from axon.agent.loop import Agent

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag
_clean_session_topic = clean_session_topic
NEW_SESSION_PREFIX = "__NEW_SESSION__:"

__all__ = ["DashboardSession", "categorize_session_time", "format_time_ago", "load_dashboard_sessions",
           "run_session_dashboard", "_clean_session_topic"]


def format_time_ago(ts: float) -> str:
    """Compact relative time: 24s, 3m, 2h, 11d."""
    diff = max(0, time.time() - ts)
    if diff < 60:
        return f"{int(diff)}s"
    if diff < 3600:
        return f"{int(diff // 60)}m"
    if diff < 86400:
        return f"{int(diff // 3600)}h"
    return f"{int(diff // 86400)}d"


def categorize_session_time(ts: float) -> str:
    """Today, Yesterday, Previous 7 Days or Older."""
    diff_days = (datetime.now().date() - datetime.fromtimestamp(ts).date()).days
    if diff_days <= 0:
        return "Today"
    if diff_days == 1:
        return "Yesterday"
    if diff_days <= 7:
        return "Previous 7 Days"
    return "Older"


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _current_entry(agent: Agent, found: DashboardSession | None) -> DashboardSession:
    """Merge on-disk data for the active session with live in-memory state."""
    messages = list(getattr(getattr(agent, "conversation", None), "messages", []) or [])
    title = found.title if found and found.title else ""
    if not title or title.startswith("New Session"):
        for m in messages:
            c = m.get("content", "") if isinstance(m, dict) else ""
            if m.get("role") == "user" and isinstance(c, str) and c.strip():
                title = clean_session_topic(c)[:32] or title
                break
    last = found.last_message if found else ""
    for m in reversed(messages):
        c = m.get("content", "") if isinstance(m, dict) else ""
        if isinstance(c, str) and c.strip():
            last = c.strip().splitlines()[-1][:80]
            break
    ledger = getattr(agent, "ledger", None)
    mem_tokens = int(getattr(ledger, "total_input_tokens", 0) or 0) + int(getattr(ledger, "total_output_tokens", 0) or 0)
    try:
        cost = float(agent.get_combined_cost()) if hasattr(agent, "get_combined_cost") else float(ledger.total())
    except Exception:
        cost = 0.0
    return DashboardSession(
        id=agent.session.active_session_id,
        title=title or "New Session",
        last_message=last or "Active session",
        status="needs_input",
        updated_at=time.time(),
        is_current=True,
        path=found.path if found else agent.session.active_file,
        total_tokens=max(found.total_tokens if found else 0, mem_tokens),
        message_count=max(found.message_count if found else 0, len(messages)),
        tags=list(found.tags) if found else [],
        is_starred=found.is_starred if found else False,
        total_cost=max(found.total_cost if found else 0.0, cost),
    )


def _session_row(s: DashboardSession) -> Any:
    def render(selected: bool, width: int) -> str:
        star = f"{GOLD}★{RST}" if s.is_starred else f"{DARK_SLATE}☆{RST}"
        dot = f"{MINT}●{RST}" if s.is_current else " "
        title = " ".join(s.title.split())
        title_s = plat.pad_to(plat.truncate_ansi(title, 26), 26)
        title_s = f"{BOLD}{WHITE}{title_s}{RST}" if selected else (f"{MINT}{title_s}{RST}" if s.is_current else f"{WHITE}{title_s}{RST}")
        tag = f"{CYAN}#{s.tags[0][:10]}{RST}" if s.tags else ""
        tag_s = plat.pad_to(tag, 12)
        cost = f"{GOLD}${s.total_cost:.4f}{RST}" if s.total_cost > 0 else f"{DARK_SLATE}$0.0000{RST}"
        meta = f"{cost} {DARK_SLATE}·{RST} {SLATE}{_fmt_tokens(s.total_tokens)} tok · {format_time_ago(s.updated_at)}{RST}"
        left = f"{dot} {star} {title_s} {tag_s} "
        room = width - plat.str_width(left) - plat.str_width(meta) - 2
        preview = plat.truncate_ansi(" ".join(s.last_message.split()), max(0, room)) if room > 8 else ""
        gap = max(1, width - plat.str_width(left) - plat.str_width(preview) - plat.str_width(meta))
        return f"{left}{SLATE}{preview}{RST}{' ' * gap}{meta}"
    return render


def run_session_dashboard(agent: Agent) -> str | None:
    """Show the switcher. Returns a session id to switch to, "__NEW_SESSION__:<prompt>" to start
    a new chat (optionally with a first prompt), or None to stay in the current session."""
    if not plat.is_interactive():
        return None

    workspace = agent.settings.workspace
    state: dict[str, Any] = {"active": agent.session.active_session_id, "buffer": ""}

    def load() -> list[DashboardSession]:
        raw = load_dashboard_sessions(workspace, state["active"])
        found = next((s for s in raw if s.id == state["active"]), None)
        return [_current_entry(agent, found)] + [s for s in raw if s.id != state["active"]]

    sessions = load()

    def build_items() -> list[SelectItem]:
        items = [SelectItem(label=s.title, value=s, group=categorize_session_time(s.updated_at), render=_session_row(s))
                 for s in sessions]

        def render_new(selected: bool, width: int) -> str:
            buf = state["buffer"]
            if buf:
                return f"{CYAN}{BOLD}＋ New chat:{RST} {WHITE}{buf}{RST}{CYAN}▏{RST}"
            label = "＋ Start a new chat" + ("  (type a first prompt)" if selected else "")
            return f"{CYAN}{BOLD}{label}{RST}" if selected else f"{SLATE}{label}{RST}"
        items.append(SelectItem(label="new", value="__new__", group=" ", render=render_new))
        return items

    def subtitle() -> str:
        total = sum(s.total_cost for s in sessions)
        toks = sum(s.total_tokens for s in sessions)
        return f"{len(sessions)} chats · ${total:.4f} · {_fmt_tokens(toks)} tok"

    def refresh(sel: Selector) -> None:
        nonlocal sessions
        sessions = load()
        sel.subtitle = subtitle()
        sel.set_items(build_items())

    def on_key(sel: Selector, key: str) -> bool:
        cur = sel.current()
        on_new = cur is not None and cur.value == "__new__"
        session: DashboardSession | None = cur.value if (cur is not None and not on_new) else None
        if on_new:
            if key == "enter":
                sel.finish(NEW_SESSION_PREFIX + state["buffer"].strip())
                return True
            if key == "backspace":
                state["buffer"] = state["buffer"][:-1]
                return True
            if key.startswith(plat.PASTE_PREFIX):
                state["buffer"] += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
                return True
            if len(key) == 1 and key >= " ":
                state["buffer"] += key
                return True
            if key == "esc" and state["buffer"]:
                state["buffer"] = ""
                return True
            return False
        if key == "enter":
            if session is not None:
                sel.finish(None if session.is_current else session.id)
            return True
        if key in ("left", "esc"):
            sel.finish(None)
            return True
        if key in ("n", "/", "end"):
            sel.index = len(sel.items) - 1
            return True
        if session is None:
            return False
        if key in ("s", "*"):
            starred = agent.session.star_session(session.id)
            session.is_starred = bool(starred)
            sel.flash(f"★ {'Starred' if starred else 'Unstarred'} “{session.title}”", GOLD)
            return True
        if key == "r":
            new_title = sel.prompt_line(f"{GOLD}Rename “{session.title}” to:{RST} ")
            if new_title:
                agent.session.rename_session(new_title, session.id)
                session.title = new_title
                sel.flash(f"✓ Renamed to “{new_title}”")
            return True
        if key == "t":
            tag = sel.prompt_line(f"{CYAN}Tag (empty removes):{RST} ", default=session.tags[0] if session.tags else "",
                                  allow_empty=True)
            if tag is not None:
                clean = tag.strip().lstrip("#")
                agent.session.tag_session(clean if clean and clean != "-" else "", session.id)
                session.tags = [clean] if clean and clean != "-" else []
                sel.flash(f"✓ Tag set to #{clean}" if session.tags else "✓ Tag removed")
            return True
        if key in ("d", "delete"):
            if sel.confirm(f"Delete chat “{session.title}” permanently?"):
                agent.session.delete_session(session.id)
                state["active"] = agent.session.active_session_id
                refresh(sel)
                sel.flash("✓ Chat deleted")
            return True
        if key == "D":
            if sel.confirm(f"Delete ALL {len(sessions)} chats permanently?"):
                count = agent.session.delete_all_sessions()
                state["active"] = agent.session.active_session_id
                refresh(sel)
                sel.flash(f"✓ Deleted {count} chats")
            return True
        return False

    sel = Selector(
        build_items(),
        title="Previous chats",
        subtitle=subtitle(),
        fullscreen=True,
        max_visible=14,
        on_key=on_key,
        footer="↑↓ move · enter open · s star · r rename · t tag · d delete · D delete all · n new chat · esc back",
    )
    result = sel.run()
    if isinstance(result, str) or result is None:
        return result
    return None

