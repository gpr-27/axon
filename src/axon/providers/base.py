"""
Internal normalized data model and Provider Protocol.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Protocol

# ─── Normalized Block Types ────────────────────────────────────────────────
@dataclass(frozen=True)
class TextBlock:
    text: str

@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None

@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]  # ALWAYS a parsed dict
    # Set when the model emitted arguments that are not a JSON object; `input` then holds {"raw": ...}.
    # The agent loop should answer such a call with an error tool_result instead of running it.
    parse_error: str | None = field(default=None, compare=False)

@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False

Block = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock

StopReason = Literal["end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"]

@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    # Portion of cache_write written with the 1-hour TTL (billed higher than 5-minute writes).
    cache_write_1h: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=self.reasoning + other.reasoning,
            cache_write_1h=self.cache_write_1h + getattr(other, "cache_write_1h", 0),
        )

@dataclass
class AssistantTurn:
    blocks: list[Block] = field(default_factory=list)
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    native: Any = None  # Provider-native payload replayed verbatim (ADR-003)
    model: str | None = None  # model that actually produced the turn (differs from settings.model after failover)

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.blocks if isinstance(b, ToolUseBlock)]

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def thinking(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, ThinkingBlock))

# ─── Stream Events for Renderer ───────────────────────────────────────────
@dataclass(frozen=True)
class TextDelta:
    text: str

@dataclass(frozen=True)
class ThinkingDelta:
    text: str

@dataclass(frozen=True)
class ToolUseStart:
    id: str
    name: str

@dataclass(frozen=True)
class ToolArgsDelta:
    id: str
    fragment: str

@dataclass(frozen=True)
class ToolUseComplete:
    id: str

@dataclass(frozen=True)
class ToolBatchStart:
    total_count: int
    tools: list[ToolUseBlock]
    user_prompt: str = ""
    mode: str = ""

@dataclass(frozen=True)
class ToolExecutionStart:
    id: str
    name: str
    input: dict[str, Any]
    mode: str = ""
    user_prompt: str = ""
    rationale: str = ""          # the model's own words just before this call (never invented)
    rationale_source: str = ""   # where the rationale came from: "text" | "thinking" | ""

@dataclass(frozen=True)
class ToolExecutionResult:
    id: str
    name: str
    input: dict[str, Any]
    content: str
    is_error: bool = False
    changes: tuple[Any, ...] = ()   # axon.agent.filechanges.FileChange: files a shell command changed

@dataclass(frozen=True)
class TurnComplete:
    stop_reason: StopReason
    usage: Usage

@dataclass(frozen=True)
class LLMCallStart:
    iteration: int
    max_iterations: int
    model: str
    message_count: int
    tokens_in: int = 0
    tools_ingested: list[dict[str, Any]] = field(default_factory=list)
    user_prompt: str = ""
    mode: str = ""

StreamEvent = (
    TextDelta
    | ThinkingDelta
    | LLMCallStart
    | ToolUseStart
    | ToolArgsDelta
    | ToolUseComplete
    | ToolBatchStart
    | ToolExecutionStart
    | ToolExecutionResult
    | TurnComplete
)

@dataclass(frozen=True)
class ProviderRetry:
    """Fallback retry/failover notice, used when axon.agent.trace.RetryEvent is unavailable.
    Same fields as RetryEvent so trace consumers can treat both alike."""
    attempt: int
    max_attempts: int
    delay_s: float
    error: str
    iteration: int = 0


def retry_event(attempt: int, max_attempts: int, delay_s: float, error: str) -> Any:
    """Build the trace's RetryEvent (preferred) or the local ProviderRetry equivalent."""
    try:
        from axon.agent.trace import RetryEvent  # CORE-owned event class
        return RetryEvent(attempt=attempt, max_attempts=max_attempts, delay_s=delay_s, error=error)
    except Exception:
        return ProviderRetry(attempt=attempt, max_attempts=max_attempts, delay_s=delay_s, error=error)


# ─── Shared helpers ───────────────────────────────────────────────────────
def parse_tool_arguments(raw: str | dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """Parse streamed tool-call arguments. Returns (input_dict, parse_error)."""
    import json

    if isinstance(raw, dict):
        return raw, None
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        val = json.loads(text)
    except (ValueError, TypeError) as e:
        return {"raw": text}, f"Tool arguments are not valid JSON ({e})"
    if not isinstance(val, dict):
        return {"raw": text}, f"Tool arguments must be a JSON object, got {type(val).__name__}"
    return val, None


class StreamState:
    """Per-stream accumulator. finalize() uses it to return a consistent turn even when the stream
    was interrupted (KeyboardInterrupt / GeneratorExit / error) before completing."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.usage: Usage = Usage()
        self.turn: AssistantTurn | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def thinking(self) -> str:
        return "".join(self.thinking_parts)

    def partial_turn(self, native_style: str) -> AssistantTurn:
        """Turn built from what streamed so far. Never contains tool_use blocks (they would dangle)."""
        text = self.text
        blocks: list[Block] = []
        if self.thinking:
            blocks.append(ThinkingBlock(text=self.thinking))
        if text:
            blocks.append(TextBlock(text=text))
        native: Any
        if native_style == "anthropic":
            native = [{"type": "text", "text": text}] if text else [{"type": "text", "text": "[interrupted]"}]
        else:
            native = {"role": "assistant", "content": text or "[interrupted]"}
        return AssistantTurn(blocks=blocks, stop_reason="end_turn", usage=self.usage, native=native)


# ─── Provider Protocol ────────────────────────────────────────────────────
class Provider(Protocol):
    name: str

    def stream(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str | None,
        thinking: bool,
    ) -> Iterator[StreamEvent]:
        """Stream provider responses and yield normalized stream events."""
        ...

    def finalize(self) -> AssistantTurn:
        """Produce the finalized AssistantTurn containing normalized blocks and native payload."""
        ...

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        """Encode normalized tool results into provider-specific message list."""
        ...

    def supports(self, feature: str) -> bool:
        """Check whether provider/model supports a given feature."""
        ...


# ─── Cancellation of an in-flight HTTP stream ──────────────────────────────

def abort_http_response(resp: Any) -> None:
    """Abort a streaming HTTP response from *another* thread, waking a blocked socket read.

    Closing an httpx response alone does not reliably interrupt a ``recv()`` blocked in a different
    thread, so the underlying socket is shut down (the read then returns immediately).
    Accepts an ``httpx.Response`` or an object exposing one as ``.response`` (SDK stream wrappers).

    When the shutdown succeeds the response is NOT closed here: closing it from this thread sets
    the socket's fd to -1 while the streaming thread may be about to ``poll()`` it again, and a poll
    on fd -1 sleeps for the whole read timeout (seen as Esc leaving the turn hung on "responding").
    The streaming thread closes the response itself when the aborted read unwinds its ``with``.
    """
    import socket as _socket
    import sys as _sys

    target = getattr(resp, "response", None) if not hasattr(resp, "extensions") else resp
    for obj in (target, resp):
        try:
            ns = (getattr(obj, "extensions", None) or {}).get("network_stream")
            sock = ns.get_extra_info("socket") if ns is not None else None
            if sock is not None:
                sock.shutdown(_socket.SHUT_RDWR)
                if _sys.platform == "win32":
                    # Winsock's shutdown() does not wake a blocked (or later) recv(); closing does, and
                    # a read on a closed Windows socket fails at once instead of polling fd -1.
                    sock.close()
                return
        except Exception:
            continue
    for obj in (resp, target):
        close = getattr(obj, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class StreamCanceller:
    """Per-thread registry of active streaming responses so ``cancel()`` can abort them.

    Providers call :meth:`begin` when a stream starts, wrap each HTTP response in :meth:`track`
    and call :meth:`check` after transport errors; :meth:`cancel` (from any thread) aborts the
    blocked read and makes the streaming thread raise ``KeyboardInterrupt``.
    """

    def __init__(self) -> None:
        import threading as _threading
        self._lock = _threading.Lock()
        self._active: dict[int, Any] = {}
        self._cancelled: set[int] = set()

    @staticmethod
    def _me() -> int:
        import threading as _threading
        return _threading.get_ident()

    def begin(self) -> None:
        with self._lock:
            self._cancelled.discard(self._me())

    def check(self) -> None:
        """Raise KeyboardInterrupt in the streaming thread when its stream was cancelled."""
        me = self._me()
        with self._lock:
            hit = me in self._cancelled
            self._cancelled.discard(me)
        if hit:
            raise KeyboardInterrupt

    def track(self, resp: Any) -> "_Tracked":
        return _Tracked(self, resp)

    def cancel(self, thread_ident: int | None = None) -> bool:
        """Abort the stream of *thread_ident* (default: every active stream). True if one was hit."""
        with self._lock:
            idents = [thread_ident] if thread_ident is not None else list(self._active)
            hits = [(i, self._active.get(i)) for i in idents]
            for i in idents:
                if i is not None:
                    self._cancelled.add(i)
        aborted = False
        for _, resp in hits:
            if resp is not None:
                abort_http_response(resp)
                aborted = True
        return aborted


class _Tracked:
    def __init__(self, owner: StreamCanceller, resp: Any) -> None:
        self.owner, self.resp = owner, resp

    def __enter__(self) -> Any:
        me = self.owner._me()
        with self.owner._lock:
            self.owner._active[me] = self.resp
            cancelled = me in self.owner._cancelled
        if cancelled:  # cancelled while connecting
            abort_http_response(self.resp)
            self.owner.check()
        return self.resp

    def __exit__(self, exc_type: Any, *exc: Any) -> None:
        with self.owner._lock:
            self.owner._active.pop(self.owner._me(), None)
        if exc_type is not None and issubclass(exc_type, Exception):
            self.owner.check()  # the aborted read failed (Windows): report it as the cancel it was
