"""Monitor: block until a background shell prints a line matching a regex, or exits, or a timeout passes."""
from __future__ import annotations

import re
import time
from typing import Any, ClassVar

from axon import interruptible
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, int_arg, output_cap
from axon.tools.shell import BACKGROUND, _clean_output

MAX_WAIT_S = 600


class MonitorTool(Tool):
    name: ClassVar[str] = "Monitor"
    description: ClassVar[str] = (
        "Wait for a background shell (Bash run_in_background) to print output matching `pattern` (regex) or to exit, "
        "up to timeout_s (default 60, max 600). Returns the new output and why it stopped. Use it instead of "
        "polling BashOutput in a loop (dev servers, builds, watchers)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "shell_id": {"type": "string", "description": "Background shell id, e.g. bash_1"},
            "pattern": {"type": "string", "description": "Regex to wait for in new output; omit to wait for exit"},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_S, "description": "Max wait (default 60)"},
        },
        "required": ["shell_id"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        shell = BACKGROUND.get(str(args.get("shell_id", "")).strip())
        timeout = int_arg(args, "timeout_s", 60, lo=1, hi=MAX_WAIT_S, strict=True)
        pat = args.get("pattern")
        rx = None
        if pat not in (None, ""):
            if not isinstance(pat, str):
                raise ToolError("Argument 'pattern' must be a string (a regex).")
            try:
                rx = re.compile(pat, re.M)
            except re.error as e:
                raise ToolError(f"Invalid pattern regex: {e}") from e

        seen, skipped = "", 0
        deadline = time.monotonic() + timeout
        reason = ""
        while True:
            chunk, skip = shell.stream.read_new()
            skipped += skip
            if chunk:
                seen += chunk
            exited = shell.proc.poll() is not None
            if exited:  # let the reader thread drain what is left in the pipe
                shell.reader.join(timeout=2)
                chunk, skip = shell.stream.read_new()
                skipped += skip
                seen += chunk
            clean = _clean_output(seen)
            if rx is not None:
                m = rx.search(clean)
                if m:
                    reason = f"matched {pat!r}"
                    break
            if exited:
                reason = f"process {shell.status}"
                break
            if time.monotonic() >= deadline:
                reason = f"timed out after {timeout}s"
                break
            interruptible.sleep(0.1)
        cap = output_cap(ctx, 30_000)
        if len(clean) > cap:
            skipped += len(clean) - cap
            clean = clean[-cap:]
        note = f"\n[{skipped} earlier characters not shown]" if skipped else ""
        head = f"[{shell.id}] {reason} · {shell.status} · {time.time() - shell.started:.0f}s"
        return f"{head}{note}\n{clean.strip(chr(10)) or '(no new output)'}"

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return super().summarize(args, output, True)
        return output.split("\n", 1)[0][:90]
