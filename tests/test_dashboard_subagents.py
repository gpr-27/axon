"""Subagents in the web app: the roster (live and finished), the full detail of one, the links between a Task step and
its subagent, stop/tell, and that a subagent transcript is only ever read."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.agent.subagent import SubagentManager  # noqa: E402
from test_dashboard import c, env, llm, server  # noqa: E402,F401
from test_dashboard_app import _hold  # noqa: E402


def _w(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _append(path: Path, recs: list[dict]) -> None:
    with open(path, "a") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")


@pytest.fixture
def parent(c, env):
    """The sample session with two Task calls: #1 finished with a saved result, #2 has a transcript but no result."""
    sid = env.ids["refactor"]
    folder = Path(c.srv.index.lookup(sid).path).parent
    _append(folder / "session.jsonl", [
        {"type": "assistant_turn", "timestamp": 50, "data": {"text": "delegating", "model": "gpt-4o", "tool_uses": [
            {"id": "call_task_1", "name": "Task", "input": {"description": "scan the repo", "prompt": "scan the repo", "subagent_type": "explore"}},
            {"id": "call_task_2", "name": "Task", "input": {"description": "write notes", "prompt": "write notes"}}]}},
        {"type": "tool_results", "timestamp": 60, "data": [
            {"tool_use_id": "call_task_1", "content": "[Subagent #1 Result (scan the repo) | 15 tokens]\nall done"}]},
        {"type": "subagent_result", "timestamp": 61, "data": {
            "task_id": "sub-1", "index": 1, "agent_type": "explore", "title": "scan the repo", "model": "gpt-4o", "prompt": "scan the repo",
            "status": "completed", "result": "all done, 3 files", "input_tokens": 10, "output_tokens": 5, "cost": 0.01, "iterations": 2,
            "elapsed_s": 4.25, "sub_session_id": f"{sid}_sub_1", "call_id": "call_task_1"}},
    ])
    sub = folder / "subagents"
    _w(sub / "1.jsonl", [
        {"type": "user_message", "timestamp": 2, "data": {"role": "user", "content": "scan the repo"}},
        {"type": "assistant_turn", "timestamp": 3, "data": {"text": "scanning", "thinking": "plan: grep first", "tool_uses": [
            {"id": "s1", "name": "Grep", "input": {"pattern": "x"}}, {"id": "s2", "name": "Edit", "input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}}],
            "model": "gpt-4o", "cost": 0.01, "usage": {"input": 10, "output": 5}, "duration_s": 1.5}},
        {"type": "permission", "timestamp": 3.5, "data": {"tool_use_id": "s2", "tool": "Edit", "outcome": "deny", "reason": "not allowed here", "source": "user"}},
        {"type": "tool_results", "timestamp": 4, "data": [{"tool_use_id": "s1", "content": "found it"}, {"tool_use_id": "s2", "content": "denied", "is_error": True}]},
        {"type": "assistant_turn", "timestamp": 5, "data": {"text": "all done, 3 files", "tool_uses": [], "model": "gpt-4o", "usage": {"input": 4, "output": 2}}},
    ])
    _w(sub / "2.jsonl", [
        {"type": "user_message", "timestamp": 7, "data": {"role": "user", "content": "write notes"}},
        {"type": "assistant_turn", "timestamp": 8, "data": {"text": "started", "tool_uses": [], "model": "gpt-4o", "usage": {"input": 3, "output": 1}}},
    ])
    return SimpleNamespace(sid=sid, folder=folder, sub=sub)


def test_roster_adds_what_the_parent_recorded(c, parent):
    v = c.json(f"/api/subagents?session={parent.sid}")
    r1, r2 = v["subagents"]
    assert (r1["type"], r1["status"], r1["model"], r1["iterations"], r1["elapsed"]) == ("explore", "completed", "gpt-4o", 2, 4.25)
    assert r1["recorded_result"] and r1["call_id"] == "call_task_1" and r1["session_id"] == f"{parent.sid}_sub_1"
    assert r1["tokens"] == 15 and r1["cost"] == 0.01
    assert r1["parent_step"]["tool"] == 0 and not r1["parent_step"]["inferred"] and r1["parent_step"]["label"].startswith("scan the repo")
    assert r2["status"] == "unrecorded" and not r2["recorded_result"]          # never shown as finished when nothing says so
    assert r2["parent_step"] is None                                           # the second Task has no result naming it: not guessed
    assert any("no saved result" in n for n in v["notes"]) and v["not_recorded"]


def test_detail_has_everything_the_transcript_and_parent_hold(c, parent):
    d = c.json(f"/api/subagent/detail?session={parent.sid}&n=1")
    assert d["prompt"] == "scan the repo" and d["result"] == "all done, 3 files"
    assert d["records"]["assistant_turn"] == 2 and d["records"]["permission"] == 1
    assert d["permissions"][0]["tool"] == "Edit" and d["permissions"][0]["outcome"] == "deny" and d["permissions"][0]["reason"] == "not allowed here"
    assert d["not_recorded"] and d["step_note"]                                # no event log: says so
    assert c.get(f"/api/subagent/detail?session={parent.sid}&n=99")[0] == 404
    assert c.get(f"/api/subagent/detail?session={parent.sid}&n=abc")[0] == 400


def test_transcript_renders_like_the_main_chat(c, parent):
    page = c.json(f"/api/session/{parent.sid}_sub_1")
    html = "".join(e["html"] for e in page["entries"])
    assert "plan: grep first" in html and "Thinking" in html                   # thinking
    assert "found it" in html and "<summary><span class=\"tn\">Grep" in html   # tool input and output
    assert "a/a.py" in html or "-a" in html                                    # the Edit diff
    assert "denied" in html and "not allowed here" in html                     # permission decision
    assert "in 10" in html and "$0.01" in html and "1.5s" in html              # per-step tokens, cost, time


def test_the_transcript_is_only_read(c, parent):
    f = parent.sub / "1.jsonl"
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    for p in (f"/api/subagents?session={parent.sid}", f"/api/subagent/detail?session={parent.sid}&n=1", f"/api/session/{parent.sid}_sub_1"):
        c.json(p)
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before


def test_a_session_held_by_a_terminal_marks_unrecorded_runs_as_running_elsewhere(c, parent, other_live_pid):
    _hold(parent.folder, other_live_pid)
    v = c.json(f"/api/subagents?session={parent.sid}")
    r2 = v["subagents"][1]
    assert r2["status"] == "running elsewhere" and r2["held_elsewhere"] and v["lease"]["pid"] == other_live_pid
    assert v["subagents"][0]["status"] == "completed"                           # a recorded result still wins
    st, _, _ = c.post("/api/subagent/stop", {"session": parent.sid, "task": "sub-2"})
    assert st == 404                                                           # nothing of it runs in this server


def test_live_subagents_stream_in_and_can_be_stopped_and_told(c, parent, monkeypatch):
    mgr = SubagentManager()
    mgr.reserve("call_task_1"), mgr.reserve("call_task_2"), mgr.reserve("call_live")
    t = mgr.register("investigate", max_steps=9, title="investigate", agent_type="plan", call_id="call_live")
    assert t.index == 3 and t.call_id == "call_live"
    t.steps, t.activity = 2, "Read a.py"
    sent: list[str] = []
    t.live_agent = SimpleNamespace(message_queue=SimpleNamespace(push=lambda text, steer=True: sent.append(text)))
    _append(parent.folder / "session.jsonl", [{"type": "assistant_turn", "timestamp": 70, "data": {"text": "", "tool_uses": [
        {"id": "call_live", "name": "Task", "input": {"description": "investigate", "prompt": "investigate"}}]}}])
    rt = SimpleNamespace(agent=SimpleNamespace(subagents=mgr), session_id=parent.sid, event_log=lambda: None)
    monkeypatch.setattr(c.srv, "runtime", lambda sid, create=True: rt)
    v = c.json(f"/api/subagents?session={parent.sid}")
    live = next(r for r in v["subagents"] if r["index"] == 3)
    assert live["live"] and live["status"] == "running" and live["type"] == "plan" and live["activity"] == "Read a.py" and live["elapsed"] >= 0
    assert live["parent_step"]["label"] == "investigate" and not live["parent_step"]["inferred"]   # exact: by call id
    st, _, out = c.post("/api/subagent/tell", {"session": parent.sid, "task": "sub-3", "text": "focus on tests"})
    assert st == 200 and sent == ["focus on tests"]
    stopped: list[str] = []
    monkeypatch.setattr(mgr, "cancel", lambda tid: stopped.append(tid) or True)    # the real one interrupts a bound thread
    st, _, out = c.post("/api/subagent/stop", {"session": parent.sid, "task": "sub-3"})
    assert st == 200 and json.loads(out)["ok"] is True and stopped == ["sub-3"]
    mgr.complete("sub-3", "done", mgr.get_task("sub-3").conversation, status="completed")
    st, _, _ = c.post("/api/subagent/tell", {"session": parent.sid, "task": "sub-3", "text": "again"})
    assert st == 409                                                           # not running any more


def test_subagent_steps_come_from_the_event_log(c, parent, monkeypatch):
    from axon.features.dash_runtime import EventLog
    log = EventLog(parent.folder)
    log.append("subagent_step", {"task_id": "sub-1", "agent_type": "explore", "kind": "llm", "name": "gpt-4o", "summary": "10 in 5 out", "cost": 0.01, "input_tokens": 10, "output_tokens": 5, "duration_s": 1.5})
    log.append("subagent_step", {"task_id": "sub-2", "agent_type": "general-purpose", "kind": "tool", "name": "Read", "summary": "x"})
    log.flush()
    d = c.json(f"/api/subagent/detail?session={parent.sid}&n=1")
    assert [s["name"] for s in d["steps"]] == ["gpt-4o"] and d["steps"][0]["cost"] == 0.01 and d["step_note"] == ""


def _app_js() -> str:
    from axon.features.dashboard_web import APP_JS
    return APP_JS


def test_the_page_script_is_valid_and_reuses_the_chat_components(tmp_path):
    js = _app_js()
    assert "function viewAgent(" in js and "subagent_step" in js and "/api/subagent/detail" in js
    mine = js[js.index("// ---------- subagents: list, one subagent in full"):]
    for reuse in ("historyNodes(", "wireHistory(", "tailHistory(", "toggleAll("):
        assert reuse in mine
    assert "function historyEntry" not in mine and "function toggleAll" not in mine     # used, not copied
    assert "animation" not in mine and "transition" not in mine                        # no animation
    node = shutil.which("node")
    if node:
        f = tmp_path / "app.js"
        f.write_text(js, encoding="utf-8")
        res = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, encoding="utf-8")
        assert res.returncode == 0, res.stderr


def test_the_script_is_served(c):
    st, _, body = c.get("/static/app.js")
    assert st == 200 and b"/api/subagent/detail" in body


def test_a_finished_subagent_shows_its_final_state_not_the_last_live_line(c, parent, monkeypatch):
    mgr = SubagentManager()
    mgr.reserve("call_task_1"), mgr.reserve("call_task_2"), mgr.reserve("call_live")
    t = mgr.register("investigate", max_steps=9, title="investigate", agent_type="plan", call_id="call_live")
    t.steps, t.activity = 2, "Step 2 · waiting for gpt-4o"
    rt = SimpleNamespace(agent=SimpleNamespace(subagents=mgr), session_id=parent.sid, event_log=lambda: None)
    monkeypatch.setattr(c.srv, "runtime", lambda sid, create=True: rt)
    mgr.complete("sub-3", "done", mgr.get_task("sub-3").conversation, status="completed")
    r = next(x for x in c.json(f"/api/subagents?session={parent.sid}")["subagents"] if x["index"] == 3)
    assert not r["live"] and "waiting for" not in r["activity"] and r["activity"].startswith("Finished"), r["activity"]
