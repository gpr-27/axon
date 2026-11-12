"""Provider-independent PDF reading chain.

Picks, per provider and model, the richest way to show a PDF to the model:

1. native   a ``document`` block, when the provider and model accept PDFs (see pdf_native);
2. images   the requested pages rendered to pictures (at most MAX_IMAGE_PAGES per read) and sent as image blocks
            together with the pages' extracted text, when the model can see pictures but takes no PDF;
3. ocr      pages without a text layer rendered and read by the OCR helper (a text-only model), marked as OCR;
4. text     plain text extraction (the existing path in fs_read).

The ``pdf_mode`` setting (auto | native | images | text) overrides the choice. Rendering uses a tool that is
already installed (poppler's pdftoppm or pdftocairo, MuPDF's mutool, PyMuPDF or pypdfium2 when importable, macOS
sips for page 1); nothing is a hard dependency. Rendered pages are cached under ~/.axon/cache/pdf-pages."""
from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from axon.errors import ToolError
from axon.providers.base import MAX_TOOL_IMAGE_BYTES, ImageRef
from axon.tools import pdfread

MAX_IMAGE_PAGES = 10
LONG_EDGE = 1500
MAX_TEXT_CHARS = 60_000
OCR_MARK = "[OCR text — may contain errors]"
MODES = ("auto", "native", "images", "text")
_MIN_TEXT = 8                    # fewer characters than this on a page: no text layer

_shown: set[str] = set()
_pending_note: list[str] = []


# ---------------------------------------------------------------- capability

def _agent_bits(agent: Any) -> tuple[Any, str, str]:
    prov = getattr(agent, "provider", None)
    st = getattr(agent, "settings", None)
    model = str(getattr(st, "model", "") or "")
    mode = str(getattr(st, "pdf_mode", "auto") or "auto").lower()
    return prov, model, mode if mode in MODES else "auto"


def _supports(prov: Any, feature: str) -> bool:
    try:
        return bool(prov is not None and prov.supports(feature))
    except Exception:
        return False


def _caps_allow(model: str, feature: str) -> bool | None:
    try:
        from axon.providers import model_caps
        return model_caps.allows(model, feature)
    except Exception:
        return None


def _claude_like(model: str) -> bool:
    m = model.lower()
    return "claude" in m or m.startswith(("opus", "sonnet", "haiku", "fable", "mythos"))


_VISION_NAMES = re.compile(r"vision|llava|pixtral|moondream|minicpm-v|[-_.]vl\b|[-_.]vl[-_.:]|gpt-4o|gpt-4\.1|gpt-5|o[134]\b|gemini|"
                           r"gemma-?3|llama-?4|claude|qwen[0-9.]*-?vl|internvl|mistral-(?:small|medium)-3|grok-?[2-9].*vision", re.I)


def name_looks_visual(model: str) -> bool:
    """Last resort when no capability list says whether a model takes pictures: well-known vision model names."""
    return bool(_VISION_NAMES.search(model or ""))


def agent_vision(agent: Any) -> bool:
    """Can the active provider and model see pictures (tool results and the user's own attachments)?"""
    prov, model, _ = _agent_bits(agent)
    if prov is None or not _supports(prov, "tool_images"):
        return False
    return _caps_allow(model, "image_input") is not False


def vision_ok(ctx: Any) -> bool:
    return agent_vision(getattr(ctx, "agent", None))


def native_pdf_ok(ctx: Any) -> bool:
    """Does the active provider and model accept a PDF document block in a tool result?
    The models list (or table) decides when it says so; otherwise the provider's own answer. A custom Anthropic-wire
    host that does not publish capabilities is trusted only for Claude model names."""
    prov, model, mode = _agent_bits(getattr(ctx, "agent", None))
    if mode in ("images", "text") or prov is None or not _supports(prov, "tool_pdf"):
        return False
    known = _caps_allow(model, "pdf_input")
    if known is not None:
        return known
    if mode == "native":
        return True
    if getattr(prov, "_official", True) is False and not _claude_like(model):
        return False
    return True


def choose(ctx: Any) -> str:
    """'native' | 'images' | 'ocr' | 'text' for the active provider, model and pdf_mode."""
    _, _, mode = _agent_bits(getattr(ctx, "agent", None))
    if mode == "text":
        return "text"
    if native_pdf_ok(ctx):
        return "native"
    return "images" if vision_ok(ctx) else "ocr"


# ---------------------------------------------------------------- renderers

def _find_renderer() -> str | None:
    for exe in ("pdftoppm", "pdftocairo", "mutool"):
        if shutil.which(exe):
            return exe
    for mod, name in (("fitz", "pymupdf"), ("pypdfium2", "pypdfium2")):
        try:
            if importlib.util.find_spec(mod) is not None:
                return name
        except (ImportError, ValueError):
            pass
    if sys.platform == "darwin" and shutil.which("sips"):
        return "sips"            # first page only
    return None


def _run(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _normalise(outdir: Path, prefix: str) -> None:
    """Rename pdftoppm-style p-01.jpg to p-1.jpg."""
    for f in outdir.glob(f"{prefix}-*"):
        m = re.fullmatch(rf"{prefix}-(\d+)\.(jpg|png)", f.name)
        if m:
            target = outdir / f"p-{int(m.group(1))}.{m.group(2)}"
            if target != f:
                f.replace(target)


def _render_poppler(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
    exe_path = shutil.which(exe) or exe
    _run([exe_path, "-jpeg", "-jpegopt", "quality=80", "-scale-to", str(LONG_EDGE), "-f", str(first), "-l", str(last),
          str(path), str(outdir / "p")])
    _normalise(outdir, "p")


def _render_mutool(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
    _run([shutil.which("mutool") or "mutool", "draw", "-q", "-r", "110", "-o", str(outdir / "p-%d.png"), str(path), f"{first}-{last}"])


def _render_pymupdf(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
    import fitz  # type: ignore[import-not-found]
    doc = fitz.open(str(path))
    for n in range(first, min(last, len(doc)) + 1):
        page = doc[n - 1]
        r = page.rect
        s = LONG_EDGE / max(r.width, r.height, 1)
        (outdir / f"p-{n}.jpg").write_bytes(page.get_pixmap(matrix=fitz.Matrix(s, s)).tobytes("jpeg"))


def _render_pdfium(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
    import pypdfium2 as pdfium  # type: ignore[import-not-found]
    doc = pdfium.PdfDocument(str(path))
    for n in range(first, min(last, len(doc)) + 1):
        page = doc[n - 1]
        w, h = page.get_size()
        img = page.render(scale=LONG_EDGE / max(w, h, 1)).to_pil().convert("RGB")
        img.save(outdir / f"p-{n}.jpg", quality=80)


def _render_sips(exe: str, path: Path, first: int, last: int, outdir: Path) -> None:
    if first == 1:
        _run(["sips", "-s", "format", "jpeg", "-Z", str(LONG_EDGE), str(path), "--out", str(outdir / "p-1.jpg")])


_RENDER: dict[str, Callable[[str, Path, int, int, Path], None]] = {
    "pdftoppm": _render_poppler, "pdftocairo": _render_poppler, "mutool": _render_mutool,
    "pymupdf": _render_pymupdf, "pypdfium2": _render_pdfium, "sips": _render_sips,
}


def _page_file(outdir: Path, n: int) -> Path | None:
    for ext in ("jpg", "png"):
        f = outdir / f"p-{n}.{ext}"
        if f.exists() and f.stat().st_size > 0:
            return f
    return None


def _fit(f: Path) -> Path | None:
    """A page picture within the per-image size limit, or None."""
    if f.stat().st_size <= MAX_TOOL_IMAGE_BYTES:
        return f
    if shutil.which("sips"):
        out = f.with_suffix(".small.jpg")
        if _run(["sips", "-s", "format", "jpeg", "-Z", "1100", str(f), "--out", str(out)]) and out.exists() \
                and out.stat().st_size <= MAX_TOOL_IMAGE_BYTES:
            return out
    return None


def render_pages(path: Path, first: int, last: int, renderer: str) -> dict[int, Path]:
    """{page: picture file} for the pages that could be rendered; cached per file version."""
    from axon import storage
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{LONG_EDGE}".encode()).hexdigest()[:20]
    outdir = storage.cache_dir("pdf-pages") / key
    outdir.mkdir(parents=True, exist_ok=True)
    missing = [n for n in range(first, last + 1) if _page_file(outdir, n) is None]
    if missing:
        try:
            _RENDER[renderer](renderer, path, min(missing), max(missing), outdir)
        except Exception:
            pass
    out: dict[int, Path] = {}
    for n in range(first, last + 1):
        f = _page_file(outdir, n)
        f = _fit(f) if f is not None else None
        if f is not None:
            out[n] = f
    return out


# ---------------------------------------------------------------- notes

def take_note() -> str:
    """A one-time hint (which tool to install) to append to the text result, else ''."""
    if not _pending_note:
        return ""
    note = _pending_note.pop()
    return "\n\n" + note


def _note_once(key: str, text: str) -> None:
    if key not in _shown:
        _shown.add(key)
        _pending_note.append(text)


NO_RENDERER = ("[Note: no tool to render PDF pages as pictures was found, so only the text was read and images, charts and "
               "scans are not included. Install poppler (macOS: brew install poppler; Debian or Ubuntu: apt install "
               "poppler-utils) or MuPDF (mutool) and read the PDF again.]")
NO_OCR = ("[Note: some pages have no text layer and no OCR engine found text in them. Install tesseract (macOS: brew install "
          "tesseract) and a page renderer such as poppler (brew install poppler).]")


# ---------------------------------------------------------------- text helpers

def _span(first: int, last: int, total: int | None, ranged: bool) -> str:
    n = last - first + 1
    if first == 1 and (last == total or (total is None and not ranged)):
        return f"{n} page{'s' if n != 1 else ''}"
    of = f" of {total}" if total else ""
    return f"page {first}{of}" if first == last else f"pages {first}-{last}{of}"


def _page_text(path: Path, n: int) -> str:
    try:
        got = pdfread.extract_pages(path, n, n)
    except ToolError:
        return ""
    return (got[0] if got else "").strip()


def _window(path: Path, rel: str, pages: Any) -> tuple[int, int, int | None, bool] | str:
    """(first, last, total, ranged), or a message when the range is beyond the end. Raises ToolError over the limit."""
    total = pdfread.page_count(path)
    ranged = pages not in (None, "")
    if ranged:
        first, last = pdfread.parse_pages(pages)
        if total is not None and first > total:
            return f"[PDF {rel} has {total} page(s); pages={pages!r} is beyond the end.]"
        if last is not None and last - first + 1 > MAX_IMAGE_PAGES:
            raise ToolError(f"{rel}: {last - first + 1} pages is more than the {MAX_IMAGE_PAGES} that can be read as pictures "
                            f"at once. Read pages=\"{first}-{first + MAX_IMAGE_PAGES - 1}\" first, then the next range.")
        if last is None:
            last = first + MAX_IMAGE_PAGES - 1
    else:
        first, last = 1, MAX_IMAGE_PAGES
    if total is not None:
        last = min(last, total)
    return first, last, total, ranged


def _more(last: int, total: int | None) -> str:
    if total and last < total:
        return f"\n\n[... {total - last} more page(s). Continue with pages=\"{last + 1}-{min(total, last + MAX_IMAGE_PAGES)}\" ...]"
    return ""


def _ocr(p: Path) -> str:
    from axon.agent.images import _extract_ocr_text
    return _extract_ocr_text(p)


# ---------------------------------------------------------------- the read

def read_visual(path: Path, rel: str, size: int, pages: Any, ctx: Any, cap: int = 100_000) -> str | None:
    """Pages as pictures (the model sees images) or OCR text for scanned pages (it does not). None: use plain text."""
    mode = choose(ctx)
    if mode not in ("images", "ocr"):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 4096))
            if b"/Encrypt" in f.read():
                return None
    except OSError:
        return None
    win = _window(path, rel, pages)
    if isinstance(win, str):
        return win
    first, last, total, ranged = win
    renderer = _find_renderer()
    if mode == "images":
        if renderer is None:
            _note_once("renderer", NO_RENDERER)
            return None
        shots = render_pages(path, first, last, renderer)
        if not shots:
            _note_once("renderer", NO_RENDERER)
            return None
        for n in sorted(shots):
            ctx.images.append(ImageRef(str(shots[n].resolve()), "image/png" if shots[n].suffix == ".png" else "image/jpeg"))
        got = sorted(shots)
        listed = ", ".join(str(n) for n in got)
        text = ""
        try:
            ex = pdfread.extract_pages(path, first, last)
            text = (ex[0] if ex else "").strip()
        except ToolError:
            pass
        head = (f"[PDF: {rel}, {_span(first, last, total, ranged)}, rendered as images. The page pictures are attached to "
                f"this result in order (pages {listed}); the extracted text of the same pages follows.]")
        body = text if text else "(no text layer on these pages; read them from the pictures)"
        missed = [n for n in range(first, last + 1) if n not in shots]
        warn = f"\n[Pages {', '.join(map(str, missed))} could not be rendered within the size limit.]" if missed else ""
        return cap_len(f"{head}\n{body}{warn}{_more(last, total)}", cap)
    # OCR: text-only model
    texts = {n: _page_text(path, n) for n in range(first, last + 1)}
    scanned = [n for n, t in texts.items() if len(t) < _MIN_TEXT]
    if not scanned:
        return None
    shots = render_pages(path, min(scanned), max(scanned), renderer) if renderer else {}
    done: dict[int, str] = {}
    for n in scanned:
        if n in shots:
            try:
                t = _ocr(shots[n]).strip()
            except Exception:
                t = ""
            if t:
                done[n] = t
    if not done:
        _note_once("ocr", NO_OCR if renderer else NO_RENDERER)
        return None
    parts = []
    for n in range(first, last + 1):
        if n in done:
            parts.append(f"--- page {n} ---\n{OCR_MARK}\n{done[n]}")
        elif texts[n]:
            parts.append(f"--- page {n} ---\n{texts[n]}")
        else:
            parts.append(f"--- page {n} ---\n(no text found on this page)")
    head = f"[PDF: {rel}, {_span(first, last, total, ranged)}, OCR text. Pages {', '.join(map(str, sorted(done)))} are scans read by OCR.]"
    return cap_len(head + "\n" + "\n".join(parts) + _more(last, total), cap)


def cap_len(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + f"\n[... cut at {cap:,} characters; read fewer pages at a time ...]"


# ---------------------------------------------------------------- card line

_CARD = re.compile(r"^\[PDF: .*?, (\d+ pages?|pages? \d+(?:-\d+)?(?: of \d+)?), (rendered as images|OCR text)[.,]")


def summary_line(output: str) -> str:
    """'PDF · pages 2-4 of 30 · rendered as images' / 'PDF · 5 pages · OCR text', else ''."""
    m = _CARD.match(output)
    return f"PDF · {m.group(1)} · {m.group(2)}" if m else ""
