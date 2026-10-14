#!/usr/bin/env python3
"""PreToolUse hook: block destructive shell commands before Axon runs them.

Wire it up in settings.json (matcher "Bash"):
    {"hooks": {"PreToolUse": [{"matcher": "Bash",
        "hooks": [{"type": "command", "command": "python .axon/hooks/block-dangerous-bash.py"}]}]}}

Axon sends the tool call as JSON on stdin ({"tool_name": "Bash", "tool_input": {"command": ...}}).
Exit code 2 blocks the call and the message on stderr is shown to the model and to you.
Exit code 0 lets it through. Works the same on Windows, macOS and Linux (stdlib only).
"""
import json
import re
import sys

# (pattern, why). Patterns are matched case-insensitively against the full command line.
DANGEROUS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b[^|;&]*\s(/|~|\$home|\*)(\s|$)", "recursive force-delete of /, ~ or *"),
    (r"\brm\s+-[a-z]*r[a-z]*\s+--no-preserve-root", "rm --no-preserve-root"),
    (r"\b(mkfs(\.\w+)?|format\s+[a-z]:)", "formats a disk"),
    (r"\bdd\s+[^|;&]*\bof=/dev/(sd|nvme|disk|hd)", "dd onto a raw disk"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bgit\s+push\b[^|;&]*\s(--force|-f)(?![-\w])", "force push (use --force-with-lease)"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard discards uncommitted work"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean -f deletes untracked files"),
    (r"\b(curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipes a download into a shell"),
    (r"\bremove-item\b[^|;&]*-recurse[^|;&]*-force[^|;&]*\s([a-z]:\\?|\\|~|\$home)(\s|$)", "Remove-Item -Recurse -Force on a drive or home"),
    (r"\brd\s+/s\s+/q\s+[a-z]:\\?(\s|$)", "rd /s /q on a drive root"),
    (r"\bchmod\s+-r\s+777\s+/", "chmod -R 777 /"),
    (r"\bshutdown\b|\breboot\b|\bstop-computer\b|\brestart-computer\b", "shuts down or reboots the machine"),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0  # never break Axon because of an unexpected payload
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    for pattern, why in DANGEROUS:
        if re.search(pattern, command, re.IGNORECASE):
            sys.stderr.write(f"Blocked by block-dangerous-bash hook: {why}.\nCommand: {command}\n"
                             "Ask the user to run it manually if it is really intended.\n")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
