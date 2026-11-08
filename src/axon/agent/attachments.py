"""Files the user drops into the prompt are copied under ``~/.axon`` the moment they arrive.

A drop usually names a temporary file, so the copy happens at drop time, not at submit time. The prompt
shows a ``[File #N: name]`` chip; on submit the chip becomes a line telling the model where the saved copy
is. Each session's ``attachments/index.jsonl`` lists everything kept, images included.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axon import storage
from axon.permissions.paths import is_protected_path

MAX_STORED_BYTES = 2 * 1024 ** 3        # bigger files are referenced where they are, and the user is told
MAX_DROP_CHARS = 16_384                 # a drop is a few paths; anything longer is ordinary pasted text
MANIFEST = "index.jsonl"
FILE_CHIP_RE = re.compile(r"\[File #(\d+): ([^\]]*)\]")
_UNSAFE_NAME = re.compile(r"[^\w.\- ]+", re.UNICODE)

_registry: dict[int, "FileAttachment"] = {}
_counter = 1


class ProtectedFile(Exception):
    """The dropped file is a credential or system file, so it is not copied or sent."""


@dataclass(frozen=True)
class FileAttachment:
    index: int
    name: str
    stored_path: Path
    original_path: str
    size_bytes: int
    mime_type: str
    by_reference: bool = False          # too large to copy: stored_path is the original

    @property
    def chip(self) -> str:
        return f"[File #{self.index}: {self.name}]"


def reset_for_tests() -> None:
    global _counter
    _registry.clear()
    _counter = 1


def get(index: int) -> FileAttachment | None:
    return _registry.get(index)


def _as_path(token: str) -> Path:
    if token.startswith("file://"):
        from axon.agent.images import unescape_path
        token = unescape_path(token)
    return Path(token).expanduser()


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _split_drop(text: str) -> list[str]:
    """Split a drop into path tokens. On Windows a backslash is a separator, not an escape, so the POSIX
    splitter would turn C:\\Users\\a into C:Usersa; quotes are kept apart from the separators there."""
    if os.name != "nt":
        return shlex.split(text, posix=True)
    out = []
    for tok in shlex.split(text, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _looks_like_path_line(line: str) -> bool:
    line = line.strip()
    return line.startswith(("/", "~", "'", '"', "file://", "\\\\")) or bool(_WIN_DRIVE_RE.match(line))


def parse_dropped_paths(text: str) -> list[Path]:
    """The paths in *text* when all of it is existing paths, which is how a terminal delivers a drop; else []."""
    text = text.strip()
    if not text or len(text) > MAX_DROP_CHARS:
        return []
    if "\n" in text and not all(_looks_like_path_line(line) for line in text.splitlines()):
        return []
    try:
        tokens = _split_drop(text)
    except ValueError:
        return []
    if not tokens:
        return []
    paths = []
    for token in tokens:
        try:
            path = _as_path(token)
            if not path.is_absolute() or not path.exists():
                return []
        except (OSError, RuntimeError, ValueError):
            return []
        paths.append(path)
    return paths


def _safe_name(name: str) -> str:
    return (_UNSAFE_NAME.sub("_", name).strip(" .") or "file")[:80]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_attachment(kind: str, name: str, stored: Path, original: str, size: int, sha256: str,
                      mime: str = "", index: int = 0) -> None:
    """Add a line to the session's manifest of what the user gave Axon."""
    entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "index": index, "name": name, "stored": str(stored),
                             "original": original, "size": size, "sha256": sha256, "mime": mime}
    try:
        with open(storage.active_session_subdir("attachments") / MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def list_session_attachments() -> list[dict[str, Any]]:
    """Everything kept for the active session, oldest first."""
    rows = []
    try:
        text = (storage.active_session_subdir("attachments") / MANIFEST).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def ingest_file(path: Path) -> FileAttachment:
    """Copy *path* into the active session's ``attachments/`` folder. Raises ProtectedFile or OSError."""
    global _counter
    path = Path(path)
    if is_protected_path(path):
        raise ProtectedFile(path.name)
    size = path.stat().st_size
    name = path.name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    index = _counter
    _counter += 1
    if size > MAX_STORED_BYTES:
        att = FileAttachment(index, name, path, str(path), size, mime, by_reference=True)
        record_attachment("reference", name, path, str(path), size, "", mime, index)
    else:
        sha256 = _sha256(path)
        folder = storage.active_session_subdir("attachments")
        target = folder / f"{sha256[:12]}_{_safe_name(name)}"
        if not target.exists():
            partial = target.with_name(target.name + ".partial")
            shutil.copyfile(path, partial)
            os.chmod(partial, 0o600)
            os.replace(partial, target)
        att = FileAttachment(index, name, target, str(path), size, mime)
        record_attachment("file", name, target, str(path), size, sha256, mime, index)
    _registry[index] = att
    return att


def _lookup(index: int) -> tuple[str, str, int] | None:
    att = _registry.get(index)
    if att is not None:
        return att.name, str(att.stored_path), att.size_bytes
    for row in reversed(list_session_attachments()):
        if row.get("index") == index and row.get("kind") in ("file", "reference"):
            return str(row.get("name", "")), str(row.get("stored", "")), int(row.get("size") or 0)
    return None


def expand_file_chips(text: str) -> str:
    """Replace each ``[File #N: name]`` chip with where the file is saved, for the model."""
    def expand(match: re.Match[str]) -> str:
        found = _lookup(int(match.group(1)))
        if found is None:
            return match.group(0)
        name, stored, size = found
        return f"[File #{match.group(1)}: {name}, {size / 1024:.1f} KB, saved at {stored}]"
    return FILE_CHIP_RE.sub(expand, text)


def looks_like_typed_drop(text: str, workspace: Path | str) -> bool:
    """True when *text* is nothing but existing file paths outside *workspace* and carries no Enter or
    escape sequence: how a terminal without bracketed paste delivers a dropped file (one burst of keys).
    A path inside a sentence, a folder, a path in the workspace or a missing file is not a drop."""
    if not text or "\x1b" in text or "\r" in text or "\n" in text or len(text.strip()) < 3:
        return False
    paths = parse_dropped_paths(text)
    if not paths or any(not p.is_file() for p in paths):
        return False
    try:
        root = Path(workspace).resolve()
        return not any(p.resolve().is_relative_to(root) for p in paths)
    except (OSError, ValueError):
        return False


def secret_labels(data: bytes | str | Path) -> list[str]:
    """What kind of secret a text file's body looks like it holds (the patterns Axon uses for memory files and
    redaction); empty for binary files, big files and clean ones. Reads at most 256 KB; changes nothing."""
    try:
        raw = Path(data).read_bytes()[:256 * 1024] if isinstance(data, Path) else (data.encode("utf-8", "replace") if isinstance(data, str) else data[:256 * 1024])
        if b"\x00" in raw[:4096]:
            return []
        from axon.features.memory_files import scan_secrets
        return scan_secrets(raw.decode("utf-8", errors="replace"))
    except Exception:
        return []


def ingest_drop(text: str) -> tuple[str, str] | None:
    """(text for the prompt, notice) when *text* is a file drop; None when it is ordinary pasted text."""
    from axon.agent.images import IMAGE_EXTENSIONS, ingest_image_file
    paths = parse_dropped_paths(text)
    if not paths or any(p.is_dir() for p in paths):
        return None
    chips, notes = [], []
    for path in paths:
        try:
            if path.suffix.lower() in IMAGE_EXTENSIONS and not is_protected_path(path):
                image = ingest_image_file(str(path))
                if image is None:
                    raise OSError("not readable")
                chips.append(image.label)
                continue
            att = ingest_file(path)
            chips.append(att.chip)
            labels = secret_labels(path)
            if labels:
                notes.append(f"{att.name} looks like it holds a secret ({', '.join(labels[:3])}). It is sent to the model if "
                             f"the model reads it. Delete {att.chip} from the line to leave it out.")
            if att.by_reference:
                notes.append(f"{att.name} is too large to keep a copy; Axon will read it where it is.")
        except ProtectedFile:
            notes.append(f"{path.name} is a protected file, so it was not attached.")
        except OSError as e:
            notes.append(f"Could not save {path.name}: {e.strerror or e}")
    return " ".join(chips), " ".join(notes)
