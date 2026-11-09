"""
The agent list and live transcripts: what you see while subagents run, and from /subagents and /tasks.

Two views. The list has one aligned row per agent (status mark, name, what it is doing, elapsed time, tool
count, tokens): Up/Down select, Enter opens, x stops a running agent or clears a finished row, a digit opens
that agent. The transcript follows one agent live: what you type goes to that agent, Esc goes back, Up/Down
scroll, Left/Right change agent, Ctrl+O expands tool output and Ctrl+X stops it. A finished agent can be
opened the same way, and a message resumes it with its history.

During a fan-out (the monitor) typing on the list queues a note for the main agent and Esc stops the turn; from
the prompt (the console, see agent_panel.py) Esc goes back to the prompt. Slash commands work in both.

Every status has its own glyph (never colour alone), every line fits the given width, a frame never gets taller
than the window, and only rows that changed are rewritten. Works on Windows consoles, macOS and Linux.
"""
from __future__ import annotations

import time
from typing import Any

from axon import platform as plat
from axon.ui.picker import LiveFrame, _write
from axon.ui.theme import (AMBER, BOLD, CYAN, DARK_SLATE, GOLD, ITALIC, LAVENDER, MINT, ROSE, RST, SLATE, WHITE,
                           sanitize, strip_ansi, wrap_ansi)

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag

# status -> (glyph, colour, label, progress-strip fill)
_STATUS: dict[str, tuple[str, str, str, str]] = {
    "completed": ("✓", MINT, "done", "━"),
    "error": ("✗", ROSE, "failed", "╳"),
    "exhausted": ("!", AMBER, "step limit", "╍"),
    "cancelled": ("⊘", SLATE, "cancelled", "─"),
    "running": ("▶", CYAN, "running", "┅"),
    "pending": ("○", SLATE, "queued", "┄"),
}
_FINISHED = ("completed", "exhausted", "error", "cancelled")
_PROMPT_LINES = 3         # the task as shown above a transcript (Ctrl+O shows all of it)
_DEFAULT_HEIGHT = 40      # frame height when the caller does not know the window size
_WIDE = 64                # from this width a row keeps the activity on its own line of the table


def _st(t: Any) -> tuple[str, str, str, str]:
    return _STATUS.get(str(getattr(t, "status", "pending")), _STATUS["pending"])


def _one_line(text: Any) -> str:
    """*text* on one row, without terminal escapes: titles, activity, results and errors are model / tool text."""
    return " ".join(sanitize(strip_ansi(str(text or ""))).split())


def _tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _dur(s: float) -> str:
    s = max(0.0, s)
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"


def _elapsed(t: Any, now: float) -> float:
    start = float(getattr(t, "start_time", 0.0) or 0.0)
    if getattr(t, "status", "") == "running" and start:
        return now - start
    return float(getattr(t, "elapsed_s", 0.0) or 0.0)


def _wall(tasks: list[Any], now: float) -> float:
    starts = [float(getattr(t, "start_time", 0.0) or 0.0) for t in tasks]
    starts = [s for s in starts if s]
    if not starts:
        return max((_elapsed(t, now) for t in tasks), default=0.0)
    if any(getattr(t, "status", "") in ("running", "pending") for t in tasks):
        return now - min(starts)
    ends = [float(getattr(t, "start_time", 0.0) or 0.0) + float(getattr(t, "elapsed_s", 0.0) or 0.0) for t in tasks]
    return max(ends) - min(starts)


def _shorten(title: str, n: int) -> str:
    """Cut *title* to *n* cells at a word boundary when that keeps most of it, else hard-cut."""
    if n <= 0:
        return ""
    if plat.str_width(title) <= n:
        return title
    out = ""
    for word in title.split():
        cand = f"{out} {word}".strip()
        if plat.str_width(cand) + 1 > n:
            break
        out = cand
    if out and plat.str_width(out) >= n * 0.6:
        return out + "…"
    return plat.truncate_ansi(title, n)


def _wrap(text: str, width: int) -> list[str]:
    return [r for para in str(text).split("\n") for r in (wrap_ansi(para, max(8, width)) if para.strip() else [])]


def _fit(line: str, width: int) -> str:
    return plat.truncate_ansi(line, width) if plat.str_width(line) > width else line


def _clock(s: float) -> str:
    """Whole seconds only: a table that ticks ten times a second is not calm."""
    s = int(max(0.0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _stats(t: Any, now: float) -> str:
    """One line of numbers for the top of a transcript."""
    steps, max_steps = int(getattr(t, "steps", 0) or 0), int(getattr(t, "max_steps", 0) or 0)
    # A step is one model call. While running, show it against the step limit; once finished, the limit is
    # irrelevant: "2 steps", not "step 2 · limit 25".
    running = getattr(t, "status", "") in ("running", "pending")
    if running and max_steps:
        step_txt = f"{SLATE}step{RST} {WHITE}{steps}{RST} {SLATE}· limit {max_steps}{RST}"
    else:
        step_txt = f"{WHITE}{steps}{RST} {SLATE}step{'s' if steps != 1 else ''}{RST}"
    parts = [step_txt, f"{WHITE}{_clock(_elapsed(t, now))}{RST}"]
    retries, resumes = int(getattr(t, "retries", 0) or 0), int(getattr(t, "resumes", 0) or 0)
    runs = int(getattr(t, "runs", 1) or 1)
    if runs > 1:
        parts.append(f"{CYAN}run {runs}{RST}")
    if retries:
        parts.append(f"{AMBER}⟳ {retries} retr{'ies' if retries != 1 else 'y'}{RST}")
    if resumes:
        parts.append(f"{CYAN}↻ {resumes} resume{'s' if resumes != 1 else ''}{RST}")
    calls = int(getattr(t, "tool_calls", 0) or 0)
    if calls:
        parts.append(f"{WHITE}{calls}{RST} {SLATE}tool{'s' if calls != 1 else ''}{RST}")
    toks = int(getattr(t, "tokens_consumed", 0) or 0)
    if toks:
        parts.append(f"{WHITE}{_tok(toks)}{RST} {SLATE}tok{RST}")
    in_t, out_t = int(getattr(t, "input_tokens", 0) or 0), int(getattr(t, "output_tokens", 0) or 0)
    if in_t or out_t:
        parts.append(f"{SLATE}{_tok(in_t)} in · {_tok(out_t)} out{RST}")
    cost = float(getattr(t, "cost", 0.0) or 0.0)
    if cost:
        parts.append(f"{SLATE}${cost:.4f}{RST}")
    return f" {DARK_SLATE}·{RST} ".join(parts)


def _activity(t: Any) -> str:
    act = _one_line(getattr(t, "activity", ""))
    if not act:
        logs = getattr(t, "live_logs", None) or []
        act = _one_line(logs[-1]) if logs else ""
    return act or ("waiting to start" if getattr(t, "status", "") == "pending" else "starting…")


def _doing(t: Any) -> tuple[str, str]:
    """(what to show in the "doing" column, its colour): the activity while running, the outcome afterwards."""
    status = str(getattr(t, "status", "pending"))
    if status == "error":
        return _one_line(getattr(t, "error_msg", "") or "failed (no reason reported)"), ROSE
    if status == "completed":
        return _first_result_line(t), SLATE
    if status == "exhausted":
        return f"stopped at the step limit ({getattr(t, 'steps', 0)}/{getattr(t, 'max_steps', 0)})", AMBER
    if status == "cancelled":
        return "stopped by you", SLATE
    act = _activity(t)
    return act, AMBER if act.lower().startswith(("retrying", "resuming")) else SLATE


def _label(t: Any) -> str:
    """How a row is addressed: the subagent's number, or the id of a shell or background turn."""
    return str(getattr(t, "label", "") or getattr(t, "index", "") or getattr(t, "id", ""))


def _display_name(t: Any) -> str:
    title = _one_line(getattr(t, "title", ""))
    title = title[:-3] + "…" if title.endswith("...") else title
    name = _one_line(getattr(t, "name", ""))
    return f"{name} · {title}" if name and name.lower() != title.lower() else title


def _pad(text: str, cells: int) -> str:
    """*text* cut to and padded out to exactly *cells* columns."""
    cut = _shorten(text, cells)
    return cut + " " * max(0, cells - plat.str_width(cut))


def _first_result_line(t: Any) -> str:
    rows = [ln for ln in str(getattr(t, "result_text", "") or "").splitlines() if ln.strip()]
    body = [ln for ln in rows if not ln.lstrip().startswith("#")] or rows
    return _one_line(body[0].strip("#*-> ")) if body else "finished"


# ─── The list ──────────────────────────────────────────────────────────────

_NUM_T, _NUM_C, _NUM_K = 7, 5, 6       # time, tools and tokens columns (right-aligned, always there)


def _numbers(t: Any, now: float) -> str:
    calls = getattr(t, "tool_calls", 0)
    toks = getattr(t, "tokens_consumed", 0)
    c = "-" if calls is None else str(int(calls or 0))
    k = "-" if not toks else _tok(int(toks))
    return f"{WHITE}{_clock(_elapsed(t, now)):>{_NUM_T}}{RST} {SLATE}{c:>{_NUM_C}} {k:>{_NUM_K}}{RST}"


def _table_layout(tasks: list[Any], width: int) -> dict[str, int]:
    """Column widths for this set of rows: stable while the agents run, whatever their activity says."""
    id_w = max([plat.str_width(_label(t)) for t in tasks] or [1])
    right = _NUM_T + _NUM_C + _NUM_K + 2
    rest = width - 6 - id_w - 1 - right - 1       # "  ❯ ▶ " + id + " " ... " " + numbers
    if width < _WIDE:
        return {"id": id_w, "name": max(6, rest), "type": 0, "doing": 0, "right": right}
    names = max([plat.str_width(_display_name(t)) for t in tasks] or [8])
    type_w = 0
    if width >= 110:
        type_w = min(16, max([plat.str_width(_one_line(getattr(t, "agent_type", ""))) for t in tasks] or [0]))
    avail = rest - (type_w + 2 if type_w else 0)
    name_w = max(14, min(names, int(avail * 0.5)))
    return {"id": id_w, "name": name_w, "type": type_w, "doing": max(8, avail - name_w - 2), "right": right}


def _table_head(lay: dict[str, int], width: int, noun: str = "agent") -> str:
    left = " " * (6 + lay["id"] + 1) + _pad(noun, lay["name"])
    if lay["type"]:
        left += "  " + _pad("type", lay["type"])
    if lay["doing"]:
        left += "  " + _pad("doing", lay["doing"])
    row = f"{left} {'time':>{_NUM_T}} {'tools':>{_NUM_C}} {'tok':>{_NUM_K}}"
    return _fit(f"{DARK_SLATE}{row}{RST}", width)


def _table_row(t: Any, lay: dict[str, int], width: int, selected: bool, now: float) -> list[str]:
    glyph, color, _, _ = _st(t)
    cursor = f"{CYAN}{BOLD}❯{RST}" if selected else " "
    weight = f"{WHITE}{BOLD}" if selected else WHITE
    label = f"{_label(t):>{lay['id']}}"
    doing, tone = _doing(t)
    line = f"  {cursor} {color}{glyph}{RST} {SLATE}{label}{RST} {weight}{_pad(_display_name(t), lay['name'])}{RST}"
    if lay["type"]:
        line += f"  {LAVENDER}{_pad(_one_line(getattr(t, 'agent_type', '')), lay['type'])}{RST}"
    if lay["doing"]:
        line += f"  {tone}{_pad(doing, lay['doing'])}{RST}"
    rows = [_fit(f"{line} {_numbers(t, now)}", width)]
    if not lay["doing"]:                          # narrow: the activity gets its own line, under the name
        indent = " " * (6 + lay["id"] + 1)
        rows.append(_fit(f"{indent}{tone}{_shorten(doing, width - len(indent) - 1)}{RST}", width))
    return rows


def agent_table(tasks: list[Any], width: int, selected: int | None = None, now: float | None = None,
                max_rows: int | None = None, noun: str = "agent") -> list[str]:
    """The aligned agent table (head + rows); also used under the prompt. Lists a window of at most *max_rows*
    screen rows around *selected* and says how many are out of view."""
    now = time.time() if now is None else now
    lay = _table_layout(tasks, width)
    per = 2 if not lay["doing"] else 1
    fit = len(tasks) if max_rows is None else max(1, (max_rows - 1) // per)
    first = 0
    if len(tasks) > fit:
        fit = max(1, ((max_rows or 2) - 2) // per)      # one line says how many are hidden
        first = min(max(0, (selected or 0) - fit // 2), len(tasks) - fit)
    lines = [_table_head(lay, width, noun)]
    for i, t in enumerate(tasks[first:first + fit], start=first):
        lines += _table_row(t, lay, width, i == selected, now)
    hidden = len(tasks) - fit
    if hidden > 0:
        above, below = first, len(tasks) - first - fit
        bits = ([f"↑ {above} above"] if above else []) + ([f"↓ {below} below"] if below else [])
        lines.append(_fit(f"    {DARK_SLATE}{' · '.join(bits)}{RST}", width))
    return lines


# ─── Chrome ────────────────────────────────────────────────────────────────

def _header(tasks: list[Any], width: int, now: float, title: str = "Subagents") -> str:
    counts = {k: sum(1 for t in tasks if getattr(t, "status", "pending") == k) for k in _STATUS}
    shown = [k for k in ("running", "completed", "error", "exhausted", "cancelled", "pending") if counts[k]]
    total_tok = sum(int(getattr(t, "tokens_consumed", 0) or 0) for t in tasks)
    right = f"{SLATE}{_tok(total_tok) + ' tok · ' if total_tok else ''}{_clock(_wall(tasks, now))}{RST}"
    left = ""
    for long in (True, False):
        chips = "  ".join(f"{_STATUS[k][1]}{_STATUS[k][0]} {counts[k]}{' ' + _STATUS[k][2] if long else ''}{RST}" for k in shown)
        left = f"  {GOLD}{BOLD}◆ {title}{RST}   {chips}"
        gap = width - plat.str_width(left) - plat.str_width(right)
        if gap >= 2:
            return left + " " * gap + right
    return _fit(left, width)


def _footer(items: list[tuple[str, str]], width: int) -> list[str]:
    rows: list[str] = []
    cur = " "
    for key, desc in items:
        piece = f" {WHITE}{BOLD}{key}{RST} {DARK_SLATE}{desc}{RST}"
        if plat.str_width(cur) + plat.str_width(piece) + 1 > width and cur.strip():
            rows.append(cur)
            cur = " "
        cur += piece + " "
    rows.append(cur.rstrip())
    return [_fit(r.rstrip(), width) for r in rows]


def _x_hint(t: Any) -> tuple[str, str]:
    return ("x", "stop") if getattr(t, "status", "") in ("running", "pending") else ("x", "clear")


def _list_footer(tasks: list[Any], selected: int, width: int, console: bool) -> list[str]:
    items = [("↑↓", "select"), ("⏎", "open")]
    if tasks:
        items.append(_x_hint(tasks[min(selected, len(tasks) - 1)]))
    items += [("esc", "back to the prompt")] if console else [("q", "queue"), ("esc", "stop the turn"), ("/btw", "ask aside")]
    return _footer(items, width)


# ─── The transcript ────────────────────────────────────────────────────────

def _title_line(t: Any, width: int, indent: str = "  ") -> str:
    glyph, color, _, _ = _st(t)
    prefix = f"{indent}{CYAN}◂{RST} {color}{BOLD}{glyph}{RST} {WHITE}{BOLD}{_label(t)}{RST} "
    a_type, model = _one_line(getattr(t, "agent_type", "")), _one_line(getattr(t, "model", ""))
    right = ""
    for a, m in ((a_type, model), (a_type, ""), ("", "")):
        right = (f"{LAVENDER}{a}{RST}" if a else "") + (f" {DARK_SLATE}·{RST} " if a and m else "") + (f"{SLATE}{m}{RST}" if m else "")
        if width - plat.str_width(prefix) - plat.str_width(right) - 2 >= min(24, plat.str_width(_display_name(t))):
            break
    room = width - plat.str_width(prefix) - plat.str_width(right) - (2 if right else 0)
    left = prefix + f"{WHITE}{BOLD}{_shorten(_display_name(t), max(4, room))}{RST}"
    gap = width - plat.str_width(left) - plat.str_width(right)
    return _fit(left + " " * max(2, gap) + right if right else left, width)


def _kv(label: str, value_rows: list[str], width: int, color: str = WHITE) -> list[str]:
    out = []
    for i, r in enumerate(value_rows or [""]):
        lab = f"{SLATE}{label:<7}{RST}" if i == 0 else " " * 7
        out.append(_fit(f"    {lab}{color}{r}{RST}", width))
    return out


def _block_text(block: Any, *names: str) -> Any:
    for n in names:
        v = block.get(n) if isinstance(block, dict) else getattr(block, n, None)
        if v:
            return v
    return ""


def _result_body(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(_block_text(c, "text") or "") if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def conversation_entries(messages: list[Any]) -> list[dict[str, Any]]:
    """Timeline entries rebuilt from a saved conversation (a subagent from an earlier run has no live timeline)."""
    out: list[dict[str, Any]] = []

    def result(body: Any, err: bool) -> None:
        raw = _result_body(body)
        first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "(no output)")
        out.append({"kind": "result", "text": first[:140], "is_error": err, "full": raw})

    for m in messages or []:
        role, content = str(m.get("role", "")), m.get("content", "")
        if role == "tool":
            result(content, False)
            continue
        if isinstance(content, str):
            if role == "assistant" and content.strip():
                out.append({"kind": "text", "text": content, "is_error": False})
            elif role == "user" and out and content.strip():      # the first user message is the task, shown above
                out.append({"kind": "note", "text": f"you: {content}", "is_error": False})
        elif isinstance(content, list):
            for b in content:
                kind = str(_block_text(b, "type") or type(b).__name__)
                if kind in ("text", "TextBlock"):
                    text = str(_block_text(b, "text"))
                    if role == "assistant" and text.strip():
                        out.append({"kind": "text", "text": text, "is_error": False})
                    elif role == "user" and out and text.strip():
                        out.append({"kind": "note", "text": f"you: {text}", "is_error": False})
                elif kind in ("tool_use", "ToolUseBlock"):
                    inp = _block_text(b, "input")
                    from axon.agent.subagent import _short_args
                    out.append({"kind": "tool", "text": f"{_block_text(b, 'name')} {_short_args(inp, 120)}".rstrip(), "is_error": False})
                elif kind in ("tool_result", "ToolResultBlock"):
                    result(_block_text(b, "content"), bool(_block_text(b, "is_error")))
        for call in m.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            out.append({"kind": "tool", "text": f"{fn.get('name', 'tool')} {_one_line(fn.get('arguments', ''))[:120]}".rstrip(), "is_error": False})
    return out


def _entries(t: Any) -> list[dict[str, Any]]:
    timeline = list(getattr(t, "timeline", None) or [])
    if timeline:
        return timeline
    conv = getattr(t, "conversation", None)
    return conversation_entries(list(getattr(conv, "messages", None) or []))


def _rows_for(text: str, room: int) -> list[str]:
    clean = sanitize(strip_ansi(str(text or "")))
    return _wrap(clean, room) or [""]


def transcript_lines(t: Any, width: int, expanded: bool = False) -> list[str]:
    """The whole transcript as display lines (the caller shows a window of them)."""
    own = getattr(t, "transcript", None)
    if callable(own):                       # a shell or a background turn: its output
        return [_fit(f"    {SLATE}{sanitize(strip_ansi(str(ln)))}{RST}", width) for ln in own()] or [f"    {SLATE}(no output yet){RST}"]
    entries = _entries(t)
    room = max(8, width - 6)
    lines: list[str] = []
    if not entries:
        for log in (list(getattr(t, "live_logs", None) or [])[-8:] or ["(starting…)"]):
            lines.append(_fit(f"    {DARK_SLATE}│{RST} {WHITE}{_one_line(log)}{RST}", width))
    last_text = ""
    for e in entries:
        kind, raw, err = str(e.get("kind", "")), str(e.get("text", "") or ""), bool(e.get("is_error"))
        text = _one_line(raw)
        if kind == "step":
            head = f"── {text or 'Step'} "
            lines.append(_fit(f"    {DARK_SLATE}{head}{'─' * max(0, min(60, room) - plat.str_width(head))}{RST}", width))
        elif kind == "text":
            last_text = raw.strip()
            lines += [_fit(f"    {WHITE}{r}{RST}", width) for r in _rows_for(raw, room)]
        elif kind == "tool":
            lines.append(_fit(f"    {CYAN}▸{RST} {WHITE}{text}{RST}", width))
        elif kind == "result":
            color = ROSE if err else SLATE
            full = str(e.get("full") or raw)
            body = [ln for ln in sanitize(strip_ansi(full)).splitlines() if ln.strip()]
            if expanded and len(body) > 1:
                lines += [_fit(f"      {color}{'↳' if i == 0 else ' '} {ln}{RST}", width) for i, ln in enumerate(body)]
            else:
                first = _one_line(body[0]) if body else "(no output)"
                more = f" ({len(body)} lines)" if len(body) > 1 else ""
                lines.append(_fit(f"      {color}↳ {plat.truncate_ansi(first, max(8, room - 4 - len(more)))}{RST}{DARK_SLATE}{more}{RST}", width))
        elif kind == "thinking":
            rows = _wrap(text, room - 2)
            shown = rows if expanded else rows[:2]
            if len(rows) > len(shown) and shown:
                shown[-1] = plat.truncate_ansi(shown[-1] + " …", room - 2)
            lines.extend(_fit(f"      {DARK_SLATE}{ITALIC}{'∴ ' if i == 0 else '  '}{r}{RST}", width) for i, r in enumerate(shown))
        elif kind == "note":
            lines.extend(_fit(f"    {MINT}{'›' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_rows_for(raw, room)))
        elif kind == "delivered":
            lines.append(_fit(f"    {MINT}✓ {text}{RST}", width))
        elif kind == "retry":
            lines.extend(_fit(f"    {AMBER}{'⟳' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:2]))
        elif kind == "resume":
            lines.extend(_fit(f"    {CYAN}{'↻' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:2]))
        elif kind == "error":
            lines.extend(_fit(f"    {ROSE}{'✗' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:4]))
        else:
            lines.append(_fit(f"    {DARK_SLATE}│{RST} {WHITE}{text}{RST}", width))
    status = str(getattr(t, "status", ""))
    report = sanitize(strip_ansi(str(getattr(t, "result_text", "") or ""))).strip()
    if status in ("completed", "exhausted") and report and report != last_text and not report.endswith(last_text or "\0"):
        head = "── Report "
        lines.append(_fit(f"    {DARK_SLATE}{head}{'─' * max(0, min(60, room) - plat.str_width(head))}{RST}", width))
        lines += [_fit(f"    {WHITE}{r}{RST}", width) for r in _rows_for(report, room)]
    note = _one_line(getattr(t, "worktree_note", ""))
    if note:
        lines.extend(_fit(f"    {AMBER}{'!' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(note, room)))
    return lines


def _detail_head(t: Any, width: int, now: float, room: int, expanded: bool) -> list[str]:
    glyph, color, label, _ = _st(t)
    lines = [_title_line(t, width)]
    lines.append(_fit(f"    {color}{label}{RST} {DARK_SLATE}·{RST} {_stats(t, now)}", width))
    if getattr(t, "kind", "agent") != "agent":
        return lines
    prompt = [_one_line(p) for p in str(getattr(t, "prompt", "") or "").splitlines() if p.strip()]
    asked = _wrap("\n".join(prompt), room) or ["(no prompt)"]
    if not expanded and len(asked) > _PROMPT_LINES:
        asked = asked[:_PROMPT_LINES - 1] + [f"… {len(asked) - _PROMPT_LINES + 1} more lines · ctrl+o shows them"]
    lines += _kv("asked", asked, width)
    if t.status == "error" or getattr(t, "error_msg", None):
        lines += _kv("reason", _wrap(_one_line(getattr(t, "error_msg", None) or "failed (no reason reported)"), room), width, ROSE)
    elif t.status == "running":
        act = _activity(t)
        lines += _kv("now", [plat.truncate_ansi(act, room)], width, AMBER if act.lower().startswith(("retrying", "resuming")) else CYAN)
    return lines


def can_message(t: Any) -> str:
    """"queue" when a message reaches it at its next step, "resume" when a message starts it again, "" when not."""
    if getattr(t, "kind", "agent") != "agent":
        return ""
    if getattr(t, "status", "") in ("running", "pending"):
        return "queue"
    return "resume" if getattr(getattr(t, "conversation", None), "messages", None) else ""


def _detail_footer(t: Any, width: int, expanded: bool) -> list[str]:
    items: list[tuple[str, str]] = []
    if can_message(t):
        items.append(("⏎", "send"))
    items += [("esc", "back"), ("↑↓", "scroll"), ("←→", "next agent"), ("ctrl+o", "collapse" if expanded else "expand")]
    if getattr(t, "status", "") in ("running", "pending"):
        items.append(("ctrl+x", "stop"))
    return _footer(items, width)


def _detail(view: Any, tasks: list[Any], width: int, now: float, height: int, scroll: int, expanded: bool,
            info: dict[str, Any] | None) -> list[str]:
    """Head, then the window of the transcript that fits *height* (the newest lines unless scrolled up)."""
    task = next((t for t in tasks if t.index == view), None)
    if task is None:
        return [f"  {SLATE}Subagent #{view} is not active.{RST}"]
    head = _detail_head(task, width, now, width - 13, expanded)
    body = transcript_lines(task, width, expanded)
    max_head = max(3, height * 2 // 3 if expanded else height // 3)
    if len(head) > max_head:
        head = head[:max_head - 1] + [f"    {DARK_SLATE}… ctrl+o shows the rest{RST}"]
    room = max(2, height - len(head) - 1)
    total = len(body)
    if info is not None:
        info["total"], info["max_scroll"], info["page"] = total, max(0, total - room), max(1, room - 2)
    rule = f"  {DARK_SLATE}{'─' * (width - 2)}{RST}"
    if total <= room:
        return head + [rule] + body
    scroll = max(0, min(scroll, total - room))
    end = total - scroll
    start = end - room
    window = body[start:end]
    if start > 0:
        window[0] = f"    {DARK_SLATE}↑ {start} earlier line{'s' if start != 1 else ''}{RST}"
    if scroll > 0:
        window[-1] = f"    {DARK_SLATE}↓ {scroll} newer line{'s' if scroll != 1 else ''} · end follows live{RST}"
    return head + [rule] + window


def _queue(agent: Any, width: int) -> list[str]:
    queue = getattr(agent, "message_queue", None) if agent is not None else None
    items = list(queue.items) if queue is not None else []
    lines = [f"  {MINT}{BOLD}◆ Queued prompts{RST} {SLATE}({len(items)}) · run after the subagents finish{RST}", ""]
    if not items:
        lines.append(f"    {SLATE}Empty. Type below and press ⏎ to queue a follow-up.{RST}")
    for i, it in enumerate(items, 1):
        tag = f"{MINT}{BOLD}next{RST}" if i == 1 else f"{DARK_SLATE}#{it.id:<3}{RST}"
        lines.append(_fit(f"    {tag} {WHITE}{_one_line(it.text)}{RST}", width))
    lines += ["", f"    {DARK_SLATE}/q <text> add · /q drop <id> remove · /q clear empty{RST}"]
    return [_fit(ln, width) for ln in lines]


_INPUT_HINT = "type a note for the main agent (it runs after the subagents finish) · /btw asks a side question"


def _input_hint(view: Any, tasks: list[Any]) -> str:
    task = next((t for t in tasks if t.index == view), None) if view not in (0, "Q") else None
    if task is None:
        return _INPUT_HINT
    how = can_message(task)
    who = _label(task)
    if how == "queue":
        return f"message {who} · it reads this before its next step"
    if how == "resume":
        return f"message {who} · resumes it with its history"
    if getattr(task, "kind", "agent") != "agent":
        return "this task takes no messages · esc goes back"
    return f"{who} has no saved history to resume · esc goes back"


def _tail(text: str, cells: int) -> str:
    """The end of *text* in at most *cells* columns ("…" + tail), so the typing position stays visible
    (wide characters and emoji count as two columns)."""
    if plat.str_width(text) <= cells:
        return text
    out, used = "", 1
    for ch in reversed(text):
        w = plat.char_width(ch)
        if used + w > cells:
            break
        out, used = ch + out, used + w
    return "…" + out


def _paste_text(key: str) -> str:
    """A bracketed / coalesced paste as one input line (newlines, CRs and tabs become spaces)."""
    text = key[len(plat.PASTE_PREFIX):].replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return sanitize(text)


def input_cursor_col(buf: str, width: int) -> int:
    """Screen column of the typing position on the input line ("  › " + the visible tail of *buf*), so
    the cursor sits where text goes, not after the placeholder hint."""
    return 4 + plat.str_width(_tail(buf, max(20, int(width)) - 5)) if buf else 4


def build_monitor_frame(view: Any, tasks: list[Any], agent: Any, buf: str, width: int, notice: str = "", *,
                        selected: int = 0, height: int | None = None, scroll: int = 0, expanded: bool = False,
                        console: bool = False, title: str = "Subagents", info: dict[str, Any] | None = None) -> list[str]:
    """Render one frame (pure function; used by the live loop and tests). Every line fits *width* and the
    frame fits *height* rows. *info* receives the size of the open transcript, for scrolling."""
    width = max(20, int(width))
    height = max(8, int(height or _DEFAULT_HEIGHT))
    now = time.time()
    selected = max(0, min(selected, len(tasks) - 1)) if tasks else 0
    rule = f"  {DARK_SLATE}{'─' * (width - 2)}{RST}"
    is_list = view == 0
    has_input = not (console and is_list)
    if view not in (0, "Q") and any(t.index == view for t in tasks):
        foot = _detail_footer(next(t for t in tasks if t.index == view), width, expanded)
    elif is_list:
        foot = _list_footer(tasks, selected, width, console)
    else:
        foot = _footer([("esc", "back")], width)
    chrome = 1 + len(foot) + (1 if notice else 0) + (1 if has_input else 0)     # rule, footer, notice, input
    room = height - chrome
    if is_list:
        lines = [_header(tasks, width, now, title), rule] if room >= 6 else []     # a very short window keeps the rows
        if not tasks:
            lines.append(f"  {SLATE}Nothing to show yet: starting the subagents…{RST}")
        else:
            sel = tasks[selected]
            lay = _table_layout(tasks, width)
            shown = lay["doing"] or width - 8 - lay["id"]      # a reason that fits its column is not repeated below
            why = _one_line(getattr(sel, "error_msg", "")) if getattr(sel, "status", "") == "error" else ""
            reason = _wrap(why, width - 8) if plat.str_width(why) > shown else []
            reason = reason[:max(0, min(4, room - 6))]
            lines += agent_table(tasks, width, selected, now, max_rows=max(2, room - len(lines) - len(reason)),
                                 noun="task" if title == "Tasks" else "agent")
            lines += [_fit(f"      {ROSE}{'✗' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(reason)]
    elif view == "Q":
        lines = _queue(agent, width)[:room]
    else:
        lines = _detail(view, tasks, width, now, room, scroll, expanded, info)
    lines = lines[:max(1, room)] + [rule] + foot
    if notice:
        lines.append(f"  {ROSE if notice.startswith('✗') else MINT}{notice}{RST}")
    if has_input:
        hint = _input_hint(view, tasks)
        shown = _tail(buf, width - 5)
        lines.append(f"  {CYAN}{BOLD}›{RST} {shown}" if buf else f"  {CYAN}{BOLD}›{RST} {DARK_SLATE}{hint}{RST}")
    return [_fit(ln, width) for ln in lines]


class CalmFrame(LiveFrame):
    """A LiveFrame that rewrites only the rows that changed, inside a synchronized update where the terminal has
    one: an elapsed time ticking must not erase and redraw the whole block."""

    def __init__(self) -> None:
        super().__init__()
        self._prev: list[str] = []

    def invalidate(self) -> None:
        self._prev = []

    def render(self, lines: list[str], cursor: tuple[int, int] | None = None) -> None:
        if plat.IS_WINDOWS:
            plat.enable_vt_mode()
        cols, _rows = plat.term_size()
        lines = [plat.truncate_ansi(line, cols - 1) for line in lines] or [""]
        if self._cols and cols != self._cols:
            self._prev = []
        out = ["\033[?2026h", plat.HIDE_CURSOR]
        up = self._rows_to_top(cols)
        if up > 0:
            out.append(f"\033[{up}A")
        if not self._prev:
            out.append("\r\033[J")
        for i, line in enumerate(lines):
            out.append("\r")
            if i >= len(self._prev) or self._prev[i] != line:
                out.append(line + "\033[K")
            if i < len(lines) - 1:
                out.append("\n")
        if len(lines) < len(self._prev):
            out.append("\033[B\r\033[J\033[A")
        self._prev = list(lines)
        self._widths = [plat.str_width(line) for line in lines]
        self._cols = cols
        if cursor is not None:
            row, col = max(0, min(cursor[0], len(lines) - 1)), max(0, min(cursor[1], cols - 1))
            down = len(lines) - 1 - row
            if down > 0:
                out.append(f"\033[{down}A")
            out.append(f"\r\033[{col}C" if col > 0 else "\r")
            out.append(plat.SHOW_CURSOR)
            self._cursor_row, self._cursor_col = row, col
        else:
            self._cursor_row, self._cursor_col = len(lines) - 1, 0
            out.append("\r")
        out.append("\033[?2026l")
        _write(out)

    def clear(self) -> None:
        super().clear()
        self._prev = []


def drive(keys: "MonitorKeys", rows: Any, agent: Any, keep_going: Any) -> None:
    """The live loop shared by the monitor and the console: read a key, act on it, draw what changed. Raises
    KeyboardInterrupt on Ctrl+C, and when the keys ask for the turn to stop."""
    from axon.agent.subagent import SCREEN_GATE
    frame = CalmFrame()
    SCREEN_GATE.attach()
    last_lines: list[str] = []
    last_draw = 0.0
    try:
        while keep_going():
            if SCREEN_GATE.wanted.is_set():         # a subagent needs an approval: the prompt gets the terminal
                frame.clear()
                last_lines = []
                with plat.cooked_terminal():
                    SCREEN_GATE.granted.set()
                    while SCREEN_GATE.wanted.is_set():
                        time.sleep(0.05)
            tasks = rows()
            key = plat.read_key(timeout=0.1)
            action = keys.handle(key, tasks) if key else None
            if action == "interrupt":
                raise KeyboardInterrupt
            if action == "close":
                return
            if action == "command":
                frame.clear()
                last_lines = []
                with plat.cooked_terminal():
                    _submit(keys.command, agent)
                tasks = rows()
            cols, term_rows = plat.term_size()
            width = max(20, min(160, cols - 2))
            lines = keys.frame(tasks, agent, width, max(8, term_rows - 2))
            now = time.monotonic()
            if key == "resize":
                frame.invalidate()
            # Elapsed times tick through the rows that changed; nothing is drawn when nothing changed.
            if lines != last_lines or key == "resize" or now - last_draw > 5.0:
                has_input = not (keys.console and keys.view == 0)
                frame.render(lines, (len(lines) - 1, input_cursor_col(keys.buf, width)) if has_input else None)
                last_lines, last_draw = lines, now
    finally:
        SCREEN_GATE.detach()
        frame.clear()


def _visible(tasks: list[Any]) -> list[Any]:
    return [t for t in tasks if not getattr(t, "dismissed", False)]


def run_live_subagent_monitor(future_map: dict[Any, int], subagents_mgr: Any, agent: Any = None) -> None:
    """Block until all futures finish, showing the interactive monitor when on a terminal."""
    if not plat.is_interactive():
        while any(not fut.done() for fut in future_map):
            time.sleep(0.1)
        return
    try:
        from axon.ui.in_flight import paused
    except ImportError:  # pragma: no cover
        import contextlib
        paused = contextlib.nullcontext  # type: ignore[assignment]

    keys = MonitorKeys(agent)
    try:
        with paused(), plat.raw_terminal(isig=True):
            drive(keys, lambda: _visible(list(subagents_mgr.all_tasks())), agent,
                  lambda: any(not fut.done() for fut in future_map))
    finally:
        # Also after Ctrl+C: the notes are already in the queue, so say where they went.
        if keys.queued:
            print(queued_notes_line(keys.queued))
        if keys.buf.strip():
            _keep_unsent(keys.buf)


class MonitorKeys:
    """Key handling of the list and the transcript (pure: no terminal I/O, so key sequences are testable).

    ``handle`` returns "command" when a slash command (other than /q) was entered: the caller runs ``self.command``
    with the terminal in cooked mode; "interrupt" for Esc on the list during a fan-out: the caller stops the turn;
    "close" for Esc on the list of the console."""

    def __init__(self, agent: Any = None, console: bool = False, title: str = "Subagents") -> None:
        self.agent = agent
        self.console = console
        self.title = title
        self.view: Any = 0
        self.selected = 0
        self.scroll = 0
        self.expanded = False
        self.buf = ""
        self.notice = ""
        self.command = ""
        self.queued: list[Any] = []
        self._total = 0
        self._max_scroll = 0
        self._page = 10

    def frame(self, tasks: list[Any], agent: Any, width: int, height: int) -> list[str]:
        """Draw, and keep the scroll position on the same lines while new ones arrive at the end."""
        info: dict[str, Any] = {}
        if self.selected >= len(tasks):
            self.selected = max(0, len(tasks) - 1)
        lines = build_monitor_frame(self.view, tasks, agent, self.buf, width, self.notice, selected=self.selected, height=height,
                                    scroll=self.scroll, expanded=self.expanded, console=self.console, title=self.title, info=info)
        if info:
            grew = info["total"] - self._total
            self._total, self._max_scroll, self._page = info["total"], info["max_scroll"], info["page"]
            if self.scroll and grew > 0:
                self.scroll += grew
                lines = build_monitor_frame(self.view, tasks, agent, self.buf, width, self.notice, selected=self.selected, height=height,
                                            scroll=self.scroll, expanded=self.expanded, console=self.console, title=self.title)
            self.scroll = min(self.scroll, self._max_scroll)
        return lines

    def _open(self, view: Any) -> None:
        self.view, self.scroll, self._total, self.notice = view, 0, 0, ""

    def handle(self, key: str, tasks: list[Any]) -> str | None:
        if key == "resize":
            return None
        if self.notice and (len(key) == 1 or key in ("backspace", "enter", "ctrl-j", "ctrl-u")
                            or key.startswith(plat.PASTE_PREFIX)):
            self.notice = ""                 # a status message lasts until the next edit
        if key in ("enter", "ctrl-j"):
            return self._enter(tasks)
        if key == "backspace":
            self.buf = self.buf[:-1]
        elif key == "ctrl-u":
            self.buf = ""
        elif key == "ctrl-c":
            return "close" if self.console else "interrupt"
        elif key == "esc":
            if self.buf:
                self.buf = ""
            elif self.view != 0:
                self._open(0)
            else:
                return "close" if self.console else "interrupt"   # as it does outside the monitor
        elif key.startswith(plat.PASTE_PREFIX):
            if self._accepts_text():
                self.buf += _paste_text(key)
        elif self.view == 0:
            self._list_key(key, tasks)
        elif self.view == "Q":
            self._queue_key(key)
        else:
            self._transcript_key(key, tasks)
        return None

    def _accepts_text(self) -> bool:
        return not (self.console and self.view == 0)

    def _queue_key(self, key: str) -> None:
        if key in ("tab", "shift-tab", "left", "right", "up", "down") and not self.buf:
            self._open(0)
        elif len(key) == 1 and key >= " ":
            self.buf += key

    def _list_key(self, key: str, tasks: list[Any]) -> None:
        empty = not self.buf
        if key in ("down", "tab") and tasks:
            self.selected = min(len(tasks) - 1, self.selected + 1)
        elif key in ("up", "shift-tab") and tasks:
            self.selected = max(0, self.selected - 1)
        elif key == "right" and empty and tasks:
            self._open(tasks[min(self.selected, len(tasks) - 1)].index)
        elif empty and key == "x" and tasks:
            self._stop_or_clear(tasks[min(self.selected, len(tasks) - 1)])
        elif empty and not self.console and key in ("q", "Q"):
            self._open("Q")
        elif empty and len(key) == 1 and key in "0123456789":   # not str.isdigit(): "²" is a digit too
            n = int(key)
            if any(t.index == n for t in tasks):
                self.selected = next(i for i, t in enumerate(tasks) if t.index == n)
                self._open(n)
        elif len(key) == 1 and key >= " " and self._accepts_text():
            self.buf += key

    def _transcript_key(self, key: str, tasks: list[Any]) -> None:
        empty = not self.buf
        if key == "up":
            self.scroll = min(self._max_scroll, self.scroll + 1)
        elif key == "down":
            self.scroll = max(0, self.scroll - 1)
        elif key == "pageup":
            self.scroll = min(self._max_scroll, self.scroll + self._page)
        elif key == "pagedown":
            self.scroll = max(0, self.scroll - self._page)
        elif key == "home" and empty:
            self.scroll = self._max_scroll
        elif key == "end" and empty:
            self.scroll = 0
        elif key == "ctrl-o":
            self.expanded = not self.expanded
            self.scroll, self._total = 0, 0
        elif key == "ctrl-x":
            task = next((t for t in tasks if t.index == self.view), None)
            if task is not None:
                self._stop_or_clear(task, clear=False)
        elif key in ("tab", "right") and empty and tasks:
            self._step(1, tasks)
        elif key in ("shift-tab", "left") and empty and tasks:
            self._step(-1, tasks)
        elif len(key) == 1 and key >= " ":
            self.buf += key

    def _step(self, delta: int, tasks: list[Any]) -> None:
        at = next((i for i, t in enumerate(tasks) if t.index == self.view), self.selected)
        self.selected = (at + delta) % len(tasks)
        self._open(tasks[self.selected].index)

    def _stop_or_clear(self, task: Any, clear: bool = True) -> None:
        who = _label(task)
        if getattr(task, "status", "") in ("running", "pending"):
            stop = getattr(task, "stop", None)
            if callable(stop):
                done = bool(stop())
            else:
                mgr = getattr(self.agent, "subagents", None)
                done = bool(mgr is not None and mgr.cancel(task.id))
            self.notice = f"✓ Stopping {who}" if done else f"✗ {who} cannot be stopped right now"
        elif clear:
            forget = getattr(task, "clear", None)
            if callable(forget):
                forget()
                self.notice = f"✓ Cleared {who} from the list"
            else:
                task.dismissed = True
                self.notice = f"✓ Cleared {who} from the list · /subagents {who} still opens it"

    def _enter(self, tasks: list[Any]) -> str | None:
        line = self.buf.strip()
        self.buf = ""
        if not line:
            if self.view == 0 and tasks:     # Enter on the list opens the selected agent
                self._open(tasks[min(self.selected, len(tasks) - 1)].index)
            return None
        if line.split(maxsplit=1)[0] in ("/q", "/queue"):
            self.notice = _queue_command(line, self.agent)
            return None
        if line.startswith("/"):
            self.command = line
            return "command"
        task = next((t for t in tasks if t.index == self.view), None) if self.view not in (0, "Q") else None
        if task is not None:
            self.notice = send_to_agent(self.agent, task, line)
            self.scroll = 0
            return None
        # A note goes to the main agent's queue; say so inside the view instead of printing into the
        # transcript while the frame is drawn.
        item = _queue_note(line, self.agent)
        if item is not None:
            self.queued.append(item)
            self.notice = (f"✓ Note #{item.id} queued for the main agent · runs after the subagents "
                           f"finish (open an agent to message it)")
        return None


def send_to_agent(agent: Any, task: Any, text: str) -> str:
    """Deliver what was typed in a transcript; the one-line outcome is shown there."""
    who = _label(task)
    if not can_message(task):
        return (f"✗ {who} has no saved history to resume" if getattr(task, "kind", "agent") == "agent"
                else f"✗ {who} takes no messages")
    try:
        from axon.agent.subagent import user_message
        kind, _task = user_message(agent, task.id, text)
    except Exception as e:  # noqa: BLE001 - ToolError carries the reason; a UI must not die here
        return f"✗ {e}"
    if kind == "resumed":
        return f"✓ Resumed {who} with your message · its report goes to the main agent"
    return f"✓ Sent to {who} · it reads this before its next step"


def _keep_unsent(text: str) -> None:
    """Text still being typed when the batch ended: put back at the prompt rather than lost."""
    try:
        from axon.ui.in_flight import _stash_type_ahead
        _stash_type_ahead(text)
        print(f"  {SLATE}The subagents finished before you sent that: your text is back at the prompt.{RST}")
    except Exception:
        pass


def queued_notes_line(items: list[Any]) -> str:
    """One line, printed when the monitor closes, naming the notes typed during the run and where they went."""
    texts = ", ".join(f'#{it.id} "{plat.truncate_ansi(" ".join(str(it.text).split()), 40)}"' for it in items)
    noun = "note" if len(items) == 1 else "notes"
    return f"  {MINT}✓ {len(items)} {noun} for the main agent{RST} {WHITE}{texts}{RST} {SLATE}· each runs as its own turn after this one{RST}"


def _queue_command(line: str, agent: Any) -> str:
    """Run a /q command typed in the monitor and return its last output line, shown inside the view (printing
    would stack lines above the frame)."""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _submit(line, agent)
    except Exception as e:
        return f"✗ {e}"
    out = [plat.strip_ansi(ln).strip() for ln in buf.getvalue().splitlines() if plat.strip_ansi(ln).strip()]
    return " · ".join(out[-2:]) if out else f"✓ {line}"


def _queue_note(line: str, agent: Any) -> Any:
    try:
        if agent is not None and hasattr(agent, "message_queue"):
            return agent.message_queue.push(line)
    except Exception:
        pass
    return None


def _submit(line: str, agent: Any) -> None:
    try:
        if line.startswith("/"):
            from axon.commands.builtin import dispatch_command
            if agent is not None:
                dispatch_command(line, agent)
        elif agent is not None and hasattr(agent, "message_queue"):
            item = agent.message_queue.push(line)
            print(f"  {MINT}✓ Note #{item.id} queued for the main agent:{RST} {WHITE}{line}{RST} {SLATE}(runs after the subagents finish){RST}")
    except Exception as e:
        print(f"  {ROSE}✗ Command error: {e}{RST}")
