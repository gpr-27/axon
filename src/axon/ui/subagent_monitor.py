"""
Live monitor shown while subagents run in parallel.

Views: [0] overview, [1..N] a subagent's detail feed, [Q] the prompt queue. Switch with the
number keys, Tab / Shift+Tab or the arrow keys. A bottom input line queues follow-up prompts
or runs slash commands (/btw, /q ...). Works on Windows consoles, macOS and Linux.

Every status has its own glyph (never colour alone), and every line fits the given width.
"""
from __future__ import annotations

import time
from typing import Any

from axon import platform as plat
from axon.ui.picker import LiveFrame
from axon.ui.theme import (AMBER, BOLD, CYAN, DARK_SLATE, GOLD, ITALIC, LAVENDER, MINT, ROSE, RST, SLATE, TEAL,
                           WHITE, wrap_ansi)

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
_TIMELINE_TAIL = 14
_PROMPT_LINES = 8


def _views(tasks: list[Any]) -> list[Any]:
    return [0] + [t.index for t in tasks] + ["Q"]


def _cycle(current: Any, delta: int, tasks: list[Any]) -> Any:
    views = _views(tasks)
    pos = views.index(current) if current in views else 0
    return views[(pos + delta) % len(views)]


def _st(t: Any) -> tuple[str, str, str, str]:
    return _STATUS.get(str(getattr(t, "status", "pending")), _STATUS["pending"])


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


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


def _bar(done: int, total: int, cells: int, color: str) -> str:
    total = max(1, total)
    fill = max(0, min(cells, round(cells * min(done, total) / total)))
    return f"{color}{'▰' * fill}{DARK_SLATE}{'▱' * (cells - fill)}{RST}"


def _stats(t: Any, now: float, bar_cells: int = 10) -> str:
    glyph, color, _, _ = _st(t)
    steps, max_steps = int(getattr(t, "steps", 0) or 0), int(getattr(t, "max_steps", 0) or 0)
    # A step is one model call. While running, show it against the step limit (the bar fills toward the
    # limit); once finished, the limit is irrelevant: "2 steps", not a mostly empty "2/25" bar.
    running = getattr(t, "status", "") in ("running", "pending")
    if running and max_steps:
        step_txt = f"{SLATE}step{RST} {WHITE}{steps}{RST} {SLATE}· limit {max_steps}{RST}"
    else:
        step_txt = f"{WHITE}{steps}{RST} {SLATE}step{'s' if steps != 1 else ''}{RST}"
    parts = [step_txt, f"{WHITE}{_dur(_elapsed(t, now))}{RST}"]
    retries, resumes = int(getattr(t, "retries", 0) or 0), int(getattr(t, "resumes", 0) or 0)
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
    bar = _bar(steps, max_steps or steps or 1, bar_cells, color) + " " if bar_cells and running else ""
    return bar + f" {DARK_SLATE}·{RST} ".join(parts)


def _activity(t: Any) -> str:
    act = _one_line(getattr(t, "activity", ""))
    if not act:
        logs = getattr(t, "live_logs", None) or []
        act = _one_line(logs[-1]) if logs else ""
    return act or ("waiting to start" if getattr(t, "status", "") == "pending" else "starting…")


def _first_result_line(t: Any) -> str:
    rows = [ln for ln in str(getattr(t, "result_text", "") or "").splitlines() if ln.strip()]
    body = [ln for ln in rows if not ln.lstrip().startswith("#")] or rows
    return _one_line(body[0].strip("#*-> ")) if body else "finished"


# ─── Chrome: header, tabs, footer ──────────────────────────────────────────

def _header(tasks: list[Any], width: int, now: float) -> list[str]:
    counts = {k: sum(1 for t in tasks if getattr(t, "status", "pending") == k) for k in _STATUS}
    shown = [k for k in ("running", "completed", "error", "exhausted", "cancelled", "pending") if counts[k]]
    total_tok = sum(int(getattr(t, "tokens_consumed", 0) or 0) for t in tasks)
    right = f"{SLATE}{_tok(total_tok)} tok · {_dur(_wall(tasks, now))}{RST}"
    line1 = ""
    for long in (True, False):
        chips = "  ".join(f"{_STATUS[k][1]}{_STATUS[k][0]} {counts[k]}{' ' + _STATUS[k][2] if long else ''}{RST}" for k in shown)
        left = f"  {GOLD}{BOLD}◆ Subagents{RST}   {chips}"
        gap = width - plat.str_width(left) - plat.str_width(right)
        if gap >= 2:
            line1 = left + " " * gap + right
            break
    line1 = line1 or _fit(left, width)
    n = max(1, len(tasks))
    finished = sum(counts[k] for k in _FINISHED)
    tail = f"  {SLATE}{finished}/{len(tasks)} finished{RST}"
    seg = max(1, min(8, (width - 2 - plat.str_width(tail)) // n - 1))
    strip = " ".join(f"{_st(t)[1]}{_st(t)[3] * seg}{RST}" for t in tasks)
    return [line1, _fit(f"  {strip}{tail}", width)]


def _tab_names(tasks: list[Any]) -> dict[int, str]:
    """Tab titles without a leading word shared by several titles ("Inspect …", "Inspect …")."""
    titles = {t.index: _one_line(t.title) for t in tasks}
    firsts = [v.split()[0].lower() for v in titles.values() if len(v.split()) > 1]
    common = {w for w in firsts if firsts.count(w) > 1}
    return {i: (v.split(None, 1)[1] if len(v.split()) > 1 and v.split()[0].lower() in common else v)
            for i, v in titles.items()}


def _tab_bar(view: Any, tasks: list[Any], q_len: int, width: int) -> str:
    names = _tab_names(tasks)

    def build(lv: int, boost: bool, tight: bool) -> str:
        tabs = [f"{GOLD}{BOLD}[{'0' if tight else '0 Main'}]{RST}" if view == 0 else f" {SLATE}{'0' if tight else '0 Main'}{RST} "]
        for t in tasks:
            glyph, color, _, _ = _st(t)
            name = _shorten(names[t.index], max(lv, 18) if boost and view == t.index else lv)
            sep = "" if tight and not name else " "
            if view == t.index:
                tabs.append(f"{CYAN}{BOLD}[{t.index}{sep}{glyph}{' ' + name if name else ''}]{RST}")
            else:
                tabs.append(f" {SLATE}{t.index}{RST}{sep}{color}{glyph}{RST}{(' ' + SLATE + name + RST) if name else ''} ")
        q = f"Q{q_len}" if tight else f"Q queue {q_len}"
        tabs.append(f"{MINT}{BOLD}[{q}]{RST}" if view == "Q" else f" {SLATE}{q}{RST}")
        return "  " + " ".join(tabs)

    for tight in (False, True):
        for lv in (range(28, 3, -1) if not tight else ()):
            for boost in (True, False):
                line = build(lv, boost, tight)
                if plat.str_width(line) <= width:
                    return line
        for boost in (True, False):
            line = build(0, boost, tight)
            if plat.str_width(line) <= width:
                return line
    return _fit(build(0, False, True), width)


def _footer(width: int) -> list[str]:
    items = [("0", "overview"), ("1-9", "open"), ("←/→", "switch"), ("Q", "queue"), ("esc", "back"),
             ("⏎", "note to main agent"), ("/btw", "ask aside")]
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


# ─── Views ─────────────────────────────────────────────────────────────────

def _title_line(t: Any, width: int, indent: str = "  ", badges: bool = True) -> str:
    glyph, color, _, _ = _st(t)
    prefix = f"{indent}{color}{BOLD}{glyph}{RST} {WHITE}{BOLD}{'#' + str(t.index):<4}{RST}"
    pre_w = plat.str_width(prefix)
    title = _one_line(t.title)
    a_type, model = _one_line(getattr(t, "agent_type", "")), _one_line(getattr(t, "model", ""))
    options = [(a_type, model), (a_type, ""), ("", "")] if badges else [("", "")]
    for a, m in options:
        right_w = (plat.str_width(a) + (3 + plat.str_width(m) if m else 0) + 2) if a else (plat.str_width(m) + 2 if m else 0)
        room = width - pre_w - right_w
        if room >= min(24, plat.str_width(title)) or (a, m) == ("", ""):
            break
    right = (f"{LAVENDER}{a}{RST}" if a else "") + (f" {DARK_SLATE}·{RST} " if a and m else "") + (f"{SLATE}{m}{RST}" if m else "")
    left = prefix + f"{WHITE}{BOLD}{_shorten(title, max(4, room))}{RST}"
    gap = width - plat.str_width(left) - plat.str_width(right)
    return _fit(left + " " * max(2, gap) + right if right else left, width)


def _outcome(t: Any, width: int, indent: str, max_lines: int) -> list[str]:
    status = getattr(t, "status", "pending")
    room = width - plat.str_width(indent) - 2
    if status == "error":
        reason = _one_line(getattr(t, "error_msg", "") or "failed (no reason reported)")
        rows = _wrap(reason, room)
        if len(rows) > max_lines:
            keep = rows[:max_lines]
            if max_lines >= 3:
                keep[-1] = f"… {len(rows) - max_lines + 1} more lines · press {t.index} to read it all"
            else:
                keep[-1] = plat.truncate_ansi(keep[-1] + " " + rows[max_lines], room - 1)
                keep[-1] = keep[-1] if keep[-1].endswith("…") else keep[-1] + "…"
            rows = keep
        return [f"{indent}{ROSE}{'✗' if i == 0 else ' '} {r}{RST}" for i, r in enumerate(rows)]
    if status == "completed":
        return [_fit(f"{indent}{SLATE}↳ {_first_result_line(t)}{RST}", width)]
    if status == "exhausted":
        return [_fit(f"{indent}{AMBER}! stopped at the step limit ({getattr(t, 'steps', 0)}/{getattr(t, 'max_steps', 0)}){RST}", width)]
    if status == "cancelled":
        return [f"{indent}{SLATE}⊘ cancelled{RST}"]
    color = AMBER if _activity(t).lower().startswith("retrying") else CYAN
    return [_fit(f"{indent}{color}↳ {_activity(t)}{RST}", width)]


def _overview(tasks: list[Any], width: int, now: float) -> list[str]:
    if not tasks:
        return [f"  {SLATE}Starting the subagents…{RST}"]
    n = len(tasks)
    compact = n > 6
    lines: list[str] = []
    sub = " " * 8
    for i, t in enumerate(tasks):
        if i and n <= 5:
            lines.append("")
        lines.append(_title_line(t, width))
        if compact:
            status, stats = getattr(t, "status", ""), _stats(t, now, bar_cells=0)
            if status == "error":
                lines.append(_outcome(t, width, sub, 1)[0])
            elif status == "running":
                lines.append(_fit(f"{sub}{stats}  {DARK_SLATE}│{RST} {CYAN}{_activity(t)}{RST}", width))
            else:
                lines.append(_fit(f"{sub}{stats}", width))
            continue
        lines.append(_fit(f"{sub}{_stats(t, now, bar_cells=10 if width >= 90 else 0)}", width))
        lines.extend(_outcome(t, width, sub, 6 if n <= 3 else (4 if n <= 5 else 3)))
    return lines


def _kv(label: str, value_rows: list[str], width: int, color: str = WHITE) -> list[str]:
    out = []
    for i, r in enumerate(value_rows or [""]):
        lab = f"{SLATE}{label:<10}{RST}" if i == 0 else " " * 10
        out.append(_fit(f"    {lab}{color}{r}{RST}", width))
    return out


def _timeline_lines(t: Any, width: int) -> list[str]:
    entries = list(getattr(t, "timeline", None) or [])
    room = width - 6
    lines: list[str] = []
    if not entries:
        logs = list(getattr(t, "live_logs", None) or [])
        for log in (logs[-8:] if logs else ["(starting…)"]):
            lines.append(_fit(f"    {DARK_SLATE}│{RST} {WHITE}{_one_line(log)}{RST}", width))
        return lines
    if len(entries) > _TIMELINE_TAIL:
        lines.append(f"    {DARK_SLATE}… {len(entries) - _TIMELINE_TAIL} earlier events{RST}")
    for e in entries[-_TIMELINE_TAIL:]:
        kind, raw, err = str(e.get("kind", "")), str(e.get("text", "") or ""), bool(e.get("is_error"))
        text = _one_line(raw)
        if kind == "step":
            head = f"── {text or 'Step'} "
            lines.append(_fit(f"    {DARK_SLATE}{head}{'─' * max(0, min(40, room) - plat.str_width(head))}{RST}", width))
        elif kind == "tool":
            lines.append(_fit(f"    {CYAN}▸{RST} {WHITE}{text}{RST}", width))
        elif kind == "result":
            body = [ln for ln in raw.splitlines() if ln.strip()]
            first = _one_line(body[0]) if body else "(no output)"
            more = f" ({len(body)} lines)" if len(body) > 1 else ""
            color = ROSE if err else SLATE
            lines.append(_fit(f"      {color}↳ {first}{RST}{DARK_SLATE}{more}{RST}", width) if not more else
                         _fit(f"      {color}↳ {plat.truncate_ansi(first, max(8, room - 4 - len(more)))}{RST}{DARK_SLATE}{more}{RST}", width))
        elif kind == "thinking":
            rows = _wrap(text, room - 2)[:2]
            if len(_wrap(text, room - 2)) > 2 and rows:
                rows[-1] = plat.truncate_ansi(rows[-1] + " …", room - 2)
            lines.extend(_fit(f"      {DARK_SLATE}{ITALIC}{'∴ ' if i == 0 else '  '}{r}{RST}", width) for i, r in enumerate(rows))
        elif kind == "retry":
            lines.extend(_fit(f"    {AMBER}{'⟳' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:2]))
        elif kind == "resume":
            lines.extend(_fit(f"    {CYAN}{'↻' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:2]))
        elif kind == "error":
            lines.extend(_fit(f"    {ROSE}{'✗' if i == 0 else ' '} {r}{RST}", width) for i, r in enumerate(_wrap(text, room)[:4]))
        else:
            lines.append(_fit(f"    {DARK_SLATE}│{RST} {WHITE}{text}{RST}", width))
    return lines


def _detail(view: Any, tasks: list[Any], width: int, now: float) -> list[str]:
    task = next((t for t in tasks if t.index == view), None)
    if task is None:
        return [f"  {SLATE}Subagent #{view} is not active.{RST}"]
    glyph, color, label, _ = _st(task)
    lines = [_title_line(task, width, badges=False), ""]
    room = width - 14
    status_txt = label if task.status != "running" else f"running · step {getattr(task, 'steps', 0)}"
    lines += _kv("status", [f"{glyph} {status_txt}"], width, color)
    if getattr(task, "agent_type", ""):
        lines += _kv("type", [_one_line(task.agent_type)], width, LAVENDER)
    if getattr(task, "model", ""):
        lines += _kv("model", [_one_line(task.model)], width)
    lines.append(_fit(f"    {SLATE}{'progress':<10}{RST}{_stats(task, now, bar_cells=12)}", width))
    in_t, out_t = int(getattr(task, "input_tokens", 0) or 0), int(getattr(task, "output_tokens", 0) or 0)
    if in_t or out_t:
        lines += _kv("tokens", [f"{_tok(in_t)} in · {_tok(out_t)} out"], width, SLATE)
    prompt = [_one_line(p) for p in str(getattr(task, "prompt", "") or "").splitlines() if p.strip()]
    asked = _wrap("\n".join(prompt), room) or ["(no prompt)"]
    if len(asked) > _PROMPT_LINES:
        asked = asked[:_PROMPT_LINES - 1] + [f"… {len(asked) - _PROMPT_LINES + 1} more lines"]
    lines += _kv("asked", asked, width)
    if task.status == "error" or getattr(task, "error_msg", None):
        lines += _kv("reason", _wrap(_one_line(getattr(task, "error_msg", None) or "failed (no reason reported)"), room),
                     width, ROSE)
    elif task.status == "running":
        act = _activity(task)
        lines += _kv("now", [plat.truncate_ansi(act, room)], width, AMBER if act.lower().startswith("retrying") else CYAN)
    elif task.status == "completed":
        lines += _kv("result", [plat.truncate_ansi(_first_result_line(task), room)], width, SLATE)
    head = "── Timeline "
    lines += ["", _fit(f"  {TEAL}{BOLD}{head}{RST}{DARK_SLATE}{'─' * max(0, width - 2 - plat.str_width(head))}{RST}", width)]
    lines += _timeline_lines(task, width)
    return lines


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
    return key[len(plat.PASTE_PREFIX):].replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")


def input_cursor_col(buf: str, width: int) -> int:
    """Screen column of the typing position on the input line ("  › " + the visible tail of *buf*), so
    the cursor sits where text goes, not after the placeholder hint."""
    return 4 + plat.str_width(_tail(buf, max(20, int(width)) - 5)) if buf else 4


def build_monitor_frame(view: Any, tasks: list[Any], agent: Any, buf: str, width: int, notice: str = "") -> list[str]:
    """Render the monitor (pure function; used by the live loop and tests). Every line fits *width*."""
    width = max(20, int(width))
    now = time.time()
    queue = getattr(agent, "message_queue", None) if agent is not None else None
    q_len = len(queue) if queue is not None else 0
    rule = f"  {DARK_SLATE}{'─' * (width - 2)}{RST}"
    lines = _header(tasks, width, now) + [_tab_bar(view, tasks, q_len, width), rule]
    if view == 0:
        lines += _overview(tasks, width, now)
    elif view == "Q":
        lines += _queue(agent, width)
    else:
        lines += _detail(view, tasks, width, now)
    lines += [rule] + _footer(width)
    if notice:
        lines.append(f"  {ROSE if notice.startswith('✗') else MINT}{notice}{RST}")
    shown = _tail(buf, width - 5)
    lines.append(f"  {CYAN}{BOLD}›{RST} {shown}" if buf else f"  {CYAN}{BOLD}›{RST} {DARK_SLATE}{_INPUT_HINT}{RST}")
    return [_fit(ln, width) for ln in lines]


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
    frame = LiveFrame()
    last_lines: list[str] = []
    last_draw = 0.0
    try:
        with paused(), plat.raw_terminal(isig=True):
            try:
                while any(not fut.done() for fut in future_map):
                    tasks = list(subagents_mgr.all_tasks())
                    key = plat.read_key(timeout=0.1)
                    if key and keys.handle(key, tasks) == "command":
                        frame.clear()
                        last_lines = []
                        with plat.cooked_terminal():
                            _submit(keys.command, agent)
                    width = max(20, min(120, plat.term_size()[0] - 2))
                    lines = build_monitor_frame(keys.view, tasks, agent, keys.buf, width, keys.notice)
                    now = time.monotonic()
                    # Redraw at least every second so elapsed times keep ticking.
                    if lines != last_lines or key == "resize" or now - last_draw > 1.0:
                        frame.render(lines, (len(lines) - 1, input_cursor_col(keys.buf, width)))
                        last_lines, last_draw = lines, now
            finally:
                frame.clear()
    finally:
        # Also after Ctrl+C: the notes are already in the queue, so say where they went.
        if keys.queued:
            print(queued_notes_line(keys.queued))


class MonitorKeys:
    """Key handling of the live monitor (pure: no terminal I/O, so key sequences are testable).

    ``handle`` returns "command" when a slash command (other than /q) was entered: the caller runs
    ``self.command`` with the terminal in cooked mode."""

    def __init__(self, agent: Any = None) -> None:
        self.agent = agent
        self.view: Any = 0
        self.buf = ""
        self.notice = ""
        self.command = ""
        self.queued: list[Any] = []

    def handle(self, key: str, tasks: list[Any]) -> str | None:
        if key == "resize":
            return None
        if self.notice and (len(key) == 1 or key in ("backspace", "enter", "ctrl-j", "ctrl-u")
                            or key.startswith(plat.PASTE_PREFIX)):
            self.notice = ""                 # a status message lasts until the next edit
        if key in ("enter", "ctrl-j"):
            return self._enter()
        if key == "backspace":
            self.buf = self.buf[:-1]
        elif key == "ctrl-u":
            self.buf = ""
        elif key == "esc":
            if self.buf:
                self.buf = ""
            else:
                self.view = 0
        elif key in ("tab", "right", "down") and not (self.buf and key == "right"):
            self.view = _cycle(self.view, 1, tasks)
        elif key in ("shift-tab", "left", "up") and not (self.buf and key == "left"):
            self.view = _cycle(self.view, -1, tasks)
        elif key.startswith(plat.PASTE_PREFIX):
            self.buf += _paste_text(key)
        elif not self.buf and key in ("q", "Q"):
            self.view = "Q"
        elif not self.buf and key in ("m", "M"):
            self.view = 0
        elif not self.buf and len(key) == 1 and key in "0123456789":   # not str.isdigit(): "²" is a digit too
            n = int(key)
            if n == 0 or any(t.index == n for t in tasks):
                self.view = n
        elif len(key) == 1 and key >= " ":
            self.buf += key
        return None

    def _enter(self) -> str | None:
        line = self.buf.strip()
        self.buf = ""
        if not line:                         # Enter on an empty input line does nothing
            return None
        if line.split(maxsplit=1)[0] in ("/q", "/queue"):
            self.notice = _queue_command(line, self.agent)
            return None
        if line.startswith("/"):
            self.command = line
            return "command"
        # A note goes to the main agent's queue; say so inside the view instead of printing into the
        # transcript while the frame is drawn.
        item = _queue_note(line, self.agent)
        if item is not None:
            self.queued.append(item)
            self.notice = (f"✓ Note #{item.id} queued for the main agent · runs after the subagents "
                           f"finish (subagents can't be messaged)")
        return None


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
