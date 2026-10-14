"""
E2E (agent A): navigation/search tools (Glob, Grep, Ls, CodeSearch), Bash (cwd persistence, timeout,
background shells + BashOutput + KillShell), TodoWrite, WebFetch (local server), WebSearch failure,
Task subagents (every built-in type), Skill, ToolSearch (deferred MCP tools) and UsageStats.
"""
from __future__ import annotations

import json
import time

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_a_common import final, local_http, mcp_config, no_traceback, results, run_tools

pytestmark = pytest.mark.e2e

posix_only = pytest.mark.skipif(IS_WINDOWS, reason="uses POSIX shell syntax")


def _ok(r) -> None:
    assert r.code == 0, str(r)
    no_traceback(r)


@pytest.fixture
def tree(axon_env: AxonEnv) -> AxonEnv:
    ws = axon_env.workspace
    (ws / "src" / "deep").mkdir(parents=True)
    (ws / "src" / "m.py").write_text("def alpha():\n    return 1\n\n\nclass Beta:\n    pass\n", encoding="utf-8")
    (ws / "src" / "deep" / "x.js").write_text("const alpha = 2;\nfunction gamma() {}\n", encoding="utf-8")
    (ws / "README.md").write_text("# Project\nAlpha docs here\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "i.js").write_text("const alpha = 99;\n", encoding="utf-8")
    return axon_env


# ─── Glob / Grep / Ls / CodeSearch ──────────────────────────────────────────

def test_glob_recursive_pattern(tree: AxonEnv) -> None:
    """Glob **/*.py finds only python files, as workspace-relative paths."""
    r, ev = run_tools(tree, [("Glob", {"pattern": "**/*.py"})])
    _ok(r)
    c = results(ev, "Glob")[0]["content"]
    assert "src/m.py" in c and ".js" not in c


def test_glob_no_match_is_not_error(tree: AxonEnv) -> None:
    """A Glob with no matches returns a non-error 'no files' style result."""
    r, ev = run_tools(tree, [("Glob", {"pattern": "**/*.rs"})])
    _ok(r)
    res = results(ev, "Glob")[0]
    assert not res["is_error"] and "m.py" not in res["content"]


def test_grep_files_with_matches_mode(tree: AxonEnv) -> None:
    """Grep files_with_matches lists matching files and skips node_modules."""
    r, ev = run_tools(tree, [("Grep", {"pattern": "alpha", "output_mode": "files_with_matches"})])
    _ok(r)
    c = results(ev, "Grep")[0]["content"]
    assert "src/m.py" in c and "src/deep/x.js" in c
    assert "node_modules" not in c


def test_grep_count_mode(tree: AxonEnv) -> None:
    """Grep count mode reports per-file match counts."""
    r, ev = run_tools(tree, [("Grep", {"pattern": "alpha", "output_mode": "count"})])
    _ok(r)
    c = results(ev, "Grep")[0]["content"]
    assert "src/m.py:1" in c and "src/deep/x.js:1" in c


def test_grep_content_mode_case_insensitive_with_line_numbers(tree: AxonEnv) -> None:
    """Grep content mode with -i matches regardless of case and shows file:line:text."""
    r, ev = run_tools(tree, [("Grep", {"pattern": "ALPHA", "-i": True, "output_mode": "content"})])
    _ok(r)
    c = results(ev, "Grep")[0]["content"]
    assert "src/m.py:1:def alpha():" in c and "README.md:2:Alpha docs here" in c


def test_grep_type_filter_and_context(tree: AxonEnv) -> None:
    """Grep type=js restricts to JS files and -A adds trailing context lines."""
    r, ev = run_tools(tree, [("Grep", {"pattern": "alpha", "type": "js", "-A": 1})])
    _ok(r)
    c = results(ev, "Grep")[0]["content"]
    assert "x.js" in c and "m.py" not in c and "gamma" in c


def test_grep_invalid_regex_is_error_result(tree: AxonEnv) -> None:
    """An invalid regular expression yields an error tool result instead of crashing the agent."""
    r, ev = run_tools(tree, [("Grep", {"pattern": "(unclosed"})])
    _ok(r)
    assert results(ev, "Grep")[0]["is_error"]


def test_ls_depth_controls_recursion(tree: AxonEnv) -> None:
    """Ls depth=1 shows only top-level entries; depth=3 descends into nested directories."""
    r, ev = run_tools(tree, [("Ls", {"depth": 1}), ("Ls", {"depth": 3})])
    _ok(r)
    shallow, deep = (x["content"] for x in results(ev, "Ls"))
    assert "src/" in shallow and "m.py" not in shallow
    assert "m.py" in deep and "x.js" in deep


def test_ls_ignore_patterns(tree: AxonEnv) -> None:
    """Ls ignore hides matching names."""
    r, ev = run_tools(tree, [("Ls", {"depth": 2, "ignore": ["*.md"]})])
    _ok(r)
    c = results(ev, "Ls")[0]["content"]
    assert "README.md" not in c and "src/" in c


def test_codesearch_definition(tree: AxonEnv) -> None:
    """CodeSearch definition locates a class definition with file:line."""
    r, ev = run_tools(tree, [("CodeSearch", {"action": "definition", "query": "Beta"})])
    _ok(r)
    c = results(ev, "CodeSearch")[0]["content"]
    assert "src/m.py:5" in c or "src/m.py:4" in c


def test_codesearch_symbols_outline(tree: AxonEnv) -> None:
    """CodeSearch symbols lists functions and classes of a file with line ranges."""
    r, ev = run_tools(tree, [("CodeSearch", {"action": "symbols", "path": "src/m.py"})])
    _ok(r)
    c = results(ev, "CodeSearch")[0]["content"]
    assert "def alpha()" in c and "class Beta" in c


def test_legacy_tool_alias_resolves(tree: AxonEnv) -> None:
    """A commonly hallucinated legacy name (read_file with file_path) is resolved to Read."""
    r, ev = run_tools(tree, [("read_file", {"file_path": "README.md"})])
    _ok(r)
    res = results(ev)[0]
    assert not res["is_error"] and "Alpha docs here" in res["content"], res


# ─── Bash ───────────────────────────────────────────────────────────────────

@posix_only
def test_bash_cwd_persists_between_calls(axon_env: AxonEnv) -> None:
    """`cd sub` in one Bash call carries over to the next Bash call in the same session."""
    (axon_env.workspace / "sub").mkdir()
    r, ev = run_tools(axon_env, [("Bash", {"command": "cd sub", "description": "cd"}),
                                 ("Bash", {"command": "pwd", "description": "pwd"})])
    _ok(r)
    second = results(ev, "Bash")[1]["content"]
    assert second.splitlines()[0].rstrip("/").endswith("/sub"), second


@posix_only
def test_bash_env_var_persistence_is_not_assumed(axon_env: AxonEnv) -> None:
    """Bash returns stdout+stderr of the command and a non-zero exit code is surfaced in the result."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "echo out; echo err 1>&2; exit 3", "description": "x"})])
    _ok(r)
    c = results(ev, "Bash")[0]["content"]
    assert "out" in c and "err" in c and "3" in c


@posix_only
def test_bash_timeout_kills_command(axon_env: AxonEnv) -> None:
    """timeout_s=1 on `sleep 20` kills the process quickly and reports the timeout as an error."""
    t0 = time.monotonic()
    r, ev = run_tools(axon_env, [("Bash", {"command": "sleep 20", "description": "sleep", "timeout_s": 1})])
    _ok(r)
    assert time.monotonic() - t0 < 15
    res = results(ev, "Bash")[0]
    assert res["is_error"] and "timed out" in res["content"].lower()


@posix_only
def test_bash_background_output_and_kill(axon_env: AxonEnv) -> None:
    """run_in_background returns a shell id; BashOutput reads its output; KillShell stops it."""
    r, ev = run_tools(axon_env, [
        ("Bash", {"command": "echo BG-READY; sleep 30", "description": "bg", "run_in_background": True}),
        ("Bash", {"command": "sleep 0.5", "description": "wait"}),
        ("BashOutput", {"shell_id": "bash_1"}),
        ("KillShell", {"shell_id": "bash_1"}),
        ("BashOutput", {"shell_id": "bash_1"}),
    ], timeout=40)
    _ok(r)
    assert "bash_1" in results(ev, "Bash")[0]["content"]
    outs = results(ev, "BashOutput")
    assert "BG-READY" in outs[0]["content"] and "running" in outs[0]["content"]
    assert "Killed" in results(ev, "KillShell")[0]["content"]
    assert "running" not in outs[1]["content"].split("\n")[0]
    assert r.duration < 25


def test_killshell_unknown_id_is_error(axon_env: AxonEnv) -> None:
    """KillShell / BashOutput with an unknown shell id return error results."""
    r, ev = run_tools(axon_env, [("KillShell", {"shell_id": "bash_99"}), ("BashOutput", {"shell_id": "bash_99"})])
    _ok(r)
    assert all(x["is_error"] for x in results(ev)), results(ev)


# ─── TodoWrite ──────────────────────────────────────────────────────────────

def test_todowrite_renders_list_and_persists_in_session(axon_env: AxonEnv) -> None:
    """TodoWrite returns the rendered list (active step shown) and the todos land in the session transcript."""
    r, ev = run_tools(axon_env, [("TodoWrite", {"todos": [
        {"content": "TODO-ALPHA", "status": "completed"}, {"content": "TODO-BETA", "status": "in_progress"},
        {"content": "TODO-GAMMA", "status": "pending"}]})])
    _ok(r)
    c = results(ev, "TodoWrite")[0]["content"]
    assert "3 items" in c and "1/3" in c and "Active Step: TODO-BETA" in c
    blob = "\n".join(p.read_text(encoding="utf-8") for p in axon_env.sessions_dir().glob("*.jsonl"))
    assert "TODO-GAMMA" in blob


def test_todowrite_rejects_invalid_status(axon_env: AxonEnv) -> None:
    """A todo with an invalid status is rejected as an error result."""
    r, ev = run_tools(axon_env, [("TodoWrite", {"todos": [{"content": "x", "status": "bogus"}]})])
    _ok(r)
    assert results(ev, "TodoWrite")[0]["is_error"]


# ─── WebFetch / WebSearch ───────────────────────────────────────────────────

def _html(handler, method, body):
    if handler.path.startswith("/json"):
        return 200, "application/json", '{"k": [1, 2], "name": "JSON-VALUE"}'
    if handler.path.startswith("/missing"):
        return 404, "text/html", "<h1>gone</h1>"
    return 200, "text/html", ("<html><head><title>E2E Page</title><script>evil()</script></head>"
                              "<body><h1>Hello Heading</h1><p>Para WEB-MARKER</p><ul><li>item1</li></ul></body></html>")


def test_webfetch_html_converted_to_text(axon_env: AxonEnv) -> None:
    """WebFetch against a local HTTP server returns title + markdown-ish text, scripts stripped."""
    with local_http(_html) as srv:
        r, ev = run_tools(axon_env, [("WebFetch", {"url": srv.url + "/page", "prompt": "find marker"})])
    _ok(r)
    c = results(ev, "WebFetch")[0]["content"]
    assert "Title: E2E Page" in c and "# Hello Heading" in c and "WEB-MARKER" in c
    assert "evil()" not in c and "Looking for: find marker" in c
    assert srv.requests and srv.requests[0]["method"] == "GET"


def test_webfetch_json_pretty_printed(axon_env: AxonEnv) -> None:
    """A JSON response is pretty-printed."""
    with local_http(_html) as srv:
        r, ev = run_tools(axon_env, [("WebFetch", {"url": srv.url + "/json"})])
    _ok(r)
    c = results(ev, "WebFetch")[0]["content"]
    assert '"name": "JSON-VALUE"' in c and "application/json" in c


def test_webfetch_http_error_status_is_error_result(axon_env: AxonEnv) -> None:
    """A 404 from the server becomes an error tool result mentioning the status."""
    with local_http(_html) as srv:
        r, ev = run_tools(axon_env, [("WebFetch", {"url": srv.url + "/missing"})])
    _ok(r)
    res = results(ev, "WebFetch")[0]
    assert res["is_error"] and "404" in res["content"]


def test_webfetch_rejects_non_http_scheme(axon_env: AxonEnv) -> None:
    """file:// URLs are refused (no local file exfiltration through WebFetch)."""
    (axon_env.workspace / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    r, ev = run_tools(axon_env, [("WebFetch", {"url": f"file://{axon_env.workspace}/secret.txt"})])
    _ok(r)
    res = results(ev, "WebFetch")[0]
    assert res["is_error"] and "TOPSECRET" not in res["content"]


def test_websearch_failure_message_when_network_unreachable(axon_env: AxonEnv) -> None:
    """With every backend unreachable (dead proxy), WebSearch fails with an actionable error result."""
    dead = {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    r, ev = run_tools(axon_env, [("WebSearch", {"query": "python asyncio tutorial"})], run_env=dead, timeout=60)
    _ok(r)
    res = results(ev, "WebSearch")[0]
    assert res["is_error"]
    assert "WebSearch failed" in res["content"] and "WebFetch" in res["content"]


# ─── Task subagents ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent_type,expect_write", [
    ("general-purpose", True), ("explore", False), ("plan", False), ("code-reviewer", False), ("test-runner", False),
])
def test_task_builtin_subagent_types(axon_env: AxonEnv, agent_type: str, expect_write: bool) -> None:
    """Each built-in subagent type runs as its own LLM conversation with its own tool set; its final
    text comes back to the main agent as the Task tool result."""
    axon_env.llm.enqueue(
        [{"tool": "Task", "args": {"description": "delegate", "prompt": f"SUBTASK-{agent_type}", "subagent_type": agent_type}}],
        [{"text": f"SUB-RESULT-{agent_type}"}],
        [{"text": "MAIN-DONE"}],
    )
    r = axon_env.run(["-p", "--output-format", "stream-json", "delegate it"])
    _ok(r)
    ev = r.json_lines()
    res = results(ev, "Task")[0]
    assert not res["is_error"] and f"SUB-RESULT-{agent_type}" in res["content"]
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 3
    sub = reqs[1]
    assert f"SUBTASK-{agent_type}" in sub.last_user_text()
    assert "Task" not in sub.tool_names  # no recursive delegation
    assert ("Write" in sub.tool_names) == expect_write, sub.tool_names
    assert "Read" in sub.tool_names
    assert any(e["type"] == "subagent" for e in ev)
    assert final(ev)["result"].strip() == "MAIN-DONE"


def test_task_unknown_subagent_type(axon_env: AxonEnv) -> None:
    """An unknown subagent_type is an error result listing the available agents; no sub-LLM call is made."""
    axon_env.llm.enqueue([{"tool": "Task", "args": {"prompt": "x", "subagent_type": "nonexistent-agent"}}], [{"text": "ok"}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "go"])
    _ok(r)
    res = results(r.json_lines(), "Task")[0]
    assert res["is_error"] and "general-purpose" in res["content"]
    assert len(axon_env.llm.completion_requests()) == 2


def test_task_custom_project_agent(axon_env: AxonEnv) -> None:
    """A project agent in .axon/agents/<name>.md is selectable via subagent_type and gets its prompt."""
    d = axon_env.workspace / ".axon" / "agents"
    d.mkdir(parents=True)
    (d / "lint-bot.md").write_text("---\nname: lint-bot\ndescription: Lints code\ntools: Read, Grep\n---\n"
                                   "CUSTOM-AGENT-PROMPT-MARK\n", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "Task", "args": {"prompt": "lint", "subagent_type": "lint-bot"}}],
                         [{"text": "LINT-OK"}], [{"text": "done"}])
    r = axon_env.run(["-p", "--output-format", "stream-json", "go"])
    _ok(r)
    res = results(r.json_lines(), "Task")[0]
    assert not res["is_error"] and "LINT-OK" in res["content"], res
    sub = axon_env.llm.completion_requests()[1]
    assert "CUSTOM-AGENT-PROMPT-MARK" in sub.system_text
    assert set(sub.tool_names) <= {"Read", "Grep", "TodoWrite", "Skill"}, sub.tool_names


# ─── Skill / ToolSearch / UsageStats ────────────────────────────────────────

def test_skill_tool_loads_project_skill(axon_env: AxonEnv) -> None:
    """The Skill tool returns the SKILL.md body of a project skill; the skill is advertised in the system prompt."""
    d = axon_env.workspace / ".axon" / "skills" / "greet"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: greet\ndescription: Greets people warmly\n---\nSKILL-BODY-MARKER\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Skill", {"skill": "greet"})])
    _ok(r)
    c = results(ev, "Skill")[0]["content"]
    assert "SKILL-BODY-MARKER" in c
    assert "/greet" in axon_env.llm.completion_requests()[0].system_text


def test_skill_unknown_lists_available(axon_env: AxonEnv) -> None:
    """Unknown skill -> error result that lists available skills."""
    r, ev = run_tools(axon_env, [("Skill", {"skill": "no-such-skill-zz"})])
    _ok(r)
    res = results(ev, "Skill")[0]
    assert res["is_error"] and "Available skills" in res["content"]


def test_toolsearch_reveals_deferred_mcp_tool(axon_env: AxonEnv) -> None:
    """With ENABLE_TOOL_SEARCH=true, MCP tools are hidden behind ToolSearch; selecting one returns its
    schema and it then becomes callable (the stdio MCP server really executes it)."""
    cfg = mcp_config(axon_env)
    r, ev = run_tools(axon_env, [("ToolSearch", {"query": "select:mcp__e2e__shout"}),
                                 ("mcp__e2e__shout", {"text": "hey there"})],
                      "--strict-mcp-config", "--mcp-config", cfg, "--", run_env={"ENABLE_TOOL_SEARCH": "true"})
    _ok(r)
    first = axon_env.llm.completion_requests()[0]
    assert "ToolSearch" in first.tool_names and "mcp__e2e__shout" not in first.tool_names
    assert "mcp__e2e__shout" in results(ev, "ToolSearch")[0]["content"]
    shout = results(ev, "mcp__e2e__shout")[0]
    assert not shout["is_error"] and "SHOUT:HEY THERE" in shout["content"]


def test_toolsearch_keyword_no_match(axon_env: AxonEnv) -> None:
    """A keyword query that matches nothing reports the deferred tool names instead of failing."""
    cfg = mcp_config(axon_env)
    r, ev = run_tools(axon_env, [("ToolSearch", {"query": "zzqqxx"})], "--strict-mcp-config", "--mcp-config", cfg, "--",
                      run_env={"ENABLE_TOOL_SEARCH": "true"})
    _ok(r)
    c = results(ev, "ToolSearch")[0]["content"]
    assert "No deferred tool" in c and "mcp__e2e__shout" in c


def test_mcp_tool_advertised_directly_without_tool_search(axon_env: AxonEnv) -> None:
    """With tool search disabled, the MCP tool schema is advertised directly and callable."""
    cfg = mcp_config(axon_env)
    r, ev = run_tools(axon_env, [("mcp__e2e__shout", {"text": "abc"})], "--strict-mcp-config", "--mcp-config", cfg, "--",
                      run_env={"ENABLE_TOOL_SEARCH": "false"})
    _ok(r)
    assert "mcp__e2e__shout" in axon_env.llm.completion_requests()[0].tool_names
    assert "SHOUT:ABC" in results(ev, "mcp__e2e__shout")[0]["content"]


def test_usagestats_reports_recorded_calls(axon_env: AxonEnv) -> None:
    """UsageStats reads ~/.axon/usage.jsonl written by earlier calls and reports the model and call count."""
    axon_env.llm.enqueue([{"text": "warmup"}])
    assert axon_env.run(["-p", "warm up"]).code == 0
    assert (axon_env.axon_home / "usage.jsonl").is_file()
    r, ev = run_tools(axon_env, [("UsageStats", {"period": "all", "group_by": "model"})])
    _ok(r)
    c = results(ev, "UsageStats")[0]["content"]
    assert "gpt-4o" in c and "LLM calls" in c
    lines = [json.loads(x) for x in (axon_env.axon_home / "usage.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) >= 2
