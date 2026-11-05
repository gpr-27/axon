"""Subagent lifecycle regressions: failed starts must not leave a task 'running' forever."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.subagent import SubagentManager, run_subagent
from axon.errors import ToolError
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


def test_agent_with_no_usable_tools_does_not_stay_running(workspace: Path) -> None:
    agents_dir = workspace / ".axon" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "toolless.md").write_text(
        "---\nname: toolless\ndescription: asks for a tool that does not exist\ntools: NoSuchTool\n---\nDo things.",
        encoding="utf-8")
    parent = make_agent(FakeProvider([]), workspace)
    parent.session.open("sess_p")

    with pytest.raises(ToolError):
        run_subagent("work", parent, subagent_type="toolless")

    task = parent.subagents.all_tasks()[0]
    assert task.status == "error" and "zero tools" in (task.error_msg or "")
    assert parent.subagents.running_count() == 0


def test_resumed_subagent_gets_a_fresh_resume_budget() -> None:
    manager = SubagentManager()
    task = manager.register("do it")
    task.resumes = 2
    manager.complete(task.id, "done", task.conversation)

    manager.reopen(task, max_steps=10)

    assert task.resumes == 0 and task.status == "running"
