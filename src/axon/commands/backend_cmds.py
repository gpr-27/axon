"""Terminal views over the plain-data features: /trail /timeline /ctxmap /search and /review hunks.

The logic lives in axon.features.{trail,timeline,ctxmap,session_search,review} and returns data; the
functions here only print it, so a web app can call the same data functions.
"""
from __future__ import annotations

from typing import Any

from axon.commands.common import (
    err, fmt_tokens, header, info, kv, section, typed_hint, typed_only, usage, width, ws_of,
)
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, CYAN, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

CAT = "Context & Cost"


def _traces(agent: Any) -> list[Any]:
    from axon.commands.agent_cmds import _traces as session_traces
    return session_traces(agent)


# ─── /trail ──────────────────────────────────────────────────────────────────

def print_trail(t: dict[str, Any]) -> None:
    header(f"Tool call {t['n']} of {t['total']}: {t['name']}", f"turn {t['turn']} · step {t['step']}")
    why = t["rationale"]
    kv("Why", f"{why['text']}  ({why['source']})" if why else "not recorded")
    inp = t["input"]
    if isinstance(inp, dict) and inp:
        for k, v in list(inp.items())[:6]:
            kv("Input " + str(k), " ".join(str(v).split())[: max(20, width() - 30)])
    else:
        kv("Input", "not recorded")
    p = t["permission"]
    if p:
        how = p["source"] + (f" · rule {p['rule']}" if p.get("rule") else "") + (f" · mode {p['mode']}" if p.get("mode") else "")
        kv("Permission", f"{p['outcome']}  ({how})")
        kv("", p["reason"], color=SLATE)
        if p.get("approval"):
            kv("You answered", p["approval"])
    else:
        kv("Permission", "not recorded")
    for h in t["hooks"]:
        kv("Hook", f"{h.get('event', '')} ran {h.get('ran', 0)}" + (" and blocked" if h.get("blocked") else ""))
    r = t["result"]
    if r:
        stats = ", ".join(x for x in (f"{r['lines']} lines" if r.get("lines") is not None else "",
                                      f"~{fmt_tokens(r['est_tokens'])} tokens" if r.get("est_tokens") is not None else "",
                                      f"{r['duration_s']:.2f}s" if r.get("duration_s") is not None else "") if x)
        kv("Result", ("error" if r["is_error"] else "ok") + (f" · {stats}" if stats else ""), color=ROSE if r["is_error"] else MINT)
        for ln in str(r["preview"]).strip().splitlines()[:4]:
            print(f"      {DARK_SLATE}{ln[: width() - 8]}{RST}")
    else:
        kv("Result", "not recorded")
    kv("Then", t["next"]["summary"])
    print()


@command("trail", category=CAT, description="Decision trail of a tool call: why, what allowed it, result, what came next",
         usage="/trail [n]   tool call number n of this session (default: the last one)")
def handle_trail(agent: Any, arg: str) -> CommandResult:
    from axon.features.trail import list_calls, trail_for
    traces = _traces(agent)
    if not list_calls(traces):
        info("No tool calls recorded yet.")
        return CommandResult(handled=True)
    a = arg.strip()
    if a and not a.isdigit():
        usage("/trail [n]")
        return CommandResult(handled=True)
    t = trail_for(traces, int(a) if a else None)
    if t is None:
        err(f"No tool call {a}. This session has {len(list_calls(traces))}.")
        return CommandResult(handled=True)
    print_trail(t)
    return CommandResult(handled=True)


# ─── /timeline ───────────────────────────────────────────────────────────────

@command("timeline", category=CAT, description="Steps of a turn as a bar chart: time, tokens, cost, cache-miss cause",
         usage="/timeline [turn]   default: the last turn")
def handle_timeline(agent: Any, arg: str) -> CommandResult:
    from axon.features.timeline import render_timeline, turn_timeline
    traces = _traces(agent)
    if not traces:
        info("No turn traces yet - run a prompt first.")
        return CommandResult(handled=True)
    a = arg.strip()
    if a and not a.isdigit():
        usage("/timeline [turn]")
        return CommandResult(handled=True)
    trace = traces[-1] if not a else next((t for t in traces if t.turn == int(a)), None)
    if trace is None:
        err(f"No turn {a} (have {len(traces)}).")
        return CommandResult(handled=True)
    lines = render_timeline(turn_timeline(trace), width=min(width(), 110))
    if not lines:
        info(f"Turn {trace.turn} has no recorded steps.")
        return CommandResult(handled=True)
    print()
    for ln in lines:
        print(ln)
    print()
    return CommandResult(handled=True)


# ─── /ctxmap ─────────────────────────────────────────────────────────────────

@command("ctxmap", category=CAT, description="What fills the context window now, and what could be dropped",
         usage="/ctxmap              items by size with share, and what can be dropped\n/ctxmap drop <id>     drop a tool result from context (full text stays on disk)")
def handle_ctxmap(agent: Any, arg: str) -> CommandResult:
    from axon.features.ctxmap import ctxmap_data, drop_result
    words = arg.split()
    if words and words[0].lower() == "drop":
        if len(words) < 2:
            usage("/ctxmap drop <id>")
            return CommandResult(handled=True)
        res = drop_result(agent, words[1])
        (info if res["ok"] else err)(res["message"])
        return CommandResult(handled=True)
    if words:
        usage("/ctxmap\n/ctxmap drop <id>")
        return CommandResult(handled=True)
    d = ctxmap_data(agent)
    header("Context map", f"{fmt_tokens(d['total'])} of {fmt_tokens(d['window'])} tokens (estimates)")
    for kind, tok in sorted(d["groups"].items(), key=lambda kv_: -kv_[1]):
        print(f"    {SLATE}{kind:<12}{RST} {WHITE}{fmt_tokens(tok):>8}{RST}  {SLATE}{tok / d['total'] * 100 if d['total'] else 0:>3.0f}%{RST}")
    items = sorted(d["items"], key=lambda i: -i["tokens"])[:15]
    section("Largest items", TEAL)
    for i in items:
        mark = f"{MINT}drop{RST}" if i["droppable"] else f"{GOLD}pinned{RST}" if i["pinned"] else f"{CYAN}compact{RST}" if i["compactable"] else f"{DARK_SLATE}keep{RST}"
        print(f"    {SLATE}{i['id'][:14]:<14}{RST} {WHITE}{i['label'][:34]:<34}{RST} {fmt_tokens(i['tokens']):>7} {SLATE}{i['share'] * 100:>3.0f}%{RST}  {mark}")
    if d["droppable_tokens"]:
        print(f"\n  {SLATE}About {fmt_tokens(d['droppable_tokens'])} tokens sit in tool results that can be dropped.{RST}"
              f"{SLATE}{typed_only(' /ctxmap drop <id> removes one from context and keeps it on disk.')}{RST}")
    print()
    return CommandResult(handled=True)


# ─── /search ─────────────────────────────────────────────────────────────────

@command("search", category="Session", description="Full-text search of this project's session transcripts (/find searches file names)",
         usage="/search <text>          this project\n/search --all <text>    every project")
def handle_search(agent: Any, arg: str) -> CommandResult:
    from axon.features.session_search import search_sessions
    words = arg.split()
    all_projects = "--all" in words
    query = " ".join(w for w in words if w != "--all").strip()
    if not query:
        usage("/search <text>\n/search --all <text>")
        return CommandResult(handled=True)
    res = search_sessions(ws_of(agent), query, all_projects=all_projects)
    header("Search", f"“{query}” · {res['files']} transcript(s)" + (" · all projects" if all_projects else ""))
    if not res["hits"]:
        print(f"\n  {SLATE}No matches.{RST}")
    for h in res["hits"]:
        proj = f"{h['project'][-24:]} " if all_projects else ""
        print(f"\n  {CYAN}{proj}{h['session']}{RST} {SLATE}turn {h['turn']} · {h['type']}{RST}\n    {h['snippet']}")
    if res["truncated"]:
        print(f"\n  {SLATE}Showing the first {len(res['hits'])} matches; narrow the search for more.{RST}")
    if res["skipped_encrypted"]:
        names = ", ".join(e["session"] for e in res["skipped_encrypted"][:5])
        print(f"\n  {GOLD}Skipped {len(res['skipped_encrypted'])} encrypted transcript(s) ({names}); they cannot be searched.{RST}")
    print()
    return CommandResult(handled=True)


# ─── /review hunks (called from /review) ─────────────────────────────────────

def _show(lines: list[str], sign: str, color: str, cap: int = 6) -> None:
    for ln in lines[:cap]:
        print(f"      {color}{sign} {ln[: width() - 10]}{RST}")
    if len(lines) > cap:
        print(f"      {DARK_SLATE}  … {len(lines) - cap} more line(s){RST}")


def review_hunks(agent: Any, arg: str) -> CommandResult:
    """``/review hunks`` | ``accept <id>|all`` | ``reject <id>``: per-hunk review of this session's edits."""
    from axon.features.review import accept_hunk, list_hunks, reject_hunk
    cp = getattr(agent, "checkpoints", None)
    if cp is None:
        info("No checkpoints in this session.")
        return CommandResult(handled=True)
    words = arg.split()
    if words and words[0].lower() in ("accept", "reject"):
        if len(words) < 2:
            usage(f"/review hunks {words[0].lower()} <id>")
            return CommandResult(handled=True)
        if words[0].lower() == "accept" and words[1].lower() == "all":
            pending = list_hunks(cp, include_accepted=False)
            for h in pending:
                accept_hunk(cp, h.id)
            info(f"Accepted {len(pending)} hunk(s).")
            return CommandResult(handled=True)
        res = (accept_hunk if words[0].lower() == "accept" else reject_hunk)(cp, words[1])
        (info if res.ok else err)(res.message)
        return CommandResult(handled=True)
    hunks = list_hunks(cp)
    if not hunks:
        info("Axon has not changed any files in this session.")
        return CommandResult(handled=True)
    header("Changes by hunk", f"{sum(1 for h in hunks if not h.accepted)} to review · {sum(1 for h in hunks if h.accepted)} accepted")
    current = None
    for h in hunks:
        if h.rel != current:
            current = h.rel
            print(f"\n  {WHITE}{BOLD}{h.rel}{RST}")
        state = f"{MINT}accepted{RST}" if h.accepted else f"{GOLD}to review{RST}"
        where = {"created": "new file", "deleted": "deleted", "binary": "binary file"}.get(h.kind, f"line {h.new_start}")
        print(f"    {CYAN}{h.id}{RST} {SLATE}{where}{RST} {state}")
        _show(h.removed, "-", ROSE)
        _show(h.added, "+", MINT)
    print()
    typed_hint("  /review hunks accept <id|all> · /review hunks reject <id>")
    return CommandResult(handled=True)
