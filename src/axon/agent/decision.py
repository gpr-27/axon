"""
Why a tool call happened, in plain words, from data Axon already has (no extra model calls).

Every tool call can be explained with five short facts:

* chosen by  - which model (and provider) wrote the call, in which step, out of how many tools
* why        - the model's own sentence that best explains the call (never invented)
* what       - what the tool does, and its arguments in plain words
* allowed    - the permission decision that let it run
* then       - where the result goes (the next model call)

``exact_call_text`` gives the call's arguments exactly as the model sent them (JSON).

The functions here return plain text (no colours); the live timeline, the restored-session view
and /inspect all style and print them the same way.
"""
from __future__ import annotations

import re
from typing import Any

RATIONALE_MAX_CHARS = 160
MIN_RATIONALE_WORDS = 4

SOURCE_LABELS = {
    "model-text": "model said",
    "text": "model said",
    "model-thinking": "model thought",
    "thinking": "model thought",
    "heuristic": "Axon's note",
}

# Argument keys whose values name what a call works on (used to match the model's sentences).
_TARGET_KEYS = ("path", "file_path", "notebook_path", "pattern", "glob", "command", "query", "url",
                "description", "subagent_type", "name")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+>#]+|\d+[.)])\s+")


# ─── why: the model's own words ─────────────────────────────────────────────

def split_sentences(text: str) -> list[str]:
    """Sentences of *text*: split at '.', '!' or '?' followed by a space, and at line breaks.
    A ':' inside a sentence never splits it. List markers ('- ', '1. ') are dropped."""
    out = []
    for piece in _SENTENCE_END.split(text or ""):
        piece = _LIST_MARKER.sub("", piece).strip()
        if piece:
            out.append(" ".join(piece.split()))
    return out


def _is_informative(sentence: str, tool_name: str) -> bool:
    """At least a few words, and more than just the tool's name."""
    words = sentence.split()
    if len(words) < MIN_RATIONALE_WORDS:
        return False
    bare = sentence.strip(" .!?:;,`'\"").lower()
    return bare != tool_name.lower()


def argument_hints(inp: Any) -> list[str]:
    """Values from a call's arguments that a sentence may mention: paths (and their file names),
    patterns, commands (and the program name), queries, URLs."""
    if not isinstance(inp, dict):
        return []
    values = [inp.get(k) for k in _TARGET_KEYS if isinstance(inp.get(k), str)]
    if not values:  # an unknown tool (MCP, plugin): any short text argument
        values = [v for v in inp.values() if isinstance(v, str) and len(v) <= 120]
    hints: list[str] = []
    for value in values:
        value = value.strip()
        if len(value) < 3:
            continue
        if len(value) <= 120:
            hints.append(value)
        if "/" in value or "\\" in value:
            base = re.split(r"[\\/]", value.rstrip("/\\"))[-1]
            if len(base) >= 3:
                hints.append(base)
        first_word = value.split()[0] if value.split() else ""
        if first_word != value and len(first_word) >= 3:
            hints.append(first_word)
    return hints


def _mention_score(sentence: str, tool_name: str, hints: list[str]) -> int:
    """2 when the sentence names one of the call's arguments, 1 when it names the tool, else 0."""
    low = sentence.lower()
    for hint in hints:
        if re.search(r"(?<!\w)" + re.escape(hint.lower()) + r"(?!\w)", low):
            return 2
    if tool_name and re.search(r"(?<![\w-])" + re.escape(tool_name.lower()) + r"(?![\w-])", low):
        return 1
    return 0


def _last_informative(text: str, tool_name: str) -> str:
    """The last sentence of *text* that says something ('' when none does)."""
    for sentence in reversed(split_sentences(text)):
        if _is_informative(sentence, tool_name):
            return sentence
    return ""


def _shorten(text: str, limit: int = RATIONALE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def pick_rationale(tool_name: str, inp: Any, own_text: str, earlier_text: str = "", thinking: str = "",
                   used: set[str] | None = None) -> tuple[str, str] | None:
    """The best sentence explaining one call, as ``(sentence, source)``; None when nothing qualifies.

    *own_text* is what the model wrote right before this call (since the previous call), *earlier_text*
    what it wrote before that in the same reply, *thinking* its reasoning. A sentence that names one
    of the call's arguments wins, then one that names the tool; ties go to the model's own text before
    this call, then the latest sentence. Without such a mention, the last sentence of *own_text* is
    used. Sentences under four words, or only the tool's name, never count; a sentence already given
    to another call in this reply (*used*) is not repeated."""
    used = used if used is not None else set()
    hints = argument_hints(inp)
    candidates: list[tuple[str, str]] = []
    for text, source in ((own_text, "model-text"), (earlier_text, "model-text"), (thinking, "model-thinking")):
        for sentence in reversed(split_sentences(text)):
            if _is_informative(sentence, tool_name) and sentence not in used:
                candidates.append((sentence, source))
    best, best_score = None, 0
    for sentence, source in candidates:
        score = _mention_score(sentence, tool_name, hints)
        if score > best_score:
            best, best_score = (sentence, source), score
    if best is None:
        last_said = _last_informative(own_text, tool_name)
        last_thought = _last_informative(thinking, tool_name)
        if last_said and last_said not in used:
            best = (last_said, "model-text")
        elif not (own_text or earlier_text).strip() and last_thought and last_thought not in used:
            best = (last_thought, "model-thinking")
    if best is None:
        return None
    used.add(best[0])
    return _shorten(best[0]), best[1]


def rationale_line(text: str, source: str) -> str:
    """'model said: "..."' / 'model thought: "..."' / "Axon's note: read-only lookup"."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    label = SOURCE_LABELS.get(source, "model said")
    if source == "heuristic":
        return f"{label}: {text}"
    return f'{label}: "{_shorten(text)}"'


# ─── chosen by ─────────────────────────────────────────────────────────────

def chosen_by_text(model: str, provider: str = "", step: int = 0, call_no: int = 0, call_total: int = 0,
                   tool_count: int = 0, prompt_tools: bool = False) -> str:
    """'chosen by claude-haiku-5-5 (Anthropic) in step 2 · call 1 of 1 · from 22 tools'."""
    if not model:
        return ""
    who = f"chosen by {model}" + (f" ({provider})" if provider else "")
    if step:
        who += f" in step {step}"
    parts = [who]
    if call_no and call_total:
        parts.append(f"call {call_no} of {call_total}")
    elif call_no:
        parts.append(f"call {call_no}")
    if tool_count:
        parts.append(f"from {tool_count} tool{'s' if tool_count != 1 else ''}"
                     + (" via prompt tools" if prompt_tools else ""))
    elif prompt_tools:
        parts.append("via prompt tools")
    return " · ".join(parts)


# ─── what: the arguments in plain words ────────────────────────────────────

def _one_line(value: Any, limit: int = 80) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _where(inp: dict[str, Any]) -> str:
    path = inp.get("path")
    return f"in {_one_line(path, 60)}" if path else "in the workspace"


def _describe_read(inp: dict[str, Any]) -> tuple[str, set[str]]:
    text = f"read {_one_line(inp.get('path') or '?', 80)}"
    offset, limit = inp.get("offset"), inp.get("limit")
    if isinstance(offset, int) and isinstance(limit, int):
        text += f" lines {offset}-{offset + limit - 1}"
    elif isinstance(offset, int):
        text += f" from line {offset}"
    elif isinstance(limit, int):
        text += f" first {limit} lines"
    return text, {"path", "offset", "limit"}


def _describe_glob(inp: dict[str, Any]) -> tuple[str, set[str]]:
    text = f"find files matching {_one_line(inp.get('pattern') or '*', 60)} {_where(inp)}"
    if inp.get("head_limit"):
        text += f", show at most {inp['head_limit']}"
    return text, {"pattern", "path", "head_limit"}


_GREP_MODES = {"content": "matching lines", "files_with_matches": "file names", "count": "match counts"}


def _describe_grep(inp: dict[str, Any]) -> tuple[str, set[str]]:
    where = _where(inp)
    if inp.get("glob"):
        where += f" ({_one_line(inp['glob'], 40)} files)"
    elif inp.get("type"):
        where += f" ({_one_line(inp['type'], 20)} files)"
    text = f'search "{_one_line(inp.get("pattern") or "", 60)}" {where}'
    mode = str(inp.get("output_mode") or "")
    if mode:
        text += f", show {_GREP_MODES.get(mode, mode)}"
    if inp.get("-i"):
        text += ", ignore case"
    if inp.get("head_limit"):
        text += f", first {inp['head_limit']}"
    return text, {"pattern", "path", "glob", "type", "output_mode", "-i", "head_limit"}


def _describe_bash(inp: dict[str, Any]) -> tuple[str, set[str]]:
    """The command itself is the call's header; this adds what the model said it does."""
    bits = []
    if inp.get("description"):
        bits.append(_one_line(inp["description"], 120))
    if inp.get("run_in_background"):
        bits.append("in the background")
    if inp.get("timeout_s"):
        bits.append(f"time limit {inp['timeout_s']}s")
    return ", ".join(bits), {"command", "description", "run_in_background", "timeout_s"}


_DESCRIBERS = {
    "Read": _describe_read,
    "Glob": _describe_glob,
    "Grep": _describe_grep,
    "Bash": _describe_bash,
}


def describe_arguments(name: str, inp: Any) -> tuple[str, set[str]]:
    """The call's arguments in plain words and the argument keys that sentence covers.

    Read / Glob / Grep / Bash get their own wording; other tools return ``("", set())`` and keep
    the generic 'key  value' argument rows."""
    describe = _DESCRIBERS.get(name)
    if describe is None or not isinstance(inp, dict):
        return "", set()
    return describe(inp)


def generic_arguments(inp: Any, skip: set[str] | None = None, limit: int = 4) -> str:
    """'key=value, key=value' for tools without their own wording (short values only)."""
    if not isinstance(inp, dict):
        return ""
    bits = []
    for key, value in inp.items():
        if str(key).startswith("_") or key in (skip or set()) or value in (None, "", [], {}):
            continue
        bits.append(f"{key}={_one_line(value, 40)}")
    more = len(bits) - limit
    return ", ".join(bits[:limit]) + (f", +{more} more" if more > 0 else "")


# ─── allowed because ───────────────────────────────────────────────────────

def _reason_words(reason: str) -> str:
    """A permission reason as words inside a sentence: 'Bypass mode active.' -> 'bypass mode active';
    names such as 'Bash(rm:*)' keep their case."""
    reason = " ".join(str(reason or "").split()).rstrip(".")
    first_word = reason.split(" ", 1)[0] if reason else ""
    if first_word.isalpha() and first_word[1:].islower():
        return reason[:1].lower() + reason[1:]
    return reason


def allowed_because(outcome: str, source: str = "", reason: str = "", approval: Any = None) -> str:
    """One short line on why the call could (or could not) run, from the permission decision:
    'allowed: bypass mode is on', 'allowed by rule Bash(git *)', 'approved by you (once)', ..."""
    outcome = str(outcome or "").lower()
    source = str(source or "")
    reason = str(reason or "")
    if source == "auto":      # auto mode's classifier: 'auto: allowed — read-only search in workspace'
        return f"auto: {'allowed' if outcome == 'allow' else 'denied' if outcome == 'deny' else 'asking'} — {reason}".rstrip(" —")
    if outcome == "deny":
        if source == "user" or approval == "deny":
            return "declined by you"
        return f"denied by {source}" + (f": {_reason_words(reason)}" if reason else "") if source else "denied"
    if outcome != "allow":
        return f"needs approval: {_reason_words(reason)}" if reason else "needs approval"
    if source == "user" or approval:
        return "approved by you" + (f" ({approval})" if approval else "")
    if source == "rule":
        rule = reason.split(":", 1)[1].strip() if ":" in reason else reason
        return f"allowed by rule {rule}" if rule else "allowed by a rule"
    if source == "hook":
        return f"allowed by hook: {_reason_words(reason)}" if reason else "allowed by hook"
    if source == "delegation":
        return "allowed: subagents are allowed; their own tool calls are checked one by one"
    low = reason.lower()
    if "bypass" in low:
        return "allowed: bypass mode is on"
    if "allowed by default" in low:
        return "allowed: this tool is allowed by default (read-only)"
    return f"allowed: {_reason_words(reason)}" if reason else "allowed"


RULE_MAX_CHARS = 40


def short_allowed(text: str) -> str:
    """The permission outcome in two to four words, from ``allowed_because`` text:
    'allowed: bypass mode is on' -> 'allowed (bypass mode)', 'allowed by rule Bash(git *)' ->
    'allowed (rule Bash(git *))', 'declined by you' -> 'declined by you'. '' when there is no text.
    The short form of a rule comes back as it is, so the rule's name survives a second pass."""
    flat = " ".join(str(text or "").split())
    low = flat.lower()
    if not low:
        return ""
    if low.startswith("allowed (rule ") and flat.endswith(")"):
        return flat
    if low.startswith("auto: "):
        return flat if len(flat) <= 80 else flat[:79] + "…"
    if low.startswith("approved by you"):
        return "approved by you"
    if low.startswith("declined by you"):
        return "declined by you"
    if low.startswith("denied"):
        by = re.match(r"denied by (\w+)", low)
        return f"denied ({by.group(1)})" if by else "denied"
    if low.startswith("needs approval"):
        return "needs approval"
    if low.startswith("allowed by rule"):
        rule = " ".join(str(text).split())[len("allowed by rule"):].strip()
        if len(rule) > RULE_MAX_CHARS:
            rule = rule[: RULE_MAX_CHARS - 1] + "…"
        return f"allowed (rule {rule})" if rule else "allowed (rule)"
    if low.startswith("allowed by hook"):
        return "allowed (hook)"
    if "bypass" in low:
        return "allowed (bypass mode)"
    if "read-only" in low:
        return "allowed (read-only)"
    if "subagent" in low:
        return "allowed (subagent)"
    return "allowed"


# ─── then: where the result goes ───────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    return f"{n} B" if n < 1024 else (f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.1f} MB")


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def then_text(n_bytes: int, est_tokens: int, next_step: int = 0, result_no: int = 0, call_id: str = "") -> str:
    """'result (54 B, ~14 tokens) goes to the model in step 3' (with '#1 [id]' at the detailed level)."""
    label = "result"
    if result_no:
        label += f" #{result_no}"
    if call_id:
        label += f" [{call_id[:12]}]"
    size = f"{_fmt_bytes(int(n_bytes or 0))}, ~{_fmt_tokens(int(est_tokens or 0))} tokens"
    where = f"the model in step {next_step}" if next_step else "the model"
    return f"{label} ({size}) goes to {where}"


def step_results_text(count: int, n_bytes: int, est_tokens: int, next_step: int = 0) -> str:
    """'2 results (314 B, ~84 tokens) go to step 2': the one line under the last tool of a step."""
    noun = "result" if count == 1 else "results"
    verb = "goes" if count == 1 else "go"
    size = f"{_fmt_bytes(int(n_bytes or 0))}, ~{_fmt_tokens(int(est_tokens or 0))} tokens"
    where = f"step {next_step}" if next_step else "the model"
    return f"{count} {noun} ({size}) {verb} to {where}"


def exact_call_text(inp: Any, raw: str = "", width: int = 100) -> str:
    """The arguments of a call exactly as the model sent them, as JSON: on one line when that fits in
    *width* columns, else indented. *raw* (the text of arguments that were not valid JSON) is returned
    as it is."""
    if raw:
        return str(raw)
    import json
    try:
        one_line = json.dumps(inp, ensure_ascii=False, default=str)
        if len(one_line) <= width:
            return one_line
        return json.dumps(inp, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(inp)


def final_answer_text(step: int, model: str = "") -> str:
    """'final answer written in step 3 by claude-haiku-5-5'."""
    if not step:
        return ""
    return f"final answer written in step {step}" + (f" by {model}" if model else "")


def pick_rationales(blocks: list[tuple[str, Any]]) -> dict[str, tuple[str, str]]:
    """Rationales for every call of one model reply, as ``{call_id: (sentence, source)}``.

    *blocks* is the reply in order: ``("text", str)``, ``("thinking", str)`` or
    ``("tool", (call_id, name, input))``. Each call gets its own best sentence (see ``pick_rationale``);
    calls without one are left out."""
    out: dict[str, tuple[str, str]] = {}
    own_text, earlier_text, thinking = "", "", ""
    used: set[str] = set()
    for kind, value in blocks:
        if kind == "text" and str(value or "").strip():
            own_text += "\n" + str(value).strip()
        elif kind == "thinking" and str(value or "").strip():
            thinking += "\n" + str(value).strip()
        elif kind == "tool":
            call_id, name, inp = value
            picked = pick_rationale(name, inp, own_text, earlier_text, thinking, used)
            if picked:
                out[call_id] = picked
            # the next call has its own preceding text; what came before stays available as context
            earlier_text, own_text = earlier_text + own_text, ""
    return out


def reply_blocks(record: dict[str, Any]) -> list[tuple[str, Any]]:
    """The ordered blocks of a stored assistant turn (session transcript ``assistant_turn`` data):
    from the provider's own content list when it kept one, else thinking, text, then the calls."""
    calls = [u for u in (record.get("tool_uses") or []) if isinstance(u, dict)]
    native = record.get("native")
    blocks: list[tuple[str, Any]] = []
    if isinstance(native, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in native):
        for b in native:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                blocks.append(("text", b.get("text") or ""))
            elif b.get("type") == "thinking":
                blocks.append(("thinking", b.get("thinking") or ""))
            elif b.get("type") == "tool_use":
                blocks.append(("tool", (str(b.get("id") or ""), str(b.get("name") or ""), b.get("input") or {})))
        return blocks
    blocks.append(("thinking", record.get("thinking") or ""))
    blocks.append(("text", record.get("text") or ""))
    for u in calls:
        blocks.append(("tool", (str(u.get("id") or ""), str(u.get("name") or ""), u.get("input") or {})))
    return blocks
