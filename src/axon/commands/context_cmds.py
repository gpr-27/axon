"""Context & cost commands: /context /payload /compact /window /cost /pricing /faq /analytics."""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from typing import Any

from axon.commands.common import (
    bar, err, estimate_tokens, fmt_tokens, fmt_usd, header, hint, info, kv, ok, record_usage, section,
    table, update_settings, usage, width,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, DIM, GOLD, LBLUE, MINT, PURPLE, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Context & Cost"


# ─── Prompt assembly helpers ────────────────────────────────────────────────

def _system_blocks(agent: Any) -> list[dict[str, Any]]:
    from axon.agent.prompt import build_system
    skills = list(getattr(getattr(agent, "skills", None), "skills", {}).values()) if isinstance(getattr(getattr(agent, "skills", None), "skills", None), dict) else []
    try:
        return build_system(agent.settings, agent.registry, skills)
    except Exception:
        return []


def _tool_schemas(agent: Any) -> list[dict[str, Any]]:
    try:
        style = "anthropic" if getattr(agent.provider, "name", "") == "anthropic" else "openai"
        return list(agent.registry.schemas(provider_style=style))
    except Exception:
        return []


def _block_kind(text: str) -> str:
    head = text.lstrip()[:60].lower()
    if head.startswith("## memory"):
        return "memory"
    if head.startswith(("## skills", "## available skills")):
        return "skills"
    if head.startswith("## subagents"):
        return "agents"
    return "system"


def _schema_name(s: dict[str, Any]) -> str:
    return str(s.get("name") or s.get("function", {}).get("name", "?"))


def _budget(agent: Any) -> tuple[int, int, int]:
    """(context window, auto-compact threshold, reply reserve) for the active model.

    The window is the model's full context window, the same figure the per-step context gauge shows
    (LLMCallEnd.context_window). The part of it held back for the reply is reported separately; the
    auto-compact threshold is computed on what is left (ContextManager.threshold)."""
    ctx = getattr(agent, "context", None)
    model = agent.settings.model
    try:
        window = int(ctx.context_window(model))
    except Exception:
        window = 200_000
    try:
        usable = int(ctx.get_effective_budget(model))
    except Exception:
        usable = window
    try:
        threshold = int(ctx.threshold(model))
    except Exception:
        threshold = int(usable * float(getattr(agent.settings, "compact_at", 0.85)))
    return window, threshold, max(0, window - usable)


def _context_why(agent: Any) -> CommandResult:
    """Context X-ray: every injected block, why it is there and what it costs."""
    ws = agent.settings.workspace
    header("Context X-ray", "what is injected into every request, and why")
    rows: list[list[str]] = []
    for i, b in enumerate(_system_blocks(agent), 1):
        txt = str(b.get("text", ""))
        kind = _block_kind(txt)
        title = (txt.strip().splitlines() or [""])[0].lstrip("# ")[:44]
        trigger = {"system": "built-in (always)", "memory": "instruction files + saved memories", "skills": "installed skills (name + description)", "agents": "subagent definitions"}[kind]
        rows.append([f"{CYAN}S{i}{RST}", f"{WHITE}{title}{RST}", f"{SLATE}{trigger}{RST}", fmt_tokens(estimate_tokens(txt))])
    table(rows, ["#", "System block", "Trigger", "Tokens"])
    try:
        from axon.agent.memory import discover_instruction_files
        files = discover_instruction_files(ws)
    except Exception:
        files = []
    if files:
        section("Instruction files (memory)")
        def walk(node: Any, depth: int, parent: str) -> None:
            why = "user-level file" if node.scope == "user" else (f"imported by {parent}" if parent else f"found in {node.path.parent}")
            print(f"    {'  ' * depth}{LBLUE}●{RST} {WHITE}{node.path}{RST} {DARK_SLATE}~{fmt_tokens(estimate_tokens(node.content))} tok · {why}{RST}")
            for imp in getattr(node, "imports", []) or []:
                walk(imp, depth + 1, node.path.name)
        for f in files:
            walk(f, 0, "")
    try:
        from axon.agent.memory import MemoryStore
        items = MemoryStore(ws).list_all()
        if items:
            section("Saved memories")
            for it in items:
                print(f"    {GOLD}●{RST} {WHITE}{it.title[:50]}{RST} {DARK_SLATE}{it.scope} · ~{fmt_tokens(estimate_tokens(it.content))} tok · saved with /memory or #{RST}")
    except Exception:
        pass
    hooks_ctx = []
    for t in (getattr(agent, "traces", None) or [])[-10:]:
        for st in getattr(t, "steps", []):
            d = st.data if isinstance(st.data, dict) else {}
            for h in ([d] if st.kind == "hook" else d.get("hooks", []) or []):
                if isinstance(h, dict) and h.get("additional_context"):
                    hooks_ctx.append((t.turn, h.get("event", "?"), str(h["additional_context"])))
    if hooks_ctx:
        section("Hook additionalContext")
        for turn, ev, txt in hooks_ctx:
            print(f"    {PURPLE}●{RST} turn {turn} · {ev} {DARK_SLATE}~{fmt_tokens(estimate_tokens(txt))} tok{RST} {SLATE}{txt[:60]}{RST}")
    schemas = _tool_schemas(agent)
    mcp = [s for s in schemas if _schema_name(s).startswith("mcp__")]
    section("Tools")
    print(f"    {PURPLE}●{RST} {len(schemas) - len(mcp)} built-in tool schemas {DARK_SLATE}~{fmt_tokens(estimate_tokens(json.dumps([s for s in schemas if s not in mcp])))} tok · always{RST}")
    if mcp:
        print(f"    {PURPLE}●{RST} {len(mcp)} MCP tool schemas {DARK_SLATE}~{fmt_tokens(estimate_tokens(json.dumps(mcp)))} tok · connected MCP servers (/mcp){RST}")
    print()
    return CommandResult(handled=True)


def _cell_glyph(free: bool = False, buffer: bool = False) -> str:
    try:
        from axon.ui.theme import supports_unicode
        uni = supports_unicode()
    except Exception:
        uni = False
    if buffer:
        return "⛝" if uni else "x"
    if free:
        return "⛶" if uni else "."
    return "⛁" if uni else "#"


def context_grid(parts: list[tuple[int, str]], window: int, threshold: int, legend: list[str] | None = None,
                 cols: int = 10, rows: int = 10) -> list[str]:
    """Claude-Code-style usage grid: cols×rows cells (1% of the window each), coloured by category,
    then free cells and the auto-compact buffer. ``legend`` lines are printed beside the grid."""
    total_cells = cols * rows
    cells: list[str] = []
    if window > 0:
        for tok, color in parts:
            n = int(round(tok / window * total_cells))
            if tok > 0 and n == 0:
                n = 1
            cells += [f"{color}{_cell_glyph()}{RST}"] * n
    cells = cells[:total_cells]
    buffer_cells = int(round(max(0, window - threshold) / window * total_cells)) if window > 0 else 0
    free_cells = max(0, total_cells - len(cells) - buffer_cells)
    cells += [f"{DARK_SLATE}{_cell_glyph(free=True)}{RST}"] * free_cells
    cells += [f"{DIM}{_cell_glyph(buffer=True)}{RST}"] * (total_cells - len(cells))
    legend = legend or []
    out = []
    for r in range(rows):
        row = " ".join(cells[r * cols:(r + 1) * cols])
        side = legend[r] if r < len(legend) else ""
        out.append(f"    {row}   {side}".rstrip())
    for extra in legend[rows:]:
        out.append(f"    {' ' * (cols * 2 - 1)}   {extra}")
    return out


def _limit_label(n: int) -> str:
    return "model maximum" if not n else fmt_tokens(n)


def handle_context_limit(agent: Any, arg: str) -> CommandResult:
    """/context limit [128k|256k|1m|max]: how much of the model's window Axon uses (picker when bare)."""
    from axon.providers.registry import CONTEXT_LIMIT_CHOICES, get_context_window, parse_token_count
    from axon.commands.common import is_tty, update_settings_file
    model_max = get_context_window(agent.settings.model)
    current = int(getattr(agent.settings, "context_limit", 0) or 0)
    value = arg.strip()
    if not value:
        if not is_tty():
            header("Context limit", f"{agent.settings.model} supports {fmt_tokens(model_max)}")
            kv("Using", f"{_limit_label(current)} (auto-compact near 75% of it)")
            hint("    /context limit 128k | 256k | 1m | max")
            print()
            return CommandResult(handled=True)
        from axon.ui.picker import pick
        labels = {f"{fmt_tokens(n)} tokens" + (" (default)" if n == 128_000 else "")
                  + ("" if n <= model_max else f" — this model stops at {fmt_tokens(model_max)}"): n
                  for n in CONTEXT_LIMIT_CHOICES}
        labels[f"Model maximum ({fmt_tokens(model_max)})"] = 0
        cur = next((k for k, v in labels.items() if v == current), None)
        choice = pick(list(labels), title="Context limit: how much of the model's window to use", current=cur)
        if not choice:
            info("Context limit unchanged.")
            return CommandResult(handled=True)
        limit = labels[choice]
    else:
        try:
            limit = parse_token_count(value)
        except ValueError as e:
            usage(f"/context limit 128k | 256k | 1m | max   ({e})")
            return CommandResult(handled=True)
    update_settings(agent, context_limit=limit)
    try:
        from axon.commands.common import ws_of
        update_settings_file({"contextLimit": limit}, workspace=ws_of(agent))
    except Exception:
        pass
    window, threshold, _ = _budget(agent)
    capped = f" (this model stops at {fmt_tokens(model_max)})" if limit and limit > model_max else ""
    ok(f"Context limit {_limit_label(limit)}{capped}: using {fmt_tokens(window)} tokens, auto-compact at "
       f"{fmt_tokens(threshold)} ({threshold * 100 // max(1, window)}%). Saved as your default.")
    return CommandResult(handled=True)


@command("context", category=CAT, description="Context usage by category; --why explains each block; limit sets the window size",
         aliases=("tokens",), usage="/context          coloured usage grid + breakdown by category\n/context --why    every injected block, its trigger and tokens\n/context all      include per-tool token sizes\n/context limit    how much of the model's window to use: 128k (default), 256k, 1m or max")
def handle_context(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if a in ("--why", "why", "xray", "--xray"):
        return _context_why(agent)
    if a.split(maxsplit=1)[:1] in (["limit"], ["size"], ["window"]):
        return handle_context_limit(agent, a.split(maxsplit=1)[1] if " " in a else "")
    blocks = _system_blocks(agent)
    parts: Counter[str] = Counter()
    for b in blocks:
        txt = str(b.get("text", ""))
        parts[_block_kind(txt)] += estimate_tokens(txt)
    schemas = _tool_schemas(agent)
    tool_tok = estimate_tokens(json.dumps(schemas, separators=(",", ":"))) if schemas else 0
    msgs = list(agent.conversation.messages)
    msg_tok = sum(estimate_tokens(m.get("content", "")) for m in msgs)
    total = sum(parts.values()) + tool_tok + msg_tok
    window, threshold, reserve = _budget(agent)
    pct = total / window if window else 0.0

    from axon.providers.registry import get_context_window
    model_max = get_context_window(agent.settings.model)
    limit_note = (f" (limit; model max {fmt_tokens(model_max)} · /context limit to change)"
                  if window < model_max else "")
    header("Context usage", f"{agent.settings.model} · window {fmt_tokens(window)}{limit_note}")
    rows = [
        ("System prompt", parts["system"] + parts["agents"], TEAL, f"{sum(1 for b in blocks if _block_kind(str(b.get('text',''))) in ('system','agents'))} blocks"),
        ("Memory files", parts["memory"], LBLUE, "AXON.md / AGENTS.md / CLAUDE.md + saved memories"),
        ("Skills list", parts["skills"], CYAN, f"{len(getattr(getattr(agent, 'skills', None), 'skills', {}) or {})} skills"),
        ("Tool schemas", tool_tok, PURPLE, f"{len(schemas)} tools"),
        ("Messages", msg_tok, GOLD, f"{len(msgs)} messages"),
    ]
    print()
    legend = [f"{WHITE}{BOLD}{fmt_tokens(total)}{RST}{SLATE} / {fmt_tokens(window)} tokens ({pct * 100:.1f}%){RST}", ""]
    legend += [f"{color}{_cell_glyph()}{RST} {label}: {WHITE}{fmt_tokens(tok)}{RST} {SLATE}({tok / window * 100 if window else 0:.1f}%){RST}"
               for label, tok, color, _ in rows]
    buffer_tok = max(0, window - threshold)
    legend.append(f"{DARK_SLATE}{_cell_glyph(free=True)}{RST} Free: {SLATE}{fmt_tokens(max(0, window - total - buffer_tok))}{RST}")
    if buffer_tok:
        legend.append(f"{DIM}{_cell_glyph(buffer=True)}{RST} Auto-compact buffer: {SLATE}{fmt_tokens(buffer_tok)}{RST}")
    for line in context_grid([(tok, color) for _, tok, color, _ in rows], window, threshold, legend):
        print(line)
    print()
    for label, tok, color, note in rows:
        frac = tok / total if total else 0
        print(f"    {color}■{RST} {label:<14} {WHITE}{fmt_tokens(tok):>8}{RST}  {bar(frac, 12, color)} {SLATE}{frac * 100:>3.0f}%{RST}  {DARK_SLATE}{note}{RST}")
    free = max(0, window - total)
    print(f"    {DARK_SLATE}□{RST} {'Free':<14} {SLATE}{fmt_tokens(free):>8}{RST}")
    print()
    compact_pct = int(round(threshold / window * 100)) if window else 0
    kv("Auto-compact at", f"{fmt_tokens(threshold)} ({compact_pct}% of window)")
    if reserve:
        kv("Reply reserve", f"{fmt_tokens(reserve)} (usable for the prompt: {fmt_tokens(window - reserve)})")
    turns = getattr(agent.settings, "max_history_turns", 0) or 0
    kv("Sliding window", f"{turns} turns" if turns else "unlimited")
    last = getattr(getattr(agent, "ledger", None), "last_usage", None)
    if last is not None and getattr(last, "input", 0):
        kv("Last API call", f"{last.input:,} input tokens (billed; estimate above is ~{fmt_tokens(total)})")
    if a in ("all", "--all", "tools"):
        section("Tool schemas")
        sized = sorted(((_schema_name(sc), estimate_tokens(json.dumps(sc))) for sc in schemas), key=lambda x: -x[1])
        for name, tok in sized:
            print(f"    {WHITE}{name:<28}{RST} {SLATE}{fmt_tokens(tok):>7}{RST}")
        print()
    hint("    /context --why explains each block · /payload shows the exact prompt · /compact [focus]")
    print()
    return CommandResult(handled=True)


@command("payload", category=CAT, description="Inspect the exact prompt sent to the model",
         aliases=("breakdown", "input"), usage="/payload          section summary with token counts\n/payload full     full system prompt, tool list and messages")
def handle_payload(agent: Any, arg: str = "") -> CommandResult:
    full = arg.strip().lower() in ("full", "raw", "all")
    blocks = _system_blocks(agent)
    schemas = _tool_schemas(agent)
    msgs = list(agent.conversation.messages)
    prev, last = (msgs[:-1], msgs[-1]) if msgs else ([], None)
    sys_tok = sum(estimate_tokens(str(b.get("text", ""))) for b in blocks)
    tool_tok = estimate_tokens(json.dumps(schemas, separators=(",", ":"))) if schemas else 0
    prev_tok = sum(estimate_tokens(m.get("content", "")) for m in prev)
    last_tok = estimate_tokens(last.get("content", "")) if last else 0
    total = sys_tok + tool_tok + prev_tok + last_tok
    w = width()

    def box(title: str, color: str) -> None:
        print(f"\n  {color}{BOLD}┌─ {title} {'─' * max(2, w - len(title) - 6)}{RST}")

    def line(text: str, color: str) -> None:
        print(f"  {color}│{RST} {text}")

    def end(color: str) -> None:
        print(f"  {color}{BOLD}└{'─' * max(2, w - 3)}{RST}")

    print(f"\n  {GOLD}{BOLD}Active Input Payload Breakdown{RST}  {SLATE}{agent.settings.model} · ~{fmt_tokens(total)} tokens{RST}")

    box(f"[1] SYSTEM PROMPT · {len(blocks)} blocks · ~{fmt_tokens(sys_tok)}", TEAL)
    for i, b in enumerate(blocks, 1):
        txt = str(b.get("text", "")).strip()
        kind = _block_kind(txt)
        line(f"{CYAN}Block {i}{RST} {DARK_SLATE}({kind}, ~{fmt_tokens(estimate_tokens(txt))}){RST} {WHITE}{(txt.splitlines() or [''])[0][:60]}{RST}", TEAL)
        if full:
            for ln in txt.splitlines()[1:]:
                line(f"  {SLATE}{ln}{RST}", TEAL)
    end(TEAL)

    box(f"[2] TOOL DEFINITIONS · {len(schemas)} tools · ~{fmt_tokens(tool_tok)}", LBLUE)
    names = [_schema_name(s) for s in schemas]
    if full:
        for s in schemas:
            desc = str(s.get("description") or s.get("function", {}).get("description", "")).splitlines()
            line(f"{WHITE}{_schema_name(s):<16}{RST} {SLATE}{(desc[0] if desc else '')[:70]}{RST}", LBLUE)
    else:
        line(f"{WHITE}{', '.join(names)[: w - 8]}{RST}", LBLUE)
    end(LBLUE)

    def show_msg(i: int, m: dict[str, Any], color: str, limit: int) -> None:
        role = str(m.get("role", "?")).upper()
        content = m.get("content", "")
        rc = TEAL if role == "USER" else (GOLD if role == "ASSISTANT" else LBLUE)
        line(f"{rc}{BOLD}[{i}] {role}{RST} {DARK_SLATE}~{fmt_tokens(estimate_tokens(content))} tok{RST}", color)
        if isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    for ln in str(blk.get("text", "")).splitlines()[:limit]:
                        line(f"  {WHITE}{ln}{RST}", color)
                elif t == "tool_use":
                    line(f"  {GOLD}tool_use{RST} {blk.get('name')} {SLATE}{json.dumps(blk.get('input'), ensure_ascii=False)[:90]}{RST}", color)
                elif t == "tool_result":
                    res = str(blk.get("content", "")).strip().replace("\n", " ")
                    line(f"  {CYAN}tool_result{RST} {SLATE}{res[:90]}{RST}", color)
        else:
            for ln in str(content or "").splitlines()[:limit]:
                line(f"  {WHITE}{ln}{RST}", color)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            line(f"  {GOLD}tool_call{RST} {fn.get('name')} {SLATE}{str(fn.get('arguments', ''))[:90]}{RST}", color)

    box(f"[3] PREVIOUS CONVERSATION · {len(prev)} messages · ~{fmt_tokens(prev_tok)}", PURPLE)
    if not prev:
        line(f"{SLATE}(none){RST}", PURPLE)
    for i, m in enumerate(prev, 1):
        show_msg(i, m, PURPLE, 1000 if full else 1)
    end(PURPLE)

    box(f"[4] LAST MESSAGE · ~{fmt_tokens(last_tok)}", MINT)
    if last is None:
        line(f"{SLATE}(none){RST}", MINT)
    else:
        show_msg(len(msgs), last, MINT, 1000 if full else 12)
    end(MINT)

    box("[5] TOTAL INPUT TOKEN RECONCILIATION", GOLD)
    for label, tok in (("System prompt", sys_tok), ("Tool definitions", tool_tok), ("Prior messages", prev_tok), ("Last message", last_tok)):
        line(f"{SLATE}{label:<18}{RST} {WHITE}~{tok:,}{RST}", GOLD)
    line(f"{SLATE}{'Estimated total':<18}{RST} {WHITE}{BOLD}~{total:,}{RST}", GOLD)
    last_u = getattr(getattr(agent, "ledger", None), "last_usage", None)
    if last_u is not None and isinstance(getattr(last_u, "input", None), int) and last_u.input > 0:
        line(f"{MINT}{'Last billed input':<18}{RST} {WHITE}{BOLD}{last_u.input:,}{RST} {SLATE}exact count the provider "
             f"returned (usage.prompt_tokens) for the previous request{RST}", GOLD)
        line(f"{SLATE}{'':<18} the ~ figures above are estimates (characters / 4) of the request as it would be "
             f"sent now; the provider's tokenizer usually counts code and JSON as fewer tokens{RST}", GOLD)
    end(GOLD)
    if not full:
        hint("  /payload full shows every line")
    print()
    return CommandResult(handled=True)


def _safe_cut_fallback(messages: list[dict[str, Any]], keep: int = 4) -> int:
    """Index of a user message carrying a fresh prompt (never a tool result) near the end."""
    def is_turn_start(m: dict[str, Any]) -> bool:
        if m.get("role") != "user":
            return False
        c = m.get("content")
        if isinstance(c, str):
            return True
        return isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    starts = [i for i, m in enumerate(messages) if is_turn_start(m)]
    candidates = [i for i in starts if i > 0 and len(messages) - i <= max(keep, 2) * 3]
    return candidates[0] if candidates else (starts[-1] if len(starts) > 1 else 0)


@command("compact", category=CAT, description="Summarise older turns to free context (optional focus)",
         usage="/compact [instructions for the summary]")
def handle_compact(agent: Any, arg: str) -> CommandResult:
    conv = agent.conversation
    before_n = len(conv.messages)
    if before_n <= 2:
        info(f"Context is already minimal ({before_n} messages).")
        return CommandResult(handled=True)
    try:
        from axon.hooks import HookManager  # type: ignore[attr-defined]
        res = HookManager(agent.settings, agent.settings.workspace).run("PreCompact", {"trigger": "manual", "custom_instructions": arg})
        if getattr(res, "blocked", False):
            err(f"Compaction blocked by hook: {getattr(res, 'message', '')}")
            return CommandResult(handled=True)
    except Exception:
        pass
    before_tok = sum(estimate_tokens(m.get("content", "")) for m in conv.messages)
    print(f"\n  {TEAL}⚡ Compacting {before_n} messages (~{fmt_tokens(before_tok)} tokens){'  · focus: ' + arg if arg else ''}…{RST}")
    ctx = getattr(agent, "context", None)
    used_llm = False
    u = None
    if ctx is not None and hasattr(ctx, "summarize_with_provider"):
        try:
            u = ctx.summarize_with_provider(conv, agent.provider, agent.settings.model, instructions=arg, keep_turns=1)
            record_usage(getattr(agent, "ledger", None), agent.settings.model, u, "compaction")
            used_llm = u is not None
        except Exception as e:
            err(f"Summarisation failed: {e}")
    elif ctx is not None and hasattr(ctx, "_summarize_older_turns"):
        ctx._summarize_older_turns(conv, keep_turns=1)
    else:
        cut = _safe_cut_fallback(conv.messages)
        if cut > 0:
            dropped = conv.messages[:cut]
            note = f"[Earlier conversation ({len(dropped)} messages) was compacted. Ask the user if details are needed.]"
            conv.messages = [{"role": "user", "content": note}, {"role": "assistant", "content": "Understood."}] + conv.messages[cut:]
    after_n = len(conv.messages)
    after_tok = sum(estimate_tokens(m.get("content", "")) for m in conv.messages)
    if after_n >= before_n and after_tok >= before_tok:
        info("Nothing to compact yet (the conversation is a single turn).")
        return CommandResult(handled=True)
    record: dict[str, Any] = {"messages": conv.messages, "instructions": arg}
    if u is not None and hasattr(u, "input"):
        # the summary call's usage, so a resumed session's ledger (/cost) still counts it
        record["model"] = agent.settings.model
        record["usage"] = {k: int(getattr(u, k, 0) or 0) for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
    try:
        agent.session.append("compaction", record)
    except Exception:
        pass
    how = "LLM summary" if used_llm else "heuristic summary"
    ok(f"Compacted {before_n} → {after_n} messages · ~{fmt_tokens(before_tok)} → ~{fmt_tokens(after_tok)} tokens ({how})")
    return CommandResult(handled=True)


@command("window", category=CAT, description="Set the sliding context window in turns (0 = unlimited)",
         usage="/window [turns]")
def handle_window(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if not a:
        turns = getattr(agent.settings, "max_history_turns", 0) or 0
        header("Sliding window")
        kv("Window", f"{turns} turns (~{turns * 2} messages)" if turns else "unlimited")
        kv("Messages now", len(agent.conversation.messages))
        hint("    /window <turns> · /window 0 for unlimited")
        print()
        return CommandResult(handled=True)
    if a in ("unlimited", "all", "none", "off"):
        a = "0"
    try:
        n = int(a)
        if n < 0:
            raise ValueError
    except ValueError:
        usage("/window <non-negative number of turns>")
        return CommandResult(handled=True)
    update_settings(agent, max_history_turns=n)
    ok(f"Sliding window set to {BOLD}{n} turns{RST}" if n else "Sliding window disabled (unlimited history)")
    return CommandResult(handled=True)


# ─── Cost ───────────────────────────────────────────────────────────────────

CACHE_TTL_S = 300.0


def prompt_cache_stats(agent: Any, now: float | None = None) -> dict[str, Any]:
    """Prompt-cache health of the main agent from the turn traces: hit/miss calls, the share of
    input read from cache, warm/cold (last call within the 5-minute TTL) and the last miss cause."""
    import time as _time
    calls = hits = misses = 0
    read = written = uncached = 0  # "uncached" accumulates usage.input (total prompt tokens)
    last_end = 0.0
    last_cause = ""
    for t in getattr(agent, "traces", None) or []:
        if getattr(t, "agent_name", "main") != "main":
            continue
        for st in getattr(t, "steps", []) or []:
            if getattr(st, "kind", "") != "llm":
                continue
            d = st.data if isinstance(st.data, dict) else {}
            u = d.get("usage") or {}
            if not isinstance(u, dict) or "input" not in u and "cache_read" not in u:
                continue
            calls += 1
            cr, cw, inp = int(u.get("cache_read", 0) or 0), int(u.get("cache_write", 0) or 0), int(u.get("input", 0) or 0)
            read, written, uncached = read + cr, written + cw, uncached + inp
            if cr > 0:
                hits += 1
            else:
                misses += 1
                if d.get("cache_miss_cause"):
                    last_cause = str(d["cache_miss_cause"])
            last_end = max(last_end, float(getattr(st, "ended_at", 0.0) or 0.0))
    denom = max(uncached, read + written)  # usage.input is the total prompt (uncached + cache read + write)
    now = _time.time() if now is None else now
    return {"calls": calls, "hits": hits, "misses": misses, "cache_read": read, "cache_write": written,
            "hit_ratio": (read / denom) if denom else 0.0, "warm": bool(last_end and now - last_end < CACHE_TTL_S),
            "last_miss_cause": last_cause}


def _is_ledger(obj: Any) -> bool:
    return isinstance(getattr(obj, "entries", None), list)


@command("cost", category=CAT, description="Cost by source and model, cache savings, $/turn",
         aliases=("usage", "ledger"), usage="/cost            session breakdown\n/cost calls      every API call\n/cost --project  all sessions of this project (from ~/.axon/usage.jsonl)\n/cost --all      every project")
def handle_cost(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    ledger = getattr(agent, "ledger", None)
    model = agent.settings.model if isinstance(getattr(getattr(agent, "settings", None), "model", None), str) else ""
    session = getattr(agent, "session", None)
    sid = getattr(session, "active_session_id", "")
    sid = sid if isinstance(sid, str) else ""
    parent = sid.rsplit("_sub_", 1)[0]
    if not (_is_ledger(ledger) and ledger.entries) and sid:
        try:
            loaded = session.load_ledger(parent, model, include_subagents=True)
            if _is_ledger(loaded):
                ledger = loaded
        except Exception:
            pass
    if not _is_ledger(ledger):
        info("No usage recorded yet.")
        return CommandResult(handled=True)

    if a in ("--all", "all", "lifetime", "global"):
        return usage_log_summary(agent, project_only=False)
    if a in ("--project", "project"):
        return usage_log_summary(agent, project_only=True)

    if a in ("calls", "call", "detail", "details", "history", "-v", "--verbose"):
        print(f"\n  {GOLD}{BOLD}Detailed Call-by-Call API History{RST}")
        if hasattr(ledger, "render_call_history"):
            print(ledger.render_call_history())
        else:
            rows = [[f"#{e.id:02d}", e.tag, e.model, f"{e.usage.input:,}", f"{e.usage.output:,}", fmt_usd(e.cost, 5)] for e in ledger.entries]
            table(rows, ["#", "source", "model", "in", "out", "cost"])
        print()
        return CommandResult(handled=True)

    total = Decimal(str(ledger.total())) if hasattr(ledger, "total") else Decimal("0")
    header("Session cost", f"{sid or 'current session'}")
    rows_data: list[dict[str, Any]] = []
    if hasattr(ledger, "breakdown"):
        try:
            rows_data = [r for r in ledger.breakdown() if isinstance(r, dict)]
        except Exception:
            rows_data = []
    if not rows_data and hasattr(ledger, "tag_breakdown"):
        for src, g in ledger.tag_breakdown().items():
            rows_data.append({"source": src, "model": ", ".join(sorted(g.get("models", []))), "calls": g.get("calls", 0),
                              "input": g.get("input_tokens", 0), "output": g.get("output_tokens", 0),
                              "cache_read": g.get("cache_read_tokens", 0), "cache_write": g.get("cache_write_tokens", 0),
                              "cost": g.get("cost", 0)})
    if not rows_data:
        print(f"    {SLATE}No API calls yet in this session.{RST}\n")
        return CommandResult(handled=True)

    try:
        from axon.session.ledger import source_label
    except Exception:
        def source_label(s: str) -> str:
            return s
    table_rows = []
    for r in rows_data:
        label = r.get("label") or source_label(str(r.get("source", "main")))
        priced = r.get("priced", True)
        table_rows.append([
            f"{WHITE}{label}{RST}", f"{SLATE}{r.get('model', '')}{RST}", str(r.get("calls", 0)),
            fmt_tokens(r.get("input", 0)), fmt_tokens(r.get("output", 0)),
            f"{fmt_tokens(r.get('cache_read', 0))}/{fmt_tokens(r.get('cache_write', 0))}",
            f"{GOLD}{fmt_usd(r.get('cost', 0), 5)}{RST}" + ("" if priced else f" {DIM}(unpriced){RST}"),
        ])
    print()
    table(table_rows, ["Source", "Model", "Calls", "Input", "Output", "Cache r/w", "Cost"])

    calls = sum(int(r.get("calls", 0)) for r in rows_data)
    tok_in = sum(int(r.get("input", 0)) for r in rows_data)
    tok_out = sum(int(r.get("output", 0)) for r in rows_data)
    cache_read = sum(int(r.get("cache_read", 0)) for r in rows_data)
    sub_cost = sum(Decimal(str(r.get("cost", 0))) for r in rows_data if str(r.get("source", "")).startswith("subagent"))
    main_calls = sum(int(r.get("calls", 0)) for r in rows_data if str(r.get("source", "main")) == "main")
    from axon.session.store import is_user_prompt
    user_turns = sum(1 for m in getattr(agent.conversation, "messages", []) if isinstance(m, dict) and is_user_prompt(m)) if hasattr(agent, "conversation") else 0
    print()
    kv("Total", f"{GOLD}{BOLD}{fmt_usd(total, 5)}{RST}  {SLATE}{calls} calls · {fmt_tokens(tok_in)} in · {fmt_tokens(tok_out)} out{RST}")
    if tok_in:
        kv("Cache hit rate", f"{cache_read / tok_in * 100:.1f}% of input tokens read from cache")
    cache = prompt_cache_stats(agent)
    if cache["calls"]:
        warm = f"{MINT}warm{RST}" if cache["warm"] else f"{SLATE}cold{RST}"
        cause = f" · last miss: {cache['last_miss_cause']}" if cache["last_miss_cause"] else ""
        kv("Prompt cache (main)", f"{cache['hit_ratio'] * 100:.0f}% of input read from cache · {cache['hits']} hit / "
                                  f"{cache['misses']} miss call(s) · {warm}{SLATE}{cause}{RST}")
    try:
        cf = Decimal(str(ledger.uncached_counterfactual(model) if model else ledger.uncached_counterfactual()))
        saved = cf - total
        if saved > 0:
            kv("Cache savings", f"{MINT}{fmt_usd(saved, 5)}{RST} {SLATE}(would have cost {fmt_usd(cf, 5)} uncached){RST}")
    except Exception:
        pass
    turns = user_turns or main_calls
    if turns:
        kv("Per turn", f"{fmt_usd(total / turns, 5)} avg over {turns} turn(s)")
    if sub_cost:
        kv("Subagents", f"{fmt_usd(sub_cost, 5)} ({float(sub_cost / total * 100) if total else 0:.0f}% of total)")
    try:
        ws_ledger = session.load_workspace_ledger(model)
        if _is_ledger(ws_ledger):
            chats = getattr(ws_ledger, "chat_count", 0)
            kv("Workspace total", f"{fmt_usd(ws_ledger.total(), 4)} across {chats} session(s)")
    except Exception:
        pass
    hint("    /cost calls lists every API call · /pricing shows model rates")
    print()
    return CommandResult(handled=True)


def _pricing_table() -> dict[str, dict[str, float]]:
    try:
        from axon.providers.registry import PRICING
        return dict(PRICING)
    except Exception:
        return {}


@command("pricing", category=CAT, description="Per-model token prices (per 1M tokens)", aliases=("prices",),
         usage="/pricing [model filter]")
def handle_pricing(agent: Any, arg: str) -> CommandResult:
    table_data = _pricing_table()
    flt = arg.strip().lower()
    cur = agent.settings.model if isinstance(getattr(getattr(agent, "settings", None), "model", None), str) else ""
    rows = []
    for name, p in sorted(table_data.items(), key=lambda kv_: (kv_[0] != cur, kv_[0])):
        if flt and flt not in name.lower():
            continue
        if not isinstance(p, dict):
            continue
        mark = f"{MINT}●{RST} " if name == cur else "  "
        def f(k: str) -> str:
            v = p.get(k)
            return f"${float(v):.3f}" if isinstance(v, (int, float)) else f"{DARK_SLATE}—{RST}"
        rows.append([mark + f"{WHITE}{name}{RST}", f(("input")), f("output"), f("cache_read"), f("cache_write")])
    header("Model pricing", "USD per 1M tokens")
    if not rows:
        print(f"    {SLATE}No pricing data{(' matching ' + flt) if flt else ''}.{RST}\n")
        return CommandResult(handled=True)
    table(rows[:60], ["Model", "Input", "Output", "Cache read", "Cache write"])
    if len(rows) > 60:
        hint(f"    … {len(rows) - 60} more; filter with /pricing <text>")
    print()
    return CommandResult(handled=True)


# ─── AgentRouter FAQ ────────────────────────────────────────────────────────

def _faq_pricing_card() -> str:
    data = _pricing_table()
    lines = [f"  {GOLD}{BOLD}╭── AgentRouter Official Pricing Table & Live Caching Rates{RST}",
             f"  {GOLD}│{RST} {SLATE}{'Model':<20} {'Input/1M':>10} {'Output/1M':>10} {'Cache read':>11} {'Cache write':>12}{RST}"]
    try:
        from axon.providers.catalog import get_preset_by_id
        preset = get_preset_by_id("agentrouter")
        models = list(getattr(preset, "models", []) or [])
    except Exception:
        models = []
    for m in models:
        p = data.get(m)
        if not isinstance(p, dict):
            continue
        def f(k: str) -> str:
            v = p.get(k)
            return f"${float(v):.3f}" if isinstance(v, (int, float)) else "—"
        lines.append(f"  {GOLD}│{RST} {WHITE}{m:<20}{RST} {f('input'):>10} {f('output'):>10} {f('cache_read'):>11} {f('cache_write'):>12}")
    lines.append(f"  {GOLD}│{RST} {DIM}Spend = (uncached in × in-rate + cached in × cache-rate + out × out-rate) × group ratio{RST}")
    lines.append(f"  {GOLD}╰{'─' * 76}{RST}")
    return "\n".join(lines)


def _faq_cards() -> dict[str, tuple[str, str]]:
    def card(color: str, title: str, body: list[str]) -> str:
        out = [f"  {color}{BOLD}╭── {title}{RST}"]
        out += [f"  {color}│{RST} {ln}" for ln in body]
        out.append(f"  {color}╰{'─' * 76}{RST}")
        return "\n".join(out)
    return {
        "402": ("402 Budget Pool Quota Has Been Exhausted", card(GOLD, "402 Budget Pool Quota Has Been Exhausted", [
            f"{WHITE}Cause:{RST} Claude and GPT models share a daily budget pool, released in batches:",
            f"  {MINT}Batch 1:{RST} 00:00 Beijing Time {SLATE}(16:00 UTC){RST}",
            f"  {MINT}Batch 2:{RST} 08:00 Beijing Time {SLATE}(00:00 UTC){RST}",
            f"  {MINT}Batch 3:{RST} 16:00 Beijing Time {SLATE}(08:00 UTC){RST}",
            f"{WHITE}Fix:{RST} switch to {CYAN}/model deepseek-v4-flash{RST} (not in the shared pool), or wait for the next batch.",
        ])),
        "400": ("400 Content Blocked (Language Restriction)", card(ROSE, "400 Content Blocked (Language Restriction)", [
            f"{WHITE}Cause:{RST} the upstream filter only accepts {MINT}Chinese (中文), English, French, German, Russian{RST}.",
            "Other scripts in the prompt, files or tool output trigger HTTP 400.",
            f"{WHITE}Fix:{RST} translate or remove the unsupported text, then retry.",
        ])),
        "401": ("401 Unauthorized & Client Configuration", card(CYAN, "401 Unauthorized & Client Configuration", [
            f"{WHITE}Cause:{RST} missing or invalid AgentRouter token.",
            f"{WHITE}Fix:{RST} {WHITE}/keys agentrouter{RST} or set AGENTROUTER_API_KEY / AXON_API_KEY.",
            f"Client guide: {SLATE}https://ps.air-outer.com/docs/claude-code.html{RST}",
        ])),
        "sensitive": ("Sensitive Words Detected (Abuse Detection)", card(PURPLE, "Sensitive Words Detected (Abuse Detection)", [
            f"{WHITE}Cause:{RST} abuse detection flagged the conversation; the context stays flagged upstream.",
            f"{WHITE}Fix:{RST} {CYAN}/clear{RST} (or start a new session) and rephrase the request.",
        ])),
        "backup": ("Backup Domain Officially Launched", card(TEAL, "Backup Domain Officially Launched", [
            f"Primary: {WHITE}https://agentrouter.org{RST}   Backup: {CYAN}https://ps.air-outer.com{RST}",
            "Same API, tokens and console. Switch with /provider or base_url in ~/.axon/config.toml.",
        ])),
        "credit": ("Account Data: $25 Credit Claim vs Credit Limit Display", card(MINT, "Account Data: $25 Credit Claim vs Credit Limit Display", [
            "New accounts can claim a $25 credit at sign-in.",
            "'Credit limit 0 / balance $0' is normal for pay-as-you-go: check Wallet / Top-up for the real quota.",
        ])),
        "gpt6": ("GPT-6 Astra: Protocol & Function Tools Notice", card(LBLUE, "GPT-6 Astra: Protocol & Function Tools Notice", [
            f"Uses the OpenAI Responses API ({WHITE}/v1/responses{RST}).",
            f"With function tools on /v1/chat/completions do {ROSE}NOT{RST} send reasoning_effort (Axon strips it).",
        ])),
        "pricing": ("AgentRouter Official Pricing Table & Live Caching Rates", _faq_pricing_card()),
    }


_FAQ_KEYS = {
    "402": "402", "quota": "402", "budget": "402", "exhausted": "402", "batches": "402", "schedule": "402",
    "400": "400", "language": "400", "blocked": "400", "languages": "400",
    "401": "401", "auth": "401", "unauthorized": "401", "token": "401", "keys": "401",
    "sensitive": "sensitive", "words": "sensitive", "abuse": "sensitive",
    "backup": "backup", "domain": "backup", "china": "backup", "mirror": "backup",
    "credit": "credit", "balance": "credit", "wallet": "credit", "$25": "credit",
    "gpt6": "gpt6", "astra": "gpt6", "responses": "gpt6",
    "pricing": "pricing", "prices": "pricing", "rates": "pricing", "cost": "pricing",
}


@command("faq", category=CAT, description="AgentRouter FAQ and error troubleshooting", aliases=("faqs",),
         usage="/faq [402|400|401|sensitive|backup|credit|gpt6|pricing]")
def handle_faq(agent: Any, arg: str) -> CommandResult:
    topic = arg.strip().lower()
    cards = _faq_cards()
    print(f"\n  {CYAN}{BOLD}AgentRouter Frequently Asked Questions (FAQ){RST}\n")
    if topic in _FAQ_KEYS:
        print(cards[_FAQ_KEYS[topic]][1])
    else:
        for _, content in cards.values():
            print(content)
            print()
    print(f"\n  {DARK_SLATE}Topics: /faq 402 · 400 · 401 · sensitive · backup · credit · gpt6 · pricing{RST}\n")
    return CommandResult(handled=True)


def usage_log_summary(agent: Any, project_only: bool, since: float | None = None) -> CommandResult:
    """Aggregate ~/.axon/usage.jsonl (one record per LLM call) by project, model and source."""
    try:
        from axon import storage
        recs = storage.read_jsonl(storage.user_file("usage.jsonl"))
        key = storage.project_key(agent.settings.workspace)
    except Exception as e:
        err(f"Usage log unavailable: {e}")
        return CommandResult(handled=True)
    if project_only:
        recs = [r for r in recs if r.get("project") == key]
    if since is not None:
        from datetime import datetime
        def ts(r: dict[str, Any]) -> float:
            try:
                return datetime.fromisoformat(str(r.get("ts"))).timestamp()
            except ValueError:
                return 0.0
        recs = [r for r in recs if ts(r) >= since]
    header("Usage log", f"{'this project' if project_only else 'all projects'} · {len(recs)} calls · ~/.axon/usage.jsonl")
    if not recs:
        print(f"    {SLATE}No recorded calls.{RST}\n")
        return CommandResult(handled=True)
    def agg(field: str) -> list[list[str]]:
        groups: dict[str, list[float]] = {}
        for r in recs:
            k = str(r.get(field) or "?")
            if field == "project" and r.get("workspace"):
                k = str(r.get("workspace"))
            u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
            g = groups.setdefault(k, [0, 0, 0, 0, 0.0])
            g[0] += 1
            g[1] += int(u.get("input", 0) or 0)
            g[2] += int(u.get("output", 0) or 0)
            g[3] += int(u.get("cache_read", 0) or 0)
            g[4] += float(r.get("cost", 0) or 0)
        rows = sorted(groups.items(), key=lambda kv_: -kv_[1][4])
        return [[f"{WHITE}{k[-40:]}{RST}", str(int(g[0])), fmt_tokens(g[1]), fmt_tokens(g[2]),
                 f"{g[3] / g[1] * 100:.0f}%" if g[1] else "-", f"{GOLD}{fmt_usd(g[4], 4)}{RST}"] for k, g in rows[:15]]
    total = sum(float(r.get("cost", 0) or 0) for r in recs)
    kv("Total", f"{GOLD}{BOLD}{fmt_usd(total, 4)}{RST}")
    if not project_only:
        section("By project")
        table(agg("project"), ["Project", "Calls", "In", "Out", "Cache", "Cost"])
    section("By model")
    table(agg("model"), ["Model", "Calls", "In", "Out", "Cache", "Cost"])
    section("By source")
    table(agg("source"), ["Source", "Calls", "In", "Out", "Cache", "Cost"])
    print()
    return CommandResult(handled=True)
