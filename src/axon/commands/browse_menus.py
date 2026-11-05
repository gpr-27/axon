"""
Keyboard menus for /skills, /agents, /plugin and /hooks run with no arguments in a terminal.

Each menu is "list -> pick -> action": the rows come from the same data the text listing prints, and
every action runs the command's own handler with the typed subcommand (``/skills delete x`` and the
menu's Delete do exactly the same thing). Without a terminal the handlers print their lists as before.
"""
from __future__ import annotations

from typing import Any

from axon.commands.common import ws_of
from axon.ui.menus import Action, ask_text, browse, choose_item
from axon.ui.picker import SelectItem


# ─── /skills ────────────────────────────────────────────────────────────────

def skills_menu(agent: Any) -> None:
    from axon.skills.interactive import handle_skills_command
    mgr = agent.skills
    ws = ws_of(agent)

    def run(text: str) -> None:
        handle_skills_command(mgr, ws, text)

    def rows() -> list[SelectItem]:
        items = []
        for name, skill in sorted(mgr.skills.items()):
            items.append(SelectItem(label=name, value=(name, skill.scope, True),
                                    hint=f"{skill.scope} · {skill.description[:50]}"))
        for name, skill in sorted(getattr(mgr, "disabled", {}).items()):
            items.append(SelectItem(label=name, value=(name, skill.scope, False), hint=f"{skill.scope} · disabled"))
        return items

    def actions_for(value: tuple[str, str, bool]) -> list[Action]:
        return skill_actions(run, ws, *value)

    def create() -> None:
        name = ask_text("Skill name")
        if name:
            run(f"create {name}")

    def extra() -> list[Action]:
        return [Action("Create a skill", create, "from a template"),
                Action("Install from GitHub", lambda: _run_with_text(run, "install", "GitHub URL")),
                Action("Status", lambda: run("status")),
                Action("Reload", lambda: run("reload"))]

    browse("Skills", rows, actions_for, extra, subtitle=f"{len(mgr.skills)} available")


def skill_actions(run: Any, ws: Any, name: str, scope: str, enabled: bool) -> list[Action]:
    """Only the actions that make sense for this skill's origin:
    bundled  -> Show, Test, Enable/Disable, Edit (copies it into the project as an override)
    plugin   -> Show, Test (the plugin switches it on and off; the plugin owns the file)
    personal/project -> Show, Test, Enable/Disable, Edit, Move (own folders only), Delete."""
    from axon.marketplace.skill_tools import installed_skills
    actions = [Action("Show", lambda: run(f"show {name}")),
               Action("Test", lambda: run(f"test {name}"), "validate SKILL.md")]
    if scope == "plugin":
        return actions
    if enabled:
        actions.append(Action("Disable", lambda: run(f"disable {name}")))
    else:
        actions.append(Action("Enable", lambda: run(f"enable {name}")))
    if scope == "bundled":
        actions.append(Action("Edit", lambda: run(f"edit {name}"), "copies it to this project first"))
        return actions
    actions.append(Action("Edit", lambda: run(f"edit {name}"), "open in your editor"))
    own = installed_skills(ws).get(name)
    if own is not None:
        other = "project" if own[0] == "user" else "user"
        actions.append(Action(f"Move to {other} scope", lambda: run(f"scope {name} {other}")))
    actions.append(Action("Delete", lambda: run(f"delete {name}"), confirm=f"Delete skill '{name}'?"))
    return actions


def _run_with_text(run: Any, verb: str, label: str) -> None:
    text = ask_text(label)
    if text:
        run(f"{verb} {text}")


# ─── /agents ────────────────────────────────────────────────────────────────

def agents_menu(agent: Any) -> None:
    from axon.commands.agent_cmds import _load_agent_defs, handle_agents
    ws = ws_of(agent)

    def run(text: str) -> None:
        handle_agents(agent, text)

    def rows() -> list[SelectItem]:
        items = []
        for d in sorted(_load_agent_defs(ws), key=lambda x: (str(x.source) != "builtin", x.name)):
            builtin = str(d.source) == "builtin"
            items.append(SelectItem(label=d.name, value=(d.name, builtin),
                                    hint=("built-in · " if builtin else "") + (d.description or "")[:50]))
        return items

    def actions_for(value: tuple[str, bool]) -> list[Action]:
        name, builtin = value
        actions = [Action("Show", lambda: run(f"show {name}"))]
        if builtin:
            actions.append(Action("Override", lambda: run(f"create {name}"), "create your own copy"))
        else:
            actions.append(Action("Edit", lambda: run(f"edit {name}"), "open in your editor"))
            actions.append(Action("Delete", lambda: run(f"delete {name}"), confirm=f"Delete agent '{name}'?"))
        return actions

    def extra() -> list[Action]:
        return [Action("Create an agent", lambda: run("create"), "step by step"),
                Action("Check agent files", lambda: run("check"))]

    browse("Subagents", rows, actions_for, extra)


# ─── /plugin ────────────────────────────────────────────────────────────────

def plugins_menu(agent: Any) -> None:
    from axon.commands import plugins as pl
    from axon.commands.agent_cmds import handle_plugin
    ws = ws_of(agent)

    def run(text: str) -> None:
        handle_plugin(agent, text)

    def rows() -> list[SelectItem]:
        items = []
        for p in pl.load_plugins(ws):
            state = "enabled" if p.enabled else "disabled"
            items.append(SelectItem(label=p.title, value=(p.name, p.enabled, bool(p.marketplace or p.record)),
                                    hint=f"{state} · {p.scope}" + (f" · {p.description[:40]}" if p.description else "")))
        return items

    def actions_for(value: tuple[str, bool, bool]) -> list[Action]:
        name, enabled, from_marketplace = value
        actions = [
            Action("Info", lambda: run(f"info {name}")),
            Action("Test", lambda: run(f"test {name}"), "validate the manifest and files"),
            Action("Disable" if enabled else "Enable", lambda: run(f"{'disable' if enabled else 'enable'} {name}")),
            Action("Configure", lambda: run(f"configure {name}"), "its settings"),
        ]
        if from_marketplace:
            actions.append(Action("Update", lambda: run(f"update {name}"), "from its marketplace"))
        actions.append(Action("Uninstall", lambda: run(f"uninstall {name}"), confirm=f"Remove plugin '{name}'?"))
        return actions

    def create() -> None:
        name = ask_text("Plugin name")
        if name:
            run(f"create {name}")

    def extra() -> list[Action]:
        return [Action("Install a plugin", lambda: _run_with_text(run, "install", "name@marketplace, folder or git URL")),
                Action("Discover plugins", lambda: run("discover")),
                Action("Marketplaces", lambda: run("marketplace list")),
                Action("Create a plugin", create),
                Action("Status", lambda: run("status")),
                Action("Reload", lambda: run("reload"))]

    browse("Plugins", rows, actions_for, extra)


# ─── /hooks ─────────────────────────────────────────────────────────────────

def hooks_menu(agent: Any, hooks: list[dict[str, Any]]) -> None:
    """Pick a hook to see it in full; list-wide: events, reload."""
    from axon.commands.agent_cmds import handle_hooks

    def rows() -> list[SelectItem]:
        items = []
        for number, h in enumerate(hooks, 1):
            summary = str(h.get("command", "")).replace("\n", " ")[:50]
            items.append(SelectItem(label=f"{h.get('event')} {h.get('matcher') or '*'}", value=number,
                                    hint=summary))
        return items

    def actions_for(number: int) -> list[Action]:
        return [Action("Show in full", lambda: handle_hooks(agent, str(number)))]

    def extra() -> list[Action]:
        return [Action("All events", lambda: handle_hooks(agent, "events")),
                Action("Reload", lambda: handle_hooks(agent, "reload"))]

    browse("Hooks", rows, actions_for, extra, subtitle=f"{len(hooks)} configured")



# ─── /sessions ──────────────────────────────────────────────────────────────

def sessions_menu(agent: Any) -> None:
    """Saved sessions: pick one to resume or delete; list-wide: restore deleted ones, delete all."""
    import datetime
    from axon.commands.registry import dispatch_command
    from axon.commands.common import info
    from axon.session.interactive import handle_session_delete
    store = agent.session

    def rows() -> list[SelectItem]:
        items = []
        for meta in store.list_recent(limit=None):
            when = datetime.datetime.fromtimestamp(meta.created_at).strftime("%m-%d %H:%M")
            current = meta.session_id == store.active_session_id
            items.append(SelectItem(
                label=(meta.first_prompt or "New session").replace("\n", " ")[:48], value=meta.session_id,
                hint=f"{when} · {meta.message_count} msgs · ${meta.total_cost:.2f}",
                badge="●" if current else "", search=meta.session_id))
        return items

    def actions_for(session_id: str) -> list[Action]:
        return [Action("Resume", lambda: dispatch_command(f"/resume {session_id}", agent), "switch to it"),
                Action("Delete", lambda: handle_session_delete(store, session_id), "moves it to the trash",
                       confirm="Delete this session? It goes to the trash.")]

    def extra() -> list[Action]:
        return [Action("Restore deleted sessions", lambda: dispatch_command("/restore", agent)),
                Action("Delete every session", lambda: handle_session_delete(store, "--all"),
                       confirm="Move every session of this workspace to the trash?")]

    if not store.list_recent(limit=None):
        info("No saved sessions for this project yet.")
    browse("Sessions", rows, actions_for, extra, subtitle="● current")


# ─── /menu ──────────────────────────────────────────────────────────────────

def navigator(agent: Any) -> None:
    """Every command by category, filterable with "/"; Enter opens the command (its own menu runs),
    Esc comes back here. The one place to walk through everything without typing."""
    from axon.commands.registry import CATEGORIES, dispatch_command, list_command_names, visible_commands

    def rows() -> list[SelectItem]:
        by_category: dict[str, list[Any]] = {}
        for c in visible_commands():
            by_category.setdefault(c.category, []).append(c)
        known = [name for name, _color in CATEGORIES]
        order = known + sorted(c for c in by_category if c not in known)
        items = []
        for category in order:
            for c in sorted(by_category.get(category, []), key=lambda x: x.name):
                items.append(SelectItem(label=f"/{c.name}", value=c.name, hint=c.description[:70], group=category,
                                        search=" ".join(c.aliases)))
        builtin = {c.name for c in visible_commands()}
        for name, description in list_command_names(ws_of(agent), agent):
            if name.lstrip("/") not in builtin:
                items.append(SelectItem(label=name, value=name.lstrip("/"), hint=description[:70],
                                        group="Custom commands and skills"))
        return items

    index = 0
    while True:
        items = rows()
        chosen = choose_item(items, "All commands", f"{len(items)} · / filter · Enter opens it · Esc closes", index)
        if chosen is None:
            return
        index = items.index(chosen) if chosen in items else 0
        dispatch_command(f"/{chosen.value}", agent)
