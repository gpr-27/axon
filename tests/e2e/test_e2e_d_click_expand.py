"""
Agent-D e2e: click a block in the normal scrolling output to expand it.

The pty is wrapped in a tiny VT emulator (``MiniTerm``) that keeps a real screen grid and answers
cursor-position queries (CSI 6n) the way a terminal does, so the test exercises the real path:
the prompt asks where it is, the user clicks the row where the tool call is drawn, the transcript
viewer opens on that tool with its full output, a second click collapses it, ``q`` returns to the
prompt with the draft intact and mouse reporting is off again before the next turn.
"""
from __future__ import annotations

import re
import time
import unicodedata

import pytest

from harness import AxonEnv, strip_ansi
from test_e2e_d_helpers import TRACEBACK, Pty, posix_only, tool_turn

pytestmark = [pytest.mark.e2e, posix_only]

ROWS, COLS = 30, 100
MOUSE_ON, MOUSE_OFF = "\x1b[?1000h", "\x1b[?1000l"


class MiniTerm:
    """Just enough of a VT100/xterm to know what is on which row (main + alternate screen)."""

    def __init__(self, rows: int, cols: int, answer) -> None:  # noqa: ANN001
        self.h, self.w = rows, cols
        self.answer = answer
        self.grid = [[" "] * cols for _ in range(rows)]
        self.r = self.c = 0
        self.saved: tuple | None = None
        self.alt = False
        self._buf = ""

    def _blank(self) -> list[str]:
        return [" "] * self.w

    def _lf(self) -> None:
        if self.r == self.h - 1:
            self.grid.pop(0)
            self.grid.append(self._blank())
        else:
            self.r += 1

    def _put(self, ch: str) -> None:
        wide = unicodedata.east_asian_width(ch) in ("W", "F")
        if unicodedata.combining(ch) or ch in "​️":
            return
        if self.c + (2 if wide else 1) > self.w:
            self.c = 0
            self._lf()
        self.grid[self.r][self.c] = ch
        self.c += 1
        if wide and self.c < self.w:
            self.grid[self.r][self.c] = ""
            self.c += 1

    def feed(self, text: str) -> None:
        s = self._buf + text
        self._buf = ""
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\x1b":
                if i + 1 >= len(s):
                    self._buf = s[i:]
                    return
                nxt = s[i + 1]
                if nxt == "[":
                    m = re.compile(r"\x1b\[([0-9;?<>=]*)([ -/]*)([@-~])").match(s, i)
                    if not m:
                        self._buf = s[i:]
                        return
                    self._csi(m.group(1), m.group(3))
                    i = m.end()
                    continue
                if nxt == "]":
                    end = re.compile(r"\x07|\x1b\\").search(s, i)
                    if not end:
                        self._buf = s[i:]
                        return
                    i = end.end()
                    continue
                i += 2
                continue
            if ch == "\n":
                self._lf()
            elif ch == "\r":
                self.c = 0
            elif ch == "\b":
                self.c = max(0, self.c - 1)
            elif ch == "\t":
                self.c = min(self.w - 1, (self.c // 8 + 1) * 8)
            elif ch >= " ":
                self._put(ch)
            i += 1

    def _csi(self, params: str, final: str) -> None:
        nums = [int(p) if p.isdigit() else 0 for p in params.lstrip("?").split(";")] if params else []
        n = nums[0] if nums and nums[0] else 1
        if params.startswith("?"):
            if final in "hl" and nums and nums[0] in (1049, 1047, 47):
                if final == "h" and not self.alt:
                    self.saved = ([row[:] for row in self.grid], self.r, self.c)
                    self.grid = [self._blank() for _ in range(self.h)]
                    self.alt = True
                elif final == "l" and self.alt and self.saved:
                    self.grid, self.r, self.c = self.saved
                    self.alt = False
            return
        if final == "A":
            self.r = max(0, self.r - n)
        elif final == "B":
            self.r = min(self.h - 1, self.r + n)
        elif final == "C":
            self.c = min(self.w - 1, self.c + n)
        elif final == "D":
            self.c = max(0, self.c - n)
        elif final in "Hf":
            self.r = min(self.h - 1, max(1, nums[0] if nums else 1) - 1)
            self.c = min(self.w - 1, max(1, nums[1] if len(nums) > 1 else 1) - 1)
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode in (2, 3):
                self.grid = [self._blank() for _ in range(self.h)]
            else:
                self.grid[self.r][self.c:] = [" "] * (self.w - self.c)
                for rr in range(self.r + 1, self.h):
                    self.grid[rr] = self._blank()
        elif final == "K":
            mode = nums[0] if nums else 0
            if mode == 2:
                self.grid[self.r] = self._blank()
            elif mode == 1:
                self.grid[self.r][: self.c + 1] = [" "] * (self.c + 1)
            else:
                self.grid[self.r][self.c:] = [" "] * (self.w - self.c)
        elif final == "n" and params == "6":
            self.answer(f"\x1b[{self.r + 1};{self.c + 1}R")

    def rows(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]

    def find(self, pattern: str) -> int | None:
        """1-based row of the last screen row matching ``pattern``."""
        hits = [i for i, row in enumerate(self.rows(), 1) if re.search(pattern, row)]
        return hits[-1] if hits else None


class TermPty(Pty):
    """Pty whose output also drives a MiniTerm that answers CSI 6n."""

    def __init__(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        super().__init__(*a, **kw)
        self.term = MiniTerm(kw.get("rows", ROWS), kw.get("cols", COLS), self.send)
        self._fed = 0

    def _pump(self, timeout: float) -> bool:
        got = super()._pump(timeout)
        if len(self._raw) > self._fed:
            chunk, self._fed = self._raw[self._fed:], len(self._raw)
            self.term.feed(chunk.decode("utf-8", "replace"))
        return got


def _wait(s: TermPty, pred, timeout: float = 10.0, what: str = "") -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s.drain(0.1)
        if pred():
            return
    pytest.fail(f"timed out waiting for {what}:\n" + "\n".join(s.term.rows()))


def test_click_on_tool_row_expands_it_in_the_transcript_and_q_returns_with_draft(axon_env: AxonEnv) -> None:
    """At the idle prompt a click on the tool call row opens the transcript on that call with its full
    output; clicking it again collapses it; q returns to the untouched prompt with the draft."""
    cmd = "for i in $(seq 1 30); do echo line-$i; done"
    axon_env.llm.enqueue(*tool_turn(cmd, "CLICK-DONE"))
    s = TermPty(["--mode", "bypass"], {**axon_env.env, "AXON_NO_TIPS": "1", "AXON_MOUSE": "1"}, axon_env.workspace,
                rows=ROWS, cols=COLS)
    try:
        s.ready(timeout=20)
        s.turn("go")
        _wait(s, lambda: MOUSE_ON in s.raw_since(0)[s.raw_since(0).rfind("CLICK-DONE"):], what="mouse reporting at the prompt")
        s.type("my draft")
        main_before = s.term.rows()
        y = s.term.find(r"● Bash .*line-\$i")
        assert y is not None, "\n".join(main_before)
        assert not any("line-15" in r for r in main_before)  # the scrollback shows only a preview

        mark = len(s._raw)
        s.send(f"\x1b[<0;10;{y}M\x1b[<0;10;{y}m")
        _wait(s, lambda: s.term.alt and any("line-15" in r for r in s.term.rows()), what="the viewer with the full output")
        rows = s.term.rows()
        assert any("Transcript" in r for r in rows[:1])
        sel = next(i for i, r in enumerate(rows, 1) if "Bash" in r and "line-$i" in r)
        assert rows[sel - 1].lstrip().startswith("❯")       # the clicked tool call is selected
        assert any(re.search(r"\b15 │ line-15", r) for r in rows)

        s.send(f"\x1b[<0;10;{sel}M\x1b[<0;10;{sel}m")         # click again: collapses
        _wait(s, lambda: not any("line-15" in r for r in s.term.rows()) and any("Bash" in r for r in s.term.rows()),
              what="the tool call to collapse")

        s.send("q")
        _wait(s, lambda: not s.term.alt and any("my draft" in r for r in s.term.rows()), what="the prompt to return")
        after = s.term.rows()
        assert after[: y] == main_before[: y]                # main screen untouched
        tail = s.raw_since(mark)
        assert "\x1b[?1049l" in tail
        assert not re.search(r"\[<\d+;\d+;\d+[Mm]|\d+;\d+R", strip_ansi(tail)), "escape text leaked"

        # submit the draft: mouse reporting is turned off before the turn runs
        axon_env.llm.enqueue([{"text": "SECOND"}])
        m, shown = len(s._raw), s.mark()
        s.send(" more\r")
        s.expect(r"SECOND", timeout=20, since=shown)
        run = s.raw_since(m)
        off = run.find(MOUSE_OFF)
        assert off != -1 and off < run.find("SECOND"), "mouse reporting still on during the turn"
        s.ready(since=shown)
        assert TRACEBACK not in s.screen and "my draft more" in s.screen
    finally:
        s.close()


def test_mouse_setting_off_emits_no_mouse_or_cpr_sequences(axon_env: AxonEnv) -> None:
    """AXON_NO_MOUSE=1: the prompt never asks for the cursor position nor enables mouse reporting."""
    axon_env.llm.enqueue(*tool_turn("echo hi", "OFF-DONE"))
    env = {**axon_env.env, "AXON_NO_MOUSE": "1"}
    s = TermPty(["--mode", "bypass"], env, axon_env.workspace, rows=ROWS, cols=COLS)
    try:
        s.ready(timeout=20)
        s.turn("go")
        raw = s.raw_since(0)
        assert "\x1b[6n" not in raw and MOUSE_ON not in raw
        assert "click a block to expand" not in s.screen
    finally:
        s.close()


def test_by_default_the_mouse_is_left_to_the_terminal(axon_env: AxonEnv) -> None:
    """Default: no mouse capture, so the wheel scrolls the terminal's history and drag selects text."""
    axon_env.llm.enqueue(*tool_turn("echo hi", "DEFAULT-DONE"))
    s = TermPty(["--mode", "bypass"], {**axon_env.env}, axon_env.workspace, rows=ROWS, cols=COLS)
    try:
        s.ready(timeout=20)
        s.turn("go")
        raw = s.raw_since(0)
        assert MOUSE_ON not in raw and "\x1b[6n" not in raw
        assert "click a block to expand" not in s.screen
    finally:
        s.close()


def test_banner_mentions_click_to_expand(axon_env: AxonEnv) -> None:
    """When click-to-expand is on, the banner says blocks can be clicked (and how to still select text)."""
    s = TermPty([], {**axon_env.env, "AXON_MOUSE": "1"}, axon_env.workspace, rows=ROWS, cols=COLS)
    try:
        s.ready(timeout=20)
        assert "click a block to expand" in s.screen and "drag selects text" in s.screen
    finally:
        s.close()
