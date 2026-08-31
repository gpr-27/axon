"""
Real-time Token Status Bar and Live Metrics Visualizer for Axon.
Renders visual token gauges, sparklines, latency metrics, and budget trackers.
"""
from __future__ import annotations
import math
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, DIM, GOLD, GRAY_BG, GREEN, ITALIC, LBLUE, MINT,
    PURPLE, RED, ROSE, RST, SLATE, TEAL, WHITE, strip_ansi, term_width,
)

if TYPE_CHECKING:
    from axon.agent.loop import Agent
    from axon.providers.base import Usage

_SPARKLINE_CHARS = (" ", "▂", "▃", "▄", "▅", "▆", "▇", "█")

def generate_sparkline(history: list[int], max_val: int | None = None) -> str:
    """Generate a Unicode sparkline string for an array of integer values."""
    if not history:
        return ""
    m = max_val or max(history) or 1
    out = []
    for val in history:
        ratio = min(1.0, max(0.0, val / m))
        idx = min(len(_SPARKLINE_CHARS) - 1, int(ratio * (len(_SPARKLINE_CHARS) - 1)))
        out.append(_SPARKLINE_CHARS[idx])
    return "".join(out)

def format_tokens(n: int) -> str:
    """Format token count into readable format (e.g. 4.2k, 1.2M)."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


class StatusBar:
    """Renders persistent or on-demand status bars and token capacity gauges."""
    _enabled: bool = True
    _token_history: list[int] = []

    @classmethod
    def toggle(cls) -> bool:
        cls._enabled = not cls._enabled
        return cls._enabled

    @classmethod
    def record_tokens(cls, tokens: int) -> None:
        cls._token_history.append(tokens)
        if len(cls._token_history) > 16:
            cls._token_history.pop(0)

    @classmethod
    def render_gauge(cls, used: int, total: int, width: int = 14) -> str:
        """Render a horizontal color-coded progress gauge."""
        ratio = min(1.0, max(0.0, used / max(1, total)))
        filled = int(ratio * width)
        empty = width - filled

        if ratio > 0.85:
            color = RED
        elif ratio > 0.65:
            color = GOLD
        else:
            color = MINT

        bar = f"{color}{'█' * filled}{SLATE}{'░' * empty}{RST}"
        pct = int(ratio * 100)
        return f"{bar} {SLATE}({pct}%){RST}"

    @classmethod
    def format_bar(
        cls,
        model: str,
        effort: str,
        mode: str,
        context_tokens: int | None = None,
        context_capacity: int = 1_000_000,
        session_cost: float | None = None,
        tool_count: int = 0,
        subagent_count: int = 0,
        total_tokens: int | None = None,
        cost: float | None = None,
    ) -> str:
        """Construct the full styled Axon status line."""
        actual_tokens = context_tokens if context_tokens is not None else (total_tokens if total_tokens is not None else 0)
        actual_cost = session_cost if session_cost is not None else (cost if cost is not None else 0.0)
        w = max(60, term_width() - 4)

        tok_str = format_tokens(actual_tokens)
        cap_str = format_tokens(context_capacity)
        gauge = cls.render_gauge(actual_tokens, context_capacity, width=10)
        spark = generate_sparkline(cls._token_history) if len(cls._token_history) >= 2 else ""
        spark_str = f" {CYAN}{spark}{RST}" if spark else ""

        cost_str = f"{GOLD}${actual_cost:.4f}{RST}"
        mode_str = f"{PURPLE}{mode}{RST}"
        model_str = f"{TEAL}{model}{RST}"
        effort_str = f"{SLATE}{effort}{RST}"

        sub_part = f" · {LBLUE}🤖 {subagent_count}{RST}" if subagent_count > 0 else ""
        tools_part = f" · {MINT}⏺ {tool_count}{RST}" if tool_count > 0 else ""

        left_side = f"  {DARK_SLATE}│{RST} {model_str} {DARK_SLATE}•{RST} {effort_str} {DARK_SLATE}•{RST} {mode_str}{tools_part}{sub_part}"
        right_side = f"Context: {tok_str}/{cap_str} {gauge}{spark_str} {DARK_SLATE}│{RST} {cost_str} {DARK_SLATE}│{RST}"

        left_len = len(strip_ansi(left_side))
        right_len = len(strip_ansi(right_side))
        spacing = max(2, w - left_len - right_len)

        top_border = f"  {DARK_SLATE}┌{'─' * (w - 2)}┐{RST}"
        mid_line = f"{left_side}{' ' * spacing}{right_side}"
        bot_border = f"  {DARK_SLATE}└{'─' * (w - 2)}┘{RST}"

        return f"\n{top_border}\n{mid_line}\n{bot_border}\n"

    @classmethod
    def print_live_status(cls, agent: Agent) -> None:
        """Print current live status bar to terminal with active context usage."""
        try:
            from axon.agent.prompt import build_system
            sys_blocks = build_system(agent.settings, agent.registry, list(agent.skills.skills.values()))
            sys_chars = sum(len(str(b.get("text", ""))) for b in sys_blocks)
            tool_schemas = agent.registry.schemas(
                provider_style="anthropic" if agent.provider and agent.provider.name == "anthropic" else "openai"
            )
            tool_chars = sum(len(str(s)) for s in tool_schemas)
            active_context_tok = int((sys_chars + tool_chars) / 3.7) + agent.conversation.token_estimate()
        except Exception:
            active_context_tok = agent.conversation.token_estimate() + 7200

        cls.record_tokens(active_context_tok)
        from axon.providers.registry import get_context_window
        cap = get_context_window(agent.settings.model)
        sub_count = len(agent.subagents.all_tasks()) if hasattr(agent, "subagents") and agent.subagents else 0
        bar = cls.format_bar(
            model=agent.settings.model,
            effort=agent.settings.effort,
            mode=agent.settings.mode,
            context_tokens=active_context_tok,
            context_capacity=cap,
            session_cost=float(agent.ledger.total()),
            subagent_count=sub_count,
        )
        print(bar)
