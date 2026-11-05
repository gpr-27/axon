"""Manual control of connectors, skills and plugins: scope, env, timeout, tool rules, test, logs, status."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.marketplace import cli, control, status
from axon.marketplace.installer import HealthResult
from axon.mcp import health as mcp_health
from axon.mcp.config import MCPConfig, ServerSpec


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "MY_API_KEY"):
        monkeypatch.setenv(name, "")
    return home


def run(tokens: list[str], workspace: Path, health=None, agent=None):
    lines: list[str] = []
    shop = cli.Marketplace(workspace, out=lines.append,
                           health_check=health or (lambda spec, ws, timeout: HealthResult("connected", tools=["a", "b"])),
                           agent=agent)
    outcome = shop.run(tokens)
    return outcome, "\n".join(lines)


def add_server(workspace: Path, name: str = "demo", scope: str = "user", **raw) -> None:
    raw = raw or {"command": "demo-server", "args": ["--x"]}
    MCPConfig(workspace).save_spec(ServerSpec.from_dict(name, raw, scope), scope)


# ─── scope ───────────────────────────────────────────────────────────────────

def test_scope_moves_a_server_between_files(workspace: Path) -> None:
    add_server(workspace)
    outcome, text = run(["scope", "demo", "project"], workspace)
    assert outcome.code == 0 and "Moved 'demo' from user to project" in text
    assert outcome.reconnect == ["demo"]
    assert (workspace / ".mcp.json").is_file()
    assert [scope for scope, _p, _r in MCPConfig(workspace).definitions("demo")] == ["project"]


def test_scope_refuses_a_copy_that_already_exists(workspace: Path) -> None:
    add_server(workspace, scope="user")
    add_server(workspace, scope="local")
    outcome, text = run(["scope", "demo", "local", "--from", "user"], workspace)
    assert outcome.code == 1 and "already defined in local scope" in text


def test_scope_to_project_warns_about_literal_keys(workspace: Path) -> None:
    add_server(workspace, command="x", env={"MY_API_KEY": "sk-literal"})
    _, text = run(["scope", "demo", "project"], workspace)
    assert "Warning" in text and "MY_API_KEY" in text


def test_scope_rejects_a_bad_scope_and_unknown_server(workspace: Path) -> None:
    add_server(workspace)
    assert run(["scope", "demo", "galaxy"], workspace)[0].code == 1
    assert run(["scope", "ghost", "user"], workspace)[0].code == 1


def test_scope_moves_skills_and_plugins_too(workspace: Path) -> None:
    from axon.marketplace import skill_tools
    skill_tools.create_skill(skill_tools.NewSkill("mover", "Moves things", "Do it."), workspace, "user")
    outcome, text = run(["scope", "mover", "project", "--tab", "skills"], workspace)
    assert outcome.code == 0 and (workspace / ".axon" / "skills" / "mover" / "SKILL.md").is_file()
    assert run(["scope", "mover", "local", "--tab", "skills"], workspace)[0].code == 1


# ─── env, timeout ────────────────────────────────────────────────────────────

def test_env_saves_a_key_outside_the_config(workspace: Path, private_home: Path) -> None:
    add_server(workspace)
    outcome, text = run(["env", "demo", "MY_API_KEY=sk-secret"], workspace)
    assert outcome.code == 0 and outcome.reconnect == ["demo"]
    config_text = (private_home / ".axon" / "mcp.json").read_text()
    assert "sk-secret" not in config_text and "${MY_API_KEY}" in config_text
    assert "sk-secret" in (private_home / ".axon" / ".env").read_text()


def test_env_literal_and_unset(workspace: Path, private_home: Path) -> None:
    add_server(workspace, command="x", env={"OLD": "1"})
    run(["env", "demo", "MODE=fast", "--literal", "--unset", "OLD"], workspace)
    env = MCPConfig(workspace).get_spec("demo").env
    assert env == {"MODE": "fast"}


def test_env_without_a_value_and_no_terminal_fails(workspace: Path) -> None:
    add_server(workspace)
    outcome, text = run(["env", "demo", "MY_API_KEY"], workspace)
    assert outcome.code == 1 and "NAME=VALUE" in text


def test_env_on_a_remote_server_notes_missing_reference(workspace: Path) -> None:
    add_server(workspace, type="http", url="https://example.com/mcp")
    _, text = run(["env", "demo", "MY_API_KEY=abc"], workspace)
    assert "does not mention" in text


def test_timeout_is_saved_and_validated(workspace: Path) -> None:
    add_server(workspace)
    outcome, _ = run(["timeout", "demo", "45"], workspace)
    assert outcome.code == 0
    assert MCPConfig(workspace).get_spec("demo").timeout == 45.0
    assert run(["timeout", "demo", "0"], workspace)[0].code == 1
    assert run(["timeout", "demo", "soon"], workspace)[0].code == 1


# ─── per-tool permissions ────────────────────────────────────────────────────

def test_tool_policy_writes_rules_and_switching_replaces_the_old_one(workspace: Path) -> None:
    add_server(workspace)
    run(["tool", "demo", "create_issue", "allow"], workspace)
    local = workspace / ".axon" / "settings.local.json"
    assert json.loads(local.read_text())["permissions"]["allow"] == ["mcp__demo__create_issue"]
    run(["tool", "demo", "create_issue", "deny"], workspace)
    perms = json.loads(local.read_text())["permissions"]
    assert perms["deny"] == ["mcp__demo__create_issue"] and perms["allow"] == []
    _, text = run(["tool", "demo"], workspace)
    assert "deny" in text and "mcp__demo__create_issue" in text
    run(["tool", "demo", "create_issue", "reset"], workspace)
    assert "No allow/ask/deny rules" in run(["tool", "demo"], workspace)[1]


def test_tool_policy_for_the_whole_server_and_global_scope(workspace: Path, private_home: Path) -> None:
    add_server(workspace)
    run(["tool", "demo", "ask", "--global"], workspace)
    data = json.loads((private_home / ".axon" / "settings.json").read_text())
    assert data["permissions"]["ask"] == ["mcp__demo"]


def test_tool_policy_rejects_unknown_server_and_policy(workspace: Path) -> None:
    add_server(workspace)
    assert run(["tool", "ghost", "x", "allow"], workspace)[0].code == 1
    assert run(["tool", "demo", "x", "maybe"], workspace)[0].code == 1


def test_a_denied_tool_is_denied_by_the_engine(workspace: Path) -> None:
    from axon.permissions.engine import PermissionEngine
    from axon.config import Settings
    add_server(workspace)
    run(["tool", "demo", "danger", "deny"], workspace)
    settings = Settings.load({"workspace": workspace, "mode": "bypass", "api_key": "k-12345"})
    engine = PermissionEngine(settings)
    assert any(kind == "deny" and rule == "mcp__demo__danger" for kind, rule, _src in
               control.tool_policies(workspace, "demo", engine))


# ─── test, logs, reconnect ───────────────────────────────────────────────────

def test_test_reports_health_and_clears_failures(workspace: Path) -> None:
    add_server(workspace)
    mcp_health.record_failure("demo", "boom")
    outcome, text = run(["test", "demo"], workspace)
    assert outcome.code == 0 and "healthy: 2 tool(s)" in text and "Tools: a, b" in text
    assert mcp_health.get("demo").failures == 0


def test_test_reports_failure_reason(workspace: Path) -> None:
    add_server(workspace)
    bad = lambda spec, ws, timeout: HealthResult("failed", "connection refused")  # noqa: E731
    outcome, text = run(["test", "demo"], workspace, health=bad)
    assert outcome.code == 1 and "connection refused" in text
    assert mcp_health.get("demo").last_error == "connection refused"
    assert mcp_health.get("demo").failures == 0     # a manual test is not a failed connection cycle


def test_test_needs_login_hint_and_disabled_server(workspace: Path) -> None:
    add_server(workspace)
    auth = lambda spec, ws, timeout: HealthResult("needs-auth")  # noqa: E731
    assert "/mcp login demo" in run(["test", "demo"], workspace, health=auth)[1]
    run(["disable", "demo"], workspace)
    assert "disabled" in run(["test", "demo"], workspace)[1]


def test_test_checks_skills_and_plugins(workspace: Path) -> None:
    from axon.marketplace import plugin_tools, skill_tools
    skill_tools.create_skill(skill_tools.NewSkill("fine", "A fine skill", "Body."), workspace, "user")
    assert "is valid" in run(["test", "fine", "--tab", "skills"], workspace)[1]
    plugin_tools.create_new_plugin("myplug", workspace, "user")
    assert "is valid" in run(["test", "myplug", "--tab", "plugins"], workspace)[1]


def test_logs_show_the_tail(workspace: Path) -> None:
    from axon.mcp.transports import log_path_for
    add_server(workspace)
    log_path_for("demo").write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")
    _, text = run(["logs", "demo", "3"], workspace)
    assert "line 99" in text and "line 96" not in text
    assert "No log" in run(["logs", "other"], workspace)[1]


def test_reconnect_outside_a_session_runs_a_test(workspace: Path) -> None:
    add_server(workspace)
    outcome, text = run(["reconnect", "demo"], workspace)
    assert "No session is running" in text and "healthy" in text and outcome.reconnect == []


def test_reconnect_in_a_session_asks_the_session(workspace: Path) -> None:
    add_server(workspace)
    outcome, _ = run(["reconnect", "all"], workspace, agent=object())
    assert outcome.reconnect == ["demo"]
    assert run(["reconnect", "ghost"], workspace, agent=object())[0].code == 1


def test_server_commands_refuse_a_skill_name(workspace: Path) -> None:
    from axon.marketplace import skill_tools
    skill_tools.create_skill(skill_tools.NewSkill("alone", "Only a skill", "Body."), workspace, "user")
    outcome, text = run(["timeout", "alone", "30"], workspace)
    assert outcome.code == 1 and "connectors only" in text


# ─── skills on and off ───────────────────────────────────────────────────────

def test_disabled_skill_leaves_the_manager_and_the_prompt(workspace: Path) -> None:
    from axon.agent.prompt import skills_block
    from axon.marketplace import skill_tools
    from axon.skills.manager import SkillManager
    skill_tools.create_skill(skill_tools.NewSkill("pdf-helper", "Helps with PDFs", "Do PDFs."), workspace, "user")
    assert "pdf-helper" in SkillManager(workspace).skills
    outcome, text = run(["disable", "pdf-helper"], workspace)
    assert outcome.code == 0 and "Disabled skill" in text
    manager = SkillManager(workspace)
    assert "pdf-helper" not in manager.skills and "pdf-helper" in manager.disabled
    assert "pdf-helper" not in skills_block(list(manager.skills.values()), skill_tool=True)
    assert "disabled" in run(["list", "skills", "--installed"], workspace)[1]
    run(["enable", "pdf-helper"], workspace)
    assert "pdf-helper" in SkillManager(workspace).skills


def test_skill_disable_for_one_project_only(workspace: Path, tmp_path: Path) -> None:
    from axon.marketplace import skill_tools
    from axon.skills.manager import SkillManager
    skill_tools.create_skill(skill_tools.NewSkill("scoped-one", "Scoped", "Body."), workspace, "user")
    run(["disable", "scoped-one", "--scope", "project"], workspace)
    assert "scoped-one" not in SkillManager(workspace).skills
    other = tmp_path / "other"
    other.mkdir()
    assert "scoped-one" in SkillManager(other).skills


def test_enable_undoes_an_off_from_a_claude_settings_file(workspace: Path) -> None:
    from axon.marketplace import skill_tools
    from axon.skills.manager import SkillManager
    skill_tools.create_skill(skill_tools.NewSkill("claude-off", "Off elsewhere", "Body."), workspace, "user")
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"skillOverrides": {"claude-off": "off"}}))
    assert "claude-off" not in SkillManager(workspace).skills
    run(["enable", "claude-off"], workspace)
    assert "claude-off" in SkillManager(workspace).skills


def test_plugin_skills_are_switched_with_the_plugin(workspace: Path) -> None:
    result = control.set_skill_enabled(workspace, "nothing-here", False)
    assert not result.ok and "No skill called" in result.message


# ─── status dashboard ────────────────────────────────────────────────────────

def test_status_lists_every_kind_without_a_session(workspace: Path) -> None:
    from axon.marketplace import plugin_tools, skill_tools
    add_server(workspace, "alpha")
    add_server(workspace, "beta")
    run(["disable", "beta"], workspace)
    skill_tools.create_skill(skill_tools.NewSkill("my-skill", "Mine", "Body."), workspace, "user")
    plugin_tools.create_new_plugin("my-plugin", workspace, "user")
    outcome, text = run(["status"], workspace)
    assert outcome.code == 0
    for word in ("connector", "alpha", "beta", "skill", "my-skill", "plugin", "my-plugin", "KIND", "LAST ERROR"):
        assert word in text
    rows = {(r.kind, r.name): r for r in status.collect(workspace)}
    assert rows[("connector", "alpha")].connected == "-" and rows[("connector", "alpha")].enabled
    assert rows[("connector", "beta")].enabled is False and rows[("connector", "beta")].connected == "no"
    assert rows[("skill", "my-skill")].scope == "personal"


def test_status_hides_built_in_skills_unless_asked(workspace: Path) -> None:
    _, short = run(["status"], workspace)
    _, full = run(["status", "--all"], workspace)
    assert "code-review" not in short and "built-in skills are not listed" in short
    assert "code-review" in full and "not listed" not in full


def test_status_check_connects_and_reports_errors(workspace: Path) -> None:
    add_server(workspace, "good")
    add_server(workspace, "bad")

    def health(spec, ws, timeout):
        if spec.name == "bad":
            return HealthResult("failed", "no such host")
        return HealthResult("connected", tools=["x", "y", "z"])

    _, text = run(["status", "--check"], workspace, health=health)
    good = next(line for line in text.splitlines() if " good " in line)
    bad = next(line for line in text.splitlines() if " bad " in line)
    assert "yes" in good and " 3 " in good
    assert "no" in bad and "no such host" in bad


def test_status_json_and_empty(workspace: Path) -> None:
    add_server(workspace)
    rows = json.loads(run(["status", "--json"], workspace)[1])
    assert any(row["kind"] == "connector" and row["name"] == "demo" for row in rows)


def test_status_uses_the_live_session_connections(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    add_server(workspace)

    class Live:
        connected = True
        tools = [1, 2, 3, 4]
        error = None

    monkeypatch.setattr(status, "live_connections", lambda: {"demo": Live()})
    row = next(r for r in status.collect(workspace) if r.name == "demo")
    assert row.connected == "yes" and row.tools == "4"


def test_status_shows_auto_disabled_servers(workspace: Path) -> None:
    add_server(workspace, "flaky")
    run(["disable", "flaky"], workspace)
    mcp_health.mark_auto_disabled("flaky")
    row = next(r for r in status.collect(workspace) if r.name == "flaky")
    assert not row.enabled and "repeated failures" in row.last_error


# ─── info for installed things ───────────────────────────────────────────────

def test_info_for_a_custom_connector_shows_installed_details(workspace: Path) -> None:
    add_server(workspace, "mine", command="x", env={"A": "1"})
    run(["tool", "mine", "t", "allow"], workspace)
    outcome, text = run(["info", "mine"], workspace)
    assert outcome.code == 0
    for word in ("custom connector", "scope", "user", "timeout", "env", "mcp__mine__t"):
        assert word in text


def test_info_for_a_catalog_connector_adds_installed_block(workspace: Path) -> None:
    run(["install", "memory"], workspace)
    _, text = run(["info", "memory"], workspace)
    assert "Installed:" in text and "timeout" in text


def test_enable_clears_the_failure_count(workspace: Path) -> None:
    add_server(workspace)
    mcp_health.record_failure("demo", "x")
    mcp_health.mark_auto_disabled("demo")
    run(["disable", "demo"], workspace)
    run(["enable", "demo"], workspace)
    assert mcp_health.get("demo").failures == 0 and not mcp_health.get("demo").auto_disabled
