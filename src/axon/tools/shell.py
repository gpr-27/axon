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
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar

from axon import envvars, interruptible
from axon import platform as plat
from axon.errors import ToolError
from axon.permissions import sandbox
from axon.tools.base import Tool, ToolContext, int_arg

_SCRUB_ENV_SUBSTRINGS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
# Variables that match a scrub substring but hold no secret, and that tools need: the ssh-agent
# socket (git over ssh), the X11 cookie path, and git's commit identity (GIT_AUTHOR_*).
# Proxy settings and GIT_CONFIG_* stay whatever they hold.
_ENV_KEEP = frozenset({"SSH_AUTH_SOCK", "XAUTHORITY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_ENV_KEEP_PREFIX = ("GIT_CONFIG_",)
# Package-index URLs keep working with the username and password cut out of them.
_ENV_URL_ONLY = frozenset({"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "NPM_CONFIG_REGISTRY", "UV_INDEX_URL"})
_URL_CREDS = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@\s]*:[^/@\s]*@", re.I)
_SECRET_VALUE = re.compile(r"^(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)")


def _is_secret_env(name: str) -> bool:
    up = name.upper()
    if up in _ENV_KEEP or up.startswith(_ENV_KEEP_PREFIX):
        return False
    up = up.replace("AUTHOR", "")  # GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL are identity, not credentials
    return any(sub in up for sub in _SCRUB_ENV_SUBSTRINGS)


def _scrub_value(name: str, value: str) -> str | None:
    """What a variable that is not secret by name may carry into a subprocess: None removes it, because
    its value looks like a credential; a package-index URL keeps working without its username and password."""
    if name.upper() in _ENV_KEEP or name.upper().startswith(_ENV_KEEP_PREFIX):
        return value
    if _URL_CREDS.match(value):
        return _URL_CREDS.sub(r"\1", value) if name.upper() in _ENV_URL_ONLY else None
    return None if _SECRET_VALUE.match(value) else value


MAX_TIMEOUT_S = 1800           # the ceiling on what the model may ask for; BASH_MAX_TIMEOUT_MS changes it
INLINE_OUTPUT_CHARS = 30_000   # output past this is saved to a file; the result shows its head and tail
MAX_READ_BACK_CHARS = 150_000  # hard ceiling of BASH_MAX_OUTPUT_LENGTH
MAX_SAVED_CHARS = 64 * 1024 * 1024
MAX_STREAMED_BYTES = 5 * 1024 ** 3  # a command whose output passes this is killed


def _ms_env(name: str) -> int | None:
    val = envvars.integer(name)
    return val if val is not None and val > 0 else None


def timeout_limits(settings: Any) -> tuple[int, int]:
    """(default, ceiling) in seconds for a foreground command. BASH_DEFAULT_TIMEOUT_MS replaces the default;
    the ceiling is the larger of BASH_MAX_TIMEOUT_MS (30 minutes when unset) and the default."""
    env_default = _ms_env("BASH_DEFAULT_TIMEOUT_MS")
    default = max(1, env_default // 1000) if env_default else int(getattr(settings, "bash_timeout_s", 0) or 180)
    env_max = _ms_env("BASH_MAX_TIMEOUT_MS")
    ceiling = max(1, env_max // 1000) if env_max else MAX_TIMEOUT_S
    return default, max(default, ceiling)


def background_disabled() -> bool:
    return envvars.on("AXON_DISABLE_BACKGROUND_TASKS")


def _bash_output_setting(workspace: Path | None) -> int | None:
    """The ``bashOutputMaxChars`` key (any settings file; a closer file wins), clamped to 4000-128000."""
    import json
    from axon import storage
    paths = [storage.axon_home() / "settings.json"]
    if workspace is not None:
        paths += [Path(workspace) / ".axon" / "settings.json", Path(workspace) / ".axon" / "settings.local.json"]
    val = None
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        got = data.get("bashOutputMaxChars") if isinstance(data, dict) else None
        if isinstance(got, int) and not isinstance(got, bool) and got > 0:
            val = got
    return None if val is None else max(4000, min(128_000, val))


def output_limits(settings: Any, workspace: Path | None) -> tuple[int, int]:
    """(inline ceiling, read-back window) in characters for a Bash result."""
    chars = _bash_output_setting(workspace)
    if chars:
        inline, window = chars, chars
    else:
        env = envvars.integer("BASH_MAX_OUTPUT_LENGTH")
        window = min(env, MAX_READ_BACK_CHARS) if env and env > 0 else INLINE_OUTPUT_CHARS
        inline = min(INLINE_OUTPUT_CHARS, window)
    cap = int(getattr(settings, "tool_output_cap", 0) or 0)   # Axon's own per-tool budget still applies when lower
    if cap > 0:
        inline, window = min(inline, cap), min(window, cap)
    return inline, window


_BENIGN_EXIT_1 = frozenset({"grep", "rg", "egrep", "fgrep", "diff", "test", "["})
_CHAIN_RE = re.compile(r"\|\||&&|[|;\n]")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _last_command_words(cmd: str) -> list[str]:
    """Words of the last command in a pipeline or chain, without leading VAR=value assignments."""
    import shlex
    last = _CHAIN_RE.split(cmd.strip())[-1] if '"' not in cmd and "'" not in cmd else None
    try:
        lex = shlex.shlex(cmd.strip() if last is None else last, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words: list[str] = []
        for tok in lex:
            if tok and set(tok) <= set("|&;\n"):
                words = []
            else:
                words.append(tok)
    except ValueError:
        return []
    while words and _ASSIGN_RE.match(words[0]):
        words.pop(0)
    return words


def benign_exit_one(cmd: str) -> bool:
    """Exit 1 from these reports a result (no match, files differ), not a failure."""
    words = _last_command_words(cmd)
    if not words:
        return False
    if words[0] == "git":
        return len(words) > 1 and words[1] == "grep"
    return Path(words[0]).name in _BENIGN_EXIT_1


# ── aliases, functions and options from the user's shell startup file ──
_snapshots: dict[str, str] = {}
_SNAP_MARK = "__AXON_SHELL_SNAPSHOT__"
_SNAP_SCRIPTS = {
    "zsh": f"printf '\\n{_SNAP_MARK}\\n'; alias -L; typeset -f; setopt 2>/dev/null | sed 's/^/setopt /'",
    "bash": f"printf '\\n{_SNAP_MARK}\\n'; alias -p; declare -f; shopt -p 2>/dev/null | grep -- ' -s '; echo 'shopt -s expand_aliases'",
    "sh": f". \"$HOME/.profile\" >/dev/null 2>&1; printf '\\n{_SNAP_MARK}\\n'; alias",
}


def shell_snapshot(info: plat.ShellInfo) -> str:
    """Opt-in (AXON_SHELL_SNAPSHOT=1): path of a script holding what the user's startup file defines, captured once per process. The commands run
    with no controlling terminal and no stdin, so an interactive shell cannot stop or hang on the startup file."""
    if info.path in _snapshots:
        return _snapshots[info.path]
    _snapshots[info.path] = ""
    script = _SNAP_SCRIPTS.get(info.kind)
    if script is None or plat.IS_WINDOWS or not envvars.on("AXON_SHELL_SNAPSHOT") or os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    argv = [info.path, "-c", script] if info.kind == "sh" else [info.path, "-ic", script]
    try:
        res = subprocess.run(argv, capture_output=True, timeout=5, stdin=subprocess.DEVNULL, start_new_session=True,
                             cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError):
        return ""
    text = res.stdout.decode("utf-8", errors="replace").split(_SNAP_MARK + "\n", 1)
    if len(text) != 2 or not text[1].strip() or len(text[1]) > 2_000_000:
        return ""
    import tempfile
    try:
        fd, path = tempfile.mkstemp(prefix="axon-shell-snapshot-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text[1])
    except OSError:
        return ""
    import atexit
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))
    _snapshots[info.path] = path
    return path


def _user_env_file() -> str:
    """A script the user named in AXON_ENV_FILE / CLAUDE_ENV_FILE before launching; it runs before each command."""
    raw = os.environ.get("AXON_ENV_FILE") or os.environ.get("CLAUDE_ENV_FILE") or ""
    return raw if raw and os.path.isfile(raw) else ""


def _apply_shell_prefix(argv: Any) -> Any:
    """AXON_SHELL_PREFIX wraps the command: ``prefix '<full shell invocation>'``; the wrapper re-evaluates $1."""
    prefix = os.environ.get("AXON_SHELL_PREFIX", "").strip()
    if not prefix or not isinstance(argv, list):
        return argv
    import shlex
    return shlex.split(prefix) + [shlex.join(argv)]


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
        f"never start editors, pagers or prompts. Default timeout {timeout_limits(None)[0]}s (timeout_s, max "
        f"{timeout_limits(None)[1]}s); on timeout the whole process tree is killed. Output over about 30,000 characters "
        f"is saved to a file; you get its head and tail and the file path. Prefer the "
        f"dedicated Read/Grep/Glob/Edit tools over cat/grep/find/sed. Always give a short 'description'."
    )


def _venv_bin(venv: Path) -> Path | None:
    """The executables dir of a virtualenv when it holds a Python interpreter."""
    for sub, exe in (("Scripts", "python.exe"), ("bin", "python"), ("bin", "python3")):
        if (venv / sub / exe).is_file():
            return venv / sub
    return None


def workspace_venv(workspace: Path) -> Path | None:
    """The project's own virtualenv (.venv/ or venv/ in the workspace root), if it has an interpreter.
    Same lookup order as the solver's interpreter detection."""
    for name in (".venv", "venv"):
        venv = workspace / name
        if _venv_bin(venv) is not None:
            return venv
    return None


def _apply_python_env(env: dict[str, str], workspace: Path) -> None:
    """Make `python` resolve to the project's interpreter in tool shells.

    An environment the user activated (VIRTUAL_ENV / CONDA_PREFIX pointing at an existing directory)
    always wins: its PATH is inherited untouched. Otherwise the workspace .venv/venv bin dir is
    prepended and VIRTUAL_ENV set, as `source .venv/bin/activate` would. A stale VIRTUAL_ENV (the env
    was moved or deleted; its activate script then keeps prepending a dead directory, so `python` is
    "command not found" even after activating) is dropped along with its PATH entry.
    """
    if any(env.get(k) and os.path.isdir(env[k]) for k in ("VIRTUAL_ENV", "CONDA_PREFIX")):
        return
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    entries = [e for e in env.get(path_key, "").split(os.pathsep) if e]
    original = list(entries)
    stale = env.pop("VIRTUAL_ENV", None)
    if stale:
        dead = {os.path.normcase(os.path.join(stale, d)) for d in ("bin", "Scripts")}
        entries = [e for e in entries if os.path.normcase(e.rstrip("/\\")) not in dead]
        env.pop("VIRTUAL_ENV_PROMPT", None)
    venv = workspace_venv(workspace)
    bindir = _venv_bin(venv) if venv is not None else None
    if venv is not None and bindir is not None:
        entries = [str(bindir)] + [e for e in entries if os.path.normcase(e) != os.path.normcase(str(bindir))]
        env["VIRTUAL_ENV"] = str(venv)
    if entries != original:
        env[path_key] = os.pathsep.join(entries)


def _python_hint(env: dict[str, str]) -> str:
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    if shutil.which("python3", path=path):
        return "(note: there is no `python` on this PATH; use `python3`, or the project's .venv/bin/python)"
    return "(note: no Python interpreter is on this PATH; create a .venv in the workspace or activate one before starting axon)"


def _child_env(workspace: Path | None = None, session_id: str = "") -> dict[str, str]:
    env = {}
    for k, v in os.environ.items():
        if _is_secret_env(k):
            continue
        kept = _scrub_value(k, v)
        if kept is not None:
            env[k] = kept
    env.update({"AXONCODE": "1", "AXON_CHILD_SESSION": "1", "AXON_PID": str(os.getpid())})
    if session_id:
        env["AXON_SESSION_ID"] = session_id
    if workspace is not None:
        _apply_python_env(env, Path(workspace))
    try:   # variables SessionStart/CwdChanged/... hooks persisted through CLAUDE_ENV_FILE
        from axon.hooks.envfile import read_env
        env.update(read_env(env))
    except Exception:
        pass
    env.update({"TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def capture_excerpt(text: str, limit: int) -> str:
    """``text`` within ``limit`` characters: its head and tail with the middle marked as elided."""
    if len(text) <= limit:
        return text
    half = limit // 2
    marker = "characters elided by Axon"
    return f"{text[:half]}\n\n[... {len(text) - 2 * half} {marker} ...]\n\n{text[-half:]}"


class _ShellDescription:
    """Descriptor so BashTool.description reflects the shell detected at runtime."""

    def __get__(self, obj: Any, owner: Any) -> str:
        return _describe()


class _Capture:
    """Keeps the head and tail of a stream within a character budget. Once the output outgrows
    the budget, everything is also spooled to a temp file (``spill_path``) so the complete
    output can be saved with the session instead of being dropped."""

    def __init__(self, cap: int, on_text: Callable[[str], None] | None = None, spill_after: int | None = None,
                 on_overflow: Callable[[], None] | None = None) -> None:
        self.cap = max(1000, cap)
        self.spill_after = spill_after if spill_after is not None else 2 * (self.cap // 2)
        self.on_overflow = on_overflow
        self.spilled = 0
        self.head: list[str] = []
        self.head_len = 0
        self.tail: collections.deque[str] = collections.deque()
        self.tail_len = 0
        self.total = 0
        self.on_text = on_text
        self.lock = threading.Lock()
        self.spill: Any = None
        self.spill_path: str = ""

    def _open_spill(self) -> None:
        import tempfile
        try:
            fd, path = tempfile.mkstemp(prefix="axon-bash-", suffix=".txt")
            self.spill = os.fdopen(fd, "w", encoding="utf-8", errors="replace", newline="")
            self.spill_path = path
            start = "".join(self.head) + "".join(self.tail)  # nothing was dropped yet
            self.spill.write(start)
            self.spilled = len(start)
        except OSError:
            self.spill, self.spill_path = None, ""

    def close_spill(self) -> str:
        with self.lock:
            if self.spill is not None:
                try:
                    self.spill.close()
                except OSError:
                    pass
                self.spill = None
            return self.spill_path

    def add(self, text: str) -> None:
        if not text:
            return
        chunk = text
        with self.lock:
            if self.spill is None and not self.spill_path and self.total + len(text) > self.spill_after:
                self._open_spill()
            if self.spill is not None and self.spilled < MAX_SAVED_CHARS:   # the saved copy stops at 64 MiB
                try:
                    self.spill.write(text[:MAX_SAVED_CHARS - self.spilled])
                    self.spilled += len(text)
                except (OSError, ValueError):
                    pass
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
                self.on_text(chunk)
            except Exception:
                self.on_text = None
        if self.on_overflow is not None and self.total > MAX_STREAMED_BYTES:
            overflow, self.on_overflow = self.on_overflow, None
            overflow()

    def text(self) -> str:
        with self.lock:
            head = "".join(self.head)
            tail = "".join(self.tail)[-(self.cap // 2):]   # a single chunk may be larger than the budget
            elided = self.total - len(head) - len(tail)
            if elided <= 0:
                return head + tail
            return f"{head}\n\n[... {elided} characters elided by Axon ...]\n\n{tail}"


def _session_of(ctx: Any) -> tuple[Path, str]:
    agent = getattr(ctx, "agent", None)
    sid = str(getattr(getattr(agent, "session", None), "active_session_id", "") or "")
    return Path(ctx.workspace), sid


def _persist_spill(spill: str, ctx: Any, cmd: str, post: Callable[[str], str] | None = None) -> str:
    """Move a spooled full output into the project's outputs/ folder; returns a note for the model.
    ``post`` cleans the text further (Bash strips its cwd-tracking sentinel line)."""
    try:
        from axon import storage
        with open(spill, "r", encoding="utf-8", errors="replace") as f:
            full = _clean_output(f.read())
        if post is not None:
            full = post(full).strip("\n") + "\n"
        ws, sid = _session_of(ctx)
        name = "bash-" + re.sub(r"[^A-Za-z0-9]+", "-", cmd)[:40].strip("-")
        saved = storage.save_full_output(ws, sid, name, full)
        return f"(full output, {len(full):,} chars, saved to {saved})" if saved else ""
    except OSError:
        return ""
    finally:
        try:
            os.unlink(spill)
        except OSError:
            pass


def _discard_spill(capture: _Capture) -> None:
    """Close and delete the spooled output of a command that was interrupted (nobody will read it)."""
    path = capture.close_spill()
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _clean_output(text: str) -> str:
    text = plat.strip_ansi(text).replace("\r\n", "\n")
    # Collapse carriage-return progress redraws to their final state.
    if "\r" in text:
        # A trailing "\r" (progress line that ended without a newline, or a "\r\n" split across reads)
        # is not a redraw: keep the text before it.
        text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
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
        self._seen_cwd: str | None = None

    # ── cwd tracking ──────────────────────────────────────────────────────
    @property
    def _sentinel(self) -> str:
        return f"__AXON_CWD_{self._token}__"

    def current_dir(self, workspace: Path) -> str:
        ws = str(workspace)
        if self._cwd_ws != ws or not self._cwd or not os.path.isdir(self._cwd):
            self._cwd, self._cwd_ws = ws, ws
        return self._cwd

    def _prelude(self, info: plat.ShellInfo) -> str:
        """Lines run before the command in the same shell: the user's startup definitions and AXON_ENV_FILE."""
        import shlex
        lines = []
        snap = shell_snapshot(info)
        if snap:
            lines.append(f". {shlex.quote(snap)} >/dev/null 2>&1")
        env_file = _user_env_file()
        if env_file:
            lines.append(f". {shlex.quote(env_file)}")
        return "\n".join(lines) + "\n" if lines else ""

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
        script = f"{self._prelude(info)}{cmd}\n__axon_ec=$?\nprintf '\\n%s%s\\n' '{s}' \"$PWD\"\nexit $__axon_ec"
        return plat.shell_command_argv(script, info)

    def _extract_cwd(self, text: str) -> str:
        s = self._sentinel
        idx = text.rfind(s)
        if idx == -1:
            return text
        line_end = text.find("\n", idx)
        new_dir = text[idx + len(s): line_end if line_end != -1 else len(text)].strip()
        if new_dir and os.path.isdir(new_dir):
            self._cwd = self._seen_cwd = new_dir
        before = text[:idx]
        if before.endswith("\n"):
            before = before[:-1]
        return before + (text[line_end + 1:] if line_end != -1 else "")

    # ── execution ─────────────────────────────────────────────────────────
    def _keep_cwd(self, ctx: ToolContext, before: str | None) -> str:
        """Decide what the directory the command ended in means for the next one. Only the main session carries it
        over, only inside the project or an additional working directory, and not with
        AXON_BASH_MAINTAIN_PROJECT_WORKING_DIR. Returns a notice for the model."""
        seen, self._seen_cwd = self._seen_cwd, None
        if envvars.on("AXON_BASH_MAINTAIN_PROJECT_WORKING_DIR"):
            self._cwd = str(ctx.workspace)
            return ""
        if getattr(getattr(ctx, "agent", None), "parent_agent", None) is not None:
            self._cwd = before   # subagent sessions never carry over working directory changes
            return ""
        if not seen:
            return ""
        roots = [Path(ctx.workspace)] + [Path(str(d)) for d in (getattr(ctx.settings, "additional_dirs", None) or [])]
        try:
            target = Path(seen).resolve()
            inside = any(target == r.resolve() or r.resolve() in target.parents for r in roots)
        except OSError:
            inside = False
        if inside:
            return ""
        self._cwd = str(ctx.workspace)
        return f"Shell cwd was reset to {ctx.workspace}"

    def _subagent_cwd(self, ctx: ToolContext) -> str | None:
        return str(ctx.workspace) if getattr(getattr(ctx, "agent", None), "parent_agent", None) is not None else None

    def _start_background(self, cmd: str, cwd: str, env: dict[str, str], info: plat.ShellInfo, ctx: ToolContext) -> str:
        log_path: Path | None = None
        try:
            from axon import storage
            ws, sid = _session_of(ctx)
            log_path = storage.outputs_dir(ws, sid) / f"background-{int(time.time() * 1000)}.log"
        except Exception:
            log_path = None
        try:
            shell = BACKGROUND.start(self._prelude(info) + cmd if not info.is_powershell and info.kind != "cmd" else cmd,
                                     cwd, env, info, log_path=log_path,
                                     wrap=lambda argv: _apply_shell_prefix(sandbox.wrap_argv(argv, ctx.settings)),
                                     display=cmd)
        except FileNotFoundError as e:
            raise ToolError(f"Shell not found ({info.name}): {e}. Set AXON_SHELL to a valid shell.") from e
        except OSError as e:
            raise ToolError(f"Failed to start {info.name}: {e}") from e
        logged = f"\nIts complete output is also logged to {log_path}." if log_path else ""
        return (f"Started background shell {shell.id} (pid {shell.proc.pid}) in {cwd}.\n"
                f"Use BashOutput with shell_id={shell.id!r} to read its output and KillShell to stop it.{logged}")

    # ── execution ─────────────────────────────────────────────────────────
    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            raise ToolError("Bash requires a non-empty 'command'.")
        default_t, ceiling = timeout_limits(ctx.settings)
        timeout = int_arg(args, "timeout_s", default_t, lo=1, hi=ceiling, strict=True)
        inline, window = output_limits(ctx.settings, Path(ctx.workspace))
        info = self.shell_for()
        cwd_before = self._cwd
        cwd = self._subagent_cwd(ctx) or self.current_dir(ctx.workspace)
        env = _child_env(ctx.workspace, _session_of(ctx)[1])
        if args.get("run_in_background"):
            if background_disabled():
                raise ToolError("Background commands are disabled (AXON_DISABLE_BACKGROUND_TASKS). Run it in the foreground.")
            return self._start_background(cmd, cwd, env, info, ctx)
        stream_cb = getattr(ctx, "on_output", None)
        proc_box: list[subprocess.Popen[bytes]] = []
        capture = _Capture(window, stream_cb if callable(stream_cb) else None, spill_after=inline,
                           on_overflow=lambda: proc_box and plat.kill_process_tree(proc_box[0]))
        argv = _apply_shell_prefix(sandbox.wrap_argv(self._wrap(cmd, info), ctx.settings))
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, **plat.new_process_group_kwargs())
        except FileNotFoundError as e:
            raise ToolError(f"Shell not found ({info.name}): {e}. Set AXON_SHELL to a valid shell.") from e
        except OSError as e:
            raise ToolError(f"Failed to start {info.name}: {e}") from e
        proc_box.append(proc)

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
            interruptible.wait_process(proc, timeout)   # in slices: on Windows one long wait would hold Esc
        except subprocess.TimeoutExpired:
            timed_out = True
            plat.kill_process_tree(proc)
        except KeyboardInterrupt:
            plat.kill_process_tree(proc)
            _discard_spill(capture)
            raise
        reader.join(timeout=2.0 if timed_out else 1.0)
        detached = reader.is_alive()  # a background child still holds the pipe; the daemon reader drains it
        output = self._extract_cwd(_clean_output(capture.text())).strip("\n")
        code = proc.returncode
        valid = code in (0, None) or (code == 1 and benign_exit_one(cmd))
        marker = "characters elided by Axon ...]"
        if len(output) > inline and not (marker in output and window <= inline):   # past the inline budget: head + tail
            output = capture_excerpt(output, inline)
        full_note = ""
        spill = capture.close_spill()   # also when detached: what was captured so far is kept, not left in the temp dir
        if spill:
            full_note = _persist_spill(spill, ctx, cmd, self._extract_cwd)
            if full_note and marker in output:  # keep the tail intact: the note goes inside the marker
                output = output.replace(marker, f"characters elided by Axon {full_note} ...]", 1)
                full_note = ""
        cwd_note = self._keep_cwd(ctx, cwd_before)
        elapsed = time.monotonic() - started
        if timed_out:
            tail = output[-4000:]
            raise ToolError(f"Command timed out after {timeout}s; the process tree was killed.\nPartial output:\n{tail}"
                            + (f"\n{full_note}" if full_note else ""))
        notes = []
        if sandbox.enabled(ctx.settings):
            failed = sandbox.start_failure(output, code)
            if failed:
                raise ToolError(f"{sandbox.SANDBOX_TAG} the sandbox could not start, so the command did not run: {failed}")
            note = sandbox.denial_note(output, code)
            if note:
                notes.append(note)
        if detached:
            notes.append("(a background process is still running and holds the output pipe; its output is not captured)")
        if "python: command not found" in output or "python: not found" in output:
            notes.append(_python_hint(env))
        if cwd_note:
            notes.append(cwd_note)
        elif self._cwd and os.path.normcase(self._cwd) != os.path.normcase(str(ctx.workspace)):
            notes.append(f"(cwd: {self._cwd})")
        if full_note:
            notes.append(full_note)
        suffix = ("\n" + "\n".join(notes)) if notes else ""
        if valid:
            if code == 1 and not output:
                return f"(Command exited with code 1 and no output: no match, or the compared files differ){suffix}"
        if not valid:
            why = f"non-zero code {code}"
            if code < 0:  # POSIX: terminated by a signal
                try:
                    import signal
                    why += f" (killed by {signal.Signals(-code).name})"
                except (ValueError, AttributeError):
                    pass
            if not output:
                return f"Command exited with {why} and no output.{suffix}"
            return f"Command exited with {why}.\nOutput:\n{output}{suffix}"
        if not output:
            return f"(Command completed with exit code 0 and no output in {elapsed:.1f}s){suffix}"
        return output + suffix

    def shell_for(self) -> plat.ShellInfo:
        return plat.shell_info()

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
    def __init__(self, shell_id: str, command: str, proc: subprocess.Popen[bytes], log_path: Path | None = None) -> None:
        self.id = shell_id
        self.command = command
        self.proc = proc
        self.started = time.time()
        self.stream = _Stream()
        self.log_path = log_path
        self._log: Any = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log = open(log_path, "a", encoding="utf-8", errors="replace", newline="")
                self._log.write(f"$ {command}\n")
                self._log.flush()
            except OSError:
                self._log = None
        self.reader = threading.Thread(target=self._pump, daemon=True, name=f"bg-shell-{shell_id}")
        self.reader.start()

    def _pump(self) -> None:
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            for chunk in iter(lambda: self.proc.stdout.read1(65536), b""):  # type: ignore[union-attr]
                text = dec.decode(chunk)
                self.stream.add(text)
                self._write_log(text)
            tail = dec.decode(b"", final=True)
            self.stream.add(tail)
            self._write_log(tail)
        except (OSError, ValueError):
            pass
        finally:
            if self._log is not None:
                try:
                    self._log.close()
                except OSError:
                    pass
                self._log = None

    def _write_log(self, text: str) -> None:
        if self._log is not None and text:
            try:
                self._log.write(text)
                self._log.flush()
            except (OSError, ValueError):
                self._log = None

    @property
    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited with code {code}"


class BackgroundShells:
    def __init__(self) -> None:
        self._shells: dict[str, BackgroundShell] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def start(self, command: str, cwd: str, env: dict[str, str], info: plat.ShellInfo,
              log_path: Path | None = None, wrap: Callable[[Any], Any] | None = None,
              display: str | None = None) -> BackgroundShell:
        argv = plat.shell_command_argv(command, info)
        proc = subprocess.Popen(wrap(argv) if wrap else argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **plat.new_process_group_kwargs())
        with self._lock:
            self._seq += 1
            shell = BackgroundShell(f"bash_{self._seq}", display if display is not None else command, proc,
                                    log_path=log_path)
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
        log = getattr(shell, "log_path", None)
        note = (f"\n[{skipped} earlier characters not shown here" + (f"; complete log: {log}" if log else "") + "]"
                if skipped else "")
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
        shell_id = str(args.get("shell_id", "")).strip()
        if BACKGROUND.get(shell_id).proc.poll() is not None:
            shell = BACKGROUND.get(shell_id)
            return f"{shell.id} ({shell.command}) had already {shell.status}; nothing to kill."
        shell = BACKGROUND.kill(shell_id)
        return f"Killed {shell.id} ({shell.command}); {shell.status}."
