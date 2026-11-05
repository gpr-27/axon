"""
Marks for things Axon waits on that send no event while they run (a hook, an MCP server, compaction, a prompt).

Code that is about to block says so::

    with waiting("hook", event="PreToolUse"):
        run_the_hook()

The live status line under the spinner (``axon.ui.live_status``) reads the active marks to say what is
happening. This module has no imports from the rest of Axon, so any layer can use it.

Kinds the status line understands: hook, mcp_call, mcp_connect, compact, title, user.
"""
from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class Wait:
    kind: str
    t0: float
    info: dict[str, Any] = field(default_factory=dict)


_WAITS: dict[int, Wait] = {}
_LOCK = threading.Lock()
_IDS = itertools.count(1)


def begin_wait(kind: str, info: dict[str, Any] | None = None, t0: float | None = None) -> int:
    token = next(_IDS)
    with _LOCK:
        _WAITS[token] = Wait(kind, time.time() if t0 is None else t0, dict(info or {}))
    return token


def end_wait(token: int) -> None:
    with _LOCK:
        _WAITS.pop(token, None)


@contextmanager
def waiting(kind: str, **info: Any) -> Iterator[None]:
    """Mark that Axon is waiting on *kind* for the duration of the block."""
    token = begin_wait(kind, info)
    try:
        yield
    finally:
        end_wait(token)


def active_waits() -> list[Wait]:
    """The marks that are open now, oldest first."""
    with _LOCK:
        return sorted(_WAITS.values(), key=lambda w: w.t0)
