"""Large attachments sent once and reused by id.

On the official Anthropic host a large image, PDF or text document is uploaded once to ``POST /v1/files`` (GA, no beta
header; 500 MB per file) and every later request refers to it by ``file_id`` instead of carrying the base64 again.
On every other provider the inline base64 path stays as it is, and the same index records each attachment once
(content hash, name, size, how often it was sent) with a local copy, so ``/files`` works the same everywhere.

The substitution runs on the converted Anthropic messages right before a request is sent (``use_file_ids``).
Uploads that fail, files that expire or vanish (404) fall back to the inline copy; uploaded files are deleted when
the process exits unless ``AXON_FILES_KEEP=1``.
"""
from __future__ import annotations

import atexit
import base64
import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

NOTE = "uploaded once · reused by id"
LOCAL_NOTE = "attached once · cached locally"
MAX_FILE_BYTES = 500 * 1024 * 1024
UPLOAD_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf", "text/plain"}
EXPIRES_IN_S = 7 * 24 * 3600
MIN_BYTES_DEFAULT = 256 * 1024
BLOB_CAP = 16 * 1024 * 1024
_LOCK = threading.RLock()
_native_seen = False                  # a request went to the official host in this process


def min_bytes() -> int:
    try:
        return int(os.environ.get("AXON_FILES_MIN_BYTES", MIN_BYTES_DEFAULT))
    except ValueError:
        return MIN_BYTES_DEFAULT


def files_dir() -> Path:
    from axon import storage
    return storage.ensure_dir(storage.axon_home() / "files")


def is_missing_file_error(status: Any, text: str) -> bool:
    """A request failed because a referenced file_id is gone or expired."""
    t = (text or "").lower()
    return status in (400, 404) and ("file" in t) and any(k in t for k in ("not found", "no such", "expired", "does not exist", "file_id", "not_found"))


# ─── HTTP ─────────────────────────────────────────────────────────────────
class FilesClient:
    def __init__(self, base_url: str, api_key: str, transport: Any = None, timeout: float = 120.0) -> None:
        import httpx
        from axon.providers.anthropic_wire import api_version
        self.base = base_url.rstrip("/").removesuffix("/v1") + "/v1/files"
        self.client = httpx.Client(transport=transport, timeout=timeout,
                                   headers={"x-api-key": api_key, "anthropic-version": api_version(base_url)})

    def upload(self, name: str, data: bytes, mime: str, expires_in: int | None = EXPIRES_IN_S) -> dict[str, Any]:
        form = {"expires_in_seconds": str(expires_in)} if expires_in else {}
        r = self.client.post(self.base, files={"file": (name, data, mime)}, data=form)
        r.raise_for_status()
        return r.json()

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        r = self.client.get(self.base, params={"limit": limit})
        r.raise_for_status()
        return r.json().get("data", [])

    def delete(self, file_id: str) -> bool:
        r = self.client.delete(f"{self.base}/{file_id}")
        return r.status_code < 300 or r.status_code == 404


# ─── index ────────────────────────────────────────────────────────────────
class Index:
    """``~/.axon/files/index.json``: content hash -> entry. Local blobs live in ``blobs/<hash>``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or files_dir()
        self.path = self.root / "index.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def blob(self, sha: str) -> Path:
        return self.root / "blobs" / sha


def key_fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:10]


class FileSession:
    """Substitutes file ids for large inline attachments. *native* is False on every host but api.anthropic.com."""

    def __init__(self, base_url: str, api_key: str, *, transport: Any = None, index: Index | None = None, minimum: int | None = None,
                 label: str = "") -> None:
        from axon.providers.transport import is_official_anthropic
        self.base_url, self.key = base_url, api_key
        self.native = bool(api_key) and is_official_anthropic(base_url)
        self.transport, self.index = transport, index or Index()
        self.minimum = min_bytes() if minimum is None else minimum
        self.fp = key_fp(api_key) if api_key else ""
        self.uploaded_now: set[str] = set()            # file ids this process created
        self._failed: dict[str, float] = {}
        self.label = label

    def client(self) -> FilesClient:
        return FilesClient(self.base_url, self.key, transport=self.transport)

    # -- walking the messages
    def substitute(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """*messages* with large base64 image/document blocks replaced by file references (a copy; the input is untouched)."""
        if not any(_has_large(m, self.minimum) for m in messages):
            return messages
        out = copy.deepcopy(messages)
        for m in out:
            c = m.get("content")
            if isinstance(c, list):
                self._blocks(c)
        return out

    def _blocks(self, blocks: list[Any]) -> None:
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                self._blocks(b["content"])
            src = b.get("source")
            if b.get("type") in ("image", "document") and isinstance(src, dict) and src.get("type") == "base64":
                new = self._one(b, src)
                if new is not None:
                    blocks[i] = new

    def _one(self, block: dict[str, Any], src: dict[str, Any]) -> dict[str, Any] | None:
        data64, mime = str(src.get("data") or ""), str(src.get("media_type") or "")
        if len(data64) * 3 // 4 < self.minimum or mime not in UPLOAD_MIMES:
            return None
        try:
            raw = base64.b64decode(data64)
        except ValueError:
            return None
        if len(raw) > MAX_FILE_BYTES:
            return None
        sha = hashlib.sha256(raw).hexdigest()
        ext = {"application/pdf": "pdf", "text/plain": "txt"}.get(mime, mime.split("/")[-1])
        entry = self._entry(sha, f"attachment-{sha[:8]}.{ext}", mime, raw)
        if not self.native:
            return None
        fid = self._file_id(entry, sha, raw)
        if not fid:
            return None
        out = dict(block)
        out["source"] = {"type": "file", "file_id": fid}
        return out

    # -- index bookkeeping
    def _entry(self, sha: str, name: str, mime: str, raw: bytes) -> dict[str, Any]:
        with _LOCK:
            data = self.index.load()
            e = data.setdefault(sha, {"sha": sha, "name": name, "mime": mime, "size": len(raw), "uses": 0, "file_id": None,
                                      "key": "", "expires_at": None, "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())})
            e["uses"] = int(e.get("uses", 0)) + 1
            if not self.native and not self.index.blob(sha).exists() and len(raw) <= BLOB_CAP:
                self.index.blob(sha).parent.mkdir(parents=True, exist_ok=True)
                self.index.blob(sha).write_bytes(raw)
                e["local_copy"] = True
            self.index.save(data)
            return e

    def _file_id(self, entry: dict[str, Any], sha: str, raw: bytes) -> str | None:
        global _native_seen
        _native_seen = True
        fid = entry.get("file_id")
        exp = entry.get("expires_at") or 0
        if fid and entry.get("key") == self.fp and (not exp or exp > time.time() + 60):
            return str(fid)
        if time.time() - self._failed.get(sha, 0) < 600:
            return None
        try:
            meta = self.client().upload(entry["name"], raw, entry["mime"])
        except Exception:                                   # noqa: BLE001 - the inline copy still works
            self._failed[sha] = time.time()
            return None
        with _LOCK:
            data = self.index.load()
            e = data.setdefault(sha, entry)
            e.update({"file_id": meta["id"], "key": self.fp, "host": self.base_url,
                      "expires_at": time.time() + EXPIRES_IN_S, "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())})
            self.index.save(data)
        self.uploaded_now.add(meta["id"])
        _register_cleanup(self)
        return str(meta["id"])

    # -- recovery and cleanup
    def forget_ids(self, messages: list[dict[str, Any]]) -> int:
        """Drop the cached ids of every file referenced in *messages* (a 404 or expiry): the next substitute() re-uploads."""
        ids = referenced_ids(messages)
        n = 0
        with _LOCK:
            data = self.index.load()
            for e in data.values():
                if e.get("file_id") in ids:
                    e["file_id"], e["expires_at"] = None, None
                    n += 1
            self.index.save(data)
        return n

    def delete_uploaded(self, only_mine: bool = False) -> int:
        """Delete uploaded files (those of this process, or every one in the index) and clear their ids."""
        with _LOCK:
            data = self.index.load()
            n = 0
            for e in data.values():
                fid = e.get("file_id")
                if not fid or (only_mine and fid not in self.uploaded_now):
                    continue
                try:
                    if self.native and self.client().delete(fid):
                        n += 1
                except Exception:                           # noqa: BLE001
                    continue
                e["file_id"], e["expires_at"] = None, None
            self.index.save(data)
        return n


def _has_large(message: dict[str, Any], minimum: int) -> bool:
    c = message.get("content")
    if not isinstance(c, list):
        return False
    for b in c:
        if not isinstance(b, dict):
            continue
        src = b.get("source")
        if b.get("type") in ("image", "document") and isinstance(src, dict) and src.get("type") == "base64":
            if len(str(src.get("data") or "")) * 3 // 4 >= minimum:
                return True
        if b.get("type") == "tool_result" and isinstance(b.get("content"), list) and _has_large({"content": b["content"]}, minimum):
            return True
    return False


def referenced_ids(messages: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()

    def walk(blocks: Any) -> None:
        for b in blocks if isinstance(blocks, list) else []:
            if isinstance(b, dict):
                src = b.get("source")
                if isinstance(src, dict) and src.get("type") == "file" and src.get("file_id"):
                    out.add(str(src["file_id"]))
                walk(b.get("content"))
    for m in messages:
        walk(m.get("content"))
    return out


_SESSIONS: dict[tuple[str, str], FileSession] = {}
_cleanup_registered = False


def session_for(base_url: str, api_key: str) -> FileSession:
    k = (base_url, api_key)
    with _LOCK:
        if k not in _SESSIONS:
            _SESSIONS[k] = FileSession(base_url, api_key)
        return _SESSIONS[k]


def use_file_ids(messages: list[dict[str, Any]], base_url: str, api_key: str) -> list[dict[str, Any]]:
    """The provider hook: large attachments by file id on the official host, deduplicated and indexed everywhere else."""
    if os.environ.get("AXON_FILES_API", "").strip().lower() in ("0", "off", "false", "no"):
        return messages
    global _native_seen
    try:
        sess = session_for(base_url, api_key)
        _native_seen = _native_seen or sess.native
        return sess.substitute(messages)
    except Exception:                                       # noqa: BLE001 - never break a request over the attachment cache
        return messages


def _register_cleanup(sess: FileSession) -> None:
    global _cleanup_registered
    if _cleanup_registered or os.environ.get("AXON_FILES_KEEP", "").strip() in ("1", "true", "yes"):
        return
    _cleanup_registered = True
    atexit.register(cleanup_all)


def cleanup_all() -> int:
    """Delete the files this process uploaded (run at exit and when a session ends)."""
    n = 0
    for s in list(_SESSIONS.values()):
        try:
            n += s.delete_uploaded(only_mine=True)
        except Exception:                                   # noqa: BLE001
            pass
    return n


def card_note(path: str | Path, size: int | None = None) -> str:
    """The one-line card note for a large attachment, '' when it will not be uploaded."""
    try:
        p = Path(path)
        size = p.stat().st_size if size is None else size
    except OSError:
        return ""
    if _native_seen and min_bytes() <= size <= MAX_FILE_BYTES:
        return NOTE
    return ""
