"""
Approval of project-scoped (.mcp.json) servers.

A cloned repository must not be able to launch programs on your machine without your consent, so in an
interactive session Axon asks before it uses a server from ``.mcp.json`` it has not seen. Answers are kept
in ``~/.axon/mcp-state.json`` (reset with ``axon mcp reset-project-choices``); ``-p`` and SDK runs load
project servers without asking. ``disabledMcpjsonServers`` / ``enabledMcpjsonServers`` /
``enableAllProjectMcpServers`` in your own settings decide without a prompt.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from axon.mcp import state
from axon.mcp.config import ServerSpec


def pending(specs: dict[str, ServerSpec]) -> list[ServerSpec]:
    return [s for s in specs.values() if s.approval == "pending" and not s.disabled]


def review(workspace: Path, specs: dict[str, ServerSpec], *, ask: Callable[[str], str] | None = None,
           out: Callable[[str], None] | None = None) -> int:
    """Ask about every pending project server; updates ``specs`` in place. Returns how many were decided.
    Enter leaves a server undecided (it stays unconnected and shows as pending in /mcp)."""
    todo = pending(specs)
    if not todo:
        return 0
    ask = ask or input
    out = out or print
    out("")
    out("  This project's .mcp.json defines MCP server(s) you have not approved yet. "
        "Approving lets Axon run the command or contact the URL:")
    for i, s in enumerate(todo, 1):
        out(f"    {i}. {s.name}  ({s.type})  {s.describe_target()[:100]}")
    try:
        answer = ask("  Approve [a]ll, review [o]ne by one, [n]one (never), Enter = decide later: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 0
    decided = 0

    def _set(spec: ServerSpec, verdict: str) -> None:
        nonlocal decided
        state.set_project_choice(workspace, spec.name, verdict)
        spec.approval = verdict
        decided += 1

    if answer in ("a", "all"):
        for s in todo:
            _set(s, "approved")
    elif answer in ("n", "none", "never"):
        for s in todo:
            _set(s, "rejected")
    elif answer in ("o", "one", "review"):
        for s in todo:
            try:
                a = ask(f"  Use '{s.name}'? [y]es / [n]o / Enter = later: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if a in ("y", "yes"):
                _set(s, "approved")
            elif a in ("n", "no"):
                _set(s, "rejected")
    ask_helper_trust(workspace, [s for s in todo if s.approval == "approved"], ask=ask, out=out)
    return decided


def ask_helper_trust(workspace: Path, specs: list[ServerSpec], *, ask: Callable[[str], str] | None = None,
                     out: Callable[[str], None] | None = None) -> bool:
    """A project or local server's ``headersHelper`` is a shell command and runs only in a folder you trust.
    Ask once, showing the commands. Returns True when the folder is trusted afterwards."""
    from axon.mcp.headers import helper_allowed
    from axon.mcp.policy import set_folder_trusted
    waiting = [s for s in specs if s.headers_helper and not helper_allowed(s, workspace)]
    if not waiting:
        return True
    ask = ask or input
    out = out or print
    out("  These servers get their headers from a shell command, which runs only in a folder you trust:")
    for s in waiting:
        out(f"    {s.name}: {s.headers_helper[:200]}")
    try:
        answer = ask("  Trust this folder and let them run? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in ("y", "yes"):
        set_folder_trusted(workspace)
        return True
    return False
