"""
Remembers how each MCP server has been doing, across sessions: when it last connected, the last
error, and how many connection attempts failed in a row.

The ledger is ``~/.axon/state/mcp-health.json``. It is what lets Axon switch a server off after
``automation.auto_disable_after`` failures in a row, and it feeds the ``/marketplace status`` table.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from axon.storage import read_state, write_state

STATE_NAME = "mcp-health"
_lock = threading.Lock()


@dataclass
class ServerHealth:
    failures: int = 0               # failed connection cycles in a row
    last_error: str = ""
    last_ok: float = 0.0            # unix time of the last good connection
    last_failure: float = 0.0
    auto_disabled: bool = False     # Axon switched the server off after too many failures
    auto_disabled_at: float = 0.0


def _load_all() -> dict[str, Any]:
    data = read_state(STATE_NAME, {})
    return data if isinstance(data, dict) else {}


def _entry(raw: Any) -> ServerHealth:
    if not isinstance(raw, dict):
        return ServerHealth()
    known = ServerHealth.__dataclass_fields__
    return ServerHealth(**{key: value for key, value in raw.items() if key in known})


def get(name: str) -> ServerHealth:
    return _entry(_load_all().get(name))


def all_health() -> dict[str, ServerHealth]:
    return {name: _entry(raw) for name, raw in _load_all().items()}


def _update(name: str, change: Any) -> ServerHealth:
    with _lock:
        data = _load_all()
        entry = _entry(data.get(name))
        change(entry)
        data[name] = asdict(entry)
        write_state(STATE_NAME, data)
    return entry


def record_success(name: str) -> ServerHealth:
    def change(entry: ServerHealth) -> None:
        entry.failures = 0
        entry.last_error = ""
        entry.last_ok = time.time()
    return _update(name, change)


def record_failure(name: str, error: str) -> ServerHealth:
    def change(entry: ServerHealth) -> None:
        entry.failures += 1
        entry.last_error = error[:300]
        entry.last_failure = time.time()
    return _update(name, change)


def note_error(name: str, error: str) -> ServerHealth:
    """Remember an error (from a manual test) without counting it as a failed connection cycle."""
    def change(entry: ServerHealth) -> None:
        entry.last_error = error[:300]
    return _update(name, change)


def mark_auto_disabled(name: str) -> ServerHealth:
    def change(entry: ServerHealth) -> None:
        entry.auto_disabled = True
        entry.auto_disabled_at = time.time()
    return _update(name, change)


def clear(name: str) -> ServerHealth:
    """Forget the failures and the auto-disabled flag (the server was enabled or fixed by hand)."""
    def change(entry: ServerHealth) -> None:
        entry.failures = 0
        entry.auto_disabled = False
        entry.auto_disabled_at = 0.0
    return _update(name, change)


def should_disable(failures: int, limit: int) -> bool:
    return limit > 0 and failures >= limit
