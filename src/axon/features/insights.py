"""Insights that show Axon's work: cache autopsy, prompt diffs, repricing, context pins, receipts.

Everything here is computed from data Axon already recorded (usage, fingerprints, traces, the
conversation). Nothing is estimated unless the output says so, and a missing number is left out.
"""
from __future__ import annotations

import re
from typing import Any

# A cache read that falls below this share of the previous read counts as a sharp drop.
DROP_RATIO = 0.5
# Below this many cached tokens a "drop" is noise (providers do not cache tiny prefixes).
MIN_DROP_TOKENS = 1024

# diagnostics.cache_miss_reason.type (Claude API) -> the words Axon shows
API_CAUSES = {
    "model_changed": "model changed",
    "system_changed": "system prompt changed",
    "tools_changed": "tool definitions changed",
    "messages_changed": "conversation history changed",
    "previous_message_not_found": "the API no longer had the previous request",
}


def fmt_k(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{round(n / 1000)}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def sharp_drop(prev_read: int, read: int) -> bool:
    """True when the cache read fell to under half of the previous call's read."""
    return prev_read >= MIN_DROP_TOKENS and read < prev_read * DROP_RATIO


def api_cause(diagnostics: Any) -> tuple[str, int] | None:
    """(cause, missed input tokens) from a Claude API ``diagnostics`` object, or None when it says nothing usable.

    ``unavailable`` and anything unknown return None, so the local fingerprint comparison is used instead."""
    if not isinstance(diagnostics, dict):
        return None
    reason = diagnostics.get("cache_miss_reason")
    kind = str(reason.get("type", "")) if isinstance(reason, dict) else ""
    cause = API_CAUSES.get(kind)
    if not cause:
        return None
    try:
        missed = max(0, int(diagnostics.get("cache_missed_input_tokens") or 0))
    except (TypeError, ValueError):
        missed = 0
    return cause, missed


def extra_cost(model: str, resent: int, usage: Any) -> float | None:
    """Dollars the re-sent tokens cost above a cache read, at the rate they were billed at; None when the model has no price."""
    try:
        from axon.providers.pricing import price_for
        q = price_for(model)
        if not q.priced or q.local or resent <= 0:
            return None
        wrote = int(getattr(usage, "cache_write", 0) or 0) > 0
        rate = q.cache_write if wrote else q.input
        cost = float((rate - q.cache_read) * resent / 1_000_000)
        return cost if cost > 0 else None
    except Exception:
        return None


def miss_detail(resent: int, extra_cost: float | None) -> str:
    """The tail of the cache-miss line: ` · 41k tokens re-sent · +$0.11` (empty when nothing was re-sent)."""
    if resent <= 0:
        return ""
    out = f" · {fmt_k(resent)} tokens re-sent"
    if extra_cost is not None and extra_cost > 0:
        out += f" · +${extra_cost:.2f}" if extra_cost >= 0.005 else " · +<$0.01"
    return out


REFUSAL_NEXT_STEP = "next: /model to retry elsewhere, set refusal_fallback_model in /config, or /clear"


def refusal_detail(details: Any) -> str:
    """` (category: cyber) · <explanation> · suggested model: X · /model to retry with another` or "" when the API gave nothing."""
    if not isinstance(details, dict):
        return ""
    cat, why, rec = (" ".join(str(details.get(k) or "").split()) for k in ("category", "explanation", "recommended_model"))
    if not (cat or why or rec):
        return ""
    parts = [f"category: {cat}" if cat else "", why, f"suggested model: {rec}" if rec else "", REFUSAL_NEXT_STEP]
    return " · " + " · ".join(p for p in parts if p)


# ─── prompt manifests and /ctxdiff ───────────────────────────────────────────

MAX_MANIFESTS = 60
CHARS_PER_TOKEN = 3.7


def _tokens(obj: Any) -> int:
    from axon.agent.state import estimate_content_tokens
    return estimate_content_tokens(obj)


def _digest(obj: Any) -> str:
    from axon.agent.trace import _canon_hash
    return _canon_hash(obj)[:16]


def _message_label(msg: dict[str, Any], names: dict[str, tuple[str, str]]) -> str:
    """A short human label: `user: fix the bug…`, `tool_result Read app.py`, `assistant: tool_use Read`."""
    role = str(msg.get("role", "?"))
    content = msg.get("content")
    if isinstance(content, str):
        return f"{role}: {' '.join(content.split())[:60]}"
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "tool_result":
            name, arg = names.get(str(b.get("tool_use_id", "")), ("", ""))
            return f"tool_result {name} {arg}".strip() if name else f"tool_result {b.get('tool_use_id', '')}"
        if kind == "tool_use":
            return f"{role}: tool_use {b.get('name', '')} {_arg_hint(b.get('input'))}".strip()
        if kind == "text":
            return f"{role}: {' '.join(str(b.get('text', '')).split())[:60]}"
        if kind:
            return f"{role}: {kind}"
    return role


def _arg_hint(inp: Any) -> str:
    if isinstance(inp, dict):
        for k in ("file_path", "path", "pattern", "command", "url", "query"):
            if inp.get(k):
                return " ".join(str(inp[k]).split())[:48]
    return ""


def tool_names(messages: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """tool_use id -> (tool name, short argument) for every call in the conversation."""
    names: dict[str, tuple[str, str]] = {}
    for m in messages:
        c = m.get("content")
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                names[str(b.get("id", ""))] = (str(b.get("name", "")), _arg_hint(b.get("input")))
    return names


def build_manifest(system: list[dict[str, Any]], tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per block of a prompt: kind, content digest (ignores cache markers), label, estimated tokens."""
    out: list[dict[str, Any]] = []
    for b in system or []:
        text = str(b.get("text", "")) if isinstance(b, dict) else str(b)
        out.append({"kind": "system", "id": _digest(b), "label": " ".join(text.split())[:60] or "system block", "tokens": _tokens(text)})
    for t in tools or []:
        name = str(t.get("name") or (t.get("function") or {}).get("name") or "tool") if isinstance(t, dict) else "tool"
        out.append({"kind": "tool", "id": _digest(t), "label": name, "tokens": _tokens(json_dumps(t))})
    names = tool_names(messages)
    for m in messages or []:
        out.append({"kind": "message", "id": _digest(m), "label": _message_label(m, names), "tokens": _tokens(m.get("content", ""))})
    return out


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), default=str)


def diff_manifests(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, Any]:
    """Blocks that entered and left between two manifests (a repeated block counts once per copy)."""
    left_pool: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for e in old:
        left_pool.setdefault((e["kind"], e["id"]), []).append(e)
    entered, unchanged = [], 0
    for e in new:
        pool = left_pool.get((e["kind"], e["id"]))
        if pool:
            pool.pop()
            unchanged += 1
        else:
            entered.append(e)
    left = [e for pool in left_pool.values() for e in pool]
    return {"entered": entered, "left": left, "unchanged": unchanged,
            "entered_tokens": sum(e["tokens"] for e in entered), "left_tokens": sum(e["tokens"] for e in left)}


def _signed_k(n: int) -> str:
    return f"{'+' if n >= 0 else '-'}{fmt_k(abs(n))}"


def render_ctxdiff(old: dict[str, Any], new: dict[str, Any], limit: int = 12) -> list[str]:
    """Plain lines (no colour) for /ctxdiff; the caller styles them."""
    d = diff_manifests(old["entries"], new["entries"])
    lines = [f"Prompt diff · call {new['n']} vs call {old['n']}" + (f" · {new['model']}" if new.get("model") else "")]
    ou, nu = old.get("usage"), new.get("usage")
    if ou and nu and ou.get("input") and nu.get("input"):
        delta = int(nu["input"]) - int(ou["input"])
        lines.append(f"input tokens (counted by the provider): {int(ou['input']):,} → {int(nu['input']):,} ({delta:+,})")
    if old.get("cost") is not None and new.get("cost") is not None:
        lines.append(f"cost of the call: ${float(old['cost']):.4f} → ${float(new['cost']):.4f}")
    for title, items, sign in (("entered", d["entered"], "+"), ("left", d["left"], "-")):
        lines.append(f"{title} ({len(items)})" + (f" · about {fmt_k(d[title + '_tokens'])} tokens" if items else ""))
        for e in sorted(items, key=lambda x: -x["tokens"])[:limit]:
            lines.append(f"  {sign} {e['kind']:<8} {e['label']:<62} {_signed_k(e['tokens'] if sign == '+' else -e['tokens']):>7}")
        if len(items) > limit:
            lines.append(f"  … and {len(items) - limit} more")
    lines.append(f"unchanged: {d['unchanged']} blocks")
    if new.get("miss"):
        lines.append("cache: miss · " + str(new.get("cause") or "cause unknown") + miss_detail(int(new.get("resent") or 0), new.get("extra_cost")))
    elif nu:
        lines.append(f"cache: {int(nu.get('cache_read', 0)):,} of {int(nu.get('input', 0)):,} input tokens read from cache")
    lines.append("Block sizes are estimates (about 3.7 characters per token); input totals come from the provider.")
    return lines


def ctxdiff_data(agent: Any, n: int | None = None) -> dict[str, Any]:
    """Data for /ctxdiff (the terminal command and the web app): {"ok", "message", "lines", "diff", "old", "new"}."""
    calls = list(getattr(getattr(agent, "_cache", None), "manifests", None) or [])
    if not calls:
        return {"ok": True, "message": "No model call has been recorded in this session yet."}
    n = calls[-1]["n"] if n is None else n
    new = next((c for c in calls if c["n"] == n), None)
    if new is None:
        return {"ok": False, "message": f"Call {n} is not available: only {calls[-1]['n']} calls were made"
                + (f", and the oldest kept is {calls[0]['n']}." if calls[0]["n"] > 1 else ".")}
    old = next((c for c in calls if c["n"] == n - 1), None)
    if old is None:
        return {"ok": True, "message": "That is the first call of the session: there is nothing earlier to compare with." if n == 1
                else f"Call {n - 1} is no longer kept (only the last {MAX_MANIFESTS} calls are)."}
    return {"ok": True, "message": "", "lines": render_ctxdiff(old, new), "diff": diff_manifests(old["entries"], new["entries"]),
            "old": {k: v for k, v in old.items() if k != "entries"}, "new": {k: v for k, v in new.items() if k != "entries"}}


def handle_ctxdiff(agent: Any, arg: str) -> bool:
    from axon.commands.common import header, info
    from axon.ui.theme import MUTED, RST
    word = arg.strip()
    if word and not word.isdigit():
        info("Give a call number such as 3, or nothing for the last two calls.")
        return False
    data = ctxdiff_data(agent, int(word) if word else None)
    if data.get("message"):
        info(data["message"])
        return bool(data["ok"])
    header(data["lines"][0])
    for ln in data["lines"][1:]:
        print(f"  {MUTED}{ln}{RST}")
    return True


# ─── /whatif: repricing a session ────────────────────────────────────────────

# Compared when no model is named (only those with a known price are shown).
WHATIF_DEFAULTS = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-4-5", "claude-haiku-4-5",
    "gpt-5", "gpt-5-mini", "o3", "gemini-2.5-pro", "gemini-2.5-flash", "deepseek-chat", "grok-4",
)
WHATIF_NOTE = ("This is a pricing calculation, not a re-run: the recorded token counts, priced at each model's list rates. "
               "A different model would answer differently, cache differently and use different numbers of tokens.")


class SessionTokens:
    def __init__(self) -> None:
        self.input = self.output = self.cache_read = self.cache_write = self.calls = 0
        self.usages: list[Any] = []
        self.actual = 0.0
        self.actual_known = True
        self.models: list[str] = []


def session_tokens(ledger: Any) -> SessionTokens:
    """Sum the ledger: every recorded call (main agent, subagents, side questions, compaction)."""
    t = SessionTokens()
    for e in list(getattr(ledger, "entries", None) or []):
        u = e.usage
        t.input += int(u.input or 0)
        t.output += int(u.output or 0)
        t.cache_read += int(u.cache_read or 0)
        t.cache_write += int(u.cache_write or 0)
        t.calls += 1
        t.usages.append(u)
        t.actual += float(e.cost or 0)
        t.actual_known = t.actual_known and bool(getattr(e, "priced", True))
        if e.model not in t.models:
            t.models.append(e.model)
    return t


def reprice(tokens: SessionTokens, model: str) -> dict[str, Any]:
    """Cost of the recorded tokens at `model`'s rates, as recorded (cache reads/writes) and with no caching at all."""
    from axon.providers.pricing import cost_details, price_for, uncached_cost
    q = price_for(model)
    if not q.priced:
        return {"model": model, "priced": False, "with_cache": None, "without_cache": None}
    return {"model": model, "priced": True,
            "with_cache": float(sum(cost_details(model, u).cost for u in tokens.usages)),
            "without_cache": float(sum(uncached_cost(model, u) for u in tokens.usages))}


def whatif_models(arg: str, used: list[str]) -> list[str]:
    from axon.providers.pricing import PRICING, PROVIDER_PRICING, price_for
    arg = arg.strip()
    known: list[str] = []
    for table in (*PROVIDER_PRICING.values(), PRICING):
        for k in table:
            if k not in known:
                known.append(k)
    if arg and arg.lower() == "all":
        return [k for k in known if price_for(k).priced and not price_for(k).local]
    if arg:
        hits = [k for k in known if k.lower().startswith(arg.lower())]
        return (hits or [arg])[:20]
    out = list(used)
    for k in WHATIF_DEFAULTS:
        if k not in out and price_for(k).priced:
            out.append(k)
    return out


def _usd(x: float | None) -> str:
    if x is None:
        return "no known price"
    return f"${x:,.2f}" if x >= 0.01 else f"${x:,.4f}"


def whatif_data(agent: Any, arg: str = "") -> dict[str, Any]:
    """Data for /whatif: session token totals, the actual cost and one repriced row per model (cheapest first)."""
    t = session_tokens(getattr(agent, "ledger", None))
    if not t.calls:
        return {"empty": True, "message": "Nothing recorded yet: this session has made no model calls to reprice."}
    rows = sorted((reprice(t, m) | {"used": m in t.models} for m in whatif_models(arg, t.models)),
                  key=lambda r: (not r["priced"], r["with_cache"] or 0.0))
    for r in rows:
        r["vs_actual_pct"] = (round((r["with_cache"] / t.actual - 1) * 100) if r["priced"] and t.actual_known and t.actual > 0 else None)
    return {"empty": False, "calls": t.calls, "input": t.input, "output": t.output, "cache_read": t.cache_read,
            "cache_write": t.cache_write, "actual": t.actual, "actual_known": t.actual_known, "models": t.models,
            "rows": rows, "note": WHATIF_NOTE}


def handle_whatif(agent: Any, arg: str) -> bool:
    from axon.commands.common import header, info, table
    from axon.ui.theme import MUTED, RST
    d = whatif_data(agent, arg)
    if d["empty"]:
        info(d["message"])
        return True
    header("What if this session ran on another model?")
    print(f"  {MUTED}{d['calls']} calls · {d['input']:,} input tokens ({d['cache_read']:,} read from cache, {d['cache_write']:,} written to cache) · {d['output']:,} output tokens{RST}")
    actual = _usd(d["actual"]) if d["actual_known"] else f"{_usd(d['actual'])} (some models in it have no known price, so this is a lower bound)"
    print(f"  {MUTED}this session actually cost {actual} on {', '.join(d['models'])}{RST}")
    rows = [[r["model"] + (" (used)" if r["used"] else ""), _usd(r["with_cache"]), _usd(r["without_cache"]),
             f"{r['vs_actual_pct']:+d}% vs actual" if r["vs_actual_pct"] is not None else ""] for r in d["rows"]]
    print()
    table(rows, headers=["model", "with caching", "without caching", ""])
    print(f"\n  {MUTED}{d['note']}{RST}")
    print(f"  {MUTED}With caching keeps the cache reads and writes recorded in this session, billed at that model's cache rates "
          f"(its plain input rate where none is listed).{RST}")
    return True


# ─── context pins ────────────────────────────────────────────────────────────

READ_LIKE = ("read", "cat", "view", "open", "fetch", "grep", "search")


def tool_calls(messages: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """(id, name, input) of every tool call in the conversation, oldest first (Anthropic and OpenAI shapes)."""
    import json
    out: list[tuple[str, str, dict[str, Any]]] = []
    for m in messages:
        content = m.get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                out.append((str(b.get("id", "")), str(b.get("name", "")), b.get("input") if isinstance(b.get("input"), dict) else {}))
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            out.append((str(tc.get("id", "")), str(fn.get("name", "")), args if isinstance(args, dict) else {}))
    return out


def tool_results(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """tool_use id -> result content, for every answered call."""
    from axon.agent.context import result_id
    out: dict[str, Any] = {}
    for m in messages:
        content = m.get("content")
        if m.get("role") == "tool" and result_id(m):
            out[result_id(m)] = content
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and result_id(m, b):
                out[result_id(m, b)] = b.get("content")
    return out


def sync_pins(agent: Any) -> None:
    """Load this session's pins from its transcript (the last `pins` record wins) when the session changed."""
    ctx = getattr(agent, "context", None)
    session = getattr(agent, "session", None)
    if ctx is None or session is None or not hasattr(ctx, "pins"):
        return
    sid = str(getattr(session, "active_session_id", "") or "")
    if getattr(ctx, "_pins_sid", None) == sid:
        return
    ctx._pins_sid = sid
    pins: dict[str, dict[str, Any]] = {}
    try:
        from axon.session.store import iter_records
        for entry in iter_records(session._file(sid)):
            if entry.get("type") == "pins" and isinstance(entry.get("data"), dict):
                pins = {str(p["id"]): {"label": str(p.get("label", "")), "tokens": int(p.get("tokens", 0) or 0)}
                        for p in entry["data"].get("pins") or [] if isinstance(p, dict) and p.get("id")}
    except Exception:
        pins = {}
    ctx.pins = pins


def _save_pins(agent: Any) -> None:
    try:
        agent.session.append("pins", {"pins": [{"id": k, **v} for k, v in agent.context.pins.items()]})
    except Exception:
        pass


def resolve_pin_target(agent: Any, target: str) -> tuple[str, str] | None:
    """(tool_use id, label) for a tool-result id or a path the model read, among results still in the conversation."""
    from pathlib import Path
    msgs = agent.conversation.messages
    calls, results = tool_calls(msgs), tool_results(msgs)
    answered = [c for c in calls if c[0] in results]
    for cid, name, inp in answered:
        if cid == target:
            return cid, f"{name} {_arg_hint(inp)}".strip()
    ws = Path(getattr(agent.settings, "workspace", "."))

    def same(value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        a, b = Path(value), Path(target)
        if not a.is_absolute():
            a = ws / a
        if not b.is_absolute():
            b = ws / b
        return a == b or str(Path(value)).endswith(str(Path(target))) and Path(target).name == Path(value).name

    hits = [c for c in answered if any(same(c[2].get(k)) for k in ("file_path", "path", "notebook_path", "filepath"))]
    if not hits:
        return None
    reads = [c for c in hits if c[1].lower().startswith(READ_LIKE)]
    cid, name, inp = (reads or hits)[-1]
    return cid, f"{name} {_arg_hint(inp)}".strip()


def pins_data(agent: Any) -> dict[str, Any]:
    """The pins of this session: {"pins": [{id, label, tokens, state}], "total_tokens", "recent": [...]}."""
    sync_pins(agent)
    msgs = agent.conversation.messages
    results = tool_results(msgs)
    rows, total = [], 0
    for cid, p in agent.context.pins.items():
        if cid in results:
            state = "in context"
        elif any(isinstance(m.get("content"), str) and f'<pinned id="{cid}"' in m["content"] for m in msgs):
            state = "kept in the summary"
        else:
            state = "no longer in this conversation"
        if state != "no longer in this conversation":
            total += int(p.get("tokens", 0))
        rows.append({"id": cid, "label": p.get("label", ""), "tokens": int(p.get("tokens", 0)), "state": state})
    recent = [{"id": c[0], "label": f"{c[1]} {_arg_hint(c[2])}".strip()} for c in tool_calls(msgs) if c[0] in results][-5:]
    return {"pins": rows, "total_tokens": total, "recent": recent[::-1]}


def pin_add(agent: Any, target: str) -> dict[str, Any]:
    """Pin the result of a file the model read or a tool-result id: {"ok", "message", "id", "label", "tokens"}."""
    from axon.agent.context import result_text
    sync_pins(agent)
    found = resolve_pin_target(agent, target.strip())
    if found is None:
        return {"ok": False, "message": f"Nothing to pin: no tool result in this conversation matches {target.strip()!r}. "
                                        "Give a file the model has read, or a tool-result id."}
    cid, label = found
    if cid in agent.context.pins:
        return {"ok": True, "message": f"Already pinned: {label} ({cid}).", "id": cid, "label": label}
    tokens = _tokens(result_text(tool_results(agent.conversation.messages).get(cid)))
    agent.context.pins[cid] = {"label": label, "tokens": tokens}
    _save_pins(agent)
    return {"ok": True, "id": cid, "label": label, "tokens": tokens,
            "message": f"Pinned {label} ({cid}) · about {fmt_k(tokens)} tokens. It stays in context in full, through trimming and compaction."}


def pin_remove(agent: Any, target: str = "") -> dict[str, Any]:
    """Release one pin (by id or path) or, with no target, all of them: {"ok", "message", "removed"}."""
    sync_pins(agent)
    pins, target = agent.context.pins, target.strip()
    if not pins:
        return {"ok": True, "message": "There are no pins to remove.", "removed": 0}
    if not target or target.lower() == "all":
        n = len(pins)
        pins.clear()
        _save_pins(agent)
        return {"ok": True, "removed": n, "message": f"Unpinned all {n} pin{'s' if n != 1 else ''}."}
    found = resolve_pin_target(agent, target)
    cid = found[0] if found and found[0] in pins else next((k for k, v in pins.items() if k == target or v.get("label", "").endswith(target)), "")
    if not cid:
        return {"ok": False, "message": f"{target!r} is not pinned.", "removed": 0}
    label = pins.pop(cid).get("label", "")
    _save_pins(agent)
    return {"ok": True, "removed": 1, "message": f"Unpinned {label} ({cid})."}


def handle_pin(agent: Any, arg: str) -> bool:
    from axon.commands.common import header, info
    from axon.ui.theme import MUTED, RST
    if arg.strip():
        r = pin_add(agent, arg)
        info(r["message"])
        return bool(r["ok"])
    d = pins_data(agent)
    if not d["pins"]:
        info("No pins yet. Pin a file the model has read, or a tool-result id, to keep that result in context in full.")
        for r in d["recent"]:
            print(f"  {MUTED}{r['id']}  {r['label']}{RST}")
        return True
    n = len(d["pins"])
    header(f"{n} pin{'s' if n != 1 else ''}")
    for r in d["pins"]:
        print(f"  {MUTED}{r['id']}  {r['label']}  · about {fmt_k(r['tokens'])} tokens · {r['state']}{RST}")
    print(f"  {MUTED}pinned total: about {fmt_k(d['total_tokens'])} tokens kept in context (estimate){RST}")
    return True


def handle_unpin(agent: Any, arg: str) -> bool:
    from axon.commands.common import info
    r = pin_remove(agent, arg)
    info(r["message"])
    return bool(r["ok"])


# ─── compaction receipt ──────────────────────────────────────────────────────

def build_receipt(old: list[dict[str, Any]], recent: list[dict[str, Any]], after: list[dict[str, Any]], summary: str) -> dict[str, Any]:
    """What one summary split did: sizes (estimates), the summary text and each message it replaced."""
    import time
    names = tool_names(old + recent)
    replaced = [{"index": i, "role": str(m.get("role", "?")), "label": _message_label(m, names), "tokens": _tokens(m.get("content", ""))}
                for i, m in enumerate(old, 1)]
    return {"t": time.time(), "trigger": "auto", "messages_before": len(old) + len(recent), "messages_after": len(after),
            "tokens_before": sum(r["tokens"] for r in replaced) + sum(_tokens(m.get("content", "")) for m in recent),
            "tokens_after": sum(_tokens(m.get("content", "")) for m in after),
            "summary": str(summary or "").strip(), "replaced": replaced}


def last_compaction(agent: Any) -> dict[str, Any] | None:
    """The receipt of the last compaction of this conversation, or None."""
    r = getattr(getattr(agent, "context", None), "last_receipt", None)
    return r if isinstance(r, dict) else None


def show_last_compaction(agent: Any, limit: int = 30) -> bool:
    from axon.commands.common import header, info
    from axon.ui.theme import MUTED, RST
    r = last_compaction(agent)
    if r is None:
        info("No compaction has happened in this session yet.")
        return True
    header(f"Last compaction ({r['trigger']})")
    print(f"  {MUTED}messages {r['messages_before']} → {r['messages_after']} · tokens about {fmt_k(r['tokens_before'])} → about {fmt_k(r['tokens_after'])}{RST}")
    print(f"  {MUTED}summary:{RST}")
    for ln in (r["summary"] or "(empty)").splitlines():
        print(f"    {MUTED}{ln}{RST}")
    print(f"  {MUTED}replaced {len(r['replaced'])} messages:{RST}")
    for m in r["replaced"][:limit]:
        print(f"    {MUTED}{m['index']:>3}  {m['label']:<64} {fmt_k(m['tokens']):>6}{RST}")
    if len(r["replaced"]) > limit:
        print(f"    {MUTED}… and {len(r['replaced']) - limit} more{RST}")
    print(f"  {MUTED}Token figures are estimates (about 3.7 characters per token).{RST}")
    return True


# ─── evidence receipts (heuristic) ───────────────────────────────────────────

_TEST_CMD = re.compile(r"\b(pytest|unittest|jest|vitest|mocha|go test|cargo test|npm (run )?test|yarn test|pnpm test|make test|tox|rspec|phpunit)\b", re.I)
_LINT_CMD = re.compile(r"\b(ruff|flake8|pylint|mypy|pyright|eslint|tsc|clippy|golangci|lint|typecheck)\b", re.I)
_BUILD_CMD = re.compile(r"\b(build|compile|tsc|cargo build|go build|make|gradle|mvn)\b", re.I)
_SEARCH_CMD = re.compile(r"\b(rg|grep|ag|ack|git grep)\b")
_FAILED = re.compile(r"\b(\d+ (failed|errors?)|FAILED|FAIL\b|Traceback|error:)", re.I)
# (kind, sentence pattern). Conservative on purpose: a miss is better than a false flag.
CLAIM_PATTERNS = (
    ("tests", re.compile(r"\b(all |the )?tests? (now )?(pass|passed|passes|are passing|are green|succeed)\b|\ball (the )?tests (pass|passed)\b", re.I)),
    ("lint", re.compile(r"\b(lint|ruff|mypy|type ?check(ing)?|eslint|tsc)\b[^.]{0,30}\b(clean|passes|passed|is green|reports no)\b", re.I)),
    ("build", re.compile(r"\b(the )?(build|compilation)\b[^.]{0,20}\b(succeeds|succeeded|passes|passed|is green|works)\b|\bit compiles\b", re.I)),
    ("callers", re.compile(r"\b(no|there are no|there is no) other (callers|usages|uses|references|call sites)\b|\bnothing else (calls|uses|references|imports)\b", re.I)),
)
_EDIT_CLAIM = re.compile(r"\b(updated|edited|modified|fixed|created|added|wrote|changed|removed) `?((?:[\w.-]+/)*[\w.-]+\.\w{1,6})`?")
_EDIT_TOOLS = ("edit", "write", "patch", "multi", "notebook")


def _last_turn_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from axon.agent.state import is_turn_start
    start = 0
    for i, m in enumerate(messages):
        if is_turn_start(m):
            start = i
    return messages[start:]


def _final_answer(turn: list[dict[str, Any]]) -> str:
    for m in reversed(turn):
        if m.get("role") != "assistant":
            continue
        c = m.get("content")
        text = c if isinstance(c, str) else " ".join(str(b.get("text", "")) for b in c or [] if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            return text
    return ""


def _sentences(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def receipts_data(agent: Any) -> dict[str, Any]:
    """Claims in the last answer and the tool call of that turn behind each (heuristic).

    {"answer_found", "claims": [{claim, kind, supported, call_id, call, note}]}"""
    from axon.agent.context import result_text
    turn = _last_turn_messages(agent.conversation.messages)
    answer = _final_answer(turn)
    calls, results = tool_calls(turn), tool_results(turn)
    out: list[dict[str, Any]] = []

    def find(pred: Any) -> tuple[str, str, str] | None:
        for cid, name, inp in reversed(calls):
            if cid in results and pred(name.lower(), inp):
                return cid, f"{name} {_arg_hint(inp)}".strip(), result_text(results[cid])
        return None

    def cmd(inp: dict[str, Any]) -> str:
        return str(inp.get("command") or inp.get("cmd") or "")

    is_shell = lambda n: any(k in n for k in ("bash", "shell", "exec", "run", "command"))  # noqa: E731
    for sentence in _sentences(answer):
        for kind, pat in CLAIM_PATTERNS:
            if not pat.search(sentence):
                continue
            rx = {"tests": _TEST_CMD, "lint": _LINT_CMD, "build": _BUILD_CMD}.get(kind)
            if kind == "callers":
                hit = find(lambda n, i: any(k in n for k in ("grep", "search", "find", "symbol", "reference")) or (is_shell(n) and _SEARCH_CMD.search(cmd(i))))
            else:
                hit = find(lambda n, i, rx=rx: is_shell(n) and bool(rx.search(cmd(i))))
            entry = {"claim": sentence, "kind": kind, "supported": hit is not None, "call_id": hit[0] if hit else "",
                     "call": hit[1] if hit else "", "note": "" if hit else "no supporting tool call in this turn"}
            if hit and kind in ("tests", "lint", "build") and _FAILED.search(hit[2]):
                entry.update(supported=False, note="the run's output mentions failures")
            out.append(entry)
        for m in _EDIT_CLAIM.finditer(sentence):
            path = m.group(2)
            hit = find(lambda n, i, p=path: any(k in n for k in _EDIT_TOOLS)
                       and any(str(i.get(k, "")).replace("\\", "/").endswith(p) for k in ("file_path", "path", "notebook_path")))
            out.append({"claim": f"{m.group(1)} {path}", "kind": "edit", "supported": hit is not None, "call_id": hit[0] if hit else "",
                        "call": hit[1] if hit else "", "note": "" if hit else "no supporting tool call in this turn"})
    return {"answer_found": bool(answer.strip()), "claims": out}


def handle_receipts(agent: Any, arg: str) -> bool:
    from axon.commands.common import header, info
    from axon.ui.theme import MUTED, RST
    d = receipts_data(agent)
    if not d["answer_found"]:
        info("There is no answer yet to check.")
        return True
    if not d["claims"]:
        info("No checkable claims found in the last answer (looked for: tests, lint, build, callers, edited files).")
        return True
    header("Receipts for the last answer (heuristic)")
    for c in d["claims"]:
        mark, tail = ("ok  ", f"{c['call']} [{c['call_id']}]") if c["supported"] else ("NONE", c["note"])
        print(f"  {MUTED}{mark} {c['claim'][:90]}  ->  {tail}{RST}")
    print(f"  {MUTED}Heuristic: a small pattern set matches sentences to the tool calls of that turn. A match shows a call ran, "
          f"not that it proves the claim.{RST}")
    return True


# ─── registration ────────────────────────────────────────────────────────────

def _commands() -> list[Any]:
    from axon.features import FeatureCommand
    return [
        FeatureCommand(name="/ctxdiff", handler=handle_ctxdiff, category="Context & Cost",
                       description="What entered and left the prompt between two model calls, and why the cache missed",
                       usage="/ctxdiff [n]   compare model call n with call n-1 (default: the last two calls)"),
        FeatureCommand(name="/whatif", handler=handle_whatif, category="Context & Cost",
                       description="Reprice this session's tokens at other models' prices, with and without caching",
                       usage="/whatif [model]   price the recorded tokens at a model (or a name prefix such as gpt-5); /whatif all lists every priced model"),
        FeatureCommand(name="/receipts", handler=handle_receipts, category="Context & Cost",
                       description="Claims in the last answer (tests pass, no other callers...) matched to the tool calls behind them",
                       usage="/receipts"),
        FeatureCommand(name="/pin", handler=handle_pin, category="Context & Cost",
                       description="Keep a tool result in context in full (never trimmed or compacted away)",
                       usage="/pin                 list the pins and their token total\n/pin <path|id>        pin the result of a file the model read, or a tool-result id"),
        FeatureCommand(name="/unpin", handler=handle_unpin, category="Context & Cost",
                       description="Release pinned tool results",
                       usage="/unpin               release every pin\n/unpin <path|id>      release one"),
    ]


COMMANDS = _commands()
