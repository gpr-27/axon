"""
One extraction layer for every common file type, shared by the Read tool and the web viewer.

    extract(path, pages=None, sheet=None, limit=2000, offset=0) -> Extracted(kind, text, meta, images)

Standard library only. Optional helpers (pdftotext/pypdf, ffprobe, macOS sips/textutil) are used when present and
degrade to a clear message when not. Nothing here executes file content, extracts archives to disk, or resolves XML
entities: DOCTYPE and entity declarations are refused (no XXE, no billion laughs), every read is size-capped, and
corrupt, truncated or password-protected files produce a message in ``Extracted.text`` instead of an exception.

``meta`` carries structure for the web viewer: ``blocks`` (documents), ``sheets``/``tables`` (spreadsheets, SQLite,
CSV), ``slides``, ``cells`` (notebooks), ``entries`` (archives), plus ``notes`` and ``total``/``next`` for paging.
"""
from __future__ import annotations

import base64
import bz2
import codecs
import csv
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
from xml.etree import ElementTree as ET

DEFAULT_LIMIT = 2000
MAX_PART_BYTES = 32 * 1024 * 1024        # one XML part inside an Office/ODF/EPUB package, after decompression
MAX_STREAM_BYTES = 400 * 1024 * 1024     # streamed sheet XML or tar decompression, after decompression
MAX_ENTRIES = 5000                       # archive entries listed
MAX_MEMBER_BYTES = 5 * 1024 * 1024       # one archive member read by path
MAX_BLOCKS = 20000
MAX_CELL_CHARS = 2000
MAX_COLS = 200
HEX_BYTES = 4096
BOMB_RATIO = 1000
BOMB_TOTAL = 4 * 1024 ** 3

OFFICE_KINDS = {"docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"}
ARCHIVE_KINDS = {"zip", "tar", "gzip"}
EXTRACT_KINDS = OFFICE_KINDS | ARCHIVE_KINDS | {"rtf", "sqlite", "audio", "video", "font", "legacy_office", "binary", "image"}
TEXT_FORMAT_KINDS = {"csv", "json", "jsonl", "yaml", "toml", "xml", "html", "svg", "log", "ipynb"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".heif", ".avif"}
MODEL_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".aiff", ".aif", ".mid", ".midi"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".ogv", ".3gp"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2", ".ttc", ".eot"}
_ZIP_KIND_BY_EXT = {".docx": "docx", ".docm": "docx", ".dotx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx",
                    ".pptm": "pptx", ".odt": "odt", ".ods": "ods", ".odp": "odp", ".epub": "epub"}
_TEXT_KIND_BY_EXT = {".csv": "csv", ".tsv": "csv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".yaml": "yaml",
                     ".yml": "yaml", ".toml": "toml", ".xml": "xml", ".xsd": "xml", ".plist": "xml", ".html": "html", ".htm": "html",
                     ".xhtml": "html", ".svg": "svg", ".log": "log", ".ipynb": "ipynb", ".rtf": "rtf"}


class FormatError(Exception):
    """A file that cannot be read, with a message fit to show a person."""


@dataclass
class ImageData:
    mime: str
    data: bytes
    label: str = ""


@dataclass
class Extracted:
    kind: str
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    images: list[ImageData] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.meta.get("error")


# ─── detection ──────────────────────────────────────────────────────────────

def sniff_utf16(data: bytes) -> str | None:
    """'utf-16-le' / 'utf-16-be' for BOM-less UTF-16 text (NUL in every other byte), else None."""
    s = data[:4096]
    if len(s) < 8 or b"\x00" not in s:
        return None
    even, odd = s[0::2].count(0), s[1::2].count(0)
    n = len(s) // 2
    if odd > n * 0.4 and even < n * 0.05:
        return "utf-16-le"
    if even > n * 0.4 and odd < n * 0.05:
        return "utf-16-be"
    return None


def decode_text(data: bytes) -> tuple[str, str]:
    """(text, encoding): UTF-8, UTF-16/32 with BOM, BOM-less UTF-16, then cp1252, then latin-1. Never raises."""
    from axon.tools.textio import decode_bytes
    if not data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        enc = sniff_utf16(data)
        if enc:
            return data.decode(enc, errors="replace"), enc
    return decode_bytes(data)


def _magic_kind(head: bytes) -> str:
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:16] == b"SQLite format 3\x00":
        return "sqlite"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[:3] == b"BZh":
        return "bzip2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if len(head) > 262 and head[257:262] == b"ustar":
        return "tar"
    if head[:5] == b"{\\rtf":
        return "rtf"
    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" or head[:6] in (b"GIF87a", b"GIF89a") \
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP") or (head[:2] == b"BM" and len(head) >= 18 and head[6:10] == b"\x00\x00\x00\x00" and int.from_bytes(head[14:18], "little") in (12, 40, 52, 56, 64, 108, 124)) \
            or head[:4] == b"\x00\x00\x01\x00" or head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis", b"heim", b"heis"):
            return "image"
        return "video" if brand not in (b"M4A ", b"M4B ", b"M4P ") else "audio"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE" or head[:4] in (b"fLaC", b"OggS") or (head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC")) or head[:3] == b"ID3" \
            or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI " or head[:4] == b"\x1aE\xdf\xa3":
        return "video"
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"wOFF", b"wOF2", b"ttcf", b"true"):
        return "font"
    return ""


def detect_kind(path: Path, head: bytes | None = None) -> str:
    """docx, xlsx, pptx, odt, ods, odp, epub, rtf, sqlite, zip, tar, gzip, pdf, image, audio, video, font, legacy_office,
    csv/json/jsonl/yaml/toml/xml/html/svg/log/ipynb, text, or binary."""
    ext = path.suffix.lower()
    if head is None:
        try:
            with open(path, "rb") as f:
                head = f.read(512)
        except OSError:
            head = b""
    magic = _magic_kind(head)
    name = path.name.lower()
    if magic == "pdf" or ext == ".pdf":
        return "pdf"
    if magic == "sqlite":
        return "sqlite"
    if magic == "ole":
        return "legacy_office" if ext in (".doc", ".xls", ".ppt", ".msg", ".dot", ".xlt", ".pps") or ext in _ZIP_KIND_BY_EXT else "binary"
    if magic == "zip":
        if ext in _ZIP_KIND_BY_EXT:
            return _ZIP_KIND_BY_EXT[ext]
        return _sniff_zip_kind(path)
    if magic in ("gzip", "bzip2", "xz") or name.endswith((".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")):
        if name.endswith((".tar.gz", ".tgz", ".tar", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")) or magic == "tar":
            return "tar"
        return "gzip" if magic in ("gzip", "bzip2", "xz") else "text"
    if magic == "tar":
        return "tar"
    if magic == "rtf":
        return "rtf"
    if magic == "image" or ext in IMAGE_EXTS:
        return "image"
    if ext in FONT_EXTS:
        return "font"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if magic in ("audio", "video"):
        from axon.tools.textio import is_binary_bytes as _isb
        if _isb(head):
            return magic
    if ext in _TEXT_KIND_BY_EXT:
        return _TEXT_KIND_BY_EXT[ext]
    if ext in (".doc", ".xls", ".ppt"):
        return "legacy_office"
    from axon.tools.textio import is_binary_bytes
    if is_binary_bytes(head) and not sniff_utf16(head):
        return "binary"
    return "text"


def _sniff_zip_kind(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist()[:4000])
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptx"
            if "META-INF/container.xml" in names and "mimetype" in names:
                return "epub"
            if "mimetype" in names and "content.xml" in names:
                mt = zf.read("mimetype")[:100]
                for key, kind in ((b"text", "odt"), (b"spreadsheet", "ods"), (b"presentation", "odp")):
                    if key in mt:
                        return kind
    except Exception:
        pass
    return "zip"


# ─── safe XML and zip helpers ───────────────────────────────────────────────

def _refuse(*_a: Any, **_k: Any) -> None:
    raise FormatError("XML with a DOCTYPE or entity declarations is refused (it could expand into huge text or read files)")


def _stream_xml(src: Any, events: tuple[str, ...] = ("end",), drop: tuple[str, ...] = (), keep: bool = False) -> Iterator[tuple[str, ET.Element]]:
    """Parse XML with pyexpat directly so DOCTYPE and entity declarations are refused whatever the encoding, yielding
    ElementTree elements as they start/end. Elements named in *drop* are detached from their parent after their end event."""
    from xml.parsers import expat
    import collections
    out: collections.deque[tuple[str, ET.Element, ET.Element | None]] = collections.deque()
    stack: list[ET.Element] = []
    root: list[ET.Element] = []
    want_start, want_end = "start" in events, "end" in events

    def conv(n: str) -> str:
        return "{" + n if "}" in n else n

    def start(name: str, attrs: dict[str, str]) -> None:
        el = ET.Element(conv(name), {conv(k): v for k, v in attrs.items()} if attrs else {})
        if stack:
            stack[-1].append(el)
        elif not root:
            root.append(el)
        stack.append(el)
        if want_start:
            out.append(("start", el, None))

    def end(_name: str) -> None:
        el = stack.pop()
        par = stack[-1] if stack else None
        if want_end:
            out.append(("end", el, par))
        elif drop and _ln(el.tag) in drop:
            out.append(("drop", el, par))

    def data(text: str) -> None:
        if not stack:
            return
        top = stack[-1]
        if len(top):
            top[-1].tail = (top[-1].tail or "") + text
        else:
            top.text = (top.text or "") + text

    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    try:
        parser.SetParamEntityParsing(0)
    except Exception:
        pass

    def drain() -> Iterator[tuple[str, ET.Element]]:
        while out:
            ev, el, par = out.popleft()
            if ev != "drop":
                yield ev, el
            if drop and par is not None and ev in ("end", "drop") and _ln(el.tag) in drop:
                try:
                    par.remove(el)
                except ValueError:
                    pass
    try:
        while True:
            chunk = src.read(65536)
            parser.Parse(chunk, not chunk)
            yield from drain()
            if not chunk:
                break
    except expat.ExpatError as e:
        raise FormatError(f"malformed XML ({expat.ErrorString(e.code)} at line {e.lineno})") from None
    if keep and root:
        out.append(("root", root[0], None))
        yield from drain()


def parse_xml(data: bytes) -> ET.Element:
    """Parse XML that has no DOCTYPE and no entity declarations. Raises FormatError for anything else."""
    for _ev, el in _stream_xml(io.BytesIO(data), events=("root",), keep=True):
        return el
    raise FormatError("malformed XML (no root element)")


def _ln(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class _Capped:
    """File-like reader that stops after *cap* bytes (decompressed), so a bomb cannot run away."""

    def __init__(self, f: Any, cap: int, what: str = "this file") -> None:
        self.f, self.cap, self.n, self.what = f, cap, 0, what

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = 1 << 20
        data = self.f.read(n)
        self.n += len(data)
        if self.n > self.cap:
            raise FormatError(f"{self.what} expands to more than {self.cap // 1048576} MB; stopped")
        return data


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise FormatError("not a valid zip package (corrupt or truncated)") from None
    except OSError as e:
        raise FormatError(f"cannot open: {e}") from None
    if len(zf.infolist()) > 100_000:
        zf.close()
        raise FormatError("package has more than 100,000 entries; refused")
    return zf


def _zread(zf: zipfile.ZipFile, name: str, cap: int = MAX_PART_BYTES) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise FormatError(f"part {name!r} is missing; the file is incomplete or not this format") from None
    if info.flag_bits & 0x1:
        raise FormatError("the file is password-protected (encrypted)")
    if info.file_size > cap:
        raise FormatError(f"part {name!r} is {info.file_size // 1048576} MB uncompressed; larger than the {cap // 1048576} MB limit")
    try:
        with zf.open(info) as f:
            data = f.read(cap + 1)
    except (zipfile.BadZipFile, zlib.error, OSError, NotImplementedError, RuntimeError) as e:
        raise FormatError(f"part {name!r} is damaged ({e})") from None
    if len(data) > cap:
        raise FormatError(f"part {name!r} expands past the {cap // 1048576} MB limit")
    return data


def _zxml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return parse_xml(_zread(zf, name))


def _zxml_opt(zf: zipfile.ZipFile, name: str) -> ET.Element | None:
    try:
        zf.getinfo(name)
    except KeyError:
        return None
    try:
        return _zxml(zf, name)
    except FormatError:
        return None


def _iterparse(zf: zipfile.ZipFile, name: str, events: tuple[str, ...] = ("end",), drop: tuple[str, ...] = ()) -> Iterator[tuple[str, ET.Element]]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise FormatError(f"part {name!r} is missing; the file is incomplete or not this format") from None
    if info.flag_bits & 0x1:
        raise FormatError("the file is password-protected (encrypted)")
    try:
        with zf.open(info) as f:
            yield from _stream_xml(_Capped(f, MAX_STREAM_BYTES, name), events, drop)
    except (zipfile.BadZipFile, zlib.error, OSError) as e:
        raise FormatError(f"part {name!r} is damaged ({e})") from None


def _rels(zf: zipfile.ZipFile, rels_name: str, base: str) -> dict[str, str]:
    root = _zxml_opt(zf, rels_name)
    out: dict[str, str] = {}
    if root is None:
        return out
    for r in root:
        rid, target = r.get("Id"), r.get("Target") or ""
        if not rid or r.get("TargetMode") == "External":
            continue
        out[rid] = _join(base, target)
    return out


def _join(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts = [p for p in base.split("/") if p]
    for seg in target.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def _cap(s: str, n: int = MAX_CELL_CHARS) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _human(n: int) -> str:
    x = float(n)
    for u in ("bytes", "KB", "MB", "GB"):
        if x < 1024 or u == "GB":
            return f"{int(x)} bytes" if u == "bytes" else f"{x:.1f} {u}"
        x /= 1024
    return f"{n} bytes"


# ─── blocks → text ──────────────────────────────────────────────────────────

def _table_text(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[c.replace("\n", " ").replace("|", "\\|") for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(lines)


def blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for b in blocks:
        t = b.get("t", "p")
        if t.startswith("h") and t[1:].isdigit():
            out.append("")
            out.append("#" * min(6, int(t[1:])) + " " + b.get("text", ""))
        elif t == "li":
            out.append("  " * int(b.get("level", 0)) + "- " + b.get("text", ""))
        elif t == "table":
            out.append("")
            out.append(_table_text(b.get("rows", [])))
            if b.get("more"):
                out.append(f"[... table continues: {b['more']} more row(s) not shown]")
            out.append("")
        elif t == "rule":
            out.append("---")
        else:
            out.append(b.get("text", ""))
    return "\n".join(out).strip("\n")


def _page_blocks(blocks: list[dict[str, Any]], offset: int, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    total = len(blocks)
    window = blocks[offset:offset + limit]
    meta: dict[str, Any] = {"total": total, "offset": offset, "unit": "block"}
    if offset + limit < total:
        meta["next"] = offset + limit + 1
    return window, meta


def _doc_result(kind: str, blocks: list[dict[str, Any]], offset: int, limit: int, meta: dict[str, Any]) -> Extracted:
    window, pm = _page_blocks(blocks, offset, limit)
    meta.update(pm)
    meta["blocks"] = window
    text = blocks_to_text(window) if window else "(no text found in this document)"
    return Extracted(kind, text, meta)


# ─── Word (.docx) ───────────────────────────────────────────────────────────

def _w_par_text(p: ET.Element) -> str:
    out: list[str] = []

    def go(el: ET.Element) -> None:
        for ch in el:
            n = _ln(ch.tag)
            if n == "t":
                out.append(ch.text or "")
            elif n == "tab":
                out.append("\t")
            elif n in ("br", "cr"):
                out.append("\n")
            elif n == "noBreakHyphen":
                out.append("-")
            elif n in ("Fallback", "pPr", "rPr", "instrText", "delText", "del"):
                continue
            else:
                go(ch)
    go(p)
    return "".join(out)


def _w_cell_text(tc: ET.Element) -> str:
    parts: list[str] = []
    for el in tc.iter():
        if _ln(el.tag) == "p":
            t = _w_par_text(el).strip()
            if t:
                parts.append(t)
    return _cap(" / ".join(parts))


def _docx_blocks(zf: zipfile.ZipFile) -> tuple[list[dict[str, Any]], bool]:
    styles: dict[str, str] = {}
    sroot = _zxml_opt(zf, "word/styles.xml")
    if sroot is not None:
        for st in sroot.iter():
            if _ln(st.tag) == "style":
                sid = ""
                for k, v in st.attrib.items():
                    if _ln(k) == "styleId":
                        sid = v
                for c in st:
                    if _ln(c.tag) == "name":
                        for k, v in c.attrib.items():
                            if _ln(k) == "val" and sid:
                                styles[sid] = v
    root = _zxml(zf, "word/document.xml")
    blocks: list[dict[str, Any]] = []
    truncated = False

    def heading(p: ET.Element) -> tuple[int, bool, int]:
        lvl, listed, ilvl = 0, False, 0
        for ppr in p:
            if _ln(ppr.tag) != "pPr":
                continue
            for c in ppr:
                n = _ln(c.tag)
                if n == "pStyle":
                    sid = next((v for k, v in c.attrib.items() if _ln(k) == "val"), "")
                    name = styles.get(sid, sid).lower()
                    m = re.match(r"heading\s*(\d)", name)
                    if m:
                        lvl = int(m.group(1))
                    elif name in ("title", "subtitle"):
                        lvl = 1 if name == "title" else 2
                    elif "list" in name:
                        listed = True
                elif n == "numPr":
                    listed = True
                    for x in c:
                        if _ln(x.tag) == "ilvl":
                            try:
                                ilvl = int(next((v for k, v in x.attrib.items() if _ln(k) == "val"), "0"))
                            except ValueError:
                                ilvl = 0
        return lvl, listed, ilvl

    def walk(el: ET.Element) -> None:
        nonlocal truncated
        for ch in el:
            if len(blocks) >= MAX_BLOCKS:
                truncated = True
                return
            n = _ln(ch.tag)
            if n == "p":
                text = _w_par_text(ch)
                if not text.strip():
                    continue
                lvl, listed, ilvl = heading(ch)
                if lvl:
                    blocks.append({"t": f"h{min(lvl, 6)}", "text": text.strip()})
                elif listed:
                    blocks.append({"t": "li", "text": text.strip(), "level": ilvl})
                else:
                    blocks.append({"t": "p", "text": text})
            elif n == "tbl":
                rows: list[list[str]] = []
                more = 0
                for tr in ch.iter():
                    if _ln(tr.tag) == "tr" and _row_belongs(ch, tr):
                        if len(rows) >= 500:
                            more += 1
                            continue
                        rows.append([_w_cell_text(tc) for tc in tr if _ln(tc.tag) == "tc"][:MAX_COLS])
                blocks.append({"t": "table", "rows": rows, "more": more})
            elif n in ("sdt", "sdtContent", "ins", "customXml", "smartTag", "body", "AlternateContent", "Choice"):
                walk(ch)
    walk(root)
    return blocks, truncated


def _row_belongs(tbl: ET.Element, tr: ET.Element) -> bool:
    return tr in list(tbl)


def _props(zf: zipfile.ZipFile) -> dict[str, str]:
    out: dict[str, str] = {}
    root = _zxml_opt(zf, "docProps/core.xml")
    if root is not None:
        for el in root:
            n = _ln(el.tag)
            if n in ("title", "creator", "subject", "modified", "created") and (el.text or "").strip():
                out[n] = el.text.strip()[:200]
    return out


def _extract_docx(path: Path, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        blocks, truncated = _docx_blocks(zf)
        meta: dict[str, Any] = {"props": _props(zf)}
    if truncated:
        meta["notes"] = [f"Only the first {MAX_BLOCKS:,} blocks were read."]
    return _doc_result("docx", blocks, offset, limit, meta)


# ─── Excel (.xlsx) ──────────────────────────────────────────────────────────

_COL = re.compile(r"([A-Za-z]+)(\d*)")


def _col_index(ref: str) -> int:
    m = _COL.match(ref or "")
    n = 0
    for ch in (m.group(1) if m else "").upper():
        n = n * 26 + ord(ch) - 64
    return max(0, n - 1)


def col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _si_text(si: ET.Element) -> str:
    out: list[str] = []
    for ch in si:
        n = _ln(ch.tag)
        if n == "t":
            out.append(ch.text or "")
        elif n == "r":
            out.extend(t.text or "" for t in ch if _ln(t.tag) == "t")
    return "".join(out)


_DATE_FMT = re.compile(r"[ymdhs]", re.I)


def _xlsx_date_styles(zf: zipfile.ZipFile) -> set[int]:
    root = _zxml_opt(zf, "xl/styles.xml")
    if root is None:
        return set()
    custom: dict[int, bool] = {}
    xfs: list[int] = []
    for el in root.iter():
        n = _ln(el.tag)
        if n == "numFmt":
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", el.get("formatCode") or "")
            custom[int(el.get("numFmtId") or 0)] = bool(_DATE_FMT.search(code)) and "0.0" not in code
        elif n == "cellXfs":
            xfs = [int(x.get("numFmtId") or 0) for x in el if _ln(x.tag) == "xf"]
    builtin = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47, 50, 51, 52, 53, 54, 55, 56, 57, 58}
    return {i for i, f in enumerate(xfs) if custom.get(f, f in builtin)}


def _excel_date(v: str) -> str:
    import datetime
    try:
        x = float(v)
        d = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=x)
        return d.strftime("%Y-%m-%d") if x == int(x) else d.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):
        return v


def _xlsx_sheets(zf: zipfile.ZipFile) -> list[dict[str, Any]]:
    wb = _zxml(zf, "xl/workbook.xml")
    rels = _rels(zf, "xl/_rels/workbook.xml.rels", "xl")
    sheets: list[dict[str, Any]] = []
    for el in wb.iter():
        if _ln(el.tag) == "sheet":
            rid = next((v for k, v in el.attrib.items() if _ln(k) == "id"), "")
            target = rels.get(rid, "")
            if target:
                sheets.append({"name": el.get("name") or f"Sheet{len(sheets) + 1}", "part": target, "hidden": el.get("state") in ("hidden", "veryHidden")})
    if not sheets:
        raise FormatError("the workbook lists no sheets")
    return sheets


def _extract_xlsx(path: Path, sheet: str | None, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        sheets = _xlsx_sheets(zf)
        names = [s["name"] for s in sheets]
        chosen = _pick(sheets, sheet)
        shared: list[str] = []
        for _ev, el in _iterparse_opt(zf, "xl/sharedStrings.xml"):
            if _ln(el.tag) == "si":
                shared.append(_cap(_si_text(el)))
                el.clear()
                if len(shared) > 2_000_000:
                    break
        dates = _xlsx_date_styles(zf)
        rows: list[list[str]] = []
        rnums: list[int] = []
        dim = ""
        seen = 0
        more = False
        ncols = 0
        for _ev, el in _iterparse(zf, chosen["part"], drop=("row",)):
            n = _ln(el.tag)
            if n == "dimension":
                dim = el.get("ref") or ""
            elif n == "row":
                try:
                    rn = int(el.get("r") or seen + 1)
                except ValueError:
                    rn = seen + 1
                seen = rn
                if rn > offset and len(rows) < limit:
                    cells: dict[int, str] = {}
                    last = -1
                    for c in el:
                        if _ln(c.tag) != "c":
                            continue
                        ci = _col_index(c.get("r") or "") if c.get("r") else last + 1
                        last = ci
                        if ci >= MAX_COLS:
                            continue
                        v = _xlsx_cell(c, shared, dates)
                        if v != "":
                            cells[ci] = v
                    width = (max(cells) + 1) if cells else 0
                    ncols = max(ncols, width)
                    rows.append([cells.get(i, "") for i in range(width)])
                    rnums.append(rn)
                elif len(rows) >= limit:
                    more = True
                el.clear()
                if more:
                    break
    total = _dim_rows(dim)
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    meta: dict[str, Any] = {"sheets": names, "current": chosen["name"], "hidden": [s["name"] for s in sheets if s["hidden"]],
                            "tables": [{"name": chosen["name"], "columns": [col_name(i) for i in range(ncols)], "rows": rows,
                                        "row_numbers": rnums, "total": total}], "total": total, "offset": offset, "unit": "row"}
    if more:
        meta["next"] = (rnums[-1] if rnums else offset) + 1
    lines = [f"Sheet {chosen['name']!r} ({names.index(chosen['name']) + 1} of {len(names)}; sheets: {', '.join(names)})"
             + (f", about {total:,} rows" if total else "")]
    if rows:
        lines.append("row | " + " | ".join(col_name(i) for i in range(ncols)))
        for rn, r in zip(rnums, rows):
            lines.append(f"{rn} | " + " | ".join(c.replace("\n", " ") for c in r))
    else:
        lines.append("(no cells in this range)")
    return Extracted("xlsx", "\n".join(lines), meta)


def _iterparse_opt(zf: zipfile.ZipFile, name: str) -> Iterator[tuple[str, ET.Element]]:
    try:
        zf.getinfo(name)
    except KeyError:
        return iter(())
    return _iterparse(zf, name, drop=("si",))


def _dim_rows(ref: str) -> int:
    m = re.search(r"(\d+)$", ref or "")
    first = re.search(r"[A-Za-z]+(\d+)", ref or "")
    if m and first:
        return max(0, int(m.group(1)) - int(first.group(1)) + 1)
    return 0


def _pick(items: list[dict[str, Any]], want: str | None) -> dict[str, Any]:
    if not want:
        return next((s for s in items if not s.get("hidden")), items[0])
    w = str(want).strip()
    for s in items:
        if s["name"] == w:
            return s
    for s in items:
        if s["name"].lower() == w.lower():
            return s
    if w.isdigit() and 1 <= int(w) <= len(items):
        return items[int(w) - 1]
    raise FormatError(f"no sheet named {w!r}; available: {', '.join(s['name'] for s in items)}")


def _xlsx_cell(c: ET.Element, shared: list[str], dates: set[int]) -> str:
    t = c.get("t") or "n"
    v = f = None
    inline = ""
    for ch in c:
        n = _ln(ch.tag)
        if n == "v":
            v = ch.text or ""
        elif n == "f":
            f = ch.text or ""
        elif n == "is":
            inline = _si_text(ch)
    if t == "inlineStr":
        return _cap(inline)
    if v is None:
        return f"={f}" if f else ""
    if t == "s":
        try:
            return shared[int(v)]
        except (ValueError, IndexError):
            return ""
    if t == "b":
        return "TRUE" if v.strip() == "1" else "FALSE"
    if t == "n":
        try:
            if int(c.get("s") or 0) in dates:
                return _excel_date(v)
        except ValueError:
            pass
    return _cap(v)


# ─── PowerPoint (.pptx) ─────────────────────────────────────────────────────

def _a_paras(shape: ET.Element) -> list[str]:
    out: list[str] = []
    for p in shape.iter():
        if _ln(p.tag) == "p":
            lvl = 0
            parts: list[str] = []
            for el in p.iter():
                n = _ln(el.tag)
                if n == "pPr":
                    try:
                        lvl = int(el.get("lvl") or 0)
                    except ValueError:
                        lvl = 0
                elif n == "t":
                    parts.append(el.text or "")
                elif n == "br":
                    parts.append("\n")
            s = "".join(parts).strip()
            if s:
                out.append(("  " * lvl) + s)
    return out


def _ph_type(sp: ET.Element) -> str:
    for el in sp.iter():
        if _ln(el.tag) == "ph":
            return el.get("type") or "body"
    return ""


def _slide_content(root: ET.Element) -> dict[str, Any]:
    title, paras, tables = "", [], []

    def walk(el: ET.Element) -> None:
        nonlocal title
        for ch in el:
            n = _ln(ch.tag)
            if n == "sp":
                ph = _ph_type(ch)
                if ph in ("sldNum", "dt", "ftr", "hdr"):
                    continue
                lines = _a_paras(ch)
                if ph in ("title", "ctrTitle") and not title:
                    title = " ".join(x.strip() for x in lines)
                else:
                    paras.extend(lines)
            elif n == "graphicFrame":
                for tbl in ch.iter():
                    if _ln(tbl.tag) == "tbl":
                        rows = []
                        for tr in tbl:
                            if _ln(tr.tag) == "tr":
                                rows.append([_cap(" ".join(_a_paras(tc))) for tc in tr if _ln(tc.tag) == "tc"][:MAX_COLS])
                        tables.append(rows[:500])
            elif n in ("grpSp", "spTree", "cSld", "AlternateContent", "Choice"):
                walk(ch)
    walk(root)
    return {"title": title, "paras": paras, "tables": tables}


def _extract_pptx(path: Path, pages: Any, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        pres = _zxml(zf, "ppt/presentation.xml")
        rels = _rels(zf, "ppt/_rels/presentation.xml.rels", "ppt")
        order: list[str] = []
        for el in pres.iter():
            if _ln(el.tag) == "sldId":
                rid = next((v for k, v in el.attrib.items() if k.startswith("{") and _ln(k) == "id"), "")
                if rid in rels:
                    order.append(rels[rid])
        total = len(order)
        first, last = _page_range(pages, total, default_last=min(total, max(1, min(limit, 50))))
        slides: list[dict[str, Any]] = []
        for i in range(first, last + 1):
            part = order[i - 1]
            content = _slide_content(_zxml(zf, part))
            notes = ""
            srel = _rels(zf, part.rsplit("/", 1)[0] + "/_rels/" + part.rsplit("/", 1)[1] + ".rels", part.rsplit("/", 1)[0])
            for target in srel.values():
                if "notesSlide" in target:
                    nroot = _zxml_opt(zf, target)
                    if nroot is not None:
                        lines: list[str] = []
                        for sp in nroot.iter():
                            if _ln(sp.tag) == "sp" and _ph_type(sp) in ("body", ""):
                                lines.extend(_a_paras(sp))
                        notes = "\n".join(lines)
            slides.append({"n": i, **content, "notes": notes})
    parts: list[str] = []
    for s in slides:
        parts.append(f"=== Slide {s['n']}" + (f": {s['title']}" if s["title"] else "") + " ===")
        parts.extend(s["paras"])
        for rows in s["tables"]:
            parts.append(_table_text(rows))
        if s["notes"]:
            parts.append("[Notes] " + s["notes"].replace("\n", "\n        "))
        parts.append("")
    meta: dict[str, Any] = {"slides": slides, "total": total, "unit": "slide", "first": first, "last": last}
    if last < total:
        meta["next_pages"] = f"{last + 1}-"
    return Extracted("pptx", "\n".join(parts).strip() or "(no slides)", meta)


def _page_range(pages: Any, total: int, default_last: int) -> tuple[int, int]:
    if total <= 0:
        raise FormatError("nothing to read: the file has no pages")
    if pages in (None, ""):
        return 1, max(1, default_last)
    s = str(pages).strip()
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d*))?", s)
    if not m or int(m.group(1)) < 1:
        raise FormatError(f"'pages' must look like '3', '2-5' or '4-', got {pages!r}")
    first = int(m.group(1))
    last = first if m.group(2) is None else (int(m.group(2)) if m.group(2) else total)
    if first > total:
        raise FormatError(f"the file has {total} page(s); pages={pages!r} is beyond the end")
    if last < first:
        raise FormatError(f"'pages' {pages!r} ends before it starts")
    return first, min(last, total, first + 199)


# ─── OpenDocument (.odt .ods .odp) ──────────────────────────────────────────

def _odf_text(el: ET.Element) -> str:
    out: list[str] = [el.text or ""]
    for ch in el:
        n = _ln(ch.tag)
        if n == "s":
            try:
                out.append(" " * min(int(next((v for k, v in ch.attrib.items() if _ln(k) == "c"), "1")), 200))
            except ValueError:
                out.append(" ")
        elif n == "tab":
            out.append("\t")
        elif n == "line-break":
            out.append("\n")
        elif n in ("note", "annotation"):
            pass
        else:
            out.append(_odf_text(ch))
        out.append(ch.tail or "")
    return "".join(out)


def _odf_check(zf: zipfile.ZipFile) -> None:
    try:
        info = zf.getinfo("content.xml")
    except KeyError:
        raise FormatError("content.xml is missing; not a valid OpenDocument file") from None
    if info.flag_bits & 1:
        raise FormatError("the file is password-protected (encrypted)")
    try:
        mf = zf.read("META-INF/manifest.xml")[:200_000]
        if b"encryption-data" in mf:
            raise FormatError("the file is password-protected (encrypted)")
    except KeyError:
        pass


def _extract_odt(path: Path, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        _odf_check(zf)
        root = _zxml(zf, "content.xml")
    blocks: list[dict[str, Any]] = []

    def table(tbl: ET.Element) -> None:
        rows = []
        for tr in tbl.iter():
            if _ln(tr.tag) == "table-row":
                rows.append([_cap(" / ".join(x for x in (_odf_text(p).strip() for p in tc.iter() if _ln(p.tag) == "p") if x))
                             for tc in tr if _ln(tc.tag) in ("table-cell", "covered-table-cell")][:MAX_COLS])
                if len(rows) >= 500:
                    break
        blocks.append({"t": "table", "rows": rows})

    def walk(el: ET.Element, level: int = 0) -> None:
        for ch in el:
            if len(blocks) >= MAX_BLOCKS:
                return
            n = _ln(ch.tag)
            if n == "h":
                lvl = next((v for k, v in ch.attrib.items() if _ln(k) == "outline-level"), "1")
                text = _odf_text(ch).strip()
                if text:
                    blocks.append({"t": f"h{min(6, int(lvl) if lvl.isdigit() else 1)}", "text": text})
            elif n == "p":
                text = _odf_text(ch)
                if text.strip():
                    blocks.append({"t": "li" if level else "p", "text": text.strip() if level else text, "level": level - 1 if level else 0})
            elif n == "list":
                walk(ch, level + 1)
            elif n == "list-item":
                walk(ch, level)
            elif n == "table":
                table(ch)
            elif n in ("section", "text", "body", "office-text", "list-header"):
                walk(ch, level)
    body = next((e for e in root if _ln(e.tag) == "body"), root)
    walk(body)
    return _doc_result("odt", blocks, offset, limit, {})


def _extract_ods(path: Path, sheet: str | None, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        _odf_check(zf)
        # first pass: sheet names
        names: list[str] = []
        for ev, el in _iterparse(zf, "content.xml", ("start",), drop=("table-row",)):
            if _ln(el.tag) == "table" and ev == "start":
                nm = next((v for k, v in el.attrib.items() if _ln(k) == "name"), "")
                if nm and nm not in names:
                    names.append(nm)
        if not names:
            raise FormatError("the spreadsheet has no sheets")
        chosen = _pick([{"name": n} for n in names], sheet)["name"]
        rows: list[list[str]] = []
        rnums: list[int] = []
        rowno = 0
        more = False
        ncols = 0
        stack: list[str] = []
        for ev, el in _iterparse(zf, "content.xml", ("start", "end"), drop=("table-row",)):
            n = _ln(el.tag)
            if ev == "start":
                if n == "table":
                    stack.append(next((v for k, v in el.attrib.items() if _ln(k) == "name"), ""))
                continue
            if n == "table":
                if stack:
                    stack.pop()
                el.clear()
                continue
            if n != "table-row" or not stack or stack[-1] != chosen or len(stack) != 1:
                continue
            rep = int(next((v for k, v in el.attrib.items() if _ln(k) == "number-rows-repeated"), "1") or 1)
            cells: list[str] = []
            for c in el:
                if _ln(c.tag) not in ("table-cell", "covered-table-cell"):
                    continue
                crep = int(next((v for k, v in c.attrib.items() if _ln(k) == "number-columns-repeated"), "1") or 1)
                val = _ods_cell(c)
                if val == "":
                    cells.extend([""] * min(crep, 4))
                else:
                    cells.extend([val] * min(crep, 50))
                if len(cells) > MAX_COLS:
                    cells = cells[:MAX_COLS]
            while cells and cells[-1] == "":
                cells.pop()
            for _ in range(min(rep, 1 if not cells else 50)):
                rowno += 1
                if cells and rowno > offset:
                    if len(rows) < limit:
                        rows.append(cells)
                        rnums.append(rowno)
                        ncols = max(ncols, len(cells))
                    else:
                        more = True
            if not cells:
                rowno += max(0, rep - 1) if rep > 1 else 0
            el.clear()
            if more:
                break
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    meta = {"sheets": names, "current": chosen, "unit": "row", "offset": offset,
            "tables": [{"name": chosen, "columns": [col_name(i) for i in range(ncols)], "rows": rows, "row_numbers": rnums, "total": 0}]}
    if more:
        meta["next"] = rnums[-1] + 1
    lines = [f"Sheet {chosen!r} (sheets: {', '.join(names)})"]
    lines += [f"{rn} | " + " | ".join(c.replace("\n", " ") for c in r) for rn, r in zip(rnums, rows)] if rows else ["(no cells in this range)"]
    return Extracted("ods", "\n".join(lines), meta)


def _ods_cell(c: ET.Element) -> str:
    at = {_ln(k): v for k, v in c.attrib.items()}
    vt = at.get("value-type", "")
    if vt in ("float", "percentage", "currency"):
        v = at.get("value", "")
        return v[:-2] if v.endswith(".0") else v
    if vt == "date":
        return at.get("date-value", "")
    if vt == "time":
        return at.get("time-value", "")
    if vt == "boolean":
        return at.get("boolean-value", "").upper()
    txt = "\n".join(_odf_text(p).strip() for p in c if _ln(p.tag) == "p")
    return _cap(txt)


def _extract_odp(path: Path, pages: Any) -> Extracted:
    with _open_zip(path) as zf:
        _odf_check(zf)
        root = _zxml(zf, "content.xml")
    pgs = [e for e in root.iter() if _ln(e.tag) == "page"]
    total = len(pgs)
    first, last = _page_range(pages, total, default_last=min(total, 50))
    slides = []
    for i in range(first, last + 1):
        pg = pgs[i - 1]
        title, paras, notes = "", [], ""
        for fr in pg.iter():
            n = _ln(fr.tag)
            if n == "notes":
                notes = "\n".join(x for x in (_odf_text(p).strip() for p in fr.iter() if _ln(p.tag) == "p") if x)
        for fr in pg:
            if _ln(fr.tag) == "notes":
                continue
            cls = next((v for k, v in fr.attrib.items() if _ln(k) == "class"), "")
            lines = [x for x in (_odf_text(p).strip() for p in fr.iter() if _ln(p.tag) == "p") if x]
            if cls == "title" and not title:
                title = " ".join(lines)
            else:
                paras.extend(lines)
        slides.append({"n": i, "title": title, "paras": paras, "tables": [], "notes": notes})
    parts: list[str] = []
    for s in slides:
        parts.append(f"=== Slide {s['n']}" + (f": {s['title']}" if s["title"] else "") + " ===")
        parts.extend(s["paras"])
        if s["notes"]:
            parts.append("[Notes] " + s["notes"])
        parts.append("")
    meta: dict[str, Any] = {"slides": slides, "total": total, "unit": "slide"}
    if last < total:
        meta["next_pages"] = f"{last + 1}-"
    return Extracted("odp", "\n".join(parts).strip() or "(no slides)", meta)


# ─── RTF ────────────────────────────────────────────────────────────────────

_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", re.S)
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "headerl", "headerr", "footerl", "footerr",
             "themedata", "colorschememapping", "latentstyles", "datastore", "listtable", "listoverridetable", "rsidtbl",
             "generator", "xmlnstbl", "fldinst", "object", "operator", "private", "revtbl", "mmathPr", "wgrffmtfilter"}
_RTF_CHARS = {"par": "\n", "line": "\n", "tab": "\t", "cell": "\t", "row": "\n", "sect": "\n", "page": "\n", "emdash": "\u2014",
              "endash": "\u2013", "bullet": "\u2022", "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c", "rdblquote": "\u201d",
              "emspace": " ", "enspace": " ", "qmspace": " "}


def rtf_to_text(src: str, max_chars: int = 5_000_000) -> str:
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, pending_skip = False, 1, 0
    total = 0
    for m in _RTF_TOKEN.finditer(src):
        word, num, hexv, sym, brace, text = m.groups()
        if brace == "{":
            stack.append((skip, uc))
            if len(stack) > 500:
                raise FormatError("RTF groups are nested too deeply")
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif word:
            if word in _RTF_SKIP:
                skip = True
            elif word == "uc":
                uc = int(num or 1)
            elif word == "u" and num is not None and not skip:
                n = int(num)
                out.append(chr(n + 65536 if n < 0 else n) if 0 <= (n + 65536 if n < 0 else n) <= 0x10FFFF else "?")
                pending_skip = uc
            elif word in _RTF_CHARS and not skip:
                out.append(_RTF_CHARS[word])
        elif hexv:
            if pending_skip:
                pending_skip -= 1
            elif not skip:
                out.append(bytes([int(hexv, 16)]).decode("cp1252", "replace"))
        elif sym:
            if sym == "*":
                skip = True
            elif sym in "\\{}" and not skip:
                out.append(sym)
            elif sym in "\r\n" and not skip:
                out.append("\n")
            elif sym == "~" and not skip:
                out.append(" ")
            elif sym == "_" and not skip:
                out.append("-")
        elif text and not skip:
            t = text.replace("\r", "").replace("\n", "")
            if pending_skip:
                cut = min(pending_skip, len(t))
                t, pending_skip = t[cut:], pending_skip - cut
            out.append(t)
        total = sum(len(x) for x in out[-1:]) + total
        if total > max_chars:
            break
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _extract_rtf(path: Path, offset: int, limit: int) -> Extracted:
    with open(path, "rb") as f:
        data = f.read(MAX_PART_BYTES + 1)
    if len(data) > MAX_PART_BYTES:
        raise FormatError("RTF file is larger than 32 MB")
    text = rtf_to_text(data.decode("latin-1"))
    blocks = [{"t": "p", "text": ln} for ln in text.split("\n")]
    return _doc_result("rtf", blocks, offset, limit, {})


# ─── EPUB and HTML text ─────────────────────────────────────────────────────

from html.parser import HTMLParser  # noqa: E402

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer",
               "table", "ul", "ol", "pre", "blockquote", "hr", "dt", "dd", "figcaption", "title"}


class _TextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self.links = 0
        self._in_title = False
        self.headings: list[tuple[int, str]] = []
        self._h: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self.links += 1
        if tag in _BLOCK_TAGS:
            self.out.append("\n")
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._h = int(tag[1])
            self.out.append("#" * self._h + " ")
        if tag == "li":
            self.out.append("- ")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f"[image: {alt}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS or tag in ("td", "th"):
            self.out.append("\n" if tag not in ("td", "th") else "\t")
        if self._h and len(tag) == 2 and tag[0] == "h":
            self._h = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(data)

    def text(self) -> str:
        s = "".join(self.out)
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        s = re.sub(r" ?\n ?", "\n", s)
        return re.sub(r"\n{3,}", "\n\n", s).strip()


def html_to_text(src: str) -> tuple[str, str, int]:
    p = _TextHTML()
    try:
        p.feed(src[:8_000_000])
        p.close()
    except Exception:
        pass
    return p.text(), p.title.strip(), p.links


def _extract_epub(path: Path, pages: Any, offset: int, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        cont = _zxml(zf, "META-INF/container.xml")
        opf_path = next((e.get("full-path") for e in cont.iter() if _ln(e.tag) == "rootfile" and e.get("full-path")), "")
        if not opf_path:
            raise FormatError("EPUB has no package document")
        opf = _zxml(zf, opf_path)
        base = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
        manifest = {e.get("id"): _join(base, e.get("href") or "") for e in opf.iter() if _ln(e.tag) == "item" and e.get("id")}
        spine = [manifest[e.get("idref")] for e in opf.iter() if _ln(e.tag) == "itemref" and e.get("idref") in manifest]
        title = next(((e.text or "").strip() for e in opf.iter() if _ln(e.tag) == "title" and (e.text or "").strip()), "")
        author = next(((e.text or "").strip() for e in opf.iter() if _ln(e.tag) == "creator" and (e.text or "").strip()), "")
        if not spine:
            raise FormatError("EPUB has no readable chapters")
        total = len(spine)
        first, last = _page_range(pages, total, default_last=min(total, 5))
        blocks: list[dict[str, Any]] = []
        chapters: list[str] = []
        for i in range(first, last + 1):
            try:
                raw = _zread(zf, spine[i - 1])
            except FormatError as e:
                blocks.append({"t": "p", "text": f"[chapter {i} unreadable: {e}]"})
                continue
            text, ctitle, _ = html_to_text(raw.decode("utf-8", "replace"))
            chapters.append(ctitle or spine[i - 1])
            blocks.append({"t": "h2", "text": f"Chapter {i}" + (f": {ctitle}" if ctitle else "")})
            blocks.extend({"t": "p", "text": ln} for ln in text.split("\n") if ln.strip())
    meta: dict[str, Any] = {"props": {k: v for k, v in (("title", title), ("creator", author)) if v}, "total": total, "unit": "chapter",
                            "first": first, "last": last}
    if last < total:
        meta["next_pages"] = f"{last + 1}-"
    window = blocks[:max(limit, 1)]
    meta["blocks"] = window
    if len(window) < len(blocks):
        meta["notes"] = ["Chapter text was cut at the line limit; request fewer chapters with pages=."]
    return Extracted("epub", blocks_to_text(window) or "(no text)", meta)


# ─── SQLite ─────────────────────────────────────────────────────────────────

def _sql_cell(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(v)):,} bytes>"
    return _cap(str(v), 300)


def _qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _extract_sqlite(path: Path, table: str | None, offset: int, limit: int) -> Extracted:
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
    except sqlite3.Error as e:
        raise FormatError(f"cannot open as a SQLite database ({e})") from None
    import time
    deadline = time.monotonic() + 4.0
    try:
        con.execute("PRAGMA query_only=ON")
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_RECURSIVE}
        con.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        try:
            objs = con.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if "encrypted" in msg or "not a database" in msg:
                raise FormatError("not a readable SQLite database (it may be encrypted or corrupt)") from None
            raise FormatError(f"cannot read the database ({msg})") from None
        tables = [(t, n, s) for t, n, s in objs if t in ("table", "view")]
        names = [n for _t, n, _s in tables]
        meta: dict[str, Any] = {"sheets": names, "unit": "row"}
        if table:
            want = next((n for n in names if n == table), None) or next((n for n in names if n.lower() == table.lower()), None)
            if not want:
                raise FormatError(f"no table named {table!r}; available: {', '.join(names) or '(none)'}")
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({_qid(want)})")]
            cur = con.execute(f"SELECT * FROM {_qid(want)} LIMIT ? OFFSET ?", (limit + 1, offset))
            rows = [[_sql_cell(v) for v in r] for r in cur.fetchall()]
            more = len(rows) > limit
            rows = rows[:limit]
            total = _count(con, want)
            meta.update({"current": want, "tables": [{"name": want, "columns": cols, "rows": rows, "total": total,
                                                      "row_numbers": list(range(offset + 1, offset + len(rows) + 1))}], "total": total, "offset": offset})
            if more:
                meta["next"] = offset + limit + 1
            lines = [f"Table {want} ({total if total is not None else '?'} rows; columns: {', '.join(cols)})", " | ".join(cols)]
            lines += [" | ".join(r) for r in rows]
            return Extracted("sqlite", "\n".join(lines), meta)
        lines = [f"SQLite database: {len(tables)} table(s)/view(s). Use part=<table> to page through one."]
        tabs = []
        for t, n, sql in tables[:60]:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({_qid(n)})")]
            cnt = _count(con, n)
            rows = []
            try:
                rows = [[_sql_cell(v) for v in r] for r in con.execute(f"SELECT * FROM {_qid(n)} LIMIT 5").fetchall()]
            except sqlite3.Error:
                pass
            tabs.append({"name": n, "kind": t, "columns": cols, "rows": rows, "total": cnt, "schema": sql or ""})
            lines += ["", f"== {t} {n} ({cnt if cnt is not None else '?'} rows) ==", _cap(sql or "", 1500), "first rows:"]
            lines += ["  " + " | ".join(r) for r in rows] or ["  (empty)"]
        others = [(t, n) for t, n, _s in objs if t in ("index", "trigger")]
        if others:
            lines += ["", "Indexes/triggers: " + ", ".join(f"{n}" for _t, n in others[:50])]
        meta["tables"] = tabs
        meta["total"] = len(tables)
        return Extracted("sqlite", "\n".join(lines), meta)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise FormatError("reading the database took too long; try part=<table>") from None
        raise FormatError(f"cannot read the database ({e})") from None
    except sqlite3.Error as e:
        raise FormatError(f"cannot read the database ({e})") from None
    finally:
        con.close()


def _count(con: sqlite3.Connection, name: str) -> int | None:
    try:
        return int(con.execute(f"SELECT COUNT(*) FROM {_qid(name)}").fetchone()[0])
    except sqlite3.Error:
        return None


# ─── archives ───────────────────────────────────────────────────────────────

def unsafe_member(name: str) -> bool:
    n = name.replace("\\", "/")
    return (not n or "\x00" in n or n.startswith("/") or re.match(r"^[A-Za-z]:", n) is not None
            or any(seg == ".." for seg in n.split("/")))


def _entries_text(entries: list[dict[str, Any]], truncated: bool, total: int | None, notices: list[str]) -> str:
    lines = list(notices)
    lines.append(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} listed" + (" (listing truncated)" if truncated else ""))
    for e in entries:
        mark = "/" if e["dir"] else ""
        flag = "  [unsafe path, not readable]" if e.get("unsafe") else ""
        lines.append(f"{'' if e['dir'] else str(e['size']).rjust(12)}  {e['name']}{mark}{flag}" if not e["dir"] else f"{'':12}  {e['name']}{'' if e['name'].endswith('/') else mark}{flag}")
    return "\n".join(lines)


def _extract_zip(path: Path, member: str | None, limit: int) -> Extracted:
    with _open_zip(path) as zf:
        infos = zf.infolist()
        if member:
            return _read_zip_member(zf, member)
        entries, total_unc, total_c = [], 0, 0
        for info in infos[:min(MAX_ENTRIES, max(limit, 200))]:
            entries.append({"name": info.filename, "size": info.file_size, "csize": info.compress_size, "dir": info.is_dir(),
                            "encrypted": bool(info.flag_bits & 1), "unsafe": unsafe_member(info.filename)})
        for info in infos:
            total_unc += info.file_size
            total_c += info.compress_size
        truncated = len(infos) > len(entries)
    notices = []
    bomb = total_unc > BOMB_TOTAL or (total_c > 0 and total_unc / total_c > BOMB_RATIO and total_unc > 100 * 1048576)
    if bomb:
        notices.append(f"WARNING: declared contents are {_human(total_unc)} from a {_human(total_c)} archive (possible zip bomb). Nothing was extracted.")
    if any(e["encrypted"] for e in entries):
        notices.append("Some entries are password-protected and cannot be read.")
    notices.append(f"Zip archive, {len(infos)} entries, {_human(total_unc)} uncompressed. Contents are listed, never extracted; read one with part=<path inside>.")
    meta = {"entries": entries, "total": len(infos), "uncompressed": total_unc, "compressed": total_c, "bomb": bomb, "truncated": truncated,
            "notes": notices}
    return Extracted("zip", _entries_text(entries, truncated, len(infos), notices), meta)


def _member_result(name: str, data: bytes, kind: str, total_size: int) -> Extracted:
    from axon.tools.textio import is_binary_bytes
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS and ext in MODEL_IMAGE_MEDIA:
        info = image_info(data)
        return Extracted(kind, f"[{name}: image, {info.get('width', '?')}x{info.get('height', '?')}, {_human(total_size)}]",
                         {"member": name, "info": info}, [ImageData(MODEL_IMAGE_MEDIA[ext], data, name)])
    if is_binary_bytes(data[:8192]) and not sniff_utf16(data):
        return Extracted(kind, f"[{name}: binary, {_human(total_size)}; first {min(len(data), HEX_BYTES)} bytes]\n" + hex_dump(data[:HEX_BYTES]),
                         {"member": name, "binary": True, "magic": _magic_name(data)})
    text, enc = decode_text(data)
    return Extracted(kind, text, {"member": name, "encoding": enc, "total_lines": text.count("\n") + 1})


def _read_zip_member(zf: zipfile.ZipFile, member: str) -> Extracted:
    if unsafe_member(member):
        raise FormatError(f"refused: {member!r} is not a safe path inside an archive (absolute path or '..')")
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise FormatError(f"{member!r} is not in the archive") from None
    if info.is_dir():
        raise FormatError(f"{member!r} is a folder")
    if info.flag_bits & 1:
        raise FormatError(f"{member!r} is password-protected")
    if info.file_size > MAX_MEMBER_BYTES:
        raise FormatError(f"{member!r} is {_human(info.file_size)}; members over {MAX_MEMBER_BYTES // 1048576} MB are not read")
    try:
        with zf.open(info) as f:
            data = f.read(MAX_MEMBER_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, OSError, NotImplementedError, RuntimeError) as e:
        raise FormatError(f"{member!r} is damaged ({e})") from None
    if len(data) > MAX_MEMBER_BYTES:
        raise FormatError(f"{member!r} expands past {MAX_MEMBER_BYTES // 1048576} MB; stopped")
    return _member_result(member, data, "zip", info.file_size)


def _open_compressed(path: Path) -> Any:
    with open(path, "rb") as f:
        head = f.read(6)
    if head[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if head[:3] == b"BZh":
        return bz2.BZ2File(path, "rb")
    if head[:6] == b"\xfd7zXZ\x00":
        return lzma.LZMAFile(path, "rb")
    return open(path, "rb")


def _extract_tar(path: Path, member: str | None, limit: int) -> Extracted:
    if member and unsafe_member(member):
        raise FormatError(f"refused: {member!r} is not a safe path inside an archive (absolute path or '..')")
    entries: list[dict[str, Any]] = []
    truncated = False
    cap_note = ""
    total_size = 0
    count = 0
    raw = _open_compressed(path)
    capped = _Capped(raw, MAX_STREAM_BYTES, "the archive")
    try:
        with tarfile.open(fileobj=capped, mode="r|") as tf:
            for ti in tf:
                count += 1
                if member:
                    if ti.name.lstrip("./") == member.lstrip("./") or ti.name == member:
                        if not ti.isreg():
                            raise FormatError(f"{member!r} is not a regular file")
                        if ti.size > MAX_MEMBER_BYTES:
                            raise FormatError(f"{member!r} is {_human(ti.size)}; members over {MAX_MEMBER_BYTES // 1048576} MB are not read")
                        fobj = tf.extractfile(ti)
                        data = fobj.read(MAX_MEMBER_BYTES + 1) if fobj else b""
                        return _member_result(member, data, "tar", ti.size)
                    if count > 200_000:
                        break
                    continue
                total_size += ti.size if ti.isreg() else 0
                if len(entries) < min(MAX_ENTRIES, max(limit, 200)):
                    kind = "link" if (ti.issym() or ti.islnk()) else ("dir" if ti.isdir() else ("file" if ti.isreg() else "special"))
                    entries.append({"name": ti.name, "size": ti.size if ti.isreg() else 0, "csize": 0, "dir": ti.isdir(),
                                    "unsafe": unsafe_member(ti.name), "type": kind, "target": ti.linkname if kind == "link" else ""})
                else:
                    truncated = True
                    if count > 200_000:
                        break
    except FormatError as e:
        if member:
            raise
        truncated = True
        cap_note = str(e)
    except (tarfile.TarError, EOFError, OSError, zlib.error, lzma.LZMAError) as e:
        if not entries and not member:
            raise FormatError(f"not a readable tar archive ({e})") from None
        truncated = True
        cap_note = f"archive is damaged or cut short ({e})"
    finally:
        try:
            raw.close()
        except Exception:
            pass
    if member:
        raise FormatError(f"{member!r} is not in the archive (or beyond the scan limit)")
    notices = [f"Tar archive, {count:,} entries seen, {_human(total_size)} of file data. Contents are listed, never extracted; read one with part=<path inside>."]
    if cap_note:
        notices.append("WARNING: " + cap_note)
    if any(e.get("type") == "link" for e in entries):
        notices.append("Links are shown but never followed.")
    meta = {"entries": entries, "total": count, "uncompressed": total_size, "truncated": truncated, "notes": notices}
    return Extracted("tar", _entries_text(entries, truncated, count, notices), meta)


def _extract_gzip(path: Path) -> Extracted:
    raw = _open_compressed(path)
    try:
        data = raw.read(MAX_MEMBER_BYTES + 1)
    except (OSError, EOFError, zlib.error, lzma.LZMAError) as e:
        raise FormatError(f"compressed file is damaged ({e})") from None
    finally:
        raw.close()
    inner = path.stem if path.suffix.lower() in (".gz", ".bz2", ".xz") else path.name
    trunc = len(data) > MAX_MEMBER_BYTES
    ex = _member_result(inner, data[:MAX_MEMBER_BYTES], "gzip", len(data))
    ex.meta.setdefault("notes", []).append("Single compressed file" + (f"; only the first {MAX_MEMBER_BYTES // 1048576} MB shown" if trunc else "") + ".")
    ex.text = f"[{path.name} decompresses to {inner}{' (truncated)' if trunc else ''}]\n" + ex.text
    return ex


# ─── images, audio, video, fonts, binaries ──────────────────────────────────

def image_info(data: bytes) -> dict[str, Any]:
    """{'format', 'width', 'height', ...} from header bytes; empty keys when unknown."""
    d = data
    try:
        if d[:8] == b"\x89PNG\r\n\x1a\n" and d[12:16] == b"IHDR":
            w, h = struct.unpack(">II", d[16:24])
            return {"format": "PNG", "width": w, "height": h, "alpha": d[25] in (4, 6)}
        if d[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", d[6:10])
            return {"format": "GIF", "width": w, "height": h, "animated": d.count(b"\x21\xf9\x04") > 1}
        if d[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(d):
                if d[i] != 0xFF:
                    i += 1
                    continue
                m = d[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7 or m == 0xFF:
                    i += 1 if m == 0xFF else 2
                    continue
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", d[i + 5:i + 9])
                    return {"format": "JPEG", "width": w, "height": h}
                i += 2 + struct.unpack(">H", d[i + 2:i + 4])[0]
            return {"format": "JPEG"}
        if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
            kind = d[12:16]
            if kind == b"VP8 " and len(d) >= 30:
                w, h = struct.unpack("<HH", d[26:30])
                return {"format": "WebP", "width": w & 0x3FFF, "height": h & 0x3FFF}
            if kind == b"VP8L" and len(d) >= 25:
                b = struct.unpack("<I", d[21:25])[0]
                return {"format": "WebP", "width": (b & 0x3FFF) + 1, "height": ((b >> 14) & 0x3FFF) + 1}
            if kind == b"VP8X" and len(d) >= 30:
                return {"format": "WebP", "width": int.from_bytes(d[24:27], "little") + 1, "height": int.from_bytes(d[27:30], "little") + 1}
            return {"format": "WebP"}
        if d[:2] == b"BM" and len(d) >= 26:
            w, h = struct.unpack("<ii", d[18:26])
            return {"format": "BMP", "width": w, "height": abs(h)}
        if d[:4] == b"\x00\x00\x01\x00" and len(d) >= 8:
            n = struct.unpack("<H", d[4:6])[0]
            w, h = d[6] or 256, d[7] or 256
            return {"format": "ICO", "width": w, "height": h, "images": n}
        if d[:4] in (b"II*\x00", b"MM\x00*"):
            e = "<" if d[:2] == b"II" else ">"
            off = struct.unpack(e + "I", d[4:8])[0]
            if off + 2 <= len(d):
                n = struct.unpack(e + "H", d[off:off + 2])[0]
                out: dict[str, Any] = {"format": "TIFF"}
                for k in range(min(n, 64)):
                    ent = d[off + 2 + k * 12: off + 14 + k * 12]
                    if len(ent) < 12:
                        break
                    tag, typ = struct.unpack(e + "HH", ent[:4])
                    val = struct.unpack(e + ("H" if typ == 3 else "I"), ent[8:10] if typ == 3 else ent[8:12])[0]
                    if tag == 256:
                        out["width"] = val
                    elif tag == 257:
                        out["height"] = val
                return out
            return {"format": "TIFF"}
        if d[4:8] == b"ftyp":
            fmt = "AVIF" if d[8:12] in (b"avif", b"avis") else "HEIC"
            i = d.find(b"ispe")
            if i > 0 and i + 16 <= len(d):
                w, h = struct.unpack(">II", d[i + 8:i + 16])
                return {"format": fmt, "width": w, "height": h}
            return {"format": fmt}
    except (struct.error, IndexError):
        pass
    return {}


def _run(cmd: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / f"axon-converted-{os.getuid() if hasattr(os, 'getuid') else 0}"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def convert_image(path: Path, max_side: int = 1600, fmt: str | None = None) -> tuple[Path, str] | None:
    """Convert/shrink a picture with macOS ``sips`` to a PNG or JPEG the model and browser take; None when unavailable."""
    sips = shutil.which("sips")
    if not sips:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    ext = path.suffix.lower()
    fmt = fmt or ("png" if ext in (".png", ".bmp", ".ico", ".gif") else "jpeg")
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_side}|{fmt}".encode()).hexdigest()[:20]
    out = _cache_dir() / f"{key}.{'png' if fmt == 'png' else 'jpg'}"
    if not out.exists():
        r = _run([sips, "-s", "format", fmt, "-Z", str(max_side), str(path.resolve()), "--out", str(out)], 40)
        if r is None or r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return None
    return out, "image/png" if fmt == "png" else "image/jpeg"


def _extract_image(path: Path, size: int, want_preview: bool = False) -> Extracted:
    with open(path, "rb") as f:
        head = f.read(262144)
    info = image_info(head)
    if not info.get("width") and shutil.which("sips"):
        r = _run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", "-g", "format", str(path.resolve())], 10)
        if r is not None and r.returncode == 0:
            out = r.stdout.decode("utf-8", "replace")
            mw, mh = re.search(r"pixelWidth: (\d+)", out), re.search(r"pixelHeight: (\d+)", out)
            if mw and mh:
                info.update(width=int(mw.group(1)), height=int(mh.group(1)))
    ext = path.suffix.lower()
    direct = ext in MODEL_IMAGE_MEDIA and (info.get("format") or "").lower() in ("png", "jpeg", "gif", "webp")
    meta: dict[str, Any] = {"info": info, "size": size, "direct": direct, "notes": []}
    dims = f"{info['width']}x{info['height']}" if info.get("width") else "unknown size"
    text = f"[Image: {path.name}, {info.get('format', ext.lstrip('.').upper() or 'unknown format')}, {dims}, {_human(size)}]"
    images: list[ImageData] = []
    if not direct and want_preview:
        conv = convert_image(path)
        if conv:
            images.append(ImageData(conv[1], conv[0].read_bytes()[:6 * 1048576], path.name))
            meta["notes"].append("Converted to a standard format for display.")
        else:
            meta["notes"].append("This format cannot be shown here and no converter (macOS sips) was found; download it to open it elsewhere.")
    if info.get("animated"):
        meta["notes"].append("Animated GIF.")
    return Extracted("image", text, meta, images)


def _ffprobe(path: Path) -> dict[str, Any] | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    r = _run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", str(path.resolve())], 12)
    if r is None or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


def _fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _extract_media(path: Path, kind: str, size: int) -> Extracted:
    meta: dict[str, Any] = {"size": size, "notes": []}
    lines = [f"[{kind.capitalize()} file: {path.name}, {_human(size)}]"]
    data = _ffprobe(path)
    if data:
        fmt = data.get("format") or {}
        try:
            dur = float(fmt.get("duration"))
            meta["duration"] = dur
            lines.append(f"Duration: {_fmt_dur(dur)} ({dur:.1f} s)")
        except (TypeError, ValueError):
            pass
        if fmt.get("format_long_name"):
            lines.append(f"Container: {fmt['format_long_name']}")
        if fmt.get("bit_rate"):
            lines.append(f"Bitrate: {int(fmt['bit_rate']) // 1000} kbps")
        streams = []
        for s in data.get("streams") or []:
            t, c = s.get("codec_type"), s.get("codec_name")
            if t == "video":
                fr = s.get("avg_frame_rate", "")
                try:
                    a, b = fr.split("/")
                    fr = f", {float(a) / float(b):.2f} fps" if float(b) else ""
                except (ValueError, ZeroDivisionError):
                    fr = ""
                streams.append({"type": t, "codec": c, "width": s.get("width"), "height": s.get("height")})
                lines.append(f"Video: {c}, {s.get('width')}x{s.get('height')}{fr}")
            elif t == "audio":
                streams.append({"type": t, "codec": c, "rate": s.get("sample_rate"), "channels": s.get("channels")})
                lines.append(f"Audio: {c}, {s.get('sample_rate', '?')} Hz, {s.get('channels', '?')} ch")
            elif t in ("subtitle", "attachment"):
                lines.append(f"{t.capitalize()}: {c}")
        meta["streams"] = streams
        tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
        shown = {k: tags[k] for k in ("title", "artist", "album", "date", "genre") if tags.get(k)}
        meta["tags"] = shown
        lines += [f"{k.capitalize()}: {v}" for k, v in shown.items()]
    else:
        wav = _wav_info(path)
        if wav:
            meta.update(wav)
            lines.append(f"Duration: {_fmt_dur(wav['duration'])}; {wav['rate']} Hz, {wav['channels']} ch, {wav['bits']}-bit PCM")
        else:
            lines.append("Duration and codec unavailable: ffprobe (part of ffmpeg) is not installed.")
            meta["notes"].append("Install ffmpeg for duration and codec details.")
    return Extracted(kind, "\n".join(lines), meta)


def _wav_info(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as f:
            h = f.read(65536)
        if h[:4] != b"RIFF" or h[8:12] != b"WAVE":
            return None
        i = h.find(b"fmt ")
        _fmtid, ch, rate, byterate, _al, bits = struct.unpack("<HHIIHH", h[i + 8:i + 24])
        j = h.find(b"data")
        dsize = struct.unpack("<I", h[j + 4:j + 8])[0]
        size = path.stat().st_size
        dsize = min(dsize, max(0, size - j - 8)) if dsize != 0xFFFFFFFF else size - j - 8
        return {"duration": dsize / byterate if byterate else 0.0, "rate": rate, "channels": ch, "bits": bits}
    except (struct.error, IndexError, OSError, ZeroDivisionError):
        return None


def _font_names(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        if data[:4] == b"wOF2":
            return {"note": "WOFF2 (Brotli-compressed); name table not read"}
        base = 0
        if data[:4] == b"ttcf":
            base = struct.unpack(">I", data[12:16])[0]
        woff = data[:4] == b"wOFF"
        if woff:
            n = struct.unpack(">H", data[12:14])[0]
            tables = {data[44 + 20 * i:48 + 20 * i]: struct.unpack(">IIII", data[48 + 20 * i:64 + 20 * i])[:3] for i in range(n)}
        else:
            n = struct.unpack(">H", data[base + 4:base + 6])[0]
            tables = {}
            for i in range(n):
                rec = data[base + 12 + 16 * i: base + 28 + 16 * i]
                tables[rec[:4]] = (struct.unpack(">I", rec[8:12])[0], struct.unpack(">I", rec[12:16])[0], struct.unpack(">I", rec[12:16])[0])

        def table(tag: bytes) -> bytes:
            off, comp, orig = tables[tag]
            raw = data[off:off + comp]
            return zlib.decompress(raw) if woff and comp < orig else raw
        if b"name" in tables:
            t = table(b"name")
            count, soff = struct.unpack(">HH", t[2:6])
            labels = {1: "family", 2: "style", 4: "full_name", 5: "version", 6: "postscript", 0: "copyright"}
            for k in range(min(count, 200)):
                pid, eid, lid, nid, ln, off = struct.unpack(">HHHHHH", t[6 + 12 * k:18 + 12 * k])
                if nid in labels and labels[nid] not in out:
                    raw = t[soff + off: soff + off + ln]
                    s = raw.decode("utf-16-be" if pid in (0, 3) else "mac_roman", "replace")
                    if s.strip():
                        out[labels[nid]] = s.strip()[:200]
        if b"maxp" in tables:
            out["glyphs"] = str(struct.unpack(">H", table(b"maxp")[4:6])[0])
    except (struct.error, KeyError, IndexError, zlib.error):
        pass
    return out


def _extract_font(path: Path, size: int) -> Extracted:
    with open(path, "rb") as f:
        data = f.read(4 * 1048576)
    names = _font_names(data)
    ext = path.suffix.lower().lstrip(".").upper()
    lines = [f"[Font file: {path.name}, {ext}, {_human(size)}]"]
    lines += [f"{k.replace('_', ' ').capitalize()}: {v}" for k, v in names.items()]
    if not names:
        lines.append("The font's name could not be read (unsupported or damaged font file).")
    return Extracted("font", "\n".join(lines), {"names": names, "size": size})


_MAGIC_NAMES = [(b"\x7fELF", "ELF executable"), (b"MZ", "Windows executable (PE)"), (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
                (b"\xca\xfe\xba\xbe", "Mach-O universal binary or Java class"), (b"%PDF-", "PDF document"), (b"PK\x03\x04", "zip-based container"),
                (b"\x1f\x8b", "gzip data"), (b"BZh", "bzip2 data"), (b"\xfd7zXZ\x00", "xz data"), (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
                (b"Rar!", "RAR archive"), (b"SQLite format 3", "SQLite database"), (b"\x89PNG", "PNG image"), (b"\xff\xd8\xff", "JPEG image"),
                (b"GIF8", "GIF image"), (b"OggS", "Ogg media"), (b"fLaC", "FLAC audio"), (b"ID3", "MP3 audio"), (b"\x00asm", "WebAssembly module"),
                (b"\xd0\xcf\x11\xe0", "legacy Microsoft Office / OLE document"), (b"\x80\x04", "Python pickle"), (b"\x80\x05", "Python pickle"),
                (b"PAR1", "Parquet data"), (b"ARROW1", "Arrow data"), (b"\x93NUMPY", "NumPy array"), (b"\x89HDF", "HDF5 data"),
                (b"dex\n", "Android Dalvik executable"), (b"!<arch>", "Unix archive (.a)"), (b"\x00\x00\x01\x00", "ICO icon")]


def _magic_name(data: bytes) -> str:
    for sig, name in _MAGIC_NAMES:
        if data.startswith(sig):
            return name
    if data[4:8] == b"ftyp":
        return "ISO media (MP4/MOV/HEIC)"
    if data[:4] == b"RIFF":
        return f"RIFF container ({data[8:12].decode('ascii', 'replace')})"
    return "unknown"


def hex_dump(data: bytes, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02x}" for b in chunk[:8]) + "  " + " ".join(f"{b:02x}" for b in chunk[8:])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}  {hx:<49} |{asc}|")
    return "\n".join(lines)


def _extract_binary(path: Path, size: int) -> Extracted:
    with open(path, "rb") as f:
        data = f.read(HEX_BYTES)
    what = _magic_name(data)
    text = f"[Binary file: {path.name}, {size:,} bytes, detected type: {what}. First {len(data)} bytes in hex:]\n" + hex_dump(data)
    return Extracted("binary", text, {"magic": what, "size": size, "hex": True})


def _extract_legacy(path: Path, size: int, offset: int, limit: int) -> Extracted:
    ext = path.suffix.lower()
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise FormatError("not a legacy Office file")
    exe = shutil.which("textutil")
    if exe and ext in (".doc", ".dot"):
        r = _run([exe, "-convert", "txt", "-stdout", str(path.resolve())], 40)
        text = r.stdout.decode("utf-8", "replace") if r is not None and r.returncode == 0 else ""
        if text.strip() and sum(ch.isprintable() or ch in "\n\t" for ch in text[:5000]) / len(text[:5000]) > 0.9 and "\x00" not in text[:5000]:
            blocks = [{"t": "p", "text": ln} for ln in text.split("\n")]
            ex = _doc_result("legacy_office", blocks, offset, limit, {"notes": ["Converted with macOS textutil."]})
            return ex
    raise FormatError(f"{ext} is a legacy binary Office format, or an encrypted Office file (password-protected), and cannot be read here. "
                      "Save it as .docx/.xlsx/.pptx (or export to PDF or CSV) and read that instead.")


# ─── text formats ───────────────────────────────────────────────────────────

def _toml_module() -> Any:
    """``tomllib`` (3.11+), or the ``tomli`` backport on Python 3.10."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib


def _shape(v: Any, depth: int = 0) -> str:
    if isinstance(v, dict):
        keys = list(v)[:12]
        return "object{" + ", ".join(str(k) for k in keys) + (", ..." if len(v) > 12 else "") + f"}} ({len(v)} keys)"
    if isinstance(v, list):
        return f"array of {len(v)}" + (f" ({_shape(v[0], depth + 1)})" if v and depth == 0 else "")
    return type(v).__name__


def detect_delimiter(sample: str, ext: str = "") -> str:
    if ext == ".tsv":
        return "\t"
    try:
        return csv.Sniffer().sniff(sample[:20000], delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    counts = {d: sample[:5000].count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def parse_csv(text: str, delim: str, max_rows: int) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    csv.field_size_limit(1 << 20)
    try:
        for i, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim)):
            if i >= max_rows:
                return rows, True
            rows.append([_cap(c, 500) for c in row[:MAX_COLS]])
    except csv.Error:
        pass
    return rows, False


def _read_text_head(path: Path, cap: int = 8 * 1048576) -> tuple[str, str, bool]:
    with open(path, "rb") as f:
        data = f.read(cap + 1)
    text, enc = decode_text(data[:cap])
    return text, enc, len(data) > cap


def _extract_textfmt(path: Path, kind: str, offset: int, limit: int, size: int) -> Extracted:
    text, enc, truncated = _read_text_head(path)
    meta: dict[str, Any] = {"encoding": enc, "size": size, "notes": []}
    if truncated:
        meta["notes"].append("File is larger than 8 MB; only the start was analysed.")
    out = text
    if kind == "csv":
        delim = detect_delimiter(text, path.suffix.lower())
        rows, more = parse_csv(text, delim, offset + limit + 1)
        header = rows[0] if rows else []
        body = rows[1:][offset:offset + limit]
        names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
        meta.update(delimiter=delim, delimiter_name=names.get(delim, delim), columns=header,
                    tables=[{"name": path.name, "columns": header, "rows": body, "row_numbers": list(range(offset + 2, offset + 2 + len(body))),
                             "total": None if (more or truncated) else max(0, len(rows) - 1)}],
                    unit="row", offset=offset)
        lines = [f"CSV ({names.get(delim, delim)}-separated), {len(header)} columns: {', '.join(header)}"]
        lines += [str(offset + 2 + i).rjust(6) + "| " + " | ".join(r) for i, r in enumerate(body)]
        out = "\n".join(lines)
        if more or offset + limit < len(rows) - 1:
            meta["next"] = offset + limit + 1
    elif kind == "json":
        try:
            v = json.loads(text)
            out = json.dumps(v, indent=2, ensure_ascii=False)
            meta["shape"] = _shape(v)
        except ValueError as e:
            meta["notes"].append(f"Not valid JSON ({e}); showing the file as is.")
    elif kind == "jsonl":
        good, bad, recs = 0, 0, []
        for ln in text.split("\n"):
            if not ln.strip():
                continue
            try:
                v = json.loads(ln)
                good += 1
                if len(recs) < limit:
                    recs.append(json.dumps(v, ensure_ascii=False))
            except ValueError:
                bad += 1
            if good + bad > 200_000:
                break
        meta["shape"] = f"{good} records" + (f", {bad} invalid line(s)" if bad else "")
        out = "\n".join(recs[offset:offset + limit]) if recs else text
    elif kind == "toml":
        try:
            v = _toml_module().loads(text)
            meta["shape"] = _shape(v)
        except Exception as e:
            meta["notes"].append(f"Not valid TOML ({e}).")
    elif kind == "yaml":
        try:
            import yaml  # optional
            docs = list(yaml.safe_load_all(text))
            meta["shape"] = f"{len(docs)} document(s); first: {_shape(docs[0])}" if docs else "empty"
        except ImportError:
            meta["notes"].append("Structure not validated (the optional PyYAML package is not installed).")
        except Exception as e:
            meta["notes"].append(f"Not valid YAML ({str(e).splitlines()[0] if str(e) else 'error'}).")
    elif kind in ("xml", "svg"):
        risky = bool(re.search(r"<script|\son\w+\s*=|javascript:|<foreignObject", text, re.I)) if kind == "svg" else False
        try:
            root = parse_xml(text.encode("utf-8", "replace")) if text.strip() else None
            if root is not None:
                meta["root"] = _ln(root.tag)
                meta["elements"] = sum(1 for _ in root.iter())
                if kind == "xml":
                    try:
                        ET.indent(root)
                    except Exception:
                        pass
                    out = ET.tostring(root, encoding="unicode")[:2_000_000]
                else:
                    meta["viewbox"] = root.get("viewBox") or ""
                    out = text
                    ttl = next((e.text for e in root.iter() if _ln(e.tag) == "title" and e.text), "")
                    if ttl:
                        meta["title"] = ttl.strip()
                meta["readable"] = " ".join(t.strip() for t in root.itertext() if t.strip())[:20000]
        except FormatError as e:
            meta["notes"].append(f"{e}; showing the file as is.")
        if risky:
            meta["notes"].append("This SVG contains script or event handlers. They never run here: it is shown as source or as a picture only.")
    elif kind == "html":
        readable, title, links = html_to_text(text)
        meta.update(title=title, links=links, readable=readable[:200000])
        out = readable
    if kind in ("json", "yaml", "toml", "xml", "svg", "log", "jsonl", "text") or kind == "html":
        lines = out.split("\n")
        meta["total"] = len(lines)
        if kind not in ("jsonl",):
            window = lines[offset:offset + limit]
            if offset + limit < len(lines):
                meta["next"] = offset + limit + 1
            out = "\n".join(l[:20000] for l in window)
    return Extracted(kind, out, meta)


# ─── notebook ───────────────────────────────────────────────────────────────

def notebook_cells(nb: dict[str, Any], max_cells: int = 500, max_img: int = 2 * 1048576) -> tuple[list[dict[str, Any]], list[ImageData]]:
    def _t(v: Any) -> str:
        return "".join(v) if isinstance(v, list) else str(v or "")
    cells: list[dict[str, Any]] = []
    images: list[ImageData] = []
    for idx, c in enumerate((nb.get("cells") or [])[:max_cells]):
        if not isinstance(c, dict):
            continue
        outs: list[dict[str, Any]] = []
        for o in (c.get("outputs") or [])[:30]:
            if not isinstance(o, dict):
                continue
            ot = o.get("output_type")
            if ot == "stream":
                outs.append({"kind": "text", "name": o.get("name", "stdout"), "text": _cap(_t(o.get("text")), 20000)})
            elif ot == "error":
                outs.append({"kind": "error", "text": f"{o.get('ename', 'Error')}: {o.get('evalue', '')}"})
            elif ot in ("execute_result", "display_data"):
                data = o.get("data") or {}
                for mime in ("image/png", "image/jpeg"):
                    if mime in data:
                        try:
                            raw = base64.b64decode("".join(data[mime]) if isinstance(data[mime], list) else data[mime], validate=False)
                        except Exception:
                            continue
                        if len(raw) <= max_img and image_info(raw).get("format") in ("PNG", "JPEG"):
                            images.append(ImageData(mime, raw, f"cell {idx} output"))
                            outs.append({"kind": "image", "mime": mime, "b64": base64.b64encode(raw).decode(), "label": f"cell {idx} output"})
                        else:
                            outs.append({"kind": "text", "name": "image", "text": f"[{mime} output not shown: larger than {max_img // 1048576} MB or invalid]"})
                        break
                else:
                    if "text/plain" in data:
                        outs.append({"kind": "text", "name": "result", "text": _cap(_t(data["text/plain"]), 20000)})
                    elif "text/html" in data:
                        outs.append({"kind": "text", "name": "html", "text": _cap(html_to_text(_t(data["text/html"]))[0], 5000)})
        cells.append({"n": idx, "type": c.get("cell_type", "code"), "exec": c.get("execution_count"),
                      "source": _cap(_t(c.get("source")), 100000), "outputs": outs})
    return cells, images


def _extract_ipynb(path: Path, offset: int, limit: int, size: int) -> Extracted:
    if size > 100 * 1048576:
        raise FormatError("notebook is larger than 100 MB")
    text, _enc, _t = _read_text_head(path, 100 * 1048576)
    try:
        nb = json.loads(text)
    except ValueError as e:
        raise FormatError(f"not valid notebook JSON ({e})") from None
    if not isinstance(nb, dict):
        raise FormatError("not a notebook (top level is not an object)")
    from axon.tools.notebook import render_notebook
    cells, images = notebook_cells(nb)
    total = len(nb.get("cells") or [])
    window = cells[offset:offset + limit]
    meta: dict[str, Any] = {"cells": window, "total": total, "unit": "cell", "images": len(images)}
    if offset + limit < total:
        meta["next"] = offset + limit + 1
    return Extracted("ipynb", render_notebook(nb, path.name), meta, images[:5])


# ─── PDF ────────────────────────────────────────────────────────────────────

def _extract_pdf(path: Path, pages: Any) -> Extracted:
    from axon.tools import pdfread
    n = pdfread.page_count(path)
    first, last = (1, min(n or 1, 20))
    if pages not in (None, ""):
        try:
            first, last = pdfread.parse_pages(pages)
        except Exception as e:
            raise FormatError(str(e)) from None
        last = last or (n or first)
        last = min(last, first + pdfread.MAX_PAGES_PER_CALL - 1)
    got = pdfread.extract_pages(path, first, last)
    meta: dict[str, Any] = {"total": n, "unit": "page", "first": first, "last": last}
    if got is None:
        return Extracted("pdf", f"[PDF with {n if n else 'an unknown number of'} page(s). Text extraction needs poppler's pdftotext on PATH "
                         "(brew install poppler) or the optional pypdf package.]", meta)
    text, engine = got
    meta["engine"] = engine
    if n and last < n:
        meta["next_pages"] = f"{last + 1}-"
    return Extracted("pdf", text or "(no text on these pages; the PDF may be scanned images)", meta)


# ─── entry point ────────────────────────────────────────────────────────────

def extract(path: Path | str, pages: Any = None, sheet: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0,
            kind: str | None = None, want_preview: bool = False) -> Extracted:
    """Extract readable content from *path*. Never raises for bad files: the message is in ``text`` and ``meta['error']``.

    *pages* selects pages/slides/chapters ("2-5"); *sheet* selects a worksheet, SQLite table or archive member;
    *offset* is the 0-based first row/block/line and *limit* the number of them."""
    p = Path(path)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), 20000))
    offset = max(0, int(offset or 0))
    sheet = str(sheet).strip() if sheet not in (None, "") else None
    try:
        size = p.stat().st_size
        k = kind or detect_kind(p)
        if k == "docx":
            return _extract_docx(p, offset, limit)
        if k == "xlsx":
            return _extract_xlsx(p, sheet, offset, limit)
        if k == "pptx":
            return _extract_pptx(p, pages, offset, limit)
        if k == "odt":
            return _extract_odt(p, offset, limit)
        if k == "ods":
            return _extract_ods(p, sheet, offset, limit)
        if k == "odp":
            return _extract_odp(p, pages)
        if k == "epub":
            return _extract_epub(p, pages, offset, limit)
        if k == "rtf":
            return _extract_rtf(p, offset, limit)
        if k == "sqlite":
            return _extract_sqlite(p, sheet, offset, limit)
        if k == "zip":
            return _extract_zip(p, sheet, limit)
        if k == "tar":
            return _extract_tar(p, sheet, limit)
        if k == "gzip":
            return _extract_gzip(p)
        if k == "image":
            return _extract_image(p, size, want_preview)
        if k in ("audio", "video"):
            return _extract_media(p, k, size)
        if k == "font":
            return _extract_font(p, size)
        if k == "legacy_office":
            return _extract_legacy(p, size, offset, limit)
        if k == "pdf":
            return _extract_pdf(p, pages)
        if k == "ipynb":
            return _extract_ipynb(p, offset, limit, size)
        if k in TEXT_FORMAT_KINDS or k == "text":
            return _extract_textfmt(p, k, offset, limit, size)
        return _extract_binary(p, size)
    except FormatError as e:
        return _failed(p, kind, str(e))
    except RecursionError:
        return _failed(p, kind, "the document is nested too deeply to read")
    except MemoryError:
        return _failed(p, kind, "the file is too large to read safely")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, struct.error, zlib.error, EOFError,
            zipfile.BadZipFile, tarfile.TarError, sqlite3.Error, ET.ParseError, UnicodeError, lzma.LZMAError) as e:
        return _failed(p, kind, f"the file is damaged or not in the expected format ({type(e).__name__}: {str(e)[:120]})")


def _failed(p: Path, kind: str | None, msg: str) -> Extracted:
    return Extracted(kind or "binary", f"[Cannot read {p.name}: {msg}]", {"error": msg})


# ─── helpers for callers ────────────────────────────────────────────────────

def text_note(kind: str, text: str, path: Path) -> str:
    """One short line about a text file Read shows as raw numbered lines (shape, delimiter, validity). Never raises."""
    try:
        if kind == "csv":
            delim = detect_delimiter(text, path.suffix.lower())
            rows, more = parse_csv(text, delim, 3)
            names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
            return f"{names.get(delim, delim)}-separated, {len(rows[0]) if rows else 0} columns: " + ", ".join(rows[0][:12]) if rows else ""
        if kind == "json":
            try:
                return "valid JSON, " + _shape(json.loads(text))
            except ValueError as e:
                return f"invalid JSON ({str(e)[:80]})"
        if kind == "jsonl":
            lines = [x for x in text.split("\n") if x.strip()]
            bad = 0
            for x in lines[:2000]:
                try:
                    json.loads(x)
                except ValueError:
                    bad += 1
            return f"JSON Lines, {len(lines)} records" + (f", {bad} invalid" if bad else "")
        if kind == "toml":
            return "valid TOML, " + _shape(_toml_module().loads(text))
        if kind in ("xml", "svg"):
            root = parse_xml(text.encode("utf-8", "replace"))
            return f"well-formed XML, root <{_ln(root.tag)}>"
        if kind == "html":
            _t, title, links = html_to_text(text[:2_000_000])
            return "HTML source (never rendered)" + (f", title {title!r}" if title else "")
    except FormatError as e:
        return str(e)
    except Exception as e:
        return f"not valid {kind.upper()} ({str(e)[:80]})" if kind in ("toml", "xml", "svg") else ""
    return ""


def prepare_model_image(path: Path, size: int, max_bytes: int) -> tuple[Path, str, str] | None:
    """(file, media type, note) of a picture the model can take: the original when it is PNG/JPEG/GIF/WebP within
    *max_bytes*, else a sips conversion; None when neither works."""
    ext = path.suffix.lower()
    media = MODEL_IMAGE_MEDIA.get(ext)
    if media and size <= max_bytes:
        return path, media, ""
    conv = convert_image(path, 2000)
    if conv and conv[0].stat().st_size <= max_bytes:
        why = "converted from " + (ext.lstrip(".").upper() or "its format") if not media else "shrunk to fit"
        return conv[0], conv[1], why
    return None


def iter_lines_bounded(f: Any, max_line: int = 20000) -> Iterator[tuple[str, int]]:
    """Yield (text, dropped_chars) per line of a binary file object without ever holding a whole giant line."""
    while True:
        chunk = f.readline(max_line * 4 + 1)
        if not chunk:
            return
        dropped = 0
        if not chunk.endswith(b"\n") and len(chunk) > max_line * 4:
            while True:
                more = f.readline(1 << 20)
                if not more:
                    break
                dropped += len(more)
                if more.endswith(b"\n"):
                    break
        s = chunk.rstrip(b"\r\n").decode("utf-8", "replace")
        if len(s) > max_line:
            dropped += len(s) - max_line
            s = s[:max_line]
        yield s, dropped
