"""
Permission prompt shown before mutating tools and shell commands run.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Literal

from axon import platform as plat
from axon.ui.picker import SelectItem, Selector
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, WHITE, sanitize, strip_ansi


def _clean(text: Any) -> str:
    return sanitize(strip_ansi(str(text)))

# "once": this call only; "always": every call of this tool for the rest of the session (in memory);
# "rule": persist the narrow rule shown in the option label (<ws>/.axon/settings.local.json).
ApprovalResult = Literal["once", "always", "rule", "deny"]

# Tools whose narrow rule does not depend on the workspace path (the label can show it exactly).
_RULE_TOOLS = ("Bash", "Git", "WebFetch", "Http")

# Letter shortcuts accepted by the picker (and the plain-text fallback).
_KEYS = {"y": "once", "a": "always", "n": "deny"}


def narrow_rule_label(tool_name: str, args: dict[str, Any]) -> str:
    """The exact rule a "rule" approval would save, e.g. ``Bash(git status:*)`` ('' when unavailable)."""
    if tool_name not in _RULE_TOOLS:
        return ""
    try:
        from pathlib import Path

        from axon.permissions.engine import PermissionEngine
        return str(PermissionEngine.narrow_rule_for(tool_name, args if isinstance(args, dict) else {}, Path.cwd()))
    except Exception:
        return ""


def _detail_lines(tool_name: str, args: dict[str, Any], reason: str) -> list[str]:
    cols = plat.term_size()[0]
    width = max(20, cols - 8)
    lines: list[str] = []
    if tool_name == "Bash":
        cmd = str(args.get("command", ""))
        for i, part in enumerate(cmd.splitlines()[:12] or [""]):
            lines.append(f"    {GOLD}{'$' if i == 0 else ' '}{RST} {WHITE}{BOLD}{plat.truncate_ansi(_clean(part), width)}{RST}")
        if args.get("description"):
            lines.append(f"    {SLATE}{plat.truncate_ansi(_clean(args['description']), width)}{RST}")
        lines.append(f"    {DARK_SLATE}runs in {plat.shell_name()}{RST}")
    elif "path" in args or "file_path" in args:
        lines.append(f"    {WHITE}{BOLD}{plat.truncate_ansi(_clean(args.get('path') or args.get('file_path')), width)}{RST}")
    else:
        try:
            text = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(args)
        lines.append(f"    {SLATE}{plat.truncate_ansi(_clean(text), width)}{RST}")
    if reason:
        lines.append(f"    {DARK_SLATE}{plat.truncate_ansi(_clean(reason), width)}{RST}")
    return lines


def ask_approval(tool_name: str, args: dict[str, Any], reason: str = "") -> ApprovalResult:
    """Ask the user to allow a tool call. Non-interactive sessions are denied (safe default)."""
    try:
        interactive_in = sys.stdin.isatty()
    except Exception:
        interactive_in = False
    if not interactive_in:
        return "deny"

    try:
        from axon.ui.in_flight import paused
    except ImportError:  # pragma: no cover - same package
        import contextlib
        paused = contextlib.nullcontext  # type: ignore[assignment]

    try:
        from axon.ui.render import pause_spinners
    except (ImportError, AttributeError):
        import contextlib
        pause_spinners = contextlib.nullcontext  # type: ignore[assignment]

    tool_name_s = _clean(tool_name)
    header = [f"  {GOLD}{BOLD}⚠ Allow {tool_name_s}?{RST}", *_detail_lines(tool_name, args, reason), ""]
    rule = narrow_rule_label(tool_name, args)
    with pause_spinners(), paused():
        if plat.is_interactive():
            items = [
                SelectItem("Yes", value="once", hint="this call only · y"),
                SelectItem("Yes, don't ask again this session", value="always",
                           hint=f"every {tool_name_s} call until you exit · a"),
                SelectItem("No", value="deny", hint="n · esc"),
            ]
            if rule:
                items.append(SelectItem(f"Yes, always allow {_clean(rule)}", value="rule",
                                        hint="saved for this project (.axon/settings.local.json)"))

            def on_key(sel: Selector, key: str) -> bool:
                value = _KEYS.get(key.lower()) if len(key) == 1 else None
                if value is None:
                    return False
                sel.finish(next(it for it in sel.items if it.value == value))
                return True

            sys.stdout.write("\n" + "\n".join(header) + "\n")
            sys.stdout.flush()
            chosen = Selector(items, title="Permission", subtitle=tool_name_s, on_key=on_key,
                              footer=f"↑↓ move · enter choose · 1-{len(items)} quick pick · y yes · a session · n/esc deny").run()
            result: ApprovalResult = chosen.value if isinstance(chosen, SelectItem) else "deny"
        else:
            print("\n" + "\n".join(header))
            print(f"  {MINT}[y]{RST} yes (this call)   {GOLD}[a]{RST} every {tool_name_s} call this session   {ROSE}[n]{RST} no")
            try:
                answer = input(f"  {BOLD}› {RST}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "n"
            result = "once" if answer in ("y", "yes") else ("always" if answer in ("a", "always") else "deny")
    label = {"once": f"{MINT}✓ Allowed once",
             "always": f"{GOLD}✓ Allowed for this session (all {tool_name_s} calls)",
             "rule": f"{GOLD}✓ Allowed {_clean(rule)} for this project",
             "deny": f"{ROSE}✗ Denied"}[result]
    print(f"  {label}{RST}\n")
    return result
