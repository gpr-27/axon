"""
Axon terminal renderer.

The ``Renderer`` turns the provider/agent ``StreamEvent`` stream into a calm, compact transcript::

    ● Read(src/axon/cli.py · lines 1-120)
      ⎿  Read 120 lines
    ● Edit(src/axon/cli.py)
      ⎿  Updated src/axon/cli.py with 2 additions and 1 removal
          41    context
          42  - old line
          42  + new line

Assistant text streams through an incremental markdown renderer, thinking is shown dim (or
collapsed to a one-line summary), a thread-safe spinner shows elapsed time, a token counter and the
interrupt hint, and each turn ends with a one-line footer.  Verbose mode (``Renderer(verbose=True)``,
``AXON_VERBOSE=1`` or ``Renderer.toggle_verbose()``) adds the detailed LLM-call / tool-invocation /
observation trace cards.

All output goes through ``theme.OUTPUT_LOCK`` so background threads never interleave mid-line.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
import threading
import time
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
from axon.ui.markdown import (
    MarkdownStream,
    file_uri,
    format_markdown,
    highlight,
    render_table,  # noqa: F401  (re-exported for callers)
    )
from axon.ui.theme import (
    ACCENT,
    AMBER,
    BOLD,
    BORDER,
    CYAN,
    DIFF_ADD,
    DIFF_ADD_BG,
    DIFF_DEL,
    DIFF_DEL_BG,
    DIM,
    ERROR,
    GRAY_BG,
    INFO,
    ITALIC,
    MUTED,
    OUTPUT_LOCK,
    PRIMARY,
    RST,
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
    return max(30, min(cap, term_width() - 2))


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
    return Path(path).suffix.lstrip(".").lower() if path else ""


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

def _locate_line(path: str, snippet: str) -> int:
    """1-based line where *snippet* starts in *path* (after an edit), or 1 when unknown."""
    if not path or not snippet:
        return 1
    try:
        p = Path(path).expanduser()
        if not p.is_file() or p.stat().st_size > 4_000_000:
            return 1
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    idx = text.find(snippet)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else 1


def diff_rows(old_text: str, new_text: str, start_line: int = 1, width: int | None = None,
              lang: str = "", context: int = 2, max_rows: int = 60) -> list[str]:
    """Unified diff rows with line numbers and tinted add/remove backgrounds."""
    width = width or _width(140)
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    last = start_line + max(len(old_lines), len(new_lines))
    num_w = max(3, len(str(last)))
    text_w = max(10, width - num_w - 4)
    rows: list[str] = []

    def row(num: int, sign: str, text: str) -> str:
        code = text.expandtabs(4)
        if sign == "+":
            body = truncate(code, text_w)
            line = f"{DIFF_ADD_BG}{DIFF_ADD}{num:>{num_w}} + {body}"
            return line + (" " * max(0, text_w - display_width(body)) if DIFF_ADD_BG else "") + RST
        if sign == "-":
            body = truncate(code, text_w)
            line = f"{DIFF_DEL_BG}{DIFF_DEL}{num:>{num_w}} - {body}"
            return line + (" " * max(0, text_w - display_width(body)) if DIFF_DEL_BG else "") + RST
        return f"{SUBTLE}{num:>{num_w}}{RST}   {truncate(highlight(code, lang), text_w)}"

    groups = list(sm.get_grouped_opcodes(context))
    for gi, group in enumerate(groups):
        if gi:
            rows.append(f"{SUBTLE}{' ' * num_w} {glyph('ellipsis')}{RST}")
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append(row(start_line + j1 + k, " ", new_lines[j1 + k]))
            if tag in ("replace", "delete"):
                for k in range(i1, i2):
                    rows.append(row(start_line + k, "-", old_lines[k]))
            if tag in ("replace", "insert"):
                for k in range(j1, j2):
                    rows.append(row(start_line + k, "+", new_lines[k]))
    if len(rows) > max_rows:
        rows = elide(rows, max_rows - 10, 8)
    return rows


def _diff_counts(old_text: str, new_text: str) -> tuple[int, int]:
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_text.splitlines(), b=new_text.splitlines(), autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


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
        if "→" in l and l.split("→", 1)[0].strip().isdigit():
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
    low = l.lower()
    if any(w in low for w in ("error", "failed", "exception", "traceback")):
        return f"{ERROR}{l}{RST}"
    if "warn" in low:
        return f"{WARNING}{l}{RST}"
    if any(w in low for w in ("passed", "success", "completed")):
        return f"{SUCCESS}{l}{RST}"
    return f"{TEXT}{l}{RST}"


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
        return f"  {MUTED}Message queue is empty. Use /q <prompt> to add prompts.{RST}"
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


def _todo_stats(lines: list[str]) -> tuple[int, int, int]:
    completed = sum(1 for l in lines if any(t in l for t in _TODO_DONE))
    active = sum(1 for l in lines if any(t in l for t in _TODO_ACTIVE))
    pending = sum(1 for l in lines if "[ ]" in l)
    total = completed + active + pending
    return completed, total, int(completed / total * 100) if total else 0


def _todo_rows(content: str, with_bar: bool = False) -> list[str]:
    lines = content.strip().splitlines()
    completed, total, pct = _todo_stats(lines)
    rows: list[str] = []
    if total and with_bar:
        bar_len = 20
        filled = int(bar_len * completed / total)
        rows.append(f"{SUCCESS}{glyph('block') * filled}{SUBTLE}{glyph('shade') * (bar_len - filled)}{RST} {BOLD}{pct}%{RST} {MUTED}({completed}/{total} done){RST}")
    for l in lines:
        s = l.strip()
        if not s or s.startswith(("Updated todos", "Progress:", "Active Step:")):
            continue
        clean = s
        for t in _TODO_DONE + _TODO_ACTIVE + ("[ ]",):
            clean = clean.replace(t, "")
        clean = clean.strip()
        if any(t in s for t in _TODO_DONE):
            rows.append(f"{SUCCESS}{glyph('ok')}{RST} {MUTED}{clean}{RST}")
        elif any(t in s for t in _TODO_ACTIVE):
            rows.append(f"{PRIMARY}{glyph('play')}{RST} {BOLD}{clean}{RST} {MUTED}(in progress){RST}")
        elif "[ ]" in s:
            rows.append(f"{MUTED}{glyph('circle')} {clean}{RST}")
        else:
            rows.append(f"{TEXT}{s}{RST}")
    return rows


def _todo_summary(content: str, limit: int = 5) -> list[str]:
    """Plan progress row + up to *limit* items (in-progress and pending first, done items collapsed)."""
    lines = content.strip().splitlines()
    completed, total, pct = _todo_stats(lines)
    if not total:
        return _todo_rows(content)[:limit] or [f"{MUTED}(empty plan){RST}"]
    rows = _todo_rows(content)
    done = [r for r in rows if glyph("ok") in strip_ansi(r)[:2]]
    rest = [r for r in rows if r not in done]
    shown = rest[:limit]
    hidden_open = len(rest) - len(shown)
    out = [f"Plan {BOLD}{completed}/{total}{RST} done ({pct}%)"] + shown
    tail = []
    if hidden_open > 0:
        tail.append(f"+{hidden_open} more open")
    if done:
        tail.append(f"+{len(done)} done")
    if tail:
        out.append(f"{SUBTLE}{glyph('ellipsis')} {', '.join(tail)}{RST}")
    return out


def render_todo_box(content: str, max_width: int = 88) -> str:
    """Task plan checklist with progress bar."""
    completed, total, pct = _todo_stats(content.strip().splitlines())
    rows = _todo_rows(content, with_bar=True)
    title = f"{BOLD}Task Plan{RST} {MUTED}{completed}/{total} completed ({pct}%){RST}" if total else f"{BOLD}Task Plan{RST}"
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
    ]
    col2 = [
        f"{CYAN}tab{RST} to cycle modes",
        f"{CYAN}double esc{RST} to clear input",
        f"{CYAN}ctrl + o{RST} for verbose output",
        f"{CYAN}ctrl + t{RST} to toggle tasks",
        f"{CYAN}ctrl + g{RST} to edit in $EDITOR",
        f"{CYAN}\\{glyph('enter')}{RST} for newline",
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


# ─── Last tool output (for /output and ctrl+o) ─────────────────────────────
_LAST_TOOL_OUTPUT = ""


def get_last_tool_output() -> str:
    return _LAST_TOOL_OUTPUT


def _save_tool_output(content: str) -> None:
    global _LAST_TOOL_OUTPUT
    _LAST_TOOL_OUTPUT = content


# ─── Argument formatting ───────────────────────────────────────────────────

def _pretty_input(inp: dict[str, Any], max_lines: int = 24, max_str: int = 240) -> list[str]:
    """Pretty JSON of a tool input with long strings and long output truncated."""
    import json

    def clip(v: Any) -> Any:
        if isinstance(v, str) and len(v) > max_str:
            return v[:max_str] + f"... (+{len(v) - max_str} chars)"
        if isinstance(v, dict):
            return {k: clip(x) for k, x in v.items() if not str(k).startswith("_")}
        if isinstance(v, list):
            return [clip(x) for x in v[:50]]
        return v

    try:
        text = json.dumps(clip(inp or {}), indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(inp)
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (+{len(lines) - max_lines} lines)"]
    return [highlight(l, "json") for l in lines]


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
    """'12.4k in (9.8k cached 79%) · 412 out · 1.9s · $0.0031 · end_turn'."""
    dot = f" {glyph('dot')} "
    inp = int(_get(usage, "input", "input_tokens", default=0) or 0)
    out = int(_get(usage, "output", "output_tokens", default=0) or 0)
    cr = int(_get(usage, "cache_read", "cache_read_tokens", default=0) or 0)
    cw = int(_get(usage, "cache_write", "cache_write_tokens", default=0) or 0)
    reasoning = int(_get(usage, "reasoning", default=0) or 0)
    parts = [f"{_fmt_tokens(inp)} in" + (f" ({_fmt_tokens(cr)} cached {int(cr * 100 / inp) if inp else 0}%)" if cr else "")]
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
    return f"{MUTED}ctx{RST} {bar} {color}{pct:.0f}%{RST}{MUTED} of {_fmt_tokens(window)}{RST}"


TRACE_LEVELS = ("compact", "normal", "detailed", "debug")


def normalize_trace_level(level: Any) -> str:
    if isinstance(level, int) and 0 <= level < len(TRACE_LEVELS):
        return TRACE_LEVELS[level]
    s = str(level or "").strip().lower()
    aliases = {"quiet": "compact", "minimal": "compact", "default": "normal", "verbose": "detailed",
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
    if name in BASH_TOOLS:
        return _one_line(_first(inp, "command", "CommandLine"), 120)
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
        return _one_line(_first(inp, "description", "title") or str(_first(inp, "prompt")).split("\n")[0], 70)
    if name in ("Process", "Env"):
        var = _first(inp, "variable")
        return f"{_first(inp, 'action', default='list')}" + (f" {var}" if var else "")
    if name == "Diff":
        return f"{_first(inp, 'path_a')} {glyph('arrow')} {_first(inp, 'path_b')}"
    for v in inp.values():
        if isinstance(v, (str, int, float)) and str(v).strip():
            return _one_line(v, 60)
    return ""


def _count_lines(content: str) -> int:
    return len([l for l in content.splitlines() if l.strip()])


def _preview(lines: list[str], keep: int = 4) -> list[str]:
    if len(lines) <= keep + 1:
        return lines
    return lines[:keep] + [f"{SUBTLE}{glyph('ellipsis')} +{len(lines) - keep} lines (ctrl+o to expand){RST}"]


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
        fn = getattr(tool, "summarize", None)
        text = fn(inp or {}, content or "", is_error) if callable(fn) else ""
        return sanitize(str(text or "")).strip().splitlines()[0] if text else ""
    except Exception:
        return ""


def tool_result_rows(name: str, inp: dict[str, Any], content: str, is_error: bool = False,
                     width: int | None = None, registry: Any = None) -> list[str]:
    """Result rows: the tool's own one-line summary (when it provides one) + rendered body."""
    rows = summarize_tool_result(name, inp, content, is_error, width)
    if not is_error and name in EDIT_TOOLS | MULTI_EDIT_TOOLS | WRITE_TOOLS | {"TodoWrite", "Task"}:
        return rows  # diff / plan / subagent rendering already carries an exact summary
    summary = _tool_summary(registry, name, inp, content, is_error)
    if summary:
        first = f"{ERROR}{summary}{RST}" if is_error else summary
        if is_error:
            rows = [first] + rows[1:] if rows and strip_ansi(rows[0]).strip() in summary else [first] + rows
        else:
            rows = [first] + rows[1:]
    return rows


def summarize_tool_result(name: str, inp: dict[str, Any], content: str, is_error: bool = False,
                          width: int | None = None) -> list[str]:
    """Rows printed under a tool header (first row follows the '⎿' marker)."""
    width = width or _width(160)
    content = sanitize(content or "")
    text = content.strip()
    lines = text.splitlines()
    path = _short_path(str(_first(inp, "path", "file_path", "AbsolutePath", "TargetFile")))

    if is_error:
        rows = [f"{ERROR}{l}{RST}" for l in (lines or ["Tool failed"])]
        return _preview(rows, 6)

    if name in READ_TOOLS:
        n = len(lines)
        return [f"Read {BOLD}{n}{RST} {'line' if n == 1 else 'lines'}"]
    if name in BASH_TOOLS:
        if not text or text == "(Command completed with exit code 0 and no output)":
            return [f"{MUTED}(no output){RST}"]
        return _preview([_classify_output_line(l) for l in lines], 4)
    if name in EDIT_TOOLS | MULTI_EDIT_TOOLS:
        if name in MULTI_EDIT_TOOLS:
            pairs = [(e.get("old_string", ""), e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict)]
        else:
            pairs = [(str(inp.get("old_string", "") or ""), str(inp.get("new_string", "") or ""))]
        added = removed = 0
        body: list[str] = []
        for k, (old_s, new_s) in enumerate(pairs):
            a, r = _diff_counts(old_s, new_s)
            added, removed = added + a, removed + r
            if k:
                body.append(f"{SUBTLE}    {glyph('ellipsis')}{RST}")
            start = _locate_line(path, new_s) if new_s else _locate_line(path, old_s.splitlines()[0] if old_s.strip() else "")
            body += ["  " + r for r in diff_rows(old_s, new_s, start, width - 7, _lang_for(path))]
        if not pairs or (not added and not removed):
            return [text.splitlines()[0] if text else f"Updated {path}"]
        summary = (f"Updated {BOLD}{path}{RST} with {SUCCESS}{_plural(added, 'addition')}{RST}"
                   f" and {ERROR}{_plural(removed, 'removal')}{RST}")
        return [summary] + body
    if name in WRITE_TOOLS:
        code = str(_first(inp, "content", "CodeContent"))
        clines = code.splitlines()
        lang = _lang_for(path)
        rows = [f"Wrote {BOLD}{_plural(len(clines), 'line')}{RST} to {BOLD}{path}{RST}"]
        prev = [f"{SUBTLE}{i:>4}{RST}  {highlight(l, lang)}" for i, l in enumerate(clines[:8], 1)]
        if len(clines) > 8:
            prev.append(f"{SUBTLE}{glyph('ellipsis')} +{len(clines) - 8} lines{RST}")
        return rows + prev
    if name in LS_TOOLS:
        n = _count_lines(text) if text != "(Empty directory)" else 0
        return [f"Listed {BOLD}{n}{RST} {'path' if n == 1 else 'paths'}"]
    if name == "Glob":
        n = 0 if text.startswith("No files") else _count_lines(text)
        return [f"Found {BOLD}{n}{RST} {'file' if n == 1 else 'files'}"]
    if name in GREP_TOOLS:
        if not text or text.startswith("No matches"):
            return [f"{MUTED}No matches{RST}"]
        n = _count_lines(text)
        return [f"Found {BOLD}{n}{RST} {'match' if n == 1 else 'matches'}"]
    if name == "FileTree":
        return [f"{BOLD}{_count_lines(text)}{RST} entries"]
    if name == "CodeSymbols":
        return [f"{BOLD}{_count_lines(text)}{RST} symbol lines"]
    if name == "WebSearch":
        return [strip_ansi(render_web_search_box(str(_first(inp, "query")), content)).strip()]
    if name == "WebFetch":
        return [strip_ansi(render_web_fetch_box(str(_first(inp, "url")), content)).strip()]
    if name == "Http":
        size_kb = len(content.encode("utf-8", errors="replace")) / 1024
        return [f"{lines[0] if lines else '(empty)'} {MUTED}{glyph('dot')} {size_kb:.1f} KB{RST}"]
    if name == "Git":
        return _preview([_git_line(l) for l in lines if l.strip()] or [f"{MUTED}(no output){RST}"], 6)
    if name == "TodoWrite":
        return _todo_summary(content)
    if name == "Task":
        return [r[2:] for r in render_subagent_result_card(content, width=width - 6).splitlines()]
    if name == "ExitPlanMode":
        return [f"{ACCENT}{BOLD}Plan proposed for approval{RST}"] + format_markdown(content, max_width=width - 6).splitlines()
    if name == "Diff":
        return _preview([highlight(l, "diff") for l in lines], 20)
    if name == "Doctor":
        return _doctor_rows(content) or [f"{MUTED}(no output){RST}"]
    if name == "Patch":
        return [f"{SUCCESS}{text}{RST}" if text else "Patch applied"]
    if not text:
        return [f"{MUTED}(no output){RST}"]
    return _preview([f"{TEXT}{l}{RST}" for l in lines], 3)


# ─── Spinner ───────────────────────────────────────────────────────────────

class _SpinnerAwareStdout:
    """stdout proxy that erases the spinner line before any other write (from any thread)."""

    def __init__(self, real: Any, spinner: "Spinner") -> None:
        self._real = real
        self._spinner = spinner

    def write(self, s: str) -> int:
        with OUTPUT_LOCK:
            self._spinner._erase_locked()
            n = self._real.write(s)
            if s:
                self._spinner._at_line_start = s.endswith("\n")
            return n if isinstance(n, int) else len(s)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._real, item)


_ACTIVE_SPINNERS: "set[Spinner]" = set()


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
    """Thread-safe status spinner: '│ ⠋ Step 2 · thinking… 3.2s · 410 tok ↓ · ctrl+c to interrupt'.

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
        self._visible = False
        self._at_line_start = True
        self._real: Any = None
        self._proxy: _SpinnerAwareStdout | None = None
        self._frame = 0
        self._paused = 0
        self.prefix = "  "
        self.started_at = 0.0

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
        self._label = label

    def start(self, label: str = "Thinking") -> None:
        self._label = label
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

    def _erase_locked(self) -> None:
        if self._visible and self._real is not None:
            try:
                self._real.write("\r\033[2K")
            except Exception:
                pass
            self._visible = False

    def _run(self) -> None:
        frames = glyph("spinner")
        while not self._stop.wait(self._interval):
            with OUTPUT_LOCK:
                if self._stop.is_set() or sys.stdout is not self._proxy:
                    break
                if not self._at_line_start or self._paused:
                    continue
                self._frame = (self._frame + 1) % len(frames)
                line = self.render_line(frames[self._frame])
                try:
                    self._real.write("\r\033[2K" + line)
                    self._real.flush()
                    self._visible = True
                except Exception:
                    break

    def render_line(self, frame: str = "") -> str:
        elapsed = max(0.0, time.time() - (self.started_at or time.time()))
        extra = self._status() if self._status else ""
        frame = frame or glyph("spinner")[0]
        dot = f" {glyph('dot')} "
        detail = f"{elapsed:.1f}s" + (f"{dot}{extra}" if extra else "") + f"{dot}{Renderer.interrupt_hint}"
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
    ("/trace", "Press ctrl+o or type /trace detailed to see full tool inputs and results"),
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

    * ``compact``  - one line per tool call + footer
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
        self.trace_level = normalize_trace_level(level or "normal")
        self._lock = threading.RLock()
        self.registry: Any = getattr(settings, "registry", None)
        self.spinner = Spinner(status=self._spinner_status)
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
        self._turn_open = False
        self._step = 0
        self._step_t0 = 0.0
        self._step_model = ""
        self._step_stats_done = True
        self._pending_stats: tuple[Any, str] | None = None
        self._pending_perm: dict[str, list[str]] = {}
        self._pending_why: dict[str, str] = {}
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
        self.last_context: tuple[int, int] = (0, 0)   # (tokens in context, window) of the last LLM call
        self._step_context: tuple[int, int] = (0, 0)

    def attach(self, agent: Any) -> None:
        """Bind to an agent: its tool registry supplies result summaries; trace level from settings."""
        self.registry = getattr(agent, "registry", None) or getattr(agent, "tools", None) or self.registry
        settings = getattr(agent, "settings", None)
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
        """Ctrl+O: compact → normal → detailed → debug → compact."""
        self.trace_level = TRACE_LEVELS[(self.level + 1) % len(TRACE_LEVELS)]
        return self.trace_level

    def toggle_verbose(self) -> bool:
        self.cycle_trace_level()
        return self.verbose

    # ── output with block spacing ─────────────────────────────────────────
    def _out(self, text: str) -> None:
        if text:
            self._tail = (self._tail + text)[-2:]
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
        """Left prefix for timeline rows (rail when a turn is open, plain indent otherwise)."""
        return (f"{BORDER}{glyph('v')}{RST} " if self._railed else "  ") + extra

    def _line(self, text: str, extra: str = "", atomic: bool = False) -> None:
        """Write one timeline row, wrapped (with hanging indent) to the terminal width.

        ``atomic=True`` (stats / summary rows made of ' · '-separated facts) only wraps at the
        separators, so a unit like '6% of 200k' or '0.03s' is never split across rows."""
        if atomic:
            text = _dot_sep_re().sub(lambda m: f"{_NBSP}{m.group(1)}{glyph('dot')}{m.group(2)} ",
                                   text.replace(" ", _NBSP))
        lead = self._rail(extra)
        cont = self._rail(extra + "  ")
        # continuation rows carry the wider hanging indent: wrap to the narrower width so no row
        # (first or continuation) ever exceeds the terminal width.
        avail = max(10, term_width() - 1 - max(display_width(lead), display_width(cont)))
        rows = wrap_ansi(text, avail)
        if atomic:
            rows = [r.replace(_NBSP, " ") for r in rows]
        self._out("".join((lead if i == 0 else cont) + r + "\n" for i, r in enumerate(rows)))

    def _lines(self, rows: Iterable[str], extra: str = "") -> None:
        width = max(20, term_width() - 1)
        text = "".join(truncate(self._rail(extra) + r, width) + "\n" for r in rows)
        self._out(text)

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
        lines = [
            (f"{PRIMARY}{glyph('logo_top')}{RST}", f"{BOLD}{TEXT}Axon{RST} {MUTED}{v_str}{RST}"),
            (f"{CYAN}{glyph('logo_mid')}{RST}", f"{TEXT}{model}{RST}" + (f" {MUTED}{dot} {effort} effort{RST}" if effort else "") + f" {MUTED}{dot} {provider}{RST}"),
            (f"{ACCENT}{glyph('logo_bot')}{RST}", f"{MUTED}{w_path}{RST}"),
        ]
        mode_s = f"{WARNING}{mode}{RST} {MUTED}mode{RST}" if mode else ""
        trace_s = f"{MUTED}trace {self.trace_level} (ctrl+o){RST}"
        tips = f"{MUTED}/help commands {dot} ! shell {dot} @ files {dot} ? shortcuts{RST}"
        width = term_width()
        if width < 52:
            out = [""]
            for logo, text in lines:
                rows = wrap_ansi(text, max(10, width - 8))
                out += [f"  {logo} {rows[0]}"] + [f"      {r}" for r in rows[1:]]
            out.append(f"  {mode_s}" if mode_s else "")
            return "\n".join(truncate(r, width - 1) for r in out) + "\n"
        rows = [f"{logo}  {text}" for logo, text in lines]
        rows.append("")
        rows.append(f"{mode_s}{f' {MUTED}{dot}{RST} ' if mode_s else ''}{trace_s}")
        rows.append(tips)
        return "\n" + box("", rows, min(width - 2, 84), color=BORDER, wrap=True) + "\n"

    def render_user_message(self, text: str) -> None:
        """Echo the user's message as a shaded bar (wraps long / multi-line input)."""
        width = max(20, min(term_width() - 4, 120))
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
    def _open_turn(self, prompt: str, model: str, mode: str) -> None:
        self._turn_no += 1
        self._turn_open = True
        self._turn_cost = 0.0
        self._turn_cost_known = False
        if self.level < 1:
            return
        self._gap()
        dot = glyph("dot")
        left = f"{BORDER}{glyph('sq_tl')}{RST} {BOLD}Turn {self._turn_no}{RST}"
        if prompt:
            left += f" {MUTED}{dot}{RST} {TEXT}\"{_one_line(prompt, 60)}\"{RST}"
        right = f"{MUTED}{model}" + (f" {dot} {mode}" if mode else "") + f"{RST}"
        width = max(30, term_width() - 1)
        gap = width - display_width(left) - display_width(right)
        line = left + " " * max(2, gap) + right if gap >= 2 else left
        self._out(truncate(line, width) + "\n")

    def _on_llm_call(self, e: LLMCallStart) -> None:
        self._finish_stream()
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
        self._step = e.iteration
        self._step_t0 = time.time()
        self._step_model = e.model
        self._step_stats_done = False
        self._text_in_step = False
        if self.level >= 1:
            dot = glyph("dot")
            ctx = f" {MUTED}{dot} ~{_fmt_tokens(e.tokens_in)} ctx{RST}" if e.tokens_in else ""
            self._line(f"{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {e.iteration}{RST}  {TEXT}LLM call{RST} "
                       f"{MUTED}{dot} {e.model} {dot} {_plural(e.message_count, 'message')}{RST}{ctx}")
            self._render_fed_back(e)
            if self.level >= 3:
                self._render_debug_fields(e, skip={"iteration", "model", "message_count", "tokens_in", "tools_ingested", "user_prompt"})
        self.spinner.prefix = self._rail() if self._railed else "  "
        self.spinner.start(f"Step {e.iteration} {glyph('dot')} thinking")
        self.spinner.set_label(f"Step {e.iteration} {glyph('dot')} thinking")

    def _render_fed_back(self, e: LLMCallStart) -> None:
        items = e.tools_ingested or []
        if e.iteration <= 1 or not items:
            return
        total_bytes = sum(int(t.get("bytes", 0) or 0) for t in items)
        total_tok = sum(int(t.get("tokens", 0) or 0) for t in items)
        errors = sum(1 for t in items if t.get("is_error"))
        summary = (f"{glyph('arrow')} fed back {_plural(len(items), 'tool result')} from step {e.iteration - 1}: "
                   f"{_fmt_bytes(total_bytes)} {glyph('dot')} ~{_fmt_tokens(total_tok)} tokens")
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
        """Print the usage/timing/cost line of the current LLM call (once).

        Prefers the data of an ``LLMCallEnd`` trace event; falls back to the provider's
        ``TurnComplete`` usage (kept pending until the next timeline boundary).
        """
        if self._step_stats_done:
            return
        if usage is None and self._pending_stats is not None:
            usage, stop = self._pending_stats
        if usage is None:
            return  # nothing known yet; a later TurnComplete / LLMCallEnd will close the step
        self._pending_stats = None
        self._step_stats_done = True
        if cost is None:
            cost = _cost_for(self._step_model, usage)
        if cost is not None:
            try:
                self._turn_cost += float(cost)
                self._turn_cost_known = True
            except (TypeError, ValueError):
                pass
        if self.level < 1:
            return
        dur = duration if duration is not None else max(0.0, time.time() - self._step_t0)
        show_stop = stop if (self.level >= 2 or stop not in ("end_turn", "tool_use", "")) else ""
        label = f"step {self._step}: " if self._text_in_step else ""
        sep = f" {MUTED}{glyph('dot')}{RST} "
        stats = f"{SUBTLE}{glyph('result')}{RST} {MUTED}{label}{_usage_line(usage, duration=dur, cost=cost, stop=show_stop)}{RST}"
        gauge = context_gauge(*context) if context else ""
        if gauge and display_width(stats + sep + gauge) > term_width() - 1 - display_width(self._rail("    ")):
            gauge = context_gauge(*context, compact=True)
        self._line(stats + (sep + gauge if gauge else ""), "  ", atomic=True)
        if miss_cause:
            self._line(f"{WARNING}{glyph('warn')} cache miss: {miss_cause}{RST}", "    ")
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
            self._thinking_lines = []
            self._thinking_start_time = time.time()
            self._think_col = -1
            self._think_word = ""
        self._thinking_lines.append(e.text)
        if self._think_full:
            self._think_stream(sanitize(e.text))

    @property
    def _think_full(self) -> bool:
        return self.show_thinking and self.level >= 2

    def _think_stream(self, text: str) -> None:
        avail = max(20, min(term_width() - 10, 100))
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
                self._think_word += ch
        if out:
            self._out("".join(out))

    def _end_thinking(self) -> None:
        if not self._thinking_active:
            return
        self._thinking_active = False
        elapsed = max(0.1, time.time() - self._thinking_start_time)
        if self._think_full:
            if self._think_word:
                self._think_stream(" ")
            if self._think_col >= 0:
                self._out(f"{RST}\n")
                self._think_col = -1
            self._line(f"{SUBTLE}{glyph('star')} thought for {elapsed:.1f}s{RST}", "  ")
            return
        if self.level < 1:
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
            self._text_in_step = True
            if self._railed:
                # Text may precede tool calls, so it is not necessarily the final answer.
                self._out(f"{BORDER}{glyph('t_right')}{glyph('h')}{RST} {BOLD}Response{RST} "
                          f"{MUTED}{glyph('dot')} step {self._step}{RST}\n")
            else:
                self._gap()
            self.spinner.set_label(f"Step {self._step} {glyph('dot')} responding")
        self._md_out(self._md.feed(e.text))

    def _md_out(self, chunk: str) -> None:
        """Write rendered markdown rows, behind the timeline rail when the response is on it."""
        if chunk and getattr(self, "_md_railed", False):
            bar = f"{BORDER}{glyph('v')}{RST}"
            chunk = "".join((f"{bar} {row}" if row else bar) + "\n" for row in chunk.split("\n")[:-1])
        self._out(chunk)

    @staticmethod
    def _md_pending_text(text: str) -> bool:
        return bool(text.strip())

    def _finish_markdown(self) -> None:
        if self._md is not None:
            self._md_out(self._md.flush())
            self._md = None
            if not self._railed:
                self._gap()

    def _finish_stream(self) -> None:
        self._end_thinking()
        self._finish_markdown()

    # Tool calls ----------------------------------------------------------
    def _on_tool_use_start(self, e: ToolUseStart) -> None:
        self._finish_stream()
        self.spinner.set_label(f"Step {self._step} {glyph('dot')} writing {_clean(e.name)} call")

    def _on_tool_args(self, e: ToolArgsDelta) -> None:
        self._out_chars += len(e.fragment)

    def _on_tool_use_complete(self, e: ToolUseComplete) -> None:
        pass

    def _on_batch(self, e: ToolBatchStart) -> None:
        self._tool_batch_count += 1
        self._finish_stream()
        self._close_step_stats()
        if e.user_prompt:
            self._current_user_prompt = e.user_prompt
        if e.mode:
            self._current_permission_mode = e.mode
        self._finish_stream()
        if self.level >= 1:
            readonly = all((getattr(t, "name", "") or "") not in MUTATING_TOOLS for t in e.tools)
            how = ("parallel, read-only" if readonly else "sequential") if e.total_count > 1 else ("read-only" if readonly else "modifies workspace")
            self._line(f"{MUTED}{glyph('arrow')} plans {_plural(e.total_count, 'tool call')} ({how}){RST}", "  ")
        self.spinner.start("Running tools")

    def _tool_header(self, call_id: str, name: str, inp: dict[str, Any]) -> str:
        target = tool_target(name, inp)
        name = _clean(name)
        self._last_header_id = call_id
        mark = f"{PRIMARY}{glyph('tool')}{RST}"
        if self.level >= 1:
            return f"{mark} {BOLD}{name}{RST}" + (f"  {TEXT}{target}{RST}" if target else "")
        return f"{mark} {BOLD}{name}{RST}" + (f"{MUTED}({RST}{target}{MUTED}){RST}" if target else "")

    def _on_tool_start(self, e: ToolExecutionStart) -> None:
        self._finish_stream()
        self._close_step_stats()
        self._tool_call_count += 1
        inp = dict(e.input or {})
        self._active_tools[e.id] = {"name": e.name, "input": inp, "idx": self._tool_call_count, "t0": time.time()}
        if not self._railed:
            self._gap()
        self._line(self._tool_header(e.id, e.name, inp))
        why = _get(e, "rationale", "why", default="") or self._pending_why.pop(e.id, "")
        if why and self.level >= 1:
            self._line(f"{SUBTLE}why: {DIM}{ITALIC}{_one_line(why, 140)}{RST}", "    ")
        if self.level >= 2:
            self._lines(_pretty_input(inp), "    ")
        self.spinner.start(f"Running {_clean(e.name)}")
        self.spinner.set_label(f"Running {_clean(e.name)}")

    def _on_tool_result(self, e: ToolExecutionResult) -> None:
        _save_tool_output(e.content)
        self._finish_stream()
        info = self._active_tools.pop(e.id, None) or {"name": e.name, "input": {}, "idx": 0, "t0": None}
        inp = {**(info.get("input") or {}), **(e.input or {})}
        if self._last_header_id != e.id:
            self._line(self._tool_header(e.id, e.name, inp))
        self._last_header_id = ""
        width = _width(160) - 6
        rows = tool_result_rows(e.name, inp, e.content, e.is_error, width, self.registry)
        self._round_obs_count += 1
        if self.level >= 1 and rows:
            content = e.content or ""
            n_bytes = len(content.encode("utf-8", errors="replace"))
            est = max(1, len(content) // 4) if content else 0
            t0 = info.get("t0")
            dur = f" {glyph('dot')} {time.time() - t0:.2f}s" if t0 else ""
            dest = f"step {self._current_iteration + 1}" if self._turn_open else "model"
            meta = f"{SUBTLE} {glyph('dot')} sent {_fmt_bytes(n_bytes)} (~{_fmt_tokens(est)} tok) {glyph('arrow')} {dest}{dur}{RST}"
            if e.name in ("Task", "Agent") and not e.is_error:
                # the subagent card is a box: keep it intact and put the payload facts under it
                rows = rows + [f"{SUBTLE}result sent {_fmt_bytes(n_bytes)} (~{_fmt_tokens(est)} tok) {glyph('arrow')} {dest}{dur}{RST}"]
            else:
                rows = [rows[0] + meta] + rows[1:]
        if self.level >= 2 and not e.is_error and e.name not in EDIT_TOOLS | MULTI_EDIT_TOOLS | WRITE_TOOLS | {"Task", "TodoWrite"}:
            preview = [f"{DIM}{l}{RST}" for l in sanitize(e.content or "").splitlines()[:12]]
            more = len((e.content or "").splitlines()) - 12
            if preview:
                rows = rows[:1] + preview + ([f"{SUBTLE}{glyph('ellipsis')} +{more} lines (/output for all){RST}"] if more > 0 else [])
            rows.append(f"{SUBTLE}observation #{self._round_obs_count} [{(e.id or '')[:12]}] queued for step {self._current_iteration + 1}{RST}")
        if rows:
            self._line(f"{SUBTLE}{glyph('result')}{RST}  " + rows[0], "  ", atomic=True)
            self._lines(["   " + r for r in rows[1:]], "  ")
        if not self._active_tools:
            self.spinner.set_label(f"Step {self._current_iteration} {glyph('dot')} waiting for model")

    def _on_turn_complete(self, e: TurnComplete) -> None:
        self._finish_stream()
        if not self._step_stats_done:
            self._pending_stats = (e.usage, e.stop_reason)
        if e.stop_reason != "tool_use":
            self.spinner.stop()
        if e.stop_reason == "max_tokens":
            self._line(f"{WARNING}{glyph('warn')} response truncated: output token limit reached{RST}", "  ")
        elif e.stop_reason == "refusal":
            self._line(f"{WARNING}{glyph('warn')} the model declined to answer{RST}", "  ")

    # Extended trace events (axon.agent.trace, duck-typed) ----------------
    def _on_trace_event(self, e: Any) -> None:
        """Glass-box events from ``axon.agent.trace`` (matched by class name; all optional)."""
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
                                   str(_get(e, "cache_miss_cause", "cache_miss", default="") or ""),
                                   (used, window) if used and window else None)
            if str(_get(e, "outcome", default="ok")) == "error" and self.level >= 0:
                self._line(f"{ERROR}{glyph('fail')} LLM call failed: {_one_line(_get(e, 'error', default='unknown error'), 160)}{RST}", "  ")
            elif str(_get(e, "outcome", default="ok")) == "interrupted":
                self._line(f"{WARNING}{glyph('warn')} interrupted{RST}", "  ")
            if str(_get(e, "stop_reason", default="")) != "tool_use":
                self.spinner.stop()
        elif kind == "LLMRequestInfo":
            if self.level >= 2 and int(_get(e, "iteration", default=1) or 1) == 1:
                self._line(f"{SUBTLE}request: system ~{_fmt_tokens(int(_get(e, 'system_tokens', default=0) or 0))} {dot} "
                           f"{_plural(int(_get(e, 'tool_count', default=0) or 0), 'tool')} ~{_fmt_tokens(int(_get(e, 'tool_schema_tokens', default=0) or 0))} {dot} "
                           f"conversation ~{_fmt_tokens(int(_get(e, 'conversation_tokens', default=0) or 0))} tokens{RST}", "    ")
            if self.level >= 3:
                self._render_debug_fields(e, skip={"iteration", "system_tokens", "tool_schema_tokens", "conversation_tokens", "tool_count"}, title="request")
        elif kind == "PermissionDecisionEvent" or ("permission" in kind.lower() and hasattr(e, "outcome")):
            if self.level < 1:
                return
            outcome = str(_get(e, "outcome", "decision", default="")).lower()
            source = str(_get(e, "source", default=""))
            reason = str(_get(e, "reason", default=""))
            approval = _get(e, "approval", default=None)
            if outcome == "allow":
                label = "approved by user" if source == "user" or approval else (
                    f"allowed by {source}" if source and source not in ("mode",) else "auto-allowed")
                color, mark = SUCCESS, glyph("ok")
            elif outcome == "deny":
                label, color, mark = (f"denied by {source}" if source else "denied"), ERROR, glyph("fail")
            else:
                label, color, mark = f"{outcome or 'ask'}", WARNING, glyph("warn")
            text = (f"{color}{mark}{RST} {MUTED}{label}" + (f": {_one_line(reason, 200)}" if reason else "")
                    + (f" ({_one_line(approval, 80)})" if approval else "") + RST)
            self._annotate_tool(str(_get(e, "id", default="")), str(_get(e, "tool", default="")), text)
        elif kind == "HookEvent":
            if self.level < 1:
                return
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
            self._annotate_tool(str(_get(e, "tool_use_id", default="")), str(_get(e, "tool_name", default="")), text)
        elif kind == "RetryEvent":
            self._finish_stream()
            self._line(f"{WARNING}{glyph('warn')} retry {_get(e, 'attempt', default='?')}/{_get(e, 'max_attempts', default='?')} "
                       f"in {float(_get(e, 'delay_s', default=0) or 0):.1f}s {dot} {_one_line(_get(e, 'error', default=''), 120)}{RST}", "  ")
        elif kind == "CompactionEvent":
            self._finish_stream()
            if bool(_get(e, "blocked_by_hook", default=False)):
                self._line(f"{WARNING}{glyph('warn')} compaction blocked by hook{RST}", "  ")
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
            label = f"{title}" + (f" ({agent_type})" if agent_type and agent_type != "general" else "")
            if phase == "start":
                self._line(f"{CYAN}{glyph('play')} subagent started{RST} {TEXT}{label}{RST}" + (f" {MUTED}{dot} {_one_line(_get(e, 'model'), 60)}{RST}" if _get(e, "model") else ""), "  ")
            else:
                status = str(_get(e, "status", default="done"))
                color = SUCCESS if status in ("completed", "done", "ok") else (AMBER if status == "exhausted" else ERROR)
                self._line(f"{color}{glyph('ok') if color == SUCCESS else glyph('warn')} subagent {status}{RST} {TEXT}{label}{RST} {MUTED}{dot} "
                           f"{_get(e, 'iterations', default=0)} steps {dot} {_fmt_tokens(int(_get(e, 'input_tokens', default=0) or 0))} in {dot} "
                           f"{_fmt_tokens(int(_get(e, 'output_tokens', default=0) or 0))} out {dot} {float(_get(e, 'duration_s', default=0) or 0):.1f}s {dot} "
                           f"${float(_get(e, 'cost', default=0) or 0):.4f}{RST}", "  ")
        elif kind == "SubagentStep":
            if self.level < 1:
                return
            self._finish_stream()
            is_llm = str(_get(e, "kind", default="")) == "llm"
            err = bool(_get(e, "is_error", default=False))
            mark = (f"{ERROR}{glyph('fail')}{RST}" if err else
                    (f"{ACCENT}{glyph('diamond')}{RST}" if is_llm else f"{PRIMARY}{glyph('tool')}{RST}"))
            name = _one_line(_get(e, "name", default="") or ("LLM call" if is_llm else "tool"), 60)
            label = f"{TEXT}LLM{RST} {MUTED}{name}{RST}" if is_llm else f"{BOLD}{name}{RST}"
            facts = []
            summary = _one_line(_get(e, "summary", default=""), 90)
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
            tag = f"{SUBTLE}[{agent}]{RST} " if agent and self.level >= 2 else ""
            self._line(f"{SUBTLE}{glyph('thinking_bar')}{RST} {tag}{mark} {label}" + (f" {MUTED}{dot} {f' {dot} '.join(facts)}{RST}" if facts else ""), "    ")
        elif kind == "SteerEvent":
            self._finish_stream()
            texts = list(_get(e, "texts", default=()) or ())
            n = int(_get(e, "count", default=len(texts)) or len(texts))
            self._line(f"{CYAN}{glyph('arrow')} {_plural(n, 'message')} you typed during the turn sent to the model{RST}", "  ")
            for t in texts[:3]:
                self._line(f"{MUTED}{glyph('chevron')} {_one_line(t, 120)}{RST}", "    ")
        elif kind == "ThinkingBoost":
            self._line(f"{ACCENT}{glyph('star')} \"{_get(e, 'keyword', default='think')}\" {dot} effort "
                       f"{_get(e, 'previous_effort', default='?')} {glyph('arrow')} {_get(e, 'effort', default='?')}"
                       f"{' + thinking' if _get(e, 'thinking', default=False) else ''}{RST}", "  ")
        elif kind == "BudgetEvent":
            self._finish_stream()
            action = str(_get(e, "action", default="warn"))
            color = ERROR if action == "stop" else WARNING
            msg = _get(e, "message", default="") or (
                f"{_get(e, 'scope', default='turn')} budget {action}: spent ${float(_get(e, 'spent_usd', default=0) or 0):.4f}"
                f" of ${float(_get(e, 'limit_usd', default=0) or 0):.2f}"
                + (f" {dot} switching to {_get(e, 'new_model')}" if _get(e, "new_model", default="") else ""))
            self._line(f"{color}{glyph('warn')} {_one_line(msg, 160)}{RST}", "  ")
        elif kind == "MemoryLoadedEvent":
            if self.level >= 1:
                tok = int(_get(e, "tokens", default=0) or 0)
                self._line(f"{SUBTLE}{glyph('info')} memory loaded{RST} {MUTED}{_short_path(str(_get(e, 'path', default='')))}"
                           f" {dot} {_get(e, 'reason', default='')}" + (f" {dot} ~{_fmt_tokens(tok)} tok" if tok else "") + RST, "  ")
        elif kind == "ToolRationale":
            if self.level >= 1:
                call_id = str(_get(e, "id", default=""))
                text = _one_line(_get(e, "text", default=""), 140)
                if call_id in self._active_tools:
                    self._line(f"{SUBTLE}why: {DIM}{ITALIC}{text}{RST}", "    ")
                elif text:
                    self._pending_why[call_id] = text
        elif kind == "ToolTiming":
            if self.level >= 3:
                self._render_debug_fields(e, skip=set(), title="timing")
        elif self.level >= 3 and _event_fields(e):
            self._render_debug_fields(e, skip=set())

    def _annotate_tool(self, call_id: str, tool: str, text: str) -> None:
        """Print a note on a tool row (re-labelled when another tool's output came in between)."""
        if call_id and self._last_header_id != call_id and tool:
            text = f"{MUTED}[{tool}]{RST} " + text
        for row in text.split("\n"):
            self._line(row, "    ")

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

    def _spinner_status(self) -> str:
        tokens = self._out_chars // 4
        return f"{_fmt_tokens(tokens)} tok {glyph('down')}" if tokens else ""

    # ── footer ────────────────────────────────────────────────────────────
    _STOP_LABELS = {"error": "Failed", "interrupted": "Interrupted", "blocked": "Blocked",
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
            self.spinner.stop()
            self.spinner.started_at = 0.0
            railed = self._railed
            in_t = int(getattr(usage, "input", 0) or 0)
            out_t = int(getattr(usage, "output", 0) or 0)
            dot = f" {SUBTLE}{glyph('dot')}{RST} "
            if self._turn_cost_known and abs(self._turn_cost - float(cost or 0)) > 1e-9:
                money = f"{WARNING}${self._turn_cost:.4f}{RST} {MUTED}(session ${float(cost or 0):.2f}){RST}"
            else:
                money = f"{WARNING}${float(cost or 0):.4f}{RST}"
            parts = [
                f"{MUTED}{elapsed:.1f}s{RST}",
                f"{MUTED}{_plural(llm_calls, 'LLM call')}{RST}",
                f"{MUTED}{_plural(tool_count, 'tool call')}{RST}",
                f"{MUTED}{_fmt_tokens(in_t)} in{RST}",
                f"{MUTED}{_fmt_tokens(out_t)} out{RST}",
                money,
            ]
            lead = f"{BORDER}{glyph('sq_bl')}{RST} " if railed else "  "
            if failed:
                label = self._STOP_LABELS.get(stop_reason, "Failed")
                lead += f"{ERROR}{glyph('fail')} {label}{RST} {SUBTLE}{glyph('dot')}{RST} "
            else:
                lead += f"{SUCCESS}{glyph('ok')} Done{RST} {SUBTLE}{glyph('dot')}{RST} "
            rows = _join_wrapped(lead, parts, dot, max(20, term_width() - 5))
            text = ("" if railed else self._gap_text()) + "\n    ".join(rows) + "\n"
            detail = str(error or "").strip()
            if failed and detail:
                err_rows = wrap_ansi(" ".join(detail.split()), max(20, term_width() - 6))
                if len(err_rows) > 6:
                    err_rows = err_rows[:6] + ["…"]
                text += "".join(f"    {ERROR}{r}{RST}\n" for r in err_rows)
            if not Renderer._tip_shown:
                tip = self._next_tip()
                if tip:
                    tip_rows = wrap_ansi(f"Tip: {tip}", max(20, term_width() - 4))
                    text += "\n" + "".join(f"  {SUBTLE}{r}{RST}\n" for r in tip_rows)
                Renderer._tip_shown = True
            self._turn_open = False
            self._out(text + "\n")

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
    head += f"  {MUTED}{model}" + (f" {dot} {mode}" if mode else "") + RST
    out.append(head)
    for step in _get(turn_trace, "steps", default=[]) or []:
        kind = str(_get(step, "kind", default=""))
        d = _get(step, "data", default=None) or {}
        if kind == "llm":
            out.append(f"{v}{ACCENT}{glyph('diamond')}{RST} {BOLD}Step {d.get('iteration', '?')}{RST}  {TEXT}LLM call{RST} "
                       f"{MUTED}{dot} {d.get('model', model)}" + (f" {dot} {_plural(int(d.get('message_count') or 0), 'message')}" if d.get("message_count") else "") + RST)
            fed = d.get("fed_back") or []
            if fed:
                tok = sum(int(f.get("tokens", 0) or 0) for f in fed if isinstance(f, dict))
                out.append(f"{v}  {MUTED}{glyph('arrow')} fed back {_plural(len(fed), 'tool result')} (~{_fmt_tokens(tok)} tokens){RST}")
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
                gauge = context_gauge(int(d.get("context_used") or 0), int(d.get("context_window") or 0))
                out.append(f"{v}  {SUBTLE}{glyph('result')}{RST} {MUTED}{_usage_line(d['usage'], duration=d.get('duration_s'), cost=d.get('cost'), stop=str(d.get('stop_reason', '')) if lvl >= 2 else '')}{RST}"
                           + (f" {MUTED}{dot}{RST} {gauge}" if gauge else ""))
                if d.get("cache_miss_cause"):
                    out.append(f"{v}    {WARNING}{glyph('warn')} cache miss: {_one_line(d['cache_miss_cause'], 100)}{RST}")
            if d.get("error"):
                out.append(f"{v}  {ERROR}{glyph('fail')} {_one_line(d['error'], 160)}{RST}")
        elif kind == "tool":
            name = _one_line(d.get("name", "Tool"), 60)
            inp = d.get("input") or {}
            dur = d.get("duration_s")
            out.append(f"{v}{PRIMARY}{glyph('tool')}{RST} {BOLD}{name}{RST}  {TEXT}{tool_target(name, inp)}{RST}" + (f" {SUBTLE}{dot} {float(dur):.2f}s{RST}" if dur is not None else ""))
            perm = d.get("permission")
            if isinstance(perm, dict):
                out.append(f"{v}    {MUTED}permission: {perm.get('outcome', '')} by {perm.get('source', '')}" + (f": {perm.get('reason')}" if perm.get("reason") else "") + RST)
            for hk in d.get("hooks") or []:
                if isinstance(hk, dict):
                    out.append(f"{v}    {MUTED}hook {hk.get('event', '')}: {'blocked' if hk.get('blocked') else 'ran'}" + (f" {dot} {_one_line(hk.get('message'), 80)}" if hk.get("message") else "") + RST)
            if lvl >= 2:
                out += [f"{v}    {r}" for r in _pretty_input(inp)]
            content = str(d.get("result_preview", "") or "")
            rows = tool_result_rows(name, inp, content, bool(d.get("is_error")), width - 8)
            if rows and d.get("bytes") is not None:
                rows[0] += f"{SUBTLE} {dot} {int(d.get('bytes') or 0) / 1024:.1f} KB {dot} ~{_fmt_tokens(int(d.get('est_tokens') or 0))} tok {glyph('arrow')} model{RST}"
            if lvl >= 2 and content and name not in EDIT_TOOLS | MULTI_EDIT_TOOLS | WRITE_TOOLS | {"TodoWrite", "Task"}:
                rows = rows[:1] + [f"{DIM}{l}{RST}" for l in content.splitlines()[:12]]
            out += [f"{v}  " + (f"{SUBTLE}{glyph('result')}{RST}  " if i == 0 else "   ") + r for i, r in enumerate(rows)]
        elif kind == "retry":
            out.append(f"{v}  {WARNING}{glyph('warn')} retry {d.get('attempt')}/{d.get('max_attempts')} in {float(d.get('delay_s') or 0):.1f}s {dot} {_one_line(d.get('error', ''), 100)}{RST}")
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
        elif kind == "thinking_boost":
            out.append(f"{v}  {ACCENT}{glyph('star')} \"{d.get('keyword', '')}\" {dot} effort {d.get('previous_effort', '?')} {glyph('arrow')} {d.get('effort', '?')}{RST}")
        elif kind == "budget":
            out.append(f"{v}  {WARNING}{glyph('warn')} {_one_line(d.get('message') or (str(d.get('scope', '')) + ' budget ' + str(d.get('action', ''))), 120)}{RST}")
        elif kind in ("permission", "hook"):
            out.append(f"{v}  {MUTED}{kind}: {_one_line(d.get('reason') or d.get('message') or d.get('outcome') or '', 120)}{RST}")
    final = str(_get(turn_trace, "final_text", default="") or "")
    if final:
        out.append(f"{BORDER}{glyph('t_right')}{glyph('h')}{RST} {BOLD}Answer{RST}")
        out += format_markdown(final, max_width=_width(110)).splitlines()
    usage = {"input": _get(turn_trace, "input_tokens", default=0), "output": _get(turn_trace, "output_tokens", default=0),
             "cache_read": _get(turn_trace, "cache_read_tokens", default=0), "cache_write": _get(turn_trace, "cache_write_tokens", default=0)}
    dur = _get(turn_trace, "duration_s", default=None)
    if dur is None:
        st, en = _get(turn_trace, "started_at", default=0) or 0, _get(turn_trace, "ended_at", default=0) or 0
        dur = max(0.0, float(en) - float(st)) if en else None
    out.append(f"{BORDER}{glyph('sq_bl')}{RST} {SUCCESS}{glyph('ok')} Done{RST} {MUTED}{dot} "
               f"{_plural(int(_get(turn_trace, 'llm_calls', default=0) or 0), 'LLM call')} {dot} "
               f"{_plural(int(_get(turn_trace, 'tool_calls', default=0) or 0), 'tool call')} {dot} "
               f"{_usage_line(usage, duration=dur, cost=_get(turn_trace, 'cost', default=None), stop=str(_get(turn_trace, 'stop_reason', default='') or ''))}{RST}")
    return "\n".join(r if r.startswith("  ") else truncate(r, width) for r in out)

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

def render_subagent_dashboard(tasks: list[Any]) -> None:
    """Subagent fan-out progress panel."""
    if not tasks:
        return
    width = min(90, term_width() - 2)
    total = len(tasks)
    done = sum(1 for t in tasks if t.status in ("completed", "exhausted", "error"))
    pct = int(done / total * 100) if total else 0
    tok_total = sum(int(getattr(t, "tokens_consumed", 0) or 0) for t in tasks)
    rows = [f"{PRIMARY}{BOLD}Progress: {done}/{total} completed ({pct}%){RST}" + (f" {MUTED}{glyph('dot')} {tok_total:,} tokens{RST}" if tok_total else "")]
    for t in tasks:
        tok = int(getattr(t, "tokens_consumed", 0) or 0)
        tok_s = f" {glyph('dot')} {tok:,} tokens" if tok else ""
        elapsed = float(getattr(t, "elapsed_s", 0.0) or 0.0)
        if t.status == "completed":
            badge, stat = f"{SUCCESS}{glyph('ok')}{RST}", f"{MUTED}{t.steps} steps {glyph('dot')} {elapsed:.1f}s{tok_s}{RST}"
        elif t.status == "exhausted":
            badge, stat = f"{AMBER}{glyph('warn')}{RST}", f"{AMBER}{t.steps} steps {glyph('dot')} step limit reached{tok_s}{RST}"
        elif t.status == "error":
            badge, stat = f"{ERROR}{glyph('fail')}{RST}", f"{ERROR}{_one_line(getattr(t, 'error_msg', '') or 'Failed', 60)}{RST}"
        else:
            badge, stat = f"{CYAN}{glyph('play')}{RST}", f"{CYAN}step {t.steps}/{t.max_steps}{RST}"
        name = truncate(f"Subagent {t.index}: {_one_line(t.title, 60)}", 34)
        rows.append(f"{badge} {BOLD}{pad(name, 34)}{RST} {stat}")
    out = "\n" + box(f"{WARNING}{BOLD}Subagent Fan-Out{RST} {MUTED}({_plural(total, 'parallel task')}){RST}", rows, width, color=WARNING)
    out += f"\n  {SUBTLE}{glyph('chevron')} press down arrow or type /subagents to inspect transcripts{RST}\n\n"
    _write(out)


def render_subagent_transcript(task: Any, show_all: bool = True) -> None:
    """Full isolated transcript of a subagent."""
    width = min(100, term_width() - 2)
    hh = glyph("hh")
    status_clr = SUCCESS if task.status == "completed" else (AMBER if task.status == "exhausted" else ERROR)
    tok = int(getattr(task, "tokens_consumed", 0) or 0)
    out = [
        f"\n{WARNING}{hh * width}{RST}",
        f"  {CYAN}{BOLD}Subagent #{task.index}: {_one_line(task.title, 200)}{RST}",
        f"  {MUTED}Status:{RST} {status_clr}{task.status.upper()}{RST}  {MUTED}Steps:{RST} {task.steps}/{task.max_steps}  "
        f"{MUTED}Duration:{RST} {task.elapsed_s:.1f}s" + (f"  {MUTED}Tokens:{RST} {tok:,}" if tok else ""),
        f"  {MUTED}Task Prompt:{RST} {TEXT}{_clean(task.prompt)}{RST}",
        f"{WARNING}{glyph('h') * width}{RST}",
    ]
    msgs = getattr(getattr(task, "conversation", None), "messages", []) or []
    if msgs:
        for idx, m in enumerate(msgs, 1):
            role = str(m.get("role", "unknown")).upper()
            clr = PRIMARY if role == "USER" else (WARNING if role == "ASSISTANT" else INFO)
            content = m.get("content", "")
            out.append(f"\n  {clr}{BOLD}[{idx}] {role}{RST}")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    kind = b.get("type", "")
                    if kind == "tool_use":
                        out.append(f"    {TEXT}{glyph('tool')}{RST} {BOLD}{_one_line(b.get('name'), 60)}{RST}{MUTED}({_one_line(tool_target(str(b.get('name')), b.get('input') or {}), 80)}){RST}")
                    elif kind == "tool_result":
                        out.append(f"      {SUBTLE}{glyph('result')}{RST}  {MUTED}{_one_line(b.get('content', ''), 110)}{RST}")
                    elif kind == "text":
                        out.append(format_markdown(str(b.get("text", "")), max_width=width - 2))
            else:
                lines = str(content).splitlines()
                if not show_all and len(lines) > 60:
                    lines = lines[:30] + [f"{MUTED}{glyph('ellipsis')} {len(lines) - 60} lines hidden (/subagents {task.index} all){RST}"] + lines[-30:]
                out.append("\n".join(f"  {_clean(l)}" for l in lines))
    elif getattr(task, "result_text", ""):
        out.append(f"\n  {CYAN}{BOLD}Final Result:{RST}\n")
        out.append(format_markdown(task.result_text, max_width=width))
    out.append(f"\n{WARNING}{hh * width}{RST}")
    out.append(f"  {PRIMARY}Type /main or press Esc to return to the main agent.{RST}\n")
    _write("\n".join(out) + "\n")


_SUB_HEADER_RE = re.compile(r"^\[Subagent #(\d+)\s*([^\]]*?)\]:\s*(.*)$", re.DOTALL)
_SUB_FAILED_RE = re.compile(r"^\[Subagent #(\d+) failed:\s*(.*)\]\s*$", re.DOTALL)


def render_subagent_result_card(content: str, width: int = 84) -> str:
    """Subagent result as a card; status comes from the result header, never from body text."""
    raw = (content or "").strip()
    idx, title, tokens, body, status = "", "", "", raw, "ok"
    fm = _SUB_FAILED_RE.match(raw)
    hm = None if fm else _SUB_HEADER_RE.match(raw)
    if fm:
        idx, body, status = fm.group(1), fm.group(2), "fail"
    elif hm:
        idx, meta, body = hm.group(1), hm.group(2), hm.group(3).strip()
        if "|" in meta:
            meta, tokens = meta.split("|", 1)
            tokens = tokens.strip()
        tm = re.search(r"\((.*)\)", meta)
        title = (tm.group(1) if tm else meta.replace("Result", "")).strip()
        if body.startswith("reached maximum iteration ceiling"):
            status = "limit"
    for prefix, repl in (("Verify the file ", "Verify "), ("Explore the workspace at ", "Explore "), ("Look at ", "Inspect ")):
        if title.startswith(prefix):
            title = repl + title[len(prefix):]
    title = truncate(_one_line(title, 200), 40)
    badge = {"ok": f"{SUCCESS}{glyph('ok')} Completed{RST}", "fail": f"{ERROR}{glyph('fail')} Failed{RST}",
             "limit": f"{AMBER}{glyph('warn')} Step limit{RST}"}[status]
    short_tok = tokens.split("(")[0].strip()
    head = f"{CYAN}{BOLD}Subagent{(' #' + idx) if idx else ''}{RST}" + (f" {MUTED}{title}{RST}" if title else "") + f" {glyph('dot')} {badge}"
    if short_tok:
        head += f" {MUTED}{glyph('dot')} {_one_line(short_tok, 40)}{RST}"
    rows = format_markdown(body, max_width=max(30, width - 4)).splitlines() if body else [f"{MUTED}(no output){RST}"]
    rows = [r[2:] if r.startswith("  ") else r for r in rows]
    footer = f"{MUTED}{_one_line(tokens, 120)}" if tokens else ""
    return box(head, rows, width, footer=footer, head=40, tail=8)
