"""
FakeProvider for deterministic, zero-network, zero-cost harness testing.
"""
from __future__ import annotations
import json
from typing import Any, Iterator
from axon.providers.base import (
    AssistantTurn,
    Block,
    Provider,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolArgsDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
    Usage,
)

class FakeProvider:
    name: str = "fake"

    def __init__(self, turns: list[AssistantTurn] | None = None) -> None:
        self._turns = list(turns) if turns is not None else []
        self.requests: list[dict[str, Any]] = []
        self._last: AssistantTurn | None = None

    def stream(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str | None = None,
        thinking: bool = True,
    ) -> Iterator[StreamEvent]:
        self.requests.append({
            "model": model,
            "system": system,
            "messages": list(messages),
            "tools": tools,
        })
        if not self._turns:
            turn = AssistantTurn(blocks=[TextBlock(text="End of fake turns")], stop_reason="end_turn")
        else:
            turn = self._turns.pop(0)

        for b in turn.blocks:
            if isinstance(b, TextBlock):
                yield TextDelta(b.text)
            elif isinstance(b, ThinkingBlock):
                yield ThinkingDelta(b.text)
            elif isinstance(b, ToolUseBlock):
                yield ToolUseStart(b.id, b.name)
                yield ToolArgsDelta(b.id, json.dumps(b.input))
                yield ToolUseComplete(b.id)

        yield TurnComplete(turn.stop_reason, turn.usage)
        self._last = turn

    def finalize(self) -> AssistantTurn:
        assert self._last is not None
        return self._last

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.tool_use_id,
                        "content": r.content,
                        **({"is_error": True} if r.is_error else {})
                    }
                    for r in results
                ]
            }
        ]

    def supports(self, feature: str) -> bool:
        return True

def scripted(*specs: Any) -> list[AssistantTurn]:
    """Helper to convert specs like ("Read", {"path": "a.py"}), "Final answer" into AssistantTurns."""
    turns: list[AssistantTurn] = []
    tool_counter = 0

    for item in specs:
        if isinstance(item, str):
            turns.append(AssistantTurn(
                blocks=[TextBlock(text=item)],
                stop_reason="end_turn",
                usage=Usage(input=100, output=50),
            ))
        elif isinstance(item, tuple):
            # Single tool use
            name, args = item
            tu_id = f"t{tool_counter}"
            tool_counter += 1
            turns.append(AssistantTurn(
                blocks=[ToolUseBlock(id=tu_id, name=name, input=args)],
                stop_reason="tool_use",
                usage=Usage(input=100, output=50),
            ))
        elif isinstance(item, list):
            # Batch of tool uses
            blocks: list[Block] = []
            for sub in item:
                if isinstance(sub, tuple):
                    name, args = sub
                    tu_id = f"t{tool_counter}"
                    tool_counter += 1
                    blocks.append(ToolUseBlock(id=tu_id, name=name, input=args))
            turns.append(AssistantTurn(
                blocks=blocks,
                stop_reason="tool_use",
                usage=Usage(input=150, output=80),
            ))
    return turns
