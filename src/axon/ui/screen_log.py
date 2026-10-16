"""
Screen log: a plain-text record of everything Axon showed in the terminal.

The REPL wraps ``sys.stdout`` in :class:`ScreenLog`. Every write still goes to the terminal
unchanged; a copy is fed through a tiny line model that understands what the renderer and the
prompt use to redraw (carriage return, erase line, cursor up, erase below, the alternate screen),
so the file ends up with what stayed on screen: timelines, tool outputs, command output, errors.
Transient rows (spinner frames, the tip under the spinner, picker redraws) are erased before they
are committed and never reach the file. Colours and hyperlinks are stripped.

Files live per project and are never rotated or truncated:
``~/.axon/projects/<project>/screen/<started>-<session>.log``.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

# OSC (hyperlinks, titles) and CSI sequences; anything else ESC-prefixed is dropped as a 2-byte sequence.
_TOKEN_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[@-_]|\r|\n|[^\x1b\r\n]+",
                       re.DOTALL)
_KEEP_PENDING = 8          # committed rows kept in memory, so a redraw (cursor up) can still take them back
_KEEP_TAGS = 5000          # rows whose transcript tag is remembered (click-to-expand at the prompt)

# Transcript entry (TranscriptEntry.uid) the renderer is printing right now; None for anything else
# (slash commands, the prompt). Every row written while it is set is tagged with it.
current_tag: int | None = None


def set_tag(tag: int | None) -> None:
    global current_tag
    current_tag = tag


class RowTags:
    """Which transcript entry each row on the main screen belongs to.

    Mirrors the terminal's row structure from the same output the screen log sees (newlines
    commit a row, cursor-up takes rows back, erase-line/erase-below drop text, a full clear
    forgets everything, the alternate screen is ignored). Unlike the log it is fed *all* output,
    including the prompt frame, so its last row is always the row above the cursor."""

    def __init__(self, keep: int = _KEEP_TAGS) -> None:
        self.rows: deque[tuple[int | None, int]] = deque(maxlen=keep)   # (tag, display width)
        self._line = ""
        self._tag: int | None = None
        self._cr = False
        self._alt = False
        self._restored = 0       # width of a row taken back by cursor-up and not erased (still on screen)

    def feed(self, s: str) -> None:
        for tok in _TOKEN_RE.findall(s):
            if tok.startswith("\x1b["):
                self._csi(tok)
            elif tok.startswith("\x1b") or self._alt:
                continue
            elif tok == "\n":
                self._commit()
            elif tok == "\r":
                self._cr = True
            else:
                if (not self._line.strip() and not self._restored) or self._tag is None:
                    self._tag = current_tag
                if self._cr:
                    self._line, self._cr = tok + self._line[len(tok):], False
                else:
                    self._line += tok

    def _commit(self) -> None:
        from axon.ui.theme import display_width
        keep = bool(self._line.strip()) or self._restored > 0
        self.rows.append((self._tag if keep else None, max(display_width(self._line), self._restored)))
        self._reset()

    def _reset(self) -> None:
        self._line, self._tag, self._cr, self._restored = "", None, False, 0

    def _csi(self, tok: str) -> None:
        body, final = tok[2:-1], tok[-1]
        if body in ("?1049", "?47", "?1047") and final in "hl":
            self._alt = final == "h"
            return
        if self._alt:
            return
        if final == "J" and body in ("2", "3"):     # full clear: the visible screen starts over
            self.clear()
        elif final in "JK":                         # erase below / erase line
            self._reset()
        elif final == "A":
            for _ in range(int(body) if body.isdigit() else 1):
                self._reset()
                if self.rows:
                    self._tag, self._restored = self.rows.pop()

    def clear(self) -> None:
        self.rows.clear()
        self._reset()

    def tag_at(self, rows_up: int, skip: int = 0, cols: int = 0) -> int | None:
        """Tag of the screen row ``rows_up`` rows above the newest ``skip`` committed rows
        (``rows_up=1``: the last row before them). Rows wider than ``cols`` count as several screen
        rows (terminal autowrap)."""
        if rows_up < 1:
            return None
        rows = list(self.rows)
        if skip:
            rows = rows[:-skip] if skip < len(rows) else []
        left = rows_up
        for tag, width in reversed(rows):
            height = max(1, -(-width // cols)) if cols > 0 and width > cols else 1
            if left <= height:
                return tag
            left -= height
        return None


_TAGS = RowTags()


def row_tags() -> RowTags:
    """The process-wide row model (fed by :class:`ScreenLog`, or by :func:`feed` when no log runs)."""
    return _TAGS


class ScreenLog:
    """``sys.stdout`` stand-in: writes through to the real stream and records the settled text."""

    def __init__(self, real: Any, path: Path | None, tags: RowTags | None = None) -> None:
        self._real = real
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file: Any = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        else:                                  # no file: only the row tags (click-to-expand) are kept
            self._file = open(os.devnull, "w", encoding="utf-8")
        self.tags = tags if tags is not None else _TAGS
        self._line = ""
        self._pending: deque[str] = deque()
        self._paused = 0
        self._alt = False
        self._cr = False                       # a carriage return: the next text overwrites from column 0
        self._lock = threading.RLock()

    # ── stream protocol ───────────────────────────────────────────────────
    def write(self, s: str) -> int:
        n = self._real.write(s)
        if s:
            with self._lock:
                try:
                    self.tags.feed(s)          # always: the prompt frame moves rows too
                    if not self._paused:
                        self._feed(s)
                except Exception:
                    pass  # logging must never break the terminal
        return n if isinstance(n, int) else len(s)

    def flush(self) -> None:
        try:
            self._real.flush()
        finally:
            with self._lock:
                try:
                    self._file.flush()
                except (OSError, ValueError):
                    pass

    def __getattr__(self, item: str) -> Any:
        return getattr(self._real, item)

    # ── recording control ─────────────────────────────────────────────────
    def pause(self) -> None:
        """Stop recording (the prompt editor redraws itself on every key)."""
        with self._lock:
            self._paused += 1
            self._commit_all()                # the screen is settled whenever the prompt shows: persist it
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except (OSError, ValueError):
                pass

    def resume(self) -> None:
        with self._lock:
            self._paused = max(0, self._paused - 1)

    def note(self, text: str) -> None:
        """Record a line that was shown while recording was paused (e.g. the submitted prompt)."""
        with self._lock:
            self._commit_all()
            for row in str(text).splitlines() or [""]:
                self._emit(row)

    def mark(self, text: str) -> None:
        """A separator line in the log only (session switches), never on screen."""
        with self._lock:
            self._commit_all()
            self._emit(f"──── {text} ──── {time.strftime('%Y-%m-%d %H:%M:%S')}")

    def close(self) -> None:
        with self._lock:
            self._commit_all()
            if self._line.strip():
                self._emit(self._line)
                self._line = ""
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except (OSError, ValueError):
                pass
            self._file.close()

    # ── line model ────────────────────────────────────────────────────────
    def _emit(self, row: str) -> None:
        try:
            self._file.write(row.rstrip() + "\n")
        except (OSError, ValueError):
            pass

    def _commit(self, row: str) -> None:
        self._pending.append(row)
        while len(self._pending) > _KEEP_PENDING:
            self._emit(self._pending.popleft())

    def _commit_all(self) -> None:
        while self._pending:
            self._emit(self._pending.popleft())

    def _feed(self, s: str) -> None:
        for tok in _TOKEN_RE.findall(s):
            if tok.startswith("\x1b["):
                self._csi(tok)
            elif tok.startswith("\x1b"):
                continue                      # OSC (links, titles) and other escapes carry no text
            elif self._alt:
                continue                      # full-screen views (transcript viewer) are not part of the log
            elif tok == "\n":
                self._commit(self._line)
                self._line, self._cr = "", False
            elif tok == "\r":
                self._cr = True               # the next erase / overwrite decides what stays
            elif self._cr:
                self._line, self._cr = tok + self._line[len(tok):], False
            else:
                self._line += tok

    def _csi(self, tok: str) -> None:
        body, final = tok[2:-1], tok[-1]
        if body in ("?1049", "?47", "?1047") and final in "hl":
            self._alt = final == "h"
            return
        if self._alt:
            return
        if final == "K":                      # erase in line
            self._line, self._cr = "", False
        elif final == "A":                    # cursor up: the row above is being redrawn
            n = int(body) if body.isdigit() else 1
            for _ in range(n):
                self._line = self._pending.pop() if self._pending else ""
        elif final == "J":                    # erase below (picker / prompt redraw)
            self._line = ""


def click_expand_enabled(settings: Any = None) -> bool:
    """Click-to-expand at the idle prompt. Off by default: while the prompt captures the mouse the
    terminal can no longer scroll its own history with the wheel or select text by dragging, which
    matters more. Opt in with ``mouse_click_expand = true`` (or AXON_MOUSE=1); AXON_NO_MOUSE wins.
    POSIX terminals only (Windows consoles do not deliver VT mouse reports here). Ctrl+F's full-screen
    transcript always supports clicks and wheel scrolling."""
    if os.environ.get("AXON_NO_MOUSE", "").strip() not in ("", "0", "false", "no"):
        return False
    if os.name == "nt":
        return False
    if os.environ.get("AXON_MOUSE", "").strip() not in ("", "0", "false", "no"):
        return True
    return bool(getattr(settings, "mouse_click_expand", False)) if settings is not None else False
