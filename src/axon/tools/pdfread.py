"""PDF page-range text extraction for Read: pdftotext (poppler) if on PATH, else pypdf if installed."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from axon.errors import ToolError

MAX_PAGES_PER_CALL = 20


def parse_pages(spec: object) -> tuple[int, int | None]:
    """'3' -> (3, 3); '2-5' -> (2, 5); '4-' -> (4, None). Raises ToolError for anything else."""
    s = str(spec).strip()
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d*))?", s)
    if not m:
        raise ToolError(f"Argument 'pages' must look like '3', '2-5' or '4-', got {spec!r}.")
    first = int(m.group(1))
    if first < 1:
        raise ToolError("Argument 'pages' is 1-based; the first page is 1.")
    if m.group(2) is None:
        return first, first
    last = int(m.group(2)) if m.group(2) else None
    if last is not None and last < first:
        raise ToolError(f"Argument 'pages' {spec!r} ends before it starts.")
    return first, last


def page_count(path: Path) -> int | None:
    exe = shutil.which("pdfinfo")
    if exe:
        try:
            out = subprocess.run([exe, str(path)], capture_output=True, text=True, encoding="utf-8", timeout=15, errors="replace").stdout
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            if m:
                return int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from pypdf import PdfReader  # optional dependency
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def extract_pages(path: Path, first: int, last: int | None) -> tuple[str, str] | None:
    """(text, engine) for the page range, or None when no extractor is available. Raises ToolError on bad input."""
    exe = shutil.which("pdftotext")
    if exe:
        cmd = [exe, "-layout", "-f", str(first)]
        if last is not None:
            cmd += ["-l", str(last)]
        try:
            res = subprocess.run(cmd + [str(path), "-"], capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise ToolError("pdftotext timed out on this PDF.") from None
        except OSError:
            res = None
        if res is not None and res.returncode == 0:
            return res.stdout.decode("utf-8", errors="replace").replace("\x0c", "\n\n"), "pdftotext"
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return None
    try:
        pages = PdfReader(str(path)).pages
        hi = len(pages) if last is None else min(last, len(pages))
        return "\n\n".join((pages[i].extract_text() or "") for i in range(first - 1, hi)), "pypdf"
    except Exception as e:  # corrupt or encrypted file
        raise ToolError(f"Could not extract text from the PDF: {type(e).__name__}: {e}") from e
