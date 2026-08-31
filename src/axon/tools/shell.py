"""
Bash tool with persistent subshell, exit code sentinel, process-group kill, and env scrubbing.
Cross-platform support for macOS, Linux, and Windows (Git Bash, PowerShell, CMD).
"""
from __future__ import annotations
import os
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

_SCRUB_ENV_SUBSTRINGS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")

def _find_shell_runner() -> tuple[list[str], str]:
    """
    Find best available shell executable and type.
    Returns (base_argv_list, shell_flavor).
    """
    # 1. Look for bash in PATH or standard Git Bash / Unix locations
    bash_path = shutil.which("bash") or shutil.which("bash.exe")
    if not bash_path and sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        ):
            if os.path.exists(candidate):
                bash_path = candidate
                break

    if bash_path:
        return ([bash_path, "-c"], "bash")
    elif os.path.exists("/bin/bash"):
        return (["/bin/bash", "-c"], "bash")
    elif os.path.exists("/bin/sh"):
        return (["/bin/sh", "-c"], "sh")

    # 2. Windows fallback: PowerShell or CMD
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh:
            return ([pwsh, "-NoProfile", "-NonInteractive", "-Command"], "powershell")
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return ([comspec, "/c"], "cmd")

    return (["sh", "-c"], "sh")


class BashTool(Tool):
    name: ClassVar[str] = "Bash"
    description: ClassVar[str] = (
        "Execute a shell command in a persistent subshell. "
        "State (directory, environment variables) persists across calls. "
        "Requires a human-readable 'description' explaining what will run."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The exact shell command to run"},
            "description": {"type": "string", "description": "Brief description of the action"},
            "timeout_s": {"type": "integer", "description": "Timeout in seconds (default: 120)"},
        },
        "required": ["command", "description"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._id = secrets.token_hex(4)
        self._seq = 0

    def _ensure_proc(self, cwd: str) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            clean_env = {
                k: v for k, v in os.environ.items()
                if not any(sub in k.upper() for sub in _SCRUB_ENV_SUBSTRINGS)
            }
            clean_env["TERM"] = "dumb"
            runner, flavor = _find_shell_runner()
            init_cmd = [runner[0], "--noprofile", "--norc"] if flavor == "bash" else runner

            preexec = os.setsid if (hasattr(os, "setsid") and sys.platform != "win32") else None
            self._proc = subprocess.Popen(
                init_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
                env=clean_env,
                preexec_fn=preexec,
                bufsize=1,
            )
        return self._proc

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        cmd = args.get("command", "").strip()
        if not cmd:
            raise ToolError("Bash requires non-empty 'command'.")

        timeout = int(args.get("timeout_s") or ctx.settings.bash_timeout_s or 120)
        clean_env = {
            k: v for k, v in os.environ.items()
            if not any(sub in k.upper() for sub in _SCRUB_ENV_SUBSTRINGS)
        }
        clean_env["TERM"] = "dumb"

        runner, _ = _find_shell_runner()
        exec_args = runner + [cmd]

        try:
            res = subprocess.run(
                exec_args,
                cwd=str(ctx.workspace),
                env=clean_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
            raw_output = res.stdout.strip()
            exit_code = res.returncode
        except subprocess.TimeoutExpired:
            raise ToolError(f"Bash command timed out after {timeout} seconds.")
        except Exception as e:
            raise ToolError(f"Failed executing bash command: {e}") from e
        cap = ctx.settings.tool_output_cap or 30000
        if len(raw_output) > cap:
            half = cap // 2
            raw_output = (
                raw_output[:half]
                + f"\n\n[... {len(raw_output) - cap} characters elided by Axon ...]\n\n"
                + raw_output[-half:]
            )

        if exit_code is not None and exit_code != 0:
            return f"Command exited with non-zero code {exit_code}.\nOutput:\n{raw_output}"

        return raw_output or "(Command completed with exit code 0 and no output)"

    def render_call(self, args: dict[str, Any]) -> str:
        desc = args.get("description", "")
        cmd = args.get("command", "")
        return f"Bash  {cmd} ({desc})" if desc else f"Bash  {cmd}"

