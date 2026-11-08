"""Real terminals for the dashboard: a pseudo-terminal (PTY) running the user's shell or the Axon REPL.

POSIX only (stdlib ``pty``). Windows has no ConPTY in the standard library, so the page says so and keeps the
line-based Shell panel. Output is a byte ring buffer read over SSE (offsets, so a reconnect replays what it
missed); input and resize are authenticated POSTs. The HTTP layer applies the same token, cookie, Host and
Origin checks as everything else, and the terminal tab tells the user plainly that it is a full shell."""
from __future__ import annotations

import base64
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from axon.features.dash_common import DashError

IS_POSIX = os.name == "posix"
MAX_TERMINALS = 8
RING_BYTES = 2 * 1024 * 1024
MAX_INPUT = 256 * 1024


class PtySession:
    def __init__(self, tid: str, argv: list[str], cwd: Path, cols: int, rows: int, label: str, theme: str = "dark") -> None:
        import fcntl
        import pty
        import struct
        import termios
        self.id = tid
        self.label = label
        self.cwd = str(cwd)
        self.argv = argv
        self.started = time.time()
        self.cols, self.rows = cols, rows
        self.code: int | None = None
        self.base = 0                       # absolute offset of ring[0]
        self.ring = bytearray()
        self.cond = threading.Condition()
        env = dict(os.environ)
        for k in ("AXON_SERVE_TOKEN",):
            env.pop(k, None)
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LINES": str(rows), "COLUMNS": str(cols),
                    # programs that read COLORFGBG (Axon does) pick a palette that suits the page's theme
                    "COLORFGBG": "0;15" if theme == "light" else "15;0"})
        pid, fd = pty.fork()
        if pid == 0:                         # child: becomes the shell
            try:
                os.chdir(str(cwd))
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(127)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        threading.Thread(target=self._pump, name=f"axon-pty-{tid}", daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            with self.cond:
                self.ring += data
                if len(self.ring) > RING_BYTES:
                    drop = len(self.ring) - RING_BYTES
                    del self.ring[:drop]
                    self.base += drop
                self.cond.notify_all()
        try:
            _, status = os.waitpid(self.pid, 0)
            self.code = os.waitstatus_to_exitcode(status)
        except (ChildProcessError, OSError):
            self.code = self.code if self.code is not None else 0
        with self.cond:
            self.cond.notify_all()
        try:
            os.close(self.fd)
        except OSError:
            pass

    @property
    def total(self) -> int:
        return self.base + len(self.ring)

    def read(self, since: int, timeout: float = 15.0) -> tuple[bytes, int, bool, bool]:
        """(bytes after offset *since*, new offset, reset, finished). ``reset`` means older output was dropped."""
        with self.cond:
            if since >= self.total and self.code is None:
                self.cond.wait(timeout)
            reset = since < self.base
            start = max(since, self.base)
            data = bytes(self.ring[start - self.base:])
            return data, self.base + len(self.ring), reset, self.code is not None and not data

    def write(self, data: bytes) -> None:
        if self.code is not None:
            raise DashError("the terminal has ended", 409)
        try:
            os.write(self.fd, data)
        except OSError:
            raise DashError("the terminal has ended", 409) from None

    def resize(self, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios
        self.cols, self.rows = cols, rows
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.killpg(os.getpgid(self.pid), signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def kill(self) -> None:
        if self.code is not None:
            return
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.killpg(os.getpgid(self.pid), sig)
            except (OSError, ProcessLookupError):
                break
            time.sleep(0.15)
            if self.code is not None:
                return
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "cwd": self.cwd, "started": self.started, "running": self.code is None,
                "code": self.code, "cols": self.cols, "rows": self.rows, "bytes": self.total}


class PtyManager:
    def __init__(self) -> None:
        self.sessions: dict[str, PtySession] = {}
        self.lock = threading.Lock()

    @staticmethod
    def available() -> tuple[bool, str]:
        if not IS_POSIX:
            return False, ("A real terminal needs a pseudo-terminal, which Python's standard library only provides on macOS "
                           "and Linux. On Windows the line-based Shell panel is available instead.")
        return True, ""

    def start(self, kind: str, cwd: Path, cols: int, rows: int, initial: str = "", theme: str = "dark") -> PtySession:
        ok, why = self.available()
        if not ok:
            raise DashError(why, 501)
        cols, rows = max(20, min(400, cols)), max(5, min(200, rows))
        if kind == "axon":
            argv = [sys.executable, "-m", "axon"]
            label = "Axon"
        else:
            shell = os.environ.get("SHELL") or "/bin/sh"
            argv = [shell, "-l"] if os.path.basename(shell) in ("zsh", "bash", "fish") else [shell]
            label = os.path.basename(shell)
        with self.lock:
            live = [s for s in self.sessions.values() if s.code is None]
            if len(live) >= MAX_TERMINALS:
                raise DashError(f"At most {MAX_TERMINALS} terminals can be open. Close one first.", 409)
            sess = PtySession(secrets.token_hex(5), argv, cwd, cols, rows, label, "light" if theme == "light" else "dark")
            self.sessions[sess.id] = sess
        if initial:                         # e.g. "/provider" typed into the Axon REPL once it has started
            def later() -> None:
                time.sleep(3.0)
                try:
                    sess.write((initial.rstrip("\n") + "\r").encode("utf-8"))
                except DashError:
                    pass
            threading.Thread(target=later, daemon=True).start()
        return sess

    def get(self, tid: str) -> PtySession:
        s = self.sessions.get(tid)
        if s is None:
            raise DashError("no such terminal", 404)
        return s

    def close(self, tid: str) -> None:
        s = self.sessions.pop(tid, None)
        if s is not None:
            s.kill()

    def close_all(self) -> None:
        for tid in list(self.sessions):
            self.close(tid)


def encode_chunk(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
