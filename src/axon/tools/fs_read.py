"""
Read tool: numbered file contents with paging, binary/image/PDF/notebook handling,
and FileState registration for the read-before-edit invariant.
"""
from __future__ import annotations

import hashlib
import re
import zlib
from pathlib import Path
from typing import Any, ClassVar

from axon import envvars
from axon.errors import ToolError
from axon.tools.base import (
    Tool, ToolContext, cap_text, default_summary, display_path, human_size, int_arg, output_cap, plural, resolve_path,
)
from axon.tools import file_formats as ff
from axon.tools.textio import is_binary_bytes, MAX_TEXT_BYTES

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000
DEFAULT_MAX_TOKENS = 25_000
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_NOTEBOOK_BYTES = 100 * 1024 * 1024


def read_token_limit() -> int:
    val = envvars.integer("AXON_FILE_READ_MAX_OUTPUT_TOKENS")
    return val if val and val > 0 else DEFAULT_MAX_TOKENS


def _tokens(text: str) -> int:
    return len(text) // 4


def _room_tokens(ctx: ToolContext) -> int:
    """What a large read is sized against: the room left in the session's context window."""
    agent = getattr(ctx, "agent", None)
    try:
        cm = getattr(agent, "context", None) or getattr(agent, "ctx", None)
        window = int(cm.context_window())
        used = int(getattr(cm, "last_input_tokens", 0) or 0)
        return max(1000, window - used - 8000)
    except Exception:
        return 200_000


def _attach_image(p: Path, data: bytes, rel: str, size: int, ctx: ToolContext) -> str | None:
    """Carry the picture itself with the result when the provider takes images; None keeps the text notice.
    Formats a provider cannot take (BMP, TIFF, HEIC, AVIF, ICO) or files that are too big go through macOS sips."""
    from axon.providers.base import MAX_TOOL_IMAGE_BYTES, ImageRef
    provider = getattr(ctx.agent, "provider", None)
    try:
        ok = bool(provider is not None and provider.supports("tool_images"))
    except Exception:
        ok = False
    if not ok:
        return None
    prep = ff.prepare_model_image(p, size, MAX_TOOL_IMAGE_BYTES)
    if prep is None:
        return None
    path, media, note = prep
    ctx.images.append(ImageRef(str(path.resolve()), media))
    return f"[Image file: {rel} ({size:,} bytes{_image_dims(data)}{', ' + note if note else ''}). The picture is attached to this result.]"


def _image_dims(data: bytes) -> str:
    info = ff.image_info(data)
    fmt = f", {info['format']}" if info.get("format") else ""
    return f"{fmt}, {info['width']}x{info['height']}" if info.get("width") else fmt


def _image_notice(p: Path, data: bytes, rel: str) -> str:
    ocr = ""
    try:
        from axon.agent.images import _extract_ocr_text
        ocr = _extract_ocr_text(p).strip()
    except Exception:
        ocr = ""
    why = ("A tool result cannot carry the picture itself; to let the model see it, the user can reference the image path in their prompt."
           if p.suffix.lower() in ff.MODEL_IMAGE_MEDIA else
           f"{p.suffix.upper().lstrip('.')} is not a format the model takes and no converter (macOS sips) is available.")
    head = f"[Image file: {rel} ({len(data):,} bytes{_image_dims(data)}). {why}"
    if ocr:
        return head + f" Text found in the image by OCR:]\n{ocr[:20_000]}"
    return head + " No OCR engine found text in it (macOS Vision or tesseract).]"


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
        "to page through large files. One call returns at most 25,000 tokens; set "
        "allow_large=true only when you need the whole file or a long range. Lines over 2000 chars are truncated. Also reads notebooks, PDFs, "
        "images, Word/Excel/PowerPoint/OpenDocument/RTF/EPUB, CSV, SQLite, archives, audio/video and fonts as extracted text; "
        "other binaries get a hex preview. You must Read a file before "
        "you Edit/Write it; reading many files in parallel in one turn is encouraged."
    )
    schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or workspace-relative file path"},
            "offset": {"type": "integer", "minimum": 1, "description": "1-based line to start reading from (default 1)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000, "description": f"Maximum number of lines to read (default {DEFAULT_LIMIT})"},
            "pages": {"type": "string", "description": "PDF pages, slides or EPUB chapters: \"3\", \"2-5\" or \"4-\""},
            "part": {"type": "string", "description": "Sheet name, SQLite table or archive member path"},
            "allow_large": {"type": "boolean", "description": "Read a large text file or long range in one call, sized "
                                                              "against the room left in the context window"},
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
        offset = int_arg(args, "offset", 1, lo=1, strict=True)
        allow_large = bool(args.get("allow_large"))
        whole = args.get("offset") is None and args.get("limit") is None
        limit = int_arg(args, "limit", 10**9 if allow_large else DEFAULT_LIMIT, lo=1, hi=None if allow_large else 20000, strict=True)
        token_limit = _room_tokens(ctx) if allow_large else read_token_limit()
        cap = token_limit * 4 if allow_large else output_cap(ctx, 100_000)

        try:
            size = p.stat().st_size
        except OSError as e:
            raise ToolError(f"Cannot access {raw_path}: {e}") from e
        if size == 0:
            ctx.file_state.record_read(p)
            return f"[{rel} exists but is empty (0 bytes).]"

        try:
            with open(p, "rb") as f:
                head = f.read(512)
                kind = ff.detect_kind(p, head)
                if kind in ff.EXTRACT_KINDS and kind != "image":
                    data = head
                else:
                    f.seek(0)
                    data = f.read(MAX_TEXT_BYTES + 1)
        except OSError as e:
            raise ToolError(f"Failed to read file {raw_path}: {e}") from e

        if kind == "image":
            if size > MAX_IMAGE_BYTES:
                raise ToolError(f"{rel} is {size // 1_048_576} MB, over the 128 MB limit for reading an image. Resize it or "
                                f"export a smaller copy, then read that copy.")
            ctx.file_state.record_read(p)
            attached = _attach_image(p, data, rel, size, ctx)
            return attached if attached is not None else _image_notice(p, data, rel)

        if kind == "pdf" or data[:5] == b"%PDF-":
            ctx.file_state.record_read(p)
            return self._read_pdf(p, data, rel, size, offset, limit, cap, args.get("pages"))

        if kind == "ipynb":
            if size > MAX_NOTEBOOK_BYTES:
                raise ToolError(f"{rel} is {size // 1_048_576} MB, over the 100 MB limit for reading a notebook. Read a slice of "
                                f"its cells with a shell command, such as: python -c \"import json; print(json.load(open('{rel}'))"
                                f"['cells'][:5])\"")
            nb = None
            try:
                from axon.tools.notebook import load_notebook, render_notebook
                nb = load_notebook(p)
            except ToolError:
                pass  # malformed notebook JSON: fall through and show raw text
            if nb is not None:
                rendered = render_notebook(nb, p.name)
                ctx.file_state.record_read(p, partial=len(rendered) > cap)
                out = cap_text(rendered, cap, "Use NotebookEdit to modify cells.")
                self._attach_notebook_images(nb, ctx)
                return out

        if kind in ff.EXTRACT_KINDS:
            ctx.file_state.record_read(p)
            return self._read_extracted(p, kind, rel, size, offset, limit, cap, args)

        if len(data) > MAX_TEXT_BYTES:
            return self._read_huge(p, rel, size, offset, limit, cap)
        if is_binary_bytes(data) and not ff.sniff_utf16(data):
            ctx.file_state.record_read(p)
            return self._read_extracted(p, "binary", rel, size, offset, limit, cap, args)

        text, encoding = ff.decode_text(data)
        ctx.file_state.record_read(p)

        lines = split_lines(text)
        total = len(lines)
        if offset > total:
            return f"[{rel} has {total} lines; offset {offset} is beyond the end of the file.]"
        end = min(total, offset - 1 + limit)
        result = _number_lines(lines[offset - 1 : end], offset)
        partial = False
        if _tokens(result) > token_limit:
            if not whole:
                raise ToolError(f"Lines {offset}-{end} of {rel} come to about {_tokens(result):,} tokens, over the limit of "
                                f"{token_limit:,}. Read a smaller portion with offset and limit, or search with Grep.")
            room, end = token_limit * 4, offset - 1
            for ln in lines:      # the first page: as many whole lines as fit
                room -= min(len(ln), MAX_LINE_CHARS) + 8
                if room < 0:
                    break
                end += 1
            end = max(end, 1)
            result = _number_lines(lines[:end], 1)
            partial = True
        if whole and end < total:
            partial = True
        ctx.file_state.record_read(p, partial=partial)
        notes = []
        if encoding not in ("utf-8", "utf-8-sig"):
            notes.append(f"decoded as {encoding}")
        if "\r\n" in text:
            notes.append("CRLF line endings (preserved on edit)")
        if kind in ff.TEXT_FORMAT_KINDS and offset == 1:
            note = ff.text_note(kind, text, p)
            if note:
                notes.append(note)
        if end < total:
            head = f"PARTIAL view: lines {offset}-{end} of {total}. " if partial else ""
            result += f"\n\n[{head}... {total - end} more lines. Continue with offset={end + 1} ...]"
        if notes:
            result = f"[{rel}: {', '.join(notes)}]\n" + result
        return cap_text(result, max(cap, token_limit * 4) if not allow_large else cap,
                        f"Use offset/limit to read a smaller range (file has {total} lines)")

    @staticmethod
    def _attach_notebook_images(nb: dict[str, Any], ctx: ToolContext) -> None:
        """Show up to three picture outputs of a notebook to a model that takes images."""
        try:
            provider = getattr(ctx.agent, "provider", None)
            if provider is None or not provider.supports("tool_images"):
                return
            from axon.providers.base import MAX_TOOL_IMAGE_BYTES, ImageRef
            _cells, images = ff.notebook_cells(nb)
            for im in images[:3]:
                if len(im.data) > MAX_TOOL_IMAGE_BYTES:
                    continue
                f = ff._cache_dir() / (hashlib.sha1(im.data).hexdigest()[:20] + (".png" if im.mime == "image/png" else ".jpg"))
                if not f.exists():
                    f.write_bytes(im.data)
                ctx.images.append(ImageRef(str(f), im.mime))
        except Exception:
            return

    def _read_extracted(self, p: Path, kind: str, rel: str, size: int, offset: int, limit: int, cap: int, args: dict[str, Any]) -> str:
        ex = ff.extract(p, pages=args.get("pages"), sheet=args.get("part"), limit=limit, offset=offset - 1, kind=kind)
        if not ex.ok:
            return ex.text
        m = ex.meta
        unit = m.get("unit", "")
        bits = [kind, f"{size:,} bytes"]
        for k, v in (m.get("props") or {}).items():
            if k in ("title", "creator"):
                bits.append(f"{k} {v!r}")
        if m.get("total") and unit:
            bits.append(f"{m['total']:,} {unit}s")
        bits.extend(m.get("notes") or []) if kind not in ("zip", "tar") else None
        head = f"[{rel}: {'; '.join(str(b) for b in bits)}]"
        tail = ""
        if m.get("next"):
            tail = f"\n\n[... more {unit or 'item'}s. Continue with offset={m['next']} ...]"
        elif m.get("next_pages"):
            tail = f"\n\n[... more. Continue with pages=\"{m['next_pages']}\" ...]"
        if m.get("sheets") and kind in ("xlsx", "ods", "sqlite") and len(m["sheets"]) > 1:
            tail += f"\n[Switch with part=<{'table' if kind == 'sqlite' else 'sheet'} name>]"
        body = ex.text
        if kind in ("zip", "tar") and not args.get("part"):
            body = body + "\n[Read a file inside with part=<path inside the archive>]"
        return cap_text(f"{head}\n{body}{tail}", cap, "Use offset/limit or pages to read a smaller range")

    def _read_huge(self, p: Path, rel: str, size: int, offset: int, limit: int, cap: int) -> str:
        """Files above the in-memory limit are paged line by line from disk; very long lines are cut, never loaded whole."""
        import itertools
        out: list[str] = []
        n = 0
        room = max(cap - 400, 1000)     # stop at a whole line before the output cap, so the header and the offset to continue from stay true
        full = False
        with open(p, "rb") as f:
            for text, dropped in itertools.islice(ff.iter_lines_bounded(f, MAX_LINE_CHARS), offset - 1, offset - 1 + limit):
                row = f"{offset + n:6d}→{text}" + (f" ...[line truncated, {dropped} more chars]" if dropped else "")
                room -= len(row) + 1
                if room < 0 and out:
                    full = True
                    break
                n += 1
                out.append(row)
        if not out:
            return f"[{rel} ({size:,} bytes) has no line {offset}: offset is beyond the end of the file.]"
        more = ""
        if full:
            more = f"\n\n[... more lines follow: this result is full. Continue with offset={offset + n} ...]"
        elif n == limit:
            more = f"\n\n[... more lines may follow. Continue with offset={offset + n} ...]"
        head = (f"[{rel}: {size // 1_048_576} MB, too large to load whole, so lines {offset}-{offset + n - 1} are read from disk; "
                f"use Grep to find a region]")
        return cap_text(head + "\n" + "\n".join(out) + more, cap, "Use a smaller limit")

    def _read_pdf(self, p: Path, data: bytes, rel: str, size: int, offset: int, limit: int, cap: int,
                  pages: Any) -> str:
        from axon.tools import pdfread
        total_pages = pdfread.page_count(p)
        if pages not in (None, ""):
            first, last = pdfread.parse_pages(pages)
            if total_pages is not None and first > total_pages:
                return f"[PDF {rel} has {total_pages} page(s); pages={pages!r} is beyond the end.]"
            if last is None or last - first + 1 > pdfread.MAX_PAGES_PER_CALL:
                last = first + pdfread.MAX_PAGES_PER_CALL - 1
            if total_pages is not None:
                last = min(last, total_pages)
        else:
            first, last = 1, None
            if total_pages is not None and total_pages > pdfread.MAX_PAGES_PER_CALL:
                last = pdfread.MAX_PAGES_PER_CALL
        got = pdfread.extract_pages(p, first, last)
        if got is not None:
            text, engine = got
            span = f"page {first}" if last == first else f"pages {first}-{last if last else (total_pages or '?')}"
            of = f" of {total_pages}" if total_pages else ""
            body = _number_lines(text.splitlines()[offset - 1 : offset - 1 + limit], offset) if text.strip() else \
                "(no text on these pages; the PDF may be scanned images)"
            more = ""
            if total_pages and last is not None and last < total_pages:
                more = f"\n\n[... {total_pages - last} more page(s). Continue with pages=\"{last + 1}-\" ...]"
            return cap_text(f"[PDF: {rel}, {span}{of}, text via {engine}]\n{body}{more}", cap)
        if pages not in (None, "") or (total_pages or 0) > pdfread.MAX_PAGES_PER_CALL:
            raise ToolError(
                "Reading PDF page ranges needs poppler's pdftotext on PATH (brew install poppler / apt install "
                "poppler-utils) or the optional pypdf package (pip install pypdf)."
            )
        text, n = extract_pdf_text(data)
        if _looks_readable(text):
            body = _number_lines(text.splitlines()[offset - 1 : offset - 1 + limit], offset)
            return cap_text(f"[PDF: {rel}, ~{n} page(s), basic text extraction]\n{body}", cap)
        return (
            f"[PDF: {rel} ({size:,} bytes, ~{n} page(s)). Text could not be extracted (compressed fonts or scanned "
            f"images). Install poppler (pdftotext) or pypdf for better extraction.]"
        )

    def summarize(self, args: dict[str, Any], output: str, is_error: bool = False) -> str:
        if is_error:
            return default_summary(output, True)
        nums = re.findall(r"^\s*(\d+)→(.*)$", output, re.M)
        if not nums:  # notices: image, binary, empty, notebook
            first = output.split("\n", 1)[0].strip().strip("[]=").strip()
            return default_summary(first, False)
        first, last = int(nums[0][0]), int(nums[-1][0])
        size = sum(len(t) + 1 for _, t in nums)
        tail = output[-300:]
        rest = re.search(r"\.\.\. (\d+) more lines\. Continue with offset=", tail)
        if rest:
            rng = f" (lines {first}-{last} of {last + int(rest.group(1)):,})"
        elif re.search(r"more lines (may )?follow", tail):     # a file paged from disk: its length is not known
            rng = f" (lines {first}-{last}, more follow)"
        else:
            rng = f" (lines {first}-{last})" if first > 1 else ""
        cut = len(re.findall(r" \.\.\.\[line truncated, \d+ more chars\]$", output, re.M))
        long_lines = f" · {plural(cut, 'long line')} cut" if cut else ""
        return f"{plural(len(nums), 'line')} · {human_size(size)}{rng}{long_lines}"

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
