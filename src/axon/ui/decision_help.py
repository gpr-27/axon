"""
Short explainers shown above a choice Axon asks you to make (AskUserQuestion, tool approvals).

The text is built only from data the prompt already has: the question and options the model wrote, the tool
and its arguments, the permission reason, the rule "always allow" would save, and the model's own words
before the call. No model call. The builders return plain lines (labels carry the meaning, not colour) that
are already wrapped to the terminal width; ``shown`` prints them and, on a real terminal, wipes them again
once the choice is made so the scrollback keeps only the answer.
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any, Iterator

from axon import platform as plat
from axon.ui.theme import MUTED, OUTPUT_LOCK, RST, glyph, sanitize, wrap_ansi

OTHER_ROW = "Other"
MAX_OPTION_ROWS = 8

# tool -> (what it does, what it can touch)
_EFFECTS: dict[str, tuple[str, str]] = {
    "Bash": ("runs this command in your shell", "run programs, change files and use the network"),
    "Git": ("runs this git command", "change the repository"),
    "Write": ("writes this file", "create the file or overwrite it"),
    "Edit": ("edits this file", "change the file in place"),
    "MultiEdit": ("makes several edits to this file", "change the file in place"),
    "Patch": ("applies a patch to this file", "change files in place"),
    "NotebookEdit": ("edits a cell of this notebook", "change the notebook file"),
    "WebFetch": ("fetches this page", "use the network; nothing on your disk changes"),
    "Http": ("sends this HTTP request", "use the network and maybe change things on the remote server"),
    "Task": ("starts a subagent for this job", "start a subagent whose own tool calls are checked one by one"),
    "ExitPlanMode": ("shows its plan for your review", "nothing by itself; it only shows the plan"),
}


def _width(width: int | None) -> int:
    return max(30, (width or plat.term_size()[0]) - 4)


def _clean(text: Any, limit: int = 0) -> str:
    s = " ".join(sanitize(str(text or "")).split())
    return s if not limit or len(s) <= limit else s[: limit - 1] + glyph("ellipsis")


def _block(rows: list[tuple[str, str]], width: int) -> list[str]:
    """Rows of (label, text) as 'Label: text' (a number label: '1  text'), wrapped with the text hanging under itself."""
    out: list[str] = []
    for label, text in rows:
        lead = f"{label.strip()}  " if label.strip().isdigit() else (f"{label}: " if label else "")
        pad = " " * len(lead)
        for i, part in enumerate(wrap_ansi(text, max(10, width - len(lead))) or [""]):
            out.append(("  " + (lead if i == 0 else pad) + part).rstrip())
    return out


def _count_phrase(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# ─── AskUserQuestion ────────────────────────────────────────────────────────

def _option_rows(options: list[dict[str, Any]], width: int, number_width: int) -> list[tuple[str, str]]:
    rows = []
    for i, o in enumerate(options[:MAX_OPTION_ROWS], 1):
        text = _clean(o.get("label"), 40)
        desc = _clean(o.get("description"), 120)
        rows.append((str(i).rjust(number_width), f"{text} - {desc}" if desc else text))
    return rows


def _keys_text(n: int, multi: bool) -> str:
    pick = f"1-{n}" if n > 1 else "1"
    if multi:
        return (f"{pick} jump to an option, space ticks it, Enter confirms (nothing ticked: the highlighted one). "
                "Esc skips: the model is told you did not answer and may ask in text or assume")
    return (f"{pick} picks at once, or arrows + Enter (starts on 1). "
            "Esc skips: the model is told you did not answer and may ask in text or assume")


def explain_question(q: dict[str, Any], index: int = 1, total: int = 1, why: str = "", width: int | None = None) -> list[str]:
    """Lines for one AskUserQuestion question: what is decided, why, what each option does, how to answer."""
    w = _width(width)
    options = list(q.get("options") or [])
    multi = bool(q.get("multiSelect"))
    head = _clean(q.get("question"), 200)
    if q.get("header"):
        head += f" [{_clean(q['header'], 24)}]"
    if total > 1:
        head += f" (question {index} of {total})"
    asked = (f"the model said \"{_clean(why, 140)}\" just before asking" if why
             else "the model chose to ask instead of guessing; it gave no reason in words")
    rows: list[tuple[str, str]] = [("Deciding", head), ("Why asked", asked)]
    rows += _option_rows(options, w, len(str(min(len(options), MAX_OPTION_ROWS))))
    if len(options) > MAX_OPTION_ROWS:
        rows.append(("", f"... and {len(options) - MAX_OPTION_ROWS} more below"))
    rows.append((OTHER_ROW, "last row: press Enter on it, then type your own answer"))
    rows.append(("Keys", _keys_text(min(len(options), 9), multi)))
    return _block(rows, w)


# ─── approvals ──────────────────────────────────────────────────────────────

def _target(tool_name: str, args: dict[str, Any]) -> str:
    """The argument the call acts on, in a few words (a Bash call: its description, else its first line)."""
    if tool_name == "Bash":
        lines = str(args.get("command", "")).splitlines()
        return _clean(args.get("description") or (lines[0] if lines else ""), 80)
    try:
        from axon.ui.render import tool_target
        return _clean(tool_target(tool_name, args), 80)
    except Exception:
        return ""


def _effects(tool_name: str) -> tuple[str, str]:
    if tool_name in _EFFECTS:
        return _EFFECTS[tool_name]
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 2 else "an MCP server"
        return (f"calls a tool on the MCP server {_clean(server, 30)}", "what it can touch is decided by that server")
    return (f"runs the {_clean(tool_name, 30)} tool", "see the arguments above for what it acts on")


def explain_approval(tool_name: str, args: dict[str, Any], reason: str = "", rule: str = "", why: str = "",
                     width: int | None = None) -> list[str]:
    """Lines for a permission prompt: the exact call in words, what it can do, why it was asked, each answer."""
    w = _width(width)
    does, can = _effects(tool_name)
    target = _target(tool_name, args if isinstance(args, dict) else {})
    what = f"{tool_name} {does}" + (f": {target}" if target else "")
    asked = _clean(reason, 140).rstrip(".") or "this tool is not allowed automatically in the current permission mode"
    if why:
        asked += f"; the model said \"{_clean(why, 120)}\""
    answers = f"1 yes, this call only · 2 yes, every {tool_name} call until you exit · 3 no"
    if rule:
        answers += f" · 4 always allow {_clean(rule, 60)} in this project"
    last = "4" if rule else "3"
    rows = [("What", what), ("Can", can), ("Why asked", asked), ("Answers", answers),
            ("Keys", f"1-{last}, y / a / n, or arrows + Enter (starts on 1). Esc = No: the model is told you "
                     "declined and may try another way; Axon waits for you, there is no timeout")]
    return _block(rows, w)


# ─── printing ───────────────────────────────────────────────────────────────

@contextlib.contextmanager
def shown(lines: list[str], erase: bool | None = None) -> Iterator[None]:
    """Print *lines* in a dim block; erase them afterwards on an interactive terminal (``erase=None``).

    A failure here must never stop the question from being asked, so every error is swallowed."""
    if erase is None:
        erase = _interactive()
    printed = _print(lines)
    try:
        yield
    finally:
        if printed and erase:
            _erase(len(lines))


def show(lines: list[str]) -> None:
    """Print the block and leave it in the scrollback."""
    _print(lines)


def _interactive() -> bool:
    try:
        return bool(plat.is_interactive())
    except Exception:
        return False


def _print(lines: list[str]) -> bool:
    if not lines:
        return False
    try:
        with OUTPUT_LOCK:
            sys.stdout.write("".join(f"{MUTED}{line}{RST}\n" for line in lines))
            sys.stdout.flush()
        return True
    except Exception:
        return False


def _erase(count: int) -> None:
    try:
        with OUTPUT_LOCK:
            sys.stdout.write(f"\033[{count}A\r\033[J")
            sys.stdout.flush()
    except Exception:
        pass


# ─── entry points for the prompts ───────────────────────────────────────────

def approval_lines(tool_name: str, args: dict[str, Any], reason: str = "", rule: str = "") -> list[str]:
    """The approval explainer for a prompt, or [] when live status is off or anything goes wrong."""
    try:
        from axon.ui import live_status
        if not live_status.explainers_enabled():
            return []
        return explain_approval(tool_name, args, reason, rule, why=live_status.current_why(tool_name))
    except Exception:
        return []


def question_lines(q: dict[str, Any], index: int = 1, total: int = 1) -> list[str]:
    """The AskUserQuestion explainer for one question, or [] when live status is off or anything goes wrong."""
    try:
        from axon.ui import live_status
        if not live_status.explainers_enabled():
            return []
        return explain_question(q, index, total, why=live_status.current_why("AskUserQuestion"))
    except Exception:
        return []
