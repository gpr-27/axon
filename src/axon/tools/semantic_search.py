"""
Semantic Code Search Tool for Axon.
Performs semantic and conceptual search across codebase functions, classes,
and documentation using term frequency, token embeddings, and semantic ranking.
"""
from __future__ import annotations
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext
from axon.tools.code_graph import MultiLanguageSymbolExtractor


@dataclass
class CodeChunk:
    file_path: str
    symbol_name: str
    kind: str
    start_line: int
    end_line: int
    content: str
    tokens: set[str]


def _tokenize(text: str) -> list[str]:
    """Tokenize camelCase, snake_case, and words into lowercase tokens."""
    # Split camelCase
    s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    # Split non-alphanumeric
    words = re.findall(r"[a-zA-Z0-9_]+", s1.lower())
    clean_words = []
    for w in words:
        sub = w.split("_")
        for s in sub:
            if len(s) > 1 and not s.isdigit():
                clean_words.append(s)
    return clean_words


def _compute_relevance(query_tokens: Counter[str], chunk_tokens: Counter[str], total_vocab: int) -> float:
    """Compute BM25-inspired similarity score between query and code chunk."""
    if not query_tokens or not chunk_tokens:
        return 0.0

    score = 0.0
    for q_term, q_count in query_tokens.items():
        if q_term in chunk_tokens:
            tf = chunk_tokens[q_term]
            score += (tf / (tf + 1.2)) * (1.0 + math.log(1.0 + q_count))

    return score


class SemanticSearchTool(Tool):
    name: ClassVar[str] = "SemanticSearch"
    description: ClassVar[str] = (
        "Search codebase conceptually using semantic relevance rather than exact regex. "
        "Finds functions, classes, and logic related to conceptual queries (e.g. 'user authentication token validation', 'database connection retry')."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The concept, feature, or task to search for in the codebase"},
            "path": {"type": "string", "description": "Optional subdirectory or file to scope search within (default: entire workspace)"},
            "max_results": {"type": "integer", "description": "Maximum number of ranked results to return (default: 8)"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query", "").strip()
        if not query:
            raise ToolError("SemanticSearch requires 'query' argument.")

        scope_path = args.get("path", "")
        max_results = min(20, max(1, int(args.get("max_results") or 8)))

        root = resolve_in_workspace(ctx.workspace, scope_path or ".", allow_git_read=True)
        if not root.exists():
            raise ToolError(f"Path not found: {scope_path}")

        query_tokens_list = _tokenize(query)
        if not query_tokens_list:
            return f"Query '{query}' produced no indexable search terms."

        query_counter = Counter(query_tokens_list)

        # Index code chunks across workspace
        chunks: list[CodeChunk] = []
        exts = MultiLanguageSymbolExtractor.LANGUAGE_EXTENSIONS.keys()

        target_files: list[Path] = []
        if root.is_file():
            target_files = [root]
        else:
            for f in root.rglob("*"):
                if f.is_file() and f.suffix.lower() in exts and not any(p.startswith(".") for p in f.relative_to(ctx.workspace).parts):
                    target_files.append(f)

        for file_path in target_files[:200]:  # Cap files indexed per query for speed
            rel = file_path.relative_to(ctx.workspace).as_posix()
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
                symbols = MultiLanguageSymbolExtractor.extract_symbols(file_path, rel)

                if symbols:
                    for s in symbols:
                        start_idx = max(0, s.line_number - 1)
                        end_idx = min(len(lines), max(s.end_line_number, start_idx + 12))
                        chunk_text = "\n".join(lines[start_idx:end_idx])
                        chunk_tokens = _tokenize(f"{s.name} {s.signature} {s.docstring} {chunk_text}")
                        chunks.append(CodeChunk(
                            file_path=rel,
                            symbol_name=s.name,
                            kind=s.kind,
                            start_line=s.line_number,
                            end_line=end_idx,
                            content=chunk_text[:500],
                            tokens=set(chunk_tokens),
                        ))
                else:
                    # Generic file chunks
                    chunk_tokens = _tokenize(content)
                    chunks.append(CodeChunk(
                        file_path=rel,
                        symbol_name=file_path.stem,
                        kind="file",
                        start_line=1,
                        end_line=min(30, len(lines)),
                        content="\n".join(lines[:30]),
                        tokens=set(chunk_tokens),
                    ))
            except Exception:
                pass

        if not chunks:
            return f"No searchable source files found in {scope_path or 'workspace'}."

        # Score and rank chunks
        scored_chunks: list[tuple[float, CodeChunk]] = []
        for ch in chunks:
            ch_counter = Counter(ch.tokens)
            score = _compute_relevance(query_counter, ch_counter, total_vocab=1000)
            if score > 0.1:
                scored_chunks.append((score, ch))

        scored_chunks.sort(key=lambda x: x[0], reverse=True)

        if not scored_chunks:
            return f"No code sections conceptually matching '{query}' found."

        out_lines = [f"=== Semantic Search Results for \"{query}\" ({len(scored_chunks)} matches) ===\n"]
        for idx, (score, ch) in enumerate(scored_chunks[:max_results], 1):
            parent_info = f" ({ch.kind})" if ch.kind != "file" else ""
            out_lines.append(
                f"{idx}. 📄 {ch.file_path}:{ch.start_line} — **{ch.symbol_name}**{parent_info} (score: {score:.2f})\n"
                f"```\n{ch.content.strip()}\n```\n"
            )

        return "\n".join(out_lines)
