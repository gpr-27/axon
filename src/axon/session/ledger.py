"""
Decimal-based cost accounting: one entry per API call (source, model, tokens, cost), totals,
per-source/model breakdowns for /cost, and a JSON-safe persistence format.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from axon.providers.base import Usage
from axon.providers.pricing import cost_details, price_for, uncached_cost
from axon.providers.registry import PRICING  # noqa: F401  (legacy import path kept for callers)

_ZERO = Decimal("0")

SOURCE_LABELS = {
    "main": "Main Agent Turns",
    "side_question": "Side Inquiries (/btw)",
    "prompt_enhancement": "Prompt Optimizer (/prompt)",
    "memory_learn": "Memory Extraction (/learn)",
    "doctor_ping": "Diagnostics Ping (/doctor)",
    "compaction": "Context Compaction",
}


def source_label(source: str) -> str:
    """Human label for a ledger source ('main', 'subagent_2', 'subagent:explorer', ...)."""
    s = source or "main"
    if s in SOURCE_LABELS:
        return SOURCE_LABELS[s]
    if s.startswith("subagent_"):
        return f"Subagent #{s.split('_', 1)[1]}"
    if s.startswith("subagent:"):
        return f"Subagent {s.split(':', 1)[1]}"
    return s.replace("_", " ").title()


@dataclass
class LedgerEntry:
    id: int
    timestamp: float
    tag: str              # source: "main", "subagent_1", "subagent:<name>", "side_question", ...
    model: str
    usage: Usage
    cost: Decimal
    group_ratio: float = 1.0
    priced: bool = True
    provider: str | None = None

    @property
    def source(self) -> str:
        return self.tag or "main"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "model": self.model,
            "usage": {
                "input": self.usage.input,
                "output": self.usage.output,
                "cache_read": self.usage.cache_read,
                "cache_write": self.usage.cache_write,
                "reasoning": self.usage.reasoning,
            },
            "cost": str(self.cost),
            "group_ratio": self.group_ratio,
            "priced": self.priced,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_id: int = 0) -> LedgerEntry:
        u = data.get("usage") or {}

        def num(key: str) -> int:
            try:
                return int(u.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        try:
            cost = Decimal(str(data.get("cost", "0")))
        except (InvalidOperation, ValueError):
            cost = _ZERO
        return cls(
            id=int(data.get("id", default_id) or default_id),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
            tag=str(data.get("source") or data.get("tag") or "main"),
            model=str(data.get("model", "")),
            usage=Usage(input=num("input"), output=num("output"), cache_read=num("cache_read"),
                        cache_write=num("cache_write"), reasoning=num("reasoning")),
            cost=cost,
            group_ratio=float(data.get("group_ratio", 1.0) or 1.0),
            priced=bool(data.get("priced", True)),
            provider=data.get("provider"),
        )


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.RLock()
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
        with self._lock:
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

    def record(
        self,
        model: str,
        usage: Usage,
        source: str | None = None,
        *,
        tag: str | None = None,
        group_ratio: float = 1.0,
        provider: str | None = None,
        cost: Decimal | None = None,
    ) -> Decimal:
        """Record one API call and return its cost. `source` (alias `tag`) names who made it:
        "main", "subagent:<name>" / "subagent_<n>", "side_question", ... Thread-safe."""
        src = source or tag or "main"
        if cost is None:
            res = cost_details(model, usage, provider=provider, group_ratio=group_ratio)
            call_cost, priced = res.cost, res.priced
        else:
            call_cost, priced = Decimal(str(cost)), True
        with self._lock:
            self.total_input_tokens += usage.input
            self.total_output_tokens += usage.output
            self.total_cache_read_tokens += usage.cache_read
            self.total_cache_write_tokens += usage.cache_write
            self.total_reasoning_tokens += usage.reasoning
            self.total_cost += call_cost
            self.turn_costs.append(call_cost)
            self.last_usage = usage
            self.entries.append(LedgerEntry(
                id=len(self.entries) + 1,
                timestamp=time.time(),
                tag=src,
                model=model,
                usage=usage,
                cost=call_cost,
                group_ratio=group_ratio,
                priced=priced,
                provider=provider,
            ))
        return call_cost

    def total(self) -> Decimal:
        return self.total_cost

    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def totals(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": len(self.entries),
                "input": self.total_input_tokens,
                "output": self.total_output_tokens,
                "cache_read": self.total_cache_read_tokens,
                "cache_write": self.total_cache_write_tokens,
                "reasoning": self.total_reasoning_tokens,
                "cost": self.total_cost,
                "unpriced_models": sorted({e.model for e in self.entries if not e.priced}),
            }

    def breakdown(self) -> list[dict[str, Any]]:
        """Per (source, model) aggregates, most expensive first.
        Keys: source, label, model, calls, input, output, cache_read, cache_write, reasoning, cost, priced."""
        with self._lock:
            entries = list(self.entries)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for e in entries:
            key = (e.source, e.model)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "source": e.source, "label": source_label(e.source), "model": e.model, "calls": 0,
                    "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0,
                    "cost": Decimal("0"), "priced": True,
                }
                order.append(key)
            g["calls"] += 1
            g["input"] += e.usage.input
            g["output"] += e.usage.output
            g["cache_read"] += e.usage.cache_read
            g["cache_write"] += e.usage.cache_write
            g["reasoning"] += e.usage.reasoning
            g["cost"] += e.cost
            g["priced"] = g["priced"] and e.priced
        rows = [groups[k] for k in order]
        rows.sort(key=lambda r: r["cost"], reverse=True)
        return rows

    def tag_breakdown(self) -> dict[str, dict[str, Any]]:
        """Aggregates grouped by source (legacy shape used by /cost and tests)."""
        with self._lock:
            entries = list(self.entries)
        groups: dict[str, dict[str, Any]] = {}
        for e in entries:
            g = groups.setdefault(e.source, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "reasoning_tokens": 0, "tokens": 0, "cost": Decimal("0.0"), "models": set(),
            })
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

    def uncached_counterfactual(self, model: str | None = None) -> Decimal:
        """What the session would have cost without prompt caching (each call at its own model's
        rates; tokens aggregated without per-call entries are priced at `model`)."""
        with self._lock:
            entries = list(self.entries)
            tot_in, tot_out = self.total_input_tokens, self.total_output_tokens
        total = _ZERO
        seen_in = seen_out = 0
        for e in entries:
            total += uncached_cost(e.model, e.usage, provider=e.provider) * Decimal(str(e.group_ratio))
            seen_in += e.usage.input
            seen_out += e.usage.output
        rest_in, rest_out = max(0, tot_in - seen_in), max(0, tot_out - seen_out)
        if (rest_in or rest_out) and model:
            total += uncached_cost(model, Usage(input=rest_in, output=rest_out))
        return total

    def savings_pct(self, model: str | None = None) -> float:
        """Percentage saved by caching versus the uncached counterfactual (0.0 when nothing to compare)."""
        cf = self.uncached_counterfactual(model)
        if cf <= 0:
            return 0.0
        return max(0.0, float((cf - self.total_cost) / cf * 100))

    # ── persistence ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "entries": [e.to_dict() for e in self.entries],
                "totals": {
                    "input": self.total_input_tokens,
                    "output": self.total_output_tokens,
                    "cache_read": self.total_cache_read_tokens,
                    "cache_write": self.total_cache_write_tokens,
                    "reasoning": self.total_reasoning_tokens,
                    "cost": str(self.total_cost),
                },
                "chat_count": self.chat_count,
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Ledger:
        """Rebuild from to_dict() output; tolerant of missing / legacy fields."""
        ledger = cls()
        if not isinstance(data, dict):
            return ledger
        for i, raw in enumerate(data.get("entries") or [], start=1):
            if isinstance(raw, dict):
                ledger._append_entry(LedgerEntry.from_dict(raw, default_id=i))
        totals = data.get("totals")
        if isinstance(totals, dict) and not ledger.entries:
            ledger.total_input_tokens = int(totals.get("input", 0) or 0)
            ledger.total_output_tokens = int(totals.get("output", 0) or 0)
            ledger.total_cache_read_tokens = int(totals.get("cache_read", 0) or 0)
            ledger.total_cache_write_tokens = int(totals.get("cache_write", 0) or 0)
            ledger.total_reasoning_tokens = int(totals.get("reasoning", 0) or 0)
            try:
                ledger.total_cost = Decimal(str(totals.get("cost", "0")))
            except (InvalidOperation, ValueError):
                pass
        ledger.chat_count = int(data.get("chat_count", 0) or 0)
        return ledger

    def _append_entry(self, entry: LedgerEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            self.total_input_tokens += entry.usage.input
            self.total_output_tokens += entry.usage.output
            self.total_cache_read_tokens += entry.usage.cache_read
            self.total_cache_write_tokens += entry.usage.cache_write
            self.total_reasoning_tokens += entry.usage.reasoning
            self.total_cost += entry.cost
            self.turn_costs.append(entry.cost)
            self.last_usage = entry.usage

    def merge(self, other: Ledger, source: str | None = None) -> None:
        """Append another ledger's entries (e.g. a subagent's), optionally re-tagging their source."""
        for e in list(other.entries):
            self._append_entry(LedgerEntry(
                id=len(self.entries) + 1, timestamp=e.timestamp, tag=source or e.tag, model=e.model,
                usage=e.usage, cost=e.cost, group_ratio=e.group_ratio, priced=e.priced, provider=e.provider,
            ))

    # ── rendering ───────────────────────────────────────────────────────
    def render(self, model: str | None = None) -> str:
        tot_tok = self.total_tokens()
        has_subagents = any(e.source.startswith("subagent") for e in self.entries)
        title = "=== Session Cost & Token Ledger (Combined Main + Subagents) ===" if has_subagents else "=== Cost & Token Ledger ==="
        lines = [
            title,
            f"Total Cost      : ${self.total_cost:.5f}",
            f"Total Tokens    : {tot_tok:,} (In: {self.total_input_tokens:,}, Out: {self.total_output_tokens:,})",
        ]
        if self.total_cache_read_tokens > 0 or self.total_cache_write_tokens > 0:
            hit_pct = (self.total_cache_read_tokens / max(1, self.total_input_tokens)) * 100
            lines.append(f"Cache Tokens    : {self.total_cache_read_tokens:,} read · {self.total_cache_write_tokens:,} written ({hit_pct:.1f}% hit rate)")
            sav = self.savings_pct(model)
            if sav > 0:
                cf = self.uncached_counterfactual(model)
                lines.append(f"Cache Savings   : ${cf - self.total_cost:.5f} ({sav:.1f}% saved vs uncached)")
        lines.append(f"API Calls Tracked: {len(self.entries)}")
        unpriced = sorted({e.model for e in self.entries if not e.priced})
        if unpriced:
            lines.append(f"Unpriced models : {', '.join(unpriced)} (no published price; counted as $0)")
        elif model and not self.entries and not price_for(model).priced:
            lines.append(f"Unpriced model  : {model} (no published price; counted as $0)")

        groups = self.tag_breakdown()
        if groups:
            lines.append("\n--- API Usage Breakdown by Source ---")
            for src, data in sorted(groups.items(), key=lambda x: x[1]["cost"], reverse=True):
                lines.append(
                    f"  • {source_label(src):<26}: {data['calls']} call{'s' if data['calls'] != 1 else ''} · "
                    f"{data['tokens']:,} tokens · ${data['cost']:.5f}"
                )
        return "\n".join(lines)

    def render_call_history(self) -> str:
        """Itemized log of every individual API call tracked."""
        if not self.entries:
            return "No individual API calls recorded."
        lines = ["=== Itemized API Call Log ==="]
        for e in list(self.entries):
            t_str = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
            tot = e.usage.input + e.usage.output
            c_info = f" (cache read: {e.usage.cache_read:,})" if e.usage.cache_read > 0 else ""
            price_note = "" if e.priced else " (unpriced)"
            lines.append(
                f"  #{e.id:02d} [{t_str}] {e.source:<18} ({e.model}): "
                f"{tot:,} tokens (in: {e.usage.input:,}{c_info} · out: {e.usage.output:,}) · ${e.cost:.5f}{price_note}"
            )
        return "\n".join(lines)


__all__ = ["Ledger", "LedgerEntry", "source_label", "SOURCE_LABELS"]
