"""Gateways that stream no usage must still give the footer token counts (estimated)."""
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage
from axon.providers.openai_compat import _estimate_usage, _usage_is_empty


def _turn(text: str, usage: Usage = Usage()) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], usage=usage)


def test_empty_usage_is_detected():
    assert _usage_is_empty(Usage())
    assert not _usage_is_empty(Usage(input=5))
    assert not _usage_is_empty(Usage(cache_read=3))


def test_estimate_counts_sent_and_received_text():
    sent = [{"role": "user", "content": "x" * 370}]
    usage = _estimate_usage(sent, _turn("y" * 370))
    assert usage.input >= 100
    assert 95 <= usage.output <= 105


def test_estimate_includes_tool_call_arguments():
    turn = AssistantTurn(blocks=[ToolUseBlock(id="1", name="Bash", input={"command": "ls -la " * 20})])
    assert _estimate_usage([{"role": "user", "content": "hi"}], turn).output > 20


def test_nothing_received_stays_empty():
    usage = _estimate_usage([{"role": "user", "content": "hi"}], _turn("   "))
    assert _usage_is_empty(usage)
