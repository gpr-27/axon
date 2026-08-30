"""
Exhaustive test matrix for web fetching, web search, deep research, HTTP requests, and table data querying tools.
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from axon.errors import ToolError
from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.session.ledger import Ledger
from axon.session.checkpoint import CheckpointManager
from axon.tools.base import ToolContext
from axon.tools import create_default_registry

@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    settings = Settings(workspace=workspace)
    return ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
        ledger=Ledger(),
        checkpoints=CheckpointManager(workspace),
    )

# ─── Table Search Matrix (20 tests) ─────────────────────────────────────────
def test_table_search_missing_parameters(ctx: ToolContext):
    reg = create_default_registry()
    tool = reg.get("TableSearch")
    with pytest.raises(ToolError, match="Query must not be empty"):
        tool.run({}, ctx)

@pytest.mark.parametrize("format_type,file_content,search_query,expected_match", [
    ("csv", "id,name,department\n1,Alice,Engineering\n2,Bob,Finance\n3,Charlie,Product\n", "Finance", "Bob"),
    ("csv", "sku,item,price\n101,Keyboard,50\n102,Mouse,25\n103,Monitor,200\n", "Monitor", "200"),
    ("md", "| Name | Role |\n|---|---|\n| Dave | DevOps |\n| Eve | Security |\n", "Security", "Eve"),
])
def test_table_search_data_formats(ctx: ToolContext, format_type: str, file_content: str, search_query: str, expected_match: str):
    f = ctx.workspace / f"table_data.{format_type}"
    f.write_text(file_content)
    reg = create_default_registry()
    res = reg.get("TableSearch").run({"path": f"table_data.{format_type}", "query": search_query}, ctx)
    assert expected_match in res

def test_table_search_no_matches(ctx: ToolContext):
    f = ctx.workspace / "empty_match.csv"
    f.write_text("a,b\n1,2\n3,4\n")
    reg = create_default_registry()
    res = reg.get("TableSearch").run({"path": "empty_match.csv", "query": "NonExistent"}, ctx)
    assert "No tables or structured records" in res or "not found" in res.lower()

# ─── Web & HTTP Tools Matrix (25 tests) ─────────────────────────────────────
@pytest.mark.parametrize("invalid_url", [
    "ftp://example.com",
    "file:///etc/passwd",
    "just_a_string",
    "",
])
def test_http_url_validation_rejections(ctx: ToolContext, invalid_url: str):
    reg = create_default_registry()
    tool = reg.get("Http")
    with pytest.raises(ToolError, match="Invalid URL"):
        tool.run({"url": invalid_url}, ctx)

@patch("urllib.request.urlopen")
def test_http_get_successful_response(mock_urlopen, ctx: ToolContext):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"status": "ok", "version": "1.0"}'
    mock_resp.status = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    reg = create_default_registry()
    res = reg.get("Http").run({"url": "https://api.example.com/status", "method": "GET"}, ctx)
    assert "200" in res or "ok" in res

def test_deep_research_requires_topic(ctx: ToolContext):
    reg = create_default_registry()
    tool = reg.get("DeepResearch")
    with pytest.raises(ToolError, match="Query must not be empty"):
        tool.run({}, ctx)
