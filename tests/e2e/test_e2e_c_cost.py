"""
E2E-C: cost accuracy. For several priced models the expected USD cost is computed BY HAND from the
rate tables in providers/pricing.py for a scripted usage (incl. cache read / cache write), then
compared with every place Axon reports it: `--output-format json` cost_usd, ~/.axon/usage.jsonl,
`/cost` (session ledger, resumed in a new process) and `/stats` / `axon stats` (usage log).
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from harness import AxonEnv
from test_e2e_c_support import ext_llm, money, no_traceback, usage_records, xenv  # noqa: F401

pytestmark = pytest.mark.e2e

from axon.providers.pricing import AGENTROUTER_PRICING, PROVIDER_PRICING  # noqa: E402


def _rates(table: str, model: str) -> dict[str, float]:
    if table == "agentrouter":
        r = dict(AGENTROUTER_PRICING[model])
    else:
        r = dict(PROVIDER_PRICING[table][model])
    r.setdefault("cache_read", r["input"])     # documented fallback: no discount assumed
    r.setdefault("cache_write", r["input"])
    return r


def _expected(rates: dict[str, float], total_in: int, out: int, read: int = 0, write: int = 0) -> Decimal:
    uncached = total_in - read - write
    return (money(rates["input"], uncached) + money(rates["cache_read"], read)
            + money(rates["cache_write"], write) + money(rates["output"], out))


def _display_value(text: str, label_re: str) -> tuple[Decimal, int]:
    m = re.search(label_re + r"\s+\$([0-9][0-9,]*\.?[0-9]*)", text)
    assert m, f"{label_re!r} not found in:\n{text[-3000:]}"
    raw = m.group(1).replace(",", "")
    decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
    return Decimal(raw), decimals


def _assert_display_matches(text: str, label_re: str, expected: Decimal) -> None:
    shown, decimals = _display_value(text, label_re)
    quantum = Decimal(1).scaleb(-decimals)
    assert expected == expected.quantize(quantum), f"test data not exact at {decimals} decimals: {expected}"
    assert abs(shown - expected) < Decimal("1e-6"), (label_re, shown, expected)


def check_all_surfaces(env: AxonEnv, expected: Decimal, model: str, calls: int = 1, extra: dict | None = None,
                       first_json: dict | None = None) -> None:
    """json cost (if given), usage.jsonl, /cost, /stats and `axon stats` all equal `expected` (1e-6)."""
    if first_json is not None:
        assert abs(Decimal(str(first_json["cost_usd"])) - expected) < Decimal("1e-6"), (first_json["cost_usd"], expected)
    recs = [r for r in usage_records(env) if r["source"] == "main"]
    assert len(recs) == calls, recs
    assert all(r["model"] == model for r in recs), [r["model"] for r in recs]
    log_total = sum(Decimal(str(r["cost"])) for r in recs)
    assert abs(log_total - expected) < Decimal("1e-6"), (log_total, expected)

    rc = env.run(["-p", "--continue", "/cost"], env=extra or {})
    assert rc.code == 0, str(rc)
    no_traceback(rc)
    _assert_display_matches(rc.output, r"Total", expected)

    rs = env.run(["-p", "--continue", "/stats", "all"], env=extra or {})
    assert rs.code == 0, str(rs)
    _assert_display_matches(rs.output, r"Spend", expected)

    ra = env.run(["stats", "all"], env=extra or {})
    assert ra.code == 0, str(ra)
    _assert_display_matches(ra.output, r"Spend", expected)


# (id, env, model logged, rate table, rate key, total_in, out, cache_read, cache_write)
CASES = [
    ("gpt-4o-custom-endpoint", {"AXON_MODEL": "gpt-4o"}, "gpt-4o", "openai", "gpt-4o", 100_000, 20_000, 40_000, 0),
    ("gpt-4.1-mini-openai", {"AXON_PROVIDER": "openai", "AXON_MODEL": "gpt-4.1-mini"}, "gpt-4.1-mini", "openai",
     "gpt-4.1-mini", 500_000, 250_000, 100_000, 0),
    ("claude-sonnet-4-6-cache-rw", {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-sonnet-4-6"}, "claude-sonnet-4-6",
     "anthropic", "claude-sonnet-4-6", 300_000, 20_000, 120_000, 80_000),
    ("claude-haiku-4-5-cache-rw", {"AXON_PROVIDER": "anthropic", "AXON_MODEL": "claude-haiku-4-5"}, "claude-haiku-4-5",
     "anthropic", "claude-haiku-4-5", 200_000, 10_000, 60_000, 40_000),
    ("deepseek-chat", {"AXON_PROVIDER": "deepseek", "AXON_MODEL": "deepseek-chat"}, "deepseek-chat", "deepseek",
     "deepseek-chat", 1_000_000, 100_000, 500_000, 0),
    ("grok-4", {"AXON_PROVIDER": "xai", "AXON_MODEL": "grok-4"}, "grok-4", "xai", "grok-4", 200_000, 10_000, 100_000, 0),
    ("gemini-2.5-flash", {"AXON_PROVIDER": "gemini", "AXON_MODEL": "gemini-2.5-flash"}, "gemini-2.5-flash", "gemini",
     "gemini-2.5-flash", 400_000, 40_000, 200_000, 0),
    ("agentrouter-deepseek-v4-flash", {"AXON_PROVIDER": "agentrouter", "AXON_MODEL": "deepseek-v4-flash"},
     "deepseek-v4-flash", "agentrouter", "deepseek-v4-flash", 250_000, 25_000, 50_000, 0),
    ("mistral-large-no-cache-rate", {"AXON_PROVIDER": "mistral", "AXON_MODEL": "mistral-large-latest"},
     "mistral-large-latest", "mistral", "mistral-large", 100_000, 50_000, 0, 0),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_cost_matches_hand_computed_price_everywhere(xenv: AxonEnv, case) -> None:
    """One call with scripted usage: json, usage.jsonl, /cost, /stats and `axon stats` equal the hand-computed cost."""
    _id, env, logged_model, table, key, total_in, out, read, write = case
    expected = _expected(_rates(table, key), total_in, out, read, write)
    assert expected > 0
    xenv.llm.enqueue([{"text": "priced"}, {"usage": {"input": total_in, "output": out, "cached": read, "cache_write": write}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    u = r.json()["usage"]
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_tokens"], u["cache_write_tokens"]) == (total_in, out, read, write)
    check_all_surfaces(xenv, expected, logged_model, extra=env, first_json=r.json())


def test_cost_prefixed_anthropic_model_uses_anthropic_rates(xenv: AxonEnv) -> None:
    """--provider anthropic --model anthropic/claude-opus-4-8 is billed at Anthropic list price."""
    expected = _expected(_rates("anthropic", "claude-opus-4-8"), 100_000, 10_000)
    xenv.llm.enqueue([{"text": "opus"}, {"usage": {"input": 100_000, "output": 10_000}}])
    r = xenv.run(["-p", "--output-format", "json", "--provider", "anthropic", "--model", "anthropic/claude-opus-4-8", "hi"])
    assert r.code == 0, str(r)
    assert abs(Decimal(str(r.json()["cost_usd"])) - expected) < Decimal("1e-6"), (r.json()["cost_usd"], expected)


def test_cost_bare_anthropic_model_uses_anthropic_rates(xenv: AxonEnv) -> None:
    """Control for the bug above: the bare id with --provider anthropic is billed at $5/$25."""
    expected = _expected(_rates("anthropic", "claude-opus-4-8"), 100_000, 10_000)
    xenv.llm.enqueue([{"text": "opus"}, {"usage": {"input": 100_000, "output": 10_000}}])
    r = xenv.run(["-p", "--output-format", "json", "--provider", "anthropic", "--model", "claude-opus-4-8", "hi"])
    assert r.code == 0, str(r)
    assert abs(Decimal(str(r.json()["cost_usd"])) - expected) < Decimal("1e-6")


def test_cost_sums_over_a_multi_call_tool_turn(xenv: AxonEnv) -> None:
    """Two LLM calls in one turn (tool call + answer): every surface reports the sum of both calls."""
    rates = _rates("openai", "gpt-4o")
    c1 = _expected(rates, 100_000, 4_000, 0)
    c2 = _expected(rates, 120_000, 6_000, 100_000)
    xenv.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo cost", "description": "e"}}, {"usage": {"input": 100_000, "output": 4_000}}],
        [{"text": "done"}, {"usage": {"input": 120_000, "output": 6_000, "cached": 100_000}}],
    )
    r = xenv.run(["-p", "--output-format", "json", "go"])
    assert r.code == 0, str(r)
    assert r.json()["iterations"] == 2
    recs = usage_records(xenv)
    assert [Decimal(str(x["cost"])) for x in recs] == [c1, c2]
    check_all_surfaces(xenv, c1 + c2, "gpt-4o", calls=2, first_json=r.json())


def test_cost_accumulates_across_resumed_turns(xenv: AxonEnv) -> None:
    """Turn 1 + turn 2 (--continue): json cost_usd of the second run is the session total; usage log has both."""
    rates = _rates("openai", "gpt-4o")
    c1 = _expected(rates, 200_000, 10_000)
    c2 = _expected(rates, 300_000, 20_000, 200_000)
    xenv.llm.enqueue([{"text": "one"}, {"usage": {"input": 200_000, "output": 10_000}}])
    r1 = xenv.run(["-p", "--output-format", "json", "first"])
    assert r1.code == 0, str(r1)
    xenv.llm.enqueue([{"text": "two"}, {"usage": {"input": 300_000, "output": 20_000, "cached": 200_000}}])
    r2 = xenv.run(["-p", "--continue", "--output-format", "json", "second"])
    assert r2.code == 0, str(r2)
    assert abs(Decimal(str(r1.json()["cost_usd"])) - c1) < Decimal("1e-6")
    assert abs(Decimal(str(r2.json()["cost_usd"])) - (c1 + c2)) < Decimal("1e-6"), (r2.json()["cost_usd"], c1 + c2)
    check_all_surfaces(xenv, c1 + c2, "gpt-4o", calls=2)


@pytest.mark.parametrize("provider,model", [("ollama", "llama3.1:8b"), ("lmstudio", "qwen/qwen3-coder-30b")])
def test_local_model_costs_zero(xenv: AxonEnv, provider: str, model: str) -> None:
    """Local providers are free: 1M tokens -> $0 in json, usage.jsonl and /cost."""
    env = {"AXON_PROVIDER": provider, "AXON_MODEL": model}
    xenv.llm.enqueue([{"text": "free"}, {"usage": {"input": 1_000_000, "output": 1_000_000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    assert r.json()["cost_usd"] == 0
    assert [x["cost"] for x in usage_records(xenv)] == [0.0]
    rc = xenv.run(["-p", "--continue", "/cost"], env=env)
    assert "$0.00000" in rc.output and "(unpriced)" not in rc.output, rc.output


def test_local_named_model_on_paid_looking_id_is_free_with_ollama(xenv: AxonEnv) -> None:
    """Even a 'gpt-4o' id served by the ollama preset is local and therefore free."""
    xenv.llm.enqueue([{"text": "free"}, {"usage": {"input": 500_000, "output": 500_000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env={"AXON_PROVIDER": "ollama", "AXON_MODEL": "gpt-4o"})
    assert r.code == 0, str(r)
    assert r.json()["cost_usd"] == 0


def test_unknown_model_is_unpriced_and_flagged(xenv: AxonEnv) -> None:
    """An unknown cloud model costs $0 and /cost marks it '(unpriced)' instead of guessing a price."""
    env = {"AXON_PROVIDER": "openai", "AXON_MODEL": "mystery-model-9000"}
    xenv.llm.enqueue([{"text": "?"}, {"usage": {"input": 100_000, "output": 100_000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    assert r.json()["cost_usd"] == 0
    rc = xenv.run(["-p", "--continue", "/cost"], env=env)
    assert rc.code == 0, str(rc)
    assert "(unpriced)" in rc.output, rc.output
    assert "mystery-model-9000" in rc.output


def test_unknown_model_logged_with_zero_cost_and_real_tokens(xenv: AxonEnv) -> None:
    """usage.jsonl keeps the unknown model's real token counts but a $0 cost (never a guessed price)."""
    env = {"AXON_PROVIDER": "openai", "AXON_MODEL": "mystery-model-9001"}
    xenv.llm.enqueue([{"text": "?"}, {"usage": {"input": 4321, "output": 123}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    recs = usage_records(xenv)
    assert len(recs) == 1
    assert recs[0]["model"] == "mystery-model-9001" and recs[0]["cost"] == 0
    assert recs[0]["usage"]["input"] == 4321 and recs[0]["usage"]["output"] == 123


def test_cost_calls_lists_each_call_cost(xenv: AxonEnv) -> None:
    """`/cost calls` shows one row per API call with that call's cost."""
    rates = _rates("openai", "gpt-4o")
    c1 = _expected(rates, 100_000, 0)
    c2 = _expected(rates, 0, 100_000)
    xenv.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo x", "description": "x"}}, {"usage": {"input": 100_000, "output": 0}}],
        [{"text": "y"}, {"usage": {"input": 0, "output": 100_000}}],
    )
    assert xenv.run(["-p", "go"]).code == 0
    rc = xenv.run(["-p", "--continue", "/cost", "calls"])
    assert rc.code == 0, str(rc)
    text = rc.output
    assert f"{float(c1):.5f}"[:6] in text or f"{float(c1):.4f}" in text, text
    assert f"{float(c2):.4f}" in text, text


def test_pricing_command_lists_model_rates(xenv: AxonEnv) -> None:
    """`/pricing gpt-4o` prints the per-1M input/output rates from pricing.py."""
    rates = _rates("openai", "gpt-4o")
    r = xenv.run(["-p", "/pricing gpt-4o"])
    assert r.code == 0, str(r)
    assert f"${rates['input']:.3f}" in r.output and f"${rates['output']:.3f}" in r.output, r.output


def test_deepseek_prompt_cache_hit_tokens_are_priced_as_cache_reads(xenv: AxonEnv) -> None:
    """DeepSeek's own usage fields (prompt_cache_hit_tokens) are read as cache hits and priced at the cache rate."""
    env = {"AXON_PROVIDER": "deepseek", "AXON_MODEL": "deepseek-chat"}
    expected = _expected(_rates("deepseek", "deepseek-chat"), 1_000_000, 100_000, 600_000)
    xenv.llm.enqueue([{"text": "ds"}, {"usage_raw": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                                                     "total_tokens": 1_100_000, "prompt_cache_hit_tokens": 600_000,
                                                     "prompt_cache_miss_tokens": 400_000}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    assert r.json()["usage"]["cache_read_tokens"] == 600_000
    check_all_surfaces(xenv, expected, "deepseek-chat", extra=env, first_json=r.json())


def test_reasoning_tokens_logged_in_usage_jsonl(xenv: AxonEnv) -> None:
    """completion_tokens_details.reasoning_tokens is persisted in usage.jsonl (cost uses completion_tokens)."""
    env = {"AXON_PROVIDER": "openai", "AXON_MODEL": "o4-mini"}
    rates = _rates("openai", "o4-mini")
    expected = _expected(rates, 100_000, 50_000)
    xenv.llm.enqueue([{"text": "r"}, {"usage_raw": {"prompt_tokens": 100_000, "completion_tokens": 50_000,
                                                    "completion_tokens_details": {"reasoning_tokens": 30_000}}}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], env=env)
    assert r.code == 0, str(r)
    rec = usage_records(xenv)[0]
    assert rec["usage"]["reasoning"] == 30_000 and rec["usage"]["output"] == 50_000
    assert abs(Decimal(str(rec["cost"])) - expected) < Decimal("1e-6")
