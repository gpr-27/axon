"""
Contextual usage tips, written by the model and grounded in the live product.

Tips are not a hand-written list. Each refresh builds two blocks of context and asks the
session's provider (``settings.small_model`` when set, else the session model) for ~8 short
tips as strict JSON:

* PRODUCT FACTS, read from live sources so the model cannot invent features: the slash-command
  registry (names, aliases, descriptions, plus custom commands/skills), the effective keyboard
  bindings (``axon.ui.keybindings``), the type-ahead behaviour documented in
  ``axon.ui.in_flight`` and the prompt prefixes (@ ! # ?).
* ACTIVITY: recent prompts, tools used and failed in recent turns (``agent.traces``), slash
  commands / prefixes already used (prompt history), context usage, model, mode and cost.

Every tip is validated: unknown ``/commands`` or key combos, over-long or duplicate tips are
dropped and ANSI/control characters stripped. The call runs on a daemon thread (one at a time,
time-boxed, failures silent), its cost goes to the ledger with source ``tips`` and the pool is
cached per project (``~/.axon/projects/<key>/tips.json``) so the next start has tips instantly.
Until the model has written any, tips are derived from the command registry and keybinding
table, ranked by what the user is doing. Only interactive REPL turns can trigger the LLM call.

API::

    tip_engine(agent=None) -> TipEngine      # process-wide singleton
    engine.observe_turn(agent, result)       # after each REPL turn; may refresh in the background
    engine.spinner_tip() -> str              # rotating tip for the spinner line ("" when off)
    engine.footer_tip() -> str               # a not-yet-shown tip for after a turn ("" when none)
    engine.tips() -> list[Tip]               # current pool

Settings: ``tips`` (master switch), ``tips_llm``, ``tips_refresh_turns``, ``tips_refresh_minutes``;
``AXON_NO_TIPS=1`` disables everything. ``/tips [refresh|on|off|llm on|llm off]``.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from axon.features import FeatureCommand

MAX_TIP_CHARS = 90
MIN_TIP_CHARS = 12
MAX_TIPS = 10
TARGET_TIPS = 8
ROTATE_S = 10.0
FIRST_REFRESH_TURNS = 1     # first model-written pool after the first REPL turn
FOOTER_EVERY_TURNS = 4      # footer tips: on a new pool, else at most once per this many turns
LLM_TIMEOUT_S = 45.0
LLM_MAX_TOKENS = 4000       # reasoning models (deepseek-v4) think before answering even with thinking off
FALLBACK_TIPS = 12
CACHE_FILE = "tips.json"
CACHE_VERSION = 1
SOURCE = "tips"
DEFAULT_REFRESH_TURNS = 5
DEFAULT_REFRESH_MINUTES = 15.0

# Prompt-line syntax handled by the dispatcher (``#`` memory, ``?``), the REPL (``!`` shell) and
# the input editor (``@`` mentions). These are grammar, not tips: the model phrases the tips.
PROMPT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/name", "run a slash command (Tab completes)"),
    ("@path", "attach a file or folder to the prompt (Tab / Right completes the path)"),
    ("!cmd", "run a shell command right away; its output is added to the conversation"),
    ("#text", "save a quick memory note for this project"),
    ("?", "on an empty prompt: show the keyboard shortcuts (/kb)"),
)


@dataclass
class Tip:
    text: str
    command: str = ""
    reason: str = ""


# ─── Text hygiene ─────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
_LEAD_RE = re.compile(r"^(?:[-*•·>]+\s*|\d+[.)]\s+)")
_TIP_PREFIX_RE = re.compile(r"^(?:pro[ -])?tips?\s*[:\-–—]\s*", re.I)
_CMD_MENTION_RE = re.compile(r"(?<![\w/.~:@\\-])/([A-Za-z][\w:-]*)(?![\w:/-]|\.\w)")
_KEY_MENTION_RE = re.compile(
    r"(?<![\w-])((?:ctrl|control|alt|option|opt|meta|shift|cmd|command|super|win)"
    r"(?:\s*[+-]\s*(?:ctrl|control|alt|option|opt|meta|shift|cmd|command|super|win))*"
    r"\s*[+-]\s*(?:enter|return|tab|esc|escape|backspace|space|left|right|up|down|delete|[a-z0-9_]))(?![a-z0-9])",
    re.I,
)


def clean_text(value: Any, limit: int | None = None) -> str:
    """Strip ANSI / control characters, list bullets and a leading "Tip:"; collapse whitespace."""
    s = _ANSI_RE.sub("", str(value or ""))
    s = _CTRL_RE.sub(" ", s)
    s = " ".join(s.split())
    s = _LEAD_RE.sub("", s)
    s = _TIP_PREFIX_RE.sub("", s).strip().strip("\"'`").strip()
    if limit is not None and len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def _clip(s: str, limit: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= limit else s[: limit - 3].rstrip(" ,;:-") + "..."


_ASCII_MAP = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2212": "-", "\u2026": "...",
                            "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ",
                            "\u2192": "->", "\u2190": "<-", "\u00b7": "-", "\u2022": "-", "\u21b5": "Enter"})


def ascii_fold(text: str) -> str:
    """Transliterate typographic characters (dashes, ellipsis, quotes, arrows, middle dots) to ASCII
    and drop anything else that is not ASCII."""
    out = str(text or "").replace(" \u00b7 ", "; ").translate(_ASCII_MAP)
    return " ".join(out.encode("ascii", errors="ignore").decode("ascii").split())


def display_text(text: str) -> str:
    """Tip text as it should be printed: ASCII-only under AXON_ASCII or when stdout cannot encode
    it (dashes/quotes/arrows transliterated, anything else dropped), unchanged otherwise."""
    if not text:
        return ""
    ascii_only = bool(os.environ.get("AXON_ASCII"))
    if not ascii_only:
        try:
            import sys
            text.encode(getattr(sys.stdout, "encoding", None) or "ascii")
            return text
        except (UnicodeEncodeError, LookupError):
            ascii_only = True
    return ascii_fold(text)


def mentioned_commands(text: str) -> list[str]:
    return [m.group(1).lower() for m in _CMD_MENTION_RE.finditer(text or "")]


def _norm_key(spec: str) -> str | None:
    spec = re.sub(r"\s*([+-])\s*", r"\1", spec.strip())
    try:
        from axon.ui.keybindings import normalize_key
        return normalize_key(spec)
    except Exception:
        return spec.lower().replace("+", "-")


def mentioned_keys(text: str) -> list[str]:
    """Normalized key combos mentioned in ``text`` ("Ctrl+O" -> "ctrl-o"; unparseable -> "?<raw>")."""
    out: list[str] = []
    for m in _KEY_MENTION_RE.finditer(text or ""):
        k = _norm_key(m.group(1))
        out.append(k if k else "?" + m.group(1).lower())
    return out


def _fmt_key(key: str) -> str:
    return " ".join(p.replace("-", "+") if len(p) > 1 else p for p in key.split())


# ─── Live product facts ───────────────────────────────────────────────────

def _registry_commands() -> list[Any]:
    try:
        from axon.commands.registry import visible_commands
        return sorted(visible_commands(), key=lambda c: c.name)
    except Exception:
        return []


def known_commands(agent: Any = None) -> set[str]:
    """Every name that dispatches as ``/name``: built-ins, aliases, features, custom commands, skills."""
    names: set[str] = set()
    try:
        from axon.commands import registry as reg
        reg._ensure_loaded()
        feats = reg.feature_table()
        names.update(reg.COMMANDS)
        names.update(reg.ALIASES)
        names.update(feats)
        names.update(reg._FEATURE_ALIASES)
    except Exception:
        pass
    for c in _registry_commands():
        names.add(c.name)
        names.update(c.aliases)
    try:
        from axon.commands.registry import list_command_names
        ws = _workspace(agent)
        for name, _desc in list_command_names(ws, agent=agent):
            names.add(name.lstrip("/").lower())
    except Exception:
        pass
    return {n.lower() for n in names if n}


def _canonical_command(name: str) -> str:
    try:
        from axon.commands.registry import resolve_builtin
        cmd = resolve_builtin(name)
        if cmd is not None:
            return cmd.name
    except Exception:
        pass
    return name.lower()


def _bindings() -> dict[str, list[str]]:
    try:
        from axon.ui.keybindings import effective_keybindings
        return effective_keybindings()
    except Exception:
        return {}


def _describe(action: str) -> str:
    try:
        from axon.ui.keybindings import describe_action
        return describe_action(action)
    except Exception:
        return ""


def _in_flight_module() -> Any:
    try:
        from axon.ui import in_flight
        return in_flight
    except Exception:
        return None


def in_flight_facts() -> str:
    """The type-ahead behaviour as documented by axon.ui.in_flight (first docstring paragraphs)."""
    mod = _in_flight_module()
    doc = inspect.getdoc(mod) if mod is not None else ""
    paras = [" ".join(p.split()) for p in (doc or "").split("\n\n") if p.strip()]
    return " ".join(paras[:2])


def known_keys() -> set[str]:
    """Key names that do something: effective prompt bindings plus the in-flight listener's keys."""
    keys: set[str] = set()
    for ks in _bindings().values():
        for k in ks:
            keys.update(k.split())
    mod = _in_flight_module()
    if mod is not None:
        try:
            src = inspect.getsource(mod)
            keys.update(re.findall(r"[\"']((?:ctrl|alt|shift)-[a-z_]+|esc|enter|tab|up|down)[\"']", src))
        except (OSError, TypeError):
            pass
        for k in mentioned_keys(inspect.getdoc(mod) or ""):
            keys.update(k.split())
    return keys


def product_facts(agent: Any = None) -> str:
    """Compact, model-facing description of what Axon can do, built from live sources."""
    lines = ["SLASH COMMANDS (/name [aliases] - description):"]
    seen: set[str] = set()
    for c in _registry_commands():
        alias = f" [{' '.join('/' + a for a in c.aliases)}]" if c.aliases else ""
        lines.append(f"/{c.name}{alias} - {_clip(c.description, 100)}")
        seen.add(c.name)
    try:
        from axon.commands.registry import list_command_names
        extra = [(n, d) for n, d in list_command_names(_workspace(agent), agent=agent) if n.lstrip("/") not in seen]
    except Exception:
        extra = []
    if extra:
        lines.append("PROJECT COMMANDS AND SKILLS:")
        lines += [f"{n} - {_clip(d, 80)}" for n, d in extra[:25]]
    binds = [(a, ks) for a, ks in sorted(_bindings().items()) if ks]
    if binds:
        lines.append("KEYBOARD SHORTCUTS AT THE PROMPT (keys - action):")
        lines += [f"{', '.join(_fmt_key(k) for k in ks)} - {_describe(a) or a}" for a, ks in binds]
    tf = in_flight_facts()
    if tf:
        lines.append("WHILE THE AGENT IS WORKING (type-ahead):")
        lines.append(tf)
    lines.append("PROMPT PREFIXES:")
    lines += [f"{p} - {d}" for p, d in PROMPT_PREFIXES]
    return "\n".join(lines)


# ─── Activity signals ─────────────────────────────────────────────────────

def _workspace(agent: Any) -> Path | None:
    try:
        ws = agent.settings.workspace
        return ws if isinstance(ws, Path) else (Path(ws) if ws else None)
    except Exception:
        return None


_HIST_CACHE: dict[str, Any] = {"key": None, "lines": []}
_HIST_LOCK = threading.Lock()


def _history_lines(agent: Any) -> list[str]:
    """Prompts submitted in this project (history.jsonl, cached on mtime) plus this session's."""
    lines: list[str] = []
    try:
        from axon.ui import input as ui_input
        path = ui_input.history_path()
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size, str(_workspace(agent)))
        except OSError:
            key = None
        with _HIST_LOCK:
            if key is not None and key == _HIST_CACHE["key"]:
                lines = list(_HIST_CACHE["lines"])
            elif key is not None:
                ws = _workspace(agent)
                lines = ui_input.load_history(limit=400, cwd=str(ws) if ws else None)
                _HIST_CACHE["key"], _HIST_CACHE["lines"] = key, list(lines)
        sess = list(getattr(ui_input, "_SESSION_HISTORY", []) or [])
        lines += [s for s in sess[-50:] if s not in lines[-50:]]
    except Exception:
        pass
    return lines


@dataclass
class Activity:
    model: str = ""
    provider: str = ""
    mode: str = ""
    effort: str = ""
    trace_level: str = ""
    turns: int = 0
    cost: float = 0.0
    context_pct: float = 0.0
    recent_prompts: list[str] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    tool_errors: Counter = field(default_factory=Counter)
    tests: str = ""
    edits: int = 0
    commands_used: Counter = field(default_factory=Counter)
    prefixes_used: Counter = field(default_factory=Counter)
    steer_used: int = 0

    def fingerprint(self) -> str:
        parts = [
            self.model, ",".join(sorted(self.tools)), ",".join(sorted(self.tool_errors)), self.tests,
            "edits" if self.edits else "", str(int(self.context_pct // 25)),
            ",".join(sorted(self.commands_used)), ",".join(sorted(self.prefixes_used)), "steer" if self.steer_used else "",
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def words(self) -> set[str]:
        """Vocabulary for ranking registry-derived tips by relevance."""
        bag = " ".join(self.recent_prompts + list(self.tools)).lower()
        if self.tool_errors:
            bag += " error errors fix debug trace failed"
        if self.tests:
            bag += " test tests verify"
        if self.tests == "failed":
            bag += " fix autofix"
        if self.edits:
            bag += " diff review commit rewind changes edits"
        if self.context_pct >= 50:
            bag += " context compact window tokens"
        if self.cost >= 0.5:
            bag += " cost budget pricing fast"
        if self.turns >= 2:
            bag += " queue side question working"
        return {w for w in re.findall(r"[a-z][a-z0-9_-]{2,}", bag)}

    def as_text(self) -> str:
        rows = [f"model: {self.model}" + (f" (provider {self.provider})" if self.provider else ""),
                f"permission mode: {self.mode} · effort: {self.effort} · trace level: {self.trace_level}",
                f"turns this session: {self.turns} · cost so far: ${self.cost:.4f} · context used: {self.context_pct:.0f}%"]
        if self.recent_prompts:
            rows.append("recent prompts (newest last):")
            rows += [f"  - {p}" for p in self.recent_prompts]
        if self.tools:
            rows.append("tools used recently: " + ", ".join(f"{k}x{v}" for k, v in self.tools.most_common(12)))
        if self.tool_errors:
            rows.append("tool errors recently: " + ", ".join(f"{k}x{v}" for k, v in self.tool_errors.most_common(8)))
        if self.tests:
            rows.append(f"last test run: {self.tests}")
        if self.edits:
            rows.append(f"files edited recently: {self.edits}")
        rows.append("slash commands the user already uses: "
                    + (", ".join(f"/{k}" for k, _ in self.commands_used.most_common(30)) or "none"))
        rows.append("prompt prefixes already used: " + (", ".join(self.prefixes_used) or "none"))
        rows.append(f"typed while the agent was working (queued/steer messages): {'yes' if self.steer_used else 'never'}")
        return "\n".join(rows)


def collect_activity(agent: Any, history: list[str] | None = None, recent_turns: int = 5) -> Activity:
    act = Activity()
    s = getattr(agent, "settings", None)
    act.model = str(getattr(s, "model", "") or "")
    act.provider = str(getattr(getattr(agent, "provider", None), "name", "") or getattr(s, "provider", "") or "")
    act.mode = str(getattr(s, "mode", "") or "")
    act.effort = str(getattr(s, "effort", "") or "")
    act.trace_level = str(getattr(s, "trace_level", "") or "")
    traces = list(getattr(agent, "traces", None) or [])
    act.turns = len(traces)
    try:
        act.cost = float(agent.get_combined_cost())
    except Exception:
        try:
            act.cost = float(agent.ledger.total())
        except Exception:
            act.cost = 0.0
    try:
        from axon.features import suggest as sg
    except Exception:
        sg = None  # type: ignore[assignment]
    recent = traces[-recent_turns:]
    for t in recent:
        prompt = clean_text(getattr(t, "prompt", ""), 160)
        if prompt and not prompt.startswith("/"):
            act.recent_prompts.append(prompt)
        for st in getattr(t, "steps", None) or []:
            if getattr(st, "kind", "") != "tool":
                continue
            d = st.data if isinstance(getattr(st, "data", None), dict) else {}
            name = str(d.get("name") or "tool")
            act.tools[name] += 1
            if d.get("is_error"):
                act.tool_errors[name] += 1
    act.recent_prompts = act.recent_prompts[-6:]
    if sg is not None and recent:
        try:
            act.context_pct = float(sg.context_pct(recent[-1]))
            act.tests = sg.tests_outcome(recent[-1])
            act.edits = sum(len(sg.edited_files(t)) for t in recent)
        except Exception:
            pass
    for line in (history if history is not None else _history_lines(agent)):
        line = (line or "").strip()
        if not line:
            continue
        head = line[0]
        if head == "/" and len(line) > 1:
            name = line[1:].split(None, 1)[0].lower()
            if re.fullmatch(r"[a-z][\w:-]*", name):
                act.commands_used[_canonical_command(name)] += 1
        elif head in "!#?":
            act.prefixes_used[head] += 1
        if re.search(r"(?:^|\s)@[\w./~-]", line):
            act.prefixes_used["@"] += 1
    try:
        from axon.agent.loop import STEER_PREFIX
    except Exception:
        STEER_PREFIX = "[User added while you were working]:"
    try:
        for m in getattr(getattr(agent, "conversation", None), "messages", None) or []:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = m.get("content")
            blocks = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if isinstance(b, dict)
                                                       and b.get("type") == "text"] if isinstance(c, list) else []
            if any(isinstance(b, str) and STEER_PREFIX in b for b in blocks):
                act.steer_used += 1
    except Exception:
        pass
    return act


# ─── Parsing and validation ───────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_OBJ_RE = re.compile(r'\{[^{}]*"text"\s*:\s*"(?:[^"\\]|\\.)*"[^{}]*\}', re.S)


def parse_tips_json(text: str) -> list[Any]:
    """Extract the tip list from a model reply: strict JSON, fenced JSON, or JSON followed /
    preceded by prose. Accepts ``{"tips": [...]}`` or a bare list. Returns [] when nothing parses."""
    if not text:
        return []
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    dec = json.JSONDecoder()
    single: list[Any] = []
    for cand in candidates:
        for i, ch in enumerate(cand):
            if ch not in "[{":
                continue
            try:
                obj, _end = dec.raw_decode(cand[i:])
            except ValueError:
                continue
            if isinstance(obj, dict):
                for key in ("tips", "items", "data"):
                    if isinstance(obj.get(key), list):
                        return list(obj[key])
                if "text" in obj and not single:
                    single = [obj]      # one bare tip object; a cut-off list may hold more (below)
                continue
            if isinstance(obj, list):
                return list(obj)
    # A reply cut off by the token limit: keep every complete {"text": ...} object that arrived.
    salvaged = []
    for m in _OBJ_RE.finditer(text):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("text"):
            salvaged.append(obj)
    return salvaged if len(salvaged) > len(single) else single


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def validate_tips(items: Iterable[Any], commands: set[str] | None = None, keys: set[str] | None = None,
                  limit: int = MAX_TIPS) -> list[Tip]:
    """Keep tips that only mention known /commands and key combos, fit MAX_TIP_CHARS and are unique."""
    cmds = {c.lower() for c in (commands if commands is not None else known_commands())}
    keyset = keys if keys is not None else known_keys()
    out: list[Tip] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, Tip):
            raw_text, raw_cmd, raw_reason = item.text, item.command, item.reason
        elif isinstance(item, dict):
            raw_text = item.get("text") or item.get("tip") or ""
            raw_cmd, raw_reason = item.get("command") or "", item.get("reason") or ""
        elif isinstance(item, str):
            raw_text, raw_cmd, raw_reason = item, "", ""
        else:
            continue
        text = clean_text(raw_text)
        command = clean_text(raw_cmd)
        reason = clean_text(raw_reason, 160)
        if not (MIN_TIP_CHARS <= len(text) <= MAX_TIP_CHARS):
            continue
        if len(command) > 40:
            command = ""
        if command and command.startswith("/"):
            command = "/" + command[1:].split(None, 1)[0] if command[1:].strip() else ""
        blob = f"{text} {command}"
        if any(c not in cmds for c in mentioned_commands(blob)):
            continue
        if command.startswith("/") and command[1:].lower() not in cmds:
            continue
        bad_key = False
        for k in mentioned_keys(blob):
            if k.startswith("?") or any(part not in keyset for part in k.split()):
                bad_key = True
                break
        if bad_key:
            continue
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(Tip(text=text, command=command, reason=reason))
        if len(out) >= limit:
            break
    return out


# ─── Registry-derived fallback ────────────────────────────────────────────

def _day_salt() -> str:
    return time.strftime("%Y-%m-%d")


def _stable_rank(s: str) -> int:
    return int(hashlib.md5(f"{_day_salt()}:{s}".encode("utf-8")).hexdigest()[:8], 16)


def registry_tips(agent: Any = None, activity: Activity | None = None, limit: int = FALLBACK_TIPS) -> list[Tip]:
    """Tips derived from the live command registry and keybinding table (no LLM), most relevant
    to the current activity first; commands the user already uses are skipped."""
    act = activity or Activity()
    words = act.words()
    used = set(act.commands_used)

    def score(name: str, desc: str) -> tuple[int, int]:
        toks = set(re.findall(r"[a-z][a-z0-9_-]{2,}", f"{name} {desc}".lower()))
        return (-len(toks & words), _stable_rank(name))

    cmd_rows = []
    for c in _registry_commands():
        if c.name in used or not c.description or getattr(c, "hidden", False):
            continue
        alias = c.aliases[0] if c.aliases and len(c.aliases[0]) < len(c.name) else ""
        label = f"/{c.name}" + (f" (/{alias})" if alias else "")
        cmd_rows.append((score(c.name, c.description), Tip(text=_clip(ascii_fold(f"{label} - {c.description}"), MAX_TIP_CHARS),
                                                           command=f"/{c.name}", reason="from the command list")))
    cmd_rows.sort(key=lambda r: r[0])
    key_rows = []
    for action, ks in _bindings().items():
        if not ks or action.split(":", 1)[0] not in ("app", "chat", "history"):
            continue
        if action in ("chat:submit", "chat:newline", "chat:exit", "chat:interrupt"):
            continue
        desc = _describe(action)
        if not desc:
            continue
        key = _fmt_key(ks[0])
        key_rows.append((score(action, desc), Tip(text=_clip(ascii_fold(f"{key} - {desc}"), MAX_TIP_CHARS), command=key,
                                                  reason="from the keyboard shortcuts")))
    key_rows.sort(key=lambda r: r[0])
    out: list[Tip] = []
    ci = ki = 0
    while len(out) < limit and (ci < len(cmd_rows) or ki < len(key_rows)):
        for _ in range(2):
            if ci < len(cmd_rows) and len(out) < limit:
                out.append(cmd_rows[ci][1])
                ci += 1
        if ki < len(key_rows) and len(out) < limit:
            out.append(key_rows[ki][1])
            ki += 1
    return out


# ─── LLM prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You write short, practical usage tips for Axon, a terminal coding agent. They appear under the "
    "working spinner while the agent runs.\n"
    "Rules:\n"
    "- Use ONLY features listed in PRODUCT FACTS. Never invent commands, flags, keys or settings. "
    "Copy /command names and key combos exactly as listed.\n"
    f"- Each tip is one imperative sentence of at most {MAX_TIP_CHARS - 5} characters, plain ASCII text: "
    "no markdown, no emoji, no em dashes, no 'Tip:' prefix.\n"
    "- Fit what the user is doing now (ACTIVITY). Prefer features they have NOT used yet that would "
    "help with their current work; skip things they already do.\n"
    "- Be concrete: name the command or key and what it gets them.\n"
    f"- Reply with JSON only: {{\"tips\": [{{\"text\": \"...\", \"command\": \"/name, a key combo or empty\", "
    f"\"reason\": \"why it fits now, <= 15 words\"}}]}} containing {TARGET_TIPS} tips."
)


def build_prompt(agent: Any, activity: Activity | None = None) -> str:
    act = activity or collect_activity(agent)
    return (f"PRODUCT FACTS\n{product_facts(agent)}\n\nACTIVITY\n{act.as_text()}\n\n"
            f"Write {TARGET_TIPS} tips as JSON.")


# ─── Cache ────────────────────────────────────────────────────────────────

def cache_path(workspace: Path | None, create: bool = False) -> Path | None:
    if workspace is None:
        return None
    try:
        from axon.storage import project_dir
        return project_dir(Path(workspace), create=create) / CACHE_FILE
    except Exception:
        return None


def save_cache(workspace: Path | None, tips: list[Tip], meta: dict[str, Any]) -> bool:
    path = cache_path(workspace, create=True)
    if path is None:
        return False
    data = {"version": CACHE_VERSION, **meta, "tips": [asdict(t) for t in tips]}
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def load_cache(workspace: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = cache_path(workspace, create=False)
    if path is None or not path.is_file():
        return [], {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("tips"), list):
        return [], {}
    meta = {k: v for k, v in data.items() if k not in ("tips", "version")}
    return list(data["tips"]), meta


# ─── Engine ───────────────────────────────────────────────────────────────

def _env_disabled() -> bool:
    return os.environ.get("AXON_NO_TIPS", "").strip().lower() not in ("", "0", "false", "no", "off")


class TipEngine:
    """Holds the tip pool, decides when to refresh it and serves spinner / footer tips."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agent: Any = None
        self._workspace: Path | None = None
        self._cache_loaded_for: str | None = None
        self._llm_tips: list[Tip] = []
        self._meta: dict[str, Any] = {}
        self._fallback: list[Tip] | None = None
        self._fallback_key: str | None = None
        self._fingerprint = ""
        self._turns = 0
        self._turns_at_refresh = 0
        self._inflight_since = 0.0
        self._last_attempt = 0.0
        self.last_error = ""
        self._footer_seen: set[str] = set()
        self._last_footer = ""
        self._pool_version = 0
        self._footer_version = -1
        self._footer_turn: int | None = None
        self._t0 = time.monotonic()
        self._offset = int(time.time() * 1000) % 997
        # Test / embedding hooks
        self.interactive: bool | None = None          # None: TTY and not under pytest
        self.provider_factory: Callable[[Any], tuple[Any, str]] | None = None
        self.timeout_s = LLM_TIMEOUT_S
        self.on_refresh: Callable[[list[Tip]], None] | None = None
        self._meta_model = ""
        self._last_spinner = ""

    # ── binding / switches ───────────────────────────────────────────
    def bind(self, agent: Any) -> "TipEngine":
        if agent is None or getattr(agent, "is_subagent", False):
            return self
        with self._lock:
            self._agent = agent
            ws = _workspace(agent)
            self._workspace = ws
            key = str(ws) if ws is not None else ""
            if key != self._cache_loaded_for:
                self._cache_loaded_for = key
                self._load_cache_locked(agent)
        return self

    def _load_cache_locked(self, agent: Any) -> None:
        items, meta = load_cache(self._workspace)
        self._llm_tips = validate_tips(items, known_commands(agent), known_keys()) if items else []
        self._meta = meta if self._llm_tips else {}
        self._fallback = None

    def _settings(self, agent: Any = None) -> Any:
        return getattr(agent if agent is not None else self._agent, "settings", None)

    def enabled(self, agent: Any = None) -> bool:
        if _env_disabled():
            return False
        return bool(getattr(self._settings(agent), "tips", True))

    def _is_interactive(self) -> bool:
        if self.interactive is not None:
            return bool(self.interactive)
        if "PYTEST_CURRENT_TEST" in os.environ:
            return False
        try:
            from axon import platform as plat
            return plat.is_interactive()
        except Exception:
            return False

    def llm_allowed(self, agent: Any = None) -> bool:
        agent = agent if agent is not None else self._agent
        if agent is None or getattr(agent, "is_subagent", False) or not self.enabled(agent):
            return False
        if not bool(getattr(self._settings(agent), "tips_llm", True)):
            return False
        return self._is_interactive()

    # ── pool ─────────────────────────────────────────────────────────
    @property
    def source(self) -> str:
        with self._lock:
            if self._llm_tips:
                return str(self._meta.get("origin", "llm"))
            return "registry"

    @property
    def meta(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._meta)

    def tips(self) -> list[Tip]:
        with self._lock:
            if self._llm_tips:
                return list(self._llm_tips)
            if self._fallback is not None and self._fallback_key == self._fingerprint:
                return list(self._fallback)
            agent, key = self._agent, self._fingerprint
        try:  # registry scan + history read happen outside the lock
            fallback = registry_tips(agent, collect_activity(agent) if agent is not None else Activity())
        except Exception:
            fallback = []
        with self._lock:
            if self._llm_tips:
                return list(self._llm_tips)
            self._fallback, self._fallback_key = fallback, key
            return list(fallback)

    def _pool_nowait(self) -> list[Tip] | None:
        """The pool without blocking: None when another thread holds the lock."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._llm_tips:
                return list(self._llm_tips)
            if self._fallback is not None:
                return list(self._fallback)
        finally:
            self._lock.release()
        return self.tips()

    def spinner_tip(self) -> str:
        """Plain text, <= MAX_TIP_CHARS, rotating every ROTATE_S seconds ("" when off / empty).
        Never waits on the engine lock (returns the previous tip, or "", if it is busy)."""
        try:
            if not self.enabled():
                return ""
            pool = self._pool_nowait()
            if pool is None:
                return self._last_spinner
            if not pool:
                return ""
            idx = (int((time.monotonic() - self._t0) // ROTATE_S) + self._offset) % len(pool)
            self._last_spinner = display_text(clean_text(pool[idx].text, MAX_TIP_CHARS))
            return self._last_spinner
        except Exception:
            return ""

    def footer_tip(self) -> str:
        """A tip not shown in the footer yet, offered when the pool is new (fresh model-written tips,
        or the first footer of the session) or FOOTER_EVERY_TURNS turns after the last one.
        "" when off, not due, or every tip in the pool was already shown."""
        try:
            if not self.enabled():
                return ""
            pool = self.tips()
            with self._lock:
                new_pool = self._pool_version != self._footer_version
                due = self._footer_turn is None or self._turns - self._footer_turn >= FOOTER_EVERY_TURNS
                if not (new_pool or due):
                    return ""
                for t in pool:
                    k = _norm(t.text)
                    if k in self._footer_seen or t.text == self._last_footer:
                        continue
                    self._footer_seen.add(k)
                    self._last_footer = t.text
                    self._footer_version = self._pool_version
                    self._footer_turn = self._turns
                    return display_text(clean_text(t.text, MAX_TIP_CHARS))
            return ""
        except Exception:
            return ""

    def set_tips(self, tips: list[Tip], meta: dict[str, Any] | None = None, persist: bool = True) -> None:
        with self._lock:
            self._llm_tips = list(tips)
            self._meta = dict(meta or {})
            self._fallback = None
            self._pool_version += 1
            ws = self._workspace
        if persist and tips:
            save_cache(ws, tips, self._meta)
            try:
                from axon.session import activity
                activity.record("tips", {"count": len(tips), "origin": self._meta.get("origin", ""),
                                         "model": self._meta.get("model", ""),
                                         "tips": [str(getattr(t, "text", t))[:200] for t in tips[:12]]},
                                workspace=ws)
            except Exception:
                pass
        cb = self.on_refresh
        if cb is not None:
            try:
                cb(list(tips))
            except Exception:
                pass

    # ── refresh policy ───────────────────────────────────────────────
    def _inflight(self, now: float) -> bool:
        return bool(self._inflight_since) and now - self._inflight_since < 2 * self.timeout_s

    def should_refresh(self, agent: Any = None, now: float | None = None) -> bool:
        agent = agent if agent is not None else self._agent
        if not self.llm_allowed(agent):
            return False
        now = time.time() if now is None else now
        s = self._settings(agent)
        turns = max(1, int(getattr(s, "tips_refresh_turns", DEFAULT_REFRESH_TURNS) or DEFAULT_REFRESH_TURNS))
        gap = max(0.0, float(getattr(s, "tips_refresh_minutes", DEFAULT_REFRESH_MINUTES) or 0.0)) * 60.0
        with self._lock:
            if self._inflight(now):
                return False
            if self._last_attempt and now - self._last_attempt < gap:
                return False
            if not self._llm_tips:
                return self._turns >= min(turns, FIRST_REFRESH_TURNS)
            if self._meta.get("fingerprint") == self._fingerprint:
                return False
            if self._turns - self._turns_at_refresh < turns:
                return False
            generated = float(self._meta.get("generated_at") or 0.0)
            return now - generated >= gap

    def observe_turn(self, agent: Any, result: Any = None) -> None:
        """Note a finished REPL turn; maybe start a background refresh. Never blocks or raises."""
        try:
            if agent is None or getattr(agent, "is_subagent", False):
                return
            self.bind(agent)
            try:
                fp = collect_activity(agent).fingerprint()
            except Exception:
                fp = self._fingerprint
            with self._lock:
                self._turns += 1
                self._fingerprint = fp
            if _agent_busy(agent):
                return
            if self.should_refresh(agent):
                self.refresh(agent, background=True)
        except Exception:
            pass

    def refresh(self, agent: Any = None, background: bool = True, force: bool = False) -> bool:
        """Start (or, with background=False, run) one LLM generation. Returns False when not started."""
        agent = agent if agent is not None else self._agent
        if agent is None or not self.llm_allowed(agent):
            return False
        now = time.time()
        with self._lock:
            if self._inflight(now):
                self.last_error = "a refresh is already running"
                return False
            self._inflight_since = now
            self._last_attempt = now
            self._turns_at_refresh = self._turns
        if background:
            threading.Thread(target=self._run, args=(agent,), name="axon-tips", daemon=True).start()
            return True
        return self._run(agent)

    def _run(self, agent: Any) -> bool:
        self.last_error = ""
        try:
            tips = self.generate(agent)
            if tips:
                self.set_tips(tips, {"origin": "llm", "generated_at": time.time(), "model": self._meta_model,
                                     "fingerprint": self._fingerprint})
                self.last_error = ""
                return True
            self.last_error = self.last_error or "the model returned no usable tips"
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            try:
                from axon.session import activity
                activity.record("tips", {"error": self.last_error}, workspace=self._workspace)
            except Exception:
                pass
            return False
        finally:
            with self._lock:
                self._inflight_since = 0.0

    # ── the LLM call ─────────────────────────────────────────────────
    def _provider(self, agent: Any) -> tuple[Any, str]:
        if self.provider_factory is not None:
            return self.provider_factory(agent)
        s = agent.settings
        main = getattr(agent, "provider", None)
        if main is None or not type(main).__module__.startswith("axon.providers"):
            return None, ""  # custom / SDK provider: never share its per-call state from a side thread
        from axon.providers.registry import provider_for
        for model in dict.fromkeys(m for m in (getattr(s, "small_model", None), s.model) if m):
            try:
                return provider_for(model, s.model_copy(update={"model": model}), failover=False), model
            except Exception:
                continue
        return None, ""

    def generate(self, agent: Any) -> list[Tip]:
        provider, model = self._provider(agent)
        if provider is None:
            self.last_error = "no provider available for tips"
            return []
        act = collect_activity(agent)
        prompt = build_prompt(agent, act)
        if _agent_busy(agent):  # the user started a new turn: retry after a later turn
            with self._lock:
                self._last_attempt = 0.0
            self.last_error = "skipped: a turn is running"
            return []
        start = time.monotonic()
        stream = provider.stream(model=model, system=[{"type": "text", "text": SYSTEM_PROMPT}],
                                 messages=[{"role": "user", "content": prompt}], tools=[],
                                 max_tokens=LLM_MAX_TOKENS, effort="low", thinking=False)
        for _ in stream:
            if time.monotonic() - start > self.timeout_s:
                raise TimeoutError(f"tips call exceeded {self.timeout_s:.0f}s")
        turn = provider.finalize()
        self._record_cost(agent, model, getattr(turn, "usage", None), time.monotonic() - start)
        self._meta_model = model
        with self._lock:
            self._fingerprint = act.fingerprint()
        items = parse_tips_json(str(getattr(turn, "text", "") or ""))
        tips = validate_tips(items, known_commands(agent), known_keys())
        if not tips:
            self.last_error = "the model returned no usable tips"
        return tips

    @staticmethod
    def _record_cost(agent: Any, model: str, usage: Any, duration_s: float) -> None:
        if usage is None:
            return
        cost: Any = 0.0
        ledger = getattr(agent, "ledger", None)
        try:
            from axon.agent.state import record_usage
            cost = record_usage(ledger, model, usage, source=SOURCE)
        except Exception:
            try:
                if ledger is not None:
                    cost = ledger.record(model, usage, source=SOURCE)
            except Exception:
                pass
        log = getattr(agent, "_log_usage", None)
        if callable(log):
            try:
                log(model, usage, float(cost or 0.0), SOURCE, duration_s)
            except Exception:
                pass


def _agent_busy(agent: Any) -> bool:
    try:
        return bool(getattr(agent, "busy", False))
    except Exception:
        return False


_ENGINE: TipEngine | None = None
_ENGINE_LOCK = threading.Lock()


def tip_engine(agent: Any = None) -> TipEngine:
    """The process-wide engine; binds ``agent`` (and loads its project's cached tips) when given."""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = TipEngine()
        eng = _ENGINE
    if agent is not None:
        eng.bind(agent)
    return eng


def reset_tip_engine() -> None:
    """Drop the singleton (tests)."""
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None


# ─── /tips ────────────────────────────────────────────────────────────────

def _print(text: str) -> None:
    try:
        from axon.ui.theme import safe_text
        print(safe_text(text), flush=True)
    except Exception:
        try:
            print(text, flush=True)
        except Exception:
            pass


def _ok() -> str:
    try:
        from axon.ui.theme import glyph
        return glyph("ok")
    except Exception:
        return "+"


def _dot() -> str:
    try:
        from axon.ui.theme import glyph
        return glyph("dot")
    except Exception:
        return "-"


def _set_setting(agent: Any, persist_key: str, **changes: Any) -> None:
    try:
        from axon.commands.common import update_settings, update_settings_file
        update_settings(agent, **changes)
        update_settings_file({persist_key: next(iter(changes.values()))})
    except Exception:
        agent.settings = agent.settings.model_copy(update=changes)


def _list(agent: Any, eng: TipEngine) -> None:
    from axon.ui.theme import BOLD, DIM, GOLD, MINT, RST, SLATE, WHITE
    pool = eng.tips()
    src = eng.source
    meta = eng.meta
    if src == "registry":
        how = "from the command list (no model-written tips yet)"
    else:
        age = ""
        if meta.get("generated_at"):
            mins = max(0, int((time.time() - float(meta["generated_at"])) // 60))
            age = f", {mins} min ago" if mins < 120 else f", {mins // 60} h ago"
        how = f"written by {meta.get('model') or 'the model'}{age}"
    state = "on" if eng.enabled(agent) else "off"
    llm = "on" if bool(getattr(agent.settings, "tips_llm", True)) else "off"
    dot = _dot()
    _print(f"\n  {GOLD}{BOLD}Tips{RST}  {SLATE}{how} {dot} tips {state} {dot} model-written {llm}{RST}")
    if _env_disabled():
        _print(f"  {SLATE}AXON_NO_TIPS is set: tips are hidden.{RST}")
    for i, t in enumerate(pool, 1):
        _print(f"   {MINT}{i:>2}.{RST} {WHITE}{display_text(t.text)}{RST}")
        if t.reason and src != "registry":
            _print(f"       {DIM}{display_text(t.reason)}{RST}")
    if not pool:
        _print(f"  {SLATE}No tips right now.{RST}")
    if eng.last_error and src == "registry":
        _print(f"  {DIM}Last refresh: {eng.last_error}{RST}")
    _print(f"  {DIM}/tips refresh {dot} /tips off|on {dot} /tips llm off|on{RST}\n")


def handle_tips(agent: Any, arg: str) -> None:
    from axon.ui.theme import GOLD, MINT, RST, SLATE
    eng = tip_engine(agent)
    words = (arg or "").strip().lower().split()
    sub = words[0] if words else ""
    if sub in ("off", "disable", "on", "enable"):
        on = sub in ("on", "enable")
        _set_setting(agent, "tips", tips=on)
        _print(f"  {MINT}{_ok()}{RST} Tips {'on' if on else 'off'} {SLATE}(saved to ~/.axon/settings.json){RST}")
        return
    if sub == "llm" and len(words) > 1 and words[1] in ("on", "off"):
        on = words[1] == "on"
        _set_setting(agent, "tipsLlm", tips_llm=on)
        _print(f"  {MINT}{_ok()}{RST} Model-written tips {'on' if on else 'off'} {SLATE}(saved to ~/.axon/settings.json){RST}")
        return
    if sub in ("refresh", "new", "regen", "regenerate"):
        if not eng.enabled(agent):
            _print(f"  {GOLD}Tips are off.{RST} {SLATE}Turn them on with /tips on.{RST}")
            return
        if not bool(getattr(agent.settings, "tips_llm", True)):
            _print(f"  {GOLD}Model-written tips are off.{RST} {SLATE}/tips llm on enables them.{RST}")
            return
        if not eng.llm_allowed(agent):
            _print(f"  {SLATE}Model-written tips are generated only in an interactive session.{RST}")
            return
        _print(f"  {SLATE}Writing tips for what you are doing...{RST}")
        if not eng.refresh(agent, background=False, force=True):
            _print(f"  {GOLD}Could not refresh tips{RST}{SLATE}: {eng.last_error or 'a refresh is already running'}{RST}")
        _list(agent, eng)
        return
    if sub and sub not in ("list", "ls", "show"):
        _print(f"  {SLATE}Usage: /tips [refresh | on | off | llm on | llm off]{RST}")
        return
    _list(agent, eng)


COMMANDS = [
    FeatureCommand(name="/tips", handler=handle_tips, category="Getting started",
                   description="Usage tips for what you are doing (model-written, grounded in the command list)",
                   usage="/tips                list the current tips\n/tips refresh        write fresh tips now\n"
                         "/tips off|on         hide / show tips\n/tips llm off|on     registry-only / model-written tips"),
]

__all__ = ["Tip", "TipEngine", "Activity", "tip_engine", "reset_tip_engine", "product_facts", "collect_activity",
           "registry_tips", "parse_tips_json", "validate_tips", "known_commands", "known_keys", "clean_text",
           "build_prompt", "save_cache", "load_cache", "cache_path", "handle_tips", "COMMANDS"]
