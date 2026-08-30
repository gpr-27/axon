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

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            reasoning=self.reasoning + other.reasoning,
        )

@dataclass
class AssistantTurn:
    blocks: list[Block] = field(default_factory=list)
    stop_reason: StopReason = "end_turn"
    usage: Usage = field(default_factory=Usage)
    native: Any = None  # Provider-native payload replayed verbatim (ADR-003)

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

@dataclass(frozen=True)
class ToolExecutionStart:
    id: str
    name: str
    input: dict[str, Any]

@dataclass(frozen=True)
class ToolExecutionResult:
    id: str
    name: str
    input: dict[str, Any]
    content: str
    is_error: bool = False

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
