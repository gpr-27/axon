"""
Write, Edit and MultiEdit: atomic, encoding- and line-ending-preserving file changes
guarded by the read-before-edit invariant and checkpointed for /rewind.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from axon.agent.filechanges import diff_counts
from axon.errors import ToolError
from axon.tools.base import (
    Tool, ToolContext, bool_arg, capture_checkpoint, default_summary, display_path, human_size, plural, resolve_path,
)
from axon.tools.textio import (
    TextFile,
    atomic_write_text,
    normalize_newlines,
    read_text_file,
)


def _atomic_write(target: Path, content: str) -> None:
    """Back-compat helper: atomic UTF-8 write of `content` exactly as given (no newline translation)."""
    atomic_write_text(target, content, encoding="utf-8", newline="")


def _load_for_edit(p: Path, raw_path: str, ctx: ToolContext) -> TextFile:
    if not p.exists():
        raise ToolError(f"Cannot edit non-existent file {raw_path}. Use Write to create new files.")
    if p.is_dir():
        raise ToolError(f"{raw_path} is a directory, not a file.")
    ctx.file_state.check_writable(p)
    return read_text_file(p)


def _prepare(tf: TextFile, s: str) -> str:
    """Bring a model-supplied snippet into the same newline form as tf.text."""
    return normalize_newlines(s) if tf.newline in ("\n", "\r\n") else s


def _not_found_hint(text: str, old: str) -> str:
    """Explain why an exact match failed: whitespace/indent differences or line-number prefixes."""
    first = next((ln for ln in old.split("\n") if ln.strip()), "").strip()
    if first and "→" in first[:10] and first.split("→", 1)[0].strip().isdigit():
        return " old_string seems to include Read's line-number prefix (\"   12→\"); remove it."
    if not first:
        return ""
    lines = text.split("\n")
    for idx, ln in enumerate(lines, 1):
        if ln.strip() == first:
            return (
                f" A line matching the first line of old_string ignoring whitespace exists at line {idx}: "
                f"{ln.strip()[:80]!r}. Check indentation/tabs and trailing spaces."
            )
    collapsed_old = " ".join(old.split())
    if collapsed_old and collapsed_old in " ".join(text.split()):
        return " The text exists with different whitespace/line breaks. Copy it exactly from a fresh Read."
    return " Re-read the file to get the exact current text."


def _match_lines(text: str, needle: str) -> list[int]:
    lines, pos, start = [], 0, 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return lines
        lines.append(text.count("\n", 0, i) + 1)
        start = i + max(1, len(needle))
        pos += 1
        if pos >= 10:
            return lines


def _apply_edit(text: str, old: str, new: str, replace_all: bool, label: str, rel: str,
                allow_noop: bool = False) -> tuple[str, int]:
    if old == "":
        raise ToolError(f"{label}: 'old_string' must not be empty. Use Write to create or overwrite files.")
    if old == new and not allow_noop:
        raise ToolError(f"{label}: 'old_string' and 'new_string' are identical; nothing to change.")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"{label}: 'old_string' was not found in {rel}.{_not_found_hint(text, old)}")
    if count > 1 and not replace_all:
        at = ", ".join(str(n) for n in _match_lines(text, old))
        raise ToolError(
            f"{label}: 'old_string' appears {count} times in {rel} (lines {at}). "
            f"Include more surrounding context to make it unique, or set replace_all=true."
        )
    return (text.replace(old, new) if replace_all else text.replace(old, new, 1)), count


def _summarize_change(before: str, after: str) -> str:
    b, a = before.count("\n"), after.count("\n")
    delta = a - b
    return f"{'+' if delta >= 0 else ''}{delta} lines"


class WriteTool(Tool):
    name: ClassVar[str] = "Write"
    description: ClassVar[str] = (
        "Create a new file or completely overwrite an existing one (parent directories are created). "
        "Overwriting requires that you Read the file first in this session. Prefer Edit/MultiEdit for changes to "
        "existing files — they are cheaper and safer. Existing files keep their encoding and CRLF/LF line endings."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative path of the file to write"},
            "content": {"type": "string", "description": "The complete file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path") or args.get("file_path") or ""
        content = args.get("content")
        if not str(raw_path).strip():
            raise ToolError("Write requires 'path'.")
        if content is None:
            raise ToolError("Write requires 'content' (use an empty string for an empty file).")
        if not isinstance(content, str):
            content = str(content)

        p = resolve_path(ctx, raw_path)
        if p.is_dir():
            raise ToolError(f"{raw_path} is a directory, not a file.")
        rel = display_path(p, ctx.workspace)

        existed = p.exists()
        encoding, newline = "utf-8", "\n"
        old_text: str | None = None
        if existed:
            ctx.file_state.check_writable(p)
            try:
                tf = read_text_file(p)
                encoding, newline, old_text = tf.encoding, tf.newline or "\n", tf.text
            except ToolError:
                pass  # binary/oversized: overwrite as UTF-8
        capture_checkpoint(ctx, p)

        # Content keeps the model's newlines unless the file was uniformly CRLF.
        text = normalize_newlines(content) if newline == "\r\n" else content
        atomic_write_text(p, text, encoding=encoding, newline=newline if newline == "\r\n" else "")
        ctx.file_state.record_read(p)
        n_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if not existed:
            change = f"Created file (+{n_lines})"
        elif old_text is not None:
            added, removed = diff_counts(normalize_newlines(old_text), normalize_newlines(content))
            change = f"Overwrote file (+{added} −{removed})"
        else:
            change = "Overwrote file"
        return f"Successfully wrote {len(content):,} characters ({n_lines} lines) to {rel}. {change}."


    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        content = str(args.get("content") or "")
        n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        verb = "overwrote" if "Overwrote" in output else "created"
        return f"{verb} · {plural(n, 'line')} · {human_size(len(content.encode('utf-8')))}"


def _edit_summary(output: str, is_error: bool) -> str:
    if is_error:
        return default_summary(output, True)
    m = re.search(r"\((.*)\)\.?$", output.strip())
    return m.group(1) if m else default_summary(output, False)


class EditTool(Tool):
    name: ClassVar[str] = "Edit"
    description: ClassVar[str] = (
        "Replace an exact string in a file you have Read this session. old_string must match the file exactly "
        "(including indentation; never include Read's line-number prefix) and must be unique unless "
        "replace_all=true — add surrounding lines to disambiguate. Use replace_all for renaming across the file. "
        "Encoding and CRLF/LF line endings are preserved. For several changes to one file use MultiEdit."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text (must differ from old_string)"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path") or args.get("file_path") or ""
        old_str = args.get("old_string")
        new_str = args.get("new_string")
        if not str(raw_path).strip() or not old_str:
            raise ToolError("Edit requires 'path' and a non-empty 'old_string'.")
        if new_str is None:
            raise ToolError("Edit requires 'new_string' (use an empty string to delete text).")
        replace_all = bool_arg(args, "replace_all", False)

        p = resolve_path(ctx, raw_path)
        tf = _load_for_edit(p, raw_path, ctx)
        rel = display_path(p, ctx.workspace)

        new_text, count = _apply_edit(
            tf.text, _prepare(tf, str(old_str)), _prepare(tf, str(new_str)), replace_all, "Edit failed", rel
        )
        capture_checkpoint(ctx, p)
        atomic_write_text(p, new_text, encoding=tf.encoding, newline=tf.newline)
        ctx.file_state.record_read(p)
        return (
            f"Successfully applied edit to {rel} ({count} occurrence{'s' if count != 1 else ''} replaced, "
            f"{_summarize_change(tf.text, new_text)})."
        )


    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        return _edit_summary(output, is_error)


class MultiEditTool(Tool):
    name: ClassVar[str] = "MultiEdit"
    description: ClassVar[str] = (
        "Apply several exact-string edits to ONE file atomically: edits run in order, each on the result of the "
        "previous one, and if any edit fails nothing is written. Same matching rules as Edit. Make sure earlier "
        "edits don't change text that later edits need to find."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "edits": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Exact text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
                    },
                    "required": ["old_string", "new_string"],
                    "additionalProperties": False,
                },
                "description": "Edits applied sequentially",
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path") or args.get("file_path") or ""
        edits = args.get("edits") or []
        if not str(raw_path).strip() or not edits:
            raise ToolError("MultiEdit requires 'path' and non-empty 'edits' list.")
        if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
            raise ToolError("MultiEdit 'edits' must be a list of {old_string, new_string, replace_all?} objects.")

        p = resolve_path(ctx, raw_path)
        tf = _load_for_edit(p, raw_path, ctx)
        rel = display_path(p, ctx.workspace)

        text = tf.text
        total = 0
        for idx, ed in enumerate(edits, 1):
            old_s, new_s = ed.get("old_string"), ed.get("new_string")
            if not old_s:
                raise ToolError(f"Edit #{idx} missing 'old_string'. No edits were applied.")
            if new_s is None:
                raise ToolError(f"Edit #{idx} missing 'new_string'. No edits were applied.")
            try:
                text, cnt = _apply_edit(
                    text, _prepare(tf, str(old_s)), _prepare(tf, str(new_s)),
                    bool_arg(ed, "replace_all", False), f"Edit #{idx} failed", rel, allow_noop=True,
                )
            except ToolError as e:
                raise ToolError(f"{e} No edits were applied (MultiEdit is all-or-nothing).") from e
            total += cnt

        capture_checkpoint(ctx, p)
        atomic_write_text(p, text, encoding=tf.encoding, newline=tf.newline)
        ctx.file_state.record_read(p)
        return (
            f"Successfully applied {len(edits)} edits to {rel} ({total} replacement{'s' if total != 1 else ''}, "
            f"{_summarize_change(tf.text, text)})."
        )

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        return f"{plural(len(args.get('edits') or []), 'edit')} · {_edit_summary(output, False)}"
