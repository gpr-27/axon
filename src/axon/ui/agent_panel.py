"""
The agent panel: the running subagents listed under the input, with keyboard control.

One panel per agent session, shared by the pinned footer (while a response streams) and the idle
prompt, so the selection and the open feed carry over between the two. ``↓`` on an empty input
selects the first agent (a ``❯`` marks it), ``↑``/``↓`` move, ``Enter`` opens its live feed,
``x`` stops it, ``Esc``/``←`` go back. With a feed open, what you type is sent to that agent.
"""
from __future__ import annotations

import time
from typing import Any

from axon.ui.theme import CYAN, RST, truncate


class AgentPanel:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.focus: int | None = None          # selected row (index into the running agents)
        self.peek: str | None = None           # task id whose live feed is open
        self.message = ""                      # one-line result of the last action (for the caller to show)
        self._history: dict[str, list[int]] = {}   # token totals over time, per subagent (the chart)

    # ── state ──────────────────────────────────────────────────────────────
    def running(self) -> list[Any]:
        from axon.ui.statusline import running_tasks
        return running_tasks(self.agent)

    def active(self) -> bool:
        """Something to show or control: running agents, or a feed that is open."""
        return bool(self.running()) or self.peek is not None

    @property
    def reply_to(self) -> str | None:
        """Id of the agent whose feed is open: what you type goes to it."""
        return self.peek

    def sample(self) -> None:
        from axon.ui.statusline import ordered_tasks
        for t in ordered_tasks(self.agent):
            hist = self._history.setdefault(t.id, [])
            hist.append(int(t.tokens_consumed or 0))
            del hist[:-60]

    def send(self, text: str) -> bool:
        """Hand *text* to the agent whose feed is open."""
        mgr = getattr(self.agent, "subagents", None)
        task = mgr.send(self.peek, text) if mgr is not None and self.peek and hasattr(mgr, "send") else None
        self.message = f"sent to {self.peek}" if task is not None else f"{self.peek} has finished"
        return task is not None

    # ── keys ───────────────────────────────────────────────────────────────
    def nav(self, key: str, draft_empty: bool) -> bool:
        """True when the key was used by the panel."""
        order = self.running()
        if self.focus is not None and self.focus >= len(order):
            self.focus = len(order) - 1 if order else None
        if self.peek is not None:
            if key in ("esc", "left"):
                self.peek = None
                return True
            return False
        if self.focus is None:
            if key == "down" and draft_empty and order:
                self.focus = 0
                return True
            return False
        if not order:
            self.focus = None
            return False
        if key == "down":
            self.focus = min(len(order) - 1, self.focus + 1)
        elif key == "up":
            self.focus = None if self.focus == 0 else self.focus - 1
        elif key in ("enter", "right"):
            self.peek = order[self.focus].id
        elif key in ("esc", "left"):
            self.focus = None
        elif key == "x":
            task = order[self.focus]
            mgr = getattr(self.agent, "subagents", None)
            if mgr is not None and task.status == "running" and mgr.cancel(task.id):
                self.message = f"stopped {task.id}"
        else:
            self.focus = None             # any other key goes back to typing
            return False
        return True

    # ── rows ───────────────────────────────────────────────────────────────
    def target(self) -> Any:
        """The task whose feed is open (it stays open after it finishes), or None."""
        from axon.ui.statusline import ordered_tasks
        task = next((t for t in ordered_tasks(self.agent) if t.id == self.peek), None)
        if self.peek and task is None:
            self.peek = None
        return task

    def rows(self, width: int) -> list[str]:
        """The roster (with the ``❯`` on the selected agent) or the open feed."""
        from axon.ui.statusline import agent_panel_rows
        task = self.target()
        if task is not None:
            return self._feed_rows(task, width)
        return agent_panel_rows(self.agent, width, max_rows=4, selected=self.focus)

    def _feed_rows(self, task: Any, w: int) -> list[str]:
        from axon.ui.statusbar import generate_sparkline
        from axon.ui.theme import GOLD, MINT, ROSE, SLATE, WHITE
        colour = {"running": GOLD, "completed": MINT}.get(task.status, ROSE)
        secs = (time.time() - task.start_time) if task.status == "running" and task.start_time else float(task.elapsed_s or 0.0)
        hist = self._history.get(task.id, [])
        rate = [max(0, b - a) for a, b in zip(hist, hist[1:])][-40:]
        tok = int(task.tokens_consumed or 0)
        mark = "◐◓◑◒"[int(time.monotonic() * 6) % 4] + " " if task.status == "running" else ""
        rows = [truncate(f"  {CYAN}◂ esc{RST} {WHITE}{task.id}{RST} {SLATE}{task.agent_type}{RST} {colour}{mark}{task.status}{RST}"
                         f" {SLATE}· {int(secs // 60)}m {int(secs % 60):02d}s · {task.steps}/{task.max_steps} steps · "
                         f"{task.tool_calls} tools · {tok / 1000:.1f}k tokens{RST}", w),
                f"  {SLATE}tokens/s{RST} {CYAN}{generate_sparkline(rate) or '·'}{RST}"]
        glyph = {"step": "◆", "tool": "▸", "result": "↳", "thinking": "…", "retry": "↻", "resume": "↻",
                 "error": "✗", "note": "✎"}
        entries = list(task.timeline)[-8:] or [{"kind": "step", "text": task.activity or task.title, "is_error": False}]
        for e in entries:
            c = ROSE if e.get("is_error") else SLATE
            rows.append(truncate(f"  {c}{glyph.get(e['kind'], '·')} {str(e['text']).replace(chr(10), ' ')}{RST}", w))
        if task.status != "running" and task.result_text:
            rows.append(truncate(f"  {MINT}result:{RST} {SLATE}{' '.join(task.result_text.split())}{RST}", w))
        return rows


def get_panel(agent: Any) -> AgentPanel:
    """The agent session's panel (created on first use, shared by the footer and the prompt)."""
    panel = getattr(agent, "_agent_panel", None)
    if not isinstance(panel, AgentPanel):
        panel = AgentPanel(agent)
        try:
            agent._agent_panel = panel
        except Exception:
            pass
    return panel
