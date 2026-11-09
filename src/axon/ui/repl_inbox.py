"""Work the web page asks of a running terminal session, done by the terminal's own main loop.

The terminal has one owner of the screen: the REPL's main thread. The prompt editor draws its frame there, and a turn
draws its output there. A turn started on another thread while the prompt was up wrote over the prompt's rows, so page
requests wait here instead: the prompt notices, clears its frame (keeping any draft), and the main loop runs them."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ReplContext:
    """What a request may do on the main thread."""
    agent: Any
    run_turn: Callable[[str], None]
    redraw: Callable[[], None]


@dataclass
class Request:
    fn: Callable[[ReplContext], Any]
    cancel: Callable[[], None] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class ReplInbox:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[Request] = deque()
        self._active = False
        self.on_session_change: Callable[[str], None] | None = None
        self._session = ""

    @property
    def active(self) -> bool:
        """True while a terminal REPL is serving requests. A piped REPL blocks in input() and cannot be woken."""
        return self._active

    def has_pending(self) -> bool:
        return bool(self._queue)

    def activate(self, session: str = "") -> None:
        with self._lock:
            self._active = True
            self._session = session

    def deactivate(self) -> None:
        with self._lock:
            self._active = False
            left = list(self._queue)
            self._queue.clear()
        for req in left:
            req.error = RuntimeError("the terminal session ended")
            if req.cancel is not None:
                req.cancel()
            req.done.set()

    def post(self, fn: Callable[[ReplContext], Any], cancel: Callable[[], None] | None = None) -> Request:
        req = Request(fn, cancel)
        with self._lock:
            if not self._active:
                req.error = RuntimeError("no terminal session is running")
                req.done.set()
                return req
            self._queue.append(req)
        return req

    def run_pending(self, ctx: ReplContext) -> int:
        """Main thread only. A Ctrl+C inside a request stops that request and leaves the others queued."""
        count = 0
        while True:
            with self._lock:
                if not self._queue:
                    return count
                req = self._queue.popleft()
            count += 1
            try:
                req.result = req.fn(ctx)
            except KeyboardInterrupt as e:
                req.error = e
                req.done.set()
                raise
            except Exception as e:
                req.error = e
            req.done.set()

    def note_session(self, session: str) -> None:
        """The terminal's session may have changed (/resume, /clear, a request above): tell the page once."""
        with self._lock:
            if session == self._session:
                return
            self._session = session
            listener = self.on_session_change
        if listener is not None:
            try:
                listener(session)
            except Exception:
                pass


INBOX = ReplInbox()
