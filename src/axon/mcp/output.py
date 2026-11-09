"""
MCP tool output limits.

* ``MAX_MCP_OUTPUT_TOKENS`` (default 25,000) caps what one successful tool result may put into the
  conversation; a result over the cap (or over 50,000 characters, whatever its token count) is written to a
  file in the session's ``outputs`` folder and replaced by a message naming that file.
* A tool can raise its own threshold with ``_meta["anthropic/maxResultSizeChars"]`` (up to 500,000
  characters); that value replaces both limits for text content.
* Over 10,000 tokens a warning goes to the server's log.
* An error result longer than about 11,000 characters keeps its first and last 5,000.
* PNG/JPEG/GIF/WebP images are saved as files; the model gets the path and sees the image inline.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Callable

from axon.mcp.client import render_content_block

logger = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 25_000
WARN_TOKENS = 10_000
DEFAULT_PERSIST_CHARS = 50_000
MAX_PERSIST_CHARS = 500_000
ERROR_LIMIT = 11_000
ERROR_KEEP = 5_000
IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif",
             "image/webp": "webp"}


def env_int(*names: str, default: int) -> int:
    for n in names:
        raw = os.environ.get(n, "").strip()
        if raw:
            try:
                return int(float(raw))
            except ValueError:
                continue
    return default


def max_output_tokens() -> int:
    v = env_int("MAX_MCP_OUTPUT_TOKENS", "AXON_MAX_MCP_OUTPUT_TOKENS", default=DEFAULT_MAX_OUTPUT_TOKENS)
    return v if v > 0 else DEFAULT_MAX_OUTPUT_TOKENS


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def tool_result_limit(tool_spec: dict[str, Any] | None) -> int | None:
    """The tool's own persist threshold from ``_meta["anthropic/maxResultSizeChars"]`` (clamped)."""
    meta = (tool_spec or {}).get("_meta")
    value = meta.get("anthropic/maxResultSizeChars") if isinstance(meta, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(min(value, MAX_PERSIST_CHARS))


def _outputs_dir(workspace: Path) -> Path | None:
    try:
        from axon import storage
        return storage.outputs_dir(Path(workspace), storage.active_session_id())
    except Exception:
        return None


def _unique(path: Path) -> Path:
    base, n = path, 2
    while path.exists():
        path = base.with_name(f"{base.stem}-{n}{base.suffix}")
        n += 1
    return path


def save_text(workspace: Path, name: str, text: str) -> Path | None:
    d = _outputs_dir(workspace)
    if d is None:
        return None
    try:
        path = _unique(d / f"{_safe(name)}.txt")
        path.write_text(text, encoding="utf-8", errors="replace")
        return path
    except OSError:
        return None


def save_bytes(workspace: Path, name: str, ext: str, data: bytes) -> Path | None:
    d = _outputs_dir(workspace)
    if d is None:
        return None
    try:
        path = _unique(d / f"{_safe(name)}.{ext}")
        path.write_bytes(data)
        return path
    except OSError:
        return None


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:100] or "mcp-result"


def render_for_model(result: dict[str, Any], *, workspace: Path, label: str,
                     images: list[Any] | None = None) -> tuple[str, bool, bool]:
    """Like :func:`axon.mcp.client.render_tool_result` but image blocks are saved to files, and ``images``
    collects references the model sees inline. Returns (text, is_error, has_image)."""
    import json
    parts: list[str] = []
    has_image = False
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "image":
            has_image = True
            mime = str(block.get("mimeType") or "").lower()
            ext = IMAGE_EXT.get(mime)
            raw = b""
            try:
                raw = base64.b64decode(str(block.get("data") or ""), validate=False)
            except Exception:
                pass
            path = save_bytes(workspace, f"{label}-image", ext, raw) if ext and raw else None
            if path is not None:
                parts.append(f"[image: {mime}, {len(raw)} bytes, saved to {path}]")
                if images is not None and _inline_size_ok(len(raw)):
                    from axon.providers.base import ImageRef
                    images.append(ImageRef(str(path), "image/jpeg" if mime == "image/jpg" else mime))
            else:
                parts.append(render_content_block(block))
        else:
            parts.append(render_content_block(block))
    text = "\n".join(p for p in parts if p)
    structured = result.get("structuredContent")
    if structured is not None:
        dumped = json.dumps(structured, ensure_ascii=False, indent=2)
        if not text:
            text = dumped
        elif dumped not in text and json.dumps(structured, ensure_ascii=False) not in text:
            text = f"{text}\n\nstructuredContent:\n{dumped}"
    if not text:
        text = "(no content)"
    return text, bool(result.get("isError")), has_image


def _inline_size_ok(size: int) -> bool:
    try:
        from axon.providers.base import MAX_TOOL_IMAGE_BYTES
    except ImportError:
        return False
    return size <= MAX_TOOL_IMAGE_BYTES


def limit_text(text: str, *, is_error: bool, has_image: bool, own_limit: int | None, workspace: Path,
               label: str, log: Callable[[str], None] | None = None) -> str:
    """Apply the error-truncation / persist-to-file rules to rendered tool output."""
    if is_error:
        if len(text) > ERROR_LIMIT:
            removed = len(text) - 2 * ERROR_KEEP
            return (text[:ERROR_KEEP] + f"\n\n[... {removed:,} characters removed ...]\n\n" + text[-ERROR_KEEP:])
        return text
    tokens = estimate_tokens(text)
    if tokens > WARN_TOKENS:
        msg = f"large MCP output from {label}: ~{tokens:,} tokens ({len(text):,} characters)"
        logger.debug(msg)
        if log:
            log(msg)
        from axon import notices
        notices.post(f"MCP tool output is large: {msg} (MAX_MCP_OUTPUT_TOKENS limits it)")
    if has_image:  # results with image data stay under the token limit, whatever the tool declares
        limit = max_output_tokens() * 4
        if len(text) > limit:
            return text[:limit] + f"\n\n[... output truncated: {len(text) - limit:,} more characters ...]"
        return text
    if own_limit is not None:
        too_big = len(text) > own_limit
    else:
        too_big = len(text) > DEFAULT_PERSIST_CHARS or tokens > max_output_tokens()
    if not too_big:
        return text
    path = save_text(workspace, label, text)
    if path is None:  # cannot write a file: keep the start so the call is still useful
        keep = own_limit if own_limit is not None else min(DEFAULT_PERSIST_CHARS, max_output_tokens() * 4)
        return text[:keep] + f"\n\n[... output truncated: {len(text) - keep:,} more characters ...]"
    return (f"[MCP result from {label} is too large for the conversation: {len(text):,} characters "
            f"(~{tokens:,} tokens). The full output was saved to {path}. Read that file (Read with offset "
            f"and limit) when you need its content.]")
