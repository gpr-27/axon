"""
Tests for AgentRouter FAQ system, comprehensive API call tracking across all surfaces,
and updated model pricing calculations.
"""
from decimal import Decimal
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from axon.session.ledger import Ledger, LedgerEntry
from axon.providers.base import Usage, AssistantTurn
from axon.providers.registry import PRICING
from axon.commands.builtin import handle_faq, handle_cost, handle_btw, handle_prompt, dispatch_command
from axon.providers.openai_compat import OpenAICompatProvider
from axon.providers.anthropic import AnthropicProvider
from axon.config import Settings


def test_agentrouter_model_pricing_constants():
    """Verify all new AgentRouter models have exact pricing according to console table."""
    # deepseek-v4-flash: Prompt $4.00, Completion $12.00, Cache $0.80 (Live deduction log rates)
    ds = PRICING["deepseek-v4-flash"]
    assert ds["input"] == 4.0
    assert ds["output"] == 12.0
    assert ds["cache_read"] == 0.8

    # gpt-5.6-sol: Prompt $3.00, Completion $15.00, Cache $0.60
    sol = PRICING["gpt-5.6-sol"]
    assert sol["input"] == 3.0
    assert sol["output"] == 15.0
    assert sol["cache_read"] == 0.6

    # gpt-6-astra: Prompt $3.00, Completion $15.00, Cache $0.60
    astra = PRICING["gpt-6-astra"]
    assert astra["input"] == 3.0
    assert astra["output"] == 15.0
    assert astra["cache_read"] == 0.6

    # claude-opus-5: Prompt $6.00, Completion $30.00, Cache Read $1.20, Cache Write $7.50
    op5 = PRICING["claude-opus-5"]
    assert op5["input"] == 6.0
    assert op5["output"] == 30.0
    assert op5["cache_read"] == 1.2
    assert op5["cache_write"] == 7.5

    # claude-opus-4-8: Prompt $8.00, Completion $40.00, Cache Read $1.60, Cache Write $10.00
    op48 = PRICING["claude-opus-4-8"]
    assert op48["input"] == 8.0
    assert op48["output"] == 40.0
    assert op48["cache_read"] == 1.6
    assert op48["cache_write"] == 10.0


def test_ledger_separate_call_tracking_and_breakdown():
    """Test that every API call is recorded with its distinct tag and itemized in breakdown."""
    ledger = Ledger()

    # 1. Main agent turn
    u1 = Usage(input=1000, output=200, cache_read=500)
    c1 = ledger.record("claude-opus-5", u1, tag="main")

    # 2. Side question (/btw)
    u2 = Usage(input=300, output=80)
    c2 = ledger.record("claude-opus-5", u2, tag="side_question")

    # 3. Prompt enhancement (/prompt)
    u3 = Usage(input=400, output=100)
    c3 = ledger.record("deepseek-v4-flash", u3, tag="prompt_enhancement")

    # 4. Memory extraction (/learn)
    u4 = Usage(input=250, output=50)
    c4 = ledger.record("deepseek-v4-flash", u4, tag="memory_learn")

    # 5. Subagent execution
    u5 = Usage(input=2000, output=500)
    c5 = ledger.record("gpt-6-astra", u5, tag="subagent_1")

    assert len(ledger.entries) == 5
    assert ledger.total_cost == c1 + c2 + c3 + c4 + c5

    # Verify breakdown
    breakdown = ledger.tag_breakdown()
    assert "main" in breakdown
    assert "side_question" in breakdown
    assert "prompt_enhancement" in breakdown
    assert "memory_learn" in breakdown
    assert "subagent_1" in breakdown

    assert breakdown["main"]["calls"] == 1
    assert breakdown["side_question"]["tokens"] == 380
    assert breakdown["prompt_enhancement"]["tokens"] == 500
    assert breakdown["memory_learn"]["tokens"] == 300
    assert breakdown["subagent_1"]["tokens"] == 2500

    # Verify render displays the category sections
    rendered = ledger.render("claude-opus-5")
    assert "Breakdown by Source" in rendered
    assert "Main Agent Turns" in rendered
    assert "Side Inquiries" in rendered
    assert "Prompt Optimizer" in rendered
    assert "Memory Extraction" in rendered
    assert "Subagent #1" in rendered

    # Verify call history
    history = ledger.render_call_history()
    assert "#01" in history
    assert "#05" in history
    assert "claude-opus-5" in history
    assert "deepseek-v4-flash" in history
    assert "gpt-6-astra" in history


def test_faq_command_all_and_topics(capsys):
    """Test /faq command rendering full guide and specific topics."""
    agent = MagicMock()

    # 1. Full FAQ listing
    res = handle_faq(agent, "")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "AgentRouter Frequently Asked Questions" in out
    assert "402 Budget Pool Quota Has Been Exhausted" in out
    assert "400 Content Blocked" in out
    assert "401 Unauthorized" in out
    assert "Sensitive Words Detected" in out
    assert "Backup Domain" in out
    assert "GPT-6 Astra" in out
    assert "Pricing Table" in out

    # 2. Specific topic jump: 402
    res = handle_faq(agent, "402")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "402 Budget Pool Quota Has Been Exhausted" in out
    assert "00:00 Beijing Time" in out
    assert "deepseek-v4-flash" in out
    # Shouldn't dump other topics when filtering
    assert "400 Content Blocked" not in out

    # 3. Specific topic jump: quota keyword
    res = handle_faq(agent, "quota")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "402 Budget Pool Quota Has Been Exhausted" in out

    # 4. Specific topic jump: 400
    res = handle_faq(agent, "400")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "400 Content Blocked (Language Restriction)" in out
    assert "Chinese (中文), English, French, German, Russian" in out

    # 5. Specific topic jump: sensitive
    res = handle_faq(agent, "sensitive")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "Sensitive Words Detected" in out
    assert "/clear" in out

    # 6. Specific topic jump: pricing
    res = handle_faq(agent, "pricing")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "deepseek-v4-flash" in out
    assert "claude-opus-5" in out
    assert "$6.000" in out


def test_dispatch_command_routes_faq():
    """Verify dispatch_command routes /faq and /faqs."""
    agent = MagicMock()
    r1 = dispatch_command(agent, "/faq")
    assert r1.handled is True

    r2 = dispatch_command(agent, "/faqs 402")
    assert r2.handled is True

    r3 = dispatch_command(agent, "/pricing")
    assert r3.handled is True

    r4 = dispatch_command(agent, "/prices")
    assert r4.handled is True


def test_handle_cost_calls_view(capsys):
    """Verify /cost calls renders the itemized call history."""
    agent = MagicMock()
    agent.ledger = Ledger()
    agent.ledger.record("gpt-5.6-sol", Usage(input=500, output=120), tag="side_question")

    res = handle_cost(agent, "calls")
    assert res.handled is True
    out = capsys.readouterr().out
    assert "Detailed Call-by-Call API History" in out
    assert "gpt-5.6-sol" in out
    assert "side_question" in out


def test_openai_compat_provider_faq_diagnostics(monkeypatch):
    """Verify OpenAICompatProvider formats AgentRouter FAQ messages cleanly on errors."""
    from pydantic import SecretStr
    from axon.errors import ProviderError
    settings = Settings(api_key=SecretStr("dummy_key"), base_url="https://agentrouter.org/v1")
    prov = OpenAICompatProvider(settings)

    # Mock httpx.stream to raise 402 error
    def mock_stream_402(*args, **kwargs):
        raise Exception("HTTP 402: budget pool quota has been exhausted")

    monkeypatch.setattr("httpx.stream", mock_stream_402)
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-opus-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100))
    assert "402 Budget Pool Quota Exhausted" in str(exc.value)
    assert "00:00 Beijing time" in str(exc.value)
    assert "/faq 402" in str(exc.value)

    # Mock httpx.stream to raise 400 content blocked
    def mock_stream_400(*args, **kwargs):
        raise Exception("400 content blocked")

    monkeypatch.setattr("httpx.stream", mock_stream_400)
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-opus-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100))
    assert "400 Content Blocked" in str(exc.value)
    assert "Chinese, English, French, German, and Russian" in str(exc.value)

    # Mock httpx.stream to raise sensitive words
    def mock_stream_sens(*args, **kwargs):
        raise Exception("sensitive_words_detected")

    monkeypatch.setattr("httpx.stream", mock_stream_sens)
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-opus-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100))
    assert "Content filter triggered (sensitive_words_detected)" in str(exc.value)
    assert "/clear" in str(exc.value)


def test_anthropic_provider_faq_diagnostics(monkeypatch):
    """Verify AnthropicProvider formats AgentRouter FAQ messages cleanly on errors."""
    from pydantic import SecretStr
    from axon.errors import ProviderError
    settings = Settings(api_key=SecretStr("dummy_key"), base_url="https://agentrouter.org")
    prov = AnthropicProvider(settings)

    # Mock client stream to raise 402
    def mock_client_stream_402(**kwargs):
        raise Exception("402 budget pool quota has been exhausted")

    prov._client.messages.stream = mock_client_stream_402
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-opus-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100))
    assert "402 Budget Pool Quota Exhausted" in str(exc.value)

    # Mock client stream to raise 400 content blocked
    def mock_client_stream_400(**kwargs):
        raise Exception("400 content blocked")

    prov._client.messages.stream = mock_client_stream_400
    with pytest.raises(ProviderError) as exc:
        list(prov.stream(model="claude-opus-5", system=[], messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=100))
    assert "400 Content Blocked" in str(exc.value)
