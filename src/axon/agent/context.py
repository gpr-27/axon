"""
Context management, token projection, and compaction ladder.
"""
from __future__ import annotations
from typing import Any
from axon.agent.state import Conversation
from axon.config import Settings
from axon.providers.registry import get_context_window

class ContextManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_effective_budget(self, model: str | None = None) -> int:
        """Calculate effective token capacity using active model context window."""
        active_model = model or self.settings.model
        model_window = get_context_window(active_model, default=1_000_000)
        return min(self.settings.turn_token_budget, model_window)

    def prepare(self, conv: Conversation, system: list[dict[str, Any]], tools: list[dict[str, Any]], model: str | None = None) -> None:
        """Apply sliding context window and compaction rungs if approaching token budget."""
        # Sliding context window enforcement (Align on clean user turn boundaries)
        if self.settings.max_history_turns > 0:
            max_msgs = self.settings.max_history_turns * 2
            if len(conv.messages) > max_msgs:
                start_idx = len(conv.messages) - max_msgs
                while start_idx < len(conv.messages) and conv.messages[start_idx].get("role") != "user":
                    start_idx += 1
                if start_idx < len(conv.messages):
                    conv.messages = conv.messages[start_idx:]

        est = conv.token_estimate()
        budget = self.get_effective_budget(model)
        threshold = int(budget * self.settings.compact_at)

        if est > threshold:
            # Rung 1: Trim oversized tool outputs
            self._trim_large_results(conv)
            est = conv.token_estimate()

        if est > threshold:
            # Rung 2: Evict old tool results older than 5 turns
            self._evict_stale_results(conv)
            est = conv.token_estimate()

        if est > threshold and len(conv.messages) >= 12:
            # Rung 3: Summarize older conversation turns for very long sessions
            self._summarize_older_turns(conv)
            est = conv.token_estimate()

    def _trim_large_results(self, conv: Conversation) -> None:
        for m in conv.messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        txt = str(block.get("content", ""))
                        if len(txt) > 8000:
                            half = 3000
                            block["content"] = (
                                txt[:half]
                                + "\n\n[... Truncated by Axon Context Compaction to reclaim space ...]\n\n"
                                + txt[-half:]
                            )

    def _evict_stale_results(self, conv: Conversation) -> None:
        """Clear results older than the last 4 messages, preserving tool_use pairs."""
        cutoff = max(0, len(conv.messages) - 4)
        for idx in range(cutoff):
            m = conv.messages[idx]
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        block["content"] = "[Result cleared to reclaim context. Re-run tool if needed.]"

    def _summarize_older_turns(self, conv: Conversation) -> None:
        """Compress older turns into a single structured summary block, keeping last 4 turns."""
        if len(conv.messages) <= 6:
            return

        cutoff = len(conv.messages) - 4
        old_msgs = conv.messages[:cutoff]
        recent_msgs = conv.messages[cutoff:]

        summary_points = []
        for m in old_msgs:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                first_line = content.strip().splitlines()[0][:100]
                summary_points.append(f"- {role.title()}: {first_line}")
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text" and b.get("text"):
                            summary_points.append(f"- {role.title()}: {b['text'][:100]}")
                        elif b.get("type") == "tool_use":
                            summary_points.append(f"- Tool Use: {b.get('name')}(...)")

        if summary_points:
            summary_msg = {
                "role": "user",
                "content": f"[Prior Conversation Summary ({len(old_msgs)} messages compacted)]:\n" + "\n".join(summary_points[:12]),
            }
            conv.messages = [summary_msg] + recent_msgs
