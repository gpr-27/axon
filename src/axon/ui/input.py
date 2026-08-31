"""
Interactive input reading with raw OS terminal key-handling, live Tab/Shift+Tab mode toggling,
SIGWINCH auto-resize, history, and interactive scrollable slash-command autocomplete popup.
"""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import readline
except ModuleNotFoundError:
    readline = None  # type: ignore

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ModuleNotFoundError:
    termios = None  # type: ignore
    tty = None      # type: ignore
    _HAS_TERMIOS = False

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
    term_width,
)

MODES_CYCLE = ["default", "acceptEdits", "plan", "bypass"]

# In-memory history across REPL turns in the session
_SESSION_HISTORY: list[str] = []

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
    ("/provider", "core", "Core & Models", "Connect local or cloud engine (Ollama, LM Studio, OpenRouter, Anthropic, OpenAI)", False),
    ("/keys", "core", "Core & Models", "View and update API keys & environment credentials (/keys <provider>)", True),
    ("/env", "core", "Core & Models", "Inspect runtime environment variables and provider keys", False),
    ("/effort", "core", "Core & Models", "Adjust neural reasoning tier (reflex, balanced, synapse, quantum)", True),
    ("/config", "core", "Core & Models", "View and adjust runtime configuration parameters", True),
    ("/status", "core", "Core & Models", "View comprehensive live system and agent status", False),
    ("/permissions", "core", "Core & Models", "Inspect active permission engine rules and mode", False),
    ("/plan", "core", "Core & Models", "View task checklist or switch to plan mode", True),

    # 📊 Context & Token Budget
    ("/breakdown", "core", "Context & Tokens", "Full input prompt breakdown & token matching", False),
    ("/context", "core", "Context & Tokens", "View active context token budget and limits", False),
    ("/compact", "core", "Context & Tokens", "Compact conversation context while preserving key facts", False),
    ("/statusbar", "core", "Context & Tokens", "Live real-time token capacity gauge and metrics", False),
    ("/analytics", "core", "Context & Tokens", "Lifetime workspace analytics and tool insights", False),
    ("/window", "core", "Context & Tokens", "Adjust sliding context window size (e.g. /window 10)", True),
    ("/cost", "core", "Context & Tokens", "View session billing ledger and prompt cache hits", False),
    ("/payload", "core", "Context & Tokens", "Inspect exact prompt payload and tool results", True),

    # 📜 History & File Revisions
    ("/history", "core", "History & Diff", "View all messages in full conversation history", False),
    ("/diff", "core", "History & Diff", "View working tree uncommitted git diff", False),
    ("/review", "core", "History & Diff", "Run automated multi-file code review", True),
    ("/rewind", "core", "History & Diff", "Revert file edits made during previous turns", False),
    ("/copy", "core", "History & Diff", "Copy last response, code blocks, or diff to clipboard", True),
    ("/expand", "core", "History & Diff", "View full un-truncated output or expand any file", True),
    ("/export", "core", "History & Diff", "Export session to Markdown or JSON transcript", True),

    # 🤖 Multi-Agent, Tasks & Skills
    ("/subagents", "core", "Multi-Agent", "Axon subagent matrix & isolated worker transcripts", False),
    ("/todos", "core", "Multi-Agent", "View active multi-step task checklist", False),
    ("/q", "core", "Multi-Agent", "Add/manage sequential prompt queue (/q <text>, /q drop, /q clear)", True),
    ("/skills", "core", "Multi-Agent", "Browse active skills studio and create new skills", True),
    ("/mcp", "ext", "Multi-Agent", "Inspect Model Context Protocol servers and tools", False),
    ("/plugin", "ext", "Multi-Agent", "Inspect installed plugins and extension manifests", True),
    ("/hooks", "ext", "Multi-Agent", "Inspect active lifecycle and execution hooks", False),
    ("/memory", "core", "Multi-Agent", "Inspect persistent workspace memory store", False),
    ("/learn", "core", "Multi-Agent", "Teach a new convention to persistent memory", True),

    # 📁 Sessions & Workspace
    ("/main", "core", "Sessions & Root", "Switch back to the Main Agent / parent chart session", False),
    ("/root", "core", "Sessions & Root", "Alias for /main to return to root chart session", False),
    ("/sessions", "core", "Sessions & Root", "Axon session matrix timeline dashboard", False),
    ("/rename", "core", "Sessions & Root", "Rename the active session with custom title", True),
    ("/tag", "core", "Sessions & Root", "Add tag to active session for categorized filtering", True),
    ("/star", "core", "Sessions & Root", "Star the active session as a favorite", False),
    ("/resume", "core", "Sessions & Root", "Resume previous session from transcript", True),
    ("/branch", "core", "Sessions & Root", "Fork current conversation into an independent branch", True),
    ("/find", "core", "Sessions & Root", "Interactive fuzzy file finder (Ctrl+P)", False),
    ("/test", "core", "Sessions & Root", "Run test suite (pytest, npm test, cargo test)", True),
    ("/notify", "core", "Sessions & Root", "Test system desktop notification", True),
    ("/tools", "core", "Sessions & Root", "List all active agent tools, schemas, and permissions", False),
    ("/doctor", "core", "Sessions & Root", "Run local diagnostics & environment health check", False),
    ("/init", "ext", "Sessions & Root", "Initialize AGENTS.md conventions file in workspace", False),

    # ⌨️ Session Control & Input
    ("/btw", "core", "Session Control", "Ask side inquiry with zero context pollution (/btw <q>)", True),
    ("/voice", "core", "Session Control", "Toggle speech dictation / voice input mode", False),
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
    if not sys.stdin.isatty() or not _HAS_TERMIOS:
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
        try:
            line = input(p_tag)
        except EOFError:
            return ("/exit", None, 0)
        return (line.strip(), None, 0)

    current_mode = mode
    current_subagent_idx = 0
    buffer: list[str] = []
    cursor_pos = 0

    # Print clean divider and status header above prompt
    def _render_status_header(m: str) -> None:
        tw = term_width()
        width = max(40, tw - 4)
        label_text, mode_color = _get_mode_info(m)
        plan_badge = f"  {MINT}{BOLD}{plan_summary}{RST}" if plan_summary else ""
        queue_badge = f"  {CYAN}{BOLD}{queue_summary}{RST}" if queue_summary else ""
        agent_hint = f" · {DARK_SLATE}/main to return · ← sessions{RST}" if subagent_label else f" · {DARK_SLATE}← sessions{RST}"
        left_str = f"▮ {label_text}{plan_badge}{queue_badge}{agent_hint}"
        right_str = f"○ {effort} · /effort · ⌨ /kb · ? shortcuts"
        pad = max(2, width - len(strip_ansi(left_str)) - len(strip_ansi(right_str)))
        sys.stdout.write(f"\r\033[K  {mode_color}{BOLD}{left_str}{RST}{' ' * pad}{SLATE}{right_str}{RST}\n")
        sys.stdout.flush()

    width = max(40, term_width() - 4)
    sys.stdout.write(f"\n  {DARK_SLATE}{'─' * width}{RST}\n")
    sys.stdout.flush()
    _render_status_header(current_mode)

    # Build history from session store + readline + recent user messages
    history: list[str] = []
    for h_item in _SESSION_HISTORY:
        if h_item and h_item not in history:
            history.append(h_item)
    if readline is not None:
        try:
            num_hist = readline.get_current_history_length()
            for i in range(1, num_hist + 1):
                item = readline.get_history_item(i)
                if item and item not in history:
                    history.append(item)
        except Exception:
            pass

    if len(history) < 15:
        try:
            s_dir = Path.home() / ".axon" / "sessions"
            if s_dir.exists():
                latest_sessions = sorted(s_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
                for s_f in latest_sessions:
                    with open(s_f, "r", encoding="utf-8", errors="ignore") as f:
                        for line_json in f:
                            entry = json.loads(line_json)
                            if entry.get("type") == "user_message":
                                u_text = entry.get("data", {}).get("content", "")
                                if isinstance(u_text, str) and u_text.strip() and u_text.strip() not in history:
                                    history.insert(0, u_text.strip())
        except Exception:
            pass

    history_idx = len(history)

    # Autocomplete state
    selected_cmd_idx = 0
    cmd_popup_open = True
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
        tw = term_width()
        width = max(40, tw - 4)
        buf_str = "".join(buffer)
        is_bash = buf_str.startswith("!")

        if is_bash:
            p_prefix = f"{AMBER}{BOLD}!{RST} "
        elif subagent_label:
            p_prefix = f"{CYAN}[{subagent_label}]{RST} {BOLD}{CYAN}❯{RST} "
        else:
            p_prefix = f"{BOLD}{CYAN}❯{RST} "

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

        avail_prompt_w = max(10, tw - len(strip_ansi(p_prefix)) - 4)
        if not buffer:
            disp_body = f"{DARK_SLATE}Ask anything, use / for commands, @ for files, ! for shell...{RST}"
            col_in_disp = 0
        elif is_bash:
            cmd_body = buf_str[1:]
            if not cmd_body:
                disp_body = f"{DARK_SLATE}type bash command (e.g. ls, git status, pytest)...{RST}"
                col_in_disp = 0
            else:
                cursor_in_body = max(0, cursor_pos - 1)
                if len(cmd_body) > avail_prompt_w:
                    start_ch = max(0, cursor_in_body - avail_prompt_w + 3)
                    disp_body = f"{WHITE}{BOLD}{cmd_body[start_ch:start_ch + avail_prompt_w]}{RST}"
                    col_in_disp = cursor_in_body - start_ch
                else:
                    disp_body = f"{WHITE}{BOLD}{cmd_body}{RST}"
                    col_in_disp = cursor_in_body
        else:
            if len(buf_str) > avail_prompt_w:
                start_ch = max(0, cursor_pos - avail_prompt_w + 3)
                slice_body = buf_str[start_ch:start_ch + avail_prompt_w]
                col_in_disp = cursor_pos - start_ch
            else:
                slice_body = buf_str
                col_in_disp = cursor_pos
            styled = re.sub(r"(\[Image\s*#\d+\])", f"{CYAN}{BOLD}\\1{RST}{WHITE}{BOLD}", slice_body)
            disp_body = f"{WHITE}{BOLD}{styled}{RST}"

        target_col = max(1, min(tw - 1, 2 + len(strip_ansi(p_prefix)) + col_in_disp + 1))

        # Check for popup lines
        popup_lines: list[str] = []
        max_visible = 8
        if matching_cmds:
            popup_header = f"Commands ({selected_cmd_idx + 1}/{len(matching_cmds)} · {MINT}● Built-in{SLATE} · {GOLD}🔌 Config/Ext{SLATE} · ↑/↓ scroll · Tab fill · Enter run)"
            p_border_w = max(4, width - len(strip_ansi(popup_header)) - 7)
            popup_lines.append(f"  {DARK_SLATE}╭── {SLATE}{popup_header}{DARK_SLATE} {'─' * p_border_w}╮{RST}")
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
                popup_lines.append(f"  {DARK_SLATE}│{RST} {prefix} {kind} {name} {cat} {desc}")
            popup_lines.append(f"  {DARK_SLATE}╰──{'─' * max(10, width - 6)}╯{RST}")
        elif matching_files:
            popup_header = f"Files ({selected_file_idx + 1}/{len(matching_files)} · ↑/↓ scroll · Tab fill · Enter select)"
            p_border_w = max(4, width - len(strip_ansi(popup_header)) - 7)
            popup_lines.append(f"  {DARK_SLATE}╭── {SLATE}{popup_header}{DARK_SLATE} {'─' * p_border_w}╮{RST}")
            start_v = max(0, min(selected_file_idx - max_visible // 2, len(matching_files) - max_visible))
            end_v = min(len(matching_files), start_v + max_visible)
            for idx in range(start_v, end_v):
                f_path = matching_files[idx]
                is_sel = (idx == selected_file_idx)
                prefix = f"{MINT}▶{RST}" if is_sel else " "
                disp = (f_path if len(f_path) <= (width - 16) else "…" + f_path[-(width - 18):])
                name = f"{WHITE}{BOLD}{disp}{RST}" if is_sel else f"{SLATE}{disp}{RST}"
                popup_lines.append(f"  {DARK_SLATE}│{RST} {prefix} 📄 {name}")
            popup_lines.append(f"  {DARK_SLATE}╰──{'─' * max(10, width - 6)}╯{RST}")

        if popup_lines:
            safe_popup = [safe_ansi_truncate(l, max(20, tw - 2)) for l in popup_lines]
            output = [f"\033[?25l\033[?7l\r\033[K  {p_prefix}{disp_body}"]
            for pl in safe_popup:
                output.append(f"\n\r\033[K{pl}")
            output.append(f"\033[J\033[{len(safe_popup)}A\r\033[{target_col}G\033[?7h\033[?25h")
            sys.stdout.write("".join(output))
        else:
            sys.stdout.write(f"\033[?25l\033[?7l\r\033[K  {p_prefix}{disp_body}\033[J\r\033[{target_col}G\033[?7h\033[?25h")
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass
    draw(initial=True)

    def handle_sigwinch(signum, frame):
        try:
            draw(initial=False)
        except Exception:
            pass
    try:
        sigwinch = getattr(signal, "SIGWINCH", None)
        orig_sigwinch = signal.signal(sigwinch, handle_sigwinch) if sigwinch is not None else None
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
                    sys.stdout.write("\033[1A")
                    _render_status_header(current_mode)
                    draw()
                continue

            # Shift+Tab variations -> Cycle modes backward across all 4 modes
            if any(raw_bytes.startswith(k) for k in (b"\x1b[Z", b"\x1b\t", b"\x1b\x09", b"\x1b[27;2;9~", b"\x1b[9;2u", b"\x1b[24~", b"\x1b[1;2Z", b"\x1bOZ")):
                idx = (MODES_CYCLE.index(current_mode) - 1) % len(MODES_CYCLE) if current_mode in MODES_CYCLE else 0
                current_mode = MODES_CYCLE[idx]
                sys.stdout.write("\033[1A")
                _render_status_header(current_mode)
                draw()
                continue

            # Ctrl+T (0x14) -> Toggle tasks / checklist (/todos)
            if raw_bytes == b"\x14":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ("/todos", current_mode if current_mode != mode else None, current_subagent_idx)

            # Ctrl+O (0x0f) -> Show full verbose output (/output)
            if raw_bytes == b"\x0f":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return ("/output", current_mode if current_mode != mode else None, current_subagent_idx)

            # Ctrl+Z (0x1a) -> Suspend process cleanly
            if raw_bytes == b"\x1a":
                if termios is not None and hasattr(signal, "SIGTSTP"):
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
                if matching_files and file_popup_open and ("@" in buf_str) and history_idx == len(history):
                    selected_file_idx = (selected_file_idx + 1) % len(matching_files)
                    draw()
                elif matching_cmds and cmd_popup_open and buf_str.startswith("/") and history_idx == len(history) and len(matching_cmds) > 1 and selected_cmd_idx < len(matching_cmds) - 1:
                    selected_cmd_idx = selected_cmd_idx + 1
                    draw()
                elif history_idx < len(history) - 1:
                    history_idx += 1
                    buffer = list(history[history_idx])
                    cursor_pos = len(buffer)
                    cmd_popup_open = False
                    file_popup_open = False
                    draw()
                elif history_idx == len(history) - 1:
                    history_idx = len(history)
                    buffer = list(saved_draft)
                    cursor_pos = len(buffer)
                    cmd_popup_open = True
                    file_popup_open = True
                    draw()
                continue

            # Up Arrow -> Navigate command history backward
            if raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                if matching_files and file_popup_open and ("@" in buf_str) and history_idx == len(history):
                    selected_file_idx = (selected_file_idx - 1) % len(matching_files)
                    draw()
                elif matching_cmds and cmd_popup_open and buf_str.startswith("/") and history_idx == len(history) and len(matching_cmds) > 1 and selected_cmd_idx > 0:
                    selected_cmd_idx = selected_cmd_idx - 1
                    draw()
                elif history and history_idx > 0:
                    if history_idx == len(history):
                        saved_draft = "".join(buffer)
                    history_idx -= 1
                    buffer = list(history[history_idx])
                    cursor_pos = len(buffer)
                    cmd_popup_open = False
                    file_popup_open = False
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
                    if termios is not None:
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
                    if tty is not None:
                        tty.setcbreak(fd)
                    draw()
                continue

            # Ctrl+A (0x01) -> Move cursor to start of prompt (Emacs/Vim)
            if raw_bytes == b"\x01":
                cursor_pos = 0
                draw()
                continue

            # Ctrl+E (0x05) -> Move cursor to end of prompt (Emacs/Vim)
            if raw_bytes == b"\x05":
                cursor_pos = len(buffer)
                draw()
                continue

            # Ctrl+K (0x0b) -> Kill text to end of line
            if raw_bytes == b"\x0b":
                if cursor_pos < len(buffer):
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = buffer[:cursor_pos]
                    draw()
                continue

            # Ctrl+U (0x15) -> Kill text to start of line
            if raw_bytes == b"\x15":
                if cursor_pos > 0:
                    undo_stack.append((list(buffer), cursor_pos))
                    buffer = buffer[cursor_pos:]
                    cursor_pos = 0
                    draw()
                continue

            # Ctrl+W (0x17) -> Delete word backward
            if raw_bytes == b"\x17":
                if cursor_pos > 0:
                    undo_stack.append((list(buffer), cursor_pos))
                    idx = cursor_pos
                    # Skip trailing whitespace
                    while idx > 0 and buffer[idx - 1] == " ":
                        idx -= 1
                    # Skip word chars
                    while idx > 0 and buffer[idx - 1] != " ":
                        idx -= 1
                    buffer = buffer[:idx] + buffer[cursor_pos:]
                    cursor_pos = idx
                    draw()
                continue

            # Alt+B / Opt+Left -> Move word backward
            if raw_bytes in (b"\x1bb", b"\x1b[1;3D", b"\x1b[1;5D", b"\x1b\x1b[D"):
                idx = cursor_pos
                while idx > 0 and buffer[idx - 1] == " ":
                    idx -= 1
                while idx > 0 and buffer[idx - 1] != " ":
                    idx -= 1
                cursor_pos = max(0, idx)
                draw()
                continue

            # Alt+F / Opt+Right -> Move word forward
            if raw_bytes in (b"\x1bf", b"\x1b[1;3C", b"\x1b[1;5C", b"\x1b\x1b[C"):
                idx = cursor_pos
                while idx < len(buffer) and buffer[idx] != " ":
                    idx += 1
                while idx < len(buffer) and buffer[idx] == " ":
                    idx += 1
                cursor_pos = min(len(buffer), idx)
                draw()
                continue

            # Ctrl+P (0x10) -> Interactive Fuzzy File Finder
            if raw_bytes == b"\x10":
                from axon.ui.fuzzy_picker import run_fuzzy_file_finder
                if termios is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
                chosen_file = run_fuzzy_file_finder(Path.cwd())
                if tty is not None:
                    tty.setcbreak(fd)
                if chosen_file:
                    undo_stack.append((list(buffer), cursor_pos))
                    ins = f"@{chosen_file} "
                    for ch in ins:
                        buffer.insert(cursor_pos, ch)
                        cursor_pos += 1
                draw(initial=True)
                continue


            # Ctrl+V (0x16) -> Paste clipboard content / image data instantly (< 40ms)
            if raw_bytes == b"\x16":
                clip_text = ""
                # 1. Try to extract image from clipboard instantly with Cocoa helper
                try:
                    import uuid
                    img_temp = Path(f"/tmp/axon_clip_{time.time_ns()}_{uuid.uuid4().hex[:6]}.png")
                    from axon.agent.images import save_clipboard_image, compact_image_paths
                    if save_clipboard_image(img_temp):
                        clip_text = f"[Image: {img_temp}] "
                except Exception:
                    pass

                # 2. Fallback to text clipboard if no binary image
                if not clip_text:
                    try:
                        p = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=0.2)
                        if p.returncode == 0 and p.stdout:
                            clip_text = p.stdout
                    except Exception:
                        pass

                if clip_text:
                    from axon.agent.images import compact_image_paths
                    compacted_clip, _ = compact_image_paths(clip_text)
                    undo_stack.append((list(buffer), cursor_pos))
                    for ch in compacted_clip:
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
            if raw_bytes in (b"\x1b[H", b"\x1b[1~", b"\x1bOH"):
                cursor_pos = 0
                draw()
                continue

            # End key
            if raw_bytes in (b"\x1b[F", b"\x1b[4~", b"\x1bOF"):
                cursor_pos = len(buffer)
                draw()
                continue

            # Delete key
            if raw_bytes in (b"\x1b[3~", b"\x1b[3;5~"):
                if 0 <= cursor_pos < len(buffer):
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
                buf_str_final = "".join(buffer).strip()
                if buf_str_final.startswith("!"):
                    p_prefix_final = f"{AMBER}{BOLD}!{RST} "
                elif subagent_label:
                    p_prefix_final = f"{CYAN}[{subagent_label}]{RST} {BOLD}{CYAN}❯{RST} "
                else:
                    p_prefix_final = f"{BOLD}{CYAN}❯{RST} "
                styled_final = re.sub(r"(\[Image\s*#\d+\])", f"{CYAN}{BOLD}\\1{RST}{WHITE}{BOLD}", buf_str_final)
                sys.stdout.write(f"\r\033[K  {p_prefix_final}{WHITE}{BOLD}{styled_final}{RST}\n\n\033[J\033[?7h\033[?25h")
                sys.stdout.flush()
                break

            # Backspace
            if raw_bytes in (b"\x7f", b"\x08"):
                cursor_pos = max(0, min(cursor_pos, len(buffer)))
                if cursor_pos > 0 and len(buffer) >= cursor_pos:
                    buffer.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    history_idx = len(history)
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
                if 0 <= cursor_pos < len(buffer):
                    buffer.pop(cursor_pos)
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
                    from axon.agent.images import compact_image_paths
                    compacted_decoded, _ = compact_image_paths(decoded)
                    decoded = compacted_decoded
                for ch in decoded:
                    if ch in ("\r", "\n"):
                        buffer.insert(cursor_pos, " ")
                    elif ord(ch) >= 32 or ch == "\t":
                        buffer.insert(cursor_pos, ch)
                    cursor_pos += 1
                history_idx = len(history)
                cmd_popup_open = True
                file_popup_open = True

                # Check if buffer now contains an uncompacted image path
                buf_full = "".join(buffer)
                if any(ext in buf_full.lower() for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
                    from axon.agent.images import compact_image_paths
                    comp_buf, _ = compact_image_paths(buf_full)
                    if comp_buf != buf_full:
                        buffer = list(comp_buf)
                        cursor_pos = min(cursor_pos, len(buffer))

                selected_cmd_idx = 0
                selected_file_idx = 0
                cmd_popup_open = True
                file_popup_open = True
                draw()
            except Exception:
                pass

    finally:
        sys.stdout.write("\033[?7h\033[?25h")
        if orig_sigwinch is not None and hasattr(signal, "SIGWINCH"):
            try:
                signal.signal(signal.SIGWINCH, orig_sigwinch)
            except Exception:
                pass
        if termios is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
            except Exception:
                pass

    line = "".join(buffer).strip()
    if line:
        if not _SESSION_HISTORY or _SESSION_HISTORY[-1] != line:
            _SESSION_HISTORY.append(line)
        if readline is not None:
            try:
                readline.add_history(line)
            except Exception:
                pass

    toggled_mode = current_mode if current_mode != mode else None
    return (line, toggled_mode, current_subagent_idx)

