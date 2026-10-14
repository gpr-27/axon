"""
E2E (agent A): permission modes (default / acceptEdits / plan / bypass), allow / deny / ask rules from
settings.json and CLI flags (incl. Bash(git status:*) prefixes and compound commands) and the workspace
path jail (absolute paths, ../, symlinks, --add-dir, credential stores).

Print mode has no interactive approver, so every "ask" decision must turn into a deny.
"""
from __future__ import annotations

import json
import os

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_a_common import no_traceback, perms, results, run_tools, write_settings

pytestmark = pytest.mark.e2e
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell syntax / symlinks")


def _ran(r) -> None:
    assert r.code == 0, str(r)
    no_traceback(r)


@pytest.fixture
def outside(axon_env: AxonEnv):
    d = axon_env.root / "outside"
    d.mkdir()
    (d / "o.txt").write_text("OUTSIDE-SECRET\n", encoding="utf-8")
    return d


# ─── modes ──────────────────────────────────────────────────────────────────

@posix_only
def test_default_mode_non_tty_denies_bash(axon_env: AxonEnv) -> None:
    """--mode default without a TTY: Bash needs approval, none is possible, so it is denied and not run."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch ran.txt", "description": "x"})], "--mode", "default")
    _ran(r)
    p = perms(ev, "Bash")[0]
    assert p["outcome"] == "deny" and p["source"] == "no-approver"
    assert results(ev, "Bash")[0]["is_error"]
    assert not (axon_env.workspace / "ran.txt").exists()


def test_default_mode_allows_reads_denies_writes(axon_env: AxonEnv) -> None:
    """--mode default: Read inside the workspace is allowed; Write is denied (ask without approver)."""
    (axon_env.workspace / "r.txt").write_text("READABLE\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "r.txt"}), ("Write", {"path": "w.txt", "content": "x"})], "--mode", "default")
    _ran(r)
    assert "READABLE" in results(ev, "Read")[0]["content"]
    assert perms(ev, "Write")[0]["outcome"] == "deny"
    assert not (axon_env.workspace / "w.txt").exists()


def test_denied_tool_message_reaches_model(axon_env: AxonEnv) -> None:
    """The denial is fed back to the model as an error tool result explaining how to allow it."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "w.txt", "content": "x"}}],
                         {"steps": [{"text": "ok"}],
                          "expect": [lambda q: any("acceptEdits" in t or "allow rule" in t for t in q.tool_results())
                                     or "denial reason missing"]})
    r = axon_env.run(["-p", "--mode", "default", "go"])
    _ran(r)
    assert not axon_env.llm.failures, axon_env.llm.failures


def test_accept_edits_allows_file_edits(axon_env: AxonEnv) -> None:
    """acceptEdits auto-approves Write/Edit inside the workspace."""
    r, ev = run_tools(axon_env, [("Write", {"path": "ae.txt", "content": "one\n"}),
                                 ("Edit", {"path": "ae.txt", "old_string": "one", "new_string": "two"})], "--mode", "acceptEdits")
    _ran(r)
    assert all(p["outcome"] == "allow" for p in perms(ev))
    assert (axon_env.workspace / "ae.txt").read_text(encoding="utf-8") == "two\n"


@posix_only
def test_accept_edits_still_asks_for_bash(axon_env: AxonEnv) -> None:
    """acceptEdits does not auto-approve shell commands (denied without a TTY)."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch b.txt", "description": "x"})], "--mode", "acceptEdits")
    _ran(r)
    assert perms(ev, "Bash")[0]["outcome"] == "deny"
    assert not (axon_env.workspace / "b.txt").exists()


def test_plan_mode_read_only(axon_env: AxonEnv) -> None:
    """Plan mode allows Read/Grep and TodoWrite, and denies Write, Edit and Bash."""
    (axon_env.workspace / "p.txt").write_text("PLAN-READ\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [
        ("Read", {"path": "p.txt"}), ("Grep", {"pattern": "PLAN"}),
        ("TodoWrite", {"todos": [{"content": "step", "status": "pending"}]}),
        ("Write", {"path": "w.txt", "content": "x"}), ("Bash", {"command": "touch x", "description": "x"}),
        ("Edit", {"path": "p.txt", "old_string": "PLAN", "new_string": "X"}),
    ], "--mode", "plan")
    _ran(r)
    outcome = {p["tool"]: p["outcome"] for p in perms(ev)}
    assert outcome == {"Read": "allow", "Grep": "allow", "TodoWrite": "allow", "Write": "deny", "Bash": "deny", "Edit": "deny"}
    assert not (axon_env.workspace / "w.txt").exists() and not (axon_env.workspace / "x").exists()
    assert (axon_env.workspace / "p.txt").read_text(encoding="utf-8") == "PLAN-READ\n"


def test_plan_mode_subagent_gets_read_only_tools(axon_env: AxonEnv) -> None:
    """In plan mode a general-purpose subagent must not be offered mutating tools."""
    axon_env.llm.enqueue([{"tool": "Task", "args": {"prompt": "investigate", "subagent_type": "general-purpose"}}],
                         [{"text": "sub"}], [{"text": "main"}])
    r = axon_env.run(["-p", "--mode", "plan", "go"])
    _ran(r)
    sub = axon_env.llm.completion_requests()[1]
    assert not {"Write", "Edit", "MultiEdit", "Bash", "Patch", "NotebookEdit"} & set(sub.tool_names), sub.tool_names


def test_permission_mode_alias_bypass_permissions(axon_env: AxonEnv) -> None:
    """--permission-mode bypassPermissions (Claude Code spelling) is accepted and auto-approves Write."""
    r, ev = run_tools(axon_env, [("Write", {"path": "bp.txt", "content": "x"})], "--permission-mode", "bypassPermissions")
    _ran(r)
    assert perms(ev, "Write")[0]["outcome"] == "allow"
    assert (axon_env.workspace / "bp.txt").exists()


def test_invalid_permission_mode_rejected(axon_env: AxonEnv) -> None:
    """An unknown --mode value is a usage error (exit 2) and no LLM call is made."""
    r = axon_env.run(["-p", "--mode", "yolo", "hi"])
    assert r.code == 2 and "invalid permission mode" in r.output
    assert axon_env.llm.completion_requests() == []


def test_dangerously_skip_permissions_overrides_mode(axon_env: AxonEnv) -> None:
    """--dangerously-skip-permissions forces bypass even when --mode default is given."""
    r, ev = run_tools(axon_env, [("Write", {"path": "d.txt", "content": "x"})], "--mode", "default",
                      "--dangerously-skip-permissions")
    _ran(r)
    assert perms(ev, "Write")[0]["outcome"] == "allow"
    assert (axon_env.workspace / "d.txt").exists()


def test_settings_default_mode(axon_env: AxonEnv) -> None:
    """permissions.defaultMode=plan in .axon/settings.json applies when no --mode flag is given."""
    write_settings(axon_env, {"permissions": {"defaultMode": "plan"}})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})])
    _ran(r)
    assert perms(ev, "Write")[0]["outcome"] == "deny"
    assert not (axon_env.workspace / "w.txt").exists()


def test_cli_mode_beats_settings_default_mode(axon_env: AxonEnv) -> None:
    """--mode acceptEdits wins over permissions.defaultMode=plan."""
    write_settings(axon_env, {"permissions": {"defaultMode": "plan"}})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})], "--mode", "acceptEdits")
    _ran(r)
    assert (axon_env.workspace / "w.txt").exists()


# ─── rules ──────────────────────────────────────────────────────────────────

@posix_only
def test_allow_rule_git_status_prefix(axon_git_env: AxonEnv) -> None:
    """Bash(git status:*) allows `git status --short` in default mode but not `git commit`."""
    env = axon_git_env
    write_settings(env, {"permissions": {"allow": ["Bash(git status:*)"]}})
    r, ev = run_tools(env, [("Bash", {"command": "git status --short", "description": "st"}),
                            ("Bash", {"command": "git commit --allow-empty -m x", "description": "commit"})], "--mode", "default")
    _ran(r)
    p = perms(ev, "Bash")
    assert p[0]["outcome"] == "allow" and "git status" in p[0]["reason"]
    assert p[1]["outcome"] == "deny"
    assert not results(ev, "Bash")[0]["is_error"]


@posix_only
def test_allow_prefix_does_not_cover_compound_command(axon_env: AxonEnv) -> None:
    """Bash(echo:*) must not approve `echo hi && touch pwned.txt` (the second command is not covered)."""
    write_settings(axon_env, {"permissions": {"allow": ["Bash(echo:*)"]}})
    r, ev = run_tools(axon_env, [("Bash", {"command": "echo hi && touch pwned.txt", "description": "x"})], "--mode", "default")
    _ran(r)
    assert perms(ev, "Bash")[0]["outcome"] == "deny"
    assert not (axon_env.workspace / "pwned.txt").exists()


@posix_only
@pytest.mark.parametrize("cmd", ["echo ok && rm -rf victim", "echo ok; rm -rf victim", "true || rm -rf victim",
                                 "echo $(rm -rf victim)",
                                 "echo ok | rm -rf victim"])
def test_deny_rule_catches_compound_commands(axon_env: AxonEnv, cmd: str) -> None:
    """Deny Bash(rm:*) blocks rm hidden in &&, ;, ||, $( ) and pipes - even in bypass mode."""
    (axon_env.workspace / "victim").mkdir()
    write_settings(axon_env, {"permissions": {"deny": ["Bash(rm:*)"]}})
    r, ev = run_tools(axon_env, [("Bash", {"command": cmd, "description": "x"})], "--mode", "bypass")
    _ran(r)
    p = perms(ev, "Bash")[0]
    assert p["outcome"] == "deny" and "rm" in p["reason"], p
    assert (axon_env.workspace / "victim").is_dir()


@posix_only
def test_deny_beats_allow(axon_env: AxonEnv) -> None:
    """When both allow Bash(touch:*) and deny Bash(touch blocked:*) match, deny wins."""
    write_settings(axon_env, {"permissions": {"allow": ["Bash(touch:*)"], "deny": ["Bash(touch blocked:*)"]}})
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch blocked", "description": "x"}),
                                 ("Bash", {"command": "touch fine", "description": "x"})], "--mode", "default")
    _ran(r)
    assert [p["outcome"] for p in perms(ev, "Bash")] == ["deny", "allow"]
    assert not (axon_env.workspace / "blocked").exists() and (axon_env.workspace / "fine").exists()


@posix_only
def test_ask_rule_beats_allow_and_bypass(axon_env: AxonEnv) -> None:
    """permissions.ask beats both an allow rule and bypass mode; without a TTY that means deny."""
    write_settings(axon_env, {"permissions": {"allow": ["Bash(touch:*)"], "ask": ["Bash(touch asked:*)"]}})
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch asked", "description": "x"})], "--mode", "bypass")
    _ran(r)
    assert perms(ev, "Bash")[0]["outcome"] == "deny"
    assert not (axon_env.workspace / "asked").exists()


@posix_only
def test_cli_allowed_tools_rule(axon_env: AxonEnv) -> None:
    """--allowedTools 'Bash(touch:*)' approves matching commands in default mode."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch viaflag", "description": "x"})], "--mode", "default",
                      "--allowedTools", "Bash(touch:*)", "--")
    _ran(r)
    assert perms(ev, "Bash")[0]["outcome"] == "allow"
    assert (axon_env.workspace / "viaflag").exists()


@posix_only
def test_cli_disallowed_tools_pattern_in_bypass(axon_env: AxonEnv) -> None:
    """--disallowedTools 'Bash(touch:*)' denies matching commands even in bypass, others still run."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch nope", "description": "x"}),
                                 ("Bash", {"command": "echo fine", "description": "x"})],
                      "--mode", "bypass", "--disallowedTools", "Bash(touch:*)", "--")
    _ran(r)
    assert [p["outcome"] for p in perms(ev, "Bash")] == ["deny", "allow"]
    assert not (axon_env.workspace / "nope").exists()


def test_write_path_deny_rule(axon_env: AxonEnv) -> None:
    """Deny Write(secrets/**) blocks writes under secrets/ while other writes succeed (bypass mode)."""
    write_settings(axon_env, {"permissions": {"deny": ["Write(secrets/**)"]}})
    r, ev = run_tools(axon_env, [("Write", {"path": "secrets/k.txt", "content": "x"}),
                                 ("Write", {"path": "public/k.txt", "content": "x"})], "--mode", "bypass")
    _ran(r)
    assert [p["outcome"] for p in perms(ev, "Write")] == ["deny", "allow"]
    assert not (axon_env.workspace / "secrets" / "k.txt").exists()
    assert (axon_env.workspace / "public" / "k.txt").exists()


def test_read_deny_rule_dotenv(axon_env: AxonEnv) -> None:
    """Deny Read(.env) keeps the model from reading .env; the secret never reaches the LLM."""
    (axon_env.workspace / ".env").write_text("DB_PASSWORD=hunter2-e2e\n", encoding="utf-8")
    write_settings(axon_env, {"permissions": {"deny": ["Read(.env)"]}})
    r, ev = run_tools(axon_env, [("Read", {"path": ".env"})], "--mode", "bypass")
    _ran(r)
    assert perms(ev, "Read")[0]["outcome"] == "deny"
    assert all("hunter2-e2e" not in q.all_text() for q in axon_env.llm.completion_requests())


def test_bare_tool_deny_rule_removes_whole_tool(axon_env: AxonEnv) -> None:
    """--disallowedTools WebFetch removes the tool from the advertised schema list."""
    r = axon_env.run(["-p", "--disallowedTools", "WebFetch", "--", "hi"])
    _ran(r)
    names = axon_env.llm.completion_requests()[0].tool_names
    assert "WebFetch" not in names and "Read" in names


def test_rules_from_claude_settings_dir_are_honoured(axon_env: AxonEnv) -> None:
    """Rules in <ws>/.claude/settings.json (Claude Code layout) are applied too."""
    write_settings(axon_env, {"permissions": {"deny": ["Write(blocked.txt)"]}}, where=".claude/settings.json")
    r, ev = run_tools(axon_env, [("Write", {"path": "blocked.txt", "content": "x"})], "--mode", "bypass")
    _ran(r)
    assert perms(ev, "Write")[0]["outcome"] == "deny"


def test_print_mode_does_not_persist_rules(axon_env: AxonEnv) -> None:
    """Denials in print mode never write new rules into the workspace settings files."""
    r, ev = run_tools(axon_env, [("Write", {"path": "x.txt", "content": "x"})], "--mode", "default")
    _ran(r)
    assert not (axon_env.workspace / ".axon").exists()


# ─── path jail ──────────────────────────────────────────────────────────────

def test_read_absolute_outside_workspace_denied_in_default(axon_env: AxonEnv, outside) -> None:
    """Reading an absolute path outside the workspace needs approval -> denied in print mode."""
    r, ev = run_tools(axon_env, [("Read", {"path": str(outside / "o.txt")})], "--mode", "default")
    _ran(r)
    assert perms(ev, "Read")[0]["outcome"] == "deny"
    assert all("OUTSIDE-SECRET" not in q.all_text() for q in axon_env.llm.completion_requests())


def test_read_dotdot_escape_denied(axon_env: AxonEnv, outside) -> None:
    """A ../ relative path that escapes the workspace is treated as outside and denied."""
    r, ev = run_tools(axon_env, [("Read", {"path": "../outside/o.txt"})], "--mode", "default")
    _ran(r)
    assert perms(ev, "Read")[0]["outcome"] == "deny"


def test_write_outside_denied_in_accept_edits(axon_env: AxonEnv, outside) -> None:
    """acceptEdits only auto-approves edits inside the workspace; outside writes are denied."""
    r, ev = run_tools(axon_env, [("Write", {"path": str(outside / "new.txt"), "content": "x"}),
                                 ("Write", {"path": "../outside/new2.txt", "content": "x"})], "--mode", "acceptEdits")
    _ran(r)
    assert [p["outcome"] for p in perms(ev, "Write")] == ["deny", "deny"]
    assert sorted(p.name for p in outside.iterdir()) == ["o.txt"]


def test_glob_outside_workspace_denied(axon_env: AxonEnv, outside) -> None:
    """Search tools pointed outside the workspace are also jailed."""
    r, ev = run_tools(axon_env, [("Glob", {"pattern": "*.txt", "path": str(outside)}),
                                 ("Grep", {"pattern": "SECRET", "path": str(outside)})], "--mode", "default")
    _ran(r)
    assert [p["outcome"] for p in perms(ev)] == ["deny", "deny"]
    assert all("OUTSIDE-SECRET" not in q.all_text() for q in axon_env.llm.completion_requests())


@posix_only
def test_symlink_escape_is_treated_as_outside(axon_env: AxonEnv, outside) -> None:
    """A symlink inside the workspace pointing outside does not let acceptEdits write outside."""
    os.symlink(outside, axon_env.workspace / "link")
    r, ev = run_tools(axon_env, [("Write", {"path": "link/evil.txt", "content": "x"})], "--mode", "acceptEdits")
    _ran(r)
    assert perms(ev, "Write")[0]["outcome"] == "deny"
    assert not (outside / "evil.txt").exists()


def test_add_dir_grants_access(axon_env: AxonEnv, outside) -> None:
    """--add-dir makes an outside directory part of the jail: reads allowed, acceptEdits writes allowed."""
    r, ev = run_tools(axon_env, [("Read", {"path": str(outside / "o.txt")}),
                                 ("Write", {"path": str(outside / "added.txt"), "content": "ADDED\n"})],
                      "--mode", "acceptEdits", f"--add-dir={outside}")
    _ran(r)
    assert [p["outcome"] for p in perms(ev)] == ["allow", "allow"]
    assert "OUTSIDE-SECRET" in results(ev, "Read")[0]["content"]
    assert (outside / "added.txt").read_text(encoding="utf-8") == "ADDED\n"


def test_additional_directories_setting(axon_env: AxonEnv, outside) -> None:
    """permissions.additionalDirectories in settings.json works like --add-dir."""
    write_settings(axon_env, {"permissions": {"additionalDirectories": [str(outside)]}})
    r, ev = run_tools(axon_env, [("Read", {"path": str(outside / "o.txt")})], "--mode", "default")
    _ran(r)
    assert perms(ev, "Read")[0]["outcome"] == "allow"


def test_add_dir_nonexistent_is_config_error(axon_env: AxonEnv, tmp_path) -> None:
    """--add-dir with a missing directory fails fast with a clear message and no LLM call."""
    r = axon_env.run(["-p", f"--add-dir={tmp_path / 'nope'}", "hi"])
    assert r.code != 0 and "not a directory" in r.output
    no_traceback(r)
    assert axon_env.llm.completion_requests() == []


def test_credential_store_blocked_even_in_bypass(axon_env: AxonEnv) -> None:
    """~/.ssh is a protected path: Read is denied even in bypass mode and the key never reaches the LLM."""
    ssh = axon_env.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("-----BEGIN KEY----- E2E-PRIVATE-KEY\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": str(ssh / "id_rsa")})], "--mode", "bypass")
    _ran(r)
    assert perms(ev, "Read")[0]["outcome"] == "deny"
    assert all("E2E-PRIVATE-KEY" not in q.all_text() for q in axon_env.llm.completion_requests())


@posix_only
def test_bash_cat_of_credential_store_blocked_in_bypass(axon_env: AxonEnv) -> None:
    """Reading ~/.ssh through Bash (`cat ~/.ssh/id_rsa`) is also blocked in bypass mode."""
    ssh = axon_env.home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("E2E-PRIVATE-KEY-2\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Bash", {"command": f"cat {ssh / 'id_rsa'}", "description": "x"})], "--mode", "bypass")
    _ran(r)
    assert all("E2E-PRIVATE-KEY-2" not in q.all_text() for q in axon_env.llm.completion_requests())


@posix_only
def test_catastrophic_command_blocked_in_bypass(axon_env: AxonEnv) -> None:
    """`rm -rf /` is refused by the structural invariant even in bypass mode."""
    r, ev = run_tools(axon_env, [("Bash", {"command": "rm -rf /", "description": "x"})], "--mode", "bypass")
    _ran(r)
    p = perms(ev, "Bash")[0]
    assert p["outcome"] == "deny" and "invariant" in p["reason"].lower()


def test_session_records_permission_decisions(axon_env: AxonEnv) -> None:
    """The denied call and its denial result are persisted in the session transcript."""
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})], "--mode", "plan")
    _ran(r)
    blob = "\n".join(p.read_text(encoding="utf-8") for p in axon_env.sessions_dir().glob("*.jsonl"))
    assert "Plan mode" in blob and json.dumps("w.txt") in blob
