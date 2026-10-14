"""
NotebookEdit: cell-level Jupyter (.ipynb) editing without corrupting notebook JSON.
Reading notebooks is done by the Read tool (it renders cells and outputs via
render_notebook below).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, capture_checkpoint, default_summary, display_path, resolve_path
from axon.tools.textio import atomic_write_text, decode_bytes

_LEGACY_ACTIONS = {
    "edit_cell": "replace", "insert_cell": "insert", "delete_cell": "delete",
    "clear_outputs": "clear_outputs", "read_cells": "read",
}


def load_notebook(p: Path) -> dict[str, Any]:
    try:
        text, _ = decode_bytes(p.read_bytes())
        nb = json.loads(text)
    except (OSError, ValueError) as e:
        raise ToolError(f"Failed to parse notebook JSON at {p.name}: {e}") from e
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ToolError(f"Invalid notebook format: 'cells' array is missing in {p.name}.")
    return nb


def _text(val: Any) -> str:
    return "".join(val) if isinstance(val, list) else str(val or "")


def render_notebook(nb: dict[str, Any], title: str, max_output_chars: int = 400) -> str:
    cells = nb.get("cells") or []
    if not cells:
        return f"=== Notebook: {title} (0 cells) ===\n[Empty Notebook]"
    lang = ((nb.get("metadata") or {}).get("language_info") or {}).get("name", "")
    head = f"=== Notebook: {title} ({len(cells)} cells) ===" + (f" [{lang}]" if lang else "")
    lines = [head]
    for idx, c in enumerate(cells):
        c_type = c.get("cell_type", "code")
        ec = c.get("execution_count")
        ec_s = f" [Execution Count: {ec}]" if ec is not None else ""
        cid = f" id={c['id']}" if c.get("id") else ""
        lines.append(f"\n--- [Cell {idx}] ({c_type}{ec_s}){cid} ---")
        lines.append(_text(c.get("source")).rstrip())
        outputs = c.get("outputs") or []
        if outputs and isinstance(outputs, list):
            lines.append(f"  ↳ Outputs ({len(outputs)} item{'s' if len(outputs) != 1 else ''}):")
            for out in outputs[:5]:
                o_type = out.get("output_type", "output")
                if o_type == "stream":
                    lines.append(f"    [{out.get('name', 'stdout')}]: {_text(out.get('text'))[:max_output_chars].strip()}")
                elif o_type in ("execute_result", "display_data"):
                    data = out.get("data") or {}
                    if "text/plain" in data:
                        lines.append(f"    [result]: {_text(data['text/plain'])[:max_output_chars].strip()}")
                    imgs = [k for k in data if k.startswith("image/")]
                    if imgs:
                        lines.append(f"    [{', '.join(imgs)} data omitted]")
                elif o_type == "error":
                    lines.append(f"    [error]: {out.get('ename', 'Error')}: {out.get('evalue', '')}")
            if len(outputs) > 5:
                lines.append(f"    ... (+{len(outputs) - 5} more outputs)")
    return "\n".join(lines)


def _source_lines(source: str) -> list[str]:
    parts = source.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _index(val: Any, n_cells: int, *, allow_end: bool = False) -> int:
    try:
        idx = int(val)
    except (TypeError, ValueError) as e:
        raise ToolError(f"cell_index must be an integer, got {val!r}.") from e
    hi = n_cells if allow_end else n_cells - 1
    if idx < 0 or idx > hi:
        raise ToolError(
            f"Cell index {idx} is out of bounds (Notebook has {n_cells} cells, valid indices: 0 to {max(hi, 0)})."
        )
    return idx


class NotebookEditTool(Tool):
    name: ClassVar[str] = "NotebookEdit"
    description: ClassVar[str] = (
        "Edit a Jupyter notebook (.ipynb) by cell. Read the notebook first (Read shows cell indices and ids). "
        "edit_mode: \"replace\" (default; set new_source of the cell), \"insert\" (new cell at cell_index, or "
        "appended when omitted; creates the notebook if missing), \"delete\", or \"clear_outputs\" (one cell or "
        "all). Cells can be addressed by 0-based cell_index or by cell_id. Replacing a code cell clears its outputs."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string", "description": "Path to the .ipynb file"},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete", "clear_outputs"],
                          "description": "Operation (default replace)"},
            "cell_index": {"type": "integer", "minimum": 0, "description": "0-based target cell index"},
            "cell_id": {"type": "string", "description": "Target cell id (alternative to cell_index)"},
            "new_source": {"type": "string", "description": "New cell source for replace/insert"},
            "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"],
                          "description": "Cell type for insert (default code) or to change type on replace"},
        },
        "required": ["notebook_path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = str(args.get("notebook_path") or args.get("path") or "").strip()
        if not raw_path:
            raise ToolError("Missing required parameter: 'notebook_path'.")
        p = resolve_path(ctx, raw_path)
        if p.suffix.lower() != ".ipynb":
            raise ToolError(f"{raw_path} is not a .ipynb notebook. Use Edit for regular files.")

        mode = str(args.get("edit_mode") or "").strip().lower()
        legacy = str(args.get("action") or "").strip().lower()
        if not mode:
            mode = _LEGACY_ACTIONS.get(legacy, "replace" if not legacy else legacy)
        source = args.get("new_source", args.get("source"))
        cell_type = args.get("cell_type")
        if cell_type is not None and cell_type not in ("code", "markdown", "raw"):
            raise ToolError("cell_type must be 'code', 'markdown' or 'raw'.")
        rel = display_path(p, ctx.workspace)

        if mode == "read":
            if not p.exists():
                raise ToolError(f"Notebook file not found: {raw_path}")
            nb = load_notebook(p)
            ctx.file_state.record_read(p)
            return render_notebook(nb, p.name)
        if mode not in ("replace", "insert", "delete", "clear_outputs"):
            raise ToolError(f"Unknown edit_mode '{mode}'. Use replace, insert, delete or clear_outputs.")

        if p.exists():
            ctx.file_state.check_writable(p)
            nb = load_notebook(p)
        elif mode == "insert":
            nb = {
                "cells": [],
                "metadata": {
                    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                    "language_info": {"name": "python"},
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        else:
            raise ToolError(f"Notebook file not found: {raw_path}")

        cells: list[dict[str, Any]] = nb["cells"]
        idx: int | None = None
        if args.get("cell_id"):
            cid = str(args["cell_id"])
            idx = next((i for i, c in enumerate(cells) if str(c.get("id")) == cid), None)
            if idx is None:
                raise ToolError(f"No cell with id '{cid}' in {rel}.")
        elif args.get("cell_index") is not None:
            idx = _index(args["cell_index"], len(cells), allow_end=(mode == "insert"))

        if mode == "replace":
            if idx is None:
                raise ToolError("Missing required parameter 'cell_index' (or cell_id) for replace.")
            if source is None:
                raise ToolError("Missing required parameter 'new_source' for replace.")
            cell = cells[idx]
            if cell_type:
                cell["cell_type"] = cell_type
            cell["source"] = _source_lines(str(source))
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
            else:
                cell.pop("outputs", None)
                cell.pop("execution_count", None)
            msg = f"✓ Successfully updated Cell #{idx} ({cell.get('cell_type')}) in {p.name}."
        elif mode == "insert":
            if source is None:
                raise ToolError("Missing required parameter 'new_source' for insert.")
            ctype = cell_type or "code"
            new_cell: dict[str, Any] = {"cell_type": ctype, "metadata": {}, "source": _source_lines(str(source))}
            if nb.get("nbformat", 4) >= 4 and nb.get("nbformat_minor", 0) >= 5:
                new_cell["id"] = uuid.uuid4().hex[:8]
            if ctype == "code":
                new_cell["outputs"] = []
                new_cell["execution_count"] = None
            if idx is None:
                cells.append(new_cell)
                idx = len(cells) - 1
            else:
                cells.insert(idx, new_cell)
            msg = f"✓ Successfully inserted new Cell #{idx} ({ctype}) into {p.name} (Total cells: {len(cells)})."
        elif mode == "delete":
            if idx is None:
                raise ToolError("Missing required parameter 'cell_index' (or cell_id) for delete.")
            removed = cells.pop(idx)
            msg = f"✓ Successfully deleted Cell #{idx} ({removed.get('cell_type')}) from {p.name} (Remaining cells: {len(cells)})."
        else:  # clear_outputs
            targets = [cells[idx]] if idx is not None else cells
            cleared = 0
            for c in targets:
                if c.get("cell_type") == "code" and (c.get("outputs") or c.get("execution_count") is not None):
                    cleared += 1
                if c.get("cell_type") == "code":
                    c["outputs"] = []
                    c["execution_count"] = None
            msg = (f"✓ Cleared outputs for Cell #{idx} in {p.name}." if idx is not None
                   else f"✓ Cleared outputs across {cleared} code cells in {p.name}.")

        capture_checkpoint(ctx, p)
        atomic_write_text(p, json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        ctx.file_state.record_read(p)
        return msg

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        return default_summary(output.lstrip("✓ ").replace("Successfully ", ""), False)

    def render_call(self, args: dict[str, Any]) -> str:
        nb = Path(str(args.get("notebook_path", ""))).name
        act = args.get("edit_mode") or args.get("action") or "replace"
        idx = args.get("cell_index", args.get("cell_id"))
        return f"NotebookEdit {nb} {act}{f' cell:{idx}' if idx is not None else ''}".strip()
