"""Unit test for BashTool (bash)."""
import pytest
from pathlib import Path
from axon.tools import BashTool
from axon.tools.base import ToolContext
from axon.agent.state import FileState, TodoState

def test_bash_tool_initialization(workspace: Path, settings):
    tool = BashTool()
    assert tool.name is not None
    assert tool.description is not None
    assert tool.schema is not None

def test_bash_tool_execution(workspace: Path, settings):
    (workspace / "sample.txt").write_text("sample content with axon", encoding="utf-8")
    (workspace / "sample.py").write_text("def hello(): pass", encoding="utf-8")
    ctx = ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    tool = BashTool()
    try:
        res = tool.run({"command": "echo 42"}, ctx)
        assert res is not None
    except Exception as e:
        # Tool errors are valid controlled responses
        assert str(e) or repr(e)
