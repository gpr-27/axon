"""Input too large to send whole still gets answered.

The full text is saved under the session's ``pastes/`` folder. The model receives the start and end, which
hold whatever the user typed around a paste, plus the saved path, and reads the rest with its file tools.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from axon import storage

AUTO_LIMIT_CAP = 100_000             # tokens; even a huge window is not worth filling with one message
AUTO_LIMIT_WINDOW_SHARE = 4          # at most a quarter of the context window
MAX_EXCERPT_TOKENS = 12_000
MIN_RECOVERY_LIMIT = 2_000
COST_NOTICE_TOKENS = 20_000          # input this large gets a cost notice even when it is sent whole
CHARS_PER_TOKEN = 3.7
EXTRA_BYTES_PER_TOKEN = 4.0          # multi-byte text takes more tokens per character

_OVERFLOW_PHRASES = re.compile(
    r"context[_ ]length|context window|prompt is too long|maximum context|too many tokens|"
    r"reduce the length|request too large|input is too long|payload too large", re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    extra_bytes = len(text.encode("utf-8", "replace")) - len(text)
    return int(len(text) / CHARS_PER_TOKEN + extra_bytes / EXTRA_BYTES_PER_TOKEN)


def inline_limit(window: int, setting: int = 0) -> int | None:
    """Largest single message to send whole, in tokens. ``setting``: 0 automatic, above 0 exact, below 0 no limit."""
    if setting < 0:
        return None
    if setting > 0:
        return setting
    return min(AUTO_LIMIT_CAP, window // AUTO_LIMIT_WINDOW_SHARE)


def is_context_overflow(err: BaseException | None) -> bool:
    """True when a provider refused a request for being too large."""
    if err is None:
        return False
    return getattr(err, "status", None) == 413 or bool(_OVERFLOW_PHRASES.search(str(err)))


def save_full_text(text: str) -> Path | None:
    """Write *text* to the active session's ``pastes/`` folder. None when it could not be saved."""
    try:
        data = text.encode("utf-8", "replace")
        target = storage.active_session_subdir("pastes") / f"{hashlib.sha256(data).hexdigest()[:16]}.txt"
        if not target.exists():
            target.write_bytes(data)
        return target
    except OSError:
        return None


def excerpt(text: str, limit_tokens: int, path: Path) -> str:
    """The start and end of *text* with a note saying where the rest is, at most about *limit_tokens*."""
    budget = max(200, min(limit_tokens // 2, MAX_EXCERPT_TOKENS))
    side = int(budget * CHARS_PER_TOKEN / 2)
    head, tail = text[:side], text[-side:]
    if "\n" in head:
        head = head[:head.rfind("\n")]
    if "\n" in tail:
        tail = tail[tail.find("\n") + 1:]
    total = text.count("\n") + 1
    omitted = max(0, total - head.count("\n") - tail.count("\n") - 2)
    note = (f"[Axon: this input is too large to send whole: {total:,} lines, about {estimate_tokens(text):,} tokens. "
            f"Its start and end are shown here, with {omitted:,} lines left out between them. The complete text is "
            f"saved at {path}. Read the rest from that file with Read (offset and limit) or Grep, and base your "
            f"answer on the whole input where the task needs it.]")
    return f"{head}\n\n{note}\n\n{tail}"


def fit(text: str, limit_tokens: int | None) -> tuple[str, Path | None]:
    """(text to send, path of the saved original). The text is unchanged when it fits or cannot be saved."""
    if limit_tokens is None or not text or estimate_tokens(text) <= limit_tokens:
        return text, None
    path = save_full_text(text)
    if path is None:
        return text, None
    return excerpt(text, limit_tokens, path), path


PASTE_CHIP_RE = re.compile(r"\[Pasted text #(\d+)(?: \+\d+ lines)?\]")


def _human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _human_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} KB"
    return f"{n} B"


def paste_notice(text: str, window: int = 200_000, setting: int = 0) -> str:
    """The note shown when a paste collapses into a chip: its size, a token estimate and whether all of it is sent."""
    tokens = estimate_tokens(text)
    limit = inline_limit(window, setting)
    size = _human_bytes(len(text.encode("utf-8", "replace")))
    note = f"pasted {size}, ~{_human_tokens(tokens)} tokens"
    if limit is not None and tokens > limit:
        return f"{note}; only the start and end will be sent"
    return f"{note} · alt+e expands or edits it"


def expand_for_send(buf: str, pastes: dict[int, str], limit_tokens: int | None) -> str:
    """The buffer with its paste chips replaced by their content. When the result would be too large to send
    whole, each oversized chip is spilled on its own (full text saved, start and end kept), so the text typed
    between chips always reaches the model in full."""
    full = PASTE_CHIP_RE.sub(lambda m: pastes.get(int(m.group(1)), m.group(0)), buf)
    if limit_tokens is None or estimate_tokens(full) <= limit_tokens:
        return full
    chips = [m for m in PASTE_CHIP_RE.finditer(buf) if int(m.group(1)) in pastes]
    if not chips:
        return full
    budget = max(MIN_RECOVERY_LIMIT, limit_tokens // (2 * len(chips)))

    def one(m: re.Match[str]) -> str:
        text = pastes[int(m.group(1))]
        return fit(text, budget)[0]
    return PASTE_CHIP_RE.sub(lambda m: one(m) if int(m.group(1)) in pastes else m.group(0), buf)


def shrink_messages(messages: list[dict[str, Any]], limit_tokens: int) -> bool:
    """Fit every plain user message in place, for history from a resumed session or a provider's rejection."""
    changed = False
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            fitted, path = fit(content, limit_tokens)
            if path is not None:
                message["content"] = fitted
                changed = True
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    fitted, path = fit(block["text"], limit_tokens)
                    if path is not None:
                        block["text"] = fitted
                        changed = True
    return changed
