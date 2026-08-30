"""
Anthropic Provider using native SDK with streaming and adaptive thinking.
"""
from __future__ import annotations
import json
from typing import Any, Iterator
from anthropic import Anthropic
from axon.config import Settings
from axon.errors import ProviderError
from axon.providers.base import (
    AssistantTurn,
    Block,
    Provider,
    StopReason,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ThinkingDelta,
    TextDelta,
    ToolArgsDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
    Usage,
)

class AnthropicProvider:
    name: str = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = Anthropic(
            auth_token=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            max_retries=3,
        )
        self._last_turn: AssistantTurn | None = None

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        if thinking:
            kwargs["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
            }
        if effort:
            kwargs["output_config"] = {"effort": effort}

        blocks: list[Block] = []
        tool_buffers: dict[int, dict[str, Any]] = {}
        stop_reason: StopReason = "end_turn"
        usage = Usage()

        try:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if event.type == "content_block_start":
                        cb = event.content_block
                        idx = event.index
                        if cb.type == "thinking":
                            tool_buffers[idx] = {"type": "thinking", "text": getattr(cb, "thinking", "")}
                        elif cb.type == "text":
                            tool_buffers[idx] = {"type": "text", "text": getattr(cb, "text", "")}
                        elif cb.type == "tool_use":
                            tool_buffers[idx] = {"type": "tool_use", "id": cb.id, "name": cb.name, "json": ""}
                            yield ToolUseStart(id=cb.id, name=cb.name)

                    elif event.type == "content_block_delta":
                        d = event.delta
                        idx = event.index
                        if d.type == "thinking_delta":
                            th = getattr(d, "thinking", "")
                            if idx in tool_buffers:
                                tool_buffers[idx]["text"] += th
                            yield ThinkingDelta(text=th)
                        elif d.type == "text_delta":
                            txt = getattr(d, "text", "")
                            if idx in tool_buffers:
                                tool_buffers[idx]["text"] += txt
                            yield TextDelta(text=txt)
                        elif d.type == "input_json_delta":
                            frag = getattr(d, "partial_json", "")
                            if idx in tool_buffers:
                                tool_buffers[idx]["json"] += frag
                            tu_id = tool_buffers.get(idx, {}).get("id", "")
                            yield ToolArgsDelta(id=tu_id, fragment=frag)

                    elif event.type == "content_block_stop":
                        idx = event.index
                        buf = tool_buffers.get(idx)
                        if buf:
                            if buf["type"] == "thinking":
                                blocks.append(ThinkingBlock(text=buf["text"]))
                            elif buf["type"] == "text":
                                blocks.append(TextBlock(text=buf["text"]))
                            elif buf["type"] == "tool_use":
                                parsed_input = {}
                                if buf["json"].strip():
                                    try:
                                        parsed_input = json.loads(buf["json"])
                                    except Exception:
                                        parsed_input = {"raw": buf["json"]}
                                blocks.append(ToolUseBlock(id=buf["id"], name=buf["name"], input=parsed_input))
                                yield ToolUseComplete(id=buf["id"])

                final_msg = stream.get_final_message()
                stop_reason = getattr(final_msg, "stop_reason", "end_turn") or "end_turn"
                raw_usage = getattr(final_msg, "usage", None)
                if raw_usage:
                    usage = Usage(
                        input=getattr(raw_usage, "input_tokens", 0) or 0,
                        output=getattr(raw_usage, "output_tokens", 0) or 0,
                        cache_read=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
                        cache_write=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
                    )

                # Store native content blocks for verbatim replay
                native_content = [b.model_dump() if hasattr(b, "model_dump") else b for b in final_msg.content]
                self._last_turn = AssistantTurn(
                    blocks=blocks,
                    stop_reason=stop_reason,  # type: ignore
                    usage=usage,
                    native=native_content,
                )
                yield TurnComplete(stop_reason=stop_reason, usage=usage)  # type: ignore

        except Exception as e:
            raise ProviderError(f"Anthropic streaming failed: {e}") from e

    def finalize(self) -> AssistantTurn:
        if self._last_turn is None:
            return AssistantTurn(blocks=[], stop_reason="end_turn", usage=Usage())
        turn = self._last_turn
        self._last_turn = None
        return turn

    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        # Anthropic requires ALL results in ONE single user message (Law 2)
        content = []
        for r in results:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,
                "content": r.content,
            }
            if r.is_error:
                block["is_error"] = True
            content.append(block)
        return [{"role": "user", "content": content}]

    def supports(self, feature: str) -> bool:
        return feature in ("tools", "thinking", "prompt_caching", "streaming")
