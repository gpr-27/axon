"""
Model Context Protocol client for Axon.

- :mod:`axon.mcp.config`     config discovery/merge (~/.axon/mcp.json, .mcp.json, .axon/mcp.json)
- :mod:`axon.mcp.transports` stdio, Streamable HTTP and legacy HTTP+SSE transports
- :mod:`axon.mcp.client`     JSON-RPC 2.0 session (initialize, tools, resources, prompts)
- :mod:`axon.mcp.bridge`     live connections + Axon tool registration
- :mod:`axon.mcp.commands`   the `/mcp` slash command
"""
