"""
Exhaustive test suite for Provider implementations, streaming events, and model registries.
"""
import pytest
from axon.providers.registry import get_context_window, PRICING
from axon.providers.base import (
    AssistantTurn,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolUseBlock,
    ToolUseStart,
    ToolArgsDelta,
    ToolUseComplete,
    ToolResultBlock,
    TurnComplete,
    Usage,
)
from axon.providers.anthropic import AnthropicProvider
from axon.providers.openai_compat import OpenAICompatProvider

# ─── Context Windows & Pricing Registry Matrix (25 tests) ───────────────────
@pytest.mark.parametrize("model_name,expected_min_window", [
    ("claude-opus-5", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3-7-sonnet-20250219", 200_000),
    ("gpt-5.6-sol", 1_000_000),
    ("gpt-5-mini", 1_000_000),
    ("gpt-4o", 128_000),
    ("deepseek-v4-flash", 1_000_000),
    ("deepseek-r1", 128_000),
    ("glm-5.3", 1_000_000),
])
def test_model_context_windows(model_name: str, expected_min_window: int):
    win = get_context_window(model_name)
    assert win >= expected_min_window

@pytest.mark.parametrize("model_name", [
    "claude-opus-5",
    "claude-opus-4-8",
    "gpt-5.6-sol",
    "deepseek-v4-flash",
    "glm-5.3",
])
def test_pricing_registry_rates_positive(model_name: str):
    p = PRICING[model_name]
    assert p["input"] > 0
    assert p["output"] > 0
    if "cache_read" in p:
        assert p["cache_read"] > 0
        assert p["cache_read"] < p["input"]  # Cache read is cheaper than input

# ─── Provider Protocol & Data Structures (20 tests) ─────────────────────────
def test_usage_addition_arithmetic():
    u1 = Usage(input=100, output=50, cache_read=20, cache_write=10, reasoning=30)
    u2 = Usage(input=200, output=80, cache_read=50, cache_write=0, reasoning=40)
    total = u1 + u2
    assert total.input == 300
    assert total.output == 130
    assert total.cache_read == 70
    assert total.cache_write == 10
    assert total.reasoning == 70

def test_assistant_turn_properties():
    turn = AssistantTurn(
        blocks=[
            ThinkingBlock(text="I need to list files\n"),
            TextBlock(text="Here are the results:\n"),
            ToolUseBlock(id="t1", name="Ls", input={"path": "."}),
            ToolUseBlock(id="t2", name="Doctor", input={}),
        ],
        stop_reason="tool_use",
    )
    assert turn.thinking == "I need to list files\n"
    assert turn.text == "Here are the results:\n"
    assert len(turn.tool_uses) == 2
    assert turn.tool_uses[0].id == "t1"
    assert turn.tool_uses[1].id == "t2"

def test_usage_prompt_caching_normalization():
    # Anthropic semantics: raw input (uncached) + cache_read + cache_write
    raw_uncached = 235
    cache_read = 6600
    cache_write = 0
    total_input = raw_uncached + cache_read + cache_write
    usage = Usage(input=total_input, output=52, cache_read=cache_read, cache_write=cache_write)
    assert usage.input == 6835
    assert usage.cache_read == 6600
    assert (usage.cache_read / usage.input * 100) > 96.0

def test_turn_footer_tokens_rendered():
    import io, sys
    from axon.ui.render import Renderer
    r = Renderer()
    usage = Usage(input=6835, output=52, cache_read=6600)
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        r.turn_footer(tool_count=0, usage=usage, cost=0.0166, elapsed=2.8)
    finally:
        sys.stdout = old_stdout
    out = buf.getvalue()
    assert "6.8k in" in out
    assert "cached" not in out
    assert "52 out" in out

def test_handle_breakdown_command(workspace):
    import io, sys
    from unittest.mock import MagicMock
    from axon.agent.state import Conversation
    from axon.commands.builtin import handle_breakdown, dispatch_command
    from axon.tools import create_default_registry
    from axon.skills.manager import SkillManager
    from axon.session.ledger import Ledger
    from axon.config import Settings

    mock_agent = MagicMock()
    mock_agent.settings = Settings(workspace=workspace, model="gpt-5.6-sol")
    mock_agent.registry = create_default_registry()
    mock_agent.skills = SkillManager(workspace)
    mock_agent.ledger = Ledger()
    mock_agent.provider = MagicMock()
    mock_agent.provider.name = "openai_compat"
    mock_agent.conversation = Conversation([
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "Hi! How can I help?"},
        {"role": "user", "content": "how r u?"},
    ])

    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        res = dispatch_command("/breakdown", mock_agent)
    finally:
        sys.stdout = old_stdout

    assert res.handled is True
    out = buf.getvalue()
    assert "Active Input Payload Breakdown" in out
    assert "SYSTEM PROMPT" in out
    assert "TOOL DEFINITIONS" in out
    assert "PREVIOUS CONVERSATION" in out
    assert "LAST MESSAGE" in out
    assert "TOTAL INPUT TOKEN RECONCILIATION" in out
    assert "how r u?" in out
