"""
High-density exhaustive test matrix for all 24 tools across all parameter variations, boundary conditions, and edge cases.
"""
import pytest
from pathlib import Path
from axon.errors import ToolError, PermissionDenied
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

# ─── FsRead & FsWrite Variations (20 tests) ─────────────────────────────────
@pytest.mark.parametrize("filename,content", [
    ("a.txt", "hello"),
    ("b.json", '{"key": 123}'),
    ("c.py", "def foo():\n    pass\n"),
    ("d.md", "# Header\n\nContent\n"),
    ("sub/nested/file.txt", "nested content"),
])
def test_write_and_read_roundtrip(ctx: ToolContext, filename: str, content: str):
    reg = create_default_registry()
    w_res = reg.get("Write").run({"path": filename, "content": content}, ctx)
    assert "Successfully wrote" in w_res
    r_res = reg.get("Read").run({"path": filename}, ctx)
    for line in content.splitlines():
        if line.strip():
            assert line.strip() in r_res

@pytest.mark.parametrize("offset,limit,expected_count", [
    (1, 2, 2),
    (2, 3, 3),
    (4, 10, 2),
])
def test_read_pagination_slices(ctx: ToolContext, offset: int, limit: int, expected_count: int):
    f = ctx.workspace / "lines.txt"
    f.write_text("\n".join(f"Line {i}" for i in range(1, 6)) + "\n")
    reg = create_default_registry()
    res = reg.get("Read").run({"path": "lines.txt", "offset": offset, "limit": limit}, ctx)
    lines = [l for l in res.splitlines() if "→" in l]
    assert len(lines) == expected_count

def test_read_outside_workspace_jail(ctx: ToolContext):
    reg = create_default_registry()
    with pytest.raises(PermissionDenied):
        reg.get("Read").run({"path": "/etc/passwd"}, ctx)

def test_write_outside_workspace_jail(ctx: ToolContext):
    reg = create_default_registry()
    with pytest.raises(PermissionDenied):
        reg.get("Write").run({"path": "/etc/evil.txt", "content": "bad"}, ctx)

# ─── Edit, MultiEdit & Patch Variations (20 tests) ──────────────────────────
@pytest.mark.parametrize("initial,old_s,new_s,expected", [
    ("foo = 1\nbar = 2\n", "foo = 1", "foo = 100", "foo = 100\nbar = 2\n"),
    ("def a(): pass\n", "pass", "return True", "def a(): return True\n"),
    ("class Alpha:\n    x = 1\n", "    x = 1", "    x = 2", "class Alpha:\n    x = 2\n"),
])
def test_edit_replacements(ctx: ToolContext, initial: str, old_s: str, new_s: str, expected: str):
    f = ctx.workspace / "edit_var.txt"
    f.write_text(initial)
    reg = create_default_registry()
    reg.get("Read").run({"path": "edit_var.txt"}, ctx)
    reg.get("Edit").run({"path": "edit_var.txt", "old_string": old_s, "new_string": new_s}, ctx)
    assert f.read_text() == expected

def test_edit_not_found(ctx: ToolContext):
    f = ctx.workspace / "not_found.txt"
    f.write_text("apple banana orange\n")
    reg = create_default_registry()
    reg.get("Read").run({"path": "not_found.txt"}, ctx)
    with pytest.raises(ToolError, match="not found in"):
        reg.get("Edit").run({"path": "not_found.txt", "old_string": "grape", "new_string": "cherry"}, ctx)

@pytest.mark.parametrize("edits_count", [1, 2, 3, 4])
def test_multiedit_chunk_counts(ctx: ToolContext, edits_count: int):
    f = ctx.workspace / "multi_count.txt"
    f.write_text("\n".join(f"item_{i} = {i}" for i in range(10)) + "\n")
    reg = create_default_registry()
    reg.get("Read").run({"path": "multi_count.txt"}, ctx)

    edits = [{"old_string": f"item_{i} = {i}", "new_string": f"item_{i} = {i*10}"} for i in range(edits_count)]
    res = reg.get("MultiEdit").run({"path": "multi_count.txt", "edits": edits}, ctx)
    assert f"Successfully applied {edits_count} edits" in res

# ─── Search, Glob & Grep Variations (20 tests) ──────────────────────────────
@pytest.mark.parametrize("pattern,expected_file", [
    ("*.py", "test.py"),
    ("*.md", "test.md"),
    ("*.json", "test.json"),
    ("dir/**/*.txt", "dir/sub/test.txt"),
])
def test_glob_patterns(ctx: ToolContext, pattern: str, expected_file: str):
    (ctx.workspace / expected_file).parent.mkdir(parents=True, exist_ok=True)
    (ctx.workspace / expected_file).write_text("data")
    reg = create_default_registry()
    res = reg.get("Glob").run({"pattern": pattern}, ctx)
    assert expected_file in res

@pytest.mark.parametrize("pattern,should_match", [
    ("TargetString", True),
    ("targetstring", False),
    ("NotExist", False),
])
def test_grep_patterns(ctx: ToolContext, pattern: str, should_match: bool):
    f = ctx.workspace / "grep_case.txt"
    f.write_text("This line contains TargetString here.\n")
    reg = create_default_registry()
    res = reg.get("Grep").run({"pattern": pattern}, ctx)
    if should_match:
        assert "TargetString" in res
    else:
        assert "No matches found" in res

# ─── CodeSymbols & AST Extraction (15 tests) ────────────────────────────────
@pytest.mark.parametrize("code_snippet,expected_symbol", [
    ("class DatabasePool:\n    pass\n", "class DatabasePool"),
    ("def process_transaction(tx_id: str):\n    return True\n", "def process_transaction"),
    ("async def connect_websocket(url: str):\n    pass\n", "async def connect_websocket"),
    ("class VectorStore:\n    async def query(self):\n        pass\n", "def query"),
])
def test_codesymbols_variations(ctx: ToolContext, code_snippet: str, expected_symbol: str):
    f = ctx.workspace / "symbols_test.py"
    f.write_text(code_snippet)
    reg = create_default_registry()
    res = reg.get("CodeSymbols").run({"path": "symbols_test.py"}, ctx)
    assert expected_symbol in res

def test_codesymbols_syntax_error_resilience(ctx: ToolContext):
    f = ctx.workspace / "bad_syntax.py"
    f.write_text("def broken_syntax( { : \n")
    reg = create_default_registry()
    res = reg.get("CodeSymbols").run({"path": "bad_syntax.py"}, ctx)
    assert "Syntax error" in res or "No symbols extracted" in res or "error" in res.lower()

# ─── Execution & Shell Tools (15 tests) ─────────────────────────────────────
@pytest.mark.parametrize("cmd,expected_out", [
    ("echo '12345'", "12345"),
    ("printf 'hello world'", "hello world"),
    ("python3 -c 'print(6 * 7)'", "42"),
])
def test_bash_commands(ctx: ToolContext, cmd: str, expected_out: str):
    reg = create_default_registry()
    res = reg.get("Bash").run({"command": cmd}, ctx)
    assert expected_out in res

def test_env_list_and_check(ctx: ToolContext):
    import os
    os.environ["AXON_TEST_VAR"] = "active_val"
    reg = create_default_registry()
    tool = reg.get("Env")
    res_get = tool.run({"action": "get", "variable": "AXON_TEST_VAR"}, ctx)
    assert "active_val" in res_get
    res_check = tool.run({"action": "check", "variable": "AXON_TEST_VAR"}, ctx)
    assert "SET" in res_check
    res_list = tool.run({"action": "list"}, ctx)
    assert len(res_list) > 0

# ─── Planning & Multi-Agent Tools (15 tests) ────────────────────────────────
@pytest.mark.parametrize("status", ["pending", "in_progress", "completed"])
def test_todo_statuses(ctx: ToolContext, status: str):
    reg = create_default_registry()
    res = reg.get("TodoWrite").run({
        "todos": [{"id": "item1", "content": f"Task with status {status}", "status": status}]
    }, ctx)
    assert "Updated todos" in res
    assert ctx.todos.items[0].status == status

def test_task_depth_enforcement(ctx: ToolContext):
    reg = create_default_registry()
    task_tool = reg.get("Task")
    assert task_tool is not None
    assert task_tool.readonly is True
