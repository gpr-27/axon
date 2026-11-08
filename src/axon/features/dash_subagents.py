"""Subagents in the web app: one roster (live and finished), and the full detail of one.

The base roster comes from ``dash_api.subagents_view`` (live manager plus ``subagents/<n>.jsonl`` files). This module
adds what only the parent session records: the ``subagent_result`` record (type, status, duration, iterations, final
report) and the parent step (the Task call) that started each one. A subagent transcript is only ever read here.
Nothing is guessed: a field that is not recorded is reported as not recorded."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:
    from axon.features.dashboard import DashServer

RESULT_RE = re.compile(r"^\[Subagent #(\d+)\b")
STEP_LIMIT = 400

# What a subagent transcript holds, and what it does not (shown on the page so a partial view is never read as complete).
NOT_RECORDED = [
    "The request sent to the model (system prompt, tool schemas) and the size of its context are not saved for subagents.",
    "Text streamed before a model call finished is saved only as the finished message.",
    "Retries, resumes and compaction inside a subagent are not saved in its transcript.",
]


def _parent_path(srv: "DashServer", sid: str, rt: Any) -> Path | None:
    try:
        ref = srv.index.lookup(sid) if sid else None
        if ref is not None:
            return Path(ref.path)
    except DashError:
        pass
    if rt is not None:
        try:
            return Path(rt.agent.session._file(rt.session_id))
        except Exception:
            return None
    return None


def _results(path: Path) -> dict[int, list[dict[str, Any]]]:
    """``subagent_result`` records of the parent transcript by subagent number (one per run)."""
    from axon.session.store import iter_records
    out: dict[int, list[dict[str, Any]]] = {}
    for rec in iter_records(path):
        if rec.get("type") == "subagent_result" and isinstance(rec.get("data"), dict):
            try:
                out.setdefault(int(rec["data"].get("index")), []).append(rec["data"])
            except (TypeError, ValueError):
                continue
    return out


def _task_calls(path: Path) -> list[dict[str, Any]]:
    """Every Task call of the parent transcript: entry number, tool number, call id, subagent number (when its result says)."""
    from axon.features.dashboard import load_transcript, tool_summary
    out: list[dict[str, Any]] = []
    for e in load_transcript(path).entries:
        if e.get("kind") != "assistant":
            continue
        for n, tool in enumerate(e["tools"]):
            if tool["name"] != "Task":
                continue
            m = RESULT_RE.match(tool.get("result") or "")
            out.append({"entry": e["i"], "tool": n, "id": tool["id"], "index": int(m.group(1)) if m else None,
                        "label": tool_summary("Task", tool["input"]) or "Task", "done": tool.get("result") is not None})
    return out


def _live_tasks(rt: Any) -> dict[int, Any]:
    mgr = getattr(getattr(rt, "agent", None), "subagents", None) if rt is not None else None
    try:
        return {t.index: t for t in mgr.all_tasks()} if mgr is not None else {}
    except Exception:
        return {}


def _holder(folder: Path | None) -> dict[str, Any] | None:
    """The process holding the session's writer lease when it is not this server (a terminal runs it)."""
    if folder is None:
        return None
    try:
        from axon.session import lease
        info = lease.holder(folder)
    except Exception:
        return None
    return info if info and int(info.get("pid") or 0) != os.getpid() else None


def _link_parents(rows: list[dict[str, Any]], calls: list[dict[str, Any]], live: dict[int, Any]) -> None:
    """Attach ``parent_step`` to each row: exact when the call id or the Task result names the subagent, otherwise
    inferred by call order for runs still in flight (marked ``inferred``)."""
    by_id = {c["id"]: c for c in calls if c["id"]}
    taken: set[int] = set()
    for r in rows:
        call = None
        cid = (live.get(r["index"]) and getattr(live[r["index"]], "call_id", "")) or r.get("call_id") or ""
        if cid and cid in by_id:
            call = by_id[cid]
        else:
            call = next((c for c in calls if c["index"] == r["index"]), None)
        if call is not None:
            r["parent_step"] = {"entry": call["entry"], "tool": call["tool"], "label": call["label"], "inferred": False}
            taken.add(id(call))
    waiting = [c for c in calls if not c["done"] and c["index"] is None and id(c) not in taken]
    pending = [r for r in rows if "parent_step" not in r and r.get("live")]
    if waiting and len(waiting) == len(pending):
        for r, c in zip(sorted(pending, key=lambda x: x["index"]), waiting):
            r["parent_step"] = {"entry": c["entry"], "tool": c["tool"], "label": c["label"], "inferred": True}
    for r in rows:
        r.setdefault("parent_step", None)


_FINAL_WORDS = {"completed": "Finished", "finished": "Finished", "failed": "Failed", "error": "Failed",
                "stopped": "Stopped", "cancelled": "Stopped", "interrupted": "Stopped"}


def _final_line(r: dict[str, Any]) -> str:
    word = _FINAL_WORDS.get(str(r.get("status") or "").lower(), "Finished")
    steps = int(r.get("iterations") or r.get("steps") or 0)
    tail = f" in {steps} step{'s' if steps != 1 else ''}" if steps and word == "Finished" else ""
    err = f": {str(r['error'])[:120]}" if word == "Failed" and r.get("error") else ""
    return word + tail + err


def roster(srv: "DashServer", sid: str) -> dict[str, Any]:
    from axon.features.dash_api import subagents_view
    base = subagents_view(srv, sid)
    rt = srv.runtime(sid, create=False)
    path = _parent_path(srv, sid, rt)
    live = _live_tasks(rt)
    recs: dict[int, list[dict[str, Any]]] = {}
    calls: list[dict[str, Any]] = []
    notes = list(base["notes"])
    if path is not None and path.exists():
        try:
            recs = _results(path)
            calls = _task_calls(path)
        except Exception:
            notes.append("The parent session's record could not be read, so types, durations and parent steps may be missing.")
    held = _holder(path.parent if path is not None else None)
    now = time.time()
    rows = base["subagents"]
    for r in rows:
        runs = recs.get(r["index"], [])
        last = runs[-1] if runs else {}
        r["recorded_result"] = bool(runs)
        r["runs"] = max(len(runs), 1)
        if last:
            r["call_id"] = last.get("call_id") or ""
            if not r.get("type"):
                r["type"] = str(last.get("agent_type") or "")
            if not r.get("live"):
                r["status"] = str(last.get("status") or r.get("status") or "")
            r["model"] = r.get("model") or str(last.get("model") or "")
            r["title"] = str(last.get("title") or r["title"]) if r.get("source") != "live" else r["title"]
            r["iterations"] = int(last.get("iterations") or 0)
            if not r.get("live"):
                r["elapsed"] = round(sum(float(x.get("elapsed_s") or 0) for x in runs), 2)
                r["tokens"] = sum(int(x.get("input_tokens") or 0) + int(x.get("output_tokens") or 0) for x in runs) or r.get("tokens", 0)
                r["cost"] = round(sum(float(x.get("cost") or 0) for x in runs), 5) if runs else r.get("cost")
            r["prompt"] = r.get("prompt") or str(last.get("prompt") or "")[:600]
        t = live.get(r["index"])
        if t is not None and r.get("live"):
            r["elapsed"] = round(max(0.0, now - float(getattr(t, "start_time", now) or now)), 1)
            r["call_id"] = getattr(t, "call_id", "") or r.get("call_id", "")
        if not r.get("live") and not last and r.get("has_transcript"):
            r["status"] = "unrecorded"      # a transcript exists but the parent never saved a result for it
            if held is not None:
                r["status"] = "running elsewhere"
        r["held_elsewhere"] = bool(held) and not r.get("live") and not last
        r.setdefault("cost", None)
        if not r.get("live") and (r.get("activity") or r.get("status") in _FINAL_WORDS):
            r["activity"] = _final_line(r)             # never leave "waiting for <model>" on a run that is over
    _link_parents(rows, calls, live)
    if any(r["held_elsewhere"] for r in rows):
        notes.append(f"This session is open in another process ({(held or {}).get('owner', 'terminal')}, pid {(held or {}).get('pid')}). "
                     "Its subagents can be read here but only stopped or told from there.")
    if any(r["status"] == "unrecorded" for r in rows):
        notes.append("Some subagents have a transcript but no saved result: they were interrupted, or finished before results were saved.")
    return {"session": sid, "subagents": rows, "notes": notes, "lease": held, "not_recorded": NOT_RECORDED}


def _sub_records(path: Path) -> dict[str, Any]:
    from axon.session.store import iter_records
    counts: dict[str, int] = {}
    perms: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for rec in iter_records(path):
        t = str(rec.get("type"))
        counts[t] = counts.get(t, 0) + 1
        d = rec.get("data")
        if t == "assistant_turn" and isinstance(d, dict):
            for tu in d.get("tool_uses") or []:
                if isinstance(tu, dict):
                    names[str(tu.get("id"))] = str(tu.get("name") or "")
        elif t == "permission" and isinstance(d, dict):
            perms.append({"tool": str(d.get("tool") or names.get(str(d.get("tool_use_id")), "")), "tool_use_id": str(d.get("tool_use_id") or ""),
                          "outcome": str(d.get("outcome") or ""), "reason": str(d.get("reason") or "")[:500],
                          "source": str(d.get("source") or ""), "approval": d.get("approval"), "ts": rec.get("timestamp")})
    return {"counts": counts, "permissions": perms[:500]}


def _steps(rt: Any, folder: Path | None, task: str) -> list[dict[str, Any]]:
    """The ``subagent_step`` events of the parent's event log for one subagent (what the live stream carried)."""
    log = None
    try:
        log = rt.event_log() if rt is not None else None
    except Exception:
        log = None
    if log is None and folder is not None:
        from axon.features.dash_runtime import EventLog
        if (folder / "web-events.jsonl").exists():
            log = EventLog(folder)
    if log is None:
        return []
    out = []
    for rec in log.read(0, 20000):
        d = rec.get("data") or {}
        if rec.get("type") == "subagent_step" and d.get("task_id") == task:
            out.append({"seq": rec.get("seq"), "ts": rec.get("ts"), "kind": d.get("kind"), "name": d.get("name"), "summary": d.get("summary"),
                        "is_error": bool(d.get("is_error")), "duration_s": d.get("duration_s"), "cost": d.get("cost"),
                        "input_tokens": d.get("input_tokens"), "output_tokens": d.get("output_tokens")})
    return out[-STEP_LIMIT:]


def detail(srv: "DashServer", sid: str, n: int) -> dict[str, Any]:
    data = roster(srv, sid)
    row = next((r for r in data["subagents"] if r["index"] == n), None)
    if row is None:
        raise DashError("no such subagent", 404)
    rt = srv.runtime(sid, create=False)
    path = _parent_path(srv, sid, rt)
    folder = path.parent if path is not None else None
    sub_file = (folder / "subagents" / f"{n}.jsonl") if folder is not None else None
    out: dict[str, Any] = {"subagent": row, "session": sid, "notes": data["notes"], "lease": data["lease"],
                           "not_recorded": list(NOT_RECORDED), "records": {}, "permissions": [], "result": "", "prompt": row.get("prompt", ""), "timeline": []}
    if path is not None and path.exists():
        runs = _results(path).get(n, [])
        if runs:
            out["result"] = str(runs[-1].get("result") or "")
            out["prompt"] = str(runs[-1].get("prompt") or out["prompt"])
    t = _live_tasks(rt).get(n)
    if t is not None:
        out["timeline"] = [{"kind": e.get("kind"), "text": str(e.get("text", ""))[:2000], "is_error": bool(e.get("is_error")), "t": e.get("t")}
                           for e in list(getattr(t, "timeline", []))[-300:]]
        out["prompt"] = str(getattr(t, "prompt", "") or out["prompt"])
        if getattr(t, "worktree_note", ""):
            out["worktree_note"] = t.worktree_note
    if sub_file is not None and sub_file.exists():
        rec = _sub_records(sub_file)
        out["permissions"], out["records"] = rec["permissions"], rec["counts"]
    out["steps"] = _steps(rt, folder, f"sub-{n}")
    out["step_note"] = ("Step lines come from the live event stream recorded by this app; a subagent run before the app was watching "
                        "has none, and its messages and tool calls are in the transcript below.") if not out["steps"] else ""
    return out
