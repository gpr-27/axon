"""
Small pure helpers for how file changes look in the terminal: gap rows between hunks, notes (binary,
whitespace-only, no newline at end of file), the deleted / renamed rows of a changed-files summary, and the
compact diff the permission prompt shows before an edit is allowed.

Everything returns plain styled rows (no trailing newline) and uses only theme tokens, so it follows the
dark / light / NO_COLOR / ASCII choices of ``axon.ui.theme``.  ``axon.ui.cards`` draws the diff itself;
this module holds the parts around it (and imports cards lazily, so cards can import this module).
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any

from axon.ui.theme import MUTED, RST, SUBTLE, display_width, glyph, truncate

EOF_NOTE = "No newline at end of file"
CREATE_PREVIEW_ROWS = 12          # a new file's preview: its first lines, the rest behind a pointer
APPROVAL_DIFF_ROWS = 14           # changed lines the permission prompt shows


def _gutter(num_w: int) -> str:
    return " " * num_w + "  "


def gap_row(num_w: int, hidden: int | None = None) -> str:
    """'⋯ 12 unchanged lines' between two hunks (just '⋯' when the number is unknown)."""
    text = f"{glyph('gap')} {hidden} unchanged line{'' if hidden == 1 else 's'}" if hidden else glyph("gap")
    return f"{SUBTLE}{_gutter(num_w)}{text}{RST}"


def note_row(num_w: int, text: str) -> str:
    """A dim note under the gutter: '\\ No newline at end of file', 'whitespace-only change'."""
    return f"{SUBTLE}{_gutter(num_w)}{text}{RST}"


def is_binary(text: str | None) -> bool:
    return bool(text) and "\x00" in text[:8192]


def binary_row(num_w: int, size: int) -> str:
    from axon.ui.cards import fmt_bytes
    return note_row(num_w, f"binary file, {fmt_bytes(size)}")


def whitespace_only(old: str, new: str) -> bool:
    """True when the texts differ but not once all whitespace is ignored."""
    if old == new:
        return False
    return re.sub(r"\s+", "", old) == re.sub(r"\s+", "", new)


def eof_difference(old: str, new: str) -> str:
    """'old' / 'new' when only that side lacks the final newline of a non-empty pair, else ''."""
    if not old or not new:
        return ""
    if old.endswith("\n") == new.endswith("\n"):
        return ""
    return "old" if not old.endswith("\n") else "new"


def short_path(path: str, root: Path | None = None) -> str:
    """The path relative to the workspace (or home as '~'), forward slashes, for display."""
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute():
        for base, prefix in ((root or Path.cwd(), ""), (Path.home(), "~/")):
            try:
                return prefix + p.relative_to(base).as_posix()
            except (ValueError, OSError):
                continue
    return path.replace("\\", "/") if "\\" in path and "/" not in path else path


# ─── changed-files summary pieces ──────────────────────────────────────────

def group_deletions(changes: list[Any], min_total: int = 4) -> tuple[list[Any], list[tuple[str, list[Any]]]]:
    """(the changes that keep their own row, [(directory, deleted files)] to list as groups).

    Few deletions stay one row each; from *min_total* on, deletions that share a directory become one row."""
    deleted = [c for c in changes if c.kind == "deleted"]
    if len(deleted) < min_total:
        return list(changes), []
    by_dir: dict[str, list[Any]] = {}
    for c in deleted:
        by_dir.setdefault(posixpath.dirname(c.path), []).append(c)
    groups = [(d, cs) for d, cs in by_dir.items() if len(cs) > 1]
    grouped = {id(c) for _, cs in groups for c in cs}
    return [c for c in changes if id(c) not in grouped], groups


def deleted_facts(c: Any) -> str:
    """'−3 · 120 B' in red: what a deleted file held (lines and size, as far as known)."""
    from axon.ui.cards import count_badge, fmt_bytes
    parts = []
    badge = count_badge(None, c.removed) if c.removed is not None else ""
    if badge:
        parts.append(badge)
    if getattr(c, "old", None):
        parts.append(f"{MUTED}{fmt_bytes(len(c.old.encode('utf-8', errors='replace')))}{RST}")
    return f" {SUBTLE}{glyph('dot')}{RST} ".join(parts)


def rename_note(c: Any, path_w: int) -> str:
    """'old/path → new/path' for a rename, shortened to fit *path_w* columns per side."""
    arrow = f" {glyph('arrow')} "
    old, new = c.old_path or "?", c.path
    room = max(8, path_w)
    return f"{MUTED}{truncate(old, room)}{RST}{arrow}{truncate(new, room)}"


# ─── the permission prompt ─────────────────────────────────────────────────

_EDIT_NAMES = {"Edit", "EditFile", "replace_file_content"}
_WRITE_NAMES = {"Write", "WriteFile", "write_to_file"}


def _read_text(path: str, limit: int = 2_000_000) -> str | None:
    try:
        p = Path(path).expanduser()
        if not p.is_file() or p.stat().st_size > limit:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def approval_rows(tool_name: str, args: dict[str, Any], width: int, max_rows: int = APPROVAL_DIFF_ROWS,
                  indent: str = "    ") -> list[str]:
    """What an Edit / MultiEdit / Write will change, drawn as the diff the result card shows afterwards:
    a header row (action, path, +/−, language) and the compact diff. [] for any other tool."""
    from axon.ui import cards
    if not isinstance(args, dict):
        return []
    path = str(args.get("path") or args.get("file_path") or args.get("TargetFile") or "")
    if not path:
        return []
    width = max(24, width - len(indent))
    lang = cards.lang_info(path)[0]
    shown = short_path(path)
    if tool_name in _WRITE_NAMES:
        new = str(args.get("content") if args.get("content") is not None else args.get("CodeContent") or "")
        old = _read_text(path)
        if old is None:
            head = cards.code_card_header(shown, new, "Create")
            body = cards.diff_rows("", new, 1, width, lang, max_rows=CREATE_PREVIEW_ROWS)
            return [indent + (truncate(r, width) if display_width(r) > width else r) for r in [head] + body]
        pairs = [(old, new)]
        verb, eof = "Overwrite", True
    elif tool_name in _EDIT_NAMES or tool_name == "MultiEdit":
        edits = args.get("edits") if tool_name == "MultiEdit" else [args]
        pairs = [(str(e.get("old_string") or ""), str(e.get("new_string") or ""))
                 for e in (edits or []) if isinstance(e, dict)]
        verb, eof = "Edit", False
        text = _read_text(path) or ""
    else:
        return []
    if not pairs:
        return []
    added = removed = 0
    body: list[str] = []
    for k, (a, b) in enumerate(pairs):
        ad, rm = cards.diff_counts(a, b)
        added, removed = added + ad, removed + rm
        if len(body) >= max_rows:
            continue
        if k:
            body.append(gap_row(3))
        start = 0
        if verb == "Edit" and a and text and text.find(a) >= 0:
            start = text.count("\n", 0, text.find(a)) + 1
        elif verb == "Overwrite":
            start = 1
        body += cards.diff_rows(a, b, start, width, lang, max_rows=max(4, max_rows - len(body)), eof_notes=eof)
    if not added and not removed and not any(a != b for a, b in pairs):
        return []
    head = cards.diff_header(shown, added, removed, verb, f"{len(pairs)} edits" if len(pairs) > 1 else "")
    lang_chip = cards.lang_badge(path)
    if lang_chip:
        head += f" {SUBTLE}{glyph('dot')}{RST} {lang_chip}"
    rows = [head] + body
    return [indent + (truncate(r, width) if display_width(r) > width else r) for r in rows]


__all__ = ["APPROVAL_DIFF_ROWS", "CREATE_PREVIEW_ROWS", "EOF_NOTE", "approval_rows", "binary_row", "deleted_facts",
           "eof_difference", "gap_row", "group_deletions", "is_binary", "note_row", "rename_note", "short_path",
           "whitespace_only"]
