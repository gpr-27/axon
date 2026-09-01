"""
Search tools: Glob (mtime-descending), Grep (ripgrep with pure-Python fallback), and Ls.
"""
from __future__ import annotations
import glob
import os
import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

class GlobTool(Tool):
    name: ClassVar[str] = "Glob"
    description: ClassVar[str] = (
        "Find files matching a glob pattern (e.g. '**/*.py'). "
        "Returns paths sorted by most recently modified first (mtime descending)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to workspace"},
            "path": {"type": "string", "description": "Base directory to search (default: workspace)"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        pattern = args.get("pattern", "")
        base_dir = args.get("path", "")
        root = resolve_in_workspace(ctx.workspace, base_dir or ".")

        full_pattern = os.path.join(str(root), pattern)
        matches = glob.glob(full_pattern, recursive=True)

        # Filter out hidden system files and internal .axon/.git
        files = []
        for m in matches:
            p = Path(m)
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts):
                try:
                    mtime = p.stat().st_mtime
                    rel = p.relative_to(ctx.workspace).as_posix()
                    files.append((mtime, rel))
                except Exception:
                    pass

        # Sort mtime descending (most recently modified first)
        files.sort(key=lambda x: x[0], reverse=True)
        paths = [f[1] for f in files]

        if not paths:
            return f"No files matched pattern '{pattern}'."
        if len(paths) > 100:
            return "\n".join(paths[:100]) + f"\n\n[... {len(paths) - 100} more files matched ...]"
        return "\n".join(paths)


class GrepTool(Tool):
    name: ClassVar[str] = "Grep"
    description: ClassVar[str] = (
        "Search file contents with a regular expression. "
        "Respects .gitignore and searches fast. "
        "Returns matching lines with file:line format."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression pattern to search for"},
            "path": {"type": "string", "description": "Directory or file to search in (default: workspace)"},
            "glob": {"type": "string", "description": "Optional glob filter (e.g. '*.py')"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        pattern = args.get("pattern", "")
        search_path = args.get("path", "")
        glob_filter = args.get("glob", "")

        if not pattern:
            raise ToolError("Grep requires 'pattern'.")

        target = resolve_in_workspace(ctx.workspace, search_path or ".", allow_git_read=True)

        # Try ripgrep first if installed
        try:
            cmd = ["rg", "-n", "--max-count=100", "--no-heading", "--no-config"]
            if glob_filter:
                cmd.extend(["-g", glob_filter])
            cmd.append(pattern)
            try:
                rel_target = target.relative_to(ctx.workspace).as_posix()
                if rel_target and rel_target != ".":
                    cmd.append(rel_target)
            except ValueError:
                cmd.append(str(target))

            res = subprocess.run(
                cmd,
                cwd=str(ctx.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res.returncode == 0:
                if res.stdout.strip():
                    return res.stdout.strip()
            elif res.returncode == 1:
                return f"No matches found for pattern '{pattern}'."
            elif res.returncode == 2:
                err_msg = res.stderr.strip() or f"ripgrep error for pattern '{pattern}'"
                raise ToolError(f"Invalid regular expression '{pattern}': {err_msg}")
        except FileNotFoundError:
            pass

        # Pure-Python fallback
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regular expression '{pattern}': {e}") from e

        results: list[str] = []
        files_to_search: list[Path] = []

        if target.is_file():
            files_to_search.append(target)
        else:
            for root, dirs, files in os.walk(target):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
                for f in files:
                    if f.startswith("."):
                        continue
                    if glob_filter and not glob.fnmatch.fnmatch(f, glob_filter):
                        continue
                    files_to_search.append(Path(root) / f)

        for fpath in files_to_search:
            if len(results) >= 100:
                break
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
                for line_idx, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        rel_path = fpath.relative_to(ctx.workspace).as_posix()
                        results.append(f"{rel_path}:{line_idx}:{line.strip()}")
                        if len(results) >= 100:
                            break
            except Exception:
                continue

        if not results:
            return f"No matches found for pattern '{pattern}'."
        return "\n".join(results)


class LsTool(Tool):
    name: ClassVar[str] = "Ls"
    description: ClassVar[str] = (
        "List immediate files and subdirectories in a directory with item types, counts, and sizes. "
        "Best for fast shallow exploration of a specific folder (use FileTree for full hierarchy diagrams, or Glob for pattern searching)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path to list (default: workspace root)"},
            "detailed": {"type": "boolean", "description": "Include file sizes and item counts (default: false)"},
        },
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        target_path = args.get("path", "")
        detailed = bool(args.get("detailed", False))
        p = resolve_in_workspace(ctx.workspace, target_path or ".", allow_git_read=True)

        if not p.exists():
            raise ToolError(f"Directory not found: {target_path or '.'}")
        if not p.is_dir():
            raise ToolError(f"Path is a file, not a directory: {target_path}")

        entries = []
        dir_count = 0
        file_count = 0

        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                dir_count += 1
                entries.append(f"{item.name}/")
            else:
                file_count += 1
                if detailed:
                    try:
                        sz = item.stat().st_size
                        sz_str = f" ({sz/1024:.1f} KB)" if sz >= 1024 else f" ({sz} B)"
                    except Exception:
                        sz_str = ""
                    entries.append(f"{item.name}{sz_str}")
                else:
                    entries.append(item.name)

        if not entries:
            return "(Empty directory)"

        res = "\n".join(entries)
        if detailed:
            res += f"\n\nTotal: {dir_count} directories, {file_count} files"
        return res
