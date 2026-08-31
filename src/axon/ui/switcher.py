"""
Interactive Session & Chat Switcher Dashboard (matching Claude Code UI).
100% flicker-free in-place terminal redraws with arrow navigation.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ModuleNotFoundError:
    termios = None  # type: ignore
    tty = None      # type: ignore
    _HAS_TERMIOS = False

from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, MINT, RST, ROSE, SLATE, TEAL, UNDER, WHITE,
    strip_ansi, term_width,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent

def format_time_ago(ts: float) -> str:
    """Format timestamp into human-readable relative string (24s, 3m, 2h, 11d)."""
    diff = max(0, time.time() - ts)
    if diff < 60:
        return f"{int(diff)}s"
    elif diff < 3600:
        return f"{int(diff // 60)}m"
    elif diff < 86400:
        return f"{int(diff // 3600)}h"
    else:
        return f"{int(diff // 86400)}d"

@dataclass
class DashboardSession:
    id: str
    title: str
    last_message: str
    status: str  # "needs_input", "working", "completed"
    updated_at: float
    is_current: bool
    path: Path
    total_tokens: int = 0
    message_count: int = 0

def load_dashboard_sessions(workspace: Path, active_id: str, limit: int | None = None, session_dir: Path | None = None) -> list[DashboardSession]:
    """Scan all session JSONL files and subagents to build structured session list."""
    if session_dir is not None:
        target_session_dir = session_dir
    elif str(workspace).startswith(("/tmp", "/var/folders", "/private/var")):
        target_session_dir = workspace.parent / ".global_axon" / "sessions"
    else:
        target_session_dir = Path.home() / ".axon" / "sessions"

    target_session_dir.mkdir(parents=True, exist_ok=True)
    all_files = sorted(target_session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Exclude sub-agent sessions (_sub_*) — they are only viewable via /subagents
    files = [f for f in all_files if "_sub_" not in f.stem]

    sessions: list[DashboardSession] = []
    target_files = files[:limit] if limit is not None else files
    
    for f in target_files:
        sid = f.stem
        is_curr = (sid == active_id)
        mtime = f.stat().st_mtime
        title = ""
        last_msg = ""
        user_prompts: list[str] = []
        assistant_texts: list[str] = []
        total_tokens = 0
        line_count = 0
        
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as s_file:
                for line in s_file:
                    if not line.strip():
                        continue
                    line_count += 1
                    try:
                        entry = json.loads(line)
                        t = entry.get("type")
                        data = entry.get("data", {})
                        if t == "user_message":
                            txt = data.get("content", "")
                            if isinstance(txt, str) and txt.strip():
                                user_prompts.append(txt.strip())
                                last_msg = txt.splitlines()[-1].strip()[:65]
                            elif isinstance(txt, list):
                                for blk in txt:
                                    if isinstance(blk, dict) and blk.get("text"):
                                        user_prompts.append(blk["text"].strip())
                                        last_msg = blk["text"].splitlines()[-1].strip()[:65]
                        elif t == "assistant_turn":
                            txt = data.get("text", "")
                            if txt and isinstance(txt, str):
                                assistant_texts.append(txt.strip())
                                last_msg = txt.splitlines()[0].strip()[:65]
                            u = data.get("usage")
                            if u and isinstance(u, dict):
                                total_tokens += int(u.get("input", 0)) + int(u.get("output", 0))
                    except Exception:
                        pass
        except Exception:
            pass

        # Generate intelligent, meaningful title
        if user_prompts:
            # Pick first substantial user prompt
            for p in user_prompts:
                p_clean = p.splitlines()[0].strip()
                if len(p_clean) > 3 and p_clean.lower() not in ("hi", "hello", "hey", "test"):
                    title = p_clean[:32]
                    break
            if not title:
                title = user_prompts[0].splitlines()[0].strip()[:32]
        elif assistant_texts:
            title = assistant_texts[0].splitlines()[0].strip()[:32]

        if not title:
            title = sid

        if is_curr and not last_msg:
            last_msg = "Active workspace session"

        # Determine status
        status = "needs_input"
        if "completed" in last_msg.lower() or "done" in last_msg.lower() or "binary search" in title.lower():
            status = "completed"

        sessions.append(
            DashboardSession(
                id=sid,
                title=title,
                last_message=last_msg or "Ready for prompt",
                status=status,
                updated_at=mtime,
                is_current=is_curr,
                path=f,
                total_tokens=total_tokens,
                message_count=line_count,
            )
        )

    return sessions

def run_session_dashboard(agent: Agent) -> str | None:
    """
    Renders interactive 100% flicker-free session switcher matching Claude Code UI.
    Returns target session_id to switch to, or a new user prompt string, or None if cancelled.
    """
    if not sys.stdin.isatty():
        return None

    workspace = agent.settings.workspace
    active_id = agent.session.active_session_id
    raw_sessions = load_dashboard_sessions(workspace, active_id)

    # Determine last message and title for current session from memory or disk
    curr_found = next((s for s in raw_sessions if s.id == active_id), None)
    last_m = "Active workspace session"
    curr_title = curr_found.title if (curr_found and curr_found.title) else "New Session"
    if agent.conversation.messages:
        for m in reversed(agent.conversation.messages):
            c = m.get("content", "")
            if isinstance(c, str) and c.strip():
                last_m = c.splitlines()[-1].strip()[:65]
                break
        for m in agent.conversation.messages:
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str) and c.strip():
                    p_clean = c.splitlines()[0].strip()
                    if len(p_clean) > 3 and p_clean.lower() not in ("hi", "hello", "hey", "test"):
                        curr_title = p_clean[:32]
                        break
                    elif curr_title == "New Session":
                        curr_title = p_clean[:32]
    elif curr_found and curr_found.last_message:
        last_m = curr_found.last_message

    mem_tokens = agent.ledger.total_input_tokens + agent.ledger.total_output_tokens
    curr_tokens = max(curr_found.total_tokens if curr_found else 0, mem_tokens)
    curr_msg_count = max(curr_found.message_count if curr_found else 0, len(agent.conversation.messages))

    curr_entry = DashboardSession(
        id=active_id,
        title=curr_title,
        last_message=last_m,
        status="needs_input",
        updated_at=time.time(),
        is_current=True,
        path=curr_found.path if curr_found else agent.session.active_file,
        total_tokens=curr_tokens,
        message_count=curr_msg_count,
    )

    # Guarantee current session is strictly at top (index 0)
    sessions = [curr_entry] + [s for s in raw_sessions if s.id != active_id]

    selected_idx = 0
    buffer: list[str] = []
    cursor_pos = 0

    # All sessions list
    flat_list = sessions

    rendered_lines = 0

    if not sys.stdin.isatty() or not _HAS_TERMIOS or termios is None:
        return None

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)

    # Clean screen for fixed full-viewport dashboard
    if sys.stdin.isatty():
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()


    def draw(initial: bool = False):
        nonlocal rendered_lines
        tw = term_width()
        width = max(50, tw - 4)
        
        lines_out: list[str] = []
        
        # Clean title banner
        lines_out.append("")
        lines_out.append(f"  {TEAL}▲{CYAN}█{MINT}▲  {BOLD}{WHITE}Previous Chats{RST} {SLATE}· {len(flat_list)} chats · ↑/↓ navigate · Enter open · Esc return{RST}")
        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")

        # Render scrollable list of all previous chats
        max_visible = 12
        start_v = max(0, min(selected_idx - max_visible // 2, max(0, len(flat_list) - max_visible)))
        end_v = min(len(flat_list), start_v + max_visible)

        for idx in range(start_v, end_v):
            s = flat_list[idx]
            is_sel = (idx == selected_idx)
            cursor_mark = f"{MINT}▶{RST}" if is_sel else " "
            clean_title = " ".join(s.title.split())[:22]

            if s.is_current:
                star = f"{MINT}●{RST}"
                t_str = f"{MINT}{BOLD}{clean_title:<22}{RST}" if not is_sel else f"{MINT}{BOLD}{UNDER}{clean_title:<22}{RST}"
            else:
                star = f"{GOLD}✱{RST}"
                t_str = f"{WHITE}{clean_title:<22}{RST}" if not is_sel else f"{WHITE}{BOLD}{UNDER}{clean_title:<22}{RST}"

            tok_part = f" ({s.total_tokens:,} tok)" if s.total_tokens > 0 else ""
            time_str = f"current · {format_time_ago(s.updated_at)}{tok_part}" if s.is_current else f"{format_time_ago(s.updated_at)}{tok_part}"
            max_msg = max(8, width - 38 - len(strip_ansi(time_str)))
            msg_preview = " ".join(s.last_message.split())[:max_msg]
            pad = max(2, width - 30 - len(msg_preview) - len(strip_ansi(time_str)))

            if is_sel:
                line_str = f"  {cursor_mark} {star} {t_str} {SLATE}{msg_preview}{' ' * pad}{SLATE}{time_str}{RST}"
            else:
                line_str = f"    {star} {t_str} {SLATE}{msg_preview}{' ' * pad}{SLATE}{time_str}{RST}"
            lines_out.append(line_str)

        lines_out.append(f"  {DARK_SLATE}{'─' * width}{RST}")
        if len(flat_list) > max_visible:
            lines_out.append(f"  {SLATE}(Showing {start_v + 1}–{end_v} of {len(flat_list)} chats · scroll with ↑/↓){RST}")

        # New chat option at the bottom
        is_new_sel = (selected_idx == len(flat_list))
        cursor_new = f"{MINT}▶{RST}" if is_new_sel else " "
        buf_str = "".join(buffer)
        if buf_str:
            t_new = f"{CYAN}{BOLD}➕ Start a new chat:{RST} {WHITE}{BOLD}{buf_str}{RST}"
            desc_new = f"{MINT}↵ Send prompt{RST}"
        else:
            t_new = f"{CYAN}{BOLD}➕ Start a new chat{RST}" if is_new_sel else f"{SLATE}➕ Start a new chat{RST}"
            desc_new = f"{DARK_SLATE}↵ Open fresh session{RST}"

        pad_new = max(2, width - len(strip_ansi(t_new)) - len(strip_ansi(desc_new)) - 6)
        if is_new_sel:
            lines_out.append(f"  {cursor_new} {t_new}{' ' * pad_new}{desc_new}")
        else:
            lines_out.append(f"    {t_new}{' ' * pad_new}{desc_new}")

        # Absolute origin fixed terminal write
        sys.stdout.write("\033[H")
        output_str = "\n".join([f"\033[2K\r{l}" for l in lines_out])
        sys.stdout.write(output_str + "\n\033[J")
        sys.stdout.flush()
        rendered_lines = len(lines_out)

    try:
        tty.setcbreak(fd)
        draw(initial=True)
        
        while True:
            raw_bytes = os.read(fd, 1024)
            if not raw_bytes:
                break

            total_items = len(flat_list) + 1

            # Esc -> Return back to active session
            if raw_bytes == b"\x1b":
                return None

            # Ctrl+C -> Quit dashboard
            if raw_bytes == b"\x03":
                return None

            # Down Arrow
            if raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                selected_idx = (selected_idx + 1) % total_items
                draw()
                continue

            # Up Arrow
            if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                selected_idx = (selected_idx - 1) % total_items
                draw()
                continue

            # Left Arrow
            if raw_bytes.startswith((b"\x1b[D", b"\x1bOD")):
                if buffer:
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        draw()
                else:
                    # Return back to active session cleanly
                    return None
                continue

            # Right Arrow
            if raw_bytes.startswith((b"\x1b[C", b"\x1bOC")):
                if buffer:
                    if cursor_pos < len(buffer):
                        cursor_pos += 1
                        draw()
                else:
                    # Return back to active session cleanly
                    return None
                continue

            # Page Up
            if raw_bytes.startswith(b"\x1b[5~"):
                selected_idx = max(0, selected_idx - 10)
                draw()
                continue

            # Page Down
            if raw_bytes.startswith(b"\x1b[6~"):
                selected_idx = min(total_items - 1, selected_idx + 10)
                draw()
                continue

            # Home key
            if raw_bytes in (b"\x1b[H", b"\x1b[1~", b"\x01", b"\x1bOH"):
                if buffer:
                    cursor_pos = 0
                else:
                    selected_idx = 0
                draw()
                continue

            # End key
            if raw_bytes in (b"\x1b[F", b"\x1b[4~", b"\x05", b"\x1bOF"):
                if buffer:
                    cursor_pos = len(buffer)
                else:
                    selected_idx = total_items - 1
                draw()
                continue

            # Delete key
            if raw_bytes in (b"\x1b[3~", b"\x1b[3;5~"):
                if cursor_pos < len(buffer):
                    buffer.pop(cursor_pos)
                    draw()
                continue

            # Ctrl+U (Clear input)
            if raw_bytes == b"\x15":
                buffer.clear()
                cursor_pos = 0
                draw()
                continue

            # Enter Key
            if raw_bytes in (b"\r", b"\n", b"\r\n"):
                buf_str = "".join(buffer).strip()
                if selected_idx == len(flat_list) or buf_str:
                    return f"__NEW_SESSION__:{buf_str}"
                elif selected_idx < len(flat_list):
                    chosen = flat_list[selected_idx]
                    if chosen.is_current or chosen.id == active_id:
                        return None
                    return chosen.id
                return None

            # Space Key when buffer is empty -> Select & Open
            if raw_bytes == b" " and not buffer:
                if selected_idx == len(flat_list):
                    return "__NEW_SESSION__:"
                elif selected_idx < len(flat_list):
                    chosen = flat_list[selected_idx]
                    if chosen.is_current or chosen.id == active_id:
                        return None
                    return chosen.id

            # Ctrl+X or 'd' (Delete session) when buffer is empty
            if (raw_bytes in (b"\x18",) or (raw_bytes == b"d" and not buffer)) and flat_list:
                if selected_idx < len(flat_list):
                    chosen = flat_list[selected_idx]
                    if not chosen.is_current and chosen.path.exists():
                        try:
                            chosen.path.unlink()
                        except Exception:
                            pass
                        raw_s = load_dashboard_sessions(workspace, active_id)
                        curr_entry = next((s for s in raw_s if s.id == active_id), None)
                        sessions = ([curr_entry] if curr_entry else []) + [s for s in raw_s if s.id != active_id]
                        flat_list = sessions
                        selected_idx = min(selected_idx, len(flat_list))
                        draw()
                continue

            # Backspace
            if raw_bytes in (b"\x7f", b"\x08"):
                if cursor_pos > 0:
                    buffer.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    draw()
                continue

            # Skip any unhandled escape sequences to prevent noise leaking into input buffer
            if raw_bytes.startswith(b"\x1b"):
                continue

            # Printable characters
            try:
                decoded = raw_bytes.decode("utf-8")
                for ch in decoded:
                    if ord(ch) >= 32:
                        buffer.insert(cursor_pos, ch)
                        cursor_pos += 1
                        selected_idx = len(flat_list)
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

            # Cleanly clear the dashboard lines so the terminal is crisp
            sys.stdout.write(f"\033[{rendered_lines}A\r")
            for _ in range(rendered_lines):
                sys.stdout.write("\033[2K\n")
            sys.stdout.write(f"\033[{rendered_lines}A\r")
            sys.stdout.flush()

    return None
