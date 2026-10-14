"""
Per-million-token USD pricing for real provider models, with family/prefix matching.

`cost_for(model, usage)` is the single cost formula used by the ledger, the session store and the UI:

    cost = uncached_input * input + cache_read * cache_read_rate + cache_write * cache_write_rate
           + output * output            (all per 1,000,000 tokens)

`usage.input` is the TOTAL prompt size (uncached + cache_read + cache_write), the convention every
provider in this package normalises to. Local models (Ollama, LM Studio, localhost servers) cost 0.
Unknown models cost 0 and are reported with `priced=False` instead of being silently guessed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

_MILLION = Decimal("1000000")

# ─── AgentRouter (default provider) verified rates ─────────────────────────
# These come from AgentRouter's live deduction log / console table and are what the gateway
# actually bills. The /api/pricing ratio feed is only used to fill in models missing here.
AGENTROUTER_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 4.00, "output": 12.00, "cache_read": 0.80, "cache_write": 0.0},
    "gpt-5.6-sol":       {"input": 3.00, "output": 15.00, "cache_read": 0.60},
    "gpt-6-astra":       {"input": 3.00, "output": 15.00, "cache_read": 0.60},
    "claude-opus-5":     {"input": 6.00, "output": 30.00, "cache_read": 1.20, "cache_write": 7.50},
    "claude-opus-4-8":   {"input": 8.00, "output": 40.00, "cache_read": 1.60, "cache_write": 10.00},
}


def _claude(inp: float, out: float, read: float | None = None) -> dict[str, float]:
    # Anthropic: 5-minute cache writes bill at 1.25x input, cache reads at 0.1x unless stated.
    return {"input": inp, "output": out, "cache_read": read if read is not None else round(inp * 0.1, 4),
            "cache_write": round(inp * 1.25, 4)}


def _oai(inp: float, out: float, read: float | None = None) -> dict[str, float]:
    d = {"input": inp, "output": out, "cache_write": 0.0}
    if read is not None:
        d["cache_read"] = read
    return d


# Official first-party list prices, keyed by provider preset id. Keys are model ids or family
# prefixes; dated ids ("claude-sonnet-4-5-20250929") resolve through prefix matching.
PROVIDER_PRICING: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        "claude-fable-5-1": _claude(10.0, 50.0, 0.25),
        "claude-mythos-5-1": _claude(10.0, 50.0, 0.25),
        "claude-fable-5": _claude(10.0, 50.0, 1.00),
        "claude-mythos-5": _claude(10.0, 50.0, 1.00),
        "claude-opus-5-5": _claude(4.0, 20.0, 0.20),
        "claude-opus-5": _claude(5.0, 25.0),
        "claude-opus-4-8": _claude(5.0, 25.0),
        "claude-opus-4-7": _claude(5.0, 25.0),
        "claude-opus-4-6": _claude(5.0, 25.0),
        "claude-opus-4-5": _claude(5.0, 25.0),
        "claude-opus-4-1": _claude(15.0, 75.0),
        "claude-opus-4": _claude(15.0, 75.0),
        "claude-sonnet-5-5": _claude(2.0, 10.0, 0.20),
        "claude-sonnet-5": _claude(2.0, 10.0),
        "claude-sonnet-4-6": _claude(3.0, 15.0),
        "claude-sonnet-4-5": _claude(3.0, 15.0),
        "claude-sonnet-4": _claude(3.0, 15.0),
        "claude-haiku-5-5": _claude(0.10, 0.50, 0.01),
        "claude-haiku-4-5": _claude(1.0, 5.0),
        "claude-3-7-sonnet": _claude(3.0, 15.0),
        "claude-3-5-sonnet": _claude(3.0, 15.0),
        "claude-3-5-haiku": _claude(0.80, 4.0),
        "claude-3-opus": _claude(15.0, 75.0),
        "claude-3-haiku": _claude(0.25, 1.25, 0.03),
    },
    "openai": {
        "gpt-5": _oai(1.25, 10.0, 0.125),
        "gpt-5-mini": _oai(0.25, 2.0, 0.025),
        "gpt-5-nano": _oai(0.05, 0.40, 0.005),
        "gpt-5-codex": _oai(1.25, 10.0, 0.125),
        "gpt-4.1": _oai(2.0, 8.0, 0.50),
        "gpt-4.1-mini": _oai(0.40, 1.60, 0.10),
        "gpt-4.1-nano": _oai(0.10, 0.40, 0.025),
        "gpt-4o": _oai(2.50, 10.0, 1.25),
        "gpt-4o-mini": _oai(0.15, 0.60, 0.075),
        "o1": _oai(15.0, 60.0, 7.50),
        "o1-mini": _oai(1.10, 4.40, 0.55),
        "o3": _oai(2.0, 8.0, 0.50),
        "o3-mini": _oai(1.10, 4.40, 0.55),
        "o3-pro": _oai(20.0, 80.0),
        "o4-mini": _oai(1.10, 4.40, 0.275),
    },
    "gemini": {
        "gemini-2.5-pro": _oai(1.25, 10.0, 0.31),
        "gemini-2.5-flash": _oai(0.30, 2.50, 0.075),
        "gemini-2.5-flash-lite": _oai(0.10, 0.40, 0.025),
        "gemini-2.0-flash": _oai(0.10, 0.40, 0.025),
        "gemini-2.0-flash-lite": _oai(0.075, 0.30),
        "gemini-1.5-pro": _oai(1.25, 5.0, 0.3125),
        "gemini-1.5-flash": _oai(0.075, 0.30, 0.01875),
    },
    "deepseek": {
        "deepseek-chat": _oai(0.27, 1.10, 0.07),
        "deepseek-reasoner": _oai(0.55, 2.19, 0.14),
    },
    "xai": {
        "grok-4": _oai(3.0, 15.0, 0.75),
        "grok-4-fast": _oai(0.20, 0.50, 0.05),
        "grok-code-fast-1": _oai(0.20, 1.50, 0.02),
        "grok-3": _oai(3.0, 15.0, 0.75),
        "grok-3-mini": _oai(0.30, 0.50, 0.075),
    },
    "groq": {
        "llama-3.3-70b-versatile": _oai(0.59, 0.79),
        "llama-3.1-8b-instant": _oai(0.05, 0.08),
        "openai/gpt-oss-120b": _oai(0.15, 0.75),
        "openai/gpt-oss-20b": _oai(0.10, 0.50),
        "moonshotai/kimi-k2-instruct": _oai(1.0, 3.0),
        "qwen/qwen3-32b": _oai(0.29, 0.59),
        "mixtral-8x7b-32768": _oai(0.24, 0.24),
        "gemma2-9b-it": _oai(0.20, 0.20),
    },
    "mistral": {
        "mistral-large": _oai(2.0, 6.0),
        "mistral-medium": _oai(0.40, 2.0),
        "mistral-small": _oai(0.10, 0.30),
        "codestral": _oai(0.30, 0.90),
        "devstral-medium": _oai(0.40, 2.0),
        "devstral-small": _oai(0.10, 0.30),
        "open-mixtral-8x22b": _oai(2.0, 6.0),
        "open-mixtral-8x7b": _oai(0.70, 0.70),
    },
    "together": {
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": _oai(0.88, 0.88),
        "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": _oai(0.18, 0.18),
        "deepseek-ai/DeepSeek-V3": _oai(1.25, 1.25),
        "deepseek-ai/DeepSeek-R1": _oai(3.0, 7.0),
        "Qwen/Qwen2.5-Coder-32B-Instruct": _oai(0.80, 0.80),
        "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8": _oai(2.0, 2.0),
        "mistralai/Mixtral-8x7B-Instruct-v0.1": _oai(0.60, 0.60),
    },
    "fireworks": {
        "accounts/fireworks/models/llama-v3p3-70b-instruct": _oai(0.90, 0.90),
        "accounts/fireworks/models/deepseek-v3": _oai(0.90, 0.90),
        "accounts/fireworks/models/deepseek-r1": _oai(3.0, 8.0),
        "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct": _oai(0.45, 1.80),
        "accounts/fireworks/models/mixtral-8x22b-instruct": _oai(1.20, 1.20),
    },
    "cerebras": {
        "llama-3.3-70b": _oai(0.85, 1.20),
        "llama3.1-8b": _oai(0.10, 0.10),
        "qwen-3-coder-480b": _oai(2.0, 2.0),
        "qwen-3-32b": _oai(0.40, 0.80),
        "gpt-oss-120b": _oai(0.35, 0.75),
    },
}

# Gateways that resell first-party models under the vendor's own ids at list price.
_RESELLER_FALLBACK = ("anthropic", "openai", "gemini", "deepseek", "xai", "mistral")

# Flat legacy view (model -> rates) kept importable from axon.providers.registry as PRICING.
# AgentRouter rates win for the ids it serves (it is the default provider).
PRICING: dict[str, dict[str, float]] = {}
for _table in PROVIDER_PRICING.values():
    for _k, _v in _table.items():
        PRICING.setdefault(_k, dict(_v))
for _k, _v in AGENTROUTER_PRICING.items():
    PRICING[_k] = dict(_v)
PRICING.setdefault("claude-3-7-sonnet-20250219", dict(PROVIDER_PRICING["anthropic"]["claude-3-7-sonnet"]))
PRICING.setdefault("claude-3-5-sonnet-20241022", dict(PROVIDER_PRICING["anthropic"]["claude-3-5-sonnet"]))

LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "local"})

# model -> provider id, recorded by provider_for() so the ledger prices calls by where they went.
_ROUTES: dict[str, str] = {}
_LIVE_SYNC_LOCK = threading.Lock()
_LIVE: dict[str, dict[str, float]] = {}
_AGENTROUTER_SYNCED = False


@dataclass(frozen=True)
class PriceQuote:
    model: str
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    priced: bool
    local: bool = False
    source: str = ""  # provider id / "agentrouter-live" / "local" / ""
    cache_write_1h: Decimal = Decimal("0")  # 1-hour TTL cache writes (Anthropic: 2x input)


@dataclass(frozen=True)
class CostResult:
    cost: Decimal
    priced: bool
    quote: PriceQuote
    input_cost: Decimal = Decimal("0")
    cache_write_cost: Decimal = Decimal("0")
    cache_read_cost: Decimal = Decimal("0")
    output_cost: Decimal = Decimal("0")

    def parts(self) -> dict[str, Decimal]:
        """Per-step $ split: input / cache_write / cache_read / output (+ total)."""
        return {"input": self.input_cost, "cache_write": self.cache_write_cost, "cache_read": self.cache_read_cost,
                "output": self.output_cost, "total": self.cost}


_ALIASES: dict[str, str] = {}


def note_alias(alias: str, resolved: str) -> None:
    """Remember that `alias` (e.g. "opus", "sonnet[1m]") currently resolves to `resolved`."""
    if alias and resolved and alias != resolved:
        _ALIASES[alias] = resolved


def note_route(model: str, provider_id: str | None) -> None:
    """Remember which provider serves `model` (called by provider_for)."""
    if model and provider_id:
        _ROUTES[model] = provider_id


def route_for(model: str) -> str | None:
    return _ROUTES.get(model)


def _strip_namespace(model: str) -> str:
    m = model.strip()
    for pre in ("models/",):
        if m.startswith(pre):
            m = m[len(pre):]
    return m.rsplit("/", 1)[-1] if "/" in m else m


def _normalize(model: str) -> str:
    m = _strip_namespace(model).lower()
    m = m.split("@", 1)[0]
    if m.startswith("claude"):
        # OpenRouter style "claude-3.7-sonnet" / "claude-sonnet-4.5" -> dash form
        m = re.sub(r"(?<=\d)\.(?=\d)", "-", m)
    return m


_BOUNDARY = set("-.:_@/")


def _prefix_lookup(table: Mapping[str, dict[str, float]], model: str) -> tuple[str, dict[str, float]] | None:
    if model in table:
        return model, table[model]
    low = model.lower()
    lowered = {k.lower(): k for k in table}
    if low in lowered:
        k = lowered[low]
        return k, table[k]
    best: str | None = None
    for lk, k in lowered.items():
        if low.startswith(lk) and len(low) > len(lk) and low[len(lk)] in _BOUNDARY:
            if best is None or len(lk) > len(best.lower()):
                best = k
    if best is not None:
        return best, table[best]
    return None


def _looks_local(model: str) -> bool:
    m = model.lower()
    return m.startswith(("ollama/", "lmstudio/", "local/")) or (":" in m and "/" not in m.split(":", 1)[0] and not m.startswith("http"))


def _quote(model: str, rates: dict[str, float], source: str, local: bool = False) -> PriceQuote:
    inp = Decimal(str(rates.get("input", 0.0)))
    cr = rates.get("cache_read")
    cw = rates.get("cache_write")
    cw_dec = Decimal(str(cw)) if cw is not None else inp
    cw1h = rates.get("cache_write_1h")
    if cw1h is not None:
        cw1h_dec = Decimal(str(cw1h))
    elif source == "anthropic" or _normalize(model).startswith("claude"):
        cw1h_dec = inp * 2
    else:
        cw1h_dec = cw_dec
    return PriceQuote(
        model=model,
        input=inp,
        output=Decimal(str(rates.get("output", 0.0))),
        cache_read=Decimal(str(cr)) if cr is not None else inp,
        cache_write=cw_dec,
        priced=True,
        local=local,
        source=source,
        cache_write_1h=cw1h_dec,
    )


def _zero(model: str, *, priced: bool, local: bool, source: str) -> PriceQuote:
    z = Decimal("0")
    return PriceQuote(model=model, input=z, output=z, cache_read=z, cache_write=z, priced=priced, local=local, source=source)


def price_for(model: str, provider: str | None = None) -> PriceQuote:
    """Resolve rates for `model`, optionally served by `provider` (preset id).

    Missing cache_read / cache_write rates fall back to the plain input rate (no discount assumed).
    """
    model = (model or "").strip()
    provider = provider or _ROUTES.get(model)
    if model in _ALIASES:
        model = _ALIASES[model]
        provider = provider or _ROUTES.get(model)
    else:
        from axon.providers.catalog import is_model_alias, resolve_model_alias
        if is_model_alias(model):
            model = resolve_model_alias(model)
    if model.lower().endswith("[1m]"):
        model = model[:-4]
    if provider in LOCAL_PROVIDERS:
        return _zero(model, priced=True, local=True, source="local")
    if not model:
        return _zero(model, priced=False, local=False, source="")

    tables: list[tuple[str, Mapping[str, dict[str, float]]]] = []
    if provider == "agentrouter" or provider is None:
        tables.append(("agentrouter", AGENTROUTER_PRICING))
        tables.append(("agentrouter-live", _LIVE))
    if provider and provider in PROVIDER_PRICING:
        tables.append((provider, PROVIDER_PRICING[provider]))
    for pid in _RESELLER_FALLBACK:
        if pid != provider:
            tables.append((pid, PROVIDER_PRICING[pid]))
    for pid, t in PROVIDER_PRICING.items():
        if pid != provider and pid not in _RESELLER_FALLBACK:
            tables.append((pid, t))
    # Legacy flat table (also receives user overrides written into PRICING at runtime).
    tables.append(("pricing", PRICING))

    for candidate in (model, _normalize(model)):
        for source, table in tables:
            hit = _prefix_lookup(table, candidate)
            if hit:
                return _quote(model, hit[1], source)

    if provider is None and _looks_local(model):
        return _zero(model, priced=True, local=True, source="local")
    return _zero(model, priced=False, local=False, source="")


def _usage_parts(usage: Any) -> tuple[int, int, int, int, int]:
    """(total_input, output, cache_read, cache_write, cache_write_1h) from a Usage or a dict."""
    if usage is None:
        return 0, 0, 0, 0, 0
    if isinstance(usage, Mapping):
        get = usage.get
        vals = (get("input", get("input_tokens", 0)), get("output", get("output_tokens", 0)),
                get("cache_read", get("cache_read_tokens", 0)), get("cache_write", get("cache_write_tokens", 0)),
                get("cache_write_1h", 0))
    else:
        vals = (getattr(usage, "input", 0), getattr(usage, "output", 0),
                getattr(usage, "cache_read", 0), getattr(usage, "cache_write", 0), getattr(usage, "cache_write_1h", 0))
    out = []
    for v in vals:
        try:
            out.append(max(0, int(v or 0)))
        except (TypeError, ValueError):
            out.append(0)
    out[4] = min(out[4], out[3])
    return out[0], out[1], out[2], out[3], out[4]


def cost_details(model: str, usage: Any, *, provider: str | None = None, group_ratio: float | Decimal = 1) -> CostResult:
    """Cost of one call plus whether the model was priced. `usage` is a Usage or a dict."""
    q = price_for(model, provider)
    total_in, out, cache_read, cache_write, write_1h = _usage_parts(usage)
    uncached = max(0, total_in - cache_read - cache_write)
    c_in = (Decimal(uncached) / _MILLION) * q.input
    c_read = (Decimal(cache_read) / _MILLION) * q.cache_read
    c_write = (Decimal(cache_write - write_1h) / _MILLION) * q.cache_write
    if write_1h:
        c_write += (Decimal(write_1h) / _MILLION) * q.cache_write_1h
    c_out = (Decimal(out) / _MILLION) * q.output
    ratio = Decimal(str(group_ratio))
    if ratio != 1:
        c_in, c_read, c_write, c_out = c_in * ratio, c_read * ratio, c_write * ratio, c_out * ratio
    cost = c_in + c_read + c_write + c_out
    return CostResult(cost=cost, priced=q.priced, quote=q, input_cost=c_in, cache_write_cost=c_write,
                      cache_read_cost=c_read, output_cost=c_out)


def cost_for(model: str, usage: Any, *, provider: str | None = None) -> Decimal:
    """USD cost (Decimal) of one call. Unknown models cost 0 (see cost_details().priced)."""
    return cost_details(model, usage, provider=provider).cost


def uncached_cost(model: str, usage: Any, *, provider: str | None = None) -> Decimal:
    """What the call would have cost with no prompt caching (all input at the full rate)."""
    q = price_for(model, provider)
    total_in, out, _, _, _ = _usage_parts(usage)
    return (Decimal(total_in) / _MILLION) * q.input + (Decimal(out) / _MILLION) * q.output


def cost_breakdown(model: str, usage: Any, *, provider: str | None = None) -> dict[str, Decimal]:
    """Per-request $ split (input / cache_write / cache_read / output / total)."""
    return cost_details(model, usage, provider=provider).parts()


def is_priced(model: str, provider: str | None = None) -> bool:
    return price_for(model, provider).priced


# ─── AgentRouter live pricing feed ─────────────────────────────────────────
def _network_disabled() -> bool:
    if os.environ.get("AXON_OFFLINE") or os.environ.get("AXON_NO_PRICING_SYNC"):
        return True
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def apply_agentrouter_pricing_feed(data: Any) -> int:
    """Merge an AgentRouter /api/pricing payload. Verified rates are never overwritten.

    new-api convention: model_ratio 1.0 == $2 / 1M input tokens; output = input * completion_ratio.
    Returns the number of models added.
    """
    items = data.get("data", []) if isinstance(data, dict) else []
    added = 0
    with _LIVE_SYNC_LOCK:
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("model_name")
            if not name or name in AGENTROUTER_PRICING:
                continue
            try:
                m_ratio = float(item.get("model_ratio", 1.0) or 0.0)
                c_ratio = float(item.get("completion_ratio", 1.0) or 1.0)
            except (TypeError, ValueError):
                continue
            in_p = m_ratio * 2.0
            cache_ratio = item.get("cache_ratio")
            try:
                cr = in_p * float(cache_ratio) if cache_ratio is not None else in_p * 0.2
            except (TypeError, ValueError):
                cr = in_p * 0.2
            _LIVE[name] = {
                "input": round(in_p, 4),
                "output": round(in_p * c_ratio, 4),
                "cache_read": round(cr, 4),
                "cache_write": 0.0 if "deepseek" in name.lower() else round(in_p * 1.25, 4),
            }
            added += 1
    return added


def sync_agentrouter_pricing(base_url: str = "https://agentrouter.org", timeout: float = 2.0) -> bool:
    """Fetch AgentRouter's /api/pricing to price models missing from the verified table.

    Skipped under pytest / AXON_OFFLINE so tests never touch the network.
    """
    global _AGENTROUTER_SYNCED
    if _network_disabled():
        return False
    from axon.providers.transport import is_agentrouter
    if not is_agentrouter(base_url):
        return False
    try:
        import urllib.request
        clean = base_url.rstrip("/").removesuffix("/v1")
        req = urllib.request.Request(f"{clean}/api/pricing", headers={"User-Agent": "axon-pricing"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        apply_agentrouter_pricing_feed(data)
        _AGENTROUTER_SYNCED = True
        return True
    except Exception:
        return False


def sync_agentrouter_pricing_async(base_url: str) -> None:
    """Fire-and-forget background refresh so provider construction never blocks on the network."""
    global _AGENTROUTER_SYNCED
    if _AGENTROUTER_SYNCED or _network_disabled():
        return
    _AGENTROUTER_SYNCED = True  # one attempt per process
    t = threading.Thread(target=sync_agentrouter_pricing, args=(base_url,), name="axon-pricing-sync", daemon=True)
    t.start()


def is_synced() -> bool:
    return _AGENTROUTER_SYNCED
