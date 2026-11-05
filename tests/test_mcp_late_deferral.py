"""MCP servers connect in the background after startup: their tools must still be deferred behind ToolSearch."""
from typing import Any, ClassVar

from axon.tools.base import Tool
from axon.tools.interactive import (
    DEFER_MAX_TOKENS, ToolSearchTool, defer_mcp_tools, defer_threshold, deferred_tool_summary, forget_deferred,
)
from axon.tools.registry import ToolRegistry


def _mcp_tool(server: str, index: int, size: int = 800) -> Tool:
    class FakeMcpTool(Tool):
        name: ClassVar[str] = f"mcp__{server}__tool_{index}"
        description: ClassVar[str] = "x " * size
        schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"a": {"type": "string"}}}

        def run(self, args, ctx):  # pragma: no cover - never called
            return ""
    return FakeMcpTool()


def _registry_with(tools: list[Tool]) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_threshold_is_capped_for_huge_windows():
    assert defer_threshold(1_000_000, 0.10) == DEFER_MAX_TOKENS
    assert defer_threshold(10_000, 0.10) == 1_000            # a small window still uses its own 10%


def test_big_server_is_deferred_even_with_a_one_million_window():
    registry = _registry_with([_mcp_tool("vercel", i) for i in range(100)])
    assert defer_mcp_tools(registry, 1_000_000) is not None
    assert not [n for n in registry.visible_names() if n.startswith("mcp__")]
    assert "ToolSearch" in registry.visible_names()


def test_servers_that_connect_later_are_deferred_too():
    registry = _registry_with([_mcp_tool("vercel", i) for i in range(100)])
    defer_mcp_tools(registry, 1_000_000)
    registry.register(_mcp_tool("swiggy", 1, size=5))       # a tiny server arriving after the first deferral
    search = defer_mcp_tools(registry, 1_000_000)
    assert isinstance(search, ToolSearchTool)
    assert "mcp__swiggy__tool_1" in search.deferred
    assert not [n for n in registry.visible_names() if n.startswith("mcp__")]


def test_nothing_is_deferred_when_no_mcp_tools_exist_yet():
    registry = _registry_with([])
    assert defer_mcp_tools(registry, 1_000_000) is None


def test_a_disconnected_servers_tools_leave_the_search_list():
    registry = _registry_with([_mcp_tool("vercel", i) for i in range(100)])
    search = defer_mcp_tools(registry, 1_000_000)
    names = [n for n in search.deferred if "vercel" in n]
    forget_deferred(registry, names)
    assert not [n for n in search.deferred if "vercel" in n]


def test_summary_groups_names_by_server():
    text = deferred_tool_summary({"mcp__a__x": 1, "mcp__a__y": 1, "mcp__b__z": 1, "Plain": 1})
    assert "mcp__a__ (2): x, y" in text and "mcp__b__ (1): z" in text and "Plain" in text
