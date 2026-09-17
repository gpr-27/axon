"""
Decimal-based cost accounting, token metrics, and ledger reporting.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
import time
from typing import Any
from axon.providers.base import Usage
from axon.providers.registry import PRICING

@dataclass
class LedgerEntry:
    id: int
    timestamp: float
    tag: str              # "main", "subagent_1", "side_question", "prompt_enhancement", "memory_learn", etc.
    model: str
    usage: Usage
    cost: Decimal
    group_ratio: float = 1.0

class Ledger:
    def __init__(self) -> None:
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_cache_write_tokens: int = 0
        self.total_reasoning_tokens: int = 0
        self.total_cost: Decimal = Decimal("0.0")
        self.turn_costs: list[Decimal] = []
        self.entries: list[LedgerEntry] = []
        self.chat_count: int = 0
        self.last_usage: Usage | None = None

    def clear(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_write_tokens = 0
        self.total_reasoning_tokens = 0
        self.total_cost = Decimal("0.0")
        self.turn_costs.clear()
        self.entries.clear()
        self.chat_count = 0
        self.last_usage = None

    def record(self, model: str, usage: Usage, *, tag: str = "main", group_ratio: float = 1.0) -> Decimal:
        pricing = PRICING.get(model, {"input": 3.0, "output": 15.0})
        in_rate = Decimal(str(pricing.get("input", 3.0)))
        out_rate = Decimal(str(pricing.get("output", 15.0)))

        # Cache read rate: from pricing or default to 0.2x (AgentRouter ratio)
        cache_rate_val = pricing.get("cache_read")
        if cache_rate_val is not None:
            cache_read_rate = Decimal(str(cache_rate_val))
        else:
            cache_read_rate = in_rate * Decimal("0.2")

        cache_write_val = pricing.get("cache_write")
        if cache_write_val is not None:
            cache_write_rate = Decimal(str(cache_write_val))
        else:
            cache_write_rate = in_rate * Decimal("1.25")

        cached_tokens = max(0, usage.cache_read)
        write_tokens = max(0, usage.cache_write)
        uncached_tokens = max(0, usage.input - cached_tokens - write_tokens)

        # Cost formula matching AgentRouter & Anthropic:
        # (uncached / 1M * input_rate) + (cached / 1M * cache_read_rate) + (write / 1M * cache_write_rate) + (output / 1M * output_rate)
        cost_uncached = (Decimal(uncached_tokens) / Decimal("1000000")) * in_rate
        cost_cache = (Decimal(cached_tokens) / Decimal("1000000")) * cache_read_rate
        cost_write = (Decimal(write_tokens) / Decimal("1000000")) * cache_write_rate
        cost_out = (Decimal(usage.output) / Decimal("1000000")) * out_rate

        g_ratio = Decimal(str(group_ratio))
        turn_cost = (cost_uncached + cost_cache + cost_write + cost_out) * g_ratio

        self.total_input_tokens += usage.input
        self.total_output_tokens += usage.output
        self.total_cache_read_tokens += usage.cache_read
        self.total_cache_write_tokens += usage.cache_write
        self.total_reasoning_tokens += usage.reasoning
        self.total_cost += turn_cost
        self.turn_costs.append(turn_cost)
        self.last_usage = usage

        entry = LedgerEntry(
            id=len(self.entries) + 1,
            timestamp=time.time(),
            tag=tag,
            model=model,
            usage=usage,
            cost=turn_cost,
            group_ratio=group_ratio,
        )
        self.entries.append(entry)

        return turn_cost

    def total(self) -> Decimal:
        return self.total_cost

    def tag_breakdown(self) -> dict[str, dict[str, Any]]:
        """Returns aggregated token usage, cost, and call counts grouped by tag."""
        groups: dict[str, dict[str, Any]] = {}
        for e in self.entries:
            tag = e.tag or "main"
            if tag not in groups:
                groups[tag] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost": Decimal("0.0"),
                    "models": set(),
                }
            g = groups[tag]
            g["calls"] += 1
            g["input_tokens"] += e.usage.input
            g["output_tokens"] += e.usage.output
            g["tokens"] = g["input_tokens"] + g["output_tokens"]
            g["cache_read_tokens"] += e.usage.cache_read
            g["cache_write_tokens"] += e.usage.cache_write
            g["reasoning_tokens"] += e.usage.reasoning
            g["cost"] += e.cost
            g["models"].add(e.model)
        return groups

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
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def render(self, model: str) -> str:
        tot_tok = self.total_tokens()
        has_subagents = any(e.tag.startswith("subagent_") for e in self.entries)
        title = "=== Session Cost & Token Ledger (Combined Main + Subagents) ===" if has_subagents else "=== Cost & Token Ledger ==="
        lines = [
            title,
            f"Total Cost      : ${self.total_cost:.5f}",
            f"Total Tokens    : {tot_tok:,} (In: {self.total_input_tokens:,}, Out: {self.total_output_tokens:,})",
        ]
        if self.total_cache_read_tokens > 0 or self.total_cache_write_tokens > 0:
            hit_pct = (self.total_cache_read_tokens / max(1, self.total_input_tokens)) * 100
            lines.append(f"Cache Tokens    : {self.total_cache_read_tokens:,} read ({hit_pct:.1f}% hit rate)")
            sav = self.savings_pct(model)
            if sav > 0:
                cf = self.uncached_counterfactual(model)
                dollars_saved = cf - self.total_cost
                lines.append(f"Cache Savings   : ${dollars_saved:.5f} ({sav:.1f}% saved vs uncached)")
        lines.append(f"API Calls Tracked: {len(self.entries)}")

        # Itemized breakdown by category
        breakdown = self.tag_breakdown()
        if len(breakdown) > 0:
            tag_labels = {
                "main": "Main Agent Turns",
                "side_question": "Side Inquiries (/btw)",
                "prompt_enhancement": "Prompt Optimizer (/prompt)",
                "memory_learn": "Memory Extraction (/learn)",
                "doctor_ping": "Diagnostics Ping (/doctor)",
            }
            lines.append("\n--- API Usage Breakdown by Source ---")
            for tag, data in sorted(breakdown.items(), key=lambda x: x[1]["cost"], reverse=True):
                label = tag_labels.get(tag)
                if not label:
                    if tag.startswith("subagent_"):
                        sub_num = tag.split("_")[-1]
                        label = f"Subagent #{sub_num}"
                    else:
                        label = tag.replace("_", " ").title()
                tag_tot = data["input_tokens"] + data["output_tokens"]
                lines.append(
                    f"  • {label:<26}: {data['calls']} call{'s' if data['calls'] != 1 else ''} · "
                    f"{tag_tot:,} tokens · ${data['cost']:.5f}"
                )
        return "\n".join(lines)

    def render_call_history(self) -> str:
        """Renders an itemized log of every individual API call tracked."""
        if not self.entries:
            return "No individual API calls recorded."
        lines = ["=== Itemized API Call Log ==="]
        for e in self.entries:
            t_str = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
            tot = e.usage.input + e.usage.output
            c_info = f" (cache read: {e.usage.cache_read:,})" if e.usage.cache_read > 0 else ""
            lines.append(
                f"  #{e.id:02d} [{t_str}] {e.tag:<18} ({e.model}): "
                f"{tot:,} tokens (in: {e.usage.input:,}{c_info} · out: {e.usage.output:,}) · ${e.cost:.5f}"
            )
        return "\n".join(lines)
