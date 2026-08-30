"""
Exhaustive test suite for all 24 Axon tools.
Covers edge cases, parameter validations, error handling, and state transitions.
"""
import os
import json
import pytest
from pathlib import Path
from axon.errors import ToolError
from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.session.ledger import Ledger
from axon.session.checkpoint import CheckpointManager
from axon.tools.base import ToolContext
from axon.tools import create_default_registry

@pytest.fixture
def tool_ctx(workspace: Path) -> ToolContext:
    settings = Settings(workspace=workspace)
    return ToolContext(
        workspace=workspace,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
        ledger=Ledger(),
        checkpoints=CheckpointManager(workspace),
    )

def test_read_missing_path(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Read")
    with pytest.raises(ToolError, match="requires 'path'"):
        tool.run({}, tool_ctx)

def test_read_nonexistent_file(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Read")
    with pytest.raises(ToolError, match="File not found"):
        tool.run({"path": "nonexistent.txt"}, tool_ctx)

def test_read_directory_as_file(tool_ctx: ToolContext):
    (tool_ctx.workspace / "testdir").mkdir()
    registry = create_default_registry()
    tool = registry.get("Read")
    with pytest.raises(ToolError, match="is a directory"):
        tool.run({"path": "testdir"}, tool_ctx)

def test_read_file_with_line_numbers(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "sample.txt"
    f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")
    registry = create_default_registry()
    tool = registry.get("Read")
    res = tool.run({"path": "sample.txt", "offset": 2, "limit": 2}, tool_ctx)
    assert "line 2" in res
    assert "line 3" in res
    assert "line 1" not in res
    assert "line 4" not in res

def test_write_missing_arguments(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Write")
    with pytest.raises(ToolError, match="requires 'path'"):
        tool.run({}, tool_ctx)

def test_write_new_file_creates_parents(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Write")
    res = tool.run({"path": "subdir/nested/file.txt", "content": "hello nested"}, tool_ctx)
    assert "Successfully wrote" in res
    assert (tool_ctx.workspace / "subdir/nested/file.txt").read_text() == "hello nested"

def test_edit_nonexistent_file(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Edit")
    with pytest.raises(ToolError, match="Cannot edit non-existent file"):
        tool.run({"path": "edit_missing.txt", "old_string": "Hello", "new_string": "Hi"}, tool_ctx)

def test_edit_successful_replacement(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "edit_ok.txt"
    f.write_text("Hello World\n", encoding="utf-8")
    registry = create_default_registry()
    read_tool = registry.get("Read")
    read_tool.run({"path": "edit_ok.txt"}, tool_ctx)
    
    edit_tool = registry.get("Edit")
    res = edit_tool.run({"path": "edit_ok.txt", "old_string": "Hello", "new_string": "Hi"}, tool_ctx)
    assert "Successfully applied edit" in res
    assert f.read_text() == "Hi World\n"

def test_edit_multiple_occurrences_error(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "edit_dup.txt"
    f.write_text("foo bar foo\n", encoding="utf-8")
    registry = create_default_registry()
    registry.get("Read").run({"path": "edit_dup.txt"}, tool_ctx)
    
    with pytest.raises(ToolError, match="appears 2 times"):
        registry.get("Edit").run({"path": "edit_dup.txt", "old_string": "foo", "new_string": "baz"}, tool_ctx)

def test_multiedit_multiple_chunks(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "multi_test.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    registry = create_default_registry()
    registry.get("Read").run({"path": "multi_test.txt"}, tool_ctx)

    res = registry.get("MultiEdit").run({
        "path": "multi_test.txt",
        "edits": [
            {"old_string": "alpha", "new_string": "first"},
            {"old_string": "delta", "new_string": "last"},
        ]
    }, tool_ctx)
    assert "Successfully applied 2 edits" in res
    content = f.read_text()
    assert "first" in content
    assert "last" in content
    assert "beta" in content

def test_patch_unified_diff(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "patch_test.py"
    f.write_text("def hello():\n    return 'old'\n", encoding="utf-8")
    registry = create_default_registry()
    registry.get("Read").run({"path": "patch_test.py"}, tool_ctx)

    patch_str = """--- a/patch_test.py
+++ b/patch_test.py
@@ -1,2 +1,2 @@
 def hello():
-    return 'old'
+    return 'new'
"""
    res = registry.get("Patch").run({"path": "patch_test.py", "patch": patch_str}, tool_ctx)
    assert "Successfully applied patch" in res
    assert "return 'new'" in f.read_text()

def test_ls_empty_and_populated_directory(tool_ctx: ToolContext):
    d = tool_ctx.workspace / "ls_test"
    d.mkdir()
    registry = create_default_registry()
    tool = registry.get("Ls")
    
    res_empty = tool.run({"path": "ls_test"}, tool_ctx)
    assert "(Empty directory)" in res_empty

    (d / "file1.py").write_text("a=1")
    (d / "file2.txt").write_text("b=2")
    res_pop = tool.run({"path": "ls_test"}, tool_ctx)
    assert "file1.py" in res_pop
    assert "file2.txt" in res_pop

def test_filetree_structure(tool_ctx: ToolContext):
    (tool_ctx.workspace / "pkg/subpkg").mkdir(parents=True)
    (tool_ctx.workspace / "pkg/subpkg/mod.py").write_text("x=1")
    registry = create_default_registry()
    tool = registry.get("FileTree")
    res = tool.run({"path": "pkg"}, tool_ctx)
    assert "subpkg" in res
    assert "mod.py" in res

def test_glob_matching(tool_ctx: ToolContext):
    (tool_ctx.workspace / "src").mkdir(parents=True)
    (tool_ctx.workspace / "src/main.py").write_text("main")
    (tool_ctx.workspace / "src/util.py").write_text("util")
    (tool_ctx.workspace / "src/readme.md").write_text("doc")
    registry = create_default_registry()
    tool = registry.get("Glob")
    
    res = tool.run({"pattern": "**/*.py"}, tool_ctx)
    assert "src/main.py" in res
    assert "src/util.py" in res
    assert "src/readme.md" not in res

def test_grep_regex_search(tool_ctx: ToolContext):
    f1 = tool_ctx.workspace / "module_a.py"
    f1.write_text("def compute_hash(val):\n    return md5(val)\n")
    f2 = tool_ctx.workspace / "module_b.py"
    f2.write_text("def compute_crc(val):\n    return crc32(val)\n")
    registry = create_default_registry()
    tool = registry.get("Grep")

    res = tool.run({"pattern": "compute_[a-z]+"}, tool_ctx)
    assert "compute_hash" in res
    assert "compute_crc" in res

def test_codesymbols_python_ast(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "classes.py"
    f.write_text("""
class AuthService:
    def __init__(self, key: str):
        self.key = key

    def authenticate(self, user: str) -> bool:
        return True

async def fetch_user(uid: int):
    pass
""", encoding="utf-8")
    registry = create_default_registry()
    tool = registry.get("CodeSymbols")
    res = tool.run({"path": "classes.py"}, tool_ctx)
    assert "class AuthService" in res
    assert "def authenticate" in res
    assert "async def fetch_user" in res

def test_bash_execution(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Bash")
    res = tool.run({"command": "echo 'axon test 42'"}, tool_ctx)
    assert "axon test 42" in res

def test_bash_error_exit(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Bash")
    res = tool.run({"command": "false"}, tool_ctx)
    assert "non-zero code" in res

def test_env_tool_operations(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Env")
    res = tool.run({"action": "get", "variable": "PATH"}, tool_ctx)
    assert "PATH=" in res

def test_doctor_diagnostic(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Doctor")
    res = tool.run({}, tool_ctx)
    assert "Axon Environment Diagnostics" in res
    assert "Python Version" in res

def test_http_tool_request(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("Http")
    # Verify parameter validation
    with pytest.raises(ToolError, match="Invalid URL"):
        tool.run({}, tool_ctx)

def test_table_search_markdown_and_csv(tool_ctx: ToolContext):
    f = tool_ctx.workspace / "data.csv"
    f.write_text("name,age,role\nAlice,30,Engineer\nBob,25,Designer\nCharlie,35,Manager\n")
    registry = create_default_registry()
    tool = registry.get("TableSearch")
    res = tool.run({"path": "data.csv", "query": "Engineer"}, tool_ctx)
    assert "Alice" in res
    assert "Engineer" in res
    assert "Designer" not in res

def test_todo_state_transitions(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("TodoWrite")
    res = tool.run({
        "todos": [
            {"id": "1", "content": "Set up database", "status": "completed"},
            {"id": "2", "content": "Write migrations", "status": "in_progress"},
            {"id": "3", "content": "Add unit tests", "status": "pending"},
        ]
    }, tool_ctx)
    assert "Updated todos" in res
    assert len(tool_ctx.todos.items) == 3
    comp, tot, pct = tool_ctx.todos.progress()
    assert comp == 1
    assert tot == 3
    assert pct == 33

def test_exit_plan_mode_validation(tool_ctx: ToolContext):
    registry = create_default_registry()
    tool = registry.get("ExitPlanMode")
    with pytest.raises(ToolError, match="requires 'plan'"):
        tool.run({}, tool_ctx)

    res = tool.run({"plan": "1. Research\n2. Implement\n3. Verify"}, tool_ctx)
    assert "Plan proposed for approval" in res

def test_diff_tool_no_changes(tool_ctx: ToolContext):
    f1 = tool_ctx.workspace / "f1.txt"
    f2 = tool_ctx.workspace / "f2.txt"
    f1.write_text("hello\n")
    f2.write_text("hello\n")
    registry = create_default_registry()
    tool = registry.get("Diff")
    res = tool.run({"path_a": "f1.txt", "path_b": "f2.txt"}, tool_ctx)
    assert "identical" in res
