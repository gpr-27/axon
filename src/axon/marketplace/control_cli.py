"""
The control subcommands of ``/marketplace`` (and ``axon marketplace``): status, test, reconnect, logs,
scope, env, timeout, tool permissions, export / import, automation, suggestions and updates.

Each handler has the shape ``handler(shop, args, rest) -> Outcome`` where ``shop`` is the
:class:`axon.marketplace.cli.Marketplace` running the command. The logic lives in
:mod:`axon.marketplace.control` and friends; this file only reads the words and prints.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from axon.marketplace import automation, control, status, suggest, transfer, updates

if TYPE_CHECKING:
    from axon.marketplace.cli import Args, Marketplace, Outcome

Handler = Callable[["Marketplace", "Args", "list[str]"], "Outcome"]
DEFAULT_LOG_LINES = 40


def report(shop: "Marketplace", result: control.Done) -> "Outcome":
    return shop.report_simple(result.ok, result.message)


def need_name(shop: "Marketplace", rest: list[str], usage: str) -> str:
    if rest:
        return rest[0]
    shop.fail(f"usage: {usage}")
    return ""


def is_connector(shop: "Marketplace", args: "Args", name: str, what: str) -> bool:
    """False (after saying why) when ``name`` is an installed skill or plugin."""
    wanted = args.option("tab")
    tab = shop.installed_tab(args, name)
    if tab == "connectors":
        return True
    if tab is None:
        if wanted in ("skills", "plugins"):
            shop.fail(f"No {wanted[:-1]} called '{name}' is installed in your own folders.")
            return False
        return True
    hint = " Use /plugin configure for plugin settings." if tab == "plugins" else ""
    shop.fail(f"{what} applies to connectors only; '{name}' is a {tab[:-1]}.{hint}")
    return False


# ─── status / test / reconnect / logs ────────────────────────────────────────

def cmd_status(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    timeout = shop_timeout(args)
    show_all = "all" in args.flags
    rows = status.collect(shop.workspace, check="check" in args.flags, timeout=timeout,
                          health_check=shop.health_check, include_bundled=show_all)
    if "json" in args.flags:
        shop.say(json.dumps(status.as_json(rows), indent=2))
        return shop.outcome
    hidden = 0 if show_all else status.bundled_skill_count(shop.workspace)
    shop.say(status.render(rows, hidden))
    return shop.outcome


def shop_timeout(args: "Args") -> float:
    from axon.marketplace.cli import parse_timeout
    return parse_timeout(args.option("timeout"))


def cmd_test(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    name = need_name(shop, rest, "/marketplace test <id>")
    if not name:
        return shop.outcome
    tab = shop.installed_tab(args, name)
    if tab == "skills":
        return report(shop, control.check_skill(shop.workspace, name))
    if tab == "plugins":
        return report(shop, control.check_plugin(shop.workspace, name))
    wanted = args.option("tab")
    if wanted in ("skills", "plugins"):
        return shop.fail(f"No {wanted[:-1]} called '{name}' is installed in your own folders.")
    if tab is None and name not in shop.connector_specs():
        return shop.fail(f"'{name}' is not an installed connector, skill or plugin.")
    shop.say(f"  Testing {name}...")
    result, tools = control.probe_server(shop.workspace, name, shop_timeout(args), shop.health_check)
    if result.ok and tools:
        shop.say("      Tools: " + ", ".join(tools[:8]) + (f" and {len(tools) - 8} more" if len(tools) > 8 else ""))
    return report(shop, result)


def cmd_reconnect(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    name = need_name(shop, rest, "/marketplace reconnect <id|all>")
    if not name or (name != "all" and not is_connector(shop, args, name, "Reconnecting")):
        return shop.outcome
    if not shop.in_session:
        shop.say("  No session is running, so there is nothing to reconnect. Checking the server instead.")
        return cmd_test(shop, args, rest)
    names = sorted(shop.connector_specs()) if name == "all" else [name]
    unknown = [n for n in names if n not in shop.connector_specs()]
    if unknown:
        return shop.fail(f"'{unknown[0]}' is not configured.")
    shop.outcome.reconnect.extend(names)
    shop.say(f"  Reconnecting {', '.join(names)}...")
    return shop.outcome


def cmd_logs(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    name = need_name(shop, rest, "/marketplace logs <id> [lines]")
    if not name or not is_connector(shop, args, name, "Logs"):
        return shop.outcome
    count = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else DEFAULT_LOG_LINES
    lines = control.read_logs(name, count)
    if not lines:
        shop.say(f"  No log for '{name}' yet.")
        return shop.outcome
    shop.say([f"  {line}" for line in lines])
    return shop.outcome


# ─── editing a connector ─────────────────────────────────────────────────────

def cmd_scope(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    if len(rest) < 2:
        return shop.fail("usage: /marketplace scope <id> user|project|local")
    name, target = rest[0], rest[1]
    tab = shop.installed_tab(args, name)
    if tab == "skills":
        return report(shop, control.move_skill_scope(shop.workspace, name, target))
    if tab == "plugins":
        return report(shop, control.move_plugin_scope(shop.workspace, name, target))
    if tab is None and args.option("tab") in ("skills", "plugins"):
        return shop.fail(f"No {args.option('tab')[:-1]} called '{name}' is installed in your own folders, "
                         f"so there is nothing to move (skills from ~/.claude, plugins and Axon itself stay where they are).")
    if tab is None and name not in shop.connector_specs():
        return shop.fail(f"'{name}' is not an installed connector, skill or plugin.")
    result = control.move_scope(shop.workspace, name, target, args.option("from") or None)
    if result.ok:
        shop.outcome.reconnect.append(name)
    return report(shop, result)


def parse_assignments(shop: "Marketplace", words: list[str]) -> dict[str, str] | None:
    """NAME=VALUE words; a bare NAME asks for the value with hidden input. None when it cannot be had."""
    values: dict[str, str] = {}
    for word in words:
        name, has_value, value = word.partition("=")
        if has_value:
            values[name] = value
            continue
        if shop.ask_secret is None:
            shop.fail(f"give a value for {name}: NAME=VALUE (no terminal to ask on)")
            return None
        values[name] = shop.ask_secret(f"  {name}: ").strip()
    return values


def cmd_env(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    name = need_name(shop, rest, "/marketplace env <id> NAME=VALUE ... [--unset NAME] [--literal]")
    if not name or not is_connector(shop, args, name, "Keys and variables"):
        return shop.outcome
    values = parse_assignments(shop, rest[1:])
    if values is None:
        return shop.outcome
    unset = args.option_list("unset")
    if not values and not unset:
        return shop.fail("nothing to change. Give NAME=VALUE or --unset NAME.")
    result = control.set_env(shop.workspace, name, values, unset, literal="literal" in args.flags)
    if result.ok:
        shop.outcome.reconnect.append(name)
    return report(shop, result)


def cmd_timeout(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    if len(rest) < 2:
        return shop.fail("usage: /marketplace timeout <id> <seconds>")
    if not is_connector(shop, args, rest[0], "The timeout"):
        return shop.outcome
    try:
        seconds = float(rest[1])
    except ValueError:
        return shop.fail("the timeout must be a number of seconds")
    result = control.set_timeout(shop.workspace, rest[0], seconds)
    if result.ok:
        shop.outcome.reconnect.append(rest[0])
    return report(shop, result)


def cmd_tool(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    usage = "/marketplace tool <server> [<tool>|*] allow|ask|deny|reset [--global]"
    name = need_name(shop, rest, usage)
    if not name or not is_connector(shop, args, name, "Tool permissions"):
        return shop.outcome
    if len(rest) == 1:
        return list_tool_policies(shop, name)
    policy = rest[-1].lower()
    tool = rest[1] if len(rest) > 2 else "*"
    result = control.set_tool_policy(shop.workspace, name, tool, policy, shop.permissions, "global" in args.flags)
    return report(shop, result)


def list_tool_policies(shop: "Marketplace", server: str) -> "Outcome":
    rules = control.tool_policies(shop.workspace, server, shop.permissions)
    if not rules:
        shop.say(f"  No allow/ask/deny rules for '{server}'. The permission mode decides.")
        return shop.outcome
    for kind, rule, source in rules:
        shop.say(f"  {kind:<6}{rule}   ({source})")
    return shop.outcome


# ─── export / import ─────────────────────────────────────────────────────────

def cmd_export(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    target = Path(rest[0]) if rest else shop.workspace / "axon-setup.json"
    secrets = "include-secrets" in args.flags
    path = transfer.write_export(shop.workspace, target, secrets)
    shop.say(f"  OK: Exported your setup to {path}")
    shop.say("      Keys are not in the file." if not secrets else "      The file holds your keys; it is owner-only. Keep it safe.")
    return shop.outcome


def cmd_import(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    if not rest:
        return shop.fail("usage: /marketplace import <file> [--dry-run] [--overwrite]")
    try:
        data = transfer.read_setup(Path(rest[0]))
    except ValueError as e:
        return shop.fail(str(e))
    dry = "dry-run" in args.flags
    result = transfer.import_setup(shop.workspace, data, overwrite="overwrite" in args.flags, dry_run=dry)
    shop.say(result.lines or ["  The file has nothing to import."])
    shop.outcome.reconnect.extend(result.changed)
    shop.say("  Dry run: nothing was changed." if dry else "  OK: import finished.")
    return shop.outcome


# ─── automation, suggestions, updates ────────────────────────────────────────

def cmd_auto(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    word = rest[0].lower() if rest else "status"
    if word in ("on", "off"):
        try:
            path = automation.save_automation({"enabled": word == "on"})
        except ValueError as e:
            return shop.fail(str(e))
        shop.say(f"  OK: Automation is {word.upper()} (saved in {path}).")
        if word == "on":
            shop.say(automation.status_lines(shop.workspace))
        return shop.outcome
    if word == "set":
        return set_automation_value(shop, rest[1:])
    if word in ("status", "show"):
        shop.say(automation.status_lines(shop.workspace))
        return shop.outcome
    return shop.fail("usage: /marketplace auto on|off|status|set <name> <value>")


def set_automation_value(shop: "Marketplace", words: list[str]) -> "Outcome":
    if len(words) != 2 or words[0] not in automation.setting_names():
        names = ", ".join(automation.setting_names())
        return shop.fail(f"usage: /marketplace auto set <name> <value>   names: {names}")
    try:
        value = automation.parse_value(words[0], words[1])
    except ValueError as e:
        return shop.fail(str(e))
    try:
        automation.save_automation({words[0]: value})
    except ValueError as e:
        return shop.fail(str(e))
    shop.say(f"  OK: {words[0]} = {value}")
    return shop.outcome


def cmd_suggest(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    if "reset" in args.flags:
        suggest.forget(shop.workspace)
        shop.say("  OK: this project will be told about fitting connectors again at the next start.")
        return shop.outcome
    shop.say(suggest.suggestion_lines(shop.workspace))
    return shop.outcome


def cmd_updates(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    shop.say("  Checking for updates...")
    found = updates.check_updates(shop.workspace)
    updates.mark_checked(found)
    if not found:
        shop.say("  Everything you installed is up to date.")
        return shop.outcome
    shop.say([f"  {item.line()}" for item in found])
    return shop.outcome


def cmd_update(shop: "Marketplace", args: "Args", rest: list[str]) -> "Outcome":
    name = need_name(shop, rest, "/marketplace update <id>")
    if not name:
        return shop.outcome
    tab = shop.installed_tab(args, name)
    if tab == "skills":
        return apply_update(shop, lambda: updates.apply_skill_update(shop.workspace, name))
    if tab == "plugins":
        return apply_update(shop, lambda: updates.apply_plugin_update(shop.workspace, name))
    if tab == "connectors":
        return shop.fail("Connectors are never updated automatically. See /marketplace updates, "
                         "then remove and install the connector again.")
    return shop.fail(f"'{name}' is not installed.")


def apply_update(shop: "Marketplace", action: Callable[[], str]) -> "Outcome":
    try:
        return shop.report_simple(True, action())
    except Exception as e:  # noqa: BLE001 - network and git errors become one clear line
        return shop.fail(str(e))


HANDLERS: dict[str, Handler] = {
    "status": cmd_status, "dashboard": cmd_status,
    "test": cmd_test, "check": cmd_test,
    "reconnect": cmd_reconnect, "restart": cmd_reconnect,
    "logs": cmd_logs, "log": cmd_logs,
    "scope": cmd_scope, "env": cmd_env, "timeout": cmd_timeout, "tool": cmd_tool,
    "export": cmd_export, "import": cmd_import,
    "auto": cmd_auto, "suggest": cmd_suggest, "updates": cmd_updates, "update": cmd_update,
}
