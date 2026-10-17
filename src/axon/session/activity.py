"""
Append-only activity journal: one line per user action or notable event, per project.

Location: ``~/.axon/projects/<project-key>/activity.jsonl`` (``AXON_HOME`` overrides ``~/.axon``).

Format (one JSON object per line, never rewritten)::

    {"ts": "2026-10-16T21:05:03.123+00:00", "t": 1792000000.123, "session_id": "session_...",
     "kind": "prompt", "data": {...}, "pid": 1234}

Kinds written by Axon (see :data:`KINDS`): prompt, command, shell, memory, side_question,
queue, steer, permission, settings, settings_file, mode, turn_start, turn_end, tool, error,
retry, clear, compact, rewind, rewind_code, resume, rename, delete, restore, tips, background,
session_start, session_end, migration, skill, subagent, hook, custom_command, plan, saved.

Durability: every record is appended in append mode (safe with several Axon processes writing
to the same project), flushed immediately and fsync'ed on turn end, session end and exit. A
torn last line from a crash is isolated by :func:`axon.storage.append_line` and skipped on read.
Secrets (API keys, tokens, passwords, private keys, values of ``*_API_KEY``/``*_TOKEN``
environment variables) are redacted before anything is written.

Every public function here is best-effort and never raises: journaling must not break a turn.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

KINDS: dict[str, str] = {
    "prompt": "a prompt you sent (typed, queued, or headless)",
    "command": "a slash command and its outcome",
    "shell": "a `!command` you ran, exit code and output",
    "memory": "a `#` memory note",
    "side_question": "a /btw side question and its answer",
    "queue": "a message queued, removed or cleared (type-ahead, ;;, busy)",
    "steer": "queued messages delivered to the model mid-turn",
    "permission": "a permission decision for a tool call",
    "settings": "an in-session settings change (model, effort, theme, ...)",
    "settings_file": "a change saved to settings.json",
    "mode": "a permission-mode change",
    "turn_start": "a model turn started",
    "turn_end": "a model turn ended: stop reason, cost, tokens, tool calls",
    "tool": "a tool call: name, input summary, result size, full-output file",
    "error": "a provider or turn error",
    "retry": "a retried model request",
    "clear": "/clear: a new session started (the old one stays resumable)",
    "compact": "the conversation was compacted",
    "rewind": "/rewind of the conversation (the transcript keeps the removed messages)",
    "rewind_code": "/rewind of files (the undone versions are archived)",
    "resume": "a session was resumed",
    "rename": "a session was renamed",
    "delete": "a session was moved to the trash",
    "restore": "a session was restored from the trash",
    "tips": "usage tips were regenerated",
    "background": "a turn was moved to / finished in the background",
    "session_start": "Axon started",
    "session_end": "Axon exited (with unsent queued messages / draft)",
    "migration": "a legacy file was moved into its project folder",
    "skill": "a skill ran (typed or called by the model): name, args, scope, result",
    "subagent": "a subagent finished: type, prompt, status, final report, tokens, cost, time",
    "hook": "a hook ran: event, blocked, message, added context, permission decision",
    "custom_command": "a custom command or MCP prompt ran, and its result",
    "plan": "a plan was created, approved or changed status",
    "saved": "a skill, agent, command, memory, rule, prompt or instructions file was saved (a copy is kept in library/)",
}

_LOCK = threading.RLock()
_CTX: dict[str, Any] = {"agent": None, "workspace": None, "session_id": ""}
_SECRETS: set[str] = set()


# ─── Redaction ──────────────────────────────────────────────────────────────

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"), REDACTED),
    (re.compile(r"\bsk-(?:ant-|proj-|or-v1-|live-|test-)?[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-+/=]{16,}"), r"\1 " + REDACTED),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*)"
                r"(\s*[=:]\s*)(['\"]?)[^\s'\"]{6,}\3"), r"\1\2\3" + REDACTED + r"\3"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]{3,}@"), r"\1" + REDACTED + "@"),
]
_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|secret|password|passwd|authorization|access[_-]?token|auth[_-]?token"
                         r"|refresh[_-]?token|^token$|^key$|private[_-]?key)")
_SECRET_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD)")


def add_secret(value: str | None) -> None:
    """Register a literal secret (e.g. the active API key) to be masked wherever it appears."""
    v = str(value or "").strip()
    if len(v) >= 8:
        _SECRETS.add(v)


def _env_secrets() -> list[str]:
    out = []
    for k, v in os.environ.items():
        if _SECRET_ENV.search(k) and v and len(v.strip()) >= 12 and not v.startswith(("/", "http")):
            out.append(v.strip())
    return out


def redact_text(text: str) -> str:
    """Mask API keys, tokens, passwords and private keys in ``text``."""
    if not text:
        return text
    for secret in sorted(set(_env_secrets()) | _SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact strings in dicts/lists; values of secret-named keys are masked."""
    if _depth > 12:
        return obj
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if isinstance(v, str) and v and _SECRET_KEY.search(ks):
                out[ks] = REDACTED
            else:
                out[ks] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [redact(v, _depth + 1) for v in obj]
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    try:
        secret = getattr(obj, "get_secret_value", None)
        if callable(secret):
            return REDACTED
    except Exception:
        pass
    return redact_text(str(obj))


# ─── Context binding ────────────────────────────────────────────────────────

def bind(agent: Any = None, workspace: Path | str | None = None, session_id: str | None = None) -> None:
    """Set the default project/session for records written without an explicit agent
    (message queue, settings helpers, ...). The main agent binds itself at session start."""
    with _LOCK:
        if agent is not None:
            try:
                _CTX["agent"] = weakref.ref(agent)
            except TypeError:
                _CTX["agent"] = lambda a=agent: a
            try:
                key = agent.settings.api_key
                add_secret(key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or ""))
            except Exception:
                pass
        if workspace is not None:
            _CTX["workspace"] = Path(workspace)
        if session_id is not None:
            _CTX["session_id"] = str(session_id)


def unbind() -> None:
    with _LOCK:
        _CTX.update({"agent": None, "workspace": None, "session_id": ""})


def _bound_agent() -> Any:
    ref = _CTX.get("agent")
    try:
        return ref() if ref is not None else None
    except Exception:
        return None


def _resolve(agent: Any, workspace: Path | str | None, session_id: str | None) -> tuple[Path, str]:
    a = agent if agent is not None else _bound_agent()
    ws: Path | None = Path(workspace) if workspace is not None else None
    sid = session_id
    if a is not None:
        if ws is None:
            try:
                ws = Path(a.settings.workspace)
            except Exception:
                ws = None
        if sid is None:
            sid = str(getattr(getattr(a, "session", None), "active_session_id", "") or "")
    if ws is None:
        ws = _CTX.get("workspace") or Path.cwd()
    if sid is None:
        sid = str(_CTX.get("session_id") or "")
    return Path(ws), sid


def journal_path(workspace: Path | str | None = None) -> Path:
    from axon import storage
    ws, _ = _resolve(None, workspace, "")
    return storage.activity_path(ws)


# ─── Writing ────────────────────────────────────────────────────────────────

def _now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def record(kind: str, data: Any = None, *, agent: Any = None, workspace: Path | str | None = None,
           session_id: str | None = None, sync: bool = False) -> dict[str, Any] | None:
    """Append one event to the project's journal. Returns the written entry (None on failure)."""
    try:
        from axon import storage
        ws, sid = _resolve(agent, workspace, session_id)
        t = time.time()
        entry = {"ts": _now_iso(t), "t": round(t, 3), "session_id": sid, "kind": str(kind),
                 "data": redact(data if data is not None else {}), "pid": os.getpid()}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        path = storage.activity_path(ws)
        with _LOCK:
            storage.ensure_dir(path.parent)
            storage.append_line(path, line, fsync=sync)
        return entry
    except Exception:
        return None


def sync(workspace: Path | str | None = None, agent: Any = None) -> None:
    """fsync the journal (turn end, exit)."""
    try:
        from axon import storage
        ws, _ = _resolve(agent, workspace, "")
        path = storage.activity_path(ws)
        if path.is_file():
            with open(path, "ab") as f:
                os.fsync(f.fileno())
    except Exception:
        pass


def preview(text: Any, limit: int = 2000) -> str:
    s = str(text if text is not None else "")
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit:,} more chars]"


def summarize_input(args: Any, limit: int = 300) -> Any:
    """A compact copy of a tool input (long strings shortened; full input is in the transcript)."""
    if isinstance(args, dict):
        return {str(k): summarize_input(v, limit) for k, v in list(args.items())[:30]}
    if isinstance(args, list):
        return [summarize_input(v, limit) for v in args[:20]]
    if isinstance(args, str):
        return preview(args, limit)
    return args


def record_side_question(agent: Any, question: str, answer: str = "", error: str = "", source: str = "btw") -> None:
    """Journal a /btw side question and also keep it in the session transcript (a record type
    that is not replayed to the model)."""
    data = {"question": question, "answer": answer, "error": error, "source": source}
    record("side_question", data, agent=agent)
    try:
        sess = getattr(agent, "session", None)
        if sess is not None and hasattr(sess, "append"):
            sess.append("side_question", redact(data))
    except Exception:
        pass


def on_exit(agent: Any, reason: str = "exit") -> None:
    """Flush everything pending at exit: stop a running turn (so its partial answer is saved),
    journal unsent queued messages and the unsent type-ahead draft, and fsync the journal."""
    try:
        if agent is not None and getattr(agent, "busy", False):
            try:
                agent.cancel_current_turn()
                t = getattr(agent, "_turn_thread", None)
                if t is not None:
                    t.join(timeout=5.0)
            except Exception:
                pass
        unsent: list[dict[str, Any]] = []
        q = getattr(agent, "message_queue", None) if agent is not None else None
        for it in list(getattr(q, "items", []) or []):
            unsent.append({"id": getattr(it, "id", None), "text": getattr(it, "text", ""),
                           "steer": bool(getattr(it, "steer", False))})
        draft = ""
        try:
            from axon.ui.in_flight import take_type_ahead
            draft = take_type_ahead()
        except Exception:
            draft = ""
        cost = None
        try:
            cost = round(float(agent.get_combined_cost()), 6)
        except Exception:
            pass
        data: dict[str, Any] = {"reason": reason, "cost_usd": cost}
        try:
            import sys
            if type(sys.stdout).__name__ == "ScreenLog" and getattr(sys.stdout, "path", None):
                data["screen_log"] = str(sys.stdout.path)
        except Exception:
            pass
        if unsent:
            data["unsent_queue"] = unsent
        if draft.strip():
            data["unsent_draft"] = draft
        record("session_end", data, agent=agent, sync=True)
        if agent is not None and (unsent or draft.strip()):
            try:
                agent.session.append("unsent_messages", redact({"queue": unsent, "draft": draft}))
            except Exception:
                pass
    except Exception:
        pass
    sync(agent=agent)


# ─── Reading ────────────────────────────────────────────────────────────────

def iter_entries(path: Path) -> Iterable[dict[str, Any]]:
    """Entries of one journal file; blank, torn or corrupt lines are skipped."""
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and "kind" in e:
                yield e


def journal_files(workspace: Path | str | None = None, all_projects: bool = False) -> list[tuple[str, Path]]:
    """(project key, journal path) pairs: the current project, or every project."""
    from axon import storage
    if all_projects:
        root = storage.axon_home() / "projects"
        try:
            return [(p.parent.name, p) for p in sorted(root.glob("*/activity.jsonl"))]
        except OSError:
            return []
    ws, _ = _resolve(None, workspace, "")
    return [(storage.project_key(ws), storage.activity_path(ws))]


def read(workspace: Path | str | None = None, *, all_projects: bool = False, kinds: Iterable[str] | None = None,
         session: str | None = None, text: str | None = None, limit: int | None = None,
         since: float | None = None) -> list[dict[str, Any]]:
    """Journal entries (oldest first) filtered by kind(s), session id prefix, free text and time;
    ``limit`` keeps the newest N. With ``all_projects`` each entry gets a ``project`` key."""
    kset = {k.strip() for k in (kinds or []) if k and k.strip()}
    needle = (text or "").lower().strip()
    out: list[dict[str, Any]] = []
    for key, path in journal_files(workspace, all_projects):
        for e in iter_entries(path):
            if kset and e.get("kind") not in kset:
                continue
            if session and not str(e.get("session_id", "")).startswith(session):
                continue
            if since is not None and float(e.get("t") or 0) < since:
                continue
            if needle and needle not in json.dumps(e, ensure_ascii=False, default=str).lower():
                continue
            if all_projects:
                e = {**e, "project": key}
            out.append(e)
    if all_projects:
        out.sort(key=lambda e: float(e.get("t") or 0))
    return out[-limit:] if limit else out


def count(workspace: Path | str | None = None) -> int:
    """Number of lines in the journal (cheap; does not parse JSON)."""
    path = journal_path(workspace)
    n = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                n += chunk.count(b"\n")
    except OSError:
        return 0
    return n


def summary_line(e: dict[str, Any], width: int = 110) -> str:
    """One readable line describing an entry (used by /activity and the markdown export)."""
    d = e.get("data") if isinstance(e.get("data"), dict) else {}
    k = str(e.get("kind", "?"))

    def g(*keys: str) -> str:
        for key in keys:
            v = d.get(key)
            if v not in (None, "", [], {}):
                return str(v)
        return ""

    if k == "prompt":
        s = g("text")
    elif k == "command":
        s = f"{g('name')} {g('arg')}".strip() + ("" if d.get("success", True) else f"  [failed: {g('message', 'error')}]")
    elif k == "shell":
        s = f"!{g('command')}  (exit {g('exit_code') or 0}, {g('output_chars') or 0} chars)"
    elif k == "memory":
        s = f"[{g('scope')}] {g('text')}"
    elif k == "side_question":
        s = f"Q: {g('question')}  A: {g('answer', 'error')}"
    elif k in ("queue", "steer"):
        s = f"{g('action')} {g('text', 'texts', 'count')}".strip()
    elif k == "permission":
        s = f"{g('tool')} -> {g('outcome')} ({g('source')}) {g('reason')}"
    elif k in ("settings", "settings_file"):
        s = json.dumps(d.get("changes") or d.get("updates") or d, ensure_ascii=False, default=str)
    elif k == "mode":
        s = f"{g('from')} -> {g('to')}"
    elif k == "turn_start":
        s = g("prompt")
    elif k == "turn_end":
        s = (f"{g('stop_reason')} · {g('iterations') or 0} call(s) · {g('tool_calls') or 0} tool(s) · "
             f"${float(d.get('cost_usd') or 0):.4f} · {float(d.get('duration_s') or 0):.1f}s")
        if d.get("error"):
            s += f" · {g('error')}"
    elif k == "tool":
        s = f"{g('name')} {g('target')}".strip() + (" [error]" if d.get("is_error") else "")
        if d.get("full_output"):
            s += f"  (full output: {g('full_output')})"
    elif k in ("error", "retry"):
        s = g("error", "message")
    elif k == "skill":
        s = f"/{g('name')} {g('args')}".strip() + f"  ({g('source') or 'user'}{', ' + g('scope') if d.get('scope') else ''})"
    elif k == "subagent":
        s = (f"{g('agent_type')}: {g('title', 'prompt')} [{g('status')}, ${float(d.get('cost') or 0):.4f}, "
             f"{float(d.get('elapsed_s') or 0):.0f}s] {g('result')}")
    elif k == "hook":
        s = f"{g('event')} {g('tool_name')}".strip() + (" BLOCKED" if d.get("blocked") else "") + f" {g('message')}"
    elif k == "custom_command":
        s = f"/{g('name')} {g('args')}".strip() + f"  ({g('kind')})"
    elif k == "plan":
        s = f"{g('action')} {g('task', 'path')}"
    elif k == "saved":
        s = f"{g('action')} {g('type')}/{g('name')} ({g('scope')}, {g('source')}) {g('path')}"
    else:
        s = json.dumps(d, ensure_ascii=False, default=str)
    s = " ".join(str(s).split())
    return s if len(s) <= width else s[: width - 1] + "…"


def export(entries: list[dict[str, Any]], path: Path, fmt: str = "md") -> Path:
    """Write entries to ``path`` as Markdown (``md``) or JSON lines (``jsonl``)."""
    from axon.session.store import atomic_write_text
    path = Path(path).expanduser()
    if fmt == "jsonl":
        body = "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in entries)
    else:
        lines = ["# Axon activity", "", f"{len(entries)} entries", "",
                 "| Time | Project | Session | Kind | Summary |", "|---|---|---|---|---|"]
        for e in entries:
            when = str(e.get("ts", ""))[:19].replace("T", " ")
            row = [when, str(e.get("project", "")), str(e.get("session_id", ""))[-24:], str(e.get("kind", "")),
                   summary_line(e, 400)]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
        lines += ["", "## Details", ""]
        for e in entries:
            lines += [f"### {e.get('ts', '')} · {e.get('kind', '')}", "", "```json",
                      json.dumps(e.get("data", {}), indent=2, ensure_ascii=False, default=str), "```", ""]
        body = "\n".join(lines) + "\n"
    atomic_write_text(path, body)
    return path


__all__ = [
    "KINDS", "REDACTED", "add_secret", "redact", "redact_text", "bind", "unbind", "journal_path", "record",
    "sync", "preview", "summarize_input", "record_side_question", "on_exit", "iter_entries", "journal_files",
    "read", "count", "summary_line", "export",
]
