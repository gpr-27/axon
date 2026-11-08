"""Structured command cards for the web app: slash commands answer with data the page draws as a neat card.

Each builder reads the same data functions the terminal commands use (never the terminal text) and returns a small
JSON document: a title, blocks (kv, table, bars, list, stack, pick, controls, text), dropdowns that re-query, and
actions. A command with no builder, or with arguments the builder does not know, still runs in the terminal renderer
and the page shows its ANSI output inside the same card frame (see dash_js_cmdcards.py).

Routes (wired by one line each in dash_api.py): GET /api/cmdcard, POST /api/cmdcard/confirm.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

TONES = ("ok", "warn", "bad", "info", "muted")
_APPROVED: dict[str, float] = {}               # destructive lines the person confirmed in the card -> when
APPROVAL_S = 120


def usd(v: Any, digits: int = 2) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    if x and abs(x) < 0.01:
        digits = max(digits, 4)
    return f"${x:,.{digits}f}"


def tok(n: Any) -> str:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return "-"
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 10_000:
        return f"{x / 1000:.0f}k"
    if x >= 1000:
        return f"{x / 1000:.1f}k"
    return str(int(x))


def dur(s: Any) -> str:
    try:
        x = float(s)
    except (TypeError, ValueError):
        return "-"
    if x < 60:
        return f"{x:.0f}s"
    if x < 3600:
        return f"{int(x // 60)}m {int(x % 60)}s"
    return f"{int(x // 3600)}h {int(x % 3600 // 60)}m"


def pct(f: float) -> str:
    return f"{f * 100:.0f}%"


def tone_for(frac: float, warn: float = 0.75, bad: float = 0.9) -> str:
    return "ok" if frac < warn else ("warn" if frac < bad else "bad")


# ─── block makers ────────────────────────────────────────────────────────────

def h(text: str, note: str = "") -> dict[str, Any]:
    return {"t": "h", "text": text, "note": note}


def kv(*rows: tuple[Any, ...]) -> dict[str, Any]:
    return {"t": "kv", "rows": [{"k": str(r[0]), "v": str(r[1]), **({"tone": r[2]} if len(r) > 2 and r[2] else {})} for r in rows]}


def table(cols: list[str], rows: list[list[Any]], right: tuple[int, ...] = (), note: str = "", empty: str = "") -> dict[str, Any]:
    return {"t": "table", "cols": [{"l": c, "r": i in right} for i, c in enumerate(cols)], "rows": rows, "note": note, "empty": empty}


def bars(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t": "bars", "rows": rows}


def items(rows: list[dict[str, Any]], empty: str = "") -> dict[str, Any]:
    return {"t": "list", "rows": rows, "empty": empty}


def text(s: str, tone: str = "") -> dict[str, Any]:
    return {"t": "text", "text": s, "tone": tone}


def stack(parts: list[dict[str, Any]], total_label: str = "") -> dict[str, Any]:
    return {"t": "stack", "parts": parts, "label": total_label}


def pick(label: str, current: str, options: list[tuple[str, str, str]], prefix: str) -> dict[str, Any]:
    """Radio-style choices: choosing one runs ``prefix + value`` and redraws the card."""
    return {"t": "pick", "label": label, "current": current, "prefix": prefix,
            "options": [{"value": v, "label": lab, "hint": hint} for v, lab, hint in options]}


def controls(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"t": "controls", "rows": rows}


def select(key: str, label: str, value: str, options: list[tuple[str, str]]) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "options": [[a, b] for a, b in options]}


def card(title: str, *blocks: dict[str, Any] | None, sub: str = "", selects: list[dict[str, Any]] | None = None,
         actions: list[dict[str, Any]] | None = None, tip: str = "") -> dict[str, Any]:
    return {"v": 1, "title": title, "sub": sub, "blocks": [b for b in blocks if b], "selects": selects or [],
            "actions": actions or [], "tip": tip}


def act(label: str, line: str = "", href: str = "", confirm: str = "") -> dict[str, Any]:
    return {"label": label, **({"line": line} if line else {}), **({"href": href} if href else {}), **({"confirm": confirm} if confirm else {})}


# ─── context passed to builders ──────────────────────────────────────────────

def _ctx(srv: Any, rt: Any) -> SimpleNamespace:
    agent = rt.agent if rt is not None else None
    ws = Path(getattr(rt, "workspace", None) or srv.workspace)
    return SimpleNamespace(srv=srv, rt=rt, agent=agent, ws=ws, _ext=None)


def _ext(c: SimpleNamespace) -> dict[str, Any]:
    if c._ext is None:
        from axon.features.dashboard import extensions_data
        c._ext = extensions_data(c.ws, c.agent)
    return c._ext


def _opt(o: dict[str, str], key: str, allowed: dict[str, Any] | list[str], default: str) -> str:
    v = (o or {}).get(key, default)
    return v if v in allowed else default


# ─── builders ────────────────────────────────────────────────────────────────

def b_usage(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands import usage_screen as us
    from axon.features import analytics, quota
    agent = c.agent
    period = _opt(o, "period", {"24h": 1, "7d": 7}, "7d")
    scope = _opt(o, "scope", ("all", "project"), "all")
    su = us.session_usage(agent)
    blocks: list[dict[str, Any] | None] = []

    # limits: only what is really known
    limit_rows: list[dict[str, Any]] = []
    for lim in quota.snapshot():
        frac = lim.used_fraction
        reset = quota.local_reset_text(lim.reset_at)
        if frac is None:
            limit_rows.append({"label": lim.label, "frac": None, "value": f"{tok(lim.remaining)} left", "note": reset})
        else:
            limit_rows.append({"label": lim.label, "frac": frac, "value": pct(frac) + " used", "tone": tone_for(frac, .75, .9),
                               "note": (f"{tok(lim.limit)} limit" + (" · " + reset if reset else ""))})
    spent = float(su.total_cost or 0)
    for key, limit in us.budget_settings(agent).items():
        frac = spent / limit if limit else 0
        limit_rows.append({"label": key.capitalize() + " budget", "frac": min(frac, 1.0), "value": pct(frac) + " used",
                           "tone": tone_for(frac), "note": f"{usd(spent)} of {usd(limit)}"})
    blocks.append(h("Limits", "what your provider reported and the budgets you set"))
    if limit_rows:
        blocks.append(bars(limit_rows))
    if not quota.snapshot():
        blocks.append(text("No rate-limit data from this provider yet. It shows here once the provider reports it.", "muted"))
    if not us.budget_settings(agent):
        blocks.append(text("No session budget is set. /budget 5 sets one.", "muted"))

    blocks.append(h("This session"))
    if su.total_cost is None:
        blocks.append(text("No API calls yet in this session.", "muted"))
    else:
        rows: list[tuple[Any, ...]] = [("Total cost", usd(su.total_cost, 4) + f"  ·  {su.calls} calls"),
                                       ("API duration", dur(su.api_s) if su.api_s is not None else "not recorded"),
                                       ("Wall duration", dur(su.wall_s) if su.wall_s is not None else "not recorded")]
        if su.added is not None:
            rows.append(("Code changes", f"+{su.added}  -{su.removed}  in {su.edited_files} file" + ("" if su.edited_files == 1 else "s")))
        blocks.append(kv(*rows))
        blocks.append(h("Breakdown"))
        blocks.append(table(["Model", "Input", "Output", "Cache read", "Cache write", "Cost"],
                            [[m.model, tok(m.input), tok(m.output), tok(m.cache_read), tok(m.cache_write), usd(m.cost, 4) + ("" if m.priced else "*")]
                             for m in su.models], right=(1, 2, 3, 4, 5),
                            note="* no price known for this model, so its cost is not counted" if any(not m.priced for m in su.models) else ""))

    fp = us.footprint(agent)
    total_fp = sum(r.tokens for r in fp)
    blocks.append(h("Sent with every turn", "estimated from text size, before the conversation"))
    blocks.append(table(["Source", "Items", "Tokens", "Share"],
                        [[r.name, r.detail, tok(r.tokens), pct(r.tokens / total_fp) if total_fp else "0%"] for r in fp], right=(2, 3)))

    # what's using your limits (all recorded sessions)
    days = analytics.Period(period, "last 24 hours" if period == "24h" else "last 7 days", time.time() - 86400 * (1 if period == "24h" else 7), 1 if period == "24h" else 7)
    proj = analytics._project_key(c.ws) if scope == "project" else None
    blocks.append(h("What's using your limits", "from your recorded usage log"))
    try:
        st = analytics.collect(days, proj)
        if not st.totals.calls:
            blocks.append(text("Nothing recorded for this period.", "muted"))
        else:
            blocks.append(kv(("Spent", usd(st.totals.cost)), ("API calls", f"{st.totals.calls:,}")))
            if scope == "all" and st.by_project:
                blocks.append(table(["Project", "Cost", "Share"], [[name, usd(a.cost), pct(a.cost / st.totals.cost) if st.totals.cost else "0%"]
                                                               for _, name, a in st.by_project[:6]], right=(1, 2)))
            blocks.append(table(["Model", "Cost", "Share"], [[name, usd(a.cost), pct(a.cost / st.totals.cost) if st.totals.cost else "0%"]
                                                          for name, a in st.by_model[:6]], right=(1, 2)))
            blocks.append(table(["Source", "Cost", "Share"], [[analytics._source_label(name), usd(a.cost), pct(a.cost / st.totals.cost) if st.totals.cost else "0%"]
                                                           for name, a in st.by_source[:6]], right=(1, 2)))
            if st.tools:
                top = sorted(st.tools, key=lambda t: -t.calls)[:6]
                blocks.append(table(["Tool", "Calls", "Time", "Errors"], [[t.name, str(t.calls), dur(t.total_s), str(t.errors)] for t in top],
                                    right=(1, 2, 3), note="tools are not billed; their output is part of the model input"))
    except Exception as e:
        blocks.append(text(f"The usage log could not be read ({type(e).__name__}).", "warn"))

    tip = ""
    if fp and total_fp:
        big = max(fp, key=lambda r: r.tokens)
        if big.tokens / total_fp > 0.4 and total_fp > 4000:
            tip = f"{big.name} is {pct(big.tokens / total_fp)} of what every turn sends (~{tok(big.tokens)} tokens). Trimming it lowers the cost of every turn."
    return card("Usage", *blocks, sub=c.agent.settings.model,
                selects=[select("period", "Period", period, [("24h", "Last 24 hours"), ("7d", "Last 7 days")]),
                         select("scope", "Scope", scope, [("all", "All projects"), ("project", "This project")])],
                actions=[act("Cost by call", "/cost calls"), act("Context", "/context")], tip=tip)


def b_cost(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.context_cmds import _is_ledger, prompt_cache_stats
    agent = c.agent
    ledger = getattr(agent, "ledger", None)
    if not _is_ledger(ledger) or not ledger.entries:
        return card("Cost", text("No usage recorded yet in this session.", "muted"))
    from decimal import Decimal
    total = Decimal(str(ledger.total()))
    rows = [r for r in (ledger.breakdown() if hasattr(ledger, "breakdown") else []) if isinstance(r, dict)]
    try:
        from axon.session.ledger import source_label
    except Exception:
        def source_label(s: str) -> str:
            return s
    calls = sum(int(r.get("calls", 0)) for r in rows)
    tin = sum(int(r.get("input", 0)) for r in rows)
    tout = sum(int(r.get("output", 0)) for r in rows)
    cread = sum(int(r.get("cache_read", 0)) for r in rows)
    facts: list[tuple[Any, ...]] = [("Total", usd(total, 5) + f"  ·  {calls} calls  ·  {tok(tin)} in  ·  {tok(tout)} out")]
    if tin:
        facts.append(("Cache hit rate", f"{cread / tin * 100:.1f}% of input read from cache"))
    cache = prompt_cache_stats(agent)
    if cache["calls"]:
        facts.append(("Prompt cache", f"{cache['hit_ratio'] * 100:.0f}% read from cache  ·  {cache['hits']} hit, {cache['misses']} miss  ·  " + ("warm" if cache["warm"] else "cold")))
    try:
        cf = Decimal(str(ledger.uncached_counterfactual(agent.settings.model)))
        if cf > total:
            facts.append(("Cache savings", f"{usd(cf - total, 5)} saved (would be {usd(cf, 5)} uncached)", "ok"))
    except Exception:
        pass
    from axon.session.store import is_user_prompt
    turns = sum(1 for m in agent.conversation.messages if isinstance(m, dict) and is_user_prompt(m)) or 0
    if turns:
        facts.append(("Per turn", f"{usd(total / turns, 5)} average over {turns} turn(s)"))
    return card("Cost", kv(*facts), h("By source and model"),
                table(["Source", "Model", "Calls", "Input", "Output", "Cache r/w", "Cost"],
                      [[r.get("label") or source_label(str(r.get("source", "main"))), r.get("model", ""), str(r.get("calls", 0)), tok(r.get("input", 0)),
                        tok(r.get("output", 0)), f"{tok(r.get('cache_read', 0))}/{tok(r.get('cache_write', 0))}",
                        usd(r.get("cost", 0), 5) + ("" if r.get("priced", True) else " (unpriced)")] for r in rows], right=(2, 3, 4, 5, 6)),
                sub=getattr(agent.session, "active_session_id", "") or "",
                actions=[act("Every call", "/cost calls"), act("Model prices", "/pricing"), act("What if another model", "/whatif")])


def b_status(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.model_cmds import current_trace_level
    agent = c.agent
    s = agent.settings
    try:
        from axon import __version__ as ver
    except Exception:
        ver = ""
    rows: list[tuple[Any, ...]] = [("Model", f"{s.model}  ·  effort {s.effort}  ·  thinking {'on' if s.thinking else 'off'}"),
                                   ("Endpoint", s.base_url), ("Mode", s.mode), ("Trace level", current_trace_level(agent)),
                                   ("Session", agent.session.active_session_id)]
    goal = getattr(agent, "goal", None) or getattr(s, "goal", None)
    if goal:
        rows.append(("Goal", str(goal)))
    msgs = agent.conversation.messages
    from axon.commands.common import estimate_tokens
    rows.append(("Context", f"{len(msgs)} messages  ·  ~{tok(sum(estimate_tokens(m.get('content', '')) for m in msgs))} tokens"))
    try:
        rows.append(("Cost", f"{usd(agent.ledger.total(), 5)}  ·  {len(agent.ledger.entries)} calls"))
    except Exception:
        pass
    extras = []
    q = getattr(agent, "message_queue", None)
    if q is not None and len(q):
        extras.append(f"{len(q)} queued")
    try:
        done, total, _ = agent.todos.progress()
        if total:
            extras.append(f"todos {done}/{total}")
    except Exception:
        pass
    if extras:
        rows.append(("Activity", "  ·  ".join(extras)))
    rows.append(("Workspace", str(c.ws)))
    try:
        from axon.agent.memory import discover_instruction_files
        rows.append(("Memory files", f"{len(discover_instruction_files(c.ws))} loaded"))
    except Exception:
        pass
    try:
        from axon import storage
        total_b = sum(int(r.get("bytes", 0)) for r in storage.storage_summary())
        rows.append(("Storage", f"{storage.axon_home()}  ·  {total_b / 1_048_576:.1f} MB"))
    except Exception:
        pass
    return card("Status", kv(*rows), sub=f"Axon {ver}".strip(), actions=[act("Usage", "/usage"), act("Context", "/context"), act("Doctor", "/doctor")])


def _context_parts(agent: Any) -> tuple[list[dict[str, Any]], int, int, int, int]:
    import json as _json
    from axon.commands.common import estimate_tokens
    from axon.commands.context_cmds import _block_kind, _budget, _system_blocks, _tool_schemas
    from collections import Counter
    parts: Counter[str] = Counter()
    for b in _system_blocks(agent):
        t = str(b.get("text", ""))
        parts[_block_kind(t)] += estimate_tokens(t)
    schemas = _tool_schemas(agent)
    tool_tok = estimate_tokens(_json.dumps(schemas, separators=(",", ":"))) if schemas else 0
    msgs = list(agent.conversation.messages)
    msg_tok = sum(estimate_tokens(m.get("content", "")) for m in msgs)
    window, threshold, reserve = _budget(agent)
    rows = [{"label": "System prompt", "tokens": parts["system"] + parts["agents"], "note": ""},
            {"label": "Memory files", "tokens": parts["memory"], "note": "AXON.md, AGENTS.md, CLAUDE.md and saved memories"},
            {"label": "Skills list", "tokens": parts["skills"], "note": f"{len(getattr(getattr(agent, 'skills', None), 'skills', {}) or {})} skills"},
            {"label": "Tool schemas", "tokens": tool_tok, "note": f"{len(schemas)} tools"},
            {"label": "Messages", "tokens": msg_tok, "note": f"{len(msgs)} messages"}]
    return rows, window, threshold, reserve, sum(r["tokens"] for r in rows)


def b_context(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    agent = c.agent
    rows, window, threshold, reserve, total = _context_parts(agent)
    parts = [{"label": r["label"], "frac": r["tokens"] / window if window else 0, "value": tok(r["tokens"]), "i": i} for i, r in enumerate(rows)]
    used = total / window if window else 0
    blocks = [stack(parts, f"{tok(total)} of {tok(window)} tokens  ({pct(used)})"),
              table(["Category", "Tokens", "Of window", "Of used"],
                    [[r["label"], tok(r["tokens"]), pct(r["tokens"] / window) if window else "-", pct(r["tokens"] / total) if total else "-"] for r in rows]
                    + [["Free", tok(max(0, window - total)), pct(max(0, window - total) / window) if window else "-", ""]], right=(1, 2, 3)),
              kv(("Auto-compact at", f"{tok(threshold)}  ({pct(threshold / window) if window else '-'} of the window)"),
                 *([("Reply reserve", tok(reserve))] if reserve else []),
                 ("Sliding window", f"{agent.settings.max_history_turns} turns" if getattr(agent.settings, "max_history_turns", 0) else "unlimited"))]
    last = getattr(getattr(agent, "ledger", None), "last_usage", None)
    if last is not None and getattr(last, "input", 0):
        blocks.append(text(f"Last API call: {last.input:,} input tokens billed. The numbers above are estimates (~4 characters per token).", "muted"))
    tip = f"Context is {pct(used)} full. /compact summarises older turns to make room." if used > 0.6 else ""
    return card("Context", *blocks, sub=f"{agent.settings.model}  ·  window {tok(window)}", tip=tip,
                actions=[act("What fills it", "/ctxmap"), act("Why each block", "/context --why"), act("Compact", "/compact", confirm="Summarise older turns now? The detail of those turns is replaced by a summary.")])


def b_ctxmap(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.ctxmap import ctxmap_data
    d = ctxmap_data(c.agent)
    window, total = d["window"], d["total"]
    order = sorted(d["groups"].items(), key=lambda kv_: -kv_[1])
    parts = [{"label": k.replace("_", " "), "frac": v / window if window else 0, "value": tok(v), "i": i} for i, (k, v) in enumerate(order)]
    top = sorted(d["items"], key=lambda i: -i["tokens"])[:12]
    rows = []
    for it in top:
        rows.append([it["label"], it["kind"].replace("_", " "), tok(it["tokens"]), pct(it["share"]), it["why"]])
    return card("Context map", stack(parts, f"{tok(total)} of {tok(window)} tokens"),
                kv(("Could be dropped", f"{tok(d['droppable_tokens'])} tokens of old tool results", "info" if d["droppable_tokens"] else "")),
                h("Largest items"), table(["Item", "Kind", "Tokens", "Share", "Note"], rows, right=(2, 3)),
                actions=[act("Compact", "/compact", confirm="Summarise older turns now?"), act("Context", "/context")])


def b_model(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features import dash_providers as dp
    s = c.agent.settings
    cur = s.model
    view = dp.providers_view(c.srv)
    active = next((p for p in view["providers"] if p["active"]), None)
    models = list(active["models"]) if active else []
    if cur not in models:
        models = [cur] + models
    opts = [(m, m, "current" if m == cur else "") for m in models]
    facts = [("Model", cur), ("Provider", active["name"] if active else "custom endpoint"), ("Endpoint", s.base_url), ("Effort", str(s.effort))]
    return card("Model", kv(*facts), pick("Switch model", cur, opts, "/model ") if len(opts) > 1 else text("This provider lists no other models.", "muted"),
                pick("Reasoning effort", str(s.effort), [(e, e, d) for e, d in _EFFORT_HINTS], "/effort "),
                actions=[act("Change provider or key", href="#/settings/providers"), act("Prices", "/pricing")])


_EFFORT_HINTS = [("reflex", "fast, minimal reasoning"), ("balanced", "standard depth"), ("synapse", "deep analysis"), ("quantum", "maximum reasoning")]


def b_effort(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.model_cmds import _EFFORT_ALIASES
    cur = _EFFORT_ALIASES.get(str(c.agent.settings.effort).lower(), str(c.agent.settings.effort))
    return card("Reasoning effort", pick("Choose how hard it thinks", cur, [(e, e, d) for e, d in _EFFORT_HINTS], "/effort "))


def b_mode(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.model_cmds import _MODE_DESC, _MODES
    cur = c.agent.settings.mode if c.agent.settings.mode in _MODES else "default"
    return card("Permission mode", pick("What Axon may do without asking", cur, [(m, m, _MODE_DESC.get(m, "")) for m in _MODES], "/mode "))


def b_theme(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.prefs_cmds import THEMES
    cur = str(getattr(c.agent.settings, "theme", "auto") or "auto")
    return card("Theme", pick("Colour theme of the terminal", cur, [(n, n, d) for n, d in THEMES], "/theme "),
                text("This sets the terminal theme. The look of this page is under Settings > Appearance.", "muted"),
                actions=[act("Appearance", href="#/settings/appearance")])


def b_trace(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.model_cmds import current_trace_level
    levels = (("compact", "one line per tool plus a footer"), ("normal", "step timeline with arguments, results, permissions"),
              ("detailed", "adds full tool input, result heads, prompt breakdown"), ("debug", "adds the raw request and usage"))
    return card("Trace level", pick("How much each turn shows", current_trace_level(c.agent), [(a, a, b) for a, b in levels], "/trace "),
                actions=[act("Inspect the last turn", "/trace last")])


def b_mcp(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    rows = _ext(c).get("mcp", [])
    lst = [{"mark": "ok" if r["status"] in ("connected", "ready") else ("off" if r["status"] in ("disabled", "not connected") else "warn"),
            "text": r["name"], "detail": f"{r['type']}  ·  {r['status']}" + (f"  ·  {r['tools']} tools" if r.get("tools") is not None else "") + f"  ·  {r['scope']}",
            "sub": r["target"] + (("  ·  " + r["error"]) if r.get("error") else "")} for r in rows]
    return card("MCP servers", items(lst, "No MCP servers are configured. /mcp add connects one."), sub=f"{len(rows)} configured",
                actions=[act("Open Extensions", href="#/extensions")])


def b_memory(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.dash_api import memory_list
    d = memory_list(c.srv)
    return card("Memory", h("Instruction files", "read at the start of every turn"),
                table(["File", "Scope", "Size"], [[f["name"], f["scope"], f"{f['size'] / 1024:.1f} KB"] for f in d["files"]], right=(2,),
                      empty="No AXON.md, CLAUDE.md or AGENTS.md here. /init creates one."),
                h("Saved memories"),
                table(["Name", "Type", "Scope", "About"], [[m["name"], m["type"], m["scope"], (m["description"] or "")[:90]] for m in d["memories"]],
                      empty="No saved memories yet. /learn remembers a rule or fact."),
                actions=[act("Open Memory", href="#/memory")])


def b_skills(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    rows = _ext(c).get("skills", [])
    return card("Skills", table(["Skill", "Scope", "Source", "About"], [[r["name"], r["scope"], r["plugin"] or "-", (r["description"] or "")[:100]] for r in rows],
                                empty="No skills are installed."), sub=f"{len(rows)} available",
                actions=[act("Browse the marketplace", href="#/extensions")])


def b_plugins(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    rows = _ext(c).get("plugins", [])
    lst = [{"mark": "ok" if r["enabled"] else "off", "text": f"{r['name']} {r['version']}".strip(), "detail": f"{r['scope']}  ·  " + ("on" if r["enabled"] else "off"),
            "sub": (r["description"] or "")[:120]} for r in rows]
    return card("Plugins", items(lst, "No plugins are installed."), sub=f"{len(rows)} installed", actions=[act("Open Extensions", href="#/extensions")])


def b_agents(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    rows = _ext(c).get("agents", [])
    return card("Subagents", table(["Agent", "Model", "Access", "About"], [[r["name"], r["model"], "read-only" if r["readonly"] else "can edit", (r["description"] or "")[:100]] for r in rows],
                                   empty="No subagent definitions found."), sub=f"{len(rows)} defined")


def b_tools(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.agent_cmds import tool_groups, tool_permission, tool_table
    tools, _ = tool_table(c.agent)
    blocks: list[dict[str, Any]] = []
    for title, names in tool_groups(tools):
        if not names:
            continue
        blocks.append(h(title))
        blocks.append(table(["Tool", "Access", "Does"], [[n, [tool_permission(c.agent, tools[n]), {"allow": "ok", "ask": "warn", "deny": "bad"}.get(tool_permission(c.agent, tools[n]), "")],
                                                          (tools[n].description.splitlines() or [""])[0][:110]] for n in names]))
    return card("Tools", *blocks, sub=f"{len(tools)} available  ·  mode {c.agent.settings.mode}", actions=[act("Rules", "/permissions")])


def b_hooks(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    rows = _ext(c).get("hooks", [])
    return card("Hooks", table(["Event", "Matcher", "Type", "Runs", "From"], [[r["event"], r["matcher"] or "all", r["type"], r["command"][:80], r["source"]] for r in rows],
                               empty="No hooks are configured."), sub=f"{len(rows)} configured")


def b_permissions(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.dash_api import permissions_view
    d = permissions_view(c.srv, c.rt.session_id if c.rt else "")
    def rules(key: str, tone: str) -> list[dict[str, Any]]:
        return [{"mark": tone, "text": r} for r in d[key]]
    return card("Permissions", kv(("Mode", d["mode"]), ("Bash sandbox", "on" if d["sandbox"] else "off", "ok" if d["sandbox"] else "")),
                h("Always allowed"), items(rules("allow", "ok"), "No allow rules."), h("Ask first"), items(rules("ask", "warn"), "No ask rules."),
                h("Never allowed"), items(rules("deny", "bad"), "No deny rules."),
                h("Extra folders"), items([{"mark": "info", "text": p} for p in d["additional_dirs"]], "Only the project folder."),
                actions=[act("Mode", "/mode"), act("Open Settings", href="#/settings")])


def b_diff(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features import gitops
    if not (gitops.git_available() and gitops.repo_root(c.ws)):
        return None
    ch = gitops.numstat(c.ws, "HEAD") if gitops._ok(c.ws, "rev-parse", "--verify", "HEAD") is not None else gitops.numstat(c.ws)
    st = gitops.repo_status(c.ws)
    if not ch and not st.untracked:
        return card("Changes", text("No uncommitted changes.", "muted"), sub=st.branch)
    peak = max([x.added + x.deleted for x in ch] + [1])
    rows = [[x.path, "binary" if x.binary else f"+{x.added}", "" if x.binary else f"-{x.deleted}"] for x in ch]
    blocks = [kv(("Branch", st.branch + (f"  ·  {st.ahead} ahead, {st.behind} behind" if st.ahead or st.behind else "")),
                 ("Changed", f"{len(ch)} file(s)  ·  +{sum(x.added for x in ch)}  -{sum(x.deleted for x in ch)}")),
              table(["File", "Added", "Removed"], rows, right=(1, 2))]
    if st.untracked:
        blocks += [h("New, not tracked yet"), items([{"mark": "info", "text": p} for p in st.untracked[:30]])]
    _ = peak
    return card("Changes", *blocks, sub=st.branch, actions=[act("Review hunks", "/review hunks"), act("Commit", "/commit")])


def b_todos(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    todos = getattr(c.agent, "todos", None)
    its = list(getattr(todos, "items", None) or [])
    if not its:
        return card("Task list", text("No task list yet. The agent makes one when the work has several steps.", "muted"))
    done, total, p = todos.progress()
    mark = {"completed": "ok", "in_progress": "run"}
    return card("Task list", bars([{"label": "Progress", "frac": done / total, "value": f"{done} of {total}", "tone": "ok" if done == total else "info", "note": ""}]),
                items([{"mark": mark.get(t.status, "off"), "text": t.content, "detail": t.status.replace("_", " ")} for t in its]), sub=f"{p}% done")


def b_tasks(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.agent_cmds import _background_shells
    shells = list(_background_shells())
    turns = getattr(c.agent, "background_tasks", None)
    turns = turns if isinstance(turns, dict) else {}
    mgr = getattr(c.agent, "subagents", None)
    subs = mgr.all_tasks() if mgr is not None and hasattr(mgr, "all_tasks") else []
    blocks: list[dict[str, Any]] = []
    now = time.time()
    if shells:
        blocks += [h("Background shells"), items([{"mark": "run" if s.status == "running" else ("ok" if s.status.endswith("code 0") else "bad"), "text": s.command[:90],
                                                  "detail": f"{s.id}  ·  {s.status}  ·  {now - s.started:.0f}s"} for s in shells])]
    if turns:
        blocks += [h("Background turns"), items([{"mark": "run" if str(getattr(r, "status", "")) == "running" else "ok", "text": str(getattr(r, "prompt", ""))[:90],
                                                 "detail": f"{tid}  ·  {getattr(r, 'status', '?')}"} for tid, r in turns.items()])]
    if subs:
        blocks += [h("Subagent tasks"), items([{"mark": {"running": "run", "completed": "ok"}.get(str(getattr(t, "status", "")), "bad"),
                                               "text": str(getattr(t, "description", "") or getattr(t, "prompt", "") or getattr(t, "id", ""))[:90],
                                               "detail": str(getattr(t, "status", ""))} for t in subs])]
    if not blocks:
        blocks = [text("Nothing is running in the background.", "muted")]
    return card("Tasks", *blocks, sub=f"{len(shells)} shell(s)  ·  {len(turns)} turn(s)  ·  {len(subs)} subagent task(s)")


def b_doctor(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.dash_ansi import strip_ansi
    from axon.tools.doctor import run_diagnostics
    from axon.features.dashboard import redact_text
    report = redact_text(strip_ansi(run_diagnostics(c.agent.settings, c.ws)))
    lst: list[dict[str, Any]] = []
    head: list[tuple[Any, ...]] = []
    for line in report.splitlines():
        s = line.strip()
        if not s or set(s) <= set("-=─━ "):
            continue
        label, sep, value = s.partition(":")
        if sep and len(label) <= 24 and value.strip():
            v = value.strip()
            tone = {"[ok]": "ok", "[!!]": "warn", "[--]": "muted"}.get(v[:4], "")
            if tone:
                v = v[4:].strip()
            head.append((label.strip(), v, tone))
        else:
            lst.append({"mark": "info", "text": s})
    blocks = [kv(*head)] if head else []
    if lst:
        blocks.append(items(lst))
    return card("Doctor", *blocks, actions=[act("Prompt audit", "/doctor prompt-audit"), act("Providers", href="#/settings/providers")])


_SECRETISH = ("key", "secret", "token", "password")


def b_config(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands.system_cmds import _CONFIG_KEYS, _panel_keys
    s = c.agent.settings
    rows: list[dict[str, Any]] = []
    seen = set()
    for key, label, choices, _ in _panel_keys(c.agent):
        seen.add(key)
        val = getattr(s, key, "")
        rows.append({"label": label, "key": key, "value": str(val).lower() if isinstance(val, bool) else str(val),
                     "type": "bool" if choices == "bool" else ("choice" if isinstance(choices, list) else "text"),
                     "options": ["true", "false"] if choices == "bool" else (list(choices) if isinstance(choices, list) else []),
                     "line": f"/config {key} " if key not in ("model", "mode", "effort") else f"/{key} "})
    other = [(k, d) for k, d in _CONFIG_KEYS.items() if k not in seen and not any(w in k for w in _SECRETISH)]
    return card("Configuration", controls(rows), h("Other settings", "change with /config <name> <value>"),
                table(["Setting", "Value", "What it does"], [[k, str(getattr(s, k, "")), d] for k, d in other]),
                actions=[act("Open Settings", href="#/settings")])


def b_sessions(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features import dash_manage
    scope = _opt(o, "scope", ("project", "all"), "project")
    d = dash_manage.list_sessions(c.srv, {"scope": scope, "limit": "10"})
    rows = d.get("sessions") or d.get("rows") or []
    lst = []
    for r in rows[:10]:
        lst.append({"mark": "ok" if r.get("live") else "off", "text": r.get("title") or r["id"], "detail": f"{r.get('messages', 0)} messages  ·  {usd(r.get('cost', 0))}  ·  {r.get('model', '')}",
                    "href": "#/chat/" + r["id"]})
    return card("Sessions", items(lst, "No saved sessions yet."), sub=f"{d.get('total', len(rows))} saved",
                selects=[select("scope", "Scope", scope, [("project", "This project"), ("all", "All projects")])], actions=[act("Open Sessions", href="#/sessions")])


def b_insights(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features import analytics
    key = _opt(o, "period", ("today", "7d", "30d", "all"), "7d")
    scope = _opt(o, "scope", ("project", "all"), "project")
    per = analytics.parse_period(key)
    st = analytics.collect(per, analytics._project_key(c.ws) if scope == "project" else None)
    sel = [select("period", "Period", key, [("today", "Today"), ("7d", "Last 7 days"), ("30d", "Last 30 days"), ("all", "All time")]),
           select("scope", "Scope", scope, [("project", "This project"), ("all", "All projects")])]
    if not st.totals.calls:
        return card("Insights", text("Nothing recorded for this period.", "muted"), selects=sel)
    t = st.totals
    peak = max([a.cost for _, a in st.days] + [0.0001])
    blocks = [kv(("Spent", usd(t.cost)), ("API calls", f"{t.calls:,}"), ("Tokens", f"{tok(t.input)} in  ·  {tok(t.output)} out"),
                 ("Cache hit", f"{t.hit_pct:.0f}%" if hasattr(t, "hit_pct") and not callable(t.hit_pct) else "-"),
                 ("Sessions", str(len(st.sessions))))]
    day_rows = [{"label": d.strftime("%b %d"), "frac": a.cost / peak, "value": usd(a.cost), "tone": "info", "note": f"{a.calls} calls"} for d, a in st.days[-14:] if a.calls]
    if day_rows:
        blocks += [h("Spend by day"), bars(day_rows)]
    blocks += [h("By model"), table(["Model", "Calls", "Cost"], [[n, str(a.calls), usd(a.cost)] for n, a in st.by_model[:8]], right=(1, 2))]
    if st.tools:
        blocks += [h("Busiest tools"), table(["Tool", "Calls", "Time", "Errors"], [[x.name, str(x.calls), dur(x.total_s), str(x.errors)] for x in sorted(st.tools, key=lambda x: -x.calls)[:8]], right=(1, 2, 3))]
    return card("Insights", *blocks, sub=per.label, selects=sel, actions=[act("Open Usage", href="#/usage")])


def b_receipts(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.insights import receipts_data
    d = receipts_data(c.agent)
    if not d["answer_found"]:
        return card("Receipts", text("No answer to check yet. Ask something first.", "muted"))
    lst = [{"mark": "ok" if x["supported"] else "warn", "text": x["claim"], "detail": x["call"] or x["note"], "sub": x["note"] if x["supported"] and x["note"] else ""} for x in d["claims"]]
    return card("Receipts", items(lst, "No checkable claims (tests, lint, build, edits, callers) in the last answer."),
                sub="claims in the last answer, matched to the tool calls behind them", tip="This match is heuristic: it looks for a command or edit in the same turn.")


def b_whatif(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.features.insights import whatif_data
    d = whatif_data(c.agent, "")
    if d["empty"]:
        return card("What if", text(d["message"], "muted"))
    rows = [[r["model"] + (" (used)" if r.get("used") else ""), usd(r["with_cache"]) if r["priced"] and r["with_cache"] is not None else "no price known",
             (f"{r['vs_actual_pct']:+d}%" if r.get("vs_actual_pct") is not None else "-")] for r in d["rows"]]
    return card("What if", kv(("Actual cost", usd(d["actual"], 4) if d["actual_known"] else "unknown"), ("Calls", str(d["calls"])), ("Tokens", f"{tok(d['input'])} in  ·  {tok(d['output'])} out")),
                h("This session repriced"), table(["Model", "Cost", "Versus actual"], rows, right=(1, 2)), tip=d.get("note", ""))


def b_budget(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.commands import usage_screen as us
    spent = float(us.session_usage(c.agent).total_cost or 0)
    b = us.budget_settings(c.agent)
    rows = [{"label": k.capitalize(), "frac": min(spent / v, 1.0), "value": pct(spent / v) + " used", "tone": tone_for(spent / v), "note": f"{usd(spent)} of {usd(v)}"} for k, v in b.items()]
    blocks = [bars(rows) if rows else text(f"No limit set. This session has spent {usd(spent)}.", "muted"),
              text("New turns stop once spending reaches the limit.", "muted")]
    return card("Budget", *blocks, actions=[act(f"Limit {usd(v, 0)}", f"/budget {v}") for v in (5, 10, 25)] + [act("Remove the limit", "/budget off")])


def b_sandbox(c: SimpleNamespace, arg: str, o: dict[str, str]) -> dict[str, Any] | None:
    if arg.strip():
        return None
    from axon.permissions import sandbox
    name, why = sandbox.backend()
    on = sandbox.enabled(c.agent.settings)
    pol = sandbox.policy_for(c.agent.settings)
    return card("Bash sandbox", kv(("State", "on" if on else "off", "ok" if on else ""), ("Backend", name or f"unavailable: {why}", "" if name else "warn"),
                                   ("Writes allowed in", ", ".join(str(p) for p in sandbox.write_roots(pol))), ("Network", "allowed" if pol.allow_network else "blocked")),
                text("With the sandbox on, shell commands can only write inside the project and temp folders, and credential files are hidden.", "muted"),
                actions=[act("Turn off" if on else "Turn on", "/sandbox off" if on else "/sandbox on")])


BUILDERS: dict[str, Callable[[SimpleNamespace, str, dict[str, str]], dict[str, Any] | None]] = {
    "usage": b_usage, "cost": b_cost, "status": b_status, "context": b_context, "ctxmap": b_ctxmap, "model": b_model, "mcp": b_mcp,
    "memory": b_memory, "skills": b_skills, "plugin": b_plugins, "agents": b_agents, "tools": b_tools, "hooks": b_hooks,
    "permissions": b_permissions, "diff": b_diff, "todos": b_todos, "tasks": b_tasks, "doctor": b_doctor, "config": b_config,
    "sessions": b_sessions, "insights": b_insights, "receipts": b_receipts, "whatif": b_whatif, "budget": b_budget,
    "sandbox": b_sandbox, "trace": b_trace, "effort": b_effort, "mode": b_mode, "theme": b_theme,
}
# the commands the owner listed, by the name they use (receipt -> receipts, plugins -> plugin)
LISTED = ("usage", "cost", "status", "context", "ctxmap", "model", "mcp", "memory", "skills", "plugin", "agents", "tools", "hooks", "permissions",
          "diff", "todos", "tasks", "doctor", "config", "sessions", "insights", "receipts", "whatif", "budget", "sandbox", "trace")
PICKERS = ("model", "effort", "mode", "theme", "trace")


# ─── confirm before destructive commands ─────────────────────────────────────

_ALSO_ASK = {"rewind", "restore"}


def needs_confirm(line: str) -> str:
    """Why this line should be confirmed in the card first ('' when it is safe to run)."""
    from axon.commands.registry import resolve_builtin
    from axon.commands.usage_menu import _needs_confirm
    parts = line.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return ""
    cmd = resolve_builtin(parts[0][1:])
    if cmd is None:
        return ""
    words = parts[1].strip() if len(parts) > 1 else ""
    if _needs_confirm(cmd.name, words) or (cmd.name in _ALSO_ASK and not words):
        return {"clear": "This clears the conversation shown here. The saved session stays in Sessions.",
                "delete": "This deletes saved session data.", "retitle": "This makes one model call per chat, which costs money.",
                "rewind": "This rewinds the conversation and may restore files.", "restore": "This restores files from a checkpoint."}.get(
            cmd.name, f"/{cmd.name} {words}".strip() + " changes or removes saved data.")
    return ""


def approve(line: str) -> None:
    for k in [k for k, t in _APPROVED.items() if time.time() - t > APPROVAL_S]:
        _APPROVED.pop(k, None)
    _APPROVED[line.strip()] = time.time()


def _approved(line: str) -> bool:
    t = _APPROVED.pop(line.strip(), 0)
    return bool(t) and time.time() - t < APPROVAL_S


# ─── entry points ────────────────────────────────────────────────────────────

def build(srv: Any, rt: Any, line: str, opts: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The card for *line*, or None when the command has no structured card (it then runs in the terminal renderer)."""
    from axon.commands.registry import resolve_builtin
    parts = line.strip().split(None, 1)
    if not parts or not parts[0].startswith("/") or rt is None:
        return None
    cmd = resolve_builtin(parts[0][1:])
    if cmd is None:
        return None
    fn = BUILDERS.get(cmd.name)
    if fn is None:
        return None
    try:
        out = fn(_ctx(srv, rt), parts[1].strip() if len(parts) > 1 else "", opts or {})
    except Exception:
        return None
    if out is not None:
        out["line"] = f"/{cmd.name}"
    return out


def try_card(srv: Any, rt: Any, line: str) -> dict[str, Any] | None:
    """Used by the command runner before it executes *line*: a confirm card for a destructive command, a data card for
    a structured one, else None."""
    why = needs_confirm(line)
    if why:
        if _approved(line):
            return None
        return {"v": 1, "title": line.strip().split()[0], "line": line.strip(), "blocks": [], "selects": [], "actions": [], "tip": "",
                "confirm": {"text": why, "line": line.strip()}}
    return build(srv, rt, line)


def choices(line: str) -> list[dict[str, Any]]:
    """Other ways to run a command, from its usage text (shown under a plain-text result)."""
    from axon.commands.registry import resolve_builtin
    from axon.commands.usage_menu import _needs_confirm, usage_choices
    parts = line.strip().split(None, 1)
    cmd = resolve_builtin(parts[0][1:]) if parts and parts[0].startswith("/") else None
    if cmd is None or len(parts) > 1:
        return []
    out = []
    for ch in usage_choices(cmd.name, cmd.usage or ""):
        full = (f"/{cmd.name} {ch.words}").strip()
        if not ch.words and not ch.ask:
            continue
        out.append({"label": ch.label, "line": full, "ask": ch.ask, "hint": ch.hint, "destructive": bool(_needs_confirm(cmd.name, ch.words))})
    return out[:12]


def audit() -> dict[str, Any]:
    """Every listed command, split by how its result is drawn."""
    from axon.commands.registry import visible_commands
    names = sorted(c.name for c in visible_commands())
    structured = [n for n in names if n in BUILDERS]
    return {"total": len(names), "structured": structured, "text": [n for n in names if n not in BUILDERS],
            "pickers": [n for n in PICKERS if n in BUILDERS]}


def get(srv: Any, qs: dict[str, str]) -> dict[str, Any]:
    if qs.get("audit") == "1":
        return audit()
    rt = srv.runtime(qs.get("session", ""), create=True)
    line = (qs.get("line") or "").strip()[:2000]
    if qs.get("choices") == "1":
        return {"choices": choices(line)}
    opts = {k[2:]: v[:60] for k, v in qs.items() if k.startswith("o.")}
    return {"card": build(srv, rt, line, opts)}


def post(srv: Any, route: str, body: dict[str, Any]) -> dict[str, Any]:
    if route == "/api/cmdcard/confirm":
        line = str(body.get("line") or "").strip()[:2000]
        if not needs_confirm(line):
            return {"ok": True}
        approve(line)
        return {"ok": True}
    from axon.features.dash_common import DashError
    raise DashError("unknown route", 404)
