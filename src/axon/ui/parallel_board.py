"""
The live block for parallel work: what the terminal shows while several tool calls of one step run at once.

    ⠹ 4 calls running in parallel · 2 done · esc to interrupt
      ✓ Read    src/axon/ui/render.py                 0.3s    4.1 KB
      ⠹ Grep    "def _on_batch"                         2s
      ⠹ Bash    pytest -q tests/test_ui.py              2s
      ○ Glob    **/*.py

One row per call (state glyph, name, short input, time, result size). The block is drawn in the spinner's live
area, so it is rewritten in place and is gone the moment the step's cards are printed; nothing of it reaches the
scrollback. Where the terminal is not interactive, or is too narrow, the spinner shows its one status row and the
finished cards are the only record (the same as before this block existed).

Pure functions (``call_rows``, ``header_text``, ``span_bar``) build the text; ``ParallelBoard`` holds the state the
renderer feeds it and is safe to read from the spinner thread.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from axon.ui.theme import ERROR, MUTED, PRIMARY, RST, SUBTLE, TEXT, display_width, glyph, middle_ellipsis, pad, sanitize, strip_ansi, truncate

MIN_WIDTH = 40          # narrower than this the block is not drawn
MAX_NAME = 14           # cells of the tool-name column
GLYPH_HZ = 4            # the running glyph turns this often: the block is rewritten at most that many times a second
_STATES = ("queued", "running", "done", "failed")


@dataclass
class CallRow:
    id: str
    name: str
    target: str = ""
    state: str = "queued"
    t0: float = 0.0          # when it started running
    t1: float = 0.0          # when its result came in
    size: int = 0            # bytes of result

    def elapsed(self, now: float) -> float:
        if self.state == "queued" or not self.t0:
            return 0.0
        return max(0.0, (self.t1 or now) - self.t0)


# ─── pure text ─────────────────────────────────────────────────────────────

def fmt_elapsed(seconds: float, running: bool) -> str:
    """Whole seconds while running (the row changes once a second), one decimal when it is done."""
    s = max(0.0, seconds)
    if s >= 3600:
        return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"
    if s >= 60:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s)}s" if running else f"{s:.1f}s"


def fmt_size(n: int) -> str:
    n = max(0, int(n))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _one_line(text: Any) -> str:
    return " ".join(sanitize(str(text or "")).split())


def header_text(rows: list[CallRow]) -> str:
    """'4 calls running in parallel · 2 done' (failures and queued calls are named only when there are some)."""
    n = len(rows)
    done = sum(1 for r in rows if r.state == "done")
    failed = sum(1 for r in rows if r.state == "failed")
    queued = sum(1 for r in rows if r.state == "queued")
    running = n - done - failed - queued
    noun = f"{n} call{'s' if n != 1 else ''}"
    head = f"{noun} running in parallel" if running or queued else f"{noun} finished"
    parts = [head]
    if done:
        parts.append(f"{done} done")
    if failed:
        parts.append(f"{failed} failed")
    if queued:
        parts.append(f"{queued} queued")
    return f" {glyph('dot')} ".join(parts)


def _state_glyph(state: str, frame: int) -> tuple[str, str]:
    if state == "done":
        return glyph("ok"), MUTED
    if state == "failed":
        return glyph("fail"), ERROR
    if state == "queued":
        return glyph("circle"), SUBTLE
    spin = glyph("spinner")
    return spin[frame % len(spin)], PRIMARY


def _call_row(r: CallRow, width: int, now: float, frame: int, name_w: int) -> str:
    g, color = _state_glyph(r.state, frame)
    running = r.state == "running"
    clock = fmt_elapsed(r.elapsed(now), running) if r.state != "queued" else ""
    size = fmt_size(r.size) if r.state in ("done", "failed") and r.size else ""
    lead = f"    {color}{g}{RST} "
    target_w = width - display_width(lead) - name_w - 1 - 2 - 2 - 6 - 9
    target = _one_line(r.target)
    target = ("" if target_w <= 3 else middle_ellipsis(target, target_w) if "/" in target and " " not in target
              else truncate(target, target_w))      # a path keeps its file name, the end that says which one
    name_color = ERROR if r.state == "failed" else (SUBTLE if r.state == "queued" else TEXT)
    body = f"{name_color}{pad(truncate(_one_line(r.name), name_w), name_w)}{RST} "
    body += f"{MUTED}{pad(target, max(0, target_w))}{RST}"
    time_col = f"{MUTED if r.state != 'running' else TEXT}{clock:>6}{RST}"
    size_col = f" {SUBTLE}{size:>8}{RST}" if size else " " * 9
    return truncate(f"{lead}{body}  {time_col}{size_col}".rstrip(), width)


def call_rows(rows: list[CallRow], width: int, now: float, *, frame: int = 0, max_rows: int = 14,
              hint: str = "") -> list[str]:
    """The block: a header row (leading glyph = the spinner) then one row per call. *max_rows* bounds the whole block;
    when the calls do not fit, running and failed ones win and the rest is counted in a last row.
    Empty when there is nothing to show or *width* is below ``MIN_WIDTH``."""
    if not rows or width < MIN_WIDTH:
        return []
    names = [truncate(_one_line(r.name), MAX_NAME) for r in rows]
    name_w = max(display_width(n) for n in names)
    running = any(r.state == "running" for r in rows)
    head_g, head_c = (glyph("spinner")[frame % len(glyph("spinner"))], PRIMARY) if running else (glyph("ok"), MUTED)
    head = f"  {head_c}{head_g}{RST} {TEXT}{header_text(rows)}{RST}"
    if hint and display_width(strip_ansi(head)) + display_width(hint) + 3 <= width:     # the hint yields to the facts
        head += f" {SUBTLE}{glyph('dot')} {hint}{RST}"
    out = [truncate(head, width)]
    room = max(1, max_rows - 1)
    shown = list(range(len(rows)))
    if len(rows) > room:
        keep = max(1, room - 1)
        urgent = [i for i in shown if rows[i].state in ("running", "failed")]
        rest = [i for i in shown if i not in urgent]
        shown = sorted((urgent + rest)[:keep])
    for i in shown:
        out.append(_call_row(rows[i], width, now, frame, name_w))
    hidden = len(rows) - len(shown)
    if hidden:
        out.append(truncate(f"    {SUBTLE}{glyph('ellipsis')} {hidden} more call{'s' if hidden != 1 else ''}{RST}", width))
    return out


# ─── the shared time axis ──────────────────────────────────────────────────

def span_bar(start: float, end: float, t0: float, t1: float, cells: int, fill: str = "", idle: str = "") -> tuple[str, str, str]:
    """(before, filled, after) strings of *cells* cells in total for the span [start, end] on the axis [t0, t1].
    The filled part is at least one cell, so a short call is still visible; every row of a board shares the axis,
    so overlapping work lines up in a column."""
    fill = fill or glyph("hh")
    idle = idle or glyph("hunk")
    cells = max(1, int(cells))
    total = max(1e-9, t1 - t0)
    a = int(math.floor(max(0.0, min(1.0, (start - t0) / total)) * cells))
    b = int(math.ceil(max(0.0, min(1.0, (end - t0) / total)) * cells))
    a = min(a, cells - 1)
    b = max(a + 1, min(cells, b))
    return idle * a, fill * (b - a), idle * (cells - b)


# ─── the state ─────────────────────────────────────────────────────────────

class ParallelBoard:
    """The calls of the current parallel batch. The renderer calls begin / start / finish / end; the spinner calls
    ``frame_rows`` (any thread). Nothing is drawn unless a batch of two or more concurrent calls is active."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._rows: dict[str, CallRow] = {}
        self._order: list[str] = []
        self.active = False

    def begin(self, calls: list[tuple[str, str]]) -> None:
        """A parallel batch starts; *calls* are (id, name) in the order the model asked, all queued until they start."""
        with self._lock:
            self._order = [cid for cid, _ in calls]
            self._rows = {cid: CallRow(id=cid, name=name) for cid, name in calls}
            self.active = len(calls) > 1

    def start(self, call_id: str, name: str, target: str = "") -> None:
        with self._lock:
            row = self._rows.get(call_id)
            if row is None:
                return
            row.name, row.target, row.state, row.t0 = name or row.name, target, "running", self.clock()

    def finish(self, call_id: str, is_error: bool = False, size: int = 0) -> None:
        with self._lock:
            row = self._rows.get(call_id)
            if row is None:
                return
            now = self.clock()
            row.t0 = row.t0 or now
            row.t1, row.state, row.size = now, ("failed" if is_error else "done"), max(0, int(size))
            if all(r.state in ("done", "failed") for r in self._rows.values()):
                self.active = False          # the batch is over: the cards are the record from here

    def end(self) -> None:
        with self._lock:
            self.active = False
            self._rows, self._order = {}, []

    def snapshot(self) -> list[CallRow]:
        with self._lock:
            if not self.active:
                return []
            return [CallRow(**vars(self._rows[i])) for i in self._order if i in self._rows]

    def frame_rows(self, width: int, height: int, hint: str = "") -> list[str] | None:
        """The block for the spinner's live area, or None (the spinner then draws its usual rows)."""
        rows = self.snapshot()
        if len(rows) < 2 or width < MIN_WIDTH or height < 10:
            return None
        now = self.clock()
        frame = int(now * GLYPH_HZ)
        return call_rows(rows, width, now, frame=frame, max_rows=max(4, min(16, height - 8)), hint=hint) or None
