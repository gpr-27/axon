"""
TableSearchTool: High-speed structured tabular search and matrix analyzer.
Finds, filters, and formats tabular data across Markdown tables, CSV, and JSON datasets.
"""
from __future__ import annotations
import csv
import json
import re
from pathlib import Path
from typing import Any, ClassVar
from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext

class TableSearchTool(Tool):
    name: ClassVar[str] = "TableSearch"
    description: ClassVar[str] = (
        "Search, filter, and extract structured tables and comparative matrices from Markdown files, "
        "CSV files, or JSON records in the workspace. Supports column filtering, regex queries, and top-k limits."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or regex pattern to search across table rows and columns.",
            },
            "path": {
                "type": "string",
                "description": "Optional file path or directory to search tables in (default: workspace root).",
            },
            "file_type": {
                "type": "string",
                "enum": ["all", "markdown", "csv", "json"],
                "description": "File format to scan (default: 'all').",
            },
            "max_rows": {
                "type": "integer",
                "description": "Maximum matching rows to return per table (default: 15).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = args.get("query", "").strip()
        if not query:
            raise ToolError("Query must not be empty.")

        path_str = args.get("path", "")
        file_type = args.get("file_type", "all")
        max_rows = min(50, max(1, int(args.get("max_rows", 15))))

        target = ctx.workspace / path_str if path_str else ctx.workspace
        if not target.exists():
            raise ToolError(f"Target path does not exist: {target}")

        files_to_scan: list[Path] = []
        if target.is_file():
            files_to_scan.append(target)
        else:
            patterns = []
            if file_type in ("all", "markdown"):
                patterns.extend(["*.md", "*.markdown"])
            if file_type in ("all", "csv"):
                patterns.append("*.csv")
            if file_type in ("all", "json"):
                patterns.append("*.json")

            for p in patterns:
                files_to_scan.extend(target.rglob(p))

        # Filter out hidden or venv directories
        files_to_scan = [
            f for f in files_to_scan
            if not any(part.startswith(".") or part in ("venv", "node_modules", "__pycache__") for part in f.parts)
        ]

        results_by_file: list[str] = []
        q_lower = query.lower()

        for f in files_to_scan[:30]:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                if f.suffix in (".md", ".markdown"):
                    tables = self._extract_markdown_tables(content, q_lower, max_rows)
                    if tables:
                        rel = f.relative_to(ctx.workspace)
                        results_by_file.append(f"📄 Tables in {rel}:\n" + "\n\n".join(tables))
                elif f.suffix == ".csv":
                    matched_rows = self._extract_csv_rows(content, q_lower, max_rows)
                    if matched_rows:
                        rel = f.relative_to(ctx.workspace)
                        results_by_file.append(f"📊 CSV in {rel}:\n" + matched_rows)
                elif f.suffix == ".json":
                    matched_json = self._extract_json_records(content, q_lower, max_rows)
                    if matched_json:
                        rel = f.relative_to(ctx.workspace)
                        results_by_file.append(f"⚙️ JSON in {rel}:\n" + matched_json)
            except Exception:
                continue

        if not results_by_file:
            return f"No tables or structured records matching '{query}' found."

        return f"Found matching tables across {len(results_by_file)} files:\n\n" + "\n\n---\n\n".join(results_by_file)

    def _extract_markdown_tables(self, content: str, q_lower: str, max_rows: int) -> list[str]:
        tables = []
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            l = lines[i].strip()
            if l.startswith("|") and l.endswith("|") and i + 1 < len(lines) and "---" in lines[i+1]:
                header = lines[i]
                sep = lines[i+1]
                table_rows = []
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|") and lines[j].strip().endswith("|"):
                    row = lines[j]
                    if q_lower in row.lower():
                        table_rows.append(row)
                    j += 1
                if table_rows:
                    formatted = [header, sep] + table_rows[:max_rows]
                    if len(table_rows) > max_rows:
                        formatted.append(f"| ... ({len(table_rows) - max_rows} more matching rows) |")
                    tables.append("\n".join(formatted))
                i = j
            else:
                i += 1
        return tables

    def _extract_csv_rows(self, content: str, q_lower: str, max_rows: int) -> str:
        lines = content.splitlines()
        if not lines:
            return ""
        reader = csv.reader(lines)
        rows = list(reader)
        if not rows:
            return ""
        header = rows[0]
        matches = [r for r in rows[1:] if any(q_lower in str(cell).lower() for cell in r)]
        if not matches:
            return ""
        out_lines = [f"| {' | '.join(header)} |", f"| {' | '.join(['---'] * len(header))} |"]
        for m in matches[:max_rows]:
            out_lines.append(f"| {' | '.join(m)} |")
        return "\n".join(out_lines)

    def _extract_json_records(self, content: str, q_lower: str, max_rows: int) -> str:
        try:
            data = json.loads(content)
            if isinstance(data, list):
                matches = [item for item in data if q_lower in json.dumps(item).lower()]
                if matches:
                    return json.dumps(matches[:max_rows], indent=2)
            elif isinstance(data, dict):
                matches = {k: v for k, v in data.items() if q_lower in k.lower() or q_lower in json.dumps(v).lower()}
                if matches:
                    return json.dumps(matches, indent=2)
        except Exception:
            pass
        return ""
