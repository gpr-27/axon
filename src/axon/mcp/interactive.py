"""
Interactive MCP (Model Context Protocol) Management Dashboard and CLI handlers.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from axon.mcp.catalog import MCP_CATALOG, MCPServerPreset, get_mcp_preset
from axon.mcp.manager import MCPManager
from axon.ui.picker import pick
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, MINT, RST, ROSE, SLATE, TEAL, WHITE,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent

def handle_mcp_interactive(agent: Agent, arg: str = "") -> None:
    """Main entrypoint for `/mcp` command."""
    manager = MCPManager(agent.settings.workspace)
    arg_clean = arg.strip().lower()

    if arg_clean in ("list", "ls", "status"):
        _list_servers(manager)
        return

    if arg_clean.startswith("add"):
        parts = arg.strip().split(maxsplit=2)
        if len(parts) >= 2:
            preset_id = parts[1].lower()
            preset = get_mcp_preset(preset_id)
            if preset:
                _install_preset_flow(manager, preset)
                return
        # Interactive add
        _choose_and_install_preset(manager)
        return

    if arg_clean.startswith("import"):
        count = manager.import_from_claude_desktop()
        if count > 0:
            print(f"\n  {MINT}✓ Successfully imported {count} MCP servers from Claude Desktop config!{RST}\n")
        else:
            claude_cfg = manager.find_claude_desktop_config()
            if not claude_cfg:
                print(f"\n  {SLATE}No Claude Desktop configuration file found on this machine.{RST}\n")
            else:
                print(f"\n  {SLATE}No MCP servers found in {claude_cfg}.{RST}\n")
        return

    if arg_clean.startswith("remove") or arg_clean.startswith("rm"):
        parts = arg.strip().split(maxsplit=1)
        if len(parts) >= 2:
            name = parts[1].strip()
            if manager.remove_server(name):
                print(f"\n  {MINT}✓ Removed MCP server '{name}'.{RST}\n")
            else:
                print(f"\n  {ROSE}MCP server '{name}' not found.{RST}\n")
            return
        _remove_server_flow(manager)
        return

    if not sys.stdin.isatty():
        _list_servers(manager)
        return

    # If no specific subcommand, present interactive main menu
    _main_mcp_menu(manager)


def _main_mcp_menu(manager: MCPManager) -> None:
    servers = manager.get_all_servers()
    print(f"\n  {GOLD}{BOLD}=== Model Context Protocol (MCP) Hub ==={RST}")
    print(f"  {SLATE}Active servers: {BOLD}{WHITE}{len(servers)}{RST} {SLATE}· Global: {manager.global_config_file}{RST}\n")

    options = [
        "1. 📦 Install Popular MCP Server (GitHub, SQLite, Postgres, Brave, Puppeteer...)",
        "2. 📋 List & Inspect Configured Servers",
        "3. ⚙️ Add Custom Stdio / SSE Server",
        "4. 📥 Import from Claude Desktop (claude_desktop_config.json)",
        "5. 🗑️ Remove an MCP Server",
        "6. ↩ Exit MCP Hub",
    ]

    chosen = pick(options, title="MCP Manager Menu")
    if not chosen or "Exit" in chosen:
        return

    if "Install Popular" in chosen:
        _choose_and_install_preset(manager)
    elif "List & Inspect" in chosen:
        _list_servers(manager)
    elif "Add Custom" in chosen:
        _add_custom_server_flow(manager)
    elif "Import from Claude" in chosen:
        count = manager.import_from_claude_desktop()
        if count > 0:
            print(f"\n  {MINT}✓ Imported {count} servers from Claude Desktop!{RST}\n")
        else:
            print(f"\n  {SLATE}No Claude Desktop configuration found.{RST}\n")
    elif "Remove" in chosen:
        _remove_server_flow(manager)

def _list_servers(manager: MCPManager) -> None:
    servers = manager.get_all_servers()
    print(f"\n  {GOLD}{BOLD}=== Configured MCP Servers ({len(servers)}) ==={RST}")
    if not servers:
        print(f"  {SLATE}No MCP servers configured yet.{RST}")
        print(f"  {DIM}Run '/mcp add' to install popular pre-configured servers in 1 click.{RST}\n")
        return

    for name, spec in servers.items():
        cmd = spec.get("command", "")
        args = " ".join(spec.get("args", []))
        env = spec.get("env")
        env_str = f" · {SLATE}Env: {', '.join(env.keys())}{RST}" if env else ""
        print(f"  • {TEAL}{BOLD}{name}{RST}: {WHITE}{cmd} {args}{RST}{env_str}")
    print()

def _choose_and_install_preset(manager: MCPManager) -> None:
    preset_options = [
        f"{p.name} ({p.category}) - {p.description}"
        for p in MCP_CATALOG
    ]
    chosen = pick(preset_options, title="Select MCP Server to Install")
    if not chosen:
        return

    idx = preset_options.index(chosen)
    preset = MCP_CATALOG[idx]
    _install_preset_flow(manager, preset)

def _install_preset_flow(manager: MCPManager, preset: MCPServerPreset) -> None:
    print(f"\n  {GOLD}▲█▲ Installing {preset.name}{RST}")
    print(f"  {SLATE}{preset.description}{RST}")
    print(f"  {DIM}Command: {preset.command} {' '.join(preset.args)}{RST}\n")

    custom_env: dict[str, str] = {}
    if preset.env_vars:
        for k in preset.env_vars.keys():
            existing = os.environ.get(k, "")
            prompt_str = f"  {BOLD}{WHITE}Enter {k} (or press Enter if already in environment): {RST}"
            try:
                val = input(prompt_str).strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n  {SLATE}Installation cancelled.{RST}\n")
                return
            if val:
                custom_env[k] = val

    manager.add_preset(preset.id, custom_env=custom_env, scope="global")
    print(f"\n  {MINT}✓ Successfully installed {preset.name}!{RST}")
    print(f"  {SLATE}Saved to {manager.global_config_file}{RST}\n")

def _add_custom_server_flow(manager: MCPManager) -> None:
    print(f"\n  {GOLD}▲█▲ Add Custom MCP Server{RST}")
    try:
        name = input(f"  {BOLD}{WHITE}Server identifier/name (e.g. my-db): {RST}").strip()
        if not name:
            return
        cmd = input(f"  {BOLD}{WHITE}Executable/Command (e.g. npx, uvx, python): {RST}").strip()
        if not cmd:
            return
        args_raw = input(f"  {BOLD}{WHITE}Arguments (space-separated, e.g. -y @package/server): {RST}").strip()
        args = args_raw.split() if args_raw else []
        env_raw = input(f"  {BOLD}{WHITE}Environment variables (KEY=VAL or press Enter to skip): {RST}").strip()
        env_dict = {}
        if env_raw and "=" in env_raw:
            k, v = env_raw.split("=", 1)
            env_dict[k.strip()] = v.strip()
    except (KeyboardInterrupt, EOFError):
        print(f"\n  {SLATE}Cancelled.{RST}\n")
        return

    manager.add_server(name, cmd, args, env=env_dict if env_dict else None, scope="global")
    print(f"\n  {MINT}✓ Successfully registered custom MCP server '{name}'!{RST}\n")

def _remove_server_flow(manager: MCPManager) -> None:
    servers = manager.get_all_servers()
    if not servers:
        print(f"\n  {SLATE}No MCP servers configured to remove.{RST}\n")
        return

    names = list(servers.keys())
    chosen = pick(names, title="Select MCP Server to Remove")
    if chosen:
        manager.remove_server(chosen)
        print(f"\n  {MINT}✓ Removed MCP server '{chosen}'.{RST}\n")
