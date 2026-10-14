"""
Shared helpers for the Agent-D end-to-end suites (interactive UX + cross-platform behaviour).

This module holds no tests itself; it is named ``test_e2e_d_*`` only so it lives in the files
owned by this suite. It is imported by ``test_e2e_d_repl.py``, ``test_e2e_d_render.py`` and
``test_e2e_d_windows.py``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from harness import IS_WINDOWS, SRC, AxonEnv, PtySession, strip_ansi

TRACEBACK = "Traceback (most recent call last)"
PROMPT_READY = r"❯ Ask anything"
SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

posix_only = pytest.mark.skipif(IS_WINDOWS, reason="pty-driven REPL tests are POSIX-only")


class Pty(PtySession):
    """PtySession with control over NO_COLOR / TERM and the window size (the harness always
    strips NO_COLOR and forces TERM=xterm-256color)."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path, rows: int = 40, cols: int = 120,
                 no_color: bool = False, term: str = "xterm-256color") -> None:  # noqa: D401
        import fcntl
        import pty
        import struct
        import termios

        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(env)
        env["TERM"] = term
        if no_color:
            env["NO_COLOR"] = "1"
        else:
            env.pop("NO_COLOR", None)
        env["COLUMNS"], env["LINES"] = str(cols), str(rows)
        self.proc = subprocess.Popen([sys.executable, "-m", "axon", *args], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, cwd=str(cwd), close_fds=True, start_new_session=True)
        os.close(slave)
        self._raw = b""

    # ── robust typing helpers ───────────────────────────────────────────
    def type(self, text: str, timeout: float = 6.0) -> None:
        """Type `text` and wait until the editor echoes it (so a following Enter is a separate read)."""
        m = self.mark()
        self.send(text)
        last = text.splitlines()[-1] if text.strip() else ""
        tail = last.lstrip("!")[-12:] if last.startswith("!") else last[-12:]
        if tail:
            self.expect(re.escape(tail), timeout=timeout, since=m)
        time.sleep(0.12)

    def submit(self, text: str, timeout: float = 6.0) -> int:
        """Type `text`, press Enter; returns the screen offset taken before typing."""
        m = self.mark()
        self.type(text, timeout=timeout)
        self.send("\r")
        return m

    def ready(self, since: int = 0, timeout: float = 20.0) -> None:
        """Wait for the idle prompt placeholder after offset `since`, then let the redraw settle."""
        self.expect(PROMPT_READY, timeout=timeout, since=since)
        self.drain(0.15)

    def turn(self, text: str, timeout: float = 25.0) -> str:
        """Submit a prompt and wait for the turn footer and the next idle prompt; returns the new screen text."""
        m = self.submit(text)
        self.expect(r"✓ Done|✗ Failed|\+ \+ Done", timeout=timeout, since=m)
        self.ready(since=m, timeout=timeout)
        return self.screen[m:]

    def command(self, text: str, expect: str, timeout: float = 10.0) -> str:
        m = self.submit(text)
        self.expect(expect, timeout=timeout, since=m)
        self.drain(0.4)
        return self.screen[m:]

    def raw_since(self, offset: int) -> str:
        return self._raw[offset:].decode("utf-8", "replace")


def open_repl(env: AxonEnv, args: tuple[str, ...] = (), rows: int = 40, cols: int = 120,
              extra_env: dict[str, str] | None = None, no_color: bool = False) -> Pty:
    if IS_WINDOWS:
        pytest.skip("pty-driven REPL tests are POSIX-only")
    s = Pty(list(args), {**env.env, **(extra_env or {})}, env.workspace, rows=rows, cols=cols, no_color=no_color)
    try:
        s.ready(timeout=20)
    except Exception:
        s.close()
        raise
    return s


def tool_turn(cmd: str = "echo hi-from-tool", answer: str = "FINAL-ANSWER", why: str = "Running it.") -> list[Any]:
    """A two-request script: text + Bash tool call, then a final answer."""
    return [[{"text": why}, {"tool": "Bash", "args": {"command": cmd, "description": "run a command"}}],
            [{"text": answer}]]


def run_script(env: AxonEnv, script_body: str, args: list[str], tmp: Path, extra_env: dict[str, str] | None = None,
               drop: tuple[str, ...] = (), py_flags: tuple[str, ...] = (), timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a small wrapper script (written to its own directory) that boots axon, capturing raw bytes."""
    d = tmp / "wrapper"
    d.mkdir(exist_ok=True)
    path = d / "wrap_axon.py"
    path.write_text(script_body, encoding="utf-8")
    e = {**env.env, **(extra_env or {})}
    for k in drop:
        e.pop(k, None)
    return subprocess.run([sys.executable, *py_flags, str(path), *args], capture_output=True, env=e,
                          cwd=str(env.workspace), timeout=timeout)


def run_raw(env: AxonEnv, args: list[str], extra_env: dict[str, str] | None = None, drop: tuple[str, ...] = (),
            py_flags: tuple[str, ...] = (), timeout: float = 60, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python [flags] -m axon args`` capturing raw bytes (no decoding by the harness)."""
    e = {**env.env, **(extra_env or {})}
    for k in drop:
        e.pop(k, None)
    return subprocess.run([sys.executable, *py_flags, "-m", "axon", *args], capture_output=True, env=e,
                          cwd=str(cwd or env.workspace), timeout=timeout, stdin=subprocess.DEVNULL)


def visible_lines(text: str) -> list[str]:
    return strip_ansi(text).replace("\r", "").splitlines()


__all__ = ["TRACEBACK", "PROMPT_READY", "SGR_RE", "posix_only", "Pty", "open_repl", "tool_turn", "run_script",
           "run_raw", "visible_lines", "SRC"]
