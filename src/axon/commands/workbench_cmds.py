"""The terminal side of the workbench: the same facts the web app's side panels show.

* ``/diff`` (alias ``/changes``): the Changes screen, ``main -> working tree``, file list with ``+n -n``,
  inline diffs, compare targets, stage / unstage / discard / open / add to prompt.
* ``/worktree`` (alias ``/wt``): worktrees with branch and change counts; switch, create, remove.
* ``/context``: the legend (used %, one segmented bar, a row per category), lists that expand.
* ``/mcp servers``: connectors with state and tool counts; tool names only inside an expanded server.
* ``/workbench`` (alias ``/panel``): one quick-summary screen of all of it, one key to jump to each part.

The numbers come from plain modules shared with the web app: ``axon.features.worktree_view`` (git) and
``axon.features.workbench_data`` (context, connectors, limits, plan, tasks). This module only lays
them out: every function returning lines takes a *width* and never returns a line wider than it, so the
same code serves the screens, a pipe and a narrow terminal. With ``NO_COLOR`` the bar uses a letter per
category instead of a colour.
"""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any, Callable

from axon import platform as plat
from axon.commands.registry import CommandResult, command
from axon.features import workbench_data as wd
from axon.features import worktree_view as wv
from axon.ui import theme as _theme
from axon.ui.picker import Selector
from axon.ui.theme import (
    BOLD, CYAN, DARK_SLATE, GOLD, GRAY_BG, LBLUE, MINT, PURPLE, RST, ROSE, SLATE, TEAL, WHITE,
    display_width, middle_ellipsis, pad, strip_ansi, truncate,
)

CAT = "Code & Review"
MAX_DIFF_ROWS = 300            # rows of one file's diff shown inline
SHOW_SERVERS = 3               # connectors listed before "Show more"
LIST_PREVIEW = 8               # entries of an expanded context list before "+N more"


# ─── small helpers ──────────────────────────────────────────────────────────

def tok(n: Any) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1000:.1f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def _uni() -> bool:
    try:
        return _theme.supports_unicode()
    except Exception:
        return False


def _minus() -> str:
    return "−" if _uni() else "-"


def _color() -> bool:
    return bool(getattr(_theme, "COLOR_ENABLED", True)) and bool(RST)


def _fit(text: str, width: int) -> str:
    return truncate(text, width) if display_width(text) > width else text


def _lr(left: str, right: str, width: int) -> str:
    """*left* and *right* on one line, right-aligned, left shortened when they do not both fit."""
    rw = display_width(right)
    room = max(1, width - rw - 2)
    left = _fit(left, room)
    return left + " " * max(2, width - display_width(left) - rw) + right


def _rule(width: int) -> str:
    return f"{DARK_SLATE}{('─' if _uni() else '-') * max(1, width)}{RST}"


def _section(title: str, width: int, right: str = "", key: str = "") -> str:
    k = f" {DARK_SLATE}[{key}]{RST}" if key else ""
    head = f"{GOLD}{BOLD}{title}{RST}{k}"
    return _lr(head, f"{SLATE}{right}{RST}", width) if right else head


def _ws(agent: Any) -> Path:
    return Path(getattr(agent.settings, "workspace", Path.cwd()))


def _real_used(agent: Any) -> int | None:
    """Tokens the provider counted on the last call (None before the first call)."""
    try:
        used = int((getattr(getattr(agent, "renderer", None), "last_context", (0, 0)) or (0, 0))[0] or 0)
        return used or None
    except Exception:
        return None


# ─── context ────────────────────────────────────────────────────────────────

CAT_STYLE: dict[str, tuple[str, str]] = {      # key -> (colour, letter used when there is no colour)
    "messages": (LBLUE, "M"), "system_tools": (GOLD, "T"), "mcp_tools": (MINT, "P"), "skills": (PURPLE, "K"),
    "system_prompt": (SLATE, "S"), "mcp_instructions": (CYAN, "I"), "custom_agents": (TEAL, "A"),
    "memory_files": (WHITE, "F"), "autocompact": (DARK_SLATE, "x"), "free": (DARK_SLATE, "."),
}
EXPANDABLE = {"mcp_tools": "MCP tools", "skills": "Skills", "memory_files": "Memory files", "custom_agents": "Custom agents",
              "messages": "Messages"}
ALIASES = {"mcp": "mcp_tools", "tools": "mcp_tools", "skills": "skills", "memory": "memory_files", "files": "memory_files",
           "agents": "custom_agents", "messages": "messages", "msgs": "messages"}


def _swatch(key: str) -> str:
    colour, letter = CAT_STYLE[key]
    if _color():
        return f"{colour}{'■' if _uni() else '#'}{RST}"
    return letter


def _allocate(rows: list[dict[str, Any]], window: int, cells: int) -> list[int]:
    """Cells per row by largest remainder; a non-empty row always gets at least one cell."""
    if window <= 0 or cells <= 0:
        return [0] * len(rows)
    raw = [r["tokens"] / window * cells for r in rows]
    out = [int(x) for x in raw]
    for i, r in enumerate(rows):
        if r["tokens"] > 0 and out[i] == 0:
            out[i] = 1
    while sum(out) > cells:
        i = max(range(len(out)), key=lambda j: out[j] if raw[j] - out[j] < 0.5 or out[j] > 1 else -1)
        out[i] -= 1
    left = cells - sum(out)
    order = sorted(range(len(rows)), key=lambda j: -(raw[j] - int(raw[j])))
    for j in order[:max(0, left)]:
        out[j] += 1
    return out


def context_bar(data: dict[str, Any], cells: int) -> str:
    alloc = _allocate(data["rows"], data["window"], cells)
    parts = []
    for r, n in zip(data["rows"], alloc):
        if n <= 0:
            continue
        colour, letter = CAT_STYLE[r["key"]]
        if _color():
            ch = "█" if _uni() else "#"
            if r["key"] == "free":
                ch = "░" if _uni() else "."
            elif r["key"] == "autocompact":
                ch = "▒" if _uni() else "x"
            parts.append(f"{colour}{ch * n}{RST}")
        else:
            parts.append(letter * n)
    return "".join(parts)


def _pct(v: float) -> str:
    return f"{v:.1f}%"


def context_lines(data: dict[str, Any], width: int, expanded: set[str] | frozenset[str] = frozenset(),
                  focus: str = "", with_keys: bool = False) -> list[tuple[str, str | None]]:
    """The context legend as (line, focus-key) pairs; rows that can expand carry a key. Estimates are
    marked with ``~``; the window and (when the provider reported it) the used total are exact."""
    out: list[tuple[str, str | None]] = []
    window, used = data["window"], data["used"]
    est = "" if data["calibrated"] else "~"
    out.append((_lr(f"{WHITE}{BOLD}{data['pct']}% used{RST}", f"{SLATE}{est}{tok(used)} / {tok(window)}{RST}", width), None))
    bar_cells = max(10, min(width, 100))
    out.append((context_bar(data, bar_cells), None))
    out.append(("", None))
    rows = data["rows"]
    num_w = max(len(tok(r["tokens"])) for r in rows) + 1
    pct_w = 6
    label_w = max(8, width - 2 - num_w - pct_w - 4)
    for r in rows:
        est_row = r["key"] not in ("autocompact",) and not (r["key"] == "messages" and data["calibrated"])
        if r["key"] == "free" and data["calibrated"]:
            est_row = False
        mark = "~" if est_row else ""
        label = _fit(r["label"], label_w)
        line = (f"{_swatch(r['key'])} {SLATE if r['key'] not in ('free',) else DARK_SLATE}{pad(label, label_w)}{RST} "
                f"{WHITE}{pad(mark + tok(r['tokens']), num_w, 'right')}{RST} {SLATE}{pad(_pct(r['pct']), pct_w, 'right')}{RST}")
        out.append((line, None))
    d = data["deferred"]
    if d["mcp_tools"]:
        out.append(("", None))
        label = _fit("MCP tools (deferred)", label_w)
        out.append((f"{DARK_SLATE}{'■' if _uni() and _color() else ' '}{RST} {SLATE}{pad(label, label_w)}{RST} "
                    f"{SLATE}{pad('~' + tok(d['mcp_tools']), num_w, 'right')}{RST} {DARK_SLATE}{pad('—' if _uni() else '-', pct_w, 'right')}{RST}", None))
        out.append((_fit(f"  {DARK_SLATE}{d['mcp_count']} tools load on demand (ToolSearch), not counted above.{RST}", width), None))
    lists = data["lists"]
    shown = [(k, lbl) for k, lbl in EXPANDABLE.items() if lists.get(k)]
    if shown:
        out.append(("", None))
    for k, lbl in shown:
        items = lists[k]
        count = sum(g["count"] for g in items) if k == "mcp_tools" else len(items)
        is_open = k in expanded
        arrow = ("⌄" if is_open else "›") if _uni() else ("v" if is_open else ">")
        line = _lr(f"{WHITE if focus == k else SLATE}{arrow} {lbl}{RST}", f"{SLATE}{count:,}{RST}", width)
        out.append((line, k))
        if is_open:
            out += [(x, None) for x in _expanded_list(k, items, width)]
    out.append(("", None))
    note = "~ estimated (about 3.7 characters per token)"
    if data["calibrated"]:
        note += "; used total and Messages come from the provider's last call"
    out.append((f"{DARK_SLATE}{_fit(note, width)}{RST}", None))
    _ = with_keys
    return out


def _expanded_list(key: str, items: list[dict[str, Any]], width: int) -> list[str]:
    lines: list[str] = []
    if key == "mcp_tools":
        rows = [(g["name"], f"{g['count']} tools" + (f", {g['deferred']} deferred" if g.get("deferred") else ""), g["tokens"]) for g in items]
    elif key == "messages":
        rows = [(i["label"] + (" (pinned)" if i.get("pinned") else " (dropped)" if i.get("dropped") else ""), "", i["tokens"]) for i in items]
    elif key == "memory_files":
        rows = [(i["name"], str(i.get("scope") or ""), i["tokens"]) for i in items]
    else:
        rows = [(i["name"], "", i["tokens"]) for i in items]
    for name, extra, t in rows[:LIST_PREVIEW]:
        right = f"{extra + '  ' if extra else ''}~{tok(t)}"
        lines.append(_lr(f"    {DARK_SLATE}{middle_ellipsis(name, max(8, width - display_width(right) - 8))}{RST}", f"{DARK_SLATE}{right}{RST}", width))
    if len(rows) > LIST_PREVIEW:
        lines.append(f"    {DARK_SLATE}+{len(rows) - LIST_PREVIEW} more{RST}")
    return lines


def context_data(agent: Any) -> dict[str, Any]:
    return wd.context_breakdown(agent, _real_used(agent))


def print_context(agent: Any, expand: set[str], width: int) -> None:
    from axon.commands.common import header, kv
    from axon.commands.context_cmds import _budget
    data = context_data(agent)
    window, threshold, reserve = _budget(agent)
    header("Context usage", f"{agent.settings.model} · window {tok(window)}")
    print()
    for line, _ in context_lines(data, width, expand):
        print("  " + line if line else "")
    kv("Auto-compact at", f"{tok(threshold)} ({threshold * 100 // max(1, window)}% of window)")
    if reserve:
        kv("Reply reserve", f"{tok(reserve)} (usable for the prompt: {tok(window - reserve)})")
    turns = getattr(agent.settings, "max_history_turns", 0) or 0
    kv("Sliding window", f"{turns} turns" if turns else "unlimited")
    last = getattr(getattr(agent, "ledger", None), "last_usage", None)
    if last is not None and getattr(last, "input", 0):
        kv("Last API call", f"{last.input:,} input tokens (billed)")
    print(f"\n  {DARK_SLATE}/context mcp|skills|memory|agents|messages|all   expands a list{RST}")
    print(f"  {DARK_SLATE}/context --why explains each block · /compact frees space{RST}\n")


# ─── connectors ─────────────────────────────────────────────────────────────

_STATE = {"connected": ("connected", MINT), "connecting": ("connecting", GOLD), "reconnecting": ("reconnecting", GOLD),
          "disabled": ("disabled", SLATE), "needs-auth": ("needs sign-in", GOLD), "needs-config": ("needs setup", GOLD),
          "pending-approval": ("needs approval", GOLD), "rejected": ("rejected", ROSE), "failed": ("failed", ROSE),
          "not connected": ("not connected", SLATE), "disconnected": ("disconnected", ROSE), "blocked": ("blocked", ROSE)}


def connector_lines(data: dict[str, Any], width: int, expanded: set[str] = frozenset(), show_all: bool = False,
                    compact: bool = False) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    servers = data["servers"]
    if not servers:
        return [(_fit(f"{SLATE}No connectors yet. /marketplace browses them, /mcp add adds one.{RST}", width), None)]
    summary = f"{data['connected']} of {len(servers)} connected" + (f" · {data['reconnect']} need attention" if data["reconnect"] else "")
    out.append((f"{SLATE}{summary}{RST}", None))
    # servers that need you come first, then the rest alphabetically
    ordered = sorted(servers, key=lambda s: (not s["needs_reconnect"], s["name"]))
    visible = ordered if show_all else ordered[:SHOW_SERVERS]
    for s in visible:
        word, colour = _STATE.get(s["status"], (s["status"], SLATE))
        is_open = s["name"] in expanded
        arrow = ("⌄" if is_open else "›") if _uni() else ("v" if is_open else ">")
        action = ""
        if s["needs_auth"]:
            action = f"{GOLD}sign in (l){RST}"
        elif s["status"] == "pending-approval":
            action = f"{GOLD}approve (l){RST}"
        elif s["needs_reconnect"] and s["status"] != "disabled":
            action = f"{GOLD}reconnect (r){RST}"
        n = s["tools"]
        tools = f"{n} tool{'s' if n != 1 else ''}" if s["status"] == "connected" and n is not None else ""
        right = "  ".join(x for x in (action, f"{colour}{word}{RST}" if not action else "", f"{DARK_SLATE}{tools}{RST}" if tools else "") if x)
        out.append((_lr(f"{WHITE}{arrow} {s['name']}{RST}", right, width), s["name"]))
        if is_open:
            if s["error"]:
                out.append((f"    {ROSE}{_fit(s['error'], width - 4)}{RST}", None))
            names = s["tool_names"]
            if names:
                for chunk in _wrap_names(names[:60], width - 4):
                    out.append((f"    {DARK_SLATE}{chunk}{RST}", None))
                if len(names) > 60:
                    out.append((f"    {DARK_SLATE}+{len(names) - 60} more{RST}", None))
            elif s["status"] == "connected":
                out.append((f"    {DARK_SLATE}No tools exposed.{RST}", None))
            else:
                out.append((f"    {DARK_SLATE}Tool names appear once it is connected.{RST}", None))
    if not show_all and len(ordered) > SHOW_SERVERS:
        n_more = len(ordered) - SHOW_SERVERS
        out.append((f"{DARK_SLATE}+{n_more} more · k opens the list{RST}", None) if compact else
                   (f"{SLATE}  Show more ({n_more}){RST}", "__more__"))
    return out


def _wrap_names(names: list[str], width: int) -> list[str]:
    lines, cur = [], ""
    for n in names:
        piece = (", " if cur else "") + n
        if cur and display_width(cur + piece) > width:
            lines.append(cur + ",")
            cur = n
        else:
            cur += piece
    if cur:
        lines.append(cur)
    return [_fit(x, width) for x in lines]


# ─── changes ────────────────────────────────────────────────────────────────

_CODE_COLOR = {"M": GOLD, "A": MINT, "D": ROSE, "U": CYAN, "R": PURPLE, "C": ROSE}


def counts_text(added: int, removed: int, binary: bool = False) -> str:
    if binary:
        return f"{DARK_SLATE}bin{RST}"
    parts = []
    if added or not removed:
        parts.append(f"{MINT}+{added:,}{RST}")
    if removed:
        parts.append(f"{ROSE}{_minus()}{removed:,}{RST}")
    return " ".join(parts)


def change_row(f: dict[str, Any], selected: bool, width: int, is_open: bool = False, arrow_on: bool = True) -> str:
    """``❯ M ● src/axon/ file.py                 +12 −3`` (the folder dim, the name bold; shortened to fit)."""
    pointer = f"{MINT}{BOLD}❯{RST}" if selected else " "
    arrow = ("⌄" if is_open else "›") if _uni() else ("v" if is_open else ">")
    arrow = arrow if arrow_on else " "
    colour = _CODE_COLOR.get(f["code"], SLATE)
    staged = f"{MINT}●{RST}" if f.get("staged") and _uni() else (f"{MINT}*{RST}" if f.get("staged") else " ")
    head = f"{pointer} {SLATE}{arrow}{RST} {colour}{BOLD}{f['code']}{RST} {staged} "
    right = counts_text(f["added"], f["removed"], f.get("binary", False))
    room = max(8, width - display_width(head) - display_width(right) - 2)
    folder = (f["dir"] + "/") if f["dir"] else ""
    name = f["name"]
    if display_width(folder + name) > room:
        folder = middle_ellipsis(folder.rstrip("/"), max(3, room - display_width(name) - 1)) + "/" if folder else ""
        if display_width(folder + name) > room:
            folder, name = "", middle_ellipsis(name, room)
    name_style = f"{BOLD}{WHITE}" if selected else WHITE
    body = f"{DARK_SLATE}{folder}{RST}{name_style}{name}{RST}"
    gap = max(1, width - display_width(head) - display_width(body) - display_width(right))
    return head + body + " " * gap + right


def _lang(path: str) -> str:
    try:
        from axon.ui.markdown import lang_for_path
        return lang_for_path(path)
    except Exception:
        return ""


def diff_row(row: dict[str, Any], lang: str, num_w: int, width: int) -> str:
    """One numbered diff line, tinted for added/removed lines, syntax-coloured, never wider than *width*."""
    kind = row["kind"]
    if kind == "hunk":
        return f"{DARK_SLATE}{' ' * num_w}   {_fit(row['text'], max(4, width - num_w - 4))}{RST}"
    if kind == "meta":
        return f"{DARK_SLATE}{' ' * num_w}   {_fit(row['text'], max(4, width - num_w - 4))}{RST}"
    n = row["old"] if kind == "del" else row["new"]
    gutter = f"{n:>{num_w}}" if n else " " * num_w
    sign = {"add": "+", "del": "-", "ctx": " "}[kind]
    text = row["text"].expandtabs(4)
    room = max(4, width - num_w - 4)
    text = _fit_plain(text, room)
    if not _color():
        return f"{gutter} {sign} {text}"
    if kind == "ctx":
        return f"{DARK_SLATE}{gutter}{RST}   {_hl(text, lang)}"
    fg, bg = (getattr(_theme, "DIFF_ADD", MINT), getattr(_theme, "DIFF_ADD_BG", "")) if kind == "add" else \
        (getattr(_theme, "DIFF_DEL", ROSE), getattr(_theme, "DIFF_DEL_BG", ""))
    fill = " " * max(0, room - display_width(text))
    return f"{bg}{fg}{gutter} {sign} {RST}{_hl(text, lang, bg)}{bg}{fill}{RST}"


def _fit_plain(text: str, room: int) -> str:
    return text if display_width(text) <= room else truncate(text, room)


def _hl(text: str, lang: str, bg: str = "") -> str:
    if not text:
        return ""
    if lang:
        try:
            from axon.ui.markdown import highlight_spans
            return "".join(f"{bg}{style}{piece}{RST}" for style, piece in highlight_spans(text, lang))
        except Exception:
            pass
    return f"{bg}{text}{RST}" if bg else text


def changes_summary_line(ch: dict[str, Any]) -> str:
    n = len(ch["files"])
    return f"{n} file{'s' if n != 1 else ''}  {counts_text(ch['added'], ch['removed'])}"


def changes_text(ch: dict[str, Any], width: int, limit: int = 40) -> list[str]:
    """The Changes list as plain lines (for a pipe, or ``/diff list``)."""
    if ch["error"]:
        return [f"{ROSE}{ch['error']}{RST}"]
    out = [_lr(f"{GOLD}{BOLD}{ch['label']}{RST}", changes_summary_line(ch), width)]
    if not ch["files"]:
        out.append(f"{MINT}Nothing changed.{RST}")
    for f in ch["files"][:limit]:
        out.append(change_row(f, False, width, arrow_on=False))
    if len(ch["files"]) > limit:
        out.append(f"{DARK_SLATE}  +{len(ch['files']) - limit} more files{RST}")
    return out


class _Screen(Selector):
    """Full-screen base: title, optional rows of chips, a body, a rule, a message/prompt line and key hints."""

    def __init__(self, title: str) -> None:
        super().__init__([], title=title, fullscreen=True)
        self.action: Any = None
        self.status = ""
        self.status_color = MINT

    def say(self, text: str, color: str = MINT) -> None:
        self.status, self.status_color = text, color

    def _keys(self, keys: list[str], width: int) -> list[str]:
        lines, cur = [], ""
        for k in keys:
            nxt = (cur + " · " if cur else "") + k
            if cur and display_width(nxt) > width - 2:
                lines.append(cur)
                cur = k
            else:
                cur = nxt
        if cur:
            lines.append(cur)
        return [f"  {DARK_SLATE}{_fit(x, width - 2)}{RST}" for x in lines]

    def chrome(self, head: list[str], body: Callable[[int, int], list[str]], keys: list[str]) -> tuple[list[str], int | None, int]:
        cols, rows = plat.term_size()
        width = max(20, cols - 2)
        foot_keys = self._keys(keys, width)
        room = max(3, rows - 1 - len(head) - 2 - len(foot_keys))
        view = body(width, room)[:room]
        lines = [_fit(h, width) for h in head] + view + [""] * (room - len(view))
        lines.append(f"  {_rule(width - 2)}")
        cur_row: int | None = None
        cur_col = 0
        if self._prompt is not None:
            label, buf = self._prompt
            lines.append(f"  {label}{WHITE}{buf}{RST}")
            cur_row, cur_col = len(lines) - 1, 2 + display_width(strip_ansi(label)) + display_width(buf)
        elif self.status:
            lines.append(_fit(f"  {self.status_color}{self.status}{RST}", width))
        else:
            lines.append("")
        lines += foot_keys
        return lines[:max(1, rows - 1)], cur_row, cur_col

    def _clear_status_on_key(self) -> None:
        self.status = ""

    def resume(self) -> Any:
        self._done = False
        self._result = None
        self.action = None
        return self.run()


# ─── the Changes screen ─────────────────────────────────────────────────────

TARGET_KEYS = {"1": "working", "2": "staged", "3": "commit", "4": "branch"}


class ChangesScreen(_Screen):
    """``main -> working tree``: files, inline diffs, compare targets, stage/unstage/discard/open/add to prompt."""

    def __init__(self, agent: Any, target: str = "working", ref: str = "") -> None:
        super().__init__("Changes")
        self.agent = agent
        self.ws = _ws(agent)
        self.target = target
        self.ref = ref
        self.query = ""
        self.typing = False
        self.top = 0
        self.reveal = False
        self.expanded: set[str] = set()
        self.diffs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.ch: dict[str, Any] = {"files": [], "added": 0, "removed": 0, "error": "", "label": ""}
        self.view: list[dict[str, Any]] = []
        self.index = 0
        self.reload()

    # data
    def reload(self, keep: str = "") -> None:
        keep = keep or (self.view[self.index]["path"] if self.view and self.index < len(self.view) else "")
        self.ch = wv.changes(self.ws, self.target, self.ref)
        self.diffs.clear()
        self.refilter(keep)

    def refilter(self, keep: str = "") -> None:
        q = self.query.lower().split()
        self.view = [f for f in self.ch["files"] if all(t in f["path"].lower() for t in q)]
        self.index = next((i for i, f in enumerate(self.view) if f["path"] == keep), min(self.index, max(0, len(self.view) - 1)))

    def cur(self) -> dict[str, Any] | None:
        return self.view[self.index] if self.view else None

    def diff_of(self, f: dict[str, Any]) -> dict[str, Any]:
        key = (f["path"], self.target, self.ref)
        if key not in self.diffs:
            self.diffs[key] = wv.file_diff(self.ws, f["path"], self.target, self.ref, f["code"])
        return self.diffs[key]

    # layout: a flat list of (file index, diff row or None); only the visible slice is turned into text
    def _layout(self) -> list[tuple[int, int]]:
        flat: list[tuple[int, int]] = []
        for i, f in enumerate(self.view):
            flat.append((i, -1))
            if f["path"] in self.expanded:
                n = min(len(self.diff_of(f)["rows"]), MAX_DIFF_ROWS)
                extra = 1 if (len(self.diff_of(f)["rows"]) > MAX_DIFF_ROWS or self.diff_of(f)["note"] or self.diff_of(f)["truncated"]) else 0
                flat += [(i, r) for r in range(n + extra)]
        return flat

    def _body(self, width: int, room: int) -> list[str]:
        return ["  " + x for x in self._rows(width - 2, room)]

    def _rows(self, width: int, room: int) -> list[str]:
        if self.ch["error"]:
            return ["", f"{ROSE}{self.ch['error']}{RST}"]
        if not self.view:
            return ["", f"{MINT}{'Nothing changed.' if not self.ch['files'] else 'No file matches the filter.'}{RST}"]
        flat = self._layout()
        sel_line = next(n for n, (i, r) in enumerate(flat) if i == self.index and r == -1)
        # keep the selected row on screen, and (when expanded) as much of its diff as fits
        if self.reveal:                                      # just expanded: put the file at the top so its diff shows
            self.reveal = False
            self.top = sel_line
        if sel_line < self.top:
            self.top = sel_line
        elif sel_line >= self.top + room:
            self.top = sel_line - room + 1
        self.top = max(0, min(self.top, max(0, len(flat) - room)))
        out: list[str] = []
        for n in range(self.top, min(len(flat), self.top + room)):
            i, r = flat[n]
            f = self.view[i]
            if r == -1:
                out.append(change_row(f, i == self.index, width, f["path"] in self.expanded))
                continue
            d = self.diff_of(f)
            rows = d["rows"]
            if r >= min(len(rows), MAX_DIFF_ROWS):
                more = len(rows) - MAX_DIFF_ROWS
                note = d["note"] or (f"+{more} more lines; press o to open the file" if more > 0 else "") or "Diff cut short."
                out.append(f"    {DARK_SLATE}{note}{RST}")
                continue
            nums = [x for x in (rows[r]["old"], rows[r]["new"]) if x]
            num_w = max(3, len(str(max([x for row in rows[:MAX_DIFF_ROWS] for x in (row["old"], row["new"]) if x] or [1]))))
            _ = nums
            out.append("    " + diff_row(rows[r], _lang(f["path"]), num_w, width - 4))
        if self.top > 0 and out and sel_line != self.top:
            out[0] = f"{DARK_SLATE}↑ {self.top} lines above{RST}"
        below = len(flat) - self.top - len(out)
        if below > 0 and out and sel_line != self.top + len(out) - 1:
            out[-1] = f"{DARK_SLATE}↓ {below + 1} more lines{RST}"
        return out

    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, _ = plat.term_size()
        width = max(20, cols - 2)
        head = [_lr(f"  {GOLD}◆{RST} {BOLD}{WHITE}Changes{RST}  {SLATE}{self.ch['label']}{RST}",
                    changes_summary_line(self.ch) + "  ", width)]
        chips = []
        for key, t in TARGET_KEYS.items():
            name = {"working": "working tree", "staged": "staged", "commit": "last commit",
                    "branch": f"branch {self.ref}".strip()}[t]
            chips.append(f"{GRAY_BG}{BOLD}{WHITE} {key} {name} {RST}" if t == self.target else f"{SLATE} {key} {name} {RST}")
        head.append("  " + "".join(chips))
        if self.typing or self.query:
            cursor = "▏" if self.typing else ""
            head.append(f"  {CYAN}⌕{RST} {WHITE}{self.query}{cursor}{RST}" + (f"  {DARK_SLATE}{len(self.view)} of {len(self.ch['files'])}{RST}" if self.query else ""))
        head.append(f"  {_rule(width - 2)}")
        keys = ["↑↓ move", "enter diff", "1-4 compare", "/ filter", "s stage", "u unstage", "x discard", "o open",
                "a add to prompt", "r refresh", "esc close"]
        if self.typing:
            keys = ["type to filter", "enter keep", "esc clear"]
        return self.chrome(head, self._body, keys)

    # keys
    def _default_key(self, key: str) -> None:
        if self.typing:
            self._typing_key(key)
            return
        f = self.cur()
        _, rows = plat.term_size()
        page = max(3, rows - 8)
        if key in ("esc", "ctrl-c", "q"):
            if self.query and key == "esc":
                self.query = ""
                self.refilter()
                return
            self.finish(None)
        elif key in ("up", "k", "ctrl-p"):
            self.index = max(0, self.index - 1)
        elif key in ("down", "j", "ctrl-n"):
            self.index = min(max(0, len(self.view) - 1), self.index + 1)
        elif key == "pageup":
            self.index = max(0, self.index - page)
        elif key == "pagedown":
            self.index = min(max(0, len(self.view) - 1), self.index + page)
        elif key == "home":
            self.index = 0
        elif key == "end":
            self.index = max(0, len(self.view) - 1)
        elif key in ("enter", "right", "space", " ") and f:
            self.expanded ^= {f["path"]}
            self.reveal = f["path"] in self.expanded
        elif key == "left" and f:
            self.expanded.discard(f["path"])
        elif key == "/":
            self.typing = True
        elif key in TARGET_KEYS:
            self.set_target(TARGET_KEYS[key])
        elif key == "t":
            order = list(wv.TARGETS)
            self.set_target(order[(order.index(self.target) + 1) % len(order)])
        elif key == "r":
            self.reload()
            self.say("Refreshed.")
        elif f and key in ("s", "u", "x", "o", "a"):
            self._act(key, f)

    def _typing_key(self, key: str) -> None:
        if key == "esc" or key == "ctrl-c":
            self.typing, self.query = False, ""
            self.refilter()
        elif key == "enter":
            self.typing = False
        elif key in ("up", "ctrl-p"):
            self.index = max(0, self.index - 1)
        elif key in ("down", "ctrl-n"):
            self.index = min(max(0, len(self.view) - 1), self.index + 1)
        elif key == "backspace":
            self.query = self.query[:-1]
            self.refilter()
        elif key == "ctrl-u":
            self.query = ""
            self.refilter()
        elif len(key) == 1 and key >= " ":
            self.query += key
            self.index = 0
            self.refilter()

    def set_target(self, target: str) -> None:
        if target == "branch":
            from axon.features.gitops import default_base
            names = wv.branch_names(self.ws)
            hint = self.ref or default_base(self.ws)
            ref = self.prompt_line(f"{GOLD}Compare with branch{RST} {DARK_SLATE}({', '.join(names[:4])}){RST} ", default=hint)
            if not ref:
                return
            if ref not in names and wv.git_text(self.ws, "rev-parse", "--verify", "-q", ref) is None:
                self.say(f"No branch or commit called {ref}.", ROSE)
                return
            self.ref = ref
        self.target = target
        self.expanded.clear()
        self.top = 0
        self.index = 0
        self.reload()

    def _act(self, key: str, f: dict[str, Any]) -> None:
        path = f["path"]
        if key == "s":
            if self.target != "working":
                return self.say("Staging works while comparing the working tree.", GOLD)
            ok, msg = wv.stage(self.ws, path)
        elif key == "u":
            ok, msg = wv.unstage(self.ws, path)
        elif key == "x":
            if self.target != "working":
                return self.say("Discard works while comparing the working tree.", GOLD)
            verb = "Delete this untracked file" if f["code"] == "U" else "Discard all changes to"
            if not self.confirm(f"{verb} {path}? This cannot be undone."):
                return self.say("Kept.", SLATE)
            ok, msg = wv.discard(self.ws, path, f["code"])
        elif key == "o":
            self.action = ("open", path)
            self.finish(("open", path))
            return
        else:                                                   # a: add this file's diff to the next prompt
            d = self.diff_of(f)
            text = "\n".join(("+" if r["kind"] == "add" else "-" if r["kind"] == "del" else " ") + r["text"] if r["kind"] in ("add", "del", "ctx")
                             else r["text"] for r in d["rows"])
            self.agent.pending_input = f"About my change to {path}:\n```diff\n{text[:20_000]}\n```\n"
            self.finish(("attach", path))
            return
        self.say(msg or ("Done." if ok else "Failed."), MINT if ok else ROSE)
        self.reload(path)


def open_in_editor(ws: Path, path: str) -> None:
    from axon.commands.common import editor_argv
    target = (ws / path)
    if not target.exists():
        print(f"  {ROSE}{path} no longer exists.{RST}")
        return
    with contextlib.suppress(Exception):
        subprocess.run([*editor_argv(), str(target)], cwd=str(ws))


def run_changes_screen(agent: Any, target: str = "working", ref: str = "") -> None:
    from axon.commands.common import ok
    screen = ChangesScreen(agent, target, ref)
    result = screen.run()
    while isinstance(result, tuple) and result[0] == "open":
        open_in_editor(_ws(agent), result[1])
        screen.reload()
        result = screen.resume()
    if isinstance(result, tuple) and result[0] == "attach":
        ok(f"Diff of {result[1]} will be attached to your next prompt.")


# ─── worktrees ──────────────────────────────────────────────────────────────

def worktree_row(w: dict[str, Any], selected: bool, width: int) -> str:
    pointer = f"{MINT}{BOLD}❯{RST}" if selected else " "
    here = f"{MINT}●{RST}" if w["current"] and _uni() else (f"{MINT}*{RST}" if w["current"] else " ")
    branch = w["branch"] or "?"
    tags = ", ".join(x for x in ("main" if w["main"] else "", "here" if w["current"] else "", "locked" if w["locked"] else "",
                                  "missing" if w["prunable"] else "") if x)
    right = counts_text(w["added"], w["removed"]) if w["dirty"] else f"{DARK_SLATE}clean{RST}"
    if w["dirty"]:
        right = f"{DARK_SLATE}{w['files']} file{'s' if w['files'] != 1 else ''}{RST}  {right}"
    left_plain = f"{pointer} {here} {BOLD if selected else ''}{WHITE}{branch}{RST}" + (f" {DARK_SLATE}({tags}){RST}" if tags else "")
    rw = display_width(right)
    room = max(10, width - rw - 3)
    left = _fit(left_plain, room)
    return left + " " * max(2, width - display_width(left) - rw) + right


def worktree_lines(rows: list[dict[str, Any]], width: int, selected: int = -1) -> list[str]:
    out = []
    for i, w in enumerate(rows):
        out.append(worktree_row(w, i == selected, width))
        path = w["path"].replace(str(Path.home()), "~", 1)
        out.append(f"      {DARK_SLATE}{w['head']} {middle_ellipsis(path, max(10, width - 8 - len(w['head']) - 1))}{RST}")
    return out


class WorktreeScreen(_Screen):
    def __init__(self, agent: Any) -> None:
        super().__init__("Worktrees")
        self.agent = agent
        self.ws = _ws(agent)
        self.rows: list[dict[str, Any]] = []
        self.index = 0
        self.reload()

    def reload(self) -> None:
        self.rows = wv.list_worktrees(self.ws)
        self.index = max(0, min(self.index, len(self.rows) - 1))

    def _body(self, width: int, room: int) -> list[str]:
        if not self.rows:
            return ["", f"  {SLATE}Not a git repository (or no commits yet).{RST}"]
        per = 2
        vis = max(1, room // per)
        start = max(0, min(self.index - vis // 2, len(self.rows) - vis))
        out: list[str] = []
        for i in range(start, min(len(self.rows), start + vis)):
            out += worktree_lines([self.rows[i]], width - 2, 0 if i == self.index else -1)
        return ["  " + x for x in out]

    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, _ = plat.term_size()
        width = max(20, cols - 2)
        head = [_lr(f"  {GOLD}◆{RST} {BOLD}{WHITE}Worktrees{RST}", f"{SLATE}{len(self.rows)}{RST}  ", width), f"  {_rule(width - 2)}"]
        return self.chrome(head, self._body, ["↑↓ move", "enter switch", "n new", "x remove", "r refresh", "esc close"])

    def _default_key(self, key: str) -> None:
        w = self.rows[self.index] if self.rows else None
        if key in ("esc", "ctrl-c", "q"):
            self.finish(None)
        elif key in ("up", "k", "ctrl-p"):
            self.index = max(0, self.index - 1)
        elif key in ("down", "j", "ctrl-n"):
            self.index = min(max(0, len(self.rows) - 1), self.index + 1)
        elif key == "r":
            self.reload()
        elif key == "enter" and w:
            if w["current"]:
                self.say("You are already here.", SLATE)
            elif w["prunable"] or not Path(w["path"]).is_dir():
                self.say("That folder is gone. Remove the entry with x.", ROSE)
            else:
                self.finish(("switch", w["path"]))
        elif key == "n":
            name = self.prompt_line(f"{GOLD}New worktree name{RST} {DARK_SLATE}(branch axon/<name>){RST} ")
            if name:
                ok, msg, path = wv.create_worktree(self.ws, name)
                self.say(msg, MINT if ok else ROSE)
                self.reload()
        elif key in ("x", "d") and w:
            if w["main"] or w["current"] or w["dirty"] or w["locked"]:
                ok, msg = wv.remove_worktree(self.ws, w["path"])
                self.say(msg, ROSE)
            elif self.confirm(f"Remove worktree {w['branch']} at {w['path']}?"):
                ok, msg = wv.remove_worktree(self.ws, w["path"])
                self.say(msg, MINT if ok else ROSE)
                self.reload()
            else:
                self.say("Kept.", SLATE)


WORKTREE_USAGE = """/worktree                 worktrees with branch and change counts (terminal: pick one to switch)
/worktree new <name>      create one on branch axon/<name> from HEAD
/worktree switch <name>   move this session into a worktree (same as /cd <path>)
/worktree remove <name>   remove one (refuses one with uncommitted changes, the main one and the current one)"""


def _find_worktree(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    key = key.strip().rstrip("/")
    for w in rows:
        if key in (w["path"], w["branch"], w["branch"].removeprefix("axon/"), Path(w["path"]).name):
            return w
    return None


@command("worktree", category=CAT, description="Git worktrees: list with changes, switch, create, remove", aliases=("wt", "worktrees"),
         usage=WORKTREE_USAGE)
def handle_worktree(agent: Any, arg: str) -> CommandResult:
    from axon.commands.common import err, header, info, is_tty, ok, width
    ws = _ws(agent)
    words = arg.split()
    sub = words[0].lower() if words else ""
    rows = wv.list_worktrees(ws)
    if not rows:
        err("Not a git repository, or it has no commits yet.")
        return CommandResult(handled=True, success=False)
    if not sub:
        if is_tty():
            res = WorktreeScreen(agent).run()
            if isinstance(res, tuple) and res[0] == "switch":
                from axon.commands.workspace_cmds import handle_cd
                handle_cd(agent, res[1])
            return CommandResult(handled=True)
        header("Worktrees", f"{len(rows)}")
        for line in worktree_lines(rows, width()):
            print("  " + line)
        print()
        return CommandResult(handled=True)
    rest = " ".join(w for w in words[1:] if w != "--yes")
    if sub in ("new", "add", "create"):
        good, msg, path = wv.create_worktree(ws, rest)
        (ok if good else err)(f"{msg}{' at ' + path if path else ''}")
        if good:
            info("  /worktree switch " + Path(path).name + " moves this session into it.")
        return CommandResult(handled=True, success=good)
    target = _find_worktree(rows, rest) if rest else None
    if sub in ("switch", "cd", "go", "use"):
        if target is None:
            err(f"No worktree '{rest}'. /worktree lists them.")
            return CommandResult(handled=True, success=False)
        from axon.commands.workspace_cmds import handle_cd
        return handle_cd(agent, target["path"])
    if sub in ("remove", "rm", "delete"):
        if target is None:
            err(f"No worktree '{rest}'. /worktree lists them.")
            return CommandResult(handled=True, success=False)
        if not target["main"] and not target["current"] and not target["dirty"] and "--yes" not in words:
            if not is_tty():
                err("Add --yes to remove it without a terminal to confirm in.")
                return CommandResult(handled=True, success=False)
            from axon.features.gitops import confirm
            if not confirm(f"Remove worktree {target['branch']} at {target['path']}?"):
                info("Kept.")
                return CommandResult(handled=True)
        good, msg = wv.remove_worktree(ws, target["path"])
        (ok if good else err)(msg)
        return CommandResult(handled=True, success=good)
    err(f"Unknown subcommand '{sub}'.")
    print(WORKTREE_USAGE)
    return CommandResult(handled=True, success=False)


# ─── the workbench screen ───────────────────────────────────────────────────

SECTIONS = [("e", "Environment"), ("c", "Context"), ("u", "Usage"), ("k", "Connectors"), ("g", "Changes"),
            ("b", "Background tasks"), ("p", "Plan and todos")]
CHIP = {"e": "Env", "c": "Context", "u": "Usage", "k": "Connectors", "g": "Changes", "b": "Tasks", "p": "Plan"}
_TODO_MARK = {"completed": ("✔", "+", MINT), "in_progress": ("▸", ">", GOLD), "pending": ("○", "-", SLATE)}


def collect(agent: Any) -> dict[str, Any]:
    """Every figure the workbench shows, gathered once (the screens re-collect on refresh)."""
    ws = _ws(agent)
    out: dict[str, Any] = {"env": wd.environment(agent)}
    for key, fn in (("context", lambda: context_data(agent)), ("limits", lambda: wd.usage_limits(agent)),
                    ("connectors", lambda: wd.connectors(ws)), ("changes", lambda: wv.changes(ws)),
                    ("background", lambda: wd.background(agent)), ("plan", lambda: wd.plan_and_todos(agent))):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _bar(frac: float, cells: int, colour: str) -> str:
    frac = min(1.0, max(0.0, frac))
    full = int(round(frac * cells))
    if _color():
        return f"{colour}{'█' * full}{DARK_SLATE}{'░' * (cells - full)}{RST}" if _uni() else f"{colour}{'#' * full}{DARK_SLATE}{'.' * (cells - full)}{RST}"
    return "[" + "#" * full + "." * (cells - full) + "]"


def workbench_sections(agent: Any, width: int, data: dict[str, Any] | None = None) -> list[tuple[str, list[str]]]:
    """(section key, lines) for each section. Quick summaries only."""
    d = data or collect(agent)
    from axon.commands.usage_screen import usd
    out: list[tuple[str, list[str]]] = []
    # environment
    e = d["env"]
    home = str(Path.home())
    path = e["workspace"].replace(home, "~", 1)
    lines = [_lr(f"{SLATE}Folder{RST}  {WHITE}{e['name']}{RST}", f"{DARK_SLATE}{middle_ellipsis(path, max(8, width // 2))}{RST}", width)]
    lines.append(f"{SLATE}Branch{RST}  {WHITE}{e['branch'] or 'not a git repository'}{RST}")
    lines.append(_lr(f"{SLATE}Model{RST}   {WHITE}{e['model']}{RST}", f"{SLATE}{e['mode']} mode{RST}", width))
    out.append(("e", lines))
    # context
    c = d["context"]
    if "error" in c:
        out.append(("c", [f"{ROSE}{c['error']}{RST}"]))
    else:
        est = "" if c["calibrated"] else "~"
        bar_w = max(10, min(width - 2, 80))
        top = sorted((r for r in c["rows"] if r["key"] not in ("free", "autocompact") and r["tokens"]), key=lambda r: -r["tokens"])[:3]
        out.append(("c", [_lr(f"{WHITE}{BOLD}{c['pct']}% used{RST}", f"{SLATE}{est}{tok(c['used'])} / {tok(c['window'])}{RST}", width),
                          context_bar(c, bar_w),
                          f"{DARK_SLATE}" + _fit("  ".join(f"{r['label']} {est}{tok(r['tokens'])}" for r in top), width) + RST]))
    # usage
    u = d["limits"]
    lines = []
    try:
        from axon.commands.usage_screen import session_usage
        su = session_usage(agent)
        spent = float(su.total_cost or 0)
        lines.append(_lr(f"{SLATE}This session{RST}  {WHITE}{usd(spent)}{RST}", f"{DARK_SLATE}{su.calls} call{'s' if su.calls != 1 else ''}{RST}", width))
    except Exception:
        spent = 0.0
    for r in (u.get("limits") or [])[:3]:
        frac = float(r["used"] or 0)
        colour = MINT if frac < 0.75 else (GOLD if frac < 0.9 else ROSE)
        lines.append(_lr(f"{SLATE}{r['label']}{RST}  {_bar(frac, 10, colour)} {colour}{frac * 100:.0f}%{RST}", f"{DARK_SLATE}{r['reset']}{RST}", width))
    if not u.get("limits"):
        lines.append(f"{DARK_SLATE}No rate-limit data from this provider.{RST}")
    b = u.get("budget") or {}
    if b.get("session_usd"):
        lines.append(f"{SLATE}Session budget{RST}  {WHITE}{usd(spent)} of {usd(b['session_usd'])}{RST}")
    out.append(("u", lines))
    # connectors
    cn = d["connectors"]
    if "error" in cn:
        out.append(("k", [f"{ROSE}{cn['error']}{RST}"]))
    else:
        out.append(("k", [x for x, _ in connector_lines(cn, width, compact=True)]))
    # changes
    ch = d["changes"]
    if "error" in ch and "files" not in ch:
        out.append(("g", [f"{ROSE}{ch['error']}{RST}"]))
    elif ch.get("error"):
        out.append(("g", [f"{SLATE}{ch['error']}{RST}"]))
    else:
        out.append(("g", [_lr(f"{WHITE}{ch['branch']}{RST} {DARK_SLATE}→ working tree{RST}", changes_summary_line(ch), width)]
                    + [change_row(f, False, width, arrow_on=False) for f in ch["files"][:3]]
                    + ([f"{DARK_SLATE}  +{len(ch['files']) - 3} more · g opens the list{RST}"] if len(ch["files"]) > 3 else [])))
    # background
    bg = d["background"]
    if "error" in bg:
        out.append(("b", [f"{ROSE}{bg['error']}{RST}"]))
    elif not bg["rows"]:
        out.append(("b", [f"{DARK_SLATE}Nothing running in the background.{RST}"]))
    else:
        lines = [f"{SLATE}{bg['running']} running · {len(bg['rows'])} total{RST}"]
        for r in bg["rows"][:4]:
            lines.append(_lr(f"{TEAL}{r['kind']}{RST} {WHITE}{_fit(' '.join(r['label'].split()), max(10, width - 30))}{RST}",
                             f"{GOLD if r['status'].startswith('running') else SLATE}{r['status']}{RST}", width))
        out.append(("b", lines))
    # plan and todos
    pl = d["plan"]
    lines = []
    if pl.get("plan_mode"):
        lines.append(f"{GOLD}Plan mode is on (read-only){RST}  {DARK_SLATE}/plan off leaves it{RST}")
    if not pl.get("items"):
        lines.append(f"{DARK_SLATE}No task list yet.{RST}")
    else:
        lines.append(f"{SLATE}{pl['done']} of {pl['total']} done{RST}")
        for it in pl["items"][:5]:
            uni, asc, colour = _TODO_MARK.get(it["status"], _TODO_MARK["pending"])
            lines.append(_fit(f"{colour}{uni if _uni() else asc}{RST} {WHITE if it['status'] == 'in_progress' else SLATE}{it['content']}{RST}", width))
        if pl["total"] > 5:
            lines.append(f"{DARK_SLATE}+{pl['total'] - 5} more · /todos{RST}")
    out.append(("p", lines))
    return out


def workbench_text(agent: Any, width: int = 100) -> str:
    names = dict(SECTIONS)
    out: list[str] = []
    for key, lines in workbench_sections(agent, width):
        out += ["", _section(names[key], width)] + ["  " + x for x in lines]
    return "\n".join(out) + "\n"


class WorkbenchScreen(_Screen):
    """Every section stacked; one key jumps to each, Enter opens its detail screen. No timers: r refreshes."""

    def __init__(self, agent: Any) -> None:
        super().__init__("Workbench")
        self.agent = agent
        self.cur = "e"
        self.scroll = 0
        self._data: dict[str, Any] | None = None

    def data(self) -> dict[str, Any]:
        if self._data is None:
            self._data = collect(self.agent)
        return self._data

    def _body(self, width: int, room: int) -> list[str]:
        flat: list[str] = []
        starts: dict[str, int] = {}
        ends: dict[str, int] = {}
        names = dict(SECTIONS)
        details = {"c": "enter: context", "k": "enter: connectors", "g": "enter: changes", "u": "enter: usage", "b": "enter: tasks", "p": "enter: todos"}
        for key, lines in workbench_sections(self.agent, width - 2, self.data()):
            starts[key] = len(flat)
            hot = key == self.cur
            title = _section(names[key], width - 2, details.get(key, "") if hot else "", key)
            flat.append((f"{MINT}{BOLD}❯{RST} " if hot else "  ") + title)
            flat += ["  " + x for x in lines]
            ends[key] = len(flat)
            flat.append("")
        # scroll only as far as needed to show the chosen section, heading first
        start, end = starts.get(self.cur, 0), ends.get(self.cur, 0)
        if start < self.scroll:
            self.scroll = start
        elif end > self.scroll + room:
            self.scroll = min(start, end - room)
        self.scroll = max(0, min(self.scroll, max(0, len(flat) - room)))
        view = flat[self.scroll:self.scroll + room]
        return view

    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, _ = plat.term_size()
        width = max(20, cols - 2)
        chips = [f"{GRAY_BG}{BOLD}{WHITE} {k} {CHIP[k]} {RST}" if k == self.cur else f"{SLATE} {k} {CHIP[k]} {RST}"
                 for k, n in SECTIONS]
        head = [f"  {GOLD}◆{RST} {BOLD}{WHITE}Workbench{RST}  {DARK_SLATE}a summary; nothing here moves{RST}", "  " + "".join(chips)]
        head.append(f"  {_rule(width - 2)}")
        return self.chrome(head, self._body, ["key jumps to a section", "↑↓ section", "enter open", "r refresh", "esc close"])

    def _default_key(self, key: str) -> None:
        letters = [k for k, _ in SECTIONS]
        i = letters.index(self.cur)
        if key in ("esc", "ctrl-c", "q"):
            self.finish(None)
        elif key in letters:
            self.cur = key
            if key in ("c", "k", "g", "u"):
                self.say(f"enter opens {dict(SECTIONS)[key].lower()} in detail", SLATE)
        elif key in ("down", "j", "tab", "ctrl-n"):
            self.cur = letters[(i + 1) % len(letters)]
        elif key in ("up", "k", "shift-tab", "ctrl-p"):
            self.cur = letters[(i - 1) % len(letters)]
        elif key == "home":
            self.cur = letters[0]
        elif key == "end":
            self.cur = letters[-1]
        elif key == "r":
            self._data = None
            self.say("Refreshed.")
        elif key == "enter":
            self.finish(("open", self.cur))


class DetailScreen(_Screen):
    """Rows you can expand: the context categories, or the connectors. Enter expands, m shows more."""

    def __init__(self, agent: Any, kind: str, expand: set[str] | None = None) -> None:
        super().__init__("Context" if kind == "context" else "Connectors")
        self.agent = agent
        self.kind = kind
        self.expanded: set[str] = set(expand or ())
        self.show_all = False
        self.focus = ""
        self.scroll = 0
        self._data: dict[str, Any] | None = None

    def data(self) -> dict[str, Any]:
        if self._data is None:
            self._data = context_data(self.agent) if self.kind == "context" else wd.connectors(_ws(self.agent))
        return self._data

    def build(self, width: int) -> list[tuple[str, str | None]]:
        if self.kind == "context":
            return context_lines(self.data(), width - 2, self.expanded, self.focus)
        return connector_lines(self.data(), width - 2, self.expanded, self.show_all)

    def _focusables(self) -> list[str]:
        cols, _ = plat.term_size()
        return [k for _, k in self.build(max(20, cols - 2)) if k]

    def _body(self, width: int, room: int) -> list[str]:
        built = self.build(width)
        keys = [k for _, k in built if k]
        if keys and self.focus not in keys:
            self.focus = keys[0]
            built = self.build(width)
        flat = [(f"{MINT}{BOLD}❯{RST} " if (k and k == self.focus) else "  ") + text for text, k in built]
        at = next((n for n, (_, k) in enumerate(built) if k and k == self.focus), 0)
        if at < self.scroll:
            self.scroll = at
        elif at >= self.scroll + room - 2:
            self.scroll = at - room + 3
        self.scroll = max(0, min(self.scroll, max(0, len(flat) - room)))
        return flat[self.scroll:self.scroll + room]

    def _frame(self) -> tuple[list[str], int | None, int]:
        cols, _ = plat.term_size()
        width = max(20, cols - 2)
        sub = f"{self.data()['window']:,} token window" if self.kind == "context" else ""
        head = [f"  {GOLD}◆{RST} {BOLD}{WHITE}{self.title}{RST}  {SLATE}{sub}{RST}", f"  {_rule(width - 2)}"]
        keys = ["↑↓ move", "enter expand", "r refresh", "esc back"]
        if self.kind == "connectors":
            keys = ["↑↓ move", "enter tools", "l sign in", "r reconnect", "R reload", "esc back"]
        return self.chrome(head, self._body, keys)

    def _default_key(self, key: str) -> None:
        keys = self._focusables()
        i = keys.index(self.focus) if self.focus in keys else 0
        if key in ("esc", "ctrl-c", "q", "left") and not (key == "left" and self.focus in self.expanded):
            self.finish(None)
        elif key in ("up", "k", "ctrl-p") and keys:
            self.focus = keys[max(0, i - 1)]
        elif key in ("down", "j", "ctrl-n") and keys:
            self.focus = keys[min(len(keys) - 1, i + 1)]
        elif key in ("enter", "right", " ", "space") and self.focus:
            if self.focus == "__more__":
                self.show_all = True
                self.focus = ""
            else:
                self.expanded ^= {self.focus}
        elif key == "left":
            self.expanded.discard(self.focus)
        elif key == "R":
            self._data = None
        elif key in ("l", "r") and self.kind == "connectors" and self.focus not in ("", "__more__"):
            s = next((x for x in self.data()["servers"] if x["name"] == self.focus), None)
            if s is None:
                return
            if key == "l" and not (s["needs_auth"] or s["status"] == "pending-approval"):
                return self.say("This server does not need signing in.", SLATE)
            verb = ("approve" if s["status"] == "pending-approval" else "login") if key == "l" else "restart"
            self.finish(("mcp", f"{verb} {s['name']}"))
        elif key == "r":
            self._data = None
            self.say("Refreshed.")


def run_workbench(agent: Any) -> None:
    screen = WorkbenchScreen(agent)
    res = screen.run()
    while isinstance(res, tuple) and res[0] == "open":
        kind = res[1]
        if kind == "c":
            DetailScreen(agent, "context").run()
        elif kind == "k":
            run_connectors(agent)
        elif kind == "g":
            run_changes_screen(agent)
        elif kind == "u":
            from axon.commands.usage_screen import run_usage_screen
            run_usage_screen(agent, "usage")
        else:
            screen.say("Nothing more to open here. /tasks and /todos have the full lists.", SLATE)
            res = screen.resume()
            continue
        screen._data = None
        res = screen.resume()


def run_connectors(agent: Any) -> None:
    from axon.mcp.commands import handle_mcp_command
    screen = DetailScreen(agent, "connectors")
    res = screen.run()
    while isinstance(res, tuple) and res[0] == "mcp":
        handle_mcp_command(agent, res[1])
        screen._data = None
        res = screen.resume()


def print_connectors(agent: Any, width: int, show_all: bool = True, expand: set[str] | None = None) -> None:
    for line, _ in connector_lines(wd.connectors(_ws(agent)), width, expand or set(), show_all):
        print("  " + line)
    print()


WORKBENCH_USAGE = """/workbench          one screen: environment, context, usage, connectors, changes, background tasks, plan and todos
                   keys e c u k g b p jump to a part, Enter opens it in detail, r refreshes, Esc closes
/workbench --plain print it as text (also what you get without a terminal)
/workbench hint on|off   show a short /workbench pointer in the status line (off by default)"""


@command("workbench", category="Session", description="One screen: environment, context, usage, connectors, changes, tasks, plan",
         aliases=("panel",), usage=WORKBENCH_USAGE)
def handle_workbench(agent: Any, arg: str) -> CommandResult:
    from axon.commands.common import err, info, is_tty, ok, update_settings, update_settings_file, width, ws_of
    a = arg.strip().lower().split()
    if a[:1] == ["hint"]:
        val = (a[1] if len(a) > 1 else "") in ("on", "true", "1", "yes")
        if len(a) < 2 or a[1] not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
            info(f"The status-line hint is {'on' if getattr(agent.settings, 'workbench_hint', False) else 'off'}. /workbench hint on|off")
            return CommandResult(handled=True)
        update_settings(agent, workbench_hint=val)
        with contextlib.suppress(Exception):
            update_settings_file({"workbenchHint": val}, workspace=ws_of(agent))
        ok(f"Status-line hint {'on' if val else 'off'}.")
        return CommandResult(handled=True)
    if a and a[0] not in ("--plain", "plain", "text"):
        err(f"Unknown option '{a[0]}'. /workbench, /workbench --plain or /workbench hint on|off")
        return CommandResult(handled=True, success=False)
    if is_tty() and not a:
        run_workbench(agent)
        return CommandResult(handled=True)
    print(workbench_text(agent, min(100, max(40, width() + 2))))
    return CommandResult(handled=True)


# ─── hooks used by /diff, /context and /mcp ─────────────────────────────────

def diff_target(arg: str) -> tuple[str, str] | None:
    """(target, ref) for the words of ``/diff``: '', staged, commit, branch <ref>; None when it is not one of them."""
    words = arg.split()
    w = words[0].lower() if words else ""
    if w in ("", "working", "tree"):
        return "working", ""
    if w in ("staged", "cached", "--cached"):
        return "staged", ""
    if w in ("commit", "last", "head", "last-commit"):
        return "commit", ""
    if w in ("branch", "vs", "base", "main") and len(words) > 1:
        return "branch", words[1]
    if w == "branch":
        return "branch", ""
    return None


def print_changes(agent: Any, target: str, ref: str, width: int) -> None:
    for line in changes_text(wv.changes(_ws(agent), target, ref), width, limit=60):
        print("  " + line)
    print()


def context_request(arg: str) -> set[str] | None:
    """Categories to expand for ``/context <word>``; None when the word is not a context view."""
    a = arg.strip().lower()
    if a in ("", "show"):
        return set()
    if a in ("all", "--all", "tools", "expand"):
        return set(EXPANDABLE)
    return {ALIASES[w] for w in a.split() if w in ALIASES} or None
