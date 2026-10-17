"""
`/mcp` slash command: the one user-facing entry point for MCP management.

    /mcp                         status of every configured server (menu in a TTY)
    /mcp list|status
    /mcp add <name> [opts] <command> [args...]      stdio server
    /mcp add --transport http|sse <name> <url> [--header "K: V"]...
          options: --scope user|project|local, --env K=V, --timeout SECONDS
    /mcp add <preset>  |  /mcp install [preset]     catalog presets
    /mcp add-json <name> '<json spec>' [--scope ...]
    /mcp remove <name> [--scope ...]
    /mcp enable|disable <name>
    /mcp restart|reconnect <name>   |   /mcp connect   |   /mcp disconnect [name]
    /mcp tools [server]  |  /mcp resources [server]  |  /mcp read <server> <uri>
    /mcp prompts [server] | /mcp get-prompt <server> <prompt> [key=value ...]
    /mcp logs <name> [lines]  |  /mcp import  |  /mcp presets
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from typing import TYPE_CHECKING, Any

from axon.mcp.bridge import MCPToolBridge, get_bridge
from axon.mcp.catalog import MCP_CATALOG, MCPServerPreset, get_mcp_preset
from axon.mcp.config import MCPConfig, normalize_scope
from axon.mcp.transports import log_path_for

try:
    from axon.ui.theme import BOLD, DIM, GOLD, MINT, ROSE, RST, SLATE, TEAL, WHITE
except (ImportError, AttributeError):  # pragma: no cover - theme is optional
    BOLD = DIM = GOLD = MINT = ROSE = RST = SLATE = TEAL = WHITE = ""

if TYPE_CHECKING:
    from axon.agent.loop import Agent


# ─── output helpers ─────────────────────────────────────────────────────

def _out(text: str = "") -> None:
    """print() that never crashes on legacy (cp1252) consoles."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        text = text.encode(enc, "replace").decode(enc, "replace")
    print(text)


def _unicode_ok() -> bool:
    enc = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    return enc in ("utf8", "utf_8")


def _dot(status: str) -> str:
    dot = "●" if _unicode_ok() else "*"
    color = {"connected": MINT, "connecting": GOLD, "disabled": SLATE,
             "needs-config": GOLD}.get(status, ROSE)
    return f"{color}{dot}{RST}"


def _ok(msg: str) -> None:
    _out(f"  {MINT}{'✓' if _unicode_ok() else '+'} {msg}{RST}")


def _err(msg: str) -> None:
    _out(f"  {ROSE}{'✗' if _unicode_ok() else 'x'} {msg}{RST}")


def _split(text: str) -> list[str]:
    """Split a slash-command argument string. POSIX rules on POSIX; on Windows backslashes are
    literal (paths like C:\\dir\\s.py survive) and surrounding quotes are stripped."""
    posix = os.name != "nt"
    try:
        lex = shlex.shlex(text, posix=posix)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return text.split()
    if not posix:
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"" else t for t in tokens]
    return tokens


def _confirm(question: str) -> bool:
    """Explicit yes/no question; anything but y/yes (including a stray Enter) means No."""
    try:
        ans = input(f"  {BOLD}{WHITE}{question} [y/N]: {RST}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return ans in ("y", "yes")


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# ─── context ────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, agent: "Agent") -> None:
        self.agent = agent
        self.workspace = agent.settings.workspace
        self.config = MCPConfig(self.workspace)
        self.bridge: MCPToolBridge = get_bridge(self.workspace)
        registry = getattr(agent, "registry", None)
        if registry is not None:
            self.bridge.attach_registry(registry)

    def connect(self, name: str) -> None:
        spec = self.config.get_spec(name)
        if spec is None:
            _err(f"MCP server '{name}' is not configured.")
            return
        if spec.disabled:
            self.bridge.disconnect_server(name)
            _out(f"  {SLATE}'{name}' is disabled (run /mcp enable {name}).{RST}")
            return
        _out(f"  {DIM}Connecting to '{name}' ({spec.type})...{RST}")
        conn = self.bridge.connect_server(spec)
        if conn.connected:
            n = len([t for t in self.bridge.active_tools if getattr(t, "server_name", None) == name])
            info = conn.client.server_info if conn.client else {}
            label = f"{info.get('name', '')} {info.get('version', '')}".strip()
            _ok(f"'{name}' connected{f' ({label})' if label else ''}: {n} tool(s) registered.")
        else:
            _err(f"'{name}' {conn.status}: {conn.error}")
            _out(f"  {DIM}Logs: {log_path_for(name)}{RST}")


# ─── subcommands ────────────────────────────────────────────────────────

def _cmd_list(ctx: _Ctx, _args: list[str]) -> None:
    specs = ctx.config.load_specs()
    rows = {r["name"]: r for r in ctx.bridge.status_rows()}
    _out(f"\n  {GOLD}{BOLD}MCP servers ({len(specs)}){RST}")
    if not specs:
        _out(f"  {SLATE}No MCP servers configured.{RST}")
        _out(f"  {DIM}Add one: /mcp add <name> <command> [args...]   "
             f"or /mcp add --transport http <name> <url>   or /mcp install{RST}\n")
        return
    for name, spec in sorted(specs.items()):
        row = rows.get(name)
        status = "disabled" if spec.disabled else (row["status"] if row else "not connected")
        transport = row["transport"] if row else spec.type
        tools = row["tools"] if row else 0
        _out(f"  {_dot(status)} {TEAL}{BOLD}{name}{RST} {SLATE}[{transport} · {spec.scope}]{RST} "
             f"{WHITE}{status}{RST}{f' · {tools} tools' if status == 'connected' else ''}")
        _out(f"      {DIM}{spec.describe_target()[:100]}{RST}")
        if row and row.get("error") and status != "connected":
            _out(f"      {ROSE}{str(row['error'])[:200]}{RST}")
    _out(f"\n  {DIM}/mcp tools · /mcp restart <name> · /mcp logs <name> · /mcp help{RST}\n")


def _parse_add(tokens: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"transport": None, "scope": "user", "headers": {}, "env": {},
                            "timeout": None, "positional": [], "command_args": []}
    i = 0

    def _val(flag: str) -> str:
        nonlocal i
        if "=" in flag and flag.startswith("--"):
            return flag.split("=", 1)[1]
        i += 1
        if i >= len(tokens):
            raise ValueError(f"{flag} needs a value")
        return tokens[i]

    while i < len(tokens):
        tok = tokens[i]
        key = tok.split("=", 1)[0] if tok.startswith("--") else tok
        if tok == "--":
            rest = tokens[i + 1:]
            if not opts["positional"]:
                raise ValueError("missing server name")
            if rest:
                opts["positional"].append(rest[0])
                opts["command_args"] = rest[1:]
                opts["transport"] = opts["transport"] or "stdio"
            break
        if key in ("-t", "--transport"):
            opts["transport"] = _val(tok).lower()
        elif key in ("-s", "--scope"):
            opts["scope"] = normalize_scope(_val(tok))
        elif key in ("-H", "--header"):
            k, sep, v = _val(tok).partition(":")
            if not sep:
                raise ValueError("header must look like 'Name: value'")
            opts["headers"][k.strip()] = v.strip()
        elif key in ("-e", "--env"):
            k, sep, v = _val(tok).partition("=")
            if not sep:
                raise ValueError("env must look like KEY=value")
            opts["env"][k.strip()] = v
        elif key == "--timeout":
            opts["timeout"] = float(_val(tok))
        else:
            opts["positional"].append(tok)
            if len(opts["positional"]) == 2:
                is_url = tok.startswith(("http://", "https://"))
                if opts["transport"] in (None, "stdio") and not is_url:
                    opts["transport"] = "stdio"
                    opts["command_args"] = tokens[i + 1:]
                    break
                opts["transport"] = opts["transport"] or "http"
        i += 1
    return opts


def _cmd_add(ctx: _Ctx, args: list[str]) -> None:
    if len(args) == 1 and not args[0].startswith("-") and get_mcp_preset(args[0]):
        _install_preset(ctx, get_mcp_preset(args[0]))  # type: ignore[arg-type]
        return
    if not args:
        if _interactive():
            _add_wizard(ctx)
        else:
            _out(f"  {SLATE}Usage: /mcp add <name> <command> [args...] | "
                 f"/mcp add --transport http <name> <url> [--header 'K: V']{RST}")
        return
    try:
        o = _parse_add(args)
    except ValueError as e:
        _err(str(e))
        return
    pos = o["positional"]
    if len(pos) < 2:
        _err("usage: /mcp add <name> <command|url> ...")
        return
    transport = {"streamable-http": "http"}.get(o["transport"], o["transport"])
    if transport not in ("stdio", "http", "sse"):
        _err(f"unknown transport '{o['transport']}' (use stdio, http or sse)")
        return
    raw: dict[str, Any]
    if transport == "stdio":
        raw = {"command": pos[1], "args": o["command_args"], "env": o["env"]}
    else:
        raw = {"type": transport, "url": pos[1], "headers": o["headers"]}
    if o["timeout"]:
        raw["timeout"] = o["timeout"]
    _save_and_connect(ctx, pos[0], raw, o["scope"])


def _save_and_connect(ctx: _Ctx, name: str, raw: dict[str, Any], scope: str) -> None:
    try:
        path = ctx.config.add_raw(name, raw, scope)
    except (ValueError, OSError) as e:
        _err(f"could not save '{name}': {e}")
        return
    _ok(f"Saved MCP server '{name}' to {path}")
    winner = ctx.config.get_spec(name)
    if winner is not None and winner.source != path:
        _out(f"  {GOLD}Note: '{name}' is also defined in {winner.source}, which takes precedence.{RST}")
    ctx.connect(name)


def _cmd_add_json(ctx: _Ctx, rest: str) -> None:
    tokens = rest.split(None, 1)
    if len(tokens) < 2:
        _err("usage: /mcp add-json <name> '<json>' [--scope user|project|local]")
        return
    name, payload = tokens[0], tokens[1].strip()
    scope = "user"
    for flag in ("--scope", "-s"):
        idx = payload.rfind(f" {flag} ")
        if idx != -1:
            scope_tok = payload[idx + len(flag) + 2:].strip()
            payload = payload[:idx].strip()
            try:
                scope = normalize_scope(scope_tok)
            except ValueError as e:
                _err(str(e))
                return
            break
    if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in "'\"":
        payload = payload[1:-1]
    try:
        raw = json.loads(payload)
    except ValueError as e:
        _err(f"invalid JSON: {e}")
        return
    if not isinstance(raw, dict):
        _err("the JSON spec must be an object")
        return
    _save_and_connect(ctx, name, raw, scope)


def _cmd_add_json_tokens(ctx: _Ctx, tokens: list[str]) -> None:
    """argv form: add-json <name> <json> [--scope S]."""
    scope = "user"
    for flag in ("--scope", "-s"):
        if flag in tokens:
            i = tokens.index(flag)
            if i + 1 >= len(tokens):
                _err(f"{flag} needs a value")
                return
            try:
                scope = normalize_scope(tokens[i + 1])
            except ValueError as e:
                _err(str(e))
                return
            tokens = tokens[:i] + tokens[i + 2:]
            break
    if len(tokens) < 2:
        _err("usage: axon mcp add-json <name> '<json>' [--scope user|project|local]")
        return
    try:
        raw = json.loads(" ".join(tokens[1:]))
    except ValueError as e:
        _err(f"invalid JSON: {e}")
        return
    if not isinstance(raw, dict):
        _err("the JSON spec must be an object")
        return
    _save_and_connect(ctx, tokens[0], raw, scope)


def _cmd_remove(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        names = sorted(ctx.config.load_specs())
        choice = _pick(names, "Select MCP server to remove") if names and _interactive() else None
        if not choice:
            _out(f"  {SLATE}Usage: /mcp remove <name> [--scope user|project|local]{RST}")
            return
        args = [choice]
    scope = None
    rest: list[str] = []
    i = 0
    while i < len(args):  # --scope may come before or after the name; --scope=X works too
        tok = args[i]
        if tok in ("--scope", "-s") or tok.startswith("--scope="):
            val = tok.split("=", 1)[1] if "=" in tok else (args[i + 1] if i + 1 < len(args) else "")
            i += 1 if "=" in tok else 2
            try:
                scope = normalize_scope(val) if val else None
            except ValueError as e:
                _err(str(e))
                return
            if scope is None:
                _err("--scope needs a value (user, project or local)")
                return
            continue
        rest.append(tok)
        i += 1
    if not rest:
        _out(f"  {SLATE}Usage: /mcp remove <name> [--scope user|project|local]{RST}")
        return
    name = rest[0]
    ctx.bridge.disconnect_server(name)
    if ctx.config.remove_server(name, scope):
        _ok(f"Removed MCP server '{name}' and unregistered its tools.")
    else:
        _err(f"MCP server '{name}' not found in config.")


def _cmd_enable(ctx: _Ctx, args: list[str], enable: bool) -> None:
    if not args:
        _err(f"usage: /mcp {'enable' if enable else 'disable'} <name>")
        return
    name = args[0]
    if ctx.config.set_disabled(name, not enable) is None:
        _err(f"MCP server '{name}' is not configured.")
        return
    if enable:
        _ok(f"Enabled '{name}'.")
        ctx.connect(name)
    else:
        ctx.bridge.disconnect_server(name)
        _ok(f"Disabled '{name}' (tools unregistered).")


def _cmd_restart(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp restart <name>")
        return
    ctx.connect(args[0])


def _cmd_connect(ctx: _Ctx, _args: list[str]) -> None:
    specs = ctx.config.load_specs()
    if not specs:
        _out(f"  {SLATE}No MCP servers configured.{RST}")
        return
    _out(f"  {DIM}Connecting {len(specs)} server(s)...{RST}")
    ctx.bridge.connect_all(specs, timeout=15.0)  # slower servers finish in the background
    _cmd_list(ctx, [])


def _cmd_disconnect(ctx: _Ctx, args: list[str]) -> None:
    if args:
        if ctx.bridge.disconnect_server(args[0]):
            _ok(f"Disconnected '{args[0]}' and unregistered its tools.")
        else:
            _err(f"'{args[0]}' is not connected.")
        return
    ctx.bridge.disconnect_all()
    _ok("All MCP servers disconnected; their tools were unregistered.")


def _cmd_tools(ctx: _Ctx, args: list[str]) -> None:
    server = args[0] if args else None
    tools = [t for t in ctx.bridge.active_tools
             if server is None or getattr(t, "server_name", None) == server]
    if not tools:
        _out(f"  {SLATE}No MCP tools{f' from {server!r}' if server else ''} are registered.{RST}")
        return
    _out(f"\n  {GOLD}{BOLD}MCP tools ({len(tools)}){RST}")
    for t in sorted(tools, key=lambda x: x.name):
        flag = f" {MINT}[read-only]{RST}" if t.readonly else ""
        desc = t.description.split("] ", 1)[-1].splitlines()[0][:80] if t.description else ""
        _out(f"  {TEAL}{t.name}{RST}{flag} {SLATE}{desc}{RST}")
    _out()


def _cmd_resources(ctx: _Ctx, args: list[str]) -> None:
    try:
        text = ctx.bridge.describe_resources(args[0] if args else None)
    except Exception as e:
        _err(str(e))
        return
    _out(f"\n  {GOLD}{BOLD}MCP resources{RST}")
    for line in text.splitlines():
        _out(f"  {line}")
    _out()


def _cmd_read(ctx: _Ctx, args: list[str]) -> None:
    if len(args) < 2:
        _err("usage: /mcp read <server> <uri>")
        return
    try:
        _out(ctx.bridge.get_connection(args[0]).read_resource(args[1]))
    except Exception as e:
        _err(str(e))


def _cmd_prompts(ctx: _Ctx, args: list[str]) -> None:
    prompts = [p for p in ctx.bridge.list_prompts() if not args or p[0] == args[0]]
    if not prompts:
        _out(f"  {SLATE}No MCP prompts available.{RST}")
        return
    _out(f"\n  {GOLD}{BOLD}MCP prompts ({len(prompts)}){RST}")
    for server, name, desc, arguments in prompts:
        arg_str = " ".join(f"{a.get('name')}{'' if a.get('required') else '?'}=" for a in arguments)
        _out(f"  {TEAL}{server}{RST}/{WHITE}{name}{RST} {DIM}{arg_str}{RST} {SLATE}{desc[:70]}{RST}")
    _out(f"\n  {DIM}/mcp get-prompt <server> <prompt> key=value ...{RST}\n")


def _cmd_get_prompt(ctx: _Ctx, args: list[str]) -> None:
    if len(args) < 2:
        _err("usage: /mcp get-prompt <server> <prompt> [key=value ...]")
        return
    kv: dict[str, str] = {}
    for a in args[2:]:
        k, sep, v = a.partition("=")
        if not sep:
            _err(f"argument '{a}' must be key=value")
            return
        kv[k] = v
    try:
        _out(ctx.bridge.get_prompt(args[0], args[1], kv))
    except Exception as e:
        _err(str(e))


def _cmd_logs(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp logs <name> [lines]")
        return
    path = log_path_for(args[0])
    n = max(1, int(args[1])) if len(args) > 1 and args[1].isdigit() else 40
    if not path.is_file():
        _out(f"  {SLATE}No log yet at {path}{RST}")
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError as e:
        _err(str(e))
        return
    _out(f"\n  {GOLD}{path}{RST}")
    for line in lines:
        _out(f"  {line}")
    _out()


def _cmd_import(ctx: _Ctx, _args: list[str]) -> None:
    found = ctx.config.find_all_external_mcp_configs()
    if not found:
        _out(f"  {SLATE}No external MCP configs found (Claude Desktop/Code, Cursor, Windsurf, VS Code).{RST}")
        return
    count = ctx.config.import_from_all_discovered()
    _ok(f"Imported {count} new server(s) from {len(found)} config(s):")
    for label, path in found:
        _out(f"    {TEAL}{label}{RST}: {WHITE}{path}{RST}")
    if count:
        _cmd_connect(ctx, [])


def _cmd_presets(ctx: _Ctx, _args: list[str]) -> None:
    recommended = set(ctx.config.detect_workspace_recommended_presets())
    _out(f"\n  {GOLD}{BOLD}MCP preset catalog{RST}")
    for p in MCP_CATALOG:
        star = f" {GOLD}(recommended){RST}" if p.id in recommended else ""
        _out(f"  {TEAL}{p.id:<14}{RST}{WHITE}{p.name}{RST}{star} {SLATE}- {p.description}{RST}")
    _out(f"\n  {DIM}/mcp install <id>{RST}\n")


def _cmd_install(ctx: _Ctx, args: list[str]) -> None:
    yes = any(a in ("-y", "--yes") for a in args)
    args = [a for a in args if a not in ("-y", "--yes")]
    if args:
        preset = get_mcp_preset(args[0])
        if preset is None:
            _err(f"unknown preset '{args[0]}'. See /mcp presets.")
            return
        _install_preset(ctx, preset, assume_yes=yes)
        return
    if not _interactive():
        _cmd_presets(ctx, [])
        return
    cancel = "Cancel"
    options = [cancel] + [f"{p.name} ({p.category}) - {p.description}" for p in MCP_CATALOG]
    choice = _pick(options, "Select MCP server to install")
    if choice and choice != cancel:
        _install_preset(ctx, MCP_CATALOG[options.index(choice) - 1])


def _install_preset(ctx: _Ctx, preset: MCPServerPreset, assume_yes: bool = False) -> None:
    """Save a catalog preset and connect it. Connecting launches ``preset.command`` (often npx,
    which downloads and runs a package), so an interactive session must confirm explicitly
    (default No); a non-interactive ``/mcp install <id>`` is already an explicit request."""
    target = " ".join([preset.command, *preset.args])
    _out(f"\n  {GOLD}Installing {preset.name}{RST} {DIM}({target}){RST}")
    if _interactive() and not assume_yes:
        verb = "download and run" if os.path.basename(preset.command).lower().startswith("npx") else "run"
        if not _confirm(f"This will {verb} `{target}` on your machine. Continue?"):
            _out(f"  {SLATE}Installation cancelled.{RST}")
            return
    custom: dict[str, str] = {}
    for key in preset.env_vars:
        if os.environ.get(key):
            _out(f"  {DIM}{key}: using ${{{key}}} from your environment{RST}")
            continue
        if not _interactive():
            _out(f"  {GOLD}{key} is not set; stored as ${{{key}}} (export it, then /mcp restart {preset.id}){RST}")
            continue
        try:
            val = input(f"  {BOLD}{WHITE}{key} (Enter to read ${{{key}}} from the environment): {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            _out(f"\n  {SLATE}Installation cancelled.{RST}")
            return
        if val:
            custom[key] = val
    ctx.config.add_preset(preset.id, custom_env=custom, scope="user")
    _ok(f"Saved {preset.name} to {ctx.config.global_config_file}")
    ctx.connect(preset.id)


def _pick(options: list[str], title: str) -> str | None:
    try:
        from axon.ui.picker import pick
        return pick(options, title=title)
    except (ImportError, AttributeError):
        return None
    except (KeyboardInterrupt, EOFError):
        return None


def _add_wizard(ctx: _Ctx) -> None:
    try:
        name = input(f"  {BOLD}{WHITE}Server name: {RST}").strip()
        if not name:
            return
        target = input(f"  {BOLD}{WHITE}Command (e.g. npx -y pkg) or URL: {RST}").strip()
        if not target:
            return
        tokens = _split(target)
        if tokens[0].startswith(("http://", "https://")):
            kind = input(f"  {BOLD}{WHITE}Transport [http/sse] (http): {RST}").strip().lower() or "http"
            auth = input(f"  {BOLD}{WHITE}Authorization header value (Enter to skip): {RST}").strip()
            raw: dict[str, Any] = {"type": kind, "url": tokens[0]}
            if auth:
                raw["headers"] = {"Authorization": auth}
        else:
            env_raw = input(f"  {BOLD}{WHITE}Env vars KEY=VAL (space separated, Enter to skip): {RST}").strip()
            env = dict(p.split("=", 1) for p in _split(env_raw) if "=" in p)
            raw = {"command": tokens[0], "args": tokens[1:], "env": env}
        scope = input(f"  {BOLD}{WHITE}Scope [user/project/local] (user): {RST}").strip() or "user"
        scope = normalize_scope(scope)
        if not _confirm(f"Save '{name}' and connect now (runs `{target}`)?"):
            _out(f"  {SLATE}Cancelled.{RST}")
            return
    except (KeyboardInterrupt, EOFError):
        _out(f"\n  {SLATE}Cancelled.{RST}")
        return
    except ValueError as e:
        _err(str(e))
        return
    _save_and_connect(ctx, name, raw, scope)


def _menu(ctx: _Ctx) -> None:
    _cmd_list(ctx, [])
    # The first (default-highlighted) entry is harmless, so a stray Enter never installs or runs anything.
    actions = [
        ("Close", lambda: None),
        ("List servers", lambda: _cmd_list(ctx, [])),
        ("Show tools of a server", lambda: _pick_and(ctx, _cmd_tools)),
        ("Show logs for a server", lambda: _pick_and(ctx, _cmd_logs)),
        ("Reconnect a server", lambda: _pick_and(ctx, _cmd_restart)),
        ("Install a preset server (GitHub, SQLite, Postgres, ...)", lambda: _cmd_install(ctx, [])),
        ("Add a custom server (stdio command or HTTP URL)", lambda: _add_wizard(ctx)),
        ("Import servers from Claude/Cursor/Windsurf/VS Code", lambda: _cmd_import(ctx, [])),
        ("Remove a server", lambda: _cmd_remove(ctx, [])),
    ]
    choice = _pick([a[0] for a in actions], "MCP")
    for label, fn in actions:
        if label == choice:
            fn()
            return


def _pick_and(ctx: _Ctx, fn: Any) -> None:
    names = sorted(ctx.config.load_specs())
    choice = _pick(names, "Select MCP server") if names else None
    if choice:
        fn(ctx, [choice])


def _cmd_help(_ctx: _Ctx, _args: list[str]) -> None:
    _out(f"\n  {GOLD}{BOLD}/mcp{RST}")
    for line in (__doc__ or "").strip().splitlines()[2:]:
        _out(f"  {line.strip()}")
    _out()


_COMMANDS = {
    "list": _cmd_list, "ls": _cmd_list, "status": _cmd_list,
    "add": _cmd_add, "remove": _cmd_remove, "rm": _cmd_remove, "delete": _cmd_remove,
    "enable": lambda c, a: _cmd_enable(c, a, True), "disable": lambda c, a: _cmd_enable(c, a, False),
    "restart": _cmd_restart, "reconnect": _cmd_restart,
    "connect": _cmd_connect, "start": _cmd_connect,
    "disconnect": _cmd_disconnect, "stop": _cmd_disconnect,
    "tools": _cmd_tools, "bridge": _cmd_tools, "live": _cmd_tools,
    "resources": _cmd_resources, "read": _cmd_read, "read-resource": _cmd_read,
    "prompts": _cmd_prompts, "get-prompt": _cmd_get_prompt, "prompt": _cmd_get_prompt,
    "logs": _cmd_logs, "log": _cmd_logs,
    "import": _cmd_import, "presets": _cmd_presets, "catalog": _cmd_presets,
    "install": _cmd_install, "help": _cmd_help, "-h": _cmd_help, "--help": _cmd_help,
}


def handle_mcp_command(agent: "Agent", arg: "str | list[str]" = "") -> None:
    """Entry point for every `/mcp ...` subcommand.

    ``arg`` is the raw slash-command text (split with :func:`_split`) or, for ``axon mcp ...``, the
    already-split argv list, which is used as-is so arguments containing spaces stay intact."""
    ctx = _Ctx(agent)
    if isinstance(arg, (list, tuple)):
        argv = [str(a) for a in arg]
        if not argv:
            _cmd_list(ctx, [])
            return
        sub, tokens = argv[0].lower(), argv[1:]
        if sub == "add-json":
            _cmd_add_json_tokens(ctx, tokens)
            return
    else:
        text = (arg or "").strip()
        if not text:
            if _interactive():
                _menu(ctx)
            else:
                _cmd_list(ctx, [])
            return
        sub, _, rest = text.partition(" ")
        sub = sub.lower()
        if sub == "add-json":
            _cmd_add_json(ctx, rest.strip())
            return
        tokens = _split(rest)
    handler = _COMMANDS.get(sub)
    if handler is None:
        _err(f"unknown subcommand '{sub}'")
        _cmd_help(ctx, [])
        return
    try:
        handler(ctx, tokens)
    except KeyboardInterrupt:
        _out(f"\n  {SLATE}Cancelled.{RST}")


__all__ = ["handle_mcp_command"]
