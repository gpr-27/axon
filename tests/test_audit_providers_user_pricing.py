"""[pricing.<provider>."<model>"] in config.toml: user prices and context sizes for custom endpoints."""
from decimal import Decimal

from axon.providers import pricing
from axon.providers.base import Usage
from axon.providers.registry import MODEL_CONTEXT_LIMITS, apply_user_pricing, get_context_window

TABLE = {"my-gw": {"gw-model-x": {"input": 2.0, "output": 10.0, "context": 300_000}}}


def teardown_function():
    apply_user_pricing({})


def test_cost_uses_the_user_price():
    assert apply_user_pricing(TABLE) == 1
    cost = pricing.cost_for("gw-model-x", Usage(input=1_000_000, output=1_000_000), provider="my-gw")
    assert cost == Decimal("12")


def test_context_window_is_applied():
    apply_user_pricing(TABLE)
    assert get_context_window("gw-model-x") == 300_000


def test_reloading_removes_old_entries_and_restores_context():
    original = MODEL_CONTEXT_LIMITS.get("gpt-oss-120b")
    apply_user_pricing({"my-gw": {"gpt-oss-120b": {"input": 1, "output": 1, "context": 999_999}}})
    assert MODEL_CONTEXT_LIMITS["gpt-oss-120b"] == 999_999
    apply_user_pricing({})
    assert MODEL_CONTEXT_LIMITS.get("gpt-oss-120b") == original
    assert "gpt-oss-120b" not in pricing.PROVIDER_PRICING.get("my-gw", {})


def test_incomplete_or_negative_entries_are_ignored():
    bad = {"my-gw": {"a": {"input": 1}, "b": {"input": -1, "output": 1}, "c": {"input": "x", "output": 1}, "d": "nope"}}
    assert apply_user_pricing(bad) == 0
    assert apply_user_pricing(None) == 0
