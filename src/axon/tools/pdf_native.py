"""Read a PDF as a native ``document`` block (docs: PDF support) instead of extracted text.

The file (or a page-range slice of it) is attached to the tool result by reference, like a picture, and read
and base64-encoded only when the request is built. Limits from the docs: standard unencrypted PDF, at most
100 pages per request unless the context window is 1M (600), and a request size bound (we keep a PDF under
MAX_NATIVE_BYTES so the request stays well inside it). When the provider or model takes no PDF input, the
caller keeps the text-extraction path (``attach`` returns None)."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from axon.errors import ToolError
from axon.providers.base import ImageRef
from axon.tools import pdfread

MAX_NATIVE_PAGES = 100
MAX_NATIVE_BYTES = 24 * 1024 * 1024
NOTE = "sent to the model natively"


def native_ok(ctx: Any) -> bool:
    """Does the active provider and model accept PDF document blocks in a tool result?"""
    from axon.tools import pdf_render
    return pdf_render.native_pdf_ok(ctx)


def _encrypted(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 4096))
            return b"/Encrypt" in f.read()
    except OSError:
        return False


def _slice(path: Path, first: int, last: int) -> Path | None:
    """A PDF of pages first..last (cached), or None when no tool to cut one is installed."""
    from axon import storage
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{first}-{last}".encode()).hexdigest()[:20]
    out = storage.cache_dir("pdf-slices") / f"{key}.pdf"
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        from pypdf import PdfReader, PdfWriter  # optional dependency
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for i in range(first - 1, min(last, len(reader.pages))):
            writer.add_page(reader.pages[i])
        with open(out, "wb") as f:
            writer.write(f)
        return out
    except ImportError:
        pass
    except Exception:
        return None
    qpdf = shutil.which("qpdf")
    try:
        if qpdf:
            subprocess.run([qpdf, str(path), "--pages", ".", f"{first}-{last}", "--", str(out)], capture_output=True, timeout=60, check=True)
            return out
        sep, uni = shutil.which("pdfseparate"), shutil.which("pdfunite")
        if sep and uni:
            tmp = out.with_suffix(".parts")
            tmp.mkdir(exist_ok=True)
            try:
                subprocess.run([sep, "-f", str(first), "-l", str(last), str(path), str(tmp / "p-%d.pdf")],
                               capture_output=True, timeout=60, check=True)
                parts = [str(tmp / f"p-{i}.pdf") for i in range(first, last + 1) if (tmp / f"p-{i}.pdf").exists()]
                if not parts:
                    return None
                if len(parts) == 1:
                    shutil.copyfile(parts[0], out)
                else:
                    subprocess.run([uni, *parts, str(out)], capture_output=True, timeout=60, check=True)
                return out
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except (OSError, subprocess.SubprocessError):
        out.unlink(missing_ok=True)
    return None


def attach(path: Path, rel: str, size: int, pages: Any, ctx: Any) -> str | None:
    """Attach the PDF (or the asked pages) to the result as a document and return the text that goes with it.
    None means: use the text path (provider takes no PDFs, encrypted file, no tool to cut a page range).
    Raises ToolError with the limit that was hit."""
    if not native_ok(ctx) or _encrypted(path):
        return None
    total = pdfread.page_count(path)
    first, last = 1, total
    ranged = pages not in (None, "")
    if ranged:
        first, last = pdfread.parse_pages(pages)
        if total is not None:
            if first > total:
                return f"[PDF {rel} has {total} page(s); pages={pages!r} is beyond the end.]"
            last = min(last if last is not None else total, total)
        elif last is None:
            return None            # an open range of a PDF whose length is unknown: use the text path
    n = (last - first + 1) if last is not None else total
    if n is not None and n > MAX_NATIVE_PAGES:
        raise ToolError(
            f"{rel} has {n} pages; at most {MAX_NATIVE_PAGES} pages can be sent to the model in one read. "
            f"Read a range, such as pages=\"{first}-{first + MAX_NATIVE_PAGES - 1}\".")
    whole = not ranged or (total is not None and first == 1 and last == total)
    target = path
    if not whole or size > MAX_NATIVE_BYTES:
        if last is None:
            return None
        cut = _slice(path, first, last)
        if cut is None:
            if size > MAX_NATIVE_BYTES:
                raise ToolError(f"{rel} is {size / 1_048_576:.0f} MB, over the {MAX_NATIVE_BYTES // 1_048_576} MB limit for sending a PDF whole. "
                                f"Read a page range (pages=\"1-20\"); that needs the optional pypdf package, qpdf, or poppler's pdfseparate and pdfunite.")
            return None            # cannot cut the range: the text path reads it
        target = cut
        if target.stat().st_size > MAX_NATIVE_BYTES:
            raise ToolError(f"Pages {first}-{last} of {rel} are {target.stat().st_size / 1_048_576:.0f} MB, over the "
                            f"{MAX_NATIVE_BYTES // 1_048_576} MB limit. Read fewer pages at a time.")
    ctx.images.append(ImageRef(str(target.resolve()), "application/pdf"))
    count = n if n is not None else (total or 0)
    span = f"pages {first}-{last} of {total}" if ranged and total else (f"{count} page{'s' if count != 1 else ''}" if count else "all pages")
    return (f"[PDF: {rel}, {span}. The document is attached to this result and was {NOTE}; "
            f"read its text, tables and figures from the attachment.]")


_SUMMARY = re.compile(r"^\[PDF: .*?(?:(\d+) pages?|pages (\d+)-(\d+) of (\d+))")


_TEXT = re.compile(r"^\[PDF: .*?, (?:pages? (\d+)(?:-(\d+|\?))?(?: of (\d+))?|~(\d+) page\(s\)), (?:text via|basic text extraction)")


def _text_summary(output: str) -> str:
    """'PDF · 12 pages · text extracted' for a PDF read as text (the provider or model takes no PDF input)."""
    m = _TEXT.match(output)
    if not m:
        return ""
    if m.group(4):
        n = int(m.group(4))
        pages = f"{n} page{'s' if n != 1 else ''}"
    else:
        first, last, total = m.group(1), m.group(2), m.group(3)
        if last in (None, "?"):
            last = first if last is None else (total or first)
        if first == "1" and total and last == total:
            pages = f"{total} page{'s' if total != '1' else ''}"
        elif first == last:
            pages = f"page {first}" + (f" of {total}" if total else "")
        else:
            pages = f"pages {first}-{last}" + (f" of {total}" if total else "")
    return f"PDF · {pages} · text extracted (images not included)"


def summary_line(output: str) -> str:
    """'PDF · 12 pages · sent to the model natively' for an attached PDF's result, 'PDF · 12 pages · text extracted'
    for one read as text, else ''."""
    if NOTE not in output[:400]:
        if not output.startswith("[PDF: "):
            return ""
        from axon.tools import pdf_render
        return pdf_render.summary_line(output) or _text_summary(output)
    m = _SUMMARY.match(output)
    if m and m.group(1):
        pages = f"{m.group(1)} page{'s' if m.group(1) != '1' else ''}"
    elif m:
        pages = f"pages {m.group(2)}-{m.group(3)} of {m.group(4)}"
    else:
        pages = "all pages"
    return f"PDF · {pages} · sent natively"
