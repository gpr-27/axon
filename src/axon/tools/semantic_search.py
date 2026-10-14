"""
Deprecated shim: semantic code search now lives in CodeSearch(action="search")
(axon.tools.code_graph.CodeSearchTool). Kept for importers; not registered.
"""
from __future__ import annotations

from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import ToolContext
from axon.tools.code_graph import CodeSearchTool


class SemanticSearchTool(CodeSearchTool):
    name: ClassVar[str] = "SemanticSearch"
    hidden: ClassVar[bool] = True

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("SemanticSearch requires 'query' argument.")
        return super().run({"action": "search", "query": query, "path": args.get("path") or ".",
                            "max_results": args.get("max_results") or 8}, ctx)


__all__ = ["SemanticSearchTool"]
