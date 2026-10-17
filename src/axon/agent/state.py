"""
State management: Conversation, FileState, TodoState.
"""
from __future__ import annotations
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from axon.errors import StaleFileError
from axon.providers.base import AssistantTurn, TextBlock, ToolResultBlock, ToolUseBlock

def _extract_images_and_text(text: str) -> list[dict[str, Any]] | str:
    """Detect image filepaths in user prompt and encode them into multimodal content blocks and OCR context."""
    from axon.agent.images import compact_image_paths
    clean_text, attachments = compact_image_paths(text)

    if not attachments:
        return text

    blocks: list[dict[str, Any]] = []
    ocr_descriptions: list[str] = []
    for att in attachments:
        b64 = att.read_base64()
        if b64:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": att.mime_type,
                    "data": b64,
                }
            })
        dim_str = f" ({att.width}x{att.height})" if att.width and att.height else ""
        desc = f"• {att.label}: {Path(att.original_path).name}{dim_str}"
        ocr_t = att.get_ocr_text()
        if ocr_t:
            desc += f"\n  Extracted Text / OCR from screenshot:\n  \"\"\"\n  {ocr_t}\n  \"\"\""
        ocr_descriptions.append(desc)

    context_prefix = f"Attached Image Details ({len(attachments)} item{'s' if len(attachments) != 1 else ''}):\n" + "\n".join(ocr_descriptions)

    if clean_text:
        full_text = f"{clean_text}\n\n{context_prefix}"
        blocks.append({"type": "text", "text": full_text})
    elif not blocks:
        return text
    else:
        blocks.append({"type": "text", "text": f"Analyze attached image:\n\n{context_prefix}"})

    return blocks

_LEDGER_LOCK = threading.RLock()


def record_usage(ledger: Any, model: str, usage: Any, source: str = "main") -> Any:
    """Thread-safe ``ledger.record`` honoring the ``source=`` contract, with fallbacks
    for the older ``tag=`` keyword and the bare positional signature."""
    if ledger is None or usage is None:
        return None
    with _LEDGER_LOCK:
        try:
            return ledger.record(model, usage, source=source)
        except TypeError:
            pass
        try:
            return ledger.record(model, usage, tag=source)
        except TypeError:
            return ledger.record(model, usage)


def is_tool_result_message(m: dict[str, Any]) -> bool:
    """True for messages that carry tool results (Anthropic tool_result blocks or OpenAI role=tool)."""
    if m.get("role") == "tool":
        return True
    content = m.get("content")
    if m.get("role") == "user" and isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def is_turn_start(m: dict[str, Any]) -> bool:
    """A safe cut point: a genuine user message (not a tool result)."""
    return m.get("role") == "user" and not is_tool_result_message(m)


def estimate_content_tokens(content: Any) -> int:
    """Accurately estimate tokens for strings, tool blocks, and multimodal image blocks."""
    if isinstance(content, str):
        return max(1, int(len(content) / 3.7))
    if isinstance(content, list):
        total = 0
        for b in content:
            if isinstance(b, dict):
                b_type = b.get("type", "")
                if b_type == "image":
                    total += 1200
                elif b_type == "text":
                    total += max(1, int(len(b.get("text", "")) / 3.7))
                elif b_type == "tool_use":
                    total += int(len(str(b.get("input", ""))) / 3.7) + 20
                elif b_type == "tool_result":
                    total += int(len(str(b.get("content", ""))) / 3.7) + 10
                else:
                    total += max(1, int(len(str(b)) / 3.7))
            else:
                total += max(1, int(len(str(b)) / 3.7))
        return total
    return max(1, int(len(str(content)) / 3.7))

class Conversation:
    """Encapsulates the provider-encodable message list."""
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages: list[dict[str, Any]] = list(messages or [])

    def append_user(self, text: str) -> None:
        processed = _extract_images_and_text(text) if isinstance(text, str) else text
        self.messages.append({"role": "user", "content": processed})

    def append_assistant(self, turn: AssistantTurn) -> None:
        if turn.native is not None:
            # Replay native content verbatim (ADR-003)
            if isinstance(turn.native, dict):
                self.messages.append(turn.native)
            else:
                self.messages.append({"role": "assistant", "content": turn.native})
        else:
            # Reconstruct content blocks
            blocks = []
            for b in turn.blocks:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "text": b.text})
                elif isinstance(b, ToolUseBlock):
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            self.messages.append({"role": "assistant", "content": blocks})

    def append_tool_results(
        self,
        results: list[ToolResultBlock],
        provider_encoder: Callable[[list[ToolResultBlock]], list[dict[str, Any]]] | None = None,
    ) -> None:
        """Law 2: Results MUST be fed back in ONE batch message."""
        if provider_encoder:
            # Provider-specific encoding (e.g. OpenAI tool message format)
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
        """Estimate tokens across conversation, accounting for text, images and OpenAI tool_calls."""
        total = 0
        for m in self.messages:
            total += estimate_content_tokens(m.get("content") or "")
            if m.get("tool_calls"):
                total += estimate_content_tokens(str(m["tool_calls"]))
        return total

    def repair(self) -> int:
        """Restore API validity: close dangling tool_use blocks with error results and
        drop empty assistant messages. Returns the number of fixes applied."""
        fixes = 0
        cleaned: list[dict[str, Any]] = []
        for m in self.messages:
            if m.get("role") == "assistant":
                content = m.get("content")
                empty = (content is None or content == [] or content == "") and not m.get("tool_calls")
                if empty:
                    fixes += 1
                    continue
            cleaned.append(m)
        self.messages = cleaned

        answered: set[str] = set()
        for m in self.messages:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                answered.add(str(m["tool_call_id"]))
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        answered.add(str(b.get("tool_use_id")))

        out: list[dict[str, Any]] = []
        for idx, m in enumerate(self.messages):
            out.append(m)
            if m.get("role") != "assistant":
                continue
            ids: list[str] = []
            content = m.get("content")
            if isinstance(content, list):
                ids += [str(b.get("id")) for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            openai_style = bool(m.get("tool_calls"))
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    ids.append(str(tc_id))
            missing = [i for i in ids if i not in answered]
            if not missing:
                continue
            fixes += len(missing)
            if openai_style:
                for i in missing:
                    out.append({"role": "tool", "tool_call_id": i, "content": "Tool call was interrupted and did not run."})
            else:
                blocks = [
                    {"type": "tool_result", "tool_use_id": i, "content": "Tool call was interrupted and did not run.", "is_error": True}
                    for i in missing
                ]
                nxt = self.messages[idx + 1] if idx + 1 < len(self.messages) else None
                if nxt is not None and is_tool_result_message(nxt) and isinstance(nxt.get("content"), list):
                    nxt["content"] = list(nxt["content"]) + blocks
                else:
                    out.append({"role": "user", "content": blocks})
            answered.update(missing)
        self.messages = out
        return fixes


class FileState:
    """Tracks (mtime_ns, sha256) per file for read-before-edit invariants."""
    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, int, str]] = {}

    def record_read(self, p: Path) -> None:
        p = p.resolve()
        if p.exists() and p.is_file():
            stat = p.stat()
            content = p.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            self._seen[p] = (stat.st_mtime_ns, stat.st_size, digest)

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
        last_mtime, last_size, last_sha = self._seen[p]
        curr_stat = p.stat()
        # mtime alone misses a write within the same clock tick (coarse on Windows), so size counts too
        if curr_stat.st_mtime_ns != last_mtime or curr_stat.st_size != last_size:
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
    active_form: str = ""


class TodoState:
    """Maintains task todo list with constraint of at most one in_progress.

    Once :meth:`bind` is called with the workspace and a session-id getter, every
    :meth:`replace` is persisted to ``axon.storage.todos_dir(ws)/<session_id>.json`` and the
    list is (re)loaded lazily whenever the active session id changes (``--continue``,
    ``--resume``, ``/resume``), so a resumed session gets its checklist back."""

    def __init__(self) -> None:
        self._items: list[Todo] = []
        self._workspace: Path | None = None
        self._sid_fn: Callable[[], str] | None = None
        self._loaded_sid: str | None = None
        self._lock = threading.RLock()

    # ── persistence ──
    def bind(self, workspace: Path | str, session_id: "Callable[[], str] | str") -> "TodoState":
        """Attach persistence. ``session_id`` is a string or a zero-arg callable returning the
        current id. Items set before binding (and not yet persisted) are kept."""
        with self._lock:
            self._workspace = Path(workspace)
            self._sid_fn = session_id if callable(session_id) else (lambda sid=str(session_id): sid)
            if self._items and self._loaded_sid is None:
                self._loaded_sid = self._sid()
                self._save()
        return self

    @property
    def bound(self) -> bool:
        return self._workspace is not None and self._sid_fn is not None

    def _sid(self) -> str:
        try:
            sid = str(self._sid_fn() or "") if self._sid_fn else ""
        except Exception:  # noqa: BLE001 - a broken getter must never break the agent
            return ""
        import re
        return sid if re.fullmatch(r"[A-Za-z0-9_.\-]+", sid) and sid not in (".", "..") else ""

    def path(self) -> Path | None:
        sid = self._sid()
        if not (self._workspace and sid):
            return None
        try:
            from axon.storage import todos_dir
            return todos_dir(self._workspace) / f"{sid}.json"
        except (ImportError, AttributeError, OSError):
            return None

    def _sync(self) -> None:
        """Reload from disk when the active session changed since the last load/save."""
        if not self.bound:
            return
        sid = self._sid()
        if sid == self._loaded_sid:
            return
        self._loaded_sid = sid
        self._items = []
        p = self.path()
        if p is None or not p.is_file():
            return
        try:
            import json
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        raw = data.get("todos") if isinstance(data, dict) else data
        if isinstance(raw, list):
            self._items = [
                Todo(id=str(it.get("id", i)), content=str(it.get("content", "")),
                     status=str(it.get("status", "pending")), active_form=str(it.get("activeForm", "") or ""))
                for i, it in enumerate(raw, 1) if isinstance(it, dict) and it.get("content")
            ]

    def _save(self) -> None:
        p = self.path()
        if p is None:
            return
        import json
        payload = [{"id": t.id, "content": t.content, "status": t.status,
                    **({"activeForm": t.active_form} if t.active_form else {})} for t in self._items]
        try:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            import os
            os.replace(tmp, p)
        except OSError:
            pass

    # ── list API ──
    @property
    def items(self) -> list[Todo]:
        with self._lock:
            self._sync()
            return self._items

    @items.setter
    def items(self, value: list[Todo]) -> None:
        with self._lock:
            self._sync()
            self._items = list(value)

    def clear(self) -> None:
        """Clear the in-memory list (the persisted file of the session is left untouched)."""
        with self._lock:
            self._sync()
            self._items = []

    def replace(self, items: list[dict[str, Any]]) -> None:
        in_prog_count = sum(1 for it in items if it.get("status") == "in_progress")
        if in_prog_count > 1:
            raise ValueError("At most one todo item can be 'in_progress' at any time.")
        with self._lock:
            self._sync()
            self._items = [
                Todo(id=str(it.get("id", i)), content=it.get("content", ""), status=it.get("status", "pending"),
                     active_form=str(it.get("activeForm") or it.get("active_form") or ""))
                for i, it in enumerate(items)
            ]
            if self.bound:
                self._loaded_sid = self._sid()
                self._save()

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
    steer: bool = False   # typed mid-turn: delivered inside the running turn between tool rounds

def _journal_queue(action: str, **data: Any) -> None:
    """Every queue change goes to the activity journal (nothing typed is ever silently lost)."""
    try:
        from axon.session import activity
        activity.record("queue", {"action": action, **data})
    except Exception:
        pass


class MessageQueue:
    """Manages sequential user prompt queue for batching and autonomous turns."""
    items: list[QueuedMessage]

    def __init__(self) -> None:
        self.items = []
        self._counter = 0
        import threading
        self._lock = threading.RLock()

    def push(self, text: str, steer: bool = False) -> QueuedMessage:
        with self._lock:
            self._counter += 1
            item = QueuedMessage(id=self._counter, text=text.strip(), steer=bool(steer))
            self.items.append(item)
        _journal_queue("push", id=item.id, text=item.text, steer=item.steer, pending=len(self.items))
        return item

    def drain_steer(self) -> list[QueuedMessage]:
        """Remove and return every steer-flagged item (in order); plain queued prompts stay."""
        with self._lock:
            steer = [it for it in self.items if getattr(it, "steer", False)]
            if steer:
                self.items = [it for it in self.items if not getattr(it, "steer", False)]
            return steer

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
                _journal_queue("remove", id=it.id, text=it.text)
                return True
        return False

    def remove_by_index(self, idx: int) -> QueuedMessage | None:
        """Remove a queued message by its 1-based positional index. Returns the removed item or None."""
        if 1 <= idx <= len(self.items):
            it = self.items.pop(idx - 1)
            _journal_queue("remove", id=it.id, text=it.text)
            return it
        return None

    def clear(self) -> None:
        if self.items:
            _journal_queue("clear", texts=[it.text for it in self.items], count=len(self.items))
        self.items.clear()

    def __len__(self) -> int:
        return len(self.items)

    def list_summary(self) -> str:
        """Return a compact one-line summary suitable for a status bar badge."""
        if not self.items:
            return ""
        n = len(self.items)
        nxt = self.items[0].text
        if len(nxt) > 40:
            nxt = nxt[:37] + "..."
        if n == 1:
            return f"📥 Queue: 1 pending · Next: {nxt}"
        return f"📥 Queue: {n} pending · Next: {nxt}"

    def render(self) -> str:
        if not self.items:
            return "No messages in queue."
        lines = [f"Message Queue ({len(self.items)} pending):"]
        for idx, it in enumerate(self.items, 1):
            tag = " [Next]" if idx == 1 else ""
            lines.append(f"  #{it.id}{tag}: {it.text}")
        return "\n".join(lines)

