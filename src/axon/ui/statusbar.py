"""
Real-time Token Status Bar and Live Metrics Visualizer for Axon.
Renders visual token gauges, sparklines, latency metrics, and budget trackers.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from axon.ui.theme import (
    CYAN, DARK_SLATE, GOLD, LBLUE, MINT,
    PURPLE, RED, RST, SLATE, TEAL, term_width,
)
from axon.platform import str_width

if TYPE_CHECKING:
    from axon.agent.loop import Agent

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
        trace_level: str = "",
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
        pct = 100.0 * actual_tokens / max(1, context_capacity)
        extras = []
        if tool_count > 0:
            extras.append(f"{MINT}⏺ {tool_count} tools{RST}")
        if subagent_count > 0:
            extras.append(f"{LBLUE}{subagent_count} subagents{RST}")
        extra_str = (f" {DARK_SLATE}·{RST} " + f" {DARK_SLATE}·{RST} ".join(extras)) if extras else ""
        try:
            from axon.ui.render import render_status_line  # shared status-line format
            core = render_status_line(model, mode=mode, context_pct=pct, session_cost=actual_cost,
                                      trace_level=trace_level, width=w)
        except (ImportError, AttributeError, TypeError):
            core = (f"{TEAL}{model}{RST} {DARK_SLATE}·{RST} {PURPLE}{mode}{RST} {DARK_SLATE}·{RST} "
                    f"ctx {pct:.0f}% {DARK_SLATE}·{RST} {GOLD}${actual_cost:.4f}{RST}")
        left_side = f"  {DARK_SLATE}│{RST} {core} {DARK_SLATE}·{RST} {SLATE}{effort}{RST}{extra_str}"
        right_side = f"{SLATE}{tok_str}/{cap_str}{RST} {gauge}{spark_str} {DARK_SLATE}│{RST}"

        left_len = str_width(left_side)
        right_len = str_width(right_side)
        spacing = max(2, w - left_len - right_len)
        inner = max(2, left_len + spacing + right_len - 4)
        top_border = f"  {DARK_SLATE}┌{'─' * inner}┐{RST}"
        mid_line = f"{left_side}{' ' * spacing}{right_side}"
        bot_border = f"  {DARK_SLATE}└{'─' * inner}┘{RST}"
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
        try:
            from axon.providers.registry import get_context_window
            cap = get_context_window(agent.settings.model)
        except (ImportError, AttributeError):
            cap = 200_000
        sub_count = len(agent.subagents.all_tasks()) if hasattr(agent, "subagents") and agent.subagents else 0
        bar = cls.format_bar(
            model=agent.settings.model,
            effort=agent.settings.effort,
            mode=agent.settings.mode,
            context_tokens=active_context_tok,
            context_capacity=cap,
            session_cost=float(agent.ledger.total()),
            subagent_count=sub_count,
            trace_level=str(getattr(getattr(agent, "renderer", None), "trace_level", "") or ""),
        )
        print(bar)
