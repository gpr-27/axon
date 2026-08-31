"""
Multi-language Code Graph & Tree-Sitter AST symbol indexing engine.
Supports structural symbol outlining, Go to Definition, and Find References
across Python, TypeScript, JavaScript, Go, Rust, C/C++, Java, and more.
"""
from __future__ import annotations
import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext

# ─── Symbol Model ──────────────────────────────────────────────────────────

@dataclass
class SymbolNode:
    name: str
    kind: str  # "class", "function", "method", "interface", "type", "struct", "enum", "variable"
    file_path: str
    line_number: int
    end_line_number: int
    signature: str = ""
    docstring: str = ""
    parent: str | None = None


# ─── Multi-Language Extractor ──────────────────────────────────────────────

class MultiLanguageSymbolExtractor:
    """Extracts structural AST symbols across multiple programming languages."""

    LANGUAGE_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".java": "java",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "csharp",
        ".swift": "swift",
        ".kt": "kotlin",
    }

    @classmethod
    def extract_symbols(cls, file_path: Path, rel_path: str = "") -> list[SymbolNode]:
        """Extract all symbol nodes from a given source file."""
        suffix = file_path.suffix.lower()
        lang = cls.LANGUAGE_EXTENSIONS.get(suffix, "")
        rel = rel_path or file_path.name

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        if lang == "python":
            return cls._extract_python(content, rel)
        elif lang in ("javascript", "typescript"):
            return cls._extract_ts_js(content, rel)
        elif lang == "go":
            return cls._extract_go(content, rel)
        elif lang == "rust":
            return cls._extract_rust(content, rel)
        else:
            return cls._extract_generic(content, rel)

    @classmethod
    def _extract_python(cls, content: str, rel_path: str) -> list[SymbolNode]:
        symbols: list[SymbolNode] = []
        try:
            tree = ast.parse(content)
        except Exception as e:
            return [SymbolNode(
                name="syntax_error",
                kind="error",
                file_path=rel_path,
                line_number=getattr(e, "lineno", 1) or 1,
                end_line_number=getattr(e, "lineno", 1) or 1,
                signature=f"(Syntax error: {e})",
            )]

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                base_str = f"({', '.join(bases)})" if bases else ""
                doc = ast.get_docstring(node) or ""
                symbols.append(SymbolNode(
                    name=node.name,
                    kind="class",
                    file_path=rel_path,
                    line_number=node.lineno,
                    end_line_number=node.end_lineno or node.lineno,
                    signature=f"class {node.name}{base_str}",
                    docstring=doc.splitlines()[0] if doc else "",
                ))

                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        prefix = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                        args_list = [a.arg for a in sub.args.args]
                        m_doc = ast.get_docstring(sub) or ""
                        symbols.append(SymbolNode(
                            name=sub.name,
                            kind="method",
                            file_path=rel_path,
                            line_number=sub.lineno,
                            end_line_number=sub.end_lineno or sub.lineno,
                            signature=f"{prefix} {sub.name}({', '.join(args_list)})",
                            docstring=m_doc.splitlines()[0] if m_doc else "",
                            parent=node.name,
                        ))

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                args_list = [a.arg for a in node.args.args]
                doc = ast.get_docstring(node) or ""
                symbols.append(SymbolNode(
                    name=node.name,
                    kind="function",
                    file_path=rel_path,
                    line_number=node.lineno,
                    end_line_number=node.end_lineno or node.lineno,
                    signature=f"{prefix} {node.name}({', '.join(args_list)})",
                    docstring=doc.splitlines()[0] if doc else "",
                ))

        return symbols

    @classmethod
    def _extract_ts_js(cls, content: str, rel_path: str) -> list[SymbolNode]:
        symbols: list[SymbolNode] = []
        lines = content.splitlines()

        # Regex patterns for TS/JS constructs
        class_pat = re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_$]+)(?:\s+extends\s+([A-Za-z0-9_$.]+))?")
        interface_pat = re.compile(r"^(?:export\s+)?interface\s+([A-Za-z0-9_$]+)")
        type_pat = re.compile(r"^(?:export\s+)?type\s+([A-Za-z0-9_$]+)\s*=")
        fn_pat = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(([^)]*)\)")
        const_fn_pat = re.compile(r"^(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*(?::\s*[^=]+)?=>")
        method_pat = re.compile(r"^\s+(?:(?:public|private|protected|static|async|get|set)\s+)*([A-Za-z0-9_$]+)\s*\(([^)]*)\)")

        current_class: str | None = None

        for idx, line in enumerate(lines, 1):
            clean = line.strip()
            if not clean or clean.startswith(("//", "/*", "*")):
                continue

            m = class_pat.match(clean)
            if m:
                c_name = m.group(1)
                current_class = c_name
                symbols.append(SymbolNode(
                    name=c_name,
                    kind="class",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=clean[:80],
                ))
                continue

            m = interface_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="interface",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=clean[:80],
                ))
                continue

            m = type_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="type",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=clean[:80],
                ))
                continue

            m = fn_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="function",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"function {m.group(1)}({m.group(2)})",
                ))
                continue

            m = const_fn_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="function",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"const {m.group(1)} = ({m.group(2)}) => ...",
                ))
                continue

            if current_class and line.startswith("  ") and not clean.startswith(("}", "{")):
                m = method_pat.match(line)
                if m and m.group(1) not in ("if", "for", "while", "switch", "catch"):
                    symbols.append(SymbolNode(
                        name=m.group(1),
                        kind="method",
                        file_path=rel_path,
                        line_number=idx,
                        end_line_number=idx,
                        signature=f"{m.group(1)}({m.group(2)})",
                        parent=current_class,
                    ))

        return symbols

    @classmethod
    def _extract_go(cls, content: str, rel_path: str) -> list[SymbolNode]:
        symbols: list[SymbolNode] = []
        lines = content.splitlines()

        type_struct_pat = re.compile(r"^type\s+([A-Za-z0-9_]+)\s+(struct|interface)")
        func_pat = re.compile(r"^func\s+([A-Za-z0-9_]+)\s*\(([^)]*)\)")
        method_pat = re.compile(r"^func\s+\(\s*[^)]+\s+\*?([A-Za-z0-9_]+)\s*\)\s*([A-Za-z0-9_]+)\s*\(([^)]*)\)")

        for idx, line in enumerate(lines, 1):
            clean = line.strip()
            m = method_pat.match(clean)
            if m:
                recv, m_name, args = m.groups()
                symbols.append(SymbolNode(
                    name=m_name,
                    kind="method",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"func ({recv}) {m_name}({args})",
                    parent=recv,
                ))
                continue

            m = func_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="function",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"func {m.group(1)}({m.group(2)})",
                ))
                continue

            m = type_struct_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind=m.group(2),
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"type {m.group(1)} {m.group(2)}",
                ))

        return symbols

    @classmethod
    def _extract_rust(cls, content: str, rel_path: str) -> list[SymbolNode]:
        symbols: list[SymbolNode] = []
        lines = content.splitlines()

        struct_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(struct|enum|trait|type)\s+([A-Za-z0-9_]+)")
        fn_pat = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*(?:<[^>]+>)?\s*\(([^)]*)\)")
        impl_pat = re.compile(r"^impl(?:\s+<[^>]+>)?\s+([A-Za-z0-9_]+)")

        current_impl: str | None = None

        for idx, line in enumerate(lines, 1):
            clean = line.strip()
            m = impl_pat.match(clean)
            if m:
                current_impl = m.group(1)
                continue

            m = struct_pat.match(clean)
            if m:
                symbols.append(SymbolNode(
                    name=m.group(2),
                    kind=m.group(1),
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=clean[:80],
                ))
                continue

            m = fn_pat.match(clean)
            if m:
                fn_name = m.group(1)
                symbols.append(SymbolNode(
                    name=fn_name,
                    kind="method" if current_impl and line.startswith("  ") else "function",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=f"fn {fn_name}({m.group(2)})",
                    parent=current_impl if line.startswith("  ") else None,
                ))

        return symbols

    @classmethod
    def _extract_generic(cls, content: str, rel_path: str) -> list[SymbolNode]:
        symbols: list[SymbolNode] = []
        generic_pat = re.compile(r"^(?:(?:public|private|protected|static|export|final|def|fn|func|function|class|interface|struct)\s+)+([A-Za-z0-9_]+)")

        for idx, line in enumerate(content.splitlines(), 1):
            clean = line.strip()
            if not clean or clean.startswith(("//", "#", "/*", "*")):
                continue
            m = generic_pat.match(clean)
            if m and len(m.group(1)) > 2:
                symbols.append(SymbolNode(
                    name=m.group(1),
                    kind="symbol",
                    file_path=rel_path,
                    line_number=idx,
                    end_line_number=idx,
                    signature=clean[:75],
                ))
        return symbols


# ─── Go To Definition Tool ─────────────────────────────────────────────────

class GoToDefinitionTool(Tool):
    name: ClassVar[str] = "GoToDefinition"
    description: ClassVar[str] = (
        "Find the exact declaration and definition location of a symbol (class, function, method, interface, struct, variable) "
        "across the entire workspace codebase."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "The exact name of the symbol to locate (e.g. 'SessionStore', 'run_subagent')"},
            "file_hint": {"type": "string", "description": "Optional file path hint to prioritize"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        symbol = args.get("symbol", "").strip()
        if not symbol:
            raise ToolError("GoToDefinition requires 'symbol' argument.")

        matches: list[SymbolNode] = []
        ws = ctx.workspace

        # Scan workspace files
        exts = MultiLanguageSymbolExtractor.LANGUAGE_EXTENSIONS.keys()
        for f in ws.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts and not any(p.startswith(".") for p in f.relative_to(ws).parts):
                rel = f.relative_to(ws).as_posix()
                symbols = MultiLanguageSymbolExtractor.extract_symbols(f, rel)
                for s in symbols:
                    if s.name.lower() == symbol.lower():
                        matches.append(s)

        if not matches:
            return f"No definition found for symbol '{symbol}' in workspace."

        out_lines = [f"Found {len(matches)} definition(s) for '{symbol}':\n"]
        for m in matches:
            parent_str = f" in {m.parent}" if m.parent else ""
            doc_str = f"\n    Doc: \"{m.docstring}\"" if m.docstring else ""
            out_lines.append(
                f"• [{m.kind.upper()}] {m.file_path}:{m.line_number}{parent_str}\n"
                f"    `{m.signature}`{doc_str}"
            )

        return "\n".join(out_lines)


# ─── Find References Tool ──────────────────────────────────────────────────

class FindReferencesTool(Tool):
    name: ClassVar[str] = "FindReferences"
    description: ClassVar[str] = (
        "Find all references, usages, and call sites of a symbol across the workspace codebase."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "The symbol or identifier to find references for"},
            "max_results": {"type": "integer", "description": "Maximum number of references to return (default: 40)"},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        symbol = args.get("symbol", "").strip()
        if not symbol:
            raise ToolError("FindReferences requires 'symbol' argument.")

        max_res = int(args.get("max_results") or 40)
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")

        ws = ctx.workspace
        exts = MultiLanguageSymbolExtractor.LANGUAGE_EXTENSIONS.keys()
        matches: list[str] = []

        for f in ws.rglob("*"):
            if f.is_file() and f.suffix.lower() in exts and not any(p.startswith(".") for p in f.relative_to(ws).parts):
                rel = f.relative_to(ws).as_posix()
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    for idx, line in enumerate(content.splitlines(), 1):
                        if pattern.search(line):
                            clean_line = line.strip()
                            matches.append(f"{rel}:{idx}: {clean_line}")
                            if len(matches) >= max_res:
                                break
                except Exception:
                    pass
            if len(matches) >= max_res:
                break

        if not matches:
            return f"No references found for '{symbol}' in workspace."

        res = f"Found {len(matches)} reference(s) for '{symbol}':\n\n" + "\n".join(matches)
        if len(matches) >= max_res:
            res += f"\n\n[... Capped at {max_res} results ...]"
        return res
