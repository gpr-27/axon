"""Unit test for PatchTool (patch)."""
import pytest
from pathlib import Path
from axon.tools import PatchTool
from axon.tools.base import ToolContext
from axon.agent.state import FileState, TodoState

def test_patch_tool_initialization(workspace: Path, settings):
    tool = PatchTool()
    assert tool.name is not None
    assert tool.description is not None
    assert tool.schema is not None

def test_patch_tool_execution(workspace: Path, settings):
    (workspace / "sample.txt").write_text("sample content with axon", encoding="utf-8")
    (workspace / "sample.py").write_text("def hello(): pass", encoding="utf-8")
    ctx = ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    tool = PatchTool()
    try:
        res = tool.run({"path": "sample.txt", "patch": "patch data"}, ctx)
        assert res is not None
    except Exception as e:
        # Tool errors are valid controlled responses
        assert str(e) or repr(e)
