"""The Context panel's MCP row: deferred tools cost 0 tokens there, so they must not be in its count."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.config import Settings
from axon.features import workbench_data as wd
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.tools.base import Tool
from axon.tools.interactive import ToolSearchTool
from fakes import FakeProvider


@pytest.fixture(autouse=True)
def _axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))


class _Fake(Tool):
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    description = "does a thing"

    def __init__(self, n: str) -> None:
        self.name = n

    def run(self, args, ctx):  # pragma: no cover
        return None


def test_mcp_row_counts_only_loaded_tools(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    s = Settings(workspace=ws, mode="default")
    agent = Agent(provider=FakeProvider([]), tools=create_default_registry(), permissions=PermissionEngine(s),
                  context=ContextManager(s), session=SessionStore(ws), ledger=Ledger(), settings=s)
    agent.registry.register(ToolSearchTool([_Fake(f"mcp__big__t{i}") for i in range(5)]))
    data = wd.context_breakdown(agent)
    groups = data["lists"]["mcp_tools"]
    assert data["deferred"]["mcp_count"] == 5 and groups[0]["count"] == 5
    assert sum(g["loaded"] for g in groups) == 0
    assert next(r for r in data["rows"] if r["key"] == "mcp_tools")["tokens"] == 0
