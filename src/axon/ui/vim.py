"""
Vim editing layer for the prompt editor (``settings.vim_mode`` / ``/vim``).

Pure logic over :class:`axon.ui.input.EditorState` (``buf``, ``pos``, ``undo``, ``kill_ring``), so
it is testable with scripted key sequences. Supported in NORMAL mode:

* motions ``h l j k w b e 0 ^ $`` (with counts, e.g. ``3w``)
* operators ``d c y`` + motion (``dw cw de d$ c0 y2w`` ...), linewise ``dd cc yy`` (``2dd``)
* ``x X D C s r<char> p P u``; insert with ``i a I A o O``; ``Esc`` back to NORMAL
* ``j``/``k`` at the first/last line walk prompt history, like the arrow keys

Keys the layer does not own (Enter, Ctrl-combos, arrows, Tab, paste, ...) are left to the
regular editor so submit / history search / mode cycling keep working in both modes.
"""
from __future__ import annotations

from typing import Any

INSERT = "insert"
NORMAL = "normal"

_MOTIONS = set("hlwbe0^$")
_OPERATORS = set("dcy")


def _cls(ch: str) -> int:
    if ch.isspace():
        return 0
    if ch.isalnum() or ch == "_":
        return 1
    return 2


def _line_bounds(buf: str, pos: int) -> tuple[int, int]:
    start = buf.rfind("\n", 0, pos) + 1
    end = buf.find("\n", pos)
    return start, (len(buf) if end == -1 else end)


def word_forward(buf: str, pos: int) -> int:
    n = len(buf)
    i = pos
    if i < n:
        c = _cls(buf[i])
        if c:
            while i < n and _cls(buf[i]) == c:
                i += 1
    while i < n and buf[i].isspace():
        i += 1
    return i


def word_end(buf: str, pos: int) -> int:
    n = len(buf)
    if n == 0:
        return 0
    i = pos + 1
    while i < n and buf[i].isspace():
        i += 1
    if i >= n:
        return n - 1
    c = _cls(buf[i])
    while i + 1 < n and _cls(buf[i + 1]) == c:
        i += 1
    return i


def word_back(buf: str, pos: int) -> int:
    i = pos - 1
    while i > 0 and buf[i].isspace():
        i -= 1
    if i <= 0:
        return 0
    c = _cls(buf[i])
    while i > 0 and _cls(buf[i - 1]) == c:
        i -= 1
    return i


class VimState:
    """Per-prompt vim state. ``handle(st, key)`` returns True when the key was consumed."""

    def __init__(self, mode: str = INSERT) -> None:
        self.mode = mode
        self.count = ""
        self.op: str | None = None
        self.op_count = ""
        self.awaiting: str | None = None    # "r" waiting for the replacement char
        self.register: tuple[str, bool] = ("", False)   # (text, linewise)
        self._insert_mark: int | None = None

    # ── public ────────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        if self.mode == INSERT:
            return "-- INSERT --"
        pending = (self.count or "") + (self.op or "") + (self.op_count or "") + (self.awaiting or "")
        return "-- NORMAL --" + (f" {pending}" if pending else "")

    def reset_pending(self) -> None:
        self.count, self.op, self.op_count, self.awaiting = "", None, "", None

    def handle(self, st: Any, key: str) -> bool:
        if self.mode == INSERT:
            if key == "esc":
                self.to_normal(st)
                return True
            return False
        return self._normal(st, key)

    def to_normal(self, st: Any) -> None:
        self.mode = NORMAL
        self.reset_pending()
        if self._insert_mark is not None:
            # One `u` undoes the whole insert session.
            del st.undo[self._insert_mark + 1:]
            self._insert_mark = None
        start, _ = _line_bounds(st.buf, st.pos)
        if st.pos > start:
            st.pos -= 1
        st.dismissed = True

    def begin_insert(self, st: Any, mark: int | None = None) -> None:
        self.mode = INSERT
        self.reset_pending()
        self._insert_mark = len(st.undo) if mark is None else mark
        st.dismissed = False

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _clamp(st: Any) -> None:
        start, end = _line_bounds(st.buf, st.pos)
        if end > start and st.pos >= end:
            st.pos = end - 1
        st.pos = max(0, min(st.pos, len(st.buf)))

    def _yank(self, st: Any, text: str, linewise: bool) -> None:
        self.register = (text, linewise)
        if text:
            st.kill_ring.append(text)
            del st.kill_ring[:-20]

    def _cut(self, st: Any, start: int, end: int, linewise: bool = False, keep: str | None = None) -> None:
        if start >= end and keep is None:
            return
        st._snapshot()
        self._yank(st, st.buf[start:end] if keep is None else keep, linewise)
        st.buf = st.buf[:start] + st.buf[end:]
        st.pos = start
        st._changed()

    def _motion_target(self, st: Any, m: str, n: int, for_op: bool) -> int:
        buf, pos = st.buf, st.pos
        start, end = _line_bounds(buf, pos)
        if m == "h":
            return max(start, pos - n)
        if m == "l":
            return min(end if for_op else max(start, end - 1), pos + n)
        if m == "0":
            return start
        if m == "^":
            i = start
            while i < end and buf[i] in " \t":
                i += 1
            return i
        if m == "$":
            return end if for_op else max(start, end - 1)
        target = pos
        for _ in range(n):
            if m == "w":
                target = word_forward(buf, target)
            elif m == "b":
                target = word_back(buf, target)
            elif m == "e":
                target = word_end(buf, target)
        return target

    def _linewise_range(self, st: Any, n: int) -> tuple[int, int]:
        start, end = _line_bounds(st.buf, st.pos)
        for _ in range(n - 1):
            if end >= len(st.buf):
                break
            end = _line_bounds(st.buf, end + 1)[1]
        return start, end

    # ── NORMAL mode ───────────────────────────────────────────────────────
    def _normal(self, st: Any, key: str) -> bool:
        if self.awaiting == "r":
            self.awaiting = None
            if len(key) == 1 and key >= " ":
                n = int(self.count or "1")
                start, end = _line_bounds(st.buf, st.pos)
                if st.pos + n <= end:
                    st._snapshot()
                    st.buf = st.buf[:st.pos] + key * n + st.buf[st.pos + n:]
                    st.pos += n - 1
                    st._changed()
            self.reset_pending()
            return True
        if key == "esc":
            if self.count or self.op:
                self.reset_pending()
                return True
            return False  # regular Esc handling (popup dismiss, Esc Esc clear / rewind)
        if len(key) != 1 or key < " ":
            self.reset_pending()
            return False  # Enter, ctrl-*, arrows, tab, paste: regular editor
        # counts
        if key.isdigit() and (key != "0" or (self.op_count if self.op else self.count)):
            if self.op:
                self.op_count += key
            else:
                self.count += key
            return True
        n = int(self.count or "1") * int(self.op_count or "1")
        if self.op:
            op = self.op
            if key == op:            # dd / cc / yy
                self._linewise_op(st, op, n)
            elif key in _MOTIONS:
                self._motion_op(st, op, key, n)
            self.reset_pending()
            if op != "c":
                self._clamp(st)
            return True
        if key in _OPERATORS:
            self.op = key
            return True
        self.reset_pending()
        self._command(st, key, n)
        return True

    def _linewise_op(self, st: Any, op: str, n: int) -> None:
        start, end = self._linewise_range(st, n)
        text = st.buf[start:end]
        if op == "y":
            self._yank(st, text, True)
            return
        if op == "c":
            mark = len(st.undo)
            # keep leading indentation like vim's cc with autoindent off: clear the whole line
            self._cut(st, start, end, linewise=True)
            self.begin_insert(st, mark)
            return
        cut_start, cut_end = start, end
        if cut_end < len(st.buf):
            cut_end += 1          # the line's newline
        elif cut_start > 0:
            cut_start -= 1        # last line: eat the preceding newline
        st._snapshot()
        self._yank(st, text, True)
        st.buf = st.buf[:cut_start] + st.buf[cut_end:]
        st.pos = _line_bounds(st.buf, min(cut_start, len(st.buf)))[0]
        st._changed()

    def _motion_op(self, st: Any, op: str, m: str, n: int) -> None:
        pos = st.pos
        if op == "c" and m == "w" and pos < len(st.buf) and not st.buf[pos].isspace():
            m = "e"               # vim: cw on a word behaves like ce
        target = self._motion_target(st, m, n, for_op=True)
        if m == "e":
            start, end = pos, min(len(st.buf), target + 1)
        elif m == "w":
            start, end = pos, target
            nl = st.buf.find("\n", pos, end)
            if nl != -1:
                end = nl          # dw never joins lines
        elif target < pos:
            start, end = target, pos
        else:
            start, end = pos, target
        if op == "y":
            self._yank(st, st.buf[start:end], False)
            st.pos = start
            return
        mark = len(st.undo)
        self._cut(st, start, end)
        if op == "c":
            self.begin_insert(st, mark)

    def _command(self, st: Any, key: str, n: int) -> None:
        buf = st.buf
        start, end = _line_bounds(buf, st.pos)
        if key in _MOTIONS:
            st.pos = self._motion_target(st, key, n, for_op=False)
            return
        if key in ("j", "k"):
            delta = 1 if key == "j" else -1
            for _ in range(n):
                if not st.move_vertical(delta):
                    st.history_step(delta)
                    break
            self._clamp(st)
            return
        if key == "x":
            self._cut(st, st.pos, min(end, st.pos + n))
            self._clamp(st)
        elif key == "X":
            self._cut(st, max(start, st.pos - n), st.pos)
        elif key == "D":
            self._cut(st, st.pos, end)
            self._clamp(st)
        elif key == "C":
            mark = len(st.undo)
            self._cut(st, st.pos, end)
            self.begin_insert(st, mark)
        elif key == "s":
            mark = len(st.undo)
            self._cut(st, st.pos, min(end, st.pos + n))
            self.begin_insert(st, mark)
        elif key == "r":
            self.count = str(n) if n > 1 else ""
            self.awaiting = "r"
        elif key == "i":
            self.begin_insert(st)
        elif key == "a":
            self.begin_insert(st)
            if st.pos < end:
                st.pos += 1
        elif key == "I":
            self.begin_insert(st)
            st.pos = self._motion_target(st, "^", 1, for_op=True)
        elif key == "A":
            self.begin_insert(st)
            st.pos = end
        elif key == "o":
            mark = len(st.undo)
            st.pos = end
            st.insert("\n")
            self.begin_insert(st, mark)
        elif key == "O":
            mark = len(st.undo)
            st.pos = start
            st.insert("\n")
            st.pos = start
            self.begin_insert(st, mark)
        elif key in ("p", "P"):
            self._put(st, key == "p", n)
        elif key == "u":
            for _ in range(n):
                st.undo_last()
            self._clamp(st)

    def _put(self, st: Any, after: bool, n: int) -> None:
        text, linewise = self.register
        if not text and st.kill_ring:
            text, linewise = st.kill_ring[-1], False
        if not text:
            return
        start, end = _line_bounds(st.buf, st.pos)
        st._snapshot()
        if linewise:
            block = "\n".join([text] * n)
            if after:
                st.buf = st.buf[:end] + "\n" + block + st.buf[end:]
                st.pos = end + 1
            else:
                st.buf = st.buf[:start] + block + "\n" + st.buf[start:]
                st.pos = start
        else:
            block = text * n
            at = st.pos + 1 if (after and st.pos < end) else st.pos
            st.buf = st.buf[:at] + block + st.buf[at:]
            st.pos = at + len(block) - 1
        st._changed()
        self._clamp(st)
