"""
Unit tests for MCP catalog, MCPManager, preset installation, and configuration persistence.
"""
from __future__ import annotations
import json
from pathlib import Path
from axon.mcp.catalog import MCP_CATALOG, get_mcp_preset
from axon.mcp.manager import MCPManager

def test_mcp_catalog_presets():
    preset_ids = [p.id for p in MCP_CATALOG]
    assert "github" in preset_ids
    assert "sqlite" in preset_ids
    assert "postgres" in preset_ids
    assert "brave-search" in preset_ids
    assert "puppeteer" in preset_ids
    assert "fetch" in preset_ids
    assert "memory" in preset_ids
    assert "filesystem" in preset_ids
    assert "git" in preset_ids

def test_mcp_manager_add_and_remove(tmp_path: Path):
    manager = MCPManager(workspace=tmp_path)
    # Override global and local files to tmp_path
    manager.global_config_file = tmp_path / "global_mcp.json"
    manager.local_config_file = tmp_path / "local_mcp.json"

    # Add custom server
    manager.add_server(
        name="custom-calc",
        command="python",
        args=["calc.py"],
        env={"PORT": "8080"},
        scope="global",
    )
    assert manager.global_config_file.exists()
    servers = manager.get_all_servers()
    assert "custom-calc" in servers
    assert servers["custom-calc"]["command"] == "python"
    assert servers["custom-calc"]["env"] == {"PORT": "8080"}

    # Install preset
    assert manager.add_preset("sqlite", scope="local")
    servers = manager.get_all_servers()
    assert "sqlite" in servers
    assert "custom-calc" in servers

    # Remove server
    assert manager.remove_server("custom-calc")
    servers = manager.get_all_servers()
    assert "custom-calc" not in servers
    assert "sqlite" in servers
