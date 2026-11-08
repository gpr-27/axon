"""Live agent plumbing for the dashboard: event hub, chat runtimes (own agent or the REPL's agent), slash
command runner, shell jobs and attachments.

A *runtime* wraps one agent. Every event the agent emits goes through the same ``on_event`` stream the terminal
renderer uses (the original callback is kept and called first), is turned into a JSON dict and appended to an
:class:`EventHub` ring buffer that the page follows over SSE. The hub numbers events, so a page that lost its
connection resumes from the last number it saw without losing any output.
"""
from __future__ import annotations

import contextlib
import ctypes
import io
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from axon.features.dash_ansi import ansi_to_html, strip_ansi
from axon.features.dash_common import DashError

APPROVAL_TIMEOUT_S = 600
MAX_EVENT_TEXT = 100_000
APPROVAL_CHOICES = ("once", "always", "rule", "deny")


# ─── events ─────────────────────────────────────────────────────────────────

class EventHub:
    """Numbered ring buffer with blocking reads (one per runtime / shell job)."""

    def __init__(self, cap: int = 6000) -> None:
        self.seq = 0
        self.buf: deque[tuple[int, dict[str, Any]]] = deque(maxlen=cap)
        self.cond = threading.Condition()
        self.closed = False
        self.sink: Callable[[str, dict[str, Any]], None] | None = None

    def emit(self, kind: str, data: dict[str, Any] | None = None) -> int:
        d = dict(data or {})
        d["type"] = kind
        d.setdefault("ts", time.time())
        with self.cond:
            self.seq += 1
            self.buf.append((self.seq, d))
            self.cond.notify_all()
            n = self.seq
        if self.sink is not None:
            try:
                self.sink(kind, d)
            except Exception:
                pass
        return n

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def read(self, since: int, timeout: float = 15.0) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
        """(events after *since*, gap). ``gap`` means older events were dropped from the buffer."""
        with self.cond:
            if self.seq <= since and not self.closed:
                self.cond.wait(timeout)
            if not self.buf:
                return [], False
            first = self.buf[0][0]
            gap = since + 1 < first and since < self.seq
            return [e for e in self.buf if e[0] > since], gap


class EventLog:
    """One ordered, replayable record per session: ``web-events.jsonl`` beside the transcript.

    Each line is {seq, ts, type, parent, data}. ``parent`` names the step an event belongs to (``call:<n>`` for
    a model call, the tool id for a tool result). Large bodies are written once under ``web-events-blobs/`` and
    the event keeps a reference ({"$blob", "len", "head"}), so nothing is duplicated and nothing is lost.
    Everything passes the secret redactor before it reaches the disk."""
    BLOB_OVER = 12_000
    BODY_KEYS = ("content", "input", "result", "text", "html", "summary", "output", "prompt")

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.path = self.folder / "web-events.jsonl"
        self.blobs = self.folder / "web-events-blobs"
        self.lock = threading.Lock()
        self.seq = self._last_seq()
        self._pending: dict[str, Any] | None = None
        self.parent = ""

    def _last_seq(self) -> int:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace").splitlines()
            import json
            for line in reversed(tail):
                try:
                    return int(json.loads(line).get("seq", 0))
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            pass
        return 0

    @staticmethod
    def _clean(value: Any) -> Any:
        from axon.features.dashboard import redact_text
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {k: EventLog._clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [EventLog._clean(v) for v in value]
        return value

    def _spill(self, data: dict[str, Any]) -> dict[str, Any]:
        import hashlib
        out = {}
        for k, v in data.items():
            if k in self.BODY_KEYS and isinstance(v, str) and len(v) > self.BLOB_OVER:
                sha = hashlib.sha256(v.encode("utf-8", "replace")).hexdigest()[:20]
                try:
                    self.blobs.mkdir(parents=True, exist_ok=True)
                    target = self.blobs / f"{sha}.txt"
                    if not target.exists():
                        target.write_text(v, encoding="utf-8")
                    out[k] = {"$blob": sha, "len": len(v), "head": v[:1500]}
                    continue
                except OSError:
                    pass
            out[k] = v
        return out

    def _write(self, rec: dict[str, Any]) -> None:
        import json
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def flush(self) -> None:
        with self.lock:
            if self._pending is not None:
                self._write(self._pending)
                self._pending = None

    def append(self, etype: str, data: dict[str, Any], ts: float | None = None) -> None:
        with self.lock:
            if etype in ("text_delta", "thinking_delta"):      # one line per burst of text, not per fragment
                p = self._pending
                if p is not None and p["type"] == etype and len(p["data"].get("text", "")) < 8000:
                    p["data"]["text"] += str(data.get("text", ""))
                    return
                if p is not None:
                    self._write(p)
                self.seq += 1
                self._pending = {"seq": self.seq, "ts": ts or time.time(), "type": etype, "parent": self.parent,
                                 "data": {"text": str(data.get("text", ""))}}
                return
            if self._pending is not None:
                self._write(self._pending)
                self._pending = None
            if etype == "llm_call_start":
                self.parent = f"call:{data.get('iteration', '')}"
            parent = data.get("tool_use_id") or data.get("id") if etype in ("tool_result", "tool_timing", "permission") else self.parent
            self.seq += 1
            body = {k: v for k, v in data.items() if k not in ("type", "ts")}
            rec = {"seq": self.seq, "ts": ts or data.get("ts") or time.time(), "type": etype, "parent": parent or "",
                   "data": self._spill(self._clean(body))}
            self._write(rec)
            if etype in ("turn_end", "interrupted", "error", "idle"):
                self.parent = ""

    def read(self, after: int = 0, limit: int = 4000) -> list[dict[str, Any]]:
        import json
        self.flush()
        out: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if int(rec.get("seq", 0)) > after:
                        out.append(rec)
                        if len(out) >= limit:
                            break
        except OSError:
            pass
        return out

    def blob(self, sha: str, offset: int = 0, limit: int = 400_000) -> dict[str, Any]:
        import re as _re
        if not _re.fullmatch(r"[0-9a-f]{20}", sha or ""):
            raise DashError("invalid blob id", 400)
        try:
            text = (self.blobs / f"{sha}.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise DashError("blob not found", 404) from None
        chunk = text[offset:offset + limit]
        return {"text": chunk, "offset": offset, "next": offset + len(chunk), "total": len(text), "done": offset + len(chunk) >= len(text)}


def _cap(text: Any, limit: int = MAX_EVENT_TEXT) -> tuple[str, int]:
    s = text if isinstance(text, str) else str(text if text is not None else "")
    return (s[:limit], len(s)) if len(s) > limit else (s, len(s))


def web_event(ev: Any) -> dict[str, Any] | None:
    """The SDK's JSON form of an agent event, with the extra fields the page shows."""
    from axon.features.sdk import _jsonable, event_to_dict
    d = event_to_dict(ev)
    if d is None:
        return None
    cls = type(ev).__name__
    if cls == "ToolExecutionResult":
        d["content"], total = _cap(d.get("content"))
        if total > MAX_EVENT_TEXT:
            d["truncated_from"] = total
        d["changes"] = _jsonable(getattr(ev, "changes", ()))
    elif cls == "ToolExecutionStart":
        d["rationale"] = getattr(ev, "rationale", "")
        d["parse_error"] = getattr(ev, "parse_error", "")
    elif cls == "CompactionEvent":
        d["summary"] = str(d.get("summary") or "")[:4000]
    return d


# ─── runtimes ───────────────────────────────────────────────────────────────

class _Slot:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.choice = "deny"
        self.value: Any = None


class Runtime:
    """One agent shown in the page. ``external`` runtimes are the REPL's own agent (mirrored, never rebuilt)."""

    def __init__(self, key: str, agent: Any, workspace: Path, *, external: bool = False,
                 close: Callable[[], None] | None = None) -> None:
        self.key = key
        self.agent = agent
        self.workspace = Path(workspace)
        self.external = external
        self.hub = EventHub()
        self.pending: dict[str, _Slot] = {}
        self.lock = threading.Lock()
        self.turn_active = False          # a web-started turn loop is running
        self.cmd_thread: threading.Thread | None = None
        self.cmd_job: Any = None
        self.context: dict[str, Any] = {}
        self.last_result: dict[str, Any] = {}
        self._orig: Callable[[Any], None] | None = None
        self._started = False
        self._close = close
        self.attached = False
        self._log: EventLog | None = None
        self._log_sid = ""
        self._next_display: str | None = None
        self._run_turn_orig: Any = None
        self.hub.sink = self._to_log

    # ── the replayable record ──
    def event_log(self) -> EventLog | None:
        sid = self.session_id
        if not sid:
            return None
        if self._log is None or self._log_sid != sid:
            if self._log is not None:
                self._log.flush()
            try:
                folder = Path(self.agent.session._file(sid)).parent
            except Exception:
                return None
            self._log, self._log_sid = EventLog(folder), sid
        return self._log

    def _to_log(self, kind: str, d: dict[str, Any]) -> None:
        log = self.event_log()
        if log is not None:
            log.append(kind, {k: v for k, v in d.items() if k not in ("type",)}, d.get("ts"))

    def record(self, kind: str, data: dict[str, Any]) -> None:
        """Add an event to the record without sending it to the page (shell commands run from the Shell panel)."""
        log = self.event_log()
        if log is not None:
            log.append(kind, data)

    # ── wiring ──
    def attach(self) -> None:
        if self.attached:
            return
        self._orig = getattr(self.agent, "on_event", None)
        self.agent.on_event = self._tee
        self.attached = True
        self.hook_turns()

    def hook_turns(self) -> None:
        """Wrap ``agent.run_turn`` so every turn, whoever starts it (the terminal's prompt, this page, a queued
        message), is announced and closed the same way: user, turn_start, ..., turn_end, idle."""
        if getattr(self, "_run_turn_orig", None) is not None:
            return
        orig = getattr(self.agent, "run_turn", None)
        if orig is None:
            return
        self._run_turn_orig = orig

        def run_turn(user_input: str) -> Any:
            shown = self._next_display if self._next_display is not None else user_input
            self._next_display = None
            self.hub.emit("user", {"text": shown[:20000], "chars": len(shown)})
            self.hub.emit("turn_start", {})
            t0 = time.time()
            try:
                res = orig(user_input)
            except KeyboardInterrupt:
                self.last_result = {"stop": "interrupted"}
                self.hub.emit("interrupted", {"message": "Stopped."})
                raise
            except BaseException as e:
                self.last_result = {"stop": "error"}
                self.hub.emit("error", {"message": f"{type(e).__name__}: {e}"})
                raise
            else:
                try:
                    from axon.features.sdk import result_to_dict
                    rd = result_to_dict(self.agent, res, time.time() - t0)
                    rd["result"], _ = _cap(rd.get("result"), 200_000)
                    self.last_result = {"stop": rd.get("stop_reason"), "cost": rd.get("cost_usd"), "is_error": rd.get("is_error")}
                    self.hub.emit("turn_end", rd)
                except Exception:
                    self.hub.emit("turn_end", {"stop_reason": "end_turn"})
                return res
            finally:
                self.hub.emit("idle", {"queue": len(getattr(self.agent, "message_queue", []) or [])})

        self.agent.run_turn = run_turn

    def unhook_turns(self) -> None:
        orig = getattr(self, "_run_turn_orig", None)
        if orig is not None:
            try:
                del self.agent.run_turn          # back to the class's method
            except AttributeError:
                pass
            self._run_turn_orig = None

    def detach(self) -> None:
        if self.attached and getattr(self.agent, "on_event", None) == self._tee:
            self.agent.on_event = self._orig
        self.unhook_turns()
        self.attached = False

    def _tee(self, ev: Any) -> None:
        if self._orig is not None:
            self._orig(ev)                  # the terminal renderer first; its KeyboardInterrupt (cancel) must propagate
        try:
            d = web_event(ev)
        except Exception:
            d = None
        if d is None:
            return
        t = d["type"]
        if t in ("llm_call_end", "cache_status"):
            used = d.get("context_used") or d.get("context_tokens")
            win = d.get("context_window")
            if used and win:
                self.context = {"used": int(used), "window": int(win), "pct": round(100 * int(used) / int(win), 1)}
        self.hub.emit(t, d)

    @property
    def session_id(self) -> str:
        try:
            return str(self.agent.session.active_session_id or "")
        except Exception:
            return ""

    @property
    def busy(self) -> bool:
        return bool(getattr(self.agent, "busy", False)) or self.turn_active or (
            self.cmd_thread is not None and self.cmd_thread.is_alive())

    # ── state for the page ──
    def _recorded_cost(self) -> float:
        """What the session's transcript (and its sub-agents') recorded as spent: the same figure the Sessions page shows."""
        try:
            store = self.agent.session
            sid = str(store.active_session_id).rsplit("_sub_", 1)[0]
            files = [store._file(sid), *store._subagent_files(sid)]
            sig = tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files if f.exists())
            cached = getattr(self, "_rec_cost", None)
            if cached and cached[0] == sig:
                return cached[1]
            from axon.session.store import iter_records
            total = 0.0
            for f in files:
                for rec in iter_records(f):
                    d = rec.get("data")
                    if rec.get("type") == "assistant_turn" and isinstance(d, dict) and isinstance(d.get("cost"), (int, float)):
                        total += float(d["cost"])
            self._rec_cost = (sig, total)
            return total
        except Exception:
            return 0.0

    def state(self) -> dict[str, Any]:
        a = self.agent
        s = a.settings
        todos: list[dict[str, Any]] = []
        try:
            todos = [{"content": t.content, "status": t.status, "active": t.active_form} for t in a.todos.items]
        except Exception:
            pass
        queue: list[dict[str, Any]] = []
        try:
            queue = [{"id": i.id, "text": i.text[:400], "steer": bool(getattr(i, "steer", False))} for i in a.message_queue.items]
        except Exception:
            pass
        try:
            cost = float(a.get_combined_cost())
        except Exception:
            cost = 0.0
        cost = max(cost, self._recorded_cost())      # a resumed session's ledger is empty or unpriced; the transcript has the truth
        bg = []
        try:
            from axon.tools import shell as shell_mod
            bg = [{"id": b.id, "command": b.command[:300], "status": b.status, "pid": b.proc.pid, "started": b.started}
                  for b in shell_mod.BACKGROUND.all()]
        except Exception:
            pass
        return {"key": self.key, "session": self.session_id, "busy": self.busy, "external": self.external,
                "mode": s.mode, "model": s.model, "effort": getattr(s, "effort", ""), "workspace": str(self.workspace),
                "queue": queue, "todos": todos, "context": self.context, "cost": cost, "seq": self.hub.seq,
                "pending": [k for k in self.pending], "background": bg,
                "thinking": bool(getattr(s, "thinking", False)), "last": self.last_result,
                "sandbox": bool(getattr(s, "sandbox_enabled", False)),
                "suggestion": str(getattr(a, "next_suggestion", "") or "")}

    # ── approvals ──
    def approver(self, tool: str, args: dict[str, Any], decision: Any) -> str:
        aid = secrets.token_hex(6)
        slot = _Slot()
        self.pending[aid] = slot
        import json
        shown, total = _cap(json.dumps(args, ensure_ascii=False, default=str), 6000)
        self.hub.emit("approval_request", {"id": aid, "tool": tool, "input": shown, "truncated": total > 6000,
                                           "reason": str(getattr(decision, "reason", "") or ""),
                                           "args": _small_args(args)})
        ok = slot.event.wait(APPROVAL_TIMEOUT_S)
        self.pending.pop(aid, None)
        choice = slot.choice if ok else "deny"
        self.hub.emit("approval_resolved", {"id": aid, "choice": choice, "tool": tool})
        return choice

    def answer(self, aid: str, choice: str) -> None:
        if choice not in APPROVAL_CHOICES:
            raise DashError("choice must be once, always, rule or deny")
        slot = self.pending.get(aid)
        if slot is None:
            raise DashError("no pending approval with that id", 404)
        slot.choice = choice
        slot.event.set()

    def ask(self, q: dict[str, Any], index: int, total: int) -> list[str] | None:
        """AskUserQuestion for a page-driven session: the question card in the page answers it."""
        qid = secrets.token_hex(6)
        slot = _Slot()
        self.pending[qid] = slot
        self.hub.emit("question_request", {"id": qid, "question": q.get("question", ""), "header": q.get("header", ""),
                                           "options": q.get("options", []), "multi": bool(q.get("multiSelect")),
                                           "index": index, "total": total})
        ok = slot.event.wait(APPROVAL_TIMEOUT_S)
        self.pending.pop(qid, None)
        picked = slot.value if ok else None
        self.hub.emit("question_resolved", {"id": qid, "labels": picked})
        return picked if isinstance(picked, list) else None

    def answer_question(self, qid: str, labels: list[str] | None) -> None:
        slot = self.pending.get(qid)
        if slot is None:
            raise DashError("no pending question with that id", 404)
        slot.value = labels
        slot.event.set()

    def elicit(self, server: str, params: dict[str, Any]) -> dict[str, Any]:
        """An MCP server asks for input mid-task: the page shows a form (or a link to open) and waits for the answer."""
        eid = secrets.token_hex(6)
        slot = _Slot()
        self.pending[eid] = slot
        schema = params.get("requestedSchema") if isinstance(params.get("requestedSchema"), dict) else {}
        required = set(schema.get("required") or [])
        fields = []
        for key, f in list((schema.get("properties") or {}).items())[:30]:
            f = f if isinstance(f, dict) else {}
            fields.append({"key": str(key), "label": str(f.get("title") or key)[:120], "type": str(f.get("type") or "string"),
                           "description": str(f.get("description") or "")[:300], "enum": [str(e) for e in (f.get("enum") or [])][:50],
                           "default": f.get("default"), "required": key in required})
        url = str(params.get("url") or "")
        self.hub.emit("elicit_request", {"id": eid, "server": server, "message": str(params.get("message") or "")[:2000],
                                         "mode": str(params.get("mode") or "form"), "url": url if url.startswith(("http://", "https://")) else "",
                                         "fields": fields})
        ok = slot.event.wait(APPROVAL_TIMEOUT_S)
        self.pending.pop(eid, None)
        val = slot.value if ok and isinstance(slot.value, dict) else {"action": "cancel"}
        action = val.get("action") if val.get("action") in ("accept", "decline", "cancel") else "cancel"
        self.hub.emit("elicit_resolved", {"id": eid, "action": action})
        out: dict[str, Any] = {"action": action}
        if action == "accept" and fields and isinstance(val.get("content"), dict):
            out["content"] = {k: v for k, v in val["content"].items() if k in {f["key"] for f in fields}}
        return out

    def answer_elicit(self, eid: str, action: str, content: Any) -> None:
        slot = self.pending.get(eid)
        if slot is None:
            raise DashError("no pending request with that id", 404)
        slot.value = {"action": action, "content": content}
        slot.event.set()

    def install_web_tools(self) -> None:
        """Interactive tools ask through the page instead of the terminal's pickers."""
        try:
            tool = self.agent.registry.get("AskUserQuestion")
            tool._ask = self.ask
            tool._interactive = lambda: True
        except Exception:
            pass

    # ── turns ──
    def send(self, line: str, display: str | None = None, steer: bool = True) -> dict[str, Any]:
        """Start a turn, or queue the message when one is running (like typing during a turn in the terminal)."""
        agent = self.agent
        with self.lock:
            if self.busy:
                from axon.ui.in_flight import push_queued
                item = push_queued(agent.message_queue, line, steer=steer)
                self.hub.emit("queued", {"id": getattr(item, "id", 0), "text": (display or line)[:2000], "steer": steer})
                return {"queued": True, "id": getattr(item, "id", 0), "steer": steer}
            self.turn_active = True
        threading.Thread(target=self._turn_loop, args=(line, display), name="axon-dash-turn", daemon=True).start()
        return {"queued": False}

    def _turn_loop(self, line: str, display: str | None) -> None:
        agent = self.agent
        swapped = False
        prev_approval = agent.on_approval
        if self.external or prev_approval is None or prev_approval != self.approver:
            agent.on_approval = self.approver          # the page answers for turns it started
            swapped = True
        if not self._started:
            self._started = True
            try:
                agent.start_session("startup")
            except Exception:
                pass
        try:
            text: str | None = line
            shown = display
            while text is not None:
                self._next_display = shown
                try:
                    res = agent.run_turn_threaded(text)
                except KeyboardInterrupt:
                    pass                                   # the wrapper already announced it
                except Exception:
                    pass                                   # ... and this
                else:
                    if res is None:
                        self.hub.emit("note", {"message": "The turn moved to the background."})
                text = shown = None
                if not self.external:                      # the REPL drains its own queue
                    nxt = agent.message_queue.pop()
                    if nxt is not None and nxt.text.strip():
                        text = nxt.text
        finally:
            if swapped:
                agent.on_approval = prev_approval
            self.turn_active = False

    def cancel(self) -> bool:
        """Interrupt the running turn (or the slash command that is running a turn). Pending approvals are denied."""
        for slot in list(self.pending.values()):
            slot.choice = "deny"
            slot.value = None
            slot.event.set()
        agent = self.agent
        if getattr(agent, "busy", False):
            try:
                return bool(agent.cancel_current_turn())
            except Exception:
                return False
        job = self.cmd_job
        if job is not None and not job.done.is_set():
            runner = getattr(self, "runner", None)
            if runner is not None:
                return runner.abort(self, job, "Stopped.")
            return job.finish({"ok": False, "html": "", "message": "Stopped.", "line": job.line})
        return False

    def close(self) -> None:
        self.cancel()
        if self._log is not None:
            self._log.flush()
        self.detach()
        self.hub.close()
        if self._started and not self.external:
            try:
                self.agent.end_session("exit")
            except Exception:
                pass
        if self._close:
            self._close()


def _small_args(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in (args or {}).items():
        out[str(k)] = v if isinstance(v, (int, float, bool)) or v is None else str(v)[:(20000 if k in ("plan", "command") else 600)]
    return out


# ─── slash commands ─────────────────────────────────────────────────────────

# Commands that end the process or can only work with a real terminal. Everything else runs in the page.
TERMINAL_ONLY: dict[str, str] = {
    "exit": "Ends the terminal session. Close the terminal (or press Ctrl+C there); the page keeps running.",
    "quit": "Ends the terminal session. Close the terminal (or press Ctrl+C there); the page keeps running.",
    "paste": "Not needed here: paste straight into the message box (Ctrl+V or Cmd+V). /paste is for the terminal prompt.",
    "terminal-setup": "This sets up key bindings for the terminal prompt, so it only applies in a terminal. The page needs no setup.",
}
# Commands that run here but only change the terminal prompt: the answer says so after the output.
TERMINAL_NOTES: dict[str, str] = {
    "vim": "This changes the terminal prompt only; the message box in the page is not affected.",
    "statusline": "This is the terminal's status line; the page shows its own status bar.",
    "statusbar": "This is the terminal's status bar; the page shows its own status bar.",
    "color": "This colours the terminal prompt only.",
    "keybindings": "These are the terminal's shortcuts; the page has its own (press ? for them).",
}


class _PageOut(io.StringIO):
    """Captured command output. It reports UTF-8 (a bare StringIO reports none, so the terminal screens fell back to
    ASCII and drew '?' for every dot and '|' for bars)."""
    encoding = "utf-8"
_RUN_LOCK = threading.Lock()


@contextlib.contextmanager
def _empty_stdin() -> Any:
    """stdin at end-of-file: a command that asks a question gets EOF instead of blocking the server."""
    import sys
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        yield
    finally:
        sys.stdin = old


class CommandJob:
    def __init__(self, line: str) -> None:
        self.id = secrets.token_hex(6)
        self.line = line
        self.started = time.time()
        self.done = threading.Event()
        self.result: dict[str, Any] = {}
        self.thread: threading.Thread | None = None
        self._gate = threading.Lock()
        self._holds_lock = False
        self._restore: Callable[[], None] | None = None

    def finish(self, result: dict[str, Any]) -> bool:
        """Publish the answer once. Whoever gets here first (the command, the timeout, Stop) wins; the command
        lock is freed here, so no path can leave it held."""
        with self._gate:
            if self.done.is_set():
                return False
            self.result = result
            if self._restore is not None:
                self._restore()
                self._restore = None
            if self._holds_lock:
                self._holds_lock = False
                _RUN_LOCK.release()
            self.done.set()
            return True


_GIT_COMMANDS = ("diff", "git", "commit", "pr")
_USAGE_RE = re.compile(r"^\s*Usage:\s*(.+)", re.S)
COMMAND_TIMEOUT_S = 300


class CommandRunner:
    """Runs slash commands with stdout/stderr captured (and stdin empty, so a picker that wants a terminal
    gets end-of-file instead of hanging the server).

    A command that starts a model turn does not run it here: the turn is taken from the command and started as an
    ordinary chat turn, so it streams in Chat, can be stopped there, and never holds the command lock."""

    def __init__(self, srv: Any = None) -> None:
        self.jobs: dict[str, CommandJob] = {}
        self.srv = srv
        self.timeout = COMMAND_TIMEOUT_S

    def list_commands(self, runtime: Runtime | None) -> list[dict[str, Any]]:
        from axon.commands.registry import list_command_names, visible_commands
        out = []
        seen = set()
        for c in sorted(visible_commands(), key=lambda c: c.name):
            seen.add(c.name)
            term = TERMINAL_ONLY.get(c.name)
            out.append({"name": f"/{c.name}", "aliases": [f"/{a}" for a in c.aliases], "category": c.category,
                        "description": c.description, "usage": c.usage, "kind": "builtin",
                        "terminal_only": term or ""})
        try:
            ws = runtime.workspace if runtime else None
            for name, desc in list_command_names(ws, runtime.agent if runtime else None):
                if name.lstrip("/") not in seen:
                    out.append({"name": name, "aliases": [], "category": "Custom and skills", "description": desc,
                                "usage": name, "kind": "extension", "terminal_only": ""})
        except Exception:
            pass
        return out

    # ── helpers ──
    def _hide_token(self, text: str) -> str:
        tok = getattr(self.srv, "token", "") or ""
        if tok:
            text = text.replace(tok, "[hidden]")
        return re.sub(r"([?&]t=)[A-Za-z0-9_\-]{12,}", r"\1[hidden]", text)

    @staticmethod
    def _quick(job: CommandJob, result: dict[str, Any]) -> CommandJob:
        job.finish({"html": "", "ms": 0, "line": job.line, **result})
        return job

    def _in_git_repo(self, ws: Path) -> bool:
        try:
            from axon.features.gitops import git_available, repo_root
            return bool(git_available() and repo_root(ws) is not None)
        except Exception:
            return True

    def _switched(self, rt: Runtime, before: str, line: str) -> dict[str, Any]:
        """The command left the runtime on another session (/clear, /new, /branch, /resume). The server finds the
        runtime by its current session id as well as its first one, so the page's event stream follows."""
        after = rt.session_id
        if not after or after == before:
            return {}
        return {"from": before, "to": after}

    def start(self, rt: Runtime, line: str, cards: bool = False) -> CommandJob:
        line = line.strip()
        if not line.startswith(("/", "#")):
            raise DashError("a slash command starts with /")
        name = (line[1:].split(None, 1) or [""])[0].lower() if line.startswith("/") else ""
        from axon.commands.registry import resolve_builtin
        cmd = resolve_builtin(name) if name else None
        key = cmd.name if cmd else name
        job = CommandJob(line)
        self.jobs[job.id] = job
        while len(self.jobs) > 40:
            self.jobs.pop(next(iter(self.jobs)))
        rest = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        if key in TERMINAL_ONLY:
            return self._quick(job, {"ok": False, "message": TERMINAL_ONLY[key], "terminal_only": True})
        from axon.features import dash_cmdcards                # a structured card (or a confirm card) instead of terminal text
        card = dash_cmdcards.try_card(self.srv, rt, line) if cards and self.srv is not None else None
        if card is not None:
            return self._quick(job, {"ok": True, "card": card})
        if key in ("provider", "providers") or (key == "model" and not rest):
            return self._quick(job, {"ok": True, "open": "#/settings/providers",
                                     "message": f"{('/' + key)} opens Settings > Providers: that is where you choose a provider and model."})
        if key in ("resume", "delete") and not rest:
            # In the terminal these open a picker. Here that picker is the Sessions page; with no keyboard to answer a
            # prompt the command would otherwise take its default (resume the newest session / delete nothing).
            verb = "open" if key == "resume" else "delete"
            return self._quick(job, {"ok": True, "open": "#/sessions",
                                     "message": f"Choose the session to {verb} in Sessions. You can also give its id: /{key} <id>."})
        if key == "setup":
            return self._quick(job, {"ok": True, "open": "#/settings/providers",
                                     "message": "Setup is a terminal wizard. In the page, Settings > Providers does the same: choose a provider, add its key and pick a model."})
        if key in ("open", "dash", "web"):
            # This page IS the app: starting another server (on the default port, with a fresh link) is never wanted.
            port = getattr(self.srv, "port", 0)
            local = f"http://127.0.0.1:{port}" if port else ""
            if rest.split()[:1] == ["stop"]:
                return self._quick(job, {"ok": False, "message": "This page is the app itself, so it can't be stopped from here. Close it from the terminal that started it."})
            return self._quick(job, {"ok": True, "message": "Axon is already open: you are looking at it." + (f" Its local address is {local}." if local else ""),
                                     "copy": local})
        if key in _GIT_COMMANDS and not (key == "diff" and re.match(r"T\d", rest)) and not self._in_git_repo(rt.workspace):
            return self._quick(job, {"ok": False, "message": "This folder isn't a git repository.",
                                     "action": {"label": "Start tracking changes here (git init)", "shell": "git init"}})

        def work() -> None:
            t0 = time.time()
            buf = _PageOut()
            ok, message, exit_req = True, "", False
            captured: list[str] = []
            if not _RUN_LOCK.acquire(timeout=5):
                job.finish({"ok": False, "html": "", "message": "Another command is still running. Wait for it to finish, or press Stop.", "ms": 0})
                return
            job._holds_lock = True
            import sys
            saved = (sys.stdout, sys.stderr, sys.stdin)
            old_cols = os.environ.get("COLUMNS")
            agent = rt.agent
            had_own = "run_turn" in getattr(agent, "__dict__", {})
            orig_turn = agent.__dict__.get("run_turn") if had_own else None
            me = threading.current_thread()

            def capture(user_input: str, *a: Any, **k: Any) -> Any:
                if threading.current_thread() is me:
                    captured.append(user_input)
                    return None
                return (orig_turn or type(agent).run_turn.__get__(agent))(user_input, *a, **k)

            def restore() -> None:
                sys.stdout, sys.stderr, sys.stdin = saved
                if old_cols is None:
                    os.environ.pop("COLUMNS", None)
                else:
                    os.environ["COLUMNS"] = old_cols
                if agent.__dict__.get("run_turn") is capture:
                    if had_own:
                        agent.run_turn = orig_turn
                    else:
                        agent.__dict__.pop("run_turn", None)

            job._restore = restore
            before = rt.session_id
            os.environ["COLUMNS"] = "110"
            agent.run_turn = capture
            sys.stdout = sys.stderr = buf
            sys.stdin = io.StringIO("")
            try:
                from axon.commands.registry import dispatch_command
                try:
                    res = dispatch_command(line, agent)
                except KeyboardInterrupt:
                    res, message = None, "Stopped."
                    ok = False
                except SystemExit:
                    res, message, ok = None, "This command tried to end the process, so it was blocked.", False
                if res is not None:
                    ok = bool(getattr(res, "success", True))
                    message = str(getattr(res, "message", "") or "")
                    exit_req = bool(getattr(res, "should_exit", False))
                elif not message:
                    message = "Not a command: type it in the chat to send it to the model." if not buf.getvalue() else ""
                if exit_req:
                    message = "This command ends the session. Use the terminal for that; the page stays open."
            except BaseException as e:  # noqa: BLE001 - the page must get an answer
                ok, message = False, f"{type(e).__name__}: {e}"
            if job.done.is_set():
                return                                     # timed out or stopped meanwhile: the answer is already out
            text = self._hide_token(buf.getvalue())
            shown, total = _cap(text, 400_000)
            plain = strip_ansi(text).strip()
            result: dict[str, Any] = {"ok": ok, "html": ansi_to_html(shown), "plain_len": len(plain), "message": message,
                                      "truncated": total > 400_000, "ms": int((time.time() - t0) * 1000), "line": line}
            m = _USAGE_RE.match(plain)
            if m and not captured and len(plain) < 1500:
                result.update(html="", needs_arg=m.group(1).strip().splitlines()[0][:300],
                              message=message if message and not message.startswith("Usage") else "")
            if key in TERMINAL_NOTES and not result.get("message"):
                result["message"] = TERMINAL_NOTES[key]
            sw = self._switched(rt, before, line)
            if sw:
                result["switched"] = sw
                if cmd is not None and cmd.name in ("branch", "fork"):
                    result["message"] = (f"Branched: you are now in a new session ({sw['to'][:8]}). "
                                         f"To go back, open the earlier one: /resume {sw['from']}")
                elif cmd is not None and cmd.name in ("clear", "reset", "new"):
                    result["message"] = f"Started a fresh session. The previous one is still in Sessions: /resume {sw['from']}"
                else:
                    result["message"] = result["message"] or f"Now in session {sw['to'][:8]}. The previous one was {sw['from']}."
            if captured:
                result.update(chat=True, session=rt.session_id)
                ready, why = (True, "")
                if self.srv is not None:
                    from axon.features import dash_providers as dp
                    ready, why = dp.provider_ready(self.srv, rt)
                if not ready:
                    result.update(ok=False, chat=False, message=why)
                    captured.clear()
                else:
                    result["message"] = "Started in Chat: the answer streams there."
            if not job.finish(result):
                return
            if rt.cmd_thread is me:
                rt.cmd_thread = None
            if captured:
                try:
                    rt.send(captured[0], display=line)
                    for extra in captured[1:]:
                        rt.send(extra, steer=False)
                except Exception as e:  # noqa: BLE001
                    rt.hub.emit("error", {"message": f"{type(e).__name__}: {e}"})

        def watchdog() -> None:
            if job.done.wait(self.timeout):
                return
            self.abort(rt, job, f"This command ran too long (over {int(self.timeout)} s) and was stopped.")

        job.thread = rt.cmd_thread = threading.Thread(target=work, name="axon-dash-cmd", daemon=True)
        rt.cmd_job = job
        rt.runner = self
        rt.cmd_thread.start()
        threading.Thread(target=watchdog, name="axon-dash-cmd-watch", daemon=True).start()
        return job

    def abort(self, rt: Runtime, job: CommandJob, why: str = "Stopped.") -> bool:
        """End a command now: answer the page, free the lock, and interrupt the thread as far as Python allows."""
        t = job.thread
        won = job.finish({"ok": False, "html": "", "message": why, "ms": int((time.time() - job.started) * 1000), "line": job.line})
        if won and t is not None and t.is_alive() and t is not threading.current_thread():
            try:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(t.ident or 0), ctypes.py_object(KeyboardInterrupt))
            except Exception:
                pass
        if rt.cmd_thread is t:
            rt.cmd_thread = None
        return won


# ─── shell ──────────────────────────────────────────────────────────────────

class ShellJob:
    def __init__(self, command: str, cwd: Path) -> None:
        self.id = secrets.token_hex(5)
        self.command = command
        self.cwd = str(cwd)
        self.started = time.time()
        self.hub = EventHub(cap=20000)
        self.proc: subprocess.Popen[bytes] | None = None
        self.code: int | None = None
        self.refused = ""
        self.finished = 0.0
        self.killed = False
        self.end_cwd = ""
        self.bytes = 0
        self.text: list[str] = []
        self.text_len = 0
        self.on_done: Callable[["ShellJob"], None] | None = None

    def done(self) -> None:
        if self.on_done is not None:
            try:
                self.on_done(self)
            except Exception:
                pass

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "command": self.command, "cwd": self.cwd, "started": self.started, "code": self.code,
                "running": self.proc is not None and self.code is None and not self.refused, "refused": self.refused,
                "duration": round((self.finished or time.time()) - self.started, 2), "killed": self.killed}


class ShellRunner:
    """Line-based command runner for the Shell panel (stdout and stderr streamed separately; no PTY, so
    full-screen programs such as vim or top do not work)."""
    MAX_BYTES = 8 * 1024 * 1024

    def __init__(self) -> None:
        self.jobs: dict[str, ShellJob] = {}
        self.history: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.cwds: dict[str, Path] = {}   # project folder -> where its last command ended

    def current_cwd(self, ws: Path) -> Path:
        """The folder the next command starts in, so `cd` carries over as it does in a terminal."""
        cwd = self.cwds.get(str(ws))
        return cwd if cwd is not None and cwd.is_dir() else Path(ws)

    def _note_cwd(self, job: ShellJob, ws: Path, cwd_file: str, command: str) -> None:
        """Remember where the command ended. A shell without an exit trap only follows a lone `cd <path>`."""
        ended = ""
        if cwd_file:
            try:
                ended = Path(cwd_file).read_text(encoding="utf-8", errors="surrogateescape").strip()
            except OSError:
                pass
            try:
                os.unlink(cwd_file)
            except OSError:
                pass
        elif job.code == 0:
            m = re.fullmatch(r"\s*cd\s+(?:/d\s+)?[\"']?([^\"'&|;]+?)[\"']?\s*", command, re.IGNORECASE)
            if m:
                ended = str((Path(job.cwd) / os.path.expanduser(m.group(1))).resolve())
        if ended and Path(ended).is_dir():
            self.cwds[str(ws)] = Path(ended)
            job.end_cwd = ended

    def guard(self, command: str, ws: Path, cwd: Path | None = None) -> str:
        from axon.permissions.engine import catastrophic_command, shell_credential_access
        if not command.strip():
            return "empty command"
        why = catastrophic_command(command)
        if why:
            return f"refused (destructive): {why}"
        # Checked as if typed after a cd, so relative paths and recursive reads resolve from the real folder.
        here = f"cd {shlex.quote(str(cwd))} && " if cwd is not None and Path(cwd) != Path(ws) else ""
        why = shell_credential_access(here + command, ws)
        if why:
            return f"refused (credential access): {why}"
        return ""

    def start(self, command: str, cwd: Path, ws: Path, settings: Any, on_done: Callable[[ShellJob], None] | None = None) -> ShellJob:
        from axon.platform import kill_process_tree, new_process_group_kwargs, shell_command_argv, shell_info  # noqa: F401
        from axon.permissions import sandbox
        from axon.tools.shell import _child_env
        job = ShellJob(command, cwd)
        job.on_done = on_done
        with self.lock:
            self.jobs[job.id] = job
            while len(self.jobs) > 30:
                old = next(iter(self.jobs))
                if self.jobs[old].proc is not None and self.jobs[old].code is None:
                    break
                self.jobs.pop(old)
        why = self.guard(command, ws, cwd)
        if why:
            job.refused = why
            job.finished = time.time()
            job.hub.emit("refused", {"message": why})
            job.hub.emit("exit", {"code": None, "refused": True})
            job.hub.close()
            self._remember(job)
            job.done()
            return job
        info = shell_info()
        env = _child_env(ws)
        cwd_file = ""
        script = command
        if info.kind in ("bash", "sh", "zsh") and not info.is_powershell:
            fd, cwd_file = tempfile.mkstemp(prefix="axon-cwd-")
            os.close(fd)
            env["AXON_CWD_FILE"] = cwd_file
            script = "trap 'pwd -P > \"$AXON_CWD_FILE\"' EXIT\n" + command
        argv = shell_command_argv(script, info)
        try:
            if sandbox.enabled(settings):
                if isinstance(argv, list):
                    argv = sandbox.wrap_argv(argv, settings)
        except Exception as e:
            job.refused = f"the sandbox is on but could not wrap the command: {e}"
            job.finished = time.time()
            job.hub.emit("refused", {"message": job.refused})
            job.hub.emit("exit", {"code": None, "refused": True})
            job.hub.close()
            self._remember(job)
            job.done()
            return job
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **new_process_group_kwargs())
        except OSError as e:
            job.refused = f"could not start the shell: {e.strerror or e}"
            job.finished = time.time()
            job.hub.emit("refused", {"message": job.refused})
            job.hub.emit("exit", {"code": None, "refused": True})
            job.hub.close()
            self._remember(job)
            job.done()
            return job
        job.proc = proc
        job.hub.emit("start", {"command": command, "cwd": str(cwd), "pid": proc.pid,
                               "sandbox": bool(sandbox.enabled(settings))})

        def pump(stream: Any, name: str) -> None:
            import codecs
            dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
            try:
                for chunk in iter(lambda: stream.read1(32768), b""):
                    job.bytes += len(chunk)
                    if job.bytes > self.MAX_BYTES:
                        job.hub.emit("note", {"message": "Output limit reached; the command was stopped."})
                        kill_process_tree(proc)
                        return
                    piece = dec.decode(chunk)
                    if job.text_len < 8_000_000:
                        job.text.append(strip_ansi(piece))
                        job.text_len += len(piece)
                    job.hub.emit("out", {"stream": name, "html": ansi_to_html(piece)})
            except (OSError, ValueError):
                pass

        readers = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
        for r in readers:
            r.start()

        def reap() -> None:
            code = proc.wait()
            for r in readers:
                r.join(timeout=2)
            job.code = code
            job.finished = time.time()
            self._note_cwd(job, ws, cwd_file, command)
            job.hub.emit("exit", {"code": code, "duration": round(job.finished - job.started, 2), "killed": job.killed,
                                  "cwd": str(self.current_cwd(ws))})
            job.hub.close()
            self._remember(job)
            job.done()

        threading.Thread(target=reap, daemon=True).start()
        return job

    def _remember(self, job: ShellJob) -> None:
        self.history.append(job.summary())
        del self.history[:-100]

    def kill(self, job_id: str) -> bool:
        from axon.platform import kill_process_tree
        job = self.jobs.get(job_id)
        if job is None or job.proc is None or job.code is not None:
            return False
        job.killed = True
        kill_process_tree(job.proc)
        return True

    def kill_all(self) -> None:
        for j in list(self.jobs.values()):
            self.kill(j.id)


# ─── attachments ────────────────────────────────────────────────────────────

class Attachments:
    """Stores what the user drops or pastes through the same durable places the terminal uses and returns
    the chip text that goes into the prompt."""
    MAX_UPLOAD = 64 * 1024 * 1024

    def __init__(self) -> None:
        self.pastes: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.lock = threading.Lock()

    def add_file(self, name: str, data: bytes, workspace: Path) -> dict[str, Any]:
        from axon import storage
        from axon.agent import attachments as att
        from axon.agent.images import IMAGE_EXTENSIONS, ingest_image_file
        safe = re.sub(r"[^\w.\- ]+", "_", Path(name or "file").name).strip(" .")[:80] or "file"
        from axon.features import dash_fs
        if dash_fs.is_secret_file(Path(name or "file").name):
            raise DashError(f'{Path(name or "file").name[:80]} is a credential file, so Axon does not attach it', 403)
        tmpdir = storage.cache_dir("dash-uploads")
        updir = tmpdir / secrets.token_hex(6)           # its own folder, so the file keeps its real name for the model to see
        updir.mkdir(mode=0o700, exist_ok=True)
        tmp = updir / safe
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o600)
            if tmp.suffix.lower() in IMAGE_EXTENSIONS:
                image = ingest_image_file(str(tmp))
                if image is None:
                    raise DashError("that image could not be read", 415)
                return {"kind": "image", "chip": image.label, "name": safe, "size": len(data),
                        "width": image.width, "height": image.height}
            try:
                a = att.ingest_file(tmp)
            except att.ProtectedFile:
                raise DashError(f'{Path(name or "file").name[:80]} is a credential or system file, so Axon does not attach it', 403) from None
            except OSError as e:
                raise DashError(f"could not save the file: {e.strerror or e}", 500) from None
            return {"kind": "file", "chip": a.chip.replace(tmp.name, safe), "name": safe, "size": len(data),
                    "stored": a.stored_path.name}
        finally:
            try:
                tmp.unlink()
                updir.rmdir()
            except OSError:
                pass

    def add_text(self, text: str, window: int = 200_000) -> dict[str, Any]:
        from axon.agent import large_input
        with self.lock:
            self.counter += 1
            n = self.counter
        tokens = large_input.estimate_tokens(text)
        lines = text.count("\n") + 1
        path = large_input.save_full_text(text)
        chip = f"[Pasted text #{n}: {lines:,} lines, ~{tokens:,} tokens]"
        with self.lock:
            self.pastes[chip] = {"text": text, "path": str(path) if path else "", "lines": lines, "tokens": tokens}
            while len(self.pastes) > 20:
                self.pastes.pop(next(iter(self.pastes)))
        return {"kind": "text", "chip": chip, "lines": lines, "tokens": tokens, "chars": len(text),
                "saved": bool(path), "limit_tokens": large_input.inline_limit(window)}

    def expand(self, text: str) -> str:
        """Chips back to what the model should receive (pasted text in full; Axon's own large-input logic then
        decides whether to send it whole or as start, end and a saved path)."""
        def repl(m: re.Match[str]) -> str:
            hit = self.pastes.get(m.group(0))
            return hit["text"] if hit else m.group(0)
        out = re.sub(r"\[Pasted text #\d+: [^\]]*\]", repl, text)
        try:
            from axon.features import dash_attach
            out = dash_attach.expand(out)           # @path references and folder chips added by drag and drop
        except Exception:
            pass
        try:
            from axon.agent.attachments import expand_file_chips
            from axon.agent.images import compact_image_paths
            out, _ = compact_image_paths(expand_file_chips(out))
        except Exception:
            pass
        return out
