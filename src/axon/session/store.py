"""
Append-only JSONL session persistence and transcript recovery.

File format: one JSON object per line ``{"type", "timestamp", "data"}`` (optionally
encrypted as ``ENC:v1:...`` when AXON_SESSION_PASSPHRASE is set). A new transcript
starts with a ``session_meta`` record naming the workspace, so listings can be
scoped per project; older transcripts without it are shown everywhere.

Robustness:
- every line is parsed independently - corrupt / truncated lines are skipped;
- appends are fsync'ed and start on a fresh line even after a torn (crashed) write;
  whole-file writes go through :func:`atomic_write_text`
  (temp file + ``os.replace`` with a retry loop for Windows sharing violations);
- listing metadata is cached per file on (mtime_ns, size), so re-opening the
  session switcher only re-reads transcripts that changed;
- deleting a session moves it (transcript, subagents, traces, checkpoints, todos, full
  outputs) to ``projects/<key>/trash/`` - :meth:`SessionStore.restore_session` brings it back.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from axon.storage import axon_home as _axon_home
from axon.agent.state import Conversation
from axon.providers.base import AssistantTurn, ToolResultBlock, Usage
from axon.session.ledger import Ledger

_IS_WINDOWS = sys.platform == "win32"
_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4)
_CONTENT_TYPES = ("user_message", "assistant_turn", "tool_results")
_LEGACY_ERROR_PREFIX = "[Provider error:"


def _is_legacy_error_turn(data: Any) -> bool:
    """Older versions saved a failed model call as a fake assistant message "[Provider error: ...]"."""
    return (isinstance(data, dict) and not data.get("tool_uses") and not data.get("usage")
            and str(data.get("text") or "").startswith(_LEGACY_ERROR_PREFIX))


# ─── File helpers ───────────────────────────────────────────────────────────

def _retry(fn: Any) -> Any:
    """Run ``fn`` retrying on PermissionError (Windows AV / indexer file locks)."""
    last: Exception | None = None
    for delay in (0.0,) + _RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return fn()
        except PermissionError as e:  # pragma: no cover - timing dependent
            last = e
    assert last is not None
    raise last


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` atomically: temp file in the same dir, fsync, ``os.replace``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _retry(lambda: os.replace(tmp, target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_line(path: Path, line: str) -> None:
    from axon.storage import append_line
    _retry(lambda: append_line(path, line, fsync=True))


def _unlink(path: Path) -> bool:
    try:
        _retry(path.unlink)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def is_temp_workspace(workspace: Path) -> bool:
    """True when the workspace lives in the OS temp directory (tests, scratch runs)."""
    try:
        from axon.permissions.paths import is_within
    except Exception:  # pragma: no cover
        return False
    roots = {tempfile.gettempdir()}
    if not _IS_WINDOWS:
        roots.update({"/tmp", "/private/tmp", "/var/folders", "/private/var/folders"})
    return any(is_within(r, workspace) for r in roots if os.path.isdir(r))


def default_session_dir(workspace: Path) -> Path:
    """Per-project transcript directory: ~/.axon/projects/<project>/sessions."""
    try:
        from axon import storage
        return storage.sessions_dir(Path(workspace))
    except Exception:
        return _axon_home() / "sessions"


def legacy_session_dirs(workspace: Path) -> list[Path]:
    """Older locations that are still read (never written): ~/.axon/sessions and the
    former temp-workspace sibling directory."""
    dirs = [_axon_home() / "sessions"]
    try:
        from axon import storage
        dirs.insert(0, storage.axon_home() / "sessions")
    except Exception:
        pass
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _norm_ws(p: str | Path) -> str:
    try:
        s = str(Path(p).resolve())
    except (OSError, RuntimeError):
        s = str(p)
    return os.path.normcase(s)


def iter_records(path: Path, passphrase: str | None = None) -> Iterator[dict[str, Any]]:
    """Yield decoded records from a transcript, skipping blank/corrupt/undecryptable lines."""
    if passphrase is None:
        passphrase = os.environ.get("AXON_SESSION_PASSPHRASE", "")
    try:
        f = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("ENC:v1:"):
                from axon.session.crypto import decrypt_session_record
                line = decrypt_session_record(line, passphrase)
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def is_user_prompt(m: dict[str, Any]) -> bool:
    """A message the user typed (not a tool-result carrier)."""
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        kinds = {b.get("type") for b in c if isinstance(b, dict)}
        return "tool_result" not in kinds and bool(kinds & {"text", "image"})
    return False


# ─── Pricing ────────────────────────────────────────────────────────────────

def _usage_from(d: dict[str, Any]) -> Usage:
    def _i(k: str) -> int:
        try:
            return int(d.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0
    return Usage(input=_i("input"), output=_i("output"), cache_read=_i("cache_read"),
                 cache_write=_i("cache_write"), reasoning=_i("reasoning"))


def estimate_cost(model: str | None, usage: Usage) -> float:
    """USD cost of one call. Uses axon.providers.pricing when the model is known,
    else a conservative $3/$15 per-1M estimate (cache read 0.2x, write 1.25x)."""
    if model:
        try:
            from axon.providers.pricing import cost_for
            return float(cost_for(model, usage))
        except Exception:
            try:
                from axon.providers.registry import PRICING
                if model in PRICING:
                    p = PRICING[model]
                    i, o = float(p.get("input", 0)), float(p.get("output", 0))
                    cr = float(p.get("cache_read", i * 0.2))
                    cw = float(p.get("cache_write", i * 1.25))
                    unc = max(0, usage.input - usage.cache_read - usage.cache_write)
                    return (unc * i + usage.cache_read * cr + usage.cache_write * cw + usage.output * o) / 1_000_000
            except Exception:
                pass
    unc = max(0, usage.input - usage.cache_read - usage.cache_write)
    return (unc * 3.0 + usage.cache_read * 0.6 + usage.cache_write * 3.75 + usage.output * 15.0) / 1_000_000


def _ledger_record(ledger: Ledger, model: str, usage: Usage, source: str) -> None:
    try:
        ledger.record(model, usage, source=source)  # type: ignore[call-arg]
    except TypeError:
        ledger.record(model, usage, tag=source)


# ─── Listing metadata ───────────────────────────────────────────────────────

@dataclass
class SessionMeta:
    session_id: str
    created_at: float
    model: str
    message_count: int
    path: Path
    first_prompt: str = "New Session"
    total_tokens: int = 0
    total_cost: float = 0.0
    is_starred: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass
class DashboardSession:
    id: str
    title: str
    last_message: str
    status: str  # "needs_input", "working", "completed"
    updated_at: float
    is_current: bool
    path: Path
    total_tokens: int = 0
    message_count: int = 0
    tags: list[str] = field(default_factory=list)
    is_starred: bool = False
    total_cost: float = 0.0


@dataclass
class _Summary:
    workspace: str | None = None
    model: str = "unknown"
    count: int = 0
    custom_title: str | None = None
    first_prompt: str = ""
    user_prompts: list[str] = field(default_factory=list)
    first_assistant: str = ""
    last_msg: str = ""
    last_type: str = ""
    last_stop: str = ""
    tags: list[str] = field(default_factory=list)
    starred: bool = False
    tokens: int = 0
    cost: float = 0.0


def clean_session_topic(text: str) -> str:
    """Extract a clean, human-friendly topic string from prompt text."""
    t = re.sub(r"```[\s\S]*?```", "", text)
    t = re.sub(r"`[^`]*`", "", t)
    t = re.sub(r"[*_#~>]", "", t)
    t = re.sub(r"^[!\/@\-\*\d\.\s]+", "", t).strip()
    t = " ".join(t.split())
    if not t:
        return ""
    return t[0].upper() + t[1:]


_cache_lock = threading.Lock()
_summary_cache: dict[str, tuple[int, int, _Summary]] = {}


def _scan(path: Path, default_model: str | None) -> _Summary:
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _Summary()
    ck = str(path)
    with _cache_lock:
        hit = _summary_cache.get(ck)
    if hit and (hit[0], hit[1]) == key:
        return hit[2]

    s = _Summary()
    for entry in iter_records(path):
        t = entry.get("type")
        data = entry.get("data", {})
        if not isinstance(data, dict):
            data = {}
        if t == "session_meta":
            s.workspace = data.get("workspace") or s.workspace
            continue
        if t == "session_rename":
            title = str(data.get("title") or "").strip()
            if title:
                s.custom_title = title
            continue
        if t == "session_tag":
            tg = str(data.get("tag", "") or "").strip().lstrip("#")
            s.tags = [] if not tg or tg == "-" else [tg]
            continue
        if t == "session_star":
            s.starred = bool(data.get("starred", False))
            continue
        if t not in _CONTENT_TYPES:
            continue
        s.count += 1
        s.last_type = t
        if t == "user_message":
            txt = data.get("content", "")
            texts: list[str] = []
            if isinstance(txt, str) and txt.strip():
                texts = [txt.strip()]
            elif isinstance(txt, list):
                texts = [str(b["text"]).strip() for b in txt if isinstance(b, dict) and b.get("text")]
            for tx in texts:
                if not s.first_prompt:
                    s.first_prompt = tx.splitlines()[0].strip()[:48]
                if len(s.user_prompts) < 8:
                    s.user_prompts.append(tx)
                s.last_msg = tx.splitlines()[-1].strip()[:65]
        elif t == "assistant_turn":
            txt = data.get("text", "")
            if isinstance(txt, str) and txt.strip():
                if not s.first_assistant:
                    s.first_assistant = txt.strip()
                s.last_msg = txt.strip().splitlines()[0].strip()[:65]
            s.last_stop = str(data.get("stop_reason") or "")
            model = data.get("model") or default_model
            if model:
                s.model = str(model)
            u = data.get("usage")
            if isinstance(u, dict):
                usage = _usage_from(u)
                s.tokens += usage.input + usage.output
                s.cost += estimate_cost(model, usage)

    with _cache_lock:
        _summary_cache[ck] = (key[0], key[1], s)
    return s


class SessionStore:
    def __init__(self, workspace: Path, session_dir: Path | None = None) -> None:
        self.workspace = Path(workspace)
        self.session_dir = Path(session_dir) if session_dir is not None else default_session_dir(self.workspace)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._explicit_dir = session_dir is not None
        self.legacy_dirs: list[Path] = [] if session_dir is not None else [
            d for d in legacy_session_dirs(self.workspace) if d != self.session_dir and d.is_dir()
        ]
        self.model: str | None = None  # active model, recorded with each assistant turn
        self.active_session_id: str = self._generate_session_id()
        self.active_file: Path = self.session_dir / f"{self.active_session_id}.jsonl"

    def _dirs(self) -> list[Path]:
        return [self.session_dir] + self.legacy_dirs

    def _file(self, session_id: str) -> Path:
        """Existing transcript for ``session_id`` (project dir first, then legacy), else the new path."""
        name = f"{session_id}.jsonl"
        for d in self._dirs():
            p = d / name
            if p.exists():
                return p
        return self.session_dir / name

    def exists(self, session_id: str) -> bool:
        return self._file(session_id).exists()

    # ── identity ──
    def _generate_session_id(self) -> str:
        """session_<date>_<workspace>_<unique>"""
        from datetime import datetime
        import uuid
        date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        ws_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", self.workspace.name).strip("_") or "workspace"
        return f"session_{date_str}_{ws_name}_{uuid.uuid4().hex[:6]}"

    def path_for(self, session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", session_id or "") or session_id in (".", ".."):
            raise ValueError(f"Invalid session id: {session_id!r}")
        return self.session_dir / f"{session_id}.jsonl"

    def open(self, session_id: str | None = None) -> str:
        if session_id:
            self.active_session_id = session_id
        else:
            self.active_session_id = self._generate_session_id()
        self.active_file = self._file(self.active_session_id)
        return self.active_session_id

    # ── writing ──
    def _encode(self, entry: dict[str, Any]) -> str:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        passphrase = os.environ.get("AXON_SESSION_PASSPHRASE", "")
        if passphrase:
            from axon.session.crypto import encrypt_session_record
            line = encrypt_session_record(line, passphrase)
        return line

    def _write_record(self, target: Path, record_type: str, data: Any) -> None:
        if not target.exists() or target.stat().st_size == 0:
            meta = {"type": "session_meta", "timestamp": time.time(),
                    "data": {"workspace": str(self.workspace), "version": 1}}
            _append_line(target, self._encode(meta))
        entry = {
            "type": record_type,
            "timestamp": time.time(),
            "data": data if isinstance(data, (dict, list, str, int, float, bool)) or data is None else str(data),
        }
        _append_line(target, self._encode(entry))

    def append(self, record_type: str, data: Any) -> None:
        """Append one JSON line + fsync for crash durability (ADR-007)."""
        self._write_record(self.active_file, record_type, data)

    def append_user(self, text: str) -> None:
        self.append("user_message", {"role": "user", "content": text})

    def append_turn(self, turn: AssistantTurn, model: str | None = None, cost: float | None = None,
                    duration_s: float | None = None) -> None:
        """Persist an assistant turn: text, thinking, full tool inputs, usage, cost and timing."""
        u = turn.usage
        model = model or getattr(turn, "model", None) or self.model
        if cost is None and u is not None and model:
            cost = estimate_cost(model, u)
        turn_data = {
            "stop_reason": turn.stop_reason,
            "text": turn.text,
            "thinking": turn.thinking,
            "tool_uses": [{"id": tu.id, "name": tu.name, "input": tu.input} for tu in turn.tool_uses],
            "native": turn.native,
            "model": model,
            "cost": round(float(cost), 8) if cost is not None else None,
            "duration_s": round(float(duration_s), 3) if duration_s is not None else None,
            "usage": {
                "input": u.input, "output": u.output, "cache_read": u.cache_read,
                "cache_write": u.cache_write, "reasoning": u.reasoning,
            } if u else None,
        }
        self.append("assistant_turn", turn_data)

    def append_results(self, results: list[ToolResultBlock], timings: dict[str, float] | None = None,
                       full_outputs: dict[str, str] | None = None) -> None:
        """Persist tool results (the content the model saw); ``timings`` maps tool_use_id -> seconds and
        ``full_outputs`` maps tool_use_id -> file holding the untruncated output."""
        self.append("tool_results", [
            {"tool_use_id": r.tool_use_id, "content": r.content, "is_error": r.is_error,
             **({"duration_s": round(timings[r.tool_use_id], 3)} if timings and r.tool_use_id in timings else {}),
             **({"full_output": full_outputs[r.tool_use_id]} if full_outputs and r.tool_use_id in full_outputs else {})}
            for r in results
        ])

    def append_permission(self, tool_use_id: str, tool: str, outcome: str, reason: str, source: str = "",
                          approval: str | None = None) -> None:
        """Persist a permission decision for a tool call (shown by /inspect and resumes)."""
        self.append("permission", {"tool_use_id": tool_use_id, "tool": tool, "outcome": outcome,
                                   "reason": reason, "source": source, "approval": approval})

    def append_rewind(self, messages: list[dict[str, Any]], mode: str, prompt_index: int) -> None:
        """Persist a conversation rewind: on resume the history is replaced by ``messages``. The
        transcript is append-only, so the rewound messages stay in the file (``/history all``)."""
        self.append("rewind", {"messages": messages, "mode": mode, "prompt_index": prompt_index})
        _journal("rewind", {"mode": mode, "prompt_index": prompt_index, "kept_messages": len(messages),
                            "transcript": str(self.active_file)}, self)

    def user_message_times(self, session_id: str | None = None) -> list[float]:
        """Timestamps of the user prompts in the *current* history of a transcript (oldest first).

        Rewinds keep a prefix of the prompts and compactions keep a suffix, mirroring read_conversation."""
        target = self._file(session_id or self.active_session_id)
        out: list[float] = []
        for entry in iter_records(target):
            t = entry.get("type")
            data = entry.get("data")
            if t == "turn_failed" and isinstance(data, dict) and data.get("rolled_back") and out:
                out.pop()  # that prompt is not in the replayed history
            elif t == "user_message":
                try:
                    out.append(float(entry.get("timestamp") or 0.0))
                except (TypeError, ValueError):
                    out.append(0.0)
            elif t in ("rewind", "compaction") and isinstance(data, dict) and isinstance(data.get("messages"), list):
                n = sum(1 for m in data["messages"] if isinstance(m, dict) and is_user_prompt(m))
                if t == "rewind":
                    out = out[:n]
                else:
                    out = ([0.0] * max(0, n - len(out))) + (out[-n:] if n else [])
        return out

    def _meta_record(self, record_type: str, data: dict[str, Any], session_id: str | None) -> None:
        sid = session_id or self.active_session_id
        self._write_record(self._file(sid), record_type, data)

    def rename_session(self, new_title: str, session_id: str | None = None) -> str:
        clean_title = " ".join(new_title.split())[:64]
        self._meta_record("session_rename", {"title": clean_title}, session_id)
        _journal("rename", {"title": clean_title, "session": session_id or self.active_session_id}, self)
        return clean_title

    def tag_session(self, tag_name: str, session_id: str | None = None) -> str:
        """Set (or clear with '-') the session tag."""
        clean_tag = tag_name.strip().lstrip("#")[:32]
        if clean_tag == "-":
            clean_tag = ""
        self._meta_record("session_tag", {"tag": clean_tag}, session_id)
        return clean_tag

    def is_starred(self, session_id: str | None = None) -> bool:
        sid = session_id or self.active_session_id
        target = self._file(sid)
        return _scan(target, self.model).starred if target.exists() else False

    def star_session(self, session_id: str | None = None) -> bool:
        """Toggle star/favorite status of a session."""
        new_status = not self.is_starred(session_id)
        self._meta_record("session_star", {"starred": new_status}, session_id)
        return new_status

    def star_all_sessions(self, starred: bool | None = None) -> bool:
        """Toggle or set star status across all listed sessions."""
        recent = self.list_recent()
        if not recent:
            return False
        target_status = any(not m.is_starred for m in recent) if starred is None else starred
        for m in recent:
            if m.path.exists():
                self._meta_record("session_star", {"starred": target_status}, m.session_id)
        return target_status

    # ── reading ──
    def read_conversation(self, session_id: str) -> Conversation:
        """Reconstruct a Conversation without changing the active session."""
        target = self._file(session_id)
        if not target.exists():
            raise FileNotFoundError(f"Session transcript not found: {target}")
        messages: list[dict[str, Any]] = []
        prompt_start = 0          # len(messages) before the latest user_message record
        prev_type = ""
        for entry in iter_records(target):
            t = entry.get("type")
            data = entry.get("data", {})
            try:
                if t == "turn_failed" and isinstance(data, dict):
                    # The failed prompt is kept in the file (and the journal) but never replayed to the model.
                    if data.get("rolled_back"):
                        messages = messages[:prompt_start]
                    prev_type = t
                    continue
                if t == "assistant_turn" and _is_legacy_error_turn(data):
                    if prev_type == "user_message":
                        messages = messages[:prompt_start]
                    prev_type = "turn_failed"
                    continue
                if t in _CONTENT_TYPES:
                    prev_type = t
                if t == "user_message" and isinstance(data, dict):
                    prompt_start = len(messages)
                    messages.append({"role": data.get("role", "user"), "content": data.get("content", "")})
                elif t == "assistant_turn" and isinstance(data, dict):
                    native = data.get("native")
                    if isinstance(native, dict) and native.get("role"):
                        msg = dict(native)
                        if msg.get("content") is None:
                            msg["content"] = ""
                        messages.append(msg)
                    elif isinstance(native, list):
                        messages.append({"role": "assistant", "content": native})
                    else:
                        blocks: list[dict[str, Any]] = []
                        if data.get("text"):
                            blocks.append({"type": "text", "text": data["text"]})
                        for tu in data.get("tool_uses", []) or []:
                            blocks.append({"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu.get("input", {})})
                        if not blocks and isinstance(native, str) and native:
                            blocks.append({"type": "text", "text": native})
                        messages.append({"role": "assistant", "content": blocks or data.get("text", "")})
                elif t in ("compaction", "rewind") and isinstance(data, dict) and isinstance(data.get("messages"), list):
                    messages = [dict(m) for m in data["messages"] if isinstance(m, dict)]
                elif t == "tool_results" and isinstance(data, list):
                    if data and isinstance(data[0], dict) and data[0].get("role") == "tool":
                        messages.extend(dict(m) for m in data if isinstance(m, dict))
                        continue
                    content = [
                        {
                            "type": "tool_result",
                            "tool_use_id": r["tool_use_id"],
                            "content": r.get("content", ""),
                            **({"is_error": True} if r.get("is_error") else {}),
                        }
                        for r in data if isinstance(r, dict) and "tool_use_id" in r
                    ]
                    if content:
                        messages.append({"role": "user", "content": content})
            except (KeyError, TypeError):
                continue
        return Conversation(messages)

    def recorded_tool_outputs(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Tool results recorded in a transcript, oldest first, numbered from 1:
        {"n", "id", "name", "target", "content", "full_output"}. ``content`` is the complete output
        (read from the saved sidecar when the model only saw a truncated copy)."""
        calls: dict[str, tuple[str, str]] = {}
        out: list[dict[str, Any]] = []
        for entry in iter_records(self._file(session_id or self.active_session_id)):
            t = entry.get("type")
            data = entry.get("data")
            if t == "assistant_turn" and isinstance(data, dict):
                for tu in data.get("tool_uses") or []:
                    if isinstance(tu, dict) and tu.get("id"):
                        inp = tu.get("input") if isinstance(tu.get("input"), dict) else {}
                        target = (inp.get("path") or inp.get("file_path") or inp.get("command") or inp.get("pattern")
                                  or inp.get("query") or inp.get("url") or inp.get("description") or "")
                        calls[str(tu["id"])] = (str(tu.get("name") or "?"), str(target))
            elif t == "tool_results" and isinstance(data, list):
                for r in data:
                    if not isinstance(r, dict):
                        continue
                    tid = str(r.get("tool_use_id") or r.get("tool_call_id") or "")
                    content = r.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False, default=str)
                    full = str(r.get("full_output") or "")
                    if full:
                        try:
                            content = Path(full).read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            pass
                    name, target = calls.get(tid, ("?", ""))
                    out.append({"n": len(out) + 1, "id": tid, "name": name, "target": target,
                                "content": content, "full_output": full})
        return out

    def last_failed_turn(self, session_id: str | None = None) -> dict[str, Any] | None:
        """{"prompt", "resume"} when the transcript ends on a failed model call (so /retry works after
        a resume), else None."""
        failed: dict[str, Any] | None = None
        last_prompt = ""
        prev = ""
        for entry in iter_records(self._file(session_id or self.active_session_id)):
            t = entry.get("type")
            data = entry.get("data")
            if t == "turn_failed" and isinstance(data, dict):
                failed = {"prompt": str(data.get("prompt") or ""), "resume": bool(data.get("resume"))}
            elif t == "assistant_turn" and _is_legacy_error_turn(data):
                failed = {"prompt": last_prompt if prev == "user_message" else "", "resume": prev != "user_message"}
            elif t in _CONTENT_TYPES:
                failed = None
                if t == "user_message" and isinstance(data, dict):
                    c = data.get("content")
                    last_prompt = c if isinstance(c, str) else " ".join(
                        str(b.get("text", "")) for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
            if t in _CONTENT_TYPES or t == "turn_failed":
                prev = t if not (t == "assistant_turn" and _is_legacy_error_turn(data)) else "turn_failed"
        return failed

    def load(self, session_id: str) -> Conversation:
        """Reconstruct Conversation and make it the active session."""
        conv = self.read_conversation(session_id)
        self.open(session_id)
        return conv

    def _subagent_files(self, session_id: str) -> list[Path]:
        found: dict[str, Path] = {}
        for d in reversed(self._dirs()):
            for p in d.glob(f"{glob_escape(session_id)}_sub_*.jsonl"):
                found[p.name] = p
        return [found[k] for k in sorted(found)]

    def load_ledger(self, session_id: str, model: str, include_subagents: bool = True) -> Ledger:
        """Rebuild the Ledger of a session from recorded usage (plus its subagents)."""
        ledger = Ledger()
        self._fill_ledger(ledger, self._file(session_id), model, "main")
        if include_subagents and "_sub_" not in session_id:
            for sub_file in self._subagent_files(session_id):
                idx = sub_file.stem.split("_sub_")[-1]
                self._fill_ledger(ledger, sub_file, model, f"subagent_{idx}")
        return ledger

    def _fill_ledger(self, ledger: Ledger, path: Path, model: str, source: str) -> None:
        if not path.exists():
            return
        for entry in iter_records(path):
            if entry.get("type") != "assistant_turn":
                continue
            data = entry.get("data", {})
            u = data.get("usage") if isinstance(data, dict) else None
            if isinstance(u, dict):
                _ledger_record(ledger, str(data.get("model") or model), _usage_from(u), source)

    def load_workspace_ledger(self, model: str) -> Ledger:
        """Cumulative ledger across this workspace's sessions (subagents included)."""
        total = Ledger()
        metas = self.list_recent()
        total.chat_count = len(metas)
        for m in metas:
            self._fill_ledger(total, m.path, model, "main")
            for sub in self._subagent_files(m.session_id):
                self._fill_ledger(total, sub, model, f"subagent_{sub.stem.split('_sub_')[-1]}")
        return total

    # ── listing ──
    def _main_files(self, all_workspaces: bool = False, strict: bool = False) -> list[tuple[Path, os.stat_result]]:
        """Main transcripts: everything in the project dir, plus legacy files that belong to
        this workspace (or carry no workspace record, unless ``strict``)."""
        out: list[tuple[Path, os.stat_result]] = []
        seen: set[str] = set()
        ws_key = _norm_ws(self.workspace)
        for idx, d in enumerate(self._dirs()):
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".jsonl") or "_sub_" in e.name or e.name.startswith(".") or e.name in seen:
                    continue
                if e.name.endswith(".traces.jsonl"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                p = Path(e.path)
                # The per-project directory belongs to this project; shared/legacy
                # directories are filtered by the workspace recorded in session_meta.
                shared = idx > 0 or self._explicit_dir
                if not all_workspaces and shared:
                    ws = _scan(p, self.model).workspace
                    if ws and _norm_ws(ws) != ws_key:
                        continue
                    if not ws and strict and idx > 0:
                        continue
                seen.add(e.name)
                out.append((p, st))
        out.sort(key=lambda x: x[1].st_mtime, reverse=True)
        return out

    def _sub_index(self) -> dict[str, list[Path]]:
        idx: dict[str, list[Path]] = {}
        try:
            for d in self._dirs():
                for e in os.scandir(d):
                    if e.name.endswith(".jsonl") and "_sub_" in e.name and not e.name.endswith(".traces.jsonl"):
                        parent = e.name[:-6].rsplit("_sub_", 1)[0]
                        idx.setdefault(parent, []).append(Path(e.path))
        except OSError:
            pass
        return idx

    def list_recent(self, limit: int | None = None, all_workspaces: bool = False) -> list[SessionMeta]:
        """Sessions of this workspace (legacy unscoped transcripts included), newest first."""
        files = self._main_files(all_workspaces)
        if limit is not None:
            files = files[:limit]
        subs = self._sub_index()
        items: list[SessionMeta] = []
        for f, st in files:
            s = _scan(f, self.model)
            tokens, cost = s.tokens, s.cost
            for sub in subs.get(f.stem, []):
                ss = _scan(sub, self.model)
                tokens += ss.tokens
                cost += ss.cost
            title = s.custom_title or s.first_prompt
            if not title:
                from datetime import datetime
                title = f"New Session ({datetime.fromtimestamp(st.st_mtime).strftime('%I:%M %p')})"
            items.append(SessionMeta(
                session_id=f.stem, created_at=st.st_mtime, model=s.model, message_count=s.count,
                path=f, first_prompt=title, total_tokens=tokens, total_cost=round(cost, 5),
                is_starred=s.starred, tags=list(s.tags),
            ))
        return items

    def dashboard_sessions(self, active_id: str, limit: int | None = None) -> list[DashboardSession]:
        files = self._main_files()
        if limit is not None:
            files = files[:limit]
        subs = self._sub_index()
        out: list[DashboardSession] = []
        for f, st in files:
            s = _scan(f, self.model)
            tokens, cost = s.tokens, s.cost
            for sub in subs.get(f.stem, []):
                ss = _scan(sub, self.model)
                tokens += ss.tokens
                cost += ss.cost
            title = ""
            if s.custom_title:
                title = s.custom_title[:28]
            elif s.user_prompts:
                for p in s.user_prompts:
                    pc = clean_session_topic(p)
                    if len(pc) > 3 and pc.lower() not in ("hi", "hello", "hey", "test"):
                        title = pc[:28]
                        break
                if not title:
                    title = clean_session_topic(s.user_prompts[0])[:28]
            elif s.first_assistant:
                title = clean_session_topic(s.first_assistant)[:28]
            if not title:
                from datetime import datetime
                title = f"New Session ({datetime.fromtimestamp(st.st_mtime).strftime('%I:%M %p').lstrip('0')})"
            is_curr = f.stem == active_id
            last_msg = s.last_msg or ("Active workspace session" if is_curr else "")
            if s.last_type == "assistant_turn" and s.last_stop in ("", "end_turn", "stop"):
                status = "completed"
            elif s.last_type in ("tool_results",) or s.last_stop == "tool_use":
                status = "working"
            else:
                status = "needs_input"
            out.append(DashboardSession(
                id=f.stem, title=title, last_message=last_msg or "Ready for prompt", status=status,
                updated_at=st.st_mtime, is_current=is_curr, path=f, total_tokens=tokens,
                message_count=s.count, tags=list(s.tags), is_starred=s.starred, total_cost=round(cost, 5),
            ))
        return out

    # ── deletion (to the trash) ──
    def _session_artifacts(self, session_id: str) -> list[Path]:
        """Every file/dir that belongs to a session: transcript, subagents, traces, checkpoints,
        todos and full-output files (current and legacy locations)."""
        main_file = self._file(session_id)
        items: list[Path] = [main_file, *self._subagent_files(session_id),
                             main_file.with_name(f"{session_id}.traces.jsonl"),
                             self.workspace / ".axon" / "checkpoints" / session_id]
        try:
            from axon import storage
            pdir = storage.project_dir(self.workspace, create=False)
            items += [pdir / "traces" / f"{session_id}.jsonl", pdir / "checkpoints" / session_id,
                      pdir / "todos" / f"{session_id}.json", pdir / "outputs" / session_id]
        except Exception:
            pass
        out: list[Path] = []
        for p in items:
            if p.exists() and p not in out:
                out.append(p)
        return out

    def trash_root(self) -> Path:
        try:
            from axon import storage
            return storage.trash_dir(self.workspace)
        except Exception:
            return self.session_dir.parent / "trash"

    def delete_session(self, session_id: str) -> bool:
        """Move a session (transcript, subagents, traces, checkpoints, todos, outputs) to the
        project trash. Nothing is destroyed; :meth:`restore_session` undoes it."""
        import shutil
        main_file = self._file(session_id)
        if not main_file.exists():
            return False
        title = ""
        try:
            title = _scan(main_file, self.model).custom_title or _scan(main_file, self.model).first_prompt
        except Exception:
            pass
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = self.trash_root() / f"{stamp}_{session_id}"
        n = 2
        while dest.exists():
            dest = self.trash_root() / f"{stamp}_{session_id}-{n}"
            n += 1
        moved: list[dict[str, str]] = []
        try:
            dest.mkdir(parents=True, exist_ok=False)
        except OSError:
            return False
        for i, src in enumerate(self._session_artifacts(session_id)):
            target = dest / f"{i:02d}_{src.name}"
            try:
                _retry(lambda s=src, t=target: shutil.move(str(s), str(t)))
                moved.append({"from": str(src), "to": target.name})
            except OSError:
                continue
        manifest = {"session_id": session_id, "title": title, "deleted_at": time.time(),
                    "workspace": str(self.workspace), "items": moved}
        try:
            atomic_write_text(dest / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        except OSError:
            pass
        with _cache_lock:
            _summary_cache.pop(str(main_file), None)
        deleted = any(m["from"] == str(main_file) for m in moved)
        _journal("delete", {"session": session_id, "title": title, "trash": str(dest), "files": len(moved)}, self)
        if session_id == self.active_session_id:
            self.open()
        return deleted

    def list_trash(self) -> list[dict[str, Any]]:
        """Trashed sessions of this project, newest first: {id, title, deleted_at, path, files}."""
        out: list[dict[str, Any]] = []
        root = self.trash_root()
        try:
            dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            return out
        for d in dirs:
            try:
                meta = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {"session_id": d.name.split("_", 1)[-1], "deleted_at": d.stat().st_mtime, "items": []}
            out.append({"id": str(meta.get("session_id") or d.name), "title": str(meta.get("title") or ""),
                        "deleted_at": float(meta.get("deleted_at") or 0), "path": d,
                        "files": len(meta.get("items") or [])})
        out.sort(key=lambda m: m["deleted_at"], reverse=True)
        return out

    def restore_session(self, query: str) -> str | None:
        """Move a trashed session back (id, unique prefix or trash folder name). Returns its id.
        Files whose original location is occupied are left in the trash folder."""
        import shutil
        items = self.list_trash()
        q = query.strip()
        hit = ([m for m in items if m["id"] == q or m["path"].name == q]
               or [m for m in items if m["id"].startswith(q) or m["path"].name.startswith(q)])
        if not q or not hit:
            return None
        entry = hit[0]  # newest deletion of that session
        d: Path = entry["path"]
        try:
            meta = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        left = []
        for it in meta.get("items") or []:
            src, dst = d / str(it.get("to")), Path(str(it.get("from")))
            if not src.exists():
                continue
            if dst.exists():
                left.append(it)
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _retry(lambda s=src, t=dst: shutil.move(str(s), str(t)))
            except OSError:
                left.append(it)
        if not left:
            shutil.rmtree(d, ignore_errors=True)
        else:
            meta["items"] = left
            try:
                atomic_write_text(d / "manifest.json", json.dumps(meta, indent=2, ensure_ascii=False))
            except OSError:
                pass
        _journal("restore", {"session": entry["id"], "from": str(d), "left_in_trash": len(left)}, self)
        return str(entry["id"])

    def delete_all_sessions(self, keep_active: bool = False) -> int:
        """Move every session listed for this workspace (and their subagents) to the trash."""
        count = 0
        for m in self.list_recent():
            if keep_active and m.session_id == self.active_session_id:
                continue
            if self.delete_session(m.session_id):
                count += 1
        if not keep_active:
            self.open()
        return count

    def latest(self) -> str | None:
        """Most recent session of *this* workspace (unscoped legacy files are ignored)."""
        files = self._main_files(strict=True)
        return files[0][0].stem if files else None


def restore_retry_state(agent: Any, session_id: str | None = None) -> None:
    """After a resume: if the session ended on a failed model call, make /retry able to resend it."""
    try:
        failed = agent.session.last_failed_turn(session_id)
    except Exception:
        failed = None
    try:
        agent.last_failed_turn = failed
    except Exception:
        pass


def _journal(kind: str, data: dict[str, Any], store: "SessionStore") -> None:
    try:
        from axon.session import activity
        activity.record(kind, data, workspace=store.workspace, session_id=store.active_session_id)
    except Exception:
        pass


LIBRARY_RECORD_TYPES = ("skill_run", "subagent_result", "hook_run", "command_run", "plan", "saved")


def library_record_text(t: str, d: dict[str, Any]) -> str:
    """One line for a library record (see axon.session.library)."""
    if t == "skill_run":
        return f"skill /{d.get('name', '')} {d.get('args', '')}".rstrip() + f" ({d.get('source') or 'user'})"
    if t == "subagent_result":
        return (f"subagent {d.get('agent_type', '')} #{d.get('index', '')} {d.get('status', '')}: "
                f"{d.get('title') or d.get('prompt', '')} -> {str(d.get('result') or '')[:200]}")
    if t == "hook_run":
        return f"hook {d.get('event', '')}" + (" BLOCKED" if d.get("blocked") else "") + f" {d.get('message') or ''}".rstrip()
    if t == "command_run":
        return f"/{d.get('name', '')} {d.get('args', '')}".rstrip() + f" ({d.get('kind', '')})"
    if t == "plan":
        return f"plan {d.get('action', '')} {d.get('path', '')}".rstrip()
    return f"{d.get('action', 'saved')} {d.get('type', '')}/{d.get('name', '')} -> {d.get('copy') or d.get('path', '')}"


def iter_history(path: Path) -> Iterator[dict[str, Any]]:
    """Every conversation record of a transcript in file order, including messages that a rewind
    or compaction later removed: {"index", "type", "timestamp", "role", "text", "data"}."""
    i = 0
    for entry in iter_records(path):
        t = entry.get("type")
        data = entry.get("data")
        if t not in _CONTENT_TYPES and t not in ("rewind", "compaction", "side_question", "shell_command",
                                                 "steer_message", "unsent_messages", *LIBRARY_RECORD_TYPES):
            continue
        i += 1
        text = ""
        role = ""
        if t == "user_message" and isinstance(data, dict):
            role = "user"
            c = data.get("content")
            text = c if isinstance(c, str) else " ".join(
                str(b.get("text", "")) for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        elif t == "assistant_turn" and isinstance(data, dict):
            role = "assistant"
            text = str(data.get("text") or "")
            if not text and isinstance(data.get("native"), dict):
                nc = data["native"].get("content")
                text = nc if isinstance(nc, str) else ""
            tools = [str(tu.get("name")) for tu in data.get("tool_uses") or [] if isinstance(tu, dict)]
            if tools:
                text = (text + " " if text else "") + f"[tools: {', '.join(tools)}]"
        elif t == "tool_results" and isinstance(data, list):
            role = "tool"
            text = " | ".join(str(r.get("content", ""))[:200] for r in data if isinstance(r, dict))
        elif t in ("rewind", "compaction") and isinstance(data, dict):
            role = "event"
            text = f"{t}: history replaced by {len(data.get('messages') or [])} message(s)"
        elif t == "side_question" and isinstance(data, dict):
            role = "event"
            text = f"/btw {data.get('question', '')} -> {data.get('answer', '')}"
        elif t == "shell_command" and isinstance(data, dict):
            role = "event"
            text = f"!{data.get('command', '')} (exit {data.get('exit_code')})"
        elif t == "steer_message" and isinstance(data, dict):
            role = "user"
            text = f"[steer] {data.get('text', '')}"
        elif t in LIBRARY_RECORD_TYPES and isinstance(data, dict):
            role = "event"
            text = library_record_text(t, data)
        elif t == "unsent_messages" and isinstance(data, dict):
            role = "event"
            text = f"unsent at exit: {len(data.get('queue') or [])} queued, draft {len(str(data.get('draft') or ''))} chars"
        yield {"index": i, "type": t, "timestamp": entry.get("timestamp"), "role": role, "text": text, "data": data}


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def load_dashboard_sessions(
    workspace: Path,
    active_id: str,
    limit: int | None = None,
    session_dir: Path | None = None,
) -> list[DashboardSession]:
    """Structured session list for the interactive switcher."""
    store = SessionStore(Path(workspace), session_dir=session_dir)
    return store.dashboard_sessions(active_id, limit=limit)


# Backward-compatible private alias used by the switcher
_clean_session_topic = clean_session_topic
