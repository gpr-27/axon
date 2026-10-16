"""E2E-B: detailed behaviour of individual slash commands through the real CLI
(``axon -p "/cmd ..."`` and the interactive REPL over a pty)."""
from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_b_util import (
    no_crash, project_dir, records, repl_cmd, run_json, start_repl, text_in_request, write_file,
    parallel, pbatch,  # noqa: F401 (pbatch is a fixture)
)

pytestmark = pytest.mark.e2e

pty_only = pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")


def cmd(env: AxonEnv, line: str, *extra: str):
    r = env.run(["-p", *extra, line])
    no_crash(r)
    assert r.code == 0, str(r)
    return r


def _seed(env: AxonEnv) -> str:
    env.llm.enqueue([{"text": "Working."}, {"tool": "Write", "args": {"path": "a.txt", "content": "A\n"}},
                     {"usage": {"input": 2000, "output": 50, "cached": 500}}],
                    [{"text": "SEED-FINAL-ANSWER"}])
    return run_json(env, "create a.txt")["session_id"]


# ─── /help ──────────────────────────────────────────────────────────────────

@parallel
def test_help_lists_categories_and_commands(axon_env: AxonEnv) -> None:
    """/help prints every category header and core commands."""
    out = cmd(axon_env, "/help").output
    for cat in ("Session", "Context & Cost", "Model & Modes", "System"):
        assert cat in out
    for c in ("/rewind", "/export", "/mcp", "/memory", "/init"):
        assert c in out


@parallel
def test_help_topic_shows_usage_and_aliases(axon_env: AxonEnv) -> None:
    """/help rewind prints the usage block and aliases of one command."""
    out = cmd(axon_env, "/help rewind").output
    assert "/rewind to <n>" in out and "/undo" in out


@parallel
def test_help_unknown_topic(axon_env: AxonEnv) -> None:
    """/help nosuch reports that no such command exists."""
    assert "No command named /nosuch" in cmd(axon_env, "/help nosuch").output


@parallel
def test_unknown_command_suggests_close_match(axon_env: AxonEnv) -> None:
    """An unknown command is not sent to the model and suggests the closest name."""
    out = cmd(axon_env, "/stauts").output
    assert "Unknown command /stauts" in out and "Did you mean:" in out
    assert "/status" in out and "/stats" in out              # the closest names, with what they do
    assert axon_env.llm.completion_requests() == []


# ─── /status /context /cost /trace /inspect /explain /stats /budget ─────────

@parallel
def test_status_reports_model_endpoint_and_storage(axon_env: AxonEnv) -> None:
    """/status shows model, the fake endpoint, the session id and AXON_HOME storage paths."""
    out = cmd(axon_env, "/status").output
    assert "gpt-4o" in out and axon_env.llm.url in out
    assert str(axon_env.axon_home) in out.replace("/private/var", "/var") or "Storage" in out
    assert "Sessions dir" in out


@parallel
def test_context_breakdown_after_turn(axon_env: AxonEnv) -> None:
    """/context on a resumed session counts messages and lists all categories."""
    _seed(axon_env)
    out = cmd(axon_env, "/context", "--continue").output
    for k in ("System prompt", "Tool schemas", "Messages", "Free"):
        assert k in out
    assert "0 messages" not in out


@parallel
def test_context_why(axon_env: AxonEnv) -> None:
    """/context --why lists injected blocks without crashing."""
    _seed(axon_env)
    cmd(axon_env, "/context --why", "--continue")


@parallel
def test_cost_breakdown_after_turn(axon_env: AxonEnv) -> None:
    """/cost after a 2-call turn shows 2 calls, model and a non-zero total."""
    _seed(axon_env)
    out = cmd(axon_env, "/cost", "--continue").output
    assert "gpt-4o" in out and "2 calls" in out, out
    assert "$0.00000" not in out.split("Total", 1)[1].splitlines()[0]


@parallel
def test_cost_calls_lists_each_api_call(axon_env: AxonEnv) -> None:
    """/cost calls prints one row per API call."""
    _seed(axon_env)
    out = cmd(axon_env, "/cost calls", "--continue").output
    assert out.count("gpt-4o") >= 2, out


@parallel
def test_trace_level_switch(axon_env: AxonEnv) -> None:
    """/trace detailed selects the detailed level."""
    out = cmd(axon_env, "/trace detailed").output
    assert "detailed" in out


@parallel
def test_inspect_last_turn_shows_tool_and_permission(axon_env: AxonEnv) -> None:
    """/inspect on a resumed session shows the Write tool call of the last turn."""
    _seed(axon_env)
    out = cmd(axon_env, "/inspect", "--continue").output
    assert "Write" in out and "a.txt" in out, out


@parallel
def test_explain_last_turn(axon_env: AxonEnv) -> None:
    """/explain narrates the last turn (mentions the tool used)."""
    _seed(axon_env)
    out = cmd(axon_env, "/explain", "--continue").output
    assert "No turn traces" not in out
    assert "Write" in out or "a.txt" in out, out


@parallel
def test_stats_after_usage(axon_env: AxonEnv) -> None:
    """/stats reads usage.jsonl and reports spend, calls and the model."""
    _seed(axon_env)
    out = cmd(axon_env, "/stats").output
    assert "Spend" in out and "2 LLM calls" in out and "gpt-4o" in out, out


@parallel
def test_budget_set_and_off(axon_env: AxonEnv) -> None:
    """/budget 5 sets a $5 limit; /budget off removes it."""
    assert "$5.00" in cmd(axon_env, "/budget 5").output
    out = cmd(axon_env, "/budget off").output.lower()
    assert "no limit" in out, out


@parallel
def test_max_budget_flag_stops_new_turn(axon_env: AxonEnv) -> None:
    """With --max-budget-usd below what was already spent, a new turn is refused."""
    axon_env.llm.enqueue([{"text": "pricey"}, {"usage": {"input": 400000, "output": 50000}}])
    run_json(axon_env, "spend")
    axon_env.llm.enqueue([{"text": "SHOULD-NOT-RUN"}])
    r = axon_env.run(["-p", "--continue", "--max-budget-usd", "0.01", "again"])
    no_crash(r)
    assert "SHOULD-NOT-RUN" not in r.output, str(r)


# ─── /report ────────────────────────────────────────────────────────────────

class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        self.tags.append(tag)


@parallel
def test_report_writes_valid_html_under_cache(axon_env: AxonEnv) -> None:
    """/report writes AXON_HOME/cache/reports/*.html: a parseable HTML document with usage data."""
    _seed(axon_env)
    cmd(axon_env, "/report")
    files = list((axon_env.axon_home / "cache" / "reports").glob("*.html"))
    assert len(files) == 1, list(axon_env.axon_home.rglob("*.html"))
    html = files[0].read_text(encoding="utf-8")
    assert html.lstrip().lower().startswith("<!doctype html")
    p = _Tags()
    p.feed(html)
    assert {"html", "head", "body"} <= set(p.tags)
    assert "gpt-4o" in html
    assert not list(axon_env.workspace.rglob("*.html"))


@parallel
def test_report_out_path(axon_env: AxonEnv) -> None:
    """/report --out PATH writes the HTML to that path."""
    out = axon_env.root / "r.html"
    cmd(axon_env, f"/report --out {out}")
    assert out.is_file() and "<html" in out.read_text(encoding="utf-8").lower()


# ─── memory ─────────────────────────────────────────────────────────────────

@parallel
def test_memory_add_project_scope(axon_env: AxonEnv) -> None:
    """/memory add <text> saves a project memory under <ws>/.axon/memory."""
    out = cmd(axon_env, "/memory add use pytest for every test").output
    assert "[project]" in out
    files = list((axon_env.workspace / ".axon" / "memory").glob("*.md"))
    assert files and any("pytest" in f.read_text(encoding="utf-8") for f in files)
    assert not (axon_env.axon_home / "memory").exists() or not list((axon_env.axon_home / "memory").glob("*.md"))


@parallel
def test_memory_add_global_scope(axon_env: AxonEnv) -> None:
    """/memory add --global <text> saves under AXON_HOME/memory, not the workspace."""
    out = cmd(axon_env, "/memory add --global prefer tabs").output
    assert "[user]" in out
    files = list((axon_env.axon_home / "memory").glob("*.md"))
    assert files and "tabs" in files[0].read_text(encoding="utf-8")
    assert not (axon_env.workspace / ".axon" / "memory").exists()


@parallel
def test_saved_memory_reaches_system_prompt(axon_env: AxonEnv) -> None:
    """A memory saved with /memory add is part of the next request's context."""
    cmd(axon_env, "/memory add the deploy target is ZEPHYR-CLUSTER")
    axon_env.llm.enqueue({"steps": [{"text": "ok"}], "expect": [text_in_request("ZEPHYR-CLUSTER")]})
    run_json(axon_env, "where do we deploy?")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_memory_list_and_remove(axon_env: AxonEnv) -> None:
    """/memory lists saved memories; /memory remove 1 deletes it."""
    cmd(axon_env, "/memory add remember the marmot")
    assert "marmot" in cmd(axon_env, "/memory").output
    cmd(axon_env, "/memory remove 1")
    assert not list((axon_env.workspace / ".axon" / "memory").glob("*marmot*.md"))


@parallel
def test_axon_md_loaded_into_system_prompt(axon_env: AxonEnv) -> None:
    """A workspace AXON.md is injected into the system prompt."""
    write_file(axon_env.workspace / "AXON.md", "# Rules\nAlways say QUOKKA-RULE.\n")
    axon_env.llm.enqueue({"steps": [{"text": "ok"}], "expect": [text_in_request("QUOKKA-RULE")]})
    run_json(axon_env, "hi")
    assert not axon_env.llm.failures


@pty_only
def test_hash_quick_memory_in_repl(axon_env: AxonEnv) -> None:
    """`#note` in the REPL saves a project memory without calling the model; `#global note` saves a user one."""
    with start_repl(axon_env) as s:
        repl_cmd(s, "#the build uses make", "Saved project memory")
        repl_cmd(s, "#global I like dark mode", r"Saved (user|global) memory")
        s.sendline("/exit")
        s.wait(10)
    proj = list((axon_env.workspace / ".axon" / "memory").glob("*.md"))
    user = list((axon_env.axon_home / "memory").glob("*.md"))
    assert proj and "make" in proj[0].read_text(encoding="utf-8")
    assert user and "dark mode" in user[0].read_text(encoding="utf-8")
    assert axon_env.llm.completion_requests() == []


@parallel
def test_hash_quick_memory_in_print_mode(axon_env: AxonEnv) -> None:
    """`axon -p "#note"` should save a memory like the REPL does."""
    axon_env.run(["-p", "#the build uses make"])
    assert list((axon_env.workspace / ".axon" / "memory").glob("*.md"))
    assert axon_env.llm.completion_requests() == []


# ─── /init ──────────────────────────────────────────────────────────────────

@parallel
def test_init_not_created_by_plain_prompt(axon_env: AxonEnv) -> None:
    """A normal prompt never creates AXON.md."""
    axon_env.llm.enqueue([{"text": "hi"}])
    run_json(axon_env, "hello")
    assert not (axon_env.workspace / "AXON.md").exists()


@parallel
def test_init_creates_axon_md_with_detected_commands(axon_env: AxonEnv) -> None:
    """/init creates AXON.md; a package.json test script is detected."""
    write_file(axon_env.workspace / "package.json", json.dumps({"name": "x", "scripts": {"test": "jest"}}))
    out = cmd(axon_env, "/init").output
    assert "Created" in out and "AXON.md" in out
    text = (axon_env.workspace / "AXON.md").read_text(encoding="utf-8")
    assert "npm test" in text or "jest" in text, text


@parallel
def test_init_refuses_to_overwrite(axon_env: AxonEnv) -> None:
    """A second /init keeps the existing AXON.md untouched and prints suggestions."""
    write_file(axon_env.workspace / "AXON.md", "MY OWN CONTENT\n")
    out = cmd(axon_env, "/init").output
    assert "already exists" in out
    assert (axon_env.workspace / "AXON.md").read_text(encoding="utf-8") == "MY OWN CONTENT\n"


@parallel
def test_init_force_without_tty_keeps_file(axon_env: AxonEnv) -> None:
    """/init --force asks for confirmation; with no terminal the default is to keep the file."""
    write_file(axon_env.workspace / "AXON.md", "KEEP\n")
    cmd(axon_env, "/init --force")
    assert (axon_env.workspace / "AXON.md").read_text(encoding="utf-8") == "KEEP\n"


@parallel
def test_init_local_creates_gitignored_file(axon_env: AxonEnv) -> None:
    """/init local creates AXON.local.md and adds it to .gitignore."""
    cmd(axon_env, "/init local")
    assert (axon_env.workspace / "AXON.local.md").is_file()
    assert "AXON.local.md" in (axon_env.workspace / ".gitignore").read_text(encoding="utf-8")


# ─── /agents ────────────────────────────────────────────────────────────────

@parallel
def test_agents_create_list_show(axon_env: AxonEnv) -> None:
    """/agents create NAME DESC writes .axon/agents/NAME.md; /agents lists it; /agents show prints it."""
    cmd(axon_env, "/agents create reviewer Reviews code carefully")
    path = axon_env.workspace / ".axon" / "agents" / "reviewer.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---") and "name: reviewer" in text and "Reviews code carefully" in text
    assert "reviewer" in cmd(axon_env, "/agents").output
    assert "Reviews code carefully" in cmd(axon_env, "/agents show reviewer").output


@parallel
def test_agents_create_global(axon_env: AxonEnv) -> None:
    """/agents create NAME --global writes under AXON_HOME/agents."""
    cmd(axon_env, "/agents create helper --global Helps out")
    assert (axon_env.axon_home / "agents" / "helper.md").is_file(), list(axon_env.home.rglob("*.md"))


@parallel
def test_custom_agent_file_is_listed(axon_env: AxonEnv) -> None:
    """A hand-written .claude/agents/*.md definition shows up in /agents."""
    write_file(axon_env.workspace / ".claude" / "agents" / "doc-writer.md",
               "---\nname: doc-writer\ndescription: Writes docs\n---\nYou write docs.\n")
    assert "doc-writer" in cmd(axon_env, "/agents").output


# ─── /hooks ─────────────────────────────────────────────────────────────────

def _hook_settings(env: AxonEnv, marker: str) -> None:
    hook = {"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [
        {"type": "command", "command": f"echo hooked > {marker}"}]}]}}
    write_file(env.workspace / ".axon" / "settings.json", json.dumps(hook))


@parallel
def test_hooks_lists_configured_hook(axon_env: AxonEnv) -> None:
    """/hooks lists a PostToolUse hook from .axon/settings.json."""
    _hook_settings(axon_env, "hook.out")
    out = cmd(axon_env, "/hooks").output
    assert "PostToolUse" in out and "echo hooked" in out


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell redirection")
@parallel
def test_post_tool_use_hook_runs(axon_env: AxonEnv) -> None:
    """A configured PostToolUse hook runs after a Write tool call."""
    _hook_settings(axon_env, "hook.out")
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "w.txt", "content": "w"}}], [{"text": "done"}])
    run_json(axon_env, "write")
    assert (axon_env.workspace / "hook.out").is_file()


# ─── /skills /output-style ──────────────────────────────────────────────────

@parallel
def test_skills_lists_project_skill(axon_env: AxonEnv) -> None:
    """A .axon/skills/<n>/SKILL.md is listed by /skills and /skills show."""
    write_file(axon_env.workspace / ".axon" / "skills" / "greet" / "SKILL.md",
               "---\nname: greet\ndescription: Greets warmly\n---\nSay GREETINGS-SKILL-BODY.\n")
    assert "greet" in cmd(axon_env, "/skills").output
    assert "GREETINGS-SKILL-BODY" in cmd(axon_env, "/skills show greet").output


@parallel
def test_skills_create(axon_env: AxonEnv) -> None:
    """/skills create NAME scaffolds .axon/skills/NAME/SKILL.md."""
    cmd(axon_env, "/skills create my-skill")
    assert (axon_env.workspace / ".axon" / "skills" / "my-skill" / "SKILL.md").is_file()


@parallel
def test_output_style_new_and_activate(axon_env: AxonEnv) -> None:
    """/output-style new + /output-style NAME saves the style and it reaches the system prompt."""
    cmd(axon_env, "/output-style new terse Short answers")
    path = axon_env.axon_home / "output-styles" / "terse.md"
    assert path.is_file()
    path.write_text(path.read_text(encoding="utf-8") + "\nTERSE-STYLE-MARKER\n", encoding="utf-8")
    cmd(axon_env, "/output-style terse")
    settings = json.loads((axon_env.axon_home / "settings.json").read_text(encoding="utf-8"))
    assert "terse" in json.dumps(settings)
    axon_env.llm.enqueue({"steps": [{"text": "k"}], "expect": [text_in_request("TERSE-STYLE-MARKER")]})
    run_json(axon_env, "hi")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_output_style_builtin_listed(axon_env: AxonEnv) -> None:
    """/output-style lists the built-in styles."""
    out = cmd(axon_env, "/output-style").output
    assert "explanatory" in out and "concise" in out


# ─── /permissions ───────────────────────────────────────────────────────────

@parallel
def test_permissions_allow_then_remove(axon_env: AxonEnv) -> None:
    """/permissions allow RULE writes settings.local.json; /permissions remove allow RULE deletes it."""
    cmd(axon_env, "/permissions allow Bash(npm test:*)")
    path = axon_env.workspace / ".axon" / "settings.local.json"
    assert "Bash(npm test:*)" in path.read_text(encoding="utf-8")
    assert "Bash(npm test:*)" in cmd(axon_env, "/permissions list").output
    cmd(axon_env, "/permissions remove allow Bash(npm test:*)")
    assert "Bash(npm test:*)" not in path.read_text(encoding="utf-8")


@parallel
def test_permissions_deny_rule_blocks_tool(axon_env: AxonEnv) -> None:
    """A deny rule added with /permissions deny blocks the matching tool even in bypass mode."""
    cmd(axon_env, "/permissions deny Write")
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "denied.txt", "content": "x"}}], [{"text": "ok"}])
    run_json(axon_env, "--mode", "bypass", "write it")
    assert not (axon_env.workspace / "denied.txt").exists()


# ─── /config /model /effort /mode /theme /vim ───────────────────────────────

@parallel
def test_config_key_value_persists(axon_env: AxonEnv) -> None:
    """/config trace_level=detailed is saved to AXON_HOME/settings.json and shown by /config."""
    cmd(axon_env, "/config trace_level=detailed")
    data = json.loads((axon_env.axon_home / "settings.json").read_text(encoding="utf-8"))
    assert "detailed" in json.dumps(data)
    assert "detailed" in cmd(axon_env, "/status").output


@parallel
def test_config_unknown_key_is_rejected(axon_env: AxonEnv) -> None:
    """/config no_such_key=1 is rejected without writing settings."""
    out = cmd(axon_env, "/config no_such_key_zz=1").output.lower()
    assert "unknown" in out or "no such" in out or "not a" in out, out
    p = axon_env.axon_home / "settings.json"
    assert not p.exists() or "no_such_key_zz" not in p.read_text(encoding="utf-8")


@parallel
def test_theme_persists(axon_env: AxonEnv) -> None:
    """/theme light saves the theme to settings.json."""
    cmd(axon_env, "/theme light")
    assert "light" in (axon_env.axon_home / "settings.json").read_text(encoding="utf-8")


@parallel
def test_vim_on_off_persists(axon_env: AxonEnv) -> None:
    """/vim on then /vim off toggles the saved editor mode."""
    cmd(axon_env, "/vim on")
    assert "vim" in (axon_env.axon_home / "settings.json").read_text(encoding="utf-8")
    cmd(axon_env, "/vim off")
    assert '"vim"' not in (axon_env.axon_home / "settings.json").read_text(encoding="utf-8")


@parallel
def test_mode_switch_is_restored_on_continue(axon_env: AxonEnv) -> None:
    """/mode plan on a resumed session is recorded; the next --continue run restores plan mode."""
    axon_env.llm.enqueue([{"text": "hi"}])
    run_json(axon_env, "hello")
    cmd(axon_env, "/mode plan", "--continue")
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "nope.txt", "content": "x"}}], [{"text": "ok"}])
    r = axon_env.run(["-p", "--continue", "write nope"])
    no_crash(r)
    assert not (axon_env.workspace / "nope.txt").exists(), str(r)


@pty_only
def test_repl_model_effort_mode_switch(axon_env: AxonEnv) -> None:
    """In the REPL, /model fake-model, /effort low and /mode plan take effect on the next turn."""
    with start_repl(axon_env) as s:
        repl_cmd(s, "/model fake-model", "fake-model")
        repl_cmd(s, "/effort low", "reflex")
        repl_cmd(s, "/mode plan", "plan")
        out = repl_cmd(s, "/status", "Mode")
        assert "plan" in out and "fake-model" in out and "reflex" in out, out
        axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "p.txt", "content": "x"}}], [{"text": "PLANNED"}])
        repl_cmd(s, "try writing", "PLANNED", 20)
        s.sendline("/exit")
        s.wait(10)
    reqs = axon_env.llm.completion_requests()
    assert reqs and reqs[0].body["model"] == "fake-model"
    assert not (axon_env.workspace / "p.txt").exists()


# ─── /add-dir /cd ───────────────────────────────────────────────────────────

@parallel
def test_add_dir_save_persists(axon_env: AxonEnv) -> None:
    """/add-dir PATH --save records the directory in .axon/settings.local.json."""
    extra = axon_env.root / "extra"
    extra.mkdir()
    cmd(axon_env, f"/add-dir {extra} --save")
    assert "extra" in (axon_env.workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8")


@parallel
def test_add_dir_flag_allows_reading_outside_workspace(axon_env: AxonEnv) -> None:
    """--add-dir lets the Read tool read a file outside the workspace in default mode."""
    extra = axon_env.root / "extra"
    write_file(extra / "outside.txt", "OUTSIDE-CONTENT")
    axon_env.llm.enqueue([{"tool": "Read", "args": {"path": str(extra / "outside.txt")}}],
                         {"steps": [{"text": "read ok"}], "expect": [text_in_request("OUTSIDE-CONTENT")]})
    run_json(axon_env, "--add-dir", str(extra), "--mode", "default", "read it")
    assert not axon_env.llm.failures, axon_env.llm.failures


@parallel
def test_cd_moves_session_storage(axon_env: AxonEnv) -> None:
    """/cd DIR switches the workspace; /status then reports the new workspace."""
    other = axon_env.root / "other-ws"
    other.mkdir()
    out = cmd(axon_env, f"/cd {other}").output
    assert "other-ws" in out


# ─── /loop /tasks /todos ────────────────────────────────────────────────────

@pty_only
def test_repl_loop_runs_prompt_twice(axon_env: AxonEnv) -> None:
    """/loop 2x PROMPT in the REPL runs the prompt two times."""
    axon_env.llm.enqueue([{"text": "LOOP-ONE"}], [{"text": "LOOP-TWO"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "/loop 2x say loop", "LOOP-TWO", 30)
        s.sendline("/exit")
        s.wait(10)
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 2 and all("say loop" in q.last_user_text() for q in reqs)


@parallel
def test_loop_in_print_mode_runs(axon_env: AxonEnv) -> None:
    """`axon -p "/loop 2x say hi"` should run the prompt twice."""
    axon_env.run(["-p", "/loop 2x say hi"])
    assert len(axon_env.llm.completion_requests()) == 2


_TODOS = [{"content": "Write the parser", "status": "in_progress", "activeForm": "Writing the parser"},
          {"content": "Add tests", "status": "pending", "activeForm": "Adding tests"}]


@pty_only
def test_repl_todos_after_todowrite(axon_env: AxonEnv) -> None:
    """After the model writes a todo list in the REPL, /todos shows the items."""
    axon_env.llm.enqueue([{"tool": "TodoWrite", "args": {"todos": _TODOS}}], [{"text": "PLANNED-TODOS"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "plan it", "PLANNED-TODOS", 20)
        out = repl_cmd(s, "/todos", "Add tests")
        assert "Write the parser" in out
        s.sendline("/exit")
        s.wait(10)


@parallel
def test_todos_survive_resume(axon_env: AxonEnv) -> None:
    """A todo list written by TodoWrite is still shown by /todos after --continue."""
    axon_env.llm.enqueue([{"tool": "TodoWrite", "args": {"todos": _TODOS}}], [{"text": "planned"}])
    run_json(axon_env, "plan it")
    out = cmd(axon_env, "/todos", "--continue").output
    assert "Write the parser" in out and "Add tests" in out, out
    assert list((project_dir(axon_env) / "todos").glob("*.json"))


@parallel
def test_tasks_lists_background_shell(axon_env: AxonEnv) -> None:
    """/tasks with nothing running reports no background tasks."""
    out = cmd(axon_env, "/tasks").output.lower()
    assert "no " in out or "background" in out


# ─── misc system commands ───────────────────────────────────────────────────

@parallel
def test_doctor_command(axon_env: AxonEnv) -> None:
    """/doctor reports the endpoint and API-key state."""
    out = cmd(axon_env, "/doctor").output
    assert "API key" in out


@parallel
def test_release_notes_current_version(axon_env: AxonEnv) -> None:
    """/release-notes shows the current version's entry."""
    import axon
    out = cmd(axon_env, "/release-notes").output
    assert axon.__version__ in out


@parallel
def test_keybindings_lists_shortcuts(axon_env: AxonEnv) -> None:
    """/keybindings lists keyboard shortcuts (Ctrl / Shift+Tab)."""
    out = cmd(axon_env, "/keybindings").output
    assert "Ctrl" in out or "ctrl" in out


@parallel(git=True)
def test_diff_in_git_repo_shows_change(axon_git_env: AxonEnv) -> None:
    """/diff in a git repo shows uncommitted changes of a tracked file."""
    import subprocess
    ws = axon_git_env.workspace
    write_file(ws / "f.txt", "one\n")
    subprocess.run(["git", "add", "."], cwd=ws, env=axon_git_env.env, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, env=axon_git_env.env, capture_output=True)
    write_file(ws / "f.txt", "one\ntwo-added-line\n")
    out = cmd(axon_git_env, "/diff").output
    assert "two-added-line" in out, out


@parallel
def test_rewind_list_shows_checkpoint(axon_env: AxonEnv) -> None:
    """/rewind list on a resumed session shows the snapshot made by the Write call."""
    _seed(axon_env)
    out = cmd(axon_env, "/rewind list", "--continue").output
    assert "a.txt" in out and "1 file" in out, out


@pty_only
def test_repl_question_mark_shows_help(axon_env: AxonEnv) -> None:
    """`?` in the REPL prints the keyboard shortcut reference."""
    with start_repl(axon_env) as s:
        out = repl_cmd(s, "?", r"chat:submit")
        assert "ctrl-r" in out
        s.sendline("/exit")
        s.wait(10)
