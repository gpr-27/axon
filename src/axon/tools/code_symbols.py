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
        suffix = file_path.suffix.lower()
        if suffix == ".py":
            return self._parse_python(file_path)
        else:
            return self._parse_generic(file_path)

    def _parse_python(self, file_path: Path) -> list[str]:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=str(file_path))
        except Exception as e:
            return [f"(Syntax/Parse error: {e})"]

        symbols: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                base_str = f"({', '.join(bases)})" if bases else ""
                doc = ast.get_docstring(node)
                doc_preview = f" — \"{doc.splitlines()[0][:60]}\"" if doc else ""
                symbols.append(f"class {node.name}{base_str} [L{node.lineno}-{node.end_lineno or node.lineno}]{doc_preview}")

                # Methods inside class
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                        args_list = [a.arg for a in sub.args.args]
                        m_doc = ast.get_docstring(sub)
                        m_doc_preview = f" — \"{m_doc.splitlines()[0][:50]}\"" if m_doc else ""
                        symbols.append(f"  • {prefix} {sub.name}({', '.join(args_list)}) [L{sub.lineno}-{sub.end_lineno or sub.lineno}]{m_doc_preview}")

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                args_list = [a.arg for a in node.args.args]
                doc = ast.get_docstring(node)
                doc_preview = f" — \"{doc.splitlines()[0][:60]}\"" if doc else ""
                symbols.append(f"{prefix} {node.name}({', '.join(args_list)}) [L{node.lineno}-{node.end_lineno or node.lineno}]{doc_preview}")

        return symbols

    def _parse_generic(self, file_path: Path) -> list[str]:
        symbols: list[str] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for idx, line in enumerate(content.splitlines(), 1):
                clean = line.strip()
                # Match class/function declarations in JS/TS/Go/Rust
                if re.match(r"^(export\s+)?(class|interface|type|struct|enum)\s+([A-Za-z0-9_]+)", clean):
                    symbols.append(f"{clean[:75]} [L{idx}]")
                elif re.match(r"^(export\s+)?(async\s+)?(function\s+|const\s+\w+\s*=\s*(async\s*)?\([^)]*\)\s*=>|func\s+|fn\s+)([A-Za-z0-9_]+)", clean):
                    symbols.append(f"{clean[:75]} [L{idx}]")
        except Exception:
            pass
        return symbols
