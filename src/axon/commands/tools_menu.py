"""
/tools in a terminal: a list of every tool (grouped, with its permission), a detail screen with the
full description and parameters, and the actions "Always allow / Ask each time / Deny / Remove my
rule". Without a terminal /tools prints the same information as text (agent_cmds.handle_tools).

The permission actions use the engine's own add_rule / remove_rule, the functions behind
``/permissions allow|ask|deny <rule>`` and ``/permissions remove`` (and behind /marketplace tool).
"""
from __future__ import annotations

from typing import Any

from axon.commands.common import clip, ok, warn
from axon.ui.detail import BodyLine
from axon.ui.menus import Action, choose_action, choose_item
from axon.ui.picker import SelectItem
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, RST, SLATE, WHITE, ROSE

KINDS = ("allow", "ask", "deny")
PERM_COLOR = {"allow": MINT, "ask": GOLD, "deny": ROSE}


# ─── rules ───────────────────────────────────────────────────────────────────

def rules_for_tool(agent: Any, name: str) -> list[tuple[str, str, str]]:
    """(kind, rule, source) of every rule that mentions the tool: the whole-tool rule, narrower
    ones like ``Bash(git status:*)``, and for an MCP tool the rule of its whole server."""
    found = []
    for entry in agent.permissions.list_rules():
        spec = str(entry.rule)
        if spec == name or spec.startswith(name + "(") or (name.startswith("mcp__") and name.startswith(spec + "__")):
            found.append((entry.kind, spec, entry.source))
    return found


def set_tool_rule(agent: Any, name: str, kind: str) -> None:
    """Make ``kind`` (allow / ask / deny) the rule for the whole tool, replacing an earlier rule of
    the same tool in any list. The typed ``/permissions allow|ask|deny <tool>`` does the saving and
    shows the rule and the file it was saved in."""
    from axon.commands.perm_cmds import handle_permissions
    for other in KINDS:
        agent.permissions.remove_rule(other, name)
    handle_permissions(agent, f"{kind} {name}")   # prints the rule and the file it was saved in


def remove_tool_rule(agent: Any, name: str) -> None:
    """Remove the whole-tool rule (allow, ask or deny): the typed ``/permissions remove <kind> <tool>``."""
    from axon.commands.perm_cmds import handle_permissions
    kinds = [kind for kind, spec, _source in rules_for_tool(agent, name) if spec == name]
    for kind in kinds:
        handle_permissions(agent, f"remove {kind} {name}")
    if not kinds:
        ok(f"You have no rule for {name}; the permission mode decides.")


# ─── the list ────────────────────────────────────────────────────────────────

def _row_renderer(name: str, permission: str, description: str):
    def render(selected: bool, width: int) -> str:
        color = PERM_COLOR.get(permission, SLATE)
        shown = f"{BOLD}{WHITE}{name:<16}{RST}" if selected else f"{SLATE}{name:<16}{RST}"
        room = max(10, width - 16 - 7)
        return f"{shown} {color}{permission:<5}{RST} {DARK_SLATE}{clip(description, room)}{RST}"
    return render


def tool_rows(agent: Any) -> list[SelectItem]:
    from axon.commands.agent_cmds import tool_groups, tool_permission, tool_table
    tools, _deferred = tool_table(agent)
    rows = []
    for title, names in tool_groups(tools):
        for name in names:
            tool = tools[name]
            permission = tool_permission(agent, tool)
            first_line = (tool.description.splitlines() or [""])[0]
            rows.append(SelectItem(label=name, value=name, hint=f"{permission} {first_line}", group=title,
                                   render=_row_renderer(name, permission, first_line)))
    return rows


def tools_menu(agent: Any) -> None:
    """List -> pick a tool -> detail screen with actions -> back to the list, until Esc."""
    index = 0
    while True:
        rows = tool_rows(agent)
        if not rows:
            return
        chosen = choose_item(rows, "Tools", f"{len(rows)} available · mode {agent.settings.mode}", min(index, len(rows) - 1))
        if chosen is None:
            return
        index = rows.index(chosen) if chosen in rows else 0
        tool_detail(agent, chosen.value)


# ─── the detail screen ───────────────────────────────────────────────────────

def detail_body(agent: Any, tool: Any, deferred: bool) -> list[BodyLine]:
    """The text of the detail screen: description, parameters and the rules that apply."""
    from axon.commands.agent_cmds import param_type
    traits = ("read-only" if tool.readonly else "can modify") + f" · default {tool.default_permission}"
    if deferred:
        traits += " · deferred: the model fetches this definition with ToolSearch when it needs it"
    lines = [BodyLine(traits, 0, DARK_SLATE), BodyLine()]
    for paragraph in tool.description.split("\n"):
        lines.append(BodyLine(paragraph.strip(), 0, SLATE))
    props = tool.schema.get("properties") or {}
    required = set(tool.schema.get("required") or [])
    if props:
        lines += [BodyLine(), BodyLine("Parameters  (* = required)", 0, f"{BOLD}{GOLD}")]
        for pname, spec in props.items():
            spec = spec if isinstance(spec, dict) else {}
            mark = "*" if pname in required else " "
            lines.append(BodyLine(f"{mark} {pname}  {param_type(spec)}", 1, f"{BOLD}{WHITE}"))
            if spec.get("enum"):
                lines.append(BodyLine("one of: " + ", ".join(map(str, spec["enum"])), 5, DARK_SLATE))
            if "default" in spec:
                lines.append(BodyLine(f"default: {spec['default']}", 5, DARK_SLATE))
            for paragraph in str(spec.get("description", "")).split("\n"):
                if paragraph.strip():
                    lines.append(BodyLine(paragraph.strip(), 5, SLATE))
    lines += [BodyLine(), BodyLine("Rules", 0, f"{BOLD}{GOLD}")]
    rules = rules_for_tool(agent, tool.name)
    if rules:
        for kind, spec, source in rules:
            lines.append(BodyLine(f"{kind}  {spec}  ({source})", 1, PERM_COLOR.get(kind, SLATE)))
    else:
        lines.append(BodyLine("No rule of yours: the permission mode decides.", 1, DARK_SLATE))
    return lines


def tool_actions(agent: Any, name: str) -> list[Action]:
    where = agent.permissions.local_settings_path()
    return [
        Action("Always allow", lambda: set_tool_rule(agent, name, "allow"), f"saved in {where.name}"),
        Action("Ask each time", lambda: set_tool_rule(agent, name, "ask"), f"saved in {where.name}"),
        Action("Deny", lambda: set_tool_rule(agent, name, "deny"), f"saved in {where.name}"),
        Action("Remove my rule", lambda: remove_tool_rule(agent, name), "the mode decides again"),
        Action("Back", lambda: None),
    ]


def tool_detail(agent: Any, name: str) -> None:
    """One tool in full. Runs an action, then shows the screen again with the new state; Esc or
    "Back" returns to the list."""
    from axon.commands.agent_cmds import tool_permission, tool_table
    while True:
        tools, deferred = tool_table(agent)
        tool = tools.get(name)
        if tool is None:
            warn(f"The tool {name} is no longer available.")
            return
        permission = tool_permission(agent, tool)
        subtitle = f"now: {PERM_COLOR.get(permission, SLATE)}{permission}{RST}  {SLATE}mode {agent.settings.mode}{RST}"
        action = choose_action(name, tool_actions(agent, name), subtitle, detail_body(agent, tool, name in deferred))
        if action is None or action.label == "Back":
            return
        action.run()
