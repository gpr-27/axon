"""
Patch: apply unified diffs (multi-hunk, multi-file, create/delete) atomically.
Hunks are located by their @@ line numbers, then by searching nearby/elsewhere
for the exact context (tolerating trailing-whitespace differences). Either every
hunk of every file applies, or nothing is written.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import Tool, ToolContext, capture_checkpoint, default_summary, display_path, plural, resolve_path
from axon.tools.textio import atomic_write_text, normalize_newlines, read_text_file

_HUNK_RE = re.compile(r"^@@+\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@+")


@dataclass
class Hunk:
    old_start: int | None          # 1-based; None when the header had no numbers
    lines: list[tuple[str, str]] = field(default_factory=list)  # (op, text) op in " ", "-", "+"
    no_eol_old: bool = False
    no_eol_new: bool = False

    @property
    def old(self) -> list[str]:
        return [t for op, t in self.lines if op in (" ", "-")]

    @property
    def new(self) -> list[str]:
        return [t for op, t in self.lines if op in (" ", "+")]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)


def _strip_prefix(p: str) -> str | None:
    p = p.strip().split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    lines = normalize_newlines(text).split("\n")
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FilePatch(_strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:]))
            files.append(cur)
            hunk = None
            i += 2
            continue
        if line.startswith(("diff --git", "index ", "new file mode", "deleted file mode", "similarity index",
                            "rename from", "rename to", "old mode", "new mode")):
            i += 1
            continue
        if line.startswith("@@"):
            if cur is None:
                cur = FilePatch(None, None)
                files.append(cur)
            m = _HUNK_RE.match(line)
            hunk = Hunk(int(m.group(1)) if m else None)
            cur.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None:
            if line.startswith("\\"):
                # "\ No newline at end of file" applies to the preceding line.
                if hunk.lines:
                    op = hunk.lines[-1][0]
                    if op in (" ", "-"):
                        hunk.no_eol_old = True
                    if op in (" ", "+"):
                        hunk.no_eol_new = True
            elif line.startswith(("+", "-", " ")):
                hunk.lines.append((line[0], line[1:]))
            elif line == "":
                # Blank context line whose leading space was stripped by an editor/model.
                if i == len(lines) - 1:
                    break
                hunk.lines.append((" ", ""))
            else:
                hunk = None  # garbage between hunks
        i += 1
    for fp in files:
        for h in fp.hunks:
            while h.lines and h.lines[-1] == (" ", ""):
                h.lines.pop()  # trailing blank lines produced by text splitting
    return [f for f in files if f.hunks or (f.old_path is None) != (f.new_path is None)]


def _find(hay: list[str], needle: list[str], expected: int, used_until: int) -> int | None:
    """Locate `needle` in `hay` at or after `used_until`, preferring positions closest to
    `expected`; exact match first, then ignoring trailing whitespace."""
    if not needle:
        return max(min(expected, len(hay)), used_until)
    n = len(needle)
    last = len(hay) - n
    if last < used_until:
        return None
    expected = min(max(expected, used_until), last)
    order = sorted(range(used_until, last + 1), key=lambda pos: abs(pos - expected))
    for pos in order:
        if hay[pos : pos + n] == needle:
            return pos
    stripped = [t.rstrip() for t in needle]
    for pos in order:
        if [s.rstrip() for s in hay[pos : pos + n]] == stripped:
            return pos
    return None


def apply_hunks(text: str, hunks: list[Hunk], label: str) -> str:
    final_nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    offset = 0
    used_until = 0
    for idx, h in enumerate(hunks, 1):
        old, new = h.old, h.new
        if h.old_start is None:
            expected = used_until
        else:
            expected = max(0, h.old_start - 1 + offset) if h.old_start > 0 else 0
        pos = _find(lines, old, expected, used_until)
        if pos is None:
            preview = "\n".join(("-" if op == "-" else " ") + t for op, t in h.lines if op != "+")[:400]
            raise ToolError(
                f"Patch hunk #{idx} for {label} does not apply: its context/removed lines were not found"
                f"{f' near line {h.old_start}' if h.old_start else ''}. Re-read the file and regenerate the patch "
                f"(or use Edit). Expected:\n{preview}"
            )
        lines[pos : pos + len(old)] = new
        offset += len(new) - len(old)
        used_until = pos + len(new)
        if used_until >= len(lines) and (h.no_eol_new or h.no_eol_old):
            final_nl = not h.no_eol_new
    out = "\n".join(lines)
    if lines and final_nl:
        out += "\n"
    return out


class PatchTool(Tool):
    name: ClassVar[str] = "Patch"
    description: ClassVar[str] = (
        "Apply a unified diff (as produced by `git diff` / `diff -u`) to one or more files atomically. Supports "
        "multiple hunks and files, new files (--- /dev/null) and deletions (+++ /dev/null). Hunks are anchored by "
        "their @@ line numbers and context lines, which must match the current file (Read it first). Use this for "
        "large multi-location changes; for small replacements Edit/MultiEdit are more reliable. `path` is only "
        "needed when the patch has no ---/+++ headers."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "Unified diff text"},
            "path": {"type": "string", "description": "Target file when the diff has no ---/+++ headers (or to override a single-file patch)"},
        },
        "required": ["patch"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = False
    default_permission: ClassVar[str] = "ask"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        patch_text = args.get("patch") or args.get("diff") or ""
        raw_path = str(args.get("path") or "").strip()
        if not str(patch_text).strip():
            raise ToolError("Patch requires a non-empty 'patch' argument.")
        files = parse_patch(str(patch_text))
        if not files:
            raise ToolError("No hunks found in patch. Expected unified diff format with '@@ -a,b +c,d @@' headers.")
        if raw_path:
            if len(files) > 1:
                raise ToolError("'path' can only be used with a single-file patch; this patch touches several files.")
            fp = files[0]
            if fp.new_path is None and fp.old_path is not None:
                fp.old_path = raw_path
            else:
                fp.new_path = raw_path
                if fp.old_path is not None:
                    fp.old_path = raw_path

        # Phase 1: compute every result in memory.
        plans: list[tuple[str, Path, str | None, str, str]] = []  # (kind, path, new_text, encoding, newline)
        for fp in files:
            target_raw = fp.new_path or fp.old_path
            if not target_raw:
                raise ToolError("Patch has hunks without a file header; pass 'path'.")
            p = resolve_path(ctx, target_raw)
            label = display_path(p, ctx.workspace)
            if fp.new_path is None:  # deletion
                if not p.exists():
                    raise ToolError(f"Cannot delete {label}: file does not exist.")
                ctx.file_state.check_writable(p)
                plans.append(("delete", p, None, "utf-8", "\n"))
                continue
            if fp.old_path is None and not p.exists():  # creation
                body = "\n".join(t for h in fp.hunks for op, t in h.lines if op == "+")
                if fp.hunks and not fp.hunks[-1].no_eol_new:
                    body += "\n"
                plans.append(("create", p, body, "utf-8", "\n"))
                continue
            if not p.exists():
                raise ToolError(f"Cannot patch non-existent file: {label}")
            ctx.file_state.check_writable(p)
            tf = read_text_file(p)
            text = tf.text if tf.newline else normalize_newlines(tf.text)
            new_text = apply_hunks(text, fp.hunks, label)
            src_path = resolve_path(ctx, fp.old_path) if fp.old_path and fp.old_path != fp.new_path else None
            plans.append(("modify", p, new_text, tf.encoding, tf.newline or "\n"))
            if src_path is not None and src_path != p and src_path.exists():
                ctx.file_state.check_writable(src_path)
                plans.append(("delete", src_path, None, "utf-8", "\n"))  # rename

        # Phase 2: write.
        done: list[str] = []
        for kind, p, new_text, enc, nl in plans:
            capture_checkpoint(ctx, p)
            label = display_path(p, ctx.workspace)
            if kind == "delete":
                p.unlink()
                ctx.file_state.invalidate(p)
                done.append(f"deleted {label}")
            else:
                atomic_write_text(p, new_text or "", encoding=enc, newline=nl)
                ctx.file_state.record_read(p)
                done.append(f"{'created' if kind == 'create' else 'patched'} {label}")
        n_hunks = sum(len(f.hunks) for f in files)
        return f"Successfully applied patch ({n_hunks} hunk{'s' if n_hunks != 1 else ''}): " + ", ".join(done) + "."

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        m = re.search(r"\((\d+) hunks?\): (.*)\.$", output.strip())
        if not m:
            return default_summary(output, False)
        n_files = m.group(2).count(",") + 1
        return f"{plural(int(m.group(1)), 'hunk')} · {plural(n_files, 'file')}"
