"""Prompt suggestions: after an answer, guess the user's next message and show it as ghost text.

This module holds the small, pure parts:

* ``build_request``  - the system prompt and the one user message sent to the small model;
* ``clean_reply``    - turns the model's reply into a usable suggestion, or "" to throw it away;
* ``SuggestionBox``  - the place where the finished suggestion waits for the prompt editor.

The agent (agent/loop.py) calls the model in a background thread; the prompt editor
(ui/input.py) reads the box while it waits for keys.
"""
from __future__ import annotations

import threading
from typing import Any, Collection

USER_LIMIT = 600          # characters of the user's last message sent to the model
ANSWER_LIMIT = 1200       # characters of the assistant's final answer
EARLIER_LIMIT = 200       # characters of each earlier user message
EARLIER_COUNT = 2
MAX_REPLY_CHARS = 100     # a longer reply is not a short follow-up
MAX_TOKENS = 40

SYSTEM_PROMPT = (
    "Predict what the user will most likely type next to this coding assistant: a short, concrete "
    "follow-up of 2-12 words, written as the user would write it (imperative or question), no quotes, "
    "no explanation. If there is no obvious next step reply with exactly NONE. "
    "Most answers leave a natural next step (apply the fix, check it, ask why, go deeper); "
    "use NONE only when the conversation is clearly finished."
)

# A reply containing one of these is the model explaining itself, not a message the user would type.
_EXPLANATION_MARKERS = ("i think", "you might", "you could", "i can't", "i cannot", "i'm sorry", "as an ai")
_QUOTE_CHARS = "\"'`*“”‘’"


def shorten(text: str, limit: int) -> str:
    """The first ``limit`` characters of ``text`` (whitespace trimmed)."""
    return str(text or "").strip()[:limit]


def message_text(message: dict[str, Any]) -> str:
    """The plain text of a conversation message; "" for tool results and other non-text content."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts)


def earlier_user_messages(messages: list[dict[str, Any]], count: int = EARLIER_COUNT) -> list[str]:
    """The user's messages before the latest one, oldest first (at most ``count``)."""
    texts = []
    for message in messages:
        if message.get("role") != "user":
            continue
        text = message_text(message).strip()
        if text:
            texts.append(text)
    return texts[:-1][-count:] if count > 0 else []


def build_request(user_message: str, answer: str, earlier: list[str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(system, messages) for the small model."""
    lines = []
    if earlier:
        lines.append("Earlier user messages, oldest first:")
        lines.extend(f"- {shorten(text, EARLIER_LIMIT)}" for text in earlier[-EARLIER_COUNT:])
        lines.append("")
    lines.append("The user's last message:")
    lines.append(shorten(user_message, USER_LIMIT))
    lines.append("")
    lines.append("The assistant's final answer:")
    lines.append(shorten(answer, ANSWER_LIMIT))
    system = [{"type": "text", "text": SYSTEM_PROMPT}]
    return system, [{"role": "user", "content": "\n".join(lines)}]


def _strip_noise(text: str) -> str:
    """Quotes, bullets and a closing full stop around the suggestion."""
    text = text.strip()
    for bullet in ("- ", "* ", "> ", "• "):
        if text.startswith(bullet):
            text = text[len(bullet):]
    text = text.strip().strip(_QUOTE_CHARS).strip()
    return text.rstrip(".").strip()


def clean_reply(reply: str, commands: Collection[str] = ()) -> str:
    """The suggestion hidden in the model's reply, or "" when the reply is not usable.

    ``commands`` are real slash command names (without the "/"): a reply starting with "/" is kept
    only when it starts with one of them."""
    raw = str(reply or "").strip()
    if not raw or "\n" in raw:
        return ""
    text = _strip_noise(raw)
    if not text or text.upper().rstrip(".!") == "NONE":
        return ""
    if len(text) > MAX_REPLY_CHARS or not any(ch.isalnum() for ch in text):
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in _EXPLANATION_MARKERS):
        return ""
    if text.startswith("/") and text[1:].split(" ", 1)[0].lower() not in commands:
        return ""
    return text


class SuggestionBox:
    """The latest suggestion, shared between the thread that makes it and the prompt editor.

    ``version`` changes whenever the text changes, so the editor can poll cheaply. ``clear`` also
    makes any suggestion still being generated stale: its result is dropped when it arrives."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""
        self._version = 0
        self._generation = 0
        self._pending = False
        self.live = False          # True only in the interactive session (not -p, not a pipe)

    def snapshot(self) -> tuple[str, int]:
        """(text, version)."""
        with self._lock:
            return self._text, self._version

    @property
    def text(self) -> str:
        return self.snapshot()[0]

    @property
    def pending(self) -> bool:
        """A suggestion is being generated right now."""
        with self._lock:
            return self._pending

    def clear(self) -> None:
        """Forget the suggestion (new turn, accepted, dismissed) and ignore one still on its way."""
        with self._lock:
            self._generation += 1
            self._pending = False
            if self._text:
                self._text = ""
                self._version += 1

    def start(self) -> int:
        """A generation begins; returns the ticket to pass to ``finish``."""
        with self._lock:
            self._generation += 1
            self._pending = True
            if self._text:
                self._text = ""
                self._version += 1
            return self._generation

    def finish(self, ticket: int, text: str) -> bool:
        """Store the result. Returns False (and stores nothing) when a newer turn made it stale."""
        with self._lock:
            if ticket != self._generation:
                return False
            self._pending = False
            if text != self._text:
                self._text = text
                self._version += 1
            return True

    def is_current(self, ticket: int) -> bool:
        with self._lock:
            return ticket == self._generation
