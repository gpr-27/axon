"""
Tool registry: lookup (with legacy-name aliases), allow/deny filtering, subsets for
subagents, and provider schema generation (Anthropic / OpenAI function format).
"""
from __future__ import annotations

import copy
from fnmatch import fnmatchcase
from typing import Any, Iterable, Iterator

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

# Legacy or commonly hallucinated tool names → (canonical tool, preset args).
# Resolved by get()/subset(); never advertised to the model.
DEFAULT_ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "LS": ("Ls", {}),
    "list_dir": ("Ls", {}),
    "FileTree": ("Ls", {"depth": 3}),
    "read_file": ("Read", {}),
    "view_file": ("Read", {}),
    "write_file": ("Write", {}),
    "edit_file": ("Edit", {}),
    "grep_search": ("Grep", {}),
    "run_command": ("Bash", {}),
    "notebook_edit": ("NotebookEdit", {}),
    "ApplyPatch": ("Patch", {}),
    "apply_patch": ("Patch", {}),
    "CodeSymbols": ("CodeSearch", {"action": "symbols"}),
    "GoToDefinition": ("CodeSearch", {"action": "definition"}),
    "SemanticSearch": ("CodeSearch", {"action": "search"}),
}

_ARG_RENAMES: dict[str, dict[str, str]] = {
    # alias name → {legacy arg: canonical arg}
    "FileTree": {"max_depth": "depth"},
    "GoToDefinition": {"symbol": "query"},
    "read_file": {"file_path": "path"},
    "view_file": {"file_path": "path", "AbsolutePath": "path"},
}


class AliasTool(Tool):
    """A legacy name bound to a canonical tool with preset/renamed arguments."""
    hidden = True

    def __init__(self, alias: str, target: Tool, preset: dict[str, Any] | None = None,
                 renames: dict[str, str] | None = None) -> None:
        self.alias = alias
        self.target = target
        self.preset = dict(preset or {})
        self.renames = dict(renames or {})

    # Instance attributes shadow the ClassVars so permissions/UI see the target's traits.
    @property
    def name(self) -> str:  # type: ignore[override]
        return self.target.name

    @property
    def description(self) -> str:  # type: ignore[override]
        return self.target.description

    @property
    def schema(self) -> dict[str, Any]:  # type: ignore[override]
        return self.target.schema

    @property
    def readonly(self) -> bool:  # type: ignore[override]
        return self.target.readonly

    @property
    def default_permission(self) -> str:  # type: ignore[override]
        return self.target.default_permission

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        merged = dict(self.preset)
        for k, v in (args or {}).items():
            merged[self.renames.get(k, k)] = v
        return self.target.run(merged, ctx)

    def render_call(self, args: dict[str, Any]) -> str:
        return self.target.render_call(args)

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        return self.target.summarize(args, output, is_error)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(name, p) or name.lower() == p.lower() for p in patterns)


def _sanitize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-Schema object valid for both Anthropic input_schema and
    OpenAI function parameters (type=object, properties dict, required ⊆ properties)."""
    params = copy.deepcopy(schema) if isinstance(schema, dict) else {}
    params["type"] = "object"
    props = params.get("properties")
    if not isinstance(props, dict):
        props = {}
    params["properties"] = props
    req = params.get("required")
    if not isinstance(req, list):
        req = []
    params["required"] = [r for r in req if isinstance(r, str) and r in props]
    return params


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, *, aliases: dict[str, tuple[str, dict[str, Any]]] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, tuple[str, dict[str, Any]]] = dict(DEFAULT_ALIASES if aliases is None else aliases)
        for t in tools or []:
            self.register(t)

    # ── mutation ──
    def register(self, tool: Tool, *, replace: bool = True) -> None:
        if not replace and tool.name in self._tools:
            raise ToolError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> Tool | None:
        """Remove a tool by name (or alias). Returns the removed tool, or None."""
        canonical = self._resolve_name(name)
        return self._tools.pop(canonical, None) if canonical else None

    def add_alias(self, alias: str, target: str, preset: dict[str, Any] | None = None) -> None:
        self._aliases[alias] = (target, dict(preset or {}))

    # ── lookup ──
    def _resolve_name(self, name: str) -> str | None:
        if name in self._tools:
            return name
        if name in self._aliases and self._aliases[name][0] in self._tools:
            return self._aliases[name][0]
        low = name.lower()
        for n in self._tools:
            if n.lower() == low:
                return n
        return None

    def get(self, name: str) -> Tool:
        if name in self._tools:
            return self._tools[name]
        if name in self._aliases:
            target, preset = self._aliases[name]
            if target in self._tools:
                if not preset and name not in _ARG_RENAMES:
                    return self._tools[target]
                return AliasTool(name, self._tools[target], preset, _ARG_RENAMES.get(name))
        canonical = self._resolve_name(name)
        if canonical:
            return self._tools[canonical]
        raise ToolError(
            f"Unknown tool '{name}'. Available tools: {', '.join(sorted(self.visible_names()))}"
        )

    def has(self, name: str) -> bool:
        return self._resolve_name(name) is not None

    __contains__ = has

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(list(self._tools.values()))

    @property
    def tools(self) -> dict[str, Tool]:
        return self._tools

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def visible_tools(self) -> list[Tool]:
        """Tools advertised to the model (hidden internal tools excluded)."""
        return [t for t in self._tools.values() if not getattr(t, "hidden", False)]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def visible_names(self) -> list[str]:
        return [t.name for t in self.visible_tools()]

    # ── schemas ──
    def schemas(self, provider_style: str = "anthropic", *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Export tool schemas, sorted by name for prompt-cache stability.

        provider_style "anthropic" → {name, description, input_schema};
        anything else (openai, deepseek, ...) → {"type": "function", "function": {...}}.
        """
        out: list[dict[str, Any]] = []
        pool = self.all_tools() if include_hidden else self.visible_tools()
        for t in sorted(pool, key=lambda x: x.name):
            params = _sanitize_schema(t.schema)
            desc = str(t.description or "").strip()
            if provider_style == "anthropic":
                out.append({"name": t.name, "description": desc, "input_schema": params})
            else:
                out.append({
                    "type": "function",
                    "function": {"name": t.name, "description": desc, "parameters": params},
                })
        return out

    # ── derived registries ──
    def subset(
        self,
        names: Iterable[str] | None = None,
        readonly_only: bool = False,
        *,
        allow: Iterable[str] | None = None,
        deny: Iterable[str] | None = None,
    ) -> ToolRegistry:
        """New registry restricted to `names` (aliases resolved; unknown names ignored),
        optionally only read-only tools, then filtered by allow/deny glob patterns."""
        if names is None:
            selected = list(self._tools.values())
        else:
            wanted: list[str] = []
            for n in names:
                canonical = self._resolve_name(str(n))
                if canonical and canonical not in wanted:
                    wanted.append(canonical)
            selected = [self._tools[n] for n in wanted]
        if readonly_only:
            selected = [t for t in selected if t.readonly or t.name == "ExitPlanMode"]
        reg = ToolRegistry(selected, aliases=self._aliases)
        if allow is not None or deny is not None:
            return reg.filtered(allow=allow, deny=deny)
        return reg

    def filtered(self, allow: Iterable[str] | None = None, deny: Iterable[str] | None = None) -> ToolRegistry:
        """Apply allow (keep only matching; None = keep all) then deny (drop matching) glob patterns,
        e.g. allow=["Read", "Grep", "mcp__github__*"], deny=["Bash"]."""
        allow_l = list(allow) if allow is not None else None
        deny_l = list(deny or [])
        kept = []
        for t in self._tools.values():
            if allow_l is not None and not _matches(t.name, allow_l):
                continue
            if deny_l and _matches(t.name, deny_l):
                continue
            kept.append(t)
        return ToolRegistry(kept, aliases=self._aliases)


def create_default_registry() -> ToolRegistry:
    from axon.tools import create_default_registry as _create
    return _create()
