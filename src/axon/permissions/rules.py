"""
Permission Rule syntax and matching logic: Tool(pattern).
"""
from __future__ import annotations
import fnmatch
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str | None = None

    def matches(self, tool_name: str, args: dict[str, Any]) -> bool:
        if self.tool != "*" and self.tool.lower() != tool_name.lower():
            return False

        if not self.pattern or self.pattern == "*":
            return True

        # Path matching for filesystem tools
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path_arg = str(args.get("path", ""))
            return fnmatch.fnmatch(path_arg, self.pattern)

        # Command matching for Bash
        if tool_name == "Bash":
            cmd = str(args.get("command", "")).strip()
            # Any shell metacharacters skip allow rules for safety
            if any(ch in cmd for ch in (";", "&&", "||", "`", "$(")):
                return False
            if self.pattern.endswith(":*"):
                prefix = self.pattern[:-2]
                return cmd.startswith(prefix)
            return fnmatch.fnmatch(cmd, self.pattern)

        return True

def parse_rule(spec: str) -> Rule:
    """Parse 'Tool(pattern)' or bare 'Tool'."""
    spec = spec.strip()
    if "(" in spec and spec.endswith(")"):
        tool_part, pat_part = spec[:-1].split("(", 1)
        return Rule(tool=tool_part.strip(), pattern=pat_part.strip())
    return Rule(tool=spec, pattern=None)

def parse_rules(specs: list[str]) -> list[Rule]:
    return [parse_rule(s) for s in specs if s.strip()]
