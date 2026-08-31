"""
Process tool: inspect running processes, check listening network ports, and check memory/CPU metrics.
"""
from __future__ import annotations
import os
import shutil
import subprocess
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class ProcessTool(Tool):
    name: ClassVar[str] = "Process"
    description: ClassVar[str] = (
        "Inspect running operating system processes, identify open listening network ports (e.g. dev servers on :3000 or :8000), "
        "and check system resource metrics."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "ports", "find"],
                "description": "Action to perform: 'list' (top processes), 'ports' (listening ports), 'find' (filter by pattern)",
            },
            "pattern": {"type": "string", "description": "Search pattern for process name or command (for 'find')"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = args.get("action", "list").lower()
        pattern = args.get("pattern", "").strip()

        if action == "ports":
            # Check open listening ports via lsof or netstat
            if shutil.which("lsof"):
                try:
                    res = subprocess.run(
                        ["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    out = res.stdout.strip()
                    if out:
                        return f"Listening TCP Ports:\n{out}"
                    return "No listening TCP ports found."
                except Exception as e:
                    return f"Port inspection failed: {e}"
            elif shutil.which("netstat"):
                try:
                    cmd = ["netstat", "-ano"] if sys.platform == "win32" else ["netstat", "-tuln"]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    out = res.stdout.strip()
                    if out:
                        lines = [l for l in out.splitlines() if "LISTEN" in l.upper() or "LISTENING" in l.upper()]
                        return "Listening Network Ports:\n" + ("\n".join(lines[:30]) if lines else out[:1500])
                    return "No listening ports found."
                except Exception as e:
                    return f"Port inspection via netstat failed: {e}"
            return "Neither lsof nor netstat is available for port inspection."

        elif action in ("list", "find"):
            try:
                if shutil.which("ps"):
                    cmd = ["ps", "-eo", "pid,%cpu,%mem,stat,time,command"]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                    lines = res.stdout.strip().splitlines()
                elif sys.platform == "win32" and shutil.which("tasklist"):
                    res = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10)
                    lines = res.stdout.strip().splitlines()
                else:
                    return "Process listing utility ('ps' or 'tasklist') not available."

                header = lines[0] if lines else ""
                data_lines = lines[1:] if len(lines) > 1 else []

                if pattern:
                    matched = [l for l in data_lines if pattern.lower() in l.lower()]
                    if not matched:
                        return f"No processes found matching '{pattern}'."
                    return f"{header}\n" + "\n".join(matched[:30])
                else:
                    return f"{header}\n" + "\n".join(data_lines[:25])
            except Exception as e:
                raise ToolError(f"Process listing failed: {e}")

        else:
            raise ToolError(f"Unknown action '{action}'. Use 'list', 'ports', or 'find'.")

