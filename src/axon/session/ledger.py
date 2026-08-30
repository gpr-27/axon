"""
Decimal-based cost accounting, token metrics, and ledger reporting.
"""
from __future__ import annotations
from decimal import Decimal
from axon.providers.base import Usage
from axon.providers.registry import PRICING

class Ledger:
    def __init__(self) -> None:
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.total_reasoning_tokens: int = 0
        self.total_cost: Decimal = Decimal("0.0")
        self.turn_costs: list[Decimal] = []
        self.chat_count: int = 0

    def clear(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_reasoning_tokens = 0
        self.total_cost = Decimal("0.0")
        self.turn_costs.clear()
        self.chat_count = 0

    def record(self, model: str, usage: Usage, *, tag: str = "main") -> Decimal:
        pricing = PRICING.get(model, {"input": 3.0, "output": 15.0, "cache_read": 0.6})
        in_rate = Decimal(str(pricing.get("input", 3.0)))
        out_rate = Decimal(str(pricing.get("output", 15.0)))
        cache_rate = Decimal(str(pricing.get("cache_read", 0.6)))

        # Cost formula
        uncached_in = max(0, usage.input - usage.cache_read)
        cost_in = (Decimal(uncached_in) / Decimal("1000000")) * in_rate
        cost_cache = (Decimal(usage.cache_read) / Decimal("1000000")) * cache_rate
        cost_out = (Decimal(usage.output) / Decimal("1000000")) * out_rate

        turn_cost = cost_in + cost_cache + cost_out

        self.total_input_tokens += usage.input
        self.total_output_tokens += usage.output
        self.total_cache_read_tokens += usage.cache_read
        self.total_cache_write_tokens += usage.cache_write
        self.total_reasoning_tokens += usage.reasoning
        self.total_cost += turn_cost
        self.turn_costs.append(turn_cost)

        return turn_cost

    def total(self) -> Decimal:
        return self.total_cost

    def uncached_counterfactual(self, model: str) -> Decimal:
        """Calculate what the session would have cost without prompt caching."""
        pricing = PRICING.get(model, {"input": 3.0, "output": 15.0})
        in_rate = Decimal(str(pricing.get("input", 3.0)))
        out_rate = Decimal(str(pricing.get("output", 15.0)))
        full_in = (Decimal(self.total_input_tokens) / Decimal("1000000")) * in_rate
        full_out = (Decimal(self.total_output_tokens) / Decimal("1000000")) * out_rate
        return full_in + full_out

    def savings_pct(self, model: str) -> float:
        """Percentage saved compared to uncached counterfactual."""
        cf = self.uncached_counterfactual(model)
        if cf > 0:
            return float((cf - self.total_cost) / cf * 100)
        return 0.0

    def render(self, model: str) -> str:
        tot_tok = self.total_input_tokens + self.total_output_tokens
        tot_prompt = self.total_input_tokens + self.total_cache_read_tokens if self.total_cache_read_tokens > self.total_input_tokens else self.total_input_tokens
        cache_pct = (
            min(100.0, (self.total_cache_read_tokens / max(1, tot_prompt) * 100))
            if tot_prompt > 0 else 0.0
        )
        counterfactual = self.uncached_counterfactual(model)
        savings = max(Decimal("0.0"), counterfactual - self.total_cost)

        return (
            f"=== Cost & Token Ledger ===\n"
            f"Total Cost      : ${self.total_cost:.5f} (Saved ${savings:.5f} via prompt caching)\n"
            f"Total Tokens    : {tot_tok:,} (In: {self.total_input_tokens:,}, Out: {self.total_output_tokens:,})\n"
            f"Cache Read Toks : {self.total_cache_read_tokens:,} ({cache_pct:.1f}% hit ratio)\n"
            f"Turns Recorded  : {len(self.turn_costs)}"
        )
