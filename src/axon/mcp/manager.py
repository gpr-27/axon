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

    def find_all_external_mcp_configs(self) -> list[tuple[str, Path]]:
        """Search all known IDEs and tools for existing MCP configurations."""
        candidates: list[tuple[str, Path]] = []

        # 1. Claude Desktop
        claude_cfg = self.find_claude_desktop_config()
        if claude_cfg and claude_cfg.exists():
            candidates.append(("Claude Desktop", claude_cfg))

        # 2. Cursor IDE (~/.cursor/mcp.json and <workspace>/.cursor/mcp.json)
        cursor_home = Path.home() / ".cursor" / "mcp.json"
        if cursor_home.exists():
            candidates.append(("Cursor (Global)", cursor_home))
        cursor_ws = self.workspace / ".cursor" / "mcp.json"
        if cursor_ws.exists():
            candidates.append(("Cursor (Workspace)", cursor_ws))

        # 3. Windsurf / Codeium
        windsurf_cfg = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        if windsurf_cfg.exists():
            candidates.append(("Windsurf", windsurf_cfg))

        # 4. VS Code (Cline / Roo Code extensions)
        if sys.platform == "darwin":
            code_storage = Path.home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
        elif sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            code_storage = Path(appdata) / "Code" / "User" / "globalStorage" if appdata else None
        else:
            code_storage = Path.home() / ".config" / "Code" / "User" / "globalStorage"

        if code_storage and code_storage.exists():
            cline_cfg = code_storage / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
            if cline_cfg.exists():
                candidates.append(("VS Code (Cline)", cline_cfg))
            roo_cfg = code_storage / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json"
            if roo_cfg.exists():
                candidates.append(("VS Code (Roo Code)", roo_cfg))

        # 5. Project root mcp.json or .mcp.json
        for candidate_name in ("mcp.json", ".mcp.json"):
            ws_mcp = self.workspace / candidate_name
            if ws_mcp.exists() and ws_mcp != self.local_config_file:
                candidates.append(("Project Root", ws_mcp))

        return candidates

    def import_from_all_discovered(self) -> int:
        """Import all MCP servers from all discovered IDE configurations."""
        all_sources = self.find_all_external_mcp_configs()
        total_imported = 0
        for source_name, cfg_path in all_sources:
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                servers = data.get("mcpServers", {})
                for name, spec in servers.items():
                    self.add_server(
                        name=name,
                        command=spec.get("command", ""),
                        args=spec.get("args", []),
                        env=spec.get("env"),
                        scope="global",
                    )
                    total_imported += 1
            except Exception:
                pass
        return total_imported

    def detect_workspace_recommended_presets(self) -> list[str]:
        """Auto-detect workspace characteristics and recommend relevant MCP presets."""
        recommended: list[str] = []
        try:
            # Check for sqlite files
            if list(self.workspace.glob("*.sqlite")) or list(self.workspace.glob("*.db")):
                recommended.append("sqlite")
            # Check for git repo with github remote
            git_cfg = self.workspace / ".git" / "config"
            if git_cfg.exists():
                try:
                    txt = git_cfg.read_text(encoding="utf-8", errors="ignore")
                    if "github.com" in txt:
                        recommended.append("github")
                except Exception:
                    pass
            # Check for PostgreSQL configs or docker compose
            dc = self.workspace / "docker-compose.yml"
            if dc.exists():
                try:
                    txt = dc.read_text(encoding="utf-8", errors="ignore").lower()
                    if "postgres" in txt:
                        recommended.append("postgres")
                except Exception:
                    pass
        except Exception:
            pass

        return recommended

