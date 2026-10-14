"""
End-to-end harness: runs the real ``axon`` CLI as a subprocess (``python -m axon``) in an isolated
home / AXON_HOME / workspace, pointed at a :class:`fake_llm.FakeLLMServer`.

    def test_x(axon_env):
        axon_env.llm.enqueue([{"text": "hi"}])
        r = axon_env.run(["-p", "hello"])
        assert r.code == 0 and "hi" in r.stdout

``run_axon_pty`` drives the interactive REPL through a pseudo-terminal (POSIX only).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from fake_llm import FakeLLMServer

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
IS_WINDOWS = os.name == "nt"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

# Variables copied from the parent environment; everything else (API keys, AXON_*) is dropped.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
                "LANG", "LC_ALL", "LC_CTYPE", "PROGRAMFILES", "PROGRAMDATA", "NUMBER_OF_PROCESSORS")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    duration: float
    args: list[str] = field(default_factory=list)

    @property
    def output(self) -> str:
        """stdout + stderr with ANSI escapes removed."""
        return strip_ansi(self.stdout + "\n" + self.stderr)

    def json(self) -> Any:
        import json
        return json.loads(self.stdout)

    def json_lines(self) -> list[Any]:
        import json
        out = []
        for line in self.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                out.append(json.loads(line))
        return out

    def __str__(self) -> str:  # helpful assertion messages
        return (f"axon {' '.join(self.args)} -> exit {self.code} in {self.duration:.2f}s\n"
                f"--- stdout ---\n{self.stdout[-4000:]}\n--- stderr ---\n{self.stderr[-4000:]}")


@dataclass
class AxonEnv:
    root: Path
    home: Path
    axon_home: Path
    workspace: Path
    llm: FakeLLMServer
    env: dict[str, str]

    def run(self, args: Iterable[str], input: str | None = None, timeout: float = 60,
            env: dict[str, str] | None = None, cwd: Path | None = None) -> Result:
        return run_axon(list(args), input=input, timeout=timeout, env={**self.env, **(env or {})},
                        cwd=cwd or self.workspace)

    def pty(self, args: Iterable[str] = (), env: dict[str, str] | None = None, **kw: Any) -> "PtySession":
        return run_axon_pty(list(args), env={**self.env, **(env or {})}, cwd=self.workspace, **kw)

    def use_anthropic_wire(self, model: str = "claude-sonnet-4-6") -> None:
        self.env.update({"AXON_PROVIDER": "anthropic", "AXON_MODEL": model})

    def sessions_dir(self) -> Path:
        from_key = _project_key(self.workspace)
        return self.axon_home / "projects" / from_key / "sessions"


def _project_key(workspace: Path) -> str:
    sys.path.insert(0, str(SRC)) if str(SRC) not in sys.path else None
    try:
        from axon.storage import project_key
        return project_key(workspace)
    except Exception:
        return re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve()))


def make_env(home: Path, axon_home: Path, base_url: str, model: str = "gpt-4o") -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "AXON_HOME": str(axon_home),
        "AXON_BASE_URL": base_url,
        "AXON_API_KEY": "sk-e2e-test-key",
        "AXON_MODEL": model,
        "AXON_OFFLINE": "1",
        "AXON_NO_UPDATE_CHECK": "1",
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "120",
        "LINES": "40",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
        "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
    })
    return env


def create_axon_env(tmp_path: Path, llm: FakeLLMServer, git: bool = False) -> AxonEnv:
    home = tmp_path / "home"
    axon_home = home / ".axon"
    ws = tmp_path / "workspace"
    for d in (home, axon_home, ws):
        d.mkdir(parents=True, exist_ok=True)
    if git:
        subprocess.run(["git", "init", "-q", str(ws)], check=False, capture_output=True)
    env = make_env(home, axon_home, llm.url)
    return AxonEnv(root=tmp_path, home=home, axon_home=axon_home, workspace=ws, llm=llm, env=env)


def run_axon(args: list[str], input: str | None = None, timeout: float = 60, env: dict[str, str] | None = None,
             cwd: Path | None = None) -> Result:
    cmd = [sys.executable, "-m", "axon", *args]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=input if input is not None else "", capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=str(cwd) if cwd else None)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        def _s(x: Any) -> str:
            return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")
        code, out, err = -9, _s(e.stdout), _s(e.stderr) + f"\n[harness] TIMEOUT after {timeout}s"
    return Result(code=code, stdout=out, stderr=err, duration=time.monotonic() - t0, args=args)


# ─── interactive (pty) ──────────────────────────────────────────────────────
class PtySession:
    """A running interactive ``axon`` attached to a pseudo-terminal."""

    def __init__(self, args: list[str], env: dict[str, str], cwd: Path, rows: int = 40, cols: int = 120) -> None:
        import pty
        import fcntl
        import struct
        import termios

        self._master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = {**env, "TERM": "xterm-256color"}
        env.pop("NO_COLOR", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "axon", *args], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, cwd=str(cwd), close_fds=True, start_new_session=True)
        os.close(slave)
        self._raw = b""

    @property
    def screen(self) -> str:
        """Everything printed so far, ANSI stripped."""
        return strip_ansi(self._raw.decode("utf-8", "replace")).replace("\r", "")

    def _pump(self, timeout: float) -> bool:
        import select
        r, _, _ = select.select([self._master], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(self._master, 65536)
        except OSError:
            return False
        if not data:
            return False
        self._raw += data
        return True

    def expect(self, pattern: str, timeout: float = 10.0, since: int = 0) -> str:
        """Wait until regex `pattern` appears in the (ANSI-stripped) screen after offset `since`."""
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            scr = self.screen
            if rx.search(scr, since):
                return scr
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (self.proc.poll() is not None and not self._pump(0.05)):
                raise TimeoutError(f"pattern {pattern!r} not seen within {timeout}s; screen tail:\n{scr[-3000:]}")
            self._pump(min(0.1, remaining))

    def mark(self) -> int:
        self.drain(0.05)
        return len(self.screen)

    def drain(self, quiet: float = 0.3) -> str:
        while self._pump(quiet):
            pass
        return self.screen

    def send(self, data: str) -> None:
        os.write(self._master, data.encode("utf-8"))

    def sendline(self, text: str) -> None:
        # type the text, then press Enter (CR, as a real terminal sends)
        self.send(text)
        time.sleep(0.15)
        self.send("\r")

    def wait(self, timeout: float = 10.0) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump(0.05)
            if self.proc.poll() is not None:
                self.drain(0.05)
                return self.proc.returncode
        return None

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 9)
            except Exception:
                self.proc.kill()
            self.proc.wait(5)
        try:
            os.close(self._master)
        except OSError:
            pass

    def __enter__(self) -> "PtySession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run_axon_pty(args: list[str], env: dict[str, str], cwd: Path, **kw: Any) -> PtySession:
    if IS_WINDOWS:
        import pytest
        pytest.skip("pty-driven REPL tests are POSIX-only")
    return PtySession(args, env, cwd, **kw)
