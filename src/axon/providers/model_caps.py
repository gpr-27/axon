"""What each model can do, from the provider's own /models list when it publishes capabilities.

Sources, best first:
  1. the Anthropic Models API ``capabilities`` object (thinking types, effort levels, structured outputs, PDF and
     image input, context management, ...), recorded while ``discover_models`` reads the live list;
  2. OpenRouter-style ``supported_parameters`` (``reasoning`` means the model takes an effort);
  3. a conservative built-in table keyed by model family (the old hard-coded tuples for Claude, the known
     reasoning families of other providers);
  4. nothing: for an unknown model no risky option is offered (``effort_levels`` is empty, ``allows`` is None).

``allows(model, feature)`` answers True / False when something is known and None when it is not, so callers keep
their old behaviour for None.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
# Axon's four effort names <-> API levels.
AXON_TO_LEVEL = {"reflex": "low", "balanced": "medium", "synapse": "high", "quantum": "xhigh"}
LEVEL_TO_AXON = {"low": "reflex", "medium": "balanced", "high": "synapse", "xhigh": "quantum", "max": "quantum"}


@dataclass(frozen=True)
class ModelCaps:
    id: str
    source: str = "live"                       # "live" | "table" | "params"
    line: str | None = None
    display_name: str = ""
    lifecycle: str = "active"
    deprecated_at: str | None = None
    retires_at: str | None = None
    max_input_tokens: int | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    thinking_types: dict[str, bool] = field(default_factory=dict)   # adaptive / enabled / disabled / between_tools
    effort: bool | None = None
    effort_levels: tuple[str, ...] = ()
    features: dict[str, bool] = field(default_factory=dict)         # structured_outputs, pdf_input, image_input, ...
    context_strategies: tuple[str, ...] = ()


_LOCK = threading.Lock()
_CAPS: dict[str, ModelCaps] = {}


def _bare(model: str) -> str:
    m = (model or "").lower().rsplit("/", 1)[-1]
    if m.endswith("[1m]"):
        m = m[:-4]
    return re.sub(r"(?<=\d)\.(?=\d)", "-", m)


def _sup(node: Any) -> bool | None:
    if isinstance(node, dict) and isinstance(node.get("supported"), bool):
        return node["supported"]
    return None


def parse_model_info(item: Any) -> ModelCaps | None:
    """One ``/v1/models`` entry -> ModelCaps, or None when it carries nothing usable."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    mid = str(item["id"])
    life = item.get("lifecycle") if item.get("lifecycle") in ("active", "deprecated", "retired") else "active"
    common = dict(line=item.get("line") if isinstance(item.get("line"), str) else None,
                  display_name=str(item.get("display_name") or ""), lifecycle=life,
                  deprecated_at=item.get("deprecated_at") or None, retires_at=item.get("retires_at") or None,
                  max_input_tokens=item.get("max_input_tokens") if isinstance(item.get("max_input_tokens"), int) else None,
                  max_tokens=item.get("max_tokens") if isinstance(item.get("max_tokens"), int) else None)
    caps = item.get("capabilities")
    if isinstance(caps, dict):
        th = caps.get("thinking") if isinstance(caps.get("thinking"), dict) else {}
        types = {k: bool(v) for k, v in ((k, _sup(n)) for k, n in (th.get("types") or {}).items()) if v is not None}
        ef = caps.get("effort") if isinstance(caps.get("effort"), dict) else {}
        levels = tuple(lv for lv in EFFORT_ORDER if _sup(ef.get(lv)) is True)
        feats: dict[str, bool] = {}
        for key in ("batch", "citations", "code_execution", "image_input", "pdf_input", "structured_outputs"):
            v = _sup(caps.get(key))
            if v is not None:
                feats[key] = v
        cm = caps.get("context_management") if isinstance(caps.get("context_management"), dict) else {}
        if _sup(cm) is not None:
            feats["context_management"] = bool(_sup(cm))
        strategies = tuple(k for k, n in cm.items() if k != "supported" and _sup(n) is True)
        st = caps.get("server_tools") if isinstance(caps.get("server_tools"), dict) else {}
        for key in ("web_search", "code_execution"):
            v = _sup(st.get(key))
            if v is not None:
                feats["server_" + key] = v
        return ModelCaps(id=mid, source="live", thinking=_sup(th), thinking_types=types, effort=_sup(ef),
                         effort_levels=levels, features=feats, context_strategies=strategies, **common)
    params = item.get("supported_parameters")
    if isinstance(params, list):
        names = {str(p) for p in params}
        has = bool(names & {"reasoning", "reasoning_effort", "include_reasoning"})
        feats = {"structured_outputs": "structured_outputs" in names or "response_format" in names}
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        mods = arch.get("input_modalities")
        if isinstance(mods, list) and mods:
            feats["image_input"] = "image" in mods          # OpenRouter-style lists publish what a model accepts
        return ModelCaps(id=mid, source="params", thinking=has, effort=has,
                         effort_levels=("low", "medium", "high") if has else (),
                         features=feats, **common)
    if common["lifecycle"] != "active" or common["retires_at"]:
        return ModelCaps(id=mid, source="params", **common)
    return None


def record(items: Any) -> int:
    """Remember the capabilities in a provider's /models list (called by the live model discovery)."""
    n = 0
    if not isinstance(items, list):
        return 0
    with _LOCK:
        for it in items:
            c = parse_model_info(it)
            if c is not None:
                _CAPS[_bare(c.id)] = c
                n += 1
    return n


def clear() -> None:
    with _LOCK:
        _CAPS.clear()


def live(model: str) -> ModelCaps | None:
    with _LOCK:
        return _CAPS.get(_bare(model))


# ─── built-in fallback table ──────────────────────────────────────────────
def _claude_table(model: str) -> ModelCaps | None:
    m = _bare(model)
    if not m.startswith("claude"):
        return None
    from axon.providers import anthropic as a
    mode = a.thinking_mode(model)
    if a._matches(model, a._EFFORT_XHIGH) or mode == "always_on":
        levels: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
    elif a._matches(model, a._EFFORT_BASIC):
        levels = ("low", "medium", "high", "max") if a._matches(model, a._EFFORT_MAX) else ("low", "medium", "high")
    else:
        levels = ()
    types = {"adaptive": mode in ("always_on", "adaptive"), "enabled": mode == "budget",
             "disabled": mode not in ("always_on",), "between_tools": m.startswith("claude-sonnet-5-5")}
    if m.startswith("claude-sonnet-5-5"):
        types["disabled"] = False
        types["between_tools"] = True
    return ModelCaps(id=model, source="table", thinking=mode != "none", thinking_types=types, effort=bool(levels),
                     effort_levels=levels)


_OPENAI_REASONING = re.compile(r"^(o\d|gpt-5|gpt-6|grok-3-mini|grok-4|deepseek-r1|deepseek-reasoner|qwq|gpt-oss)")


def _family_table(model: str) -> ModelCaps | None:
    m = _bare(model)
    if m.startswith(("gpt-oss",)):
        return ModelCaps(id=model, source="table", thinking=True, effort=True, effort_levels=("low", "medium", "high"))
    if m.startswith("grok-3-mini"):
        return ModelCaps(id=model, source="table", thinking=True, effort=True, effort_levels=("low", "high"))
    if _OPENAI_REASONING.match(m) and not m.startswith(("deepseek", "qwq")):
        return ModelCaps(id=model, source="table", thinking=True, effort=True, effort_levels=("low", "medium", "high"))
    return None   # unknown family: offer nothing risky


def lookup(model: str) -> ModelCaps | None:
    """Live capabilities when the provider published them, else the built-in table, else None."""
    return live(model) or _claude_table(model) or _family_table(model)


def allows(model: str, feature: str) -> bool | None:
    """True / False for a feature the models list (or table) knows about, None when unknown."""
    c = live(model)
    if c is not None and feature in c.features:
        return c.features[feature]
    return None


def effort_levels(model: str) -> tuple[str, ...]:
    """API effort levels the model takes; () when it has none or when nothing is known."""
    c = lookup(model)
    return tuple(c.effort_levels) if c is not None and c.effort is not False else ()


def offered_efforts(model: str, all_names: tuple[str, ...] = ("reflex", "balanced", "synapse", "quantum")) -> list[str]:
    """Axon effort names worth offering for *model*. Unknown models keep the full list (Axon maps them itself);
    a model known to have no effort gets just the highest (a no-op choice is not offered)."""
    c = lookup(model)
    if c is None:
        return list(all_names)
    levels = set(effort_levels(model))
    if not levels:
        return [all_names[-1]] if c.source != "table" or c.effort is False else list(all_names)
    out = []
    for name in all_names:
        lv = AXON_TO_LEVEL[name]
        if lv in levels or (lv == "xhigh" and "max" in levels):
            out.append(name)
    return out or list(all_names)


def pick_level(model: str, level: str | None) -> str | None:
    """The API level to send for Axon's canonical *level* (low/medium/high/xhigh), or None to send no effort."""
    if not level:
        return None
    have = effort_levels(model)
    if not have:
        return None
    if level in have:
        return level
    if level == "xhigh" and "max" in have:
        return "max"
    want = EFFORT_ORDER.index(level)
    lower = [lv for lv in have if EFFORT_ORDER.index(lv) <= want]
    return lower[-1] if lower else have[0]


# ─── Anthropic thinking / effort request fields from capabilities ─────────
def build_thinking_params(caps: ModelCaps, *, model: str, thinking: bool, level: str | None, max_tokens: int,
                          budgets: dict[str, int]) -> dict[str, Any]:
    """Request fields (thinking / output_config) the model accepts, per its published thinking types.

    Never sends a thinking type the model rejects. With thinking off: ``disabled`` where accepted, else
    ``between_tools`` (Sonnet 5.5), else nothing; those modes accept effort ``high`` or below only, so the level
    is lowered to high rather than risking a 400."""
    out: dict[str, Any] = {}
    t = caps.thinking_types
    send = level
    if thinking:
        if t.get("adaptive"):
            out["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif t.get("enabled") and max_tokens > 2_048:
            budget = min(budgets.get(level or "high", 16_000), max_tokens - 1_024)
            if budget >= 1_024:
                out["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif caps.thinking is not False and t:
        off = "disabled" if t.get("disabled") and not t.get("between_tools") else "between_tools" if t.get("between_tools") else ""
        if off:
            out["thinking"] = {"type": off}
            if send in ("xhigh", "max"):
                send = "high"
    api = pick_level(model, send)
    if api:
        out["output_config"] = {"effort": api}
    return out


def thinking_type_sent(extra: dict[str, Any] | None) -> str:
    return str(((extra or {}).get("thinking") or {}).get("type") or "")


# ─── lifecycle ────────────────────────────────────────────────────────────
def _day(ts: str | None) -> str:
    return (ts or "")[:10]


def lifecycle_note(model: str) -> str:
    """'' for an active model; 'deprecated · retires 2027-01-15' / 'retired' otherwise."""
    c = live(model)
    if c is None or c.lifecycle == "active":
        return f"retires {_day(c.retires_at)}" if c is not None and c.retires_at else ""
    if c.lifecycle == "retired":
        return "retired" + (f" {_day(c.retires_at)}" if c.retires_at else "")
    return "deprecated" + (f" · retires {_day(c.retires_at)}" if c.retires_at else (f" · since {_day(c.deprecated_at)}" if c.deprecated_at else ""))


def deprecation_warning(model: str) -> str:
    c = live(model)
    if c is None or c.lifecycle == "active":
        return ""
    when = f" on {_day(c.retires_at)}" if c.retires_at else ""
    if c.lifecycle == "retired":
        return f"{model} is retired: requests naming it fail. Pick another model."
    return f"{model} is deprecated and retires{when or ' soon'}; it is closed to new adopters. Plan a move to a newer model."
