"""
Context management: token projection, prompt-cache breakpoints and the compaction ladder.

Rungs (cheapest first):
  1. trim oversized tool outputs
  2. clear stale tool outputs (older than the last few messages)
  3. summarize older turns (via the provider when available, heuristic fallback)

All cuts happen at genuine user-turn boundaries so a tool_result is never orphaned
from its tool_use.
"""
from __future__ import annotations
import json
from typing import Any
from axon.agent.state import Conversation, estimate_content_tokens, is_turn_start
from axon.config import Settings
from axon.providers.registry import get_context_window

DEFAULT_CONTEXT_WINDOW = 128_000
TRIM_THRESHOLD_CHARS = 8_000
STALE_PLACEHOLDER = "[Result cleared to reclaim context. Re-run the tool if needed.]"

SUMMARY_SYSTEM = (
    "You are compacting a coding-assistant conversation so work can continue in a fresh context. "
    "Write a dense, factual summary. Preserve: the user's goals and explicit instructions; key decisions; "
    "files read or modified (exact paths) and what changed; commands run and their outcomes; errors and how "
    "they were resolved; open questions; and the precise next steps. Do not invent details. Use markdown headings."
)


def _strip_cache_control(obj: Any) -> None:
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            if isinstance(v, (list, dict)):
                _strip_cache_control(v)
    elif isinstance(obj, list):
        for v in obj:
            _strip_cache_control(v)


def _render_for_summary(messages: list[dict[str, Any]], limit_chars: int = 400_000) -> str:
    lines: list[str] = []
    for m in messages:
        role = str(m.get("role", "?"))
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    lines.append(f"[{role}] {b.get('text', '')}")
                elif t == "tool_use":
                    lines.append(f"[{role} tool_use {b.get('name')}] {json.dumps(b.get('input', {}), default=str)[:2000]}")
                elif t == "tool_result":
                    lines.append(f"[tool_result] {str(b.get('content', ''))[:3000]}")
                elif t == "image":
                    lines.append(f"[{role}] (image)")
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function", {}) or {}
                lines.append(f"[{role} tool_call {fn.get('name')}] {str(fn.get('arguments', ''))[:2000]}")
    text = "\n".join(lines)
    if len(text) > limit_chars:
        half = limit_chars // 2
        text = text[:half] + "\n[... middle of transcript omitted ...]\n" + text[-half:]
    return text


class ContextManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── budgeting ───────────────────────────────────────────────────
    def context_window(self, model: str | None = None) -> int:
        return get_context_window(model or self.settings.model, default=DEFAULT_CONTEXT_WINDOW)

    def get_effective_budget(self, model: str | None = None) -> int:
        """Usable prompt capacity: the model's context window (less room for the reply)."""
        window = self.context_window(model)
        reserve = min(int(self.settings.max_tokens), window // 4)
        return max(window - reserve, window // 2)

    @staticmethod
    def overhead_tokens(system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        sys_text = "".join(str(b.get("text", "")) for b in system)
        return max(0, int(len(sys_text) / 3.7)) + max(0, int(len(json.dumps(tools, separators=(",", ":"), default=str)) / 4.0))

    def projected_tokens(self, conv: Conversation, system: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        return conv.token_estimate() + self.overhead_tokens(system, tools)

    def threshold(self, model: str | None = None) -> int:
        return int(self.get_effective_budget(model) * float(self.settings.compact_at))

    def needs_compaction(self, conv: Conversation, system: list[dict[str, Any]], tools: list[dict[str, Any]], model: str | None = None) -> bool:
        return self.projected_tokens(conv, system, tools) > self.threshold(model)

    # ── main entry ──────────────────────────────────────────────────
    def prepare(self, conv: Conversation, system: list[dict[str, Any]], tools: list[dict[str, Any]], model: str | None = None) -> None:
        """Apply sliding window, cheap compaction rungs and prompt-cache breakpoints."""
        self._apply_sliding_window(conv)

        if self.needs_compaction(conv, system, tools, model):
            self._trim_large_results(conv)
        if self.needs_compaction(conv, system, tools, model):
            self._evict_stale_results(conv)
        if self.needs_compaction(conv, system, tools, model) and len(conv.messages) >= 12:
            self._summarize_older_turns(conv)

        self._apply_cache_control(conv, system, tools, model=model)

    def _apply_sliding_window(self, conv: Conversation) -> None:
        if self.settings.max_history_turns <= 0:
            return
        max_msgs = self.settings.max_history_turns * 2
        if len(conv.messages) <= max_msgs:
            return
        start_idx = len(conv.messages) - max_msgs
        while start_idx < len(conv.messages) and not is_turn_start(conv.messages[start_idx]):
            start_idx += 1
        if start_idx >= len(conv.messages):
            # No clean boundary inside the window: fall back to the latest genuine user turn.
            starts = [i for i, m in enumerate(conv.messages) if is_turn_start(m)]
            if not starts:
                return
            start_idx = starts[-1]
        conv.messages = conv.messages[start_idx:]

    # ── rung 0: cache breakpoints ───────────────────────────────────
    def _apply_cache_control(
        self,
        conv: Conversation,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> None:
        """Place at most 3 Anthropic cache breakpoints (tools, system, conversation prefix).

        Old markers on messages are always removed first so they never accumulate past
        the API limit of 4 breakpoints."""
        for m in conv.messages:
            _strip_cache_control(m.get("content"))
        active_model = model or self.settings.model
        try:
            from axon.providers.registry import ANTHROPIC_MODELS
        except ImportError:
            ANTHROPIC_MODELS = set()  # type: ignore[assignment]
        if not (active_model in ANTHROPIC_MODELS or "claude" in str(active_model).lower()):
            return
        if tools and isinstance(tools[-1], dict):
            tools[-1]["cache_control"] = {"type": "ephemeral"}
        if system and isinstance(system[-1], dict):
            for b in system[:-1]:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
            system[-1]["cache_control"] = {"type": "ephemeral"}
        # Breakpoint on the newest structured message so the prefix is cached for the next call.
        for m in reversed(conv.messages[-4:]):
            content = m.get("content")
            if isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1]["cache_control"] = {"type": "ephemeral"}
                break

    # ── rung 1 / 2 ──────────────────────────────────────────────────
    def _trim_large_results(self, conv: Conversation) -> None:
        def trim(txt: str) -> str:
            half = 3000
            return txt[:half] + "\n\n[... truncated by Axon context compaction ...]\n\n" + txt[-half:]

        for m in conv.messages:
            content = m.get("content")
            if m.get("role") == "tool" and isinstance(content, str) and len(content) > TRIM_THRESHOLD_CHARS:
                m["content"] = trim(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        inner = block.get("content", "")
                        if isinstance(inner, str) and len(inner) > TRIM_THRESHOLD_CHARS:
                            block["content"] = trim(inner)

    def _evict_stale_results(self, conv: Conversation) -> None:
        """Clear tool outputs older than the last 4 messages (pairing is preserved)."""
        cutoff = max(0, len(conv.messages) - 4)
        for idx in range(cutoff):
            m = conv.messages[idx]
            content = m.get("content")
            if m.get("role") == "tool":
                m["content"] = STALE_PLACEHOLDER
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        block["content"] = STALE_PLACEHOLDER

    # ── rung 3 ──────────────────────────────────────────────────────
    @staticmethod
    def split_point(conv: Conversation, keep_turns: int = 2) -> int:
        """Index of the first message to keep: the start of the ``keep_turns``-th last user turn."""
        starts = [i for i, m in enumerate(conv.messages) if is_turn_start(m)]
        if len(starts) <= keep_turns:
            # Few (long agentic) turns: cut before an assistant message, keeping the last 4 messages.
            n = len(conv.messages)
            for i in range(n - 4, 1, -1):
                if conv.messages[i].get("role") == "assistant":
                    return i
            return 0
        return starts[-max(1, keep_turns)]

    def _summarize_older_turns(self, conv: Conversation, summary_text: str | None = None, keep_turns: int = 2) -> bool:
        """Replace older turns with a summary message, cutting at a clean boundary."""
        cut = self.split_point(conv, keep_turns)
        if cut <= 0:
            return False
        old_msgs = conv.messages[:cut]
        recent = conv.messages[cut:]
        if summary_text is None:
            points: list[str] = []
            for m in old_msgs:
                role = str(m.get("role", "unknown"))
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    points.append(f"- {role.title()}: {content.strip().splitlines()[0][:160]}")
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                            points.append(f"- {role.title()}: {str(b['text']).strip()[:160]}")
                        elif isinstance(b, dict) and b.get("type") == "tool_use":
                            points.append(f"- Tool: {b.get('name')}")
            summary_text = "\n".join(points[-40:]) or "(no textual content)"
        summary_msg = {
            "role": "user",
            "content": (
                f"[Summary of the earlier conversation ({len(old_msgs)} messages compacted)]\n\n{summary_text.strip()}"
            ),
        }
        if recent and recent[0].get("role") == "user":
            # Keep strict user/assistant alternation: an assistant ack between two user messages.
            conv.messages = [summary_msg, {"role": "assistant", "content": [{"type": "text", "text": "Understood. Continuing from the summary."}]}] + recent
        else:
            conv.messages = [summary_msg] + recent
        return True

    def generate_summary(
        self,
        conv: Conversation,
        provider: Any,
        model: str,
        instructions: str = "",
        keep_turns: int = 2,
        thinking: bool = False,
    ) -> tuple[str | None, Any]:
        """Ask the provider for a summary of the turns before the split point.
        Returns (summary or None, usage or None). Does not modify the conversation."""
        cut = self.split_point(conv, keep_turns)
        if cut <= 0:
            return None, None
        transcript = _render_for_summary(conv.messages[:cut])
        ask = "Summarize this conversation transcript for continuation."
        if instructions:
            ask += f"\nAdditional focus: {instructions}"
        try:
            stream = provider.stream(
                model=model,
                system=[{"type": "text", "text": SUMMARY_SYSTEM}],
                messages=[{"role": "user", "content": f"{ask}\n\n<transcript>\n{transcript}\n</transcript>"}],
                tools=[],
                max_tokens=4096,
                effort="low",
                thinking=thinking,
            )
            for _ in stream:
                pass
            turn = provider.finalize()
        except Exception:
            return None, None
        return ((turn.text or "").strip() or None), getattr(turn, "usage", None)

    def summarize_with_provider(
        self,
        conv: Conversation,
        provider: Any,
        model: str,
        instructions: str = "",
        keep_turns: int = 2,
    ) -> Any:
        """Summarize older turns with an LLM call and apply it. Returns the call's Usage (or None).
        Falls back to the heuristic summary when the provider fails."""
        if self.split_point(conv, keep_turns) <= 0:
            return None
        summary, usage = self.generate_summary(conv, provider, model, instructions, keep_turns)
        self._summarize_older_turns(conv, summary_text=summary, keep_turns=keep_turns)
        return usage


__all__ = ["ContextManager", "estimate_content_tokens", "DEFAULT_CONTEXT_WINDOW"]
