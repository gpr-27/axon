"""
Every command has a keyboard menu, and no menu screen tells the user to type another command.

* the SWEEP (tests/menu_sweep.py) runs every registered command with no arguments, as a terminal
  user would (menus open and are closed with Esc) and as a script would (plain text), and every line
  of every "What next?" menu and every action menu choice;
* the CATALOGUE checks each command is in exactly one of: own menu, usage menu, action menu, report
  menu, or the short NO_MENU table;
* the ACTION MATRIX checks that each menu entry calls the same handler as its typed equivalent;
* /tools, /permissions and /menu are driven with scripted keys.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fakes import FakeProvider
from test_core_helpers import make_agent
from test_keyboard_menus import axon_home, keys, picks, plain  # noqa: F401  (fixtures)

import menu_sweep
from axon import platform as plat
from axon.commands import usage_menu
from axon.commands.registry import resolve_builtin, visible_commands
from axon.tools.base import Tool
from axon.tools.interactive import defer_mcp_tools
from axon.ui import menus, picker


# ─── the sweep ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sweeps(tmp_path_factory):
    """Text-mode and terminal-mode runs of every command (a few seconds; shared by the tests below)."""
    patch = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("sweep-home")
    patch.setenv("AXON_HOME", str(home))
    patch.setenv("AXON_CLIPBOARD_FILE", str(home / "clipboard.txt"))
    patch.setattr(Path, "home", classmethod(lambda cls: home))
    try:
        work = tmp_path_factory.mktemp("sweep-work")
        yield {"text": menu_sweep.sweep(work, False), "tty": menu_sweep.sweep(work, True),
               "steps": menu_sweep.sweep_steps(work)}
    finally:
        patch.undo()


def test_sweep_ran_every_command_without_crashing(sweeps) -> None:
    names = {c.name for c in visible_commands()}
    for mode in ("text", "tty"):
        assert set(sweeps[mode]) == names
        crashed = {n: r.error for n, r in sweeps[mode].items() if r.error}
        assert not crashed, f"{mode}: {crashed}"


def test_terminal_mode_prints_no_typed_follow_up_hint(sweeps) -> None:
    """The point of the menus: in a terminal no command tells the user to type something."""
    flagged = {n: r.hints for n, r in sweeps["tty"].items() if r.hints}
    assert not flagged, "typed hints in a terminal:\n" + "\n".join(f"/{n}: {h}" for n, h in flagged.items())


def test_every_what_next_line_and_menu_choice_prints_no_hint(sweeps) -> None:
    flagged = {line: r.hints for line, r in sweeps["steps"].items() if r.hints}
    assert not flagged, "\n".join(f"{line}: {h}" for line, h in flagged.items())
    assert len(sweeps["steps"]) > 150  # the lines really were collected


def test_every_command_shows_a_menu_or_is_in_the_short_no_menu_table(sweeps) -> None:
    silent = {n for n, r in sweeps["tty"].items() if not r.menus_shown}
    assert silent == set(usage_menu.NO_MENU)


def test_text_mode_keeps_the_hints_and_shows_no_menu(sweeps) -> None:
    """Without a terminal the output is the same text as before, including the hint lines."""
    with_hints = {n for n, r in sweeps["text"].items() if r.hints}
    assert len(with_hints) >= 40
    assert any("for parameters" in h for h in sweeps["text"]["tools"].hints)
    assert any("/permissions allow" in h for h in sweeps["text"]["permissions"].hints)
    assert not {n for n, r in sweeps["text"].items() if r.menus_shown}


def test_the_sweep_notices_a_hint() -> None:
    """The detector itself: it must flag the old /tools line and leave ordinary text alone."""
    assert menu_sweep.hint_lines("    /tools <name> for parameters · rules: /permissions")
    assert menu_sweep.hint_lines("Add one with /mcp add <name> <command>")
    assert menu_sweep.hint_lines("Usage: /btw <question>")
    assert not menu_sweep.hint_lines("Read  allow  Read a file from /Users/me/project/src")
    assert not menu_sweep.hint_lines("mode bypass · 22 tools available")


# ─── the catalogue ───────────────────────────────────────────────────────────

def _tables() -> dict[str, set[str]]:
    return {"own": set(usage_menu.OWN_MENU), "usage": set(usage_menu.USAGE_MENU),
            "action": set(usage_menu.ACTION_MENU), "report": set(usage_menu.REPORT_MENU),
            "none": set(usage_menu.NO_MENU)}


def test_every_command_is_in_exactly_one_table() -> None:
    tables = _tables()
    for command in visible_commands():
        owners = [kind for kind, names in tables.items() if command.name in names]
        assert len(owners) == 1, f"/{command.name} is in {owners or 'no table'}"
        assert usage_menu.menu_kind(command.name) == owners[0]


def test_no_table_lists_a_command_that_does_not_exist() -> None:
    for kind, names in _tables().items():
        for name in names:
            assert resolve_builtin(name) is not None, f"{kind}: /{name} is not a command"


def test_no_menu_is_short_and_every_entry_has_a_reason() -> None:
    assert len(usage_menu.NO_MENU) <= 3
    assert all(reason.strip() for reason in usage_menu.NO_MENU.values())


def test_every_menu_has_something_to_choose() -> None:
    for command in visible_commands():
        kind = usage_menu.menu_kind(command.name)
        if kind in ("usage", "action"):
            assert usage_menu.usage_choices(command.name, command.usage or ""), command.name
        if kind == "report":
            assert usage_menu.next_steps(command), f"/{command.name} has no What-next entries"


# ─── the action matrix: each menu entry runs the typed command ───────────────

def _choice_cases() -> list[tuple[str, int]]:
    cases = []
    for command in visible_commands():
        if command.name in usage_menu.ACTION_MENU or command.name in usage_menu.USAGE_MENU:
            for position in range(len(usage_menu.usage_choices(command.name, command.usage or ""))):
                cases.append((command.name, position))
    return cases


@pytest.mark.parametrize("name, position", _choice_cases())
def test_action_and_usage_menu_choice_runs_the_command_handler(name, position, monkeypatch) -> None:
    command = resolve_builtin(name)
    choice = usage_menu.usage_choices(name, command.usage or "")[position]
    calls: list[str] = []
    patched = dataclasses.replace(command, handler=lambda agent, words: calls.append(words))
    monkeypatch.setattr("builtins.input", lambda prompt="": "answer")
    shown = {"n": 0}

    def choose(items, title, subtitle="", index=0, body=None):
        labels = [plain(i.label) for i in items]
        if "Yes" in labels:           # a confirmation: say yes
            return items[labels.index("Yes")]
        shown["n"] += 1
        return items[position] if shown["n"] == 1 else None
    monkeypatch.setattr(menus, "_select", choose)
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    usage_menu.run_usage_menu(SimpleNamespace(), patched)
    expected = f"{choice.words} answer".strip() if choice.ask else choice.words
    assert calls == [expected]


def _step_cases() -> list[tuple[str, int]]:
    cases = []
    for command in visible_commands():
        if command.name in usage_menu.REPORT_MENU or command.name in usage_menu.NEXT_STEPS:
            for position in range(len(usage_menu.next_steps(command))):
                cases.append((command.name, position))
    return cases


@pytest.mark.parametrize("name, position", _step_cases())
def test_what_next_entry_runs_its_typed_line(name, position, monkeypatch) -> None:
    command = resolve_builtin(name)
    step = usage_menu.next_steps(command)[position]
    ran: list[str] = []
    monkeypatch.setattr("axon.commands.registry.dispatch_command", lambda line, agent: ran.append(line))
    monkeypatch.setattr("builtins.input", lambda prompt="": "answer")
    shown = {"n": 0}

    def choose(items, title, subtitle="", index=0, body=None):
        labels = [plain(i.label) for i in items]
        if "Yes" in labels:
            return items[labels.index("Yes")]
        shown["n"] += 1
        return items[position] if shown["n"] == 1 else None
    monkeypatch.setattr(menus, "_select", choose)
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    usage_menu.run_next_menu(SimpleNamespace(), command)
    assert ran == [step.line.replace("{}", "answer")]


def test_a_report_is_printed_then_what_next_is_offered(workspace, picks, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks([None])
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/status", agent)
    out = plain(capsys.readouterr().out)
    assert "Axon status" in out and "/cost" not in out and "/doctor" not in out
    assert shown[0][0] == "What next?" and "Cost breakdown" in shown[0][1]


def test_what_next_choice_runs_and_comes_back_to_the_menu(workspace, picks, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks(["Every API call", None])   # /cost, then Esc
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/cost", agent)
    titles = [title for title, _ in shown]
    assert titles.count("What next?") >= 2 and "Call-by-Call" in plain(capsys.readouterr().out)


def test_an_action_command_says_what_it_does_and_can_be_cancelled(workspace, picks, monkeypatch) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    agent = make_agent(FakeProvider([]), workspace)
    agent.conversation.messages.append({"role": "user", "content": "keep me"})
    shown = picks(["Run it", "No"])          # /clear asks first; No keeps the conversation
    dispatch_command("/clear", agent)
    assert shown[0][1][0] == "Run it" and shown[0][1][-1] == "Cancel"
    assert any(m.get("content") == "keep me" for m in agent.conversation.messages)


def test_exit_has_no_menu(workspace, picks, monkeypatch) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks([])
    result = dispatch_command("/exit", make_agent(FakeProvider([]), workspace))
    assert result.should_exit and not shown


def test_a_command_in_a_script_still_prints_text_only(workspace, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: False)
    before = picker.menus_run()
    dispatch_command("/tools", make_agent(FakeProvider([]), workspace))
    assert picker.menus_run() == before
    assert "/tools <name> for parameters" in plain(capsys.readouterr().out)


# ─── /tools ──────────────────────────────────────────────────────────────────

class _Mcp(Tool):
    name: ClassVar[str] = "mcp__demo__do_thing"
    description: ClassVar[str] = "Does a demo thing. " * 40 + "THE-END-OF-THE-DESCRIPTION"
    schema: ClassVar[dict[str, Any]] = {"type": "object", "required": ["q"], "properties": {
        "q": {"type": "string", "description": "The query. " + "L" * 200 + "END-OF-Q"},
        "limit": {"type": ["integer", "null"], "description": "How many."},
        "mode": {"type": "string", "enum": ["fast", "full"], "description": "Engine."}}}

    def run(self, args, ctx):  # pragma: no cover
        return ""


@pytest.fixture
def tools_agent(workspace, axon_home, monkeypatch):
    agent = make_agent(FakeProvider([]), workspace, mode="default")
    agent.registry.register(_Mcp())
    assert defer_mcp_tools(agent.registry, 1_000_000, force=True) is not None
    assert "mcp__demo__do_thing" not in agent.registry.visible_names()   # really deferred
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    monkeypatch.setattr("axon.commands.agent_cmds.is_tty", lambda: True)
    return agent


def test_tools_list_is_grouped_and_includes_deferred_tools(tools_agent, picks) -> None:
    from axon.commands.tools_menu import tool_rows
    rows = tool_rows(tools_agent)
    groups = list(dict.fromkeys(r.group for r in rows))
    assert groups[:6] == ["Files", "Search", "Shell", "Web", "Planning & agents", "MCP"]
    by_name = {r.value: r for r in rows}
    assert "mcp__demo__do_thing" in by_name and by_name["mcp__demo__do_thing"].group == "MCP"
    assert by_name["Bash"].group == "Shell"
    for row in rows:   # name, current permission and the first line of the description
        assert row.hint.split(" ", 1)[0] in ("allow", "ask", "deny")
    assert plain(by_name["Read"].render(False, 80)).split()[:2] == ["Read", "allow"]


def test_tools_in_a_terminal_opens_the_list_and_prints_no_hint(tools_agent, picks, capsys) -> None:
    from axon.commands.registry import dispatch_command
    shown = picks([None])
    dispatch_command("/tools", tools_agent)
    assert shown[0][0] == "Tools" and "mcp__demo__do_thing" in shown[0][1]
    out = plain(capsys.readouterr().out)
    assert "for parameters" not in out and "/permissions" not in out


def test_tool_detail_shows_the_full_description_and_every_parameter(tools_agent, picks) -> None:
    from axon.commands.tools_menu import tools_menu
    shown = picks(["mcp__demo__do_thing", None, None])
    tools_menu(tools_agent)
    assert [title for title, _ in shown] == ["Tools", "mcp__demo__do_thing", "Tools"]
    body = picks.bodies[1]
    assert "THE-END-OF-THE-DESCRIPTION" in body            # the whole text, nothing cut
    assert "L" * 200 in body and "END-OF-Q" in body         # the whole parameter text
    assert "* q  string" in body and "limit  integer|null" in body and "one of: fast, full" in body
    assert "deferred" in body
    assert shown[1][1] == ["Always allow", "Ask each time", "Deny", "Remove my rule", "Back"]


@pytest.mark.parametrize("label, kind", [("Always allow", "allow"), ("Ask each time", "ask"), ("Deny", "deny")])
def test_tool_action_writes_the_rule_and_shows_where(label, kind, tools_agent, picks, workspace, capsys) -> None:
    from axon.commands.tools_menu import tool_detail
    picks([label, None])
    tool_detail(tools_agent, "Bash")
    saved = workspace / ".axon" / "settings.local.json"
    assert json.loads(saved.read_text())["permissions"][kind] == ["Bash"]
    assert [(e.kind, str(e.rule)) for e in tools_agent.permissions.list_rules()] == [(kind, "Bash")]
    out = plain(capsys.readouterr().out)
    assert f"{kind} Bash" in out and str(saved) in out
    from axon.commands.agent_cmds import tool_permission, tool_table
    assert tool_permission(tools_agent, tool_table(tools_agent)[0]["Bash"]) == kind   # the list shows it now


def test_choosing_another_action_replaces_the_rule(tools_agent, picks, workspace) -> None:
    from axon.commands.tools_menu import tool_detail
    picks(["Always allow", "Deny", None])
    tool_detail(tools_agent, "Bash")
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text())["permissions"]
    assert data.get("allow", []) == [] and data["deny"] == ["Bash"]


def test_remove_my_rule(tools_agent, picks, workspace, capsys) -> None:
    from axon.commands.tools_menu import tool_detail
    picks(["Ask each time", "Remove my rule", None])
    tool_detail(tools_agent, "Bash")
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text())["permissions"]
    assert data["ask"] == [] and not tools_agent.permissions.list_rules()
    assert "Removed ask Bash" in plain(capsys.readouterr().out)


def test_remove_my_rule_without_a_rule_says_so(tools_agent, picks, capsys) -> None:
    from axon.commands.tools_menu import tool_detail
    picks(["Remove my rule", None])
    tool_detail(tools_agent, "Bash")
    assert "no rule for Bash" in plain(capsys.readouterr().out)


def test_tools_esc_goes_back_one_level_at_a_time(tools_agent, picks) -> None:
    from axon.commands.tools_menu import tools_menu
    shown = picks(["Read", None, "Grep", "Back", None])
    tools_menu(tools_agent)
    assert [title for title, _ in shown] == ["Tools", "Read", "Tools", "Grep", "Tools"]


def test_tools_typed_name_still_prints_the_parameters(tools_agent, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    dispatch_command("/tools mcp__demo__do_thing", tools_agent)
    out = plain(capsys.readouterr().out)
    assert "L" * 200 in out and "deferred" in out


def test_detail_screen_scrolls_and_wraps_at_narrow_widths(tools_agent, keys, monkeypatch) -> None:
    """Real keys through the real DetailScreen at 50 columns: PgDn reveals the end of a long text."""
    from axon.commands.tools_menu import tool_detail
    frames: list[list[str]] = []
    monkeypatch.setattr(picker.LiveFrame, "render", lambda self, lines, cursor=None: frames.append([plain(x) for x in lines]))
    monkeypatch.setattr(picker.LiveFrame, "clear", lambda self: None)
    keys(["pagedown", "pagedown", "pagedown", "pagedown", "pagedown", "esc"])
    monkeypatch.setattr(plat, "term_size", lambda: (50, 24))
    tool_detail(tools_agent, "mcp__demo__do_thing")
    body_lines = [line for frame in frames for line in frame if "saved in" not in line and "mode decides" not in line]
    assert [l for l in body_lines if len(l) > 50] == []   # the text wraps at the terminal width
    assert not any("END-OF-Q" in "".join(f) for f in frames[:1])          # first screen: the start only
    assert any("↓" in line and "more line" in line for line in frames[0])
    assert any("END-OF-Q" in " ".join(f) for f in frames[1:])             # scrolled to the end
    assert any("↑" in line for line in frames[-1])


# ─── /permissions ────────────────────────────────────────────────────────────

@pytest.fixture
def perm_agent(workspace, axon_home, monkeypatch):
    agent = make_agent(FakeProvider([]), workspace, mode="default")
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    return agent


def _answers(monkeypatch, *texts: str) -> None:
    queue = deque(texts)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.popleft())


def test_add_a_rule_tool_then_kind_then_scope(perm_agent, picks, workspace, monkeypatch, capsys) -> None:
    from axon.commands.permissions_menu import permissions_menu
    _answers(monkeypatch, "git status:*")
    shown = picks(["Add a rule", "Bash", "Only calls matching", "Allow", "This project", None])
    permissions_menu(perm_agent)
    titles = [title for title, _ in shown]
    assert titles[:5] == ["Permissions", "Add a rule: which tool?", "What should the rule cover for Bash?",
                          "Bash(git status:*): allow, ask or deny?", "Where should the rule be saved?"]
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text())["permissions"]
    assert data["allow"] == ["Bash(git status:*)"]
    out = plain(capsys.readouterr().out)
    assert "allow Bash(git status:*)" in out and "settings.local.json" in out


def test_add_a_whole_tool_rule_for_this_session_only(perm_agent, picks, workspace) -> None:
    from axon.commands.permissions_menu import permissions_menu
    picks(["Add a rule", "WebFetch", "Every WebFetch call", "Ask", "This session only", None])
    permissions_menu(perm_agent)
    assert not (workspace / ".axon" / "settings.local.json").exists()
    assert [(e.kind, str(e.rule), e.source) for e in perm_agent.permissions.list_rules()] == [("ask", "WebFetch", "session")]


def test_add_a_rule_for_all_projects(perm_agent, picks, axon_home) -> None:
    from axon.commands.permissions_menu import permissions_menu
    picks(["Add a rule", "Grep", "Every Grep call", "Deny", "All my projects", None])
    permissions_menu(perm_agent)
    assert json.loads((axon_home / "settings.json").read_text())["permissions"]["deny"] == ["Grep"]
    assert [str(e.rule) for e in perm_agent.permissions.list_rules()] == ["Grep"]


def test_add_a_typed_rule_and_cancel_midway(perm_agent, picks, workspace, monkeypatch) -> None:
    from axon.commands.permissions_menu import add_rule_flow
    _answers(monkeypatch, "Edit(src/**)")
    picks(["Another rule", "Allow", "This project"])
    assert add_rule_flow(perm_agent) is True
    picks(["Bash", "Every Bash call", None])           # Esc at the kind question
    assert add_rule_flow(perm_agent) is False
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text())["permissions"]
    assert data["allow"] == ["Edit(src/**)"] and "Bash" not in json.dumps(data)


def test_pick_a_rule_to_change_or_remove_it(perm_agent, picks, workspace) -> None:
    from axon.commands.permissions_menu import permissions_menu
    perm_agent.permissions.add_rule("allow", "Bash(npm test:*)")
    shown = picks(["Bash(npm test:*)", "Change to deny", "Bash(npm test:*)", "Remove", "Yes", None])
    permissions_menu(perm_agent)
    assert shown[1][1] == ["Change to ask", "Change to deny", "Remove"]
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text())["permissions"]
    assert data["deny"] == [] and data["allow"] == []     # moved to deny, then removed
    assert not perm_agent.permissions.list_rules()


def test_change_rule_moves_it_between_lists(perm_agent, workspace) -> None:
    from axon.commands.permissions_menu import change_rule
    perm_agent.permissions.add_rule("allow", "Read(src/**)")
    change_rule(perm_agent, "allow", "ask", "Read(src/**)")
    assert [(e.kind, str(e.rule)) for e in perm_agent.permissions.list_rules()] == [("ask", "Read(src/**)")]


def test_remove_rule_asks_first(perm_agent, picks) -> None:
    from axon.commands.permissions_menu import permissions_menu
    perm_agent.permissions.add_rule("deny", "Bash")
    picks(["Bash", "Remove", "No", None, None])
    permissions_menu(perm_agent)
    assert [str(e.rule) for e in perm_agent.permissions.list_rules()] == ["Bash"]


def test_permissions_menu_groups_rules_and_prints_no_hint(perm_agent, picks, capsys) -> None:
    from axon.commands.registry import dispatch_command
    perm_agent.permissions.add_rule("allow", "Read")
    perm_agent.permissions.add_rule("deny", "Bash(rm *)")
    shown = picks([None])
    dispatch_command("/permissions", perm_agent)
    assert shown[0][0] == "Permissions" and shown[0][1][:2] == ["Bash(rm *)", "Read"]   # deny before allow
    assert shown[0][1][-3:] == ["Add a rule", "Add a directory", "Reload rules from disk"]
    assert "remove <kind>" not in plain(capsys.readouterr().out)


def test_typed_permissions_still_work(perm_agent, capsys) -> None:
    from axon.commands.registry import dispatch_command
    dispatch_command("/permissions allow Bash(git log:*)", perm_agent)
    assert "Bash(git log:*)" in plain(capsys.readouterr().out)


# ─── /menu and /help ─────────────────────────────────────────────────────────

def test_menu_lists_every_command_by_category(workspace, picks, monkeypatch) -> None:
    from axon.commands.browse_menus import navigator
    from axon.commands.registry import CATEGORIES
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks([None])
    navigator(make_agent(FakeProvider([]), workspace))
    labels = shown[0][1]
    assert shown[0][0] == "All commands"
    assert {f"/{c.name}" for c in visible_commands()} <= set(labels)
    order = [c.category for c in sorted(visible_commands(), key=lambda c: (
        [n for n, _ in CATEGORIES].index(c.category) if c.category in [n for n, _ in CATEGORIES] else 99, c.name))]
    first_of_each = list(dict.fromkeys(order))
    assert first_of_each[0] == CATEGORIES[0][0]


def test_menu_enter_opens_the_command_and_comes_back(workspace, keys, monkeypatch) -> None:
    from axon.commands.browse_menus import navigator
    ran: list[str] = []
    monkeypatch.setattr("axon.commands.registry.dispatch_command", lambda line, agent: ran.append(line))
    monkeypatch.setattr(picker.LiveFrame, "render", lambda self, lines, cursor=None: None)
    monkeypatch.setattr(picker.LiveFrame, "clear", lambda self: None)
    keys(["/", *"/tools", "enter",      # filter with "/", open /tools
          "/", *"/cost", "enter",        # back in the list: filter again, open /cost
          "esc", "esc"])                 # clear the filter, then close
    navigator(make_agent(FakeProvider([]), workspace))
    assert ran == ["/tools", "/cost"]


def test_help_and_menu_open_the_navigator(workspace, picks, monkeypatch) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    shown = picks([None, None])
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/help", agent)
    dispatch_command("/menu", agent)
    assert [title for title, _ in shown] == ["All commands", "All commands"]


def test_menu_has_a_key_binding() -> None:
    from axon.ui import input as prompt_input
    from axon.ui.keybindings import ACTIONS
    assert ACTIONS["app:menu"][1] == ("alt-h",)
    assert prompt_input._ACTION_COMMANDS["app:menu"] == "/menu"


def test_menu_in_a_script_prints_the_command_list(workspace, monkeypatch, capsys) -> None:
    from axon.commands.registry import dispatch_command
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: False)
    dispatch_command("/menu", make_agent(FakeProvider([]), workspace))
    assert "Axon commands" in plain(capsys.readouterr().out)


# ─── other menus added by the sweep ──────────────────────────────────────────

def test_sessions_menu_resumes_or_deletes(workspace, axon_home, picks, monkeypatch) -> None:
    from axon.commands.browse_menus import sessions_menu
    agent = make_agent(FakeProvider([]), workspace)
    metas = [SimpleNamespace(session_id="s1", created_at=1_700_000_000, first_prompt="fix the bug",
                             message_count=4, total_cost=0.1)]
    agent.session.list_recent = lambda limit=None: metas      # type: ignore[method-assign]
    ran: list[str] = []
    monkeypatch.setattr("axon.commands.registry.dispatch_command", lambda line, a: ran.append(line))
    picks(["fix the bug", "Resume", None])
    sessions_menu(agent)
    assert ran == ["/resume s1"]


def test_tips_menu_opens_the_command_of_a_tip(workspace, picks, monkeypatch) -> None:
    from axon.features import tips
    agent = make_agent(FakeProvider([]), workspace)
    ran: list[str] = []
    monkeypatch.setattr("axon.commands.registry.dispatch_command", lambda line, a: ran.append(line))
    engine = tips.tip_engine(agent)
    tip = next(t for t in engine.tips() if tips.display_text(t.text).startswith("/"))
    shown = picks([tips.display_text(tip.text), "Open", None])
    tips._tips_menu(agent, engine)
    assert ran == [tips.display_text(tip.text).split(" ", 1)[0]]
    assert "Write fresh tips now" in shown[0][1]


def test_new_picks_a_template_then_the_folder_name(workspace, picks, monkeypatch) -> None:
    from axon.features import scaffold
    templates = scaffold.list_templates()
    assert templates
    chosen = templates[0].name
    made: list[tuple] = []
    monkeypatch.setattr(scaffold, "create_project", lambda *a, **k: made.append((a, k)))
    monkeypatch.setattr("axon.commands.common.is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "demo-app")
    picks([chosen])
    scaffold.handle_new(SimpleNamespace(settings=SimpleNamespace(workspace=workspace)), "")
    assert made and made[0][0][0] == chosen and made[0][0][1].name == "demo-app"


def test_hooks_reload_reloads_and_stops(workspace, capsys) -> None:
    from axon.commands.agent_cmds import handle_hooks
    handle_hooks(make_agent(FakeProvider([]), workspace), "reload")
    out = plain(capsys.readouterr().out)
    assert "Hooks reloaded" in out and "Unknown event" not in out
