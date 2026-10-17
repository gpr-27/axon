"""
Shared text-file I/O for the file tools.

* Decoding with BOM detection and fallback encodings (utf-8 -> cp1252 -> latin-1).
* Binary detection.
* Line-ending detection so edits preserve CRLF files byte-for-byte.
* Atomic writes (temp file in the same directory + fsync + os.replace) with a
  retry loop for Windows, where os.replace raises PermissionError while an
  editor/antivirus/indexer briefly holds the target open.
"""
from __future__ import annotations

import codecs
import os
import time
from dataclasses import dataclass
from pathlib import Path

from axon.errors import ToolError

BINARY_SNIFF_BYTES = 8192
MAX_TEXT_BYTES = 20 * 1024 * 1024  # refuse to load text files above 20 MB

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".avif"}


@dataclass
class TextFile:
    """Decoded file contents plus what is needed to write it back faithfully."""
    text: str          # content with line endings normalized to "\n" when `newline` is uniform
    encoding: str      # codec name usable with str.encode (utf-8-sig/utf-16 keep their BOM)
    newline: str       # "\n", "\r\n", or "" when the file mixes endings (text kept verbatim)


def is_binary_bytes(data: bytes) -> bool:
    """Heuristic: NUL bytes (outside UTF-16 BOM files) or mostly non-text bytes."""
    if not data:
        return False
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return False
    sample = data[:BINARY_SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    # Control characters other than \t \n \r \f \b \x1b are rare in text.
    text_chars = bytes({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7F})
    nontext = sample.translate(None, text_chars)
    return len(nontext) / len(sample) > 0.30


def decode_bytes(data: bytes) -> tuple[str, str]:
    """Decode bytes, returning (text, encoding). Never raises."""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode("utf-32", errors="replace"), "utf-32"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace"), "utf-16"
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def detect_newline(text: str) -> str:
    """Return "\\r\\n" or "\\n" for files with uniform endings, "" for mixed files."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return ""
    if crlf:
        return "\r\n"
    return "\n"


def read_text_file(path: Path) -> TextFile:
    """Read and decode a text file. Raises ToolError for binary or oversized files."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ToolError(f"Cannot stat {path.name}: {e}") from e
    if size > MAX_TEXT_BYTES:
        raise ToolError(f"{path.name} is {size / 1_048_576:.1f} MB, too large to load as text.")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ToolError(f"Failed to read {path.name}: {e}") from e
    if is_binary_bytes(data):
        raise ToolError(f"{path.name} appears to be a binary file; refusing to treat it as text.")
    text, enc = decode_bytes(data)
    nl = detect_newline(text)
    if nl == "\r\n":
        text = text.replace("\r\n", "\n")
    return TextFile(text=text, encoding=enc, newline=nl)


def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n")


def encode_for_disk(text: str, newline: str, encoding: str) -> bytes:
    """Re-apply the original line ending and encode. Falls back to UTF-8 when the
    original single-byte encoding cannot represent new characters."""
    if newline == "\r\n":
        text = normalize_newlines(text).replace("\n", "\r\n")
    try:
        return text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode("utf-8")


def reencoding_note(text: str, encoding: str) -> str:
    """Note for the tool result when ``text`` cannot be written back in the file's original
    encoding, so encode_for_disk falls back to UTF-8 (the model was promised encoding preservation)."""
    try:
        text.encode(encoding)
        return ""
    except (UnicodeEncodeError, LookupError):
        return f" Note: the new text has characters {encoding} cannot represent, so the file was re-encoded as UTF-8."


def _replace_with_retry(src: str, dst: str, attempts: int = 6) -> None:
    """os.replace with backoff: on Windows the target may be transiently locked
    (WinError 5 / 32) by an editor, antivirus or search indexer."""
    delay = 0.05
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically replace `target` with `data` (binary mode: no newline translation)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    # pid + random suffix: concurrent writers in one process (parallel subagents) must not share a temp file
    tmp = target.parent / f".{target.name}.{os.getpid()}.{os.urandom(3).hex()}.axon-tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if target.exists():
            try:
                os.chmod(tmp, target.stat().st_mode & 0o7777)
            except OSError:
                pass
        try:
            _replace_with_retry(str(tmp), str(target))
        except PermissionError as e:
            raise ToolError(
                f"Could not write {target.name}: the file is locked by another process ({e}). "
                f"Close it in other programs and retry."
            ) from e
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(target: Path, text: str, encoding: str = "utf-8", newline: str = "\n") -> None:
    atomic_write_bytes(target, encode_for_disk(text, newline, encoding))
