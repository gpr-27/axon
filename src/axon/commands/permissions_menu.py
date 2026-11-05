"""
/permissions in a terminal: the rules grouped by Deny / Ask / Allow, the extra directories, and the
actions on each (change to allow / ask / deny, remove), plus a guided "Add a rule": pick the tool,
what it should cover, allow / ask / deny, and where to save it.

Every action runs the same handler as the typed command: ``/permissions allow <rule>``,
``/permissions remove <kind> <rule>``, ``/permissions session ...`` and ``/add-dir``.
"""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, ok
from axon.ui.menus import Action, ask_text, browse, choose_item, choose_value
from axon.ui.picker import SelectItem
from axon.ui.theme import GOLD, MINT, ROSE, RST, TEAL

KINDS = ("allow", "ask", "deny")
KIND_ORDER = ("deny", "ask", "allow")
KIND_COLOR = {"allow": MINT, "ask": GOLD, "deny": ROSE}
KIND_HELP = {"allow": "run without asking", "ask": "always ask first, even in bypass mode", "deny": "never run"}


def _run(agent: Any, text: str) -> None:
    """The typed command ``/permissions <text>``."""
    from axon.commands.perm_cmds import handle_permissions
    handle_permissions(agent, text)


# ─── the main list ───────────────────────────────────────────────────────────

def permissions_menu(agent: Any) -> None:
    perms = agent.permissions

    def rows() -> list[SelectItem]:
        items = []
        entries = perms.list_rules()
        for kind in KIND_ORDER:
            for entry in entries:
                if entry.kind == kind:
                    items.append(SelectItem(label=str(entry.rule), value=("rule", kind, str(entry.rule), entry.source),
                                            hint=entry.source, group=f"{kind.capitalize()} rules",
                                            badge=f"{KIND_COLOR[kind]}●{RST}"))
        for directory in perms.working_dirs() if hasattr(perms, "working_dirs") else []:
            items.append(SelectItem(label=str(directory), value=("dir", str(directory)), group="Extra directories",
                                    badge=f"{TEAL}▸{RST}"))
        return items

    def actions_for(value: tuple[str, ...]) -> list[Action]:
        if value[0] == "dir":
            path = value[1]
            return [Action("Remove this directory", lambda: _remove_directory(agent, path),
                           confirm=f"Stop allowing {path}?")]
        _tag, kind, spec, source = value
        actions = [Action(f"Change to {other}", lambda other=other: change_rule(agent, kind, other, spec, source),
                          KIND_HELP[other]) for other in KINDS if other != kind]
        actions.append(Action("Remove", lambda: _run(agent, f"remove {kind} {spec}"), f"from {source}",
                              confirm=f"Remove the {kind} rule {spec}?"))
        return actions

    def extra() -> list[Action]:
        return [Action("Add a rule", lambda: add_rule_flow(agent), "pick a tool, allow / ask / deny, where to save"),
                Action("Add a directory", lambda: _add_directory(agent), "let tools work outside the project"),
                Action("Reload rules from disk", lambda: _run(agent, "reload"))]

    browse("Permissions", rows, actions_for, extra, subtitle=f"mode {agent.settings.mode}")


def change_rule(agent: Any, old_kind: str, new_kind: str, spec: str, source: str = "") -> None:
    """Move a rule from one list to another (allow / ask / deny): remove it, add it to the new one
    in the place it came from (a session rule stays for this session, a user rule stays user-wide)."""
    _run(agent, f"remove {old_kind} {spec}")
    if source == "session":
        save_rule(agent, new_kind, spec, "session")
        return
    path = next((p for label, p, fmt in agent.permissions.rule_sources() if label == source and fmt == "json"), None) \
        if source not in ("", "local") and hasattr(agent.permissions, "rule_sources") else None
    if path is None:
        _run(agent, f"{new_kind} {spec}")      # no file of its own to go back to: this project, private
        return
    from axon.marketplace.control import write_rule
    try:
        write_rule(path, new_kind, spec)
        agent.permissions.reload()
    except Exception as exc:  # noqa: BLE001
        err(f"Could not save the rule: {exc}")
        return
    ok(f"{new_kind} {spec}  (saved in {path})")


def _remove_directory(agent: Any, path: str) -> None:
    from axon.commands.workspace_cmds import handle_add_dir
    handle_add_dir(agent, f"remove {path}")


def _add_directory(agent: Any) -> None:
    from axon.commands.workspace_cmds import handle_add_dir
    text = ask_text("Directory to allow")
    if text:
        handle_add_dir(agent, f"{text} --save")


# ─── add a rule, step by step ────────────────────────────────────────────────

def pick_tool(agent: Any) -> str | None:
    """Step 1: the tool. A first row lets the user type a rule pattern of their own. None on Esc."""
    from axon.commands.agent_cmds import tool_groups, tool_table
    tools, _deferred = tool_table(agent)
    items = [SelectItem(label="Another rule…", value="", hint="type it, e.g. WebFetch(domain:docs.python.org)",
                        group="Other")]
    for title, names in tool_groups(tools):
        for name in names:
            first_line = (tools[name].description.splitlines() or [""])[0][:60]
            items.append(SelectItem(label=name, value=name, hint=first_line, group=title))
    chosen = choose_item(items, "Add a rule: which tool?", "Esc to cancel")
    return None if chosen is None else chosen.value


def rule_spec_for(tool: str) -> str | None:
    """Step 2: the whole tool, or only calls matching a pattern. Returns the rule text, None on Esc."""
    if not tool:
        return ask_text("Rule (for example Bash(git status:*) or Edit(src/**))") or None
    covers = choose_value(f"What should the rule cover for {tool}?",
                          [(f"Every {tool} call", "all", f"rule: {tool}"),
                           ("Only calls matching a pattern…", "pattern", f"rule: {tool}(pattern)")])
    if covers is None:
        return None
    if covers == "all":
        return tool
    pattern = ask_text(f"Pattern inside {tool}( ), for example git status:* or src/**")
    return f"{tool}({pattern})" if pattern else None


def add_rule_flow(agent: Any) -> bool:
    """Tool -> what it covers -> allow / ask / deny -> where to save it. True when a rule was saved."""
    tool = pick_tool(agent)
    if tool is None:
        return False
    spec = rule_spec_for(tool)
    if not spec:
        return False
    kind = choose_value(f"{spec}: allow, ask or deny?", [(k.capitalize(), k, KIND_HELP[k]) for k in KINDS])
    if kind is None:
        return False
    where = agent.permissions.local_settings_path()
    scope = choose_value("Where should the rule be saved?", [
        ("This project, private", "local", str(where)),
        ("This session only", "session", "forgotten when Axon exits"),
        ("All my projects", "user", "~/.axon/settings.json")])
    if scope is None:
        return False
    save_rule(agent, kind, spec, scope)
    return True


def save_rule(agent: Any, kind: str, spec: str, scope: str) -> None:
    """Save a rule in the chosen place. "local" and "session" are the typed ``/permissions`` commands."""
    if scope == "local":
        _run(agent, f"{kind} {spec}")
    elif scope == "session":
        _run(agent, f"session {kind} {spec}")
    elif scope == "user":
        from axon.marketplace.control import settings_file, write_rule
        try:
            path = settings_file(agent.settings.workspace, True)
            write_rule(path, kind, spec)
            agent.permissions.reload()
        except Exception as exc:
            err(f"Could not save the rule: {exc}")
            return
        ok(f"{kind} {spec}  (saved in {path})")
