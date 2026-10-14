"""
Multi-language Code Graph & Tree-Sitter AST symbol indexing engine.
Supports structural symbol outlining, Go to Definition, and Find References
across Python, TypeScript, JavaScript, Go, Rust, C/C++, Java, and more.
"""
from __future__ import annotations
import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools._files import iter_files
from axon.tools.base import Tool, ToolContext, cap_text, default_summary, display_path, int_arg, output_cap, plural, resolve_path

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


def _arg_names(a: ast.arguments) -> list[str]:
    names = [x.arg for x in getattr(a, "posonlyargs", [])] + [x.arg for x in a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return names


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
                        args_list = _arg_names(sub.args)
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
                args_list = _arg_names(node.args)
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

            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id.isupper() or isinstance(node, ast.AnnAssign)):
                        symbols.append(SymbolNode(
                            name=t.id,
                            kind="variable",
                            file_path=rel_path,
                            line_number=node.lineno,
                            end_line_number=node.end_lineno or node.lineno,
                            signature=(ast.get_source_segment(content, node) or t.id).split("\n")[0][:80],
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


# ─── CodeSearch tool (symbols outline · go-to-definition · ranked concept search) ─

MAX_INDEX_FILES = 4000
MAX_SOURCE_BYTES = 1_000_000


def _source_files(root: Path, limit: int = MAX_INDEX_FILES) -> list[Path]:
    exts = MultiLanguageSymbolExtractor.LANGUAGE_EXTENSIONS
    out: list[Path] = []
    for fp in iter_files(root, include_hidden=False):
        if fp.suffix.lower() in exts:
            try:
                if fp.stat().st_size > MAX_SOURCE_BYTES:
                    continue
            except OSError:
                continue
            out.append(fp)
            if len(out) >= limit:
                break
    return out


def _tokenize(text: str) -> list[str]:
    """Split identifiers on camelCase / snake_case into lowercase word tokens."""
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s1)
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", s1):
        w = w.lower()
        if len(w) > 1 and not w.isdigit():
            out.append(w)
    return out


def _stem(tok: str) -> str:
    for suf in ("ing", "ers", "er", "ed", "es", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def _format_symbol(s: SymbolNode) -> str:
    line = f"L{s.line_number}" if s.line_number == s.end_line_number else f"L{s.line_number}-{s.end_line_number}"
    doc = f" — \"{s.docstring}\"" if s.docstring else ""
    if s.kind == "method":
        return f"    • {s.signature or s.name} [{line}]{doc}"
    return f"  {s.signature or s.name} [{line}]{doc}"


class CodeSearchTool(Tool):
    name: ClassVar[str] = "CodeSearch"
    description: ClassVar[str] = (
        "Structural code navigation (Python AST; regex-based for JS/TS, Go, Rust, Java, C/C++, C#, Ruby, PHP...). "
        "action=\"symbols\": outline classes/functions/methods with signatures and line ranges for a file or "
        "directory — far cheaper than reading whole files. action=\"definition\": find where `query` (a symbol name) "
        "is defined. action=\"search\": rank code symbols by relevance to a natural-language `query` "
        "(e.g. \"retry http request backoff\") when you don't know exact names. For usages/references of a "
        "name, use Grep with a word-boundary pattern (\\bname\\b)."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["symbols", "definition", "search"],
                       "description": "What to do (default: symbols if only path given, else definition)"},
            "query": {"type": "string", "description": "Symbol name (definition) or concept description (search)"},
            "path": {"type": "string", "description": "File or directory scope (default: workspace root)"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Result cap (default 30 for definition, 10 for search)"},
        },
        "required": [],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = str(args.get("query") or args.get("symbol") or "").strip()
        action = str(args.get("action") or "").strip().lower()
        if not action:
            action = "definition" if query else "symbols"
        raw_path = args.get("path") or args.get("file_hint") or "."
        if action == "definition" and args.get("file_hint") and not args.get("path"):
            raw_path = "."  # file_hint only prioritises; scope stays the workspace
        root = resolve_path(ctx, raw_path, allow_git_read=True)
        if not root.exists():
            raise ToolError(f"Path not found: {raw_path}")
        cap = output_cap(ctx, 30_000)

        if action == "symbols":
            return cap_text(self._symbols(root, ctx, args), cap, "Narrow 'path' to a subdirectory or file")
        if not query:
            raise ToolError(f"CodeSearch action '{action}' requires 'query'.")
        if action == "definition":
            return cap_text(self._definition(query, root, ctx, args), cap)
        if action == "search":
            return cap_text(self._search(query, root, ctx, args), cap)
        raise ToolError(f"Unknown action '{action}'. Use symbols, definition or search.")

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        if output.startswith("No "):
            return default_summary(output, False)
        m = re.match(r"Found (\d+) definition", output)
        if m:
            return plural(int(m.group(1)), "definition")
        m = re.match(r"Top (\d+) of (\d+) symbols", output)
        if m:
            return f"top {m.group(1)} of {plural(int(m.group(2)), 'related symbol')}"
        n_files = len(re.findall(r"^\S.*:$", output, re.M))
        n_syms = len(re.findall(r"\[L\d+", output))
        return f"{plural(n_syms, 'symbol')} in {plural(n_files, 'file')}"

    # ── actions ──
    def _symbols(self, root: Path, ctx: ToolContext, args: dict[str, Any]) -> str:
        files = [root] if root.is_file() else _source_files(root, 400)
        if not files:
            return f"No parseable source files found in {display_path(root, ctx.workspace)}."
        max_files = 60
        sections = []
        for fp in files[:max_files]:
            rel = display_path(fp, ctx.workspace)
            syms = MultiLanguageSymbolExtractor.extract_symbols(fp, rel)
            if syms:
                sections.append(f"{rel}:\n" + "\n".join(_format_symbol(s) for s in syms))
        if not sections:
            return f"No code symbols identified in {display_path(root, ctx.workspace)}."
        out = "\n\n".join(sections)
        if len(files) > max_files:
            out += f"\n\n[... {len(files) - max_files} more source files not outlined; narrow 'path' ...]"
        return out

    def _definition(self, symbol: str, root: Path, ctx: ToolContext, args: dict[str, Any]) -> str:
        limit = int_arg(args, "max_results", 30, lo=1, hi=100)
        name = symbol.split(".")[-1].split("::")[-1]
        parent = symbol.split(".")[-2] if "." in symbol else None
        hint = str(args.get("file_hint") or "").replace("\\", "/")
        exact: list[SymbolNode] = []
        loose: list[SymbolNode] = []
        files = [root] if root.is_file() else _source_files(root)
        if hint:
            files.sort(key=lambda f: 0 if hint in f.as_posix() else 1)
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not pat.search(text):
                continue  # cheap prefilter before parsing
            rel = display_path(fp, ctx.workspace)
            for s in MultiLanguageSymbolExtractor.extract_symbols(fp, rel):
                if s.name == name and (parent is None or s.parent == parent):
                    exact.append(s)
                elif s.name.lower() == name.lower():
                    loose.append(s)
            if len(exact) >= limit:
                break
        matches = exact or loose
        if not matches:
            return (f"No definition found for '{symbol}' in {display_path(root, ctx.workspace)}. "
                    f"Try action=\"search\" or Grep for the name.")
        lines = [f"Found {len(matches)} definition(s) for '{symbol}':"]
        for m in matches[:limit]:
            par = f" in {m.parent}" if m.parent else ""
            doc = f"\n    \"{m.docstring}\"" if m.docstring else ""
            lines.append(f"• [{m.kind.upper()}] {m.file_path}:{m.line_number}{par}\n    {m.signature}{doc}")
        return "\n".join(lines)

    def _search(self, query: str, root: Path, ctx: ToolContext, args: dict[str, Any]) -> str:
        limit = int_arg(args, "max_results", 10, lo=1, hi=50)
        q_terms = {_stem(t) for t in _tokenize(query)}
        if not q_terms:
            return f"Query '{query}' produced no searchable terms."
        files = [root] if root.is_file() else _source_files(root, 1500)

        docs: list[tuple[SymbolNode, list[str], str]] = []
        for fp in files:
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                continue
            rel = display_path(fp, ctx.workspace)
            syms = MultiLanguageSymbolExtractor.extract_symbols(fp, rel)
            for s in syms:
                if s.kind in ("error", "class") and s.end_line_number - s.line_number > 40:
                    body = lines[s.line_number - 1 : s.line_number + 8]  # class header only
                else:
                    end = max(s.end_line_number, s.line_number + 10)
                    body = lines[s.line_number - 1 : min(end, s.line_number + 80)]
                text = "\n".join(body)
                name_toks = [_stem(t) for t in _tokenize(f"{s.parent or ''} {s.name}")]
                toks = name_toks * 3 + [_stem(t) for t in _tokenize(f"{s.signature} {s.docstring} {text} {rel}")]
                docs.append((s, toks, text))
        if not docs:
            return f"No indexable source symbols found in {display_path(root, ctx.workspace)}."

        # BM25 over symbol chunks.
        import math
        from collections import Counter
        n = len(docs)
        avgdl = sum(len(d[1]) for d in docs) / n or 1.0
        df: Counter[str] = Counter()
        for _, toks, _ in docs:
            df.update(set(toks) & q_terms)
        k1, b = 1.4, 0.75
        scored: list[tuple[float, SymbolNode, str]] = []
        for s, toks, text in docs:
            tf = Counter(t for t in toks if t in q_terms)
            if not tf:
                continue
            dl = len(toks)
            score = 0.0
            for term, f in tf.items():
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
            score *= 1 + 0.5 * (len(tf) - 1)  # reward covering more query terms
            scored.append((score, s, text))
        if not scored:
            return f"No code related to '{query}' found. Try different wording or Grep."
        scored.sort(key=lambda x: -x[0])
        out = [f"Top {min(limit, len(scored))} of {len(scored)} symbols related to \"{query}\":"]
        for i, (score, s, text) in enumerate(scored[:limit], 1):
            preview = "\n".join(text.split("\n")[:6]).rstrip()
            out.append(f"\n{i}. {s.file_path}:{s.line_number} {s.kind} {s.parent + '.' if s.parent else ''}{s.name} "
                       f"(score {score:.1f})\n```\n{preview}\n```")
        return "\n".join(out)


# ─── Back-compat shims (not registered; kept for importers) ─────────────────

class GoToDefinitionTool(CodeSearchTool):
    """Deprecated: use CodeSearch(action="definition")."""
    name: ClassVar[str] = "GoToDefinition"
    hidden: ClassVar[bool] = True

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        sym = str(args.get("symbol") or args.get("query") or "").strip()
        if not sym:
            raise ToolError("GoToDefinition requires 'symbol' argument.")
        return super().run({**args, "action": "definition", "query": sym}, ctx)


class FindReferencesTool(Tool):
    """Deprecated: word-boundary Grep over source files."""
    name: ClassVar[str] = "FindReferences"
    description: ClassVar[str] = "Deprecated: use Grep with pattern \\bname\\b."
    schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"
    hidden: ClassVar[bool] = True

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        sym = str(args.get("symbol") or "").strip()
        if not sym:
            raise ToolError("FindReferences requires 'symbol' argument.")
        from axon.tools.search import GrepTool
        out = GrepTool().run({"pattern": r"\b" + re.escape(sym) + r"\b", "head_limit": int_arg(args, "max_results", 40, lo=1, hi=500)}, ctx)
        if out.startswith("No matches"):
            return f"No references found for '{sym}' in workspace."
        hits = [ln for ln in out.split("\n") if ln and not ln.startswith("[")]
        return f"Found {len(hits)} reference(s) for '{sym}':\n\n{out}"
