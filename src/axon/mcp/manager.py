"""
Model Context Protocol (MCP) Configuration Manager.
Loads, merges, and updates MCP servers across global ~/.axon/mcp.json, workspace .axon/mcp.json,
and Claude Desktop config files.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any
from axon.mcp.catalog import MCPServerPreset, get_mcp_preset

class MCPManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.global_config_file = Path.home() / ".axon" / "mcp.json"
        self.local_config_file = workspace / ".axon" / "mcp.json"

    def get_all_servers(self) -> dict[str, dict[str, Any]]:
        """Load and merge servers from global and local MCP configurations."""
        merged: dict[str, dict[str, Any]] = {}

        # 1. Global config ~/.axon/mcp.json
        if self.global_config_file.exists():
            try:
                data = json.loads(self.global_config_file.read_text(encoding="utf-8"))
                merged.update(data.get("mcpServers", {}))
            except Exception:
                pass

        # 2. Local config .axon/mcp.json
        if self.local_config_file.exists():
            try:
                data = json.loads(self.local_config_file.read_text(encoding="utf-8"))
                merged.update(data.get("mcpServers", {}))
            except Exception:
                pass

        return merged

    def add_server(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        scope: str = "global",
    ) -> Path:
        """Add an MCP server configuration."""
        target_file = self.global_config_file if scope == "global" else self.local_config_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        existing_data: dict[str, Any] = {"mcpServers": {}}
        if target_file.exists():
            try:
                existing_data = json.loads(target_file.read_text(encoding="utf-8"))
                if "mcpServers" not in existing_data:
                    existing_data["mcpServers"] = {}
            except Exception:
                existing_data = {"mcpServers": {}}

        server_spec: dict[str, Any] = {
            "command": command,
            "args": args or [],
        }
        if env:
            server_spec["env"] = env

        existing_data["mcpServers"][name] = server_spec
        target_file.write_text(json.dumps(existing_data, indent=2), encoding="utf-8")
        return target_file

    def remove_server(self, name: str) -> bool:
        """Remove an MCP server from both local and global configs."""
        removed = False
        for cfg in (self.local_config_file, self.global_config_file):
            if cfg.exists():
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                    if "mcpServers" in data and name in data["mcpServers"]:
                        del data["mcpServers"][name]
                        cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
                        removed = True
                except Exception:
                    pass
        return removed

    def add_preset(self, preset_id: str, custom_env: dict[str, str] | None = None, scope: str = "global") -> bool:
        """Install a pre-configured MCP server preset."""
        preset = get_mcp_preset(preset_id)
        if not preset:
            return False
        env = dict(preset.env_vars)
        if custom_env:
            env.update(custom_env)
        self.add_server(
            name=preset.id,
            command=preset.command,
            args=preset.args,
            env=env if env else None,
            scope=scope,
        )
        return True

    def find_claude_desktop_config(self) -> Path | None:
        """Locate Claude Desktop config if installed on machine."""
        if sys.platform == "darwin":
            candidate = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            candidate = Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
        else:
            candidate = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

        if candidate and candidate.exists():
            return candidate
        return None

    def import_from_claude_desktop(self) -> int:
        """Import all configured MCP servers from Claude Desktop."""
        claude_cfg = self.find_claude_desktop_config()
        if not claude_cfg:
            return 0

        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            for name, spec in servers.items():
                self.add_server(
                    name=name,
                    command=spec.get("command", ""),
                    args=spec.get("args", []),
                    env=spec.get("env"),
                    scope="global",
                )
            return len(servers)
        except Exception:
            return 0
