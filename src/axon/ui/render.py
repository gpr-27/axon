"""
Axon terminal renderer.

The ``Renderer`` turns the provider/agent ``StreamEvent`` stream into a calm, compact transcript.  Every
tool call - built-in, MCP or unknown - has the same two sections: the input (header, then every argument
the header does not show as an aligned ``key  value`` row) and the output (a summary row after ``⎿``,
then the body: highlighted code, a diff, pretty JSON, or the head + tail of plain output)::

    ● Bash  $ pytest tests/ -q
        Run the unit tests
        timeout  120
      ⎿  ✗ exit 1 · ██████▒▒ 6 passed · 2 failed · 14 lines of output · 3.2s
         E   assert 3 == 4
         … +10 more lines · /output 7 to view all
    ● github › search_issues  label:bug
        MCP tool from github
        owner  acme
      ⎿  JSON object · 2 keys · 147 B · 0.4s
         {
           "total_count": 2,
    ● Read  src/missing.py
        reads a file
      ⎿  ✗ error: File not found: src/missing.py
         ▎ Did you mean src/main.py?

Assistant text streams through an incremental markdown renderer, thinking is shown dim (or
collapsed to a one-line summary), a thread-safe spinner shows elapsed time, a token counter and the
interrupt hint, and each turn ends with a one-line footer.  Verbose mode (``Renderer(verbose=True)``,
``AXON_VERBOSE=1`` or ``Renderer.toggle_verbose()``) adds the detailed LLM-call / tool-invocation /
observation trace cards.

All output goes through ``theme.OUTPUT_LOCK`` so background threads never interleave mid-line.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from axon.providers.base import (
    LLMCallStart,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseComplete,
    ToolUseStart,
    TurnComplete,
)
from axon.agent import decision as _decision
from axon.ui import cards as _cards
from axon.ui import live_status as _live
from axon.ui.parallel_board import ParallelBoard as _ParallelBoard
from axon.ui import tool_cards as _tool_cards
from axon.ui.markdown import (
    MarkdownStream,
    file_uri,
    format_markdown,
    highlight,
    render_table,  # noqa: F401  (re-exported for callers)
    )
from axon.ui.theme import (
    closed_box,
    shorten_path,
    ACCENT,
    AMBER,
    BOLD,
    BORDER,
    CYAN,
    DIFF_ADD,
    DIFF_DEL,
    DIM,
    ERROR,
    GRAY_BG,
    INFO,
    ITALIC,
    MUTED,
    OUTPUT_LOCK,
    PRIMARY,
    RST,
    STRIKE,
    SUBTLE,
    SUCCESS,
    TEXT,
    UNDER,
    WARNING,
    colors_enabled,
    display_width,
    glyph,
    hyperlink,
    pad,
    safe_text,
    sanitize,
    strip_ansi,
    supports_emoji,
    layout_width,
    term_height,
    term_width,
    truncate,
    wrap_ansi,
)

# Legacy aliases kept for importers of this module.
GREEN = DIFF_ADD
RED = DIFF_DEL
WHITE = TEXT
GOLD = WARNING
MINT = SUCCESS
ROSE = ERROR
SLATE = MUTED
TEAL = PRIMARY
DARK_SLATE = SUBTLE
DARK_BORDER = BORDER

HEAD_LINES = 6
TAIL_LINES = 3

# ─── Tool name groups ──────────────────────────────────────────────────────
READ_TOOLS = {"Read", "ReadFile", "view_file", "read_file"}
WRITE_TOOLS = {"Write", "WriteFile", "write_to_file"}
EDIT_TOOLS = {"Edit", "EditFile", "replace_file_content"}
MULTI_EDIT_TOOLS = {"MultiEdit"}
BASH_TOOLS = {"Bash", "bash", "run_command"}
LS_TOOLS = {"Ls", "list_dir"}
GREP_TOOLS = {"Grep", "grep_search"}
MUTATING_TOOLS = WRITE_TOOLS | EDIT_TOOLS | MULTI_EDIT_TOOLS | BASH_TOOLS | {"Patch"}


# ─── Output ────────────────────────────────────────────────────────────────

def _typed_only(text: str) -> str:
    """``text`` without a terminal, "" in one (the command's menu offers it instead)."""
    from axon.commands.common import typed_only
    return typed_only(text)


def _write(text: str) -> None:
    """Write to stdout under the shared output lock; never raises on unencodable characters."""
    if not text:
        return
    with OUTPUT_LOCK:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            sys.stdout.write(safe_text(text))
        except ValueError:  # closed stream
            return
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass


def _width(cap: int = 100) -> int:
    """Width for a block of output: the whole terminal (the old per-block caps left half of a wide
    window empty); the max_width setting can narrow everything at once."""
    return max(30, layout_width())


# ─── Small helpers ─────────────────────────────────────────────────────────

def get_ordinal(n: int) -> str:
    """Return English ordinal string (1st, 2nd, 3rd, 4th, ...)."""
    suffix = "th" if 11 <= (n % 100) <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 100_000:
        return f"{round(n / 1000)}k"
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n}"


def _fmt_bytes(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB")


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def get_file_icon(path_str: str) -> str:
    """Optional file-type emoji (only with ``AXON_EMOJI=1`` on emoji-capable terminals), else ``""``."""
    if not os.environ.get("AXON_EMOJI") or not supports_emoji():
        return ""
    p = str(path_str).lower()
    if p.endswith((".py", ".pyi")):
        return "🐍"
    if p.endswith((".toml", ".yaml", ".yml", ".json", ".env", ".ini", ".cfg")):
        return "⚙️"
    if p.endswith((".md", ".markdown", ".txt", ".rst")):
        return "📄"
    if p.endswith("/") or ("/" in p and "." not in Path(p).name):
        return "📁"
    return "📄"


def make_clickable_link(path_str: str, line_range: str = "", workspace: Path | None = None) -> str:
    """Path rendered as an OSC 8 ``file://`` hyperlink (plain text when unsupported)."""
    return hyperlink(file_uri(path_str, workspace), f"{path_str}{line_range}")


def make_clickable_custom(label: str, target_path: str, workspace: Path | None = None) -> str:
    """Custom label hyperlinked to *target_path*."""
    return hyperlink(file_uri(target_path, workspace), label)


def highlight_code(line: str, lang: str = "") -> str:
    """Syntax-highlight one line of code (language optional)."""
    return highlight(line, lang)


def _lang_for(path: str) -> str:
    from axon.ui.markdown import lang_for_path
    return lang_for_path(path) if path else ""


def _short_path(p: str) -> str:
    """Display path relative to cwd when possible."""
    if not p:
        return ""
    try:
        pp = Path(p)
        if pp.is_absolute():
            return str(pp.relative_to(Path.cwd()))
    except (ValueError, OSError):
        pass
    return p


def _first(inp: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        v = inp.get(k)
        if v not in (None, ""):
            return v
    return default


def _clean(text: Any) -> str:
    """Model/tool-provided text made safe to print: ANSI/OSC sequences and control chars removed."""
    return sanitize(strip_ansi(str(text if text is not None else "")))


_NBSP = "\u00a0"
_SGR_RUN = r"((?:\x1b\[[0-9;]*m)*)"


def _dot_sep_re() -> "re.Pattern[str]":
    """' · ' separator with spaces already made non-breaking (the dot glyph depends on the encoding)."""
    return re.compile(_NBSP + _SGR_RUN + re.escape(glyph("dot")) + _SGR_RUN + _NBSP)


def _subagent_number(task_id: Any) -> str:
    """'sub-3' -> '3' ('' for ids that do not carry a number)."""
    m = re.fullmatch(r"sub-(\d+)", str(task_id or ""))
    return m.group(1) if m else ""


def _one_line(text: Any, limit: int = 80) -> str:
    s = " ".join(_clean(text).split())
    return s if len(s) <= limit else s[: limit - 1] + glyph("ellipsis")


# ─── Box & layout primitives (the single set used by every panel) ─────────

def elide(rows: list[str], head: int = HEAD_LINES, tail: int = TAIL_LINES, indent: str = "") -> list[str]:
    """Keep the first *head* and last *tail* rows with a '… N lines hidden' marker in between."""
    n = len(rows)
    if head < 0 or n <= head + tail + 1:
        return rows
    marker = f"{indent}{SUBTLE}{glyph('ellipsis')} {n - head - tail} lines hidden{RST}"
    return rows[:head] + [marker] + (rows[n - tail:] if tail else [])


def box(
    title: str,
    rows: Iterable[str],
    width: int = 88,
    *,
    footer: str = "",
    color: str = BORDER,
    wrap: bool = False,
    head: int = -1,
    tail: int = TAIL_LINES,
) -> str:
    """Rounded, open-right panel::

        ╭─ Title ────────────
        │ row
        ╰─ footer ───────────
    """
    width = max(24, width)
    h, v = glyph("h"), glyph("v")
    inner = width - 4
    body: list[str] = []
    for r in rows:
        if wrap:
            body.extend(wrap_ansi(r, inner) if r else [""])
        else:
            body.append(truncate(r, inner))
    body = elide(body, head, tail)
    title_part = f" {title}{RST} " if title else ""
    top_fill = max(2, width - 4 - display_width(title_part))
    out = [f"  {color}{glyph('tl')}{h}{RST}{title_part}{color}{h * top_fill}{RST}"]
    out += [f"  {color}{v}{RST} {r}" for r in body]
    foot_part = f" {footer}{RST} " if footer else ""
    bot_fill = max(2, width - 4 - display_width(foot_part))
    out.append(f"  {color}{glyph('bl')}{h}{RST}{foot_part}{color}{h * bot_fill}{RST}")
    return "\n".join(out)


# ─── Diff rendering ────────────────────────────────────────────────────────

def _locate_line(path: str, snippet: str, unknown: int = 1) -> int:
    """1-based line where *snippet* starts in *path* (after an edit), or *unknown* when it can't be found."""
    if not path or not snippet:
        return unknown
    try:
        p = Path(path).expanduser()
        if not p.is_file() or p.stat().st_size > 4_000_000:
            return unknown
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return unknown
    idx = text.find(snippet)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else unknown


def diff_rows(old_text: str, new_text: str, start_line: int = 1, width: int | None = None,
              lang: str = "", context: int = 2, max_rows: int = 60, eof_notes: bool = False) -> list[str]:
    """Unified diff rows: real line numbers, hunk separators, tinted +/- rows with intra-line word
    highlights; long lines wrap with the gutter kept (see ``axon.ui.cards.diff_rows``)."""
    return _cards.diff_rows(old_text, new_text, start_line, width or _width(140), lang, context, max_rows, _MORE_HINT,
                            eof_notes=eof_notes)


def _diff_counts(old_text: str, new_text: str) -> tuple[int, int]:
    return _cards.diff_counts(old_text, new_text)


def render_diff_box(old_text: str, new_text: str, filename: str = "", max_width: int = 88, max_show: int = 3) -> str:
    """Boxed diff of two texts (line numbers, tinted additions/removals)."""
    if old_text == new_text:
        return ""
    added, removed = _diff_counts(old_text, new_text)
    title = f"{BOLD}Diff{RST}" + (f" {glyph('dot')} {filename}" if filename else "") + f" {SUCCESS}+{added}{RST} {ERROR}-{removed}{RST}"
    return box(title, diff_rows(old_text, new_text, width=max_width - 4, lang=_lang_for(filename)), max_width)


def render_diff_tool_box(content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Boxed rendering of an existing unified diff text."""
    rows = [highlight(l, "diff") for l in content.strip().splitlines()]
    return box(f"{BOLD}File comparison{RST}", rows, max_width, head=40, tail=10)


# ─── Tool result panels (used by the Renderer and by slash commands) ──────

def render_read_box(p: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """File content panel with line numbers and syntax highlighting."""
    lines = content.strip("\n").splitlines()
    lang = _lang_for(p)
    rows = []
    for l in lines:
        if "→" in l and l.split("→", 1)[0].strip().isdecimal():
            num, code = l.split("→", 1)
            rows.append(f"{SUBTLE}{int(num.strip()):>4} {glyph('v')}{RST} {highlight(code, lang)}")
        else:
            rows.append(f"{SUBTLE}     {glyph('v')}{RST} {highlight(l, lang)}")
    size_kb = len(content.encode("utf-8", errors="replace")) / 1024
    title = f"{BOLD}{make_clickable_custom(Path(p).name if p else 'File', p)}{RST} {MUTED}{_plural(len(lines), 'line')} {glyph('dot')} {size_kb:.1f} KB{RST}"
    return box(title, rows, max_width, footer=f"{MUTED}Read {_plural(len(lines), 'line')}", head=HEAD_LINES)


def render_ls_box(path_str: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Directory listing panel (directories highlighted, entries hyperlinked)."""
    items = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not items or content.strip() == "(Empty directory)":
        return f"  {MUTED}(empty directory){RST}"
    base = Path(path_str or ".").expanduser()
    rows = []
    for item in items:
        if item.startswith(("Total:", "...", "Directory")):
            rows.append(f"{MUTED}{item}{RST}")
        elif item.endswith("/"):
            rows.append(f"{PRIMARY}{make_clickable_custom(item, str(base / item.rstrip('/')))}{RST}")
        else:
            rows.append(f"{TEXT}{make_clickable_custom(item, str(base / item))}{RST}")
    title = f"{BOLD}{path_str or '.'}{RST} {MUTED}{_plural(len(items), 'item')}{RST}"
    return box(title, rows, max_width, footer=f"{MUTED}Found {_plural(len(items), 'item')}", head=HEAD_LINES)


def render_grep_box(pattern: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Search matches with hyperlinked path:line and snippet."""
    lines = [l for l in content.strip().splitlines() if l.strip()]
    if not lines or content.strip().startswith("No matches"):
        return f"  {MUTED}No matches for \"{pattern}\"{RST}"
    rows = []
    for l in lines:
        parts = l.split(":", 2)
        if len(parts) == 3 and parts[1].strip().isdigit():
            rows.append(f"{INFO}{make_clickable_link(parts[0], ':' + parts[1].strip())}{RST} {MUTED}{parts[2].strip()}{RST}")
        else:
            rows.append(f"{TEXT}{l}{RST}")
    title = f"{BOLD}grep{RST} {WARNING}\"{pattern}\"{RST} {MUTED}{_plural(len(lines), 'match', 'matches')}{RST}"
    return box(title, rows, max_width, footer=f"{MUTED}Found {_plural(len(lines), 'match', 'matches')}", head=HEAD_LINES)


def render_glob_box(pattern: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Matched file paths (hyperlinked)."""
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines or content.strip().startswith("No files"):
        return f"  {MUTED}No files matched '{pattern}'{RST}"
    rows = [f"{TEXT}{make_clickable_link(l)}{RST}" for l in lines]
    title = f"{BOLD}glob{RST} {WARNING}{pattern}{RST} {MUTED}{_plural(len(lines), 'file')}{RST}"
    return box(title, rows, max_width, footer=f"{MUTED}Found {_plural(len(lines), 'file')}", head=HEAD_LINES)


def render_write_box(p: str, content_written: str, max_show: int = 60, max_width: int = 88) -> str:
    """Newly written file preview with line numbers."""
    lines = content_written.splitlines() if content_written else []
    if not lines:
        return f"  {SUCCESS}{glyph('ok')} Saved {p}{RST}"
    lang = _lang_for(p)
    rows = [f"{SUBTLE}{i:>4} {glyph('v')}{RST} {highlight(l, lang)}" for i, l in enumerate(lines, 1)]
    size_kb = len(content_written.encode("utf-8", errors="replace")) / 1024
    title = f"{BOLD}{Path(p).name if p else 'File'}{RST} {MUTED}{_plural(len(lines), 'line')} {glyph('dot')} {size_kb:.1f} KB{RST}"
    return box(title, rows, max_width, footer=f"{SUCCESS}{glyph('ok')} Saved {p}", color=BORDER, head=HEAD_LINES)


def stream_write_box(p: str, content_written: str, max_show: int = 60, max_width: int = 88) -> None:
    """Print ``render_write_box`` output."""
    _write(render_write_box(p, content_written, max_show, max_width) + "\n")


def _classify_output_line(l: str) -> str:
    return _cards.classify_output_line(l)


def render_bash_box(content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Shell output panel (errors / warnings / successes coloured)."""
    lines = content.strip().splitlines()
    if not lines or content.strip() == "(Command completed with exit code 0 and no output)":
        return f"  {SUCCESS}{glyph('ok')} Command completed (no output){RST}"
    rows = [_classify_output_line(l) for l in lines]
    return box(f"{BOLD}Output{RST} {MUTED}{_plural(len(lines), 'line')}{RST}", rows, max_width, head=HEAD_LINES)


def _doctor_rows(content: str) -> list[str]:
    rows = []
    for l in content.strip().splitlines():
        if l.startswith("===") or not l.strip():
            continue
        if ":" in l:
            k, val = l.split(":", 1)
            rows.append(f"{PRIMARY}{pad(k.strip(), 18)}{RST} {TEXT}{val.strip()}{RST}")
        else:
            rows.append(f"{TEXT}{l}{RST}")
    return rows


def render_doctor_box(content: str, max_width: int = 88) -> str:
    """Diagnostics panel (key: value rows)."""
    return box(f"{BOLD}Axon System Diagnostics{RST}", _doctor_rows(content), max_width, wrap=True)


def render_queue_box(queue: Any, max_width: int = 88) -> str:
    """Pending message queue with ids and removal hints."""
    items = getattr(queue, "items", [])
    if not items:
        return f"  {MUTED}Message queue is empty.{_typed_only(' Use /q <prompt> to add prompts.')}{RST}"
    rows = []
    for idx, it in enumerate(items, 1):
        if idx == 1:
            rows.append(f"{PRIMARY}{BOLD}#{it.id} next{RST}  {TEXT}{BOLD}{it.text}{RST}")
        else:
            rows.append(f"{MUTED}#{it.id}{RST}       {TEXT}{it.text}{RST}")
    return box(
        f"{BOLD}Message Queue{RST} {MUTED}{len(items)} pending{RST}", rows, max_width, wrap=True,
        footer=f"{MUTED}/q drop <id> {glyph('dot')} /q clear {glyph('dot')} Enter runs next",
    )


_TODO_DONE = ("[✓]", "[x]", "[X]")
_TODO_ACTIVE = ("[▶]", "[>]", "[*]")
_TODO_ITEM_RE = re.compile(r"^\s*\[(.)\]\s*(?:(\d+)[.)]\s*)?(.*)$")
_PLAN_FULL = 25          # plans up to this many items are always shown in full


def _todo_stats(lines: list[str]) -> tuple[int, int, int]:
    states = [st for st, _, _ in _todo_items("\n".join(lines))]
    completed, total = states.count("done"), sum(1 for st in states if st)
    return completed, total, int(completed / total * 100) if total else 0


def _todo_items(content: str) -> list[tuple[str, str, str]]:
    """(state, number, text) per plan item; state is done / active / open, or "" for a free-text line."""
    items: list[tuple[str, str, str]] = []
    n = 0
    for l in content.strip().splitlines():
        s = l.strip()
        if not s or s.startswith(("Updated todos", "Progress:", "Active Step:")):
            continue
        m = _TODO_ITEM_RE.match(s)
        mark = f"[{m.group(1)}]" if m else ""
        if m and mark in _TODO_DONE + _TODO_ACTIVE + ("[ ]",):
            n += 1
            state = "done" if mark in _TODO_DONE else ("active" if mark in _TODO_ACTIVE else "open")
            items.append((state, m.group(2) or str(n), _clean(m.group(3)).strip()))
        else:
            items.append(("", "", _clean(s)))
    return items


def _plan_bar(completed: int, total: int, length: int = 16) -> str:
    filled = int(length * completed / total) if total else 0
    return f"{SUCCESS}{glyph('block') * filled}{SUBTLE}{glyph('shade') * (length - filled)}{RST}"


def _todo_item_rows(state: str, num: str, text: str, num_w: int, width: int | None) -> list[str]:
    """One plan item, numbered, wrapped under its own text (never truncated)."""
    if not state:
        return [f"{TEXT}{text}{RST}"]
    lead = f"{num}.".rjust(num_w + 1)
    if state == "done":
        mark, body = f"{SUCCESS}{glyph('ok')}{RST}", f"{MUTED}{lead} {STRIKE}{text}{RST}"
    elif state == "active":
        mark = f"{ACCENT}{BOLD}{glyph('play')}{RST}"
        body = f"{ACCENT}{BOLD}{lead} {text}{RST}  {ACCENT}{glyph('dot')} in progress{RST}"
    else:
        mark, body = f"{MUTED}{glyph('circle')}{RST}", f"{TEXT}{lead} {text}{RST}"
    if not width:
        return [f"{mark} {body}"]
    hang = " " * (2 + num_w + 2)
    pieces = wrap_ansi(body, max(10, width - 2)) or [""]
    out = [f"{mark} {pieces[0]}"]
    rest = " ".join(strip_ansi(p) for p in pieces[1:])
    if rest:
        style = {"done": MUTED, "active": f"{ACCENT}{BOLD}"}.get(state, TEXT)
        out += [f"{hang}{style}{p}{RST}" for p in wrap_ansi(rest, max(10, width - len(hang)))]
    return out


def _todo_rows(content: str, with_bar: bool = False, width: int | None = None) -> list[str]:
    """Every plan item, numbered: done ✓ (muted, struck through), in progress ▶ (bold accent), open ○."""
    lines = content.strip().splitlines()
    completed, total, pct = _todo_stats(lines)
    rows: list[str] = []
    if total and with_bar:
        rows.append(f"{_plan_bar(completed, total, 20)} {BOLD}{pct}%{RST} {MUTED}({completed}/{total} done){RST}")
    items = _todo_items(content)
    num_w = max((len(n) for st, n, _ in items if st), default=1)
    hidden = 0
    if total > _PLAN_FULL:          # very long plans: fold finished items into one line, keep every open one
        hidden = sum(1 for st, _, _ in items if st == "done")
        items = [it for it in items if it[0] != "done"]
        rows.append(f"{SUCCESS}{glyph('ok')}{RST} {MUTED}{_plural(hidden, 'completed item')} folded{RST}")
    for st, n, text in items:
        rows += _todo_item_rows(st, n, text, num_w, width)
    return rows


def _todo_summary(content: str, limit: int = _PLAN_FULL, width: int | None = None) -> list[str]:
    """Plan card under a TodoWrite call: 'Plan · 2/6 done ████░░ 33%' + every item behind an accent bar.

    Plans are never cut short: up to _PLAN_FULL items all show; beyond that only finished items fold."""
    lines = content.strip().splitlines()
    completed, total, pct = _todo_stats(lines)
    if not total:
        return [f"{ACCENT}{BOLD}Plan{RST}"] + (_todo_rows(content, width=width) or [f"{MUTED}(empty plan){RST}"])
    active = sum(1 for st, _, _ in _todo_items(content) if st == "active")
    head = (f"{ACCENT}{BOLD}Plan{RST} {SUBTLE}{glyph('dot')}{RST} {BOLD}{completed}/{total}{RST} done "
            f"{_plan_bar(completed, total)} {BOLD}{pct}%{RST}")
    if active:
        head += f" {SUBTLE}{glyph('dot')}{RST} {ACCENT}{active} in progress{RST}"
    rail = f"{ACCENT}{glyph('quote')}{RST} "
    return [head] + [rail + r for r in _todo_rows(content, width=max(20, (width or _width(160) - 8) - 2))]


def render_todo_box(content: str, max_width: int = 88) -> str:
    """Task plan checklist with progress bar."""
    completed, total, pct = _todo_stats(content.strip().splitlines())
    rows = _todo_rows(content, with_bar=True, width=max(20, max(24, max_width) - 4))
    title = f"{ACCENT}{BOLD}Task Plan{RST} {MUTED}{completed}/{total} completed ({pct}%){RST}" if total else f"{ACCENT}{BOLD}Task Plan{RST}"
    return box(title, rows, max_width, wrap=True)


def render_side_question_box(question: str, answer: str, max_width: int = 88) -> str:
    """Isolated /btw side-question answer."""
    width = min(max_width, max(40, term_width() - 2))
    rows = [f"{INFO}{BOLD}Question Asked:{RST}"]
    rows += [f"  {TEXT}{BOLD}{q}{RST}" for q in question.strip().splitlines() if q.strip()]
    rows.append("")
    rows.append(f"{SUCCESS}{BOLD}Direct Answer (Factual & Non-Inferential):{RST}")
    rows += [f"  {r}" for r in format_markdown(answer.strip(), max_width=width - 6).splitlines()]
    rows.append("")
    rows.append(f"{ACCENT}Scratchpad Isolation:{RST} {MUTED}Zero pollution to main conversation flow{RST}")
    return box(f"{WARNING}{BOLD}Side Question (/btw){RST}", rows, width, wrap=True)


def render_shortcuts_footer(max_width: int = 88) -> str:
    """Two-column keyboard shortcuts helper."""
    col1 = [
        f"{WARNING}!{RST} for {UNDER}shell{RST} mode",
        f"{WARNING}/{RST} for commands",
        f"{WARNING}@{RST} for file paths",
        f"{WARNING}/btw{RST} for side question",
        f"{CYAN}ctrl + s{RST} to stash prompt",
        f"{WARNING}?{RST} or {WARNING}/kb{RST} for shortcuts",
        f"{CYAN}ctrl + f{RST} for transcript",
    ]
    col2 = [
        f"{CYAN}tab{RST} to cycle modes",
        f"{CYAN}double esc{RST} to clear input",
        f"{CYAN}ctrl + o{RST} to cycle trace detail",
        f"{CYAN}ctrl + t{RST} to toggle tasks",
        f"{CYAN}ctrl + g{RST} to edit in $EDITOR",
        f"{CYAN}\\{glyph('enter')}{RST} for newline",
        f"{MUTED}type while it works{RST} to queue",
    ]
    if max_width < 60:
        return "\n".join(f"  {c}" for c in col1 + col2)
    return "\n".join(f"  {pad(c1, 34)}{c2}" for c1, c2 in zip(col1, col2))


def render_web_search_box(query: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    clean = content.strip()
    if not clean or "no search results found" in clean.lower() or clean.startswith("No matches"):
        return f"  {MUTED}No search results for \"{query}\"{RST}"
    lines = [l for l in clean.splitlines() if l.strip()]
    count = sum(1 for l in lines if re.match(r"^\d+\.\s+", l)) or len(lines)
    return f"  {SUCCESS}Found {_plural(count, 'search result')}{RST} {MUTED}for \"{query}\"{RST}"


def render_web_fetch_box(url: str, content: str, max_width: int = 88) -> str:
    size_kb = len(content.encode("utf-8", errors="replace")) / 1024
    display_url = truncate(url, 60)
    if not content.strip() or content.strip() == "[Empty response]":
        return f"  {MUTED}(empty response from {display_url}){RST}"
    return f"  {SUCCESS}Fetched {size_kb:.1f} KB{RST} {MUTED}from{RST} {INFO}{hyperlink(url, display_url)}{RST}"


def render_git_box(subcmd: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    rows = [_git_line(l) for l in content.strip().splitlines() if l.strip()]
    return box(f"{BOLD}Git {subcmd}{RST}", rows, max_width, head=HEAD_LINES)


def _git_line(l: str) -> str:
    if l.startswith("##"):
        return f"{PRIMARY}{BOLD}{l}{RST}"
    if l[:2] in (" M", "M ", "MM", "R ", " R"):
        return f"{WARNING}{l}{RST}"
    if l[:2] in ("??", " A", "A ", "AM"):
        return f"{DIFF_ADD}{l}{RST}"
    if l[:2] in (" D", "D "):
        return f"{DIFF_DEL}{l}{RST}"
    if l.startswith(("+", "-", "@@", "diff ")):
        return highlight(l, "diff")
    return f"{TEXT}{l}{RST}"


def render_symbols_box(path_str: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    rows = []
    for l in content.strip().splitlines():
        if not l.strip():
            continue
        s = l.strip()
        if "class " in s:
            rows.append(f"  {WARNING}{s}{RST}")
        elif any(k in s for k in ("def ", "func ", "fn ", "function ")):
            rows.append(f"  {INFO}{s}{RST}")
        else:
            rows.append(f"{BOLD}{PRIMARY}{l}{RST}")
    return box(f"{BOLD}Code Symbols{RST} {MUTED}{path_str}{RST}", rows, max_width, head=HEAD_LINES)


def render_tree_box(content: str, max_show: int = 4, max_width: int = 88) -> str:
    rows = []
    for l in content.strip().splitlines():
        name = l.strip(" ├─└│")
        style = f"{PRIMARY}{BOLD}" if l.endswith("/") else TEXT
        rows.append(f"{style}{make_clickable_custom(l, name.rstrip('/'))}{RST}")
    return box(f"{BOLD}Directory Tree{RST}", rows, max_width, head=HEAD_LINES)


def render_http_box(method: str, url: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    size_kb = len(content.encode("utf-8", errors="replace")) / 1024
    first = _one_line(content.strip().splitlines()[0], 40) if content.strip() else "(empty)"
    return f"  {INFO}HTTP {method}{RST} {MUTED}{truncate(url, 50)} {glyph('dot')} {first} {glyph('dot')} {size_kb:.1f} KB{RST}"


def render_process_box(action: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    rows = [f"{TEXT}{l}{RST}" for l in content.strip().splitlines()]
    return box(f"{BOLD}Process Info{RST} {MUTED}{glyph('dot')} {action}{RST}", rows, max_width, head=HEAD_LINES)


def render_env_box(action: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    rows = []
    for l in content.strip().splitlines():
        if "=" in l:
            k, val = l.split("=", 1)
            rows.append(f"{PRIMARY}{pad(k, 22)}{RST} {TEXT}{val}{RST}")
        else:
            rows.append(f"{TEXT}{l}{RST}")
    return box(f"{BOLD}Environment{RST} {MUTED}{glyph('dot')} {action}{RST}", rows, max_width, head=HEAD_LINES)


def render_deep_research_box(query: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    return f"  {SUCCESS}Deep research completed{RST} {MUTED}for \"{_one_line(query, 45)}\"{RST}"


def render_table_search_box(query: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    return f"  {SUCCESS}Structured table search completed{RST} {MUTED}for \"{_one_line(query, 45)}\"{RST}"


def render_patch_box(path_str: str, content: str, max_width: int = 88) -> str:
    return f"  {SUCCESS}{glyph('ok')} {content.strip()}{RST}"


def render_error_box(tool_name: str, content: str, max_width: int = 88) -> str:
    """Error panel."""
    rows = [f"{ERROR}{l}{RST}" for l in (content.strip().splitlines() or ["(no details)"])]
    return box(f"{ERROR}{BOLD}{glyph('fail')} Error{RST} {MUTED}{glyph('dot')} {tool_name}{RST}", rows, max_width,
               color=ERROR, wrap=True, head=12, tail=4)


# ─── Tool outputs of the session (for /output and /output <n>) ─────────────
_LAST_TOOL_OUTPUT = ""
_TOOL_OUTPUTS: "dict[int, tuple[str, str, str]]" = {}   # n -> (tool name, target, full output)
_TOOL_OUTPUT_SEQ = 0
_OUTPUT_BY_ID: "dict[str, int]" = {}   # tool_use id -> '/output <n>' number (read by agent/client_context for its stub)
_TOOL_CALLS: "dict[int, dict[str, Any]]" = {}   # n -> how call #n was made (see Renderer._remember_call)
_TOOL_OUTPUT_KEEP = 200
# Hint appended to elided previews ("… +12 more lines · /output 7"); set per tool result.
_MORE_HINT = "/output"


def output_number_for(tool_use_id: str) -> int:
    """The '/output <n>' number of the result of tool call *tool_use_id*, 0 when unknown or no longer kept."""
    n = _OUTPUT_BY_ID.get(str(tool_use_id), 0)
    return n if n in _TOOL_OUTPUTS else 0


def get_last_tool_output() -> str:
    return _LAST_TOOL_OUTPUT


def get_tool_output(n: int) -> tuple[str, str, str] | None:
    """(name, target, full output) of tool call #n of this session, if still kept."""
    return _TOOL_OUTPUTS.get(int(n))


def list_tool_outputs(limit: int = 20) -> list[tuple[int, str, str, int]]:
    """Most recent tool outputs: (n, name, target, line count), newest last."""
    items = sorted(_TOOL_OUTPUTS.items())[-limit:]
    return [(n, name, target, len(out.splitlines())) for n, (name, target, out) in items]


def _output_file(n: int, content: str) -> str:
    """Write a full tool output to a temp file and return its file:// URL (for clickable hints)."""
    try:
        import tempfile

        folder = Path(tempfile.gettempdir()) / f"axon-outputs-{os.getpid()}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"output-{n}.txt"
        path.write_text(content, encoding="utf-8", errors="replace")
        return path.as_uri()
    except OSError:
        return ""


def _save_tool_output(content: str, name: str = "", target: str = "") -> int:
    """Remember a tool's full output; returns its session-wide number (shown as '/output <n>')."""
    global _LAST_TOOL_OUTPUT, _TOOL_OUTPUT_SEQ
    _LAST_TOOL_OUTPUT = content
    _TOOL_OUTPUT_SEQ += 1
    _TOOL_OUTPUTS[_TOOL_OUTPUT_SEQ] = (name, target, content or "")
    for old in sorted(_TOOL_OUTPUTS)[:-_TOOL_OUTPUT_KEEP]:
        del _TOOL_OUTPUTS[old]
    return _TOOL_OUTPUT_SEQ


# ─── Session transcript (structured; read by the expandable transcript viewer) ─

@dataclass
class TranscriptEntry:
    """One block of the session as the user saw it, kept in full (plain text, no ANSI).

    kind: "turn" | "step" | "thinking" | "text" | "tool" | "note"."""
    kind: str
    turn: int
    step: int = 0
    title: str = ""          # 'Turn 3 · "fix the test"', 'Step 2 · gpt-4o', 'Bash  git status'
    summary: str = ""        # one-line result / stats
    body: str = ""           # full tool output, response text or thinking
    detail: str = ""         # tool input as JSON
    status: str = "ok"       # ok | error | running
    started_at: float = 0.0
    duration: float | None = None
    id: str = ""             # tool call id
    output_no: int = 0       # '/output <n>' number of a tool result
    why: str = ""            # why the model called the tool (its own words before the call)
    why_source: str = ""     # model-text | model-thinking | heuristic
    chosen: str = ""         # 'chosen by <model> (<provider>) in step 2 · call 1 of 1 · from 22 tools'
    allowed: str = ""        # the permission decision in plain words
    then: str = ""           # where the result went ('result (54 B, ~14 tokens) goes to the model in step 3')
    uid: int = field(default_factory=lambda: next(_UIDS))   # stable id: screen rows are tagged with it


_UIDS = itertools.count(1)


def _click_expand_on(settings: Any = None) -> bool:
    try:
        from axon.ui.screen_log import click_expand_enabled
        return click_expand_enabled(settings) and sys.stdout.isatty()
    except Exception:
        return False


def _tag_rows(uid: int | None) -> None:
    """Rows printed from now on belong to transcript entry ``uid`` (click-to-expand at the prompt)."""
    try:
        from axon.ui.screen_log import set_tag
        set_tag(uid)
    except Exception:
        pass


_TRANSCRIPT: list[TranscriptEntry] = []
_TRANSCRIPT_LOCK = threading.RLock()
_TRANSCRIPT_KEEP = 4000


def get_transcript() -> list[TranscriptEntry]:
    """Snapshot of the session transcript (entries may still be updated while a tool runs)."""
    with _TRANSCRIPT_LOCK:
        return list(_TRANSCRIPT)


def clear_transcript() -> None:
    with _TRANSCRIPT_LOCK:
        _TRANSCRIPT.clear()


def _record(entry: TranscriptEntry) -> TranscriptEntry:
    _tag_rows(entry.uid)
    with _TRANSCRIPT_LOCK:
        _TRANSCRIPT.append(entry)
        if len(_TRANSCRIPT) > _TRANSCRIPT_KEEP:
            del _TRANSCRIPT[: len(_TRANSCRIPT) - _TRANSCRIPT_KEEP]
    return entry


def _plain(text: Any) -> str:
    return strip_ansi(str(text or ""))


# ─── Argument formatting ───────────────────────────────────────────────────

def _event_fields(e: Any) -> dict[str, Any]:
    """Public fields of an event/dataclass/dict (duck-typed)."""
    if isinstance(e, dict):
        return dict(e)
    try:
        import dataclasses

        if dataclasses.is_dataclass(e):
            return {f.name: getattr(e, f.name) for f in dataclasses.fields(e)}
    except Exception:
        pass
    return {k: v for k, v in vars(e).items() if not k.startswith("_")} if hasattr(e, "__dict__") else {}


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute / key among *names*."""
    for n in names:
        if isinstance(obj, dict):
            if obj.get(n) not in (None, ""):
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v not in (None, ""):
                return v
    return default


def _usage_line(usage: Any, *, duration: float | None = None, cost: Any = None, stop: str = "") -> str:
    """'12.4k in (79% cached) · 412 out · 1.9s · $0.0031 · end_turn'."""
    dot = f" {glyph('dot')} "
    inp = int(_get(usage, "input", "input_tokens", default=0) or 0)
    out = int(_get(usage, "output", "output_tokens", default=0) or 0)
    cr = int(_get(usage, "cache_read", "cache_read_tokens", default=0) or 0)
    cw = int(_get(usage, "cache_write", "cache_write_tokens", default=0) or 0)
    reasoning = int(_get(usage, "reasoning", default=0) or 0)
    parts = [f"{_fmt_tokens(inp)} in" + (f" ({int(cr * 100 / inp) if inp else 0}% cached)" if cr else "")]
    if cw:
        parts.append(f"{_fmt_tokens(cw)} cache write")
    parts.append(f"{_fmt_tokens(out)} out" + (f" ({_fmt_tokens(reasoning)} reasoning)" if reasoning else ""))
    if duration is not None:
        parts.append(f"{duration:.1f}s")
    if cost is not None:
        try:
            parts.append(f"${float(cost):.4f}")
        except (TypeError, ValueError):
            pass
    if stop:
        parts.append(stop)
    return dot.join(parts)


def _cost_for(model: str, usage: Any) -> Any:
    try:
        from axon.providers.pricing import cost_for

        return cost_for(model, usage)
    except Exception:
        return None


def context_gauge(used: int, window: int, cells: int = 8, compact: bool = False) -> str:
    """'ctx ████░░░░ 52% of 200k' coloured green < 50 %, amber <= 75 %, red above.

    ``compact=True`` gives the short form 'ctx 52%/200k' for narrow terminals."""
    if not window or used <= 0:
        return ""
    ratio = min(1.0, used / window)
    pct = ratio * 100
    color = SUCCESS if pct < 50 else (AMBER if pct <= 75 else ERROR)
    if compact:
        return f"{MUTED}ctx{RST} {color}{pct:.0f}%{RST}{MUTED}/{_fmt_tokens(window)}{RST}"
    filled = min(cells, max(1, round(ratio * cells)))
    bar = f"{color}{glyph('block') * filled}{SUBTLE}{glyph('shade') * (cells - filled)}{RST}"
    return f"{MUTED}context{RST} {bar} {color}{pct:.0f}%{RST}{MUTED} of {_fmt_tokens(window)}{RST}"


# ─── End-of-turn summary: one row per LLM call ─────────────────────────────

_SUMMARY_HEADER = ("step", "model", "time", "in", "cached", "cache write", "out", "cost")
_SUMMARY_MODEL_CHARS = 28


def step_usage(step: int, model: str, duration: Any, usage: Any, cost: Any) -> dict[str, Any]:
    """The figures of one LLM call that the end-of-turn table shows (``usage`` None: never reported)."""
    def number(*names: str) -> int:
        return int(_get(usage, *names, default=0) or 0) if usage is not None else 0

    def decimal(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {"step": int(step or 0), "model": str(model or ""), "duration": decimal(duration),
            "input": number("input", "input_tokens"), "cache_read": number("cache_read", "cache_read_tokens"),
            "cache_write": number("cache_write", "cache_write_tokens"), "output": number("output", "output_tokens"),
            "cost": decimal(cost), "reported": usage is not None}


def _summary_cells(s: dict[str, Any]) -> list[str]:
    """One table row: step, model, time, in, cached %, cache write, out, cost ('-' when unknown)."""
    model = truncate(_clean(s.get("model") or "?"), _SUMMARY_MODEL_CHARS)
    if s.get("stopped"):
        model += " (stopped)"
    time_s = f"{s['duration']:.1f}s" if s.get("duration") is not None else "-"
    if not s.get("reported"):
        return [str(s.get("step") or "?"), model, time_s, "-", "-", "-", "-", "-"]
    inp, read = int(s.get("input") or 0), int(s.get("cache_read") or 0)
    cached = f"{int(read * 100 / inp)}%" if inp and read else "-"
    write = _fmt_tokens(int(s["cache_write"])) if s.get("cache_write") else "-"
    cost = f"${s['cost']:.4f}" if s.get("cost") is not None else "-"
    return [str(s.get("step") or "?"), model, time_s, _fmt_tokens(inp), cached, write,
            _fmt_tokens(int(s.get("output") or 0)), cost]


def _summary_table_row(cells: list[str], widths: list[int], color: str) -> str:
    """Cells joined by two spaces: the model column left-aligned, the figures right-aligned."""
    out = []
    for i, (cell, w) in enumerate(zip(cells, widths)):
        gap = " " * max(0, w - display_width(cell))
        out.append(cell + gap if i == 1 else gap + cell)
    return f"{color}{'  '.join(out).rstrip()}{RST}"


def _summary_line(s: dict[str, Any]) -> str:
    """The narrow-terminal form of one row: 'step 1 · model · 3.7s · 32.5k in (95% cached) · 800 out · $0.0009'."""
    cells = _summary_cells(s)
    head = f"step {cells[0]} {glyph('dot')} {cells[1]} {glyph('dot')} {cells[2]}"
    if not s.get("reported"):
        return head + f" {glyph('dot')} no usage reported"
    usage = {"input": s["input"], "output": s["output"], "cache_read": s["cache_read"], "cache_write": s["cache_write"]}
    return head + f" {glyph('dot')} " + _usage_line(usage, cost=s.get("cost"))


def turn_summary_rows(steps: list[dict[str, Any]], width: int, stopped: bool = False) -> list[str]:
    """The end-of-turn usage table (rows without the rail): a title, a header and one row per LLM call.
    A terminal too narrow for the table gets one wrapped line per call instead."""
    if not steps:
        return []
    title = (f"{WARNING}used so far {glyph('dot')} the turn stopped{RST}" if stopped
             else f"{MUTED}usage by step{RST}")
    table = [list(_SUMMARY_HEADER)] + [_summary_cells(s) for s in steps]
    widths = [max(display_width(row[i]) for row in table) for i in range(len(_SUMMARY_HEADER))]
    rows = [title]
    if sum(widths) + 2 * (len(widths) - 1) <= width:
        rows.append(_summary_table_row(table[0], widths, SUBTLE))
        rows += [_summary_table_row(cells, widths, MUTED) for cells in table[1:]]
        return rows
    for s in steps:
        rows += wrap_ansi(f"{MUTED}{_summary_line(s)}{RST}", max(10, width))
    return rows


TRACE_LEVELS = ("compact", "normal", "detailed", "debug")
DEFAULT_TRACE_LEVEL = "normal"


def normalize_trace_level(level: Any) -> str:
    if isinstance(level, int) and 0 <= level < len(TRACE_LEVELS):
        return TRACE_LEVELS[level]
    s = str(level or "").strip().lower()
    aliases = {"quiet": "compact", "minimal": "compact", "default": DEFAULT_TRACE_LEVEL, "verbose": "detailed",
               "full": "detailed", "raw": "debug"}
    s = aliases.get(s, s)
    return s if s in TRACE_LEVELS else "normal"


def _cost_split(model: str, usage: Any) -> str:
    """'$ in 0.0021 · cache-write 0.0004 · cache-read 0.0001 · out 0.0012' using provider pricing."""
    try:
        from decimal import Decimal

        from axon.providers.pricing import cost_details

        res = cost_details(model, usage)
        if not res.priced:
            return "cost: model not priced"
        q = res.quote
        inp = int(_get(usage, "input", default=0) or 0)
        cr = int(_get(usage, "cache_read", default=0) or 0)
        cw = int(_get(usage, "cache_write", default=0) or 0)
        out = int(_get(usage, "output", default=0) or 0)
        m = Decimal(1_000_000)
        parts = [("in", Decimal(max(0, inp - cr - cw)) / m * q.input), ("cache-write", Decimal(cw) / m * q.cache_write),
                 ("cache-read", Decimal(cr) / m * q.cache_read), ("out", Decimal(out) / m * q.output)]
        return "$ " + f" {glyph('dot')} ".join(f"{k} {float(v):.4f}" for k, v in parts if v or k in ("in", "out"))
    except Exception:
        return ""


def render_status_line(model: str, mode: str = "", context_pct: float | None = None, cache_warm: bool | None = None,
                       cache_ttl_s: float | None = None, session_cost: float | None = None,
                       trace_level: str = "", width: int | None = None) -> str:
    """'model · mode · ctx 42% · cache warm 3:12 · $0.12 · trace normal' (context: green <50, amber <75, red)."""
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    parts = [f"{TEXT}{model}{RST}"]
    if mode:
        parts.append(f"{WARNING}{mode}{RST}")
    if context_pct is not None:
        pct = max(0.0, float(context_pct))
        color = SUCCESS if pct < 50 else (AMBER if pct <= 75 else ERROR)
        parts.append(f"{color}ctx {pct:.0f}%{RST}")
    if cache_warm is not None:
        if cache_warm:
            ttl = ""
            if cache_ttl_s is not None and cache_ttl_s > 0:
                ttl = f" {int(cache_ttl_s) // 60}:{int(cache_ttl_s) % 60:02d}"
            parts.append(f"{SUCCESS}cache warm{ttl}{RST}")
        else:
            parts.append(f"{MUTED}cache cold{RST}")
    if session_cost is not None:
        parts.append(f"{WARNING}${float(session_cost):.4f}{RST}")
    if trace_level:
        parts.append(f"{MUTED}trace {trace_level}{RST}")
    return truncate("  " + dot.join(parts), (width or term_width()) - 1)



# ─── Tool header targets & result summaries ────────────────────────────────

def tool_target(name: str, inp: dict[str, Any]) -> str:
    """Concise argument shown in the '● Name(target)' header (sanitized: it is model-provided)."""
    return " ".join(_clean(_tool_target(name, inp)).split())


def _tool_target(name: str, inp: dict[str, Any]) -> str:
    inp = inp or {}
    path = _short_path(str(_first(inp, "path", "file_path", "AbsolutePath", "TargetFile", "DirectoryPath")))
    if name in READ_TOOLS:
        off = _first(inp, "offset", "StartLine", default=None)
        lim = _first(inp, "limit", default=None)
        end = _first(inp, "EndLine", default=None)
        try:
            if off and lim:
                return f"{path} {glyph('dot')} lines {int(off)}-{int(off) + int(lim) - 1}"
            if off and end:
                return f"{path} {glyph('dot')} lines {int(off)}-{int(end)}"
            if off:
                return f"{path} {glyph('dot')} from line {int(off)}"
        except (TypeError, ValueError):
            pass
        return path
    if name in BASH_TOOLS:     # shown in full (wraps); '\'-continued lines joined
        return _one_line(re.sub(r"[ \t]*\\\n", " ", _strip_cd_prefix(str(_first(inp, "command", "CommandLine")))), 600)
    if name in WRITE_TOOLS | EDIT_TOOLS | MULTI_EDIT_TOOLS or name in ("Patch", "CodeSymbols"):
        return path
    if name in LS_TOOLS or name == "FileTree":
        return path or "."
    if name == "Glob":
        base = _first(inp, "path")
        return f"{_first(inp, 'pattern', default='*')}" + (f" in {base}" if base else "")
    if name in GREP_TOOLS:
        base = _first(inp, "path", "SearchPath")
        return f"\"{_first(inp, 'pattern', 'Query')}\"" + (f" in {base}" if base else "")
    if name == "Git":
        return f"{_first(inp, 'subcommand', default='status')} {_first(inp, 'args')}".strip()
    if name in ("WebSearch", "DeepResearch", "TableSearch"):
        return f"\"{_one_line(_first(inp, 'query'), 80)}\""
    if name == "WebFetch":
        return str(_first(inp, "url"))
    if name == "Http":
        return f"{str(_first(inp, 'method', default='GET')).upper()} {_first(inp, 'url')}"
    if name == "TodoWrite":
        return _plural(len(inp.get("todos") or []), "item")
    if name == "Task":
        what = _one_line(_first(inp, "description", "title") or str(_first(inp, "prompt")).split("\n")[0], 60)
        kind = _one_line(_first(inp, "subagent_type") or "general-purpose", 30)
        return f"{what} {glyph('dot')} {kind}"
    if name in ("Process", "Env"):
        var = _first(inp, "variable")
        return f"{_first(inp, 'action', default='list')}" + (f" {var}" if var else "")
    if name == "Diff":
        return f"{_first(inp, 'path_a')} {glyph('arrow')} {_first(inp, 'path_b')}"
    key = _generic_target_key(inp)
    return _one_line(inp[key], 60) if key else ""


# Keys that best name what a generic / MCP tool acts on, most telling first (the header shows that one).
_TARGET_PREFERRED = ("query", "q", "url", "uri", "path", "file_path", "file", "filename", "pattern", "command",
                     "name", "title", "id", "key", "text", "prompt")


def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float)) and not isinstance(v, bool) and bool(str(v).strip())


def _generic_target_key(inp: dict[str, Any]) -> str:
    """The argument a generic tool's header shows: a well-known key, else the first scalar one."""
    for k in _TARGET_PREFERRED:
        if _is_scalar(inp.get(k)):
            return k
    return next((str(k) for k, v in inp.items() if not str(k).startswith("_") and _is_scalar(v)), "")


_TOOL_PURPOSE = {
    "Read": "reads a file", "Write": "writes a file", "Edit": "edits a file", "MultiEdit": "makes several edits to a file",
    "Bash": "runs a shell command", "BashOutput": "reads a background command's output", "KillShell": "stops a background command",
    "Grep": "searches inside files", "Glob": "finds files by name", "Ls": "lists a folder", "FileTree": "shows the folder tree",
    "WebSearch": "searches the web", "WebFetch": "fetches a web page", "Task": "delegates to a subagent (own context, returns one report)",
    "TodoWrite": "updates the task plan", "AskUserQuestion": "asks you a question", "Git": "runs a git command",
    "Patch": "applies a patch", "NotebookEdit": "edits a notebook cell", "CodeSymbols": "lists code symbols",
    "Http": "sends an HTTP request", "ExitPlanMode": "proposes a plan for your approval", "Diff": "compares two files",
    "DeepResearch": "researches a question on the web", "TableSearch": "searches tabular data",
    "Process": "inspects running processes", "Env": "reads environment variables", "Doctor": "checks the setup",
}


def tool_purpose(name: str, registry: Any = None) -> str:
    """What a tool does, in a few plain words ('runs a shell command'), for the line under its header."""
    if name in _TOOL_PURPOSE:
        return _TOOL_PURPOSE[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP tool from {parts[1]}" if len(parts) > 2 else "MCP tool"
    try:
        desc = str(getattr(registry.get(name), "description", "") or "") if registry is not None else ""
    except Exception:
        desc = ""
    first = re.split(r"(?<=[.!?])\s|\n", desc.strip(), maxsplit=1)[0].rstrip(".") if desc.strip() else ""
    return _one_line(first[:1].lower() + first[1:], 60) if first else ""


_CD_PREFIX_RE = re.compile(r"""^\s*cd\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s*(?:&&|;)\s*""")


def _strip_cd_prefix(command: str) -> str:
    """Drop a leading 'cd <current dir> &&' (models add it to every command; it hides the real one)."""
    m = _CD_PREFIX_RE.match(command or "")
    if not m:
        return command
    target = m.group(1) or m.group(2) or m.group(3) or ""
    try:
        if Path(os.path.expanduser(target)).resolve() == Path.cwd().resolve():
            return command[m.end():]
    except (OSError, ValueError):
        pass
    return command


# ─── Tool call input: header, command block, argument rows ────────────────
#
#   ● Bash  $ pytest tests/ -q \
#             --maxfail 1                       <- the rest of a multi-line command, aligned under it
#       Run the unit tests                      <- what it does (the command's own description)
#       timeout  120                            <- every argument the header does not already show
#
# Every tool (built-in, MCP or unknown) gets the same shape; the same rows are drawn live, by /inspect
# (format_trace) and for restored sessions (format_conversation_replay).

_PATH_KEYS = ("path", "file_path", "AbsolutePath", "TargetFile", "DirectoryPath", "notebook_path")
# Arguments a tool's header (or its purpose line) already shows, per tool: the input rows skip them.
_TARGET_KEYS: dict[str, tuple[str, ...]] = {}
for _names, _keys in (
    (READ_TOOLS, _PATH_KEYS + ("offset", "limit", "StartLine", "EndLine")),
    (BASH_TOOLS, ("command", "CommandLine", "description")),
    (WRITE_TOOLS | EDIT_TOOLS | MULTI_EDIT_TOOLS | LS_TOOLS | {"Patch", "CodeSymbols", "NotebookEdit", "FileTree"},
     _PATH_KEYS),
    ({"Glob"}, ("pattern", "path")),
    (GREP_TOOLS, ("pattern", "Query", "path", "SearchPath")),
    ({"Git"}, ("subcommand", "args")),
    ({"WebSearch", "DeepResearch", "TableSearch"}, ("query",)),
    ({"WebFetch"}, ("url",)),
    ({"Http"}, ("method", "url")),
    ({"TodoWrite"}, ("todos",)),
    ({"Task", "Agent"}, ("description", "title", "subagent_type")),
    ({"Process", "Env"}, ("action", "variable")),
    ({"Diff"}, ("path_a", "path_b")),
):
    for _n in _names:
        _TARGET_KEYS[_n] = _keys
# Bodies the result card draws in full (the diff of an Edit, the file of a Write): hidden at the normal
# level, a size summary at detailed, the text itself at debug.
_BODY_KEYS = {"content", "CodeContent", "old_string", "new_string", "edits", "patch", "new_source"}
_ARG_ROWS = (0, 2, 6, 16)          # rows per argument value, by trace detail
_ARG_COUNT = (0, 8, 40, 200)       # arguments listed, by trace detail
_CMD_LINES = (1, 12, 40, 400)      # lines of a multi-line shell command, by trace detail


def tool_label(name: str) -> str:
    """Display name of a tool: 'mcp__github__search_issues' -> 'github › search_issues'."""
    name = _clean(name)
    if name.startswith("mcp__"):
        parts = [p for p in name.split("__")[1:] if p]
        if len(parts) >= 2:
            return f"{parts[0]} {glyph('chevron')} {'__'.join(parts[1:])}"
    return name


def _command_lines(inp: dict[str, Any]) -> list[str]:
    """A shell command as the lines it was written in (a leading 'cd <workspace> &&' dropped)."""
    cmd = _strip_cd_prefix(str(_first(inp, "command", "CommandLine")))
    lines = [sanitize(strip_ansi(l)).rstrip().expandtabs(4) for l in cmd.strip("\n").splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def tool_header_rows(name: str, inp: dict[str, Any], detail: int = 1) -> list[tuple[int, str]]:
    """The call header: ``[(indent, row)]`` - '● Name  target' and, for a multi-line shell command, its
    other lines indented to sit under the first one. Compact (detail 0) is the one-line 'Name(target)'."""
    inp = inp or {}
    mark = f"{PRIMARY}{glyph('tool')}{RST}"
    label = tool_label(name)
    if name in BASH_TOOLS:
        lines = _command_lines(inp)
        if detail <= 0:
            return [(0, f"{mark} {BOLD}{label}{RST}" + (f"{MUTED}({RST}{tool_target(name, inp)}{MUTED}){RST}" if lines else ""))]
        if not lines:
            return [(0, f"{mark} {BOLD}{label}{RST}")]
        lead = f"{mark} {BOLD}{label}{RST}  "
        rows = [(0, lead + f"{MUTED}${RST} {TEXT}{lines[0]}{RST}")]
        keep = _lim(_CMD_LINES, detail)
        indent = display_width(lead) + 2
        rows += [(indent, f"{TEXT}{l}{RST}") for l in lines[1:keep]]
        if len(lines) > keep:
            rows.append((indent, f"{SUBTLE}{glyph('ellipsis')} +{_plural(len(lines) - keep, 'more line')}{RST}"))
        return rows
    target = tool_target(name, inp)
    if detail <= 0:
        return [(0, f"{mark} {BOLD}{label}{RST}" + (f"{MUTED}({RST}{target}{MUTED}){RST}" if target else ""))]
    return [(0, f"{mark} {BOLD}{label}{RST}" + (f"  {TEXT}{target}{RST}" if target else ""))]


def tool_what_row(name: str, inp: dict[str, Any], registry: Any = None) -> tuple[str, set[str]]:
    """'finds files by name · find files matching *.toml in src, show at most 5' and the argument keys
    that sentence already covers (so they are not repeated as 'key  value' rows). A Bash call's own
    description replaces the generic purpose; its command is the header."""
    purpose = tool_purpose(name, registry)
    summary, covered = _decision.describe_arguments(name, inp or {})
    if name in BASH_TOOLS:
        text = summary or purpose
    else:
        text = f" {glyph('dot')} ".join(b for b in (purpose, _clean(summary)) if b)
    return (f"{SUBTLE}{text}{RST}" if text else ""), covered


def tool_why_row(why: str, source: str = "") -> str:
    """'model said: “…”' / 'model thought: “…”' / "Axon's note: read-only lookup" ('' without a why)."""
    line = _decision.rationale_line(_clean(why), source)
    if not line:
        return ""
    label, _, text = line.partition(": ")
    return f"{SUBTLE}{label}:{RST} {DIM}{ITALIC}{text}{RST}"


def tool_chosen_row(chosen: str) -> str:
    # agent/decision.py joins the facts with a literal ' · ': use the encoding-aware dot (ASCII under AXON_ASCII)
    dot = glyph("dot")
    return f"{SUBTLE}{_clean(chosen).replace(' · ', f' {dot} ')}{RST}" if chosen else ""


def tool_allowed_row(outcome: str, source: str = "", reason: str = "", approval: Any = None) -> str:
    """'✓ allowed: bypass mode is on' / '✗ denied by rule: …' (one short line)."""
    text = _one_line(_decision.allowed_because(outcome, source, reason, approval), 200)
    outcome = str(outcome or "").lower()
    if outcome == "allow":
        return f"{SUCCESS}{glyph('ok')}{RST} {MUTED}{text}{RST}"
    if outcome == "deny":
        return f"{ERROR}{glyph('fail')}{RST} {MUTED}{text}{RST}"
    return f"{WARNING}{glyph('warn')}{RST} {MUTED}{text}{RST}"


def tool_then_row(text: str) -> str:
    return f"{SUBTLE}{glyph('arrow')} {text}{RST}" if text else ""


def _arg_value_rows(v: Any, width: int, max_rows: int) -> list[str]:
    """One argument value as rows: scalars coloured by type, text wrapped, objects as (pretty) JSON."""
    import json

    if v is None:
        return [f"{MUTED}null{RST}"]
    if isinstance(v, bool):
        return [f"{INFO}{'true' if v else 'false'}{RST}"]
    if isinstance(v, (int, float)):
        return [f"{INFO}{v}{RST}"]
    if isinstance(v, (dict, list)):
        try:
            compact = json.dumps(v, ensure_ascii=False, default=str)
            pretty = json.dumps(v, indent=2, ensure_ascii=False, default=str).splitlines()
        except (TypeError, ValueError):
            compact, pretty = repr(v), [repr(v)]
        compact = _clean(compact)
        if display_width(compact) <= width or max_rows <= 2:
            return [highlight(truncate(compact, width), "json")]
        rows = [highlight(truncate(_clean(l), width), "json") for l in pretty[:max_rows]]
        if len(pretty) > max_rows:
            rows.append(f"{SUBTLE}{glyph('ellipsis')} +{_plural(len(pretty) - max_rows, 'more line')}{RST}")
        return rows
    lines = [l.expandtabs(4) for l in _clean(v).splitlines()] or [""]
    if len(lines) > 1 and max_rows <= 2:          # normal level: the first line and how much follows
        more = f" {SUBTLE}{glyph('wrap')} +{_plural(len(lines) - 1, 'line')}{RST}"
        return [f"{TEXT}{truncate(lines[0], max(8, width - display_width(more)))}{RST}{more}"]
    rows: list[str] = []
    for i, l in enumerate(lines):
        rows += wrap_ansi(f"{TEXT}{l}{RST}", width) or [""]
        if len(rows) > max_rows:
            left = len(lines) - i - 1
            rows = rows[:max_rows - 1] + [truncate(rows[max_rows - 1], width - 1) + f"{SUBTLE}{glyph('ellipsis')}{RST}"]
            if left:
                rows.append(f"{SUBTLE}{glyph('ellipsis')} +{_plural(left, 'more line')}{RST}")
            break
    return rows


def _body_summary(v: Any) -> str:
    """'12 lines · 340 B' / '3 items' for an argument the result card already draws."""
    if isinstance(v, str):
        return f"{MUTED}{_plural(_cards.count_lines(v), 'line')} {glyph('dot')} {_fmt_bytes(len(v.encode('utf-8', errors='replace')))}{RST}"
    if isinstance(v, (list, dict)):
        return f"{MUTED}{_plural(len(v), 'item')}{RST}"
    return f"{MUTED}{_one_line(v, 40)}{RST}"


def tool_input_rows(name: str, inp: dict[str, Any], width: int | None = None, detail: int = 1,
                    skip: set[str] | None = None) -> list[str]:
    """The call's arguments as aligned 'key  value' rows, leaving out what the header already shows.

    compact: none · normal: every other argument (long text: its first line) · detailed: also the bodies
    the result card draws (as a size) and longer values · debug: everything, bodies in full."""
    if detail <= 0 or not isinstance(inp, dict):
        return []
    width = max(24, width or _width(160) - 10)
    shown = set(_TARGET_KEYS[name]) if name in _TARGET_KEYS else {_generic_target_key(inp)}
    shown |= set(skip or ())       # already said in plain words on the 'what' row
    items = [(str(k), v) for k, v in inp.items() if not str(k).startswith("_") and str(k) not in shown]
    if detail < 2:
        items = [(k, v) for k, v in items if k not in _BODY_KEYS and v not in (None, "", [], {})]
    if not items:
        return []
    key_w = min(18, max(display_width(_clean(k)) for k, _ in items))
    val_w = max(16, width - key_w - 2)
    limit, max_rows = _lim(_ARG_COUNT, detail), _lim(_ARG_ROWS, detail)
    rows: list[str] = []
    for k, v in items[:limit]:
        vals = [_body_summary(v)] if k in _BODY_KEYS and detail == 2 else _arg_value_rows(v, val_w, max_rows)
        rows.append(f"{MUTED}{pad(truncate(_clean(k), key_w), key_w)}{RST}  {vals[0]}")
        rows += [f"{' ' * key_w}  {r}" for r in vals[1:]]
    if len(items) > limit:
        rows.append(f"{SUBTLE}{glyph('ellipsis')} +{_plural(len(items) - limit, 'more argument')}{RST}")
    return rows


def tool_call_rows(name: str, inp: dict[str, Any], detail: int = 1, width: int | None = None,
                   registry: Any = None, why: str = "", why_source: str = "",
                   chosen: str = "") -> list[tuple[int, str]]:
    """Header and explanation rows of one tool call as ``[(indent, row)]`` (indent in columns, relative
    to the '●'): what the live timeline, /inspect and restored sessions all print.

    Below the header (normal level and up): who chose the call (*chosen*), why (the model's own words,
    *why* from *why_source*), what it does with its arguments in plain words, then any argument the
    plain words do not cover as 'key  value' rows. Detailed and debug list every argument."""
    rows = tool_header_rows(name, inp, detail)
    if detail >= 1:
        what, covered = tool_what_row(name, inp, registry)
        for row in (tool_chosen_row(chosen), tool_why_row(why, why_source), what):
            if row:
                rows.append((4, row))
        skip = covered if detail < 2 else set()
        rows += [(4, r) for r in tool_input_rows(name, inp, (width or _width(160) - 10), detail, skip)]
    return rows


def _count_lines(content: str) -> int:
    return len([l for l in content.splitlines() if l.strip()])


def _preview(lines: list[str], keep: int = 4) -> list[str]:
    if len(lines) <= keep + 1:
        return lines
    return lines[:keep] + [f"{SUBTLE}{glyph('ellipsis')} +{len(lines) - keep} more lines {glyph('dot')} {_MORE_HINT} to view all{RST}"]


_DEFAULT_REGISTRY: Any = None


def _tool_summary(registry: Any, name: str, inp: dict[str, Any], content: str, is_error: bool) -> str:
    """One-line summary from the tool's own ``summarize(args, output, is_error)`` (guarded)."""
    global _DEFAULT_REGISTRY
    reg = registry
    if reg is None:
        if _DEFAULT_REGISTRY is None:
            try:
                from axon.tools import create_default_registry

                _DEFAULT_REGISTRY = create_default_registry()
            except Exception:
                _DEFAULT_REGISTRY = False
        reg = _DEFAULT_REGISTRY or None
    if reg is None:
        return ""
    try:
        tool = reg.get(name)
        for _ in range(4):          # aliases / deferred proxies wrap the real tool
            inner = getattr(tool, "target", None)
            if inner is None or not callable(getattr(inner, "summarize", None)):
                break
            tool = inner
        try:
            from axon.tools.base import Tool as _BaseTool

            if getattr(type(tool), "summarize", None) is _BaseTool.summarize:
                return ""   # the generic 'first line · N lines' summary; the renderer's own rows are clearer
        except ImportError:
            pass
        fn = getattr(tool, "summarize", None)
        text = fn(inp or {}, content or "", is_error) if callable(fn) else ""
        # tools join their facts with a literal ' · ': use the encoding-aware dot (ASCII under AXON_ASCII)
        return sanitize(str(text or "")).strip().splitlines()[0].replace(" · ", f" {glyph('dot')} ") if text else ""
    except Exception:
        return ""


def tool_result_rows(name: str, inp: dict[str, Any], content: str, is_error: bool = False,
                     width: int | None = None, registry: Any = None, detail: int = 1, link: str = "",
                     preview: bool = True) -> list[str]:
    """Result rows: the tool's own one-line summary (when it provides one) + rendered body.

    *detail* follows the trace level (0 compact: the summary row only, 1 normal, 2+ detailed: longer
    previews); *link* (a file:// URI of the full output) makes the summary row an OSC 8 hyperlink.
    ``preview=False`` leaves out the rendered body (the caller shows the exact text instead)."""
    changes = list(inp.get("_changes") or ())
    content, listed = _split_file_changes(content)
    changes = changes or listed
    rows = summarize_tool_result(name, inp, content, is_error, width, detail=detail)
    if not preview:
        rows = rows[:1]
    if changes:
        rows = rows + _cards.file_change_rows(changes, width or _width(140),
                                              _lim(_DIFF_ROWS, detail) if detail > 0 else 1 + len(changes), _MORE_HINT)
    if _is_card_rows(name, rows) or (not is_error and name in _CARD_HEADER_TOOLS | {"TodoWrite", "Task"}):
        return rows  # file / diff / plan / subagent cards already carry an exact summary
    # the tool's own summary names a success better than a line count; an error keeps its full message
    summary = "" if is_error else _tool_summary(registry, name, inp, content, is_error)
    if summary:
        rows = [summary] + rows[1:]
    if link and rows and "\x1b]8;" not in rows[0]:
        rows[0] = hyperlink(link, rows[0])
    return rows


_CHANGES_RE = re.compile(r"\n?\[Files changed by this command: (.*)\]\s*$", re.S)
_CHANGE_ITEM_RE = re.compile(r"(created|modified|deleted) (.+?)(?: \((\+\d+)?\s*([−-]\d+)?\))?$")
_RENAME_ITEM_RE = re.compile(r"renamed (.+?) → (.+)$")


def _split_file_changes(content: str) -> tuple[str, list[Any]]:
    """The output without its '[Files changed by this command: …]' line, and that line's changes
    (paths and counts only: restored history has no texts to diff)."""
    m = _CHANGES_RE.search(content or "")
    if not m:
        return content, []
    from axon.agent.filechanges import FileChange
    changes = []
    for item in m.group(1).split(", "):
        rm = _RENAME_ITEM_RE.match(item.strip())
        if rm:
            changes.append(FileChange(rm.group(2), "renamed", None, None, old_path=rm.group(1)))
            continue
        im = _CHANGE_ITEM_RE.match(item.strip())
        if im:
            added = int(im.group(3)[1:]) if im.group(3) else None
            removed = int(im.group(4)[1:]) if im.group(4) else None
            changes.append(FileChange(im.group(2), im.group(1), added, removed))
    return content[:m.start()], changes


# Tools whose first row is a file / diff card header (it links to the file itself, not the output).
_CARD_HEADER_TOOLS = WRITE_TOOLS | EDIT_TOOLS | MULTI_EDIT_TOOLS | {"Patch", "NotebookEdit"}
# Tools drawn as cards: the detailed trace shows a longer card instead of the raw output preview.
CARD_TOOLS = _CARD_HEADER_TOOLS | READ_TOOLS | BASH_TOOLS | LS_TOOLS | GREP_TOOLS | {
    "Glob", "Task", "TodoWrite", "WebFetch", "Http", "Git", "Diff"}

# trace detail -> preview sizes (compact, normal, detailed, full: the transcript viewer's expanded view)
_FULL = 200_000
_CODE_LINES = (0, 30, 120, _FULL)
_DIFF_ROWS = (0, 60, 240, _FULL)
_READ_LINES = (0, 15, 30, _FULL)      # normal: as many lines as the exact result shows (EXACT_RESULT_LINES)
_LIST_ROWS = (0, 6, 24, _FULL)
_GREP_ROWS = (0, 12, 40, _FULL)
_JSON_LINES = (0, 12, 40, _FULL)
_OUT_HEAD_TAIL = ((0, 0), (4, 2), (20, 6), (_FULL, 0))
# tools whose expanded transcript entry is drawn with the live card (others: numbered raw output)
_TRANSCRIPT_CARDS = _CARD_HEADER_TOOLS | READ_TOOLS | GREP_TOOLS | {"Glob", "WebFetch", "Http"}


def transcript_card_rows(name: str, detail_json: str, body: str, width: int) -> list[str] | None:
    """Full card body (without its summary row) for an expanded tool entry of the transcript viewer,
    so history looks like the live timeline; None when the tool has no card (raw output is shown)."""
    if not body.strip() and name not in _CARD_HEADER_TOOLS:
        return None
    if name not in _TRANSCRIPT_CARDS and not (name.startswith("mcp__") and _cards.find_json(body)):
        return None
    try:
        import json as _json
        inp = _json.loads(detail_json) if detail_json else {}
        rows = tool_result_rows(name, inp if isinstance(inp, dict) else {}, body, False, width, detail=3)
    except Exception:
        return None
    if name in _CARD_HEADER_TOOLS:
        return rows            # the header row says what changed
    return rows[1:] or None


def _lim(table: tuple, detail: int) -> Any:
    return table[max(0, min(len(table) - 1, detail))]


def summarize_tool_result(name: str, inp: dict[str, Any], content: str, is_error: bool = False,
                          width: int | None = None, detail: int = 1) -> list[str]:
    """Rows printed under a tool header (first row follows the '⎿' marker)."""
    width = width or _width(160) - 8
    rows = _summarize_tool_result(name, inp or {}, content, is_error, width, detail)
    if detail <= 0 and not is_error and name not in ("Task", "TodoWrite", "ExitPlanMode") and len(rows) > 1:
        # compact: one row per tool (a file / diff card keeps just its header row)
        n = len(sanitize(content or "").strip().splitlines())
        n = max(n, len(rows) - 1)
        rows = rows[:1] + ([] if name in _CARD_HEADER_TOOLS or n <= 1 else [_cards.more_row(n, _MORE_HINT)])
    return rows


def _summarize_tool_result(name: str, inp: dict[str, Any], content: str, is_error: bool, width: int,
                           detail: int) -> list[str]:
    content = sanitize(content or "")
    text = content.strip()
    lines = text.splitlines()
    path = _short_path(str(_first(inp, "path", "file_path", "AbsolutePath", "TargetFile", "notebook_path")))
    dot = f" {SUBTLE}{glyph('dot')}{RST} "

    if is_error and not (name in ("Task", "Agent") and text.startswith("[Subagent #")):
        return _error_rows(lines, width, detail)

    if name in READ_TOOLS:
        n = len(lines)
        head = f"Read {BOLD}{n}{RST} {'line' if n == 1 else 'lines'}"
        numbered = [m for m in (re.match(r"^\s*(\d+)→(.*)$", l) for l in lines) if m]
        keep = _lim(_READ_LINES, detail)
        if numbered and len(numbered) >= len(lines) // 2:
            shown = numbered[:keep]
            last = int(numbered[-1].group(1))
            body = _cards.numbered_rows([m.group(2) for m in shown], _lang_for(path), width,
                                        int(shown[0].group(1)) if shown else 1, max(2, len(str(last))))
            if len(lines) > len(shown):
                body.append(_cards.more_row(len(lines) - len(shown), _MORE_HINT))
            return [head] + body
        import textwrap
        body = textwrap.dedent(content.rstrip().lstrip("\n")).splitlines()     # keep the line-number column aligned
        return [head] + _preview([f"{DIM}{l}{RST}" for l in body], 3)
    if name in BASH_TOOLS:
        return _bash_rows(content, width, detail)
    if name in EDIT_TOOLS | MULTI_EDIT_TOOLS:
        if name in MULTI_EDIT_TOOLS:
            pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict)]
        else:
            pairs = [(str(inp.get("old_string", "") or ""), str(inp.get("new_string", "") or ""))]
        added = removed = 0
        body: list[str] = []
        budget = _lim(_DIFF_ROWS, detail)
        for k, (old_s, new_s) in enumerate(pairs):
            a, r = _diff_counts(old_s, new_s)
            added, removed = added + a, removed + r
            if detail <= 0 or len(body) >= budget:
                continue
            if k:
                body.append(f"{SUBTLE}{' ' * 3}  {glyph('hunk') * 3} edit {k + 1}{RST}")
            start = (_locate_line(path, new_s, 0) if new_s else
                     _locate_line(path, old_s.splitlines()[0] if old_s.strip() else "", 0))   # 0: unknown, no numbers
            body += diff_rows(old_s, new_s, start, width, _lang_for(path), max_rows=max(4, budget - len(body)))
        if not pairs or (not added and not removed):
            return [text.splitlines()[0] if text else f"Updated {path}"]
        extra = _plural(len(pairs), "edit") if len(pairs) > 1 else ""
        return [_cards.diff_header(path, added, removed, "Edited", extra)] + body
    if name in WRITE_TOOLS:
        code = str(_first(inp, "content", "CodeContent"))
        prev = inp.get("_prev_content")
        if isinstance(prev, str):            # live: the old content is known, so always show the diff
            if prev == code:
                return [_cards.code_card_header(path, code, "Overwrote") + f"{dot}{MUTED}no changes{RST}"]
            a, r = _diff_counts(prev, code)
            header = _cards.diff_header(path, a, r, "Overwrote", _plural(_cards.count_lines(code), "line"))
            if detail <= 0:
                return [header]
            return [header] + diff_rows(prev, code, 1, width, _lang_for(path), max_rows=_lim(_DIFF_ROWS, detail), eof_notes=True)
        if "Overwrote" in text:              # restored history: the counts the tool reported, then the file
            m = re.search(r"Overwrote file \(\+(\d+) [−-](\d+)\)", text)
            counts = (int(m.group(1)), int(m.group(2))) if m else (None, None)
            header = _cards.code_card_header(path, code, "Overwrote", *counts)
            if detail <= 0:
                return [header]
            return [header] + _cards.code_body(code, path, width, _lim(_CODE_LINES, detail), _MORE_HINT)
        header = _cards.code_card_header(path, code, "Created")      # the line count is in the header: no '+N' twice
        if detail <= 0 or not code:
            return [header]                  # a new file: every line is an addition
        return [header] + diff_rows("", code, 1, width, _lang_for(path), max_rows=_lim(_CODE_LINES, detail))
    if name == "Patch":
        patch = str(inp.get("patch") or "")
        files, body = _cards.patch_rows(patch, width, _lim(_DIFF_ROWS, detail), _MORE_HINT)
        if files:
            a, r = sum(f["added"] for f in files), sum(f["removed"] for f in files)
            if len(files) == 1:
                header = _cards.diff_header(files[0]["path"] or path, a, r, "Patched", _plural(len(files[0]["hunks"]), "hunk"))
            else:
                header = f"{INFO}{glyph('edited')} Patched{RST} {BOLD}{_plural(len(files), 'file')}{RST}{dot}{_cards.change_badge(a, r)}"
            return [header] + body
        return [f"{SUCCESS}{text}{RST}" if text else "Patch applied"]
    if name == "NotebookEdit":
        mode = str(inp.get("edit_mode") or "replace")
        new_s = "" if mode == "delete" else str(inp.get("new_source") or "")
        old_s = "" if mode == "insert" else str(inp.get("_prev_content") or "")
        if mode in ("replace", "insert", "delete") and (old_s or new_s) and old_s != new_s:
            a, r = _diff_counts(old_s, new_s)
            cell = inp.get("cell_id") or inp.get("cell_index")
            header = _cards.diff_header(path, a, r, "Edited", f"cell {cell}" if cell not in (None, "") else mode)
            if detail <= 0:
                return [header]
            lang = "markdown" if str(inp.get("cell_type") or "") == "markdown" else "python"
            return [header] + diff_rows(old_s, new_s, 1, width, lang, max_rows=_lim(_DIFF_ROWS, detail))
        return [f"{TEXT}{lines[0]}{RST}" if lines else "notebook updated"]
    if name in LS_TOOLS:
        n = _count_lines(text) if text != "(Empty directory)" else 0
        head = f"Listed {BOLD}{n}{RST} {'path' if n == 1 else 'paths'}"
        if not n:
            return [head]
        entries = [l for l in lines if l.strip() and not l.startswith(("...", "(")) and not re.match(r"^\(\d+ dir", l)]
        if any(("├" in l or "└" in l or "|--" in l) for l in entries):
            tree = [f"{PRIMARY}{BOLD}{l}{RST}" if l.rstrip().endswith("/") else f"{TEXT}{l}{RST}" for l in entries]
            return [head] + _preview(tree, _lim(_LIST_ROWS, detail))
        base = str(_first(inp, "path", default="")) or ""
        return [head] + _cards.column_rows([re.sub(r" \([\d.]+ ?[KMG]?B\)$", "", l.strip()) for l in entries], width, _lim(_LIST_ROWS, detail), _MORE_HINT, base)
    if name == "Glob":
        n = 0 if text.startswith("No files") else _count_lines(text)
        head = f"Found {BOLD}{n}{RST} {'file' if n == 1 else 'files'}"
        if not n:
            return [head]
        paths = [l for l in lines if l.strip() and not l.startswith("[")]
        return [head] + _cards.column_rows(paths, width, _lim(_LIST_ROWS, detail), _MORE_HINT)
    if name in GREP_TOOLS:
        if not text or text.startswith("No matches"):
            return [f"{MUTED}No matches{RST}"]
        hits, files, body = _cards.grep_rows(content, str(_first(inp, "pattern", "Query")), width,
                                             _lim(_GREP_ROWS, detail), _MORE_HINT, bool(inp.get("-i")))
        n = hits or _count_lines(text)
        return [f"Found {BOLD}{n}{RST} {'match' if n == 1 else 'matches'}"
                + (f" in {_plural(files, 'file')}" if files > 1 else "")] + body
    if name in ("FileTree", "CodeSymbols"):
        body = [l for l in content.rstrip().lstrip("\n").splitlines() if l.strip()]
        head = f"{BOLD}{len(body)}{RST} " + ("entries" if name == "FileTree" else "symbol lines")
        if name == "FileTree":
            styled = [f"{PRIMARY}{BOLD}{l}{RST}" if l.rstrip().endswith("/") else f"{TEXT}{l}{RST}" for l in body]
        else:
            styled = [f"{INFO}{l}{RST}" if re.search(r"\b(def|class|func|fn|function)\b", l) else f"{TEXT}{l}{RST}" for l in body]
        keep = _lim(_LIST_ROWS, detail)
        return [head] + styled[:keep] + ([_cards.more_row(len(body) - keep, _MORE_HINT)] if len(body) > keep else [])
    if name == "WebSearch":
        return [strip_ansi(render_web_search_box(str(_first(inp, "query")), content)).strip()]
    if name in ("WebFetch", "Http"):
        if name == "WebFetch":
            first = strip_ansi(render_web_fetch_box(str(_first(inp, "url")), content)).strip()
        else:
            size_kb = len(content.encode("utf-8", errors="replace")) / 1024
            first = f"{lines[0] if lines else '(empty)'} {MUTED}{glyph('dot')} {size_kb:.1f} KB{RST}"
        found = _cards.find_json(content)
        if found and detail > 0:
            return [first] + _cards.json_rows(found[1], width, _lim(_JSON_LINES, detail), _MORE_HINT)
        return [first]
    if name == "Git":
        return _preview([_git_line(l) for l in lines if l.strip()] or [f"{MUTED}(no output){RST}"], 6)
    if name == "TodoWrite":
        return _todo_summary(content, width=width)
    if name == "Task":
        # the card sits under the "⎿" gutter, so it gets the width left after that indent
        return [r[2:] for r in render_subagent_result_card(content, width=max(40, width - 6)).splitlines()]
    if name == "ExitPlanMode":
        return [f"{ACCENT}{BOLD}Plan proposed for approval{RST}"] + format_markdown(content, max_width=width).splitlines()
    if name == "Diff":
        return _preview([highlight(l, "diff") for l in lines], 20)
    if name == "Doctor":
        return _doctor_rows(content) or [f"{MUTED}(no output){RST}"]
    if name == "AskUserQuestion":
        answers = re.findall(r"->\s*(.+)$", text, re.MULTILINE)
        if answers:
            return [f"{SUCCESS}answered:{RST} {TEXT}{_one_line('; '.join(answers), 120)}{RST}"]
    return _generic_rows(content, path, width, detail)


# ─── Result bodies shared by every tool without a dedicated card ──────────

_NO_OUTPUT_RE = re.compile(r"^\(Command completed with exit code 0 and no output(?: in ([\d.]+)s)?\)$")
_CWD_NOTE_RE = re.compile(r"^\(cwd: (.+)\)$")
_EXIT_RE = re.compile(r"^Command exited with non-zero code (-?\d+)\.?[ \t]*\n?(?:Output:[ \t]*\n?)?")


def _trim_blank(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


def _error_rows(lines: list[str], width: int, detail: int) -> list[str]:
    """A failed call: 'error: <message>' first, then the details behind a red accent bar (head + tail
    when long). The bar and the word 'error' carry the meaning without colour."""
    lines = _trim_blank(list(lines)) or ["Tool failed"]
    msg = re.sub(r"^(?:error|Error|ERROR)\s*:\s*", "", lines[0].strip()) or "Tool failed"
    rows = [f"{ERROR}{BOLD}error{RST}{ERROR}: {msg}{RST}"]
    rest = _trim_blank(lines[1:])
    if not rest:
        return rows
    bar = f"{ERROR}{glyph('quote')}{RST} "
    h, t = (6, 2) if detail <= 1 else _lim(_OUT_HEAD_TAIL, detail)
    n = len(rest)
    head, tail = (rest, []) if n <= h + t + 1 else (rest[:h], rest[n - t:] if t else [])

    def style(l: str) -> str:
        l = l.expandtabs(4)
        return _cards.classify_output_line(l) if _cards._TB_FILE_RE.match(l) else f"{ERROR}{l}{RST}"

    rows += [bar + style(l) for l in head]
    if tail or n > len(head):
        rows.append(bar + _cards.more_row(n - len(head) - len(tail), _MORE_HINT))
        rows += [bar + style(l) for l in tail]
    return rows


def _bash_rows(content: str, width: int, detail: int) -> list[str]:
    """Shell result: '✓ exit 0' / '✗ exit 1' (plus a test-run bar, the line count and the shell's cwd when
    it is not the workspace) as the header row, then the output, head + tail when long."""
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    text = content.strip("\n")
    code = 0
    m = _EXIT_RE.match(text.lstrip())
    if m:
        code = int(m.group(1))
        text = text.lstrip()[m.end():]
    lines = text.rstrip().splitlines()            # keep leading spaces (git status columns)
    cwd = ""
    kept: list[str] = []
    for l in lines:
        cm = _CWD_NOTE_RE.match(l.strip())
        if cm:
            cwd = cm.group(1)
        elif not _NO_OUTPUT_RE.match(l.strip()):
            kept.append(l)
    lines = _trim_blank(kept)
    status = (f"{ERROR}{BOLD}{glyph('fail')} exit {code}{RST}" if code else f"{SUCCESS}{glyph('ok')} exit 0{RST}")
    where = f"{dot}{MUTED}in {_short_path(cwd)}{RST}" if cwd else ""
    n = len(lines)
    if not n:
        return [status + dot + f"{MUTED}(no output){RST}" + where]
    summary = _cards.test_summary("\n".join(lines))
    if summary:
        bar = _cards.test_bar(summary)
        took = re.search(r"\bin ([\d.]+m?s)\b", "\n".join(lines[-3:]))
        if n == 1 and not code:
            return [bar + (f"{dot}{MUTED}{took.group(1)}{RST}" if took else "") + where]
        first = (status + dot if code else "") + bar + dot + f"{MUTED}{_plural(n, 'line')} of output{RST}" + where
    else:
        if n == 1 and not code:
            return [_classify_output_line(lines[0]) + where]          # one line: show it, not a count
        first = status + dot + f"{MUTED}{_plural(n, 'line')} of output{RST}" + where
    h, t = _lim(_OUT_HEAD_TAIL, detail)
    return [first] + _cards.output_rows(lines, width, h, t, _MORE_HINT)


def _json_shape(pretty: str) -> str:
    """'JSON object · 3 keys' / 'JSON array · 12 items' (or the line count of truncated JSON)."""
    import json

    try:
        data = json.loads(pretty)
    except ValueError:
        return f"JSON {glyph('dot')} {_plural(len(pretty.splitlines()), 'line')}"
    if isinstance(data, dict):
        return f"JSON object {glyph('dot')} {_plural(len(data), 'key')}"
    return f"JSON array {glyph('dot')} {_plural(len(data), 'item')}"


def _text_rows(lines: list[str], width: int, head: int, tail: int, lang: str = "") -> list[str]:
    """Plain output rows (highlighted when *lang* is known): the first *head* and last *tail* lines with
    a '… +N more lines · /output n' row between."""
    n = len(lines)
    keep_head, keep_tail = (lines, []) if n <= head + tail + 1 else (lines[:head], lines[n - tail:] if tail else [])

    def style(l: str) -> str:
        l = l.expandtabs(4)
        return highlight(l, lang) if lang else f"{TEXT}{l}{RST}"

    rows = [style(l) for l in keep_head]
    if keep_tail or n > len(keep_head):
        rows.append(_cards.more_row(n - len(keep_head) - len(keep_tail), _MORE_HINT))
        rows += [style(l) for l in keep_tail]
    return rows


def _generic_rows(content: str, path: str, width: int, detail: int) -> list[str]:
    """Any other tool (MCP servers, plugins): JSON pretty-printed and highlighted, other text as a line
    count + size header and the head / tail of the output (highlighted when the call names a code file)."""
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    text = content.strip()
    if not text:
        return [f"{MUTED}(no output){RST}"]
    size = f"{MUTED}{_fmt_bytes(len(text.encode('utf-8', errors='replace')))}{RST}"
    found = _cards.find_json(content)
    if found:
        head, pretty = found
        first = head.splitlines()[0] if head else f"{_json_shape(pretty)}{dot}{size}"
        return [first] + _cards.json_rows(pretty, width, _lim(_JSON_LINES, detail) if detail > 0 else 0, _MORE_HINT)
    lines = _trim_blank(content.rstrip().splitlines())
    if len(lines) == 1:
        return [f"{TEXT}{lines[0].strip()}{RST}"]
    lang = _lang_for(path) if path else ""
    h, t = _lim(_OUT_HEAD_TAIL, detail)
    return [f"{_plural(len(lines), 'line')} of output{dot}{size}"] + _text_rows(lines, width, h, t, lang)


# ─── Exact call and result: what the model sent, and what it gets back ─────

EXACT_CALL_LINES = 15      # rows of a call's exact arguments shown at the normal trace level
EXACT_RESULT_LINES = 15    # rows of a result's exact text shown at the normal trace level
_EXACT_CALL_ROWS = (0, EXACT_CALL_LINES, 40, _FULL)
_EXACT_RESULT_ROWS = (0, EXACT_RESULT_LINES, 40, _FULL)
# Results whose card is drawn from the result text itself, so the card replaces the exact text:
# Read (the numbered lines, highlighted) and the formatted cards of plans and subagent reports.
_CARD_IS_RESULT = {"TodoWrite", "Task", "Agent", "ExitPlanMode"}
# Results whose card shows something the result text does not (the diff of the call's own arguments):
# the card is kept and the exact text follows it.
_CARD_PLUS_EXACT = _CARD_HEADER_TOOLS


def _hard_wrap(line: str, width: int) -> list[str]:
    """*line* cut into pieces of at most *width* columns, keeping every character (spaces included).
    A piece ends after its last space when it has one, so words are not split."""
    pieces: list[str] = []
    current, used = "", 0
    for ch in line:
        w = display_width(ch)
        if current and used + w > width:
            cut = current.rfind(" ") + 1
            if not current[:cut].strip() or cut >= len(current):
                cut = len(current)          # no space after a word to break at: cut the word
            pieces.append(current[:cut])
            current = current[cut:]
            used = display_width(current)
        current += ch
        used += w
    pieces.append(current)
    return pieces


def exact_text_rows(text: str, width: int, max_rows: int, hint: str = "") -> list[str]:
    """*text* verbatim behind a thin bar, cut to *width* columns: the first *max_rows* rows, then
    '… +N more lines · <hint> to view all'."""
    bar = f"{SUBTLE}{glyph('v')}{RST} "
    lines = sanitize(text or "").expandtabs(4).split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [bar + f"{MUTED}(empty){RST}"]
    rows: list[str] = []
    piece_width = max(10, width - 2)
    for index, line in enumerate(lines):
        room = (max_rows - len(rows) + 1) * piece_width
        for piece in _hard_wrap(line[:room], piece_width):   # a huge line is only cut as far as it is shown
            if len(rows) >= max_rows:
                rows.append(bar + _cards.more_row(len(lines) - index, hint))
                return rows
            rows.append(bar + f"{TEXT}{piece}{RST}")
    return rows


def exact_call_rows(name: str, inp: dict[str, Any], width: int, detail: int = 1, raw: str = "",
                    hint: str = "/transcript") -> list[str]:
    """'exact call · Name' and the arguments exactly as the model sent them (JSON; the raw text when they
    were not valid JSON). Nothing at the compact level."""
    if detail <= 0:
        return []
    text = _decision.exact_call_text(inp, raw, max(10, width - 2))
    label = f"exact call {glyph('dot')} {_clean(name)}"
    if raw:
        label += " (not valid JSON: the raw text the model wrote)"
    return [f"{SUBTLE}{label}{RST}"] + exact_text_rows(text, width, _lim(_EXACT_CALL_ROWS, detail), hint)


def _exact_label(text: str, note: str = "") -> str:
    """'exact result sent to the model (12 lines)' (+ ' · <note>')."""
    n = len(sanitize(text or "").rstrip().splitlines())
    label = f"exact result sent to the model ({_plural(n, 'line')})"
    if note:
        label += f" {glyph('dot')} {note}"
    return f"{SUBTLE}{label}{RST}"


def _is_numbered_read(content: str) -> bool:
    return bool(re.search(r"^\s*\d+→", content or "", re.M))


def _summary_is_text(summary: str, text: str) -> bool:
    """True when the one-row summary already shows the whole (one-line) result text."""
    one = sanitize(text or "").strip()
    return bool(one) and "\n" not in one and strip_ansi(summary).strip() == one


def tool_result_rows_exact(name: str, inp: dict[str, Any], content: str, is_error: bool = False,
                           width: int | None = None, registry: Any = None, detail: int = 1, link: str = "",
                           hint: str = "/output", exact_text: str | None = None) -> list[str]:
    """Result rows that make the exact text the model gets back explicit (normal level and up).

    * a diff card (Edit, Write, ...) is kept and the exact text follows it;
    * Read's numbered, highlighted card is the exact text: it is labelled as such;
    * plan and subagent cards are kept, with a pointer to the exact text;
    * every other result (shell output, search results, JSON, errors, ...): the summary row, then the
      exact text (its first rows and a '… +N more lines · /output N' pointer).

    *exact_text* is what the model received when *content* is something else (a restored session draws
    its cards from the untruncated output). Compact (detail 0) is ``tool_result_rows`` unchanged."""
    if detail <= 0:
        return tool_result_rows(name, inp, content, is_error, width, registry, detail=detail, link=link)
    width = width or _width(160) - 8
    exact = content if exact_text is None else exact_text
    read_card = name in READ_TOOLS and _is_numbered_read(content)
    keep_card = not is_error and (name in _CARD_PLUS_EXACT or name in _CARD_IS_RESULT or read_card)
    rows = tool_result_rows(name, inp, content, is_error, width, registry, detail=detail, link=link,
                            preview=keep_card)
    if not rows:
        return rows
    if read_card and len(rows) > 1:
        return rows[:1] + [_exact_label(exact, "drawn with colours")] + rows[1:]
    if name in _CARD_IS_RESULT and keep_card:
        return rows + [f"{SUBTLE}the card shows the result sent to the model {glyph('dot')} {hint} for its exact text{RST}"]
    if _summary_is_text(rows[0], exact) and len(rows) == 1:
        return [rows[0] + f"{SUBTLE} {glyph('dot')} exact result sent to the model{RST}"]
    body = exact_text_rows(exact, width - 2, _lim(_EXACT_RESULT_ROWS, detail), hint)
    return rows + [_exact_label(exact)] + body


def tool_output_details(n: int, width: int | None = None) -> list[str]:
    """The full explainer block of tool call #n (the one '/output n' shows): who chose it and why, what it
    does, the permission outcome, the exact call exactly as the model sent it, how long it took and where
    its result went. The exact result is the output itself, which '/output' prints below this block.
    ``[]`` when the call was not kept (an older session, or its record was dropped)."""
    meta = _TOOL_CALLS.get(int(n))
    if meta is None:
        return []
    width = width or max(40, _width(160) - 8)
    name = str(meta.get("name", "tool"))
    inp = {k: v for k, v in (meta.get("input") or {}).items() if not str(k).startswith("_")}
    rows = [text for _, text in tool_call_rows(name, inp, 2, width, None, str(meta.get("why") or ""),
                                              str(meta.get("why_source") or ""), str(meta.get("chosen") or ""))]
    allowed = str(meta.get("allowed") or "")
    if allowed:
        blocked = allowed.startswith(("denied", "declined"))
        rows.append(f"{ERROR if blocked else SUCCESS}{glyph('fail') if blocked else glyph('ok')}{RST} {MUTED}{allowed}{RST}")
    rows += exact_call_rows(name, inp, width, 3)
    content = str(meta.get("content") or "")
    n_bytes = len(content.encode("utf-8", errors="replace"))
    took = meta.get("duration")
    step = int(meta.get("step") or 0)
    rows.append(f"{SUBTLE}{glyph('arrow')} " + (f"took {float(took):.2f}s {glyph('dot')} " if took is not None else "")
                + _decision.then_text(n_bytes, max(1, int(len(content) / 3.7)) if content else 0, step + 1 if step else 0)
                + f"{RST}")
    return rows


# ─── Spinner ───────────────────────────────────────────────────────────────

class _SpinnerAwareStdout:
    """stdout proxy that erases the spinner line before any other write (from any thread)."""

    def __init__(self, real: Any, spinner: "Spinner") -> None:
        self._real = real
        self._spinner = spinner

    def write(self, s: str) -> int:
        with OUTPUT_LOCK:
            erase = self._spinner._erase_str_locked()
            # the erase and the new text go out as ONE write (inside a synchronized-output frame where the
            # terminal supports it), so the screen never shows the spinner gone and the text not there yet
            n = self._real.write(_sync_frame(erase + s) if erase else s)
            if s:
                self._spinner._at_line_start = s.endswith("\n")
            return n if isinstance(n, int) else len(s)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._real, item)


_ACTIVE_SPINNERS: "set[Spinner]" = set()

_SYNC_TERMS = ("kitty", "foot", "alacritty", "ghostty", "wezterm", "contour", "mintty", "rio")
# Not "vscode" (also Cursor and other xterm.js hosts): whether the embedded xterm.js honours DECSET 2026 depends on
# the editor build, it was not shown to help there, and a half-applied frame is what a stale live row looks like.
# AXON_SYNC_OUTPUT=1 turns it on for anyone who wants it.
_SYNC_PROGRAMS = ("iTerm.app", "WezTerm", "ghostty", "WarpTerminal", "Hyper", "rio", "kitty")


def sync_output_supported() -> bool:
    """Whether to wrap frames in synchronized output (DECSET 2026) so a redraw lands atomically.

    Terminals that do not know the mode ignore it, but only the ones known to support it are asked, so an
    unknown one never risks a stray character. ``AXON_SYNC_OUTPUT=1`` forces it on, ``0`` off."""
    forced = os.environ.get("AXON_SYNC_OUTPUT", "").strip().lower()
    if forced in ("0", "off", "false", "no"):
        return False
    if forced in ("1", "on", "true", "yes"):
        return True
    term = os.environ.get("TERM", "").lower()
    return os.environ.get("TERM_PROGRAM", "") in _SYNC_PROGRAMS or any(t in term for t in _SYNC_TERMS) \
        or bool(os.environ.get("KITTY_WINDOW_ID") or os.environ.get("WT_SESSION"))


def _sync_frame(text: str) -> str:
    """*text* as one atomic frame when the terminal supports synchronized output, else unchanged."""
    return f"\033[?2026h{text}\033[?2026l" if text and sync_output_supported() else text


class pause_spinners:
    """Context manager: hide every running spinner while reading user input (approval prompts etc.)."""

    def __enter__(self) -> "pause_spinners":
        with OUTPUT_LOCK:
            for sp in list(_ACTIVE_SPINNERS):
                sp._paused += 1
                sp._erase_locked()
        return self

    def __exit__(self, *exc: Any) -> None:
        with OUTPUT_LOCK:
            for sp in list(_ACTIVE_SPINNERS):
                sp._paused = max(0, sp._paused - 1)


class Spinner:
    """Thread-safe status spinner: '⠋ running Bash: List files · 4s · esc to interrupt', and a tip under it at times.

    Only active on an interactive TTY.  While running, ``sys.stdout`` is wrapped so any write from
    any thread first erases the spinner line; the spinner is redrawn on the next tick, and only when
    the cursor sits at the start of a line, so it never corrupts other output.
    """

    # Type-ahead draft shown at the end of the spinner line (set by the in-flight listener).
    draft_provider: Callable[[], str] | None = None

    def __init__(self, status: Callable[[], str] | None = None, interval: float = 0.1) -> None:
        self._status = status
        self._interval = interval
        self._label = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_frame = ""
        self._visible = False
        self._at_line_start = True
        self._real: Any = None
        self._proxy: _SpinnerAwareStdout | None = None
        self._frame = 0
        self._paused = 0
        self._rows = 1          # rows the visible spinner occupies (spinner + optional hint row)
        self._row_widths: list[int] = [1]   # display width of each row of the drawn frame
        self._drawn_cols = 0                # window width when it was drawn
        self._phase_t0 = 0.0
        self.prefix = "  "
        self.started_at = 0.0
        self.live: Any = None   # axon.ui.live_status.LiveStatus: the "now" and tip rows (set by the Renderer)
        self.preview_provider: Callable[[], str] | None = None   # the response's unfinished line
        self.board_provider: Callable[[int, int, str], list[str] | None] | None = None   # parallel calls block (parallel_board.py)

    @staticmethod
    def enabled() -> bool:
        if os.environ.get("AXON_NO_SPINNER") or os.environ.get("TERM") == "dumb":
            return False
        if os.name == "nt" and not colors_enabled():
            return False  # no VT processing: cursor control would print garbage
        try:
            return bool(sys.stdout.isatty())
        except Exception:
            return False

    @property
    def running(self) -> bool:
        return self._thread is not None

    def set_label(self, label: str) -> None:
        if label != self._label:
            self._phase_t0 = time.time()     # live timer of the current activity (this tool, this step)
        self._label = label

    def start(self, label: str = "Thinking") -> None:
        self.set_label(label)
        if self._thread is not None or not self.enabled():
            return
        with OUTPUT_LOCK:
            self._real = sys.stdout
            self._proxy = _SpinnerAwareStdout(self._real, self)
            sys.stdout = self._proxy  # type: ignore[assignment]
            self._at_line_start = True
        self.started_at = self.started_at or time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="axon-spinner", daemon=True)
        _ACTIVE_SPINNERS.add(self)
        self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is None:
            return
        self._stop.set()
        if t is not threading.current_thread():
            t.join(timeout=1.0)
        with OUTPUT_LOCK:
            self._erase_locked()
            if sys.stdout is self._proxy:
                sys.stdout = self._real
            self._proxy = None
            self._thread = None
            _ACTIVE_SPINNERS.discard(self)

    def reset_clock(self) -> None:
        self.started_at = time.time()

    def _erase_str_locked(self) -> str:
        """The escape sequence that clears the visible spinner rows (empty when none are shown)."""
        if not (self._visible and self._real is not None):
            return ""
        # the hint row sits under the spinner row: clear it, then go back up and clear the spinner
        rows, lead = self._rows, ""
        cols = term_width()
        if self._drawn_cols and cols < self._drawn_cols:
            # the window got narrower since this frame was drawn: the terminal re-wrapped it (a long row is now
            # several), and may have moved the cursor below the pinned rows that re-wrapped too
            rows = sum(max(1, -(-w // max(1, cols))) for w in self._row_widths) or rows
            try:
                from axon.ui.footer import wrap_drift
                drift = wrap_drift(cols)
            except Exception:
                drift = 0
            if drift:
                lead = f"\033[{drift}A"
        self._drawn_cols = 0
        seq = lead + "\r\033[2K" + "\033[A\r\033[2K" * (rows - 1)
        self._visible = False
        self._rows = 1
        return seq

    def _erase_locked(self) -> None:
        seq = self._erase_str_locked()
        if seq:
            try:
                self._real.write(seq)
            except Exception:
                pass

    def nudge(self) -> None:
        """New response text arrived: redraw the live row soon (within ~30 ms) instead of at the next tick."""
        self._wake.set()

    def _live_rows(self) -> Any:
        """What the live status says now (``now`` for the spinner row, ``tip`` for the row under it); None when off."""
        live = self.live
        if live is None or not live.enabled():
            return None
        hint = f" {glyph('dot')} {Renderer.interrupt_hint}"
        # two columns spare: the rail's own prefix is printed right after a redraw, and a log that
        # keeps the erase codes out must not see a row wider than the window
        room = max(16, term_width() - 3 - display_width(self.prefix) - 2 - display_width(hint))
        return live.rows(room, show_tip=term_height() >= 14)

    def status_rows(self) -> list[str]:
        """The optional row under the spinner: a tip. The spinner row itself says what is happening."""
        if term_height() < 12:
            return []
        rows = self._live_rows()
        if rows is None or not rows.now:
            hint = self.hint_line()
            return [hint] if hint else []
        if not rows.tip:
            return []
        return [truncate(f"{self._row_lead()}{MUTED}Tip: {sanitize(rows.tip)}{RST}", max(10, term_width() - 1))]

    def _row_lead(self) -> str:
        return self.prefix + "  "

    def hint_line(self) -> str:
        """Second row under the spinner: a contextual tip, or how to talk to the agent while it works."""
        if os.environ.get("AXON_NO_TIPS") or term_height() < 12:
            return ""
        elapsed = time.time() - (self.started_at or time.time())
        tip = ""
        if Spinner.draft_provider is not None and elapsed < 6:
            tip = f"Type a message to queue it for the agent (sent between steps) {glyph('dot')} /btw asks a side question"
        else:
            try:
                from axon.features.tips import tip_engine

                tip = tip_engine().spinner_tip() or ""
            except Exception:
                tip = ""
        if not tip:
            return ""
        lead = self._row_lead()
        return truncate(f"{lead}{MUTED}Tip: {sanitize(tip)}{RST}", max(10, term_width() - 1))

    MIN_REDRAW = 0.04       # text-driven redraws are at most ~33 per second; the glyph still turns every interval

    def _run(self) -> None:
        frames = glyph("spinner")
        last_draw = 0.0
        last_glyph = time.monotonic()
        while True:
            woke = self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if woke:
                gap = self.MIN_REDRAW - (time.monotonic() - last_draw)
                if gap > 0 and self._stop.wait(gap):
                    break
                self._wake.clear()
            with OUTPUT_LOCK:
                if self._stop.is_set() or sys.stdout is not self._proxy:
                    break
                if not self._at_line_start or self._paused:
                    continue
                now = time.monotonic()
                if not woke or now - last_glyph >= self._interval:
                    self._frame = (self._frame + 1) % len(frames)
                    last_glyph = now
                text, rows = self.frame_text(frames[self._frame])
                if self._visible and text == self._last_frame:
                    continue                # nothing changed since what is on screen: write nothing
                self._last_frame = text
                try:
                    # erase the old rows and draw the new ones in a single write: no half-drawn frame is ever visible
                    self._real.write(_sync_frame(self._erase_str_locked() + "\r\033[2K" + text))
                    self._real.flush()
                    self._visible = True
                    self._rows = rows
                    self._row_widths = [display_width(strip_ansi(r)) for r in text.split("\n\033[2K")]
                    self._drawn_cols = term_width()
                    last_draw = now
                except Exception:
                    break

    def frame_text(self, frame: str = "") -> tuple[str, int]:
        """One frame of the live area and its row count: the response's unfinished line, the spinner, its status."""
        preview = ""
        if self.preview_provider is not None:
            try:
                preview = truncate(self.preview_provider() or "", max(10, term_width() - 1))
            except Exception:
                preview = ""
        board = self._board_rows()
        if board:           # the block's header row is the spinner row: one place for the status, the clock and the hint
            rows = ([preview] if preview else []) + board
        else:
            rows = ([preview] if preview else []) + [self.render_line(frame)] + self.status_rows()
        return "\n\033[2K".join(rows), len(rows)

    def _board_rows(self) -> list[str]:
        """The parallel-calls block (header, one row per call, the typed-ahead draft); [] when none is running."""
        provider = self.board_provider
        if provider is None:
            return []
        try:
            rows = provider(max(10, term_width() - 1), term_height(), f"{Renderer.interrupt_hint}") or []
            draft = sanitize(str(Spinner.draft_provider() or "")) if rows and Spinner.draft_provider is not None else ""
        except Exception:
            return []
        if draft:
            width = max(10, term_width() - 1)
            draft = draft.replace("\n", f" {glyph('enter')} ")
            room = max(8, width - 10)
            tail = draft if display_width(draft) <= room else glyph("ellipsis") + draft[-max(1, room - 1):]
            rows.append(f"    {CYAN}{glyph('chevron')}{RST} {TEXT}{safe_text(tail)}{RST}{CYAN}{glyph('quote')}{RST}")
        return rows

    def render_line(self, frame: str = "") -> str:
        elapsed = max(0.0, time.time() - (self.started_at or time.time()))
        extra = self._status() if self._status else ""
        frame = frame or glyph("spinner")[0]
        dot = f" {glyph('dot')} "
        rows = self._live_rows()
        if rows is not None and rows.now:      # it carries its own clock
            line = f"{self.prefix}{ACCENT}{frame}{RST} {TEXT}{rows.now}{RST} {SUBTLE}{glyph('dot')} {Renderer.interrupt_hint}{RST}"
        else:
            phase = max(0.0, time.time() - (self._phase_t0 or self.started_at or time.time()))
            clock = f"{phase:.1f}s" + (f" (turn {elapsed:.0f}s)" if elapsed - phase >= 1.0 else "")
            detail = clock + (f"{dot}{extra}" if extra else "") + f"{dot}{Renderer.interrupt_hint}"
            line = f"{self.prefix}{ACCENT}{frame}{RST} {TEXT}{self._label}{glyph('ellipsis')}{RST} {SUBTLE}{detail}{RST}"
        width = max(10, term_width() - 1)
        draft = ""
        provider = Spinner.draft_provider
        if provider is not None:
            try:
                draft = sanitize(str(provider() or "")).replace("\n", f" {glyph('enter')} ")
            except Exception:
                draft = ""
        if draft:
            room = width - display_width(line) - 6
            if room < 12:
                line = truncate(line, max(10, width - 30))
                room = width - display_width(line) - 6
            tail = draft if display_width(draft) <= room else glyph("ellipsis") + draft[-max(1, room - 1):]
            line += f"  {CYAN}{glyph('chevron')}{RST} {TEXT}{safe_text(tail)}{RST}{CYAN}{glyph('quote')}{RST}"
        return truncate(line, width)


# ─── Renderer ──────────────────────────────────────────────────────────────

_TIPS: list[tuple[str, str]] = [
    ("/help", "Type /help to list every command, or ? for keyboard shortcuts"),
    ("/trace", "Press alt+l or type /trace detailed to see full tool inputs and results"),
    ("/inspect", "Type /inspect to replay the full timeline of the last turn"),
    ("/model", "Type /model to switch models mid-session"),
    ("/compact", "Type /compact to summarise the conversation and free context"),
    ("/cost", "Type /cost for a token and cost breakdown of this session"),
    ("/btw", "Type /btw <question> while a turn runs to ask a side question"),
    ("/init", "Type /init to create an AGENTS.md / axon.md project guide"),
]


def _join_wrapped(lead: str, parts: list[str], sep: str, width: int) -> list[str]:
    """Join *parts* with *sep*, breaking rows only between parts (never inside '6.8k in')."""
    rows: list[str] = []
    cur = lead
    cur_has = False
    for p in parts:
        cand = cur + (sep if cur_has else "") + p
        if cur_has and display_width(cand) > width:
            rows.append(cur)
            cur, cur_has = p, True
        else:
            cur, cur_has = cand, True
    rows.append(cur)
    return [r for row in rows for r in wrap_ansi(row, width)]


def _registered_commands() -> set[str] | None:
    try:
        from axon.ui.input import ALL_SLASH_COMMANDS

        return {c[0] if isinstance(c, (tuple, list)) else str(c) for c in ALL_SLASH_COMMANDS}
    except Exception:
        return None


class Renderer:
    """Renders agent stream events as a numbered step timeline.

    Trace levels (``set_trace_level`` / ``cycle_trace_level`` / ``AXON_TRACE_LEVEL``):

    * ``compact``  - the answer, one tidy row per tool call with a short preview, failures with
      their error, and a short summary row; Ctrl+O opens the transcript with everything else
    * ``normal``   - (default) turn rail with every LLM call (tokens, cache, time, cost), reasoning
      summary, tool calls with arguments, result summaries + what is fed back, diffs, permission /
      hook / retry / compaction events
    * ``detailed`` - plus full tool input JSON, result previews, observation payload details, full
      reasoning stream and stop reasons
    * ``debug``    - plus raw request / usage fields of every event
    """

    _tip_idx: int = 0
    _tip_shown: bool = False
    interrupt_hint: str = "ctrl+c to interrupt"

    def __init__(self, show_thinking: bool = False, collapsible: bool = True, verbose: bool | None = None,
                 trace_level: str | None = None, settings: Any = None) -> None:
        self.show_thinking = show_thinking
        self.collapsible = collapsible
        level = trace_level or os.environ.get("AXON_TRACE_LEVEL") or getattr(settings, "trace_level", None)
        if verbose:
            level = "detailed"
        self.trace_level = normalize_trace_level(level or DEFAULT_TRACE_LEVEL)
        self._lock = threading.RLock()
        self.registry: Any = getattr(settings, "registry", None)
        self.spinner = Spinner(status=self._spinner_status)
        self.live = _live.LiveStatus(agent_of=lambda: getattr(self, "_agent", None), provider_of=self._provider_for)
        self.live.fallback_tip = self._generic_tip
        self.live.detail_of = lambda: self.level >= 1
        self.spinner.live = self.live
        self.spinner.preview_provider = self._stream_preview
        self.board = _ParallelBoard()
        self.spinner.board_provider = self.board.frame_rows
        _live.set_current(self.live)
        self._tail = "\n\n"
        self._md: MarkdownStream | None = None
        self._md_railed = False
        self._thinking_active = False
        self._thinking_lines: list[str] = []
        self._thinking_start_time = 0.0
        self._think_col = -1
        self._think_word = ""
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._last_header_id = ""
        self._turn_no = 0
        self._agent: Any = None
        self._t_turn: TranscriptEntry | None = None
        self._t_step: TranscriptEntry | None = None
        self._t_tools: dict[str, TranscriptEntry] = {}
        self._resp_raw: list[str] = []
        self._turn_open = False
        self._step = 0
        self._step_t0 = 0.0
        self._step_model = ""
        self._step_stats_done = True
        self._pending_stats: tuple[Any, str] | None = None
        self._pending_perm: dict[str, list[str]] = {}
        self._pending_why: dict[str, tuple[str, str]] = {}   # tool id -> (why, source) before the call starts
        self._step_tool_count = 0                  # tool definitions sent with the current LLM call
        self._step_prompt_tools = False            # ... described in the prompt (no native tool calling)
        self._step_tool_uses = 0                   # tool calls the current LLM call wrote
        self._step_call_no = 0                     # calls of the current step started so far
        self._answer_step: tuple[int, str] = (0, "")   # (step, model) that wrote the final answer
        self._batch_order: list[str] = []          # tool ids of the current batch, in call order
        self._batch_blocks: dict[str, dict[str, Any]] = {}
        self._batch_next = 0
        self._batch_parallel = False
        self._batch_tasks = 0                            # Task calls in the current batch
        self._sub_steps: dict[str, list[Any]] = {}       # normal: a subagent's steps held until it ends (parallel runs)
        self._step_said = ""                             # the text the model wrote in this step, as printed
        self._text_in_step = False
        self._turn_cost = 0.0
        self._turn_cost_known = False
        self._tool_call_count = 0
        self._tool_batch_count = 0
        self._round_obs_count = 0
        self._current_iteration = 1
        self._current_user_prompt = ""
        self._current_permission_mode = "bypass"
        self._out_chars = 0
        self._turn_text = False                    # the model wrote response text this turn
        self.last_context: tuple[int, int] = (0, 0)   # (tokens in context, window) of the last LLM call
        self._step_context: tuple[int, int] = (0, 0)
        self._pending_exact: dict[str, list[str]] = {}   # tool id -> its 'exact call' rows, not printed yet
        self._turn_steps: list[dict[str, Any]] = []      # usage of each LLM call this turn (end-of-turn table)
        self._turn_context: tuple[int, int] = (0, 0)     # context use after the turn's last LLM call
        # Phased tool cards (normal trace level): a call is drawn once, when its result is in.
        self._calls: dict[str, dict[str, Any]] = {}      # tool id -> what its card shows (see _on_tool_start)
        self._ready_cards: dict[str, tuple[list[str], str]] = {}   # finished cards waiting for their turn to print
        self._call_notes: dict[str, list[str]] = {}      # tool id -> hook messages for its card
        self._allowed_info: dict[str, str] = {}          # tool id -> permission text (may arrive before the call starts)
        self._step_results = [0, 0, 0]                   # results of this step: count, bytes, estimated tokens
        self._step_calls_due = 0                         # calls of this step that have not finished yet
        self._step_line_open = False                     # a '→ N results go to step N' line is waiting to be printed
        self._batch_total = 1                            # calls in the current batch (numbers show when > 1)
        self._batch_mode = ""                            # 'sequential' / 'parallel' / '' (one call)
        self._batch_common: str | None = None            # permission outcome the batch shares (None: not known yet)
        self._tools_header_done = False                  # the 'TOOLS' header of the current batch is printed
        self._row_blank = False                          # the last row written was an empty rail row
        self._after_header = False                       # nothing written since the step header
        self._answer_label_step = 0                      # step whose 'ANSWER' label is already printed
        self._batch_name_w = 4                           # width of the tool-name column of the current batch
        # What the model is doing right now, for the live status line (ui/live_status.py reads it):
        # {"phase": "idle" | "choosing" | "selected" | "input" | "running", "tool": name, "args": text so far,
        #  "since": time.time() when the phase began}. Set by ToolUseStart, ToolArgsDelta, the tool start and
        # the tool result; "args" is the argument text as the model streams it.
        self.tool_phase: dict[str, Any] = {"phase": "idle", "tool": "", "args": "", "since": 0.0}

    def attach(self, agent: Any) -> None:
        """Bind to an agent: its tool registry supplies result summaries; trace level from settings."""
        self.registry = getattr(agent, "registry", None) or getattr(agent, "tools", None) or self.registry
        self._agent = agent
        settings = getattr(agent, "settings", None)
        try:
            from axon.ui.theme import set_max_layout_width
            set_max_layout_width(getattr(settings, "max_width", 0))
        except Exception:
            pass
        level = getattr(settings, "trace_level", None)
        if level:
            self.set_trace_level(level)
        # The prompt reads its status line / workspace from this agent; persisted UI settings apply.
        try:
            from axon.ui import input as _ui_input
            _ui_input.set_prompt_agent(agent)
            vim = getattr(settings, "vim_mode", None)
            if str(getattr(settings, "editor_mode", "") or "").lower() == "vim":
                vim = True
            if vim is not None:
                _ui_input.set_vim_mode(bool(vim))
            from axon.ui.vim import set_insert_remaps
            set_insert_remaps(getattr(settings, "vim_insert_mode_remaps", None))
            color = getattr(settings, "prompt_color", None) or getattr(agent, "prompt_color", None)
            if color:
                _ui_input.set_prompt_color(str(color))
        except Exception:
            pass
        theme_name = getattr(settings, "theme", None)
        if isinstance(theme_name, str) and theme_name and not os.environ.get("AXON_THEME"):
            try:
                from axon.ui.theme import set_theme
                set_theme(theme_name)
            except ValueError:
                pass

    # ── trace level ───────────────────────────────────────────────────────
    @property
    def level(self) -> int:
        return TRACE_LEVELS.index(self.trace_level)

    @property
    def verbose(self) -> bool:
        return self.level >= 2

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self.trace_level = "detailed" if value else "normal"

    def set_trace_level(self, level: Any) -> str:
        self.trace_level = normalize_trace_level(level)
        return self.trace_level

    def cycle_trace_level(self) -> str:
        """Alt+L: compact → normal (the default) → detailed → debug → compact."""
        self.trace_level = TRACE_LEVELS[(self.level + 1) % len(TRACE_LEVELS)]
        return self.trace_level

    def toggle_verbose(self) -> bool:
        self.cycle_trace_level()
        return self.verbose

    # ── output with block spacing ─────────────────────────────────────────
    def _out(self, text: str) -> None:
        if text:
            self._tail = (self._tail + text)[-2:]
            self._after_header = False
            last_row = strip_ansi(text).rstrip("\n").rsplit("\n", 1)[-1]
            self._row_blank = not last_row.strip(" │|")
            _write(text)

    def _gap_text(self) -> str:
        if self._tail.endswith("\n\n"):
            return ""
        return "\n" if self._tail.endswith("\n") else "\n\n"

    def _gap(self) -> None:
        self._out(self._gap_text())

    @property
    def _railed(self) -> bool:
        return self._turn_open and self.level >= 1

    def _rail(self, extra: str = "") -> str:
        """Left prefix for timeline rows (rail when a turn is open, plain indent otherwise). At the compact
        level there is no step above a row to nest under, so rows of a turn sit one level further left."""
        if self._turn_open and self.level == 0:
            extra = extra[2:]
        return (f"{BORDER}{glyph('v')}{RST} " if self._railed else "  ") + extra

    def _step_label(self, what: str) -> str:
        """The spinner's label: 'Step 2 · thinking', or just 'Thinking' at the compact level."""
        return f"Step {self._step} {glyph('dot')} {what}" if self.level >= 1 else what[:1].upper() + what[1:]

    def _line(self, text: str, extra: str = "", atomic: bool = False, hang: str = "  ") -> None:
        """Write one timeline row, wrapped (with hanging indent *hang*) to the terminal width.

        ``atomic=True`` (stats / summary rows made of ' · '-separated facts) only wraps at the
        separators, so a unit like '6% of 200k' or '0.03s' is never split across rows."""
        if atomic:
            text = _dot_sep_re().sub(lambda m: f"{_NBSP}{m.group(1)}{glyph('dot')}{m.group(2)} ",
                                   text.replace(" ", _NBSP))
        lead = self._rail(extra)
        cont = self._rail(extra + hang)
        # continuation rows carry the wider hanging indent: wrap to the narrower width so no row
        # (first or continuation) ever exceeds the terminal width.
        avail = max(10, term_width() - 1 - max(display_width(lead), display_width(cont)))
        rows = wrap_ansi(text, avail)
        if atomic:
            rows = [r.replace(_NBSP, " ") for r in rows]
        self._out("".join((lead if i == 0 else cont) + r + "\n" for i, r in enumerate(rows)))

    def _lines(self, rows: Iterable[str], extra: str = "") -> None:
        """Write rows under the current block. A row wider than the terminal wraps (with a hanging indent
        that keeps it inside the block) instead of being cut off, and never fills the last column, so
        the terminal has nothing to re-wrap when the window is resized."""
        lead = self._rail(extra)
        cont = self._rail(extra + "  ")
        avail = max(10, term_width() - 1 - max(display_width(lead), display_width(cont)))
        out = []
        for r in rows:
            for i, piece in enumerate(wrap_ansi(r, avail) or [""]):
                out.append((lead if i == 0 else cont) + piece + "\n")
        self._out("".join(out))

    # ── banner / user message / errors ────────────────────────────────────
    def print_banner(self, version: str = "", model: str = "", effort: str = "", workspace: str = "",
                     mode: str = "", base_url: str = "") -> None:
        """Welcome banner: logo, version, model, provider, workspace, mode and tips (width-adaptive)."""
        self._out(self.banner_text(version, model, effort, workspace, mode, base_url) + "\n")

    def banner_text(self, version: str = "", model: str = "", effort: str = "", workspace: str = "",
                    mode: str = "", base_url: str = "") -> str:
        try:
            import axon

            v = version or getattr(axon, "__version__", "")
        except Exception:
            v = version
        v_str = (v if str(v).startswith("v") else f"v{v}") if v else ""
        w_path = str(workspace) if workspace else str(Path.cwd())
        home = str(Path.home())
        if w_path.startswith(home):
            w_path = "~" + w_path[len(home):]
        model = model or "(no model)"
        provider = _provider_label(model, base_url)
        dot = glyph("dot")
        width = term_width()
        box_w = max(40, min(width - 2, 84))        # the whole box, margin included; never wider than the window
        inner = box_w - 6                          # columns between "│ " and " │", less the 2-space margin
        logo = [f"{PRIMARY}{glyph('logo_top')}{RST}", f"{CYAN}{glyph('logo_mid')}{RST}", f"{ACCENT}{glyph('logo_bot')}{RST}"]
        try:   # the brand logo (equal-width coloured rows) when the brand module is there
            from axon.ui.brand import logo_rows
            rows3 = list(logo_rows())
            if len(rows3) == 3:
                logo = rows3
        except Exception:
            pass
        lw = max(display_width(x) for x in logo)
        text_w = max(8, inner - lw - 2)
        project, data_row = "", ""
        try:   # which project this is and where its data (sessions, memory, logs) is kept
            from axon.session.layout import project_info
            info = project_info(Path(workspace) if workspace else Path.cwd())
            name = str(info["name"])
            path_room = max(8, text_w - display_width(name) - 3)
            project = f"{TEXT}{name}{RST} {MUTED}{dot} {shorten_path(w_path, path_room)}{RST}"
            shown = shorten_path(str(info["data_dir_display"]), max(8, inner - len("project data: ")))
            data = hyperlink(Path(info["data_dir"]).as_uri(), shown)
            data_row = f"{MUTED}project data: {data}{RST}"
            try:     # what memory is loaded: "memory: 2 files + 5 notes"
                from axon.features.memory_hub import summary as _mem_summary
                mem = _mem_summary(Path(workspace) if workspace else Path.cwd())
                if mem and display_width(f"project data: {shown} {dot} {mem}") <= inner:
                    data_row += f" {MUTED}{dot} {mem}{RST}"
            except Exception:
                pass
        except Exception:
            project = f"{MUTED}{shorten_path(w_path, text_w)}{RST}"
        lines = [
            (logo[0], f"{BOLD}{TEXT}Axon{RST} {MUTED}{v_str}{RST}"),
            (logo[1], [f"{TEXT}{model}{RST}"] + ([f"{MUTED}{effort} effort{RST}"] if effort else []) + [f"{MUTED}{provider}{RST}"]),
            (logo[2], project),
        ]
        mode_s = f"{WARNING}{mode}{RST} {MUTED}mode{RST}" if mode else ""
        trace_s = f"{MUTED}trace {self.trace_level} (alt+l){RST}"

        def between(pieces: list[str], room: int, trail: str = "") -> list[str]:
            """'a · b · c' in rows of *room* columns, broken between the pieces and never inside one."""
            sep, out, cur = f" {MUTED}{dot}{RST} ", [], ""
            for piece in pieces:
                trial = piece if not cur else cur + sep + piece
                if cur and display_width(trial) > room:
                    out.append(cur + trail)
                    cur = piece
                else:
                    cur = trial
            out.append(cur)
            return out

        beside: list[str] = []                     # the text beside the logo; long text wraps under itself
        for _lg, text in lines:
            if isinstance(text, list):
                parts = between(text, text_w, f" {MUTED}{dot}{RST}")
                parts = [x if display_width(x) <= text_w else truncate(x, text_w) for x in parts]
            else:
                parts = wrap_ansi(text, text_w) or [""]
            beside += parts
        # the logo's three rows stay together whatever wraps beside them
        rows: list[str] = [f"{logo[i] if i < len(logo) else ' ' * lw}  {r}" for i, r in enumerate(beside)]
        if data_row:
            rows.append(data_row)
        rows.append("")
        rows.append(f"{mode_s}{f' {MUTED}{dot}{RST} ' if mode_s else ''}{trace_s}")
        hints = [["/help commands", "! shell", "@ files", "? shortcuts"],
                 ["type while Axon works to queue a message", "esc interrupts", "/btw side question"]]
        if _click_expand_on(getattr(getattr(self, "_agent", None), "settings", None)):
            sel = "⌥/shift+drag" if glyph("dot") == "·" else "alt/shift+drag"
            hints.append(["click a block to expand", f"{sel} selects text"])
        for hint in hints:
            rows += between([f"{MUTED}{h}{RST}" for h in hint], inner)
        tip = ""
        if not os.environ.get("AXON_NO_TIPS"):
            try:
                from axon.features.tips import tip_engine

                tip = sanitize(tip_engine().spinner_tip() or "")
            except Exception:
                tip = ""
        if tip:
            rows += ["", f"{INFO}{glyph('info')} Tip:{RST} {MUTED}{tip}{RST}"]
        return "\n" + closed_box(rows, box_w, color=BORDER) + "\n"

    def render_user_message(self, text: str) -> None:
        """Echo the user's message as a shaded bar (wraps long / multi-line input)."""
        width = max(20, layout_width(4))
        clean = sanitize(text.strip())
        rows: list[str] = []
        for i, para in enumerate(clean.splitlines() or [""]):
            for j, r in enumerate(wrap_ansi(para, width - 4)):
                lead = f"{glyph('chevron')} " if (i == 0 and j == 0) else "  "
                rows.append(f"  {GRAY_BG}{BOLD}{TEXT}{pad(f' {lead}{r}', width)}{RST}")
        self._out("\n".join(rows) + "\n\n")

    def print_error(self, title: str, message: str) -> None:
        """Error panel for provider / agent failures."""
        with self._lock:
            self._finish_stream()
            self.spinner.stop()
            self._gap()
            self._out(render_error_box(title, message, max_width=_width(100)) + "\n")

    # ── event dispatch ────────────────────────────────────────────────────
    def on_event(self, e: Any) -> None:
        handler = self._HANDLERS.get(type(e))
        self.live.observe(e)
        with self._lock:
            try:
                if handler is not None:
                    handler(self, e)
                else:
                    self._on_trace_event(e)
            except Exception as exc:  # rendering must never break the agent loop
                if os.environ.get("AXON_DEBUG"):
                    raise
                self._out(f"  {ERROR}[render error: {type(exc).__name__}: {exc}]{RST}\n")

    # Turn / LLM call -----------------------------------------------------
    _SHELL_CTX_RE = re.compile(r"<bash-input>.*?\(The user ran this command in the terminal; its output is shown for context\.\)\s*",
                               re.DOTALL)

    def _provider_for(self, model: str) -> str:
        """Which provider serves *model* ('AgentRouter', 'Anthropic', ...) - '' without an attached agent."""
        agent = getattr(self, "_agent", None)
        if agent is None:
            return ""
        return provider_name(getattr(agent, "settings", None), model)

    def _open_turn(self, prompt: str, model: str, mode: str) -> None:
        # `!cmd` output travels with the next prompt as context; the header shows what the user asked
        prompt = self._SHELL_CTX_RE.sub("", prompt or "").strip()
        # Number turns like the session does (/clear and /resume start over); count locally otherwise.
        traces = getattr(getattr(self, "_agent", None), "traces", None)
        self._turn_no = len(traces) if isinstance(traces, list) and traces else self._turn_no + 1
        self._t_turn = _record(TranscriptEntry("turn", self._turn_no, title=f'Turn {self._turn_no}' + (f' · "{_one_line(prompt, 80)}"' if prompt else ""),
                                               summary=f"{model}" + (f" · {mode} mode" if mode else ""), body=_plain(prompt),
                                               started_at=time.time(), status="running"))
        self._t_step = None
        self._turn_open = True
        self._turn_text = False
        self._turn_cost = 0.0
        self._turn_cost_known = False
        self._turn_steps = []
        self._turn_context = (0, 0)
        if self.level < 1:
            return
        self._gap()
        dot = glyph("dot")
        left = f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {self._turn_no}{RST}"
        if prompt:
            left += f" {MUTED}{dot}{RST} {TEXT}\"{_one_line(prompt, 60)}\"{RST}"
        prov = self._provider_for(model)
        right = f"{MUTED}{model}" + (f" via {prov}" if prov else "") + (f" {dot} {mode} mode" if mode else "") + f"{RST}"
        width = max(30, layout_width(1))
        gap = width - display_width(left) - display_width(right)
        line = left + " " * max(2, gap) + right if gap >= 2 else left
        self._out(truncate(line, width) + "\n")

    def _on_llm_call(self, e: LLMCallStart) -> None:
        self.board.end()
        self._flush_cards(force=True)
        self._flush_blocks(force=True)
        if self._sub_steps:
            self._flush_sub_steps()
        self._finish_stream()
        self._flush_step_line()
        self._set_phase("idle")
        self._close_step_stats()
        if e.user_prompt:
            self._current_user_prompt = e.user_prompt
        if e.mode:
            self._current_permission_mode = e.mode
        if e.iteration == 1 or not self._turn_open:
            self._tool_call_count = 0
            self._tool_batch_count = 0
            self._out_chars = 0
            self.spinner.reset_clock()
            self._open_turn(self._current_user_prompt, e.model, self._current_permission_mode)
        self._current_iteration = e.iteration
        self._round_obs_count = 0
        self._step_tool_count, self._step_prompt_tools = 0, False
        self._step_tool_uses, self._step_call_no = 0, 0
        self._step_results, self._step_calls_due = [0, 0, 0], 0
        if e.iteration == 1:
            self._answer_step = (0, "")
        self._step = e.iteration
        self._step_t0 = time.time()
        self._step_model = e.model
        self._step_stats_done = False
        self._text_in_step = False
        self._step_said = ""
        self._t_step = _record(TranscriptEntry("step", self._turn_no, e.iteration, title=f"Step {e.iteration} · {e.model}",
                                               summary=f"reads {_plural(e.message_count, 'message')}", started_at=time.time(),
                                               status="running"))
        if self.level >= 1:
            dot = glyph("dot")
            if e.iteration > 1:
                self._line("")          # air between steps
            size = f" (~{_fmt_tokens(e.tokens_in)} tokens)" if e.tokens_in else ""
            prov = self._provider_for(e.model)

            def step_row(with_prov: bool) -> str:
                if self.level == 1:       # normal: the turn header names the provider; no 'LLM call' / 'reads'
                    tokens = f" {dot} ~{_fmt_tokens(e.tokens_in)} tokens" if e.tokens_in else ""
                    return (f"{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {e.iteration}{RST} {MUTED}{dot}{RST} {TEXT}{e.model}{RST}"
                            f" {MUTED}{dot} {_plural(e.message_count, 'message')}{tokens}{RST}")
                return (f"{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {e.iteration}{RST}  {TEXT}LLM call{RST} {MUTED}{dot}{RST} "
                        f"{TEXT}{e.model}{RST}" + (f" {MUTED}({prov}){RST}" if with_prov and prov else "")
                        + f" {MUTED}{dot} reads {_plural(e.message_count, 'message')}{size}{RST}")
            row = step_row(True)
            if prov and display_width(self._rail() + row) > term_width() - 1:
                row = step_row(False)          # narrow window: the turn header already names the provider
            if self.level == 1 and display_width(self._rail() + row) > term_width() - 1:
                short = f" {MUTED}{dot} {_plural(e.message_count, 'message')}{RST}"      # then the token count goes,
                row = (f"{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {e.iteration}{RST} {MUTED}{dot}{RST} {TEXT}{e.model}{RST}"
                       + (short if display_width(self._rail() + f'Step {e.iteration} {dot} {e.model} {dot} {_plural(e.message_count, "message")}') <= term_width() - 1 else ""))
            self._line(row)
            self._after_header = True
            self._render_fed_back(e)
            if self.level >= 3:
                self._render_debug_fields(e, skip={"iteration", "model", "message_count", "tokens_in", "tools_ingested", "user_prompt"})
        self.spinner.prefix = self._rail() if self._railed else "  "
        self.spinner.start(self._step_label("thinking"))
        self.spinner.set_label(self._step_label("thinking"))

    def _render_fed_back(self, e: LLMCallStart) -> None:
        items = e.tools_ingested or []
        if e.iteration <= 1 or not items or self.level < 2:
            return          # normal level: the '→ N results go to step N' line under the tools says it
        total_bytes = sum(int(t.get("bytes", 0) or 0) for t in items)
        total_tok = sum(int(t.get("tokens", 0) or 0) for t in items)
        errors = sum(1 for t in items if t.get("is_error"))
        summary = (f"{glyph('arrow')} includes {_plural(len(items), 'tool result')} from step {e.iteration - 1} "
                   f"({_fmt_bytes(total_bytes)} {glyph('dot')} ~{_fmt_tokens(total_tok)} tokens)")
        if errors:
            summary += f" {glyph('dot')} {ERROR}{_plural(errors, 'error')}{RST}{MUTED}"
        self._line(f"{MUTED}{summary}{RST}", "  ")
        if self.level >= 2:
            for t in items:
                status = f"{ERROR}{glyph('fail')}{RST}" if t.get("is_error") else f"{SUCCESS}{glyph('ok')}{RST}"
                target = t.get("target", "") or (tool_target(str(t.get("name", "")), t["input"]) if t.get("input") else "")
                self._line(f"{status} {BOLD}{t.get('name', 'Tool')}{RST} {MUTED}{target}{RST} {SUBTLE}{glyph('dot')} "
                           f"{t.get('lines', 0)} lines {glyph('dot')} {int(t.get('bytes', 0) or 0):,} bytes {glyph('dot')} "
                           f"~{_fmt_tokens(int(t.get('tokens', 0) or 0))} tok"
                           + (f" {glyph('dot')} [{str(t.get('id'))[:12]}]" if t.get("id") else "") + f"{RST}", "    ")

    def _close_step_stats(self, usage: Any = None, stop: str = "", cost: Any = None, duration: float | None = None,
                          miss_cause: str = "", context: tuple[int, int] | None = None) -> None:
        """Record the usage/timing/cost of the current LLM call (once) for the end-of-turn summary.

        Only the detailed and debug levels also print it as a line in the middle of the turn; compact
        and normal show it in the table under the final answer. Prefers the data of an ``LLMCallEnd``
        trace event; falls back to the provider's ``TurnComplete`` usage (kept pending until the next
        timeline boundary).
        """
        if self._step_stats_done:
            return
        if usage is None and self._pending_stats is not None:
            usage, stop = self._pending_stats
        if usage is None:
            return  # nothing known yet; a later TurnComplete / LLMCallEnd will close the step
        self._pending_stats = None
        self._step_stats_done = True
        if self._t_step is not None:
            dur_s = duration if duration is not None else max(0.0, time.time() - self._step_t0)
            self._t_step.summary = _plain(f"replied in {dur_s:.1f}s · " + _usage_line(usage, cost=cost))
            self._t_step.duration, self._t_step.status = dur_s, "ok"
        if cost is None:
            cost = _cost_for(self._step_model, usage)
        if cost is not None:
            try:
                self._turn_cost += float(cost)
                self._turn_cost_known = True
            except (TypeError, ValueError):
                pass
        dur = duration if duration is not None else max(0.0, time.time() - self._step_t0)
        self._turn_steps.append(step_usage(self._step, self._step_model, dur, usage, cost))
        if context:
            self._turn_context = context
        if self.level < 2:
            if miss_cause and self.level == 1:     # the table has the cached share; why it missed is said here
                mark = "info" if "first request" in miss_cause else "warn"     # a cold start is expected, not a problem
                self._line(f"{MUTED}{glyph(mark)} cache miss: {miss_cause}{RST}", "  ")
            return       # compact / normal: no cost line mid-turn; the end-of-turn table has it
        show_stop = stop if (self.level >= 2 or stop not in ("end_turn", "tool_use", "")) else ""
        sep = f" {MUTED}{glyph('dot')}{RST} "
        stats = (f"{SUBTLE}{glyph('wrap')}{RST} {MUTED}replied in {dur:.1f}s {glyph('dot')} "
                 f"{_usage_line(usage, cost=cost, stop=show_stop)}{RST}")
        gauge = context_gauge(*context) if context else ""
        if gauge and display_width(stats + sep + gauge) > term_width() - 1 - display_width(self._rail("    ")):
            gauge = context_gauge(*context, compact=True)
        self._line(stats + (sep + gauge if gauge else ""), "  ", atomic=True)
        if miss_cause and self.level >= 2:
            self._line(f"{MUTED}{glyph('warn')} cache miss: {miss_cause}{RST}", "    ")
        if self.level >= 2:
            split = _cost_split(self._step_model, usage)
            if split:
                self._line(f"{SUBTLE}{split}{RST}", "    ")
        if self.level >= 3:
            self._render_debug_fields(usage, skip=set(), title="usage")

    def _render_debug_fields(self, obj: Any, skip: set[str], title: str = "") -> None:
        fields = {k: v for k, v in _event_fields(obj).items() if k not in skip}
        if not fields:
            return
        text = ", ".join(f"{k}={_one_line(v, 60)}" for k, v in fields.items())
        prefix = f"{title}: " if title else f"{type(obj).__name__}: "
        for r in wrap_ansi(f"{SUBTLE}{prefix}{text}{RST}", max(20, term_width() - 8)):
            self._line(r, "    ")

    # Thinking ------------------------------------------------------------
    def _on_thinking(self, e: ThinkingDelta) -> None:
        self._out_chars += len(e.text)
        if not self._thinking_active:
            self._finish_markdown()
            self._thinking_active = True
            self._thinking_uid = next(_UIDS)
            _tag_rows(self._thinking_uid)
            self._thinking_lines = []
            self._thinking_start_time = time.time()
            self._think_col = -1
            self._think_word = ""
        self._thinking_lines.append(e.text)
        if self._think_full and len(self._thinking_lines) == 1:
            # thinking is its own block, set apart from tool calls and the response
            if self.level == 1:       # normal: the same THINKING label as the finished card, then the thought as it arrives
                self._section(f"{PRIMARY}{BOLD}{glyph('star')} THINKING{RST}")
            else:
                self._line(f"{SUBTLE}{glyph('star')}{RST} {MUTED}{ITALIC}Thinking{RST}", "  ")
        if self._think_full:
            self._think_stream(sanitize(e.text))

    @property
    def _think_full(self) -> bool:
        """The whole thought, streamed as it arrives: at normal (always, so a long think is not a silent wait) and at the
        detailed and debug levels (when thinking display is on). Compact shows nothing."""
        return self.level == 1 or (self.show_thinking and self.level >= 2)

    def _think_stream(self, text: str) -> None:
        avail = max(20, layout_width(10))
        out: list[str] = []
        lead = self._rail("  ") + f"{SUBTLE}{glyph('thinking_bar')}{RST} {DIM}{ITALIC}"

        def start_row() -> None:
            out.append(lead)
            self._think_col = 0

        def end_row() -> None:
            out.append(f"{RST}\n")
            self._think_col = -1

        def flush_word() -> None:
            w = self._think_word
            if not w:
                return
            ww = display_width(w)
            if self._think_col < 0:
                start_row()
            elif self._think_col > 0 and self._think_col + ww > avail:
                end_row()
                start_row()
            out.append(w)
            self._think_col += ww
            self._think_word = ""

        for ch in text:
            if ch == "\n":
                flush_word()
                if self._think_col >= 0:
                    end_row()
            elif ch in " \t":
                flush_word()
                if self._think_col > 0:
                    out.append(" ")
                    self._think_col += 1
            else:
                if display_width(self._think_word) + display_width(ch) > avail:
                    flush_word()        # unspaced text (CJK, a long URL) is cut at the row end, not left to the terminal
                self._think_word += ch
        if out:
            self._out("".join(out))

    def _end_thinking(self) -> None:
        if not self._thinking_active:
            return
        self._thinking_active = False
        elapsed = max(0.1, time.time() - self._thinking_start_time)
        full_text = "".join(self._thinking_lines).strip()
        if full_text:
            _record(TranscriptEntry("thinking", self._turn_no, self._step, title=f"Thought for {elapsed:.1f}s",
                                    summary=_one_line(full_text, 120), body=full_text, duration=elapsed,
                                    uid=getattr(self, "_thinking_uid", 0) or next(_UIDS)))
        if self._think_full:
            if self._think_word:
                self._think_stream(" ")
            if self._think_col >= 0:
                self._out(f"{RST}\n")
                self._think_col = -1
            if self.level != 1:       # normal already has its THINKING label above the thought; the time is in /transcript
                self._line(f"{SUBTLE}{glyph('star')} thought for {elapsed:.1f}s{RST}", "  ")
            return
        if self.level < 1:
            return
        if self.level == 1:       # normal: '✻ THINKING  0.1s · first words of the thought…' (the rest: detailed)
            if full_text:
                head, *body = _tool_cards.thinking_rows(elapsed, full_text, self._card_width(), indent="")
                self._section(head)
                if body:
                    self._card_out(body)
            return
        full = " ".join("".join(self._thinking_lines).split())
        first = re.split(r"(?<=[.!?])\s", full, maxsplit=1)[0] if full else ""
        text = f"{SUBTLE}{glyph('star')} thought for {elapsed:.1f}s{RST}"
        if first:
            text += f" {SUBTLE}{glyph('chevron')}{RST} {DIM}{ITALIC}{first}{RST}"
        self._line(text, "  ")

    # Text ----------------------------------------------------------------
    def _on_text(self, e: TextDelta) -> None:
        self._out_chars += len(e.text)
        self._end_thinking()
        if self._md is None:
            if not e.text.strip() and not self._md_pending_text(e.text):
                return
            # On the timeline the response keeps the rail: its rows are drawn 2 columns narrower.
            self._md_railed = self._railed
            self._md = MarkdownStream(width=_width(110) - (2 if self._md_railed else 0))
            self._text_uid = next(_UIDS)
            _tag_rows(self._text_uid)
            self._text_in_step = True
            if not self._railed:
                self._gap()
            if self.level == 1 and self._answer_label_step != self._step:
                self._answer_label_step = self._step
                self._section(_tool_cards.answer_label(), gap=True)
            # On the timeline the text is a child of its step (same column as tool calls); no header row.
            self.spinner.set_label(self._step_label("responding"))
        self._resp_raw.append(e.text)
        self._step_said += e.text
        self._turn_text = True
        self._md_out(self._md.feed(e.text))
        self.spinner.nudge()            # show the unfinished line now, not at the next 100 ms tick

    def _md_out(self, chunk: str) -> None:
        """Write rendered markdown rows, behind the timeline rail when the response is on it."""
        if chunk and getattr(self, "_md_railed", False):
            chunk = "".join(self._railed_row(row) + "\n" for row in chunk.split("\n")[:-1])
        self._out(chunk)

    @staticmethod
    def _railed_row(row: str) -> str:
        # rail + an accent bar: what the model *says* is easy to tell apart from trace rows
        return f"{BORDER}{glyph('v')}{RST} {ACCENT}{glyph('quote')}{RST} {row[2:] if row.startswith('  ') else row}"

    def _stream_preview(self) -> str:
        """The response's unfinished line, styled like the rows it will become."""
        md = self._md
        row = md.preview() if md is not None else ""
        return self._railed_row(row) if row and getattr(self, "_md_railed", False) else row

    @staticmethod
    def _md_pending_text(text: str) -> bool:
        return bool(text.strip())

    def _finish_markdown(self) -> None:
        if self._md is not None:
            self._md_out(self._md.flush())
            self._md = None
            raw = "".join(self._resp_raw).strip()
            self._resp_raw = []
            if raw:
                _record(TranscriptEntry("text", self._turn_no, self._step, title="Response",
                                        summary=_one_line(raw, 120), body=raw,
                                        uid=getattr(self, "_text_uid", 0) or next(_UIDS)))
            if not self._railed:
                self._gap()

    def _finish_stream(self) -> None:
        self._end_thinking()
        self._finish_markdown()

    # Tool calls ----------------------------------------------------------
    def _on_tool_use_start(self, e: ToolUseStart) -> None:
        self._finish_stream()
        self._set_phase("selected" if e.name else "choosing", _clean(e.name))
        self.spinner.set_label(self._step_label(f"writing {_clean(e.name)} call"))
        self._live_writes().start(getattr(e, "id", ""), e.name)

    def _live_writes(self) -> Any:
        lw = getattr(self, "_lw_tracker", None)
        if lw is None:
            from axon.agent.live_write import LiveWriteTracker
            lw = self._lw_tracker = LiveWriteTracker()
        return lw

    def _live_write_label(self, e: ToolArgsDelta) -> None:
        """A file being written streams in: the spinner row says 'writing path (N lines so far)', updated in
        place (nothing is added to the scrollback; the normal diff card follows when the call is complete)."""
        from axon.agent.live_write import label
        snap = self._live_writes().feed(getattr(e, "id", ""), e.fragment)
        text = label(snap)
        if text:
            t0 = getattr(self.spinner, "_phase_t0", None)
            self.spinner.set_label(self._step_label(_clean(text)))
            if t0 is not None:
                self.spinner._phase_t0 = t0          # one running timer for the whole call, not one per update

    def _on_tool_args(self, e: ToolArgsDelta) -> None:
        self._out_chars += len(e.fragment)
        self._live_write_label(e)
        phase = self.tool_phase
        if phase.get("phase") in ("choosing", "selected", "input"):
            phase["phase"] = "input"
            phase["args"] = (str(phase.get("args", "")) + sanitize(e.fragment))[-2000:]

    def _on_tool_use_complete(self, e: ToolUseComplete) -> None:
        self._live_writes().finish(getattr(e, "id", ""))

    def _on_batch(self, e: ToolBatchStart) -> None:
        self._flush_cards(force=True)
        self._flush_blocks(force=True)
        tools = list(e.tools or ())
        self._batch_order = [i for i in (str(getattr(t, 'id', '') or '') for t in tools) if i]
        self._batch_blocks = {}
        self._batch_next = 0
        readonly = all(self._is_readonly(getattr(t, "name", "") or "") for t in tools)
        # Same rule as Agent._execute_batch: only an all-read-only batch runs concurrently, and only
        # then can results arrive out of order (so only then are tool blocks buffered).
        said = getattr(e, "parallel", None)          # the loop's own word (parallel_tools = 1 runs them one by one)
        self._batch_parallel = (readonly if said is None else bool(said)) and len(self._batch_order) > 1
        self._batch_tasks = sum(1 for t in tools if (getattr(t, "name", "") or "") == "Task")
        if self._batch_parallel and not self._batch_tasks:          # a fan-out of subagents has its own board (the monitor)
            self.board.begin([(i, _clean(str(getattr(t, "name", "") or "tool"))) for t in tools
                              if (i := str(getattr(t, "id", "") or ""))])
        else:
            self.board.end()
        self._tool_batch_count += 1
        self._finish_stream()
        self._close_step_stats()
        if e.user_prompt:
            self._current_user_prompt = e.user_prompt
        if e.mode:
            self._current_permission_mode = e.mode
        if self.level <= 1:
            names = [str(getattr(t, "name", "") or "tool") for t in tools]
            self._batch_name_w = _tool_cards.name_column_width(names, self._card_width())
            self._batch_total = int(e.total_count or len(tools))
            self._step_calls_due = max(self._step_calls_due, 0) + self._batch_total
            self._batch_mode = ("parallel" if self._batch_parallel else "sequential") if self._batch_total > 1 else ""
            self._batch_common, self._tools_header_done = None, False       # the TOOLS header waits for the first card
        elif self.level >= 2:
            if e.total_count > 1:
                how = "in parallel, read-only" if self._batch_parallel else "one after another"
            else:
                how = "read-only" if readonly else "may change files"
            self._line(f"{MUTED}{glyph('arrow')} runs {_plural(e.total_count, 'tool')} {how}{RST}", "  ")
        self.spinner.start("Running tools")

    def _is_readonly(self, name: str) -> bool:
        if name == "Task":
            return True
        reg = self.registry
        if reg is not None:
            try:
                return bool(reg.get(name).readonly)
            except Exception:
                pass
        return name not in MUTATING_TOOLS

    def _tool_header(self, call_id: str, name: str, inp: dict[str, Any], mark_last: bool = True) -> str:
        """The '● Name  target' row of a call (re-printed when another call's output came in between)."""
        if mark_last:
            self._last_header_id = call_id
        return tool_header_rows(name, inp, 1 if self.level >= 1 else 0)[0][1]

    def _print_call(self, rows: list[tuple[int, str]]) -> None:
        """Header, command block, purpose and argument rows of a call (see ``tool_call_rows``)."""
        for indent, text in rows:
            self._line(text, "  " + " " * indent)

    def _block(self, call_id: str) -> dict[str, Any]:
        """The pending output block of a parallel tool call (created on first use)."""
        b = self._batch_blocks.get(call_id)
        if b is None:
            b = {"header": "", "call": [], "notes": [], "rows": None, "printed": False}
            self._batch_blocks[call_id] = b
        return b

    def _emit_block(self, b: dict[str, Any]) -> None:
        """Print one tool call as a single, contiguous block: header, notes, result."""
        b["printed"] = True
        if b.get("uid"):
            _tag_rows(b["uid"])
        if not self._railed:
            self._gap()
        if b["call"]:
            self._print_call(b["call"])
        elif b["header"]:
            self._line(b["header"], "  ")
        for note in b["notes"]:
            for row in note.split("\n"):
                self._line(row, "      ")
        if b.get("exact"):
            self._lines(b["exact"], "      ")
        if b["rows"]:
            first, rest = b["rows"]
            if strip_ansi(first).lstrip().startswith(glyph("tl")):
                self._lines([first], "    ")
            else:
                self._line(first, "    ", atomic=True, hang="   ")
            if rest:
                self._lines(rest, "    " if b.get("card") else "       ")

    def _flush_blocks(self, force: bool = False) -> None:
        """Print finished parallel tool blocks in the order the model called them."""
        while self._batch_next < len(self._batch_order):
            b = self._batch_blocks.get(self._batch_order[self._batch_next])
            if b is None or b["rows"] is None:
                if not force:
                    return
                if b is not None and not b["printed"] and (b["header"] or b["notes"]):
                    self._emit_block(b)
            else:
                self._emit_block(b)
            self._batch_next += 1

    def _on_tool_start(self, e: ToolExecutionStart) -> None:
        self._finish_stream()
        self._close_step_stats()
        self._tool_call_count += 1
        inp = dict(e.input or {})
        self._active_tools[e.id] = {"name": e.name, "input": inp, "idx": self._tool_call_count, "t0": time.time()}
        prev = self._snapshot_before(e.name, inp)
        if prev is not None:
            self._active_tools[e.id]["prev"] = prev
        pending_why, pending_source = self._pending_why.pop(e.id, ("", ""))
        why = _get(e, "rationale", "why", default="") or pending_why
        why_source = _get(e, "rationale_source", default="") or pending_source
        self._step_call_no += 1
        chosen = self._chosen_text()
        try:
            import json as _json
            detail = _json.dumps(inp, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            detail = repr(inp)
        self._t_tools[e.id] = _record(TranscriptEntry("tool", self._turn_no, self._step,
                                                      title=f"{_clean(e.name)}  {tool_target(e.name, inp)}".rstrip(),
                                                      detail=detail, status="running", started_at=time.time(), id=e.id,
                                                      why=_plain(why), why_source=str(why_source), chosen=chosen))
        self._set_phase("running", _clean(e.name))
        self.board.start(e.id, _clean(e.name), _one_line(_tool_cards.tidy_paths(tool_target(e.name, inp)), 120))
        if self.level <= 1:
            # The whole call is drawn when the result is in (see _card_result).
            number = self._batch_order.index(e.id) + 1 if e.id in self._batch_order else self._step_call_no
            self._calls[e.id] = {"name": e.name, "input": inp, "why": _clean(why), "why_source": str(why_source),
                                 "t0": time.time(), "number": number}
            call, exact = [], []
        else:
            call = tool_call_rows(e.name, inp, self.level, _width(160) - 12, self.registry, why, str(why_source), chosen)
            exact = self._exact_call(e, inp)
        if self.level <= 1:
            pass
        elif self._batch_parallel and e.id in self._batch_order:
            b = self._block(e.id)
            b["uid"] = self._t_tools[e.id].uid
            b["header"] = self._tool_header(e.id, e.name, inp, mark_last=False)
            b["call"] = call
            b["exact"] = exact
        else:
            # printed after the permission decision ('allowed: …'), or with the result at the latest
            self._pending_exact[e.id] = exact
            if not self._railed:
                self._gap()
            self._last_header_id = e.id
            self._print_call(call)
        running = [i for i in self._active_tools.values()]
        label = (f"Running {_clean(e.name)} {_one_line(tool_target(e.name, inp), 40)}".rstrip() if len(running) == 1
                 else f"Running {len(running)} tools")
        self.spinner.start(label)
        self.spinner.set_label(label)

    def _exact_call(self, e: ToolExecutionStart, inp: dict[str, Any]) -> list[str]:
        """The 'exact call' rows of a call: its arguments exactly as the model sent them."""
        raw = ""
        if _get(e, "parse_error", default=""):
            raw = str(inp.get("raw") or "")
        return exact_call_rows(e.name, inp, _width(160) - 12, self.level, raw)

    def _print_exact_call(self, call_id: str) -> None:
        """Print a call's pending 'exact call' rows (once)."""
        rows = self._pending_exact.pop(call_id, None)
        if rows:
            self._lines(rows, "      ")

    def _chosen_text(self) -> str:
        """'chosen by <model> (<provider>) in step N · call i of n · from T tools' for the call starting now."""
        if not self._step_model:
            return ""
        return _decision.chosen_by_text(self._step_model, self._provider_for(self._step_model), self._step,
                                        self._step_call_no, self._step_tool_uses, self._step_tool_count,
                                        self._step_prompt_tools)

    def _workspace(self) -> Path:
        ws = getattr(getattr(self._agent, "settings", None), "workspace", None)
        try:
            return Path(ws) if ws else Path.cwd()
        except (TypeError, OSError):
            return Path.cwd()

    def _snapshot_before(self, name: str, inp: dict[str, Any]) -> str | None:
        """What a file / notebook cell held before Write / NotebookEdit ran (so the card can show a diff)."""
        if name not in WRITE_TOOLS and name != "NotebookEdit":
            return None
        raw = str(_first(inp, "path", "file_path", "notebook_path", "TargetFile") or "")
        if not raw:
            return None
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = self._workspace() / p
            if not p.is_file() or p.stat().st_size > 2_000_000:
                return None
            text = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        if "\x00" in text[:4096]:
            return None
        if name in WRITE_TOOLS:
            return text
        try:   # NotebookEdit: the target cell's source
            import json as _json
            cells = _json.loads(text).get("cells") or []
            cid, idx = inp.get("cell_id"), inp.get("cell_index")
            cell = next((c for c in cells if cid and c.get("id") == cid), None)
            if cell is None and isinstance(idx, int) and 0 <= idx < len(cells):
                cell = cells[idx]
            src = (cell or {}).get("source", "")
            return "".join(src) if isinstance(src, list) else str(src or "")
        except Exception:
            return None

    def _on_tool_result(self, e: ToolExecutionResult) -> None:
        global _MORE_HINT
        self._finish_stream()
        self._print_exact_call(e.id)
        info = self._active_tools.pop(e.id, None) or {"name": e.name, "input": {}, "idx": 0, "t0": None}
        self.board.finish(e.id, bool(e.is_error), len((e.content or "").encode("utf-8", "ignore")))
        inp = {**(info.get("input") or {}), **(e.input or {})}
        if "prev" in info:
            inp["_prev_content"] = info["prev"]
        if getattr(e, "changes", ()):
            inp["_changes"] = e.changes
        if e.id in self._t_tools:
            _tag_rows(self._t_tools[e.id].uid)
        out_no = _save_tool_output(e.content, _clean(e.name), tool_target(e.name, inp))
        _OUTPUT_BY_ID[str(e.id)] = out_no
        self._remember_call(out_no, e, inp, info)
        if self.level <= 1:
            self._card_result(e, info, inp, out_no)
            return
        in_block = self._batch_parallel and e.id in self._batch_order
        if not in_block:
            if self._last_header_id != e.id:
                self._line(self._tool_header(e.id, e.name, inp), "  ")
            self._last_header_id = ""
        width = _width(160) - 10         # rail + "⎿  " + the hanging indent _lines reserves; never the last column
        _MORE_HINT = f"/output {out_no}"
        link = _output_file(out_no, e.content or "") if (e.content or "").strip() else ""
        if link:
            _MORE_HINT = hyperlink(link, _MORE_HINT)              # click (cmd/ctrl+click) opens the full output
        try:
            rows = tool_result_rows_exact(e.name, inp, e.content, e.is_error, width, self.registry,
                                          detail=min(self.level, 2), link=link, hint=_MORE_HINT)
        finally:
            _MORE_HINT = "/output"
        entry = self._t_tools.pop(e.id, None) or _record(TranscriptEntry(
            "tool", self._turn_no, self._step, title=f"{_clean(e.name)}  {tool_target(e.name, inp)}".rstrip(), id=e.id))
        entry.body, entry.output_no = sanitize(e.content or ""), out_no
        entry.summary = _plain(rows[0]) if rows else ""
        entry.status = "error" if e.is_error else "ok"
        if entry.started_at:
            entry.duration = time.time() - entry.started_at
        card = _is_card_rows(e.name, rows)
        if e.is_error and rows and not card:
            rows[0] = f"{ERROR}{glyph('fail')}{RST} " + rows[0]
        self._round_obs_count += 1
        if self.level >= 1 and rows:
            t0 = info.get("t0")
            dur = f"{time.time() - t0:.2f}s" if t0 else ""
            facts = [dur] if dur else []
            sep = f" {glyph('dot')} "
            if card:
                # the subagent card is a box: fold the timing into its bottom border
                if facts:
                    rows = _append_box_footer(rows, f"took {sep.join(facts)}")
            elif facts:
                rows = [rows[0] + f"{SUBTLE}{sep}{sep.join(facts)}{RST}"] + rows[1:]
        then = self._then_text(e)
        entry.then = then
        then_row = tool_then_row(then) if self.level >= 1 else ""
        if then_row:
            rows = rows + [then_row]
        if in_block:
            b = self._block(e.id)
            if not b["header"]:
                b["header"] = self._tool_header(e.id, e.name, inp, mark_last=False)
            if card:      # the card draws its own frame: no '⎿' in front, every row at one indent
                b["rows"], b["card"] = (rows[0], rows[1:]), True
            else:
                b["rows"], b["card"] = ((f"{SUBTLE}{glyph('result')}{RST}  " + rows[0]) if rows else "", rows[1:]), False
            self._flush_blocks()
        elif card:
            self._lines(rows, "    ")
        elif rows:
            self._line(f"{SUBTLE}{glyph('result')}{RST}  " + rows[0], "    ", atomic=True, hang="   ")
            self._lines(rows[1:], "       ")         # under the text after '⎿  '; wraps stay inside
        if not self._active_tools:
            self.spinner.set_label(self._step_label("waiting for model"))

    # ── tool calls at the compact and normal levels ────────────────────────
    # A call is drawn once, when its result is in (ui/tool_cards.py): one tidy row and a preview at compact,
    # the choose / input / run / output card at normal.
    # Parallel calls finish in any order but their cards are printed in the order the model asked.
    def _section_width(self) -> int:
        return max(20, term_width() - 1 - display_width(self._rail()))

    def _section(self, text: str, gap: bool = False) -> None:
        """One section label row on the rail ('▣ TOOLS · 2 calls', '✻ THINKING …', '▍ ANSWER'). *gap*: a blank
        rail row first, unless the step header or a blank row is right above."""
        if gap and not self._row_blank and not self._after_header:
            self._line("")
        self._out(self._rail() + truncate(text, self._section_width()) + "\n")

    def _card_width(self) -> int:
        """Columns a card row may use: never wide enough for the terminal to wrap it."""
        return max(30, term_width() - 1 - display_width(self._rail("  ")))

    def _call_rows(self, call: "_tool_cards.CardCall") -> list[str]:
        draw = _tool_cards.compact_rows if self.level == 0 else _tool_cards.card_rows
        return draw(call, self._card_width())

    def _card_out(self, rows: list[str]) -> None:
        lead = self._rail("  ")
        self._out("".join(lead + r + "\n" for r in rows))

    def _set_phase(self, phase: str, tool: str = "", args: str = "") -> None:
        self.tool_phase = {"phase": phase, "tool": tool, "args": args, "since": time.time()}

    def _remember_call(self, out_no: int, e: ToolExecutionResult, inp: dict[str, Any], info: dict[str, Any]) -> None:
        """Keep what '/output <n>' needs to explain call #n in full (see ``tool_output_details``)."""
        entry = self._t_tools.get(e.id)
        t0 = info.get("t0")
        _TOOL_CALLS[out_no] = {
            "name": e.name, "input": dict(inp), "is_error": bool(e.is_error), "content": e.content or "",
            "why": (entry.why if entry else "") or "", "why_source": (entry.why_source if entry else "") or "",
            "chosen": (entry.chosen if entry else "") or "", "allowed": self._allowed_info.get(e.id, ""),
            "duration": (time.time() - t0) if t0 else None, "step": self._step,
        }
        for old in sorted(_TOOL_CALLS)[:-_TOOL_OUTPUT_KEEP]:
            del _TOOL_CALLS[old]

    def _card_call(self, call_id: str, name: str, inp: dict[str, Any], content: str, is_error: bool, state: str,
                   out_no: int, why: str, duration: float | None) -> "_tool_cards.CardCall":
        saved = self._calls.pop(call_id, {})
        batched = call_id in self._batch_order
        if batched and self._batch_common is None:
            self._batch_common = _tool_cards.common_permission([self._allowed_info.get(i, "") for i in self._batch_order])
        common = (self._batch_common or "") if batched else _decision.short_allowed(self._allowed_info.get(call_id, ""))
        self._card_common = common
        hint = f"/output {out_no}" if out_no else "/output"
        link = _output_file(out_no, content) if out_no and content.strip() else ""
        if link:
            hint = hyperlink(link, hint)            # click (cmd/ctrl+click) opens the full output
        return _tool_cards.CardCall(
            number=int(saved.get("number") or self._step_call_no or 1), name=name, inp=inp,
            why=str(saved.get("why") or why or ""), why_source=str(saved.get("why_source") or ""),
            said=self._step_said, total=self._batch_total if batched else 1, common_allowed=common,
            allowed=self._allowed_info.pop(call_id, ""),
            content=content, is_error=is_error, duration=duration, state=state,
            hint=hint, notes=self._call_notes.pop(call_id, []),
            registry=self.registry, name_w=self._batch_name_w)

    def _card_result(self, e: ToolExecutionResult, info: dict[str, Any], inp: dict[str, Any], out_no: int) -> None:
        entry = self._t_tools.pop(e.id, None) or _record(TranscriptEntry(
            "tool", self._turn_no, self._step, title=f"{_clean(e.name)}  {tool_target(e.name, inp)}".rstrip(), id=e.id))
        content = e.content or ""
        entry.body, entry.output_no = sanitize(content), out_no
        entry.status = "error" if e.is_error else "ok"
        if entry.started_at:
            entry.duration = time.time() - entry.started_at
        t0 = info.get("t0")
        call = self._card_call(e.id, e.name, inp, content, e.is_error, "done", out_no, entry.why,
                               (time.time() - t0) if t0 else None)
        preview = _tool_cards.output_rows(call, 120)
        entry.summary = _plain(preview[0]) if preview else ""
        entry.then = self._then_text(e)
        self._round_obs_count += 1
        self._queue_card(e.id, self._call_rows(call), content)
        self._set_phase("idle")
        if not self._active_tools:
            self.spinner.set_label(self._step_label("waiting for model"))

    def _queue_card(self, call_id: str, rows: list[str], content: str) -> None:
        if call_id in self._batch_order[self._batch_next:]:
            self._ready_cards[call_id] = (rows, content)
            self._flush_cards()
        else:
            self._print_card(rows, content, batched=False)

    def _flush_cards(self, force: bool = False) -> None:
        """Print finished cards in the order the model called them; *force* also closes calls that never finished."""
        while self._batch_next < len(self._batch_order):
            call_id = self._batch_order[self._batch_next]
            ready = self._ready_cards.pop(call_id, None)
            if ready is None:
                if not force:
                    return
                ready = self._stopped_card(call_id)
            if ready is not None:
                self._print_card(*ready)
            self._batch_next += 1
        if force:
            for call_id in list(self._calls):
                ready = self._stopped_card(call_id)
                if ready is not None:
                    self._print_card(*ready)

    def _stopped_card(self, call_id: str) -> tuple[list[str], str] | None:
        saved = self._calls.get(call_id)
        if saved is None:
            return None
        self._active_tools.pop(call_id, None)
        entry = self._t_tools.pop(call_id, None)
        if entry is not None:
            entry.status, entry.summary = "error", "stopped before it finished"
        call = self._card_call(call_id, str(saved.get("name", "tool")), dict(saved.get("input") or {}), "", False,
                               "stopped", 0, entry.why if entry else "", time.time() - float(saved.get("t0") or time.time()))
        return self._call_rows(call), ""

    def _print_card(self, rows: list[str], content: str, batched: bool = True) -> None:
        if self.level == 0:         # no sections and no accounting: the rows are the whole story
            self._card_out(rows)
            self._step_calls_due -= 1
            return
        if not batched:
            self._section(_tool_cards.tools_label(1, "", getattr(self, "_card_common", ""), self._section_width()), gap=True)
        elif not self._tools_header_done:
            self._tools_header_done = True
            self._section(_tool_cards.tools_label(self._batch_total, self._batch_mode, self._batch_common or "",
                                                       self._section_width()), gap=True)
        self._card_out(rows)
        self._step_results[0] += 1
        self._step_results[1] += len(content.encode("utf-8", errors="replace"))
        self._step_results[2] += max(1, int(len(content) / 3.7)) if content else 0
        self._step_calls_due -= 1
        self._step_line_open = True
        if self._step_calls_due <= 0:
            self._flush_step_line()

    def _flush_step_line(self) -> None:
        """'→ 2 results (314 B, ~84 tokens) go to step 2': one line under the last tool of a step."""
        if not self._step_line_open:
            return
        self._step_line_open = False
        count, n_bytes, est = self._step_results
        self._step_results, self._step_calls_due = [0, 0, 0], 0
        if count:
            self._card_out([_tool_cards.step_results_text(count, n_bytes, est, self._current_iteration + 1,
                                                          self._card_width())])

    def _then_text(self, e: ToolExecutionResult) -> str:
        """Where this result goes: 'result (54 B, ~14 tokens) goes to the model in step 3' (detailed: '#n [id]')."""
        content = e.content or ""
        n_bytes = len(content.encode("utf-8", errors="replace"))
        est = max(1, int(len(content) / 3.7)) if content else 0     # the estimate the next step's 'includes' line uses
        next_step = self._current_iteration + 1 if self._turn_open else 0
        if self.level >= 2:
            return _decision.then_text(n_bytes, est, next_step, self._round_obs_count, e.id or "")
        return _decision.then_text(n_bytes, est, next_step)

    def _on_turn_complete(self, e: TurnComplete) -> None:
        self.board.end()
        self._flush_cards(force=True)
        self._flush_blocks(force=True)
        self._flush_step_line()
        self._finish_stream()
        if not self._step_stats_done:
            self._pending_stats = (e.usage, e.stop_reason)
        if e.stop_reason != "tool_use":
            self.spinner.stop()
        if e.stop_reason == "max_tokens":
            self._line(f"{WARNING}{glyph('warn')} response truncated: output token limit reached{RST}", "  ")
        elif e.stop_reason == "refusal":
            from axon.features.insights import REFUSAL_NEXT_STEP, refusal_detail
            self._line(f"{WARNING}{glyph('warn')} the model declined to answer{refusal_detail(getattr(e, 'stop_details', None)) or ' · ' + REFUSAL_NEXT_STEP}{RST}", "  ")

    # Extended trace events (axon.agent.trace, duck-typed) ----------------
    def _on_trace_event(self, e: Any) -> None:
        """Trace events from ``axon.agent.trace`` (matched by class name; all optional)."""
        kind = type(e).__name__
        dot = glyph("dot")
        if kind == "LLMCallEnd":
            self._finish_stream()
            usage = _get(e, "usage", default=None)
            if not self._step_stats_done and usage is None and self._pending_stats:
                usage = self._pending_stats[0]
            used = int(_get(e, "context_used", default=0) or 0)
            window = int(_get(e, "context_window", default=0) or 0)
            if used and window:
                self.last_context = (used, window)
            self._close_step_stats(usage, str(_get(e, "stop_reason", default="") or ""), _get(e, "cost", default=None),
                                   _get(e, "duration_s", "duration", default=None),
                                   (str(_get(e, "cache_miss_cause", "cache_miss", default="") or "")
                                    + str(_get(e, "cache_miss_detail", default="") or "")),
                                   (used, window) if used and window else None)
            if str(_get(e, "outcome", default="ok")) == "error" and self.level >= 0:
                self._line(f"{ERROR}{glyph('fail')} model call failed: {_one_line(_get(e, 'error', default='unknown error'), 160)}{RST}", "  ")
            elif str(_get(e, "outcome", default="ok")) == "interrupted":
                self._line(f"{WARNING}{glyph('warn')} interrupted{RST}", "  ")
            self._step_tool_uses = int(_get(e, "tool_use_count", default=0) or 0)
            if str(_get(e, "stop_reason", default="")) != "tool_use":
                self.spinner.stop()
                if str(_get(e, "outcome", default="ok")) == "ok":
                    self._answer_step = (self._step, self._step_model)
        elif kind == "LLMRequestInfo":
            self._step_tool_count = int(_get(e, "tool_count", default=0) or 0)
            self._step_prompt_tools = bool(_get(e, "prompt_tools", default=False))
            if self.level >= 2:          # detailed / debug: what each step's request holds
                self._line(f"{SUBTLE}request: system ~{_fmt_tokens(int(_get(e, 'system_tokens', default=0) or 0))} {dot} "
                           f"{_plural(int(_get(e, 'tool_count', default=0) or 0), 'tool')} ~{_fmt_tokens(int(_get(e, 'tool_schema_tokens', default=0) or 0))} {dot} "
                           f"conversation ~{_fmt_tokens(int(_get(e, 'conversation_tokens', default=0) or 0))} tokens{RST}", "    ")
            if self.level >= 3:
                self._render_debug_fields(e, skip={"iteration", "system_tokens", "tool_schema_tokens", "conversation_tokens", "tool_count"}, title="request")
        elif kind == "PermissionDecisionEvent" or ("permission" in kind.lower() and hasattr(e, "outcome")):
            outcome = str(_get(e, "outcome", "decision", default="")).lower()
            source = str(_get(e, "source", default=""))
            reason = str(_get(e, "reason", default=""))
            approval = _get(e, "approval", default=None)
            call_id = str(_get(e, "id", default=""))
            text = tool_allowed_row(outcome, source, reason, approval)
            allowed_text = _decision.allowed_because(outcome, source, reason, approval)
            self._allowed_info[call_id] = allowed_text
            if call_id in self._t_tools:
                self._t_tools[call_id].allowed = allowed_text
            if self.level <= 1:
                return          # the card says it; the transcript and /output keep it at every level
            self._annotate_tool(call_id, str(_get(e, "tool", default="")), text)
            self._print_exact_call(call_id)
        elif kind == "HookEvent":
            blocked = bool(_get(e, "blocked", default=False))
            ctx = str(_get(e, "additional_context", default="") or "")
            msg = str(_get(e, "message", default="") or "")
            color = ERROR if blocked else MUTED
            text = (f"{color}{glyph('chevron')} hook {_get(e, 'event', default='hook')} "
                    f"{'blocked' if blocked else 'ran'} ({_plural(int(_get(e, 'ran', default=1) or 1), 'command')})"
                    + (f" {dot} injected ~{_fmt_tokens(max(1, len(ctx) // 4))} tok context" if ctx else "")
                    + (f" {dot} {_one_line(msg, 100)}" if msg else "") + RST)
            for w in _get(e, "warnings", default=()) or ():
                text += f"\n{WARNING}{glyph('warn')} {_one_line(w, 120)}{RST}"
            if self.level <= 1:       # a note on the call's card (compact shows only the ones that matter)
                self._call_notes.setdefault(str(_get(e, "tool_use_id", default="")), []).append(
                    " ".join(_plain(text).split()))
                return
            self._annotate_tool(str(_get(e, "tool_use_id", default="")), str(_get(e, "tool_name", default="")), text)
        elif kind == "RetryEvent":
            self._finish_stream()
            err = _one_line(_get(e, "error", default="") or "provider error", 120)
            _record(TranscriptEntry("note", self._turn_no, self._step, title="Retry", status="error",
                                    summary=f"{err} · retry {_get(e, 'attempt', default='?')} of {_get(e, 'max_attempts', default='?')}"))
            self._line(f"{WARNING}{glyph('warn')} {err} {dot} retrying in {float(_get(e, 'delay_s', default=0) or 0):.1f}s "
                       f"(retry {_get(e, 'attempt', default='?')} of {_get(e, 'max_attempts', default='?')}){RST}", "  ")
        elif kind == "LargeInputEvent":
            self._finish_stream()
            lines, full, sent = int(_get(e, "lines", default=0) or 0), int(_get(e, "tokens_full", default=0) or 0), int(_get(e, "tokens_sent", default=0) or 0)
            cost_full, cost_sent = float(_get(e, "cost_full", default=0.0) or 0.0), float(_get(e, "cost_sent", default=0.0) or 0.0)
            if bool(_get(e, "spilled", default=False)):
                self._line(f"{INFO}{glyph('info')} large input: {lines:,} lines, ~{_fmt_tokens(full)} tokens{RST} {MUTED}{dot} sending the start and end "
                           f"(~{_fmt_tokens(sent)}); the model reads the rest as needed"
                           + (f" {dot} ~${cost_full:.2f} {glyph('arrow')} ~${cost_sent:.2f} input" if cost_full else "") + RST, "  ")
                self._line(f"{MUTED}full text saved: {_get(e, 'path', default='')}{RST}", "  ")
            else:
                self._line(f"{INFO}{glyph('info')} large input: {lines:,} lines, ~{_fmt_tokens(full)} tokens{RST}"
                           + (f" {MUTED}{dot} ~${cost_full:.2f} per request{RST}" if cost_full else ""), "  ")
        elif kind == "CompactionEvent":
            self._finish_stream()
            if bool(_get(e, "blocked_by_hook", default=False)):
                self._line(f"{WARNING}{glyph('warn')} compaction blocked by hook{RST}", "  ")
                return
            phase = str(_get(e, "phase", default="") or "")
            if phase:      # a background compaction's one status line (started, failed, dropped, cancelled)
                from axon.agent.compaction import describe_event
                self._line(f"{MUTED}{glyph('info')} {describe_event(e)}{RST}", "  ")
                return
            if str(_get(e, "trigger", default="")) == "background":
                tb, ta = int(_get(e, "tokens_before", default=0) or 0), int(_get(e, "tokens_after", default=0) or 0)
                cost = float(_get(e, "cost", default=0.0) or 0.0)
                self._line(f"{INFO}{glyph('info')} compacted:{RST} {MUTED}{_fmt_tokens(tb)} {glyph('arrow')} {_fmt_tokens(ta)}"
                           + (f" {dot} ${cost:.4f}" if cost else "") + RST, "  ")
                return
            tb, ta = int(_get(e, "tokens_before", default=0) or 0), int(_get(e, "tokens_after", default=0) or 0)
            mb, ma = int(_get(e, "messages_before", default=0) or 0), int(_get(e, "messages_after", default=0) or 0)
            cost = float(_get(e, "cost", default=0.0) or 0.0)
            model = _get(e, "model", default="")
            self._line(f"{INFO}{glyph('info')} context compacted ({_get(e, 'trigger', default='auto')}){RST} {MUTED}"
                       f"{_fmt_tokens(tb)} {glyph('arrow')} {_fmt_tokens(ta)} tokens {dot} {mb} {glyph('arrow')} {ma} messages"
                       + (f" {dot} {model}" if model else "") + (f" {dot} ${cost:.4f}" if cost else "") + RST, "  ")
        elif kind == "SubagentEvent":
            phase = str(_get(e, "phase", default=""))
            title = _one_line(_get(e, "title", default="subagent"), 50)
            agent_type = _one_line(_get(e, "agent_type", default=""), 40)
            num = _subagent_number(_get(e, "task_id", default=""))
            label = f"#{num} {title}" if num else title
            label += f" ({agent_type})" if agent_type else ""
            if phase == "queued":
                self._line(f"{AMBER}{glyph('warn')} subagent queued{RST} {TEXT}{label}{RST} {MUTED}{dot} "
                           f"{_one_line(_get(e, 'detail', default='') or 'waiting for a free slot', 120)}{RST}", "  ")
            elif phase == "start":
                self._line(f"{CYAN}{glyph('play')} subagent started{RST} {TEXT}{label}{RST}" + (f" {MUTED}{dot} {_one_line(_get(e, 'model'), 60)}{RST}" if _get(e, "model") else ""), "  ")
            else:
                status = str(_get(e, "status", default="done"))
                color = SUCCESS if status in ("completed", "done", "ok") else (AMBER if status == "exhausted" else ERROR)
                if status in ("error", "cancelled"):
                    reason = _one_line(_get(e, "detail", default="") or "failed", 120)
                    self._line(f"{ERROR}{glyph('fail')} subagent {status}{RST} {TEXT}{label}{RST} {MUTED}{dot} {reason}{RST}", "  ")
                elif self.level == 0:
                    self._line(f"{color}{glyph('ok') if color == SUCCESS else glyph('warn')} subagent {status}{RST} {TEXT}{label}{RST} {MUTED}{dot} "
                               f"{float(_get(e, 'duration_s', default=0) or 0):.1f}s{RST}", "  ")
                else:
                    self._line(f"{color}{glyph('ok') if color == SUCCESS else glyph('warn')} subagent {status}{RST} {TEXT}{label}{RST} {MUTED}{dot} "
                               f"{_plural(int(_get(e, 'iterations', default=0) or 0), 'step')} {dot} {_fmt_tokens(int(_get(e, 'input_tokens', default=0) or 0))} in {dot} "
                               f"{_fmt_tokens(int(_get(e, 'output_tokens', default=0) or 0))} out {dot} {float(_get(e, 'duration_s', default=0) or 0):.1f}s {dot} "
                               f"${float(_get(e, 'cost', default=0) or 0):.4f}{RST}", "  ")
                self._sub_step_out(self._sub_steps.pop(str(_get(e, "task_id", default="")), []))
        elif kind == "SubagentStep":
            # Each subagent's own LLM and tool calls. Normal: a lone subagent's steps are listed as they happen;
            # with several running at once each one's steps wait for its 'subagent completed' line, so parallel
            # runs don't interleave. Detailed and debug print every step at once, tagged with its subagent.
            if self.level < 1:
                return
            if self.level == 1:
                task_id = str(_get(e, "task_id", default=""))
                if self._batch_tasks > 1 or task_id in self._sub_steps:
                    self._sub_steps.setdefault(task_id, []).append(e)
                else:
                    self._finish_stream()
                    self._sub_step_out([e])
                return
            self._finish_stream()
            is_llm = str(_get(e, "kind", default="")) == "llm"
            err = bool(_get(e, "is_error", default=False))
            mark = (f"{ERROR}{glyph('fail')}{RST}" if err else
                    (f"{ACCENT}{glyph('diamond')}{RST}" if is_llm else f"{PRIMARY}{glyph('tool')}{RST}"))
            name = _one_line(_get(e, "name", default="") or ("LLM call" if is_llm else "tool"), 60)
            label = f"{TEXT}LLM{RST} {MUTED}{name}{RST}" if is_llm else f"{BOLD}{name}{RST}"
            facts = []
            summary = _one_line(_get(e, "summary", default=""), 90).replace(" · ", f" {dot} ")   # subagent.py writes a literal dot
            if summary:
                facts.append(summary)
            if is_llm and (_get(e, "input_tokens", default=0) or _get(e, "output_tokens", default=0)):
                facts.append(f"{_fmt_tokens(int(_get(e, 'input_tokens', default=0) or 0))} in {dot} "
                             f"{_fmt_tokens(int(_get(e, 'output_tokens', default=0) or 0))} out")
            dur = _get(e, "duration_s", default=None)
            if dur:
                facts.append(f"{float(dur):.1f}s")
            if float(_get(e, "cost", default=0) or 0):
                facts.append(f"${float(_get(e, 'cost', default=0) or 0):.4f}")
            agent = _one_line(_get(e, "agent_type", default=""), 40)
            num = _subagent_number(_get(e, "task_id", default=""))
            tag = f"{CYAN}subagent #{num}{RST} {SUBTLE}({agent}){RST} " if num else ""
            self._line(f"{SUBTLE}{glyph('thinking_bar')}{RST} {tag}{mark} {label}" + (f" {MUTED}{dot} {f' {dot} '.join(facts)}{RST}" if facts else ""), "    ")
        elif kind == "SteerEvent":
            self._finish_stream()
            from axon.agent.loop import STEER_STATE_TEXT
            texts = list(_get(e, "texts", default=()) or ())
            mode = str(_get(e, "mode", default="next_step") or "next_step")
            if _get(e, "shown", default=False):      # already printed where it was typed: only say it was read
                self._line(f"{MUTED}{STEER_STATE_TEXT['next_step']}{RST}", "  ")
            else:
                for t in texts[:3]:
                    self._line(f"{CYAN}{glyph('chevron')} you:{RST} {_one_line(t, 120)}", "  ")
                self._line(f"{MUTED}{STEER_STATE_TEXT.get(mode, STEER_STATE_TEXT['next_step'])}{RST}", "    ")
        elif kind == "ContextEditEvent":      # the API cleared old tool results from what the model saw (history is unchanged)
            self._finish_stream()
            self._line(f"{MUTED}{glyph('info')} {_get(e, 'message', default='')}{RST}", "  ")
        elif kind == "GoalCheckEvent":
            from axon.ui import goal_block as _goal_block     # one GOAL block per check; its cost is the 'goal check' usage row
            self._finish_stream()
            if _get(e, "phase", default="start") == "start":
                if self.level >= 1:
                    self._line(f"{MUTED}checking the goal: {_one_line(_get(e, 'goal', default=''), 100)}{RST}", "  ")
            else:
                self._lines(_goal_block.goal_block_rows(e, self._summary_width()), "  ")
                row = _goal_block.goal_usage_row(e)
                if row is not None:
                    self._turn_steps.append(row)
                    if row.get("cost") is not None:
                        self._turn_cost += float(row["cost"])
                        self._turn_cost_known = True
        elif kind in ("AdvisorEvent", "AutoPermissionEvent"):
            # Nothing is printed here: the tool card already shows the advice or the 'auto: allowed — …' row.
            # Only the cost lands in the end-of-turn table, as its own row ('advisor' / 'permission check').
            from axon.agent import sidecall as _side
            row = _side.usage_row("advisor" if kind == "AdvisorEvent" else "permission check", e if hasattr(e, "usage") else None)
            if row is not None:
                _side.merge_row(self._turn_steps, row)
                if row.get("cost") is not None:
                    self._turn_cost += float(row["cost"])
                    self._turn_cost_known = True
        elif kind == "ThinkingBoost":
            self._line(f"{ACCENT}{glyph('star')} \"{_get(e, 'keyword', default='think')}\" {dot} effort "
                       f"{_get(e, 'previous_effort', default='?')} {glyph('arrow')} {_get(e, 'effort', default='?')}"
                       f"{' + thinking' if _get(e, 'thinking', default=False) else ''}{RST}", "  ")
        elif kind == "BudgetEvent":
            self._finish_stream()
            action = str(_get(e, "action", default="warn"))
            color = ERROR if action == "stop" else (MUTED if action == "notice" else WARNING)      # a notice (50, 80, 100 percent) is quiet
            msg = _get(e, "message", default="") or (
                f"{_get(e, 'scope', default='turn')} budget {action}: spent ${float(_get(e, 'spent_usd', default=0) or 0):.4f}"
                f" of ${float(_get(e, 'limit_usd', default=0) or 0):.2f}"
                + (f" {dot} switching to {_get(e, 'new_model')}" if _get(e, "new_model", default="") else ""))
            self._line(f"{color}{glyph('info' if action == 'notice' else 'warn')} {_one_line(msg, 160)}{RST}", "  ")
        elif kind == "MemoryLoadedEvent":
            if _get(e, "reason", default="") == "saved":
                self._line(f"{SUBTLE}{glyph('info')} memory saved:{RST} {MUTED}{_one_line(str(_get(e, 'message', default='')), 140)}{RST}", "  ")
            elif self.level >= 1:
                tok = int(_get(e, "tokens", default=0) or 0)
                self._line(f"{SUBTLE}{glyph('info')} memory loaded{RST} {MUTED}{_short_path(str(_get(e, 'path', default='')))}"
                           f" {dot} {_get(e, 'reason', default='')}" + (f" {dot} ~{_fmt_tokens(tok)} tok" if tok else "") + RST, "  ")
        elif kind == "ToolRationale":
            call_id = str(_get(e, "id", default=""))
            text = _one_line(_get(e, "text", default=""), _decision.RATIONALE_MAX_CHARS)
            source = str(_get(e, "source", default=""))
            if call_id in self._active_tools:
                row = tool_why_row(text, source)
                if row and self.level >= 2:
                    self._line(row, "      ")
                elif row and call_id in self._calls and not self._calls[call_id].get("why"):
                    self._calls[call_id]["why"] = _clean(text)
                    self._calls[call_id]["why_source"] = source
                if call_id in self._t_tools and not self._t_tools[call_id].why:
                    self._t_tools[call_id].why = _plain(text)
                    self._t_tools[call_id].why_source = source
            elif text:
                self._pending_why[call_id] = (text, source)
        elif kind == "ToolTiming":
            if self.level >= 3:
                self._render_debug_fields(e, skip=set(), title="timing")
        elif self.level >= 3 and _event_fields(e):
            self._render_debug_fields(e, skip=set())

    def _sub_step_out(self, steps: list[Any]) -> None:
        """A subagent's own steps, one row each, under its 'subagent started / completed' line."""
        width = max(20, term_width() - 1 - display_width(self._rail("    ")))
        for ev in steps:
            self._line(truncate(_subagent_step_row(ev), width), "    ")

    def _flush_sub_steps(self) -> None:
        """Held steps of subagents that have not ended (stopped, or still running in the background)."""
        held, self._sub_steps = self._sub_steps, {}
        for task_id, steps in held.items():
            num = _subagent_number(task_id)
            self._line(f"{CYAN}subagent {'#' + num if num else task_id}{RST} {MUTED}{glyph('dot')} {_plural(len(steps), 'step')} so far{RST}", "  ")
            self._sub_step_out(steps)

    def _annotate_tool(self, call_id: str, tool: str, text: str) -> None:
        """Print a note on a tool row (re-labelled when another tool's output came in between)."""
        if call_id and self._batch_parallel and call_id in self._batch_order:
            b = self._block(call_id)
            if not b["printed"]:
                b["notes"].append(text)
                return
        if call_id and self._last_header_id != call_id and tool:
            text = f"{MUTED}[{tool}]{RST} " + text
        for row in text.split("\n"):
            self._line(row, "      ")

    _HANDLERS: dict[type, Callable[["Renderer", Any], None]] = {
        LLMCallStart: _on_llm_call,
        ThinkingDelta: _on_thinking,
        TextDelta: _on_text,
        ToolUseStart: _on_tool_use_start,
        ToolArgsDelta: _on_tool_args,
        ToolUseComplete: _on_tool_use_complete,
        ToolBatchStart: _on_batch,
        ToolExecutionStart: _on_tool_start,
        ToolExecutionResult: _on_tool_result,
        TurnComplete: _on_turn_complete,
    }

    @staticmethod
    def _generic_tip() -> str:
        """Last-resort tip for the live rows: the tips engine's current pool tip ('' when tips are off)."""
        try:
            from axon.features.tips import tip_engine
            return tip_engine().spinner_tip() or ""
        except Exception:
            return ""

    def _spinner_status(self) -> str:
        tokens = self._out_chars // 4
        return f"{_fmt_tokens(tokens)} tok {glyph('down')}" if tokens and self.level >= 1 else ""

    # ── footer ────────────────────────────────────────────────────────────
    _STOP_LABELS = {"error": "Failed", "interrupted": "Interrupted", "blocked": "Blocked", "declined": "Declined",
                    "budget_exceeded": "Stopped (budget)", "token_budget": "Stopped (token budget)",
                    "max_iterations": "Stopped (iteration limit)"}

    def turn_footer(self, tool_count: int, usage: Any, cost: float, elapsed: float, llm_calls: int = 1,
                    stop_reason: str = "", error: str = "") -> None:
        """Close the turn: '└ ✓ Done in 4.8s · 3 LLM calls · 3 tool calls · 18k in · 1k out · $0.0094'.

        A failed stop (``stop_reason`` error/interrupted/blocked/budget_exceeded/token_budget/
        max_iterations) shows '✗ <reason>' instead of '✓ Done', followed by ``error``."""
        failed = stop_reason in self._STOP_LABELS
        with self._lock:
            self._finish_stream()
            self._close_step_stats()
            self._note_unfinished_step()
            self._pending_exact.clear()       # calls that never got a result (the turn was stopped)
            self.spinner.stop()
            self.spinner.started_at = 0.0
            self.live.end_turn()
            if not self._tail.endswith("\n"):
                self._out("\n")          # the summary never starts on a half-written line
            railed = self._railed
            answer_step, answer_model = self._answer_step
            if railed and not failed and llm_calls > 1 and answer_step:
                # which call wrote the answer above (the earlier ones chose tools)
                self._line(f"{MUTED}{glyph('arrow')} {_decision.final_answer_text(answer_step, answer_model)}{RST}", "  ")
            if railed:
                self._lines(turn_summary_rows(self._turn_steps, self._summary_width(), stopped=failed), "  ")
            self._answer_step = (0, "")
            in_t = int(getattr(usage, "input", 0) or 0)
            out_t = int(getattr(usage, "output", 0) or 0)
            dot = f" {SUBTLE}{glyph('dot')}{RST} "
            if self._turn_cost_known and abs(self._turn_cost - float(cost or 0)) > 1e-9:
                money = f"{WARNING}${self._turn_cost:.4f}{RST} {MUTED}(session ${float(cost or 0):.2f}){RST}"
            else:
                money = f"{WARNING}${float(cost or 0):.4f}{RST}"
            parts = [
                f"{MUTED}{_plural(llm_calls, 'LLM call')}{RST}",
                f"{MUTED}{_plural(tool_count, 'tool call')}{RST}",
                f"{MUTED}{_fmt_tokens(in_t)} in{RST}",
                f"{MUTED}{_fmt_tokens(out_t)} out{RST}",
                money,
            ]
            if railed and self._turn_context[0] and self._turn_context[1]:
                parts.append(context_gauge(*self._turn_context))      # the context gauge, once per turn
            self._turn_steps = []
            shown = parts if self.level >= 1 else self._compact_footer_parts(tool_count, cost)
            lead = f"{BORDER}{glyph('sq_bl')}{RST} " if railed else "  "
            sep = f" {SUBTLE}{glyph('dot')}{RST} " if shown else ""
            if failed:
                label = self._STOP_LABELS.get(stop_reason, "Failed")
                lead += f"{ERROR}{glyph('fail')} {label}{RST} {MUTED}after {elapsed:.1f}s{RST}{sep}"
            else:
                lead += f"{SUCCESS}{glyph('ok')} Done{RST} {MUTED}in {elapsed:.1f}s{RST}{sep}"
            rows = _join_wrapped(lead, shown, dot, max(20, layout_width(5))) if shown else [lead]
            _tag_rows(self._t_turn.uid if self._t_turn is not None else None)
            if self._t_turn is not None:
                self._t_turn.status = "error" if failed else "ok"
                self._t_turn.duration = elapsed
                self._t_turn.summary = _plain(f"{self._STOP_LABELS.get(stop_reason, 'Failed') if failed else 'Done'} in {elapsed:.1f}s · "
                                              + " · ".join(_plain(x) for x in parts) + (f" · {error}" if failed and error else ""))
                self._t_turn = None
            text = ("" if railed else self._gap_text()) + "\n    ".join(rows) + "\n"
            detail = str(error or "").strip()
            if failed and detail:
                err_rows = wrap_ansi(" ".join(detail.split()), max(20, term_width() - 6))
                if len(err_rows) > 6:
                    err_rows = err_rows[:6] + ["…"]
                text += "".join(f"    {ERROR}{r}{RST}\n" for r in err_rows)
            if failed and stop_reason in ("error", "interrupted"):
                if tool_count > 0 or self._turn_text:
                    text += f"    {MUTED}Work done so far is kept {glyph('dot')} type {BOLD}continue{RST}{MUTED} to resume{RST}\n"
                else:
                    text += f"    {MUTED}Nothing was done yet {glyph('dot')} {BOLD}/retry{RST}{MUTED} sends your message again{RST}\n"
            if stop_reason == "declined":
                text += f"    {MUTED}Stopped at your answer {glyph('dot')} tell Axon what to do instead{RST}\n"
            tip = self._footer_tip()
            if tip:
                tip_rows = wrap_ansi(f"{INFO}{glyph('info')} Tip:{RST} {MUTED}{tip}{RST}", max(20, layout_width(4)))
                text += "\n" + "".join(f"  {r}\n" for r in tip_rows)
            Renderer._tip_shown = True
            self._turn_open = False
            self._out(text + "\n")
            _tag_rows(None)

    def _compact_footer_parts(self, tool_count: int, cost: Any) -> list[str]:
        """The compact summary after 'Done in 4.4s': how many tools, what it cost, where the detail is."""
        parts = [f"{MUTED}{_plural(tool_count, 'tool')}{RST}"] if tool_count else []
        spent = self._turn_cost if self._turn_cost_known else float(cost or 0)     # the status line has the session's
        if spent:
            parts.append(f"{MUTED}${spent:.4f}{RST}")
        try:
            if tool_count and sys.stdout.isatty():
                parts.append(f"{SUBTLE}ctrl+o details{RST}")
        except Exception:
            pass
        return parts

    def _note_unfinished_step(self) -> None:
        """A step that never reported its usage (interrupted or failed mid-call) still gets a table row."""
        if self._step_stats_done or not self._step:
            return
        self._step_stats_done = True
        if any(s.get("step") == self._step for s in self._turn_steps):
            return
        duration = max(0.0, time.time() - self._step_t0) if self._step_t0 else None
        row = step_usage(self._step, self._step_model, duration, None, None)
        row["stopped"] = True
        self._turn_steps.append(row)

    def _summary_width(self) -> int:
        """Columns the end-of-turn table may use under the rail."""
        return max(20, term_width() - 1 - display_width(self._rail("    ")))

    def _footer_tip(self) -> str:
        """Contextual tip after a turn (LLM-generated by axon.features.tips; registry-derived fallback)."""
        if not getattr(self, "show_tips", True):
            self.pending_tip = ""
            return ""
        pending = getattr(self, "pending_tip", "")
        if pending:                         # a next step for this turn beats a generic usage tip
            self.pending_tip = ""
            return sanitize(pending)
        if os.environ.get("AXON_NO_TIPS"):
            return ""
        try:
            from axon.features.tips import tip_engine

            return sanitize(tip_engine().footer_tip() or "")
        except Exception:
            return "" if Renderer._tip_shown else self._next_tip()

    @staticmethod
    def _next_tip() -> str:
        registered = _registered_commands()
        tips = [t for cmd, t in _TIPS if registered is None or cmd in registered]
        if not tips:
            return ""
        tip = tips[Renderer._tip_idx % len(tips)]
        Renderer._tip_idx += 1
        return tip

    # ── stored trace replay (/inspect) ────────────────────────────────────
    def render_trace(self, turn_trace: Any, level: str = "detailed") -> None:
        """Re-print a stored turn trace as a static timeline (used by /inspect)."""
        self._out(format_trace(turn_trace, level) + "\n")


def _stored_why(d: dict[str, Any]) -> tuple[str, str]:
    """(text, source) of a stored tool step's rationale: ``{"text", "source"}`` or an older plain string."""
    why = d.get("why") or d.get("rationale") or ""
    if isinstance(why, dict):
        return str(why.get("text") or ""), str(why.get("source") or "")
    return str(why), ""


def format_trace(turn_trace: Any, level: str = "detailed") -> str:
    """Render a stored ``axon.agent.trace.TurnTrace`` (object or its ``to_dict()``) as a timeline."""
    if turn_trace is None:
        return f"  {MUTED}No trace recorded yet.{RST}"
    lvl = TRACE_LEVELS.index(normalize_trace_level(level))
    v = f"{BORDER}{glyph('v')}{RST} "
    dot = glyph("dot")
    width = max(30, term_width() - 1)
    out: list[str] = []
    prompt = _get(turn_trace, "prompt", default="")
    model = _get(turn_trace, "model", default="")
    mode = _get(turn_trace, "mode", default="")
    head = f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {_get(turn_trace, 'turn', 'index', default='?')}{RST}"
    if prompt:
        head += f" {MUTED}{dot}{RST} {TEXT}\"{_one_line(prompt, 60)}\"{RST}"
    head += f"  {MUTED}{model}" + (f" {dot} {mode} mode" if mode else "") + RST
    out.append(head)
    llm: dict[str, Any] = {}          # the LLM call whose tool calls follow (for 'chosen by' / 'then')
    answer_step = (0, "")
    step_rows: list[dict[str, Any]] = []      # usage of each LLM call, for the end-of-turn table
    context = (0, 0)
    all_steps = list(_get(turn_trace, "steps", default=[]) or [])
    for pos, step in enumerate(all_steps):
        kind = str(_get(step, "kind", default=""))
        d = _get(step, "data", default=None) or {}
        if kind == "llm":
            req = d.get("request") or {}
            llm = {"step": int(d.get("iteration") or 0), "model": str(d.get("model") or model),
                   "provider": str(req.get("provider") or ""), "tool_count": int(req.get("tool_count") or 0),
                   "prompt_tools": bool(req.get("prompt_tools")), "uses": int(d.get("tool_use_count") or 0), "call_no": 0}
            if d.get("stop_reason") and d.get("stop_reason") != "tool_use" and d.get("outcome", "ok") == "ok":
                answer_step = (llm["step"], llm["model"])
            if lvl <= 1:        # normal: no 'LLM call' / 'reads' (the turn header names the provider)
                out.append(f"{v}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {d.get('iteration', '?')}{RST} {MUTED}{dot}{RST} {TEXT}{d.get('model', model)}{RST}"
                           + (f" {MUTED}{dot} {_plural(int(d.get('message_count') or 0), 'message')}{RST}" if d.get("message_count") else ""))
            else:
                out.append(f"{v}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {d.get('iteration', '?')}{RST}  {TEXT}LLM call{RST} {MUTED}{dot}{RST} {TEXT}{d.get('model', model)}{RST}"
                           + (f" {MUTED}{dot} reads {_plural(int(d.get('message_count') or 0), 'message')}{RST}" if d.get("message_count") else ""))
            fed = (d.get("fed_back") or []) if lvl >= 2 else []
            if fed:
                tok = sum(int(f.get("tokens", 0) or 0) for f in fed if isinstance(f, dict))
                out.append(f"{v}  {MUTED}{glyph('arrow')} includes {_plural(len(fed), 'tool result')} (~{_fmt_tokens(tok)} tokens){RST}")
            req = d.get("request") or {}
            if req and lvl >= 2:
                out.append(f"{v}    {SUBTLE}request: system ~{_fmt_tokens(int(req.get('system_tokens', 0) or 0))} {dot} "
                           f"{_plural(int(req.get('tool_count', 0) or 0), 'tool')} ~{_fmt_tokens(int(req.get('tool_schema_tokens', 0) or 0))} {dot} "
                           f"conversation ~{_fmt_tokens(int(req.get('conversation_tokens', 0) or 0))}{RST}")
            thinking = str(d.get("thinking") or "")
            if thinking:
                rows = wrap_ansi(" ".join(thinking.split()), width - 8) if lvl >= 2 else [_one_line(thinking, width - 14)]
                out += [f"{v}    {DIM}{ITALIC}{r}{RST}" for r in rows]
            if d.get("usage") is not None:
                step_rows.append(step_usage(d.get("iteration") or 0, d.get("model") or model, d.get("duration_s"),
                                            d["usage"], d.get("cost")))
                if d.get("context_used") and d.get("context_window"):
                    context = (int(d["context_used"]), int(d["context_window"]))
            if d.get("usage") is not None and lvl >= 2:      # detailed / debug: each call's figures in place
                gauge = context_gauge(int(d.get("context_used") or 0), int(d.get("context_window") or 0))
                out.append(f"{v}  {SUBTLE}{glyph('wrap')}{RST} {MUTED}" + (f"replied in {float(d['duration_s']):.1f}s {dot} " if d.get("duration_s") is not None else "") + f"{_usage_line(d['usage'], cost=d.get('cost'), stop=str(d.get('stop_reason', '')) if lvl >= 2 else '')}{RST}"
                           + (f" {MUTED}{dot}{RST} {gauge}" if gauge else ""))
                if d.get("cache_miss_cause") and lvl >= 2:
                    out.append(f"{v}    {MUTED}{glyph('warn')} cache miss: {_one_line(d['cache_miss_cause'], 100)}{_one_line(d.get('cache_miss_detail', ''), 60)}{RST}")
            if d.get("error"):
                out.append(f"{v}  {ERROR}{glyph('fail')} {_one_line(d['error'], 160)}{RST}")
        elif kind == "tool":
            name = _one_line(d.get("name", "Tool"), 60)
            inp = d.get("input") or {}
            inp = inp if isinstance(inp, dict) else {}
            dur = d.get("duration_s")
            why, why_source = _stored_why(d)
            if lvl <= 1:           # normal: the phased card, like the live view
                perm = d.get("permission") if isinstance(d.get("permission"), dict) else {}
                allowed = _decision.allowed_because(str(perm.get("outcome", "")), str(perm.get("source", "")),
                                                    str(perm.get("reason", "")), perm.get("approval")) if perm else ""
                llm["call_no"] = llm.get("call_no", 0) + 1
                if llm["call_no"] == 1 and not llm.get("uses"):      # the trace did not count them: the run of tool steps
                    run = 0
                    for nxt in all_steps[pos:]:
                        if str(_get(nxt, "kind", default="")) != "tool":
                            break
                        run += 1
                    llm["uses"] = run
                total = int(llm.get("uses") or 1)
                if llm["call_no"] == 1:
                    run_perms = []
                    for nxt in all_steps[pos:pos + total]:
                        perm_d = (_get(nxt, "data", default=None) or {}).get("permission")
                        if str(_get(nxt, "kind", default="")) == "tool" and isinstance(perm_d, dict):
                            run_perms.append(_decision.allowed_because(str(perm_d.get("outcome", "")), str(perm_d.get("source", "")),
                                                                       str(perm_d.get("reason", "")), perm_d.get("approval")))
                    llm["common"] = _tool_cards.common_permission(run_perms)
                    out.append(f"{v}{_tool_cards.tools_label(total, '', llm['common'], width - 4)}")
                call = _tool_cards.CardCall(
                    number=llm["call_no"], name=name, inp=inp, why=why, why_source=why_source, total=total, allowed=allowed, common_allowed=llm.get("common", ""),
                    content=str(d.get("result_preview", "") or ""), is_error=bool(d.get("is_error")),
                    duration=float(dur) if dur is not None else None, hint="/transcript",
                    state="done" if "result_preview" in d else "stopped")
                out += [f"{v}  {r}" for r in _tool_cards.card_rows(call, width - 6)]
                llm["bytes"] = llm.get("bytes", 0) + int(d.get("bytes") or 0)
                llm["tokens"] = llm.get("tokens", 0) + int(d.get("est_tokens") or 0)
                if llm["call_no"] >= total:
                    out.append(f"{v}  " + _tool_cards.step_results_text(total, llm["bytes"], llm["tokens"], llm.get("step", 0) + 1, width - 6))
                continue
            chosen = ""
            if llm:
                llm["call_no"] += 1
                chosen = _decision.chosen_by_text(llm["model"], llm["provider"], llm["step"], llm["call_no"],
                                                  llm["uses"], llm["tool_count"], llm["prompt_tools"])
            call = tool_call_rows(name, inp, max(1, lvl), width - 10, why=why, why_source=why_source, chosen=chosen)
            for i, (indent, row) in enumerate(call):
                if i == 0 and dur is not None:
                    row += f" {SUBTLE}{dot} {float(dur):.2f}s{RST}"
                out += [f"{v}  {' ' * indent}{r}" for r in _hang(row, width - 4 - indent, "", "  ")]
            perm = d.get("permission")
            if isinstance(perm, dict):
                out.append(f"{v}      " + tool_allowed_row(str(perm.get("outcome", "")), str(perm.get("source", "")),
                                                          str(perm.get("reason", "")), perm.get("approval")))
            for hk in d.get("hooks") or []:
                if isinstance(hk, dict):
                    out.append(f"{v}      {MUTED}hook {hk.get('event', '')}: {'blocked' if hk.get('blocked') else 'ran'}" + (f" {dot} {_one_line(hk.get('message'), 80)}" if hk.get("message") else "") + RST)
            content = str(d.get("result_preview", "") or "")
            is_err = bool(d.get("is_error"))
            rows = tool_result_rows(name, inp, content, is_err, width - 12, detail=min(lvl, 2))
            if d.get("bytes") is not None:
                next_step = llm["step"] + 1 if llm.get("step") else 0
                rows = rows + [tool_then_row(_decision.then_text(int(d.get("bytes") or 0), int(d.get("est_tokens") or 0), next_step))]
            if _is_card_rows(name, rows):
                out += [f"{v}    " + r for r in rows]
            elif rows:
                mark = f"{ERROR}{glyph('fail')}{RST} " if is_err else ""
                out += [f"{v}    {SUBTLE}{glyph('result')}{RST}  {r}" if i == 0 else f"{v}       {r}"
                        for i, r in enumerate(_hang(mark + rows[0], width - 9, "", ""))]
                out += [f"{v}       {r}" for r in rows[1:]]
        elif kind == "retry":
            out.append(f"{v}  {WARNING}{glyph('warn')} {_one_line(d.get('error', '') or 'provider error', 100)} {dot} retried in {float(d.get('delay_s') or 0):.1f}s (retry {d.get('attempt')} of {d.get('max_attempts')}){RST}")
        elif kind == "compaction":
            out.append(f"{v}  {INFO}{glyph('info')} context compacted{RST} {MUTED}{_fmt_tokens(int(d.get('tokens_before') or 0))} {glyph('arrow')} "
                       f"{_fmt_tokens(int(d.get('tokens_after') or 0))} tokens {dot} ${float(d.get('cost') or 0):.4f}{RST}")
        elif kind == "subagent":
            label = _one_line(d.get("title", "") or d.get("agent_type", "") or "subagent", 50)
            facts = [str(d.get("status", "") or "")]
            if d.get("iterations"):
                facts.append(_plural(int(d.get("iterations") or 0), "step"))
            if d.get("duration_s"):
                facts.append(f"{float(d.get('duration_s') or 0):.1f}s")
            facts.append(f"${float(d.get('cost') or 0):.4f}")
            out.append(f"{v}  {CYAN}{glyph('play')} subagent{RST} {TEXT}{label}{RST} {MUTED}"
                       + f" {dot} ".join(f for f in facts if f) + RST)
            for sub in d.get("steps") or []:
                if not isinstance(sub, dict):
                    continue
                is_llm = sub.get("kind") == "llm"
                mark = (f"{ERROR}{glyph('fail')}{RST}" if sub.get("is_error") else
                        (f"{ACCENT}{glyph('diamond')}{RST}" if is_llm else f"{PRIMARY}{glyph('tool')}{RST}"))
                bits = [_one_line(sub.get("summary", ""), 80)] if sub.get("summary") else []
                if is_llm and (sub.get("input_tokens") or sub.get("output_tokens")):
                    bits.append(f"{_fmt_tokens(int(sub.get('input_tokens') or 0))} in {dot} {_fmt_tokens(int(sub.get('output_tokens') or 0))} out")
                if sub.get("duration_s"):
                    bits.append(f"{float(sub['duration_s']):.1f}s")
                name = _one_line(sub.get("name", "") or ("LLM call" if is_llm else "tool"), 60)
                head_s = f"{TEXT}LLM{RST} {MUTED}{name}{RST}" if is_llm else f"{BOLD}{name}{RST}"
                out.append(f"{v}    {SUBTLE}{glyph('thinking_bar')}{RST} {mark} {head_s}" + (f" {MUTED}{dot} {f' {dot} '.join(bits)}{RST}" if bits else ""))
        elif kind == "steer":
            texts = d.get("texts") or []
            out.append(f"{v}  {CYAN}{glyph('arrow')} {_plural(int(d.get('count') or len(texts)), 'message')} you typed during the turn sent to the model{RST}")
            out += [f"{v}    {MUTED}{glyph('chevron')} {_one_line(t, 100)}{RST}" for t in texts[:3]]
        elif kind == "goal_check":
            from axon.ui import goal_block as _goal_block
            out += [f"{v}  {r}" for r in _goal_block.goal_block_rows(d, width - 6)]
            row = _goal_block.goal_usage_row(d)
            if row is not None:
                step_rows.append(row)
        elif kind in ("advisor", "auto_permission"):
            from axon.agent import sidecall as _side
            row = _side.usage_row("advisor" if kind == "advisor" else "permission check", type("U", (), d)() if d.get("usage") else None)
            if row is not None:
                _side.merge_row(step_rows, row)
        elif kind == "thinking_boost":
            out.append(f"{v}  {ACCENT}{glyph('star')} \"{d.get('keyword', '')}\" {dot} effort {d.get('previous_effort', '?')} {glyph('arrow')} {d.get('effort', '?')}{RST}")
        elif kind == "budget":
            out.append(f"{v}  {WARNING}{glyph('warn')} {_one_line(d.get('message') or (str(d.get('scope', '')) + ' budget ' + str(d.get('action', ''))), 120)}{RST}")
        elif kind in ("permission", "hook"):
            out.append(f"{v}  {MUTED}{kind}: {_one_line(d.get('reason') or d.get('message') or d.get('outcome') or '', 120)}{RST}")
    final = str(_get(turn_trace, "final_text", default="") or "")
    stop = str(_get(turn_trace, "stop_reason", default="") or "")
    stopped = stop in Renderer._STOP_LABELS
    if final:
        out.append(f"{BORDER}{glyph('t_right')}{glyph('h')}{RST} {BOLD}Answer{RST}")
        out += format_markdown(final, max_width=_width(110)).splitlines()
    if final and answer_step[0] > 1 and not stopped:
        out.append(f"{v}  {MUTED}{glyph('arrow')} {_decision.final_answer_text(*answer_step)}{RST}")
    out += [f"{v}  {r}" for r in turn_summary_rows(step_rows, width - 4, stopped=stopped)]
    usage = {"input": _get(turn_trace, "input_tokens", default=0), "output": _get(turn_trace, "output_tokens", default=0),
             "cache_read": _get(turn_trace, "cache_read_tokens", default=0), "cache_write": _get(turn_trace, "cache_write_tokens", default=0)}
    dur = _get(turn_trace, "duration_s", default=None)
    if dur is None:
        st, en = _get(turn_trace, "started_at", default=0) or 0, _get(turn_trace, "ended_at", default=0) or 0
        dur = max(0.0, float(en) - float(st)) if en else None
    status = (f"{ERROR}{glyph('fail')} {Renderer._STOP_LABELS[stop]}{RST}" if stopped
              else f"{SUCCESS}{glyph('ok')} Done{RST}")
    gauge = context_gauge(*context) if context[0] and context[1] else ""
    out.append(f"{BORDER}{glyph('sq_bl')}{RST} {status} {MUTED}{dot} "
               f"{_plural(int(_get(turn_trace, 'llm_calls', default=0) or 0), 'LLM call')} {dot} "
               f"{_plural(int(_get(turn_trace, 'tool_calls', default=0) or 0), 'tool call')} {dot} "
               f"{_usage_line(usage, duration=dur, cost=_get(turn_trace, 'cost', default=None), stop=stop)}{RST}")
    if gauge:
        out.append(f"    {gauge}")          # the context gauge, once, under the turn's totals
    return "\n".join(r if r.startswith("  ") else truncate(r, width) for r in out)

def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def format_conversation_replay(messages: list[Any], registry: Any = None, width: int | None = None,
                               tag: str = "(restored)", row_tags: list[int | None] | None = None,
                               detail: int = 1) -> list[str]:
    """A restored conversation drawn like the live timeline: each prompt, then per turn the thinking
    summary, every tool call (what it does, its result preview) and the response behind the accent bar."""
    width = width or _width(110)
    results: dict[str, tuple[str, bool]] = {}
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(m, dict) and m.get("role") == "tool":
            results[str(m.get("tool_call_id", ""))] = (_block_text(content), False)
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                results[str(b.get("tool_use_id", ""))] = (_block_text(b.get("content")), bool(b.get("is_error")))
    rail = f"{BORDER}{glyph('v')}{RST} "
    says = f"{ACCENT}{glyph('quote')}{RST} "
    out: list[str] = []
    turn_open = False
    turn = 0

    def close() -> None:
        nonlocal turn_open
        if turn_open:
            out.append(f"{BORDER}{glyph('sq_bl')}{RST}")
            out.append("")
            turn_open = False

    by_call = {e.id: e.uid for e in get_transcript() if e.kind == "tool" and e.id}

    def tool_rows(name: str, inp: dict[str, Any], call_id: str) -> None:
        start = len(out)
        try:
            _tool_rows(name, inp, call_id)
        finally:
            if row_tags is not None and call_id in by_call:
                spans.append((start, len(out), by_call[call_id]))

    spans: list[tuple[int, int, int]] = []
    phased = detail <= 1
    # the assistant message being drawn: its calls, their reasons, and what its TOOLS section has shown so far
    msg: dict[str, Any] = {"n": 0, "i": 0, "bytes": 0, "tokens": 0, "names": [], "why": {}, "step": 0}

    def start_message(m: dict[str, Any], blocks: list[Any]) -> None:
        uses = [(str(b.get("id", "")), str(b.get("name", "tool")), b.get("input") if isinstance(b.get("input"), dict) else {})
                for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function", {}) if isinstance(tc, dict) else {}
            uses.append((str(tc.get("id", "")) if isinstance(tc, dict) else "", str(fn.get("name", "tool")), {}))
        reply = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                reply.append(("text", b.get("text") or ""))
            elif b.get("type") in ("thinking", "reasoning"):
                reply.append(("thinking", b.get("thinking") or b.get("text") or ""))
            elif b.get("type") == "tool_use":
                reply.append(("tool", (str(b.get("id", "")), str(b.get("name", "tool")), b.get("input") or {})))
        try:
            why = _decision.pick_rationales(reply)
        except Exception:
            why = {}
        msg.update(n=len(uses), i=0, bytes=0, tokens=0, names=[u[1] for u in uses], why=why, step=msg["step"] + 1)

    def phased_tool(name: str, inp: dict[str, Any], call_id: str) -> None:
        msg["i"] += 1
        if msg["i"] == 1:
            if out and strip_ansi(out[-1]).strip(" │|"):
                out.append(rail.rstrip())
            out.append(f"{rail}{_tool_cards.tools_label(msg['n'], _tool_cards.batch_mode(msg['names'], registry))}")
        has_result = call_id in results
        content, is_err = results.get(call_id, ("", False))
        why, why_source = msg["why"].get(call_id) or ("", "")
        call = _tool_cards.CardCall(
            number=msg["i"], name=name, inp=inp, why=why, why_source=why_source, content=sanitize(content),
            is_error=is_err, state="done" if has_result else "stopped", hint="/transcript", registry=registry,
            name_w=_tool_cards.name_column_width(msg["names"] or [name], width - 6), total=msg["n"])
        out.extend(f"{rail}  {r}" for r in _tool_cards.card_rows(call, width - 6))
        msg["bytes"] += len(content.encode("utf-8", errors="replace"))
        msg["tokens"] += max(1, int(len(content) / 3.7)) if content else 0
        if msg["i"] >= msg["n"]:
            out.append(f"{rail}  {_tool_cards.step_results_text(msg['n'], msg['bytes'], msg['tokens'], msg['step'] + 1, width - 6)}")

    def _tool_rows(name: str, inp: dict[str, Any], call_id: str) -> None:
        if phased:
            phased_tool(name, inp, call_id)
            return
        for indent, row in tool_call_rows(name, inp, max(1, detail), width - 8, registry):
            out.extend(f"{rail}  {' ' * indent}{r}" for r in _hang(row, width - 4 - indent, "", "  "))
        out.extend(f"{rail}      {r}" for r in exact_call_rows(name, inp, width - 10, max(1, detail)))
        if call_id in results:
            global _MORE_HINT
            content, is_err = results[call_id]
            _MORE_HINT = "/transcript"            # restored outputs are browsed in the transcript viewer
            try:
                rows = tool_result_rows_exact(name, inp, sanitize(content), is_err, width - 8, registry,
                                              detail=detail, hint="/transcript")
            finally:
                _MORE_HINT = "/output"
            if _is_card_rows(name, rows):
                out.extend(f"{rail}    {r}" for r in rows)
            elif rows:
                mark = f"{ERROR}{glyph('fail')}{RST} " if is_err else ""
                first = _hang(mark + rows[0], width - 9, "", "")        # after the rail and '    ⎿  '
                out.append(f"{rail}    {SUBTLE}{glyph('result')}{RST}  {first[0]}")
                out.extend(f"{rail}       {r}" for r in first[1:] + rows[1:])
        else:
            out.append(f"{rail}    {SUBTLE}{glyph('result')}  (no result recorded){RST}")

    from axon.ui.input import format_user_prompt_banner
    for m in messages:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "user":
            text = Renderer._SHELL_CTX_RE.sub("", _block_text(content)).strip()
            if not text:
                continue
            close()
            turn += 1
            msg["step"] = 0
            out += format_user_prompt_banner(text, max_w=width + 2).splitlines() + [""]
            out.append(f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {turn}{RST}" + (f" {MUTED}{tag}{RST}" if tag else ""))
            turn_open = True
        elif role == "assistant":
            if not turn_open:
                out.append(f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {turn or 1}{RST}" + (f" {MUTED}{tag}{RST}" if tag else ""))
                turn_open = True
            blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if content else [])
            start_message(m, blocks)
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type")
                if kind == "thinking" and str(b.get("thinking") or b.get("text") or "").strip():
                    if phased:
                        out += [f"{rail}{r}" for r in _tool_cards.thinking_rows(None, str(b.get('thinking') or b.get('text')), width - 2)]
                        continue
                    first = _one_line(b.get("thinking") or b.get("text"), max(20, width - 20))
                    out.append(f"{rail}  {SUBTLE}{glyph('star')} thought {glyph('chevron')}{RST} {DIM}{ITALIC}{first}{RST}")
                elif kind == "text" and str(b.get("text", "")).strip():
                    if phased:
                        if out and strip_ansi(out[-1]).strip(" │|"):
                            out.append(rail.rstrip())
                        out.append(f"{rail}{_tool_cards.answer_label()}")
                    md = format_markdown(str(b["text"]).strip(), max_width=width - 2).splitlines()
                    out.extend(f"{rail}{says}{r[2:] if r.startswith('  ') else r}" for r in md)
                elif kind == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    tool_rows(str(b.get("name", "tool")), inp, str(b.get("id", "")))
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function", {}) if isinstance(tc, dict) else {}
                try:
                    import json as _json
                    inp = _json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    inp = {}
                tool_rows(str(fn.get("name", "tool")), inp if isinstance(inp, dict) else {}, str(tc.get("id", "")))
    close()
    if row_tags is not None:            # which transcript entry each row shows (clickable after a redraw)
        row_tags[:] = [None] * len(out)
        for a, b, uid in spans:
            row_tags[a:b] = [uid] * (b - a)
    return out


def provider_name(settings: Any, model: str) -> str:
    """Provider shown next to a model in the timeline (shared by the live view, the saved transcript and
    the restored view, so all three name it the same way). '' when it cannot be determined."""
    base = str(getattr(settings, "base_url", "") or "")
    pid = str(getattr(settings, "provider", "") or "")
    try:
        if pid:          # an explicit provider preset names itself
            from axon.providers.catalog import PROVIDER_PRESETS
            preset = next((x for x in PROVIDER_PRESETS if getattr(x, "id", "") == pid), None)
            if preset is not None and getattr(preset, "name", ""):
                return _one_line(preset.name, 40)
        label = _provider_label(model or "", base)
    except Exception:
        return ""
    return "" if label in ("", "API") else _one_line(label, 40)


def _provider_label(model: str, base_url: str) -> str:
    url = (base_url or "").lower()
    if any(k in url for k in ("localhost", "127.0.0.1", ":11434")) or "ollama" in model.lower():
        return "local inference"
    try:
        from axon.providers.catalog import PROVIDER_PRESETS

        presets = list(PROVIDER_PRESETS)
        by_url = [p for p in presets if url and (getattr(p, "base_url", "") or "").rstrip("/").lower() and
                  (getattr(p, "base_url", "") or "").rstrip("/").lower() in url]
        by_model = [p for p in presets if model and model in (getattr(p, "models", None) or [])]
        for p in (by_url or ([] if url else by_model))[:1]:
            label = getattr(p, "name", "") or getattr(p, "label", "") or ""
            if getattr(p, "category", "") == "Local (Offline & Free)" or not getattr(p, "requires_key", True):
                return f"{label} (local)" if label else "local inference"
            return label or "API"
    except Exception:
        pass
    if url:
        host = re.sub(r"^https?://", "", url).split("/")[0]
        return host or "API"
    return "API"


# ─── Subagents ─────────────────────────────────────────────────────────────

_SUB_STATES = {  # status -> (glyph name, color, word)
    "completed": ("ok", SUCCESS, "done"), "exhausted": ("warn", AMBER, "step limit"),
    "error": ("fail", ERROR, "failed"), "running": ("play", CYAN, "running"),
    "cancelled": ("fail", MUTED, "stopped"), "stopped": ("fail", MUTED, "stopped"),
}


def _sub_state(status: str) -> tuple[str, str, str]:
    g, color, word = _SUB_STATES.get(status, ("circle", MUTED, "waiting"))
    return glyph(g), color, word


def _fmt_secs(s: float) -> str:
    """31.1s / 2m 05s / 1h 02m."""
    s = max(0.0, float(s or 0))
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m {int(s % 60):02d}s"
    return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"


def _hang(text: str, width: int, first: str, rest: str) -> list[str]:
    """Wrap styled *text* so every row, its prefix included, fits *width*: *first* leads row one, *rest* the others."""
    avail = max(10, width - max(display_width(first), display_width(rest)))
    return [(first if i == 0 else rest) + r for i, r in enumerate(wrap_ansi(text, avail) or [""])]


def _hang_facts(text: str, width: int, first: str = "  ", rest: str = "  ") -> list[str]:
    """Like _hang, but ' · '-separated facts only break between facts ('2m 20s wall time' stays whole)."""
    d = re.escape(glyph("dot"))
    held = re.sub(rf"(?<=[^\s{d}]) (?=[^\s{d}])", _NBSP, text)
    return [r.replace(_NBSP, " ") for r in _hang(held, width, first, rest)]


def _sub_behind_scenes(t: Any) -> str:
    """'14 tool calls · 2 provider retries · 1 resume after an error' (only the non-zero facts)."""
    calls, retries, resumes = (int(getattr(t, k, 0) or 0) for k in ("tool_calls", "retries", "resumes"))
    facts = ([_plural(calls, "tool call")] if calls else []) + ([_plural(retries, "provider retry", "provider retries")] if retries else [])
    if resumes:
        facts.append(_plural(resumes, "resume") + " after an error")
    return f" {glyph('dot')} ".join(facts)


def _subagent_step_row(ev: Any) -> str:
    """One step of a subagent: '▏ ⚒ Read · src/a.py · 0.2s' or '▏ ◆ LLM model · 1.2k in · 80 out · 1.1s'."""
    dot = f" {glyph('dot')} "
    is_llm = str(_get(ev, "kind", default="")) == "llm"
    err = bool(_get(ev, "is_error", default=False))
    mark = (f"{ERROR}{glyph('fail')}{RST}" if err else
            (f"{ACCENT}{glyph('diamond')}{RST}" if is_llm else f"{PRIMARY}{glyph('tool')}{RST}"))
    name = _one_line(_get(ev, "name", default="") or ("LLM call" if is_llm else "tool"), 40)
    label = f"{TEXT}LLM{RST} {MUTED}{name}{RST}" if is_llm else f"{BOLD}{name}{RST}"
    facts = []
    summary = _one_line(_get(ev, "summary", default=""), 90).replace(" · ", dot)
    if summary:
        facts.append(summary)
    if is_llm and (_get(ev, "input_tokens", default=0) or _get(ev, "output_tokens", default=0)):
        facts.append(f"{_fmt_tokens(int(_get(ev, 'input_tokens', default=0) or 0))} in{dot}"
                     f"{_fmt_tokens(int(_get(ev, 'output_tokens', default=0) or 0))} out")
    if _get(ev, "duration_s", default=None):
        facts.append(f"{float(_get(ev, 'duration_s')):.1f}s")
    row = f"{SUBTLE}{glyph('thinking_bar')}{RST} {mark} {label}"
    if facts:
        row += f"{MUTED}{dot}{dot.join(facts)}{RST}"
    return row


def _subagent_step_rows(tasks: list[Any], steps: list[Any], width: int) -> list[str]:
    """Each subagent's own steps (LLM and tool calls) under a heading with its number and title, one
    subagent after the other, so parallel runs do not interleave."""
    dot = f" {glyph('dot')} "
    by_task: dict[str, list[Any]] = {}
    for ev in steps:
        by_task.setdefault(str(_get(ev, "task_id", default="")), []).append(ev)
    out: list[str] = []
    for t in tasks:
        mine = by_task.get(str(getattr(t, "id", "") or f"sub-{t.index}"), [])
        if not mine:
            continue
        out.append(f"  {CYAN}{BOLD}#{t.index}{RST} {TEXT}{BOLD}{_one_line(t.title, max(10, width - 20))}{RST} "
                   f"{MUTED}{dot}{len(mine)} step{'s' if len(mine) != 1 else ''}{RST}")
        out += [truncate("    " + _subagent_step_row(ev), width) for ev in mine]
        out.append("")
    return out


def render_subagent_dashboard(tasks: list[Any], step_events: list[Any] | None = None, level: int = 1) -> None:
    """Summary table printed after a batch of parallel Task calls: one row per subagent, in call order.
    *step_events* (the SubagentStep events held while the live monitor was open) are listed under the table, grouped
    by subagent, at the normal trace level and above.

    Each failure is printed in full under its row (never cut), with the retries / resumes that came
    before it; status is a glyph + word so it never relies on color alone."""
    if not tasks:
        return
    width = max(40, min(140, term_width() - 2))
    dot = f" {glyph('dot')} "
    count = {k: sum(1 for t in tasks if t.status == k) for k in ("completed", "exhausted", "error")}
    running = len(tasks) - sum(count.values())
    tok_total = sum(int(getattr(t, "tokens_consumed", 0) or 0) for t in tasks)
    wall = max((float(getattr(t, "elapsed_s", 0.0) or 0.0) for t in tasks), default=0.0)
    cost = sum(float(getattr(t, "cost", 0.0) or 0.0) for t in tasks)
    parts = [f"{SUCCESS}{count['completed']} done{RST}"]
    if count["exhausted"]:
        parts.append(f"{AMBER}{count['exhausted']} hit the step limit{RST}")
    if count["error"]:
        parts.append(f"{ERROR}{BOLD}{count['error']} failed{RST}")
    if running:
        parts.append(f"{CYAN}{running} running{RST}")
    facts = [f"{tok_total:,} tokens"] if tok_total else []
    if wall:
        facts.append(f"{_fmt_secs(wall)} wall time")
    if cost:
        facts.append(f"${cost:.4f}")
    head = (f"{PRIMARY}{BOLD}Subagents{RST}{MUTED}{dot}{len(tasks)} in parallel{dot}{RST}"
            + f"{MUTED}{dot}{RST}".join(parts) + (f"{MUTED}{dot}{dot.join(facts)}{RST}" if facts else ""))

    # columns sized to their content; the title takes what is left (and is only cut when it must be)
    idx_w = max(1, max(len(str(t.index)) for t in tasks))
    steps = [f"{t.steps}/{t.max_steps}" if t.status not in ("completed", "error") else f"{t.steps}" for t in tasks]
    times = [_fmt_secs(getattr(t, "elapsed_s", 0.0) or 0.0) for t in tasks]
    toks = [f"{int(getattr(t, 'tokens_consumed', 0) or 0):,}" if getattr(t, "tokens_consumed", 0) else "-" for t in tasks]
    states = [f"{_sub_state(t.status)[0]} {_sub_state(t.status)[2]}" for t in tasks]
    types = [_one_line(getattr(t, "agent_type", "") or "", 18) for t in tasks]
    models = [_one_line(getattr(t, "model", "") or "", 24) for t in tasks]
    titles = [_one_line(t.title, 400) for t in tasks]

    def w(head_: str, cells: list[str]) -> int:
        return max([display_width(head_)] + [display_width(c) for c in cells])

    cols = [("#", idx_w), ("Status", w("Status", states)), ("Steps", w("Steps", steps)),
            ("Time", w("Time", times)), ("Tokens", w("Tokens", toks))]
    opt = [("Type", w("Type", types), any(types)), ("Model", w("Model", models), any(models))]
    fixed = sum(c[1] for c in cols)
    want = min(w("Subagent", titles), 44)        # an optional column only when titles still fit beside it
    shown_opt: list[tuple[str, int]] = []
    for name, cw, present in opt:
        if not present:
            continue
        left = width - 2 - fixed - sum(c[1] for c in shown_opt) - cw - 2 * (len(cols) + len(shown_opt) + 1)
        if left >= want:
            shown_opt.append((name, cw))
    left = width - 2 - fixed - sum(c[1] for c in shown_opt) - 2 * (len(cols) + len(shown_opt))
    title_w = max(8, min(left, w("Subagent", titles)))
    order = [cols[0], ("Subagent", title_w)] + shown_opt + cols[1:]

    out = ["", *_hang_facts(head, width)]
    out.append("  " + "  ".join(f"{MUTED}{pad(n, cw)}{RST}" for n, cw in order).rstrip())
    rule = f"  {SUBTLE}{glyph('h') * (sum(cw for _, cw in order) + 2 * (len(order) - 1))}{RST}"
    out.append(rule)
    detail = "  " + " " * (idx_w + 2)
    for i, t in enumerate(tasks):
        g, color, word = _sub_state(t.status)
        tparts = wrap_ansi(titles[i], title_w) or [""]     # a long title wraps inside its column (2 rows max)
        if len(tparts) > 2:
            tparts = [tparts[0], truncate(" ".join(tparts[1:]), title_w)]
        cells = {"#": f"{BOLD}{t.index}{RST}", "Subagent": f"{TEXT}{BOLD}{tparts[0]}{RST}",
                 "Type": f"{CYAN}{types[i]}{RST}", "Model": f"{MUTED}{models[i]}{RST}",
                 "Status": f"{color}{states[i]}{RST}", "Steps": f"{MUTED}{steps[i]}{RST}",
                 "Time": f"{MUTED}{times[i]}{RST}", "Tokens": f"{MUTED}{toks[i]}{RST}"}
        out.append(("  " + "  ".join(pad(cells[n], cw) for n, cw in order)).rstrip())
        out += [f"  {' ' * idx_w}  {TEXT}{BOLD}{p}{RST}" for p in tparts[1:]]
        if t.status == "error":
            reason = " ".join(_clean(getattr(t, "error_msg", "") or "the model call failed").split())
            out += _hang(f"{ERROR}{reason}{RST}", width, f"{detail}{ERROR}{glyph('fail')}{RST} ", detail + "  ")
        elif t.status == "exhausted":
            out += _hang(f"{AMBER}stopped at the step limit ({t.steps}/{t.max_steps} steps); its report says what is left{RST}",
                         width, f"{detail}{AMBER}{glyph('warn')}{RST} ", detail + "  ")
        extra = _sub_behind_scenes(t)
        if extra and (t.status in ("error", "exhausted") or getattr(t, "retries", 0) or getattr(t, "resumes", 0)):
            out += _hang(f"{MUTED}{extra}{RST}", width, f"{detail}{SUBTLE}{glyph('info')}{RST} ", detail + "  ")
    out.append(rule)
    if step_events and level >= 1:
        out.append("")
        out += _subagent_step_rows(tasks, step_events, width)
    foot = (f"{SUBTLE}/subagents{_NBSP}<#> opens a subagent's full transcript (prompt, every tool call, its report)"
            f"{dot}Tab{_NBSP}/{_NBSP}0-9 switch views during a run{RST}")
    out += [r.replace(_NBSP, " ") for r in _hang(foot, width, "  ", "  ")]
    out.append("")
    _write("\n".join(out) + "\n")


def render_subagent_transcript(task: Any, show_all: bool = True) -> None:
    """Full isolated transcript of a subagent: header block, the prompt, then every message (or the
    live activity timeline when no conversation was kept)."""
    width = max(40, min(120, term_width() - 2))
    hh, dot = glyph("hh"), f" {glyph('dot')} "
    g, color, word = _sub_state(task.status)
    tok = int(getattr(task, "tokens_consumed", 0) or 0)
    tin, tout = int(getattr(task, "input_tokens", 0) or 0), int(getattr(task, "output_tokens", 0) or 0)

    def section(label: str) -> str:
        return f"\n  {MUTED}{BOLD}{label}{RST} {SUBTLE}{glyph('h') * max(2, width - 3 - display_width(label))}{RST}"

    facts = [f"{color}{BOLD}{g} {word}{RST}"]
    for v in (getattr(task, "agent_type", ""), getattr(task, "model", "")):
        if v:
            facts.append(f"{CYAN}{_one_line(v, 40)}{RST}")
    facts.append(f"{MUTED}{task.steps}/{task.max_steps} steps{RST}")
    facts.append(f"{MUTED}{_fmt_secs(getattr(task, 'elapsed_s', 0.0) or 0.0)}{RST}")
    if tok:
        facts.append(f"{MUTED}{tok:,} tokens" + (f" (in {tin:,}{dot}out {tout:,})" if tin or tout else "") + RST)
    extra = _sub_behind_scenes(task)
    if extra:
        facts.append(f"{MUTED}{extra}{RST}")
    out = [f"\n{color}{hh * width}{RST}",
           *_hang(f"{CYAN}{BOLD}Subagent #{task.index}{RST}{MUTED}{dot}{RST}{TEXT}{BOLD}{_one_line(task.title, 300)}{RST}", width, "  ", "  "),
           *_hang_facts(f"{SUBTLE}{dot}{RST}".join(facts), width)]
    if task.status == "error" and getattr(task, "error_msg", ""):
        out += _hang(f"{ERROR}{' '.join(_clean(task.error_msg).split())}{RST}", width, f"  {ERROR}{glyph('fail')} failed:{RST} ", "    ")
    out.append(section("Prompt"))
    for para in _clean(task.prompt).splitlines() or [""]:
        out += _hang(f"{TEXT}{para}{RST}", width, "    ", "    ")

    msgs = getattr(getattr(task, "conversation", None), "messages", []) or []
    timeline = getattr(task, "timeline", None) or []
    if msgs:
        out.append(section(f"Transcript{dot}{_plural(len(msgs), 'message')}"))
        for idx, m in enumerate(msgs, 1):
            role = str(m.get("role", "unknown"))
            content = m.get("content", "")
            blocks = content if isinstance(content, list) else []
            if role == "assistant":
                label, clr = "Subagent", WARNING
            elif blocks and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                label, clr = "Tool results", INFO
            elif role == "user":
                label, clr = ("Task from the parent agent" if idx == 1 else "Input"), PRIMARY
            else:
                label, clr = role.capitalize(), INFO
            out.append(f"\n  {clr}{BOLD}[{idx}] {label}{RST}")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    kind = b.get("type", "")
                    if kind == "tool_use":
                        nm = _one_line(b.get("name"), 60)
                        out += _hang(f"{BOLD}{nm}{RST}  {MUTED}{tool_target(str(b.get('name')), b.get('input') or {})}{RST}",
                                     width, f"    {PRIMARY}{glyph('tool')}{RST} ", "      ")
                    elif kind == "tool_result":
                        body = b.get("content", "")
                        if isinstance(body, list):
                            body = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in body)
                        err = bool(b.get("is_error"))
                        lines = [l for l in _clean(body).splitlines() if l.strip()]
                        keep = lines if show_all and len(lines) <= 6 else lines[:3]
                        mark = f"{ERROR}{glyph('fail')}{RST} " if err else ""
                        for j, l in enumerate(keep or ["(empty)"]):
                            out.append(truncate(f"      {SUBTLE}{glyph('result') if j == 0 else ' '}{RST}  {mark if j == 0 else ''}"
                                                f"{ERROR if err else MUTED}{l}{RST}", width))
                        if len(lines) > len(keep):
                            out.append(f"         {SUBTLE}{glyph('ellipsis')} {_plural(len(lines) - len(keep), 'more line')}{RST}")
                    elif kind == "text":
                        out += ["  " + r for r in format_markdown(str(b.get("text", "")), max_width=width - 4).splitlines()]
            else:
                lines = str(content).splitlines()
                if not show_all and len(lines) > 60:
                    lines = lines[:30] + [f"{MUTED}{glyph('ellipsis')} {len(lines) - 60} lines hidden (/subagents {task.index} all){RST}"] + lines[-30:]
                for l in lines:
                    out += _hang(_clean(l) if glyph("ellipsis") not in l else l, width, "    ", "    ")
    elif timeline:
        out.append(section(f"Activity{dot}{_plural(len(timeline), 'event')}"))
        marks = {"step": (glyph("diamond"), CYAN), "tool": (glyph("tool"), PRIMARY), "result": (glyph("result"), SUBTLE),
                 "thinking": (glyph("thinking_bar"), MUTED), "retry": (glyph("warn"), AMBER),
                 "resume": (glyph("wrap"), AMBER), "error": (glyph("fail"), ERROR)}
        for ent in timeline:
            if not isinstance(ent, dict):
                continue
            kind = str(ent.get("kind", ""))
            mk, clr = marks.get(kind, (glyph("bullet"), MUTED))
            if ent.get("is_error"):
                clr = ERROR
            indent = "      " if kind in ("result", "thinking") else "    "
            out += _hang(f"{ERROR if ent.get('is_error') else (TEXT if kind in ('tool', 'step') else MUTED)}{_one_line(ent.get('text', ''), 2000)}{RST}",
                         width, f"{indent}{clr}{mk}{RST} ", indent + "  ")
    if getattr(task, "result_text", "") and not msgs:
        out.append(section("Report"))
        out += ["  " + r for r in format_markdown(task.result_text, max_width=width - 4).splitlines()]
    out.append(f"\n{color}{hh * width}{RST}")
    out.append(f"  {PRIMARY}Type /main or press Esc to return to the main agent.{RST}\n")
    _write("\n".join(out) + "\n")


_SUB_HEADER_RE = re.compile(r"^\[Subagent #(\d+)\s*([^\]]*?)\]:\s*(.*)$", re.DOTALL)
# '[Subagent #3 (title) failed: reason]' or '[Subagent #3 (title) failed after 7 steps: reason]', optionally
# followed (inside or after the bracket) by '\n\nProgress before the failure:\n- Read a.py\n- ...'
_SUB_FAILED_RE = re.compile(r"^\[Subagent #(\d+)(?: \((.*?)\))? failed(?: after (\d+) steps?)?:\s*(.*?)\]?\s*$", re.DOTALL)
_SUB_PROGRESS_RE = re.compile(r"\n\s*Progress before the failure:\s*\n", re.IGNORECASE)


def _parse_subagent_failure(body: str) -> tuple[str, list[str]]:
    """Failure text -> (reason, progress items)."""
    parts = _SUB_PROGRESS_RE.split(body, maxsplit=1)
    reason = parts[0].strip()
    if reason.endswith("]") and reason.count("[") < reason.count("]"):
        reason = reason[:-1].rstrip()
    progress: list[str] = []
    if len(parts) > 1:
        for l in parts[1].strip().splitlines():
            item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", l).strip()
            if item.endswith("]") and item.count("[") < item.count("]"):
                item = item[:-1].rstrip()
            if item:
                progress.append(item)
    return reason, progress


# First body words of a subagent that hit its step limit (axon.agent.subagent.STEP_LIMIT_PREFIX, lower-cased,
# plus older wordings); such a subagent still writes a wrap-up report. Not imported: render must not pull
# in the agent package at import time.
_STEP_LIMIT_STARTS = ("stopped at the step limit", "reached the iteration limit", "reached maximum iteration ceiling")
_STEP_PROGRESS_RE = re.compile(r"(?:^|\n)\s*Progress before the step limit:\s*\n", re.IGNORECASE)


def _parse_step_limit_progress(body: str) -> tuple[str, list[str]]:
    """Step-limit body -> (the wrap-up report, the '- tool' progress items after it)."""
    parts = _STEP_PROGRESS_RE.split(body, maxsplit=1)
    progress = []
    if len(parts) > 1:
        for l in parts[1].strip().splitlines():
            item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", l).strip()
            if item:
                progress.append(item)
    return parts[0].strip(), progress


def _append_box_footer(rows: list[str], text: str) -> list[str]:
    """Fold *text* into the bottom border of the box whose last row is in *rows* ('╰─ … · took 3.1s ──').
    Rows are returned unchanged plus a plain row when the border cannot take it at the same width."""
    if not rows or not strip_ansi(rows[-1]).lstrip().startswith(glyph("bl")):
        return rows + [f"{SUBTLE}{text}{RST}"]
    last = rows[-1]
    m = re.search(r"((?:\x1b\[[0-9;]*m)*)(" + re.escape(glyph("h")) + r"+)((?:\x1b\[[0-9;]*m)*)$", last)
    if not m:
        return rows + [f"{SUBTLE}{text}{RST}"]
    head, color, total = last[:m.start()], m.group(1), display_width(last)
    plain_head = strip_ansi(head).strip()
    if plain_head in (glyph("bl"), glyph("bl") + glyph("h")):        # no footer yet: '╰─ took 3.1s ──'
        new = head + (f"{color}{glyph('h')}{RST}" if plain_head == glyph("bl") else "") + f" {SUBTLE}{text}{RST} "
    else:
        new = head.rstrip(" ") + f" {SUBTLE}{glyph('dot')} {text}{RST} "
    fill = total - display_width(new)
    if fill < 2:
        return rows + [f"{SUBTLE}{text}{RST}"]
    return rows[:-1] + [new + color + glyph("h") * fill + RST]


def _is_card_rows(name: str, rows: list[str]) -> bool:
    """True for a subagent card (rows that already draw their own box, so no '⎿' marker in front)."""
    return name in ("Task", "Agent") and bool(rows) and strip_ansi(rows[0]).lstrip().startswith(glyph("tl"))


def render_subagent_result_card(content: str, width: int = 84) -> str:
    """Subagent result as a card; status comes from the result header, never from body text.

    ╭─ Subagent #5 · Inspect docs · ✗ Failed ──────
    │ 7 steps before the failure
    │ ✗ <the full reason, wrapped>
    │ Progress before the failure
    │   • Read a.py
    │ → the parent agent sees this failure and can retry
    ╰──────────────────────────────────────────────"""
    raw = (content or "").strip()
    idx, title, tokens, body, status, nsteps = "", "", "", raw, "ok", ""
    fm = _SUB_FAILED_RE.match(raw)
    hm = None if fm else _SUB_HEADER_RE.match(raw)
    if fm:
        idx, title, nsteps, body, status = fm.group(1), fm.group(2) or "", fm.group(3) or "", fm.group(4), "fail"
    elif hm:
        idx, meta, body = hm.group(1), hm.group(2), hm.group(3).strip()
        if "|" in meta:
            meta, tokens = meta.split("|", 1)
            tokens = tokens.strip()
        tm = re.search(r"\((.*)\)", meta)
        title = (tm.group(1) if tm else meta.replace("Result", "")).strip()
        if body.lower().startswith(_STEP_LIMIT_STARTS):
            status = "limit"
    for prefix, repl in (("Verify the file ", "Verify "), ("Explore the workspace at ", "Explore "), ("Look at ", "Inspect ")):
        if title.startswith(prefix):
            title = repl + title[len(prefix):]
    width = max(30, width)
    color, g, word = {"ok": (SUCCESS, glyph("ok"), "Completed"), "fail": (ERROR, glyph("fail"), "Failed"),
                      "limit": (AMBER, glyph("warn"), "Step limit")}[status]
    badge = f"{color}{BOLD}{g} {word}{RST}"
    label = f"Subagent{(' #' + idx) if idx else ''}"
    room = width - 8 - len(label) - display_width(f"{g} {word}") - 6
    full_title = _one_line(title, 300)
    title = truncate(full_title, max(8, room)) if title else ""
    sep = f" {SUBTLE}{glyph('dot')}{RST} "
    head = f"{CYAN}{BOLD}{label}{RST}" + (f"{sep}{TEXT}{BOLD}{title}{RST}" if title else "") + f"{sep}{badge}"
    inner = width - 4
    rows: list[str] = []
    if title and title != full_title:          # the header had to cut the title: show it whole once
        rows += _hang(f"{TEXT}{BOLD}{full_title}{RST}", inner, "", "")
    if status == "fail":
        reason, progress = _parse_subagent_failure(body)
        if nsteps:
            rows.append(f"{MUTED}{_plural(int(nsteps), 'step')} before the failure{RST}")
        rows += _hang(f"{ERROR}{' '.join(reason.split()) or 'the subagent stopped without a reason'}{RST}",
                      inner, f"{ERROR}{glyph('fail')}{RST} ", "  ")
        if progress:
            rows.append(f"{MUTED}{BOLD}Progress before the failure{RST}")
            for item in progress:
                rows += _hang(f"{MUTED}{_one_line(item, 1000)}{RST}", inner, f"  {SUBTLE}{glyph('bullet')}{RST} ", "    ")
        rows.append(f"{SUBTLE}{glyph('arrow')} the parent agent sees this failure and can retry{RST}")
    else:
        short_tok = tokens.split("(")[0].strip()
        detail = re.search(r"\((.*)\)", tokens)
        if short_tok:
            rows.append(f"{MUTED}{_one_line(short_tok, 60)}" + (f"{sep}{MUTED}{_one_line(detail.group(1), 60)}" if detail else "") + RST)
        progress: list[str] = []
        if status == "limit":
            first, _, rest = body.partition("\n")
            rows += _hang(f"{AMBER}{' '.join(first.split())}{RST}", inner, f"{AMBER}{glyph('warn')}{RST} ", "  ")
            body, progress = _parse_step_limit_progress(rest.strip())
        md = format_markdown(body, max_width=max(26, inner)).splitlines() if body else (
            [] if status == "limit" else [f"{MUTED}(no output){RST}"])
        rows += [r[2:] if r.startswith("  ") else r for r in md]
        if progress:
            rows.append(f"{MUTED}{BOLD}Progress before the step limit{RST}")
            for item in progress:
                rows += _hang(f"{MUTED}{_one_line(item, 1000)}{RST}", inner, f"  {SUBTLE}{glyph('bullet')}{RST} ", "    ")
    return box(head, rows, width, color=color if status != "ok" else BORDER, head=40, tail=8)
