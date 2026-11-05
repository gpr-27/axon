"""
Regression tests for the defects found in the src/axon/commands code review: each test fails on the
code as it was before the fix.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeProvider
from pydantic import SecretStr
from test_core_helpers import make_agent

import menu_sweep
from axon.commands.registry import dispatch_command


@pytest.fixture
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon-home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    return home


@pytest.fixture
def agent(workspace: Path, axon_home: Path):
    return make_agent(FakeProvider([]), workspace, mode="default")


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)


# ─── crashes on odd arguments ────────────────────────────────────────────────

def test_agents_create_with_only_the_global_flag_asks_for_a_name(agent, capsys) -> None:
    dispatch_command("/agents create --global", agent)
    out = capsys.readouterr().out
    assert "failed" not in out and "IndexError" not in out
    assert "Usage" in out and "/agents create" in out


def test_subagents_edit_only_matches_the_word_edit(agent, monkeypatch) -> None:
    """A subagent titled "editor ..." must not be taken for "/subagents edit <n>"."""
    calls: list[str] = []
    monkeypatch.setattr("axon.commands.agent_cmds._parent_tasks", lambda *a: [])
    monkeypatch.setattr("axon.commands.parity_cmds.edit_subagent_result", lambda a, q: calls.append(q))
    dispatch_command("/subagents editor", agent)
    assert calls == []
    dispatch_command("/subagents edit 1", agent)
    assert calls == ["1"]


def test_queue_drop_without_an_id_does_not_queue_the_word_drop(agent, capsys) -> None:
    dispatch_command("/queue drop", agent)
    assert len(agent.message_queue) == 0
    assert "Usage" in capsys.readouterr().out


def test_queue_prompt_that_starts_with_remove_is_queued(agent) -> None:
    dispatch_command("/queue remove the stale cache files", agent)
    assert [it.text for it in agent.message_queue.items] == ["remove the stale cache files"]
    item_id = agent.message_queue.items[0].id
    dispatch_command(f"/queue drop {item_id}", agent)
    assert len(agent.message_queue) == 0


# ─── wrong results / wrong side effects ──────────────────────────────────────

def test_cost_all_does_not_need_a_session_ledger(monkeypatch) -> None:
    from axon.commands.context_cmds import handle_cost
    seen: list[bool] = []
    monkeypatch.setattr("axon.commands.context_cmds.usage_log_summary",
                        lambda agent, project_only, since=None: seen.append(project_only))
    bare = SimpleNamespace(settings=SimpleNamespace(model="m", workspace=Path(".")), ledger=None, session=None)
    handle_cost(bare, "--all")
    handle_cost(bare, "--project")
    assert seen == [False, True]


def test_notify_with_an_unknown_word_sends_nothing(agent, monkeypatch, capsys) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("axon.ui.notify.send_desktop_notification", lambda title, msg: sent.append((title, msg)) or True)
    for line in ("/notify on", "/notify after", "/notify hello there"):
        dispatch_command(line, agent)
    assert sent == []
    assert "Usage" in capsys.readouterr().out
    dispatch_command("/notify test it works", agent)
    assert sent == [("Axon", "it works")]


def test_statusbar_off_keeps_an_already_hidden_bar_hidden(agent, monkeypatch) -> None:
    from axon.ui.statusbar import StatusBar
    monkeypatch.setattr(StatusBar, "_enabled", False)
    dispatch_command("/statusbar off", agent)
    assert StatusBar._enabled is False
    dispatch_command("/statusbar on", agent)
    assert StatusBar._enabled is True
    dispatch_command("/statusbar on", agent)
    assert StatusBar._enabled is True


def test_diff_in_a_terminal_outside_a_git_repo_says_so(agent, tty, monkeypatch, capsys) -> None:
    from axon.commands.code_cmds import handle_diff
    monkeypatch.setattr("axon.commands.code_cmds.is_tty", lambda: True)
    handle_diff(agent, "")                     # the file viewer, not the plain text
    out = capsys.readouterr().out.lower()
    assert "not a git repository" in out and "clean" not in out


# ─── secrets ─────────────────────────────────────────────────────────────────

def test_endpoint_test_never_sends_the_session_key_to_an_unknown_host(workspace, monkeypatch) -> None:
    from axon.commands.endpoint_cmds import test_target
    from axon.providers import endpoints as ep
    sent: list[tuple[str, str]] = []

    def fake_test(url, key="", api_format="openai", timeout=6.0):
        sent.append((url, key))
        return ep.EndpointTest(reachable=True, ok=False, message="stub")

    monkeypatch.setattr(ep, "test_endpoint", fake_test)
    agent = SimpleNamespace(settings=SimpleNamespace(api_key=SecretStr("sk-session-secret-1234567890"),
                                                     base_url="https://gateway.internal.example/v1"))
    test_target(agent, "https://other-host.example/v1")
    assert sent == [("https://other-host.example/v1", "")]
    sent.clear()
    test_target(agent, "https://gateway.internal.example/v1")      # the URL the session already talks to
    assert sent == [("https://gateway.internal.example/v1", "sk-session-secret-1234567890")]


def test_bug_report_masks_keys_typed_into_the_description(agent, axon_home) -> None:
    key = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    dispatch_command(f"/bug it fails when I use {key}", agent)
    reports = list((axon_home / "bug-reports").glob("bug-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert key not in text and "it fails when I use" in text


# ─── scopes and files ────────────────────────────────────────────────────────

def test_changing_a_session_rule_keeps_it_session_only(agent, workspace) -> None:
    from axon.commands.permissions_menu import change_rule
    agent.permissions.add_session_rule("allow", "Read(src/**)")
    change_rule(agent, "allow", "ask", "Read(src/**)", "session")
    assert [(e.kind, str(e.rule), e.source) for e in agent.permissions.list_rules()] == [("ask", "Read(src/**)", "session")]
    assert not (workspace / ".axon" / "settings.local.json").exists()


def test_changing_a_user_rule_keeps_it_in_the_user_settings(agent, workspace, axon_home) -> None:
    from axon.commands.permissions_menu import change_rule
    (axon_home / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read(src/**)"]}}), encoding="utf-8")
    agent.permissions.reload()
    assert [e.source for e in agent.permissions.list_rules()] == ["user"]
    change_rule(agent, "allow", "deny", "Read(src/**)", "user")
    data = json.loads((axon_home / "settings.json").read_text(encoding="utf-8"))["permissions"]
    assert data["allow"] == [] and data["deny"] == ["Read(src/**)"]
    assert not (workspace / ".axon" / "settings.local.json").exists()


def _plugin(home: Path, name: str) -> None:
    d = home / "plugins" / name
    d.mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")


def test_disabling_a_plugin_for_one_project_does_not_disable_it_everywhere(tmp_path, axon_home) -> None:
    from axon.commands import plugins as pl
    from axon.commands.plugin_cmds import run_plugin_command
    _plugin(axon_home, "foo")
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=one))
    run_plugin_command(agent, "disable foo --scope project", lambda a: None, lambda a, t: None)
    assert [p.enabled for p in pl.load_plugins(one)] == [False]
    assert [p.enabled for p in pl.load_plugins(two)] == [True]


def test_plugin_commands_never_overwrite_a_settings_file_they_cannot_parse(tmp_path, axon_home, capsys) -> None:
    from axon.commands import plugins as pl
    from axon.commands.plugin_cmds import run_plugin_command
    _plugin(axon_home, "foo")
    broken = '{"model": "x",\n // a comment\n "theme": "dark"}'
    (axon_home / "settings.json").write_text(broken, encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws))
    run_plugin_command(agent, "disable foo --scope user", lambda a: None, lambda a, t: None)
    assert (axon_home / "settings.json").read_text(encoding="utf-8") == broken
    assert "not valid JSON" in capsys.readouterr().out
    with pytest.raises(ValueError):
        pl.set_enabled_in_settings(ws, "foo", None, "user")
    assert (axon_home / "settings.json").read_text(encoding="utf-8") == broken


def test_removing_a_setting_does_not_create_empty_settings_files(tmp_path, axon_home) -> None:
    from axon.commands import plugins as pl
    ws = tmp_path / "ws"
    ws.mkdir()
    for scope in ("user", "project", "local"):
        pl.set_enabled_in_settings(ws, "foo@bar", None, scope)
    assert not (ws / ".axon").exists() and not (axon_home / "settings.json").exists()


# ─── hints in a terminal ─────────────────────────────────────────────────────

def _hints(out: str) -> list[str]:
    return menu_sweep.hint_lines(out)


def test_tasks_with_nothing_running_gives_no_typed_hint_in_a_terminal(agent, tty, monkeypatch, capsys) -> None:
    monkeypatch.setattr("axon.commands.agent_cmds._background_shells", lambda: [])
    from axon.commands.agent_cmds import handle_tasks
    handle_tasks(agent, "")
    out = capsys.readouterr().out
    assert "No background work" in out and not _hints(out)


def test_skill_doctor_without_skills_gives_no_typed_hint_in_a_terminal(agent, tty, capsys) -> None:
    from axon.commands.insight_cmds import handle_skill_doctor
    agent.skills = SimpleNamespace(skills={})
    handle_skill_doctor(agent, "")
    out = capsys.readouterr().out
    assert "No skills installed" in out and not _hints(out)


def test_notify_report_gives_no_typed_hint_in_a_terminal(agent, tty, capsys) -> None:
    from axon.commands.system_cmds import handle_notify
    handle_notify(agent, "")
    out = capsys.readouterr().out
    assert "Desktop notifications" in out and not _hints(out)


def test_plan_already_on_gives_no_typed_hint_in_a_terminal(workspace, tty, axon_home, capsys) -> None:
    from axon.commands.model_cmds import handle_plan
    planner = make_agent(FakeProvider([]), workspace, mode="plan")
    handle_plan(planner, "")
    out = capsys.readouterr().out
    assert "already on" in out and not _hints(out)


def test_faq_topics_line_is_a_hint_for_scripts_only(agent, tty, monkeypatch, capsys) -> None:
    from axon.commands.context_cmds import handle_faq
    handle_faq(agent, "402")
    assert "Topics:" not in capsys.readouterr().out
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: False)
    handle_faq(agent, "402")
    assert "Topics:" in capsys.readouterr().out


def test_text_mode_keeps_the_typed_follow_ups(agent, monkeypatch, capsys) -> None:
    from axon.commands.agent_cmds import handle_tasks
    monkeypatch.setattr("axon.commands.agent_cmds._background_shells", lambda: [])
    handle_tasks(agent, "")
    assert "/todos" in capsys.readouterr().out


# ─── memory scope ────────────────────────────────────────────────────────────

def test_memory_add_only_treats_flags_as_the_scope(agent, workspace, axon_home) -> None:
    from axon.agent.memory import MemoryStore
    dispatch_command("/memory add global state must be avoided", agent)
    dispatch_command("#user prefers tabs", agent)
    items = MemoryStore(workspace).list_all()
    by_text = {it.content.strip(): it.scope for it in items}
    assert by_text == {"global state must be avoided": "project", "user prefers tabs": "project"}
    dispatch_command("/memory add --global answers in French", agent)
    dispatch_command("#global always run the tests", agent)
    scopes = {it.content.strip(): it.scope for it in MemoryStore(workspace).list_all()}
    assert scopes["answers in French"] == "global" and scopes["always run the tests"] == "global"
