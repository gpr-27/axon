"""
Keyboard menus: a bare command in a terminal opens a menu, arguments keep the typed behaviour, and
without a terminal the plain text comes out as before.

Two ways to drive the menus:
* ``keys([...])`` scripts real key presses through axon.platform.read_key, so the shared Selector
  (tabs, "/" filter, Esc, Enter) runs exactly as in a terminal;
* ``picks([...])`` replaces axon.ui.menus._select and chooses rows by label, which keeps the action
  matrix tests short. ``None`` in the list (or running out) means Esc.
"""
from __future__ import annotations

import contextlib
import re
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon import platform as plat
from axon.ui import menus

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain(text: str) -> str:
    return ANSI.sub("", text)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch):
    def install(seq: list[str]) -> deque:
        queue = deque(seq)

        def fake_read_key(timeout=None, report_resize=True):
            if not queue:
                raise AssertionError("ran out of scripted keys")
            return queue.popleft()
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
        return queue
    return install


@pytest.fixture
def picks(monkeypatch: pytest.MonkeyPatch):
    """Choose menu rows by label prefix. Returns the list of (title, labels) menus that were shown."""
    shown: list[tuple[str, list[str]]] = []
    bodies: list[str] = []   # the text of each detail screen, joined

    def install(choices: list[str | None]) -> list[tuple[str, list[str]]]:
        queue = deque(choices)

        def fake_select(items, title, subtitle="", index=0, body=None):
            labels = [plain(i.label) for i in items]
            shown.append((title, labels))
            bodies.append("\n".join(line.text for line in body or []))
            if not queue:
                return None
            wanted = queue.popleft()
            if wanted is None:
                return None
            for item, label in zip(items, labels):
                if label == wanted or label.startswith(wanted):
                    return item
            raise AssertionError(f"no row {wanted!r} in menu {title!r}: {labels}")
        monkeypatch.setattr(menus, "_select", fake_select)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        return shown
    install.bodies = bodies  # type: ignore[attr-defined]
    return install


@pytest.fixture
def axon_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axon-home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "RENDER_API_KEY"):
        monkeypatch.setenv(name, "")
    return home


def connected(spec, workspace, timeout):
    from axon.marketplace.installer import HealthResult
    return HealthResult("connected", tools=["search", "fetch"])


# ─── /mcp ───────────────────────────────────────────────────────────────────

def _spec(**extra: Any) -> SimpleNamespace:
    base = dict(disabled=False, approval="approved", managed=False, scope="user", type="stdio",
                has_authorization_header=False, url="", describe_target=lambda: "python server.py",
                config_warnings=lambda: [])
    base.update(extra)
    return SimpleNamespace(**base)


def _mcp_ctx(specs: dict[str, Any], tools: list[Any] | None = None) -> SimpleNamespace:
    config = SimpleNamespace(load_specs=lambda: specs, get_spec=lambda n: specs.get(n), notices=[], blocked=[],
                             problems=[], conflicts={})
    bridge = SimpleNamespace(connections={}, active_tools=tools or [], excluded={},
                             status_rows=lambda: [{"name": n, "status": "connected", "transport": "stdio", "tools": 1,
                                                   "warnings": []} for n in specs])
    return SimpleNamespace(config=config, bridge=bridge, cli=False, workspace=Path("."))


def _tool() -> SimpleNamespace:
    return SimpleNamespace(name="mcp__srv__echo", server_name="srv", readonly=True,
                           description="[MCP:srv] Echo the text back", full_description="[MCP:srv] Echo the text back",
                           schema={"type": "object", "properties": {"text": {"type": "string"}}})


def test_mcp_choose_server_then_tools_shows_description_and_schema(picks, monkeypatch, capsys) -> None:
    from axon.mcp import commands as c
    monkeypatch.setattr(c.approval, "pending", lambda specs: False)
    ctx = _mcp_ctx({"srv": _spec()}, [_tool()])
    shown = picks(["srv", "Tools", "mcp__srv__echo"])  # then Esc, Esc, Esc
    c._menu(ctx)
    out = plain(capsys.readouterr().out)
    assert "Echo the text back" in out and "Input schema" in out and '"text"' in out
    assert [title for title, _ in shown][:3] == ["MCP servers", "srv", "srv tools"]


def test_mcp_list_is_printed_once(monkeypatch, capsys) -> None:
    """Regression: /mcp showed the 'MCP servers (2)' block twice (the text list, then the menu's
    'List servers'). In a terminal the list is the menu; without one it is printed once."""
    from axon.mcp import commands as c
    ctx = _mcp_ctx({"a": _spec(), "b": _spec()})
    monkeypatch.setattr(c, "_Ctx", lambda agent: ctx)
    monkeypatch.setattr(c.approval, "pending", lambda specs: False)
    monkeypatch.setattr(c, "_interactive", lambda: False)
    c.handle_mcp_command(SimpleNamespace(), "")
    out = plain(capsys.readouterr().out)
    assert out.count("MCP servers (2)") == 1
    assert "/mcp get <name>" in out  # the hint line stays without a terminal

    titles: list[str] = []
    monkeypatch.setattr(c, "_interactive", lambda: True)
    monkeypatch.setattr(menus, "_select", lambda items, title, *a, **k: titles.append(title))
    c.handle_mcp_command(SimpleNamespace(), "")
    out = plain(capsys.readouterr().out)
    assert out.count("MCP servers (") == 0 and titles == ["MCP servers"]
    assert "/mcp get <name>" not in out


MCP_ACTIONS = {  # action label -> (function patched, server spec that offers it)
    "Details": ("_cmd_get", {}), "Tools": ("_tools_menu", {}), "Reconnect": ("_cmd_restart", {}),
    "Logs": ("_cmd_logs", {}), "Sign in": ("_cmd_login", {"type": "http"}),
    "Sign out": ("_cmd_logout", {"type": "http"}), "Approve": ("_cmd_approve", {"approval": "pending"}),
    "Disable": ("_cmd_enable", {}), "Enable": ("_cmd_enable", {"disabled": True}), "Remove": ("_cmd_remove", {}),
}


@pytest.mark.parametrize("label", sorted(MCP_ACTIONS))
def test_mcp_each_action_runs_its_own_handler_once(label: str, picks, monkeypatch) -> None:
    from axon.mcp import commands as c
    target, extra = MCP_ACTIONS[label]
    calls: list[str] = []
    for name in {t for t, _ in MCP_ACTIONS.values()}:
        monkeypatch.setattr(c, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(c.approval, "pending", lambda specs: False)
    ctx = _mcp_ctx({"srv": _spec(**extra)})
    picks(["srv", label, "Yes"] if label == "Remove" else ["srv", label])
    c._menu(ctx)
    assert calls == [target]


def test_mcp_actions_fit_the_server(monkeypatch) -> None:
    from axon.mcp import commands as c
    stdio = [a.label for a in c._server_actions(_mcp_ctx({"s": _spec()}), "s")]
    assert "Sign in" not in stdio and "Enable" not in stdio and "Disable" in stdio
    managed = [a.label for a in c._server_actions(_mcp_ctx({"s": _spec(managed=True)}), "s")]
    assert "Remove" not in managed and "Disable" not in managed


def test_mcp_remove_needs_confirmation(picks, monkeypatch) -> None:
    from axon.mcp import commands as c
    removed: list[Any] = []
    monkeypatch.setattr(c, "_cmd_remove", lambda ctx, args: removed.append(args))
    monkeypatch.setattr(c.approval, "pending", lambda specs: False)
    picks(["srv", "Remove", "No"])
    c._menu(_mcp_ctx({"srv": _spec()}))
    assert removed == []


# ─── /marketplace ───────────────────────────────────────────────────────────

def _shop(workspace: Path, health=connected, out: list[str] | None = None):
    from axon.marketplace import cli
    lines = out if out is not None else []
    return cli.Marketplace(workspace, out=lines.append, ask_line=lambda p: "", ask_secret=lambda p: "",
                           health_check=health)


def test_marketplace_switch_tab_filter_and_open(workspace, axon_home, keys, capsys) -> None:
    lines: list[str] = []
    shop = _shop(workspace, out=lines)
    keys(["right", "/", *"frontend", "enter",   # Skills tab, filter, open the item
          "esc",                                  # back from its actions
          "esc"])                                 # close the browser
    outcome = shop.run([])
    assert outcome.code == 0
    text = plain("\n".join(lines))
    assert "frontend-design" in text  # the detail panel of the opened skill
    screen = plain(capsys.readouterr().out)
    assert "Skills" in screen and "⌕" in screen


def test_marketplace_y_toggles_yours(workspace, axon_home, keys, capsys) -> None:
    keys(["y", "esc"])
    _shop(workspace).run([])
    assert "[Yours] Discover" in plain(capsys.readouterr().out)


def test_marketplace_guided_install_then_remove_with_confirmation(workspace, axon_home, keys) -> None:
    from axon.marketplace import installer
    shop = _shop(workspace)
    keys(["/", *"context7", "enter",   # open Context7
          "enter",                        # Install
          "enter",                        # scope: user (preselected)
          "esc",                          # next steps: back to the list
          "esc"])
    shop.run([])
    assert "context7" in installer.installed_names(workspace)
    assert "context7" in shop.outcome.installed

    keys(["y", "enter",                  # Yours -> Context7
          *["down"] * 6, "enter",         # Remove
          "enter",                        # confirm: No (preselected) keeps it
          "enter", *["down"] * 6, "enter", "down", "enter",   # still on Yours: Remove, Yes
          "esc"])
    _shop(workspace).run([])
    assert "context7" not in installer.installed_names(workspace)


def test_marketplace_install_asks_for_missing_keys_hidden(workspace, axon_home, picks, monkeypatch) -> None:
    from axon.marketplace import browser, catalog, installer
    asked: list[str] = []
    monkeypatch.setattr(menus.getpass, "getpass", lambda prompt: (asked.append(prompt), "ghp_test")[1])
    monkeypatch.setattr("axon.marketplace.installer.save_secrets", lambda values, fresh: list(fresh))
    picks(["user", None])
    shop = _shop(workspace)
    entry = catalog.find_entry(catalog.load_tab("connectors"), "github")
    browser.Browser(shop).install_connector(entry)
    assert asked and "GITHUB_PERSONAL_ACCESS_TOKEN" in asked[0]
    assert "github" in installer.installed_names(workspace)


@pytest.mark.parametrize("answer, kept", [("Retry", True), ("Keep anyway", True), ("Cancel", False)])
def test_marketplace_failed_health_check_offers_retry_keep_cancel(answer, kept, workspace, axon_home, picks) -> None:
    from axon.marketplace import browser, catalog, installer
    from axon.marketplace.installer import HealthResult
    calls = {"n": 0}

    def flaky(spec, ws, timeout):
        calls["n"] += 1
        return HealthResult("failed", "boom") if calls["n"] == 1 else HealthResult("connected", tools=["t"])
    picks(["user", answer, None])
    shop = _shop(workspace, health=flaky)
    browser.Browser(shop).install_connector(catalog.find_entry(catalog.load_tab("connectors"), "context7"))
    assert ("context7" in installer.installed_names(workspace)) is kept


def _browser_actions(workspace: Path, tab: str, name: str) -> tuple[list[str], list[list[str]]]:
    from axon.marketplace import browser
    shop = _shop(workspace)
    ran: list[list[str]] = []
    shop.run = lambda tokens: ran.append(list(tokens))  # type: ignore[method-assign]
    b = browser.Browser(shop, tab)
    actions = b.installed_actions(tab, name, None)
    for action in actions:
        if action.label in ("Test", "Disable", "Enable", "Remove"):
            action.run()
    return [a.label for a in actions], ran


@pytest.mark.parametrize("tab", ["skills", "plugins"])
def test_marketplace_skill_and_plugin_items_never_get_connector_actions(tab, workspace, axon_home) -> None:
    labels, ran = _browser_actions(workspace, tab, "thing")
    assert not {"Tools", "Permissions", "Keys"} & set(labels)
    assert all(tokens[-2:] == ["--tab", tab] for tokens in ran)
    assert len(ran) == 3  # Test, Enable/Disable, Remove: one handler call each


def test_marketplace_connector_actions(workspace, axon_home) -> None:
    labels, ran = _browser_actions(workspace, "connectors", "srv")
    assert {"Tools", "Permissions", "Keys", "Scope", "Remove"} <= set(labels)
    assert [t[0] for t in ran] == ["test", "disable", "remove"]


def test_marketplace_test_on_a_skill_never_reports_mcp_errors(workspace, axon_home) -> None:
    lines: list[str] = []
    shop = _shop(workspace, out=lines)
    for verb in (["test", "theme-factory"], ["scope", "theme-factory", "user"], ["env", "theme-factory", "A=1"],
                 ["timeout", "theme-factory", "5"], ["tool", "theme-factory", "*", "allow"]):
        shop.run([*verb, "--tab", "skills"])
    text = "\n".join(lines)
    assert "not configured" not in text and "own config files" not in text
    assert text.count("No skill called 'theme-factory'") == 5


# ─── /skills ────────────────────────────────────────────────────────────────

def _write_skill(folder: Path, name: str, description: str = "Formats things nicely.") -> Path:
    path = folder / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\nDo the thing.\n", encoding="utf-8")
    return path


@pytest.fixture
def skill_agent(workspace, axon_home):
    from axon.skills.manager import SkillManager
    _write_skill(Path.home() / ".claude" / "skills", "my-style")          # personal, ~/.claude
    _write_skill(axon_home / "skills", "own-user")                        # personal, ~/.axon (own folder)
    _write_skill(workspace / ".axon" / "skills", "own-project")           # project (own folder)
    mgr = SkillManager(workspace)
    return SimpleNamespace(skills=mgr, settings=SimpleNamespace(workspace=workspace))


def _skill_labels(agent: Any, name: str) -> list[str]:
    from axon.commands.browse_menus import skill_actions
    skill = agent.skills.get(name)
    return [a.label for a in skill_actions(lambda t: None, agent.settings.workspace, name, skill.scope, True)]


def test_skill_actions_depend_on_where_the_skill_lives(skill_agent) -> None:
    bundled = next(n for n, s in skill_agent.skills.skills.items() if s.scope == "bundled")
    assert _skill_labels(skill_agent, bundled) == ["Show", "Test", "Disable", "Edit"]
    assert "Delete" in _skill_labels(skill_agent, "my-style")
    assert not any(label.startswith("Move") for label in _skill_labels(skill_agent, "my-style"))
    assert "Move to project scope" in _skill_labels(skill_agent, "own-user")
    assert "Move to user scope" in _skill_labels(skill_agent, "own-project")
    for name in ("my-style", bundled):
        assert not {"Tools", "Reconnect", "Keys", "Permissions"} & set(_skill_labels(skill_agent, name))


@pytest.mark.parametrize("name", ["my-style", "own-user", "own-project", "BUNDLED"])
def test_skill_test_validates_the_skill_file(name, skill_agent, capsys) -> None:
    from axon.skills.interactive import handle_skills_command
    if name == "BUNDLED":
        name = next(n for n, s in skill_agent.skills.skills.items() if s.scope == "bundled")
    handle_skills_command(skill_agent.skills, skill_agent.settings.workspace, f"test {name}")
    out = plain(capsys.readouterr().out)
    assert f"OK: skill '{name}' is valid." in out
    assert "not configured" not in out and "Error" not in out


def test_skill_test_reports_problems(skill_agent, capsys) -> None:
    from axon.skills.interactive import handle_skills_command
    _write_skill(Path.home() / ".claude" / "skills", "broken", description="")
    skill_agent.skills.discover()
    handle_skills_command(skill_agent.skills, skill_agent.settings.workspace, "test broken")
    out = plain(capsys.readouterr().out)
    assert "problem" in out and "no description" in out


def test_skill_disable_works_for_claude_folder_skills(skill_agent, capsys) -> None:
    from axon.skills.interactive import handle_skills_command
    handle_skills_command(skill_agent.skills, skill_agent.settings.workspace, "disable my-style")
    out = plain(capsys.readouterr().out)
    assert "Disabled skill 'my-style'" in out and "not installed" not in out


def test_skills_menu_one_selection_runs_one_handler(skill_agent, picks, monkeypatch) -> None:
    from axon.commands import browse_menus
    from axon.skills import interactive
    calls: list[str] = []
    monkeypatch.setattr(interactive, "handle_skills_command", lambda mgr, ws, text: calls.append(text))
    picks(["my-style", "Test"])
    browse_menus.skills_menu(skill_agent)
    assert calls == ["test my-style"]


def test_skills_menu_delete_asks_once_in_the_menu(skill_agent, picks, monkeypatch, capsys) -> None:
    from axon.commands import browse_menus
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked twice"))
    picks(["own-project", "Delete", "Yes"])
    browse_menus.skills_menu(skill_agent)
    assert not (skill_agent.settings.workspace / ".axon" / "skills" / "own-project").exists()


# ─── /plugin, /agents, /hooks ───────────────────────────────────────────────

def test_plugin_actions(workspace, axon_home, picks, monkeypatch) -> None:
    from axon.commands import browse_menus
    from axon.commands import plugins as pl
    plugin = pl.Plugin(name="demo", path=workspace, scope="user")
    monkeypatch.setattr(pl, "load_plugins", lambda ws: [plugin])
    ran: list[str] = []
    monkeypatch.setattr("axon.commands.agent_cmds.handle_plugin", lambda agent, text: ran.append(text))
    shown = picks(["demo", "Test"])
    browse_menus.plugins_menu(SimpleNamespace(settings=SimpleNamespace(workspace=workspace)))
    labels = shown[1][1]
    assert "Update" not in labels  # not from a marketplace
    assert not {"Tools", "Keys", "Permissions"} & set(labels)
    assert ran == ["test demo"]


@pytest.mark.parametrize("builtin, label, expected", [
    (True, "Show", "show helper"), (True, "Override", "create helper"),
    (False, "Edit", "edit helper"), (False, "Delete", "delete helper")])
def test_agent_actions(builtin, label, expected, workspace, picks, monkeypatch) -> None:
    from axon.commands import agent_cmds, browse_menus
    d = SimpleNamespace(name="helper", source="builtin" if builtin else str(workspace / "helper.md"), description="")
    monkeypatch.setattr(agent_cmds, "_load_agent_defs", lambda ws: [d])
    ran: list[str] = []
    monkeypatch.setattr(agent_cmds, "handle_agents", lambda agent, text: ran.append(text))
    picks(["helper", label, "Yes"] if label == "Delete" else ["helper", label])
    browse_menus.agents_menu(SimpleNamespace(settings=SimpleNamespace(workspace=workspace)))
    assert ran == [expected]


def test_hooks_menu_shows_one_hook(picks, monkeypatch) -> None:
    from axon.commands import agent_cmds, browse_menus
    ran: list[str] = []
    monkeypatch.setattr(agent_cmds, "handle_hooks", lambda agent, text: ran.append(text))
    picks(["PreToolUse Bash", "Show in full"])
    browse_menus.hooks_menu(SimpleNamespace(), [{"event": "PreToolUse", "matcher": "Bash", "command": "echo"}])
    assert ran == ["1"]


# ─── /output, /payload, /trace ──────────────────────────────────────────────

def test_output_menu_shows_the_chosen_output(workspace, picks, monkeypatch, capsys) -> None:
    from axon.commands import code_cmds
    from axon.ui import render
    outputs = {1: ("Bash", "ls", "first output"), 2: ("Read", "a.py", "second output")}
    monkeypatch.setattr(render, "list_tool_outputs", lambda limit=20: [(n, a, b, 1) for n, (a, b, _) in outputs.items()])
    monkeypatch.setattr(render, "get_tool_output", lambda n: outputs.get(int(n)))
    monkeypatch.setattr(code_cmds, "is_tty", lambda: True)
    shown = picks(["#1 Bash"])
    code_cmds.handle_output(SimpleNamespace(settings=SimpleNamespace(workspace=workspace)), "")
    assert shown[0][1][0].startswith("#2")  # newest first
    assert "first output" in plain(capsys.readouterr().out)


def _payload_agent(settings, registry) -> SimpleNamespace:
    messages = [{"role": "user", "content": "hello there"}, {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "last question"}]
    return SimpleNamespace(settings=settings, registry=registry, provider=None, ledger=None,
                           conversation=SimpleNamespace(messages=messages), skills=None)


def test_payload_menu_expands_one_section(settings, registry, picks, monkeypatch, capsys) -> None:
    from axon.commands import context_cmds
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks(["Previous conversation"])
    context_cmds.handle_payload(_payload_agent(settings, registry), "")
    out = plain(capsys.readouterr().out)
    assert shown[0][0] == "Expand a section of the payload"
    assert "/payload full shows every line" not in out
    assert out.count("[3] PREVIOUS CONVERSATION") == 2  # the summary, then the expanded section
    assert out.count("[1] SYSTEM PROMPT") == 1


def test_payload_section_argument_is_scriptable(settings, registry, capsys) -> None:
    from axon.commands import context_cmds
    context_cmds.handle_payload(_payload_agent(settings, registry), "last")
    out = plain(capsys.readouterr().out)
    assert "[4] LAST MESSAGE" in out and "[1] SYSTEM PROMPT" not in out


def test_trace_picker_sets_the_level(settings, picks, monkeypatch) -> None:
    from axon.commands import model_cmds
    agent = SimpleNamespace(settings=settings, renderer=SimpleNamespace(trace_level="normal"))
    monkeypatch.setattr(model_cmds, "is_tty", lambda: True)
    monkeypatch.setattr(model_cmds, "update_settings", lambda a, **kw: setattr(a, "changed", kw))
    picks(["detailed"])
    model_cmds.handle_trace(agent, "")
    assert agent.renderer.trace_level == "detailed"


# ─── usage menus and non-TTY text ───────────────────────────────────────────

def test_usage_menu_asks_for_a_placeholder_and_runs_once(picks, monkeypatch) -> None:
    from axon.commands.registry import resolve_builtin
    from axon.commands.usage_menu import run_usage_menu
    import dataclasses
    calls: list[str] = []
    cmd = dataclasses.replace(resolve_builtin("notify"), handler=lambda agent, words: calls.append(words))
    monkeypatch.setattr("builtins.input", lambda prompt: "30")
    picks(["/notify after <seconds>"])
    run_usage_menu(SimpleNamespace(), cmd)
    assert calls == ["after 30"]


def test_usage_menu_confirms_destructive_choices(picks, monkeypatch) -> None:
    from axon.commands.registry import resolve_builtin
    from axon.commands.usage_menu import run_usage_menu
    import dataclasses
    calls: list[str] = []
    cmd = dataclasses.replace(resolve_builtin("queue"), handler=lambda agent, words: calls.append(words))
    picks(["/queue clear", "No", None])
    run_usage_menu(SimpleNamespace(), cmd)
    assert calls == []


def test_non_tty_keeps_the_text_output(workspace, axon_home, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    from axon.marketplace import cli
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: False)
    lines: list[str] = []
    cli.run_command([], workspace, interactive=False, out=lines.append)
    assert "Install with: /marketplace install <id>" in "\n".join(lines)
    called: list[str] = []
    monkeypatch.setattr("axon.commands.registry.resolve_builtin",
                        lambda name: SimpleNamespace(name="notify", handler=lambda a, t: called.append(t), usage="", description=""))
    dispatch_command("/notify", SimpleNamespace(settings=SimpleNamespace(workspace=workspace)))
    assert called == [""]  # no menu without a terminal: the handler itself runs


def test_every_command_is_catalogued() -> None:
    """Each listed command opens its own menu, gets a usage or action menu, prints a report with a
    "What next?" menu, or says why it needs none (the full check is in test_keyboard_menus_sweep.py)."""
    from axon.commands.registry import visible_commands
    from axon.commands.usage_menu import menu_kind
    missing = sorted(c.name for c in visible_commands() if not menu_kind(c.name))
    assert not missing, f"add these to axon.commands.usage_menu: {missing}"
