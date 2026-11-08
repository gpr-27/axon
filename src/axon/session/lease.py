"""Which process is writing a session right now.

Two writers on one session would interleave their histories, so a process holds a lease while the session is
open. A lease from a process that has ended counts as free, so a crash never leaves a session locked.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEASE_FILE = "writer.lease"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True      # exists, owned by someone else
    return True


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_live(info: dict[str, Any]) -> bool:
    if info.get("host") != socket.gethostname():
        return True      # another machine (shared folder): it cannot be checked, so assume it is running
    try:
        return _pid_alive(int(info.get("pid") or 0))
    except (TypeError, ValueError):
        return False


def holder(session_dir: Path) -> dict[str, Any] | None:
    """The live process holding this session (pid, host, owner, started), or None when it is free."""
    info = _read(Path(session_dir) / LEASE_FILE)
    return info if info is not None and _is_live(info) else None


@dataclass
class Lease:
    path: Path

    def release(self) -> None:
        info = _read(self.path)
        if info is not None and info.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass


def acquire(session_dir: Path, owner: str = "terminal") -> Lease | None:
    """Take the session for this process. None when another live process holds it. Taking a lease this
    process already holds, or one left by a process that has ended, succeeds."""
    folder = Path(session_dir)
    path = folder / LEASE_FILE
    record = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "owner": owner, "started": time.time()})
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Lease(path)
    for _ in range(3):
        current = holder(folder)
        if current is not None and current.get("pid") != os.getpid():
            return None
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue         # another process won the race: look again
        except OSError:
            return Lease(path)  # a read-only folder must not stop the session from working
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(record)
        return Lease(path)
    return None
