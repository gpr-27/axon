"""Forking a session keeps its subagent transcripts: /subagents in the fork still lists and opens them."""
from __future__ import annotations

from pathlib import Path

from axon import cli
from axon.agent.subagent import run_subagent, sync_subagents_for_session
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def test_fork_copies_subagent_transcripts(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("main answer") + scripted("sub report")), workspace)
    sid = agent.session.open(None)
    agent.run_turn("first prompt")
    assert "sub report" in run_subagent("look around", agent, subagent_type="explore", description="scan")
    old = agent.session.session_dir / sid / "subagents"
    assert (old / "1.jsonl").is_file()
    new_id = cli.fork_session(agent, lambda m: None)
    new = agent.session.session_dir / new_id / "subagents"
    assert sorted(p.name for p in new.iterdir()) == sorted(p.name for p in old.iterdir())
    assert (new / "1.jsonl").read_bytes() == (old / "1.jsonl").read_bytes()
    agent.subagents.clear()
    sync_subagents_for_session(agent)
    assert [t.index for t in agent.subagents.all_tasks()] == [1]
