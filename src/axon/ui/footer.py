"""
Pinned footer for a running turn.

While the agent works, the bottom rows of the terminal stay fixed: a rule, the input box (the
type-ahead draft), a rule, the status line (model, mode, context, cost) and the agent roster.
Output keeps scrolling above them because the terminal's scroll region (DECSTBM) is limited to
the rows over the footer. Every write happens under ``OUTPUT_LOCK`` and brackets the cursor with
save/restore, so the renderer and spinner never see the footer.

Set ``AXON_PINNED_INPUT=0`` to turn it off.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable

from axon import platform as plat
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, LBLUE, OUTPUT_LOCK, RST, sanitize, strip_ansi, truncate


def _plain(text: object) -> str:
    """Subagent text (model words, tool output) on one row, without terminal escapes."""
    return " ".join(sanitize(strip_ansi(str(text or ""))).split())


_SAVE, _RESTORE = "\0337", "\0338"
_MAX_ROWS = 16


def enabled() -> bool:
    return (os.environ.get("AXON_PINNED_INPUT", "1") != "0" and sys.platform != "win32"
            and plat.is_interactive())


class PinnedFooter:
    def __init__(self, agent: Any, draft: Callable[[], str], interval: float = 0.5) -> None:
        self.agent = agent
        self._draft = draft
        self._interval = interval
        self._height = 0              # rows reserved at the bottom (0 = not installed)
        self._size = (0, 0)
        self._active = False
        self._suspended = False
        self._stop = threading.Event()
        self._last_sample = 0.0
        self._wake = threading.Event()            # set by poke(): repaint now instead of at the next tick
        self._status_cache: tuple[float, str] = (0.0, "")
        self._thread: threading.Thread | None = None
        self._out = sys.__stdout__
        self._notice = ""
        self._notice_until = 0.0
        self._focus: int | None = None            # selected agent row (index into the roster order)
        self._peek: str | None = None             # subagent whose live feed is open
        self._history: dict[str, list[int]] = {}   # token totals over time, per subagent (the chart)

    # ── content ────────────────────────────────────────────────────────────
    def height(self) -> int:
        return self._height

    def notice(self, text: str, secs: float = 3.0) -> None:
        """Short message on the top rule (e.g. "copied 661 chars to clipboard")."""
        self._notice, self._notice_until = text, time.monotonic() + secs
        self.refresh()

    # ── agent selection / peek ─────────────────────────────────────────────
    @property
    def reply_to(self) -> str | None:
        """Id of the subagent whose feed is open: what you type goes to it."""
        return self._peek

    def _order(self) -> list[Any]:
        """The agents listed under the input bar: the running ones."""
        from axon.ui.statusline import running_tasks
        return running_tasks(self.agent)

    def nav(self, key: str, draft_empty: bool) -> bool:
        """Keyboard control of the agent rows. True when the key was used."""
        order = self._order()
        if self._focus is not None and self._focus >= len(order):
            self._focus = len(order) - 1 if order else None
        if self._peek is not None:
            if key in ("esc", "left"):
                self._peek = None
                self.refresh()
                return True
            return False
        if self._focus is None:
            if key == "down" and draft_empty and order:
                self._focus = 0
                self.refresh()
                return True
            return False
        if not order:
            self._focus = None
            return False
        if key == "down":
            self._focus = min(len(order) - 1, self._focus + 1)
        elif key == "up":
            self._focus = None if self._focus == 0 else self._focus - 1
        elif key in ("enter", "right"):
            self._peek = order[self._focus].id
        elif key in ("esc", "left"):
            self._focus = None
        elif key == "x":
            task = order[self._focus]
            mgr = getattr(self.agent, "subagents", None)
            if mgr is not None and task.status == "running" and mgr.cancel(task.id):
                self.notice(f"stopped {task.id}")
        else:
            self._focus = None            # any other key goes back to typing
            self.refresh()
            return False
        self.refresh()
        return True

    def _sample(self) -> None:
        from axon.ui.statusline import ordered_tasks
        for t in ordered_tasks(self.agent):
            hist = self._history.setdefault(t.id, [])
            hist.append(int(t.tokens_consumed or 0))
            del hist[:-60]

    def _peek_rows(self, task: Any, w: int) -> list[str]:
        """Live feed of one subagent: status, token-rate chart and its latest steps."""
        from axon.ui.statusbar import generate_sparkline
        from axon.ui.theme import GOLD, MINT, ROSE, SLATE, WHITE
        colour = {"running": GOLD, "completed": MINT}.get(task.status, ROSE)
        secs = (time.time() - task.start_time) if task.status == "running" and task.start_time else float(task.elapsed_s or 0.0)
        hist = self._history.get(task.id, [])
        rate = [max(0, b - a) for a, b in zip(hist, hist[1:])][-40:]
        tok = int(task.tokens_consumed or 0)
        mark = "◐◓◑◒"[int(time.monotonic() * 6) % 4] + " " if task.status == "running" else ""
        rows = [truncate(f"  {CYAN}◂ esc{RST} {WHITE}{task.id}{RST} {SLATE}{_plain(task.agent_type)}{RST} {colour}{mark}{task.status}{RST}"
                         f" {SLATE}· {int(secs // 60)}m {int(secs % 60):02d}s · {task.steps}/{task.max_steps} steps · "
                         f"{task.tool_calls} tools · {tok / 1000:.1f}k tokens{RST}", w),
                f"  {SLATE}tokens/s{RST} {CYAN}{generate_sparkline(rate) or '·'}{RST}"]
        glyph = {"step": "◆", "tool": "▸", "result": "↳", "thinking": "…", "retry": "↻", "resume": "↻",
                 "error": "✗", "note": "✎"}
        entries = list(task.timeline)[-8:] or [{"kind": "step", "text": task.activity or task.title, "is_error": False}]
        for e in entries:
            c = ROSE if e.get("is_error") else SLATE
            rows.append(truncate(f"  {c}{glyph.get(e['kind'], '·')} {_plain(e['text'])}{RST}", w))
        if task.status != "running" and task.result_text:
            rows.append(truncate(f"  {MINT}result:{RST} {SLATE}{_plain(task.result_text)}{RST}", w))
        return rows

    def build(self, cols: int) -> list[str]:
        w = max(10, cols - 1)
        note = self._notice if time.monotonic() < self._notice_until else ""
        if note:
            tail = f" {note} "
            rule = f"{DARK_SLATE}{'─' * max(2, w - 2 - len(tail))}{RST}{LBLUE}{tail}{RST}"
        else:
            rule = f"{DARK_SLATE}{'─' * (w - 2)}{RST}"
        draft = self._draft() or ""
        from axon.ui.statusline import ordered_tasks
        target = next((t for t in ordered_tasks(self.agent) if t.id == self._peek), None)   # a finished one stays open
        if self._peek and target is None:
            self._peek = None
        lead = f"{CYAN}{BOLD}[{target.id}] ❯{RST}" if target is not None else f"{CYAN}❯{RST}"
        if draft:
            text = draft.replace("\n", " ⏎ ")
            room = w - 6 - (len(target.id) + 4 if target is not None else 0)
            text = text if len(text) <= room else "…" + text[-(room - 1):]
            box = f" {lead} {text}{CYAN}▏{RST}"
        elif target is not None:
            box = f" {lead} {DARK_SLATE}type a message for {target.id} · enter sends it · esc back{RST}"
        else:
            box = f" {lead} {DARK_SLATE}type to queue a message · enter sends it between steps · esc interrupts{RST}"
        rows = [f" {rule}", truncate(box, w), f" {rule}"]
        try:
            from axon.ui.statusline import agent_panel_rows
            rows.append(truncate(self._status_line(w), w))
            if target is not None:
                rows += self._peek_rows(target, w)
            else:
                panel = agent_panel_rows(self.agent, w, max_rows=4, selected=self._focus)
                rows += panel
        except Exception:
            pass
        return rows[:_MAX_ROWS]

    # ── terminal plumbing (callers hold OUTPUT_LOCK) ───────────────────────
    def _write(self, text: str) -> None:
        try:
            self._out.write(text)
            self._out.flush()
        except Exception:
            pass

    def _install(self, rows: int, size: tuple[int, int]) -> None:
        cols, lines = size
        self._write("\n" * rows + f"\033[{rows}A" + _SAVE + f"\033[1;{lines - rows}r" + _RESTORE)
        self._height, self._size = rows, size

    def _remove(self) -> None:
        if not self._height:
            return
        cols, lines = self._size
        clear = "".join(f"\033[{lines - self._height + 1 + i};1H\033[2K" for i in range(self._height))
        self._write(_SAVE + clear + "\033[r" + _RESTORE)
        self._height = 0

    def _paint_locked(self) -> None:
        if not self._active or self._suspended:
            return
        size = plat.term_size()
        rows = self.build(size[0])
        if size[1] < 12:                       # too short a window: leave the terminal alone
            self._remove()
            return
        if len(rows) != self._height or size != self._size:
            self._remove()
            self._install(len(rows), size)
        cols, lines = self._size
        body = "".join(f"\033[{lines - self._height + 1 + i};1H\033[2K{row}" for i, row in enumerate(rows))
        self._write(_SAVE + body + _RESTORE)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        with OUTPUT_LOCK:
            self._active = True
            self._paint_locked()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="PinnedFooter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with OUTPUT_LOCK:
            self._active = False
            self._remove()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None

    def suspend(self) -> None:
        """Give the whole terminal back (approval prompts, pickers)."""
        with OUTPUT_LOCK:
            self._suspended = True
            self._remove()

    def resume(self) -> None:
        with OUTPUT_LOCK:
            self._suspended = False
            self._paint_locked()

    def poke(self) -> None:
        """Ask the footer thread to repaint soon (cheap; safe from the key-reading thread)."""
        self._wake.set()

    def _status_line(self, w: int) -> str:
        """The status line, rebuilt at most every 1.5 s (it runs git and reads the ledger)."""
        from axon.ui.statusline import default_status_line
        now = time.monotonic()
        if now - self._status_cache[0] > 1.5 or not self._status_cache[1]:
            self._status_cache = (now, default_status_line(self.agent, w, include_mode=True))
        return self._status_cache[1]

    def refresh(self) -> None:
        with OUTPUT_LOCK:
            self._paint_locked()

    def _run(self) -> None:
        while not self._stop.is_set():
            animating = bool(self._order()) if self._height else False   # running agents: redraw at ~7 fps
            woke = self._wake.wait(0.15 if animating else self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                if not woke and time.monotonic() - self._last_sample >= self._interval:
                    self._last_sample = time.monotonic()
                    self._sample()
                self.refresh()
            except Exception:
                pass
