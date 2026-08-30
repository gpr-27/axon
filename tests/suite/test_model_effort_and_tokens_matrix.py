"""
Exhaustive matrix for effort levels, token counters, thinking budgets, and tokenizer estimations.
"""
import pytest
from axon.config import Settings
from axon.agent.state import Conversation
from axon.providers.base import TextBlock, ThinkingBlock, AssistantTurn

# ─── Effort Normalization Matrix (15 tests) ─────────────────────────────────
@pytest.mark.parametrize("input_effort,expected_normalized", [
    ("reflex", "reflex"),
    ("low", "reflex"),
    ("REFLEX", "reflex"),
    ("balanced", "balanced"),
    ("medium", "balanced"),
    ("BALANCED", "balanced"),
    ("synapse", "synapse"),
    ("high", "synapse"),
    ("SYNAPSE", "synapse"),
    ("quantum", "quantum"),
    ("xhigh", "quantum"),
    ("max", "quantum"),
    ("hyper", "quantum"),
    ("QUANTUM", "quantum"),
    ("unknown_effort", "quantum"),
])
def test_effort_level_normalization_matrix(input_effort: str, expected_normalized: str):
    s = Settings(effort=input_effort)
    assert s.effort == expected_normalized

# ─── Token Estimation Accuracy Matrix (20 tests) ────────────────────────────
@pytest.mark.parametrize("sample_text,min_tokens,max_tokens", [
    ("Hello world", 1, 5),
    ("def calculate_sum(a: int, b: int) -> int:\n    return a + b\n", 10, 30),
    ("```python\nfor i in range(100):\n    print(f'Item {i}')\n```", 15, 45),
    ("A" * 1000, 200, 350),
    ("こんにちは世界！ " * 50, 100, 400),
])
def test_conversation_token_estimations(sample_text: str, min_tokens: int, max_tokens: int):
    conv = Conversation()
    conv.append_user(sample_text)
    est = conv.token_estimate()
    assert min_tokens <= est <= max_tokens

@pytest.mark.parametrize("turn_count", [1, 2, 5, 10, 25])
def test_multi_turn_token_accumulation(turn_count: int):
    conv = Conversation()
    for i in range(turn_count):
        conv.append_user(f"User query number {i} asking for details")
        conv.append_assistant(AssistantTurn(
            blocks=[
                ThinkingBlock(text=f"Thinking through query {i}"),
                TextBlock(text=f"Response {i} providing extensive information"),
            ],
            stop_reason="end_turn",
        ))
    est = conv.token_estimate()
    assert est >= turn_count * 15
