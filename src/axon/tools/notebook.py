"""
Jupyter Notebook (.ipynb) cell-level editor and inspector.
Safely inspects, edits, inserts, deletes, and cleans cells in Jupyter notebooks
without corrupting JSON schemas, metadata, or execution structures.
"""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.permissions.paths import resolve_in_workspace
from axon.tools.base import Tool, ToolContext


class NotebookEditTool(Tool):
    name: ClassVar[str] = "notebook_edit"
    description: ClassVar[str] = (
        "Safe cell-level Jupyter Notebook (.ipynb) reader, editor, inserter, and output manager. "
        "Allows viewing notebook cells with outputs, updating specific cell sources, adding/removing cells, "
        "and clearing heavy outputs without corrupting notebook JSON structure."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Path to the .ipynb Jupyter notebook file (relative to workspace or absolute).",
            },
            "action": {
                "type": "string",
                "enum": ["read_cells", "edit_cell", "insert_cell", "delete_cell", "clear_outputs"],
                "description": "The notebook operation to perform.",
            },
            "cell_index": {
                "type": "integer",
                "description": "0-indexed position of the target cell (required for edit_cell, delete_cell; optional for insert_cell, clear_outputs).",
            },
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown", "raw"],
                "description": "Type of cell (for insert_cell or edit_cell). Defaults to 'code'.",
            },
            "source": {
                "type": "string",
                "description": "The new code or markdown text content for the cell (required for edit_cell and insert_cell).",
            },
        },
        "required": ["notebook_path", "action"],
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("notebook_path", "").strip()
        if not raw_path:
            raise ToolError("Missing required parameter: 'notebook_path'.")

        target_path = resolve_in_workspace(ctx.workspace, raw_path)
        action = args.get("action", "read_cells").strip().lower()
        cell_index = args.get("cell_index")
        cell_type = args.get("cell_type", "code")
        source = args.get("source")

        if not target_path.exists():
            if action == "insert_cell":
                # Create a fresh notebook if it doesn't exist
                nb_data = {
                    "cells": [],
                    "metadata": {
                        "language_info": {"name": "python"},
                        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                    },
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            else:
                raise ToolError(f"Notebook file not found: {raw_path}")
        else:
            try:
                content = target_path.read_text(encoding="utf-8")
                nb_data = json.loads(content)
            except Exception as e:
                raise ToolError(f"Failed to parse notebook JSON at {raw_path}: {e}") from e

        if "cells" not in nb_data or not isinstance(nb_data["cells"], list):
            raise ToolError(f"Invalid notebook format: 'cells' array is missing in {raw_path}.")

        cells: list[dict[str, Any]] = nb_data["cells"]

        # ── 1. READ CELLS ──────────────────────────────────────────────────
        if action == "read_cells":
            if not cells:
                return f"=== Notebook: {target_path.name} (0 cells) ===\n[Empty Notebook]"

            lines: list[str] = [f"=== Notebook: {target_path.name} ({len(cells)} cells) ==="]
            for idx, c in enumerate(cells):
                c_type = c.get("cell_type", "code")
                exec_count = c.get("execution_count")
                exec_str = f" [Execution Count: {exec_count}]" if exec_count is not None else ""
                lines.append(f"\n--- [Cell {idx}] ({c_type}{exec_str}) ---")

                # Extract source text
                src_val = c.get("source", "")
                if isinstance(src_val, list):
                    src_text = "".join(src_val)
                else:
                    src_text = str(src_val)
                lines.append(src_text.rstrip())

                # Summarize outputs for code cells
                outputs = c.get("outputs", [])
                if outputs and isinstance(outputs, list):
                    lines.append(f"  ↳ Outputs ({len(outputs)} item{'s' if len(outputs) != 1 else ''}):")
                    for out_idx, out in enumerate(outputs[:3]):
                        out_type = out.get("output_type", "output")
                        if out_type == "stream":
                            text_out = "".join(out.get("text", []))[:200]
                            lines.append(f"    [{out.get('name', 'stdout')}]: {text_out.strip()}")
                        elif out_type in ("execute_result", "display_data"):
                            data_dict = out.get("data", {})
                            if "text/plain" in data_dict:
                                plain_txt = "".join(data_dict["text/plain"])[:200]
                                lines.append(f"    [result]: {plain_txt.strip()}")
                            elif "image/png" in data_dict:
                                lines.append("    [image/png data]")
                        elif out_type == "error":
                            ename = out.get("ename", "Error")
                            evalue = out.get("evalue", "")
                            lines.append(f"    [error]: {ename}: {evalue}")
                    if len(outputs) > 3:
                        lines.append(f"    ... (+{len(outputs) - 3} more outputs)")

            return "\n".join(lines)

        # ── 2. EDIT CELL ───────────────────────────────────────────────────
        elif action == "edit_cell":
            if cell_index is None:
                raise ToolError("Missing required parameter 'cell_index' for action 'edit_cell'.")
            if cell_index < 0 or cell_index >= len(cells):
                raise ToolError(f"Cell index {cell_index} is out of bounds (Notebook has {len(cells)} cells, valid indices: 0 to {len(cells)-1}).")
            if source is None:
                raise ToolError("Missing required parameter 'source' for action 'edit_cell'.")

            target_cell = cells[cell_index]
            if cell_type:
                target_cell["cell_type"] = cell_type

            # Format source as newline-terminated array of lines
            source_lines = [l + "\n" for l in source.splitlines()]
            if source_lines and not source.endswith("\n"):
                source_lines[-1] = source_lines[-1].rstrip("\n")
            target_cell["source"] = source_lines

            # Reset execution outputs since code changed
            if target_cell.get("cell_type") == "code":
                target_cell["outputs"] = []
                target_cell["execution_count"] = None

            self._save_notebook(target_path, nb_data)
            return f"✓ Successfully updated Cell #{cell_index} ({target_cell.get('cell_type')}) in {target_path.name}."

        # ── 3. INSERT CELL ─────────────────────────────────────────────────
        elif action == "insert_cell":
            if source is None:
                raise ToolError("Missing required parameter 'source' for action 'insert_cell'.")

            source_lines = [l + "\n" for l in source.splitlines()]
            if source_lines and not source.endswith("\n"):
                source_lines[-1] = source_lines[-1].rstrip("\n")

            new_cell: dict[str, Any] = {
                "cell_type": cell_type or "code",
                "metadata": {},
                "source": source_lines,
                "id": uuid.uuid4().hex[:8],
            }
            if new_cell["cell_type"] == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None

            if cell_index is None or cell_index >= len(cells):
                cells.append(new_cell)
                ins_idx = len(cells) - 1
            else:
                ins_idx = max(0, cell_index)
                cells.insert(ins_idx, new_cell)

            self._save_notebook(target_path, nb_data)
            return f"✓ Successfully inserted new Cell #{ins_idx} ({new_cell['cell_type']}) into {target_path.name} (Total cells: {len(cells)})."

        # ── 4. DELETE CELL ─────────────────────────────────────────────────
        elif action == "delete_cell":
            if cell_index is None:
                raise ToolError("Missing required parameter 'cell_index' for action 'delete_cell'.")
            if cell_index < 0 or cell_index >= len(cells):
                raise ToolError(f"Cell index {cell_index} is out of bounds (Notebook has {len(cells)} cells, valid indices: 0 to {len(cells)-1}).")

            deleted = cells.pop(cell_index)
            self._save_notebook(target_path, nb_data)
            return f"✓ Successfully deleted Cell #{cell_index} ({deleted.get('cell_type')}) from {target_path.name} (Remaining cells: {len(cells)})."

        # ── 5. CLEAR OUTPUTS ───────────────────────────────────────────────
        elif action == "clear_outputs":
            if cell_index is not None:
                if cell_index < 0 or cell_index >= len(cells):
                    raise ToolError(f"Cell index {cell_index} is out of bounds (Notebook has {len(cells)} cells).")
                c = cells[cell_index]
                if c.get("cell_type") == "code":
                    c["outputs"] = []
                    c["execution_count"] = None
                self._save_notebook(target_path, nb_data)
                return f"✓ Cleared outputs for Cell #{cell_index} in {target_path.name}."
            else:
                cleared_count = 0
                for c in cells:
                    if c.get("cell_type") == "code" and (c.get("outputs") or c.get("execution_count") is not None):
                        c["outputs"] = []
                        c["execution_count"] = None
                        cleared_count += 1
                self._save_notebook(target_path, nb_data)
                return f"✓ Cleared outputs across {cleared_count} code cells in {target_path.name}."

        else:
            raise ToolError(f"Unknown action: '{action}'. Valid actions: read_cells, edit_cell, insert_cell, delete_cell, clear_outputs.")

    def _save_notebook(self, path: Path, data: dict[str, Any]) -> None:
        """Atomically save formatted notebook JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_json = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        temp_path = path.with_suffix(".tmp_nb")
        try:
            temp_path.write_text(raw_json, encoding="utf-8")
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

    def render_call(self, args: dict[str, Any]) -> str:
        nb = Path(args.get("notebook_path", "")).name
        act = args.get("action", "")
        idx = args.get("cell_index")
        idx_str = f" cell:{idx}" if idx is not None else ""
        return f"notebook_edit {nb} action:{act}{idx_str}".strip()
