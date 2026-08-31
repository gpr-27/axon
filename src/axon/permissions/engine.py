"""
PermissionEngine: allow/ask/deny decisions across modes and rules.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from axon.config import Mode, Settings
from axon.permissions.rules import Rule, parse_rules
from axon.tools.base import Tool

DecisionOutcome = Literal["allow", "ask", "deny"]

@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    reason: str
    rule: str | None = None

class PermissionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.allow_rules: list[Rule] = parse_rules(settings.permissions.allow)
        self.deny_rules: list[Rule] = parse_rules(settings.permissions.deny)

    def check(self, tool: Tool, args: dict[str, Any], mode: Mode) -> Decision:
        """
        Evaluate in order:
        1. Hard invariants (never overridable)
        2. Deny rules (deny always wins over allow)
        3. Allow rules
        4. Mode defaults
        """
        # Invariant checks
        if tool.name == "Bash":
            cmd = str(args.get("command", "")).strip()
            if cmd.startswith("rm -rf /") or cmd.startswith("rm -rf /*"):
                return Decision("deny", "Root filesystem removal is blocked by structural invariant.")

        # Deny rules
        for r in self.deny_rules:
            if r.matches(tool.name, args):
                return Decision("deny", f"Blocked by deny rule: {r.tool}({r.pattern or '*'})", str(r))

        # Allow rules
        for r in self.allow_rules:
            if r.matches(tool.name, args):
                return Decision("allow", f"Allowed by explicit rule: {r.tool}({r.pattern or '*'})", str(r))

        # Mode defaults
        if mode == "bypass":
            return Decision("allow", "Bypass mode active.")

        if mode == "plan":
            if not tool.readonly and tool.name != "ExitPlanMode":
                return Decision("deny", f"Plan mode prevents mutating action '{tool.name}'.")
            return Decision("allow", "Plan mode permits read-only exploration.")

        if mode == "acceptEdits":
            if tool.name in ("Write", "Edit", "MultiEdit", "Patch") or tool.readonly:
                return Decision("allow", "acceptEdits mode allows filesystem changes.")
            return Decision("ask", "acceptEdits requires confirmation for shell/destructive commands.")

        # Default mode
        if tool.readonly:
            return Decision("allow", "Read-only tool allowed by default.")

        return Decision("ask", f"Command or modification '{tool.name}' requires user approval.")

    def grant_persistent(self, rule: Rule, project_dir: Path) -> None:
        """Append an 'always allow' rule back to the project config cleanly."""
        self.allow_rules.append(rule)
        rule_str = f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool
        axon_dir = project_dir / ".axon"
        axon_dir.mkdir(parents=True, exist_ok=True)
        config_path = axon_dir / "config.toml"
        try:
            allow_rules: list[str] = []
            deny_rules: list[str] = []

            if config_path.exists():
                try:
                    try:
                        import tomllib
                    except ModuleNotFoundError:
                        import tomli as tomllib  # type: ignore[no-redef]
                    with open(config_path, "rb") as f:
                        data = tomllib.load(f)
                    perms = data.get("permissions", {})
                    allow_rules = list(perms.get("allow", []))
                    deny_rules = list(perms.get("deny", []))
                except Exception:
                    pass

            if rule_str not in allow_rules:
                allow_rules.append(rule_str)

            lines = ["# Axon Workspace Configuration", "[permissions]"]
            quoted_allows = ", ".join(f'"{r}"' for r in allow_rules)
            lines.append(f"allow = [{quoted_allows}]")
            if deny_rules:
                quoted_denies = ", ".join(f'"{r}"' for r in deny_rules)
                lines.append(f"deny = [{quoted_denies}]")
            lines.append("")

            config_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
