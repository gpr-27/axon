"""/permissions: list, add and remove allow/deny rules."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, header, kv, ok, section, usage, typed_hint
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
_KIND_COLOR = {"allow": MINT, "ask": GOLD, "deny": ROSE}


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
            from axon.commands.permissions_menu import permissions_menu
            permissions_menu(agent)
            return CommandResult(handled=True)

    if sub in ("allow", "deny", "add", "ask"):
        kind = "allow" if sub in ("allow", "add") else sub
        if not rest:
            usage(f"/permissions {kind} <rule>   e.g. Bash(git status:*)")
            return CommandResult(handled=True)
        if not hasattr(perms, "add_rule"):
            err("This permission engine does not support persistent rules.")
            return CommandResult(handled=True)
        try:
            rule = perms.add_rule(kind, rest)
        except ValueError as e:
            err(str(e))
            return CommandResult(handled=True)
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
    typed_hint("    /permissions allow|ask|deny <rule> · remove <kind> <rule> · /add-dir <path> · /help permissions")
    print()
    return CommandResult(handled=True)
