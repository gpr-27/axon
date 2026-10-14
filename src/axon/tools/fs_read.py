"""
Read tool: numbered file contents with paging, binary/image/PDF/notebook handling,
and FileState registration for the read-before-edit invariant.
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path
from typing import Any, ClassVar

from axon.errors import ToolError
from axon.tools.base import (
    Tool, ToolContext, cap_text, default_summary, display_path, human_size, int_arg, output_cap, plural, resolve_path,
)
from axon.tools.textio import IMAGE_EXTENSIONS, decode_bytes, is_binary_bytes, MAX_TEXT_BYTES

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000


def _image_notice(p: Path, data: bytes, rel: str) -> str:
    dims = ""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            dims = f", {w}x{h}"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            dims = f", {int.from_bytes(data[6:8], 'little')}x{int.from_bytes(data[8:10], 'little')}"
    except Exception:
        dims = ""
    return (
        f"[Image file: {rel} ({len(data):,} bytes{dims}). Image content cannot be shown as text. "
        f"To let the model see it, the user can reference the image path in their prompt.]"
    )


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PDF_TEXT_OP = re.compile(rb"\((?:\\.|[^\\)])*\)\s*Tj|\[(?:[^\]]*)\]\s*TJ|T\*|Td|TD|ET", re.DOTALL)
_PDF_STR = re.compile(rb"\(((?:\\.|[^\\)])*)\)", re.DOTALL)


def _pdf_unescape(s: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):  # backslash
            n = s[i + 1]
            mapping = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
                       ord("("): 40, ord(")"): 41, ord("\\"): 92}
            if n in mapping:
                out.append(mapping[n]); i += 2; continue
            if 0x30 <= n <= 0x37:
                j = i + 1
                while j < len(s) and j < i + 4 and 0x30 <= s[j] <= 0x37:
                    j += 1
                out.append(int(s[i + 1 : j], 8) & 0xFF); i = j; continue
            i += 1; continue
        out.append(c); i += 1
    return out.decode("latin-1")


def extract_pdf_text(data: bytes, max_chars: int = 200_000) -> tuple[str, int]:
    """Best-effort text extraction from PDF content streams (Flate or raw).
    Returns (text, page_count). Works for PDFs with simple (non-CID) fonts."""
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    chunks: list[str] = []
    total = 0
    for m in _PDF_STREAM.finditer(data):
        raw = m.group(1)
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            content = raw
        if b"Tj" not in content and b"TJ" not in content:
            continue
        line: list[str] = []
        for op in _PDF_TEXT_OP.finditer(content):
            tok = op.group(0)
            if tok.endswith(b"Tj") or tok.endswith(b"TJ"):
                for s in _PDF_STR.finditer(tok):
                    line.append(_pdf_unescape(s.group(1)))
            else:
                if line:
                    chunks.append("".join(line))
                    total += len(chunks[-1])
                    line = []
        if line:
            chunks.append("".join(line))
        if total > max_chars:
            break
    text = "\n".join(c.strip() for c in chunks if c.strip())
    return text, pages


def _looks_readable(text: str) -> bool:
    if not text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return printable / len(text) > 0.85 and sum(ch.isalpha() for ch in text) / len(text) > 0.3


def split_lines(text: str) -> list[str]:
    """Split on \n only (str.splitlines also splits on \x0c, \x1c, U+2028...,
    which would desynchronise line numbers from editors and Grep)."""
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _number_lines(lines: list[str], start: int) -> str:
    out = []
    for i, line in enumerate(lines, start):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f" ...[line truncated, {len(line) - MAX_LINE_CHARS} more chars]"
        out.append(f"{i:6d}→{line}")
    return "\n".join(out)


class ReadTool(Tool):
    name: ClassVar[str] = "Read"
    description: ClassVar[str] = (
        "Read a file. Output is numbered like `cat -n` (\"     12→text\"); the number prefix is not part of the file, "
        "so never include it in Edit old_string. Reads up to 2000 lines from `offset` by default — use offset/limit "
        "to page through large files. Lines over 2000 chars are truncated. Handles .ipynb notebooks (cells + outputs), "
        "PDFs (text extraction), images (metadata notice) and detects binary files. You must Read a file before "
        "you Edit/Write it; reading many files in parallel in one turn is encouraged."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative file path"},
            "offset": {"type": "integer", "minimum": 1, "description": "1-based line to start reading from (default 1)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000, "description": f"Maximum number of lines to read (default {DEFAULT_LIMIT})"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    readonly: ClassVar[bool] = True
    default_permission: ClassVar[str] = "allow"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        raw_path = args.get("path") or args.get("file_path") or ""
        if not str(raw_path).strip():
            raise ToolError("Read requires 'path' argument.")

        p = resolve_path(ctx, raw_path, allow_git_read=True)
        if not p.exists():
            hint = self._suggest(p, ctx)
            raise ToolError(f"File not found: {raw_path}{hint}")
        if p.is_dir():
            raise ToolError(f"Path is a directory, not a file: {raw_path}. Use Ls or Glob to list it.")

        rel = display_path(p, ctx.workspace)
        offset = int_arg(args, "offset", 1, lo=1)
        limit = int_arg(args, "limit", DEFAULT_LIMIT, lo=1, hi=20000)
        cap = output_cap(ctx, 100_000)

        try:
            size = p.stat().st_size
        except OSError as e:
            raise ToolError(f"Cannot access {raw_path}: {e}") from e
        if size == 0:
            ctx.file_state.record_read(p)
            return f"[{rel} exists but is empty (0 bytes).]"

        suffix = p.suffix.lower()
        try:
            with open(p, "rb") as f:
                data = f.read(MAX_TEXT_BYTES + 1)
        except OSError as e:
            raise ToolError(f"Failed to read file {raw_path}: {e}") from e

        if suffix in IMAGE_EXTENSIONS:
            ctx.file_state.record_read(p)
            return _image_notice(p, data, rel)

        if suffix == ".pdf" or data[:5] == b"%PDF-":
            ctx.file_state.record_read(p)
            text, pages = extract_pdf_text(data)
            if _looks_readable(text):
                lines = text.splitlines()
                body = _number_lines(lines[offset - 1 : offset - 1 + limit], offset)
                return cap_text(f"[PDF: {rel}, ~{pages} page(s), extracted text]\n{body}", cap)
            return (
                f"[PDF: {rel} ({size:,} bytes, ~{pages} page(s)). Text could not be extracted (compressed fonts or "
                f"scanned images). If poppler is installed, try Bash: pdftotext \"{rel}\" -]"
            )

        if suffix == ".ipynb":
            try:
                from axon.tools.notebook import load_notebook, render_notebook
                nb = load_notebook(p)
                ctx.file_state.record_read(p)
                return cap_text(render_notebook(nb, p.name), cap, "Use NotebookEdit to modify cells.")
            except ToolError:
                pass  # malformed notebook JSON: fall through and show raw text

        if len(data) > MAX_TEXT_BYTES:
            raise ToolError(
                f"{rel} is larger than {MAX_TEXT_BYTES // 1_048_576} MB. Use Grep to find the relevant region, "
                f"or Bash (head/tail/sed -n) to extract a range."
            )
        if is_binary_bytes(data):
            ctx.file_state.record_read(p)
            return f"[Binary file: {rel} ({size:,} bytes). Not displayed as text.]"

        text, encoding = decode_bytes(data)
        ctx.file_state.record_read(p)

        lines = split_lines(text)
        total = len(lines)
        if offset > total:
            return f"[{rel} has {total} lines; offset {offset} is beyond the end of the file.]"
        end = min(total, offset - 1 + limit)
        result = _number_lines(lines[offset - 1 : end], offset)
        notes = []
        if encoding not in ("utf-8", "utf-8-sig"):
            notes.append(f"decoded as {encoding}")
        if "\r\n" in text:
            notes.append("CRLF line endings (preserved on edit)")
        if end < total:
            result += f"\n\n[... {total - end} more lines. Continue with offset={end + 1} ...]"
        if notes:
            result = f"[{rel}: {', '.join(notes)}]\n" + result
        return cap_text(result, cap, f"Use offset/limit to read a smaller range (file has {total} lines)")

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        nums = re.findall(r"^\s*(\d+)→(.*)$", output, re.M)
        if not nums:  # notices: image, binary, empty, notebook
            first = output.split("\n", 1)[0].strip().strip("[]=").strip()
            return default_summary(first, False)
        first, last = int(nums[0][0]), int(nums[-1][0])
        size = sum(len(t) + 1 for _, t in nums)
        rng = f" (lines {first}-{last})" if first > 1 or "more lines" in output else ""
        return f"{plural(len(nums), 'line')} · {human_size(size)}{rng}"

    @staticmethod
    def _suggest(p: Path, ctx: ToolContext) -> str:
        parent = p.parent
        if not parent.is_dir():
            return ""
        try:
            stem = p.stem.lower()
            cands = [c.name for c in parent.iterdir() if c.is_file() and (c.stem.lower() == stem or stem in c.name.lower())]
        except OSError:
            return ""
        if cands:
            return f". Did you mean: {', '.join(sorted(cands)[:5])}?"
        return ""
