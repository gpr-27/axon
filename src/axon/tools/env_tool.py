"""
Env tool: inspect environment variables, check configurations, and safely mask sensitive secrets.
"""
from __future__ import annotations
import os
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

_SENSITIVE_KEYS = {"key", "secret", "token", "password", "auth", "credential", "private", "cert"}

class EnvTool(Tool):
    name: ClassVar[str] = "Env"
    description: ClassVar[str] = (
        "Safely inspect system environment variables and project configuration flags. "
        "Automatically masks sensitive values (API keys, tokens, passwords) for security."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "list", "check"],
                "description": "Action: 'get' (specific variable), 'list' (all non-sensitive vars), 'check' (verify presence)",
            },
            "variable": {"type": "string", "description": "Variable name to inspect or check"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        action = args.get("action", "list").lower()
        var_name = args.get("variable", "").strip()

        if action == "get":
            if not var_name:
                raise ToolError("Env get requires 'variable' argument.")
            val = os.environ.get(var_name)
            if val is None:
                return f"Environment variable '{var_name}' is not set."
            if any(s in var_name.lower() for s in _SENSITIVE_KEYS):
                masked = val[:3] + "..." + val[-3:] if len(val) > 8 else "***"
                return f"{var_name}={masked} (Sensitive value masked)"
            return f"{var_name}={val}"

        elif action == "check":
            if not var_name:
                raise ToolError("Env check requires 'variable' argument.")
            is_set = var_name in os.environ
            return f"Environment variable '{var_name}': {'SET' if is_set else 'NOT SET'}"

        elif action == "list":
            lines = []
            for k in sorted(os.environ.keys()):
                if k.startswith(("_", "TERM", "SHELL", "PATH", "LANG", "USER", "HOME", "PWD", "EDITOR", "PYTHON")):
                    val = os.environ[k]
                    if any(s in k.lower() for s in _SENSITIVE_KEYS):
                        val = "***"
                    lines.append(f"{k}={val}")
                elif not any(s in k.lower() for s in _SENSITIVE_KEYS):
                    lines.append(f"{k}={os.environ[k]}")
            return "\n".join(lines[:40]) or "(No common environment variables found)"

        else:
            raise ToolError(f"Unknown action '{action}'. Use 'get', 'list', or 'check'.")
