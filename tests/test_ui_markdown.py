from __future__ import annotations
import io
import sys
from axon.ui.markdown import format_markdown, render_table, str_width, char_width
from axon.ui.render import (
    Renderer,
    get_file_icon,
    render_diff_box,
    render_read_box,
    render_ls_box,
    render_grep_box,
    render_glob_box,
    render_write_box,
    render_bash_box,
    render_doctor_box,
    render_todo_box,
    render_error_box,
    render_web_search_box,
    render_web_fetch_box,
    render_git_box,
    render_symbols_box,
    render_tree_box,
    render_http_box,
    render_process_box,
    render_patch_box,
    render_env_box,
    render_diff_tool_box,
    make_clickable_link,
    get_last_tool_output,
)
from axon.ui.theme import strip_ansi
from axon.providers.base import (
    Usage,
    ToolBatchStart,
    ToolExecutionStart,
    ToolExecutionResult,
    ToolUseBlock,
    LLMCallStart,
)

def test_char_and_str_width():
    assert char_width("a") == 1
    assert char_width("✅") == 2
    assert str_width("hello") == 5
    assert str_width("\x1b[1m\x1b[97mhello\x1b[0m") == 5
    assert str_width("\x1b]8;;file:///foo/bar\x1b\\link\x1b]8;;\x1b\\") == 4

def test_render_table_structure():
    table_raw = [
        "| Name | Status | Description |",
        "| :--- | :---: | ---: |",
        "| `foo.py` | ✅ Ready | Python file |",
        "| `bar.cpp` | ⏳ Pending | C++ file |",
    ]
    lines = render_table(table_raw, max_total_width=80)
    assert len(lines) >= 5
    assert "┌" in lines[0] and "┐" in lines[0]
    assert "Name" in lines[1] and "Status" in lines[1]
    assert "├" in lines[2] and "┼" in lines[2]
    assert "└" in lines[-1] and "┘" in lines[-1]

def test_format_markdown_checklists_and_callouts():
    text = """
### Checklist
- [x] Done task
- [ ] Todo task

> [!NOTE] This is important context.
"""
    formatted = format_markdown(text)
    assert "☑" in formatted
    assert "☐" in formatted
    assert "ℹ Note:" in formatted

def test_renderer_user_message_and_footer(capsys):
    renderer = Renderer()
    renderer.render_user_message("hello world")
    captured = capsys.readouterr()
    assert "› hello world" in strip_ansi(captured.out)

    usage = Usage(input=1000, output=200, cache_read=500)
    renderer.turn_footer(tool_count=2, usage=usage, cost=0.0123, elapsed=1.5, llm_calls=1)
    captured2 = capsys.readouterr()
    plain2 = strip_ansi(captured2.out)
    assert "Worked for 1.5s" in plain2
    assert "2 tool calls" in plain2
    assert "1 LLM call" in plain2
    assert "$0.0123" in plain2

def test_file_icons_and_diff():
    assert get_file_icon("main.py") == "🐍"
    assert get_file_icon("config.toml") == "⚙️"
    assert get_file_icon("solution.cpp") == "⚡"
    assert get_file_icon("README.md") == "📄"
    assert get_file_icon("app/src") == "📁"

    diff_str = render_diff_box("line1\nline2", "line1\nline2_edited\nline3", filename="test.py")
    plain_diff = strip_ansi(diff_str)
    assert "┌── Diff · 🐍 test.py (+2 / -1 lines)" in plain_diff
    assert "+line2_edited" in plain_diff
    assert "-line2" in plain_diff

def test_specialized_boxes():
    # Read box
    read_box = render_read_box("test.py", "     1→import os\n     2→print(os.getcwd())")
    plain_read = strip_ansi(read_box)
    assert "File Content · 🐍 test.py (2 lines" in plain_read
    assert "1→import os" in plain_read
    assert "Read 2 lines" in plain_read

    # Ls box
    ls_box = render_ls_box("src", ".axon/\naxon/\nREADME.md\nmain.py")
    plain_ls = strip_ansi(ls_box)
    assert "Directory Listing · 📁 src (4 items)" in plain_ls
    assert "📁 .axon/" in plain_ls
    assert "🐍 main.py" in plain_ls  # 4 items <= head(4)+tail(4), all visible
    assert "Found 4 items" in plain_ls



    # Grep box
    grep_box = render_grep_box("run_turn", "src/loop.py:78:def run_turn(self):")
    plain_grep = strip_ansi(grep_box)
    assert "Grep Matches · \"run_turn\" (1 matches)" in plain_grep
    assert "src/loop.py:78" in plain_grep
    assert "Found 1 matches" in plain_grep

    # Glob box
    glob_box = render_glob_box("*.py", "main.py\nsetup.py")
    plain_glob = strip_ansi(glob_box)
    assert "Glob Matches · 📁 *.py (2 files)" in plain_glob
    assert "🐍 main.py" in plain_glob
    assert "Found 2 items" in plain_glob

    # Write box
    write_box = render_write_box("new_file.py", "import sys\nprint('hi')")
    plain_write = strip_ansi(write_box)
    assert "Created File · 🐍 new_file.py" in plain_write
    assert "Saved new_file.py" in plain_write

    # Bash box
    bash_box = render_bash_box("total 136\ndrwxr-xr-x  11 gpr  staff  352")
    plain_bash = strip_ansi(bash_box)
    assert "Output · 2 lines" in plain_bash
    assert "drwxr-xr-x" in plain_bash

    # Doctor box
    doc_box = render_doctor_box("Python Version : 3.14.5\nStatus : Healthy")
    plain_doc = strip_ansi(doc_box)
    assert "Axon System Diagnostics" in plain_doc
    assert "Python Version" in plain_doc

    # Todo box
    todo_box = render_todo_box("[✓] 1. First item\n[▶] 2. Second item\n[ ] 3. Third item")
    plain_todo = strip_ansi(todo_box)
    assert "Task Plan" in plain_todo
    assert "1/3 completed (33%)" in plain_todo
    assert "First item" in plain_todo

    # WebSearch badge
    search_box = render_web_search_box("python docs", "1. Python Documentation\n   URL: https://docs.python.org\n   Snippet: Official docs")
    plain_search = strip_ansi(search_box)
    assert "Found 1 search result" in plain_search
    assert "python docs" in plain_search

    # WebFetch badge
    fetch_box = render_web_fetch_box("https://docs.python.org", "[WebFetch Result from https://docs.python.org]:\n\nWelcome to Python documentation.\nPython is a programming language.")
    plain_fetch = strip_ansi(fetch_box)
    assert "Fetched" in plain_fetch
    assert "docs.python.org" in plain_fetch

    # Git box
    git_box = render_git_box("status", "## main...origin/main\n M src/axon/ui/render.py\n?? tests/new_test.py")
    plain_git = strip_ansi(git_box)
    assert "Git status" in plain_git
    assert "## main" in plain_git

    # Symbols box
    sym_box = render_symbols_box("models.py", "📄 models.py:\n  class User [L1-10]\n  • def save() [L5-8]")
    plain_sym = strip_ansi(sym_box)
    assert "Code Symbols · models.py" in plain_sym
    assert "class User" in plain_sym

    # Tree box
    tree_box = render_tree_box("axon/\n├── agent/\n└── ui/")
    plain_tree = strip_ansi(tree_box)
    assert "Directory Tree" in plain_tree
    assert "agent/" in plain_tree

    # HTTP badge
    http_box = render_http_box("GET", "https://api.github.com/zen", "HTTP 200 OK\nKeep it logically awesome.")
    plain_http = strip_ansi(http_box)
    assert "HTTP GET" in plain_http
    assert "api.github.com" in plain_http

    # Process box
    proc_box = render_process_box("ports", "Listening TCP Ports:\npython 8000 LISTEN")
    plain_proc = strip_ansi(proc_box)
    assert "Process Info · ports" in plain_proc

    # Patch box
    patch_box = render_patch_box("main.py", "Successfully applied patch to main.py.")
    plain_patch = strip_ansi(patch_box)
    assert "Successfully applied patch" in plain_patch

def test_renderer_tool_execution_events(capsys):
    renderer = Renderer()

    # Turn iteration 1 start
    renderer.on_event(LLMCallStart(iteration=1, max_iterations=25, model="deepseek-v4-flash", message_count=5))
    captured = capsys.readouterr()
    assert "1st LLM Call" in strip_ansi(captured.out)

    # 1st Tool Call
    renderer.on_event(ToolExecutionStart(id="t1", name="Ls", input={"path": "."}))
    renderer.on_event(ToolExecutionResult(id="t1", name="Ls", input={"path": "."}, content="axon/\ncheck_models.py"))
    captured1 = capsys.readouterr()
    plain1 = strip_ansi(captured1.out)
    assert "1st Tool Call · 🛠️ Ls ❯ Listed directory entries: 📁 ." in plain1
    assert "Directory Listing · 📁 . (2 items)" in plain1
    assert "🐍 check_models.py" in plain1

    # 2nd Tool Call (Bash)
    renderer.on_event(ToolExecutionStart(id="t2", name="Bash", input={"command": "python3 -m pytest"}))
    renderer.on_event(ToolExecutionResult(id="t2", name="Bash", input={"command": "python3 -m pytest"}, content="14 passed in 0.1s"))
    captured2 = capsys.readouterr()
    plain2 = strip_ansi(captured2.out)
    assert "2nd Tool Call · 🛠️ Bash ❯ Ran command: python3 -m pytest" in plain2
    assert "14 passed in 0.1s" in plain2
    assert "┌── Output" in plain2

    # 3rd Tool Call (Read)
    renderer.on_event(ToolExecutionStart(id="t3", name="Read", input={"path": "src/axon/ui/render.py", "offset": 1, "limit": 100}))
    renderer.on_event(ToolExecutionResult(id="t3", name="Read", input={"path": "src/axon/ui/render.py"}, content="     1→line\n"*100))
    captured3 = capsys.readouterr()
    plain3 = strip_ansi(captured3.out)
    assert "3rd Tool Call · 🛠️ Read ❯ Read file: 🐍 src/axon/ui/render.py #L1-100" in plain3
    assert "File Content · 🐍 render.py (100 lines" in plain3
    assert "Read 100 lines" in plain3

    # Output persistence
    assert "line" in get_last_tool_output()

    # 2nd LLM Call
    renderer.on_event(LLMCallStart(iteration=2, max_iterations=25, model="deepseek-v4-flash", message_count=8))
    captured5 = capsys.readouterr()
    plain5 = strip_ansi(captured5.out)
    assert "2nd LLM Call" in plain5
    assert "deepseek-v4-flash" in plain5

