"""
Vim editing layer for the prompt editor (``settings.vim_mode`` / ``/vim``).

Pure logic over :class:`axon.ui.input.EditorState` (``buf``, ``pos``, ``undo``, ``kill_ring``), so
it is testable with scripted key sequences. Supported in NORMAL mode:

* motions ``h l j k Space w b e 0 ^ $ gg G f F t T ; , %`` (with counts, e.g. ``3w``)
* operators ``d c y`` + motion or text object (``dw cw de d$ c0 y2w dgg dG dj dk df, dt) d% ci" da( yiW`` ...),
  linewise ``dd cc yy`` (``2dd``), and ``>> << J . Y S``
* ``x X D C s r<char> p P u``; insert with ``i a I A o O``; ``Esc`` back to NORMAL
* visual selection with ``v`` / ``V``: motions extend it, ``d x y c s p r ~ u U > < J o`` and text objects act on it
* ``j``/``k`` at the first/last line walk prompt history, like the arrow keys
* ``/`` opens reverse history search, ``?`` the shortcut panel

``vimInsertModeRemaps`` maps a two-key sequence typed in INSERT mode to Esc. The first key is inserted as
usual; the second, within a second, takes it back and leaves INSERT mode. Otherwise both stay as text.

Keys the layer does not own (Enter, Ctrl-combos, arrows, Tab, paste, ...) are left to the
regular editor so submit / history search / mode cycling keep working in both modes.
"""
from __future__ import annotations

import time
from typing import Any

INSERT = "insert"
NORMAL = "normal"
VISUAL = "visual"
VISUAL_LINE = "visual line"

_MOTIONS = set("hlwbe0^$ ")
_OPERATORS = set("dcy")
_FIND_KEYS = set("fFtT")
_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _PAIRS.items()}
_OBJECT_ALIASES = {")": "(", "]": "[", "}": "{", "b": "(", "B": "{"}
INDENT = "  "
MAX_COUNT = 9999            # a typed count such as 99999999p must not hang the prompt
REMAP_WINDOW_S = 1.0
_insert_remaps: frozenset[str] = frozenset()


def set_insert_remaps(mapping: Any) -> frozenset[str]:
    """Keep the entries of ``vimInsertModeRemaps`` that are valid: two printable characters mapped to "<Esc>"."""
    global _insert_remaps
    items = mapping.items() if isinstance(mapping, dict) else ()
    _insert_remaps = frozenset(k for k, v in items if isinstance(k, str) and len(k) == 2 and k.isprintable() and v == "<Esc>")
    return _insert_remaps


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


def match_bracket(buf: str, pos: int) -> int | None:
    """Index of the bracket that ``%`` jumps to: from the bracket under the cursor, else the next one on the line."""
    _start, end = _line_bounds(buf, pos)
    i = pos
    while i < end and buf[i] not in _PAIRS and buf[i] not in _CLOSERS:
        i += 1
    if i >= end:
        return None
    ch = buf[i]
    depth = 0
    if ch in _PAIRS:
        close = _PAIRS[ch]
        for j in range(i, len(buf)):
            if buf[j] == ch:
                depth += 1
            elif buf[j] == close:
                depth -= 1
                if depth == 0:
                    return j
    else:
        opener = _CLOSERS[ch]
        for j in range(i, -1, -1):
            if buf[j] == ch:
                depth += 1
            elif buf[j] == opener:
                depth -= 1
                if depth == 0:
                    return j
    return None


def _word_object(buf: str, pos: int, around: bool, big: bool) -> tuple[int, int] | None:
    if not buf:
        return None
    pos = min(pos, len(buf) - 1)

    def kind(c: str) -> int:
        return (0 if c.isspace() else 1) if big else _cls(c)
    k = kind(buf[pos])
    start, end = pos, pos + 1
    while start > 0 and buf[start - 1] != "\n" and kind(buf[start - 1]) == k:
        start -= 1
    while end < len(buf) and buf[end] != "\n" and kind(buf[end]) == k:
        end += 1
    if around:
        if end < len(buf) and buf[end] in " \t":
            while end < len(buf) and buf[end] in " \t":
                end += 1
        else:
            while start > 0 and buf[start - 1] in " \t":
                start -= 1
    return start, end


def _quote_object(buf: str, pos: int, q: str, around: bool) -> tuple[int, int] | None:
    start, end = _line_bounds(buf, pos)
    marks = [i for i in range(start, end) if buf[i] == q]
    for a, b in zip(marks[::2], marks[1::2]):
        if pos <= b:                     # the pair holding the cursor, or the next one on the line
            return (a, b + 1) if around else (a + 1, b)
    return None


def _pair_object(buf: str, pos: int, opener: str, around: bool) -> tuple[int, int] | None:
    close = _PAIRS[opener]
    depth = 0
    start = None
    for i in range(min(pos, len(buf) - 1), -1, -1):
        c = buf[i]
        if c == close and i != pos:
            depth += 1
        elif c == opener:
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return None
    end = match_bracket(buf, start)
    if end is None:
        return None
    return (start, end + 1) if around else (start + 1, end)


def text_object(buf: str, pos: int, scope: str, what: str) -> tuple[int, int] | None:
    """(start, end) of a text object such as ``i"`` (scope "i") or ``aW`` (scope "a")."""
    around = scope == "a"
    if what in ("w", "W"):
        return _word_object(buf, pos, around, what == "W")
    if what in ('"', "'", "`"):
        return _quote_object(buf, pos, what, around)
    opener = _OBJECT_ALIASES.get(what, what)
    if opener in _PAIRS and buf:
        return _pair_object(buf, pos, opener, around)
    return None


class VimState:
    """Per-prompt vim state. ``handle(st, key)`` returns True when the key was consumed."""

    def __init__(self, mode: str = INSERT) -> None:
        self.mode = mode
        self.count = ""
        self.op: str | None = None
        self.op_count = ""
        self.awaiting: str | None = None    # a key that needs one more: r g f F t T i a > <
        self.register: tuple[str, bool] = ("", False)   # (text, linewise)
        self.anchor = 0                     # the other end of a visual selection
        self.last_find: tuple[str, str] | None = None   # (f/F/t/T, char), for ; and ,
        self.last_change: list[str] = []    # keys of the last change, for .
        self._recording: list[str] | None = None
        self._replaying = False
        self._insert_mark: int | None = None
        self._first_key: tuple[str, int, float] | None = None   # remap: (key, where it went, when)

    # ── public ────────────────────────────────────────────────────────────
    @property
    def label(self) -> str:
        if self.mode == INSERT:
            return "-- INSERT --"
        if self.mode == VISUAL:
            return "-- VISUAL --"
        if self.mode == VISUAL_LINE:
            return "-- VISUAL LINE --"
        pending = (self.count or "") + (self.op or "") + (self.op_count or "") + (self.awaiting or "")
        return "-- NORMAL --" + (f" {pending}" if pending else "")

    @property
    def status_name(self) -> str:
        """The mode as the status-line JSON spells it: NORMAL, INSERT, VISUAL or VISUAL LINE."""
        return self.mode.upper()

    def selection(self, st: Any) -> tuple[int, int] | None:
        """(start, end) of the visual selection, end exclusive; None outside visual mode."""
        if self.mode == VISUAL:
            a, b = sorted((self.anchor, st.pos))
            return a, min(len(st.buf), b + 1)
        if self.mode == VISUAL_LINE:
            a, b = sorted((self.anchor, st.pos))
            return _line_bounds(st.buf, a)[0], _line_bounds(st.buf, b)[1]
        return None

    def reset_pending(self) -> None:
        self.count, self.op, self.op_count, self.awaiting = "", None, "", None

    def handle(self, st: Any, key: str) -> bool:
        if self.mode == INSERT:
            if key == "esc":
                self.to_normal(st)
                self._finish_recording()
                return True
            if self._recording is not None:
                self._recording.append(key)
            return self._insert_remap(st, key)
        if self.mode in (VISUAL, VISUAL_LINE):
            return self._visual(st, key)
        return self._normal_recorded(st, key)

    # ── repeat ────────────────────────────────────────────────────────────
    def _normal_recorded(self, st: Any, key: str) -> bool:
        """Runs one NORMAL key; a finished change is remembered for ``.``."""
        fresh = not (self.op or self.awaiting or self.count)
        if fresh and not self._replaying:
            self._recording = []
        before = (st.buf, self.mode)
        if self._recording is not None and key != ".":
            self._recording.append(key)
        consumed = self._normal(st, key)
        if not self._replaying and not (self.op or self.awaiting or self.count) and self._recording is not None:
            if self.mode == INSERT:
                return consumed                 # the typed text belongs to the change; Esc ends it
            if (st.buf, self.mode) != before and key != "u":
                self.last_change = self._recording
            self._recording = None
        return consumed

    def _finish_recording(self) -> None:
        if self._recording is not None and not self._replaying:
            self.last_change = self._recording + ["esc"]
        self._recording = None

    def _repeat(self, st: Any) -> None:
        keys = list(self.last_change)
        if not keys:
            return
        self._replaying = True
        try:
            for key in keys:
                if self.mode == INSERT and key != "esc":
                    if len(key) == 1 and key >= " ":
                        st.insert(key)
                    elif key in ("enter", "ctrl-j", "shift-enter", "alt-enter"):
                        st.insert("\n")
                    elif key == "backspace":
                        st.backspace()
                else:
                    self.handle(st, key)
        finally:
            self._replaying = False

    # ── insert mode ───────────────────────────────────────────────────────
    def _insert_remap(self, st: Any, key: str) -> bool:
        """True when *key* completes a remap: the first key comes back out and INSERT mode ends."""
        first, self._first_key = self._first_key, None
        if not _insert_remaps or len(key) != 1:
            return False
        now = time.monotonic()
        if first is not None and first[0] + key in _insert_remaps and now - first[2] <= REMAP_WINDOW_S \
                and st.pos == first[1] + 1 and st.buf[first[1]:st.pos] == first[0]:
            st.buf = st.buf[:first[1]] + st.buf[st.pos:]
            st.pos = first[1]
            st._changed()
            self.to_normal(st)
            return True
        if any(r[0] == key for r in _insert_remaps):
            self._first_key = (key, st.pos, now)
        return False

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

    def _find(self, st: Any, kind: str, ch: str, n: int = 1, from_pos: int | None = None) -> int | None:
        """Where f/F/t/T lands on the current line, or None."""
        buf = st.buf
        pos = st.pos if from_pos is None else from_pos
        start, end = _line_bounds(buf, pos)
        i = pos
        repeat_t = from_pos is None and kind in "tT" and self.awaiting is None
        for count in range(n):
            if kind in "ft":
                begin = i + 1 + (1 if repeat_t and count == 0 and kind == "t" and i + 1 < end and buf[i + 1] == ch else 0)
                i = buf.find(ch, begin, end)
            else:
                begin = i - (1 if repeat_t and count == 0 and kind == "T" and i - 1 >= start and buf[i - 1] == ch else 0)
                i = buf.rfind(ch, start, max(start, begin))
            if i == -1:
                return None
        if kind == "t":
            return i - 1
        if kind == "T":
            return i + 1
        return i

    def _repeat_find(self, st: Any, reverse: bool, n: int) -> int | None:
        if self.last_find is None:
            return None
        kind, ch = self.last_find
        if reverse:
            kind = kind.swapcase()
        return self._find(st, kind, ch, n)

    def _motion_target(self, st: Any, m: str, n: int, for_op: bool) -> int:
        buf, pos = st.buf, st.pos
        start, end = _line_bounds(buf, pos)
        if m == "h":
            return max(start, pos - n)
        if m in ("l", " "):
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
        if self.awaiting is not None:
            return self._awaited(st, key)
        if key == "esc":
            if self.count or self.op:
                self.reset_pending()
                return True
            return False  # regular Esc handling (popup dismiss, Esc Esc clear / rewind)
        if len(key) != 1 or key < " ":
            self.reset_pending()
            return False  # Enter, ctrl-*, arrows, tab, paste: regular editor
        # counts
        if key in "0123456789" and (key != "0" or (self.op_count if self.op else self.count)):
            if self.op:
                self.op_count = str(min(int(self.op_count + key), MAX_COUNT))
            else:
                self.count = str(min(int(self.count + key), MAX_COUNT))
            return True
        n = min(int(self.count or "1") * int(self.op_count or "1"), MAX_COUNT)
        if self.op:
            return self._after_operator(st, key, n)
        if key in _OPERATORS:
            self.op = key
            return True
        if key in _FIND_KEYS or key in "rg><":
            self.awaiting = key
            return True
        self.reset_pending()
        self._command(st, key, n)
        return True

    def _after_operator(self, st: Any, key: str, n: int) -> bool:
        op = self.op
        assert op is not None
        if key in _FIND_KEYS or key in "gia":
            self.awaiting = key
            return True
        if key == op:            # dd / cc / yy
            self._linewise_op(st, op, n)
        elif key in _MOTIONS:
            self._motion_op(st, op, key, n)
        elif key in ("j", "k"):
            self._line_motion_op(st, op, n if key == "j" else -n)
        elif key == "G":
            self._lines_to(st, op, len(st.buf))
        elif key == "%":
            target = match_bracket(st.buf, st.pos)
            if target is not None:
                self._span_op(st, op, min(st.pos, target), max(st.pos, target) + 1)
        elif key in (";", ","):
            target = self._repeat_find(st, key == ",", n)
            if target is not None:
                self._span_op(st, op, min(st.pos, target), max(st.pos, target) + (1 if target >= st.pos else 0))
        self._end_op(st, op)
        return True

    def _end_op(self, st: Any, op: str) -> None:
        self.reset_pending()
        if op != "c":
            self._clamp(st)

    def _awaited(self, st: Any, key: str) -> bool:
        what = self.awaiting
        op = self.op
        self.awaiting = None
        n = min(int(self.count or "1") * int(self.op_count or "1"), MAX_COUNT)
        if key == "esc" or (len(key) != 1 and key != "enter"):
            self.reset_pending()
            return True
        if what == "r":
            if len(key) == 1 and key >= " ":
                _start, end = _line_bounds(st.buf, st.pos)
                if st.pos + n <= end:
                    st._snapshot()
                    st.buf = st.buf[:st.pos] + key * n + st.buf[st.pos + n:]
                    st.pos += n - 1
                    st._changed()
            self.reset_pending()
            return True
        if what == "g":
            if key == "g":
                if op:
                    self._lines_to(st, op, 0)
                    self._end_op(st, op)
                    return True
                st.pos = 0
            self.reset_pending()
            return True
        if what in (">", "<"):
            if key == what:
                self._shift_lines(st, *self._linewise_range(st, n), what == ">")
            self.reset_pending()
            return True
        if what in ("i", "a") and op:
            span = text_object(st.buf, st.pos, what, key)
            if span is not None:
                self._span_op(st, op, *span)
            self._end_op(st, op)
            return True
        if what in _FIND_KEYS and len(key) == 1:
            self.last_find = (what, key)
            target = self._find(st, what, key, n, from_pos=st.pos)
            if target is not None:
                if op:
                    a, b = (st.pos, target + 1) if target >= st.pos else (target, st.pos)
                    self._span_op(st, op, a, b)
                    self._end_op(st, op)
                    return True
                st.pos = target
        if op:
            self._end_op(st, op)
        self.reset_pending()
        return True

    def _span_op(self, st: Any, op: str, start: int, end: int) -> None:
        """Apply d / c / y to the characters in [start, end)."""
        if op == "y":
            self._yank(st, st.buf[start:end], False)
            st.pos = start
            return
        mark = len(st.undo)
        self._cut(st, start, end)
        if op == "c":
            self.begin_insert(st, mark)

    def _lines_to(self, st: Any, op: str, where: int) -> None:
        """d/c/y from the current line through the line holding *where* (first or last line)."""
        a, b = sorted((st.pos, min(where, len(st.buf))))
        self._lines_op(st, op, _line_bounds(st.buf, a)[0], _line_bounds(st.buf, b)[1])

    def _line_motion_op(self, st: Any, op: str, delta: int) -> None:
        """dj / dk: the current line and the ones below or above. Nothing happens when there is no such line."""
        start, end = _line_bounds(st.buf, st.pos)
        for _ in range(abs(delta)):
            if delta > 0:
                if end >= len(st.buf):
                    return
                end = _line_bounds(st.buf, end + 1)[1]
            else:
                if start == 0:
                    return
                start = _line_bounds(st.buf, start - 1)[0]
        self._lines_op(st, op, start, end)

    def _lines_op(self, st: Any, op: str, start: int, end: int) -> None:
        text = st.buf[start:end]
        if op == "y":
            self._yank(st, text, True)
            st.pos = start
            return
        mark = len(st.undo)
        if op == "c":
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

    def _linewise_op(self, st: Any, op: str, n: int) -> None:
        start, end = self._linewise_range(st, n)
        if op == "y":
            self._yank(st, st.buf[start:end], True)
            return
        self._lines_op(st, op, start, end)

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
        self._span_op(st, op, start, end)

    def _shift_lines(self, st: Any, start: int, end: int, right: bool) -> None:
        lines = st.buf[start:end].split("\n")
        if right:
            lines = [INDENT + ln if ln else ln for ln in lines]
        else:
            lines = [ln[len(INDENT):] if ln.startswith(INDENT) else ln.lstrip(" \t") if ln[:1] == "\t" or ln[:1] == " " else ln
                     for ln in lines]
        st._snapshot()
        st.buf = st.buf[:start] + "\n".join(lines) + st.buf[end:]
        i = start
        while i < len(st.buf) and st.buf[i] in " \t":
            i += 1
        st.pos = i
        st._changed()

    def _join_lines(self, st: Any, start: int, end: int, joins: int) -> None:
        st._snapshot()
        buf = st.buf
        at = st.pos
        for _ in range(joins):
            nl = buf.find("\n", start, end)
            if nl == -1:
                break
            rest = nl + 1
            while rest < len(buf) and buf[rest] in " \t":
                rest += 1
            sep = "" if nl == 0 or buf[nl - 1] in " \t" or rest >= len(buf) or buf[rest] == "\n" else " "
            at = nl + len(sep) - (1 if not sep else 0)
            end -= rest - nl - len(sep)
            buf = buf[:nl] + sep + buf[rest:]
        st.buf = buf
        st.pos = max(0, min(at, len(buf)))
        st._changed()
        self._clamp(st)

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
        if key == "G":
            st.pos = buf.rfind("\n") + 1
        elif key == "%":
            target = match_bracket(buf, st.pos)
            if target is not None:
                st.pos = target
        elif key in (";", ","):
            target = self._repeat_find(st, key == ",", n)
            if target is not None:
                st.pos = target
        elif key == "/":
            st.search_start()
            st.notice = "esc, i, / opens the command menu instead"
        elif key == ".":
            for _ in range(n):
                self._repeat(st)
        elif key == "v":
            self.anchor, self.mode = st.pos, VISUAL
        elif key == "V":
            self.anchor, self.mode = st.pos, VISUAL_LINE
        elif key == "x":
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
        elif key == "Y":
            self._linewise_op(st, "y", n)
        elif key == "S":
            self._linewise_op(st, "c", n)
        elif key == "J":
            self._join_lines(st, st.pos, len(buf), max(1, n))
        elif key == "s":
            mark = len(st.undo)
            self._cut(st, st.pos, min(end, st.pos + n))
            self.begin_insert(st, mark)
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

    # ── VISUAL mode ───────────────────────────────────────────────────────
    def _leave_visual(self, st: Any) -> None:
        self.mode = NORMAL
        self.reset_pending()
        self._clamp(st)

    def _visual(self, st: Any, key: str) -> bool:
        if self.awaiting is not None:
            return self._visual_awaited(st, key)
        if key == "esc":
            self._leave_visual(st)
            return True
        if key in ("up", "down", "left", "right"):
            key = {"up": "k", "down": "j", "left": "h", "right": "l"}[key]
        if len(key) != 1 or key < " ":
            return True           # nothing else reaches the editor while a selection is open
        if key in "123456789" or (key == "0" and self.count):
            self.count = str(min(int(self.count + key), MAX_COUNT))
            return True
        n = int(self.count or "1")
        self.count = ""
        span = self.selection(st)
        assert span is not None
        start, end = span
        linewise = self.mode == VISUAL_LINE
        if key in _MOTIONS:
            st.pos = self._motion_target(st, key, n, for_op=False)
        elif key in ("j", "k"):
            for _ in range(n):
                st.move_vertical(1 if key == "j" else -1)
            self._clamp(st)
        elif key == "G":
            st.pos = st.buf.rfind("\n") + 1
        elif key == "%":
            target = match_bracket(st.buf, st.pos)
            if target is not None:
                st.pos = target
        elif key in (";", ","):
            target = self._repeat_find(st, key == ",", n)
            if target is not None:
                st.pos = target
        elif key in _FIND_KEYS or key in "gria":
            self.awaiting = key
        elif key == "o":
            self.anchor, st.pos = st.pos, self.anchor
        elif key == "v":
            if self.mode == VISUAL:
                self._leave_visual(st)
            else:
                self.mode = VISUAL
        elif key == "V":
            if self.mode == VISUAL_LINE:
                self._leave_visual(st)
            else:
                self.mode = VISUAL_LINE
        elif key in ("d", "x"):
            self._leave_visual(st)
            if linewise:
                self._lines_op(st, "d", start, end)
            else:
                self._cut(st, start, end)
            self._clamp(st)
        elif key == "y":
            self._yank(st, st.buf[start:end], linewise)
            st.pos = start
            self._leave_visual(st)
        elif key in ("c", "s"):
            mark = len(st.undo)
            self._cut(st, start, end, linewise=linewise)
            self.begin_insert(st, mark)
        elif key == "p":
            text, _lw = self.register
            if not text and st.kill_ring:
                text = st.kill_ring[-1]
            st._snapshot()
            st.buf = st.buf[:start] + text + st.buf[end:]
            st.pos = start + max(0, len(text) - 1)
            st._changed()
            self._leave_visual(st)
        elif key in ("~", "u", "U"):
            piece = st.buf[start:end]
            piece = piece.swapcase() if key == "~" else (piece.lower() if key == "u" else piece.upper())
            self._replace_span(st, start, end, piece)
        elif key in (">", "<"):
            a, b = _line_bounds(st.buf, start)[0], _line_bounds(st.buf, max(start, end - 1))[1]
            self._shift_lines(st, a, b, key == ">")
            self._leave_visual(st)
        elif key == "J":
            a, b = _line_bounds(st.buf, start)[0], _line_bounds(st.buf, max(start, end - 1))[1]
            joins = max(1, st.buf.count("\n", a, b))
            st.pos = a
            self._leave_visual(st)
            self._join_lines(st, a, b, joins)
        return True

    def _replace_span(self, st: Any, start: int, end: int, text: str) -> None:
        st._snapshot()
        st.buf = st.buf[:start] + text + st.buf[end:]
        st.pos = start
        st._changed()
        self._leave_visual(st)

    def _visual_awaited(self, st: Any, key: str) -> bool:
        what = self.awaiting
        self.awaiting = None
        if key == "esc" or len(key) != 1:
            return True
        n = int(self.count or "1")
        self.count = ""
        if what == "g":
            if key == "g":
                st.pos = 0
        elif what == "r":
            span = self.selection(st)
            assert span is not None
            piece = "".join("\n" if c == "\n" else key for c in st.buf[span[0]:span[1]])
            self._replace_span(st, span[0], span[1], piece)
        elif what in ("i", "a"):
            span = text_object(st.buf, st.pos, what, key)
            if span is not None and span[1] > span[0]:
                self.anchor, st.pos = span[0], span[1] - 1
        elif what in _FIND_KEYS:
            self.last_find = (what, key)
            target = self._find(st, what, key, n, from_pos=st.pos)
            if target is not None:
                st.pos = target
        return True

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
