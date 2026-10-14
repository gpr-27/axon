"""
Bash tool: run a command in the platform shell (bash/sh on POSIX; PowerShell 7, Windows
PowerShell or cmd.exe on Windows; AXON_SHELL overrides) with streamed, capped UTF-8 output,
a timeout that kills the whole process tree, and a working directory that persists between
calls.
"""
from __future__ import annotations

import codecs
import collections
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar

from axon import platform as plat
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

_SCRUB_ENV_SUBSTRINGS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
MAX_TIMEOUT_S = 1800


def _find_shell_runner() -> tuple[list[str], str]:
    """(argv prefix, flavor) for the active shell; flavor is bash | sh | powershell | cmd."""
    info = plat.shell_info()
    flavor = "powershell" if info.is_powershell else ("cmd" if info.kind == "cmd" else ("bash" if info.kind == "bash" else "sh"))
    return plat.default_shell(), flavor


def _os_label() -> str:
    if plat.IS_WINDOWS:
        return "Windows"
    return "macOS" if sys.platform == "darwin" else ("Linux" if sys.platform.startswith("linux") else sys.platform)


def shell_syntax_hint(info: plat.ShellInfo | None = None) -> str:
    info = info or plat.shell_info()
    if info.is_powershell:
        chain = "`&&`/`||` work (PowerShell 7+)" if info.kind == "pwsh" else "chain with `;` (`&&` is NOT available in Windows PowerShell 5.1)"
        return (f"Write PowerShell, not bash: Get-ChildItem, Select-String, Get-Content, Remove-Item, $env:NAME = 'v', "
                f"{chain}. Native tools (git, python, npm) work as usual.")
    if info.kind == "cmd":
        return "Write cmd.exe syntax: dir, type, set NAME=value, `&&` to chain, %NAME% for variables."
    return "Write POSIX shell / bash syntax."


def _describe() -> str:
    info = plat.shell_info()
    return (
        f"Run a shell command and return its combined stdout/stderr and exit code. Active shell: {info.name} on "
        f"{_os_label()}. {shell_syntax_hint(info)} The working directory persists between calls (use cd); "
        f"environment variables and other shell state do not. Commands run non-interactively with stdin closed: "
        f"never start editors, pagers or prompts. Default timeout {180}s (timeout_s, max {MAX_TIMEOUT_S}); on "
        f"timeout the whole process tree is killed. Very long output is truncated in the middle. Prefer the "
        f"dedicated Read/Grep/Glob/Edit tools over cat/grep/find/sed. Always give a short 'description'."
    )


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(sub in k.upper() for sub in _SCRUB_ENV_SUBSTRINGS)}
    env.update({"TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


class _ShellDescription:
    """Descriptor so BashTool.description reflects the shell detected at runtime."""

    def __get__(self, obj: Any, owner: Any) -> str:
        return _describe()


class _Capture:
    """Keeps the head and tail of a stream within a character budget."""

    def __init__(self, cap: int, on_text: Callable[[str], None] | None = None) -> None:
        self.cap = max(1000, cap)
        self.head: list[str] = []
        self.head_len = 0
        self.tail: collections.deque[str] = collections.deque()
        self.tail_len = 0
        self.total = 0
        self.on_text = on_text
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            self.total += len(text)
            half = self.cap // 2
            if self.head_len < half:
                take = text[: half - self.head_len]
                self.head.append(take)
                self.head_len += len(take)
                text = text[len(take):]
            if text:
                self.tail.append(text)
                self.tail_len += len(text)
                while self.tail and self.tail_len - len(self.tail[0]) >= half:
                    self.tail_len -= len(self.tail.popleft())
        if self.on_text is not None:
            try:
                self.on_text(text)
            except Exception:
                self.on_text = None

    def text(self) -> str:
        with self.lock:
            head = "".join(self.head)
            tail = "".join(self.tail)
            kept = len(head) + len(tail)
            if self.total <= kept:
                return head + tail
            tail = tail[-(self.cap // 2):]
            elided = self.total - len(head) - len(tail)
            return f"{head}\n\n[... {elided} characters elided by Axon ...]\n\n{tail}"


def _clean_output(text: str) -> str:
    text = plat.strip_ansi(text).replace("\r\n", "\n")
    # Collapse carriage-return progress redraws to their final state.
    if "\r" in text:
        text = "\n".join(line.rsplit("\r", 1)[-1] if line.strip("\r") else "" for line in text.split("\n"))
    return text


class BashTool(Tool):
    name: ClassVar[str] = "Bash"
    description = _ShellDescription()  # type: ignore[assignment]
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to run in the active shell"},
            "description": {"type": "string", "description": "Short description of what the command does"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_S,
                          "description": "Timeout in seconds (default 180)"},
            "run_in_background": {"type": "boolean",
                                  "description": "Start without waiting (dev servers, watchers). Returns a shell_id "
                                                 "for BashOutput / KillShell."},
        },
        "required": ["command", "description"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def __init__(self) -> None:
        self._token = secrets.token_hex(6)
        self._cwd: str | None = None
        self._cwd_ws: str | None = None

    # ── cwd tracking ──────────────────────────────────────────────────────
    @property
    def _sentinel(self) -> str:
        return f"__AXON_CWD_{self._token}__"

    def current_dir(self, workspace: Path) -> str:
        ws = str(workspace)
        if self._cwd_ws != ws or not self._cwd or not os.path.isdir(self._cwd):
            self._cwd, self._cwd_ws = ws, ws
        return self._cwd

    def _wrap(self, cmd: str, info: plat.ShellInfo) -> list[str] | str:
        s = self._sentinel
        if info.is_powershell:
            script = (
                f"{cmd}\n"
                "$__axon_ok = $?\n"
                "$__axon_ec = if ($LASTEXITCODE) { $LASTEXITCODE } elseif (-not $__axon_ok) { 1 } else { 0 }\n"
                f"[Console]::Out.WriteLine(''); [Console]::Out.WriteLine('{s}' + (Get-Location).ProviderPath)\n"
                "exit $__axon_ec"
            )
            return plat.shell_command_argv(script, info)
        if info.kind == "cmd":
            if "!" in cmd:  # delayed expansion would mangle '!': run without cwd tracking
                return plat.shell_command_argv(cmd, info)
            one = " & ".join(line for line in cmd.splitlines() if line.strip()) or cmd
            return (f'"{info.path}" /d /s /v:on /c "chcp 65001>nul & {one} & set __AXON_EC=!errorlevel! & '
                    f'echo.& echo {s}!CD!& exit /b !__AXON_EC!"')
        script = f"{cmd}\n__axon_ec=$?\nprintf '\\n%s%s\\n' '{s}' \"$PWD\"\nexit $__axon_ec"
        return plat.shell_command_argv(script, info)

    def _extract_cwd(self, text: str) -> str:
        s = self._sentinel
        idx = text.rfind(s)
        if idx == -1:
            return text
        line_end = text.find("\n", idx)
        new_dir = text[idx + len(s): line_end if line_end != -1 else len(text)].strip()
        if new_dir and os.path.isdir(new_dir):
            self._cwd = new_dir
        before = text[:idx]
        if before.endswith("\n"):
            before = before[:-1]
        return before + (text[line_end + 1:] if line_end != -1 else "")

    # ── execution ─────────────────────────────────────────────────────────
    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            raise ToolError("Bash requires a non-empty 'command'.")
        try:
            timeout = int(args.get("timeout_s") or getattr(ctx.settings, "bash_timeout_s", 0) or 180)
        except (TypeError, ValueError):
            timeout = 180
        timeout = max(1, min(MAX_TIMEOUT_S, timeout))
        cap = int(getattr(ctx.settings, "tool_output_cap", 0) or 30000)
        info = plat.shell_info()
        cwd = self.current_dir(ctx.workspace)
        env = _child_env()
        if args.get("run_in_background"):
            shell = BACKGROUND.start(cmd, cwd, env, info)
            return (f"Started background shell {shell.id} (pid {shell.proc.pid}) in {cwd}.\n"
                    f"Use BashOutput with shell_id={shell.id!r} to read its output and KillShell to stop it.")
        stream_cb = getattr(ctx, "on_output", None)
        capture = _Capture(cap, stream_cb if callable(stream_cb) else None)
        argv = self._wrap(cmd, info)
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **plat.new_process_group_kwargs())
        except FileNotFoundError as e:
            raise ToolError(f"Shell not found ({info.name}): {e}. Set AXON_SHELL to a valid shell.") from e
        except OSError as e:
            raise ToolError(f"Failed to start {info.name}: {e}") from e

        def pump() -> None:
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
            assert proc.stdout is not None
            try:
                for chunk in iter(lambda: proc.stdout.read1(65536), b""):  # type: ignore[union-attr]
                    capture.add(dec.decode(chunk))
                capture.add(dec.decode(b"", final=True))
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=pump, daemon=True, name="bash-output")
        reader.start()
        started = time.monotonic()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            plat.kill_process_tree(proc)
        except KeyboardInterrupt:
            plat.kill_process_tree(proc)
            raise
        reader.join(timeout=2.0 if timed_out else 1.0)
        detached = reader.is_alive()  # a background child still holds the pipe; the daemon reader drains it
        output = self._extract_cwd(_clean_output(capture.text())).strip("\n")
        elapsed = time.monotonic() - started
        if timed_out:
            tail = output[-4000:]
            raise ToolError(f"Command timed out after {timeout}s; the process tree was killed.\nPartial output:\n{tail}")
        code = proc.returncode
        notes = []
        if detached:
            notes.append("(a background process is still running and holds the output pipe; its output is not captured)")
        if self._cwd and os.path.normcase(self._cwd) != os.path.normcase(str(ctx.workspace)):
            notes.append(f"(cwd: {self._cwd})")
        suffix = ("\n" + "\n".join(notes)) if notes else ""
        if code not in (0, None):
            return f"Command exited with non-zero code {code}.\nOutput:\n{output}{suffix}"
        if not output:
            return f"(Command completed with exit code 0 and no output in {elapsed:.1f}s){suffix}"
        return output + suffix

    def render_call(self, args: dict[str, Any]) -> str:
        desc = args.get("description", "")
        cmd = args.get("command", "")
        return f"Bash  {cmd} ({desc})" if desc else f"Bash  {cmd}"



# ─────────────────────────────────────────────────────────────────────────────
# Background shells
# ─────────────────────────────────────────────────────────────────────────────

class _Stream:
    """Append-only text buffer with a size cap and a read cursor for incremental reads."""

    def __init__(self, cap: int = 2_000_000) -> None:
        self.cap = cap
        self.buf = ""
        self.base = 0      # absolute offset of buf[0]
        self.read_at = 0   # absolute offset already returned to the model
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        with self.lock:
            self.buf += text
            if len(self.buf) > self.cap:
                drop = len(self.buf) - self.cap
                self.buf = self.buf[drop:]
                self.base += drop

    def read_new(self) -> tuple[str, int]:
        with self.lock:
            start = max(self.read_at, self.base)
            skipped = start - self.read_at
            text = self.buf[start - self.base:]
            self.read_at = self.base + len(self.buf)
            return text, skipped


class BackgroundShell:
    def __init__(self, shell_id: str, command: str, proc: subprocess.Popen[bytes]) -> None:
        self.id = shell_id
        self.command = command
        self.proc = proc
        self.started = time.time()
        self.stream = _Stream()
        self.reader = threading.Thread(target=self._pump, daemon=True, name=f"bg-shell-{shell_id}")
        self.reader.start()

    def _pump(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            for chunk in iter(lambda: self.proc.stdout.read1(65536), b""):  # type: ignore[union-attr]
                self.stream.add(dec.decode(chunk))
            self.stream.add(dec.decode(b"", final=True))
        except (OSError, ValueError):
            pass

    @property
    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited with code {code}"


class BackgroundShells:
    def __init__(self) -> None:
        self._shells: dict[str, BackgroundShell] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def start(self, command: str, cwd: str, env: dict[str, str], info: plat.ShellInfo) -> BackgroundShell:
        proc = subprocess.Popen(plat.shell_command_argv(command, info), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **plat.new_process_group_kwargs())
        with self._lock:
            self._seq += 1
            shell = BackgroundShell(f"bash_{self._seq}", command, proc)
            self._shells[shell.id] = shell
        return shell

    def get(self, shell_id: str) -> BackgroundShell:
        with self._lock:
            shell = self._shells.get(shell_id)
        if shell is None:
            known = ", ".join(self._shells) or "none"
            raise ToolError(f"No background shell {shell_id!r} (known: {known}).")
        return shell

    def all(self) -> list[BackgroundShell]:
        with self._lock:
            return list(self._shells.values())

    def kill(self, shell_id: str) -> BackgroundShell:
        shell = self.get(shell_id)
        if shell.proc.poll() is None:
            plat.kill_process_tree(shell.proc)
        return shell

    def kill_all(self) -> None:
        for shell in self.all():
            if shell.proc.poll() is None:
                plat.kill_process_tree(shell.proc, grace=0.2)


BACKGROUND = BackgroundShells()
import atexit as _atexit  # noqa: E402
_atexit.register(BACKGROUND.kill_all)


class BashOutputTool(Tool):
    name: ClassVar[str] = "BashOutput"
    description: ClassVar[str] = (
        "Read new output from a background shell started with Bash(run_in_background=true) since the last read, "
        "plus its status. Optional 'filter' is a regex; only matching lines are returned."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell id, e.g. bash_1"},
            "filter": {"type": "string", "description": "Regex; only lines matching it are returned"},
        },
        "required": ["shell_id"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        import re
        shell = BACKGROUND.get(str(args.get("shell_id", "")).strip())
        text, skipped = shell.stream.read_new()
        text = _clean_output(text)
        pattern = str(args.get("filter") or "")
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                raise ToolError(f"Invalid filter regex: {e}") from e
            text = "\n".join(ln for ln in text.splitlines() if rx.search(ln))
        cap = int(getattr(ctx.settings, "tool_output_cap", 0) or 30000)
        if len(text) > cap:
            skipped += len(text) - cap
            text = text[-cap:]
        head = f"[{shell.id}] {shell.status} · {time.time() - shell.started:.0f}s · {shell.command}"
        note = f"\n[{skipped} earlier characters dropped]" if skipped else ""
        return f"{head}{note}\n{text.strip(chr(10)) or '(no new output)'}"


class KillShellTool(Tool):
    name: ClassVar[str] = "KillShell"
    description: ClassVar[str] = "Stop a background shell (and all of its child processes) by shell_id."
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"shell_id": {"type": "string", "description": "Background shell id, e.g. bash_1"}},
        "required": ["shell_id"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        shell = BACKGROUND.kill(str(args.get("shell_id", "")).strip())
        return f"Killed {shell.id} ({shell.command}); {shell.status}."
