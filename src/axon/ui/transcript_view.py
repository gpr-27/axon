"""
Full-screen interactive transcript viewer (``/transcript``, Ctrl+F at the idle prompt).

A scrolling terminal cannot re-open lines that already scrolled away, so this view redraws the
session from the structured transcript the renderer records (:func:`axon.ui.render.get_transcript`)
on the alternate screen, nested like the live timeline::

    ▾ Turn 2 · "fix the test"                       Done in 6.4s · 2 LLM calls  ✓
      ▾ ◆ Step 1 · gpt-4o  replied in 2.5s · 1.2k tokens · $0.01
        ▸ ✻ Thought for 1.2s › The test fails because…
        ▸ ● Bash  pytest -q · runs a shell command                         ✓ 0.42s
            ⎿ 3 passed · 12 lines
        ▸ ▎ The fix was…

Clicking a row (or Enter/Space on the selected row) expands it: tool calls show their input and
the complete numbered output, thinking shows the full reasoning, responses render as markdown.
Running tool calls tick their elapsed time live. The pure parts (row layout, fold/expand state,
search, the conversation fallback) are plain functions/methods so they are unit-testable; only
:meth:`TranscriptViewer.run` touches the terminal, and it always restores it (mouse reporting
off, alternate screen left, cursor shown) in a ``finally`` block.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from axon import platform as plat
from axon.ui import theme as T
from axon.ui.theme import display_width, glyph, hard_wrap_ansi, sanitize, strip_ansi, truncate, wrap_ansi

FOLDABLE = ("turn", "step")
OPEN_KEY = "ctrl-f"  # default binding of app:transcript (see axon.ui.keybindings)
RESPONSE_PREVIEW_LINES = 3


# ─── small helpers ─────────────────────────────────────────────────────────

def _sym(uni: str, asc: str) -> str:
    """A glyph not in theme.GLYPHS, with the same ASCII fallback rules."""
    if os.environ.get("AXON_ASCII") or not T.supports_unicode():
        return asc
    return uni


def fmt_secs(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = max(0.0, float(seconds))
    if s < 1:
        return f"{s:.2f}s"
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def elapsed_text(entry: Any, now: float | None = None) -> str:
    """Live elapsed time of a running entry (from ``started_at``), else its recorded duration."""
    now = time.time() if now is None else now
    if getattr(entry, "status", "") == "running" and getattr(entry, "started_at", 0):
        return fmt_secs(now - float(entry.started_at))
    return fmt_secs(getattr(entry, "duration", None))


def _spinner(now: float) -> str:
    frames = glyph("spinner")
    return frames[int(now * 10) % len(frames)] if frames else "*"


def _one_line(text: Any, limit: int = 80) -> str:
    s = " ".join(sanitize(strip_ansi(str(text or ""))).split())
    return s if len(s) <= limit else s[: max(1, limit - 1)] + glyph("ellipsis")


def _split_title(title: str) -> tuple[str, str]:
    """'Bash  git status' -> ('Bash', 'git status')."""
    name, _sep, target = (title or "").partition("  ")
    return name.strip(), target.strip()


def _fit(left: str, right: str, width: int) -> str:
    """``left`` and ``right`` on one row of ``width`` columns (left truncated first)."""
    if not right:
        return truncate(left, width)
    rw = display_width(right)
    if rw + 2 > width:
        return truncate(left, width)
    left = truncate(left, width - rw - 2)
    return left + " " * max(2, width - display_width(left) - rw) + right


def _status_mark(entry: Any, now: float) -> str:
    st = getattr(entry, "status", "ok")
    el = elapsed_text(entry, now)
    if st == "running":
        return f"{T.WARNING}{_spinner(now)}{T.RST} {T.MUTED}{el or 'running'}{T.RST}"
    if st == "error":
        return f"{T.ERROR}{glyph('fail')}{T.RST}" + (f" {T.MUTED}{el}{T.RST}" if el else "")
    return f"{T.SUCCESS}{glyph('ok')}{T.RST}" + (f" {T.MUTED}{el}{T.RST}" if el else "")


def _tool_purpose(name: str, registry: Any = None) -> str:
    try:
        from axon.ui.render import tool_purpose
    except ImportError:
        return ""
    try:
        return tool_purpose(name, registry)
    except Exception:
        return ""


def _card_rows(name: str, e: Any, width: int) -> list[str] | None:
    if getattr(e, "status", "") == "error":
        return None
    try:
        from axon.ui.render import transcript_card_rows
        return transcript_card_rows(name, getattr(e, "detail", "") or "", e.body or "", width)
    except Exception:
        return None


def _highlight(line: str, lang: str = "") -> str:
    try:
        from axon.ui.markdown import highlight
        return highlight(line, lang)
    except Exception:
        return line


def _markdown(text: str, width: int) -> list[str]:
    try:
        from axon.ui.markdown import format_markdown
        rows = format_markdown(text, max_width=width).split("\n")
    except Exception:
        rows = text.split("\n")
    return [r[2:] if r.startswith("  ") else r for r in rows]


# ─── hierarchy ─────────────────────────────────────────────────────────────

@dataclass
class Node:
    entry: Any
    depth: int
    parents: tuple[int, ...]  # ids of the turn/step entries above this one

    @property
    def key(self) -> int:
        return id(self.entry)

    @property
    def kind(self) -> str:
        return str(getattr(self.entry, "kind", ""))


def build_nodes(entries: Iterable[Any]) -> list[Node]:
    """Place each entry under its turn and step (steps under turns, everything else under both)."""
    nodes: list[Node] = []
    turn: Any = None
    steps: dict[tuple[int, int], Any] = {}
    for e in entries:
        kind = getattr(e, "kind", "")
        if kind == "turn":
            turn, steps = e, {}
            nodes.append(Node(e, 0, ()))
            continue
        in_turn = turn is not None and getattr(turn, "turn", None) == getattr(e, "turn", None)
        parents: tuple[int, ...] = (id(turn),) if in_turn else ()
        if kind == "step":
            steps[(getattr(e, "turn", 0), getattr(e, "step", 0))] = e
            nodes.append(Node(e, len(parents), parents))
            continue
        step = steps.get((getattr(e, "turn", 0), getattr(e, "step", 0)))
        if step is not None and in_turn:
            parents = parents + (id(step),)
        nodes.append(Node(e, len(parents), parents))
    return nodes


# ─── conversation fallback (resumed sessions) ──────────────────────────────

def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "image":
                    parts.append("[image]")
                elif b.get("type") == "tool_result":
                    parts.append(_content_text(b.get("content")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _first_line(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return _one_line(ln, 120)
    return ""


def entries_from_messages(messages: Iterable[Any], registry: Any = None) -> list[Any]:
    """Transcript entries rebuilt from provider messages (user prompts, assistant text and thinking,
    tool calls with their matching results), for sessions the renderer did not draw (``/resume``).
    Understands Anthropic-style ``tool_use``/``tool_result`` blocks and OpenAI-style
    ``tool_calls`` + ``role: tool`` messages."""
    from axon.ui.render import TranscriptEntry
    try:
        from axon.ui.render import tool_target
    except ImportError:  # pragma: no cover
        def tool_target(name: str, inp: dict[str, Any]) -> str:
            return ""

    out: list[Any] = []
    tools: dict[str, Any] = {}
    turn_no = 0
    turn: Any = None
    counts = {"tools": 0, "texts": 0}

    def close_turn() -> None:
        if turn is not None:
            bits = []
            if counts["tools"]:
                bits.append(f"{counts['tools']} tool call{'s' if counts['tools'] != 1 else ''}")
            if counts["texts"]:
                bits.append(f"{counts['texts']} response{'s' if counts['texts'] != 1 else ''}")
            turn.summary = " · ".join(bits) or "restored"
            if any(t.status == "error" for t in tools.values() if t.turn == turn.turn):
                turn.status = "error"

    def ensure_turn() -> None:
        nonlocal turn, turn_no
        if turn is None:
            turn_no += 1
            turn = TranscriptEntry("turn", turn_no, title=f"Turn {turn_no}", status="ok")
            out.append(turn)

    def add_tool(call_id: str, name: str, inp: Any) -> None:
        ensure_turn()
        if not isinstance(inp, dict):
            try:
                inp = json.loads(inp) if isinstance(inp, str) and inp.strip() else {}
            except ValueError:
                inp = {"arguments": inp}
            if not isinstance(inp, dict):
                inp = {"input": inp}
        try:
            detail = json.dumps(inp, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            detail = repr(inp)
        try:
            target = tool_target(name, inp)
        except Exception:
            target = ""
        e = TranscriptEntry("tool", turn_no, title=f"{sanitize(name)}  {target}".rstrip(), detail=detail,
                            status="ok", id=str(call_id), summary="(no result)")
        tools[str(call_id)] = e
        counts["tools"] += 1
        out.append(e)

    def add_result(call_id: str, content: Any, is_error: bool) -> None:
        e = tools.get(str(call_id))
        if e is None:
            return
        e.body = sanitize(strip_ansi(_content_text(content)))
        e.status = "error" if is_error else "ok"
        e.summary = _first_line(e.body) or "(no output)"

    for m in messages or ():
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for b in results:
                    add_result(b.get("tool_use_id", ""), b.get("content"), bool(b.get("is_error")))
                continue
            text = _content_text(content).strip()
            if not text:
                continue
            close_turn()
            turn_no += 1
            counts.update(tools=0, texts=0)
            turn = TranscriptEntry("turn", turn_no, title=f'Turn {turn_no} · "{_one_line(text, 80)}"',
                                   body=sanitize(strip_ansi(text)), status="ok")
            out.append(turn)
        elif role == "assistant":
            blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}]
                                                                   if isinstance(content, str) else [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type")
                if kind == "text" and str(b.get("text", "")).strip():
                    ensure_turn()
                    body = sanitize(strip_ansi(str(b["text"]).strip()))
                    counts["texts"] += 1
                    out.append(TranscriptEntry("text", turn_no, title="Response", summary=_one_line(body, 120), body=body))
                elif kind in ("thinking", "reasoning") and str(b.get("thinking") or b.get("text") or "").strip():
                    ensure_turn()
                    body = sanitize(strip_ansi(str(b.get("thinking") or b.get("text")).strip()))
                    out.append(TranscriptEntry("thinking", turn_no, title="Thinking", summary=_one_line(body, 120), body=body))
                elif kind == "tool_use":
                    add_tool(b.get("id", ""), str(b.get("name", "tool")), b.get("input") or {})
            for call in m.get("tool_calls") or ():
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    add_tool(call.get("id", ""), str(fn.get("name", "tool")), fn.get("arguments") or {})
        elif role == "tool":
            add_result(m.get("tool_call_id", ""), content, bool(m.get("is_error")))
    close_turn()
    return out


# ─── rows ──────────────────────────────────────────────────────────────────

@dataclass
class Row:
    key: int          # id() of the entry this row belongs to
    text: str         # styled, fits the width
    header: bool = False


def _wrap_plain(text: str, width: int) -> list[str]:
    rows: list[str] = []
    for ln in (text or "").split("\n"):
        rows.extend(wrap_ansi(ln.expandtabs(4), max(4, width)) if ln else [""])
    return rows


class Layout:
    """Turns entries + view state into rows. Stateless apart from a cache of finished entries."""

    def __init__(self, registry: Any = None) -> None:
        self.registry = registry
        self._cache: dict[tuple[Any, ...], list[str]] = {}

    def entry_rows(self, node: Node, width: int, *, expanded: bool, folded: bool, selected: bool,
                   now: float, hidden: int = 0) -> list[str]:
        e = node.entry
        running = getattr(e, "status", "") == "running"
        ck = (node.key, width, expanded, folded, selected, hidden, getattr(e, "status", ""), getattr(e, "duration", None),
              len(getattr(e, "body", "") or ""), getattr(e, "summary", ""), getattr(e, "title", ""), T.RST, glyph("ok"))
        if not running and ck in self._cache:
            return self._cache[ck]
        indent = "  " * node.depth
        gutter = f"{T.ACCENT}{T.BOLD}{glyph('prompt')}{T.RST} " if selected else "  "
        avail = max(10, width - 2 - len(indent))
        body = self._rows(node, avail, expanded, folded, now, hidden)
        lines = [truncate((gutter if i == 0 else "  ") + indent + r, width) for i, r in enumerate(body)]
        if not running:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[ck] = lines
        return lines

    # one method per kind; every row is <= avail columns
    def _rows(self, node: Node, avail: int, expanded: bool, folded: bool, now: float, hidden: int) -> list[str]:
        e = node.entry
        kind = node.kind
        R = T.RST
        if kind in FOLDABLE:
            mark = _sym("▸", "+") if folded else _sym("▾", "-")
            extra = f" {T.SUBTLE}({hidden} hidden){R}" if folded and hidden else ""
            if kind == "turn":
                left = f"{T.MUTED}{mark}{R} {T.BOLD}{T.TEXT}{_one_line(e.title, 200)}{R}{extra}"
                right = f"{T.MUTED}{_one_line(e.summary, 120)}{R}  {_status_mark(e, now)}" if e.summary else _status_mark(e, now)
                if display_width(left) + display_width(right) + 2 > avail and e.summary:
                    return [_fit(left, _status_mark(e, now), avail),
                            truncate(f"    {T.MUTED}{_one_line(e.summary, 300)}{R}", avail)]
                return [_fit(left, right, avail)]
            title = _one_line(e.title, 200)
            summ = _one_line(e.summary, 200)
            left = (f"{T.MUTED}{mark}{R} {T.ACCENT}{glyph('diamond')}{R} {T.BOLD}{title}{R}"
                    + (f"  {T.MUTED}{summ}{R}" if summ and getattr(e, "status", "") != "running" else "") + extra)
            right = _status_mark(e, now) if getattr(e, "status", "") == "running" else ""
            return [_fit(left, right, avail)] if right else wrap_ansi(left, avail)[:2]
        fold = (_sym("▾", "-") if expanded else _sym("▸", "+")) if self.expandable(e) else " "
        fold = f"{T.MUTED}{fold}{R}"
        if kind == "tool":
            return self._tool_rows(e, avail, expanded, fold, now)
        if kind == "thinking":
            star = f"{T.SUBTLE}{glyph('star')} {_one_line(e.title, 60) or 'Thinking'}{R}"
            if not expanded:
                summ = _one_line(e.summary or e.body, 400)
                return [truncate(f"{fold} {star} {T.SUBTLE}{glyph('chevron')}{R} {T.DIM}{T.ITALIC}{summ}{R}", avail)]
            bar = f"{T.SUBTLE}{glyph('thinking_bar')}{R}"
            rows = [f"{fold} {star}"]
            rows += [f"  {bar} {T.DIM}{T.ITALIC}{ln}{R}" if ln else f"  {bar}" for ln in _wrap_plain(e.body, avail - 4)]
            return rows
        if kind == "text":
            bar = f"{T.ACCENT}{glyph('quote')}{R}"
            body = _markdown(e.body.strip(), avail - 4)
            while body and not strip_ansi(body[0]).strip():
                body.pop(0)
            more = 0
            if not expanded and len(body) > RESPONSE_PREVIEW_LINES:
                more = len(body) - RESPONSE_PREVIEW_LINES
                body = body[:RESPONSE_PREVIEW_LINES]
            rows = [f"{fold if i == 0 else ' '} {bar} {T.TEXT}{ln}{R}" for i, ln in enumerate(body or [""])]
            if more:
                rows.append(f"  {bar} {T.MUTED}{glyph('ellipsis')} +{more} more line{'s' if more != 1 else ''} "
                            f"{glyph('dot')} click to expand{R}")
            return [truncate(r, avail) for r in rows]
        # note (retries, errors, …)
        color = T.ERROR if getattr(e, "status", "") == "error" else T.WARNING
        icon = glyph("warn") if getattr(e, "status", "") == "error" else glyph("info")
        rows = [truncate(f"{fold} {color}{icon} {_one_line(e.title, 60)}{R} {T.MUTED}{_one_line(e.summary, 300)}{R}", avail)]
        if expanded and e.body:
            rows += [f"    {T.MUTED}{ln}{R}" for ln in _wrap_plain(e.body, avail - 4)]
        return rows

    @staticmethod
    def expandable(e: Any) -> bool:
        kind = getattr(e, "kind", "")
        if kind == "tool":
            return True
        if kind == "text":
            return bool(getattr(e, "body", ""))
        return bool(getattr(e, "body", "")) or bool(getattr(e, "detail", ""))

    def _tool_rows(self, e: Any, avail: int, expanded: bool, fold: str, now: float) -> list[str]:
        R = T.RST
        name, target = _split_title(e.title)
        purpose = _tool_purpose(name, self.registry)
        why = _one_line(getattr(e, "why", "") or "", 200)
        color = T.ERROR if e.status == "error" else T.PRIMARY
        left = f"{fold} {color}{glyph('tool')}{R} {T.BOLD}{T.TEXT}{_one_line(name, 40)}{R}"
        if target:
            left += f"  {T.TEXT}{_one_line(target, 200)}{R}"
        if purpose and not expanded:
            left += f" {T.SUBTLE}{glyph('dot')} {purpose}{R}"
        rows = [_fit(left, _status_mark(e, now), avail)]
        body = e.body or ""
        n_lines = len(body.splitlines())
        res = glyph("result")
        if e.status == "running":
            summary = f"{T.MUTED}running {elapsed_text(e, now)}{R}"
        else:
            summ = _one_line(e.summary, 300) or ("(no output)" if not body.strip() else _first_line(body))
            summary = f"{T.ERROR if e.status == 'error' else T.MUTED}{summ}{R}"
            if not expanded and n_lines > 1 and "line" not in summ:
                summary += f" {T.SUBTLE}{glyph('dot')} {n_lines} lines{R}"
        rows.append(truncate(f"    {T.SUBTLE}{res}{R} {summary}", avail))
        if not expanded:
            return rows
        inner = avail - 6
        if purpose:
            rows.append(truncate(f"      {T.MUTED}What{R}  {T.TEXT}{purpose}{R}", avail))
        if why:
            rows += [f"      {T.MUTED}Why{R}   {T.ITALIC}{ln}{R}" if i == 0 else f"            {T.ITALIC}{ln}{R}"
                     for i, ln in enumerate(wrap_ansi(why, max(10, inner - 6)))]
        if e.detail:
            rows.append(f"      {T.MUTED}{T.BOLD}Input{R}")
            for ln in e.detail.splitlines():
                for part in hard_wrap_ansi(_highlight(ln, "json"), max(10, inner)):
                    rows.append(f"      {part}")
        label = "Output"
        if e.output_no:
            label += f" #{e.output_no}"
        rows.append(f"      {T.MUTED}{T.BOLD}{label}{R} {T.SUBTLE}{glyph('dot')} {n_lines} line{'s' if n_lines != 1 else ''}{R}")
        if e.status == "running" and not body:
            rows.append(f"      {T.MUTED}running {elapsed_text(e, now)}{glyph('ellipsis')}{R}")
        elif not body.strip():
            rows.append(f"      {T.MUTED}(no output){R}")
        elif (card := _card_rows(name, e, inner)) is not None:
            rows += [f"      {r}" for r in card]     # the same file / diff / search card as the live timeline
        else:
            nw = len(str(n_lines))
            text_w = max(8, inner - nw - 3)
            v = glyph("v")
            for i, ln in enumerate(body.splitlines(), 1):
                parts = hard_wrap_ansi(_highlight(ln.expandtabs(4)), text_w) or [""]
                for j, part in enumerate(parts):
                    num = f"{i:>{nw}}" if j == 0 else " " * nw
                    rows.append(f"      {T.SUBTLE}{num} {v}{R} {part}")
        return rows


# ─── the viewer ────────────────────────────────────────────────────────────

def footer_help() -> str:
    d = glyph("dot")
    return f"click/enter expand {d} e/c all {d} / search {d} y copy {d} q close"


class TranscriptViewer:
    """View state + key/mouse handling. ``source`` returns the current entries (called on refresh,
    so a live transcript keeps growing while the view is open)."""

    def __init__(self, source: Callable[[], list[Any]] | list[Any], width: int = 100, height: int = 30,
                 clock: Callable[[], float] = time.time, registry: Any = None,
                 copier: Callable[[str], bool] | None = None) -> None:
        self._source = source if callable(source) else (lambda s=list(source): s)
        self.width, self.height = max(20, width), max(5, height)
        self.clock = clock
        self.layout = Layout(registry)
        self.copier = copier
        self.expanded: set[int] = set()
        self.folded: set[int] = set()
        self.auto_expanded: set[int] = set()
        self.entries: list[Any] = []
        self.nodes: list[Node] = []
        self.sel: int | None = None
        self.top = 0
        self.query = ""
        self.searching = False
        self._search_origin: int | None = None
        self.message = ""
        self.done = False
        self._sig: Any = None
        self._rows: list[Row] = []
        self.refresh()
        vis = self.visible()
        if vis:
            self.sel = vis[-1].key
        self.end()

    # ── data ───────────────────────────────────────────────────────────
    def _signature(self) -> Any:
        tail = self.entries[-64:]
        return (len(self.entries), tuple((id(e), e.status, e.duration, len(e.body or ""), e.summary) for e in tail))

    def refresh(self) -> bool:
        """Re-read the entries; True when the view must be redrawn (changed or something is running)."""
        try:
            self.entries = list(self._source())
        except Exception:
            self.entries = []
        sig = self._signature()
        changed = sig != self._sig
        self._sig = sig
        if changed:
            self.nodes = build_nodes(self.entries)
            keys = {n.key for n in self.nodes}
            self.expanded &= keys
            self.folded &= keys
            if self.sel not in keys:
                vis = self.visible()
                self.sel = vis[-1].key if vis else None
        return changed or self.any_running()

    def any_running(self) -> bool:
        return any(getattr(e, "status", "") == "running" for e in self.entries)

    def node(self, key: int | None) -> Node | None:
        for n in self.nodes:
            if n.key == key:
                return n
        return None

    def visible(self) -> list[Node]:
        return [n for n in self.nodes if not any(p in self.folded for p in n.parents)]

    def _hidden_count(self, key: int) -> int:
        return sum(1 for n in self.nodes if key in n.parents)

    # ── layout ─────────────────────────────────────────────────────────
    @property
    def body_height(self) -> int:
        return max(1, self.height - 2)

    def rows(self, now: float | None = None) -> list[Row]:
        now = self.clock() if now is None else now
        out: list[Row] = []
        w = self.width - 1  # never write the last column (no pending-wrap surprises)
        for n in self.visible():
            lines = self.layout.entry_rows(n, w, expanded=n.key in self.expanded, folded=n.key in self.folded,
                                           selected=n.key == self.sel, now=now,
                                           hidden=self._hidden_count(n.key) if n.key in self.folded else 0)
            for i, ln in enumerate(lines):
                out.append(Row(n.key, ln, header=i == 0))
        self._rows = out
        return out

    def header_index(self, key: int | None, rows: list[Row] | None = None) -> int:
        for i, r in enumerate(rows if rows is not None else self.rows()):
            if r.key == key:
                return i
        return 0

    def _clamp_top(self, rows: list[Row]) -> None:
        self.top = max(0, min(self.top, max(0, len(rows) - self.body_height)))

    def ensure_visible(self, key: int | None = None) -> None:
        rows = self.rows()
        i = self.header_index(self.sel if key is None else key, rows)
        if i < self.top:
            self.top = i
        elif i >= self.top + self.body_height:
            self.top = i - self.body_height + 1
        self._clamp_top(rows)

    def frame(self, now: float | None = None) -> list[str]:
        """The full screen: title bar, body rows, footer (each line fits ``width - 1`` columns)."""
        now = self.clock() if now is None else now
        rows = self.rows(now)
        self._clamp_top(rows)
        w = self.width - 1
        R = T.RST
        vis = self.visible()
        n_turns = sum(1 for e in self.entries if e.kind == "turn")
        n_tools = sum(1 for e in self.entries if e.kind == "tool")
        d = glyph("dot")
        left = (f"{T.PRIMARY}{T.BOLD}Transcript{R} {T.MUTED}{d} {n_turns} turn{'s' if n_turns != 1 else ''} {d} "
                f"{n_tools} tool call{'s' if n_tools != 1 else ''}{R}")
        pos = next((i for i, n in enumerate(vis, 1) if n.key == self.sel), 0)
        right = f"{T.MUTED}{pos}/{len(vis)}{R}"
        if self.any_running():
            right = f"{T.WARNING}{_spinner(now)} live{R}  " + right
        out = [_fit(left, right, w)]
        body = rows[self.top:self.top + self.body_height]
        out += [r.text for r in body]
        if not rows:
            out.append(f"  {T.MUTED}Nothing in the transcript yet {d} run a prompt first{R}")
        out += [""] * (self.body_height + 1 - len(out))
        if self.searching:
            foot = f"{T.ACCENT}/{R}{T.TEXT}{self.query}{R}{T.ACCENT}{_sym('▏', '_')}{R}"
            if self.message:
                foot += f"  {T.MUTED}{self.message}{R}"
        elif self.message:
            foot = f"{T.TEXT}{self.message}{R}"
        else:
            foot = f"{T.MUTED}{footer_help()}{R}"
            if self.query:
                foot = f"{T.MUTED}n/N next {d} {R}" + foot
        out.append(truncate(foot, w))
        return [truncate(ln, w) for ln in out[: self.height]]

    # ── actions ────────────────────────────────────────────────────────
    def toggle(self, key: int | None = None) -> None:
        key = self.sel if key is None else key
        n = self.node(key)
        if n is None:
            return
        self.auto_expanded.discard(key)
        if n.kind in FOLDABLE:
            self.folded.symmetric_difference_update({key})
        elif self.layout.expandable(n.entry):
            self.expanded.symmetric_difference_update({key})
        self.sel = key
        self.ensure_visible()

    def expand_all(self) -> None:
        self.folded.clear()
        self.expanded = {n.key for n in self.nodes if n.kind not in FOLDABLE and self.layout.expandable(n.entry)}
        self.ensure_visible()

    def collapse_all(self) -> None:
        self.expanded.clear()
        self.auto_expanded.clear()
        self.ensure_visible()

    def toggle_turns(self) -> None:
        turns = {n.key for n in self.nodes if n.kind == "turn"}
        if turns - self.folded:
            self.folded |= turns
            sel = self.node(self.sel)
            if sel is not None and sel.parents:  # keep the selection on a visible row
                self.sel = sel.parents[0]
        else:
            self.folded -= turns
        self.ensure_visible()

    def move(self, delta: int) -> None:
        vis = self.visible()
        if not vis:
            return
        keys = [n.key for n in vis]
        rows = self.rows()
        i_sel = self.header_index(self.sel, rows)
        in_view = self.top <= i_sel < self.top + self.body_height
        if not in_view:  # scrolled away with the wheel: start from what is on screen
            shown = [r.key for r in rows[self.top:self.top + self.body_height] if r.header]
            if shown:
                self.sel = shown[0] if delta > 0 else shown[-1]
                self.ensure_visible()
                return
        idx = keys.index(self.sel) if self.sel in keys else len(keys) - 1
        self.sel = keys[max(0, min(len(keys) - 1, idx + delta))]
        self.ensure_visible()

    def scroll(self, delta: int, select: bool = False) -> None:
        rows = self.rows()
        self.top += delta
        self._clamp_top(rows)
        if select:
            shown = [r.key for r in rows[self.top:self.top + self.body_height] if r.header]
            if shown:
                at_end = self.top + self.body_height >= len(rows)
                self.sel = shown[-1] if (at_end and delta > 0) else shown[0]

    def home(self) -> None:
        vis = self.visible()
        self.sel = vis[0].key if vis else None
        self.top = 0

    def end(self) -> None:
        vis = self.visible()
        if vis:
            self.sel = vis[-1].key
        rows = self.rows()
        self.top = max(0, len(rows) - self.body_height)

    def focus(self, uid: int | None, expand: bool = False) -> bool:
        """Select the entry whose ``uid`` is given (unfolding its turn/step), optionally expanded, and
        scroll it to the top of the view. False when no such entry is in the transcript."""
        n = next((n for n in self.nodes if getattr(n.entry, "uid", None) == uid), None) if uid else None
        if n is None:
            return False
        self.folded -= set(n.parents)
        if expand and n.kind not in FOLDABLE and self.layout.expandable(n.entry):
            self.expanded.add(n.key)
        self.sel = n.key
        rows = self.rows()
        self.top = self.header_index(n.key, rows)
        self._clamp_top(rows)
        return True

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = max(20, width), max(5, height)
        self.ensure_visible()

    def copy_selected(self) -> None:
        n = self.node(self.sel)
        if n is None:
            self.message = "Nothing selected"
            return
        e = n.entry
        text = e.body or e.detail or e.title
        copier = self.copier
        if copier is None:
            from axon.ui.clipboard import copy_to_clipboard as copier  # type: ignore[no-redef]
        try:
            ok = bool(text) and copier(text)
        except Exception:
            ok = False
        self.message = (f"Copied {len(text):,} chars" if ok else
                        "Nothing to copy" if not text else "No clipboard utility available (pbcopy, clip, xclip, wl-copy)")

    # ── search ─────────────────────────────────────────────────────────
    @staticmethod
    def _haystack(e: Any) -> str:
        return "\n".join(str(getattr(e, f, "") or "") for f in ("title", "summary", "body", "detail")).lower()

    def matches(self) -> list[int]:
        q = self.query.lower()
        if not q:
            return []
        return [n.key for n in self.nodes if q in self._haystack(n.entry)]

    def find(self, forward: bool = True, include_current: bool = False, origin: int | None = None) -> bool:
        hits = self.matches()
        if not hits:
            self.message = f"no match for {self.query!r}" if self.query else ""
            return False
        order = [n.key for n in self.nodes]
        start_key = self.sel if origin is None else origin
        start = order.index(start_key) if start_key in order else -1
        seq = order[start:] + order[:start] if forward else list(reversed(order[:start + 1])) + list(reversed(order[start + 1:]))
        if not include_current and seq and seq[0] == start_key:
            seq = seq[1:] + seq[:1]
        target = next((k for k in seq if k in set(hits)), hits[0])
        self._land(target)
        self.message = f"match {hits.index(target) + 1}/{len(hits)}"
        return True

    def _land(self, key: int) -> None:
        for k in list(self.auto_expanded):
            if k != key:
                self.expanded.discard(k)
                self.auto_expanded.discard(k)
        n = self.node(key)
        if n is None:
            return
        self.folded -= set(n.parents)
        e = n.entry
        q = self.query.lower()
        head = f"{e.title}\n{e.summary}".lower()
        if q and q not in head and n.kind not in FOLDABLE and key not in self.expanded and self.layout.expandable(e):
            self.expanded.add(key)
            self.auto_expanded.add(key)
        self.sel = key
        self.ensure_visible()
        if q and key in self.expanded:  # bring the matching line into view
            rows = self.rows()
            first = self.header_index(key, rows)
            for i in range(first, len(rows)):
                if rows[i].key != key:
                    break
                if q in strip_ansi(rows[i].text).lower():
                    if not self.top <= i < self.top + self.body_height:
                        self.top = max(first, i - self.body_height // 2) if i - first >= self.body_height else first
                        self._clamp_top(rows)
                    break

    def _search_key(self, key: str) -> None:
        if key in ("esc", "ctrl-c", "ctrl-g"):
            self.searching = False
            self.message = ""
            return
        if key == "enter":
            self.searching = False
            if self.query and not self.matches():
                self.message = f"no match for {self.query!r}"
            return
        if key == "backspace":
            self.query = self.query[:-1]
        elif key.startswith(plat.PASTE_PREFIX):
            self.query += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
        elif len(key) == 1 and key >= " ":
            self.query += key
        else:
            return
        if self.query:
            self.find(forward=True, include_current=True, origin=self._search_origin)
        else:
            self.message = ""

    # ── input ──────────────────────────────────────────────────────────
    def handle_mouse(self, ev: Any) -> None:
        if ev.kind == "wheel-up":
            self.scroll(-3)
        elif ev.kind == "wheel-down":
            self.scroll(3)
        elif ev.kind == "press" and ev.button == 0:
            idx = self.top + (ev.y - 2)  # row 1 is the title bar
            rows = self._rows or self.rows()
            if 1 < ev.y <= 1 + self.body_height and 0 <= idx < len(rows):
                self.toggle(rows[idx].key)

    def handle_key(self, key: str) -> bool:
        """Apply one key; False when the viewer should close."""
        if key.startswith(plat.MOUSE_PREFIX):
            ev = plat.parse_mouse(key)
            if ev is not None:
                self.handle_mouse(ev)
            return True
        if self.searching:
            self._search_key(key)
            return True
        self.message = ""
        if key in ("q", "Q", "esc", "ctrl-c", OPEN_KEY):  # the key that opened the view closes it too
            self.done = True
            return False
        if key in ("down", "j", "ctrl-n"):
            self.move(1)
        elif key in ("up", "k", "ctrl-p"):
            self.move(-1)
        elif key in ("enter", " ", "tab", "o"):
            self.toggle()
        elif key == "right" or key == "l":
            n = self.node(self.sel)
            if n is not None and (n.key in self.folded or (n.kind not in FOLDABLE and n.key not in self.expanded)):
                self.toggle()
        elif key == "left" or key == "h":
            n = self.node(self.sel)
            if n is not None and (n.key in self.expanded or (n.kind in FOLDABLE and n.key not in self.folded)):
                self.toggle()
            elif n is not None and n.parents:
                self.sel = n.parents[-1]
                self.ensure_visible()
        elif key == "e":
            self.expand_all()
        elif key == "c":
            self.collapse_all()
        elif key == "t":
            self.toggle_turns()
        elif key in ("pagedown", "f"):
            self.scroll(self.body_height - 1, select=True)
        elif key in ("pageup", "b"):
            self.scroll(-(self.body_height - 1), select=True)
        elif key == "ctrl-d":
            self.scroll(self.body_height // 2, select=True)
        elif key == "ctrl-u":
            self.scroll(-(self.body_height // 2), select=True)
        elif key in ("g", "home"):
            self.home()
        elif key in ("G", "end"):
            self.end()
        elif key == "/":
            self.searching = True
            self._search_origin = self.sel
            self.query = ""
        elif key == "n":
            if self.query:
                self.find(forward=True)
        elif key == "N":
            if self.query:
                self.find(forward=False)
        elif key == "y":
            self.copy_selected()
        return True

    # ── terminal loop ──────────────────────────────────────────────────
    def draw(self) -> None:
        lines = self.frame()
        buf = ["\x1b[?25l"]
        for i, ln in enumerate(lines, 1):
            buf.append(f"\x1b[{i};1H{ln}{T.RST}\x1b[K")
        buf.append("\x1b[J")
        _write("".join(buf))

    def run(self, mouse: bool | None = None) -> None:
        """Interactive loop on the alternate screen. Always restores the terminal."""
        use_mouse = (not plat.IS_WINDOWS) if mouse is None else mouse
        was_on = plat.mouse_reporting()      # opened from the prompt by a click: it stays on afterwards
        plat.setup_console()
        with plat.raw_terminal(isig=False):
            try:
                _write(plat.ENTER_ALT_SCREEN + plat.HIDE_CURSOR)
                plat.set_mouse_reporting(use_mouse)
                width, height = plat.term_size()
                if (width, height) != (self.width, self.height):
                    self.resize(width, height)
                self.draw()
                while not self.done:
                    try:
                        key = plat.read_key(timeout=0.25 if self.any_running() else 1.0)
                    except EOFError:
                        break
                    if key is None:
                        if self.refresh():
                            self.draw()
                        continue
                    if key == "resize":
                        self.resize(*plat.term_size())
                        self.draw()
                        continue
                    if not self.handle_key(key):
                        break
                    self.refresh()
                    self.draw()
            finally:
                plat.set_mouse_reporting(was_on and use_mouse)
                _write(plat.EXIT_ALT_SCREEN + plat.SHOW_CURSOR)


def _write(s: str) -> None:
    try:
        with T.OUTPUT_LOCK:
            import sys
            sys.stdout.write(s)
            sys.stdout.flush()
    except Exception:
        pass


# ─── entry points ──────────────────────────────────────────────────────────

def transcript_source(agent: Any = None) -> tuple[Callable[[], list[Any]] | list[Any], bool]:
    """(source, live): the renderer's transcript when it has entries, else entries rebuilt from the
    agent's conversation (e.g. right after ``/resume``)."""
    from axon.ui.render import get_transcript
    if get_transcript():
        return get_transcript, True
    msgs = getattr(getattr(agent, "conversation", None), "messages", None) or []
    return entries_from_messages(msgs, registry=getattr(agent, "tools", None)), False


def plain_dump(entries: list[Any], width: int = 100, registry: Any = None) -> list[str]:
    """Collapsed rows without the selection gutter (non-interactive output of /transcript)."""
    v = TranscriptViewer(entries, width=width, height=10_000, registry=registry)
    v.sel = None
    return [r.text[2:] if r.text.startswith("  ") else r.text for r in v.rows()]


def open_transcript_viewer(agent: Any = None, focus: int | None = None, expand: bool = False) -> bool:
    """Open the full-screen viewer; on a non-interactive terminal print the collapsed transcript.
    ``focus`` selects the entry with that ``uid`` (``expand``: opened up), else the view starts at
    the end. Returns True when the interactive viewer ran."""
    source, _live = transcript_source(agent)
    registry = getattr(agent, "tools", None)
    if not plat.is_interactive() or not plat.supports_alt_screen():
        entries = source() if callable(source) else source
        if not entries:
            print(f"\n  {T.MUTED}Nothing in the transcript yet.{T.RST}\n")
            return False
        print()
        for ln in plain_dump(list(entries), width=min(T.term_width(), 140) - 2, registry=registry):
            print("  " + ln)
        print()
        return False
    cols, rows = plat.term_size()
    viewer = TranscriptViewer(source, width=cols, height=rows, registry=registry)
    if focus is not None:
        viewer.focus(focus, expand=expand)
    viewer.run()
    return True


__all__ = [
    "Layout", "Node", "Row", "TranscriptViewer", "build_nodes", "elapsed_text", "entries_from_messages",
    "fmt_secs", "footer_help", "open_transcript_viewer", "plain_dump", "transcript_source",
]
