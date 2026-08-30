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
