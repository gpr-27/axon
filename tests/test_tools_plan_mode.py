"""EnterPlanMode flips the agent into plan mode, where mutating tools are denied."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry
from axon.tools.base import ToolContext


class FakeAgent:
    is_subagent = False

    def __init__(self, ws):
        self.settings = Settings(workspace=ws, mode="default")
        self.permissions = PermissionEngine(self.settings)

    def set_mode(self, mode):
        self.settings = self.settings.model_copy(update={"mode": mode})
        self.permissions.settings = self.settings


def test_enter_plan_mode(workspace: Path):
    reg = create_default_registry()
    t = reg.get("EnterPlanMode")
    assert t.readonly and t.default_permission == "allow"
    ag = FakeAgent(workspace)
    ctx = ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=ag.settings, agent=ag)
    assert ag.permissions.check(reg.get("Write"), {"path": "a", "content": "x"}).outcome == "ask"
    assert "Entered plan mode" in t.run({}, ctx)
    assert ag.settings.mode == "plan"
    assert ag.permissions.check(reg.get("Write"), {"path": "a", "content": "x"}).outcome == "deny"
    assert t.run({}, ctx) == "Already in plan mode."


def test_requires_agent_and_not_subagent(workspace: Path):
    t = create_default_registry().get("EnterPlanMode")
    ctx = ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))
    with pytest.raises(ToolError):
        t.run({}, ctx)
    ag = FakeAgent(workspace)
    ag.is_subagent = True
    ctx.agent = ag
    with pytest.raises(ToolError, match="subagent"):
        t.run({}, ctx)
