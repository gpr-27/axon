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
    assert "› hello world" in strip_ansi(capsys.readouterr().out)

    usage = Usage(input=1000, output=200, cache_read=500)
    renderer.turn_footer(tool_count=2, usage=usage, cost=0.0123, elapsed=1.5, llm_calls=1)
    plain2 = strip_ansi(capsys.readouterr().out)
    assert "Done · 1.5s" in plain2
    assert "2 tool calls" in plain2
    assert "1 LLM call" in plain2
    assert "$0.0123" in plain2


def test_file_icons_and_diff(monkeypatch):
    monkeypatch.delenv("AXON_EMOJI", raising=False)
    assert get_file_icon("main.py") == ""  # single-width design: no emoji unless AXON_EMOJI=1

    diff_str = render_diff_box("line1\nline2", "line1\nline2_edited\nline3", filename="test.py")
    plain_diff = strip_ansi(diff_str)
    assert "Diff · test.py +2 -1" in plain_diff
    assert "2 + line2_edited" in plain_diff
    assert "2 - line2" in plain_diff
    assert "3 + line3" in plain_diff


def test_specialized_boxes():
    plain_read = strip_ansi(render_read_box("test.py", "     1→import os\n     2→print(os.getcwd())"))
    assert "test.py 2 lines" in plain_read
    assert "1 │ import os" in plain_read
    assert "Read 2 lines" in plain_read

    plain_ls = strip_ansi(render_ls_box("src", ".axon/\naxon/\nREADME.md\nmain.py"))
    assert "src 4 items" in plain_ls
    assert ".axon/" in plain_ls and "main.py" in plain_ls
    assert "Found 4 items" in plain_ls

    plain_grep = strip_ansi(render_grep_box("run_turn", "src/loop.py:78:def run_turn(self):"))
    assert 'grep "run_turn" 1 match' in plain_grep
    assert "src/loop.py:78" in plain_grep

    plain_glob = strip_ansi(render_glob_box("*.py", "main.py\nsetup.py"))
    assert "glob *.py 2 files" in plain_glob and "main.py" in plain_glob

    plain_write = strip_ansi(render_write_box("new_file.py", "import sys\nprint('hi')"))
    assert "new_file.py 2 lines" in plain_write
    assert "Saved new_file.py" in plain_write

    plain_bash = strip_ansi(render_bash_box("total 136\ndrwxr-xr-x  11 gpr  staff  352"))
    assert "Output 2 lines" in plain_bash and "drwxr-xr-x" in plain_bash

    plain_doc = strip_ansi(render_doctor_box("Python Version : 3.14.5\nStatus : Healthy"))
    assert "Axon System Diagnostics" in plain_doc and "Python Version" in plain_doc

    plain_todo = strip_ansi(render_todo_box("[✓] 1. First item\n[▶] 2. Second item\n[ ] 3. Third item"))
    assert "Task Plan" in plain_todo
    assert "1/3 completed (33%)" in plain_todo
    assert "First item" in plain_todo

    plain_search = strip_ansi(render_web_search_box("python docs", "1. Python Documentation\n   URL: https://docs.python.org"))
    assert "Found 1 search result" in plain_search and "python docs" in plain_search

    plain_fetch = strip_ansi(render_web_fetch_box("https://docs.python.org", "Welcome to Python documentation."))
    assert "Fetched" in plain_fetch and "docs.python.org" in plain_fetch

    plain_git = strip_ansi(render_git_box("status", "## main...origin/main\n M src/axon/ui/render.py\n?? tests/new_test.py"))
    assert "Git status" in plain_git and "## main" in plain_git

    plain_sym = strip_ansi(render_symbols_box("models.py", "models.py:\n  class User [L1-10]\n  • def save() [L5-8]"))
    assert "Code Symbols models.py" in plain_sym and "class User" in plain_sym

    plain_tree = strip_ansi(render_tree_box("axon/\n├── agent/\n└── ui/"))
    assert "Directory Tree" in plain_tree and "agent/" in plain_tree

    plain_http = strip_ansi(render_http_box("GET", "https://api.github.com/zen", "HTTP 200 OK\nKeep it logically awesome."))
    assert "HTTP GET" in plain_http and "api.github.com" in plain_http

    assert "Process Info · ports" in strip_ansi(render_process_box("ports", "python 8000 LISTEN"))
    assert "Successfully applied patch" in strip_ansi(render_patch_box("main.py", "Successfully applied patch to main.py."))
    assert "HOME" in strip_ansi(render_env_box("list", "HOME=/x"))
    assert "+a" in strip_ansi(render_diff_tool_box("--- a\n+++ b\n+a\n-b"))
    assert "boom" in strip_ansi(render_error_box("Bash", "boom"))


def test_renderer_tool_execution_events(capsys):
    renderer = Renderer(trace_level="normal")
    renderer.on_event(LLMCallStart(iteration=1, max_iterations=25, model="deepseek-v4-flash", message_count=5))
    plain0 = strip_ansi(capsys.readouterr().out)
    assert "Turn" in plain0 and "Step 1  LLM call · deepseek-v4-flash · 5 messages" in plain0

    renderer.on_event(ToolExecutionStart(id="t1", name="Ls", input={"path": "."}))
    renderer.on_event(ToolExecutionResult(id="t1", name="Ls", input={"path": "."}, content="axon/\ncheck_models.py"))
    plain1 = strip_ansi(capsys.readouterr().out)
    assert "● Ls  ." in plain1
    assert "⎿" in plain1 and "→ step 2" in plain1

    renderer.on_event(ToolExecutionStart(id="t2", name="Bash", input={"command": "python3 -m pytest"}))
    renderer.on_event(ToolExecutionResult(id="t2", name="Bash", input={"command": "python3 -m pytest"}, content="14 passed in 0.1s"))
    plain2 = strip_ansi(capsys.readouterr().out)
    assert "● Bash  python3 -m pytest" in plain2

    renderer.on_event(ToolExecutionStart(id="t3", name="Read", input={"path": "src/axon/ui/render.py", "offset": 1, "limit": 100}))
    renderer.on_event(ToolExecutionResult(id="t3", name="Read", input={"path": "src/axon/ui/render.py"}, content="     1→line\n" * 100))
    plain3 = strip_ansi(capsys.readouterr().out)
    assert "● Read  src/axon/ui/render.py · lines 1-100" in plain3
    assert "100 lines" in plain3
    assert "line" in get_last_tool_output()

    renderer.on_event(LLMCallStart(iteration=2, max_iterations=25, model="deepseek-v4-flash", message_count=8))
    assert "Step 2  LLM call" in strip_ansi(capsys.readouterr().out)


def test_renderer_compact_level(capsys):
    renderer = Renderer(trace_level="compact")
    renderer.on_event(LLMCallStart(iteration=1, max_iterations=25, model="m", message_count=1))
    assert "Step 1" not in strip_ansi(capsys.readouterr().out)
    renderer.on_event(ToolExecutionStart(id="t1", name="Glob", input={"pattern": "*.py"}))
    renderer.on_event(ToolExecutionResult(id="t1", name="Glob", input={}, content="a.py\nb.py"))
    out = strip_ansi(capsys.readouterr().out)
    assert "● Glob(*.py)" in out
    assert "→ step" not in out
