"""
Interactive prompt line editor for the Axon REPL.

Features: persistent history (~/.axon/history, prefix-filtered with ↑/↓), multi-line input
(Shift+Enter / Alt+Enter / Ctrl+J / trailing backslash / paste), slash-command menu (built-in
and custom commands), @file completion, Shift+Tab mode cycling, wide-character aware wrapping,
live resize, undo, stash, external editor, and a plain input() fallback when not on a TTY.
All key handling goes through axon.platform so it behaves the same on Windows consoles,
macOS and Linux.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axon import platform as plat
from axon.ui.picker import LiveFrame
from axon.ui.theme import (
    AMBER, BOLD, CYAN, DARK_SLATE, GOLD, GRAY_BG, MINT, PURPLE, ROSE, RST, SLATE, TEAL, UNDER, WHITE,
    term_width,
)

_HAS_TERMIOS = plat.HAS_TERMIOS  # back-compat flag

MODES_CYCLE = ["default", "acceptEdits", "plan", "bypass"]
SWITCH_SESSION = "__SWITCH_SESSION__"
HISTORY_LIMIT = 1000

# In-process history of this session (also persisted to ~/.axon/history).
_SESSION_HISTORY: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Styling helpers (public: used by cli/session code and tests)
# ─────────────────────────────────────────────────────────────────────────────

def safe_ansi_truncate(text: str, max_w: int) -> str:
    """Truncate to max_w visible cells without corrupting ANSI codes."""
    if plat.str_width(text) <= max_w:
        return text
    return plat.truncate_ansi(text, max_w, ellipsis="") + RST


def highlight_prompt_syntax(text: str, allow_prefix: bool = True) -> str:
    """Colour @file mentions, /commands, !shell, `code`, quotes and image tokens."""
    if not text:
        return ""
    if allow_prefix and text.startswith("!"):
        return f"{AMBER}{BOLD}!{RST}{GOLD}{BOLD}{text[1:]}{RST}"
    if allow_prefix and text.startswith("#"):
        return f"{PURPLE}{BOLD}#{RST}{PURPLE}{text[1:]}{RST}"
    if allow_prefix and text.startswith("/"):
        name, sep, rest = text.partition(" ")
        return f"{MINT}{BOLD}{name}{RST}{WHITE}{sep}{rest}{RST}"
    base = f"{RST}{WHITE}"
    styled = re.sub(r"(\[Image\s*#\d+\])", f"{CYAN}{BOLD}\\1{base}", text)
    styled = re.sub(r"((?<![\w])@[\w\-./\\:]+)", f"{CYAN}{UNDER}\\1{base}", styled)
    styled = re.sub(r"(`[^`]+`)", f"{TEAL}\\1{base}", styled)
    styled = re.sub(r"(\"[^\"\n]*\"|'[^'\n]*')", f"{GOLD}\\1{base}", styled)
    styled = THINK_KEYWORD_RE.sub(lambda m: f"{PURPLE}{BOLD}{m.group(0)}{base}", styled)
    return f"{WHITE}{styled}{RST}"


# Thinking keywords raise the effort for one turn (the agent loop emits ThinkingBoost).
THINK_KEYWORD_RE = re.compile(r"\b(?:ultrathink|think\s+harder|think\s+hard|megathink)\b", re.IGNORECASE)
# Zero-width and bidi-control characters that can hide text from the reader.
INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def format_user_prompt_banner(text: str, subagent_label: str | None = "", is_bash: bool = False,
                              max_w: int | None = None) -> str:
    """Render a submitted prompt as a full-width tinted block (echoed into the transcript)."""
    from axon.ui.theme import layout_width
    tw = max_w or layout_width(0)            # same measure as the turn timeline
    width = max(20, tw - 4)
    lines = text.strip().splitlines() or [""]
    if is_bash:
        first = "! "
        lines[0] = lines[0][1:].lstrip() if lines[0].startswith("!") else lines[0]   # the prefix already shows it
    elif subagent_label:
        first = f"[{subagent_label}] › "
    else:
        first = "› "
    out = []
    for i, line in enumerate(lines):
        pref = first if i == 0 else "  "
        body = highlight_prompt_syntax(line, allow_prefix=(i == 0 and not is_bash)).replace(RST, f"{RST}{GRAY_BG}")
        avail = width - 2 - plat.str_width(pref)
        if plat.str_width(body) > avail:
            body = plat.truncate_ansi(body, avail) + GRAY_BG
        pad = max(0, avail - plat.str_width(body))
        out.append(f"  {GRAY_BG}{BOLD}{WHITE} {pref}{RST}{GRAY_BG}{body}{' ' * pad} {RST}")
    return "\n".join(out)


# name, kind ("core"/"ext"/"custom"), category, description, takes_args
ALL_SLASH_COMMANDS: list[tuple[str, str, str, str, bool]] = [
    ("/model", "core", "Core & Models", "Switch active LLM (Claude, GPT, DeepSeek)", True),
    ("/keys", "core", "Core & Models", "API keys: add/replace (verified), test, remove, where stored", True),
    ("/endpoint", "core", "Core & Models", "Provider base URLs: redirect to a proxy, add custom endpoints, test, reset", True),
    ("/settings", "core", "Core & Models", "All settings in one place: model, keys, endpoint, permissions, display, cost, data", True),
    ("/provider", "core", "Core & Models", "Connect local or cloud engine (Ollama, LM Studio, OpenRouter, Anthropic, OpenAI)", False),
    ("/env", "core", "Core & Models", "Inspect runtime environment variables and provider keys", False),
    ("/effort", "core", "Core & Models", "Adjust reasoning tier (reflex, balanced, synapse, quantum)", True),
    ("/config", "core", "Core & Models", "View and adjust runtime configuration parameters", True),
    ("/status", "core", "Core & Models", "View live system and agent status", False),
    ("/permissions", "core", "Core & Models", "Inspect permission rules and mode", False),
    ("/plan", "core", "Core & Models", "View task checklist or switch to plan mode", True),
    ("/breakdown", "core", "Context & Tokens", "Full input prompt breakdown & token matching", False),
    ("/context", "core", "Context & Tokens", "View context token budget and limits", False),
    ("/compact", "core", "Context & Tokens", "Compact conversation context while preserving key facts", False),
    ("/statusbar", "core", "Context & Tokens", "Live token capacity gauge and metrics", False),
    ("/analytics", "core", "Context & Tokens", "Lifetime workspace analytics and tool insights", False),
    ("/window", "core", "Context & Tokens", "Adjust sliding context window size (e.g. /window 10)", True),
    ("/cost", "core", "Context & Tokens", "Session cost ledger and prompt cache hits", False),
    ("/pricing", "core", "Context & Tokens", "Model pricing matrix & cache discounts", False),
    ("/payload", "core", "Context & Tokens", "Inspect exact prompt payload and tool results", True),
    ("/history", "core", "History & Diff", "View all messages in the conversation", False),
    ("/diff", "core", "History & Diff", "View uncommitted git diff", False),
    ("/review", "core", "History & Diff", "Run automated multi-file code review", True),
    ("/rewind", "core", "History & Diff", "Revert file edits made during previous turns", False),
    ("/copy", "core", "History & Diff", "Copy last response, code blocks, or diff to clipboard", True),
    ("/expand", "core", "History & Diff", "View full un-truncated output or expand any file", True),
    ("/transcript", "core", "History & Diff", "Full-screen transcript: click a tool call to expand it (Ctrl+F)", False),
    ("/export", "core", "History & Diff", "Export session to Markdown or JSON transcript", True),
    ("/subagents", "core", "Multi-Agent", "Subagent matrix & isolated worker transcripts", False),
    ("/agents", "core", "Multi-Agent", "List and manage custom subagent definitions", True),
    ("/todos", "core", "Multi-Agent", "View active multi-step task checklist", False),
    ("/q", "core", "Multi-Agent", "Prompt queue (/q <text>, /q drop <id>, /q clear)", True),
    ("/skills", "core", "Multi-Agent", "Browse skills and create new ones", True),
    ("/mcp", "ext", "Multi-Agent", "Model Context Protocol servers and tools", True),
    ("/plugin", "ext", "Multi-Agent", "Installed plugins and extension manifests", True),
    ("/hooks", "ext", "Multi-Agent", "Lifecycle and tool hooks", True),
    ("/memory", "core", "Multi-Agent", "Manage memory (list, add, delete, view, clear)", True),
    ("/learn", "core", "Multi-Agent", "Teach a new convention to persistent memory", True),
    ("/prompt", "core", "Prompt Engineering", "Optimize rough prompts or load agent templates", True),
    ("/optimize", "core", "Prompt Engineering", "Turn a simple prompt into precise agent instructions", True),
    ("/template", "core", "Prompt Engineering", "Load prompt templates (bugfix, refactor, testgen)", True),
    ("/main", "core", "Sessions & Root", "Switch back to the main agent session", False),
    ("/sessions", "core", "Sessions & Root", "Session switcher dashboard", False),
    ("/rename", "core", "Sessions & Root", "Rename the active session", True),
    ("/tag", "core", "Sessions & Root", "Tag the active session", True),
    ("/star", "core", "Sessions & Root", "Star the active session", False),
    ("/delete", "core", "Sessions & Root", "Delete a past session (/delete <id> or interactive)", True),
    ("/resume", "core", "Sessions & Root", "Resume a previous session", True),
    ("/branch", "core", "Sessions & Root", "Fork the conversation into an independent branch", True),
    ("/find", "core", "Sessions & Root", "Fuzzy file finder (Ctrl+P)", False),
    ("/test", "core", "Sessions & Root", "Run test suite (pytest, npm test, cargo test)", True),
    ("/notify", "core", "Sessions & Root", "Send a test desktop notification", True),
    ("/tools", "core", "Sessions & Root", "List agent tools, schemas and permissions", False),
    ("/doctor", "core", "Sessions & Root", "Local diagnostics & environment health check", False),
    ("/init", "ext", "Sessions & Root", "Create an AGENTS.md conventions file in the workspace", False),
    ("/faq", "core", "Session Control", "FAQs & error troubleshooting (/faq 402, 400, pricing)", True),
    ("/btw", "core", "Session Control", "Side question with zero context pollution (/btw <q>)", True),
    ("/voice", "core", "Session Control", "Toggle voice input mode", False),
    ("/kb", "core", "Session Control", "Keybindings and shortcuts cheat sheet", False),
    ("/help", "core", "Session Control", "Categorized command reference", False),
    ("/paste", "core", "Session Control", "Enter multiline paste mode", False),
    ("/thinking", "core", "Session Control", "Toggle reasoning display", True),
    ("/clear", "core", "Session Control", "Clear the conversation and start fresh", False),
    ("/exit", "core", "Session Control", "Save and exit", False),
]

_MODE_LABELS = {
    "default": ("manual mode on", SLATE, "⏵"),
    "acceptEdits": ("auto-accept edits on", TEAL, "⏵⏵"),
    "plan": ("plan mode on", PURPLE, "⏸"),
    "bypass": ("bypass permissions on", GOLD, "⚠"),
}


def _get_mode_info(mode: str) -> tuple[str, str]:
    label, color, _icon = _MODE_LABELS.get(mode, (f"{mode} mode", SLATE, "⏵"))
    return f"{label} (shift+tab to cycle)", color


def cycle_mode(mode: str, delta: int = 1) -> str:
    if mode not in MODES_CYCLE:
        return MODES_CYCLE[0]
    return MODES_CYCLE[(MODES_CYCLE.index(mode) + delta) % len(MODES_CYCLE)]


# ─────────────────────────────────────────────────────────────────────────────
# History
# ─────────────────────────────────────────────────────────────────────────────

def history_path() -> Path:
    """Global prompt history (JSONL records {ts, project, session, display})."""
    from axon.storage import user_file
    return user_file("history.jsonl")


def _legacy_history_path() -> Path:
    from axon.storage import user_file
    return user_file("history")


def _read_records(path: Path) -> list[dict[str, Any]]:
    try:
        from axon.storage import read_jsonl
        return read_jsonl(path)
    except ImportError:
        out: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            pass
        return out


def load_history(limit: int = HISTORY_LIMIT, cwd: str | None = None) -> list[str]:
    """Oldest-first prompts for this project (records without a project count everywhere),
    then this session's prompts; duplicates collapse to their most recent position. Entries from
    the legacy ~/.axon/history file are still read."""
    here = os.path.normcase(os.path.abspath(cwd or os.getcwd()))
    entries: list[str] = []
    for path in (_legacy_history_path(), history_path()):
        for rec in _read_records(path):
            text = str(rec.get("display") or rec.get("text") or "")
            proj = rec.get("project") or rec.get("cwd")
            if not text or (proj and os.path.normcase(os.path.abspath(str(proj))) != here):
                continue
            entries.append(text)
    entries.extend(_SESSION_HISTORY)
    seen: set[str] = set()
    out: list[str] = []
    for text in reversed(entries):
        if text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def add_history(text: str, session_id: str = "") -> None:
    """Record a submitted prompt (this module is the only writer of prompt history)."""
    text = text.strip()
    if not text:
        return
    try:   # `/keys set openai sk-…` must never reach history.jsonl
        from axon.providers.keystore import redact_command_line
        text = redact_command_line(text)
    except Exception:
        pass
    if not _SESSION_HISTORY or _SESSION_HISTORY[-1] != text:
        _SESSION_HISTORY.append(text)
    try:
        from axon.storage import record_history
        record_history(Path(os.getcwd()), text, session_id)
    except ImportError:
        try:
            path = history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "project": os.getcwd(), "session": session_id,
                                    "display": text}, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Completion sources
# ─────────────────────────────────────────────────────────────────────────────

_dyn_cache: dict[str, tuple[float, list[tuple[str, str, str, str, bool]]]] = {}


def registered_slash_commands(cwd: Path | None = None) -> list[tuple[str, str, str, str, bool]]:
    """Commands known to the command registry (custom commands, plugin and feature commands, skills,
    MCP prompts) that are not in the static built-in table. Cached for a few seconds."""
    key = str(cwd or os.getcwd())
    hit = _dyn_cache.get(key)
    if hit and time.monotonic() - hit[0] < 5.0:
        return hit[1]
    out: list[tuple[str, str, str, str, bool]] = []
    try:
        try:
            from axon.commands import list_command_names
        except ImportError:
            from axon.commands.registry import list_command_names  # type: ignore[no-redef]
        known = {c[0] for c in ALL_SLASH_COMMANDS}
        for name, desc in list_command_names(Path(key)):
            if name not in known:
                known.add(name)
                kind = "mcp" if name.startswith("/mcp__") else "custom"
                out.append((name, kind, "Custom & Skills", str(desc or "custom command")[:90], True))
    except Exception:
        out = []
    _dyn_cache[key] = (time.monotonic(), out)
    return out


def match_slash_commands(query: str, cwd: Path | None = None) -> list[tuple[str, str, str, str, bool]]:
    """Commands for the text after '/': exact, prefix, name-substring, then description matches."""
    q = query.lower().lstrip("/")
    cmds = ALL_SLASH_COMMANDS + registered_slash_commands(cwd)
    if not q:
        return list(cmds)
    prefix = sorted((c for c in cmds if c[0][1:].lower().startswith(q)), key=lambda c: c[0][1:].lower() != q)
    sub = [c for c in cmds if c not in prefix and q in c[0].lower()]
    desc = [c for c in cmds if c not in prefix and c not in sub and q in c[3].lower()]
    return prefix + sub + desc


_res_cache: dict[str, Any] = {"t": 0.0, "items": []}


def mcp_resource_mentions(ttl: float = 10.0) -> list[str]:
    """'server:uri' strings for resources of connected MCP servers (never starts a server)."""
    now = time.monotonic()
    if now - _res_cache["t"] < ttl:
        return list(_res_cache["items"])
    items: list[str] = []
    try:
        from axon.mcp import bridge as _bridge_mod
        bridge = getattr(_bridge_mod, "_bridge", None)
        conns = dict(getattr(bridge, "connections", {}) or {}) if bridge is not None else {}
        for name, conn in sorted(conns.items()):
            client = getattr(conn, "client", None)
            if not getattr(conn, "connected", False) or client is None:
                continue
            try:
                if hasattr(client, "has") and not client.has("resources"):
                    continue
                for res in client.list_resources(refresh=False)[:200]:
                    uri = str(res.get("uri") or "") if isinstance(res, dict) else ""
                    if uri:
                        items.append(f"{name}:{uri}")
            except Exception:
                continue
    except Exception:
        items = []
    _res_cache.update(t=now, items=items)
    return items


def is_resource_mention(item: str) -> bool:
    return item in _res_cache["items"]


def workspace_dirs(files: list[str]) -> list[str]:
    """Every parent directory of the given relative paths, as 'dir/' (shallow first)."""
    seen: set[str] = set()
    for f in files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            seen.add("/".join(parts[:i]) + "/")
    return sorted(seen, key=lambda d: (d.count("/"), d))


def match_files(query: str, cwd: Path | None = None, limit: int = 50) -> list[str]:
    """@-completion candidates: files and directories ('dir/'), plus 'server:uri' MCP resources."""
    from axon.ui.fuzzy_picker import list_workspace_files, rank_files
    resources = mcp_resource_mentions()
    server = query.split(":", 1)[0] if ":" in query else ""
    if server and any(r.startswith(server + ":") for r in resources):
        return rank_files(query, resources, limit=limit)
    files = list_workspace_files(Path(cwd or os.getcwd()))
    if query.endswith("/"):
        under = [f for f in workspace_dirs(files) + files if f.startswith(query) and f != query]
        children = [f for f in under if "/" not in f[len(query):].rstrip("/")]
        if children:
            return children[:limit]
    ranked = rank_files(query, files + workspace_dirs(files), limit=limit)
    if resources:
        extra = rank_files(query, resources, limit=10) if query else resources[:5]
        ranked = ranked[:max(0, limit - len(extra))] + [r for r in extra if r not in ranked]
    return ranked


# ─────────────────────────────────────────────────────────────────────────────
# Editor state (pure logic; rendering and I/O are separate so it is testable)
# ─────────────────────────────────────────────────────────────────────────────

_SLASH_RE = re.compile(r"(?:^|\s)/([\w:.-]*)$")
_AT_RE = re.compile(r"(?:^|\s)@([^\s]*)$")
PASTE_CHIP_RE = re.compile(r"\[Pasted text #(\d+)(?: \+\d+ lines)?\]")
LARGE_PASTE_CHARS = 1200
LARGE_PASTE_LINES = 12


@dataclass
class Popup:
    kind: str                  # "slash" | "file"
    items: list[Any]
    query: str
    start: int                 # buffer index where the completed token starts
    index: int = 0


@dataclass
class EditorState:
    mode: str = "default"
    buf: str = ""
    pos: int = 0
    history: list[str] = field(default_factory=list)
    hist_idx: int = -1         # -1 = editing a fresh line
    hist_prefix: str = ""
    draft: str = ""
    undo: list[tuple[str, int]] = field(default_factory=list)
    stash: str = ""
    dismissed: bool = False    # popup closed with Esc until the buffer changes
    popup_index: int = 0
    last_esc: float = 0.0
    last_ctrl_d: float = 0.0
    notice: str = ""
    cwd: Path = field(default_factory=Path.cwd)
    kill_ring: list[str] = field(default_factory=list)
    yank_span: tuple[int, int] | None = None
    yank_idx: int = 0
    pastes: dict[int, str] = field(default_factory=dict)
    search: str | None = None  # Ctrl+R query while searching
    search_hit: int = -1
    search_saved: tuple[str, int] = ("", 0)
    vim: Any = None            # axon.ui.vim.VimState when vim mode is on
    status_rows: list[str] = field(default_factory=list)  # custom / built-in status line

    # -- editing primitives -------------------------------------------------------------------
    def _snapshot(self) -> None:
        if not self.undo or self.undo[-1] != (self.buf, self.pos):
            self.undo.append((self.buf, self.pos))
            del self.undo[:-200]

    def _changed(self) -> None:
        self.dismissed = False
        self.popup_index = 0
        self.hist_idx = -1
        self.yank_span = None

    def insert(self, text: str) -> None:
        if not text:
            return
        self._snapshot()
        self.buf = self.buf[:self.pos] + text + self.buf[self.pos:]
        self.pos += len(text)
        self._changed()

    def insert_paste(self, text: str) -> None:
        """Insert pasted text; very large pastes collapse into a [Pasted text #N] chip."""
        lines = text.count("\n") + 1
        if len(text) >= LARGE_PASTE_CHARS or lines > LARGE_PASTE_LINES:
            n = max(self.pastes, default=0) + 1
            self.pastes[n] = text
            _save_paste(text)
            self.insert(f"[Pasted text #{n} +{lines} lines]")
        else:
            self.insert(text)

    def expanded(self) -> str:
        """Buffer with paste chips replaced by their full content (what gets submitted)."""
        return PASTE_CHIP_RE.sub(lambda m: self.pastes.get(int(m.group(1)), m.group(0)), self.buf)

    def backspace(self) -> None:
        if self.pos > 0:
            self._snapshot()
            # Delete a whole paste chip at once.
            m = None
            for m in PASTE_CHIP_RE.finditer(self.buf):
                if m.end() == self.pos:
                    break
            else:
                m = None
            start = m.start() if m else self.pos - 1
            self.buf = self.buf[:start] + self.buf[self.pos:]
            self.pos = start
            self._changed()

    def delete(self) -> None:
        if self.pos < len(self.buf):
            self._snapshot()
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
            self._changed()

    def set_text(self, text: str, pos: int | None = None) -> None:
        self.buf = text
        self.pos = len(text) if pos is None else max(0, min(pos, len(text)))

    def clear(self) -> None:
        if self.buf:
            self._snapshot()
            self.draft = ""
            if self.buf.strip() and (not self.history or self.history[-1] != self.buf):
                self.history.append(self.buf)  # Up restores the cleared draft
        self.set_text("")
        self._changed()

    def word_left(self) -> int:
        i = self.pos
        while i > 0 and self.buf[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buf[i - 1].isspace():
            i -= 1
        return i

    def word_right(self) -> int:
        i, n = self.pos, len(self.buf)
        while i < n and self.buf[i].isspace():
            i += 1
        while i < n and not self.buf[i].isspace():
            i += 1
        return i

    def kill(self, start: int, end: int) -> None:
        if start < end:
            self._snapshot()
            self.kill_ring.append(self.buf[start:end])
            del self.kill_ring[:-20]
            self.buf = self.buf[:start] + self.buf[end:]
            self.pos = start
            self._changed()

    def yank(self, cycle: bool = False) -> None:
        """Ctrl+Y pastes the last kill; Alt+Y right after replaces it with older kills."""
        if not self.kill_ring:
            return
        if cycle and self.yank_span is not None:
            start, end = self.yank_span
            self.yank_idx = (self.yank_idx + 1) % len(self.kill_ring)
            text = self.kill_ring[-1 - self.yank_idx]
            self.buf = self.buf[:start] + text + self.buf[end:]
            self.pos = start + len(text)
            self.yank_span = (start, self.pos)
            return
        if cycle:
            return
        self.yank_idx = 0
        text = self.kill_ring[-1]
        start = self.pos
        self.insert(text)
        self.yank_span = (start, self.pos)

    def line_bounds(self) -> tuple[int, int]:
        start = self.buf.rfind("\n", 0, self.pos) + 1
        end = self.buf.find("\n", self.pos)
        return start, (len(self.buf) if end == -1 else end)

    def undo_last(self) -> None:
        if self.undo:
            self.buf, self.pos = self.undo.pop()
            self.dismissed = False

    # -- multi-line cursor movement -----------------------------------------------------------
    def move_vertical(self, delta: int) -> bool:
        """Move the cursor one logical line up/down keeping the column. False at the edge."""
        start, _end = self.line_bounds()
        col = self.pos - start
        if delta < 0:
            if start == 0:
                return False
            prev_start = self.buf.rfind("\n", 0, start - 1) + 1
            self.pos = min(prev_start + col, start - 1)
        else:
            nxt = self.buf.find("\n", self.pos)
            if nxt == -1:
                return False
            nxt_end = self.buf.find("\n", nxt + 1)
            nxt_end = len(self.buf) if nxt_end == -1 else nxt_end
            self.pos = min(nxt + 1 + col, nxt_end)
        return True

    # -- history ------------------------------------------------------------------------------
    def history_step(self, delta: int) -> bool:
        """Navigate history (delta -1 = older). Filters by the text typed before navigating."""
        if self.hist_idx == -1:
            self.hist_prefix = self.buf if "\n" not in self.buf else ""
            self.draft = self.buf
        matches = [i for i, h in enumerate(self.history) if h.startswith(self.hist_prefix) and h != self.draft]
        if not matches:
            return False
        if self.hist_idx == -1:
            if delta > 0:
                return False
            target = matches[-1]
        else:
            cur = matches.index(self.hist_idx) if self.hist_idx in matches else len(matches)
            nxt = cur + delta
            if nxt >= len(matches):
                self.hist_idx = -1
                self.set_text(self.draft)
                return True
            if nxt < 0:
                return False
            target = matches[nxt]
        self.hist_idx = target
        self.set_text(self.history[target])
        self.dismissed = True
        return True

    def search_start(self) -> None:
        self.search = ""
        self.search_hit = -1
        self.search_saved = (self.buf, self.pos)

    def search_update(self, older: bool = False) -> None:
        """Find the newest (or next older) history entry containing the search query."""
        q = (self.search or "").lower()
        start = (self.search_hit - 1) if (older and self.search_hit >= 0) else len(self.history) - 1
        for i in range(start, -1, -1):
            if q in self.history[i].lower():
                self.search_hit = i
                self.set_text(self.history[i])
                return
        if not older:
            self.search_hit = -1
            self.set_text(*self.search_saved)

    def search_end(self, accept: bool) -> None:
        if not accept:
            self.set_text(*self.search_saved)
        self.search = None
        self.dismissed = True

    def shell_history_complete(self) -> bool:
        """Tab in ! mode: complete from earlier shell commands in this project's history."""
        prefix = self.buf
        for h in reversed(self.history):
            if h.startswith("!") and h.startswith(prefix) and h != prefix:
                self._snapshot()
                self.set_text(h)
                return True
        return False

    # -- completion ---------------------------------------------------------------------------
    def popup(self) -> Popup | None:
        if self.dismissed or self.search is not None or self.buf.startswith("!"):
            return None
        before = self.buf[:self.pos]
        m = _SLASH_RE.search(before)
        if m and (m.start(1) == 1 or "/" not in m.group(1)):
            items = match_slash_commands(m.group(1), self.cwd)
            if items:
                return Popup("slash", items, m.group(1), m.start(1) - 1, self.popup_index % len(items))
            return None
        m = _AT_RE.search(before)
        if m:
            items = match_files(m.group(1), self.cwd)
            if items:
                return Popup("file", items, m.group(1), self.pos - len(m.group(1)) - 1, self.popup_index % len(items))
        return None

    def accept_completion(self, pop: Popup, for_submit: bool = False) -> bool:
        """Insert the selected completion. Returns True when the buffer should be submitted."""
        if pop.kind == "slash":
            name, _kind, _cat, _desc, takes_args = pop.items[pop.index]
            rest = self.buf[self.pos:]
            head = self.buf[:pop.start]
            if for_submit and not head.strip():
                self.set_text(name + rest)
                return True
            fill = head + name + (" " if takes_args or rest or head.strip() else "")
            self._snapshot()
            self.set_text(fill + rest.lstrip(), len(fill))
            self.dismissed = True
            return False
        path = pop.items[pop.index]
        drill = path.endswith("/") and not for_submit and not is_resource_mention(path)
        if " " in path:
            path = f'"{path}"'
        self._snapshot()
        new = self.buf[:pop.start] + "@" + path + ("" if drill else " ")
        self.set_text(new + self.buf[self.pos:].lstrip(" "), len(new))
        self.dismissed = not drill   # Tab on a directory keeps the popup open on its contents
        return False


def _save_paste(text: str) -> None:
    """Keep large pastes in ~/.axon/projects/<key>/pastes so a session can be inspected later."""
    try:
        import hashlib
        from axon.storage import project_subdir
        d = project_subdir(None, "pastes")
        (d / f"{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]}.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

def layout_rows(text: str, width: int, pos: int) -> tuple[list[str], int, int]:
    """Soft-wrap text into rows of at most `width` cells. Returns (rows, cursor_row, cursor_col)."""
    width = max(4, width)
    rows: list[str] = [""]
    row_w = 0
    cur_row, cur_col = 0, 0
    for i, ch in enumerate(text):
        if i == pos:
            cur_row, cur_col = len(rows) - 1, row_w
        if ch == "\n":
            rows.append("")
            row_w = 0
            continue
        w = plat.char_width(ch) if ch != "\t" else 4
        if row_w + w > width:
            rows.append("")
            row_w = 0
            if i == pos:
                cur_row, cur_col = len(rows) - 1, 0
        rows[-1] += ch if ch != "\t" else "    "
        row_w += w
    if pos >= len(text):
        cur_row, cur_col = len(rows) - 1, row_w
        if cur_col >= width:
            rows.append("")
            cur_row, cur_col = len(rows) - 1, 0
    return rows, cur_row, cur_col


@dataclass
class PromptChrome:
    effort: str = "high"
    plan_summary: str | None = None
    queue_summary: str | None = None
    subagent_label: str | None = None


def status_line(mode: str, chrome: PromptChrome, width: int, vim_label: str = "") -> str:
    label, color, icon = _MODE_LABELS.get(mode, (f"{mode} mode", SLATE, "⏵"))
    left = f"{color}{BOLD}{icon} {label}{RST} {DARK_SLATE}(shift+tab to cycle){RST}"
    if vim_label:
        vim_color = MINT if "INSERT" in vim_label else GOLD
        left = f"{vim_color}{BOLD}{vim_label}{RST}  " + left
    badges = []
    if chrome.plan_summary:
        badges.append(f"{MINT}{chrome.plan_summary}{RST}")
    if chrome.queue_summary:
        badges.append(f"{CYAN}{chrome.queue_summary}{RST}")
    if chrome.subagent_label:
        badges.append(f"{CYAN}{chrome.subagent_label}{RST} {DARK_SLATE}/main to return{RST}")
    if badges:
        left += "  " + "  ".join(badges)
    right_opts = [f"{SLATE}○ {chrome.effort}{RST} {DARK_SLATE}· / commands · @ files · ! shell · ? help{RST}",
                  f"{SLATE}○ {chrome.effort}{RST} {DARK_SLATE}· / commands{RST}", f"{SLATE}○ {chrome.effort}{RST}", ""]
    for right in right_opts:
        gap = width - plat.str_width(left) - plat.str_width(right)
        if gap >= 2 or not right:
            return f"  {left}{' ' * max(2, gap)}{right}" if right else f"  {left}"
    return f"  {left}"


def render_popup(pop: Popup, cols: int, max_rows: int) -> list[str]:
    n = len(pop.items)
    visible = max(1, min(8, max_rows, n))
    start = max(0, min(pop.index - visible // 2, n - visible))
    lines: list[str] = []
    for i in range(start, start + visible):
        sel = i == pop.index
        mark = f"{MINT}{BOLD}❯{RST}" if sel else " "
        if pop.kind == "slash":
            name, kind, cat, desc, _args = pop.items[i]
            name_s = plat.pad_to(name, 16)
            name_s = f"{BOLD}{WHITE}{name_s}{RST}" if sel else f"{CYAN}{name_s}{RST}"
            tag = (f"{GOLD}◇{RST}" if kind == "custom" else (f"{PURPLE}◆{RST}" if kind in ("ext", "mcp")
                   else f"{DARK_SLATE}·{RST}"))
            body = f"{tag} {name_s} {(WHITE if sel else SLATE)}{desc}{RST}"
            if cols >= 100:
                body += f"  {DARK_SLATE}{cat}{RST}"
        else:
            path = pop.items[i]
            if is_resource_mention(path):
                server, _, uri = path.partition(":")
                body = (f"{PURPLE}◆{RST} " + (f"{BOLD}{WHITE}{uri}{RST}" if sel else f"{WHITE}{uri}{RST}")
                        + f"  {DARK_SLATE}mcp:{server}{RST}")
            else:
                is_dir = path.endswith("/")
                parent, _, fname = path.rstrip("/").rpartition("/")
                fname += "/" if is_dir else ""
                name_c = CYAN if is_dir else WHITE
                body = (f"{BOLD}{name_c}{fname}{RST}" if sel else f"{name_c}{fname}{RST}") + (f"  {DARK_SLATE}{parent}/{RST}" if parent else "")
        lines.append(f"  {mark} {body}")
    more = []
    if start > 0:
        more.append(f"↑ {start}")
    if start + visible < n:
        more.append(f"↓ {n - start - visible}")
    hint = "tab complete · enter " + ("run" if pop.kind == "slash" else "insert") + " · esc dismiss"
    if pop.kind == "file" and str(pop.items[pop.index]).endswith("/"):
        hint = "tab open folder · enter mention folder · esc dismiss"
    lines.append(f"    {DARK_SLATE}{'  '.join(more) + ('  ·  ' if more else '')}{hint}{RST}")
    return lines


def build_frame(st: EditorState, chrome: PromptChrome) -> tuple[list[str], tuple[int, int]]:
    cols, rows_total = plat.term_size()
    width = max(20, cols - 4)
    bash = st.buf.startswith("!")
    if bash:
        prefix = f"{AMBER}{BOLD}!{RST} "
        body, pos = st.buf[1:], max(0, st.pos - 1)
        rule_color = AMBER
    else:
        prefix = f"{CYAN}{BOLD}[{chrome.subagent_label}]{RST} {CYAN}{BOLD}❯{RST} " if chrome.subagent_label else f"{CYAN}{BOLD}❯{RST} "
        body, pos = st.buf, st.pos
        rule_color = _PROMPT_COLOR or DARK_SLATE
    pw = plat.str_width(prefix)
    text_w = max(8, cols - 3 - pw)
    rows, crow, ccol = layout_rows(body, text_w, pos)
    max_rows = max(3, rows_total - 6)
    first = max(0, min(crow - max_rows // 2, len(rows) - max_rows)) if len(rows) > max_rows else 0
    shown = rows[first:first + max_rows]
    lines = [f"  {rule_color}{'─' * width}{RST}"]
    if not body:
        hint = ("shell mode · run a command, then Axon responds to its output (esc to leave)" if bash
                else "Ask anything · / commands · @ files · ! shell · # memory · ? shortcuts")
        lines.append(f"  {prefix}{DARK_SLATE}{hint}{RST}")
    else:
        for i, row in enumerate(shown):
            lead = prefix if (first + i) == 0 else " " * pw
            styled = (f"{GOLD}{row}{RST}" if bash else highlight_prompt_syntax(row, allow_prefix=(first + i) == 0))
            lines.append(f"  {lead}{styled}")
    cursor = (1 + crow - first, 2 + pw + ccol)
    lines.append(f"  {rule_color}{'─' * width}{RST}")
    lines.append(status_line(st.mode, chrome, width, st.vim.label if st.vim is not None else ""))
    for row in st.status_rows:
        lines.append(safe_ansi_truncate(row, cols - 1))
    if st.search is not None:
        found = "" if st.search_hit >= 0 or not st.search else f"  {ROSE}no match{RST}"
        lines.append(f"  {GOLD}reverse-i-search:{RST} {WHITE}{st.search}{RST}{CYAN}▏{RST}{found}  "
                     f"{DARK_SLATE}ctrl+r older · enter run · tab/esc edit · ctrl+c cancel{RST}")
    elif st.notice:
        lines.append(f"  {DARK_SLATE}{st.notice}{RST}")
    elif INVISIBLE_RE.search(st.buf):
        n = len(INVISIBLE_RE.findall(st.buf))
        lines.append(f"  {AMBER}! prompt contains {n} invisible / bidi control character{'s' if n != 1 else ''}{RST}")
    pop = st.popup()
    if pop is not None:
        lines.extend(render_popup(pop, cols, max(1, rows_total - len(lines) - 2)))
    return lines, cursor


# ─────────────────────────────────────────────────────────────────────────────
# Plain fallback
# ─────────────────────────────────────────────────────────────────────────────

def _plain_input(mode: str, chrome: PromptChrome) -> tuple[str, str | None, int]:
    width = max(40, term_width() - 4)
    sys.stdout.write(f"  {DARK_SLATE}{'─' * width}{RST}\n{status_line(mode, chrome, width)}\n")
    sys.stdout.flush()
    label = f"  {CYAN}[{chrome.subagent_label}]{RST} › " if chrome.subagent_label else "  › "
    try:
        line = input(label)
    except EOFError:
        if plat.IS_WINDOWS:
            # Ctrl+C at a Windows input() can surface as EOFError followed by KeyboardInterrupt.
            time.sleep(0.05)
        return ("/exit", None, 0)
    if line.strip():
        add_history(line)
    return (line.strip(), None, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive editor
# ─────────────────────────────────────────────────────────────────────────────

_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp)\b", re.IGNORECASE)


def _paste_text(text: str) -> str:
    """Normalize pasted text; pasted image file paths become [Image #N] attachment chips."""
    text = plat.normalize_newlines(text)
    if _IMAGE_EXT_RE.search(text):
        try:
            from axon.agent.images import compact_image_paths
            compacted, attachments = compact_image_paths(text)
            if attachments:
                return compacted
        except Exception:
            pass
    return text


def _clipboard_insert() -> str:
    """Ctrl+V: an image on the clipboard becomes an attachment token, else clipboard text."""
    try:
        from axon.agent.images import compact_image_paths, get_images_cache_dir, save_clipboard_image
        dest = get_images_cache_dir() / f"clip_{time.time_ns()}.png"
        saved = ""
        try:
            if save_clipboard_image(dest):
                saved = str(dest)
        except Exception:
            saved = ""
        if not saved:  # Linux / BSD: wl-paste or xclip
            from axon.ui.clipboard import _LAST_IMAGE_PATH, paste_image_to
            if paste_image_to(dest):
                saved = _LAST_IMAGE_PATH[0] or str(dest)
        if saved:
            chip = compact_image_paths(f"{saved} ")[0]
            if chip.strip() != saved:
                return chip
    except Exception:
        pass
    try:
        from axon.ui.clipboard import paste_from_clipboard
        return _paste_text(paste_from_clipboard() or "")
    except Exception:
        return ""


def _edit_externally(text: str) -> str | None:
    fd, path = tempfile.mkstemp(suffix=".md", prefix="axon_prompt_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        with plat.cooked_terminal():
            try:
                subprocess.call(plat.editor_command() + [path])
            except (FileNotFoundError, OSError):
                return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().rstrip("\n")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


REWIND = "__REWIND__"
TRACE_CYCLE = "__TRACE_CYCLE__"
_last_empty_ctrl_c = 0.0

# Keys that end the prompt and hand a command to the REPL (default bindings; see keybindings.py).
_SHORTCUT_COMMANDS = {"ctrl-t": "/todos", "ctrl-o": TRACE_CYCLE, "ctrl-f": "/transcript", "alt-p": "/model",
                      "alt-t": "/thinking"}
_ACTION_COMMANDS = {"app:todos": "/todos", "app:traceCycle": TRACE_CYCLE, "app:transcript": "/transcript",
                    "app:modelPicker": "/model",
                    "app:thinkingToggle": "/thinking", "app:fastToggle": "/fast"}


# ─────────────────────────────────────────────────────────────────────────────
# Vim mode / prompt agent (module state set by /vim and Renderer.attach)
# ─────────────────────────────────────────────────────────────────────────────

_VIM_ENABLED = False
_ACTIVE_EDITOR: "EditorState | None" = None
_PROMPT_AGENT: Any = None


def set_vim_mode(enabled: bool) -> bool:
    """Turn vim editing on/off for the prompt (takes effect immediately, also mid-prompt)."""
    global _VIM_ENABLED
    _VIM_ENABLED = bool(enabled)
    st = _ACTIVE_EDITOR
    if st is not None:
        if _VIM_ENABLED and st.vim is None:
            from axon.ui.vim import VimState
            st.vim = VimState()
        elif not _VIM_ENABLED:
            st.vim = None
    return _VIM_ENABLED


def vim_mode_enabled() -> bool:
    return _VIM_ENABLED


def vim_mode_label() -> str:
    """'insert' / 'normal' while vim mode is on (for the status-line JSON), else ''."""
    if not _VIM_ENABLED:
        return ""
    st = _ACTIVE_EDITOR
    vim = getattr(st, "vim", None) if st is not None else None
    return getattr(vim, "mode", "insert") if vim is not None else "insert"


_PROMPT_COLOR = ""
_NAMED_PROMPT_COLORS = {"red": 203, "orange": 215, "yellow": 221, "green": 114, "cyan": 80, "blue": 75,
                        "purple": 141, "pink": 211, "gray": 245, "grey": 245}


def set_prompt_color(name: str | None) -> str:
    """Colour of the prompt's rules (/color). 'default'/None restores the theme colour."""
    global _PROMPT_COLOR
    n = str(name or "default").strip().lower()
    if n in ("", "default", "none"):
        _PROMPT_COLOR = ""
    elif n.startswith("\033[") or n.startswith("\x1b["):
        _PROMPT_COLOR = n
    elif n in _NAMED_PROMPT_COLORS:
        from axon.ui.theme import colors_enabled
        _PROMPT_COLOR = f"\033[38;5;{_NAMED_PROMPT_COLORS[n]}m" if colors_enabled() else ""
    else:
        raise ValueError(f"unknown prompt colour {name!r}")
    return n


def set_prompt_agent(agent: Any) -> None:
    """Agent whose settings feed the status line under the prompt (weakly referenced)."""
    global _PROMPT_AGENT
    if agent is None:
        _PROMPT_AGENT = None
        return
    try:
        import weakref
        _PROMPT_AGENT = weakref.ref(agent)
    except TypeError:
        _PROMPT_AGENT = lambda: agent  # noqa: E731 - objects without weakref support


def _prompt_agent() -> Any:
    ref = _PROMPT_AGENT
    return ref() if callable(ref) else None


# ─────────────────────────────────────────────────────────────────────────────
# Key handling (pure: no terminal I/O, so scripted key sequences can be tested)
# ─────────────────────────────────────────────────────────────────────────────

class PromptEditor:
    """Applies keys to an EditorState. ``handle`` returns None (keep editing) or an outcome:
    ("submit", None) · ("finish", value) · ("raise", exc_type) · ("ui", "editor"|"finder"|"redraw"|"suspend")."""

    def __init__(self, st: EditorState, resolver: Any = None) -> None:
        self.st = st
        if resolver is None:
            from axon.ui.keybindings import KeyResolver
            resolver = KeyResolver()
        self.resolver = resolver

    def handle(self, key: str) -> tuple[str, Any] | None:
        global _last_empty_ctrl_c
        st = self.st
        st.notice = ""
        if key != "esc":
            st.last_esc = 0.0
        if key != "ctrl-d":
            st.last_ctrl_d = 0.0
        if key != "ctrl-c":
            _last_empty_ctrl_c = 0.0
        if st.search is not None:
            return self._search_key(key)
        if key == "resize":
            return None
        if key.startswith(plat.PASTE_PREFIX):
            if st.vim is not None and st.vim.mode != "insert":
                st.vim.begin_insert(st)
            st.insert_paste(_paste_text(key[len(plat.PASTE_PREFIX):]))
            return None
        if st.vim is not None and not self.resolver.pending and st.vim.handle(st, key):
            return None
        action, raw = self.resolver.feed(key)
        if action is not None:
            return self.do(action)
        for k in raw:
            if k == "?" and not st.buf:
                return ("finish", "/kb")
            if len(k) == 1 and (k >= " " or k == "\t"):
                st.insert(k)
        return None

    def _search_key(self, key: str) -> tuple[str, Any] | None:
        st = self.st
        assert st.search is not None
        if key == "ctrl-r":
            st.search_update(older=True)
        elif key == "backspace":
            st.search = st.search[:-1]
            st.search_update()
        elif key in ("ctrl-c", "ctrl-g"):
            st.search_end(accept=False)
        elif key == "enter":
            st.search_end(accept=True)
            return ("submit", None)
        elif key in ("tab", "esc", "right", "left", "home", "end"):
            st.search_end(accept=True)
        elif len(key) == 1 and key >= " ":
            st.search += key
            st.search_update()
        elif key.startswith(plat.PASTE_PREFIX):
            st.search += key[len(plat.PASTE_PREFIX):].replace("\n", " ")
            st.search_update()
        elif key != "resize":
            st.search_end(accept=True)
        return None

    def do(self, action: str) -> tuple[str, Any] | None:  # noqa: C901 - one flat dispatch table
        global _last_empty_ctrl_c
        st = self.st
        pop = st.popup()
        bash = st.buf.startswith("!")
        if action == "chat:submit":
            if pop is not None:
                if st.accept_completion(pop, for_submit=True):
                    return ("submit", None)
            elif st.pos == len(st.buf) and st.buf.endswith("\\"):
                st.buf = st.buf[:-1]
                st.pos -= 1
                st.insert("\n")
            else:
                return ("submit", None)
        elif action == "chat:newline":
            st.insert("\n")
        elif action == "chat:complete":
            if pop is not None:
                st.accept_completion(pop)
            elif bash:
                st.shell_history_complete()
            else:
                st.notice = "shift+tab cycles the permission mode"   # Tab only completes (CC parity)
        elif action == "chat:cycleMode":
            st.mode = cycle_mode(st.mode, 1)
        elif action in ("cursor:up", "cursor:down"):
            delta = -1 if action == "cursor:up" else 1
            if pop is not None:
                st.popup_index = (pop.index + delta) % len(pop.items)
            elif not st.move_vertical(delta):
                st.history_step(delta)
        elif action == "history:prev":
            st.history_step(-1)
        elif action == "history:next":
            st.history_step(1)
        elif action == "history:search":
            st.search_start()
        elif action == "chat:cancel":
            now = time.monotonic()
            if pop is not None:
                st.dismissed = True
            elif bash and st.buf == "!":
                st.set_text("")
            elif now - st.last_esc < 0.6:
                st.last_esc = 0.0
                if not st.buf:
                    return ("finish", REWIND)
                st.clear()
                st.notice = "draft cleared · ↑ to restore"
            else:
                st.last_esc = now
                st.notice = "esc again to clear the draft" if st.buf else "esc again to rewind"
        elif action == "chat:interrupt":
            if st.buf:
                st.clear()
                st.notice = "draft cleared · ↑ to restore · ctrl+c again on an empty prompt to exit"
            else:
                now = time.monotonic()
                if now - _last_empty_ctrl_c < 1.5:
                    _last_empty_ctrl_c = 0.0
                    return ("raise", EOFError)
                _last_empty_ctrl_c = now
                return ("raise", KeyboardInterrupt)
        elif action == "chat:exit":
            if st.buf:
                st.delete()
            else:
                now = time.monotonic()
                if now - st.last_ctrl_d < 0.8:
                    return ("raise", EOFError)
                st.last_ctrl_d = now
                st.notice = "press ctrl+d again to exit"
        elif action == "edit:backspace":
            if bash and st.buf == "!":
                st.set_text("")
            else:
                st.backspace()
        elif action == "edit:delete":
            st.delete()
        elif action == "cursor:left":
            if not st.buf:
                return ("finish", SWITCH_SESSION)
            st.pos = max(0, st.pos - 1)
        elif action == "cursor:right":
            if pop is not None and pop.kind == "file" and st.pos == len(st.buf):
                st.accept_completion(pop)
            else:
                st.pos = min(len(st.buf), st.pos + 1)
        elif action == "cursor:wordLeft":
            st.pos = st.word_left()
        elif action == "cursor:wordRight":
            st.pos = st.word_right()
        elif action == "cursor:lineStart":
            st.pos = st.line_bounds()[0]
        elif action == "cursor:lineEnd":
            st.pos = st.line_bounds()[1]
        elif action == "edit:killToEnd":
            st.kill(st.pos, st.line_bounds()[1])
        elif action == "edit:killToStart":
            if bash and st.buf == "!":
                st.set_text("")
            else:
                st.kill(st.line_bounds()[0], st.pos)
        elif action == "edit:killWord":
            st.kill(st.word_left(), st.pos)
        elif action == "edit:killWordForward":
            st.kill(st.pos, st.word_right())
        elif action == "edit:yank":
            st.yank()
        elif action == "edit:yankPop":
            st.yank(cycle=True)
        elif action == "edit:undo":
            st.undo_last()
        elif action == "chat:stash":
            if st.buf:
                st.stash = st.buf
                st._snapshot()
                st.set_text("")
                st.notice = "prompt stashed · ctrl+s to restore"
            elif st.stash:
                st.set_text(st.stash)
                st.stash = ""
        elif action == "chat:paste":
            pasted = _clipboard_insert()
            if pasted:
                st.insert_paste(pasted)
            else:
                st.notice = "clipboard is empty"
        elif action == "app:vimToggle":
            on = set_vim_mode(not vim_mode_enabled())
            if on and st.vim is None:
                from axon.ui.vim import VimState
                st.vim = VimState()
            elif not on:
                st.vim = None
            st.notice = f"vim mode {'on' if on else 'off'}"
        elif action in _ACTION_COMMANDS:
            return ("finish", _ACTION_COMMANDS[action])
        elif action == "chat:externalEditor":
            return ("ui", "editor")
        elif action == "app:fileFinder":
            return ("ui", "finder")
        elif action == "app:redraw":
            return ("ui", "redraw")
        elif action == "app:suspend":
            return ("ui", "suspend")
        return None


def _status_rows(agent: Any) -> list[str]:
    if agent is None:
        return []
    try:
        from axon.ui.statusline import prompt_status_rows
        return prompt_status_rows(agent)
    except Exception:
        return []


def read_input(
    mode: str = "default",
    effort: str = "high",
    prompt: str = "",
    subagents: list[Any] | None = None,
    active_idx: int = 0,
    plan_summary: str | None = None,
    queue_summary: str | None = None,
    subagent_label: str | None = None,
    **kwargs: Any,
) -> tuple[str, str | None, int]:
    """Read one prompt. Returns (text, new_mode_if_changed, 0).

    Keyword extras: ``initial`` (prefill the editor, e.g. a recalled queued prompt), ``cwd`` (history + completion scope; defaults to the attached agent's
    workspace, else the process cwd), ``session_id``, ``agent`` (status line source; defaults to
    the agent registered by ``set_prompt_agent``), ``vim_mode`` (override).

    While the prompt is empty and idle, setting ``axon.commands.workspace_cmds.LOOP_WAKE`` makes
    this return ``("", None, 0)`` so the REPL drains ``agent.message_queue`` (e.g. a /loop run).

    Special results: "__SWITCH_SESSION__" (← on empty prompt), "__REWIND__" (Esc Esc on empty
    prompt), "__TRACE_CYCLE__" (Ctrl+O), "/todos" (Ctrl+T), "/transcript" (Ctrl+F), "/model" (Alt+P), "/thinking"
    (Alt+T), "/kb" ("?" on empty prompt). Raises KeyboardInterrupt on Ctrl+C with an empty line
    and EOFError on a second Ctrl+C, or Ctrl+D pressed twice, on an empty line.
    """
    global _ACTIVE_EDITOR
    session_id = str(kwargs.get("session_id") or "")
    chrome = PromptChrome(effort=effort, plan_summary=plan_summary, queue_summary=queue_summary,
                          subagent_label=subagent_label)
    if not plat.is_interactive():
        return _plain_input(mode, chrome)

    agent = kwargs.get("agent") or _prompt_agent()
    cwd_arg = kwargs.get("cwd") or getattr(getattr(agent, "settings", None), "workspace", None)
    cwd = Path(cwd_arg) if cwd_arg else Path.cwd()
    plat.setup_console()
    try:
        from axon.ui.screen_log import set_tag
        set_tag(None)              # whatever prints after the prompt (commands) is not a transcript block
    except ImportError:
        pass
    st = EditorState(mode=mode, history=load_history(cwd=str(cwd)), cwd=cwd)
    vim_on = kwargs.get("vim_mode")
    if vim_on if vim_on is not None else _VIM_ENABLED:
        from axon.ui.vim import VimState
        st.vim = VimState()
    st.status_rows = _status_rows(agent)
    initial = kwargs.get("initial") or prompt
    try:  # keys typed while the previous turn was finishing are not lost
        from axon.ui.in_flight import take_type_ahead
        ahead = take_type_ahead()
    except (ImportError, AttributeError):
        ahead = ""
    if ahead:
        initial = f"{initial}{ahead}" if initial else ahead
    global _RESIZE_DRAFT
    if _RESIZE_DRAFT:              # what was typed before a window resize redrew the screen
        initial, _RESIZE_DRAFT = _RESIZE_DRAFT, ""
    if initial:
        st.set_text(str(initial))
    editor = PromptEditor(st)
    wake = _loop_wake_event()
    frame = LiveFrame()
    sys.stdout.write("\n")
    _ACTIVE_EDITOR = st

    def changed_mode() -> str | None:
        return st.mode if st.mode != mode else None

    def finish(value: str) -> tuple[str, str | None, int]:
        frame.clear()
        return (value, changed_mode(), 0)

    def submit() -> tuple[str, str | None, int]:
        shown = st.buf.strip()
        text = st.expanded().strip()
        frame.clear()
        if text:
            try:
                from axon.providers.keystore import redact_command_line
                shown = redact_command_line(shown)
            except Exception:
                pass
            _echo_submitted(shown, chrome)
            add_history(text, session_id)
        return (text, changed_mode(), 0)

    width0 = term_width()          # the real window (shutil's size prefers a stale $COLUMNS)
    resized_at = 0.0
    clicks = ClickMap.for_prompt(agent)   # click a block above the prompt -> expand it in the transcript
    try:
        with plat.raw_terminal(isig=False, bracketed_paste=True):
            frame.render(*build_frame(st, chrome))
            if clicks is not None:
                clicks.measure(frame)
            while True:
                try:
                    key = plat.read_key(timeout=0.25 if (wake is not None or resized_at) else None)
                except EOFError:
                    frame.clear()
                    raise
                if key == "resize":
                    resized_at = time.monotonic()      # wait until the user stops dragging the window
                    continue
                if key is None and resized_at and time.monotonic() - resized_at >= 0.25:
                    resized_at = 0.0
                    if term_width() != width0:
                        # The terminal re-wrapped everything already printed: hand back so the REPL redraws
                        # the conversation at the new width (the draft is kept).
                        _RESIZE_DRAFT = st.buf
                        return (RESIZE_REDRAW, changed_mode(), 0)
                    frame.render(*build_frame(st, chrome))
                    if clicks is not None:
                        clicks.measure(frame)
                    continue
                if key and key.startswith((plat.MOUSE_PREFIX, plat.CPR_PREFIX)):
                    target = clicks.target(key) if clicks is not None else None
                    if target is not None:
                        _open_transcript_from_prompt(agent, target)
                        if term_width() != width0:
                            _RESIZE_DRAFT = st.buf
                            return (RESIZE_REDRAW, changed_mode(), 0)
                        frame.render(*build_frame(st, chrome))
                        if clicks is not None:
                            clicks.update(frame)
                    continue
                if key is None:
                    if wake is not None and wake.is_set() and not st.buf:
                        wake.clear()
                        return finish("")
                    continue
                outcome = editor.handle(key)
                if outcome is not None:
                    kind, value = outcome
                    if kind == "submit":
                        return submit()
                    if kind == "finish":
                        return finish(value)
                    if kind == "raise":
                        frame.clear()
                        raise value
                    plat.set_mouse_reporting(False)    # editor / finder / suspend own the terminal
                    frame = _run_ui_action(value, st, frame)
                    frame.render(*build_frame(st, chrome))
                    if clicks is not None:
                        clicks.measure(frame)
                    continue
                frame.render(*build_frame(st, chrome))
                if clicks is not None:
                    clicks.update(frame)
    finally:
        plat.set_mouse_reporting(False)                # never leave mouse reporting on for a turn / shell / exit
        _ACTIVE_EDITOR = None


RESIZE_REDRAW = "__RESIZE_REDRAW__"
_RESIZE_DRAFT = ""


class ClickMap:
    """Maps a mouse click above the idle prompt to the transcript entry printed on that row.

    The rows themselves are tagged by :class:`axon.ui.screen_log.RowTags` (fed with all output, so its
    newest rows are the prompt frame's rows above the cursor). Where they are on screen comes from one
    cursor position report (CSI 6n) after the frame is drawn; frames that grow past the bottom of the
    window scroll everything up, which :meth:`update` accounts for without asking again."""

    WHEEL_UP = -1        # target(): open the viewer at the end (scroll back through history)

    def __init__(self, tags: Any) -> None:
        self.tags = tags
        self.frame_top = 0     # 1-based screen row of the frame's first line (0: unknown -> clicks ignored)
        self._skip = 0         # frame rows above the cursor (already committed in the row model)

    @classmethod
    def for_prompt(cls, agent: Any = None) -> "ClickMap | None":
        try:
            from axon.ui.screen_log import RowTags, click_expand_enabled
        except ImportError:
            return None
        tags = getattr(sys.stdout, "tags", None)
        if not isinstance(tags, RowTags) or not plat.is_interactive():
            return None
        if not click_expand_enabled(getattr(agent, "settings", None)):
            return None
        return cls(tags)

    _misses = 0            # terminals that never answer CSI 6n are not asked again (each try waits 150 ms)

    def measure(self, frame: LiveFrame) -> bool:
        """Locate the frame with a cursor position report; turns mouse reporting on when it works."""
        pos = plat.query_cursor_position(0.15) if ClickMap._misses < 2 else None
        if pos is None:
            ClickMap._misses += 1
            self.frame_top = 0
            plat.set_mouse_reporting(False)
            return False
        ClickMap._misses = 0
        self.frame_top = max(1, pos[0] - frame._cursor_row)
        self._skip = frame._cursor_row
        plat.set_mouse_reporting(True)
        return True

    def update(self, frame: LiveFrame, height: int | None = None) -> None:
        """After a re-render: a taller frame near the bottom scrolled the screen up."""
        if not self.frame_top:
            return
        h = height if height is not None else plat.term_size()[1]
        n = max(1, len(frame._widths))
        if self.frame_top + n - 1 > h:
            self.frame_top = max(1, h - n + 1)
        self._skip = frame._cursor_row

    def entry_at(self, y: int, cols: int | None = None) -> int | None:
        """uid of the transcript entry shown on screen row ``y`` (1-based), or None."""
        if not self.frame_top or y >= self.frame_top or y < 1:
            return None
        return self.tags.tag_at(self.frame_top - y, skip=self._skip, cols=cols or plat.term_size()[0])

    def target(self, key: str) -> int | None:
        ev = plat.parse_mouse(key)
        if ev is None or not self.frame_top:
            return None
        if ev.kind == "wheel-up":
            return self.WHEEL_UP
        if ev.kind == "press" and ev.button == 0:
            return self.entry_at(ev.y)
        return None


def _open_transcript_from_prompt(agent: Any, target: int) -> None:
    """Open the full-screen transcript over the prompt (alternate screen: the prompt and the draft
    stay exactly as they were)."""
    try:
        from axon.ui.transcript_view import open_transcript_viewer
        if target == ClickMap.WHEEL_UP:
            open_transcript_viewer(agent)
        else:
            open_transcript_viewer(agent, focus=target, expand=True)
    except Exception:
        pass


def _loop_wake_event() -> Any:
    """The /loop wake-up event (threading.Event) or None when the commands package is unavailable."""
    try:
        from axon.commands.workspace_cmds import LOOP_WAKE
        return LOOP_WAKE
    except (ImportError, AttributeError):
        return None


def _run_ui_action(name: str, st: EditorState, frame: LiveFrame) -> LiveFrame:
    """Actions that leave the prompt frame (editor, finder, redraw, suspend). Returns a new frame."""
    if name == "redraw":
        sys.stdout.write("\033[H\033[2J")
        return LiveFrame()
    if name == "editor":
        frame.clear()
        edited = _edit_externally(st.buf)
        if edited is None:
            st.notice = f"could not start editor ({' '.join(plat.editor_command())}); set $EDITOR"
        else:
            st._snapshot()
            st.set_text(edited)
            st._changed()
        return LiveFrame()
    if name == "finder":
        frame.clear()
        from axon.ui.fuzzy_picker import run_fuzzy_file_finder
        chosen = run_fuzzy_file_finder(st.cwd)
        if chosen:
            st.insert(f"@{chosen} ")
        return LiveFrame()
    if name == "suspend" and not plat.IS_WINDOWS and hasattr(signal, "SIGTSTP"):
        frame.clear()
        with plat.cooked_terminal():
            os.kill(os.getpid(), signal.SIGTSTP)
        return LiveFrame()
    return frame


def _echo_submitted(text: str, chrome: PromptChrome) -> None:
    sys.stdout.write(format_user_prompt_banner(text, subagent_label=chrome.subagent_label,
                                               is_bash=text.startswith("!")) + "\n\n")
    sys.stdout.flush()
