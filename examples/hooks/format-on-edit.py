#!/usr/bin/env python3
"""PostToolUse hook: format a file right after Axon edits it.

Wire it up in settings.json (matcher "Edit|Write|MultiEdit|Patch"):
    {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit|Patch",
        "hooks": [{"type": "command", "command": "python .axon/hooks/format-on-edit.py", "timeout": 60}]}]}}

Picks a formatter by file extension and only runs it when it is installed:
  .py                      ruff format  (or black)
  .js .jsx .ts .tsx .json .css .scss .md .html .yaml .yml   prettier (via npx --no-install)
  .go                      gofmt -w
  .rs                      rustfmt
Missing formatters are skipped silently, so the hook is safe on any machine. Exit code 0 always:
a formatting problem should never block the agent (it is reported on stderr as a warning).
"""
import json
import os
import shutil
import subprocess
import sys

FORMATTERS = {
    ".py": [["ruff", "format", "{file}"], ["black", "-q", "{file}"]],
    ".go": [["gofmt", "-w", "{file}"]],
    ".rs": [["rustfmt", "{file}"]],
}
for _ext in (".js", ".jsx", ".ts", ".tsx", ".json", ".css", ".scss", ".md", ".html", ".yaml", ".yml"):
    FORMATTERS[_ext] = [["npx", "--no-install", "prettier", "--write", "{file}"]]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("path") or tool_input.get("file_path") or ""
    if not path:
        return 0
    project = os.environ.get("AXON_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    full = path if os.path.isabs(path) else os.path.join(project, path)
    if not os.path.isfile(full):
        return 0
    ext = os.path.splitext(full)[1].lower()
    for argv in FORMATTERS.get(ext, []):
        exe = shutil.which(argv[0])
        if exe is None:
            continue
        cmd = [exe] + [a.replace("{file}", full) for a in argv[1:]]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, cwd=project)
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"format-on-edit: {argv[0]} failed: {e}\n")
            continue
        if proc.returncode == 0:
            return 0
        sys.stderr.write(f"format-on-edit: {argv[0]} exited {proc.returncode}: {proc.stderr.strip()[:300]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
