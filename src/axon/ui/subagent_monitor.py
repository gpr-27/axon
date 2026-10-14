"""
Live monitor shown while subagents run in parallel.

Views: [0] main overview, [1..N] a subagent's live feed, [Q] the prompt queue. Switch with the
number keys, Tab / Shift+Tab or the arrow keys. A bottom input line queues follow-up prompts
or runs slash commands (/btw, /q ...). Works on Windows consoles, macOS and Linux.
"""
from __future__ import annotations

import time
from typing import Any

from axon import platform as plat
from axon.ui.picker import LiveFrame
from axon.ui.theme import AMBER, BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag


def _views(tasks: list[Any]) -> list[Any]:
    return [0] + [t.index for t in tasks] + ["Q"]


def _cycle(current: Any, delta: int, tasks: list[Any]) -> Any:
    views = _views(tasks)
    pos = views.index(current) if current in views else 0
    return views[(pos + delta) % len(views)]


def _status_badge(t: Any) -> tuple[str, str]:
    if t.status == "completed":
        return f"{MINT}✓{RST}", f"{SLATE}{t.steps} steps · {t.elapsed_s:.1f}s · done{RST}"
    if t.status == "exhausted":
        return f"{AMBER}!{RST}", f"{AMBER}{t.steps} steps · step limit reached{RST}"
    if t.status == "error":
        return f"{ROSE}✗{RST}", f"{ROSE}error{RST}"
    last = (t.live_logs[-1] if getattr(t, "live_logs", None) else "thinking…").replace("\n", " ")
    return f"{CYAN}▶{RST}", f"{CYAN}step {t.steps} · {plat.truncate_ansi(last, 48)}{RST}"


def build_monitor_frame(view: Any, tasks: list[Any], agent: Any, buf: str, width: int) -> list[str]:
    """Render the monitor (pure function; used by the live loop and tests)."""
    queue = getattr(agent, "message_queue", None) if agent is not None else None
    q_len = len(queue) if queue is not None else 0
    tabs = [f"{GOLD}{BOLD}[0 Main]{RST}" if view == 0 else f"{SLATE}0 Main{RST}"]
    for t in tasks:
        icon, _ = _status_badge(t)
        name = plat.truncate_ansi(str(t.title), 12)
        tabs.append(f"{CYAN}{BOLD}[{t.index} {name}]{RST}{icon}" if view == t.index else f"{SLATE}{t.index} {name}{RST}{icon}")
    tabs.append(f"{MINT}{BOLD}[Q queue {q_len}]{RST}" if view == "Q" else f"{SLATE}Q queue {q_len}{RST}")
    lines = ["  " + "  ".join(tabs), f"  {DARK_SLATE}{'─' * width}{RST}"]
    if view == 0:
        done = sum(1 for t in tasks if t.status in ("completed", "exhausted", "error"))
        lines.append(f"  {TEAL}{BOLD}Subagents {done}/{len(tasks)} finished{RST}")
        for t in tasks:
            icon, status = _status_badge(t)
            lines.append(f"    {icon} {WHITE}{plat.pad_to(plat.truncate_ansi(f'#{t.index} {t.title}', 30), 30)}{RST} {status}")
    elif view == "Q":
        items = list(queue.items) if queue is not None else []
        lines.append(f"  {CYAN}{BOLD}Queued prompts ({len(items)}){RST}")
        if not items:
            lines.append(f"    {SLATE}Empty. Type below and press Enter to queue a follow-up.{RST}")
        for i, it in enumerate(items, 1):
            tag = f"{MINT}next{RST}" if i == 1 else f"{DARK_SLATE}#{it.id}{RST}"
            lines.append(f"    {tag} {WHITE}{plat.truncate_ansi(it.text.replace(chr(10), ' '), width - 12)}{RST}")
        lines.append(f"    {DARK_SLATE}/q <text> add · /q drop <id> remove · /q clear empty{RST}")
    else:
        task = next((t for t in tasks if t.index == view), None)
        if task is None:
            lines.append(f"  {SLATE}Subagent #{view} is not active.{RST}")
        else:
            icon, status = _status_badge(task)
            lines.append(f"  {icon} {CYAN}{BOLD}#{task.index} {task.title}{RST}  {status}")
            for log in (task.live_logs[-8:] if task.live_logs else ["(starting…)"]):
                lines.append(f"    {DARK_SLATE}│{RST} {WHITE}{log}{RST}")
    lines.append(f"  {DARK_SLATE}{'─' * width}{RST}")
    lines.append(f"  {DARK_SLATE}0-9 view · Q queue · tab/←→ cycle · esc main · enter queue · /btw side question{RST}")
    lines.append(f"  {CYAN}{BOLD}›{RST} {buf}")
    return lines


def run_live_subagent_monitor(future_map: dict[Any, int], subagents_mgr: Any, agent: Any = None) -> None:
    """Block until all futures finish, showing the interactive monitor when on a terminal."""
    if not plat.is_interactive():
        while any(not fut.done() for fut in future_map):
            time.sleep(0.1)
        return
    try:
        from axon.ui.in_flight import paused
    except ImportError:  # pragma: no cover
        import contextlib
        paused = contextlib.nullcontext  # type: ignore[assignment]

    view: Any = 0
    buf = ""
    frame = LiveFrame()
    last_lines: list[str] = []
    last_draw = 0.0
    with paused(), plat.raw_terminal(isig=True):
        try:
            while any(not fut.done() for fut in future_map):
                tasks = list(subagents_mgr.all_tasks())
                key = plat.read_key(timeout=0.1)
                if key:
                    if key in ("enter", "ctrl-j"):
                        line = buf.strip()
                        buf = ""
                        if line:
                            frame.clear()
                            last_lines = []
                            with plat.cooked_terminal():
                                _submit(line, agent)
                    elif key == "backspace":
                        buf = buf[:-1]
                    elif key == "ctrl-u":
                        buf = ""
                    elif key == "esc":
                        if buf:
                            buf = ""
                        else:
                            view = 0
                    elif key in ("tab", "right", "down") and not (buf and key == "right"):
                        view = _cycle(view, 1, tasks)
                    elif key in ("shift-tab", "left", "up") and not (buf and key == "left"):
                        view = _cycle(view, -1, tasks)
                    elif key.startswith(plat.PASTE_PREFIX):
                        buf += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
                    elif not buf and key in ("q", "Q"):
                        view = "Q"
                    elif not buf and key in ("m", "M"):
                        view = 0
                    elif not buf and key.isdigit() and len(key) == 1:
                        n = int(key)
                        if n == 0 or any(t.index == n for t in tasks):
                            view = n
                    elif len(key) == 1 and key >= " ":
                        buf += key
                cols = plat.term_size()[0]
                lines = build_monitor_frame(view, tasks, agent, buf, min(88, max(20, cols - 4)))
                now = time.monotonic()
                if lines != last_lines or key == "resize" or now - last_draw > 1.0:
                    frame.render(lines, (len(lines) - 1, 4 + plat.str_width(buf)))
                    last_lines, last_draw = lines, now
        finally:
            frame.clear()


def _submit(line: str, agent: Any) -> None:
    try:
        if line.startswith("/"):
            from axon.commands.builtin import dispatch_command
            if agent is not None:
                dispatch_command(line, agent)
        elif agent is not None and hasattr(agent, "message_queue"):
            item = agent.message_queue.push(line)
            print(f"  {MINT}✓ Queued #{item.id}:{RST} {WHITE}{line}{RST} {SLATE}(runs after the subagents finish){RST}")
    except Exception as e:
        print(f"  {ROSE}✗ Command error: {e}{RST}")
