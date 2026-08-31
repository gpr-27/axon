"""
Model Context Protocol (MCP) Server Presets Catalog.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class MCPServerPreset:
    id: str
    name: str
    description: str
    command: str
    args: list[str]
    env_vars: dict[str, str] = field(default_factory=dict)
    category: str = "Developer Tools"

MCP_CATALOG: list[MCPServerPreset] = [
    MCPServerPreset(
        id="github",
        name="GitHub MCP",
        description="Inspect repositories, search code, manage issues, and review pull requests",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env_vars={"GITHUB_PERSONAL_ACCESS_TOKEN": "your_github_token_here"},
        category="Developer Tools",
    ),
    MCPServerPreset(
        id="sqlite",
        name="SQLite Database MCP",
        description="Connect, query, inspect tables, and analyze SQLite database files",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-sqlite", "--db-path", "./database.sqlite"],
        category="Databases",
    ),
    MCPServerPreset(
        id="postgres",
        name="PostgreSQL MCP",
        description="Read-only schema inspection and SQL queries for PostgreSQL databases",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/mydb"],
        category="Databases",
    ),
    MCPServerPreset(
        id="brave-search",
        name="Brave Web Search MCP",
        description="High-speed web search, news aggregation, and live research",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-brave-search"],
        env_vars={"BRAVE_API_KEY": "your_brave_api_key_here"},
        category="Research & Web",
    ),
    MCPServerPreset(
        id="puppeteer",
        name="Puppeteer Web Browser MCP",
        description="Automate headless browser navigation, web scraping, and visual screenshot capture",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-puppeteer"],
        category="Research & Web",
    ),
    MCPServerPreset(
        id="fetch",
        name="Fetch & Markdown Scraper MCP",
        description="Fetch URLs and convert web pages into clean, optimized markdown",
        command="uvx",
        args=["mcp-server-fetch"],
        category="Research & Web",
    ),
    MCPServerPreset(
        id="memory",
        name="Knowledge Graph Memory MCP",
        description="Persistent entity and relation knowledge graph across conversations",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-memory"],
        category="Memory & Knowledge",
    ),
    MCPServerPreset(
        id="filesystem",
        name="Filesystem Access MCP",
        description="Secure sandboxed access to local directories and assets",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "./"],
        category="Developer Tools",
    ),
    MCPServerPreset(
        id="git",
        name="Git Server MCP",
        description="Deep git tree analysis, commit diff inspection, and blame tracking",
        command="uvx",
        args=["mcp-server-git", "--repository", "."],
        category="Developer Tools",
    ),
    MCPServerPreset(
        id="slack",
        name="Slack Integration MCP",
        description="Read channels, post thread updates, and search workplace discussions",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env_vars={"SLACK_BOT_TOKEN": "xoxb-...", "SLACK_TEAM_ID": "T..."},
        category="Integrations",
    ),
]

def get_mcp_preset(preset_id: str) -> MCPServerPreset | None:
    for p in MCP_CATALOG:
        if p.id.lower() == preset_id.lower():
            return p
    return None
