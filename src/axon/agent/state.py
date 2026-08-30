"""
State management: Conversation, FileState, TodoState.
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from axon.errors import StaleFileError
from axon.providers.base import AssistantTurn, ToolResultBlock

class Conversation:
    """Encapsulates the provider-encodable message list."""
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages: list[dict[str, Any]] = list(messages or [])

    def append_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def append_assistant(self, turn: AssistantTurn) -> None:
        if turn.native is not None:
            # Replay native content verbatim (ADR-003)
            if isinstance(turn.native, dict):
                self.messages.append(turn.native)
            else:
                self.messages.append({"role": "assistant", "content": turn.native})
        else:
            # Build content blocks for synthetic / fake turns
            blocks = []
            for b in turn.blocks:
                if hasattr(b, "text"):
                    blocks.append({"type": "text", "text": b.text})
                elif hasattr(b, "input"):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            self.messages.append({"role": "assistant", "content": blocks or turn.text})

    def append_tool_results(self, results: list[ToolResultBlock], provider_encoder: Any = None) -> None:
        """
        Batch all tool results into one user message (Law 2 / Anthropic)
        or provider-encoded messages (OpenAI).
        """
        if provider_encoder:
            encoded = provider_encoder(results)
            self.messages.extend(encoded)
        else:
            # Default Anthropic single user message with tool_result blocks
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.tool_use_id,
                    "content": r.content,
                    **({"is_error": True} if r.is_error else {})
                }
                for r in results
            ]
            self.messages.append({"role": "user", "content": content})

    def validate(self) -> None:
        """Validates that every tool_use has a corresponding tool_result in history (Law 1)."""
        pending_ids: set[str] = set()
        for m in self.messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            pending_ids.add(block["id"])
                        elif block.get("type") == "tool_result":
                            pending_ids.discard(block["tool_use_id"])
        if pending_ids:
            raise ValueError(f"Unpaired tool_use blocks remaining: {pending_ids}")

    def token_estimate(self) -> int:
        """Estimate tokens across conversation."""
        total_chars = 0
        for m in self.messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total_chars += len(c)
            elif isinstance(c, list):
                total_chars += sum(len(str(b)) for b in c)
        return int(total_chars / 3.7)


class FileState:
    """Tracks (mtime_ns, sha256) per file for read-before-edit invariants."""
    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, str]] = {}

    def record_read(self, p: Path) -> None:
        p = p.resolve()
        if p.exists() and p.is_file():
            stat = p.stat()
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            self._seen[p] = (stat.st_mtime_ns, digest)

    def check_writable(self, p: Path) -> None:
        """Enforces that file was read in this session and is not stale."""
        p = p.resolve()
        if not p.exists():
            # New file creation is writable
            return
        if p not in self._seen:
            raise StaleFileError(
                f"You have not read {p.name} in this session. "
                f"You must Read a file before editing it."
            )
        last_mtime, last_sha = self._seen[p]
        curr_stat = p.stat()
        if curr_stat.st_mtime_ns != last_mtime:
            curr_sha = hashlib.sha256(p.read_bytes()).hexdigest()
            if curr_sha != last_sha:
                raise StaleFileError(
                    f"{p.name} has changed on disk since you read it. "
                    f"Re-read the file to see the latest version before editing."
                )

    def invalidate(self, p: Path) -> None:
        self._seen.pop(p.resolve(), None)

    def clear(self) -> None:
        self._seen.clear()


@dataclass
class Todo:
    id: str
    content: str
    status: str  # "pending", "in_progress", "completed"

class TodoState:
    """Maintains task todo list with constraint of at most one in_progress."""
    def __init__(self) -> None:
        self.items: list[Todo] = []

    def clear(self) -> None:
        self.items.clear()

    def replace(self, items: list[dict[str, Any]]) -> None:
        in_prog_count = sum(1 for it in items if it.get("status") == "in_progress")
        if in_prog_count > 1:
            raise ValueError("At most one todo item can be 'in_progress' at any time.")
        self.items = [
            Todo(id=str(it.get("id", i)), content=it.get("content", ""), status=it.get("status", "pending"))
            for i, it in enumerate(items)
        ]

    def progress(self) -> tuple[int, int, int]:
        """Returns (completed_count, total_count, percentage)."""
        if not self.items:
            return 0, 0, 0
        total = len(self.items)
        completed = sum(1 for t in self.items if t.status == "completed")
        pct = int(completed / total * 100)
        return completed, total, pct

    def current_task(self) -> str | None:
        """Return the description of the currently in_progress task."""
        for t in self.items:
            if t.status == "in_progress":
                return t.content
        return None

    def render(self) -> str:
        if not self.items:
            return "No todos recorded."
        completed, total, pct = self.progress()
        bar_len = 14
        filled = int(bar_len * completed / total) if total > 0 else 0
        prog_bar = "█" * filled + "░" * (bar_len - filled)

        lines = [f"Progress: [{prog_bar}] {completed}/{total} ({pct}%)"]
        curr = self.current_task()
        if curr:
            lines.append(f"Active Step: {curr}")
        lines.append("")
        for idx, t in enumerate(self.items, 1):
            if t.status == "completed":
                mark = "[✓]"
            elif t.status == "in_progress":
                mark = "[▶]"
            else:
                mark = "[ ]"
            lines.append(f"  {mark} {idx}. {t.content}")
        return "\n".join(lines)


@dataclass
class QueuedMessage:
    id: int
    text: str

class MessageQueue:
    """Manages sequential user prompt queue for batching and autonomous turns."""
    def __init__(self) -> None:
        self.items: list[QueuedMessage] = []
        self._counter: int = 0

    def push(self, text: str) -> QueuedMessage:
        self._counter += 1
        item = QueuedMessage(id=self._counter, text=text.strip())
        self.items.append(item)
        return item

    def pop(self) -> QueuedMessage | None:
        if self.items:
            return self.items.pop(0)
        return None

    def peek(self) -> QueuedMessage | None:
        if self.items:
            return self.items[0]
        return None

    def remove(self, item_id: int) -> bool:
        for i, it in enumerate(self.items):
            if it.id == item_id:
                self.items.pop(i)
                return True
        return False

    def clear(self) -> None:
        self.items.clear()

    def __len__(self) -> int:
        return len(self.items)

    def render(self) -> str:
        if not self.items:
            return "No messages in queue."
        lines = [f"Message Queue ({len(self.items)} pending):"]
        for idx, it in enumerate(self.items, 1):
            tag = " [Next]" if idx == 1 else ""
            lines.append(f"  #{it.id}{tag}: {it.text}")
        return "\n".join(lines)
