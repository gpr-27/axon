from __future__ import annotations
import difflib
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from axon.providers.base import (
    LLMCallStart,
    StreamEvent,
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
from axon.ui.markdown import format_markdown, render_table, str_width
from axon.ui.theme import (
    AMBER,
    BOLD,
    CYAN,
    DARK_SLATE,
    DIM,
    GOLD,
    GRAY_BG,
    ITALIC,
    LBLUE,
    MINT,
    ORANGE,
    PURPLE,
    ROSE,
    RST,
    SLATE,
    TEAL,
    TERRACOTTA,
    WHITE,
    strip_ansi,
    term_width,
)

GREEN = "\033[32m"
RED = "\033[31m"
UNDER = "\033[4m"

def get_ordinal(n: int) -> str:
    """Return English ordinal string (1st, 2nd, 3rd, 4th, 5th, etc.)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def get_file_icon(path_str: str) -> str:
    """Return contextual icon matching IDE syntax for file extension or directory."""
    p = str(path_str).lower()
    if p.endswith((".py", ".pyi")):
        return "🐍"
    elif p.endswith((".toml", ".yaml", ".yml", ".json", ".env", ".ini", ".cfg")):
        return "⚙️"
    elif p.endswith((".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".rs", ".go")):
        return "⚡"
    elif p.endswith((".md", ".markdown", ".txt", ".rst")):
        return "📄"
    elif p.endswith((".html", ".css", ".js", ".ts", ".jsx", ".tsx")):
        return "🌐"
    elif p.endswith((".sh", ".bash", ".zsh")):
        return "📜"
    elif "/" in path_str and "." not in Path(path_str).name:
        return "📁"
    return "📄"

def make_clickable_link(path_str: str, line_range: str = "", workspace: Path | None = None) -> str:
    """Create terminal OSC 8 clickable link."""
    ws = workspace or Path.cwd()
    p = Path(path_str)
    abs_p = (ws / p).resolve() if not p.is_absolute() else p
    file_url = f"file://{abs_p}"
    display = f"{path_str}{line_range}"
    return f"\x1b]8;;{file_url}\x1b\\{display}\x1b]8;;\x1b\\"

def make_clickable_custom(label: str, target_path: str, workspace: Path | None = None) -> str:
    """Create terminal OSC 8 clickable link with custom display label."""
    ws = workspace or Path.cwd()
    p = Path(target_path)
    abs_p = (ws / p).resolve() if not p.is_absolute() else p
    file_url = f"file://{abs_p}"
    return f"\x1b]8;;{file_url}\x1b\\{label}\x1b]8;;\x1b\\"

def highlight_code(line: str) -> str:
    """Token-based fast syntax highlighter that guarantees 100% clean ANSI sequences."""
    if not line:
        return ""
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "/*", "* ")):
        return f"{DIM}{SLATE}{ITALIC}{line}{RST}"

    token_spec = [
        ("COMMENT", r"#.*"),
        ("STRING",  r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"),
        ("WORD",    r"[a-zA-Z_]\w*"),
        ("NUMBER",  r"\b\d+(?:\.\d+)?\b"),
        ("SPACE",   r"\s+"),
        ("OTHER",   r"."),
    ]
    tok_regex = "|".join(f"(?P<{pair[0]}>{pair[1]})" for pair in token_spec)
    keywords = {
        "def", "class", "import", "from", "return", "if", "else", "elif",
        "for", "while", "try", "except", "finally", "with", "as", "async",
        "await", "lambda", "yield", "self", "True", "False", "None", "in",
        "is", "not", "and", "or", "function", "const", "let", "var", "package",
        "func", "struct", "interface", "type", "nil", "switch", "case", "break", "continue"
    }
    types = {
        "int", "str", "float", "bool", "list", "dict", "set", "tuple", "Any",
        "Optional", "Union", "List", "Dict", "Set", "Tuple", "Path", "Iterator", "ClassVar"
    }

    out = []
    for mo in re.finditer(tok_regex, line):
        kind = mo.lastgroup
        val = mo.group()
        if kind == "COMMENT":
            out.append(f"{DIM}{SLATE}{ITALIC}{val}{RST}")
        elif kind == "STRING":
            out.append(f"{MINT}{val}{RST}")
        elif kind == "WORD":
            if val in keywords:
                out.append(f"{PURPLE}{val}{RST}")
            elif val in types:
                out.append(f"{TEAL}{val}{RST}")
            else:
                out.append(f"{WHITE}{val}{RST}")
        elif kind == "NUMBER":
            out.append(f"{GOLD}{val}{RST}")
        else:
            out.append(val)

    return "".join(out)

HEAD_LINES = 3
TAIL_LINES = 2

def _head_tail(inner: list[str], head: int = HEAD_LINES, tail: int = TAIL_LINES, max_line_len: int = 100) -> list[str]:
    """Return inner lines with a head+tail view: show first N, hidden indicator, last N, clamped to max_line_len."""
    safe_inner = []
    for line in inner:
        plain = strip_ansi(line)
        if len(plain) > max_line_len:
            prefix = f"  {DARK_SLATE}│{RST} "
            if line.startswith(prefix):
                content_part = plain.lstrip(" │")
                safe_inner.append(f"{prefix}{content_part[:max_line_len - 10]}...")
            else:
                safe_inner.append(f"{plain[:max_line_len - 3]}...")
        else:
            safe_inner.append(line)

    n = len(safe_inner)
    if n <= head + tail:
        return safe_inner
    hidden = n - head - tail
    sep = f"  {DARK_SLATE}│{RST}  {DIM}··· {hidden} lines hidden ···{RST}"
    return safe_inner[:head] + [sep] + safe_inner[n - tail:]


def render_diff_box(old_text: str, new_text: str, filename: str = "", max_width: int = 88, max_show: int = 3) -> str:
    """Render unified diff box with red/green syntax highlighting."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    if not diff:
        return ""

    content_lines = [l for l in diff if not l.startswith(("---", "+++", "@@"))]
    added = sum(1 for l in content_lines if l.startswith("+"))
    removed = sum(1 for l in content_lines if l.startswith("-"))

    icon = get_file_icon(filename) if filename else ""
    fname_str = f" · {icon} {filename}" if filename else ""
    title = f"Diff{fname_str} (+{added} / -{removed} lines)"

    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in content_lines:
        if l.startswith("+"):
            inner.append(f"  {DARK_SLATE}│{RST} {GREEN}{l}{RST}")
        elif l.startswith("-"):
            inner.append(f"  {DARK_SLATE}│{RST} {RED}{l}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {DIM}{l}{RST}")
    out.extend(_head_tail(inner))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_read_box(p: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render file read content box with styled line numbers and syntax highlighting."""
    filename = Path(p).name if p else "File"
    icon = get_file_icon(p)
    lines = content.strip().splitlines()
    line_cnt = len(lines)
    size_kb = len(content) / 1024

    clickable_f = make_clickable_custom(filename, p)
    title = f"File Content · {icon} {filename} ({line_cnt} lines · {size_kb:.1f} KB)"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {LBLUE}File Content · {icon} {clickable_f} {SLATE}({line_cnt} lines · {size_kb:.1f} KB){DARK_SLATE} {'─' * border_w}{RST}"]

    inner = []
    for l in lines:
        if "→" in l:
            parts = l.split("→", 1)
            line_no_raw = parts[0].strip()
            line_no = int(line_no_raw) if line_no_raw.isdigit() else 0
            code_text = parts[1] if len(parts) > 1 else ""
            hl_code = highlight_code(code_text)
            inner.append(f"  {DARK_SLATE}│{RST} {DARK_SLATE}{line_no:4d} │{RST} {hl_code}")
        else:
            hl_code = highlight_code(l)
            inner.append(f"  {DARK_SLATE}│{RST}      │ {hl_code}")
    out.extend(_head_tail(inner))

    out.append(f"  {DARK_SLATE}└── Read {line_cnt} lines ({size_kb:.1f} KB) {'─' * max(4, max_width - 32)}{RST}")
    return "\n".join(out)

def render_ls_box(path_str: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render directory contents list in a specialized box with contextual file/folder icons and clickable links."""
    raw_lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not raw_lines or content.strip() == "(Empty directory)":
        return f"    {SLATE}└─ (Empty directory){RST}"

    base_p = Path(path_str or ".").expanduser()
    title = f"Directory Listing · 📁 {make_clickable_link(path_str or '.')} ({len(raw_lines)} items)"
    border_w = max(10, max_width - str_width(f"Directory Listing · 📁 {path_str or '.'} ({len(raw_lines)} items)") - 8)
    out = [f"  {DARK_SLATE}┌── {TEAL}{title}{DARK_SLATE} {'─' * border_w}{RST}"]

    inner = []
    for item in raw_lines:
        if item.startswith("Total:") or item.startswith("...") or item.startswith("Directory"):
            inner.append(f"  {DARK_SLATE}│{RST} {SLATE}{item}{RST}")
            continue
        clean_name = item.rstrip("/")
        item_path = str(base_p / clean_name)
        if item.endswith("/"):
            inner.append(f"  {DARK_SLATE}│{RST} 📁 {CYAN}{make_clickable_custom(item, item_path)}{RST}")
        else:
            icon = get_file_icon(item)
            inner.append(f"  {DARK_SLATE}│{RST} {icon} {WHITE}{make_clickable_custom(item, item_path)}{RST}")
    out.extend(_head_tail(inner))

    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    out.append(f"    {SLATE}└─ Found {len(raw_lines)} items{RST}")
    return "\n".join(out)

def render_grep_box(pattern: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render search matches with clickable file paths, line numbers, and snippets."""
    lines = content.strip().splitlines()
    if not lines or content.strip().startswith("No matches"):
        return f"    {SLATE}└─ No matches found for \"{pattern}\"{RST}"

    title = f"Grep Matches · \"{pattern}\" ({len(lines)} matches)"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {MINT}{title}{DARK_SLATE} {'─' * border_w}{RST}"]

    inner = []
    for l in lines:
        parts = l.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            fpath, lineno, snippet = parts
            icon = get_file_icon(fpath)
            clickable_f = make_clickable_link(fpath, f":{lineno}")
            inner.append(f"  {DARK_SLATE}│{RST} {icon} {WHITE}{clickable_f}{RST}  {DIM}{snippet.strip()}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner))

    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    out.append(f"    {SLATE}└─ Found {len(lines)} matches{RST}")
    return "\n".join(out)

def render_glob_box(pattern: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render matched file paths with icons and clickable links."""
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    if not lines or content.strip().startswith("No files"):
        return f"    {SLATE}└─ No files matched pattern '{pattern}'{RST}"

    title = f"Glob Matches · 📁 {pattern} ({len(lines)} files)"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {TEAL}{title}{DARK_SLATE} {'─' * border_w}{RST}"]

    inner = [f"  {DARK_SLATE}│{RST} {get_file_icon(l)} {WHITE}{make_clickable_link(l)}{RST}" for l in lines]
    out.extend(_head_tail(inner))

    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    out.append(f"    {SLATE}└─ Found {len(lines)} items{RST}")
    return "\n".join(out)

def render_write_box(p: str, content_written: str, max_show: int = 60, max_width: int = 88) -> str:
    """Render newly written file snippet with premium rounded frames, syntax highlighting and clean line numbers."""
    filename = Path(p).name if p else "File"
    ext = Path(p).suffix[1:].upper() if Path(p).suffix else "FILE"
    icon = get_file_icon(p)
    lines = content_written.strip().splitlines() if content_written else []
    line_cnt = len(lines)
    size_kb = len(content_written) / 1024 if content_written else 0

    if lines:
        header_tag = f"[{ext}] · {line_cnt} lines · {size_kb:.1f} KB"
        title = f"Created File · {icon} {filename} {DARK_SLATE}──{RST} {SLATE}{header_tag}{RST}"
        title_plain = f"Created File · {icon} {filename} ── {header_tag}"
        border_w = max(4, max_width - str_width(title_plain) - 8)
        out = [f"  {MINT}╭──{RST} {BOLD}{WHITE}{title}{RST} {MINT}{'─' * border_w}╮{RST}"]
        inner = [f"  {MINT}│{RST} {DARK_SLATE}{idx:4d} │{RST} {highlight_code(l)}" for idx, l in enumerate(lines, 1)]
        def _write_sep(n_hidden: int) -> str:
            return f"  {MINT}│{RST}  {DIM}··· {n_hidden} lines hidden ···{RST}"
        total = len(inner)
        if total > HEAD_LINES + TAIL_LINES:
            hidden = total - HEAD_LINES - TAIL_LINES
            out.extend(inner[:HEAD_LINES])
            out.append(_write_sep(hidden))
            out.extend(inner[total - TAIL_LINES:])
        else:
            out.extend(inner)
        saved_str = f"✓ Saved {p}"
        bot_border_w = max(4, max_width - len(saved_str) - 8)
        out.append(f"  {MINT}╰── {saved_str} {'─' * bot_border_w}╯{RST}")
        return "\n".join(out)
    return f"  {MINT}  └─ ✓ Saved {p}{RST}"

def stream_write_box(p: str, content_written: str, max_show: int = 60, max_width: int = 88) -> None:
    """Stream newly written file lines smoothly with premium rounded frames, live syntax highlighting and clean stopping."""
    filename = Path(p).name if p else "File"
    ext = Path(p).suffix[1:].upper() if Path(p).suffix else "FILE"
    icon = get_file_icon(p)
    lines = content_written.strip().splitlines() if content_written else []
    line_cnt = len(lines)
    size_kb = len(content_written) / 1024 if content_written else 0

    if not lines:
        sys.stdout.write(f"  {MINT}  └─ ✓ Saved {p}{RST}\n\n")
        sys.stdout.flush()
        return

    header_tag = f"[{ext}] · {line_cnt} lines · {size_kb:.1f} KB"
    title = f"Created File · {icon} {filename} {DARK_SLATE}──{RST} {SLATE}{header_tag}{RST}"
    title_plain = f"Created File · {icon} {filename} ── {header_tag}"
    border_w = max(4, max_width - str_width(title_plain) - 8)
    sys.stdout.write(f"  {MINT}╭──{RST} {BOLD}{WHITE}{title}{RST} {MINT}{'─' * border_w}╮{RST}\n")
    sys.stdout.flush()

    inner = [f"  {MINT}│{RST} {DARK_SLATE}{idx:4d} │{RST} {highlight_code(l)}" for idx, l in enumerate(lines, 1)]
    total = len(inner)
    if total > HEAD_LINES + TAIL_LINES:
        hidden = total - HEAD_LINES - TAIL_LINES
        display = inner[:HEAD_LINES] + [f"  {MINT}│{RST}  {DIM}··· {hidden} lines hidden ···{RST}"] + inner[total - TAIL_LINES:]
    else:
        display = inner
    for row in display:
        sys.stdout.write(row + "\n")
        sys.stdout.flush()

    saved_str = f"✓ Saved {p}"
    bot_border_w = max(4, max_width - len(saved_str) - 8)
    sys.stdout.write(f"  {MINT}╰── {saved_str} {'─' * bot_border_w}╯{RST}\n\n")
    sys.stdout.flush()

def render_bash_box(content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render bash stdout/stderr output box with styled terminal formatting."""
    lines = content.strip().splitlines()
    if not lines or content.strip() == "(Command completed with exit code 0 and no output)":
        return f"  {MINT}  └─ ✓ Command completed successfully{RST}"

    title = f"Output · {len(lines)} line{'s' if len(lines) != 1 else ''}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {LBLUE}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        if "error" in l.lower() or "failed" in l.lower() or "exception" in l.lower():
            inner.append(f"  {DARK_SLATE}│{RST} {ROSE}{l}{RST}")
        elif "warning" in l.lower() or "warn" in l.lower():
            inner.append(f"  {DARK_SLATE}│{RST} {AMBER}{l}{RST}")
        elif "passed" in l.lower() or "success" in l.lower() or "completed" in l.lower():
            inner.append(f"  {DARK_SLATE}│{RST} {MINT}{l}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_doctor_box(content: str, max_width: int = 88) -> str:
    """Render diagnostics output box."""
    lines = content.strip().splitlines()
    title = "Axon System Diagnostics"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {TEAL}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    for l in lines:
        if l.startswith("==="):
            continue
        if ":" in l:
            k, v = l.split(":", 1)
            out.append(f"  {DARK_SLATE}│{RST} {TEAL}{k:<16}:{RST} {WHITE}{v.strip()}{RST}")
        else:
            out.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_queue_box(queue: Any, max_width: int = 88) -> str:
    """Render pending message queue with ids and removal hints."""
    items = getattr(queue, "items", [])
    if not items:
        return f"  {SLATE}📥 Message queue is empty. Use /q <prompt> to add prompts.{RST}"

    title = f"📥 Message Queue ({len(items)} pending)"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]

    for idx, it in enumerate(items, 1):
        if idx == 1:
            badge = f"{CYAN}{BOLD}#{it.id} [Next]{RST}"
            out.append(f"  {DARK_SLATE}│{RST}  {badge}: {WHITE}{BOLD}{it.text}{RST}")
        else:
            badge = f"{SLATE}#{it.id}{RST}"
            out.append(f"  {DARK_SLATE}│{RST}  {badge}: {WHITE}{it.text}{RST}")

    out.append(f"  {DARK_SLATE}├{'─' * max(10, max_width - 6)}{RST}")
    out.append(f"  {DARK_SLATE}│{RST}  {DARK_SLATE}› Type {BOLD}/q drop <id>{RST}{DARK_SLATE} to remove, {BOLD}/q clear{RST}{DARK_SLATE} to empty, or press Enter to run next{RST}")
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_todo_box(content: str, max_width: int = 88) -> str:
    """Render task plan checklist with live progress bar and step indicators."""
    lines = content.strip().splitlines()

    completed = sum(1 for l in lines if "[✓]" in l or "[x]" in l)
    in_prog = sum(1 for l in lines if "[▶]" in l or "[>]" in l or "[*]" in l)
    pending = sum(1 for l in lines if "[ ]" in l)
    total = completed + in_prog + pending
    pct = int(completed / total * 100) if total > 0 else 0

    bar_len = 16
    filled = int(bar_len * completed / total) if total > 0 else 0
    prog_bar = "█" * filled + "░" * (bar_len - filled)

    title = f"Task Plan · {completed}/{total} completed ({pct}%)" if total > 0 else "Task Plan Checklist"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]

    if total > 0:
        bar_str = f"  {DARK_SLATE}│{RST}  {MINT}[{prog_bar}]{RST} {GOLD}{BOLD}{pct}%{RST} {SLATE}({completed}/{total} steps finished){RST}"
        out.append(bar_str)
        out.append(f"  {DARK_SLATE}├{'─' * max(10, max_width - 6)}{RST}")

    for l in lines:
        if l.startswith("Updated todos") or l.startswith("Progress:") or l.startswith("Active Step:") or not l.strip():
            continue
        if "[✓]" in l or "[x]" in l:
            clean_l = l.replace("[x]", "").replace("[✓]", "").strip()
            out.append(f"  {DARK_SLATE}│{RST}  {MINT}✓ {clean_l}{RST}")
        elif "[▶]" in l or "[>]" in l or "[*]" in l:
            clean_l = l.replace("[>]", "").replace("[▶]", "").replace("[*]", "").strip()
            out.append(f"  {DARK_SLATE}│{RST}  {CYAN}{BOLD}▶ {clean_l} {SLATE}(in progress){RST}")
        elif "[ ]" in l:
            clean_l = l.replace("[ ]", "").strip()
            out.append(f"  {DARK_SLATE}│{RST}  {SLATE}○ {clean_l}{RST}")
        else:
            out.append(f"  {DARK_SLATE}│{RST}  {WHITE}{l.strip()}{RST}")

    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_side_question_box(question: str, answer: str, max_width: int = 88) -> str:
    """Render isolated /btw side question response without polluting main conversation."""
    title = f"💬 Side Question / BTW · \"{question[:40]}\""
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    for l in answer.strip().splitlines():
        out.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.append(f"  {DARK_SLATE}└── {SLATE}(Context preserved · Returning to main flow){DARK_SLATE} {'─' * max(5, max_width - 48)}{RST}")
    return "\n".join(out)

def render_shortcuts_footer(max_width: int = 88) -> str:
    """Render the 2-column shortcuts helper panel."""
    col1 = [
        f"{GOLD}!{RST} for {UNDER}shell{RST} mode",
        f"{GOLD}/{RST} for commands",
        f"{GOLD}@{RST} for file paths",
        f"{GOLD}/btw{RST} for side question",
        f"{GOLD}?{RST} or {GOLD}/kb{RST} for shortcuts",
    ]
    col2 = [
        f"{CYAN}tab{RST} to cycle modes",
        f"{CYAN}double esc{RST} to clear input",
        f"{CYAN}ctrl + o{RST} for verbose output",
        f"{CYAN}ctrl + t{RST} to toggle tasks",
        f"{CYAN}\\⏎{RST} for newline",
    ]

    max_rows = max(len(col1), len(col2))
    out = []
    for r in range(max_rows):
        c1 = col1[r] if r < len(col1) else ""
        c2 = col2[r] if r < len(col2) else ""
        pad1 = max(4, 34 - str_width(c1))
        out.append(f"  {c1}{' ' * pad1}{c2}")
    return "\n".join(out)

def render_web_search_box(query: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render compact search result badge."""
    clean = content.strip()
    if not clean or "no search results found" in clean.lower() or clean.startswith("No matches"):
        return f"    {SLATE}└─ 🔍 No search results found for \"{query}\"{RST}"
    lines = [l for l in clean.splitlines() if l.strip()]
    count = sum(1 for l in lines if re.match(r"^\d+\.\s+", l)) or len(lines)
    return f"    {SLATE}└─ 🔍 {TEAL}Found {count} search result{'s' if count != 1 else ''}{SLATE} for \"{query}\" · loaded into context{RST}"

def render_web_fetch_box(url: str, content: str, max_width: int = 88) -> str:
    """Render compact WebFetch result badge."""
    size_kb = len(content) / 1024
    display_url = url if len(url) <= 55 else url[:52] + "..."
    if not content.strip() or content.strip() == "[Empty response]":
        return f"    {SLATE}└─ 🌐 {DIM}(Empty web response from {display_url}){RST}"
    return f"    {SLATE}└─ 🌐 {MINT}Fetched {size_kb:.1f} KB{SLATE} from {UNDER}{CYAN}{display_url}{RST}"

def render_git_box(subcmd: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render git output box with color status."""
    lines = [l for l in content.strip().splitlines() if l.strip()]
    title = f"Git {subcmd}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {MINT}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        if l.startswith("##"):
            inner.append(f"  {DARK_SLATE}│{RST} {TEAL}{BOLD}{l}{RST}")
        elif l.startswith(" M") or l.startswith("M "):
            inner.append(f"  {DARK_SLATE}│{RST} {GOLD}{l}{RST}")
        elif l.startswith("??") or l.startswith(" A") or l.startswith("A "):
            inner.append(f"  {DARK_SLATE}│{RST} {GREEN}{l}{RST}")
        elif l.startswith(" D") or l.startswith("D "):
            inner.append(f"  {DARK_SLATE}│{RST} {RED}{l}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner, head=2, tail=1, max_line_len=max_width - 8))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_symbols_box(path_str: str, content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render extracted code symbols tree."""
    lines = [l for l in content.strip().splitlines() if l.strip()]
    title = f"Code Symbols · {path_str}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {LBLUE}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        if l.startswith("📄"):
            inner.append(f"  {DARK_SLATE}│{RST} {BOLD}{TEAL}{l}{RST}")
        elif "class " in l:
            inner.append(f"  {DARK_SLATE}│{RST}   {GOLD}{l.strip()}{RST}")
        elif "def " in l or "func " in l or "fn " in l:
            inner.append(f"  {DARK_SLATE}│{RST}   {CYAN}{l.strip()}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner, head=2, tail=2, max_line_len=max_width - 8))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_tree_box(content: str, max_show: int = 4, max_width: int = 88) -> str:
    """Render full visual directory tree with clickable OSC-8 file paths."""
    lines = content.strip().splitlines()
    title = "Directory Tree"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {TEAL}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        clean_name = l.strip(" ├─└│ ")
        if l.endswith("/"):
            inner.append(f"  {DARK_SLATE}│{RST} {CYAN}{BOLD}{make_clickable_custom(l, clean_name)}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{make_clickable_custom(l, clean_name)}{RST}")
    out.extend(_head_tail(inner, head=3, tail=2, max_line_len=max_width - 8))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_http_box(method: str, url: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render compact HTTP response badge."""
    size_kb = len(content) / 1024
    display_url = url if len(url) <= 50 else url[:47] + "..."
    return f"    {SLATE}└─ 🌐 {CYAN}HTTP {method}{SLATE} {display_url} ({size_kb:.1f} KB){RST}"


def render_process_box(action: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render process or port inspection box."""
    lines = content.strip().splitlines()
    title = f"Process Info · {action}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = [f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}" for l in lines]
    out.extend(_head_tail(inner))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_env_box(action: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render environment variable inspection box."""
    lines = content.strip().splitlines()
    title = f"Environment · {action}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {TEAL}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        if "=" in l:
            k, v = l.split("=", 1)
            inner.append(f"  {DARK_SLATE}│{RST} {TEAL}{k:<20}={RST} {WHITE}{v}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_diff_tool_box(content: str, max_show: int = 3, max_width: int = 88) -> str:
    """Render file comparison diff box."""
    lines = content.strip().splitlines()
    title = "File Comparison"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {GOLD}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    inner = []
    for l in lines:
        if l.startswith("+") and not l.startswith("+++"):
            inner.append(f"  {DARK_SLATE}│{RST} {GREEN}{l}{RST}")
        elif l.startswith("-") and not l.startswith("---"):
            inner.append(f"  {DARK_SLATE}│{RST} {RED}{l}{RST}")
        elif l.startswith("@@") or l.startswith("==="):
            inner.append(f"  {DARK_SLATE}│{RST} {CYAN}{l}{RST}")
        else:
            inner.append(f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}")
    out.extend(_head_tail(inner))
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

def render_deep_research_box(query: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render ultra-compact deep research result badge."""
    clean_q = query[:45] + "..." if len(query) > 45 else query
    return f"    {SLATE}└─ 🧠 {GOLD}Deep research completed{SLATE} for \"{clean_q}\" · findings loaded into context{RST}"

def render_table_search_box(query: str, content: str, max_show: int = 2, max_width: int = 88) -> str:
    """Render compact table search result badge."""
    clean_q = query[:45] + "..." if len(query) > 45 else query
    return f"    {SLATE}└─ 📊 {CYAN}Structured table search completed{SLATE} for \"{clean_q}\"{RST}"

def render_patch_box(path_str: str, content: str, max_width: int = 88) -> str:
    """Render patch result."""
    return f"  {MINT}  └─ ✓ {content.strip()}{RST}"

def render_error_box(tool_name: str, content: str, max_width: int = 88) -> str:
    """Render error message box."""
    lines = content.strip().splitlines()
    title = f"Error · {tool_name}"
    border_w = max(10, max_width - str_width(title) - 8)
    out = [f"  {DARK_SLATE}┌── {ROSE}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
    for l in lines:
        out.append(f"  {DARK_SLATE}│{RST} {ROSE}{l}{RST}")
    out.append(f"  {DARK_SLATE}└──{'─' * max(10, max_width - 6)}{RST}")
    return "\n".join(out)

_LAST_TOOL_OUTPUT = ""

def get_last_tool_output() -> str:
    return _LAST_TOOL_OUTPUT

def _save_tool_output(content: str) -> None:
    global _LAST_TOOL_OUTPUT
    _LAST_TOOL_OUTPUT = content

def format_tool_decision_breakdown(tools: list) -> list[str]:
    """Format an informative breakdown of model-chosen tools, switches, and intent."""
    lines = []
    total = len(tools)
    for idx, tu in enumerate(tools):
        is_last = (idx == total - 1)
        tree_char = "└─" if is_last else "├─"
        name = getattr(tu, "name", "") or "Tool"
        inp = getattr(tu, "input", {}) or {}

        target_parts = []
        intent_desc = ""

        if name in ("FileTree",):
            p = inp.get("path") or "."
            d = inp.get("max_depth") or 3
            target_parts.append(f"path=\"{p}\"")
            if d:
                target_parts.append(f"max_depth={d}")
            intent_desc = "Scan directory hierarchy & files"
        elif name in ("Ls", "list_dir"):
            p = inp.get("path") or inp.get("DirectoryPath") or "."
            target_parts.append(f"path=\"{p}\"")
            intent_desc = "List directory entries"
        elif name in ("Read", "ReadFile", "view_file", "read_file"):
            p = inp.get("path") or inp.get("AbsolutePath") or ""
            target_parts.append(f"path=\"{p}\"")
            if inp.get("offset") or inp.get("StartLine"):
                target_parts.append(f"lines={inp.get('offset') or inp.get('StartLine')}-{inp.get('limit') or inp.get('EndLine') or ''}")
            intent_desc = "Inspect file contents"
        elif name in ("Write", "WriteFile", "write_to_file"):
            p = inp.get("path") or inp.get("TargetFile") or ""
            target_parts.append(f"path=\"{p}\"")
            intent_desc = "Create/write new file"
        elif name in ("Edit", "EditFile", "replace_file_content"):
            p = inp.get("path") or inp.get("TargetFile") or ""
            target_parts.append(f"path=\"{p}\"")
            intent_desc = "Apply targeted code modifications"
        elif name in ("Bash", "bash", "run_command"):
            cmd = inp.get("command") or inp.get("CommandLine") or ""
            target_parts.append(f"cmd=\"{cmd[:40]}{'...' if len(cmd) > 40 else ''}\"")
            intent_desc = "Execute shell command"
        elif name in ("Grep", "grep_search"):
            q = inp.get("pattern") or inp.get("Query") or ""
            p = inp.get("path") or inp.get("SearchPath") or ""
            target_parts.append(f"query=\"{q}\"")
            if p:
                target_parts.append(f"in=\"{p}\"")
            intent_desc = "Search codebase for pattern"
        elif name in ("Glob",):
            pat = inp.get("pattern") or "*"
            target_parts.append(f"pattern=\"{pat}\"")
            intent_desc = "Find files matching wildcard pattern"
        elif name in ("CodeSymbols",):
            p = inp.get("path") or "."
            target_parts.append(f"path=\"{p}\"")
            intent_desc = "Extract classes and functions"
        elif name in ("WebSearch",):
            q = inp.get("query") or ""
            target_parts.append(f"query=\"{q}\"")
            intent_desc = "Search web for real-time info"
        elif name in ("WebFetch",):
            url = inp.get("url") or ""
            target_parts.append(f"url=\"{url}\"")
            intent_desc = "Fetch and parse web page"
        elif name in ("Git",):
            subcmd = inp.get("subcommand") or "status"
            target_parts.append(f"subcommand=\"{subcmd}\"")
            intent_desc = "Version control operation"
        elif name in ("TodoWrite",):
            todos = inp.get("todos", [])
            target_parts.append(f"count={len(todos)}")
            intent_desc = "Update active plan checklist"
        elif name in ("Task",):
            target_parts.append("subagent=fanout")
            intent_desc = "Spawn delegated subagent task"
        else:
            intent_desc = f"Execute {name}"

        params_str = f"({', '.join(target_parts)})" if target_parts else ""
        lines.append(
            f"    {DARK_SLATE}{tree_char}{RST} {idx+1}. {CYAN}{BOLD}{name}{RST}{WHITE}{params_str}{RST} {DARK_SLATE}→{RST} {SLATE}{intent_desc}{RST}"
        )
    return lines


class Renderer:
    _tip_idx: int = 0

    def __init__(self, show_thinking: bool = False, collapsible: bool = True) -> None:
        self.show_thinking = show_thinking
        self.collapsible = collapsible
        self._thinking_active = False
        self._thinking_lines: list[str] = []
        self._thinking_start_time: float = 0.0
        self._text_active = False
        self._buffer_text = ""
        self._table_buffer: list[str] = []
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._tool_batch_count = 0
        self._tool_call_count = 0
        self._thinking_cur_line_len = 0

    def print_banner(self, version: str, model: str, effort: str, workspace: str, mode: str) -> None:
        """Render Axon unique neural core logo and session header."""
        w_path = str(workspace)
        home = str(Path.home())
        if w_path.startswith(home):
            w_path = "~" + w_path[len(home):]

        v_str = version if version.startswith("v") else f"v{version}"
        axon_logo = [
            f"  {TEAL}▲{CYAN}█{MINT}▲  {BOLD}{WHITE}Axon{RST} {DIM}{v_str}{RST}",
            f"  {TEAL}█{CYAN}⚡{MINT}█  {SLATE}{model} with {effort} effort · API Usage Billing{RST}",
            f"  {TEAL}▼{CYAN}█{MINT}▼  {DARK_SLATE}{w_path}{RST}",
        ]
        print()
        for line in axon_logo:
            print(line)
        print()

    def render_user_message(self, text: str) -> None:
        """Render user input inside a distinctive Axon dark shaded bubble bar."""
        width = max(40, term_width() - 4)
        clean_text = text.strip()
        pad_len = max(0, width - len(clean_text) - 4)
        bar = f"  {GRAY_BG}{BOLD}{WHITE} › {clean_text}{' ' * pad_len} {RST}"
        sys.stdout.write(f"{bar}\n\n")
        sys.stdout.flush()

    def _flush_table(self) -> None:
        """Render and print buffered table rows as a clean aligned box table."""
        if self._table_buffer:
            tbl_lines = render_table(self._table_buffer, max_total_width=min(term_width() - 4, 100))
            for line in tbl_lines:
                sys.stdout.write(f"{line}\n")
            sys.stdout.flush()
            self._table_buffer = []

    def _flush_all_buffers(self) -> None:
        """Flush remaining text buffer and table buffer."""
        if self._buffer_text:
            rendered = format_markdown(self._buffer_text, max_width=min(term_width() - 4, 100))
            if rendered:
                sys.stdout.write(f"{rendered}\n")
            self._buffer_text = ""
        else:
            self._flush_table()
        sys.stdout.flush()

    def on_event(self, e: StreamEvent) -> None:
        if isinstance(e, LLMCallStart):
            if e.iteration == 1:
                self._tool_call_count = 0
                self._tool_batch_count = 0
            self._flush_all_buffers()
            self._text_active = False
            self._thinking_active = False
            step_str = get_ordinal(e.iteration)
            if e.tokens_in >= 1000:
                tok_str = f" · ~{e.tokens_in/1000:.1f}k in"
            elif e.tokens_in > 0:
                tok_str = f" · ~{e.tokens_in} in"
            else:
                tok_str = ""
            msg_str = f"{e.message_count} message{'s' if e.message_count != 1 else ''} in context{tok_str}"
            if e.iteration > 1:
                sys.stdout.write(f"\n  {GOLD}⚡ LLM Call [{step_str}]{RST} {SLATE}· {BOLD}{WHITE}{e.model}{RST} {SLATE}({msg_str}){RST}\n")
                sys.stdout.write(f"    {DARK_SLATE}└─ 📥 Ingesting results from previous tool executions to conclude answer…{RST}\n\n")
            else:
                sys.stdout.write(f"\n  {GOLD}⚡ LLM Call [{step_str}]{RST} {SLATE}· {BOLD}{WHITE}{e.model}{RST} {SLATE}({msg_str})…{RST}\n")
            sys.stdout.flush()

        elif isinstance(e, ThinkingDelta):
            if not self._thinking_active:
                if self.show_thinking:
                    sys.stdout.write(f"\n  {PURPLE}{BOLD}🧠 Neural Reasoning & Plan{RST}\n  {PURPLE}│{RST} {SLATE}")
                else:
                    sys.stdout.write(f"  {PURPLE}🧠 Reasoning…{RST}\r")
                self._thinking_active = True
                self._thinking_lines = []
                self._thinking_start_time = time.time()
                self._thinking_cur_line_len = 0
            self._thinking_lines.append(e.text)
            if self.show_thinking:
                tw = term_width()
                max_line_w = max(40, min(tw - 8, 96))
                for ch in e.text:
                    if ch == "\n":
                        sys.stdout.write(f"\n  {PURPLE}│{RST} {SLATE}")
                        self._thinking_cur_line_len = 0
                    else:
                        if self._thinking_cur_line_len >= max_line_w and ch == " ":
                            sys.stdout.write(f"\n  {PURPLE}│{RST} {SLATE}")
                            self._thinking_cur_line_len = 0
                        else:
                            sys.stdout.write(ch)
                            self._thinking_cur_line_len += 1
                sys.stdout.flush()

        elif isinstance(e, TextDelta):
            if self._thinking_active:
                elapsed = max(0.1, time.time() - self._thinking_start_time)
                if self.show_thinking:
                    sys.stdout.write(f"{RST}\n  {PURPLE}└── {DIM}Reasoned for {elapsed:.1f}s · Strategy formulated{RST}\n\n")
                else:
                    full_thinking = "".join(self._thinking_lines).strip()
                    first_thought = full_thinking.split(".")[0].replace("\n", " ").strip() if full_thinking else "Analyzed context"
                    if len(first_thought) > 75:
                        first_thought = first_thought[:72] + "..."
                    sys.stdout.write(f"\033[2K\r  {PURPLE}🧠 Thought for {elapsed:.1f}s{RST} {DARK_SLATE}›{RST} {DIM}{ITALIC}{first_thought}{RST}\n\n")
                self._thinking_active = False

            self._text_active = True
            self._buffer_text += e.text

            # Process completed lines
            if "\n" in self._buffer_text:
                parts = self._buffer_text.split("\n")
                to_print = parts[:-1]
                self._buffer_text = parts[-1]

                for line in to_print:
                    stripped = line.strip()
                    if stripped.startswith("|") and ("|" in stripped[1:]):
                        self._table_buffer.append(stripped)
                    else:
                        self._flush_table()
                        rendered = format_markdown(line, max_width=min(term_width() - 4, 100))
                        sys.stdout.write(f"{rendered}\n")
                sys.stdout.flush()

        elif isinstance(e, ToolBatchStart):
            self._tool_batch_count += 1
            if self._thinking_active:
                elapsed = max(0.1, time.time() - self._thinking_start_time)
                if self.show_thinking:
                    sys.stdout.write(f"{RST}\n  {PURPLE}└── {DIM}Reasoned for {elapsed:.1f}s · Strategy formulated{RST}\n\n")
                else:
                    full_thinking = "".join(self._thinking_lines).strip()
                    first_thought = full_thinking.split(".")[0].replace("\n", " ").strip() if full_thinking else "Decided on tool actions"
                    if len(first_thought) > 75:
                        first_thought = first_thought[:72] + "..."
                    sys.stdout.write(f"\033[2K\r  {PURPLE}🧠 Thought for {elapsed:.1f}s{RST} {DARK_SLATE}›{RST} {DIM}{ITALIC}{first_thought}{RST}\n\n")
                self._thinking_active = False
            self._flush_all_buffers()

            batch_str = get_ordinal(self._tool_batch_count)
            sys.stdout.write(f"\n  {GOLD}{BOLD}⚡ Tool Execution Plan [{batch_str}]{RST} {SLATE}(LLM selected {e.total_count} action{'s' if e.total_count != 1 else ''}):{RST}\n")
            plan_lines = format_tool_decision_breakdown(e.tools)
            for pl in plan_lines:
                sys.stdout.write(f"{pl}\n")
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._text_active = False

        elif isinstance(e, ToolUseStart):
            if self._thinking_active:
                elapsed = max(0.1, time.time() - self._thinking_start_time)
                if self.show_thinking:
                    sys.stdout.write(f"{RST}\n  {PURPLE}└── {DIM}Reasoned for {elapsed:.1f}s · Strategy formulated{RST}\n\n")
                else:
                    full_thinking = "".join(self._thinking_lines).strip()
                    first_thought = full_thinking.split(".")[0].replace("\n", " ").strip() if full_thinking else "Decided on tool actions"
                    if len(first_thought) > 75:
                        first_thought = first_thought[:72] + "..."
                    sys.stdout.write(f"\033[2K\r  {PURPLE}🧠 Thought for {elapsed:.1f}s{RST} {DARK_SLATE}›{RST} {DIM}{ITALIC}{first_thought}{RST}\n\n")
                self._thinking_active = False
            self._flush_all_buffers()
            self._text_active = False

        elif isinstance(e, ToolArgsDelta):
            pass

        elif isinstance(e, ToolUseComplete):
            self._text_active = False

        elif isinstance(e, ToolExecutionStart):
            self._tool_call_count += 1
            tool_idx_str = get_ordinal(self._tool_call_count)
            self._active_tools[e.id] = {**e.input, "_tool_idx": self._tool_call_count}
            name = e.name
            t_input = e.input

            prefix = f"  {GOLD}🛠️ Tool Action [{tool_idx_str}]{RST} {DARK_SLATE}·{RST} {CYAN}{BOLD}{name}{RST} {SLATE}❯{RST}"

            if name in ("Bash", "bash", "run_command"):
                cmd = t_input.get("command") or t_input.get("CommandLine") or ""
                desc = t_input.get("description") or ""
                desc_str = f" {SLATE}({desc}){RST}" if desc else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Executing:{RST} {CYAN}{BOLD}$ {cmd}{RST}{desc_str}\n")
            elif name in ("Read", "ReadFile", "view_file", "read_file"):
                p = t_input.get("path") or t_input.get("AbsolutePath") or ""
                offset = t_input.get("offset") or t_input.get("StartLine")
                limit = t_input.get("limit") or t_input.get("EndLine")
                range_str = f" #L{offset}-{limit}" if offset and limit else (f" #L{offset}+" if offset else "")
                icon = get_file_icon(p)
                link = make_clickable_link(p, range_str)
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Inspecting file:{RST} {icon} {WHITE}{link}{RST}\n")
            elif name in ("Write", "WriteFile", "write_to_file"):
                p = t_input.get("path") or t_input.get("TargetFile") or ""
                code_c = t_input.get("content") or t_input.get("CodeContent") or ""
                added = len(code_c.splitlines()) if code_c else 0
                icon = get_file_icon(p)
                link = make_clickable_link(p)
                diff_stat = f" {MINT}+{added} lines{RST}" if added > 0 else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Creating file:{RST} {icon} {WHITE}{link}{RST}{diff_stat}\n")
            elif name in ("Edit", "EditFile", "replace_file_content"):
                p = t_input.get("path") or t_input.get("TargetFile") or ""
                old_s = t_input.get("old_string", "")
                new_s = t_input.get("new_string", "")
                added = len(new_s.splitlines()) if new_s else 0
                removed = len(old_s.splitlines()) if old_s else 0
                icon = get_file_icon(p)
                link = make_clickable_link(p)
                diff_stat = f" {MINT}+{added}{RST} {ROSE}-{removed}{RST}" if (added or removed) else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Modifying file:{RST} {icon} {WHITE}{link}{RST}{diff_stat}\n")
            elif name in ("MultiEdit",):
                p = t_input.get("path") or t_input.get("TargetFile") or ""
                edits = t_input.get("edits", [])
                icon = get_file_icon(p)
                link = make_clickable_link(p)
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Applying multiple edits to:{RST} {icon} {WHITE}{link}{RST} {SLATE}({len(edits)} edits){RST}\n")
            elif name in ("Ls", "list_dir"):
                p = t_input.get("path") or t_input.get("DirectoryPath") or "."
                link = make_clickable_link(p)
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Listing directory:{RST} 📁 {WHITE}{link}{RST}\n")
            elif name in ("FileTree",):
                p = t_input.get("path") or "."
                max_d = t_input.get("max_depth") or 3
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Scanning project tree:{RST} 📁 {WHITE}{p}{RST} {SLATE}(depth: {max_d}){RST}\n")
            elif name in ("Glob",):
                pattern = t_input.get("pattern") or "*"
                base_p = t_input.get("path") or ""
                base_str = f" in {base_p}" if base_p else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Matching files by pattern:{RST} 📁 {CYAN}{pattern}{RST}{SLATE}{base_str}{RST}\n")
            elif name in ("Grep", "grep_search"):
                pattern = t_input.get("pattern") or t_input.get("Query") or ""
                search_p = t_input.get("path") or t_input.get("SearchPath") or ""
                in_str = f" in {search_p}" if search_p else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Searching codebase for:{RST} {MINT}\"{pattern}\"{RST}{SLATE}{in_str}{RST}\n")
            elif name in ("CodeSymbols",):
                p = t_input.get("path") or "."
                icon = get_file_icon(p)
                link = make_clickable_link(p)
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Parsing code symbols from:{RST} {icon} {WHITE}{link}{RST}\n")
            elif name in ("Git",):
                subcmd = t_input.get("subcommand") or "status"
                extra = t_input.get("args") or ""
                extra_str = f" {WHITE}{extra}{RST}" if extra else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Running git {subcmd}:{RST}{extra_str}\n")
            elif name in ("Patch",):
                p = t_input.get("path") or ""
                icon = get_file_icon(p)
                link = make_clickable_link(p)
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Applying unified patch to:{RST} {icon} {WHITE}{link}{RST}\n")
            elif name in ("WebSearch",):
                q = t_input.get("query") or ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Searching the web for:{RST} 🔍 {CYAN}\"{q}\"{RST}\n")
            elif name in ("WebFetch",):
                url = t_input.get("url") or ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Fetching content from:{RST} 🌐 {WHITE}{url}{RST}\n")
            elif name in ("Http",):
                method = (t_input.get("method") or "GET").upper()
                url = t_input.get("url") or ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Dispatching HTTP {method} to:{RST} 🌐 {CYAN}{url}{RST}\n")
            elif name in ("Process",):
                action = t_input.get("action") or "list"
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Inspecting system processes:{RST} {SLATE}(action: {action}){RST}\n")
            elif name in ("Env",):
                action = t_input.get("action") or "list"
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Inspecting environment:{RST} {SLATE}(action: {action}){RST}\n")
                var_n = t_input.get("variable", "")
                var_str = f" ({var_n})" if var_n else ""
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Inspected environment variables:{RST} {SLATE}(action: {action}{var_str}){RST}\n")
            elif name in ("Diff",):
                pa = t_input.get("path_a", "")
                pb = t_input.get("path_b", "")
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Compared files:{RST} 📄 {pa} ↔ {pb}\n")
            elif name in ("TodoWrite",):
                todos = t_input.get("todos", [])
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Updated active task plan checklist:{RST} {SLATE}({len(todos)} items){RST}\n")
            elif name in ("Task",):
                prompt = t_input.get("prompt", "")
                preview = prompt.splitlines()[0][:50] if prompt else "Subtask"
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Spawned subagent for task:{RST} ⚡ {SLATE}\"{preview}\"{RST}\n")
            elif name in ("Doctor",):
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Executed system environment health check{RST}\n")
            elif name in ("ExitPlanMode",):
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Proposed implementation plan for approval{RST}\n")
            elif name in ("DeepResearch",):
                q = t_input.get("query", "")
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Initiated deep research on:{RST} 🧠 {GOLD}\"{q}\"{RST}\n")
            elif name in ("TableSearch",):
                q = t_input.get("query", "")
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Searched structured tables for:{RST} 📊 {CYAN}\"{q}\"{RST}\n")
            else:
                sys.stdout.write(f"{prefix} {WHITE}{BOLD}Executed {name}{RST}\n")
            sys.stdout.flush()

        elif isinstance(e, ToolExecutionResult):
            _save_tool_output(e.content)
            width = min(88, max(50, term_width() - 4))
            name = e.name
            t_input = {**self._active_tools.get(e.id, {}), **e.input}
            content = e.content

            if e.is_error:
                sys.stdout.write(f"{render_error_box(name, content, max_width=width)}\n\n")
            elif name in ("Read", "ReadFile", "view_file", "read_file"):
                p = t_input.get("path") or t_input.get("AbsolutePath") or ""
                sys.stdout.write(f"{render_read_box(p, content, max_show=5, max_width=width)}\n\n")
            elif name in ("Bash", "bash", "run_command"):
                sys.stdout.write(f"{render_bash_box(content, max_show=5, max_width=width)}\n\n")
            elif name in ("Ls", "list_dir"):
                p = t_input.get("path") or t_input.get("DirectoryPath") or "."
                sys.stdout.write(f"{render_ls_box(p, content, max_show=5, max_width=width)}\n\n")
            elif name in ("Glob",):
                pattern = t_input.get("pattern") or "*"
                sys.stdout.write(f"{render_glob_box(pattern, content, max_show=5, max_width=width)}\n\n")
            elif name in ("Grep", "grep_search"):
                pattern = t_input.get("pattern") or t_input.get("Query") or ""
                sys.stdout.write(f"{render_grep_box(pattern, content, max_show=5, max_width=width)}\n\n")
            elif name in ("WebSearch",):
                q = t_input.get("query") or ""
                sys.stdout.write(f"{render_web_search_box(q, content, max_show=3, max_width=width)}\n\n")
            elif name in ("WebFetch",):
                url = t_input.get("url") or "Web"
                sys.stdout.write(f"{render_web_fetch_box(url, content, max_width=width)}\n\n")
            elif name in ("DeepResearch",):
                q = t_input.get("query") or ""
                sys.stdout.write(f"{render_deep_research_box(q, content, max_show=3, max_width=width)}\n\n")
            elif name in ("TableSearch",):
                q = t_input.get("query") or ""
                sys.stdout.write(f"{render_table_search_box(q, content, max_show=3, max_width=width)}\n\n")
            elif name in ("Git",):
                subcmd = t_input.get("subcommand") or "status"
                sys.stdout.write(f"{render_git_box(subcmd, content, max_show=5, max_width=width)}\n\n")
            elif name in ("CodeSymbols",):
                p = t_input.get("path") or "."
                sys.stdout.write(f"{render_symbols_box(p, content, max_show=6, max_width=width)}\n\n")
            elif name in ("FileTree",):
                sys.stdout.write(f"{render_tree_box(content, max_show=6, max_width=width)}\n\n")
            elif name in ("Patch",):
                p = t_input.get("path") or ""
                sys.stdout.write(f"{render_patch_box(p, content, max_width=width)}\n\n")
            elif name in ("Http",):
                method = (t_input.get("method") or "GET").upper()
                url = t_input.get("url") or ""
                sys.stdout.write(f"{render_http_box(method, url, content, max_show=4, max_width=width)}\n\n")
            elif name in ("Process",):
                action = t_input.get("action") or "list"
                sys.stdout.write(f"{render_process_box(action, content, max_show=4, max_width=width)}\n\n")
            elif name in ("Env",):
                action = t_input.get("action") or "list"
                sys.stdout.write(f"{render_env_box(action, content, max_show=4, max_width=width)}\n\n")
            elif name in ("Diff",):
                sys.stdout.write(f"{render_diff_tool_box(content, max_show=5, max_width=width)}\n\n")
            elif name in ("Edit", "EditFile", "MultiEdit", "replace_file_content"):
                p = t_input.get("path") or t_input.get("TargetFile") or ""
                old_s = t_input.get("old_string", "")
                new_s = t_input.get("new_string", "")
                filename = Path(p).name if p else ""
                if old_s and new_s:
                    diff_rendered = render_diff_box(old_s, new_s, filename=filename, max_width=width, max_show=5)
                    if diff_rendered:
                        sys.stdout.write(f"{diff_rendered}\n")
                sys.stdout.write(f"  {MINT}  └─ ✓ Applied edits to {p}{RST}\n\n")
            elif name in ("Write", "WriteFile", "write_to_file"):
                p = t_input.get("path") or t_input.get("TargetFile") or ""
                code_c = t_input.get("content") or t_input.get("CodeContent") or ""
                stream_write_box(p, code_c, max_show=10, max_width=width)
            elif name in ("Doctor",):
                sys.stdout.write(f"{render_doctor_box(content, max_width=width)}\n\n")
            elif name in ("TodoWrite",):
                sys.stdout.write(f"{render_todo_box(content, max_width=width)}\n\n")
            elif name in ("ExitPlanMode",):
                sys.stdout.write(f"  {DARK_SLATE}┌── {GOLD}Plan Proposed for Approval{DARK_SLATE} {'─' * max(10, width - 32)}{RST}\n")
                lines = content.strip().splitlines()
                inner = [f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}" for l in lines]
                out_plan = _head_tail(inner, head=5, tail=2, max_line_len=width - 8)
                for l in out_plan:
                    sys.stdout.write(f"{l}\n")
                sys.stdout.write(f"  {DARK_SLATE}└──{'─' * max(10, width - 6)}{RST}\n\n")
            elif name in ("Task",):
                sys.stdout.write(f"{render_subagent_result_card(content, width=width)}\n\n")
            else:
                lines = content.strip().splitlines()
                if len(lines) <= 2:
                    sys.stdout.write(f"  {SLATE}  └─ {content.strip()}{RST}\n\n")
                else:
                    title = f"Result · {name}"
                    border_w = max(10, width - str_width(title) - 8)
                    out = [f"  {DARK_SLATE}┌── {SLATE}{title}{DARK_SLATE} {'─' * border_w}{RST}"]
                    inner = [f"  {DARK_SLATE}│{RST} {WHITE}{l}{RST}" for l in lines]
                    out.extend(_head_tail(inner, head=3, tail=2, max_line_len=width - 8))
                    out.append(f"  {DARK_SLATE}└──{'─' * max(10, width - 6)}{RST}")
                    sys.stdout.write(f"{chr(10).join(out)}\n\n")
            sys.stdout.flush()

        elif isinstance(e, TurnComplete):
            self._flush_all_buffers()
            self._thinking_active = False
            self._text_active = False
            sys.stdout.write("\n")
            sys.stdout.flush()

    def status_line(self, model: str, cost: float, workspace: str, mode: str) -> None:
        pass

    def turn_footer(self, tool_count: int, usage: Any, cost: float, elapsed: float, llm_calls: int = 1) -> None:
        in_t = usage.input
        out_t = usage.output
        now_str = datetime.now().strftime("%I:%M %p").lstrip("0")
        verb = "Worked" if tool_count > 0 else "Thought"

        in_fmt = f"{in_t/1000:.1f}k" if in_t >= 1000 else f"{in_t}"
        out_fmt = f"{out_t/1000:.1f}k" if out_t >= 1000 else f"{out_t}"
        llm_str = f"{llm_calls} LLM call{'s' if llm_calls != 1 else ''}"
        tool_str = f"{tool_count} tool call{'s' if tool_count != 1 else ''}"

        sys.stdout.write(f"\n  {PURPLE}✻{RST} {SLATE}{verb} for {elapsed:.1f}s · done {now_str}{RST}\n")
        sys.stdout.write(
            f"  {DARK_SLATE}└─ {CYAN}{llm_str}{DARK_SLATE} · "
            f"{MINT}{tool_str}{DARK_SLATE} · "
            f"{WHITE}{in_fmt} in{DARK_SLATE} · "
            f"{WHITE}{out_fmt} out{DARK_SLATE} · "
            f"{GOLD}${cost:.4f}{DARK_SLATE} · "
            f"{SLATE}{elapsed:.1f}s{RST}\n"
        )
        tips = [
            f"💡 Tip: Use {GOLD}!{SLATE} to execute direct shell commands (e.g. {GOLD}!pytest{SLATE}, {GOLD}!git status{SLATE}, {GOLD}!npm test{SLATE}) instantly",
            f"💡 Tip: Press {GOLD}Ctrl+V{SLATE} or drag & drop screenshots into terminal to attach images {CYAN}[Image #1]{SLATE} with automatic OCR",
            f"💡 Tip: Type {GOLD}/model{SLATE} to switch models (e.g. {GOLD}/model deepseek-v4-flash{SLATE} for speed, {GOLD}/model claude-opus-5{SLATE} for vision)",
            f"💡 Tip: Type {GOLD}/effort{SLATE} to toggle reasoning depth between {CYAN}low{SLATE}, {CYAN}medium{SLATE}, {CYAN}high{SLATE}, and {CYAN}quantum{SLATE}",
            f"💡 Tip: Press {GOLD}Tab{SLATE} in the prompt bar to switch permission modes ({GOLD}bypass{SLATE} · {MINT}auto-accept{SLATE} · {AMBER}manual{SLATE} · {PURPLE}plan{SLATE})",
            f"💡 Tip: Press {GOLD}← Left Arrow{SLATE} on an empty prompt to open the interactive Session Switcher dashboard",
            f"💡 Tip: Type {GOLD}/learn <rule>{SLATE} to save project conventions, or {GOLD}/learn --global <rule>{SLATE} for universal cross-repo memory",
            f"💡 Tip: Type {GOLD}/memory{SLATE} to inspect, search, and manage persistent learned rules and preferences",
            f"💡 Tip: Type {GOLD}/research <topic>{SLATE} to run multi-source deep research and generate comparative matrix tables",
            f"💡 Tip: Type {GOLD}/todo{SLATE} to inspect or manage structured execution checklists with visual progress bars",
            f"💡 Tip: Type {GOLD}/q <prompt>{SLATE} to enqueue follow-up prompts for autonomous batch execution",
            f"💡 Tip: Type {GOLD}/review{SLATE} or {GOLD}/review <path>{SLATE} to run automated multi-file code review on logic, security, and performance",
            f"💡 Tip: Type {GOLD}/diff{SLATE} to view uncommitted working tree git diffs across the entire workspace",
            f"💡 Tip: Type {GOLD}/rewind{SLATE} to roll back and revert file modifications made during previous agent turns",
            f"💡 Tip: Type {GOLD}/payload{SLATE} to view active system prompt blocks and exact token counts",
            f"💡 Tip: Type {GOLD}/cost{SLATE} to inspect session token ledger, token usage, and real-time API billing",
            f"💡 Tip: Type {GOLD}/compact{SLATE} to manually compact conversation context and free token budget while retaining key facts",
            f"💡 Tip: Type {GOLD}/window <N>{SLATE} to adjust the sliding context window (e.g. {GOLD}/window 10{SLATE} to retain latest 10 turns)",
            f"💡 Tip: Type {GOLD}/skills{SLATE} to browse active skills, {GOLD}/skill create <name>{SLATE} to scaffold new skills, or {GOLD}/skill import <url>{SLATE} to install",
            f"💡 Tip: Type {GOLD}@{SLATE} followed by a filename to fuzzy-search and link project files directly into your prompt",
            f"💡 Tip: Type {GOLD}/branch{SLATE} to fork the current conversation into an independent parallel exploration branch",
            f"💡 Tip: Type {GOLD}/resume{SLATE} to resume any historical conversation session from its JSONL transcript",
            f"💡 Tip: Type {GOLD}/ask <question>{SLATE} to ask a side question in an isolated context without polluting main chat history",
            f"💡 Tip: Type {GOLD}/subagents{SLATE} to inspect parallel subagent worker execution matrix and isolated transcripts",
            f"💡 Tip: Type {GOLD}/mcp{SLATE} to inspect Model Context Protocol server connections, schemas, and external tools",
            f"💡 Tip: Type {GOLD}/hooks{SLATE} to inspect active lifecycle, pre-tool, and post-tool execution hooks",
            f"💡 Tip: Type {GOLD}/doctor{SLATE} to run local environment diagnostics, proxy endpoint latency tests, and tool checks",
            f"💡 Tip: Type {GOLD}/init{SLATE} to initialize an {WHITE}AGENTS.md{SLATE} or {WHITE}axon.md{SLATE} conventions guide in the current workspace",
            f"💡 Tip: Press {GOLD}Ctrl+U{SLATE} to clear the prompt line, and {GOLD}Ctrl+C{SLATE} to cancel a running turn gracefully",
            f"💡 Tip: Type {GOLD}?{SLATE} or {GOLD}/kb{SLATE} to open the full keyboard shortcuts and commands cheatsheet",
        ]
        chosen_tip = tips[Renderer._tip_idx % len(tips)]
        Renderer._tip_idx += 1
        sys.stdout.write(f"\n  {SLATE}{chosen_tip}{RST}\n\n")
        sys.stdout.flush()

def render_subagent_dashboard(tasks: list[Any]) -> None:
    """Render a clean Claude-style subagent fan-out progress dashboard."""
    if not tasks:
        return

    width = min(84, term_width() - 4)
    total = len(tasks)
    completed = sum(1 for t in tasks if t.status in ("completed", "exhausted", "error"))
    pct = int((completed / total * 100)) if total > 0 else 0

    tot_sub_tok = sum(getattr(t, "tokens_consumed", 0) for t in tasks)
    tok_badge = f" · Total Subagent Tokens: {tot_sub_tok:,}" if tot_sub_tok > 0 else ""

    header = f"┌── ⚡ Subagent Fan-Out ({total} parallel tasks){tok_badge} "
    header_pad = max(2, width - len(header) + 3)

    sys.stdout.write(f"\n  {GOLD}{BOLD}{header}{'─' * header_pad}┐{RST}\n")
    ratio_str = f"Progress: {completed}/{total} completed ({pct}%)"
    sub_pad = max(2, width - len(ratio_str) - 6)
    sys.stdout.write(f"  {GOLD}│{RST}  {TEAL}{BOLD}{ratio_str}{RST}{' ' * sub_pad}{GOLD}│{RST}\n")
    sys.stdout.write(f"  {GOLD}├{'─' * (width - 2)}┤{RST}\n")

    from axon.ui.theme import strip_ansi
    for t in tasks:
        tok_str = f" · {t.tokens_consumed:,} tokens" if getattr(t, "tokens_consumed", 0) > 0 else ""
        if t.status == "completed":
            badge = f"{MINT}[✓]{RST}"
            stat_str = f"{SLATE}· {t.steps} steps · {t.elapsed_s:.1f}s{tok_str} · Done{RST}"
        elif t.status == "exhausted":
            badge = f"{AMBER}[!]{RST}"
            stat_str = f"{AMBER}· {t.steps} steps · Ceiling Hit{tok_str}{RST}"
        elif t.status == "error":
            badge = f"{ROSE}[✗]{RST}"
            stat_str = f"{ROSE}· Error: {t.error_msg or 'Failed'}{RST}"
        else:
            badge = f"{CYAN}[▶]{RST}"
            stat_str = f"{CYAN}· step {t.steps}/{t.max_steps} · Running...{RST}"

        name_str = f"Subagent {t.index}: {t.title}"
        if len(name_str) > 34:
            name_str = name_str[:31] + "..."
        line_content = f"  {badge} {WHITE}{BOLD}{name_str:<34}{RST} {stat_str}"
        vis_len = len(strip_ansi(line_content))
        row_pad = max(2, width - vis_len - 3)
        sys.stdout.write(f"  {GOLD}│{RST}{line_content}{' ' * row_pad}{GOLD}│{RST}\n")

    sys.stdout.write(f"  {GOLD}└{'─' * (width - 2)}┘{RST}\n")
    sys.stdout.write(f"  {DARK_SLATE}› Press {BOLD}↓{RST}{DARK_SLATE} (down arrow) or type {BOLD}/subagents{RST}{DARK_SLATE} to inspect isolated transcripts{RST}\n\n")
    sys.stdout.flush()

def render_subagent_transcript(task: Any, show_all: bool = True) -> None:
    """Render the complete isolated transcript and tool calls of a subagent."""
    width = min(84, term_width() - 4)
    print(f"\n{GOLD}{'═' * width}{RST}")
    print(f"  {CYAN}{BOLD}🔍 Viewing Subagent #{task.index}: {task.title}{RST}")
    status_clr = MINT if task.status == "completed" else (AMBER if task.status == "exhausted" else ROSE)
    tok_disp = f"  {SLATE}|  Tokens:{RST} {getattr(task, 'tokens_consumed', 0):,}" if getattr(task, "tokens_consumed", 0) > 0 else ""
    print(f"  {SLATE}Status:{RST} {status_clr}{task.status.upper()}{RST}  {SLATE}|  Steps:{RST} {task.steps}/{task.max_steps}  {SLATE}|  Duration:{RST} {task.elapsed_s:.1f}s{tok_disp}")
    print(f"  {SLATE}Task Prompt:{RST} {WHITE}{task.prompt}{RST}")
    print(f"{GOLD}{'─' * width}{RST}")

    msgs = getattr(task.conversation, "messages", [])
    if msgs:
        for idx, m in enumerate(msgs, 1):
            role = m.get("role", "unknown").upper()
            clr = TEAL if role == "USER" else (GOLD if role == "ASSISTANT" else LBLUE)
            content = m.get("content", "")
            print(f"\n  {clr}{BOLD}[Subagent Msg {idx}] {role}:{RST}")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict):
                        b_type = b.get("type", "")
                        if b_type == "tool_use":
                            print(f"    {DARK_SLATE}⏺ ToolUse:{RST} {b.get('name')}({b.get('input')})")
                        elif b_type == "tool_result":
                            c_prev = str(b.get("content", "")).strip().replace("\n", " ")[:120]
                            print(f"    {DARK_SLATE}└─ Result [{b.get('tool_use_id')}]:{RST} {c_prev}...")
                        elif b_type == "text":
                            print(f"    {WHITE}{b.get('text', '')}{RST}")
            else:
                txt = str(content)
                lines = txt.splitlines()
                if not show_all and len(lines) > 60:
                    print(f"  {WHITE}" + "\n  ".join(lines[:30]) + f"\n\n  {SLATE}... ({len(lines)-60} lines hidden · Read all with /subagents {task.index} all) ...{RST}\n\n  " + "\n  ".join(lines[-30:]) + f"{RST}")
                else:
                    print(f"  {WHITE}" + "\n  ".join(lines) + f"{RST}")
    elif task.result_text:
        from axon.ui.markdown import format_markdown
        print(f"\n  {CYAN}{BOLD}Final Result:{RST}\n")
        print(format_markdown(task.result_text, max_width=width))

    print(f"\n{GOLD}{'═' * width}{RST}")
    print(f"  {TEAL}Type {BOLD}/main{RST}{TEAL} or press Esc to return to Main Agent chat.{RST}\n")

def render_subagent_result_card(content: str, width: int = 84) -> str:
    """Format and organize subagent execution results into a sleek, rich markdown card."""
    from axon.ui.markdown import format_markdown, str_width
    raw = content.strip()

    sub_idx = ""
    task_title = ""
    token_info = ""
    body = raw

    # Match [Subagent #1 Result (Verify ...)]: or [Subagent #1 Result (Verify ...) | 1,420 tokens]:
    header_m = re.match(r"^\[Subagent #(\d+)\s*(?:Result\s*)?(?:\((.*?)\))?(?:\s*\|\s*(.*?))?\]:\s*\n?(.*)", raw, re.DOTALL)
    if header_m:
        sub_idx = f"#{header_m.group(1)}"
        task_title = (header_m.group(2) or "").strip()
        token_info = (header_m.group(3) or "").strip()
        body = header_m.group(4).strip()
    else:
        # Check failed or ceiling match
        header_m2 = re.match(r"^\[Subagent #(\d+)\s*(.*?)(?:\s*\((.*?)\))?(?:\s*\|\s*(.*?))?\]:\s*(.*)", raw, re.DOTALL)
        if header_m2:
            sub_idx = f"#{header_m2.group(1)}"
            task_title = (header_m2.group(3) or header_m2.group(2) or "").strip()
            token_info = (header_m2.group(4) or "").strip()
            body = header_m2.group(5).strip() or header_m2.group(2)

    # Clean up verbose prefixes in task title
    if task_title.startswith("Verify the file "):
        task_title = task_title.replace("Verify the file ", "Verify ")
    elif task_title.startswith("Explore the workspace at "):
        task_title = task_title.replace("Explore the workspace at ", "Explore ")
    elif task_title.startswith("Look at "):
        task_title = task_title.replace("Look at ", "Inspect ")
    if len(task_title) > 34:
        task_title = task_title[:32] + "…"

    is_fail = "failed" in raw.lower() or "error" in raw.lower() or "✗" in raw
    status_badge = f"{ROSE}✗ Failed{RST}" if is_fail else f"{MINT}✓ Completed{RST}"
    if token_info:
        short_tok = token_info.split("(")[0].strip()
        status_badge = f"{status_badge}{DARK_SLATE} · {GOLD}{short_tok}{RST}"

    title_part = f"🤖 SUBAGENT RESULT · Subagent {sub_idx}" + (f" ({task_title})" if task_title else "")
    top_header = f"  {DARK_SLATE}╭── {CYAN}{BOLD}{title_part}{RST} {DARK_SLATE}─── [{status_badge}{DARK_SLATE}]"
    rem_w = max(4, width - str_width(top_header) - 2)
    top_border = f"{top_header} " + ("─" * rem_w) + f"╮{RST}"

    formatted_body = format_markdown(body, max_width=max(40, width - 6))
    body_lines = formatted_body.splitlines()

    out_lines = [top_border]
    for line in body_lines:
        out_lines.append(f"  {DARK_SLATE}│{RST}  {line}")

    if token_info:
        foot_label = f"Subagent {sub_idx} Consumed: {token_info}"
        foot_rem = max(4, width - str_width(foot_label) - 8)
        bottom_border = f"  {DARK_SLATE}╰── {SLATE}{foot_label}{DARK_SLATE} " + ("─" * foot_rem) + f"╯{RST}"
    else:
        bottom_border = f"  {DARK_SLATE}╰──" + ("─" * max(4, width - 6)) + f"╯{RST}"
    out_lines.append(bottom_border)

    return "\n".join(out_lines)
