"""Subagents over AXON_MAX_CONCURRENT_SUBAGENTS: the slot check and the registration are one step, a background
spawn over the limit is queued (and says so) instead of blocking its Task call, and a queued one can be stopped."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from axon.agent.subagent import SubagentManager, delegate, run_subagent
from axon.agent.trace import SubagentEvent
from axon.errors import ToolError
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _wait(cond: Any, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def _parent(workspace: Path, *replies: str) -> tuple[Any, list[Any]]:
    script: list[Any] = []
    for r in replies:
        script += scripted(r)
    events: list[Any] = []
    parent = make_agent(FakeProvider(script), workspace, events=events)
    parent.session.open("sess_p")
    return parent, events


def test_a_burst_of_spawns_never_exceeds_the_limit(workspace: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AXON_MAX_CONCURRENT_SUBAGENTS", "2")
    monkeypatch.setenv("AXON_SUBAGENT_QUEUE_WAIT", "0")
    parent, _ = _parent(workspace)
    parent.subagents = SubagentManager()
    real = SubagentManager.running_count

    def slow_count(self: Any) -> int:        # widen the gap between "is there a slot" and "take it"
        n = real(self)
        time.sleep(0.05)
        return n
    monkeypatch.setattr(SubagentManager, "running_count", slow_count)
    monkeypatch.setattr("axon.agent.subagent.run_subagent", lambda *a, **k: time.sleep(1.5) or "r")   # holds its slot
    gate = threading.Barrier(6)

    def spawn() -> None:
        gate.wait()
        try:
            delegate("work", parent, background=True, subagent_type="explore")
        except ToolError:
            pass
    threads = [threading.Thread(target=spawn) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert real(parent.subagents) <= 2


def test_background_spawn_over_the_limit_is_queued_and_returns_at_once(workspace: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AXON_MAX_CONCURRENT_SUBAGENTS", "1")
    monkeypatch.setenv("AXON_SUBAGENT_QUEUE_WAIT", "120")
    parent, events = _parent(workspace, "late report")
    busy = parent.subagents.register("busy", title="busy")
    t0 = time.monotonic()
    out = delegate("look", parent, background=True, subagent_type="explore", description="scan")
    assert time.monotonic() - t0 < 2.0
    assert out.startswith("Queued subagent #2") and "limit 1" in out
    task = parent.subagents.get_task("2")
    _wait(lambda: any(isinstance(e, SubagentEvent) and e.phase == "queued" for e in events))
    assert task.status == "queued" and "Queued" in task.activity and parent.subagents.running_count() == 1
    assert not any(isinstance(e, SubagentEvent) and e.phase == "start" for e in events)
    parent.subagents.complete(busy.id, "ok", parent.conversation)
    _wait(lambda: task.status == "completed")
    phases = [e.phase for e in events if isinstance(e, SubagentEvent)]
    assert phases == ["queued", "start", "end"]
    _wait(lambda: len(parent.message_queue) == 1)
    assert "late report" in parent.message_queue.items[0].text


def test_a_queued_subagent_can_be_stopped(workspace: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AXON_MAX_CONCURRENT_SUBAGENTS", "1")
    parent, events = _parent(workspace, "never used")
    parent.subagents.register("busy", title="busy")
    delegate("look", parent, background=True, subagent_type="explore", description="scan")
    task = parent.subagents.get_task("2")
    assert task.status == "queued"
    assert parent.subagents.cancel(task.id) is True
    assert task.status == "cancelled" and task.stopped
    _wait(lambda: len(parent.message_queue) == 1)
    assert "stopped by the user while it was queued" in parent.message_queue.items[0].text
    ends = [e for e in events if isinstance(e, SubagentEvent) and e.phase == "end"]
    assert [e.status for e in ends] == ["cancelled"]
    assert parent.subagents.running_count() == 1          # the queued one never took a slot


def test_foreground_spawn_shows_queued_then_fails_after_the_wait(workspace: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AXON_MAX_CONCURRENT_SUBAGENTS", "1")
    monkeypatch.setenv("AXON_SUBAGENT_QUEUE_WAIT", "0.3")
    parent, events = _parent(workspace, "x")
    parent.subagents.register("busy", title="busy")
    with pytest.raises(ToolError, match="Concurrent subagent limit"):
        run_subagent("another", parent, subagent_type="explore", description="second")
    task = parent.subagents.get_task("2")
    assert task.status == "error" and "limit" in (task.error_msg or "")
    assert [(e.phase, e.status) for e in events if isinstance(e, SubagentEvent)] == [("queued", "queued"), ("end", "error")]


def test_queued_subagents_start_in_the_order_they_were_queued() -> None:
    m = SubagentManager()
    a = m.register("a", limit=1)
    b = m.register("b", limit=1)
    c = m.register("c", limit=1)
    assert (a.status, b.status, c.status) == ("running", "queued", "queued")
    assert not m.admit(c.id, 1) and not m.admit(b.id, 1)
    m.complete(a.id, "ok", a.conversation)
    assert not m.admit(c.id, 1)            # b was queued first
    assert m.admit(b.id, 1) and b.status == "running" and not m.admit(c.id, 1)


def test_monitor_and_roster_say_queued() -> None:
    from axon.ui.subagent_monitor import _header, _x_hint, agent_table
    from axon.ui.theme import strip_ansi
    m = SubagentManager()
    m.register("a", title="first", limit=1)
    b = m.register("b", title="second", limit=1)
    b.activity = "Queued · waiting for a free slot (1 running, limit 1)"
    text = strip_ansi("\n".join(agent_table(m.all_tasks(), 120)))
    assert "waiting for a free slot" in text
    assert "1 running" in strip_ansi(_header(m.all_tasks(), 120, time.time())) and "1 queued" in strip_ansi(_header(m.all_tasks(), 120, time.time()))
    assert _x_hint(b) == ("x", "stop")


def test_trace_and_terminal_show_the_queue_step(capsys: Any) -> None:
    from axon.agent.trace import TurnTrace
    from axon.ui.theme import strip_ansi
    queued = SubagentEvent(phase="queued", task_id="sub-2", agent_type="explore", title="scan", model="m", status="queued",
                           detail="waiting for a free slot (1 running, limit 1)")
    started = SubagentEvent(phase="start", task_id="sub-2", agent_type="explore", title="scan", model="m")
    tr = TurnTrace(turn=1, prompt="hi", model="m", mode="default")
    tr.record(queued)
    tr.record(started)
    subs = [s for s in tr.steps if s.kind == "subagent"]
    assert len(subs) == 1 and subs[0].data["phase"] == "start" and "queued_s" in subs[0].data
    from axon.ui.render import Renderer
    Renderer(trace_level="normal").on_event(queued)
    out = strip_ansi(capsys.readouterr().out)
    assert "subagent queued" in out and "#2 scan" in out and "waiting for a free slot" in out


def test_step_limit_is_announced_as_it_happens(workspace: Path) -> None:
    from axon.agent.trace import SubagentStep
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    events: list[Any] = []
    script = scripted(("Read", {"file_path": "a.txt"}), ("Read", {"file_path": "a.txt"}), "wrap-up report")
    parent = make_agent(FakeProvider(script), workspace, events=events)
    parent.session.open("sess_p")
    out = run_subagent("read things", parent, subagent_type="explore", max_iterations=2)
    assert "wrap-up report" in out
    notes = [e for e in events if isinstance(e, SubagentStep) and e.kind == "note"]
    assert [n.name for n in notes] == ["step limit"] and "2 steps" in notes[0].summary
    ends = [e for e in events if isinstance(e, SubagentEvent) and e.phase == "end"]
    assert ends[0].status == "exhausted" and events.index(notes[0]) < events.index(ends[0])
