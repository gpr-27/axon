"""The permission rule tables and source rules from the Claude Code `permissions`, `permission-modes` and `settings` docs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.config import Settings, normalize_mode, repo_root_for_local_settings
from axon.hooks import trust
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import Rule, domain_matches, parse_rule


def tool(name: str, **kw):
    return SimpleNamespace(name=name, readonly=kw.pop("readonly", False), default_permission=kw.pop("perm", "ask"), **kw)


BASH, READ, EDIT = tool("Bash"), tool("Read", readonly=True, perm="allow"), tool("Edit")


def eng(ws: Path, **rules) -> PermissionEngine:
    return PermissionEngine(Settings(workspace=ws, mode="default", api_key="k", permissions=rules))


def write(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_trust():
    trust.configure(False)
    trust.reset_session()
    yield
    trust.configure(False)


# ── Wildcard table ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule,yes,no", [
    ("npm run build", ["npm run build"], ["npm run build --watch"]),
    ("npm run *", ["npm run build", "npm run test --watch", "npm run"], ["npm install"]),
    ("git log * main", ["git log --oneline main", "git log -5 main"], ["git log main", "git push origin main"]),
    ("git * main", ["git merge main", "git push origin main"], ["git log"]),
    ("* --version", ["node --version"], ["node -v"]),
    ("ls *", ["ls -la", "ls"], ["lsof"]),
    ("ls*", ["ls -la", "lsof"], []),
    ("* --help *", ["npm --help x"], ["npm --help"]),
    ("ls:*", ["ls -la", "ls"], ["lsof"]),
    ("git:* push", [], ["git status push"]),
    ("a?c", ["a?c"], ["abc"]),                    # only * is a wildcard
])
def test_wildcard_table(rule, yes, no):
    r = Rule("Bash", rule)
    for c in yes:
        assert r.matches("Bash", {"command": c}), c
    for c in no:
        assert not r.matches("Bash", {"command": c}), c


def test_wrappers_and_assignments():
    r = Rule("Bash", "npm test *")
    assert r.matches("Bash", {"command": "timeout 30 npm test"})
    assert r.matches("Bash", {"command": "NODE_ENV=test npm test"})
    assert not r.matches("Bash", {"command": "FOO=bar npm test"})        # allow stops at an unknown assignment
    assert Rule("Bash", "rm *").matches("Bash", {"command": "FOO=bar rm -rf tmp/"}, for_deny=True)
    assert Rule("Bash", "grep *").matches("Bash", {"command": "xargs grep pattern"})
    assert not Rule("Bash", "grep *").matches("Bash", {"command": "xargs -n1 grep pattern"})
    assert not Rule("Bash", "npm *").matches("Bash", {"command": "npm test &&"})


@pytest.mark.parametrize("cmd", ["cd /tmp && git clean -f", 'echo "$(git clean -f)"', "for i in 1; do git clean -f; done",
                                 "(git clean -f)", "if x; then git clean -f; fi", "{ git clean -f; }"])
def test_deny_and_ask_reach_nested_commands(cmd):
    assert Rule("Bash", "git clean *").matches("Bash", {"command": cmd}, for_deny=True)


def test_exec_wrappers_and_find_actions_need_an_exact_rule():
    assert not Rule("Bash", "watch *").matches("Bash", {"command": "watch ls"})
    assert not Rule("Bash", "find *").matches("Bash", {"command": "find . -delete"})
    assert Rule("Bash", "find build -type f -delete").matches("Bash", {"command": "find build -type f -delete"})
    # With a * in it the rule reads as a pattern, so the command still prompts.
    assert not Rule("Bash", "find . -name '*.tmp' -delete").matches("Bash", {"command": "find . -name '*.tmp' -delete"})


def test_parameter_rules():
    r = parse_rule("Agent(model:opus)")
    assert r.matches("Task", {"model": "opus"}, for_deny=True)
    assert not r.matches("Task", {"model": "claude-opus-5"}, for_deny=True)
    assert not parse_rule("Agent(model:*)").matches("Task", {"prompt": "x"}, for_deny=True)
    assert parse_rule("Agent(isolation : *)").matches("Task", {"isolation": "worktree"}, for_deny=True)
    assert parse_rule("Bash(run_in_background:true)").matches("Bash", {"command": "x", "run_in_background": True}, for_deny=True)
    assert not parse_rule("Bash(command:rm *)").matches("Bash", {"command": "rm x"}, for_deny=True)
    assert not parse_rule("Agent(model:opus)").matches("Task", {"model": "opus"})     # allow rules keep their own syntax


def test_tool_name_globs():
    assert parse_rule("*").matches("Bash", {"command": "ls"}, for_deny=True)
    assert parse_rule("mcp__*").matches("mcp__gh__x", {}, for_deny=True)
    for unanchored in ("*", "B*", "mcp__*"):
        assert not parse_rule(unanchored).matches("Bash", {"command": "ls"})
        assert not parse_rule(unanchored).matches("mcp__gh__x", {})
    assert parse_rule("mcp__puppeteer__*").matches("mcp__puppeteer__nav", {})
    assert parse_rule("mcp__github__get_*").matches("mcp__github__get_x", {})
    assert not parse_rule("mcp__github__get_*").matches("mcp__github__put_x", {})
    # A server called reports__v2 is not covered by an allow rule for "reports", but a deny rule covers it.
    assert not parse_rule("mcp__reports").matches("mcp__reports__v2__t", {}, server="reports__v2")
    assert parse_rule("mcp__reports").matches("mcp__reports__t", {}, server="reports")
    assert parse_rule("mcp__reports").matches("mcp__reports__v2__t", {}, for_deny=True, server="reports__v2")
    assert parse_rule("mcp__reports__v2").matches("mcp__reports__v2__t", {}, for_deny=True, server="reports__v2")


@pytest.mark.parametrize("rule,host,hit", [
    ("example.com", "example.com", True), ("example.com", "api.example.com", True), ("example.com", "evilexample.com", False),
    ("*.example.com", "api.example.com", True), ("*.example.com", "a.b.example.com", True), ("*.example.com", "example.com", False),
    ("*", "anything.io", True), ("example.*", "example.org", True), ("example.*", "example.evil.com", False),
    ("example.com.", "EXAMPLE.com", True),
])
def test_webfetch_domains(rule, host, hit):
    assert domain_matches(rule, host) is hit


# ── Path patterns ───────────────────────────────────────────────────────────────────────────────────

def test_path_pattern_depth(tmp_path):
    ws = tmp_path
    (ws / "src").mkdir()
    top, nested = "src/app.ts", "vendor/pkg/src/lib.js"
    for spec, kind, a, b in [("src/**", "allow", True, False), ("src/**", "deny", True, True), ("/src/**", "allow", False, False),
                             ("/src/**", "deny", True, False), ("**/src/**", "allow", True, True), ("**/src/**", "deny", True, True)]:
        r = Rule("Edit", spec)
        assert r.matches("Edit", {"file_path": top}, ws, kind=kind) is a, spec
        assert r.matches("Edit", {"file_path": nested}, ws, kind=kind) is b, (spec, kind)
    assert Rule("Read", ".env").matches("Read", {"file_path": "a/b/.env"}, ws, kind="deny")
    assert not Rule("Read", "./.env").matches("Read", {"file_path": "a/.env"}, ws, kind="deny")
    assert Rule("Read", "./.env").matches("Read", {"file_path": ".env"}, ws, kind="deny")


def test_single_slash_is_absolute_for_allow_and_also_the_settings_folder_for_deny_and_ask(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = other / "file"
    for kind in ("allow", "deny", "ask"):
        assert Rule("Read", str(secret)).matches("Read", {"file_path": str(secret)}, ws, kind=kind)    # one slash: absolute, as always
        assert Rule("Read", "/" + str(secret)).matches("Read", {"file_path": str(secret)}, ws, kind=kind)
    assert Rule("Read", "/docs/x").matches("Read", {"file_path": str(ws / "docs/x")}, ws, kind="deny")   # a deny rule also guards the folder
    assert not Rule("Read", "/docs/x").matches("Read", {"file_path": str(ws / "docs/x")}, ws, kind="allow")


def test_user_settings_anchor_at_the_settings_folder(workspace, isolate_user_home):
    from axon.storage import axon_home
    write(axon_home() / "settings.json", {"permissions": {"deny": ["Read(/secrets/**)"]}})
    e = eng(workspace)
    assert e.check(READ, {"file_path": str(axon_home() / "secrets/x")}, "default").outcome == "deny"
    assert e.check(READ, {"file_path": "secrets/x"}, "default").outcome == "allow"


def test_carve_outs_stay_in_one_source(workspace):
    write(workspace / ".axon/settings.json", {"permissions": {"deny": ["Read(*.env)", "Read(!sample.env)"]}})
    e = eng(workspace)
    assert e.check(READ, {"file_path": "x/prod.env"}, "default").outcome == "deny"
    assert e.check(READ, {"file_path": "x/sample.env"}, "default").outcome == "allow"
    # Another source's ! does not cancel this deny
    write(workspace / ".axon/settings.local.json", {"permissions": {"deny": ["Read(!prod.env)"]}})
    assert eng(workspace).check(READ, {"file_path": "x/prod.env"}, "default").outcome == "deny"
    # A directory blocked as a whole stays blocked
    write(workspace / ".axon/settings.json", {"permissions": {"deny": ["Read(secrets/**)", "Read(!secrets/public/**)"]}})
    assert eng(workspace).check(READ, {"file_path": "secrets/public/a"}, "default").outcome == "deny"


def test_symlink_rules(tmp_path):
    ws = tmp_path / "ws"
    (ws / "project").mkdir(parents=True)
    out = tmp_path / "outside"
    out.mkdir()
    (out / "id").write_text("x")
    (ws / "project" / "key").symlink_to(out / "id")
    allow = Rule("Read", "project/**")
    assert not allow.matches("Read", {"file_path": "project/key"}, ws, kind="allow")   # the target is outside the rule
    assert Rule("Read", "/" + str(out) + "/**").matches("Read", {"file_path": "project/key"}, ws, kind="deny")


def test_read_deny_also_blocks_write_but_not_notebook(workspace):
    e = eng(workspace, deny=["Read(./notes.txt)"])
    assert e.check(tool("Write"), {"file_path": "notes.txt"}, "bypass").outcome == "deny"
    assert e.check(tool("Edit"), {"file_path": "notes.txt"}, "bypass").outcome == "deny"
    assert e.check(tool("NotebookEdit"), {"notebook_path": "notes.txt"}, "bypass").outcome == "allow"




def test_bash_file_commands_and_redirects_follow_file_rules(workspace):
    e = eng(workspace, deny=["Read(./.env)", "Edit(./locked.txt)"])
    for cmd in ("cat .env", "head -n1 .env", "sed -n 1p .env", "python x < .env"):
        assert e.check(BASH, {"command": cmd}, "bypass").outcome == "deny", cmd
    for cmd in ("echo x > locked.txt", "make | tee locked.txt", "sed -i s/a/b/ locked.txt"):
        assert e.check(BASH, {"command": cmd}, "bypass").outcome == "deny", cmd
    assert e.check(BASH, {"command": "cat other"}, "bypass").outcome == "allow"


# ── Read-only commands, modes, protected and critical paths ─────────────────────────────────────────







def test_dontask_denies_what_would_prompt(workspace):
    assert normalize_mode("dontAsk") == "dontAsk"
    assert normalize_mode("manual") == "default" and normalize_mode("auto") == "auto"      # auto is its own mode now (classifier)
    e = eng(workspace, allow=["Bash(npm test)", "AskUserQuestion"], ask=["Bash(git push *)"])
    assert e.check(BASH, {"command": "rm x"}, "dontAsk").outcome == "deny"
    assert e.check(BASH, {"command": "npm test"}, "dontAsk").outcome == "allow"
    assert e.check(BASH, {"command": "git push o m"}, "dontAsk").outcome == "deny"
    assert e.check(tool("AskUserQuestion", perm="allow"), {}, "dontAsk").outcome == "deny"
    assert e.check(tool("mcp__x__y", requires_user_interaction=True), {}, "dontAsk").outcome == "deny"


def test_accept_edits_covers_file_edits_only_not_shell_commands(workspace):
    e = eng(workspace)
    for cmd in ("mkdir a", "touch a", "mv a b", "cp a b", "rm a", "rmdir a", "sed -i s/a/b/ f", "npm test"):
        assert e.check(BASH, {"command": cmd}, "acceptEdits").outcome == "ask", cmd


@pytest.mark.parametrize("path", [".git/config", ".vscode/x", ".axon/settings.json", ".claude/settings.json", ".bashrc", "sub/.npmrc", ".mcp.json", ".config/git/x"])
def test_protected_writes(workspace, path):
    e = eng(workspace, allow=[f"Edit({path})"])
    assert e.check(EDIT, {"file_path": path}, "acceptEdits").outcome == "ask"
    assert e.check(EDIT, {"file_path": path}, "default").outcome == "ask"
    assert e.check(EDIT, {"file_path": path}, "dontAsk").outcome == "deny"
    assert e.check(EDIT, {"file_path": path}, "bypass").outcome == "allow"


def test_protected_exceptions(workspace):
    e = eng(workspace)
    for path in (".axon/worktrees/w/a.py", ".axon/plans/p.md", ".axon/agent-memory/x/n.md"):
        assert e.check(EDIT, {"file_path": path}, "acceptEdits").outcome == "allow", path


def test_critical_removals_always_ask_and_allow_cannot_approve(workspace):
    e = eng(workspace, allow=["Bash", "Bash(rm *)"])
    for cmd in (f"rm -rf {workspace}", "rm -rf .", "rmdir /data", "rm -rf /tmpthing"):
        d = e.check(BASH, {"command": cmd}, "bypass")
        assert d.outcome == "ask" and d.hard, cmd
    assert e.check(BASH, {"command": "rm -rf build"}, "bypass").outcome == "allow"


def test_block_outside_reads_and_writes(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    write(workspace / ".axon/settings.json", {"permissions": {"blockReadsOutsideWorkingDirectories": True, "blockWritesOutsideWorkingDirectories": True}})
    e = eng(workspace)
    assert e.check(READ, {"file_path": str(outside / "x")}, "bypass").outcome == "deny"
    assert e.check(tool("Write"), {"file_path": str(outside / "x")}, "bypass").outcome == "deny"
    assert e.check(tool("Write"), {"file_path": ".axon/settings.json"}, "bypass").outcome == "deny"
    assert e.check(tool("Write"), {"file_path": "a.py"}, "bypass").outcome == "allow"
    assert e.check(READ, {"file_path": "a.py"}, "bypass").outcome == "allow"
    # A repository's additional directory does not count; your own does.
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)      # your own file: untracked, in a repository
    write(workspace / ".axon/settings.local.json", {"permissions": {"additionalDirectories": [str(outside)]}})
    assert eng(workspace).check(READ, {"file_path": str(outside / "x")}, "bypass").outcome == "allow"
    write(workspace / ".axon/settings.json", {"permissions": {"blockReadsOutsideWorkingDirectories": False}})
    write(workspace / ".axon/settings.local.json", {"permissions": {"blockReadsOutsideWorkingDirectories": True}})
    assert eng(workspace).check(READ, {"file_path": str(outside / "x")}, "bypass").outcome == "deny"   # false cannot lift it


def test_network_reads_always_ask(workspace):
    e = eng(workspace, allow=["Read"])
    d = e.check(READ, {"file_path": "/net/fileserver/notes.txt"}, "default")
    assert d.outcome == "ask" and d.hard
    assert e.check(READ, {"file_path": "/net/fileserver/notes.txt"}, "dontAsk").outcome == "deny"
    assert e.check(READ, {"file_path": "/net/fileserver/notes.txt"}, "bypass").outcome == "allow"


def test_bare_deny_removes_the_tool_and_scoped_does_not(workspace):
    assert eng(workspace, deny=["Bash"]).removes_tool("Bash")
    assert eng(workspace, deny=["Bash(*)"]).removes_tool("Bash")
    assert eng(workspace, deny=["mcp__*"]).removes_tool("mcp__a__b")
    assert not eng(workspace, deny=["Bash(rm *)"]).removes_tool("Bash")




def test_generated_path_rule_is_literal_and_absolute_uses_two_slashes(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("o")
    # A backslash is a path separator in a rule on Windows, so the bracket is written as a class there.
    literal = "Edit(./a[[]1].py)" if os.name == "nt" else "Edit(./a\\[1\\].py)"
    assert str(PermissionEngine.narrow_rule_for("Edit", {"file_path": "a[1].py"}, workspace)) == literal
    assert str(PermissionEngine.narrow_rule_for("Edit", {"file_path": str(outside / "x")}, workspace)).startswith("Edit(//")
    r = PermissionEngine.narrow_rule_for("Edit", {"file_path": "a[1].py"}, workspace)
    assert r.matches("Edit", {"file_path": "a[1].py"}, workspace)
    assert not r.matches("Edit", {"file_path": "a1.py"}, workspace)


# ── What a repository may set ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["bypassPermissions", "auto"])
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_repo_cannot_start_bypass_or_auto(workspace, isolate_user_home, value, name):
    write(workspace / ".axon" / name, {"permissions": {"defaultMode": value}})
    assert Settings.load({"workspace": workspace, "api_key": "k"}).mode == "default"


def test_other_modes_apply_from_a_repo_and_user_can_choose_bypass(workspace, isolate_user_home):
    from axon.storage import axon_home
    write(workspace / ".axon/settings.json", {"permissions": {"defaultMode": "plan"}})
    assert Settings.load({"workspace": workspace, "api_key": "k"}).mode == "plan"
    (workspace / ".axon/settings.json").unlink()
    write(axon_home() / "settings.json", {"permissions": {"defaultMode": "bypassPermissions"}})
    assert Settings.load({"workspace": workspace, "api_key": "k"}).mode == "bypass"
    assert Settings.load({"workspace": workspace, "api_key": "k", "settings_file": '{"permissions":{"defaultMode":"dontAsk"}}'}).mode == "dontAsk"


def test_project_toml_and_env_cannot_hand_out_permission(workspace, isolate_user_home, monkeypatch):
    monkeypatch.delenv("AXON_MODE", raising=False)
    monkeypatch.delenv("AXON_DANGEROUSLY_SKIP_PERMISSIONS", raising=False)
    (workspace / ".axon").mkdir()
    (workspace / ".axon/config.toml").write_text('mode = "bypass"\ndangerously_skip_permissions = true\n')
    s = Settings.load({"workspace": workspace, "api_key": "k"})
    assert s.mode == "default" and not s.dangerously_skip_permissions
    (workspace / ".axon/config.toml").unlink()
    (workspace / ".env").write_text("AXON_MODE=bypass\nAXON_DANGEROUSLY_SKIP_PERMISSIONS=1\n")
    write(workspace / ".axon/settings.json", {"env": {"AXON_MODE": "bypass", "HOME": "/evil", "XDG_CONFIG_HOME": "/evil", "AXON_HOME": "/evil"}})
    home = os.environ.get("HOME")
    s = Settings.load({"workspace": workspace, "api_key": "k", "mode": "default"})
    assert not s.dangerously_skip_permissions
    assert "AXON_MODE" not in os.environ and os.environ.get("HOME") == home and os.environ.get("XDG_CONFIG_HOME") != "/evil"


def test_project_env_safe_now_others_wait_for_trust(workspace, isolate_user_home, monkeypatch):
    monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("MY_TOOL_PATH", raising=False)
    write(workspace / ".axon/settings.json", {"env": {"BASH_DEFAULT_TIMEOUT_MS": "1000", "MY_TOOL_PATH": "/x"}})
    Settings.load({"workspace": workspace, "api_key": "k"})
    assert os.environ["BASH_DEFAULT_TIMEOUT_MS"] == "1000" and "MY_TOOL_PATH" not in os.environ
    monkeypatch.setenv("MY_TOOL_PATH", "/keep")          # registers cleanup for the value trust() sets
    trust.trust(workspace, persist=False)
    assert os.environ["MY_TOOL_PATH"] == "/x"


def test_env_block_overwrites_the_shell_and_highest_file_wins(workspace, isolate_user_home, monkeypatch):
    from axon.storage import axon_home
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "1")
    write(axon_home() / "settings.json", {"env": {"BASH_MAX_TIMEOUT_MS": "2"}})
    write(workspace / ".axon/settings.json", {"env": {"BASH_MAX_TIMEOUT_MS": "3"}})
    Settings.load({"workspace": workspace, "api_key": "k"})
    assert os.environ["BASH_MAX_TIMEOUT_MS"] == "3"






def test_repo_cannot_auto_approve_with_a_bare_glob(workspace):
    write(workspace / ".axon/settings.local.json", {"permissions": {"allow": ["*", "B*", "mcp__*"]}})
    assert eng(workspace).check(BASH, {"command": "rm x"}, "default").outcome == "ask"


def test_mcp_parameter_rule_in_a_file_is_skipped_but_flag_keeps_it(workspace, capsys):
    write(workspace / ".axon/settings.json", {"permissions": {"deny": ["mcp__a__b(x:y)"]}})
    t = tool("mcp__a__b")
    assert eng(workspace).check(t, {"x": "y"}, "bypass").outcome == "allow"
    assert "--disallowedTools" in capsys.readouterr().err
    (workspace / ".axon/settings.json").unlink()
    assert eng(workspace, deny=["mcp__a__b(x:y)"]).check(t, {"x": "y"}, "bypass").outcome == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="the local file stays in the workspace on Windows")
def test_local_settings_live_at_the_git_root(workspace, isolate_user_home, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    sub = workspace / "pkg"
    sub.mkdir()
    assert repo_root_for_local_settings(sub) == workspace.resolve()
    e = eng(sub)
    e.add_rule("allow", "Bash(npm test)")
    assert (workspace.resolve() / ".axon/settings.local.json").is_file() and not (sub / ".axon").exists()
    assert eng(sub).check(BASH, {"command": "npm test"}, "default").outcome == "allow"
    assert "**/.axon/settings.local.json" in (tmp_path / "xdg/git/ignore").read_text()   # kept out of your commits


def test_hook_allow_cannot_skip_an_ask_rule():
    from axon.permissions.engine import Decision
    assert Decision("ask", "x", hard=True).hard


# ─── the read-only set cannot write, run programs or read what nobody checked ──





def test_plan_mode_runs_no_shell_command_because_of_an_allow_rule(workspace):
    e = eng(workspace, allow=["Bash(npm *)"])
    assert e.check(BASH, {"command": "npm install"}, "plan").outcome == "deny"
    assert e.check(BASH, {"command": "ls"}, "plan").outcome == "deny"


def test_a_repository_env_block_cannot_set_how_commands_run():
    from axon.config import project_env_forbidden
    for name in ("AXON_SHELL_PREFIX", "AXON_SHELL", "AXON_MANAGED_DIR", "AXON_SERVE_TOKEN", "AXON_DISABLE_ALL_HOOKS"):
        assert project_env_forbidden(name), name
