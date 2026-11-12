"""Dreams: consolidate this project's auto-memory notes using what past sessions actually said.

A dream reads (a bounded, secret-free amount of) the project's earlier session transcripts plus the current notes,
asks the configured model (``small_model`` when set, else the main model; any provider, plain chat messages, no JSON
mode needed) for a cleaned-up notes set, and saves the answer as a NEW set under ``projects/<key>/dreams/<id>/``:

  proposal.json   what was asked, the model, tokens/cost, insights, the base hashes of the notes it read
  notes/*.md      the proposed notes (same file format as real notes)
  original/*.md   copy of the notes as they were when the proposal was made

Nothing in the live notes changes until :func:`apply`. Applying writes the proposed notes through the normal memory
writer (so every change lands in the history log) and moves notes the proposal dropped to the memory trash: the
original is never deleted, and :func:`undo` puts the original set back. One dream runs at a time and can be cancelled.
"""
from __future__ import annotations

import difflib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.features import memory_files as mf
from axon.features import memory_history as mh

MAX_SESSIONS = 15
MAX_TRANSCRIPT_CHARS = 30_000
MAX_NOTES_CHARS = 30_000
MAX_NOTE_BODY = 4_000
MAX_NOTES_OUT = 60
MAX_OUT_TOKENS = 6_000
TIMEOUT_S = 180.0
SOURCE = "dream"

SYSTEM = """You maintain a developer's long-term memory notes for one software project. You are given the current notes and excerpts of recent sessions in that project. Produce a CONSOLIDATED set of notes: remove duplicates, merge related notes, replace notes the sessions show are stale or contradicted, keep still-true facts, and add durable facts or insights that the sessions show but the notes lack (preferences, conventions, commands, decisions, pitfalls). Never invent facts the notes or sessions do not support. Never include secrets, keys, tokens or passwords.

Answer ONLY in this plain-text format, no JSON, no code fences:

<note file="short-kebab-name.md" type="user|feedback|project|reference" name="Short title" description="one line">
note body in markdown
</note>
(repeat for every note in the final set, at most 40)
<insight>one durable insight worth the owner's attention</insight>
<dropped file="old-file.md">why it was merged or removed</dropped>
"""


class Cancelled(Exception):
    pass


@dataclass
class Proposed:
    file: str
    name: str
    type: str
    description: str
    body: str


@dataclass
class Proposal:
    id: str
    created: float
    model: str
    notes: list[Proposed]
    insights: list[str]
    dropped: list[dict[str, str]]
    base: dict[str, str]                 # existing note file -> sha256 when read
    sessions: int = 0
    transcript_chars: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0
    status: str = "proposed"             # proposed | applied | undone

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "created": self.created, "model": self.model, "insights": self.insights, "dropped": self.dropped,
                "base": self.base, "sessions": self.sessions, "transcript_chars": self.transcript_chars, "usage": self.usage,
                "cost": self.cost, "status": self.status,
                "notes": [n.__dict__ for n in self.notes]}


# ─── storage ────────────────────────────────────────────────────────────────

def dreams_dir(ws: Path) -> Path:
    from axon import storage
    return storage.project_dir(Path(ws)) / "dreams"


def _save(ws: Path, p: Proposal, originals: dict[str, str] | None = None) -> None:
    from axon.session.store import atomic_write_text
    d = dreams_dir(ws) / p.id
    (d / "notes").mkdir(parents=True, exist_ok=True)
    for n in p.notes:
        atomic_write_text(d / "notes" / n.file, mf.render_memory(n.name, n.description, n.type, "project", mf.today(), mf.today(), n.body))
    if originals is not None:
        (d / "original").mkdir(parents=True, exist_ok=True)
        for f, text in originals.items():
            atomic_write_text(d / "original" / f, text)
    atomic_write_text(d / "proposal.json", json.dumps(p.to_json(), indent=1, ensure_ascii=False))


def load(ws: Path, did: str = "") -> Proposal | None:
    root = dreams_dir(ws)
    if not root.is_dir():
        return None
    ids = sorted((x.name for x in root.iterdir() if (x / "proposal.json").is_file()), reverse=True)
    if did:
        ids = [i for i in ids if i == did or i.startswith(did)]
    if not ids:
        return None
    try:
        d = json.loads((root / ids[0] / "proposal.json").read_text(encoding="utf-8"))
        return Proposal(id=d["id"], created=d["created"], model=d["model"], notes=[Proposed(**n) for n in d["notes"]],
                        insights=d.get("insights", []), dropped=d.get("dropped", []), base=d.get("base", {}),
                        sessions=d.get("sessions", 0), transcript_chars=d.get("transcript_chars", 0), usage=d.get("usage", {}),
                        cost=d.get("cost", 0.0), status=d.get("status", "proposed"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_proposals(ws: Path) -> list[Proposal]:
    root = dreams_dir(ws)
    out = []
    if root.is_dir():
        for x in sorted(root.iterdir(), reverse=True):
            p = load(ws, x.name) if x.is_dir() else None
            if p:
                out.append(p)
    return out


# ─── reading what the model gets ────────────────────────────────────────────

def read_transcripts(ws: Path, *, max_sessions: int = MAX_SESSIONS, max_chars: int = MAX_TRANSCRIPT_CHARS,
                     skip: str = "") -> tuple[str, int]:
    """Condensed, secret-free text of this project's most recent sessions: (text, sessions used). Bounded."""
    from axon import storage
    from axon.session.store import iter_records
    root = storage.sessions_dir(Path(ws), create=False)
    if not root.is_dir():
        return "", 0
    files = []
    for d in root.iterdir():
        f = d / storage.SESSION_FILE
        if d.is_dir() and d.name != skip and not storage.UNATTACHED == d.name and f.is_file() and "_sub_" not in d.name:
            try:
                files.append((f.stat().st_mtime, d.name, f))
            except OSError:
                pass
    files.sort(reverse=True)
    parts: list[str] = []
    used = n = 0
    for _mt, name, f in files[:max_sessions]:
        lines: list[str] = []
        for rec in iter_records(f):
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            if rec.get("type") == "user_message" and isinstance(data.get("content"), str):
                lines.append("USER: " + " ".join(data["content"].split())[:600])
            elif rec.get("type") == "assistant_turn" and str(data.get("text") or "").strip():
                lines.append("ASSISTANT: " + " ".join(str(data["text"]).split())[:400])
        if not lines:
            continue
        block = mf.redact(f"## session {name[:20]}\n" + "\n".join(lines))
        room = max_chars - used
        if room < 400:
            break
        block = block[:room]
        parts.append(block)
        used += len(block)
        n += 1
    return "\n\n".join(parts), n


def current_notes(ws: Path) -> list[mf.Memory]:
    return mf.list_memories(ws, ("project",))


def _notes_text(notes: list[mf.Memory]) -> str:
    out, used = [], 0
    for m in notes:
        t = f'<note file="{m.file}" type="{m.type}" name="{m.name}" description="{m.description}">\n{m.body.strip()[:MAX_NOTE_BODY]}\n</note>'
        if used + len(t) > MAX_NOTES_CHARS:
            break
        out.append(mf.redact(t))
        used += len(t)
    return "\n".join(out) or "(no notes yet)"


# ─── parsing the model's answer (works without any JSON mode) ───────────────

_NOTE_RX = re.compile(r"<note\b([^>]*)>(.*?)</note>", re.S | re.I)
_ATTR_RX = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_INSIGHT_RX = re.compile(r"<insight>(.*?)</insight>", re.S | re.I)
_DROP_RX = re.compile(r'<dropped\b[^>]*file\s*=\s*"([^"]*)"[^>]*>(.*?)</dropped>', re.S | re.I)


def parse_answer(text: str) -> tuple[list[Proposed], list[str], list[dict[str, str]]]:
    text = re.sub(r"```[a-zA-Z]*", "", text or "")
    notes: list[Proposed] = []
    seen: set[str] = set()
    for attrs, body in _NOTE_RX.findall(text):
        a = {k.lower(): v for k, v in _ATTR_RX.findall(attrs)}
        body = body.strip()
        if not body:
            continue
        name = " ".join(a.get("name", "").split()) or mf.derive_description(body)[:60]
        fname = a.get("file", "").strip()
        if not mf._SAFE_FILE.match(fname):
            fname = mf.slugify(fname.removesuffix(".md") or name) + ".md"
        base, i = fname, 2
        while fname in seen:
            fname = base.removesuffix(".md") + f"-{i}.md"
            i += 1
        seen.add(fname)
        typ = a.get("type", "project") if a.get("type") in mf.TYPES else "project"
        notes.append(Proposed(fname, name[:120], typ, " ".join(a.get("description", "").split())[:200] or mf.derive_description(body),
                              body[:MAX_NOTE_BODY]))
    insights = [" ".join(x.split())[:300] for x in _INSIGHT_RX.findall(text) if x.strip()][:10]
    dropped = [{"file": f.strip(), "reason": " ".join(r.split())[:200]} for f, r in _DROP_RX.findall(text)]
    return notes[:MAX_NOTES_OUT], insights, dropped


# ─── provider (any provider/model the user configured) ──────────────────────

def resolve_provider(agent: Any) -> tuple[Any, str]:
    """``small_model`` through the normal provider registry (its own key and endpoint), else the session's provider."""
    s = agent.settings
    small = str(getattr(s, "small_model", "") or "").strip()
    if small and small != s.model:
        try:
            from axon.providers.registry import provider_for
            return provider_for(small, s.model_copy(update={"model": small}), failover=False), small
        except Exception:
            pass
    if getattr(agent, "provider", None) is None:
        raise RuntimeError("no provider configured (use /provider or /model)")
    return agent.provider, s.model


# ─── the dream ──────────────────────────────────────────────────────────────

def dream(ws: Path, provider: Any, model: str, *, ledger: Any = None, cancel: threading.Event | None = None,
          skip_session: str = "", progress: Callable[[str], None] | None = None) -> Proposal:
    """Read transcripts and notes, ask the model, save the proposal. Raises Cancelled / RuntimeError."""
    ws = Path(ws)
    say = progress or (lambda _m: None)
    cancel = cancel or threading.Event()
    say("reading past sessions")
    transcript, n_sessions = read_transcripts(ws, skip=skip_session)
    notes = current_notes(ws)
    if not transcript and not notes:
        raise RuntimeError("Nothing to consolidate: no earlier sessions and no notes for this project yet.")
    prompt = (f"CURRENT NOTES:\n{_notes_text(notes)}\n\nRECENT SESSIONS (oldest detail trimmed, secrets removed):\n"
              f"{transcript or '(none)'}\n\nWrite the consolidated notes set now.")
    say(f"asking {model}")
    start = time.monotonic()
    stream = provider.stream(model=model, system=[{"type": "text", "text": SYSTEM}],
                             messages=[{"role": "user", "content": prompt}], tools=[], max_tokens=MAX_OUT_TOKENS,
                             effort="low", thinking=False)
    try:
        for _ in stream:
            if cancel.is_set():
                raise Cancelled("cancelled")
            if time.monotonic() - start > TIMEOUT_S:
                raise RuntimeError(f"the model took longer than {TIMEOUT_S:.0f}s")
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if cancel.is_set():
        raise Cancelled("cancelled")
    turn = provider.finalize()
    usage = getattr(turn, "usage", None)
    cost = 0.0
    if usage is not None:
        try:
            from axon.agent.state import record_usage
            c = record_usage(ledger, model, usage, source=SOURCE)
            if c is None:
                from axon.session.store import estimate_cost
                c = estimate_cost(model, usage)
            cost = float(getattr(c, "cost", c) or 0.0)
        except Exception:
            cost = 0.0
    new, insights, dropped = parse_answer(str(getattr(turn, "text", "") or ""))
    if not new:
        raise RuntimeError("The model's answer had no usable notes (it must reply with <note ...> blocks). Nothing was changed; try again or pick another model.")
    p = Proposal(id=time.strftime("%Y%m%d-%H%M%S"), created=time.time(), model=model, notes=new, insights=insights, dropped=dropped,
                 base={m.file: mh.file_sha(m.path) for m in notes}, sessions=n_sessions, transcript_chars=len(transcript),
                 usage={"input": int(getattr(usage, "input", 0) or 0), "output": int(getattr(usage, "output", 0) or 0)}, cost=cost)
    originals = {m.file: m.path.read_text(encoding="utf-8", errors="replace") for m in notes}
    _save(ws, p, originals)
    say("proposal saved")
    return p


# ─── diff, apply, undo ──────────────────────────────────────────────────────

def diff(ws: Path, p: Proposal, max_lines: int = 6) -> list[dict[str, Any]]:
    """Per file: added | changed | unchanged | removed, with a few changed lines. Compares with the live notes."""
    cur = {m.file: m for m in current_notes(ws)}
    out: list[dict[str, Any]] = []
    new_files = set()
    for n in p.notes:
        new_files.add(n.file)
        m = cur.get(n.file)
        if m is None:
            out.append({"file": n.file, "name": n.name, "kind": "added", "lines": [f"+ {ln}" for ln in n.body.splitlines() if ln.strip()][:max_lines]})
        elif m.body.strip() == n.body.strip() and m.name == n.name and m.description == n.description:
            out.append({"file": n.file, "name": n.name, "kind": "unchanged", "lines": []})
        else:
            ch = [ln for ln in difflib.unified_diff(m.body.strip().splitlines(), n.body.strip().splitlines(), lineterm="", n=0)
                  if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
            out.append({"file": n.file, "name": n.name, "kind": "changed", "lines": ch[:max_lines], "more": max(0, len(ch) - max_lines)})
    why = {d["file"]: d["reason"] for d in p.dropped}
    for f, m in cur.items():
        if f not in new_files:
            out.append({"file": f, "name": m.name, "kind": "removed", "lines": [why.get(f, "merged into another note or no longer true")]})
    return out


def summary_line(d: list[dict[str, Any]]) -> str:
    c = {k: sum(1 for x in d if x["kind"] == k) for k in ("added", "changed", "removed", "unchanged")}
    return f"{c['added']} added, {c['changed']} changed, {c['removed']} removed, {c['unchanged']} unchanged"


def apply(ws: Path, p: Proposal) -> dict[str, Any]:
    """Make the proposed set the live notes. Refuses (changing nothing) if a note changed since the proposal was made."""
    ws = Path(ws)
    if p.status == "applied":
        raise ValueError(f"Dream {p.id} is already applied; /dream --undo puts the original notes back.")
    cur = {m.file: m for m in current_notes(ws)}
    changed = [f for f in set(cur) | set(p.base)
               if (mh.file_sha(cur[f].path) if f in cur else "") != p.base.get(f, "")]
    if changed:
        raise ValueError("These notes changed since the dream read them: " + ", ".join(sorted(changed)) +
                         ". Nothing was applied; run /dream again so it works from the current notes.")
    wrote = removed = 0
    keep = {n.file for n in p.notes}
    with mh.writing_as(f"dream {p.id}"):
        for n in p.notes:
            m = cur.get(n.file)
            if m is not None and m.body.strip() == n.body.strip() and m.name == n.name and m.description == n.description:
                continue
            mf.write_memory(ws, "project", name=n.name, body=n.body, description=n.description, type_=n.type, file=n.file,
                            expected_sha=p.base.get(n.file) if m is not None else None)
            wrote += 1
        for f, m in cur.items():
            if f not in keep:
                mf.delete_memory(ws, m)
                removed += 1
    p.status = "applied"
    _save(ws, p)
    return {"ok": True, "id": p.id, "written": wrote, "removed": removed, "kept_original": str(dreams_dir(ws) / p.id / "original")}


def undo(ws: Path, p: Proposal) -> dict[str, Any]:
    """Put the original notes back (files the dream created are trashed, not deleted)."""
    ws = Path(ws)
    if p.status != "applied":
        raise ValueError("That dream has not been applied; nothing to undo.")
    orig = dreams_dir(ws) / p.id / "original"
    files = {f.name: f.read_text(encoding="utf-8", errors="replace") for f in orig.glob("*.md")} if orig.is_dir() else {}
    with mh.writing_as(f"dream {p.id} (undo)"):
        for f, text in files.items():
            mh.apply_text(ws, mh.note_key("project", f), text, action="restored", note=f"original set of dream {p.id}")
        for m in current_notes(ws):
            if m.file not in files:
                mf.delete_memory(ws, m)
    p.status = "undone"
    _save(ws, p)
    return {"ok": True, "id": p.id, "restored": len(files)}


# ─── one background job at a time ───────────────────────────────────────────

class Job:
    def __init__(self, ws: Path, provider: Any, model: str, ledger: Any, skip_session: str) -> None:
        self.ws, self.provider, self.model, self.ledger, self.skip = Path(ws), provider, model, ledger, skip_session
        self.cancel = threading.Event()
        self.state = "running"                 # running | done | failed | cancelled
        self.message = "starting"
        self.error = ""
        self.proposal: Proposal | None = None
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True, name="axon-dream")

    def _run(self) -> None:
        try:
            self.proposal = dream(self.ws, self.provider, self.model, ledger=self.ledger, cancel=self.cancel,
                                  skip_session=self.skip, progress=lambda m: setattr(self, "message", m))
            self.state = "done"
        except Cancelled:
            self.state, self.message = "cancelled", "cancelled"
        except Exception as e:
            self.state, self.error = "failed", str(e) or type(e).__name__

    def info(self) -> dict[str, Any]:
        return {"state": self.state, "message": self.message, "error": self.error, "elapsed": round(time.time() - self.started, 1),
                "model": self.model, "id": self.proposal.id if self.proposal else ""}


_JOB: Job | None = None
_LOCK = threading.Lock()


def start(ws: Path, provider: Any, model: str, ledger: Any = None, skip_session: str = "") -> Job:
    """Start the dream in the background. Raises RuntimeError when one is already running."""
    global _JOB
    with _LOCK:
        if _JOB is not None and _JOB.state == "running":
            raise RuntimeError("A dream is already running. /dream --cancel stops it.")
        _JOB = Job(ws, provider, model, ledger, skip_session)
        _JOB.thread.start()
        return _JOB


def current() -> Job | None:
    return _JOB


def cancel() -> bool:
    j = _JOB
    if j is not None and j.state == "running":
        j.cancel.set()
        return True
    return False
