"""
FileTree tool: render a compact visual directory tree diagram with depth control and ignore rules.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

_IGNORED_DIRS = {".git", ".axon", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv", ".mypy_cache", ".ruff_cache"}

class FileTreeTool(Tool):
    name: ClassVar[str] = "FileTree"
    description: ClassVar[str] = (
        "Generate a visual directory tree diagram for the workspace or a subdirectory. "
        "Respects depth limits and automatically filters out heavy system directories."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Base directory path (default: workspace root)"},
            "max_depth": {"type": "integer", "description": "Maximum directory traversal depth (default: 3)"},
            "include_hidden": {"type": "boolean", "description": "Include hidden files (default: false)"},
        },
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        max_depth = min(10, max(1, int(args.get("max_depth") or 5)))
        include_hidden = bool(args.get("include_hidden", False))

        p = resolve_in_workspace(ctx.workspace, raw_path or ".", allow_git_read=True)
        if not p.exists():
            raise ToolError(f"Directory not found: {raw_path or '.'}")
        if not p.is_dir():
            raise ToolError(f"Path is a file, not a directory: {raw_path}")

        lines = [f"{p.name or '.'}/"]
        total_files = 0
        total_dirs = 0

        def _traverse(cur_dir: Path, prefix: str, depth: int) -> None:
            nonlocal total_files, total_dirs
            if depth > max_depth:
                return

            try:
                entries = sorted(cur_dir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except Exception:
                return

            # Filter entries
            filtered = []
            for e in entries:
                if not include_hidden and e.name.startswith(".") and e.name not in (".env.example",):
                    continue
                if e.is_dir() and e.name in _IGNORED_DIRS:
                    continue
                filtered.append(e)

            count = len(filtered)
            for idx, entry in enumerate(filtered):
                is_last = idx == (count - 1)
                connector = "└── " if is_last else "├── "
                sub_prefix = "    " if is_last else "│   "

                if entry.is_dir():
                    total_dirs += 1
                    lines.append(f"{prefix}{connector}{entry.name}/")
                    if depth < max_depth:
                        _traverse(entry, prefix + sub_prefix, depth + 1)
                else:
                    total_files += 1
                    try:
                        sz = entry.stat().st_size
                        sz_str = f" ({sz/1024:.1f} KB)" if sz >= 1024 else f" ({sz} B)"
                    except Exception:
                        sz_str = ""
                    lines.append(f"{prefix}{connector}{entry.name}{sz_str}")

                if len(lines) > 500:
                    lines.append(f"{prefix}... [truncated after 500 entries] ...")
                    return

        _traverse(p, "", 1)
        summary = f"\n\n({total_dirs} directories, {total_files} files)"
        return "\n".join(lines) + summary
