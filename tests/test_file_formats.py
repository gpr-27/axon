"""Every common file type through the shared extraction layer and the Read tool. Fixtures are built in code."""
from __future__ import annotations

import base64
import gzip
import io
import json
import shutil
import sqlite3
import struct
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.tools import create_default_registry
from axon.tools import file_formats as ff
from axon.tools.base import ToolContext
from test_dashboard import c, env, server  # noqa: F401,E402  (fixtures for the web route test)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CT = '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))


def read(ctx: ToolContext, **args) -> str:
    return create_default_registry().get("Read").run(args, ctx)


def zipf(path: Path, files: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return path


def make_docx(path: Path, body: str | None = None) -> Path:
    body = body or (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly Report</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Revenue grew </w:t></w:r><w:r><w:t>12%.</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>First point</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sales</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:p><w:r><w:t>EMEA</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    styles = (f'<w:styles xmlns:w="{W}"><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>')
    core = ('<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Q3</dc:title><dc:creator>Ana</dc:creator></cp:coreProperties>')
    return zipf(path, {"[Content_Types].xml": CT, "word/document.xml": doc, "word/styles.xml": styles, "docProps/core.xml": core})


def make_xlsx(path: Path, rows: int = 5) -> Path:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    wb = (f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/>'
          f'<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="x" Target="worksheets/sheet2.xml"/></Relationships>')
    sst = f'<sst xmlns="{ns}"><si><t>Name</t></si><si><t>Total</t></si><si><r><t>Al</t></r><r><t>ice</t></r></si></sst>'
    styles = f'<styleSheet xmlns="{ns}"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    body = '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1"><v>1</v></c></row>'
    for i in range(2, rows + 1):
        body += (f'<row r="{i}"><c r="A{i}" t="s"><v>2</v></c><c r="B{i}"><f>SUM(1,{i})</f><v>{i + 1}</v></c>'
                 f'<c r="C{i}" s="1"><v>45000</v></c></row>')
    s1 = f'<worksheet xmlns="{ns}"><dimension ref="A1:D{rows}"/><sheetData>{body}</sheetData></worksheet>'
    s2 = f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>hello notes</t></is></c></row></sheetData></worksheet>'
    return zipf(path, {"[Content_Types].xml": CT, "xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": rels, "xl/sharedStrings.xml": sst,
                       "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": s1, "xl/worksheets/sheet2.xml": s2})


def make_pptx(path: Path) -> Path:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pres = f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>'
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="slides/slide1.xml"/><Relationship Id="rId2" Type="x" Target="slides/slide2.xml"/></Relationships>')

    def slide(title: str, body: str) -> str:
        return (f'<p:sld xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree>'
                f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>'
                f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>'
                f'</p:spTree></p:cSld></p:sld>')
    notes = (f'<p:notes xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>'
             f'<p:txBody><a:p><a:r><a:t>Say hello</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>')
    srel = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>')
    return zipf(path, {"[Content_Types].xml": CT, "ppt/presentation.xml": pres, "ppt/_rels/presentation.xml.rels": rels,
                       "ppt/slides/slide1.xml": slide("Launch plan", "Ship in May"), "ppt/slides/slide2.xml": slide("Risks", "Budget"),
                       "ppt/slides/_rels/slide1.xml.rels": srel, "ppt/notesSlides/notesSlide1.xml": notes})


ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"')


def make_odf(path: Path, kind: str) -> Path:
    if kind == "odt":
        body = ('<office:text><text:h text:outline-level="1">Heading A</text:h><text:p>Para with<text:s text:c="2"/>spaces</text:p>'
                '<text:list><text:list-item><text:p>bullet one</text:p></text:list-item></text:list></office:text>')
    elif kind == "ods":
        body = ('<office:spreadsheet><table:table table:name="Data"><table:table-row><table:table-cell office:value-type="string"><text:p>Item</text:p></table:table-cell>'
                '<table:table-cell office:value-type="float" office:value="7"><text:p>7</text:p></table:table-cell></table:table-row>'
                '<table:table-row table:number-rows-repeated="1000000"><table:table-cell table:number-columns-repeated="16000"/></table:table-row>'
                '</table:table></office:spreadsheet>')
    else:
        body = ('<office:presentation><draw:page draw:name="p1"><draw:frame presentation:class="title"><draw:text-box><text:p>Deck title</text:p></draw:text-box></draw:frame>'
                '<draw:frame><draw:text-box><text:p>Body words</text:p></draw:text-box></draw:frame>'
                '<presentation:notes><draw:frame><draw:text-box><text:p>speaker note</text:p></draw:text-box></draw:frame></presentation:notes></draw:page></office:presentation>')
    content = f'<?xml version="1.0"?><office:document-content {ODF_NS}><office:body>{body}</office:body></office:document-content>'
    return zipf(path, {"mimetype": f"application/vnd.oasis.opendocument.{ {'odt': 'text', 'ods': 'spreadsheet', 'odp': 'presentation'}[kind] }",
                       "content.xml": content})


def make_epub(path: Path) -> Path:
    cont = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>My Book</dc:title><dc:creator>Zed</dc:creator></metadata>'
           '<manifest><item id="c1" href="ch1.xhtml"/><item id="c2" href="ch2.xhtml"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    ch = lambda t, b: f'<html><head><title>{t}</title><style>p{{}}</style></head><body><h1>{t}</h1><p>{b}</p><script>evil()</script></body></html>'  # noqa: E731
    return zipf(path, {"mimetype": "application/epub+zip", "META-INF/container.xml": cont, "OEBPS/content.opf": opf,
                       "OEBPS/ch1.xhtml": ch("One", "Call me Ishmael."), "OEBPS/ch2.xhtml": ch("Two", "Second chapter text.")})


# ─── detection ──────────────────────────────────────────────────────────────

def test_detect_kinds(tmp_path):
    cases = {"a.docx": make_docx, "b.xlsx": make_xlsx, "c.pptx": make_pptx}
    for name, fn in cases.items():
        assert ff.detect_kind(fn(tmp_path / name)) == name.split(".")[1]
    assert ff.detect_kind(make_odf(tmp_path / "x.odt", "odt")) == "odt"
    assert ff.detect_kind(make_epub(tmp_path / "x.epub")) == "epub"
    renamed = zipf(tmp_path / "noext", {"word/document.xml": "<a/>"})
    assert ff.detect_kind(renamed) == "docx"                       # sniffed by content
    t = tmp_path / "plain.txt"
    t.write_text("BMW and ID3 tags are just words here")
    assert ff.detect_kind(t) == "text"
    t.write_bytes("BMW".encode() + b"\x00" * 40)
    assert ff.detect_kind(t) == "binary"
    (tmp_path / "x.csv").write_text("a,b\n1,2\n")
    assert ff.detect_kind(tmp_path / "x.csv") == "csv"


# ─── Word, Excel, PowerPoint ────────────────────────────────────────────────

def test_docx_headings_lists_tables(ctx, workspace):
    make_docx(workspace / "r.docx")
    out = read(ctx, path="r.docx")
    assert "# Quarterly Report" in out and "Revenue grew 12%." in out and "- First point" in out
    assert "| Region | Sales |" in out and "| EMEA | 42 |" in out
    assert "title 'Q3'" in out and "creator 'Ana'" in out
    ex = ff.extract(workspace / "r.docx")
    assert [b["t"] for b in ex.meta["blocks"]] == ["h1", "p", "li", "table"]


def test_docx_paging(ctx, workspace):
    body = "".join(f"<w:p><w:r><w:t>line {i}</w:t></w:r></w:p>" for i in range(50))
    make_docx(workspace / "big.docx", body)
    out = read(ctx, path="big.docx", limit=10)
    assert "line 9" in out and "line 10" not in out and "Continue with offset=11" in out
    out2 = read(ctx, path="big.docx", offset=11, limit=10)
    assert "line 10" in out2 and "line 19" in out2 and "line 9\n" not in out2


def test_xlsx_sheets_formulas_dates_paging(ctx, workspace):
    make_xlsx(workspace / "s.xlsx", rows=30)
    out = read(ctx, path="s.xlsx", limit=5)
    assert "Sales" in out and "Notes" in out                       # sheet list
    assert "Name | Total" in out and "Alice" in out                # shared strings, rich text joined
    assert "2023-03-15" in out                                     # date style
    assert "| 3 |" in out or "| 3 " in out                         # formula cached result (SUM(1,2)=3)
    assert "Continue with offset=6" in out and "part=<sheet name>" in out
    page2 = read(ctx, path="s.xlsx", offset=6, limit=5)
    assert page2.count("Alice") == 5 and "\n1 |" not in page2
    notes = read(ctx, path="s.xlsx", part="Notes")
    assert "hello notes" in notes
    bad = read(ctx, path="s.xlsx", part="Nope")
    assert "no sheet named" in bad and "Sales" in bad


def test_pptx_slides_and_notes(ctx, workspace):
    make_pptx(workspace / "d.pptx")
    out = read(ctx, path="d.pptx")
    assert "=== Slide 1: Launch plan ===" in out and "Ship in May" in out and "[Notes] Say hello" in out
    assert "Slide 2: Risks" in out
    only2 = read(ctx, path="d.pptx", pages="2")
    assert "Risks" in only2 and "Launch plan" not in only2
    assert "beyond the end" in read(ctx, path="d.pptx", pages="9")


# ─── OpenDocument, RTF, EPUB ────────────────────────────────────────────────

def test_odf_family(ctx, workspace):
    make_odf(workspace / "a.odt", "odt")
    out = read(ctx, path="a.odt")
    assert "# Heading A" in out and "Para with  spaces" in out and "- bullet one" in out
    make_odf(workspace / "a.ods", "ods")
    out = read(ctx, path="a.ods")                                  # a million repeated empty rows/cols must not hang or bloat
    assert "Item | 7" in out and len(out) < 2000
    make_odf(workspace / "a.odp", "odp")
    out = read(ctx, path="a.odp")
    assert "Slide 1: Deck title" in out and "Body words" in out and "speaker note" in out


def test_rtf_plain_text(ctx, workspace):
    rtf = (r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}{\*\generator Word;}\f0 Hello \b bold\b0  caf\'e9 \u8364? and \u228?x\par Line two\par}")
    (workspace / "a.rtf").write_text(rtf)
    out = read(ctx, path="a.rtf")
    assert "Hello bold café € and äx" in out and "Line two" in out and "Arial" not in out and "Word" not in out


def test_epub_chapters(ctx, workspace):
    make_epub(workspace / "b.epub")
    out = read(ctx, path="b.epub")
    assert "My Book" in out and "Call me Ishmael." in out and "Second chapter text." in out and "evil()" not in out
    ch2 = read(ctx, path="b.epub", pages="2")
    assert "Second chapter" in ch2 and "Ishmael" not in ch2


# ─── text formats ───────────────────────────────────────────────────────────

def test_csv_delimiter_detection(ctx, workspace):
    (workspace / "semi.csv").write_text("name;age\nAnn;30\nBob;41\n")
    out = read(ctx, path="semi.csv")
    assert "semicolon-separated, 2 columns: name, age" in out and "→Ann;30" in out        # raw lines stay editable
    ex = ff.extract(workspace / "semi.csv")
    assert ex.meta["delimiter"] == ";" and ex.meta["tables"][0]["rows"] == [["Ann", "30"], ["Bob", "41"]]
    (workspace / "t.tsv").write_text("a\tb\n1\t2\n")
    assert ff.extract(workspace / "t.tsv").meta["delimiter"] == "\t"
    (workspace / "pipe.csv").write_text("a|b|c\n1|2|3\n4|5|6\n")
    assert ff.extract(workspace / "pipe.csv").meta["delimiter"] == "|"


def test_json_jsonl_toml_yaml_xml_html_svg(ctx, workspace):
    (workspace / "a.json").write_text('{"a": [1, 2], "b": {"c": 1}}')
    assert "valid JSON, object{a, b}" in read(ctx, path="a.json")
    (workspace / "bad.json").write_text('{"a": ')
    assert "invalid JSON" in read(ctx, path="bad.json")
    (workspace / "a.jsonl").write_text('{"x":1}\n{"x":2}\nnot json\n')
    assert "JSON Lines, 3 records, 1 invalid" in read(ctx, path="a.jsonl")
    (workspace / "a.toml").write_text('[tool]\nname = "x"\n')
    assert "valid TOML" in read(ctx, path="a.toml")
    (workspace / "a.yaml").write_text("key: value\nlist:\n  - 1\n")
    assert "key: value" in read(ctx, path="a.yaml")
    ex = ff.extract(workspace / "a.yaml")
    assert ex.ok and "key: value" in ex.text
    (workspace / "a.xml").write_text("<root><a>1</a><b>two</b></root>")
    assert "well-formed XML, root <root>" in read(ctx, path="a.xml")
    ex = ff.extract(workspace / "a.xml")
    assert "<a>1</a>" in ex.text and ex.meta["readable"] == "1 two"
    (workspace / "a.html").write_text("<html><head><title>T</title><script>alert(1)</script></head><body><h1>Hi</h1><p>there <a href='/x'>link</a></p></body></html>")
    ex = ff.extract(workspace / "a.html")
    assert "# Hi" in ex.text and "alert" not in ex.text and ex.meta["title"] == "T" and ex.meta["links"] == 1
    assert "<h1>Hi</h1>" in read(ctx, path="a.html")                       # source is shown, never rendered
    (workspace / "a.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>x()</script><title>Dot</title></svg>')
    ex = ff.extract(workspace / "a.svg")
    assert ex.meta["viewbox"] == "0 0 10 10" and any("never run" in n for n in ex.meta["notes"])


def test_encodings(ctx, workspace):
    (workspace / "u16.txt").write_bytes("héllo wörld\nline2\n".encode("utf-16"))
    assert "héllo wörld" in read(ctx, path="u16.txt")
    (workspace / "u16le.txt").write_bytes("plain words here\nsecond\n".encode("utf-16-le"))        # no BOM
    assert "plain words here" in read(ctx, path="u16le.txt")
    (workspace / "l1.txt").write_bytes("caf\xe9 au lait\n".encode("latin-1"))
    assert "café au lait" in read(ctx, path="l1.txt")


def test_huge_and_long_line_files_page_without_loading(ctx, workspace):
    p = workspace / "big.log"
    with open(p, "wb") as f:
        for i in range(1, 400_000):
            f.write(f"2026-01-01 line number {i:06d} padding padding padding padding\n".encode())     # ~ 24 MB
    assert p.stat().st_size > 20 * 1024 * 1024
    out = read(ctx, path="big.log", offset=300_000, limit=3)
    assert "line number 299999" not in out and "line number 300000" in out and "300002→" in out and "too large to load whole" in out
    one = workspace / "one.json"
    one.write_bytes(b"[" + b"1," * 15_000_000 + b"1]")                      # a 30 MB single line
    out = read(ctx, path="one.json", limit=2)
    assert "line truncated" in out and len(out) < 40_000
    mini = workspace / "mini.js"
    mini.write_text("x" * 50_000 + "\nsecond\n")
    out = read(ctx, path="mini.js")
    assert "line truncated" in out and "second" in out


# ─── notebook ───────────────────────────────────────────────────────────────

def png_bytes(w=3, h=2) -> bytes:
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_notebook_cells_outputs_images(workspace):
    nb = {"nbformat": 4, "metadata": {}, "cells": [
        {"cell_type": "markdown", "source": ["# Title"]},
        {"cell_type": "code", "execution_count": 1, "source": ["print(1)"], "outputs": [
            {"output_type": "stream", "name": "stdout", "text": ["1\n"]},
            {"output_type": "display_data", "data": {"image/png": base64.b64encode(png_bytes()).decode(), "text/plain": "<fig>"}},
            {"output_type": "error", "ename": "ValueError", "evalue": "bad", "traceback": []}]}]}
    (workspace / "n.ipynb").write_text(json.dumps(nb))
    ex = ff.extract(workspace / "n.ipynb")
    assert len(ex.images) == 1 and ex.images[0].data[:4] == b"\x89PNG"
    cells = ex.meta["cells"]
    assert cells[0]["type"] == "markdown" and cells[1]["outputs"][1]["kind"] == "image" and cells[1]["outputs"][2]["text"] == "ValueError: bad"
    (workspace / "broken.ipynb").write_text("{not json")
    assert ff.extract(workspace / "broken.ipynb").meta.get("error")


# ─── SQLite ─────────────────────────────────────────────────────────────────

def make_db(path: Path, rows: int = 25) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, photo BLOB)")
    con.executemany("INSERT INTO people (name, photo) VALUES (?, ?)", [(f"person{i}", b"\x00\x01") for i in range(rows)])
    con.execute('CREATE TABLE "odd ""name" (x)')
    con.commit()
    con.close()
    return path


def test_sqlite_overview_table_paging_readonly(ctx, workspace):
    db = make_db(workspace / "d.sqlite")
    before = db.read_bytes()
    out = read(ctx, path="d.sqlite")
    assert "people" in out and "CREATE TABLE people" in out and "(25 rows)" in out and "person0" in out and "<blob 2 bytes>" in out
    page = read(ctx, path="d.sqlite", part="people", offset=21, limit=3)
    assert "person20" in page and "person22" in page and "person19" not in page
    assert "Continue with offset=24" in page
    assert "no table named" in read(ctx, path="d.sqlite", part="nope")
    assert "odd" in read(ctx, path="d.sqlite", part='odd "name')
    assert db.read_bytes() == before                                       # opened read-only: byte-identical
    ex = ff.extract(db, sheet="people", limit=1)
    assert ex.meta["tables"][0]["total"] == 25
    (workspace / "garbage.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 200)
    assert ff.extract(workspace / "garbage.db").meta.get("error")


# ─── archives ───────────────────────────────────────────────────────────────

def test_zip_listing_member_and_traversal(ctx, workspace):
    zipf(workspace / "a.zip", {"docs/readme.txt": "hello from inside", "bin/x.dat": b"\x00\x01\x02" * 100, "../evil.txt": "no"})
    out = read(ctx, path="a.zip")
    assert "docs/readme.txt" in out and "unsafe path" in out and "Zip archive, 3 entries" in out
    assert "hello from inside" in read(ctx, path="a.zip", part="docs/readme.txt")
    assert "hex" in read(ctx, path="a.zip", part="bin/x.dat").lower() or "binary" in read(ctx, path="a.zip", part="bin/x.dat")
    for bad in ("../evil.txt", "/etc/passwd", "a/../../x", "C:\\x"):
        assert "refused" in read(ctx, path="a.zip", part=bad)
    assert "not in the archive" in read(ctx, path="a.zip", part="missing.txt")
    assert not (workspace.parent / "evil.txt").exists()


def test_zip_bomb_is_listed_never_expanded(ctx, workspace):
    p = workspace / "bomb.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\x00" * (300 * 1024 * 1024))
    assert p.stat().st_size < 1_000_000
    out = read(ctx, path="bomb.zip")
    assert "possible zip bomb" in out and "zeros.bin" in out
    assert "larger than" in read(ctx, path="bomb.zip", part="zeros.bin") or "members over" in read(ctx, path="bomb.zip", part="zeros.bin")
    # a bomb disguised as a docx part is refused by the size cap, not expanded
    z2 = zipf(workspace / "bomb.docx", {"word/document.xml": "<w:document xmlns:w='%s'><w:body>" % W + "<w:p/>" * 10 + "</w:body></w:document>" + " " * (40 * 1024 * 1024)})
    out = read(ctx, path="bomb.docx")
    assert "Cannot read" in out and "limit" in out


def test_tar_family(ctx, workspace):
    for name, mode in (("a.tar", "w"), ("a.tar.gz", "w:gz"), ("a.tgz", "w:gz")):
        with tarfile.open(workspace / name, mode) as tf:
            data = b"inner text\n"
            ti = tarfile.TarInfo("d/f.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
            link = tarfile.TarInfo("d/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        out = read(ctx, path=name)
        assert "d/f.txt" in out and "Links are shown but never followed" in out, name
        assert "inner text" in read(ctx, path=name, part="d/f.txt")
        assert "not a regular file" in read(ctx, path=name, part="d/link")
        assert "refused" in read(ctx, path=name, part="../x")


def test_gzip_bomb_tar_stops(workspace):
    class Zeros(io.RawIOBase):
        def __init__(self, n): self.left = n
        def readable(self): return True
        def readinto(self, b):
            n = min(len(b), self.left)
            b[:n] = bytes(n)
            self.left -= n
            return n
    size = 600 * 1024 * 1024
    p = workspace / "bomb.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        ti = tarfile.TarInfo("big.bin")
        ti.size = size
        tf.addfile(ti, Zeros(size))
    assert p.stat().st_size < 3_000_000
    ex = ff.extract(p)
    assert ex.kind == "tar" and any("expands to more than" in n for n in ex.meta["notes"]) and ex.meta["truncated"]
    assert "members over 5 MB" in ff.extract(p, sheet="big.bin").text


def test_plain_gzip(ctx, workspace):
    with gzip.open(workspace / "notes.txt.gz", "wb") as g:
        g.write(b"compressed words\n")
    out = read(ctx, path="notes.txt.gz")
    assert "compressed words" in out and "decompresses to notes.txt" in out


# ─── safety: XML entities, malformed, encrypted, credentials ────────────────

def test_xml_entities_and_doctype_refused(ctx, workspace):
    laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
              f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
    zipf(workspace / "laugh.docx", {"word/document.xml": laughs})
    out = read(ctx, path="laugh.docx")
    assert "Cannot read" in out and "DOCTYPE" in out
    xxe = f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&e;</w:t></w:r></w:p></w:body></w:document>'
    zipf(workspace / "xxe.docx", {"word/document.xml": xxe})
    out = read(ctx, path="xxe.docx")
    assert "root:" not in out and "DOCTYPE" in out
    # a UTF-16 or late DOCTYPE is caught by the parser's own handlers
    late = '<?xml version="1.0"?>' + "<!-- c -->" * 8000 + '<!DOCTYPE a [<!ENTITY q "zz">]><a>&q;</a>'
    with pytest.raises(ff.FormatError):
        ff.parse_xml(late.encode())
    (workspace / "e.xml").write_text('<!DOCTYPE a [<!ENTITY q "zz">]><a>&q;</a>')
    assert "DOCTYPE" in read(ctx, path="e.xml")


def test_malformed_and_encrypted_files_never_crash(ctx, workspace):
    (workspace / "trunc.docx").write_bytes(make_docx(workspace / "ok.docx").read_bytes()[:150])
    assert "Cannot read" in read(ctx, path="trunc.docx")
    (workspace / "notzip.xlsx").write_bytes(b"PK\x03\x04" + b"junk" * 50)
    assert "Cannot read" in read(ctx, path="notzip.xlsx")
    zipf(workspace / "empty.docx", {"readme": "x"})
    assert "missing" in read(ctx, path="empty.docx")
    zipf(workspace / "badxml.pptx", {"ppt/presentation.xml": "<a><b></a>"})
    assert "malformed XML" in read(ctx, path="badxml.pptx")
    (workspace / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    out = read(ctx, path="old.doc")
    assert "legacy binary Office" in out or "Cannot read" in out
    (workspace / "deep.docx").write_bytes(zipf(workspace / "d0.docx", {"word/document.xml": f'<w:document xmlns:w="{W}"><w:body>' + "<w:sdt><w:sdtContent>" * 5000 + "</w:sdtContent></w:sdt>" * 5000 + "</w:body></w:document>"}).read_bytes())
    assert isinstance(read(ctx, path="deep.docx"), str)                     # deep nesting: a message, not a crash
    enc = workspace / "enc.docx"
    with zipfile.ZipFile(enc, "w") as z:
        z.writestr("word/document.xml", "<x/>")
    raw = bytearray(enc.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):             # set the "encrypted" flag bit in local and central headers
        i = raw.find(sig)
        raw[i + off] |= 1
    enc.write_bytes(bytes(raw))
    assert "password-protected" in read(ctx, path="enc.docx")


def test_credentials_are_still_refused(ctx, workspace):
    from axon.errors import ToolError
    ssh = workspace / ".ssh"
    ssh.mkdir()
    zipf(ssh / "keys.zip", {"a": "b"})
    with pytest.raises(ToolError):
        read(ctx, path=".ssh/keys.zip")


# ─── images, media, fonts, binaries ─────────────────────────────────────────

def test_image_info_formats():
    assert ff.image_info(png_bytes(7, 5)) == {"format": "PNG", "width": 7, "height": 5, "alpha": False}
    assert ff.image_info(b"GIF89a" + struct.pack("<HH", 9, 4) + b"\x00" * 20)["width"] == 9
    bmp = b"BM" + b"\x00" * 16 + struct.pack("<ii", 30, -20) + b"\x00" * 10
    assert (ff.image_info(bmp)["width"], ff.image_info(bmp)["height"]) == (30, 20)
    ico = b"\x00\x00\x01\x00\x01\x00" + bytes([16, 32]) + b"\x00" * 10
    assert (ff.image_info(ico)["width"], ff.image_info(ico)["height"]) == (16, 32)
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 2) + struct.pack("<HHII", 256, 4, 1, 640) + struct.pack("<HHII", 257, 4, 1, 480)
    assert (ff.image_info(tiff)["width"], ff.image_info(tiff)["height"]) == (640, 480)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8 + b"ispe\x00\x00\x00\x00" + struct.pack(">II", 4032, 3024)
    assert ff.image_info(heic)["width"] == 4032
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x0a\x00\x00\x00" + b"\x00" * 4 + (99).to_bytes(3, "little") + (49).to_bytes(3, "little")
    assert (ff.image_info(webp)["width"], ff.image_info(webp)["height"]) == (100, 50)
    assert ff.image_info(b"garbage") == {}


class _Provider:
    def __init__(self, yes: bool) -> None:
        self.yes = yes

    def supports(self, cap: str) -> bool:
        return self.yes and cap == "tool_images"


class _Agent:
    def __init__(self, yes: bool) -> None:
        self.provider = _Provider(yes)


def test_read_image_attach_and_text_fallback(ctx, workspace):
    (workspace / "p.png").write_bytes(png_bytes(4, 3))
    ctx.agent = _Agent(True)
    out = read(ctx, path="p.png")
    assert "PNG, 4x3" in out and "attached" in out and len(ctx.images) == 1
    ctx.images.clear()
    ctx.agent = _Agent(False)
    out = read(ctx, path="p.png")
    assert "PNG, 4x3" in out and not ctx.images


@pytest.mark.skipif(not shutil.which("sips"), reason="macOS sips not available")
def test_unsupported_image_is_converted_with_sips(ctx, workspace):
    (workspace / "src.png").write_bytes(png_bytes(8, 6))
    import subprocess
    subprocess.run(["sips", "-s", "format", "bmp", str(workspace / "src.png"), "--out", str(workspace / "pic.bmp")], capture_output=True, check=True)
    ctx.agent = _Agent(True)
    out = read(ctx, path="pic.bmp")
    assert "BMP, 8x6" in out and "converted from BMP" in out and len(ctx.images) == 1
    assert ctx.images[0].media_type in ("image/png", "image/jpeg") and Path(ctx.images[0].path).exists()


def test_unsupported_image_without_converter_says_so(ctx, workspace, monkeypatch):
    (workspace / "pic.bmp").write_bytes(b"BM" + b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 8 + struct.pack("<ii", 8, 6) + b"\x00" * 40)
    monkeypatch.setattr(ff.shutil, "which", lambda n: None)
    ctx.agent = _Agent(True)
    out = read(ctx, path="pic.bmp")
    assert "BMP" in out and "not a format the model takes" in out and not ctx.images


def test_wav_and_media_without_ffprobe(ctx, workspace, monkeypatch):
    rate, secs = 8000, 2
    pcm = b"\x00\x00" * rate * secs
    wav = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm
    (workspace / "s.wav").write_bytes(wav)
    monkeypatch.setattr(ff.shutil, "which", lambda n: None)
    out = read(ctx, path="s.wav")
    assert "Audio file: s.wav" in out and "Duration: 0:02" in out and "8000 Hz" in out
    (workspace / "v.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    out = read(ctx, path="v.mp4")
    assert "Video file: v.mp4" in out and "ffprobe" in out


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
def test_wav_with_ffprobe(workspace):
    rate = 8000
    pcm = b"\x00\x00" * rate
    (workspace / "s.wav").write_bytes(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm)) + pcm)
    ex = ff.extract(workspace / "s.wav")
    assert "Duration: 0:01" in ex.text and "pcm_s16le" in ex.text


def test_font_names(ctx, workspace):
    def font(family: str) -> bytes:
        name = family.encode("utf-16-be")
        rec = struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(name), 0)
        table = struct.pack(">HHH", 0, 1, 6 + 12) + rec + name
        header = b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 16, 0, 0)
        entry = b"name" + struct.pack(">III", 0, 12 + 16, len(table))
        return header + entry + table
    (workspace / "f.ttf").write_bytes(font("Inter Display"))
    out = read(ctx, path="f.ttf")
    assert "Font file: f.ttf" in out and "Family: Inter Display" in out
    (workspace / "broken.otf").write_bytes(b"OTTO" + b"\x00" * 30)
    assert "could not be read" in read(ctx, path="broken.otf")


def test_unknown_binary_hex_preview(ctx, workspace):
    (workspace / "blob.bin").write_bytes(b"\x7fELF" + bytes(range(256)) * 40)
    out = read(ctx, path="blob.bin")
    assert "ELF executable" in out and "10,244 bytes" in out and "00000000  7f 45 4c 46" in out and "|.ELF" in out
    assert out.count("\n") < 300                                          # only the first 4 KB
    assert "00001000" not in out


def test_text_and_code_unchanged(ctx, workspace):
    (workspace / "a.py").write_bytes(b"print('hi')\nx = 1\n")        # LF on every platform (write_text would write CRLF on Windows)
    out = read(ctx, path="a.py")
    assert out == "     1→print('hi')\n     2→x = 1"
    (workspace / "empty.docx").write_bytes(b"")
    assert "empty" in read(ctx, path="empty.docx")


def test_read_description_stays_short():
    tool = create_default_registry().get("Read")
    assert len(tool.description) < 850 and len(json.dumps(tool.schema)) < 1300


def test_web_viewer_extraction_matches_read(workspace):
    from axon.features import dash_fs
    make_xlsx(workspace / "s.xlsx", rows=4)
    d = dash_fs.read_file(workspace, [], "s.xlsx")
    assert d["extractable"] and d["kind"] == "xlsx"
    ex = dash_fs.extract_file(workspace, [], "s.xlsx", sheet="Sales")
    assert ex["kind"] == "xlsx" and ex["meta"]["sheets"] == ["Sales", "Notes"] and ex["meta"]["tables"][0]["rows"][0][:2] == ["Name", "Total"]
    make_db(workspace / "d.db")
    assert dash_fs.read_file(workspace, [], "d.db")["extractable"]
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    assert dash_fs.extract_file(workspace, [], "blob.bin")["kind"] == "binary"
    (workspace / "id_rsa.pem").write_bytes(b"\x00\x01")
    from axon.features.dash_common import DashError
    with pytest.raises(DashError):
        dash_fs.extract_file(workspace, [], "id_rsa.pem")
    (workspace / "a.csv").write_text("a;b\n1;2\n")
    assert dash_fs.read_file(workspace, [], "a.csv")["delimiter"] == ";"


def test_web_secrets_in_extracted_text_are_masked(workspace):
    from axon.features import dash_fs
    make_docx(workspace / "k.docx", '<w:p><w:r><w:t>API_KEY=sk-live-abcdefghijklmnop123456</w:t></w:r></w:p>')
    ex = dash_fs.extract_file(workspace, [], "k.docx")
    assert "abcdefghijklmnop123456" not in json.dumps(ex)


# ─── the web route ──────────────────────────────────────────────────────────

def test_web_route_and_client_script(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from axon.features.dash_js_files import JS_FILES
    for needle in ("function extractedView", "function textFormatView", "f.extractable", "/api/file/extract", "Ask Axon", "function archiveTree"):
        assert needle in JS_FILES, needle
    assert JS_FILES.count("innerHTML") == 1                              # the new views build text nodes only
    node = shutil.which("node")
    if node:
        import subprocess
        p = tmp_path / "f.js"
        p.write_text(JS_FILES, encoding="utf-8")
        assert subprocess.run([node, "--check", str(p)], capture_output=True, text=True, encoding="utf-8").returncode == 0


def test_web_route_serves_extraction(c, env):
    make_xlsx(env.ws / "s.xlsx", rows=3)
    d = c.json("/api/file/extract?path=s.xlsx&sheet=Notes")
    assert d["kind"] == "xlsx" and d["meta"]["tables"][0]["rows"] == [["hello notes"]]
    (env.ws / "id_ed25519").write_bytes(b"\x00secret")
    assert c.get("/api/file/extract?path=id_ed25519")[0] in (403, 404)
    assert c.get("/api/file/extract?path=../../etc/hosts")[0] in (400, 403, 404)


def _docx(path, text):
    import zipfile
    body = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", body)


def test_mention_of_a_word_file_gives_its_text_not_bytes(tmp_path):
    from axon.cli import expand_file_mentions
    _docx(tmp_path / "report.docx", "Quarterly numbers")
    out = expand_file_mentions("see @report.docx", tmp_path)
    assert "Quarterly numbers" in out and "PK" not in out and "�" not in out


def test_mention_of_a_binary_never_inlines_raw_bytes(tmp_path):
    from axon.cli import expand_file_mentions
    (tmp_path / "blob.bin").write_bytes(bytes(range(256)) * 20)
    assert "�" not in expand_file_mentions("see @blob.bin", tmp_path)


def test_mention_of_a_credential_file_is_refused(tmp_path):
    from axon.cli import expand_file_mentions
    (tmp_path / ".env").write_text("API_KEY=sk-secret-value\n", encoding="utf-8")
    out = expand_file_mentions("see @.env", tmp_path)
    assert "sk-secret-value" not in out and "protected" in out


def test_mention_of_plain_text_is_unchanged(tmp_path):
    from axon.cli import expand_file_mentions
    (tmp_path / "notes.txt").write_text("hello world", encoding="utf-8")
    out = expand_file_mentions("see @notes.txt", tmp_path)
    assert "[File: notes.txt]" in out and "hello world" in out


def test_toml_falls_back_to_tomli_on_python_310(ctx, workspace, monkeypatch):
    """Python 3.10 has no tomllib; the tomli backport must be used instead of reporting the file as invalid."""
    import sys
    try:
        import tomllib as real           # 3.11+
    except ModuleNotFoundError:
        import tomli as real             # 3.10: the declared dependency (pyproject: tomli; python_version < '3.11')
    monkeypatch.setitem(sys.modules, "tomllib", None)      # `import tomllib` now raises ModuleNotFoundError
    monkeypatch.setitem(sys.modules, "tomli", real)        # what the backport provides
    (workspace / "a.toml").write_text('[tool]\nname = "x"\n')
    assert "valid TOML" in read(ctx, path="a.toml")
    assert "Not valid TOML" not in read(ctx, path="a.toml")
