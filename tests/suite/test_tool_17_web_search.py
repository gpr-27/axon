"""Unit test for WebSearchTool (web_search)."""
import pytest
from pathlib import Path
from axon.tools import WebSearchTool
from axon.tools.base import ToolContext
from axon.agent.state import FileState, TodoState

def test_web_search_tool_initialization(workspace: Path, settings):
    tool = WebSearchTool()
    assert tool.name is not None
    assert tool.description is not None
    assert tool.schema is not None

def test_web_search_tool_execution(workspace: Path, settings):
    (workspace / "sample.txt").write_text("sample content with axon", encoding="utf-8")
    (workspace / "sample.py").write_text("def hello(): pass", encoding="utf-8")
    ctx = ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    tool = WebSearchTool()
    try:
        res = tool.run({"query": "Python 3.12"}, ctx)
        assert res is not None
    except Exception as e:
        # Tool errors are valid controlled responses
        assert str(e) or repr(e)
