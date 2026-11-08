"""
Turn timeline: every step of a turn with its duration, tokens, cost and cache-miss cause.
``turn_timeline`` returns data from a saved trace (only what was recorded; the rest is None);
``render_timeline`` draws it with axon.ui.charts. The web app can use ``turn_timeline`` alone.
"""
from __future__ import annotations

from typing import Any


def _target(inp: Any) -> str:
    if isinstance(inp, dict):
        for k in ("path", "file_path", "pattern", "query", "url", "command", "description"):
            if inp.get(k):
                return str(inp[k]).replace("\n", " ")[:40]
    return ""


def turn_timeline(trace: Any) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for st in trace.steps:
        d = st.data
        if st.kind == "llm":
            u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
            cache = d.get("cache") if isinstance(d.get("cache"), dict) else {}
            rows.append({"index": st.index, "kind": "llm", "label": f"model {d.get('iteration', '')}".strip(),
                         "duration_s": float(d.get("duration_s") or st.duration_s),
                         "tokens_in": int(u["input"]) if "input" in u else None,
                         "tokens_out": int(u["output"]) if "output" in u else None,
                         "cache_read": int(u["cache_read"]) if "cache_read" in u else None,
                         "cost": float(d["cost"]) if d.get("cost") is not None else None,
                         "cache_hit_pct": cache.get("hit_pct"),
                         "cache_miss_cause": str(d.get("cache_miss_cause") or cache.get("cause") or "")})
        elif st.kind == "tool":
            rows.append({"index": st.index, "kind": "tool", "label": f"{d.get('name', '?')} {_target(d.get('input'))}".strip(),
                         "duration_s": float(d.get("duration_s") or st.duration_s), "tokens_in": None,
                         "tokens_out": int(d["est_tokens"]) if d.get("est_tokens") is not None else None,
                         "cache_read": None, "cost": None, "cache_hit_pct": None, "cache_miss_cause": ""})
        elif st.kind in ("retry", "compaction"):
            rows.append({"index": st.index, "kind": st.kind, "label": st.kind, "duration_s": st.duration_s,
                         "tokens_in": None, "tokens_out": None, "cache_read": None,
                         "cost": float(d["cost"]) if d.get("cost") else None, "cache_hit_pct": None, "cache_miss_cause": ""})
    return {"turn": trace.turn, "model": trace.model, "total_s": float(trace.duration_s), "cost": float(trace.cost),
            "rows": rows}


def _secs(v: float) -> str:
    return f"{v:.1f}s" if v < 100 else f"{v:.0f}s"


def render_timeline(data: dict[str, Any], width: int = 100) -> list[str]:
    """Bar chart of step durations plus one muted detail line per step that has tokens, cost or a miss cause."""
    from axon.ui.charts import bar_chart
    from axon.ui.theme import MUTED, RST, truncate
    rows = data["rows"]
    if not rows:
        return []
    labels = [f"{r['index']} {r['label']}" for r in rows]
    out = bar_chart(labels, [r["duration_s"] for r in rows], width,
                    title=f"Turn {data['turn']} · {_secs(data['total_s'])} (seconds per step)")
    for r in rows:
        bits = []
        if r["tokens_in"] is not None:
            bits.append(f"{r['tokens_in']:,} in / {r['tokens_out']:,} out")
        elif r["tokens_out"] is not None:
            bits.append(f"~{r['tokens_out']:,} tokens returned")
        if r["cost"]:
            bits.append(f"${r['cost']:.4f}")
        if r["cache_miss_cause"]:
            bits.append(f"cache miss: {r['cache_miss_cause']}")
        elif r["cache_hit_pct"] is not None:
            bits.append(f"cache hit {r['cache_hit_pct']:.0f}%")
        if bits:
            out.append(truncate(f"  {MUTED}{r['index']:>2}  {' · '.join(bits)}{RST}", width))
    return out
