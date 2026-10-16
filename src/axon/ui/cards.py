"""
Result cards for the Axon timeline: how files, diffs, searches and command output look in the terminal.

Every function returns plain rows (styled strings, no trailing newline) that fit the given ``width``;
the renderer puts them behind its rail.  The same functions draw the live timeline, ``/inspect``
(format_trace), restored conversations (format_conversation_replay) and the transcript viewer, so
history looks exactly like it did live.

* ``code_card``    - ``✚ Created src/cart.py · ■ Python · 42 lines · 1.2 KB`` + numbered, highlighted body
* ``diff_rows``    - real file line numbers, hunk separators, tinted +/- rows with intra-line word highlights
* ``grep_rows``    - matches grouped by file, ``line:`` numbers, the match itself highlighted
* ``column_rows``  - compact multi-column listing (Glob / Ls), grouped by directory
* ``json_rows``    - pretty-printed, highlighted JSON
* ``test_summary`` - pytest / jest / vitest / mocha / go test / cargo / unittest result counts + a pass/fail bar
* ``output_rows``  - command output with errors / warnings / tracebacks tinted (head + tail when long)

Highlighting work is capped to the rows actually shown, so a 10k-line result renders instantly.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from axon.ui.markdown import (
    _join_spans,
    file_uri,
    highlight,
    highlight_block,
    highlight_spans,
    lang_for_path,
)
from axon.ui import theme as _theme
from axon.ui.theme import (
    ACCENT,
    BOLD,
    CYAN,
    DIFF_ADD,
    DIFF_ADD_BG,
    DIFF_DEL,
    DIFF_DEL_BG,
    DIM,
    ERROR,
    INFO,
    MUTED,
    PRIMARY,
    RST,
    SUBTLE,
    SUCCESS,
    TEXT,
    WARNING,
    display_width,
    glyph,
    hard_wrap_ansi,
    hyperlink,
    pad,
    truncate,
)

# ─── Languages ─────────────────────────────────────────────────────────────
# extension -> (label, palette colour name)
_LANG_LABELS: dict[str, tuple[str, str]] = {
    "py": ("Python", "SKY"), "pyi": ("Python", "SKY"), "ipynb": ("Notebook", "ORANGE"),
    "js": ("JavaScript", "WARNING"), "mjs": ("JavaScript", "WARNING"), "cjs": ("JavaScript", "WARNING"),
    "jsx": ("JSX", "CYAN"), "ts": ("TypeScript", "INFO"), "tsx": ("TSX", "INFO"),
    "json": ("JSON", "AMBER"), "jsonl": ("JSON Lines", "AMBER"), "yaml": ("YAML", "CORAL"), "yml": ("YAML", "CORAL"),
    "toml": ("TOML", "TERRACOTTA"), "ini": ("INI", "MUTED"), "cfg": ("Config", "MUTED"), "env": ("Env", "MUTED"),
    "sh": ("Shell", "EMERALD"), "bash": ("Bash", "EMERALD"), "zsh": ("Zsh", "EMERALD"), "fish": ("Fish", "EMERALD"),
    "ps1": ("PowerShell", "INFO"), "bat": ("Batch", "MUTED"), "cmd": ("Batch", "MUTED"),
    "go": ("Go", "CYAN"), "rs": ("Rust", "TERRACOTTA"), "c": ("C", "INFO"), "h": ("C header", "INFO"),
    "cpp": ("C++", "INFO"), "cc": ("C++", "INFO"), "hpp": ("C++ header", "INFO"), "cs": ("C#", "INDIGO"),
    "java": ("Java", "ORANGE"), "kt": ("Kotlin", "INDIGO"), "swift": ("Swift", "ORANGE"), "rb": ("Ruby", "ERROR"),
    "php": ("PHP", "LAVENDER"), "lua": ("Lua", "INDIGO"), "r": ("R", "INFO"), "scala": ("Scala", "ERROR"),
    "dart": ("Dart", "SKY"), "zig": ("Zig", "AMBER"),
    "html": ("HTML", "ORANGE"), "htm": ("HTML", "ORANGE"), "xml": ("XML", "ORANGE"), "svg": ("SVG", "ORANGE"),
    "vue": ("Vue", "EMERALD"), "svelte": ("Svelte", "ORANGE"),
    "css": ("CSS", "INDIGO"), "scss": ("SCSS", "CORAL"), "sass": ("Sass", "CORAL"), "less": ("Less", "INDIGO"),
    "sql": ("SQL", "AMBER"), "md": ("Markdown", "TEXT"), "mdx": ("MDX", "TEXT"), "rst": ("reST", "TEXT"),
    "txt": ("Text", "MUTED"), "csv": ("CSV", "EMERALD"), "lock": ("Lockfile", "MUTED"),
}
_NAME_LABELS = {"dockerfile": ("Dockerfile", "INFO"), "makefile": ("Makefile", "TERRACOTTA"),
                "containerfile": ("Containerfile", "INFO")}


def lang_info(path: str) -> tuple[str, str, str]:
    """(highlighter language, display label, colour code) for a file path."""
    name = Path(str(path or "")).name.lower()
    ext = Path(name).suffix.lstrip(".")
    label, color_name = _NAME_LABELS.get(name) or _LANG_LABELS.get(ext) or (ext.upper() if ext else "", "MUTED")
    return lang_for_path(path), label, getattr(_theme, color_name, "") or ""


def lang_badge(path: str) -> str:
    """'■ Python' in the language's colour ('' for an unknown extension)."""
    _, label, color = lang_info(path)
    if not label:
        return ""
    chip = glyph("chip")
    return f"{color}{chip + ' ' if chip else ''}{BOLD}{label}{RST}"


# ─── Small helpers ─────────────────────────────────────────────────────────

def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def fmt_bytes(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB")


def count_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def more_row(n: int, hint: str, indent: str = "", unit: str = "line") -> str:
    """'… +12 more lines · /output 7 to view all'."""
    tail = f" {glyph('dot')} {hint} to view all" if hint else ""
    return f"{indent}{SUBTLE}{glyph('ellipsis')} +{n} more {unit if n == 1 else unit + 's'}{tail}{RST}"


def _path_link(path: str, label: str | None = None) -> str:
    return hyperlink(file_uri(path), label if label is not None else path) if path else (label or "")


def _with_bg(styled: str, bg: str) -> str:
    """Keep *bg* active across the resets inside *styled* (highlighted code on a tinted row)."""
    if not bg or not RST:
        return styled
    return bg + styled.replace(RST, RST + bg)


# ─── Code card (Write / Read previews) ─────────────────────────────────────

def numbered_rows(lines: list[str], lang: str, width: int, start: int = 1, num_w: int | None = None,
                  bg: str = "") -> list[str]:
    """'  12 │ code' rows: highlighted, long lines wrapped under the gutter (never truncated)."""
    num_w = num_w or max(2, len(str(start + max(0, len(lines) - 1))))
    bar = glyph("v")
    text_w = max(8, width - num_w - 3)
    mark = glyph("wrap") + " "
    out: list[str] = []
    for i, hl in enumerate(highlight_block([ln.expandtabs(4) for ln in lines], lang)):
        parts = hard_wrap_ansi(hl, max(4, text_w - display_width(mark)), first_width=text_w) if hl else [""]
        for j, part in enumerate(parts):
            num = f"{start + i:>{num_w}}" if j == 0 else " " * num_w
            cont = f"{MUTED}{mark}{RST}" if j else ""
            body = _with_bg(cont + part, bg)
            fill = " " * max(0, text_w - display_width(cont + part)) if bg else ""
            out.append(f"{SUBTLE}{num} {bar}{RST} {body}{bg}{fill}{RST}" if bg else f"{SUBTLE}{num} {bar}{RST} {body}")
    return out


def code_body(content: str, path: str, width: int, max_lines: int, hint: str = "", start: int = 1) -> list[str]:
    """Numbered, highlighted rows of *content* (first *max_lines* lines + a '… +N more lines' row)."""
    lines = content.splitlines()
    if not lines or max_lines <= 0:
        return [more_row(len(lines), hint)] if lines and hint else []
    shown = lines[:max_lines]
    num_w = max(2, len(str(start + len(lines) - 1)))
    rows = numbered_rows(shown, lang_for_path(path), width, start, num_w)
    if len(lines) > len(shown):
        rows.append(more_row(len(lines) - len(shown), hint, " " * num_w + " " + glyph("bl") + " "))
    return rows


def code_card_header(path: str, content: str, verb: str = "Created") -> str:
    """'✚ Created src/shop/cart.py · ■ Python · 42 lines · 1.2 KB'."""
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    mark = {"Created": (SUCCESS, glyph("created")), "Overwrote": (WARNING, glyph("overwrote"))}.get(verb, (INFO, glyph("edited")))
    head = f"{mark[0]}{mark[1]} {verb}{RST} {BOLD}{_path_link(path)}{RST}"
    facts = [lang_badge(path), f"{MUTED}{_plural(count_lines(content), 'line')}{RST}",
             f"{MUTED}{fmt_bytes(len(content.encode('utf-8', errors='replace')))}{RST}"]
    return head + dot + dot.join(f for f in facts if f)


def code_card(path: str, content: str, width: int, max_lines: int = 30, hint: str = "", verb: str = "Created") -> list[str]:
    """Header row + numbered, highlighted body of a file Axon wrote."""
    return [code_card_header(path, content, verb)] + code_body(content, path, width, max_lines, hint)


# ─── Diff card (Edit / MultiEdit / Patch / overwrite) ──────────────────────

_WORD_RE = re.compile(r"\w+|\s+|[^\w\s]")


def _word_changes(a: str, b: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    """Changed character ranges of *a* and *b* (word granularity), or None when the lines differ too much."""
    ta, tb = _WORD_RE.findall(a), _WORD_RE.findall(b)
    if len(ta) * len(tb) > 40_000:
        return None
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    if sm.ratio() < 0.35:
        return None
    pa = [0]
    for t in ta:
        pa.append(pa[-1] + len(t))
    pb = [0]
    for t in tb:
        pb.append(pb[-1] + len(t))
    ra: list[tuple[int, int]] = []
    rb: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            ra.append((pa[i1], pa[i2]))
        if j2 > j1:
            rb.append((pb[j1], pb[j2]))
    return ra, rb


def _pair_lines(old: list[str], new: list[str]) -> tuple[dict[int, list], dict[int, list]]:
    """Match removed lines with the added lines they became (in order, by similarity) and return the
    changed character ranges of each paired line: ({old index: ranges}, {new index: ranges})."""
    old_ch: dict[int, list] = {}
    new_ch: dict[int, list] = {}
    if len(old) * len(new) > 400:
        pairs = list(zip(range(len(old)), range(len(new))))
    else:
        pairs, j = [], 0
        for i, a in enumerate(old):
            best, best_r = -1, 0.0
            for k in range(j, len(new)):
                sm = difflib.SequenceMatcher(a=a, b=new[k], autojunk=False)
                if sm.real_quick_ratio() <= best_r or sm.quick_ratio() <= best_r:
                    continue
                r = sm.ratio()
                if r > best_r:
                    best, best_r = k, r
            if best >= 0 and best_r >= 0.5:
                pairs.append((i, best))
                j = best + 1
    for i, k in pairs:
        ch = _word_changes(old[i], new[k])
        if ch:
            old_ch[i], new_ch[k] = ch
    return old_ch, new_ch


def _tinted_line(text: str, lang: str, fg: str, bg: str, word_bg: str, changed: list[tuple[int, int]]) -> str:
    """A +/- line: syntax colours on the line tint, changed words on a stronger tint."""
    if not _theme.COLOR_ENABLED and not (fg or bg):
        return text
    spans = highlight_spans(text, lang) if lang else [("", text)]
    out: list[str] = []
    pos = 0
    cur = None
    for style, tx in spans:
        i = 0
        while i < len(tx):
            at = pos + i
            hit = next(((s, e) for s, e in changed if s <= at < e), None)
            if hit:
                j = min(len(tx), hit[1] - pos)
                seg_bg = word_bg or bg
            else:
                nxt = min([s for s, _ in changed if s > at] + [pos + len(tx)])
                j = min(len(tx), nxt - pos)
                seg_bg = bg
            want = f"{RST}{seg_bg}{style or fg}{BOLD if hit and word_bg else ''}"
            if want != cur:                 # only switch styles where they change
                out.append(want)
                cur = want
            out.append(tx[i:j])
            i = j
        pos += len(tx)
    return "".join(out) + RST


def _diff_line_rows(num: int | None, sign: str, text: str, num_w: int, text_w: int, lang: str,
                    changed: list[tuple[int, int]] | None = None) -> list[str]:
    """One diff line (wrapped onto continuation rows that keep the gutter)."""
    code = text.expandtabs(4)
    n = f"{num:>{num_w}}" if num is not None and num > 0 else " " * num_w
    if sign == " ":
        hl = highlight(code, lang)
        parts = hard_wrap_ansi(hl, text_w) if hl else [""]
        return [f"{SUBTLE}{n if k == 0 else ' ' * num_w}{RST}   {p}" for k, p in enumerate(parts)]
    add = sign == "+"
    fg, bg = (DIFF_ADD, DIFF_ADD_BG) if add else (DIFF_DEL, DIFF_DEL_BG)
    word_bg = getattr(_theme, "DIFF_ADD_WORD_BG" if add else "DIFF_DEL_WORD_BG", "")
    body = _tinted_line(code, lang, fg, bg, word_bg, changed or [])
    parts = hard_wrap_ansi(body, text_w) if code else [""]
    rows = []
    for k, p in enumerate(parts):
        lead = f"{n} {sign} " if k == 0 else f"{' ' * num_w} {glyph('wrap') if len(glyph('wrap')) == 1 else ' '} "
        fill = " " * max(0, text_w - display_width(p)) if bg else ""
        rows.append(f"{bg}{fg}{lead}{p}{bg}{fill}{RST}" if bg else f"{fg}{lead}{p}{RST}")
    return rows


def _hunk_sep(num_w: int, width: int, label: str = "") -> str:
    h = glyph("hunk")
    text = f" {label} " if label else ""
    fill = max(3, min(width - num_w - 2 - len(text), 12))
    return f"{SUBTLE}{' ' * num_w}  {h * 3}{text}{h * max(0, fill - 3)}{RST}"


def diff_rows(old_text: str, new_text: str, start_line: int = 1, width: int = 80, lang: str = "",
              context: int = 2, max_rows: int = 60, hint: str = "", old_start: int | None = None) -> list[str]:
    """Unified diff of two texts: real line numbers, hunk separators, tinted +/- rows with intra-line
    word highlights; long lines wrap with the gutter kept."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    numbered = start_line > 0          # 0: where the change sits is unknown - leave the number gutter blank
    start_line = max(1, start_line)
    old_start = start_line if old_start is None else old_start
    if len(old_lines) + len(new_lines) > 20_000:
        a, r = len(new_lines), len(old_lines)
        return [f"{MUTED}(diff too large to show: {a} lines replace {r}){RST}"]
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    last = max(start_line + len(new_lines), old_start + len(old_lines))
    num_w = max(3, len(str(last)))
    text_w = max(10, width - num_w - 3)
    rows: list[str] = []
    groups = list(sm.get_grouped_opcodes(context))
    for gi, group in enumerate(groups):
        if gi:
            rows.append(_hunk_sep(num_w, width, f"line {start_line + group[0][3]}" if numbered else ""))
        for tag, i1, i2, j1, j2 in group:
            if len(rows) > max_rows * 2:
                break
            if tag == "equal":
                for k in range(i2 - i1):
                    rows += _diff_line_rows((start_line + j1 + k) * numbered, " ", new_lines[j1 + k], num_w, text_w, lang)
                continue
            room = max(1, max_rows * 2 - len(rows))          # never style rows that will not be shown
            i2s, j2s = min(i2, i1 + room), min(j2, j1 + room)
            old_ch, new_ch = (_pair_lines(old_lines[i1:i2s], new_lines[j1:j2s]) if tag == "replace" else ({}, {}))
            if tag in ("replace", "delete"):
                for k in range(i1, i2s):
                    rows += _diff_line_rows((old_start + k) * numbered, "-", old_lines[k], num_w, text_w, lang, old_ch.get(k - i1))
                if i2s < i2:
                    rows += [""] * (i2 - i2s)          # placeholders: counted in the '+N more rows' marker
            if tag in ("replace", "insert"):
                for k in range(j1, j2s):
                    rows += _diff_line_rows((start_line + k) * numbered, "+", new_lines[k], num_w, text_w, lang, new_ch.get(k - j1))
                if j2s < j2:
                    rows += [""] * (j2 - j2s)
    if len(rows) > max_rows:
        hidden = len(rows) - max_rows
        rows = rows[:max_rows] + [more_row(hidden, hint, " " * num_w + "  ", "row")]
    return rows


def diff_counts(old_text: str, new_text: str) -> tuple[int, int]:
    added = removed = 0
    a, b = old_text.splitlines(), new_text.splitlines()
    if len(a) + len(b) > 20_000:
        return len(b), len(a)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def change_badge(added: int, removed: int) -> str:
    """'+3 −1' (green / red)."""
    return f"{SUCCESS}+{added}{RST} {ERROR}{glyph('minus')}{removed}{RST}"


def diff_header(path: str, added: int, removed: int, verb: str = "Edited", extra: str = "") -> str:
    """'✎ Edited src/app.py · +3 −1'."""
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    mark = glyph("overwrote") if verb == "Overwrote" else glyph("edited")
    color = WARNING if verb == "Overwrote" else INFO
    head = f"{color}{mark} {verb}{RST} {BOLD}{_path_link(path)}{RST}{dot}{change_badge(added, removed)}"
    return head + (dot + f"{MUTED}{extra}{RST}" if extra else "")


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff(text: str) -> list[dict[str, Any]]:
    """Files of a unified diff: [{'path', 'added', 'removed', 'hunks': [(old_start, new_start, lines)]}]."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    hunk: list[str] | None = None
    for ln in (text or "").splitlines():
        if ln.startswith("--- "):
            cur = {"path": "", "old": ln[4:].split("\t")[0].strip(), "added": 0, "removed": 0, "hunks": []}
            files.append(cur)
            hunk = None
            continue
        if ln.startswith("+++ ") and cur is not None and not cur["path"]:
            p = ln[4:].split("\t")[0].strip()
            if p == "/dev/null":
                p = cur["old"]
            cur["path"] = re.sub(r"^[ab]/", "", p)
            continue
        m = _HUNK_RE.match(ln)
        if m:
            if cur is None:
                cur = {"path": "", "old": "", "added": 0, "removed": 0, "hunks": []}
                files.append(cur)
            hunk = []
            cur["hunks"].append((int(m.group(1)), int(m.group(3)), hunk))
            continue
        if hunk is None or cur is None or ln.startswith(("diff ", "index ", "\\ No newline")):
            continue
        hunk.append(ln)
        if ln.startswith("+"):
            cur["added"] += 1
        elif ln.startswith("-"):
            cur["removed"] += 1
    return [f for f in files if f["hunks"]]


def hunk_rows(old_start: int, new_start: int, lines: list[str], width: int, lang: str, num_w: int = 4) -> list[str]:
    """Rows of one unified-diff hunk (line numbers from the hunk header, word highlights on -/+ pairs)."""
    text_w = max(10, width - num_w - 3)
    out: list[str] = []
    o, n = old_start, new_start
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("-") or ln.startswith("+"):
            dels, adds = [], []
            while i < len(lines) and lines[i].startswith("-"):
                dels.append(lines[i][1:])
                i += 1
            while i < len(lines) and lines[i].startswith("+"):
                adds.append(lines[i][1:])
                i += 1
            old_ch, new_ch = _pair_lines(dels, adds)
            for k, t in enumerate(dels):
                out += _diff_line_rows(o, "-", t, num_w, text_w, lang, old_ch.get(k))
                o += 1
            for k, t in enumerate(adds):
                out += _diff_line_rows(n, "+", t, num_w, text_w, lang, new_ch.get(k))
                n += 1
            continue
        out += _diff_line_rows(n, " ", ln[1:] if ln.startswith(" ") else ln, num_w, text_w, lang)
        o += 1
        n += 1
        i += 1
    return out


def patch_rows(patch: str, width: int, max_rows: int = 60, hint: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """(parsed files, rows) for a unified diff: per file a 'path +a −r' row, then its hunks."""
    files = parse_unified_diff(patch)
    rows: list[str] = []
    for f in files:
        lang = lang_for_path(f["path"])
        last = max((h[1] + len(h[2]) for h in f["hunks"]), default=1)
        num_w = max(3, len(str(last)))
        if len(files) > 1:
            rows.append(f"{BOLD}{_path_link(f['path'])}{RST} {change_badge(f['added'], f['removed'])}")
        for k, (os_, ns, lines) in enumerate(f["hunks"]):
            if k or len(files) > 1:
                rows.append(_hunk_sep(num_w, width, f"line {ns}"))
            rows += hunk_rows(os_, ns, lines, width, lang, num_w)
    if len(rows) > max_rows:
        rows = rows[:max_rows] + [more_row(len(rows) - max_rows, hint, "  ", "row")]
    return files, rows


# ─── Grep ──────────────────────────────────────────────────────────────────

_GREP_HIT_RE = re.compile(r"^(.+?):(\d+):(.*)$")
_GREP_CTX_RE = re.compile(r"^(.+?)-(\d+)-(.*)$")


def _match_regex(pattern: str, ignore_case: bool) -> re.Pattern[str] | None:
    if not pattern:
        return None
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error:
        try:
            return re.compile(re.escape(pattern), flags)
        except re.error:
            return None


def mark_matches(text: str, rx: re.Pattern[str] | None, lang: str = "") -> str:
    """*text* highlighted, with every match of *rx* on the match tint (bold when colours are off-limits)."""
    if rx is None:
        return highlight(text, lang) if lang else f"{TEXT}{text}{RST}"
    bg = getattr(_theme, "MATCH_BG", "")
    out, pos = [], 0
    for m in rx.finditer(text):
        if m.end() == m.start():
            continue
        out.append(highlight(text[pos:m.start()], lang) if lang else f"{TEXT}{text[pos:m.start()]}{RST}")
        out.append(f"{bg}{WARNING}{BOLD}{m.group()}{RST}")
        pos = m.end()
        if len(out) > 200:
            break
    out.append(highlight(text[pos:], lang) if lang else f"{TEXT}{text[pos:]}{RST}")
    return "".join(out)


def grep_rows(content: str, pattern: str, width: int, max_rows: int = 12, hint: str = "",
              ignore_case: bool = False) -> tuple[int, int, list[str]]:
    """(matches, files, rows): matches grouped under a file header, 'line:' numbers, match highlighted."""
    lines = [ln for ln in (content or "").splitlines() if ln.strip()]
    rx = _match_regex(pattern, ignore_case)
    groups: dict[str, list[tuple[int, str, bool]]] = {}
    other: list[str] = []
    hits = 0
    for ln in lines:
        if ln.startswith("[...") or ln == "--":
            continue
        m = _GREP_HIT_RE.match(ln)
        if m and not (m.group(1).startswith("Found ") and m.group(2) == ""):
            groups.setdefault(m.group(1), []).append((int(m.group(2)), m.group(3), True))
            hits += 1
            continue
        c = _GREP_CTX_RE.match(ln)
        if c and c.group(1) in groups:
            groups[c.group(1)].append((int(c.group(2)), c.group(3), False))
            continue
        other.append(ln)
    if not groups:          # files_with_matches / count / odd formats: a plain list
        rows = [f"{TEXT}{_path_link(ln.split(':')[0], ln)}{RST}" if not ln.startswith("Found ") else f"{MUTED}{ln}{RST}"
                for ln in other[:max_rows]]
        if len(other) > max_rows:
            rows.append(more_row(len(other) - max_rows, hint))
        return len(other), len(other), rows
    rows: list[str] = []
    shown = 0
    for path, items in groups.items():
        if shown >= max_rows:
            break
        n_hits = sum(1 for it in items if it[2])
        rows.append(f"{INFO}{BOLD}{_path_link(path)}{RST} {SUBTLE}{glyph('dot')} {_plural(n_hits, 'match', 'matches')}{RST}")
        num_w = max(3, len(str(max(it[0] for it in items))))
        lang = lang_for_path(path)
        for num, text, is_hit in items:
            if shown >= max_rows:
                break
            body = text.expandtabs(4).strip("\n")
            body_w = max(8, width - num_w - 4)
            styled = mark_matches(body, rx, lang) if is_hit else f"{DIM}{body}{RST}"
            parts = hard_wrap_ansi(styled, body_w) if body else [""]
            sep = ":" if is_hit else "-"
            rows.append(f"  {SUBTLE}{num:>{num_w}}{sep}{RST} {parts[0]}")
            rows += [f"  {' ' * (num_w + 1)} {p}" for p in parts[1:]]
            shown += 1
    total_items = sum(len(v) for v in groups.values())
    if total_items > shown:
        rows.append(more_row(total_items - shown, hint, "", "line"))
    return hits, len(groups), rows


# ─── Lists (Glob / Ls) ─────────────────────────────────────────────────────

def _columns(items: list[str], width: int, indent: int = 2) -> list[str]:
    """Lay styled *items* out in as many columns as fit (column-major, like ls)."""
    if not items:
        return []
    cell = max(display_width(i) for i in items) + 2
    ncols = max(1, min(len(items), (width - indent) // max(1, cell)))
    nrows = -(-len(items) // ncols)
    rows = []
    for r in range(nrows):
        parts = [items[c * nrows + r] for c in range(ncols) if c * nrows + r < len(items)]
        line = "".join(pad(p, cell) for p in parts[:-1]) + parts[-1]
        rows.append(" " * indent + truncate(line, width - indent))
    return rows


def column_rows(paths: list[str], width: int, max_rows: int = 8, hint: str = "", base: str = "") -> list[str]:
    """Paths grouped by directory, names in columns; directories (ending in '/') coloured."""
    def name_style(p: str, full: str) -> str:
        if p.endswith("/"):
            return f"{PRIMARY}{BOLD}{_path_link(full.rstrip('/'), p)}{RST}"
        return f"{TEXT}{_path_link(full, p)}{RST}"

    groups: dict[str, list[str]] = {}
    for p in paths:
        p = p.strip()
        if p:
            d, _, _name = p.rstrip("/").rpartition("/")
            groups.setdefault(d, []).append(p)
    rows: list[str] = []
    multi = len(groups) > 1 or (len(groups) == 1 and next(iter(groups)) != "")
    shown = 0
    for d, items in groups.items():
        if len(rows) >= max_rows:
            break
        names = []
        for p in sorted(items, key=lambda x: (not x.endswith("/"), x.lower()))[: max(1, max_rows * 8)]:
            name = p.rstrip("/").rpartition("/")[2] + ("/" if p.endswith("/") else "")
            full = f"{base.rstrip('/')}/{p}" if base and not p.startswith(("/", "~")) else p
            names.append(name_style(name, full))
        if multi:
            rows.append(f"{MUTED}{glyph('dir_mark')} {d or '.'}/{RST} {SUBTLE}{glyph('dot')} {len(items)}{RST}")
        cols = _columns(names, width, 2 if multi else 0)
        take = cols[: max(0, max_rows - len(rows))]
        rows += take
        nrows = max(1, len(cols))
        shown += sum(1 for idx in range(len(names)) if idx % nrows < len(take))
    hidden = len([p for p in paths if p.strip()]) - shown
    if hidden > 0:
        rows.append(more_row(hidden, hint, "", "path"))
    return rows


# ─── JSON ──────────────────────────────────────────────────────────────────

def find_json(text: str) -> tuple[str, str] | None:
    """(head, pretty JSON) when *text* is JSON or a header block followed by a JSON body."""
    s = (text or "").strip()
    if not s:
        return None
    head = ""
    body = s
    if s[:1] not in "[{":
        cut = s.find("\n\n")
        if cut < 0:
            return None
        head, body = s[:cut], s[cut + 2:].strip()
        if body[:1] not in "[{":
            return None
    body = re.sub(r"\n\n\[\.\.\. .*$", "", body, flags=re.S)       # tool footers ('[... 120 more chars ...]')
    try:
        data = json.loads(body)
    except ValueError:
        if body[:1] in "[{" and "\n" in body and re.search(r'^\s*"[^"]+":', body, re.M):
            return head, body      # already pretty but truncated: still highlight it
        return None
    if not isinstance(data, (dict, list)):
        return None
    return head, json.dumps(data, indent=2, ensure_ascii=False)


def json_rows(pretty: str, width: int, max_lines: int = 12, hint: str = "") -> list[str]:
    lines = pretty.splitlines()
    rows: list[str] = []
    for ln in lines[:max_lines]:
        rows += hard_wrap_ansi(highlight(ln, "json"), width) if ln else [""]
    if len(lines) > max_lines:
        rows.append(more_row(len(lines) - max_lines, hint))
    return rows


# ─── Test-runner summaries ─────────────────────────────────────────────────

_COUNT_WORDS = {
    "passed": "passed", "passing": "passed", "pass": "passed", "ok": "passed",
    "failed": "failed", "failing": "failed", "failures": "failed", "failure": "failed", "fail": "failed",
    "error": "errors", "errors": "errors",
    "skipped": "skipped", "pending": "skipped", "todo": "skipped", "ignored": "skipped", "deselected": "skipped",
    "xfailed": "xfailed", "xpassed": "xpassed",
}


def test_summary(text: str) -> dict[str, Any] | None:
    """Counts from a test runner's summary ({'runner', 'passed', 'failed', 'errors', 'skipped', ...})."""
    if not text:
        return None
    tail = "\n".join(text.splitlines()[-80:])
    counts: dict[str, int] = {}
    runner = ""
    # pytest: '==== 2 failed, 10 passed, 1 skipped in 0.52s ====' or '14 passed in 0.1s'
    m = None
    for m in re.finditer(r"^[=\s]*((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
                         r"(?:, )?)+) in [\d.]+s", tail, re.M):
        pass
    if m:
        runner = "pytest"
        for n, w in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)", m.group(1)):
            key = _COUNT_WORDS.get(w, w)
            counts[key] = counts.get(key, 0) + int(n)
    # cargo: 'test result: FAILED. 8 passed; 2 failed; 0 ignored'
    if not counts:
        ms = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text)
        if ms:
            runner = "cargo test"
            counts = {"passed": sum(int(a) for a, _, _ in ms), "failed": sum(int(b) for _, b, _ in ms),
                      "skipped": sum(int(c) for _, _, c in ms)}
    # jest / vitest: 'Tests:       2 failed, 10 passed, 12 total' · 'Tests  2 failed | 10 passed (12)'
    if not counts:
        m = re.search(r"^\s*Tests:?\s+((?:\d+ \w+(?:, | \| |\s+)?)+)", tail, re.M)
        if m:
            runner = "vitest" if "|" in m.group(1) else "jest"
            for n, w in re.findall(r"(\d+) (passed|failed|skipped|todo|pending)", m.group(1)):
                key = _COUNT_WORDS[w]
                counts[key] = counts.get(key, 0) + int(n)
    # mocha: '10 passing (2s)' / '2 failing'
    if not counts:
        p = re.search(r"^\s*(\d+) passing\b", tail, re.M)
        if p:
            runner = "mocha"
            counts["passed"] = int(p.group(1))
            f = re.search(r"^\s*(\d+) failing\b", tail, re.M)
            if f:
                counts["failed"] = int(f.group(1))
            s = re.search(r"^\s*(\d+) pending\b", tail, re.M)
            if s:
                counts["skipped"] = int(s.group(1))
    # unittest: 'Ran 12 tests in 0.01s' + 'FAILED (failures=2, errors=1)' / 'OK (skipped=1)'
    if not counts:
        r = re.search(r"^Ran (\d+) tests? in [\d.]+s", tail, re.M)
        if r:
            runner = "unittest"
            total = int(r.group(1))
            bad = {k: int(v) for k, v in re.findall(r"(failures|errors|skipped)=(\d+)", tail)}
            counts = {"failed": bad.get("failures", 0), "errors": bad.get("errors", 0), "skipped": bad.get("skipped", 0)}
            counts["passed"] = max(0, total - sum(counts.values()))
    # go test: '--- PASS: TestX' / '--- FAIL: TestY' / 'ok  pkg 0.1s' / 'FAIL pkg 0.2s'
    if not counts:
        passes = len(re.findall(r"^\s*--- PASS:", text, re.M))
        fails = len(re.findall(r"^\s*--- FAIL:", text, re.M))
        skips = len(re.findall(r"^\s*--- SKIP:", text, re.M))
        if passes or fails:
            runner = "go test"
            counts = {"passed": passes, "failed": fails, "skipped": skips}
        else:
            ok_pkgs = len(re.findall(r"^ok\s+\S+\s+[\d.]+s", text, re.M))
            bad_pkgs = len(re.findall(r"^FAIL\s+\S+\s+[\d.]+s", text, re.M))
            if ok_pkgs or bad_pkgs:
                runner = "go test"
                counts = {"passed": ok_pkgs, "failed": bad_pkgs}
                counts["unit"] = 1   # packages, not tests
    if not counts or not any(v for k, v in counts.items() if k != "unit"):
        return None
    counts.setdefault("passed", 0)
    counts.setdefault("failed", 0)
    return {"runner": runner, **counts}


def test_bar(summary: dict[str, Any], cells: int = 12) -> str:
    """'██████████▒▒ 10 passed · 2 failed · 1 skipped' (green passed, red failed/errors, dim skipped)."""
    passed = int(summary.get("passed", 0)) + int(summary.get("xfailed", 0))
    failed = int(summary.get("failed", 0)) + int(summary.get("errors", 0)) + int(summary.get("xpassed", 0))
    skipped = int(summary.get("skipped", 0))
    total = max(1, passed + failed + skipped)
    f_cells = max(1, round(cells * failed / total)) if failed else 0
    s_cells = max(1, round(cells * skipped / total)) if skipped and cells - f_cells > 1 else 0
    p_cells = max(0, cells - f_cells - s_cells)
    if passed and not p_cells:
        p_cells, f_cells = 1, max(1, f_cells - 1)
    bar = (f"{SUCCESS}{glyph('block') * p_cells}{RST}{ERROR}{glyph('fail_cell') * f_cells}{RST}"
           f"{SUBTLE}{glyph('shade') * s_cells}{RST}")
    dot = f" {SUBTLE}{glyph('dot')}{RST} "
    unit = " packages" if summary.get("unit") else ""
    parts = [f"{SUCCESS}{BOLD}{summary.get('passed', 0)} passed{unit}{RST}"]
    if summary.get("failed"):
        parts.append(f"{ERROR}{BOLD}{summary['failed']} failed{RST}")
    if summary.get("errors"):
        parts.append(f"{ERROR}{_plural(int(summary['errors']), 'error')}{RST}")
    if skipped:
        parts.append(f"{MUTED}{skipped} skipped{RST}")
    return f"{bar} " + dot.join(parts)


# ─── Command output ────────────────────────────────────────────────────────

_TB_FILE_RE = re.compile(r'^(\s*File ")([^"]+)(", line )(\d+)(.*)$')


def classify_output_line(line: str) -> str:
    """Tint one line of command output: errors red, warnings amber, successes green, tracebacks linked."""
    m = _TB_FILE_RE.match(line)
    if m:
        return (f"{MUTED}{m.group(1)}{RST}{INFO}{_path_link(m.group(2))}{RST}{MUTED}{m.group(3)}{RST}"
                f"{WARNING}{m.group(4)}{RST}{MUTED}{m.group(5)}{RST}")
    s = line.lstrip()
    low = s.lower()
    if s.startswith(("E ", "E\t", ">       ")) or s == "E":
        return f"{ERROR}{line}{RST}"
    if re.match(r"^(FAILED|FAIL|ERROR|--- FAIL)\b", s) or any(w in low for w in ("error", "failed", "exception", "traceback", "panic:")):
        return f"{ERROR}{line}{RST}"
    if "warn" in low or "deprecat" in low:
        return f"{WARNING}{line}{RST}"
    if re.match(r"^(PASSED|PASS|ok)\b", s) or any(w in low for w in ("passed", "success", "completed")):
        return f"{SUCCESS}{line}{RST}"
    if s.startswith(("$ ", "> ")):
        return f"{CYAN}{line}{RST}"
    return f"{TEXT}{line}{RST}" if TEXT else line


def output_rows(lines: list[str], width: int, head: int = 4, tail: int = 2, hint: str = "") -> list[str]:
    """Tinted output rows: the first *head* and last *tail* lines with a '… +N more lines' row between."""
    n = len(lines)
    if n <= head + tail + 1:
        keep_head, keep_tail = lines, []
    else:
        keep_head, keep_tail = lines[:head], (lines[n - tail:] if tail else [])
    rows = [classify_output_line(ln.expandtabs(4)) for ln in keep_head]
    if keep_tail or n > len(keep_head):
        rows.append(more_row(n - len(keep_head) - len(keep_tail), hint))
        rows += [classify_output_line(ln.expandtabs(4)) for ln in keep_tail]
    return rows


__all__ = [
    "ACCENT", "change_badge", "classify_output_line", "code_body", "code_card", "code_card_header", "column_rows",
    "count_lines", "diff_counts", "diff_header", "diff_rows", "find_json", "fmt_bytes", "grep_rows", "hunk_rows",
    "json_rows", "lang_badge", "lang_info", "mark_matches", "more_row", "numbered_rows", "parse_unified_diff",
    "patch_rows", "test_bar", "test_summary", "_join_spans",
]
