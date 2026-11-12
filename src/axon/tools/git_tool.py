"""Git: read-mostly git access. status/diff/log/blame/show/... run without asking; anything that changes the
repository goes through the normal permission prompt (the permission engine decides by subcommand)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.permissions.rules import git_call
from axon.tools.base import Tool, ToolContext, cap_text, output_cap, resolve_path

READ_SUBCOMMANDS = ("status", "diff", "log", "blame", "show", "branch", "tag", "remote", "rev-parse", "ls-files",
                    "describe", "shortlog")
WRITE_SUBCOMMANDS = ("add", "restore", "commit", "checkout", "switch", "stash", "merge", "rebase", "cherry-pick",
                     "revert", "reset", "mv", "rm", "fetch", "pull", "push")
# Flags that make git run helper programs, write files, or read outside the repository.
_BLOCKED_FLAG = re.compile(
    r"^(--config-env.*|--exec-path.*|--output.*|-O.*|--open-files-in-pager.*|--ext-diff|--no-index|"
    r"--upload-pack.*|--receive-pack.*|--exec.*|--git-dir.*|--work-tree.*|--textconv|--paginate|"
    r"--contents.*|--ignore-revs-file.*)$"          # blame --contents FILE shows any file; --ignore-revs-file quotes its first bad line
)
_BLOCKED_LONG = ("--config-env", "--exec-path", "--output", "--open-files-in-pager", "--ext-diff", "--no-index", "--upload-pack",
                 "--receive-pack", "--exec", "--git-dir", "--work-tree", "--textconv", "--paginate", "--contents", "--ignore-revs-file")


def _abbreviates_blocked(name: str) -> bool:
    """git takes any unambiguous prefix of a long flag, so ``--upl=cmd`` is ``--upload-pack=cmd``."""
    return any(b.startswith(name) for b in _BLOCKED_LONG)


_TEXT_SUBS = ("diff", "log", "show")


class GitTool(Tool):
    name: ClassVar[str] = "Git"
    description: ClassVar[str] = (
        "Run git in the workspace. Read-only: status, diff, log, blame, show, branch, tag, remote, rev-parse, "
        "ls-files, describe, shortlog (no prompt). Changing: add, restore, commit, checkout, switch, stash, merge, "
        "rebase, cherry-pick, revert, reset, mv, rm, fetch, pull, push (asks first). Pass flags and paths in `args`."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "subcommand": {"type": "string", "description": "e.g. status, diff, log, blame, show, commit"},
            "args": {"type": "array", "items": {"type": "string"},
                     "description": "Arguments after the subcommand, e.g. [\"--stat\", \"HEAD~1\", \"--\", \"src\"]"},
            "path": {"type": "string", "description": "Folder of the repository, when it is not the workspace"},
        },
        "required": ["subcommand"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    serial: ClassVar[bool | None] = True      # ordering-sensitive: never concurrent with another call
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        sub, extra, folder = git_call(args)
        cwd = resolve_path(ctx, folder) if folder else ctx.workspace
        if not Path(cwd).is_dir():
            raise ToolError(f"{folder} is not a folder.")
        if sub not in READ_SUBCOMMANDS + WRITE_SUBCOMMANDS:
            raise ToolError(f"Unsupported git subcommand {sub!r}. Supported: {', '.join(READ_SUBCOMMANDS + WRITE_SUBCOMMANDS)}. "
                            f"Use Bash for anything else.")
        raw_extra = args.get("args")
        if raw_extra is not None and (not isinstance(raw_extra, list) or not all(isinstance(a, str) for a in raw_extra)):
            raise ToolError("Git 'args' must be a list of strings.")
        for a in extra:
            if "\x00" in a or "\n" in a:
                raise ToolError("Git arguments must not contain newlines or NUL characters.")
            if a == "--":
                break
            if sub == "blame" and a.startswith("-S"):       # blame -S FILE reads a file of revisions and quotes it in its error
                raise ToolError("git blame -S is not allowed through the Git tool (it reads an arbitrary file). Use Bash if you really need it.")
            if a.startswith("--") and len(a) > 3 and _abbreviates_blocked(a.split("=", 1)[0]):
                raise ToolError(f"git flag {a!r} is not allowed through the Git tool (git accepts shortened flag names, and this one "
                                f"can run helper programs, write files or read outside the repository).")
            if a.startswith("-") and _BLOCKED_FLAG.match(a):
                raise ToolError(f"git flag {a!r} is not allowed through the Git tool (it can run helper programs, "
                                f"write files or read outside the repository). Use Bash if you really need it.")
        exe = shutil.which("git")
        if not exe:
            raise ToolError("git is not installed or not on PATH.")

        argv = [exe, "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.pager=cat",
                "-c", "protocol.ext.allow=never"]
        if sub in READ_SUBCOMMANDS:
            argv += ["-c", "core.hooksPath=" + os.devnull]
        argv.append(sub)
        if sub in _TEXT_SUBS:
            argv += ["--no-ext-diff", "--no-textconv"]
        if sub == "status" and not extra:
            argv += ["--short", "--branch"]
        if sub == "log" and not any(a.startswith(("-n", "--max-count")) or re.fullmatch(r"-\d+", a) for a in extra):
            argv += ["--max-count=50"]
        argv += extra

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_EXTERNAL_DIFF")}
        env.update({"GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_ASKPASS": "true", "LC_ALL": "C.UTF-8" if os.name != "nt" else env.get("LC_ALL", "")})
        timeout = 120 if sub in ("fetch", "pull", "push") else 60
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise ToolError(f"git {sub} timed out after {timeout}s.") from None
        except OSError as e:
            raise ToolError(f"Could not run git: {e}") from e
        text = out.decode("utf-8", errors="replace").strip("\n")
        if proc.returncode != 0:
            raise ToolError(f"git {sub} failed (exit {proc.returncode}):\n{cap_text(text, 4000)}")
        text = cap_text(text, output_cap(ctx, 30_000), "Narrow it with paths, -n, or --stat.")
        return text or f"(git {sub}: no output)"

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        from axon.tools.base import default_summary
        return default_summary(output, is_error)

    def render_call(self, args: dict[str, Any]) -> str:
        return "Git " + " ".join([str(args.get("subcommand", ""))] + [str(a) for a in (args.get("args") or [])])[:100]
