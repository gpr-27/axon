"""
Backward-compatible facade: ``MCPManager`` is the MCP configuration store.
The implementation lives in :mod:`axon.mcp.config`.
"""
from __future__ import annotations

from axon.mcp.config import MCPConfig, ServerSpec

MCPManager = MCPConfig

__all__ = ["MCPManager", "MCPConfig", "ServerSpec"]
