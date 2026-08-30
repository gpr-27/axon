"""
Interactive live monitor for concurrent subagents.
Allows toggling live between Main Agent and Subagents 1..N using number keys [0-9], Tab, and Arrows,
with a live active input bar at the bottom for executing /ask, /btw, and queuing prompts.
"""
from __future__ import annotations
import os
import select
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
    ITALIC,
    LBLUE,
    MINT,
    PURPLE,
    ROSE,
    RST,
    SLATE,
    TEAL,
    WHITE,
    strip_ansi,
    term_width,
)

def run_live_subagent_monitor(future_map: dict[Any, int], subagents_mgr: Any, agent: Any = None) -> None:
    """
    Runs interactive live monitor while futures in future_map complete.
    Allows user to switch live views between Main [0] and Subagents [1..N] via keys,
    and provides a responsive live input bar at the bottom to execute /ask, /btw, or queue prompts.
    """
    if not sys.stdin.isatty():
        return

    fd = sys.stdin.fileno()
    old_attr = termios.tcgetattr(fd)
    viewing_idx = 0  # 0 = Main overview, 1..N = Subagents
    last_rendered_lines = 0
    input_buf: list[str] = []
    cursor_pos: int = 0

    try:
        tty.setcbreak(fd)
        while any(not fut.done() for fut in future_map):
            tasks = subagents_mgr.all_tasks()
            total_tasks = len(tasks)

            # Check if keyboard input is available without blocking
            r, _, _ = select.select([sys.stdin], [], [], 0.08)
            if r:
                raw_bytes = os.read(fd, 1024)
                if not raw_bytes:
                    break

                # Ctrl+C
                if raw_bytes == b"\x03":
                    break

                # Enter -> Submit /ask, /btw, slash command, or queue message
                if raw_bytes in (b"\r", b"\n", b"\r\n"):
                    if input_buf:
                        line_text = "".join(input_buf).strip()
                        input_buf.clear()
                        cursor_pos = 0
                        if line_text:
                            # Clear current monitor frame cleanly
                            sys.stdout.write("\033[?25l")
                            if last_rendered_lines > 1:
                                sys.stdout.write(f"\033[{last_rendered_lines - 1}A\r\033[J")
                            elif last_rendered_lines == 1:
                                sys.stdout.write("\r\033[J")
                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            last_rendered_lines = 0
                            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
                            try:
                                if line_text.startswith("/"):
                                    from axon.commands.builtin import dispatch_command
                                    if agent:
                                        dispatch_command(line_text, agent)
                                else:
                                    if agent and hasattr(agent, "message_queue"):
                                        agent.message_queue.push(line_text)
                                        print(f"\n  {MINT}✓ Queued follow-up:{RST} {WHITE}{line_text}{RST} {SLATE}(runs when subagents complete){RST}\n")
                            except Exception as e:
                                print(f"\n  ❌ Command error: {e}\n")
                            finally:
                                tty.setcbreak(fd)
                            continue

                # Esc
                elif raw_bytes == b"\x1b":
                    if input_buf:
                        input_buf.clear()
                        cursor_pos = 0
                    else:
                        viewing_idx = 0

                # Backspace
                elif raw_bytes in (b"\x7f", b"\x08"):
                    if cursor_pos > 0:
                        input_buf.pop(cursor_pos - 1)
                        cursor_pos -= 1

                # Ctrl+U (Clear input)
                elif raw_bytes == b"\x15":
                    input_buf.clear()
                    cursor_pos = 0

                # Left Arrow
                elif raw_bytes.startswith((b"\x1b[D", b"\x1bOD")):
                    if cursor_pos > 0:
                        cursor_pos -= 1
                    elif not input_buf:
                        viewing_idx = (viewing_idx - 1) % (total_tasks + 1)

                # Right Arrow
                elif raw_bytes.startswith((b"\x1b[C", b"\x1bOC")):
                    if cursor_pos < len(input_buf):
                        cursor_pos += 1
                    elif not input_buf:
                        viewing_idx = (viewing_idx + 1) % (total_tasks + 1)

                # Up Arrow / Down Arrow
                elif raw_bytes.startswith((b"\x1b[A", b"\x1bOA")):
                    if not input_buf:
                        viewing_idx = (viewing_idx - 1) % (total_tasks + 1)
                elif raw_bytes.startswith((b"\x1b[B", b"\x1bOB")):
                    if not input_buf:
                        viewing_idx = (viewing_idx + 1) % (total_tasks + 1)

                # Tab
                elif raw_bytes in (b"\t", b"\x09"):
                    if not input_buf:
                        viewing_idx = (viewing_idx + 1) % (total_tasks + 1)
                    else:
                        # Tab autocomplete /btw or /ask
                        curr_str = "".join(input_buf)
                        if curr_str.startswith("/b"):
                            input_buf = list("/btw ")
                            cursor_pos = len(input_buf)
                        elif curr_str.startswith("/a"):
                            input_buf = list("/ask ")
                            cursor_pos = len(input_buf)

                # Home / End
                elif raw_bytes in (b"\x1b[H", b"\x1b[1~", b"\x01"):
                    cursor_pos = 0
                elif raw_bytes in (b"\x1b[F", b"\x1b[4~", b"\x05"):
                    cursor_pos = len(input_buf)

                # Number keys when buffer is empty -> Quick agent switch
                elif not input_buf and raw_bytes in (b"0", b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8", b"9"):
                    num = int(raw_bytes.decode("latin1"))
                    if num <= total_tasks:
                        viewing_idx = num

                # Skip unhandled escape sequences
                elif raw_bytes.startswith(b"\x1b"):
                    pass

                # Printable characters
                else:
                    try:
                        decoded = raw_bytes.decode("utf-8")
                        for ch in decoded:
                            if ord(ch) >= 32:
                                input_buf.insert(cursor_pos, ch)
                                cursor_pos += 1
                    except Exception:
                        pass

            # Render frame
            tasks = subagents_mgr.all_tasks()
            tw = term_width()
            width = min(84, max(20, tw - 4))
            safe_max_w = max(20, tw - 2)

            frame_lines = []

            # 1. Tab bar
            tab_items = []
            if viewing_idx == 0:
                tab_items.append(f"{GOLD}{BOLD}● [0:Main]{RST}")
            else:
                tab_items.append(f"{SLATE}○ [0:Main]{RST}")

            for t in tasks:
                short_name = t.title
                if len(short_name) > 10:
                    short_name = short_name[:9] + "…"
                st_icon = "✓" if t.status == "completed" else ("!" if t.status == "exhausted" else "▶")
                if viewing_idx == t.index:
                    tab_items.append(f"{CYAN}{BOLD}● [{t.index}:{short_name}]({st_icon}){RST}")
                else:
                    tab_items.append(f"{SLATE}○ [{t.index}:{short_name}]{RST}")

            tabs_str = "  ".join(tab_items)
            frame_lines.append(f"  {tabs_str}")
            frame_lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")

            # 2. Body View: Main Overview vs Specific Subagent
            if viewing_idx == 0:
                # Main Overview
                done_count = sum(1 for t in tasks if t.status in ("completed", "exhausted", "error"))
                frame_lines.append(f"  {TEAL}{BOLD}⚡ Subagents Live Overview: {done_count}/{total_tasks} finished{RST}")
                for t in tasks:
                    if t.status == "completed":
                        b = f"{MINT}[✓]{RST}"
                        st = f"{SLATE}· {t.steps} steps · {t.elapsed_s:.1f}s · Done{RST}"
                    elif t.status == "exhausted":
                        b = f"{AMBER}[!]{RST}"
                        st = f"{AMBER}· {t.steps} steps · Ceiling Hit{RST}"
                    elif t.status == "error":
                        b = f"{ROSE}[✗]{RST}"
                        st = f"{ROSE}· Error{RST}"
                    else:
                        b = f"{CYAN}[▶]{RST}"
                        last_act = t.live_logs[-1] if t.live_logs else "Thinking..."
                        if len(last_act) > 35:
                            last_act = last_act[:32] + "..."
                        st = f"{CYAN}· step {t.steps} · {last_act}{RST}"
                    s_name = f"Subagent {t.index}: {t.title}"
                    if len(s_name) > 32:
                        s_name = s_name[:29] + "..."
                    frame_lines.append(f"    {b} {WHITE}{s_name:<32}{RST} {st}")
            else:
                # Specific Subagent detailed live view
                task = None
                for t in tasks:
                    if t.index == viewing_idx:
                        task = t
                        break
                if task:
                    st_clr = MINT if task.status == "completed" else (AMBER if task.status == "exhausted" else CYAN)
                    frame_lines.append(
                        f"  {CYAN}{BOLD}🔍 Live Feed: Subagent #{task.index} ({task.title}){RST} "
                        f"[{st_clr}{task.status.upper()}{RST} · {task.steps} steps · {task.elapsed_s:.1f}s]"
                    )
                    # Show recent live logs
                    logs = task.live_logs[-6:] if task.live_logs else ["  (Initializing subagent...)"]
                    for l in logs:
                        frame_lines.append(f"    {SLATE}│{RST} {WHITE}{l}{RST}")
                else:
                    frame_lines.append(f"  {SLATE}(Subagent #{viewing_idx} not active){RST}")

            # 3. Footer Control Bar
            frame_lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")
            ctrl_hint = f"{GOLD}[0-{min(9, total_tasks)}]{RST} {SLATE}select agent · {GOLD}[Tab / ↓ ↑]{RST} {SLATE}cycle · {GOLD}[Esc]{RST} {SLATE}Main · {GOLD}/ask{SLATE} or {GOLD}/btw{SLATE} side question{RST}"
            frame_lines.append(f"  {ctrl_hint}")
            frame_lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")

            # 4. Live Interactive Input Bar
            p_prefix = f"  {CYAN}{BOLD}›{RST} "
            buf_str = "".join(input_buf)
            frame_lines.append(f"{p_prefix}{buf_str}")

            # Safe truncate lines so no line wraps and breaks cursor arithmetic
            safe_lines: list[str] = []
            for l in frame_lines:
                vis_len = len(strip_ansi(l))
                if vis_len <= safe_max_w:
                    safe_lines.append(l)
                else:
                    excess = vis_len - safe_max_w
                    safe_lines.append(l[:max(0, len(l) - excess)] + RST)

            # Cleanly erase previous frame and draw new frame
            sys.stdout.write("\033[?25l")
            if last_rendered_lines > 1:
                sys.stdout.write(f"\033[{last_rendered_lines - 1}A\r\033[J")
            elif last_rendered_lines == 1:
                sys.stdout.write("\r\033[J")
            else:
                sys.stdout.write("\r\033[J")

            # Write all lines (no trailing newline after the input line, so cursor stays on input line)
            sys.stdout.write("\n".join(safe_lines))
            last_rendered_lines = len(safe_lines)

            # Move cursor to target position on the input line
            p_len = len(strip_ansi(p_prefix))
            target_col = max(1, min(tw - 1, p_len + cursor_pos + 1))
            sys.stdout.write(f"\r\033[{target_col}G\033[?25h")
            sys.stdout.flush()

    finally:
        sys.stdout.write("\033[?25l")
        if last_rendered_lines > 1:
            sys.stdout.write(f"\033[{last_rendered_lines - 1}A\r\033[J")
        elif last_rendered_lines == 1:
            sys.stdout.write("\r\033[J")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        last_rendered_lines = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        if input_buf and agent and hasattr(agent, "message_queue"):
            rem = "".join(input_buf).strip()
            if rem:
                agent.message_queue.push(rem)
