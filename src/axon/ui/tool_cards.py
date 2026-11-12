"""
Tool calls in the terminal, drawn once when the result is in.

At the compact trace level (opt-in) a call is one header row and a short preview (``compact_rows``)::

    ✓ Bash  echo hello && ls -la  0.2s
        hello
        total 8
        … +3 more lines · /output 1
    ✓ Read  notes.md  2 lines  0.1s
    ✗ Bash  ls /missing  failed · exit 1 · 2.1s
        ls: /missing: No such file or directory

The rest of this module is the phased card of the normal trace level (``card_rows``).

Each step of a turn has up to three sections, told apart by their label::

    ✻ THINKING  0.1s
      The user wants a quick test of Bash and Ls, so I will run one harmless
      command and list the folder together.

    ▣ TOOLS · 2 calls · one after another · allowed: bypass mode
      1 Bash  choose  Run a harmless echo to test Bash
              input   echo "bash works: $(date)"
              run     ✓ 0.27s
              output  bash works: Thu Nov 5 19:59:13 IST 2026
      2 Ls    input   .
              run     ✓ 0.00s · 15 dirs, 11 files
              output  .axon/  .github/  .mypy_cache/  ... +22 more · /output 19
      → 2 results (314 B, ~84 tokens) go to step 2

The stages of a call, in the order they happen:

* choose  - why the model made the call, in its own words; left out when it gave no real reason
* input   - the exact command, or the arguments in plain words (workspace paths shortened)
* run     - how it ended (status, time, result size, 'no output'); the permission outcome is said once in
            the TOOLS header and again here only for a call that differs (asked, denied, by rule ...)
* output  - at most 3 rows of the result; the last one is '+N more · /output K' when something is cut

In a narrow window the extra options of the input go first, then the choose line, then the preview
shrinks to one row.

The model and provider are named once, in the step header, never per call. The longer explainer
(chosen by / exact call / exact result) belongs to the detailed level, ``/output K`` and ``/transcript``.

Everything here is pure: data in, styled rows out (each at most *width* columns). The live
renderer, the restored-session views and the transcript viewer all call it, so they agree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from axon.agent import decision as _decision
import os

from axon.ui.theme import (
    ACCENT, BOLD, DIM, ERROR, INFO, ITALIC, MUTED, PRIMARY, RST, SUBTLE, SUCCESS, TEXT,
    display_width, glyph, sanitize, strip_ansi, truncate, wrap_ansi,
)

LABEL_W = 6              # 'choose', 'input ', 'run   ', 'output'
LABEL_GAP = 2
OUTPUT_ROWS = 3          # rows of result preview, the '+N more' pointer included
CHOOSE_ROWS = 2
INPUT_ROWS = 2
COMMAND_ROWS = 60        # rows of a shell command shown exactly before '+N more lines'
RUN_ROWS = 2
SHOW_OPTIONS_FROM = 80   # narrower windows drop the extra options of the input line first ...
SHOW_CHOOSE_FROM = 60    # ... then the choose line ...
SHOW_PREVIEW_FROM = 46   # ... then the result preview shrinks to one row
CHANGED_FILE_ROWS = 4    # files a shell command changed that are listed under its output
DIFF_BODY_ROWS = 8       # changed lines of an Edit / Write card before '+N more lines'
PLAN_ROWS = 6
THINK_PREVIEW_CHARS = 100
THINK_ROWS = 100_000          # the thought as wrapped rows at the normal level: this many, then a pointer to the rest
COMPACT_DIFF_ROWS = 6    # changed lines of an Edit / Write at the compact level before '+N more lines'

# Arguments that carry no choice of the model (defaults, limits, plumbing): not worth a place on the input line.
_NOISY_OPTIONS = {
    "output_mode", "head_limit", "-A", "-B", "-C", "-n", "multiline", "timeout", "timeout_s", "run_in_background",
    "description", "offset", "limit", "num_results", "max_results", "per_page", "page", "format", "encoding",
    "context", "case_insensitive", "max_depth", "depth", "recursive", "show_hidden",
}
_REAL_WHY_SOURCES = {"", "model-text", "text", "model-thinking", "thinking"}      # not 'heuristic' (Axon's own filler)
_EMPTY_NOTES = {"no matches", "no files matched", "(empty directory)", "(no output)"}
_DIFF_TOOLS = {"Write", "WriteFile", "write_to_file", "Edit", "EditFile", "replace_file_content",
               "MultiEdit", "Patch", "NotebookEdit"}
_BASH_TOOLS = {"Bash", "bash", "run_command"}
_LS_TOOLS = {"Ls", "list_dir"}
_GREP_TOOLS = {"Grep", "grep_search"}
_READ_TOOLS = {"Read", "ReadFile", "view_file", "read_file"}
_SUBAGENT_TOOLS = {"Task", "Agent"}

_NUMBERED_RE = re.compile(r"^\s*(\d+)→(.*)$")
_GREP_HIT_RE = re.compile(r"^(.+?):(\d+)[:-](.*)$")
_SIZE_SUFFIX_RE = re.compile(r" \([\d.]+ ?[KMGB]+\)$")
_TREE_PREFIX_RE = re.compile(r"^[\s│├└─|`\\-]*(?=\S)")
_ERROR_WORDS = re.compile(r"error|fatal|failed|not found|no such|denied|exception|cannot|can't|invalid", re.I)


def unicode_ok() -> bool:
    """True when the terminal draws the circled digits and block glyphs."""
    return glyph("ok") == "✓"


@dataclass
class CardCall:
    """Everything one card shows. *state*: 'done', 'running' (no result yet) or 'stopped'."""
    number: int
    name: str
    inp: dict[str, Any] = field(default_factory=dict)
    why: str = ""                 # the model's own sentence for this call
    why_source: str = ""          # model-text | model-thinking | heuristic ('' when unknown: counts as the model's)
    allowed: str = ""             # permission text ('allowed: bypass mode is on') or its short form
    content: str = ""             # the result text the model received
    is_error: bool = False
    duration: float | None = None
    state: str = "done"
    hint: str = "/output"         # where the full result is: '/output 7' or '/transcript'
    notes: list[str] = field(default_factory=list)   # hook messages and the like (rare)
    registry: Any = None
    name_w: int = 0               # width of the tool-name column (shared by the calls of a step)
    total: int = 1                # calls in this step: the number is shown only when there are several
    common_allowed: str = ""      # the permission outcome said in the TOOLS header: this card repeats it only if it differs
    workspace: str = ""           # paths under it are shown relative (default: the current folder)
    said: str = ""                # what the model wrote before the calls of this step (printed right above the cards)


# ─── small helpers ─────────────────────────────────────────────────────────

def _render() -> Any:
    from axon.ui import render
    return render


def fmt_secs(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f"{max(0.0, float(seconds)):.2f}s"


def _elapsed(seconds: float) -> str:
    """'3.3s', '1m 04s': the clock of a call that is still running (the same one the transcript viewer shows)."""
    from axon.ui.transcript_view import fmt_secs as viewer_secs
    return viewer_secs(seconds)


def name_column_width(names: list[str], width: int = 100) -> int:
    """Width of the tool-name column for the calls of one step: the longest name, between 4 and 10
    (up to 24 unless the window is narrow, so 'github › search_issues' stays whole)."""
    render = _render()
    longest = max((display_width(render.tool_label(n)) for n in names), default=4)
    return max(4, min(24 if width >= 60 else 10, longest))


def tidy_paths(text: str, workspace: str = "") -> str:
    """*text* with the workspace folder cut off the front of paths and the home folder written as ~."""
    # A path may be written with either separator (models often write C:/proj/src on Windows), so try both spellings.
    def cut(text: str, folder: str, replacement: str) -> str:
        for spelled in dict.fromkeys((folder, folder.replace("\\", "/"), folder.replace("/", "\\"))):
            for sep in ("/", "\\"):
                text = text.replace(spelled + sep, replacement + sep if replacement == "~" else "")
            text = text.replace(spelled, replacement)
        return text

    root = (workspace or os.getcwd()).rstrip("/\\")
    if root and len(root) > 1:
        text = cut(text, root, ".")
    home = os.path.expanduser("~").rstrip("/\\")
    if home and len(home) > 1:
        text = cut(text, home, "~")
    return text


def _wrap_plain(text: str, width: int, max_rows: int) -> list[str]:
    """*text* wrapped at spaces (never inside a word) to at most *max_rows* rows; a word wider than the row,
    such as a long path, keeps its tail behind a leading ellipsis. The last row ends with an ellipsis when cut."""
    width = max(8, width)
    ellipsis = glyph("ellipsis")
    rows: list[str] = []
    current = ""
    for word in text.split(" "):
        if display_width(word) > width:
            word = ellipsis + word[-(width - 1):]
        if current and display_width(current) + 1 + display_width(word) > width:
            rows.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        rows.append(current)
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        rows[-1] = truncate(rows[-1], width - 1, "") + ellipsis
    return rows


def _wrap(styled: str, width: int, max_rows: int) -> list[str]:
    """*styled* wrapped to *width* columns, at most *max_rows* rows (the last one ends with an ellipsis)."""
    rows = wrap_ansi(styled, max(8, width)) if styled else []
    if len(rows) <= max_rows:
        return rows
    rows = rows[:max_rows]
    rows[-1] = truncate(rows[-1], max(1, width - 1), "") + f"{SUBTLE}{glyph('ellipsis')}{RST}"
    return rows


def _fit(styled: str, width: int) -> str:
    return truncate(styled, max(8, width))


def _pointer(more: int, hint: str, unit: str = "") -> str:
    return f"{SUBTLE}{glyph('ellipsis')} +{more} more{unit} {glyph('dot')} {hint or '/output'}{RST}"


def _pointer_width(more: int, hint: str, unit: str = "") -> int:
    return display_width(_pointer(more, hint, unit))


def _preview(rows: list[str], hint: str, width: int, unit: str = " lines", limit: int = OUTPUT_ROWS) -> list[str]:
    """At most *limit* rows: all of *rows* when they fit, else the first ones and the pointer."""
    rows = [_fit(r, width) for r in rows]
    if len(rows) <= limit:
        return rows
    keep = limit - 1
    return rows[:keep] + [_pointer(len(rows) - keep, hint, unit)]


def _text_lines(content: str) -> list[str]:
    lines = [l.rstrip().expandtabs(4) for l in sanitize(content or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


# ─── choose / input / run ──────────────────────────────────────────────────

def _same_words(a: str, b: str) -> bool:
    """Whether two texts say the same thing once markdown marks, case and spacing are set aside."""
    def words(s: str) -> str:
        return " ".join(re.sub(r"[*_`#>]", "", str(s or "")).split()).rstrip(".:!").lower()
    return bool(words(a)) and words(a) == words(b)


def real_reason(c: CardCall) -> str:
    """The model's own reason for the call; '' for Axon's filler or a sentence that only repeats the
    shell call's description."""
    why = " ".join(str(c.why or "").split())
    if not why or c.why_source not in _REAL_WHY_SOURCES:
        return ""
    description = c.inp.get("description") if isinstance(c.inp, dict) else None
    if isinstance(description, str) and " ".join(description.split()).rstrip(".").lower() == why.rstrip(".").lower():
        return ""
    if c.total > 1 and c.why_source != "model-thinking" and c.said and _same_words(c.said, why):
        return ""           # one sentence for the whole batch, printed right above: not this one call's reason
    return why


def choose_text(c: CardCall, with_name: bool = False) -> str:
    """The model's own reason for the call ('' when it gave no real one: the card then has no choose line).
    *with_name* puts the full tool name in front of it (for a name too long for the name column)."""
    why = real_reason(c)
    if not why:
        return ""
    label = _render().tool_label(c.name)
    return (f"{MUTED}{label} {_dash()}{RST} " if with_name else "") + f"{TEXT}{why}{RST}"


def _option_text(key: str, value: Any) -> str:
    if value is True:
        return key
    return f"{key}={' '.join(str(value).split())[:24]}"


def input_options(name: str, inp: dict[str, Any]) -> list[str]:
    """Up to two options the model chose beyond the main argument ('glob=*.py', '-i'); defaults and plumbing
    ('output_mode', 'head_limit', '-A', timeouts ...) are left out."""
    render = _render()
    covered = set(render._TARGET_KEYS[name]) if name in render._TARGET_KEYS else {render._generic_target_key(inp)}
    covered |= set(render._BODY_KEYS)
    out = []
    for key, value in inp.items():
        key = str(key)
        if key.startswith("_") or key in covered or key in _NOISY_OPTIONS or value in (None, "", [], {}, False, 0):
            continue
        if isinstance(value, (dict, list)):
            continue
        out.append(_option_text(key, value))
    return out[:2]


def input_rows(c: CardCall, width: int, options: bool = True) -> list[str]:
    """The input line(s): a shell command exactly as the model sent it (every line, up to COMMAND_ROWS), else
    the main argument in plain words. The workspace is cut off paths. Options go on the same row when they
    fit, else on a dim row of their own."""
    render = _render()
    inp = c.inp if isinstance(c.inp, dict) else {}
    if c.name in _BASH_TOOLS:
        rows: list[str] = []
        lines = render._command_lines(inp)
        for line in lines:
            rows += _wrap_plain(tidy_paths(line, c.workspace), width, COMMAND_ROWS) or [""]
        if len(rows) > COMMAND_ROWS:
            hidden = len(rows) - (COMMAND_ROWS - 1)
            return [f"{TEXT}{r}{RST}" for r in rows[:COMMAND_ROWS - 1]] + [_pointer(hidden, c.hint, " lines")]
        return [f"{TEXT}{r}{RST}" for r in rows] or [f"{MUTED}(no command){RST}"]
    target = tidy_paths(render.tool_target(c.name, inp), c.workspace)
    rows = [f"{TEXT}{r}{RST}" for r in _wrap_plain(target, width, INPUT_ROWS)] if target else []
    opts = input_options(c.name, inp) if options else []
    if opts:
        text = f"{MUTED}{'  '.join(opts)}{RST}"
        if rows and display_width(rows[-1]) + 2 + display_width(text) <= width:
            rows[-1] += "  " + text
        else:
            rows.append(truncate(text, width))
    return rows or [f"{MUTED}(no arguments){RST}"]


def bash_exit_code(content: str) -> int:
    """The exit code a shell result reports (0 when it does not say otherwise)."""
    m = _render()._EXIT_RE.match(sanitize(content or "").lstrip())
    return int(m.group(1)) if m else 0


def is_failed(c: CardCall) -> bool:
    if c.state != "done":
        return False
    if c.is_error:
        return True
    return c.name in _BASH_TOOLS and bash_exit_code(c.content) != 0


def result_summary(c: CardCall) -> str:
    """A short fact about the result for the run line ('15 dirs, 11 files', '42 lines'); '' for most tools."""
    if c.name in _BASH_TOOLS or c.name in _DIFF_TOOLS or c.is_error:
        return ""
    render = _render()
    try:
        text = render._tool_summary(c.registry, c.name, c.inp, c.content, False)
    except Exception:
        return ""
    text = (text or "").replace("directories", "dirs").replace("directory", "dir")
    return text if 0 < display_width(text) <= 36 else ""


def permission_text(c: CardCall) -> str:
    """The permission outcome this card must say: nothing when it is the one the TOOLS header already says."""
    short = _decision.short_allowed(c.allowed) or str(c.allowed or "")
    common = _decision.short_allowed(c.common_allowed) or str(c.common_allowed or "")
    if not short or (common and short == common):
        return ""
    return short


def run_text(c: CardCall, note: str = "") -> str:
    """'✓ 0.27s', '✓ 0.00s · no matches', '✗ failed · exit 1 · 0.01s', '▶ running…' (+ ' · denied (rule)' when
    the permission outcome differs from the section's)."""
    if c.state == "running":
        secs = f" {MUTED}{_elapsed(c.duration)}{RST}" if c.duration else ""
        return f"{PRIMARY}{glyph('play')} running{glyph('ellipsis')}{RST}{secs}"
    permission = permission_text(c)
    tail = f" {MUTED}{glyph('dot')} {permission}{RST}" if permission else ""
    if c.state == "stopped":
        return f"{ERROR}{glyph('fail')} stopped before it finished{RST}{tail}"
    secs = fmt_secs(c.duration)
    dot = f" {glyph('dot')} "
    if is_failed(c):
        blocked = (_decision.short_allowed(c.allowed) or "").startswith(("denied", "declined"))
        bits = ["blocked" if blocked else "failed"]
        code = bash_exit_code(c.content) if c.name in _BASH_TOOLS else 0
        if code:
            bits.append(f"exit {code}")
        if secs:
            bits.append(secs)
        if note:
            bits.append(note)
        return f"{ERROR}{glyph('fail')} {dot.join(bits)}{RST}{tail}"
    summary = result_summary(c)
    bits = [b for b in (secs, summary, "" if note and note in summary else note) if b]
    return f"{SUCCESS}{glyph('ok')}{RST} {TEXT}{dot.join(bits)}{RST}".rstrip() + tail


# ─── output ────────────────────────────────────────────────────────────────

def first_error_line(lines: list[str]) -> str:
    """The line that names the problem: a Traceback's last line, else the first line that looks like an error."""
    lines = [l.strip() for l in lines if l.strip()]
    if not lines:
        return ""
    if any(l.startswith("Traceback") for l in lines):
        return lines[-1]
    for line in lines:
        if _ERROR_WORDS.search(line):
            return line
    return lines[0]


def _error_output(c: CardCall, lines: list[str], width: int) -> list[str]:
    first = first_error_line(lines) or "tool failed"
    first = re.sub(r"^(?:error|Error|ERROR)\s*:\s*", "", first) or "tool failed"
    rows = _wrap(f"{ERROR}{first}{RST}", width, 2)
    others = len([l for l in lines if l.strip()]) - 1
    if others > 0:
        rows = rows[:OUTPUT_ROWS - 1] + [_pointer(others, c.hint, " lines")]
    return rows


def _names_row(names: list[str], width: int, hint: str) -> str:
    """Names on one row as far as they fit, then '… +N more · /output K'."""
    sep = "  "
    shown: list[str] = []
    for i, name in enumerate(names):
        trial = sep.join(shown + [name])
        left = len(names) - len(shown) - 1
        room = width - (2 + _pointer_width(left, hint) if left else 0)
        if display_width(trial) > room:
            break
        shown.append(name)
    if not shown:
        shown = [truncate(names[0], max(8, width // 2))]
    styled = "  ".join(f"{PRIMARY}{n}{RST}" if n.endswith("/") else f"{TEXT}{n}{RST}" for n in shown)
    left = len(names) - len(shown)
    return styled + (f"  {_pointer(left, hint)}" if left else "")


def _ls_names(lines: list[str]) -> list[str]:
    names = []
    for line in lines:
        if not line.strip() or line.startswith(("...", "(", "[")):
            continue
        text = line.rstrip()
        if "├" in text or "└" in text:
            text = _TREE_PREFIX_RE.sub("", text)
        names.append(_SIZE_SUFFIX_RE.sub("", text.strip()))
    return [n for n in names if n]


def _bash_output(c: CardCall, width: int) -> list[str]:
    render = _render()
    text = render._split_file_changes(sanitize(c.content or ""))[0].strip("\n")
    m = render._EXIT_RE.match(text.lstrip())
    code = 0
    if m:
        code = int(m.group(1))
        text = text.lstrip()[m.end():]
    lines = []
    for line in text.rstrip().splitlines():
        if render._CWD_NOTE_RE.match(line.strip()) or render._NO_OUTPUT_RE.match(line.strip()):
            continue
        lines.append(line.rstrip().expandtabs(4))
    lines = _text_lines("\n".join(lines))
    if code or c.is_error:
        return _error_output(c, lines, width) if lines else [f"{ERROR}(no output){RST}"]
    changes = list(c.inp.get("_changes") or ()) if isinstance(c.inp, dict) else []
    changes = changes or render._split_file_changes(sanitize(c.content or ""))[1]
    summary = render._cards.file_change_rows(changes, width, 1, c.hint) if changes else []   # totals + one row per file
    if len(summary) > 1 + CHANGED_FILE_ROWS:
        summary = summary[:1 + CHANGED_FILE_ROWS] + [_pointer(len(summary) - 1 - CHANGED_FILE_ROWS, c.hint, " files")]
    if not lines:
        return ([f"{MUTED}(no output){RST}"] if not summary else []) + [_fit(r, width) for r in summary]
    shown = _preview([f"{TEXT}{l}{RST}" for l in lines], c.hint, width,
                     limit=OUTPUT_ROWS - 1 if summary else OUTPUT_ROWS)
    return shown + [_fit(r, width) for r in summary]


def _grep_output(c: CardCall, width: int) -> list[str]:
    lines = _text_lines(c.content)
    if not lines or lines[0].startswith("No matches"):
        return [f"{MUTED}no matches{RST}"]
    rows = []
    for line in lines:
        if line.startswith(("Found ", "[", "--")):
            continue
        line = tidy_paths(line, c.workspace)
        m = _GREP_HIT_RE.match(line)
        if m:
            rows.append(f"{INFO}{m.group(1)}:{m.group(2)}{RST}  {TEXT}{m.group(3).strip()}{RST}")
        else:
            rows.append(f"{TEXT}{line.strip()}{RST}")
    return _preview(rows or [f"{MUTED}no matches{RST}"], c.hint, width, " matches")


def _read_output(c: CardCall, width: int) -> list[str]:
    lines = _text_lines(c.content)
    numbered = [m for m in (_NUMBERED_RE.match(l) for l in lines) if m]
    if numbered and len(numbered) >= len(lines) // 2:
        num_w = len(str(numbered[-1].group(1)))
        rows = [f"{MUTED}{m.group(1).rjust(num_w)}{RST}  {TEXT}{m.group(2).rstrip()}{RST}" for m in numbered]
        return _preview(rows, c.hint, width)
    return _preview([f"{TEXT}{l}{RST}" for l in lines] or [f"{MUTED}(empty){RST}"], c.hint, width)


def _web_search_output(c: CardCall, width: int) -> list[str]:
    lines = _text_lines(c.content)
    titles = [m.group(1).strip() for m in (re.match(r"^\d+\.\s+(.*)$", l) for l in lines) if m]
    if not titles:
        return _preview([f"{TEXT}{l}{RST}" for l in lines] or [f"{MUTED}(no output){RST}"], c.hint, width)
    return _preview([f"{TEXT}{t}{RST}" for t in titles], c.hint, width, " results")


def _diff_sign(row: str) -> str:
    m = re.match(r"^\s*\d*\s+([+-])(?:\s|$)", strip_ansi(row))
    return m.group(1) if m else ""


def _collapse_diff(body: list[str], limit: int) -> tuple[list[str], int]:
    """The first *limit* changed lines of a diff (half removed, half added when both exist), in order, and
    how many rows were left out. A diff that fits is returned as it is, context rows included."""
    if len(body) <= limit:
        return body, 0
    removed = [i for i, row in enumerate(body) if _diff_sign(row) == "-"]
    added = [i for i, row in enumerate(body) if _diff_sign(row) == "+"]
    take_removed = min(len(removed), max(limit // 2, limit - len(added)))
    take_added = min(len(added), limit - take_removed)
    keep = sorted(removed[:take_removed] + added[:take_added]) or list(range(limit))
    return [body[i] for i in keep], len(body) - len(keep)


def _diff_output(c: CardCall, width: int) -> list[str]:
    render = _render()
    rows = render.summarize_tool_result(c.name, dict(c.inp), c.content, False, width, detail=1)
    if not rows:
        return [f"{MUTED}(no output){RST}"]
    created = "Created" in strip_ansi(rows[0])[:12]          # a new file: its first lines, not a 'diff'
    shown, hidden = _collapse_diff(rows[1:], 12 if created else DIFF_BODY_ROWS)
    out = [rows[0]] + shown
    if hidden:
        out.append(_pointer(hidden, c.hint, " lines"))
    return [_fit(r, width) for r in out]


def _plan_output(c: CardCall, width: int) -> list[str]:
    render = _render()
    rows = render._todo_summary(c.content, width=width)
    if len(rows) > PLAN_ROWS:
        rows = rows[:PLAN_ROWS] + [_pointer(len(rows) - PLAN_ROWS, c.hint, " lines")]
    return [_fit(r, width) for r in rows] or [f"{MUTED}(no output){RST}"]


def _generic_output(c: CardCall, width: int) -> list[str]:
    render = _render()
    lines = _text_lines(c.content)
    if not lines:
        return [f"{MUTED}(no output){RST}"]
    found = render._cards.find_json(c.content)
    if found:
        pretty = found[1].splitlines()
        shape = f"{TEXT}{render._json_shape(found[1])}{RST}"
        rows = [shape] + [f"{MUTED}{l.strip()}{RST}" for l in pretty[1:2] if l.strip() not in ("{", "[")]
        if len(pretty) > 3:
            rows.append(_pointer(len(pretty) - 1, c.hint, " lines"))
        return [_fit(r, width) for r in rows[:OUTPUT_ROWS]]
    return _preview([f"{TEXT}{l}{RST}" for l in lines], c.hint, width)


def output_rows(c: CardCall, width: int) -> list[str]:
    """The result preview: at most OUTPUT_ROWS rows (a diff card: its header and DIFF_BODY_ROWS changed lines)."""
    if c.state != "done":
        return []
    name = c.name
    text = sanitize(c.content or "")
    if name in _SUBAGENT_TOOLS or name == "ExitPlanMode":
        return _generic_output(c, width) if not c.is_error else _error_output(c, _text_lines(text), width)
    if name == "TodoWrite" and not c.is_error:
        return _plan_output(c, width)
    if name in _BASH_TOOLS:
        return _bash_output(c, width)
    if c.is_error:
        return _error_output(c, _text_lines(text), width)
    if name in _DIFF_TOOLS:
        return _diff_output(c, width)
    lines = _text_lines(text)
    if name in _LS_TOOLS:
        names = _ls_names(lines)
        return [_names_row(names, width, c.hint)] if names else [f"{MUTED}(empty directory){RST}"]
    if name == "Glob":
        if not lines or lines[0].startswith("No files"):
            return [f"{MUTED}no files matched{RST}"]
        names = [l.strip() for l in lines if l.strip() and not l.startswith("[")]
        return [_names_row(names, width, c.hint)]
    if name in _GREP_TOOLS:
        return _grep_output(c, width)
    if name in _READ_TOOLS:
        return _read_output(c, width)
    if name == "WebSearch":
        return _web_search_output(c, width)
    return _generic_output(c, width)


# ─── the card ──────────────────────────────────────────────────────────────

def _one_row(rows: list[str], width: int) -> list[str]:
    """The preview squeezed to one row: the first row, with the '+N more' pointer behind it when there is room."""
    if len(rows) <= 1:
        return rows
    last = strip_ansi(rows[-1])
    m = re.search(r"\+(\d+) more\w*(?: \w+)? \S+ (.*)$", last)
    if m:
        short = f"{SUBTLE}+{m.group(1)} {glyph('dot')} {m.group(2)}{RST}"
        room = width - display_width(short) - 2
        return [truncate(rows[0], max(8, room)) + "  " + short]
    return rows[:1]


def card_rows(c: CardCall, width: int) -> list[str]:
    """One tool call as rows of at most *width* columns: the step number (several calls only) and the tool
    name, then choose (only with a real reason) / input / run / output."""
    render = _render()
    label = render.tool_label(c.name)
    name_w = c.name_w or name_column_width([c.name], width)
    gutter_w = 3 if c.total > 1 else 0
    lead_w = gutter_w + name_w + 2
    text_w = max(12, width - lead_w - LABEL_W - LABEL_GAP)
    output = output_rows(c, text_w) if c.state == "done" else []
    note = ""
    if len(output) == 1 and strip_ansi(output[0]).strip() in _EMPTY_NOTES:
        note, output = strip_ansi(output[0]).strip().strip("()"), []       # said on the run line, not as an empty row
    if width < SHOW_PREVIEW_FROM:
        output = _one_row(output, text_w)
    fields: list[tuple[str, list[str]]] = []
    reason = choose_text(c, with_name=display_width(label) > name_w) if width >= SHOW_CHOOSE_FROM else ""
    if reason:
        fields.append(("choose", _wrap(reason, text_w, CHOOSE_ROWS)))
    fields.append(("input", input_rows(c, text_w, options=width >= SHOW_OPTIONS_FROM)))
    fields.append(("run", _wrap(run_text(c, note), text_w, RUN_ROWS)))
    if output:
        fields.append(("output", output))
    for line in c.notes[:2]:
        fields.append(("note", _wrap(f"{MUTED}{line}{RST}", text_w, 1)))
    rows: list[str] = []
    first = True
    for tag, texts in fields:
        for j, text in enumerate(texts):
            if first:
                shown = label if display_width(label) <= name_w else truncate(label, name_w)
                gutter = f"{MUTED}{str(c.number).rjust(2)}{RST} " if gutter_w else ""
                lead = gutter + f"{BOLD}{TEXT}{shown}{RST}" + " " * (name_w - display_width(shown)) + "  "
                first = False
            else:
                lead = " " * lead_w
            marker = f"{SUBTLE}{tag.ljust(LABEL_W)}{RST}  " if j == 0 else " " * (LABEL_W + LABEL_GAP)
            rows.append(truncate(lead + marker + text, width))
    return rows


# ─── the compact call: one header row and a short preview ───────────────────

def _compact_secs(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return "<0.1s" if float(seconds) < 0.1 else _elapsed(float(seconds))


def _compact_target(c: CardCall) -> str:
    """The key argument on one line: a shell command's first line (with how many follow), else the target."""
    render = _render()
    inp = c.inp if isinstance(c.inp, dict) else {}
    if c.name in _BASH_TOOLS:
        lines = [l for l in render._command_lines(inp) if l.strip()]
        if not lines:
            return ""
        more = f" +{len(lines) - 1} lines" if len(lines) > 2 else (" +1 line" if len(lines) == 2 else "")
        return " ".join(tidy_paths(lines[0], c.workspace).split()) + more
    return tidy_paths(render.tool_target(c.name, inp), c.workspace)


def _fit_target(target: str, room: int) -> str:
    """*target* cut to *room* columns: a path keeps its tail (the file name), anything else its start."""
    if display_width(target) <= room:
        return target
    ellipsis = glyph("ellipsis")
    if " " not in target and ("/" in target or "\\" in target) and room > 1:
        tail = target
        while tail and display_width(tail) > room - 1:
            tail = tail[1:]
        return ellipsis + tail
    return truncate(target, max(1, room))


def _diff_stat(first_row: str) -> str:
    """'+1 −1' (coloured) from a diff card's header row, with 'created' / 'overwrote' in front when it says so."""
    plain = strip_ansi(first_row)
    pieces = [p.strip() for p in plain.split(f" {glyph('dot')} ")]
    verb = next((v for v in ("Created", "Overwrote") if v in pieces[0]), "")
    stat = next((p for p in pieces[1:] if re.match(r"^[+\-−]\d", p)), "")
    if not stat:
        changed = next((p for p in pieces[1:] if p == "no changes"), "")
        return " ".join(x for x in (verb.lower(), changed) if x)
    words = []
    for word in stat.split():
        if word[1:] == "0":
            continue
        words.append(f"{SUCCESS}{word}{RST}" if word.startswith("+") else (f"{ERROR}{word}{RST}" if word[0] in "-−" else word))
    return (f"{MUTED}{verb.lower()}{RST} " if verb else "") + " ".join(words)


def _compact_diff(c: CardCall, width: int) -> tuple[str, list[str]]:
    """(the stat for the header row, the changed lines). A diff is the one result that keeps a longer preview."""
    render = _render()
    rows = render.summarize_tool_result(c.name, dict(c.inp), c.content, False, width, detail=1)
    if not rows:
        return "", []
    shown, hidden = _collapse_diff(rows[1:], COMPACT_DIFF_ROWS)
    return _diff_stat(rows[0]), [_fit(r, width) for r in shown] + ([_pointer(hidden, c.hint, " lines")] if hidden else [])


def _quiet(row: str) -> str:
    """A preview row with its body text muted, so the header rows carry the eye (errors keep their colour)."""
    return row.replace(TEXT, MUTED) if TEXT else row


def _compact_tails(c: CardCall, fact: str) -> list[str]:
    """What follows the argument on the header row, longest first (a narrow window takes a shorter one)."""
    secs = _compact_secs(c.duration)
    dot = f" {glyph('dot')} "
    if c.state == "stopped":
        return [f"{ERROR}stopped before it finished{RST}", f"{ERROR}stopped{RST}"]
    if is_failed(c):
        blocked = (_decision.short_allowed(c.allowed) or "").startswith(("denied", "declined"))
        word = "blocked" if blocked else "failed"
        code = bash_exit_code(c.content) if c.name in _BASH_TOOLS else 0
        full = [word] + ([f"exit {code}"] if code else []) + ([secs] if secs else [])
        return [f"{ERROR}{dot.join(full)}{RST}", f"{ERROR}{dot.join([word] + ([secs] if secs else []))}{RST}", f"{ERROR}{word}{RST}"]
    timing = f"{MUTED}{secs}{RST}" if secs else ""
    with_fact = f"{MUTED}{fact}{RST}" + (f"{MUTED}{dot}{RST}{timing}" if timing else "") if fact else ""
    return [t for t in (with_fact, timing) if t] + [""]


def _without_subagent_banner(c: CardCall) -> CardCall:
    """A subagent's report without its '[Subagent #1 Result (...) | N tokens ...]:' line."""
    if c.name not in _SUBAGENT_TOOLS or c.is_error:
        return c
    first, _, rest = sanitize(c.content or "").lstrip().partition("\n")
    return replace(c, content=rest) if first.startswith("[Subagent #") and rest.strip() else c


def compact_rows(c: CardCall, width: int) -> list[str]:
    """One tool call at the compact level: 'status name  argument  facts · time', then a short preview.

    Failures always say why. The reason for the call, its permission outcome and the rest of the
    result stay in the transcript (Ctrl+F) and ``/output K``."""
    render = _render()
    width = max(20, width)
    failed = is_failed(c)
    body_w = max(12, width - 2)
    fact, body = "", []
    if c.state == "done" and c.name in _DIFF_TOOLS and not failed:
        fact, body = _compact_diff(c, body_w)
    elif c.state == "done":
        body = output_rows(_without_subagent_banner(c), body_w)
        if len(body) == 1 and strip_ansi(body[0]).strip() in _EMPTY_NOTES and not failed:
            fact, body = strip_ansi(body[0]).strip().strip("()"), []
        if not failed:
            summary = result_summary(c).split(f" {glyph('dot')} ")[0]
            fact = summary or fact
            if c.name in _READ_TOOLS:
                body = []                # the file's text is the model's business: the header says how much it read
            elif width < SHOW_PREVIEW_FROM:
                body = _one_row(body, body_w)
            body = [_quiet(r) for r in body]
    mark = f"{ERROR}{glyph('fail')}{RST}" if failed or c.state == "stopped" else f"{SUCCESS}{glyph('ok')}{RST}"
    label = render.tool_label(c.name)
    if display_width(label) > max(10, width // 3):
        label = truncate(label, max(10, width // 3))
    lead = f"{mark} {BOLD}{TEXT}{label}{RST}"
    target = _compact_target(c)
    used = display_width(lead)
    tails = _compact_tails(c, fact)
    tail = tails[-1]
    for option in tails:            # keep the facts while the argument still gets a fair share of the row
        room = width - used - 2 - (display_width(option) + 2 if option else 0)
        if room >= min(16, display_width(target)):
            tail = option
            break
    room = width - used - 2 - (display_width(tail) + 2 if tail else 0)
    show_options = width >= SHOW_OPTIONS_FROM and c.name not in _SUBAGENT_TOOLS       # a subagent's prompt is no option
    options = input_options(c.name, c.inp if isinstance(c.inp, dict) else {}) if show_options else []
    header = lead
    if target and room > 0:
        shown = _fit_target(target, room)
        header += f"  {TEXT}{shown}{RST}"
        extra = "  ".join(options)
        if extra and display_width(shown) + 2 + display_width(extra) <= room:
            header += f"  {MUTED}{extra}{RST}"
    if tail:
        header += f"  {tail}"
    rows = [truncate(header, width)]
    rows += ["  " + r for r in body]
    rows += ["  " + r for line in c.notes[:2] if "blocked" in line or glyph("warn") in line
             for r in _wrap(f"{MUTED}{line}{RST}", body_w, 1)]
    return rows


def step_results_text(count: int, n_bytes: int, est_tokens: int, next_step: int, width: int = 0) -> str:
    """'→ 2 results (314 B, ~84 tokens) go to step 2'; in a narrow window the token count, then the size, go first."""
    arrow = glyph("arrow")
    noun = "result" if count == 1 else "results"
    verb = "goes" if count == 1 else "go"
    where = f"step {next_step}" if next_step else "the model"
    size = _decision._fmt_bytes(int(n_bytes or 0))
    full = _decision.step_results_text(count, n_bytes, est_tokens, next_step)
    options = [full, f"{count} {noun} ({size}) {verb} to {where}", f"{count} {noun} {arrow} {where}"]
    text = options[-1]
    for option in options:
        if not width or display_width(f"{arrow} {option}") <= width:
            text = option
            break
    return truncate(f"{SUBTLE}{arrow} {text}{RST}", width) if width else f"{SUBTLE}{arrow} {text}{RST}"


# ─── section labels ────────────────────────────────────────────────────────

def _dash() -> str:
    return "—" if unicode_ok() else "-"


def _mark(uni: str, asc: str) -> str:
    return uni if unicode_ok() else asc


def allowed_header(short: str) -> str:
    """'allowed (bypass mode)' -> 'allowed: bypass mode' for the TOOLS header."""
    m = re.match(r"^(\w+) \((.+)\)$", short or "")
    return f"{m.group(1)}: {m.group(2)}" if m else (short or "")


def common_permission(allowed_texts: list[str]) -> str:
    """The permission outcome most calls of a step share, in its short form ('' when none is known)."""
    shorts = [_decision.short_allowed(t) for t in allowed_texts if t]
    return max(dict.fromkeys(shorts), key=shorts.count) if shorts else ""


def tools_label(count: int, mode: str = "", allowed: str = "", width: int = 0) -> str:
    """'▣ TOOLS · 2 calls · one after another · allowed: bypass mode' (*mode*: 'sequential', 'parallel' or ''
    for a single call; *allowed*: the short permission outcome the calls share)."""
    noun = "call" if count == 1 else "calls"
    how = {"parallel": "in parallel", "sequential": "one after another"}.get(mode, "") if count > 1 else ""
    dot = glyph("dot")
    base = f" {dot} {count} {noun}"
    perm = f" {dot} {allowed_header(allowed)}" if allowed else ""
    word = f" {dot} {allowed.split(' ')[0]}" if allowed else ""
    options = [base + (f" {dot} {how}" if how else "") + perm, base + perm, base + word, base]
    tail = options[-1]
    for option in options:             # a narrow window drops the mode first, then the permission detail
        if not width or display_width("▣ TOOLS" + option) <= width:
            tail = option
            break
    return f"{PRIMARY}{BOLD}{_mark('▣', '#')} TOOLS{RST}{MUTED}{tail}{RST}"


def answer_label() -> str:
    return f"{ACCENT}{BOLD}{_mark('▍', '|')} ANSWER{RST}"


def batch_mode(names: list[str], registry: Any = None) -> str:
    """'parallel' when the calls of one step all only read (they run together), else 'sequential';
    '' for a single call. The rule the agent loop uses to decide."""
    if len(names) < 2:
        return ""
    render = _render()
    for name in names:
        if name == "Task":
            continue
        try:
            readonly = bool(registry.get(name).readonly) if registry is not None else name not in render.MUTATING_TOOLS
        except Exception:
            readonly = name not in render.MUTATING_TOOLS
        if not readonly:
            return "sequential"
    return "parallel"


def thinking_line(seconds: float | None, text: str, width: int) -> str:
    """'✻ THINKING  0.1s · first ~100 characters of the thought…' (one row, never wider than *width*)."""
    flat = " ".join(str(text or "").split())
    cut = flat[:THINK_PREVIEW_CHARS].rstrip()
    preview = cut + (glyph("ellipsis") if len(flat) > len(cut) else "")
    head = f"{SUBTLE}{glyph('star')} THINKING{RST}"
    if seconds is not None:
        head += f"  {MUTED}{seconds:.1f}s{RST}"
    if preview:
        head += f" {MUTED}{glyph('dot')}{RST} {DIM}{ITALIC}{preview}{RST}" if seconds is not None else f"  {DIM}{ITALIC}{preview}{RST}"
    return truncate(head, width)


def thinking_rows(seconds: float | None, text: str, width: int, hint: str = "/transcript", max_rows: int = THINK_ROWS,
                  indent: str = "  ") -> list[str]:
    """The thought as it was written: '✻ THINKING  0.1s' then the text wrapped and indented, dim italic, at most
    *max_rows* rows and then a pointer to the rest. A thought with no text is just the header."""
    head = f"{PRIMARY}{BOLD}{glyph('star')} THINKING{RST}" + (f"  {MUTED}{seconds:.1f}s{RST}" if seconds is not None else "")
    flat = " ".join(str(text or "").split())
    if not flat:
        return [truncate(head, width)]
    body = _wrap_plain(flat, max(20, width - len(indent)), 10_000)
    shown = body[:max_rows]
    rows = [truncate(head, width)] + [f"{indent}{DIM}{ITALIC}{r}{RST}" for r in shown]
    if len(body) > max_rows:
        rows.append(f"{indent}{MUTED}{glyph('ellipsis')} +{len(body) - max_rows} more lines {glyph('dot')} {hint}{RST}")
    return rows


__all__ = [
    "CardCall", "batch_mode", "answer_label", "card_rows", "choose_text", "compact_rows", "first_error_line", "input_rows",
    "name_column_width", "output_rows", "common_permission", "real_reason", "tidy_paths", "run_text", "step_results_text", "thinking_line", "thinking_rows",
    "tools_label",
]
