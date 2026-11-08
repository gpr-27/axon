"""/trail: why a tool call was made, what allowed it, what it returned and what came next."""
from __future__ import annotations

from pathlib import Path

from axon.agent.trace import PermissionDecisionEvent
from axon.features.trail import list_calls, trail_for
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _run(workspace: Path, mode: str = "bypass"):
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    prov = FakeProvider(scripted(("Read", {"path": str(workspace / "a.txt")}), ("Read", {"path": str(workspace / "a.txt")}), "done"))
    agent = make_agent(prov, workspace, mode=mode)
    agent.run_turn("read it")
    return agent


def test_permission_event_records_mode_and_rule(workspace: Path) -> None:
    agent = _run(workspace)
    steps = [s for s in agent.traces[-1].steps if s.kind == "tool"]
    perm = steps[0].data["permission"]
    assert perm["mode"] == "bypass" and perm["outcome"] == "allow" and "rule" in perm


def test_trail_pieces(workspace: Path) -> None:
    agent = _run(workspace)
    calls = list_calls(agent.traces)
    assert len(calls) == 2
    t = trail_for(agent.traces, 1)
    assert t["name"] == "Read" and t["input"]["path"].endswith("a.txt")
    assert t["permission"]["outcome"] == "allow" and t["permission"]["mode"] == "bypass"
    assert t["rationale"] is not None and t["rationale"]["source"] in ("model-text", "model-thinking", "heuristic")
    assert t["result"]["is_error"] is False and "hello" in t["result"]["preview"]
    assert t["next"]["kind"] in ("llm", "tool")


def test_missing_pieces_are_reported_as_not_recorded_not_invented() -> None:
    from axon.agent.trace import Step, TurnTrace
    tr = TurnTrace(turn=1, prompt="p", model="m", mode="default")
    tr.steps.append(Step(index=1, kind="tool", started_at=1.0, ended_at=2.0, data={"id": "x", "name": "Bash", "input": {"command": "ls"}}))
    t = trail_for([tr], 1)
    assert t["rationale"] is None and t["permission"] is None and t["result"] is None
    assert t["next"]["kind"] == "end"


def test_default_is_the_last_call_and_out_of_range_is_none(workspace: Path) -> None:
    agent = _run(workspace)
    assert trail_for(agent.traces, None)["n"] == 2
    assert trail_for(agent.traces, 9) is None


def test_event_type_has_new_fields_with_defaults() -> None:
    e = PermissionDecisionEvent(id="1", tool="Read", outcome="allow", reason="r", source="mode")
    assert e.rule == "" and e.mode == ""
