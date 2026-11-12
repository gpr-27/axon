"""Per-MCP-server tool controls that apply with every provider: allow / deny lists, defer on/off, and pinning.

Config keys of a server (``.mcp.json`` / user config): ``allowedTools`` and ``deniedTools`` (tool names or ``*`` patterns,
matched on the server's own tool name), ``alwaysLoad`` (true: never deferred, false: always deferred) and ``pinTools``
(the tool list seen first stays fixed for the session, so a server that changes its tools mid-session cannot change
what the model is offered or invalidate the prompt cache). ``/mcp filter`` edits them.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Any


def _match(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) or name == p for p in patterns)


def allowed(spec: Any, tool_name: str) -> bool:
    allow, deny = list(getattr(spec, "allow_tools", []) or []), list(getattr(spec, "deny_tools", []) or [])
    if deny and _match(tool_name, deny):
        return False
    return not allow or _match(tool_name, allow)


def filter_specs(spec: Any, tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """(tool specs the server may offer, names left out by the allow/deny lists)."""
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for t in tools:
        if allowed(spec, str(t.get("name", ""))):
            kept.append(t)
        else:
            dropped.append(str(t.get("name", "")))
    return kept, dropped


def pinned(pins: dict[str, list[dict[str, Any]]], spec: Any, name: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool list to publish for *name*: the first one seen when ``pinTools`` is on, else the current one."""
    if not getattr(spec, "pin_tools", False):
        pins.pop(name, None)
        return tools
    return pins.setdefault(name, [dict(t) for t in tools])


def set_option(config: Any, name: str, key: str, value: Any) -> bool:
    """Write one key of a server into the config file that defines it (None removes the key)."""
    spec = config.get_spec(name)
    if spec is None or spec.source is None or spec.managed or spec.scope == "plugin":
        return False
    from axon.mcp.config import _read_json
    data = _read_json(spec.source)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get(name), dict):
        return False
    if value is None:
        servers[name].pop(key, None)
    else:
        servers[name][key] = value
    config._write(spec.source, data)
    return True


USAGE = ("/mcp filter <server>                        show this server's tool controls\n"
         "/mcp filter <server> allow <tool|pattern>... only these tools (none = all)\n"
         "/mcp filter <server> deny <tool|pattern>...  never offer these tools\n"
         "/mcp filter <server> defer on|off|auto       always defer / never defer / by size\n"
         "/mcp filter <server> pin on|off              keep the tool list fixed for the session")


def run(config: Any, args: list[str], say: Any, fail: Any) -> bool:
    """Handle ``/mcp filter``. Returns True when the config changed (the caller reconnects the server)."""
    if not args:
        fail("usage:\n" + USAGE)
        return False
    name, rest = args[0], args[1:]
    spec = config.get_spec(name)
    if spec is None:
        fail(f"MCP server '{name}' is not configured.")
        return False
    if not rest:
        say(f"{name}: allow={spec.allow_tools or 'all'} deny={spec.deny_tools or 'none'} "
            f"defer={'never' if spec.always_load is True else 'always' if spec.always_load is False else 'auto'} "
            f"pin={'on' if spec.pin_tools else 'off'}")
        return False
    verb, vals = rest[0].lower(), rest[1:]
    if verb in ("allow", "deny"):
        key = "allowedTools" if verb == "allow" else "deniedTools"
        ok = set_option(config, name, key, list(vals) or None)
    elif verb == "defer" and vals and vals[0].lower() in ("on", "off", "auto"):
        ok = set_option(config, name, "alwaysLoad", {"on": False, "off": True, "auto": None}[vals[0].lower()])
    elif verb == "pin" and vals and vals[0].lower() in ("on", "off"):
        ok = set_option(config, name, "pinTools", True if vals[0].lower() == "on" else None)
    else:
        fail("usage:\n" + USAGE)
        return False
    if not ok:
        fail(f"'{name}' cannot be edited here (organization-managed, plugin, or not in a config file).")
        return False
    say(f"Updated '{name}' ({json.dumps(rest)}).")
    return True
