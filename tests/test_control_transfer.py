"""Export / import of the whole setup, and the same controls through /mcp, /skills, /plugin and /marketplace."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.marketplace import cli, control, plugin_tools, skill_tools, transfer
from axon.marketplace.catalog import load_plugins
from axon.marketplace.installer import HealthResult
from axon.mcp.config import MCPConfig, ServerSpec


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    return home


def run(tokens: list[str], workspace: Path, agent=None):
    lines: list[str] = []
    shop = cli.Marketplace(workspace, out=lines.append, agent=agent,
                           health_check=lambda s, w, t: HealthResult("connected", tools=["t"]))
    return shop.run(tokens), "\n".join(lines)


def new_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Switch to a fresh AXON_HOME and a fresh workspace, as if on another computer."""
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    ws = tmp_path / "ws2"
    ws.mkdir()
    return ws


def build_setup(workspace: Path) -> None:
    config = MCPConfig(workspace)
    config.save_spec(ServerSpec.from_dict("plain", {"command": "npx", "args": ["-y", "pkg"], "timeout": 45}), "user")
    config.save_spec(ServerSpec.from_dict(
        "keyed", {"command": "srv", "env": {"MY_API_KEY": "sk-literal-secret", "MODE": "fast"}}), "project")
    config.save_spec(ServerSpec.from_dict(
        "web", {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer abc123"}}), "local")
    skill_tools.create_skill(skill_tools.NewSkill("my-skill", "Mine", "Do it.", ["do it"],
                                                  {"run.sh": "echo hi\n"}), workspace, "user")
    skill_tools.create_skill(skill_tools.NewSkill("off-skill", "Switched off", "Body."), workspace, "project")
    run(["disable", "off-skill"], workspace)
    run(["tool", "plain", "danger", "deny"], workspace)


# ─── export ──────────────────────────────────────────────────────────────────

def test_export_masks_secrets_and_lists_the_keys_needed(workspace: Path, tmp_path: Path) -> None:
    build_setup(workspace)
    path = transfer.write_export(workspace, tmp_path / "setup.json")
    text = path.read_text()
    assert "sk-literal-secret" not in text and "abc123" not in text
    data = json.loads(text)
    keyed = next(c for c in data["connectors"] if c["name"] == "keyed")
    assert keyed["config"]["env"] == {"MY_API_KEY": "${MY_API_KEY}", "MODE": "fast"}
    assert keyed["needs_keys"] == ["MY_API_KEY"] and keyed["scope"] == "project"
    web = next(c for c in data["connectors"] if c["name"] == "web")
    assert web["config"]["headers"]["Authorization"] == "Bearer ${AUTHORIZATION}"
    assert data["format"] == "axon-setup" and data["includes_secrets"] is False


def test_export_names_skills_plugins_permissions_and_disabled_skills(workspace: Path, tmp_path: Path) -> None:
    build_setup(workspace)
    plugin_tools.create_new_plugin("my-plugin", workspace, "user")
    data = transfer.export_setup(workspace)
    skill = next(s for s in data["skills"] if s["name"] == "my-skill")
    assert set(skill["files"]) == {"SKILL.md", "scripts/run.sh"}
    assert [p["name"] for p in data["plugins"]] == ["my-plugin"]
    assert data["permissions"]["deny"] == ["mcp__plain__danger"]
    assert data["disabled_skills"] == ["off-skill"]
    assert data["automation"]["enabled"] is False


def test_export_with_secrets_is_owner_only(workspace: Path, tmp_path: Path) -> None:
    build_setup(workspace)
    path = transfer.write_export(workspace, tmp_path / "full.json", include_secrets=True)
    assert "sk-literal-secret" in path.read_text()
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_export_command_default_file_and_message(workspace: Path) -> None:
    build_setup(workspace)
    outcome, text = run(["export"], workspace)
    assert outcome.code == 0 and (workspace / "axon-setup.json").is_file() and "Keys are not in the file" in text


# ─── import ──────────────────────────────────────────────────────────────────

def test_round_trip_to_a_fresh_machine(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_setup(workspace)
    export = transfer.write_export(workspace, tmp_path / "setup.json")
    ws2 = new_machine(tmp_path, monkeypatch)
    outcome, text = run(["import", str(export)], ws2)
    assert outcome.code == 0 and sorted(outcome.reconnect) == ["keyed", "plain", "web"]
    specs = MCPConfig(ws2).load_specs()
    assert specs["plain"].timeout == 45 and specs["plain"].scope == "user"
    assert specs["keyed"].scope == "project" and specs["keyed"].env["MY_API_KEY"] == "${MY_API_KEY}"
    assert "needs a key: /marketplace env keyed MY_API_KEY" in text
    from axon.skills.manager import SkillManager
    manager = SkillManager(ws2)
    assert "my-skill" in manager.skills and "off-skill" in manager.disabled
    assert (skill_tools.skills_root(ws2, "user") / "my-skill" / "scripts" / "run.sh").read_text() == "echo hi\n"
    assert control.tool_policies(ws2, "plain") == [("deny", "mcp__plain__danger", "local")]


def test_import_twice_changes_nothing_the_second_time(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_setup(workspace)
    export = transfer.write_export(workspace, tmp_path / "setup.json")
    ws2 = new_machine(tmp_path, monkeypatch)
    run(["import", str(export)], ws2)
    outcome, text = run(["import", str(export)], ws2)
    assert outcome.reconnect == [] and "already installed" in text


def test_dry_run_writes_nothing(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_setup(workspace)
    export = transfer.write_export(workspace, tmp_path / "setup.json")
    ws2 = new_machine(tmp_path, monkeypatch)
    outcome, text = run(["import", str(export), "--dry-run"], ws2)
    assert "would add" in text and "Dry run" in text and outcome.reconnect == []
    assert MCPConfig(ws2).load_specs() == {}


def test_a_different_server_with_the_same_name_needs_overwrite(workspace: Path, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    build_setup(workspace)
    export = transfer.write_export(workspace, tmp_path / "setup.json")
    ws2 = new_machine(tmp_path, monkeypatch)
    MCPConfig(ws2).save_spec(ServerSpec.from_dict("plain", {"command": "other"}), "user")
    assert "different server uses this name" in run(["import", str(export)], ws2)[1]
    assert MCPConfig(ws2).get_spec("plain").command == "other"
    run(["import", str(export), "--overwrite"], ws2)
    assert MCPConfig(ws2).get_spec("plain").command == "npx"


def test_import_never_takes_auto_install_from_a_file(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.marketplace import automation
    file = tmp_path / "evil.json"
    file.write_text(json.dumps({"format": "axon-setup", "version": 1,
                                "automation": {"enabled": True, "auto_install": True, "auto_update": True, "suggest": False}}))
    ws2 = new_machine(tmp_path, monkeypatch)
    _, text = run(["import", str(file)], ws2)
    auto = automation.load_automation(ws2)
    assert auto.enabled and auto.suggest is False and not auto.auto_install and not auto.auto_update
    assert "never imported" in text


def test_import_rejects_bad_files(workspace: Path, tmp_path: Path) -> None:
    assert run(["import", str(tmp_path / "missing.json")], workspace)[0].code == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert "not valid JSON" in run(["import", str(bad)], workspace)[1]
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"hello": 1}))
    assert "not an Axon setup export" in run(["import", str(other)], workspace)[1]
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"format": "axon-setup", "version": 99}))
    assert "unsupported export version" in run(["import", str(future)], workspace)[1]
    assert run(["import"], workspace)[0].code == 1


def test_import_ignores_unsafe_skill_paths_and_broken_entries(workspace: Path, tmp_path: Path) -> None:
    file = tmp_path / "tricky.json"
    file.write_text(json.dumps({
        "format": "axon-setup", "version": 1,
        "skills": [{"name": "sneaky", "scope": "user",
                    "files": {"SKILL.md": "---\nname: sneaky\ndescription: d\n---\n\nbody\n", "../../escape.txt": "x",
                              "/abs.txt": "y"}},
                   {"name": "../bad", "files": {"SKILL.md": "x"}}, "junk"],
        "connectors": [{"name": "broken"}, {"name": "ok", "config": {"command": "x"}}, 5]}))
    _, text = run(["import", str(file)], workspace)
    assert "skipped skill" in text and "skipped connector 'broken'" in text
    assert (skill_tools.skills_root(workspace, "user") / "sneaky" / "SKILL.md").is_file()
    assert not (skill_tools.skills_root(workspace, "user").parent / "escape.txt").exists()
    assert "ok" in MCPConfig(workspace).load_specs()


def test_catalog_plugins_are_reinstalled_and_others_are_listed(workspace: Path, tmp_path: Path) -> None:
    catalog_name = load_plugins()[0].name
    file = tmp_path / "plugins.json"
    file.write_text(json.dumps({"format": "axon-setup", "version": 1, "plugins": [
        {"name": catalog_name, "marketplace": plugin_tools.MARKETPLACE_NAME, "scope": "user"},
        {"name": "outside", "marketplace": "somewhere"}]}))
    calls = []
    data = transfer.read_setup(file)
    report = transfer.import_setup(workspace, data, plugin_installer=lambda entry, ws, scope: calls.append(entry.name))
    assert calls == [catalog_name]
    assert any("/plugin install outside@somewhere" in line for line in report.lines)


# ─── the same controls through the other commands ────────────────────────────

class FakeAgent:
    def __init__(self, workspace: Path) -> None:
        from types import SimpleNamespace
        self.settings = SimpleNamespace(workspace=workspace)
        self.registry = None
        self.permissions = control.permission_engine(workspace)
        self.skills = None


def test_mcp_command_runs_the_marketplace_controls(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.mcp.commands import handle_mcp_command
    from axon.mcp.bridge import shutdown_mcp_bridge
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("demo", {"command": "x"}), "user")
    agent = FakeAgent(workspace)
    handle_mcp_command(agent, "timeout demo 33")
    assert MCPConfig(workspace).get_spec("demo").timeout == 33
    handle_mcp_command(agent, "tool demo * ask")
    assert control.tool_policies(workspace, "demo") == [("ask", "mcp__demo", "local")]
    handle_mcp_command(agent, "scope demo local")
    assert MCPConfig(workspace).get_spec("demo").scope == "local"
    handle_mcp_command(agent, "dashboard")
    assert "demo" in capsys.readouterr().out
    shutdown_mcp_bridge()


def test_mcp_cli_form_does_not_need_a_session(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.mcp.commands import handle_mcp_command
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("demo", {"command": "x"}), "user")
    handle_mcp_command(FakeAgent(workspace), ["timeout", "demo", "12"])
    assert MCPConfig(workspace).get_spec("demo").timeout == 12


def test_mcp_import_with_a_file_loads_a_setup(workspace: Path, tmp_path: Path) -> None:
    from axon.mcp.commands import handle_mcp_command
    file = tmp_path / "s.json"
    file.write_text(json.dumps({"format": "axon-setup", "version": 1,
                                "connectors": [{"name": "fromfile", "config": {"command": "x"}}]}))
    agent = FakeAgent(workspace)
    handle_mcp_command(agent, f"import {file}")
    assert "fromfile" in MCPConfig(workspace).load_specs()


def test_skills_command_enable_disable_status(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.skills.interactive import handle_skills_command
    from axon.skills.manager import SkillManager
    skill_tools.create_skill(skill_tools.NewSkill("my-skill", "Mine", "Do it."), workspace, "user")
    manager = SkillManager(workspace)
    handle_skills_command(manager, workspace, "disable my-skill")
    assert "my-skill" not in manager.skills
    handle_skills_command(manager, workspace, "list")
    assert "Disabled" in capsys.readouterr().out
    handle_skills_command(manager, workspace, "enable my-skill")
    assert "my-skill" in manager.skills
    handle_skills_command(manager, workspace, "status")
    assert "my-skill" in capsys.readouterr().out
    handle_skills_command(manager, workspace, "scope my-skill project")
    assert (workspace / ".axon" / "skills" / "my-skill" / "SKILL.md").is_file()
    handle_skills_command(manager, workspace, "test my-skill")
    assert "is valid" in capsys.readouterr().out


def test_plugin_command_status_test_scope(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.plugin_cmds import run_plugin_command
    plugin_tools.create_new_plugin("my-plugin", workspace, "user")
    agent = FakeAgent(workspace)
    noop = lambda *a, **k: None  # noqa: E731
    run_plugin_command(agent, "status", noop, noop)
    assert "my-plugin" in capsys.readouterr().out
    run_plugin_command(agent, "test my-plugin", noop, noop)
    assert "is valid" in capsys.readouterr().out
    run_plugin_command(agent, "scope my-plugin project", noop, noop)
    assert (workspace / ".axon" / "plugins" / "my-plugin").is_dir()


def test_marketplace_session_handler_follows_changes(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.commands import marketplace_cmds
    from axon.mcp import commands as mcp_commands
    MCPConfig(workspace).save_spec(ServerSpec.from_dict("demo", {"command": "x"}), "user")
    seen: list[str] = []
    monkeypatch.setattr(mcp_commands, "handle_mcp_command", lambda agent, arg: seen.append(arg))
    agent = FakeAgent(workspace)
    marketplace_cmds.handle_marketplace(agent, "timeout demo 20")
    marketplace_cmds.handle_marketplace(agent, "reconnect all")
    assert seen == ["reconnect demo", "reconnect demo"]
    removed: list[str] = []
    from axon.mcp import bridge
    monkeypatch.setattr(bridge.MCPToolBridge, "disconnect_server", lambda self, name: removed.append(name) or True)
    marketplace_cmds.handle_marketplace(agent, "remove demo")
    assert removed == ["demo"]


def test_help_lists_the_new_commands(workspace: Path) -> None:
    _, text = run(["help"], workspace)
    for word in ("status", "test", "scope", "env", "timeout", "tool", "export", "import", "auto", "suggest", "updates"):
        assert f"marketplace {word}" in text
    assert run(["frobnicate"], workspace)[0].code == 1
