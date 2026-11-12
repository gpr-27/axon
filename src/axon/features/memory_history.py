"""Write history, optimistic concurrency and redaction of past versions for every memory file and note.

Every write to a memory (a scope file such as AXON.md, or a topic note) appends one entry to a small local log:
who wrote it (user via /memory, ``#`` quick-add, auto-capture, the model's file tools, the dashboard, a restore, a
dream), when, which session, the sha256 of the resulting file and the text itself (already redacted of secrets).
The log lives next to the project's data (``projects/<key>/memory-history``; global files under
``~/.axon/memory-history``), keeps at most ``MAX_VERSIONS`` versions and ``MAX_BYTES`` of text per memory, and never
leaves the machine.

Optimistic concurrency: a writer may pass the sha256 of the text it read (``expected_sha``). If the file has changed
since, the write raises :class:`Conflict` (a ``ValueError`` with a plain message) and nothing is written.

Redaction: :func:`redact_version` scrubs a secret from a past version's stored text but keeps the entry (who, when,
hash) as the audit trail.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

MAX_VERSIONS = 40
MAX_BYTES = 2_000_000
PLACEHOLDER = "[redacted]"

_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar("axon_memory_actor", default="user (/memory)")
_SESSION: contextvars.ContextVar[str] = contextvars.ContextVar("axon_memory_session", default="")


class Conflict(ValueError):
    """The memory changed since the writer read it. Nothing was written."""

    def __init__(self, label: str, expected: str, actual: str) -> None:
        self.label, self.expected, self.actual = label, expected, actual
        super().__init__(f"{label} changed since it was read (you had {expected[:8] or 'nothing'}, it is now "
                         f"{actual[:8] or 'gone'}): someone else, for example the dashboard, edited it. "
                         "Nothing was written; read it again and retry.")


@contextlib.contextmanager
def writing_as(actor: str, session: str = "") -> Iterator[None]:
    """Everything written inside this block is logged as coming from ``actor`` (and ``session``)."""
    t1 = _ACTOR.set(actor)
    t2 = _SESSION.set(session or _SESSION.get())
    try:
        yield
    finally:
        _ACTOR.reset(t1)
        _SESSION.reset(t2)


def sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def file_sha(path: Path) -> str:
    """sha256 of a file's text as the memory code reads it ('' when it does not exist)."""
    try:
        return sha(Path(path).read_text(encoding="utf-8", errors="replace")) if Path(path).is_file() else ""
    except OSError:
        return ""


def check(label: str, path: Path, expected: str | None) -> None:
    """Raise :class:`Conflict` when ``expected`` is given and the file no longer has that hash."""
    if expected is None:
        return
    now = file_sha(path)
    if now == "" and expected == sha(""):
        return                                            # "I saw it empty" matches a file that does not exist yet
    if now != expected:
        raise Conflict(label, expected, now)


# ─── keys and storage ───────────────────────────────────────────────────────

FILE_KEY_NAMES = {"global": "AXON.md", "project": "AXON.md", "local": "AXON.local.md"}
_NOTE_NS = {"global": "global-notes", "project": "auto", "repo": "repo"}


def file_key(scope: str) -> str:
    return f"file-{scope}"


def note_key(mf_scope: str, file: str) -> str:
    return f"{_NOTE_NS[mf_scope]}/{file}"


def _store_dir(ws: Path, key: str) -> Path:
    from axon import storage
    if key == "file-global" or key.startswith("global-notes/"):
        return storage.axon_home() / "memory-history"
    return storage.project_dir(Path(ws)) / "memory-history"


def _log_path(ws: Path, key: str) -> Path:
    return _store_dir(ws, key) / (re.sub(r"[^A-Za-z0-9._-]", "__", key) + ".jsonl")


@dataclass
class Entry:
    v: int
    ts: float
    actor: str
    session: str
    action: str
    sha: str
    size: int
    text: str = ""
    redacted: bool = False
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def brief(self) -> dict[str, Any]:
        return {"v": self.v, "ts": self.ts, "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
                "actor": self.actor, "session": self.session, "action": self.action, "sha": self.sha[:12],
                "size": self.size, "redacted": self.redacted, "note": self.note}


def _load(ws: Path, key: str) -> list[Entry]:
    out: list[Entry] = []
    try:
        for ln in _log_path(ws, key).read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(ln)
                out.append(Entry(v=int(d["v"]), ts=float(d["ts"]), actor=str(d.get("actor", "")), session=str(d.get("session", "")),
                                 action=str(d.get("action", "")), sha=str(d.get("sha", "")), size=int(d.get("size", 0)),
                                 text=str(d.get("text", "")), redacted=bool(d.get("redacted")), note=str(d.get("note", ""))))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return out


def _save(ws: Path, key: str, entries: list[Entry]) -> None:
    from axon.session.store import atomic_write_text
    entries = entries[-MAX_VERSIONS:]
    while len(entries) > 2 and sum(len(e.text) for e in entries) > MAX_BYTES:
        entries = entries[1:]
    p = _log_path(ws, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"v": e.v, "ts": e.ts, "actor": e.actor, "session": e.session, "action": e.action, "sha": e.sha,
                         "size": e.size, "text": e.text, "redacted": e.redacted, "note": e.note}, ensure_ascii=False)
             for e in entries]
    atomic_write_text(p, "\n".join(lines) + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _scrub(text: str) -> str:
    from axon.features import memory_files as mf
    return mf.redact(text)


def record(ws: Path, key: str, before: str | None, after: str | None, action: str, *, note: str = "") -> Entry | None:
    """Log one write. ``before`` is the text on disk just before (None: it did not exist), ``after`` the text written
    (None: the file was deleted). A change made outside Axon since the last logged version is logged first, so no
    version is ever lost. Never raises: history must not break a write."""
    try:
        entries = _load(ws, key)
        nxt = (entries[-1].v if entries else 0) + 1
        if before is not None and (not entries or entries[-1].sha != sha(before)) and action != "baseline":
            entries.append(Entry(nxt, time.time(), "outside edit", "", "baseline", sha(before), len(before.encode("utf-8", "ignore")),
                                 _scrub(before), False, "found changed since the last logged write"))
            nxt += 1
        text = after or ""
        if after is None:
            action = "deleted"
        e = Entry(nxt, time.time(), _ACTOR.get(), _SESSION.get(), action, sha(text) if after is not None else "",
                  len(text.encode("utf-8", "ignore")), _scrub(text), False, note)
        if entries and after is not None and entries[-1].sha == e.sha and entries[-1].action != "deleted":
            return None                                   # identical to the last version: nothing to log
        entries.append(e)
        _save(ws, key, entries)
        return e
    except Exception:
        return None


def versions(ws: Path, key: str) -> list[Entry]:
    return _load(ws, key)


def get_version(ws: Path, key: str, v: int) -> Entry | None:
    return next((e for e in _load(ws, key) if e.v == v), None)


# ─── naming what the user typed ─────────────────────────────────────────────

def resolve(ws: Path, ref: str) -> tuple[str, str]:
    """(key, label) for ``global|project|local`` (the instruction files) or a note (number, id, ``scope:id``)."""
    from axon.features import memory_files as mf
    r = (ref or "").strip()
    if r.lower() in FILE_KEY_NAMES:
        return file_key(r.lower()), f"{r.lower()} memory ({FILE_KEY_NAMES[r.lower()]})"
    m = mf.find_memory(ws, r)
    if m is None:
        raise ValueError(f"No memory or scope matching '{ref}'. Use global, project, local, or a note number/id from /memory.")
    return note_key(m.scope, m.file), f"{m.name} ({m.scope}:{m.id})"


def all_keys(ws: Path) -> list[str]:
    out: list[str] = []
    for d in {_store_dir(ws, "file-global"), _store_dir(ws, "x")}:
        try:
            out += [p.stem for p in d.glob("*.jsonl")]
        except OSError:
            pass
    return sorted(out)


# ─── writing a version back ─────────────────────────────────────────────────

def _current_text(ws: Path, key: str) -> tuple[Path | None, str | None]:
    from axon.features import memory_hub as hub, memory_files as mf
    if key.startswith("file-"):
        p = hub.file_path(ws, key[5:])
        return p, (p.read_text(encoding="utf-8", errors="replace") if p is not None and p.is_file() else None)
    ns, _, fname = key.partition("/")
    scope = next(s for s, n in _NOTE_NS.items() if n == ns)
    p = mf.scope_dir(ws, scope) / fname
    return p, (p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None)


def apply_text(ws: Path, key: str, text: str, *, action: str, note: str = "", expected_sha: str | None = None) -> str:
    """Make ``text`` the content of the memory ``key`` (file scope or note). Returns the new sha."""
    from axon.features import memory_hub as hub, memory_files as mf
    from axon.session.store import atomic_write_text
    p, cur = _current_text(ws, key)
    assert p is not None
    if expected_sha is not None and sha(cur) != expected_sha:
        raise Conflict(key, expected_sha, sha(cur))
    if key.startswith("file-"):
        hub.write_scope(ws, key[5:], text, _action=action, _note=note)
        return file_sha(p)
    scope = next(s for s, n in _NOTE_NS.items() if n == _NOTE_NS_KEY(key))
    if cur is not None:
        mf.trash_copy(ws, scope, p, "replaced")
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, text)
    mem = mf.parse_memory(p, scope)
    if mem is not None:
        mf.upsert_index(ws, scope, mem.name, mem.file, mem.description)
    record(ws, key, cur, text, action, note=note)
    return file_sha(p)


def _NOTE_NS_KEY(key: str) -> str:
    return key.partition("/")[0]


def restore_version(ws: Path, key: str, v: int, *, expected_sha: str | None = None) -> dict[str, Any]:
    """Write version ``v`` back as the current content (the current text stays in the log as its own version)."""
    e = get_version(ws, key, v)
    if e is None:
        raise ValueError(f"No version {v}. The versions kept are listed by /memory history.")
    if e.action == "deleted":
        raise ValueError(f"Version {v} is the deletion itself; restore the version before it.")
    new = apply_text(ws, key, e.text, action="restored", note=f"restored version {v}", expected_sha=expected_sha)
    return {"ok": True, "key": key, "from": v, "sha": new, "redacted_version": e.redacted}


def redact_version(ws: Path, key: str, v: int, secret: str = "") -> dict[str, Any]:
    """Scrub a secret from a past version (``secret``: the literal value; empty: the automatic secret patterns).
    The entry stays in the log (who, when, hash) marked redacted. When it is also the live text, the file is scrubbed too."""
    entries = _load(ws, key)
    e = next((x for x in entries if x.v == v), None)
    if e is None:
        raise ValueError(f"No version {v}.")
    secret = secret.strip()
    if secret:
        if secret not in e.text:
            raise ValueError(f"That value does not appear in version {v}.")
        new = e.text.replace(secret, PLACEHOLDER)
    else:
        new = _scrub(e.text)
        if new == e.text:
            raise ValueError(f"Nothing secret-looking found in version {v}. Pass the value to remove: /memory redact <note> {v} <value>.")
    count = e.text.count(secret) if secret else 1
    was_live = bool(e.sha) and e.sha == sha(_current_text(ws, key)[1])
    e.text, e.redacted = new, True
    e.note = (e.note + "; " if e.note else "") + f"secret scrubbed ({count} place{'s' if count != 1 else ''}); hash is of the original"
    _save(ws, key, entries)
    live = False
    if was_live:
        with writing_as("redaction"):
            apply_text(ws, key, new, action="redacted", note=f"scrubbed version {v}")
        live = True
    return {"ok": True, "key": key, "v": v, "live_scrubbed": live}
