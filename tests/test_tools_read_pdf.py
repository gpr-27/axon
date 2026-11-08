"""Read: PDF page ranges (pdftotext/pypdf), clear errors without an extractor, image notice."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.tools import create_default_registry, pdfread
from axon.tools.base import ToolContext


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))


def make_pdf(path: Path, pages: list[str]) -> None:
    """Minimal valid multi-page PDF with one text line per page."""
    objs: list[bytes] = []
    n = len(pages)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(pages):
        pn, cn = 3 + 2 * i, 4 + 2 * i
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents {cn} 0 R "
                    f"/Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> >>".encode())
        stream = f"BT /F1 18 Tf 20 150 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def test_parse_pages():
    assert pdfread.parse_pages("3") == (3, 3)
    assert pdfread.parse_pages("2-5") == (2, 5)
    assert pdfread.parse_pages("4-") == (4, None)
    for bad in ("0", "a", "5-2", "-3", "1,2"):
        with pytest.raises(ToolError):
            pdfread.parse_pages(bad)


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler not installed")
def test_page_range_with_pdftotext(ctx, workspace):
    make_pdf(workspace / "d.pdf", [f"alpha page {i}" for i in range(1, 6)])
    r = create_default_registry().get("Read")
    out = r.run({"path": "d.pdf", "pages": "2-3"}, ctx)
    assert "alpha page 2" in out and "alpha page 3" in out and "alpha page 4" not in out and "pdftotext" in out
    assert 'pages="4-"' in out


def test_page_range_without_extractor_is_clear_error(ctx, workspace, monkeypatch):
    make_pdf(workspace / "d.pdf", ["hello"])
    monkeypatch.setattr(pdfread.shutil, "which", lambda n: None)
    monkeypatch.setitem(__import__("sys").modules, "pypdf", None)
    with pytest.raises(ToolError, match="pdftotext|pypdf"):
        create_default_registry().get("Read").run({"path": "d.pdf", "pages": "1"}, ctx)


def test_plain_pdf_still_reads_with_builtin_extractor(ctx, workspace, monkeypatch):
    make_pdf(workspace / "d.pdf", ["builtin text here"])
    monkeypatch.setattr(pdfread.shutil, "which", lambda n: None)
    monkeypatch.setitem(__import__("sys").modules, "pypdf", None)
    out = create_default_registry().get("Read").run({"path": "d.pdf"}, ctx)
    assert "builtin text here" in out


def test_pages_beyond_end(ctx, workspace, monkeypatch):
    make_pdf(workspace / "d.pdf", ["one"])
    monkeypatch.setattr(pdfread, "page_count", lambda p: 1)
    out = create_default_registry().get("Read").run({"path": "d.pdf", "pages": "9"}, ctx)
    assert "beyond the end" in out


def test_image_notice_is_clear(ctx, workspace, monkeypatch):
    import axon.agent.images as images
    monkeypatch.setattr(images, "_extract_ocr_text", lambda p: "")
    png = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (4).to_bytes(4, "big") + (3).to_bytes(4, "big") + b"\0" * 20
    (workspace / "i.png").write_bytes(png)
    out = create_default_registry().get("Read").run({"path": "i.png"}, ctx)
    assert "4x3" in out and "reference the image path" in out
    monkeypatch.setattr(images, "_extract_ocr_text", lambda p: "HELLO OCR")
    assert "HELLO OCR" in create_default_registry().get("Read").run({"path": "i.png"}, ctx)
