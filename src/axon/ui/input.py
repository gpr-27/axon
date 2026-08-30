"""
Interactive input reading with raw OS terminal key-handling, live Tab/Shift+Tab mode toggling,
SIGWINCH auto-resize, history, and interactive scrollable slash-command autocomplete popup.
"""
from __future__ import annotations
import os
import readline
import signal
import subprocess
import sys
import termios
import time
import tty
from typing import Any
from axon.ui.theme import (
    AMBER,
    BOLD,
    CYAN,
    DARK_SLATE,
    DIM,
    GOLD,
    MINT,
    PURPLE,
    RST,
    SLATE,
    TEAL,
    UNDER,
    WHITE,
    strip_ansi,
    term_height,
    term_width,
)

MODES_CYCLE = ["default", "acceptEdits", "plan", "bypass"]

# In-memory history across REPL turns in the session
_SESSION_HISTORY: list[str] = []

import re

def safe_ansi_truncate(text: str, max_w: int) -> str:
    """Truncates visible width of string to max_w without corrupting ANSI codes."""
    if len(strip_ansi(text)) <= max_w:
        return text
    tokens = re.split(r"(\x1b\[[0-9;]*[a-zA-Z]|\x1b\]8;;.*?(?:\x1b\\|\x07))", text)
    res: list[str] = []
    vis_count = 0
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("\x1b"):
            res.append(tok)
        else:
            remaining = max_w - vis_count
            if remaining <= 0:
                break
            if len(tok) <= remaining:
                res.append(tok)
                vis_count += len(tok)
            else:
                res.append(tok[:remaining])
                vis_count += remaining
                break
    return "".join(res) + RST

ALL_SLASH_COMMANDS: list[tuple[str, str, str, str, bool]] = [
    # ⚙️ Core Configuration & Models
    ("/model", "core", "Core & Models", "Switch active LLM (Claude, GPT, DeepSeek, GLM)", True),
    ("/effort", "core", "Core & Models", "Adjust neural reasoning tier (reflex, balanced, synapse, quantum)", True),
    ("/config", "core", "Core & Models", "View and adjust runtime configuration parameters", True),
    ("/status", "core", "Core & Models", "View comprehensive live system and agent status", False),
    ("/permissions", "core", "Core & Models", "Inspect active permission engine rules and mode", False),
    ("/plan", "core", "Core & Models", "View task checklist or switch to plan mode", True),

    # 📊 Context & Token Budget
    ("/context", "core", "Context & Tokens", "View active context token budget and limits", False),
    ("/compact", "core", "Context & Tokens", "Compact conversation context while preserving key facts", False),
    ("/window", "core", "Context & Tokens", "Adjust sliding context window size (e.g. /window 10)", True),
    ("/cost", "core", "Context & Tokens", "View session billing ledger and prompt cache hits", False),
    ("/payload", "core", "Context & Tokens", "Inspect exact prompt payload and tool results", True),

    # 📜 History & File Revisions
    ("/history", "core", "History & Diff", "View all messages in full conversation history", False),
    ("/diff", "core", "History & Diff", "View working tree uncommitted git diff", False),
    ("/review", "core", "History & Diff", "Run automated multi-file code review", True),
    ("/rewind", "core", "History & Diff", "Revert file edits made during previous turns", False),
    ("/expand", "core", "History & Diff", "View full un-truncated output or expand any file", True),

    # 🤖 Multi-Agent, Tasks & Skills
    ("/subagents", "core", "Multi-Agent", "Axon subagent matrix & isolated worker transcripts", False),
    ("/todos", "core", "Multi-Agent", "View active multi-step task checklist", False),
    ("/queue", "core", "Multi-Agent", "Add/manage sequential prompt queue (/queue <text>)", True),
    ("/q", "core", "Multi-Agent", "Quick alias for message queue (/q <text>, /q drop, /q clear)", True),
    ("/skills", "core", "Multi-Agent", "Browse active skills studio and create new skills", True),
    ("/mcp", "ext", "Multi-Agent", "Inspect Model Context Protocol servers and tools", False),
    ("/plugin", "ext", "Multi-Agent", "Inspect installed plugins and extension manifests", False),
    ("/hooks", "ext", "Multi-Agent", "Inspect active lifecycle and execution hooks", False),
    ("/memory", "core", "Multi-Agent", "Inspect persistent workspace memory store", False),
    ("/learn", "core", "Multi-Agent", "Teach a new convention to persistent memory", True),

    # 📁 Sessions & Workspace
    ("/main", "core", "Sessions & Root", "Switch back to the Main Agent / parent chart session", False),
    ("/root", "core", "Sessions & Root", "Alias for /main to return to root chart session", False),
    ("/sessions", "core", "Sessions & Root", "Axon session matrix timeline dashboard", False),
    ("/resume", "core", "Sessions & Root", "Resume previous session from transcript", True),
    ("/branch", "core", "Sessions & Root", "Fork current conversation into an independent branch", True),
    ("/tools", "core", "Sessions & Root", "List all 24 active agent tools, schemas, and permissions", False),
    ("/doctor", "core", "Sessions & Root", "Run local diagnostics & environment health check", False),
    ("/init", "ext", "Sessions & Root", "Initialize AGENTS.md conventions file in workspace", False),

    # ⌨️ Session Control & Input
    ("/ask", "core", "Session Control", "Ask simultaneous side question in isolated context", True),
    ("/kb", "core", "Session Control", "Keybindings and shortcuts cheat sheet", False),
    ("/help", "core", "Session Control", "Show categorized command reference and help", False),
    ("/clear", "core", "Session Control", "Clear active conversation and start fresh session", False),
    ("/exit", "core", "Session Control", "Save and exit current session", False),
]

def _get_mode_info(mode: str) -> tuple[str, str]:
    return {
        "default": ("manual mode on (Tab to change)", SLATE),
        "acceptEdits": ("auto-accept edits on (Tab to change)", TEAL),
        "plan": ("plan mode on (Tab to change)", PURPLE),
        "bypass": ("bypass permissions on (Tab to change)", GOLD),
    }.get(mode, (f"{mode} mode (Tab to change)", SLATE))

def read_input(
    mode: str = "default",
    effort: str = "high",
    prompt: str = "",
    subagents: list[Any] | None = None,
    active_idx: int = 0,
    plan_summary: str | None = None,
    queue_summary: str | None = None,
    subagent_label: str | None = None,
    **kwargs: Any,
) -> tuple[str, str | None, int]:
    """
    Read user input with live Tab/Shift+Tab mode switching, dynamic auto-resize,
    arrow history, clean main view, live plan progress, message queue,
    and interactive scrollable slash-command autocomplete menu.
    Returns (user_text, new_mode_if_toggled, 0).
    """
    if not sys.stdin.isatty():
        width = max(40, term_width() - 4)
        label_text, mode_color = _get_mode_info(mode)
        plan_badge = f"  {MINT}{BOLD}{plan_summary}{RST}" if plan_summary else ""
        queue_badge = f"  {CYAN}{BOLD}{queue_summary}{RST}" if queue_summary else ""
        agent_hint = f" · {DARK_SLATE}/main to return{RST}" if subagent_label else ""
        left_str = f"▮ {label_text}{plan_badge}{queue_badge}{agent_hint}"
        right_str = f"○ {effort} · /effort · ⌨ /kb · ? shortcuts"
        pad = max(2, width - len(strip_ansi(left_str)) - len(strip_ansi(right_str)))
        sys.stdout.write(f"  {DARK_SLATE}{'─' * width}{RST}\n")
        sys.stdout.write(f"  {mode_color}{BOLD}{left_str}{RST}{' ' * pad}{SLATE}{right_str}{RST}\n")
        sys.stdout.write(f"  {DARK_SLATE}{'─' * width}{RST}\n")
        sys.stdout.flush()
        p_tag = f"  {CYAN}[{subagent_label}]{RST} {BOLD}{WHITE}›{RST} " if subagent_label else f"  {BOLD}{WHITE}›{RST} "
        line = input(p_tag)
        return (line.strip(), None, 0)

    current_mode = mode
    current_subagent_idx = 0
    buffer: list[str] = []
    cursor_pos = 0

    # Build history from session store + readline
    history: list[str] = list(_SESSION_HISTORY)
    num_hist = readline.get_current_history_length()
    for i in range(1, num_hist + 1):
        item = readline.get_history_item(i)
        if item and item not in history:
            history.append(item)
    history_idx = len(history)

    saved_draft = ""
    last_prompt_lines = 1
    last_popup_lines = 0
    last_rendered_lines = 0
    last_prompt_row = 0

    # Slash command autocomplete state
    selected_cmd_idx = 0
    cmd_popup_open = True

    # File @ autocomplete state
    selected_file_idx = 0
    file_popup_open = True

    def get_matching_commands(prefix: str) -> list[tuple[str, str, str, str, bool]]:
        if not prefix.startswith("/"):
            return []
        p_clean = prefix.split()[0].lower() if " " in prefix else prefix.lower()
        return [c for c in ALL_SLASH_COMMANDS if c[0].startswith(p_clean)]

    def get_matching_files(query: str) -> list[str]:
        q = query.lower()
        res = []
        cwd = os.getcwd()
        ignore = {".git", "__pycache__", ".axon", ".pytest_cache", "node_modules", ".venv", "venv", ".idea", ".vscode"}
        try:
            for root, dirs, files in os.walk(cwd):
                dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
                for f in sorted(files):
                    if f.startswith("."):
                        continue
                    rel = os.path.relpath(os.path.join(root, f), cwd)
                    if not q or q in rel.lower():
                        res.append(rel)
                        if len(res) >= 8:
                            return res
        except Exception:
            pass
        return res

    def draw(initial: bool = False):
        nonlocal last_prompt_lines, last_popup_lines, last_rendered_lines, last_prompt_row
        tw = term_width()
        width = max(40, tw - 4)
        buf_str = "".join(buffer)
        is_bash = buf_str.startswith("!")

        if is_bash:
            label_text = "bash mode on (Enter to run in shell)"
            mode_color = AMBER
            right_str = f"{AMBER}⚡ direct shell execution{RST} · {SLATE}cd supported{RST}"
            p_prefix = f"{AMBER}{BOLD}!{RST} "
            p_plain = "! "
        else:
            label_text, mode_color = _get_mode_info(current_mode)
            right_str = f"○ {effort} · /effort · ⌨ /kb · ? shortcuts"
            if subagent_label:
                p_prefix = f"{CYAN}[{subagent_label}]{RST} {BOLD}{WHITE}›{RST} "
                p_plain = f"[{subagent_label}] › "
            else:
                p_prefix = f"{BOLD}{WHITE}›{RST} "
                p_plain = "› "

        plan_badge = f"  {MINT}{BOLD}{plan_summary}{RST}" if plan_summary else ""
        queue_badge = f"  {CYAN}{BOLD}{queue_summary}{RST}" if queue_summary else ""

        # Check slash autocomplete
        matching_cmds: list[tuple[str, str, str, str, bool]] = []
        if not is_bash and cmd_popup_open and buf_str.startswith("/") and not (" " in buf_str.strip() and not buf_str.endswith(" ")):
            matching_cmds = get_matching_commands(buf_str)

        # Check file @ autocomplete
        matching_files: list[str] = []
        buf_before_cur = "".join(buffer[:cursor_pos])
        last_at = buf_before_cur.rfind("@")
        if file_popup_open and not matching_cmds and last_at != -1 and (" " not in buf_before_cur[last_at:]):
            at_token = buf_before_cur[last_at + 1:]
            matching_files = get_matching_files(at_token)

        max_visible = 8

        frame_lines: list[str] = []
        frame_lines.append(f"  {DARK_SLATE}{'─' * max(10, width)}{RST}")

        # Prompt input row (horizontally scrolls if buffer exceeds available columns)
        prompt_row_idx = len(frame_lines)
        avail_prompt_w = max(10, tw - len(strip_ansi(p_prefix)) - 4)
        if not buffer:
            placeholder = f"{DARK_SLATE}Ask anything, use /ask for side Qs, @ to link files, /help...{RST}"
            frame_lines.append(f"  {p_prefix}{placeholder}")
            col_in_disp = 0
        elif is_bash:
            cmd_body = buf_str[1:]
            if not cmd_body:
                placeholder = f"{DARK_SLATE}type bash command (e.g. ls, git status, pytest)...{RST}"
                frame_lines.append(f"  {p_prefix}{placeholder}")
                col_in_disp = 0
            else:
                cursor_in_body = max(0, cursor_pos - 1)
                if len(cmd_body) > avail_prompt_w:
                    start_ch = max(0, cursor_in_body - avail_prompt_w + 3)
                    disp_body = cmd_body[start_ch:start_ch + avail_prompt_w]
                    col_in_disp = cursor_in_body - start_ch
                else:
                    disp_body = cmd_body
                    col_in_disp = cursor_in_body
                frame_lines.append(f"  {p_prefix}{WHITE}{BOLD}{disp_body}{RST}")
        else:
            if len(buf_str) > avail_prompt_w:
                start_ch = max(0, cursor_pos - avail_prompt_w + 3)
                disp_body = buf_str[start_ch:start_ch + avail_prompt_w]
                col_in_disp = cursor_pos - start_ch
            else:
                disp_body = buf_str
                col_in_disp = cursor_pos
            frame_lines.append(f"  {p_prefix}{WHITE}{BOLD}{disp_body}{RST}")

        # Status row below prompt (strictly fits within terminal width)
        max_status_w = max(20, tw - 4)
        if subagent_label:
            agent_hint = f" · {DARK_SLATE}/main to return · ← sessions{RST}"
        else:
            agent_hint = f" · {DARK_SLATE}← sessions{RST}"

        left_visible = f"▮ {label_text}{plan_badge}{queue_badge}{agent_hint}"
        left_len = len(strip_ansi(left_visible))
        right_len = len(strip_ansi(right_str))

        if left_len + right_len + 4 <= max_status_w:
            pad = max_status_w - left_len - right_len
            status_line = f"  {mode_color}{BOLD}▮ {label_text}{RST}{plan_badge}{queue_badge}{agent_hint}{' ' * pad}{SLATE}{right_str}{RST}"
        elif left_len + 14 <= max_status_w:
            short_right = f"○ {effort}"
            pad = max(2, max_status_w - left_len - len(strip_ansi(short_right)))
            status_line = f"  {mode_color}{BOLD}▮ {label_text}{RST}{plan_badge}{queue_badge}{agent_hint}{' ' * pad}{SLATE}{short_right}{RST}"
        else:
            status_line = f"  {mode_color}{BOLD}▮ {label_text}{RST}{plan_badge}{queue_badge}{agent_hint}"

        frame_lines.append(status_line)

        # Popups
        if matching_cmds:
            popup_header = f"Commands ({selected_cmd_idx + 1}/{len(matching_cmds)} · {MINT}● Built-in{SLATE} · {GOLD}🔌 Config/Ext{SLATE} · ↑/↓ scroll · Tab fill · Enter run)"
            p_border_w = max(4, width - len(strip_ansi(popup_header)) - 7)
            frame_lines.append(f"  {DARK_SLATE}╭── {SLATE}{popup_header}{DARK_SLATE} {'─' * p_border_w}╮{RST}\033[K")
            start_v = max(0, min(selected_cmd_idx - max_visible // 2, len(matching_cmds) - max_visible))
            end_v = min(len(matching_cmds), start_v + max_visible)
            for idx in range(start_v, end_v):
                c_name, c_kind, c_cat, c_desc, _ = matching_cmds[idx]
                is_sel = (idx == selected_cmd_idx)
                prefix = f"{MINT}▶{RST}" if is_sel else " "
                kind = f"{MINT}●{RST}" if c_kind == "core" else f"{GOLD}🔌{RST}"
                name = f"{WHITE}{BOLD}{c_name:<12}{RST}" if is_sel else f"{CYAN}{c_name:<12}{RST}"
                cat = f"{PURPLE}{c_cat:<16}{RST}" if is_sel else f"{DARK_SLATE}{c_cat:<16}{RST}"
                desc = (f"{WHITE}{c_desc}{RST}" if is_sel else f"{SLATE}{c_desc}{RST}")[:max(10, width - 42)]
                frame_lines.append(f"  {DARK_SLATE}│{RST} {prefix} {kind} {name} {cat} {desc}\033[K")
            frame_lines.append(f"  {DARK_SLATE}╰──{'─' * max(10, width - 6)}╯{RST}\033[K")
        elif matching_files:
            popup_header = f"Files ({selected_file_idx + 1}/{len(matching_files)} · ↑/↓ scroll · Tab fill · Enter select)"
            p_border_w = max(4, width - len(strip_ansi(popup_header)) - 7)
            frame_lines.append(f"  {DARK_SLATE}╭── {SLATE}{popup_header}{DARK_SLATE} {'─' * p_border_w}╮{RST}\033[K")
            start_v = max(0, min(selected_file_idx - max_visible // 2, len(matching_files) - max_visible))
            end_v = min(len(matching_files), start_v + max_visible)
            for idx in range(start_v, end_v):
                f_path = matching_files[idx]
                is_sel = (idx == selected_file_idx)
                prefix = f"{MINT}▶{RST}" if is_sel else " "
                disp = (f_path if len(f_path) <= (width - 16) else "…" + f_path[-(width - 18):])
                name = f"{WHITE}{BOLD}{disp}{RST}" if is_sel else f"{SLATE}{disp}{RST}"
                frame_lines.append(f"  {DARK_SLATE}│{RST} {prefix} 📄 {name}\033[K")
            frame_lines.append(f"  {DARK_SLATE}╰──{'─' * max(10, width - 6)}╯{RST}\033[K")

        # Cleanly fit all frame lines within terminal width to prevent auto-wrap skew
        safe_max_w = max(20, tw - 2)
        safe_lines = [safe_ansi_truncate(l, safe_max_w) for l in frame_lines]

        # Erase previous frame cleanly from the prompt line up to frame top
        sys.stdout.write("\033[?25l")
        if not initial and last_prompt_row > 0:
            sys.stdout.write(f"\033[{last_prompt_row}A\r\033[J")
        else:
            sys.stdout.write("\r\033[J")
        sys.stdout.write("\n".join(safe_lines))
        lines_up = max(0, (len(safe_lines) - 1) - prompt_row_idx)
        target_col = max(1, min(tw - 1, 2 + len(strip_ansi(p_prefix)) + col_in_disp + 1))
        if lines_up > 0:
            sys.stdout.write(f"\033[{lines_up}A\r\033[{target_col}G\033[?25h")
        else:
            sys.stdout.write(f"\r\033[{target_col}G\033[?25h")
        sys.stdout.flush()
        last_rendered_lines = len(safe_lines)
        last_prompt_row = prompt_row_idx

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass
    draw(initial=True)

    def handle_sigwinch(signum, frame):
        nonlocal last_prompt_row
        try:
            sys.stdout.write("\033[?25l")
            if last_prompt_row > 0:
                sys.stdout.write(f"\033[{last_prompt_row}A\r\033[J")
            else:
                sys.stdout.write("\r\033[J")
            last_prompt_row = 0
            draw(initial=True)
        except Exception:
            pass
    try:
        orig_sigwinch = signal.signal(signal.SIGWINCH, handle_sigwinch)
    except Exception:
        orig_sigwinch = None

    last_esc_time = 0.0
    stashed_prompt = ""
    undo_stack: list[tuple[list[str], int]] = []

    try:
        tty.setcbreak(fd)
        while True:
            raw_bytes = os.read(fd, 8192)
            if not raw_bytes: break
            buf_str = "".join(buffer)
            matching_cmds = get_matching_commands(buf_str) if (cmd_popup_open and buf_str.startswith("/")) else []
            buf_before_cur = "".join(buffer[:cursor_pos])
            last_at = buf_before_cur.rfind("@")
            matching_files = get_matching_files(buf_before_cur[last_at + 1:]) if (file_popup_open and not matching_cmds and last_at != -1 and " " not in buf_before_cur[last_at:]) else []

            if raw_bytes in (b"\t", b"\x09"):
                if matching_files:
                    new_before = buf_before_cur[:last_at + 1] + matching_files[selected_file_idx % len(matching_files)] + " "
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = list(new_before + "".join(buffer[cursor_pos:]))
                    cursor_pos = len(new_before)
                    draw()
                elif matching_cmds:
                    item = matching_cmds[selected_cmd_idx % len(matching_cmds)]
                    fill = f"{item[0]} " if item[-1] else item[0]
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer, cursor_pos = list(fill), len(fill)
                    draw()
                else:
                    idx = (MODES_CYCLE.index(current_mode) + 1) % len(MODES_CYCLE) if current_mode in MODES_CYCLE else 0
                    current_mode = MODES_CYCLE[idx]
                    draw()
                continue

            # Shift+Tab variations -> Cycle modes backward across all 4 modes
            if any(raw_bytes.startswith(k) for k in (b"\x1b[Z", b"\x1b\t", b"\x1b\x09", b"\x1b[27;2;9~", b"\x1b[9;2u", b"\x1b[24~", b"\x1b[1;2Z", b"\x1bOZ")):
                idx = (MODES_CYCLE.index(current_mode) - 1) % len(MODES_CYCLE) if current_mode in MODES_CYCLE else 0
                current_mode = MODES_CYCLE[idx]
                draw()
                continue

            # Ctrl+T (0x14) -> Toggle tasks / checklist (/todos)
            if raw_bytes == b"\x14":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ("/todos", current_mode if current_mode != mode else None, current_subagent_idx)

            # Ctrl+O (0x0f) -> Toggle verbose output / thinking (/thinking)
            if raw_bytes == b"\x0f":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ("/thinking", current_mode if current_mode != mode else None, current_subagent_idx)

            # Ctrl+Z (0x1a) -> Suspend process cleanly
            if raw_bytes == b"\x1a":
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
                os.kill(os.getpid(), signal.SIGTSTP)
                tty.setcbreak(fd)
                draw()
                continue

            # Opt+P / Alt+P (b"\x1bp", b"\xcf\x80") -> Switch model
            if raw_bytes in (b"\x1bp", b"\xcf\x80"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ("/model", current_mode if current_mode != mode else None, current_subagent_idx)

            # Down Arrow -> Navigate command history forward
            if raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                if matching_files:
                    selected_file_idx = (selected_file_idx + 1) % len(matching_files)
                    draw()
                elif matching_cmds:
                    selected_cmd_idx = (selected_cmd_idx + 1) % len(matching_cmds)
                    draw()
                elif history_idx < len(history) - 1:
                    history_idx += 1
                    buffer = list(history[history_idx])
                    cursor_pos = len(buffer)
                    draw()
                elif history_idx == len(history) - 1:
                    history_idx = len(history)
                    buffer = list(saved_draft)
                    cursor_pos = len(buffer)
                    draw()
                continue

            # Up Arrow -> Navigate command history backward
            if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                if matching_files:
                    selected_file_idx = (selected_file_idx - 1) % len(matching_files)
                    draw()
                elif matching_cmds:
                    selected_cmd_idx = (selected_cmd_idx - 1) % len(matching_cmds)
                    draw()
                elif history and history_idx > 0:
                    if history_idx == len(history):
                        saved_draft = "".join(buffer)
                    history_idx -= 1
                    buffer = list(history[history_idx])
                    cursor_pos = len(buffer)
                    draw()
                continue

            # Esc -> Dismiss popup, or double tap Esc to clear input
            if raw_bytes == b"\x1b":
                if (matching_cmds and cmd_popup_open) or (matching_files and file_popup_open):
                    cmd_popup_open = False
                    file_popup_open = False
                    draw()
                    continue
                now_t = time.time()
                if now_t - last_esc_time < 0.45:
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = []
                    cursor_pos = 0
                    cmd_popup_open = True
                    draw()
                    last_esc_time = 0.0
                    continue
                last_esc_time = now_t

            # Ctrl+S (0x13) -> Stash prompt / Restore stashed prompt
            if raw_bytes == b"\x13":
                if buffer:
                    stashed_prompt = "".join(buffer)
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = []
                    cursor_pos = 0
                    draw()
                elif stashed_prompt:
                    buffer = list(stashed_prompt)
                    cursor_pos = len(buffer)
                    stashed_prompt = ""
                    draw()
                continue

            # Ctrl+G (0x07) -> Edit in $EDITOR (nano/vim)
            if raw_bytes == b"\x07":
                import tempfile
                editor = os.environ.get("EDITOR") or "nano"
                with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w+", encoding="utf-8") as tf:
                    tf.write("".join(buffer))
                    tf_path = tf.name
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
                    subprocess.call([editor, tf_path])
                    with open(tf_path, "r", encoding="utf-8") as tf_read:
                        new_text = tf_read.read()
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = list(new_text.strip())
                    cursor_pos = len(buffer)
                finally:
                    try:
                        os.remove(tf_path)
                    except Exception:
                        pass
                    tty.setcbreak(fd)
                    draw()
                continue

            # Ctrl+V (0x16) -> Paste clipboard content / image paths
            if raw_bytes == b"\x16":
                clip_text = ""
                try:
                    p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=1)
                    if p.returncode == 0:
                        clip_text = p.stdout
                except Exception:
                    pass
                if clip_text:
                    undo_stack.append((list(buffer), cursor_pos))
                    for ch in clip_text:
                        buffer.insert(cursor_pos, ch)
                        cursor_pos += 1
                    draw()
                continue

            # Ctrl+Shift+_ / Undo (0x1f)
            if raw_bytes in (b"\x1f", b"\x1b[31;2~"):
                if undo_stack:
                    prev_buf, prev_pos = undo_stack.pop()
                    buffer = prev_buf
                    cursor_pos = min(len(buffer), prev_pos)
                    draw()
                continue

            # Right Arrow
            if raw_bytes.startswith((b"\x1b[C", b"\x1bOC")):
                if cursor_pos < len(buffer):
                    cursor_pos += 1
                    draw()
                continue

            # Left Arrow -> Switch to session switcher if buffer is empty, else move cursor left
            if raw_bytes.startswith((b"\x1b[D", b"\x1bOD")):
                if not buffer:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return ("__SWITCH_SESSION__", current_mode if current_mode != mode else None, current_subagent_idx)
                if cursor_pos > 0:
                    cursor_pos -= 1
                    draw()
                continue

            # Home key
            if raw_bytes in (b"\x1b[H", b"\x1b[1~", b"\x1bOH", b"\x01"):
                cursor_pos = 0
                draw()
                continue

            # End key
            if raw_bytes in (b"\x1b[F", b"\x1b[4~", b"\x1bOF", b"\x05"):
                cursor_pos = len(buffer)
                draw()
                continue

            # Delete key
            if raw_bytes in (b"\x1b[3~", b"\x1b[3;5~"):
                if cursor_pos < len(buffer):
                    buffer.pop(cursor_pos)
                    draw()
                continue

            # Enter (submit prompt, complete slash command, or complete @file)
            if raw_bytes in (b"\r", b"\n", b"\r\n"):
                if matching_files:
                    chosen_file = matching_files[selected_file_idx % len(matching_files)]
                    new_before = buf_before_cur[:last_at + 1] + chosen_file + " "
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = list(new_before + "".join(buffer[cursor_pos:]))
                    cursor_pos = len(new_before)
                    file_popup_open = False
                    draw()
                    continue

                buf_text = "".join(buffer).strip()
                if matching_cmds and not (" " in buf_text):
                    chosen_cmd = matching_cmds[selected_cmd_idx % len(matching_cmds)][0]
                    buffer = list(chosen_cmd)
                    cursor_pos = len(buffer)
                    cmd_popup_open = False
                    draw()

                if buffer and buffer[-1] == "\\":
                    buffer.pop()
                    buffer.append("\n")
                    cursor_pos = len(buffer)
                    draw()
                    continue
                if last_prompt_row > 0:
                    sys.stdout.write(f"\033[{last_prompt_row}A\r\033[J")
                else:
                    sys.stdout.write("\r\033[J")
                sys.stdout.flush()
                break

            # Backspace
            if raw_bytes in (b"\x7f", b"\x08"):
                if cursor_pos > 0:
                    buffer.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    selected_cmd_idx = 0
                    selected_file_idx = 0
                    cmd_popup_open = True
                    file_popup_open = True
                    draw()
                continue

            # Ctrl+C
            if raw_bytes == b"\x03":
                sys.stdout.write("\n")
                raise KeyboardInterrupt

            # Ctrl+D
            if raw_bytes == b"\x04":
                if not buffer:
                    sys.stdout.write("\n")
                    raise EOFError
                if cursor_pos < len(buffer):
                    buffer.pop(cursor_pos)
                    draw()
                continue

            # Ctrl+U (Clear line)
            if raw_bytes == b"\x15":
                undo_stack.append((list(buffer), cursor_pos))
                buffer = []
                cursor_pos = 0
                cmd_popup_open = True
                file_popup_open = True
                draw()
                continue

            # Skip unknown escape sequences
            if raw_bytes.startswith(b"\x1b"):
                continue

            # Printable characters & Paste text
            try:
                decoded = raw_bytes.decode("utf-8")
                if len(decoded) > 3:
                    undo_stack.append((list(buffer), cursor_pos))
                for ch in decoded:
                    if ch in ("\r", "\n"):
                        buffer.insert(cursor_pos, " ")
                    elif ord(ch) >= 32 or ch == "\t":
                        buffer.insert(cursor_pos, ch)
                    cursor_pos += 1
                selected_cmd_idx = 0
                selected_file_idx = 0
                cmd_popup_open = True
                file_popup_open = True
                draw()
            except Exception:
                pass

    finally:
        sys.stdout.write("\033[?25h")
        if orig_sigwinch is not None:
            try:
                signal.signal(signal.SIGWINCH, orig_sigwinch)
            except Exception:
                pass
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)

    line = "".join(buffer).strip()
    if line:
        if not _SESSION_HISTORY or _SESSION_HISTORY[-1] != line:
            _SESSION_HISTORY.append(line)
        try:
            readline.add_history(line)
        except Exception:
            pass

    toggled_mode = current_mode if current_mode != mode else None
    return (line, toggled_mode, current_subagent_idx)
