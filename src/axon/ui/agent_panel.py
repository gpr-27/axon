"""
The console: the agent list and transcripts of subagent_monitor.py, opened from the idle prompt (/subagents, /tasks).

/tasks lists background shells and background turns beside the subagents, with the same keys: Enter opens the
output of a shell or a turn, x stops it. They appear as rows with the fields of a subagent task, so the list and
the transcript do not care what they show. ``open_console`` is also what a key at the prompt calls.
"""
from __future__ import annotations

import time
from typing import Any

from axon import platform as plat
from axon.ui.subagent_monitor import MonitorKeys, _keep_unsent, _visible, drive

_active = False
_cleared: set[str] = set()     # shells and turns cleared from the list with x (the session keeps them)
_QUIET_EVENTS = ("SubagentEvent", "SubagentStep")     # a held retry is the main agent's own: it is printed on close


class ShellRow:
    """A background shell as a row of the list."""
    kind = "shell"
    tool_calls = None
    tokens_consumed = 0
    steps = max_steps = retries = resumes = runs = 0
    agent_type = "shell"
    model = prompt = result_text = ""
    timeline: list[Any] = []

    def __init__(self, shell: Any, number: int) -> None:
        self.shell = shell
        self.id = self.label = shell.id
        self.index = 10_000 + number          # never a subagent's number, so digit keys only open subagents
        self.title = " ".join(str(shell.command).split())
        state = str(shell.status)
        killed = state.endswith(("code -15", "code -9"))
        self.status = "running" if state == "running" else ("completed" if state.endswith("code 0") else "cancelled" if killed else "error")
        self.error_msg = state if self.status == "error" else None
        self.start_time = float(shell.started or 0.0) if self.status == "running" else 0.0
        self.elapsed_s = max(0.0, time.time() - float(shell.started or 0.0))
        tail = self.transcript()
        self.activity = tail[-1] if tail else ""
        if self.status == "completed":
            self.result_text = self.activity or state

    def transcript(self) -> list[str]:
        with self.shell.stream.lock:
            text = self.shell.stream.buf
        return [ln for ln in str(text).splitlines() if ln.strip()][-400:]

    def stop(self) -> bool:
        from axon.tools.shell import BACKGROUND
        BACKGROUND.kill(self.shell.id)
        return True

    def clear(self) -> None:
        _cleared.add(self.shell.id)


class TurnRow:
    """A turn detached with Ctrl+B as a row of the list."""
    kind = "turn"
    tool_calls = None
    tokens_consumed = 0
    steps = max_steps = retries = resumes = runs = 0
    agent_type = "turn"
    model = result_text = ""
    timeline: list[Any] = []

    def __init__(self, tid: str, rec: Any, number: int) -> None:
        self.rec = rec
        self.id = self.label = str(tid)
        self.index = 20_000 + number
        self.prompt = str(getattr(rec, "prompt", ""))
        self.title = " ".join(self.prompt.split()) or self.id
        state = str(getattr(rec, "status", "running"))
        self.status = {"running": "running", "completed": "completed", "interrupted": "cancelled"}.get(state, "error")
        self.error_msg = str(getattr(rec, "error", "") or state) if self.status == "error" else None
        self.start_time = float(getattr(rec, "started_at", 0.0) or 0.0) if self.status == "running" else 0.0
        self.elapsed_s = float(getattr(rec, "elapsed_s", 0.0) or 0.0)
        log = list(getattr(rec, "log", []) or [])
        self.activity = str(log[-1]) if log else ""
        final = str(getattr(getattr(rec, "result", None), "final_text", "") or "")
        if self.status == "completed":
            self.result_text = final or self.activity

    def clear(self) -> None:
        _cleared.add(self.id)

    def transcript(self) -> list[str]:
        lines = list(getattr(self.rec, "log", []) or [])
        final = str(getattr(getattr(self.rec, "result", None), "final_text", "") or "")
        return lines + (["", *final.splitlines()] if final else [])


def shell_rows() -> list[Any]:
    try:
        from axon.tools.shell import BACKGROUND
        return [ShellRow(sh, i) for i, sh in enumerate(BACKGROUND.all()) if sh.id not in _cleared]
    except Exception:
        return []


def turn_rows(agent: Any) -> list[Any]:
    turns = getattr(agent, "background_tasks", None)
    if not isinstance(turns, dict):
        return []
    return [TurnRow(tid, rec, i) for i, (tid, rec) in enumerate(list(turns.items())) if str(tid) not in _cleared]


def subagent_rows(agent: Any) -> list[Any]:
    mgr = getattr(agent, "subagents", None)
    try:
        return _visible(list(mgr.all_tasks())) if mgr is not None else []
    except Exception:
        return []


def task_rows(agent: Any) -> list[Any]:
    """Everything /tasks lists, in a stable order (a row must not jump while it is being selected)."""
    return shell_rows() + turn_rows(agent) + subagent_rows(agent)


def available() -> bool:
    """The console needs the whole terminal: not without one, not inside itself, not beside a running turn."""
    from axon.agent.subagent import SCREEN_GATE
    if _active or SCREEN_GATE.attached() or not plat.is_interactive():
        return False
    try:
        from axon.ui import in_flight
        return not list(getattr(in_flight, "_active", []) or [])
    except Exception:
        return True


def open_console(agent: Any, *, everything: bool = False, target: Any = None) -> bool:
    """Run the list until Esc. *everything* adds shells and background turns (/tasks); *target* opens that row's
    transcript first. False when the console cannot open here: the caller then prints a table instead."""
    global _active
    if not available():
        return False
    rows = (lambda: task_rows(agent)) if everything else (lambda: subagent_rows(agent))
    keys = MonitorKeys(agent, console=True, title="Tasks" if everything else "Subagents")
    if target is not None:
        keys.view = target.index
        current = rows()
        keys.selected = next((i for i, t in enumerate(current) if t.index == target.index), 0)
    _active = True
    held = getattr(agent, "_held_events", False) is None     # the main agent prints nothing while this owns the screen
    if held:
        agent._held_events = []
    try:
        with plat.raw_terminal(isig=True):
            try:
                drive(keys, rows, agent, lambda: True)
            except KeyboardInterrupt:
                pass
    finally:
        _active = False
        if held:
            events, agent._held_events = agent._held_events or [], None
            for ev in events:         # for the trace; their lines were this screen's rows
                agent._deliver(ev, show=type(ev).__name__ not in _QUIET_EVENTS, held=True)
    if keys.buf.strip():
        _keep_unsent(keys.buf)
    return True
