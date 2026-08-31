"""
CodeSymbols tool: extract classes, functions, methods, docstrings, and signatures from source files without loading full file bodies.
"""
from __future__ import annotations
import ast
import re
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

class CodeSymbolsTool(Tool):
    name: ClassVar[str] = "CodeSymbols"
    description: ClassVar[str] = (
        "Extract structural code symbols (classes, functions, methods, signatures, docstrings, line numbers) "
        "from a file or directory. Extremely efficient for exploring architecture without loading full files."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file or directory to outline symbols from"},
            "max_depth": {"type": "integer", "description": "Max directory depth when path is a directory (default: 2)"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path", "")
        if not raw_path:
            raise ToolError("CodeSymbols requires 'path' argument.")

        p = resolve_in_workspace(ctx.workspace, raw_path, allow_git_read=True)
        if not p.exists():
            raise ToolError(f"Path not found: {raw_path}")

        files_to_parse: list[Path] = []
        if p.is_file():
            files_to_parse.append(p)
        else:
            max_depth = int(args.get("max_depth") or 2)
            for f in p.rglob("*"):
                if f.is_file() and not any(part.startswith(".") for part in f.relative_to(p).parts):
                    rel = f.relative_to(p)
                    if len(rel.parts) <= max_depth and f.suffix in (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".cpp", ".c", ".h"):
                        files_to_parse.append(f)

        if not files_to_parse:
            return f"No parseable source files found in {raw_path}."

        out_sections = []
        for file_path in files_to_parse[:30]:
            rel_file = file_path.relative_to(ctx.workspace).as_posix()
            symbols = self._parse_file(file_path)
            if symbols:
                out_sections.append(f"📄 {rel_file}:\n" + "\n".join(f"  {s}" for s in symbols))

        if not out_sections:
            return f"No code symbols identified in {raw_path}."

        res = "\n\n".join(out_sections)
        if len(files_to_parse) > 30:
            res += f"\n\n[... {len(files_to_parse) - 30} more files omitted ...]"
        return res

    def _parse_file(self, file_path: Path) -> list[str]:
        from axon.tools.code_graph import MultiLanguageSymbolExtractor
        symbols_nodes = MultiLanguageSymbolExtractor.extract_symbols(file_path)
        out: list[str] = []
        for s in symbols_nodes:
            parent_info = f" in {s.parent}" if s.parent else ""
            doc_preview = f" — \"{s.docstring}\"" if s.docstring else ""
            line_str = f"L{s.line_number}" if s.line_number == s.end_line_number else f"L{s.line_number}-{s.end_line_number}"
            if s.kind == "method":
                out.append(f"  • {s.signature} [{line_str}]{doc_preview}")
            else:
                out.append(f"{s.signature or s.name} [{line_str}]{parent_info}{doc_preview}")
        return out
