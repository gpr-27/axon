"""
Unit tests for Tool suite operations and safety invariants.
"""
import pytest
from pathlib import Path
from axon.errors import StaleFileError, PermissionDenied
from axon.tools.base import ToolContext
from axon.tools.fs_read import ReadTool
from axon.tools.fs_write import WriteTool, EditTool, MultiEditTool
from axon.tools.search import GlobTool, GrepTool, LsTool
from axon.tools.todo import TodoWriteTool
from axon.agent.state import FileState, TodoState

def test_read_and_edit_lifecycle(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    test_file = workspace / "sample.py"
    test_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    read_tool = ReadTool()
    edit_tool = EditTool()

    # 1. Edit fails without reading first (Read-before-edit invariant)
    with pytest.raises(StaleFileError) as exc:
        edit_tool.run({"path": "sample.py", "old_string": "'world'", "new_string": "'axon'"}, ctx)
    assert "You have not read" in str(exc.value)

    # 2. Read the file
    out = read_tool.run({"path": "sample.py"}, ctx)
    assert "def hello():" in out

    # 3. Edit succeeds now
    edit_res = edit_tool.run({"path": "sample.py", "old_string": "'world'", "new_string": "'axon'"}, ctx)
    assert "Successfully applied edit" in edit_res
    assert test_file.read_text(encoding="utf-8") == "def hello():\n    return 'axon'\n"

def test_path_jail_escape_attempt(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    read_tool = ReadTool()
    with pytest.raises(PermissionDenied):
        read_tool.run({"path": "/etc/hosts"}, ctx)

    with pytest.raises(PermissionDenied):
        read_tool.run({"path": "~/.ssh/id_rsa"}, ctx)

def test_glob_and_grep_and_ls(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    (workspace / "a.py").write_text("x = 100\ndef find_me(): pass\n", encoding="utf-8")
    (workspace / "b.txt").write_text("other text", encoding="utf-8")

    glob_tool = GlobTool()
    grep_tool = GrepTool()
    ls_tool = LsTool()

    # Glob
    glob_res = glob_tool.run({"pattern": "*.py"}, ctx)
    assert "a.py" in glob_res
    assert "b.txt" not in glob_res

    # Grep
    grep_res = grep_tool.run({"pattern": "find_me"}, ctx)
    assert "a.py:2:def find_me(): pass" in grep_res

    # Ls
    ls_res = ls_tool.run({}, ctx)
    assert "a.py" in ls_res
    assert "b.txt" in ls_res

def test_todo_write_constraint(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    todo_tool = TodoWriteTool()
    # At most 1 in_progress
    res = todo_tool.run({
        "todos": [
            {"id": "1", "content": "task 1", "status": "completed"},
            {"id": "2", "content": "task 2", "status": "in_progress"},
            {"id": "3", "content": "task 3", "status": "pending"},
        ]
    }, ctx)
    assert "[✓] 1. task 1" in res or "[x] 1: task 1" in res
    assert "[▶] 2. task 2" in res or "[>] 2: task 2" in res
    assert "[ ] 3. task 3" in res or "[ ] 3: task 3" in res
    assert "1/3 (33%)" in res

    # 2 in_progress should error
    with pytest.raises(Exception):
        todo_tool.run({
            "todos": [
                {"id": "1", "content": "task 1", "status": "in_progress"},
                {"id": "2", "content": "task 2", "status": "in_progress"},
            ]
        }, ctx)

def test_code_symbols_and_file_tree(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    code_file = workspace / "models.py"
    code_file.write_text("""
class UserProfile:
    \"\"\"Represents a user account profile.\"\"\"
    def __init__(self, username: str):
        self.username = username

    def get_display_name(self) -> str:
        return self.username.capitalize()

def calculate_metric(values: list[float]) -> float:
    \"\"\"Compute average.\"\"\"
    return sum(values) / len(values)
""", encoding="utf-8")

    from axon.tools.code_symbols import CodeSymbolsTool
    from axon.tools.file_tree import FileTreeTool

    symbols_tool = CodeSymbolsTool()
    res = symbols_tool.run({"path": "models.py"}, ctx)
    assert "class UserProfile" in res
    assert "def __init__(self, username)" in res
    assert "def get_display_name(self)" in res
    assert "def calculate_metric(values)" in res

    tree_tool = FileTreeTool()
    tree_res = tree_tool.run({"path": "."}, ctx)
    assert "models.py" in tree_res
    assert "directories" in tree_res

def test_patch_tool_lifecycle(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    target_file = workspace / "script.py"
    target_file.write_text("def run():\n    print('old')\n", encoding="utf-8")

    # Read first to satisfy file state
    read_tool = ReadTool()
    read_tool.run({"path": "script.py"}, ctx)

    from axon.tools.patch import PatchTool
    patch_tool = PatchTool()

    patch_str = """--- a/script.py
+++ b/script.py
@@ -1,2 +1,2 @@
 def run():
-    print('old')
+    print('new_axon')
"""
    res = patch_tool.run({"path": "script.py", "patch": patch_str}, ctx)
    assert "Successfully applied patch" in res
    assert target_file.read_text(encoding="utf-8") == "def run():\n    print('new_axon')\n"

def test_git_and_process_and_web_search(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    from axon.tools.git import GitTool
    from axon.tools.process_tool import ProcessTool
    from axon.tools.web_search import WebSearchTool

    git_tool = GitTool()
    git_res = git_tool.run({"subcommand": "status"}, ctx)
    assert "Git" in git_res or "status" in git_res or "not a git repository" in git_res

    proc_tool = ProcessTool()
    proc_res = proc_tool.run({"action": "list"}, ctx)
    assert "PID" in proc_res or "%CPU" in proc_res or "COMMAND" in proc_res

    web_tool = WebSearchTool()
    web_res = web_tool.run({"query": "python async await"}, ctx)
    assert "web" in web_res.lower() or "python" in web_res.lower() or "search" in web_res.lower()

def test_env_and_diff_tools(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    from axon.tools.env_tool import EnvTool
    from axon.tools.diff_tool import DiffTool

    env_tool = EnvTool()
    res_list = env_tool.run({"action": "list"}, ctx)
    assert "PATH=" in res_list or "HOME=" in res_list

    res_check = env_tool.run({"action": "check", "variable": "PATH"}, ctx)
    assert "SET" in res_check

    fa = workspace / "a.txt"
    fb = workspace / "b.txt"
    fa.write_text("line1\nline2\n", encoding="utf-8")
    fb.write_text("line1\nline2_mod\nline3\n", encoding="utf-8")

    diff_tool = DiffTool()
    diff_res = diff_tool.run({"path_a": "a.txt", "path_b": "b.txt"}, ctx)
    assert "+line2_mod" in diff_res
    assert "-line2" in diff_res

def test_bundled_skills_catalog(workspace: Path):
    from axon.skills.manager import SkillManager
    from axon.skills.interactive import handle_skills_command
    mgr = SkillManager(workspace)
    assert len(mgr.skills) >= 16
    assert "subagent-fanout" in mgr.skills
    assert "refactor" in mgr.skills
    assert "security-audit" in mgr.skills
    assert "test-gen" in mgr.skills
    assert "optimize" in mgr.skills
    assert "deep-research" in mgr.skills
    assert "table-search" in mgr.skills
    assert "skill-creator" in mgr.skills
    assert "api-tester" in mgr.skills
    assert "docker" in mgr.skills
    assert "db-migration" in mgr.skills
    assert "frontend-ui" in mgr.skills
    assert "benchmark" in mgr.skills

    # Test creating custom skill via wizard
    handle_skills_command(mgr, workspace, "create my-custom-skill tester")
    assert "my-custom-skill" in mgr.skills
    custom_skill = mgr.skills["my-custom-skill"]
    assert custom_skill.scope == "project"

    # Test deleting custom skill
    handle_skills_command(mgr, workspace, "delete my-custom-skill")
    assert "my-custom-skill" not in mgr.skills

def test_deep_research_and_table_search_tools(workspace: Path, settings):
    file_state = FileState()
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=file_state, todos=todos, settings=settings)

    from axon.tools.deep_research import DeepResearchTool
    from axon.tools.table_search import TableSearchTool

    # Test DeepResearchTool
    dr_tool = DeepResearchTool()
    res_dr = dr_tool.run({"query": "DFA vs NFA algorithms", "depth": "deep", "max_sources": 3, "save_report": True}, ctx)
    assert "Deep Research Completed" in res_dr
    assert "Executive Summary" in res_dr
    assert "Comparative Analysis" in res_dr

    # Test TableSearchTool on markdown table
    doc_path = workspace / "comparison.md"
    doc_path.write_text("""
# Comparison
| Algorithm | Time Complexity | Space Complexity |
|---|---|---|
| QuickSort | O(N log N) | O(log N) |
| MergeSort | O(N log N) | O(N) |
| BubbleSort | O(N^2) | O(1) |
""", encoding="utf-8")

    ts_tool = TableSearchTool()
    res_ts = ts_tool.run({"query": "MergeSort", "path": "comparison.md"}, ctx)
    assert "MergeSort" in res_ts
    assert "Time Complexity" in res_ts

def test_memory_store_and_skill_importer(workspace: Path):
    from axon.agent.memory import MemoryStore
    from axon.skills.importer import parse_github_skill_url

    # MemoryStore tests
    store = MemoryStore(workspace)
    item = store.learn("Always maintain 100% test coverage with pytest", category="conventions")
    assert item.id is not None
    assert "pytest" in item.content

    all_items = store.list_all()
    assert len(all_items) == 1
    assert all_items[0].title == "Always maintain 100% test coverage with pytest"

    # Delete memory
    deleted = store.delete(item.id)
    assert deleted is True
    assert len(store.list_all()) == 0

    # GitHub Skill URL parser tests
    owner, repo, ref, subpath = parse_github_skill_url("anthropics/anthropic-quickstarts/skills/rag")
    assert owner == "anthropics"
    assert repo == "anthropic-quickstarts"
    assert subpath == "skills/rag"

