"""/whatif: reprice the session's recorded tokens against other models' prices (not a re-run)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from axon.features import insights
from axon.providers.base import Usage
from axon.providers.pricing import cost_for, uncached_cost
from axon.session.ledger import Ledger
from axon.ui.theme import strip_ansi


def _ledger() -> Ledger:
    led = Ledger()
    led.record("claude-sonnet-4-5", Usage(input=100_000, output=2_000, cache_read=60_000, cache_write=10_000), source="main")
    led.record("claude-sonnet-4-5", Usage(input=50_000, output=1_000, cache_read=45_000), source="main")
    return led


class _Agent:
    def __init__(self, led: Ledger, model: str = "claude-sonnet-4-5") -> None:
        self.ledger = led
        self.settings = type("S", (), {"model": model})()


def test_totals_add_every_recorded_call() -> None:
    t = insights.session_tokens(_ledger())
    assert (t.input, t.output, t.cache_read, t.cache_write, t.calls) == (150_000, 3_000, 105_000, 10_000, 2)


def test_reprice_with_and_without_caching_matches_the_pricing_module() -> None:
    t = insights.session_tokens(_ledger())
    row = insights.reprice(t, "claude-haiku-4-5")
    assert row["priced"] is True
    expect_with = sum(cost_for("claude-haiku-4-5", e.usage) for e in _ledger().entries)
    expect_without = sum(uncached_cost("claude-haiku-4-5", e.usage) for e in _ledger().entries)
    assert abs(Decimal(str(row["with_cache"])) - expect_with) < Decimal("0.000001")
    assert abs(Decimal(str(row["without_cache"])) - expect_without) < Decimal("0.000001")
    assert row["without_cache"] > row["with_cache"]


def test_unknown_model_is_marked_not_priced_at_zero() -> None:
    row = insights.reprice(insights.session_tokens(_ledger()), "totally-unknown-9000")
    assert row["priced"] is False and row["with_cache"] is None and row["without_cache"] is None


def test_command_prints_the_disclaimer_and_marks_unknown(capsys: Any) -> None:
    insights.handle_whatif(_Agent(_ledger()), "totally-unknown-9000")
    out = strip_ansi(capsys.readouterr().out)
    assert "pricing calculation, not a re-run" in out
    assert "totally-unknown-9000" in out and "no known price" in out


def test_command_default_table_lists_models_and_actual_cost(capsys: Any) -> None:
    insights.handle_whatif(_Agent(_ledger()), "")
    out = strip_ansi(capsys.readouterr().out)
    assert "claude-haiku-4-5" in out and "with caching" in out and "without caching" in out
    assert "this session" in out and "150,000" in out


def test_empty_session_says_so(capsys: Any) -> None:
    insights.handle_whatif(_Agent(Ledger()), "")
    assert "nothing recorded" in strip_ansi(capsys.readouterr().out).lower()


def test_free_text_prefix_matches_several_models(capsys: Any) -> None:
    insights.handle_whatif(_Agent(_ledger()), "gpt-5")
    out = strip_ansi(capsys.readouterr().out)
    assert "gpt-5-mini" in out and "gpt-5 " in out + " "
