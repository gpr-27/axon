"""
Interactive permission approval prompt for mutating and shell commands.
"""
from __future__ import annotations
import sys
from typing import Any, Literal
from axon.ui.theme import BOLD, DIM, GOLD, RED, RST, TEAL, WHITE

ApprovalResult = Literal["once", "always", "deny"]

def ask_approval(tool_name: str, args: dict[str, Any], reason: str = "") -> ApprovalResult:
    """Prompt user for interactive authorization."""
    if not sys.stdin.isatty():
        return "once"

    print(f"\n  {GOLD}{BOLD}⚠️  Permission Required: {tool_name}{RST}")
    if reason:
        print(f"  {DIM}Reason: {reason}{RST}")

    if tool_name == "Bash":
        cmd = args.get("command", "")
        desc = args.get("description", "")
        print(f"  {WHITE}Command    :{RST} {BOLD}{cmd}{RST}")
        if desc:
            print(f"  {WHITE}Description:{RST} {desc}")
    elif tool_name in ("Write", "Edit", "MultiEdit"):
        print(f"  {WHITE}Target File:{RST} {args.get('path', '')}")

    print(f"\n  Allow this action?  {TEAL}[y] allow once{RST}   {GOLD}[a] always allow for session{RST}   {RED}[n] deny{RST}")

    while True:
        try:
            choice = input(f"  {BOLD}› {RST}").strip().lower()
            if choice in ("y", "yes", ""):
                print(f"  {TEAL}✓ Allowed once{RST}\n")
                return "once"
            elif choice in ("a", "always"):
                print(f"  {GOLD}✓ Always allowed for session{RST}\n")
                return "always"
            elif choice in ("n", "no", "d", "deny"):
                print(f"  {RED}✗ Action denied by user{RST}\n")
                return "deny"
        except (KeyboardInterrupt, EOFError):
            print(f"\n  {RED}✗ Action cancelled{RST}\n")
            return "deny"
