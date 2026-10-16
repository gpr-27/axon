"""E2E-F: behaviour of CLI flags and subcommands that previously had only parse-level coverage.

Each test runs the real ``axon`` CLI against the fake LLM server and asserts an observable effect
(what reached the provider, files on disk, exit codes and messages), not just "no crash":
``--version``, ``--help`` (subcommand list), ``--agent`` / ``--agents``, ``--debug`` / ``--debug-file``,
``--verbose``, ``-w/--worktree``, ``-n`` + ``-r <name>``, ``--fork-session``, ``axon purge``, ``axon doctor``.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from harness import SRC, AxonEnv, _project_key

pytestmark = pytest.mark.e2e


def _version() -> str:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import axon
    return axon.__version__


def _calls(env: AxonEnv) -> list:
    """Completion requests (chat/messages) the fake provider received, in order."""
    return [r for r in env.llm.requests if r.wire in ("openai", "anthropic")]


def _git_commit(ws: Path) -> None:
    (ws / "README.md").write_text("hello\n", encoding="utf-8")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "init"]):
        subprocess.run(cmd, cwd=str(ws), check=True, capture_output=True)


# ─── --version / --help ─────────────────────────────────────────────────────

@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_flag_prints_package_version(axon_env: AxonEnv, flag: str) -> None:
    r = axon_env.run([flag])
    assert r.code == 0, str(r)
    assert r.stdout.strip() == f"axon v{_version()}"
    assert _calls(axon_env) == []


def test_help_lists_every_subcommand(axon_env: AxonEnv) -> None:
    """`axon --help` names all subcommands the CLI dispatches (built-in and feature ones)."""
    r = axon_env.run(["--help"])
    assert r.code == 0, str(r)
    flat = " ".join(r.stdout.split())
    for sub in ("mcp", "doctor", "purge", "setup", "new", "map", "serve", "report", "stats"):
        assert f"axon {sub}" in flat, (sub, r.stdout[-1500:])


# ─── --agent / --agents ─────────────────────────────────────────────────────

def test_agent_flag_uses_definition_prompt_and_readonly_tools(axon_env: AxonEnv) -> None:
    """`--agent explore`: the main session gets the explore prompt and, because the definition is
    read-only, no file-modifying tools (same as when explore runs as a Task subagent)."""
    axon_env.llm.enqueue([{"text": "EXPLORED"}])
    r = axon_env.run(["-p", "--agent", "explore", "where is main?"])
    assert r.code == 0 and "EXPLORED" in r.stdout, str(r)
    req = _calls(axon_env)[0]
    assert "read-only exploration subagent" in req.system_text
    tools = set(req.tool_names)
    assert {"Read", "Grep"} <= tools, tools
    assert not tools & {"Write", "Edit", "Bash"}, tools


def test_agent_flag_unknown_name_fails_with_available_list(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "--agent", "no-such-agent", "hi"])
    assert r.code == 2, str(r)
    assert "unknown agent 'no-such-agent'" in r.stderr and "explore" in r.stderr
    assert _calls(axon_env) == []


def test_agents_json_defines_session_agent_usable_by_agent_flag(axon_env: AxonEnv) -> None:
    spec = {"rev": {"description": "Reviews things", "prompt": "You are REV-PROMPT-7731.", "tools": ["Read"]}}
    axon_env.llm.enqueue([{"text": "REVIEWED"}])
    r = axon_env.run(["-p", "--agents", json.dumps(spec), "--agent", "rev", "review it"])
    assert r.code == 0 and "REVIEWED" in r.stdout, str(r)
    req = _calls(axon_env)[-1]
    assert "REV-PROMPT-7731" in req.system_text
    assert "Read" in req.tool_names and "Write" not in req.tool_names and "Bash" not in req.tool_names


def test_agents_json_is_offered_to_the_task_tool(axon_env: AxonEnv) -> None:
    spec = {"sec-checker": {"description": "SECCHK checks security", "prompt": "Check security."}}
    axon_env.llm.enqueue([{"text": "ok"}])
    r = axon_env.run(["-p", "--agents", json.dumps(spec), "hi"])
    assert r.code == 0, str(r)
    body = json.dumps(_calls(axon_env)[-1].body)
    assert "sec-checker" in body and "SECCHK" in body


def test_agents_json_malformed_is_rejected(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "--agents", '{"x": {"description": "no prompt"}}', "hi"])
    assert r.code == 2 and "--agents" in r.stderr, str(r)
    assert _calls(axon_env) == []


# ─── --debug / --debug-file / --verbose ─────────────────────────────────────

def test_debug_file_receives_agent_events(axon_env: AxonEnv, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "axon-debug.log"
    axon_env.llm.enqueue([{"text": "DEBUGGED"}])
    r = axon_env.run(["-p", "--debug-file", str(log), "hello"])
    assert r.code == 0 and "DEBUGGED" in r.stdout, str(r)
    text = log.read_text(encoding="utf-8")
    assert "debug logging started" in text
    assert "axon.agent" in text and "LLMCallStart" in text and '"user_prompt": "hello"' in text, text[-2000:]


def test_debug_flag_writes_session_log_under_axon_home(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "DBG2"}])
    r = axon_env.run(["-p", "--debug=agent", "hello"])
    assert r.code == 0 and "DBG2" in r.stdout, str(r)
    m = re.search(r"Debug log: (\S+\.log)", r.output)
    assert m, r.output
    log = Path(m.group(1))
    assert log.resolve().is_relative_to(axon_env.axon_home.resolve()), log  # default location is under AXON_HOME
    assert "axon.agent" in log.read_text(encoding="utf-8")


def test_verbose_announces_additional_directories(axon_env: AxonEnv, tmp_path: Path) -> None:
    extra = tmp_path / "extra-dir"
    extra.mkdir()
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    quiet = axon_env.run(["-p", "--add-dir", str(extra), "hi"])
    loud = axon_env.run(["-p", "--verbose", "--add-dir", str(extra), "hi"])
    assert quiet.code == 0 and loud.code == 0, str(loud)
    assert "Additional directories" not in quiet.output
    assert "Additional directories" in loud.output and "extra-dir" in loud.output


# ─── -w / --worktree ────────────────────────────────────────────────────────

def test_worktree_runs_session_in_a_git_worktree(axon_env: AxonEnv) -> None:
    _git_commit(axon_env.workspace)
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "wt.txt", "content": "in worktree\n"}}], [{"text": "WT-DONE"}])
    r = axon_env.run(["-p", "--mode", "bypass", "-w", "feat", "make wt.txt"])
    assert r.code == 0 and "WT-DONE" in r.stdout, str(r)
    assert "Worktree:" in r.output and "axon/feat" in r.output and "new" in r.output
    wt = axon_env.axon_home / "projects" / _project_key(axon_env.workspace) / "worktrees" / "feat"
    assert (wt / "wt.txt").read_text(encoding="utf-8") == "in worktree\n"
    assert not (axon_env.workspace / "wt.txt").exists()
    branches = subprocess.run(["git", "branch", "--list", "axon/feat"], cwd=str(axon_env.workspace),
                              capture_output=True, text=True).stdout
    assert "axon/feat" in branches
    # second run reuses it
    axon_env.llm.enqueue([{"text": "AGAIN"}])
    r2 = axon_env.run(["-p", "-w", "feat", "again"])
    assert r2.code == 0 and "AGAIN" in r2.stdout, str(r2)
    assert "Worktree:" in r2.output and ", new" not in r2.output


def test_worktree_outside_git_fails_cleanly(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "-w", "feat", "hi"])
    assert r.code == 2 and "--worktree needs a git repository" in r.stderr, str(r)
    assert _calls(axon_env) == []


# ─── -n / -r / --fork-session ───────────────────────────────────────────────

def test_name_then_resume_by_name_carries_history(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "FIRST-ANSWER"}], [{"text": "SECOND"}])
    r1 = axon_env.run(["-p", "-n", "my-named-sess", "remember the word PINEAPPLE-42"])
    assert r1.code == 0, str(r1)
    r2 = axon_env.run(["-p", "-r", "my-named-sess", "what was the word?"])
    assert r2.code == 0 and "SECOND" in r2.stdout, str(r2)
    msgs = json.dumps(_calls(axon_env)[-1].messages)
    assert "PINEAPPLE-42" in msgs and "FIRST-ANSWER" in msgs


def test_resume_unknown_id_fails(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "-r", "does-not-exist-123", "hi"])
    assert r.code == 1 and "No conversation found" in r.stderr, str(r)
    assert _calls(axon_env) == []


def test_continue_without_sessions_fails(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "--continue", "hi"])
    assert r.code == 1 and "No conversation found to continue" in r.stderr, str(r)


def test_fork_session_keeps_history_but_writes_a_new_session(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "ORIG"}], [{"text": "FORKED"}])
    assert axon_env.run(["-p", "say ORIG-MARK"]).code == 0
    before = {p.name for p in axon_env.sessions_dir().glob("*.jsonl")}
    r = axon_env.run(["-p", "--continue", "--fork-session", "continue"])
    assert r.code == 0 and "FORKED" in r.stdout, str(r)
    assert "ORIG-MARK" in json.dumps(_calls(axon_env)[-1].messages)
    after = {p.name for p in axon_env.sessions_dir().glob("*.jsonl")}
    assert len(after - before) == 1, (before, after)


# ─── subcommands ────────────────────────────────────────────────────────────

def test_purge_project_requires_yes_then_removes_sessions(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "x"}])
    assert axon_env.run(["-p", "hi"]).code == 0
    assert any(axon_env.sessions_dir().glob("*.jsonl"))
    refused = axon_env.run(["purge", "project"])
    assert refused.code == 2 and "--yes" in refused.output, str(refused)
    assert any(axon_env.sessions_dir().glob("*.jsonl"))
    bad = axon_env.run(["purge", "everything-now", "--yes"])
    assert bad.code == 2 and "usage: axon purge" in bad.output
    ok = axon_env.run(["purge", "project", "--yes"])
    assert ok.code == 0 and "removed" in ok.output, str(ok)
    assert not any(axon_env.sessions_dir().glob("*.jsonl"))


def test_doctor_reports_active_configuration(axon_env: AxonEnv) -> None:
    r = axon_env.run(["doctor"])
    assert r.code in (0, 1), str(r)
    assert "Axon doctor" in r.output
    assert "gpt-4o" in r.output and axon_env.llm.url.split("//")[1] in r.output
    assert str(axon_env.workspace.resolve()) in r.output or str(axon_env.workspace) in r.output
    assert "sk-e2e-test-key" not in r.output  # key values are never printed


def test_debug_api_filter_logs_provider_calls_only(axon_env: AxonEnv, tmp_path: Path) -> None:
    """`--debug api` (the documented example) captures the LLM calls and nothing else."""
    log = tmp_path / "api.log"
    axon_env.llm.enqueue([{"tool": "Read", "args": {"path": "nope.txt"}}], [{"text": "API-DONE"}])
    r = axon_env.run(["-p", "--debug-file", str(log), "--debug=api", "read it"])
    assert r.code == 0 and "API-DONE" in r.stdout, str(r)
    text = log.read_text(encoding="utf-8")
    assert "LLMCallStart" in text and "LLMCallEnd" in text, text[-1500:]
    assert "ToolExecutionStart" not in text
