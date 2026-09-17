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
    ("gpt-6-astra", 1_000_000),
])
def test_model_context_windows(model_name: str, expected_min_window: int):
    win = get_context_window(model_name)
    assert win >= expected_min_window

@pytest.mark.parametrize("model_name", [
    "claude-opus-5",
    "claude-opus-4-8",
    "gpt-5.6-sol",
    "gpt-6-astra",
    "deepseek-v4-flash",
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

# ─── GPT-6 Astra & Response Protocol Verification ─────────────────────────────
def test_gpt6_astra_routes_to_responses_protocol(workspace):
    from unittest.mock import patch, MagicMock
    from axon.config import Settings
    from axon.providers.openai_compat import OpenAICompatProvider

    settings = Settings(workspace=workspace, base_url="https://agentrouter.org/v1")
    provider = OpenAICompatProvider(settings)
    assert provider._responses_url == "https://agentrouter.org/v1/responses"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_lines.return_value = [
        'event: response.text.delta',
        'data: {"delta": "Hello from GPT-6"}',
        'event: response.done',
        'data: {"response": {"usage": {"input_tokens": 120, "output_tokens": 15, "input_token_details": {"cached_tokens": 40}}}}',
        'data: [DONE]',
    ]

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_resp

    with patch("httpx.stream", return_value=mock_stream_ctx) as mock_stream:
        events = list(provider.stream(
            model="gpt-6-astra",
            system=[{"type": "text", "text": "You are a helpful assistant"}],
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            max_tokens=4096,
            effort="high",
        ))

        # Check call arguments
        mock_stream.assert_called_once()
        called_args, called_kwargs = mock_stream.call_args
        method, url = called_args[0], called_args[1]
        assert method == "POST"
        assert url == "https://agentrouter.org/v1/responses"

        sent_body = called_kwargs["json"]
        assert sent_body["model"] == "gpt-6-astra"
        assert "input" in sent_body
        assert sent_body["reasoning"] == {"effort": "high"}
        # Top-level reasoning_effort must NOT be present (system notice requirement)
        assert "reasoning_effort" not in sent_body

        # Check turn completion and usage
        turn = provider.finalize()
        assert turn.text == "Hello from GPT-6"
        assert turn.usage.input == 120
        assert turn.usage.output == 15
        assert turn.usage.cache_read == 40

def test_gpt6_astra_chat_completions_omits_reasoning_effort_with_tools(workspace):
    from unittest.mock import patch, MagicMock
    from axon.config import Settings
    from axon.providers.openai_compat import OpenAICompatProvider

    settings = Settings(workspace=workspace, base_url="https://agentrouter.org/v1")
    provider = OpenAICompatProvider(settings)

    # If forced to chat/completions (e.g., via non-responses model or chat url fallback)
    # verify that if model has gpt-6 and tools are present, reasoning_effort is suppressed
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_lines.return_value = [
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        'data: [DONE]',
    ]
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_resp

    with patch.object(provider, "_responses_url", provider._url):
        # Force provider to think responses_url is chat completions
        with patch("httpx.stream", return_value=mock_stream_ctx) as mock_stream:
            # 1. Other reasoning model with tools keeps reasoning_effort
            list(provider.stream(
                model="o3-mini",
                system=[],
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
                max_tokens=1000,
                effort="high",
            ))
            sent_body = mock_stream.call_args[1]["json"]
            assert sent_body.get("reasoning_effort") == "high"

            # 2. In /v1/chat/completions, gpt-6-astra WITH tools suppresses reasoning_effort
            # to prevent "Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions"
            provider.settings = provider.settings.model_copy(update={"wire_api": "chat"})
            list(provider.stream(
                model="gpt-6-astra",
                system=[],
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
                max_tokens=1000,
                effort="high",
            ))
            sent_body = mock_stream.call_args[1]["json"]
            # When wire_api is chat, reasoning_effort MUST NOT be sent with tools for gpt-6-astra!
            assert "reasoning_effort" not in sent_body

def test_gpt6_astra_responses_sse_parsing(workspace):
    from unittest.mock import patch, MagicMock
    from axon.config import Settings
    from axon.providers.openai_compat import OpenAICompatProvider

    settings = Settings(workspace=workspace, base_url="https://agentrouter.org/v1")
    provider = OpenAICompatProvider(settings)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_lines.return_value = [
        'event: response.reasoning.delta',
        'data: {"type": "response.reasoning.delta", "delta": "Thinking about the files"}',
        'event: response.text.delta',
        'data: {"type": "response.text.delta", "delta": "I will run bash"}',
        'event: response.output_item.added',
        'data: {"type": "response.output_item.added", "output_index": 0, "item": {"id": "call_abc", "type": "function_call", "name": "Bash", "call_id": "call_abc"}}',
        'event: response.function_call_arguments.delta',
        'data: {"type": "response.function_call_arguments.delta", "call_id": "call_abc", "delta": "{\\"command\\": \\"ls\\"}"}',
        'event: response.output_item.done',
        'data: {"type": "response.output_item.done", "item": {"id": "call_abc", "type": "function_call", "name": "Bash", "call_id": "call_abc", "arguments": "{\\"command\\": \\"ls\\"}"}}',
        'event: response.done',
        'data: {"type": "response.done", "response": {"status": "completed", "usage": {"input_tokens": 500, "output_tokens": 60, "input_token_details": {"cached_tokens": 300}}}}',
        'data: [DONE]',
    ]

    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__enter__.return_value = mock_resp

    with patch("httpx.stream", return_value=mock_stream_ctx):
        events = list(provider.stream(
            model="gpt-6-astra",
            system=[],
            messages=[{"role": "user", "content": "list directory"}],
            tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            max_tokens=4096,
        ))

        turn = provider.finalize()
        assert turn.thinking == "Thinking about the files"
        assert turn.text == "I will run bash"
        assert len(turn.tool_uses) == 1
        assert turn.tool_uses[0].name == "Bash"
        assert turn.tool_uses[0].input == {"command": "ls"}
        assert turn.stop_reason == "tool_use"
        assert turn.usage.input == 500
        assert turn.usage.output == 60
        assert turn.usage.cache_read == 300

def test_gpt6_astra_error_recovery_to_responses(workspace):
    from unittest.mock import patch, MagicMock
    from axon.config import Settings
    from axon.providers.openai_compat import OpenAICompatProvider

    settings = Settings(workspace=workspace, base_url="https://agentrouter.org/v1/chat/completions")
    provider = OpenAICompatProvider(settings)

    # First attempt returns the exact error from the notice:
    # "Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions"
    error_resp = MagicMock()
    error_resp.status_code = 400
    error_resp.text = '{"error": {"message": "Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions"}}'

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.iter_lines.return_value = [
        'event: response.text.delta',
        'data: {"delta": "Recovered via Response protocol"}',
        'event: response.done',
        'data: {"response": {"usage": {"input_tokens": 50, "output_tokens": 10}}}',
        'data: [DONE]',
    ]

    err_ctx = MagicMock()
    err_ctx.__enter__.return_value = error_resp
    succ_ctx = MagicMock()
    succ_ctx.__enter__.return_value = success_resp

    call_count = 0
    def fake_stream(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return err_ctx
        return succ_ctx

    with patch("httpx.stream", side_effect=fake_stream):
        events = list(provider.stream(
            model="gpt-6-astra",
            system=[],
            messages=[{"role": "user", "content": "test"}],
            tools=[{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            max_tokens=1000,
            effort="medium",
        ))

        assert call_count == 2
        turn = provider.finalize()
        assert turn.text == "Recovered via Response protocol"

