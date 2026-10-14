"""/permissions: list, add and remove allow/deny rules."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, header, hint, kv, ok, section, usage
from axon.commands.registry import CommandResult, command
from axon.ui.theme import BOLD, DARK_SLATE, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE

USAGE = """/permissions                          interactive panel (Allow / Ask / Deny / Directories tabs); list without a terminal
/permissions list                     list mode and rules
/permissions allow <rule>             add an allow rule (saved to .axon/settings.local.json)
/permissions ask <rule>               always ask before matching calls (beats allow rules and bypass)
/permissions deny <rule>              add a deny rule
/permissions remove <allow|ask|deny> <rule>
/permissions session <allow|ask|deny> <rule>   rule for this session only
Rule examples: Bash(git status:*)  Bash(npm run *)  Edit(src/**)  Read(~/.ssh/**)  WebFetch(domain:docs.python.org)  mcp__github"""


KINDS = ("allow", "ask", "deny")
TABS = ["Allow", "Ask", "Deny", "Directories"]
_KIND_COLOR = {"allow": MINT, "ask": GOLD, "deny": ROSE}


def _panel(agent: Any) -> CommandResult:
    """Interactive rules panel: Tab switches Allow/Ask/Deny/Directories, Enter on "+ Add" adds,
    d / Delete removes the selected entry. Rules are saved to .axon/settings.local.json."""
    from axon.ui.picker import SelectItem, Selector
    perms = agent.permissions
    add_label = {"allow": "+ Add allow rule…", "ask": "+ Add ask rule…", "deny": "+ Add deny rule…", "dirs": "+ Add directory…"}

    def kind_of(tab: int) -> str:
        return ("allow", "ask", "deny", "dirs")[tab]

    def items_for(tab: int) -> list[SelectItem]:
        kind = kind_of(tab)
        out = [SelectItem(label=add_label[kind], value=("add", kind, None))]
        if kind == "dirs":
            for d in (perms.working_dirs() if hasattr(perms, "working_dirs") else []):
                out.append(SelectItem(label=str(d), value=("item", kind, str(d)), badge=f"{TEAL}▸{RST}"))
            return out
        for e in perms.list_rules():
            if e.kind == kind:
                out.append(SelectItem(label=str(e.rule), value=("item", kind, str(e.rule)), hint=e.source,
                                      badge=f"{_KIND_COLOR[kind]}●{RST}"))
        return out

    def on_key(sel: Any, key: str) -> bool:
        cur = sel.current()
        if cur is None:
            return False
        action, kind, spec = cur.value
        if key == "enter" and action == "add":
            text = sel.prompt_line("Directory: " if kind == "dirs" else f"{kind} rule (e.g. Bash(npm test:*)): ")
            if text:
                try:
                    if kind == "dirs":
                        from axon.commands.workspace_cmds import _resolve_dir, add_working_dir
                        p = _resolve_dir(agent, text)
                        if p is None:
                            sel.flash(f"Not a directory: {text}", ROSE)
                            return True
                        add_working_dir(agent, p, save=True)
                        sel.flash(f"Added {p}")
                    else:
                        rule = perms.add_rule(kind, text)
                        sel.flash(f"Added {kind} {rule}")
                except Exception as e:  # invalid rule syntax
                    sel.flash(f"Could not add: {e}", ROSE)
                sel.set_items(items_for(sel.tab_index))
            return True
        if key in ("d", "delete", "backspace", "x") and action == "item":
            if sel.confirm(f"Remove {spec}?"):
                if kind == "dirs":
                    from axon.commands.workspace_cmds import handle_add_dir
                    import contextlib
                    import io
                    with contextlib.redirect_stdout(io.StringIO()):
                        handle_add_dir(agent, f"remove {spec}")
                    sel.flash(f"Removed {spec}")
                else:
                    where = perms.remove_rule(kind, spec)
                    sel.flash(f"Removed from {', '.join(where)}" if where else "Not removable (built-in config)",
                              MINT if where else ROSE)
                sel.set_items(items_for(sel.tab_index), keep_index=False)
            return True
        if key == "enter":
            return True  # Enter on a rule does nothing (d removes it)
        return False

    Selector(title="Permissions", subtitle=f"mode {agent.settings.mode} · saved to {perms.local_settings_path()}",
             tabs=TABS, items_for_tab=items_for, on_key=on_key,
             footer="Tab switch · Enter add · d remove · Esc close").run()
    return CommandResult(handled=True)


@command("permissions", category="Model & Modes", description="Manage allow / ask / deny rules and working directories",
         aliases=("perms", "permission"), usage=USAGE)
def handle_permissions(agent: Any, arg: str) -> CommandResult:
    perms = agent.permissions
    parts = arg.strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if not sub:
        from axon.commands.common import is_tty
        if is_tty() and hasattr(perms, "list_rules"):
            return _panel(agent)

    if sub in ("allow", "deny", "add", "ask"):
        kind = "allow" if sub in ("allow", "add") else sub
        if not rest:
            usage(f"/permissions {kind} <rule>   e.g. Bash(git status:*)")
            return CommandResult(handled=True)
        if not hasattr(perms, "add_rule"):
            err("This permission engine does not support persistent rules.")
            return CommandResult(handled=True)
        rule = perms.add_rule(kind, rest)
        color = _KIND_COLOR[kind]
        ok(f"{color}{kind}{RST} {BOLD}{rule}{RST} {SLATE}→ {perms.local_settings_path()}{RST}")
        return CommandResult(handled=True)

    if sub in ("session", "temp"):
        k, _, spec = rest.partition(" ")
        if k not in KINDS or not spec.strip():
            usage("/permissions session <allow|ask|deny> <rule>")
            return CommandResult(handled=True)
        rule = perms.add_session_rule(k, spec.strip())
        ok(f"Session {k} rule {BOLD}{rule}{RST} (not saved)")
        return CommandResult(handled=True)

    if sub in ("remove", "rm", "delete", "del"):
        k, _, spec = rest.partition(" ")
        if k not in KINDS or not spec.strip():
            usage("/permissions remove <allow|ask|deny> <rule>")
            return CommandResult(handled=True)
        removed = perms.remove_rule(k, spec.strip())
        if removed:
            ok(f"Removed {k} {BOLD}{spec.strip()}{RST} from {', '.join(removed)}")
        else:
            err(f"No {k} rule '{spec.strip()}' found.")
        return CommandResult(handled=True)

    if sub in ("reload",):
        perms.reload()
        ok("Permission rules reloaded from disk.")
        return CommandResult(handled=True)

    if sub and sub not in ("list", "ls", "show"):
        usage(USAGE)
        return CommandResult(handled=True)

    mode = agent.settings.mode
    header("Permissions", f"mode {mode}")
    kv("Workspace", agent.settings.workspace)
    kv("Mode", {"default": "ask before edits/commands", "acceptEdits": "auto-approve edits in workspace",
                "plan": "read-only", "bypass": "approve everything except deny rules"}.get(mode, mode))
    entries = perms.list_rules() if hasattr(perms, "list_rules") else []
    for kind, color in (("deny", ROSE), ("ask", GOLD), ("allow", MINT)):
        rows = [e for e in entries if e.kind == kind]
        section(f"{kind.capitalize()} rules ({len(rows)})", color)
        if not rows:
            print(f"    {DARK_SLATE}none{RST}")
        for e in rows:
            print(f"    {color}●{RST} {WHITE}{e.rule}{RST}  {DARK_SLATE}{e.source}{RST}")
    dirs = perms.working_dirs() if hasattr(perms, "working_dirs") else []
    if dirs:
        section(f"Additional directories ({len(dirs)})", TEAL)
        for d in dirs:
            print(f"    {TEAL}▸{RST} {WHITE}{d}{RST}")
    section("Always protected", GOLD)
    print(f"    {SLATE}credential stores (~/.ssh, ~/.aws, ~/.gnupg, .netrc, ~/.axon/.env), OS system directories,{RST}")
    print(f"    {SLATE}catastrophic shell commands (rm -rf /, mkfs, dd to devices, fork bombs){RST}")
    print(f"    {SLATE}paths outside the workspace always ask (except bypass mode){RST}")
    print()
    hint("    /permissions allow|ask|deny <rule> · remove <kind> <rule> · /add-dir <path> · /help permissions")
    print()
    return CommandResult(handled=True)
