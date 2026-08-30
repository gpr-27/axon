"""
Git tool: perform git operations (status, diff, log, branch, commit, checkout, stash) with safety guards.
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

_ALLOWED_SUBCOMMANDS = {
    "status": ["status", "--short", "--branch"],
    "diff": ["diff"],
    "diff_staged": ["diff", "--cached"],
    "log": ["log", "-n", "10", "--oneline", "--decorate"],
    "branch": ["branch", "-a"],
    "commit": ["commit", "-m"],
    "checkout": ["checkout"],
    "stash": ["stash"],
    "add": ["add"],
}

class GitTool(Tool):
    name: ClassVar[str] = "Git"
    description: ClassVar[str] = (
        "Execute structured Git operations in the workspace repository. "
        "Supports 'status', 'diff', 'diff_staged', 'log', 'branch', 'add', 'commit', 'checkout', 'stash'."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "enum": ["status", "diff", "diff_staged", "log", "branch", "add", "commit", "checkout", "stash"],
                "description": "Git subcommand to execute",
            },
            "args": {
                "type": "string",
                "description": "Arguments for the subcommand (e.g. commit message, file path, branch name, log count)",
            },
        },
        "required": ["subcommand"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        subcmd = args.get("subcommand", "").strip()
        extra_args = args.get("args", "").strip()

        if subcmd not in _ALLOWED_SUBCOMMANDS:
            raise ToolError(f"Unsupported git subcommand '{subcmd}'. Allowed: {', '.join(_ALLOWED_SUBCOMMANDS)}")

        base_cmd = ["git"]
        if subcmd == "status":
            base_cmd.extend(["status", "--short", "--branch"])
        elif subcmd == "diff":
            base_cmd.append("diff")
            if extra_args:
                base_cmd.extend(extra_args.split())
        elif subcmd == "diff_staged":
            base_cmd.extend(["diff", "--cached"])
            if extra_args:
                base_cmd.extend(extra_args.split())
        elif subcmd == "log":
            count = extra_args if extra_args.isdigit() else "10"
            base_cmd.extend(["log", f"-n{count}", "--pretty=format:%h %ad | %s%d [%an]", "--date=short"])
        elif subcmd == "branch":
            base_cmd.extend(["branch", "-a"])
        elif subcmd == "add":
            if not extra_args:
                raise ToolError("Git add requires file path or '.' in 'args'.")
            base_cmd.extend(["add", extra_args])
        elif subcmd == "commit":
            if not extra_args:
                raise ToolError("Git commit requires commit message in 'args'.")
            base_cmd.extend(["commit", "-m", extra_args])
        elif subcmd == "checkout":
            if not extra_args:
                raise ToolError("Git checkout requires branch/file name in 'args'.")
            base_cmd.extend(["checkout", extra_args])
        elif subcmd == "stash":
            base_cmd.append("stash")
            if extra_args:
                base_cmd.extend(extra_args.split())

        try:
            res = subprocess.run(
                base_cmd,
                cwd=str(ctx.workspace),
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = (res.stdout or res.stderr).strip()
            if res.returncode != 0:
                if "not a git repository" in out.lower():
                    return f"[Workspace {ctx.workspace} is not a initialized Git repository. Run `git init` via Bash to initialize.]"
                return f"[Git {subcmd} returned exit code {res.returncode}]:\n{out}"
            return out or f"(Git {subcmd} completed with no output)"
        except Exception as e:
            raise ToolError(f"Git execution failed: {e}") from e
