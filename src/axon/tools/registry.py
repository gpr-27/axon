"""
Tool registry and schema formatting.
"""
from __future__ import annotations
from typing import Any
from axon.errors import ToolError
from axon.tools.base import Tool

class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"Unknown tool '{name}'. Available tools: {', '.join(sorted(self._tools.keys()))}"
            )
        return self._tools[name]

    @property
    def tools(self) -> dict[str, Tool]:
        return self._tools

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self, provider_style: str = "anthropic") -> list[dict[str, Any]]:
        """
        Export tool schemas in either Anthropic or OpenAI-compatible function format.
        """
        schemas = []
        for t in self._tools.values():
            if provider_style == "anthropic":
                schemas.append({
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.schema,
                })
            else:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    }
                })
        return schemas

    def subset(self, names: list[str] | None = None, readonly_only: bool = False) -> ToolRegistry:
        selected = []
        for name, tool in self._tools.items():
            if names is not None and name not in names:
                continue
            if readonly_only and not tool.readonly:
                continue
            selected.append(tool)
        return ToolRegistry(selected)

def create_default_registry() -> ToolRegistry:
    from axon.tools import create_default_registry as _create
    return _create()
