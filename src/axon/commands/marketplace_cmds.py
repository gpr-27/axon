"""/marketplace (aliases /connectors, /store): browse and install connectors, skills and plugins."""
from __future__ import annotations

from axon import platform
from typing import Any

from axon.commands.common import ws_of
from axon.commands.registry import CommandResult, command

CAT = "Agents, Skills & Tools"
USAGE = """/marketplace                          browse (terminal) or list the connectors
/marketplace list|search|info|categories      find connectors, skills and plugins
/marketplace install|remove <id> · add <url|command> · create-skill · create-plugin
/marketplace enable|disable <id>     switch a connector, skill or plugin on or off
/marketplace status [--check] [--all]  everything installed: enabled, connected, tools, last error, scope
/marketplace test <id> · reconnect <id|all> · logs <id> [lines]
/marketplace scope <id> user|project|local
/marketplace env <id> NAME=VALUE ... [--unset NAME] · timeout <id> <seconds>
/marketplace tool <id> [<tool>|*] allow|ask|deny|reset [--global]
/marketplace export [file] [--include-secrets] · import <file> [--dry-run] [--overwrite]
/marketplace auto on|off|status|set <name> <value>     automatic behaviour (off by default)
/marketplace suggest [--reset] · updates · update <id>"""


def split_words(text: str) -> list[str]:
    try:
        return platform.split_words(text)
    except ValueError:
        return text.split()


def follow_in_session(agent: Any, outcome: Any) -> None:
    """Best effort: make the running session follow what the command changed."""
    try:
        from axon.mcp.bridge import get_bridge
        from axon.mcp.commands import handle_mcp_command
    except ImportError:
        return
    for name in outcome.removed:
        get_bridge().disconnect_server(name)
    for name in dict.fromkeys(outcome.installed + outcome.reconnect):
        try:
            handle_mcp_command(agent, f"reconnect {name}")
        except Exception:  # noqa: BLE001 - the change itself already succeeded
            continue


_CHANGES = {"install", "add-catalog", "remove", "uninstall", "rm", "enable", "disable", "update", "import", "remove-source", "refresh"}


@command("marketplace", category=CAT, aliases=("connectors", "store"),
         description="Browse, install and control connectors (MCP), skills and plugins", usage=USAGE)
def handle_marketplace(agent: Any, arg: str) -> CommandResult:
    from axon.marketplace.cli import run_command
    words = split_words(arg)
    outcome = run_command(words, ws_of(agent), agent=agent)
    follow_in_session(agent, outcome)
    if words and words[0].lower() in _CHANGES and outcome.code == 0:     # new skills, hooks and plugin servers apply now
        try:
            from axon.commands.agent_cmds import _refresh_extensions
            _refresh_extensions(agent)
        except Exception:  # noqa: BLE001
            pass
    return CommandResult(handled=True, success=outcome.code == 0)


@command("trust", category=CAT, description="Review and allow project hooks, plugins and MCP servers for this folder",
         usage="/trust · /trust yes · /trust revoke [path] · /trust list")
def handle_trust(agent: Any, arg: str) -> CommandResult:
    """Folder trust (axon.hooks.trust): project hooks, plugins and MCP servers run only in a trusted folder."""
    from axon.hooks import trust
    ws = ws_of(agent)
    if trust.run_command(arg, ws, ask=input):
        hooks = getattr(agent, "hooks", None)
        if hooks is not None:
            try:
                hooks.reload()
            except Exception:  # noqa: BLE001 - the trust decision itself is saved
                pass
    return CommandResult(handled=True, success=True)
