"""
One-line notices from background work (connectors, skills, updates).

Background threads must not print into the prompt, so they post a line here. The next prompt the user
sends shows every waiting line once (see :mod:`axon.skills.auto`, which is called by the prompt hook).
"""
from __future__ import annotations

import threading

MAX_WAITING = 50

_lock = threading.Lock()
_waiting: list[str] = []


def post(text: str) -> None:
    """Queue a notice. The same text is not queued twice while it is still waiting."""
    line = " ".join(text.split())
    if not line:
        return
    with _lock:
        if line in _waiting:
            return
        if len(_waiting) >= MAX_WAITING:
            _waiting.pop(0)
        _waiting.append(line)


def drain() -> list[str]:
    """Return and clear every waiting notice."""
    with _lock:
        lines = list(_waiting)
        _waiting.clear()
    return lines


def peek() -> list[str]:
    with _lock:
        return list(_waiting)
