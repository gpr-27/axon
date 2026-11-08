"""
`/mcp` slash command (and `axon mcp ...`): the one user-facing entry point for MCP management.

    /mcp                         status of every configured server (menu in a TTY)
    /mcp list|status
    /mcp get <name>              scope, status, endpoint, auth and warnings of one server
    /mcp add <name> [opts] <command> [args...]      stdio server
    /mcp add --transport http|sse <name> <url> [--header "K: V"]...
          options: --scope user|project|local, --env K=V [K=V...], --timeout SECONDS,
                   --client-id ID [--client-secret] [--callback-port N]   (OAuth app)
    /mcp add <preset>  |  /mcp install [preset]     catalog presets
    /mcp add-json <name> '<json spec>' [--scope ...] [--client-secret]
    /mcp remove <name> [--scope ...]
    /mcp enable|disable <name>
    /mcp restart|reconnect <name|all>   |   /mcp connect   |   /mcp disconnect [name]
    /mcp login <name> [--no-browser]  |  /mcp logout <name>        OAuth sign-in / sign-out
    /mcp approve [name|all]  |  /mcp reject <name>  |  /mcp reset-project-choices   .mcp.json approvals
    /mcp tools [server]  |  /mcp resources [server]  |  /mcp read <server> <uri>
    /mcp prompts [server] | /mcp get-prompt <server> <prompt> [key=value ...]
    /mcp logs <name> [lines]  |  /mcp import  |  /mcp add-from-claude-desktop  |  /mcp presets
    /mcp test <name> [--timeout S]       connect once and list the tools (health check)
    /mcp scope <name> user|project|local   move a server to another scope
    /mcp env <name> NAME=VALUE ... [--unset NAME]   keys and variables (keys go to ~/.axon/.env)
    /mcp timeout <name> <seconds>        time limit for the server
    /mcp tool <name> [<tool>|*] allow|ask|deny|reset   permission per tool
    /mcp dashboard [--check]             every connector, skill and plugin on one screen
    /mcp export [file] | /mcp import <file>   the whole setup as one JSON file
    /mcp auto on|off|status | /mcp suggest | /mcp updates   automatic behaviour (off by default)
    axon mcp serve                 run Axon's tools as an MCP server over stdio
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import TYPE_CHECKING, Any

from axon.mcp import approval, state
from axon.mcp.bridge import MCPToolBridge, get_bridge
from axon.mcp.catalog import MCP_CATALOG, MCPServerPreset, get_mcp_preset
from axon.mcp.config import MCPConfig, ServerSpec, normalize_scope, valid_server_name
from axon.mcp.oauth import OAuthError, OAuthManager, forget, store_client_secret
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
    color = {"connected": MINT, "connecting": GOLD, "reconnecting": GOLD, "disabled": SLATE,
             "needs-config": GOLD, "needs-auth": GOLD, "pending-approval": GOLD}.get(status, ROSE)
    return f"{color}{dot}{RST}"


def status_label(status: str, error: str | None = None) -> str:
    """Claude-Code style one-liner for a server status (plain text)."""
    ok, bang, bad, pause, off = (("✔", "!", "✘", "⏸", "⊘") if _unicode_ok() else ("+", "!", "x", "||", "-"))
    table = {
        "connected": f"{ok} Connected",
        "connecting": "… Connecting",
        "reconnecting": "… Reconnecting",
        "needs-auth": f"{bang} Needs authentication",
        "needs-config": f"{bang} Needs configuration",
        "pending-approval": f"{pause} Pending approval (run /mcp approve)",
        "rejected": f"{bad} Rejected (see disabledMcpjsonServers or /mcp reset-project-choices)",
        "disabled": f"{off} Disabled for this project (re-enable with /mcp enable <name>)",
        "blocked": f"{bad} Blocked by enterprise managed policy",
        "disconnected": f"{bad} Disconnected",
        "stopped": f"{off} Stopped",
        "pending": "… Not connected yet",
    }
    return table.get(status, f"{bad} Failed to connect")


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
    def __init__(self, agent: "Agent", cli: bool = False) -> None:
        self.agent = agent
        self.cli = cli            # `axon mcp ...` from a shell rather than /mcp inside a session
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

def _health_check(ctx: _Ctx, specs: dict[str, ServerSpec], timeout: float = 15.0) -> None:
    """`axon mcp list` outside a session: connect every enabled server so the list shows real health."""
    todo = {n: s for n, s in specs.items() if not s.disabled}
    if todo:
        ctx.bridge.connect_all(todo, timeout=timeout)
        ctx.bridge.wait_until_settled(0.1)


def _cmd_list(ctx: _Ctx, _args: list[str]) -> None:
    specs = ctx.config.load_specs()
    if ctx.cli and specs:
        _health_check(ctx, specs)
    rows = {r["name"]: r for r in ctx.bridge.status_rows()}
    _out(f"\n  {GOLD}{BOLD}MCP servers ({len(specs)}){RST}")
    for note in ctx.config.notices:
        _out(f"  {GOLD}{note}{RST}")
    for name, why in ctx.config.blocked:
        _out(f"  {SLATE}'{name}' is not loaded: "
             f"{'blocked' if why == 'denied' else 'not allowed'} by enterprise policy{RST}")
    for name, src, msg in ctx.config.problems:
        _out(f"  {GOLD}Skipped '{name}' ({src}): {msg}{RST}")
    if not specs:
        _out(f"  {SLATE}No MCP servers configured.{RST}")
        _out(f"  {DIM}Add one: /mcp add <name> <command> [args...]   "
             f"or /mcp add --transport http <name> <url>   or /mcp install{RST}\n")
        return
    for name, spec in sorted(specs.items()):
        row = rows.get(name)
        if spec.disabled:
            status = "disabled"
        elif spec.approval in ("pending", "rejected"):
            status = "pending-approval" if spec.approval == "pending" else "rejected"
        else:
            status = row["status"] if row else "not connected"
        transport = row["transport"] if row else spec.type
        tools = row["tools"] if row else 0
        origin = "managed" if spec.managed else spec.scope
        extra = f" · {tools} tools" if status == "connected" else ""
        if status == "connected" and row and row.get("advertises_tools") and tools == 0:
            extra = " · advertises tools but exposes none"
        _out(f"  {_dot(status)} {TEAL}{BOLD}{name}{RST} {SLATE}[{transport} · {origin}]{RST} "
             f"{WHITE}{status_label(status) if status != 'not connected' else status}{RST}{extra}")
        _out(f"      {DIM}{('host only: ' + _host_only(spec.url)) if spec.managed and spec.url else spec.describe_target()[:100]}{RST}")
        if row and row.get("error") and status != "connected":
            _out(f"      {ROSE}Issue: {str(row['error'])[:200]}{RST}")
        for w in (row["warnings"] if row else spec.config_warnings()):
            _out(f"      {GOLD}Warning: {w}{RST}")
    for name, defs in ctx.config.conflicts.items():
        listing = "; ".join(f"{sc}: {target}" for sc, target in defs)
        _out(f"  {GOLD}'{name}' is defined in more than one scope with different endpoints ({listing}). "
             f"The highest-precedence one loads; remove the others with /mcp remove {name} --scope <scope>.{RST}")
    if not _interactive():  # in a terminal /mcp opens the menu instead of asking you to type these
        _out(f"\n  {DIM}/mcp get <name> · /mcp tools · /mcp restart <name> · /mcp logs <name> · /mcp help{RST}\n")
    else:
        _out()


def _host_only(url: str) -> str:
    from urllib.parse import urlsplit
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}/…" if u.scheme else url


_SECRET_NAME = re.compile(r"auth|token|key|secret|password|passwd|cookie|credential", re.I)


def _show_value(name: str, value: Any) -> str:
    """A header or env value for display: values of secret-looking names keep only their first 4 characters."""
    text = str(value)
    if not _SECRET_NAME.search(name) or "${" in text:
        return text
    return text[:4] + "…" if len(text) > 8 else "…"


def _cmd_get(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp get <name>")
        return
    name = args[0]
    spec = ctx.config.get_spec(name)
    if spec is None:
        _err(f"No MCP server found with name: {name}")
        return
    if ctx.cli and not spec.disabled and spec.approval == "approved":
        ctx.bridge.connect_server(spec, timeout=15.0)
    conn = ctx.bridge.connections.get(name)
    status = "disabled" if spec.disabled else (
        "pending-approval" if spec.approval == "pending" else
        "rejected" if spec.approval == "rejected" else (conn.status if conn else "pending"))
    _out(f"\n  {TEAL}{BOLD}{name}{RST}:")
    where = "managed by your organization" if spec.managed else (
        f"{spec.scope} ({spec.source})" if spec.source else spec.scope)
    _out(f"    Scope: {where}")
    _out(f"    Status: {status_label(status)}")
    if conn and conn.error and status not in ("connected", "disabled"):
        _out(f"    Issue: {conn.error[:300]}")
    _out(f"    Type: {spec.type}")
    if spec.type == "stdio":
        redact = spec.scope == "plugin"
        _out(f"    Command: {'stdio' if redact else spec.command}")
        _out(f"    Args: {'' if redact else ' '.join(spec.args)}")
        if spec.env:
            _out("    Environment:")
            for k, v in spec.env.items():
                _out(f"      {k}={'[REDACTED]' if redact else _show_value(k, v)}")
    else:
        _out(f"    URL: {_host_only(spec.url) if spec.managed else spec.url}")
        if spec.headers:
            _out("    Headers:")
            for k, v in spec.headers.items():
                _out(f"      {k}{'' if spec.managed else ': ' + _show_value(k, v)}")
        if spec.headers_helper:
            _out(f"    headersHelper: {spec.headers_helper}")
        if spec.oauth or spec.type in ("http", "sse"):
            try:
                auth = OAuthManager(spec.resolved(ctx.workspace)).status()
            except Exception:
                auth = "not authenticated"
            configured = []
            if spec.oauth.get("clientId"):
                configured.append("client ID configured")
            if spec.oauth.get("callbackPort"):
                configured.append(f"callback port {spec.oauth['callbackPort']}")
            if spec.oauth.get("scopes"):
                configured.append(f"scopes: {spec.oauth['scopes']}")
            if spec.oauth.get("authServerMetadataUrl"):
                configured.append("custom metadata URL")
            _out(f"    OAuth: {auth}" + (f" ({', '.join(configured)})" if configured else ""))
    if spec.timeout_explicit:
        _out(f"    Timeout: {spec.timeout:g}s per tool call")
    if spec.always_load is not None:
        _out(f"    alwaysLoad: {str(spec.always_load).lower()}")
    if conn and conn.connected:
        info = conn.client.server_info if conn.client else {}
        label = f"{info.get('name', '')} {info.get('version', '')}".strip()
        n = len([t for t in ctx.bridge.active_tools if getattr(t, "server_name", None) == name])
        _out(f"    Tools: {n}" + (f"  ({label})" if label else ""))
        for tool, why in ctx.bridge.excluded.get(name, []):
            _out(f"    {GOLD}Tool '{tool}' left out: {why}{RST}")
    for w in spec.config_warnings():
        _out(f"    {GOLD}Warning: {w}{RST}")
    for sc, target in ctx.config.conflicts.get(name, []):
        _out(f"    {GOLD}Also defined in {sc}: {target}{RST}")
    _out(f"    Logs: {log_path_for(name)}\n")
    if ctx.cli:
        ctx.bridge.disconnect_all()


def _parse_add(tokens: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"transport": None, "scope": "user", "headers": {}, "env": {},
                            "timeout": None, "positional": [], "command_args": [],
                            "client_id": None, "client_secret": False, "callback_port": None}
    i = 0

    def _val(flag: str) -> str:
        nonlocal i
        if "=" in flag and flag.startswith("--"):
            return flag.split("=", 1)[1]
        i += 1
        if i >= len(tokens):
            raise ValueError(f"{flag} needs a value")
        return tokens[i]

    def _env_pair(raw: str) -> None:
        k, sep, v = raw.partition("=")
        if not sep:
            raise ValueError("env must look like KEY=value")
        opts["env"][k.strip()] = v

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
            _env_pair(_val(tok))
            # --env takes several KEY=value pairs; the server name must not directly follow it
            while i + 1 < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i + 1]):
                i += 1
                _env_pair(tokens[i])
        elif key == "--timeout":
            opts["timeout"] = float(_val(tok))
        elif key == "--client-id":
            opts["client_id"] = _val(tok)
        elif key == "--client-secret":
            opts["client_secret"] = True
        elif key == "--callback-port":
            opts["callback_port"] = int(_val(tok))
        else:
            opts["positional"].append(tok)
            if len(opts["positional"]) == 2:
                is_url = tok.startswith(("http://", "https://", "ws://", "wss://"))
                if opts["transport"] in (None, "stdio") and not is_url:
                    opts["transport"] = "stdio"
                    opts["command_args"] = tokens[i + 1:]
                    break
                opts["transport"] = opts["transport"] or ("ws" if tok.startswith(("ws://", "wss://")) else "http")
        i += 1
    return opts


def _client_secret(flag: bool) -> str | None:
    """--client-secret: MCP_CLIENT_SECRET when set, else a masked prompt (interactive only)."""
    if not flag:
        return None
    env = os.environ.get("MCP_CLIENT_SECRET", "")
    if env:
        return env
    if not _interactive():
        _err("--client-secret needs the MCP_CLIENT_SECRET environment variable when there is no terminal")
        return None
    import getpass
    try:
        return getpass.getpass("  Enter OAuth client secret: ") or None
    except (EOFError, KeyboardInterrupt):
        return None


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
    if transport not in ("stdio", "http", "sse", "ws"):
        _err(f"unknown transport '{o['transport']}' (use stdio, http or sse)")
        return
    raw: dict[str, Any]
    if transport == "stdio":
        raw = {"command": pos[1], "args": o["command_args"], "env": o["env"]}
    else:
        raw = {"type": transport, "url": pos[1], "headers": o["headers"]}
        oauth: dict[str, Any] = {}
        if o["client_id"]:
            oauth["clientId"] = o["client_id"]
        if o["callback_port"]:
            oauth["callbackPort"] = o["callback_port"]
        if oauth:
            raw["oauth"] = oauth
    if o["timeout"]:
        raw["timeout"] = o["timeout"]
    if o["client_secret"] and not o["client_id"]:
        _err("--client-secret needs --client-id")
        return
    secret = _client_secret(o["client_secret"])
    _save_and_connect(ctx, pos[0], raw, o["scope"], client_secret=secret)


def _save_and_connect(ctx: _Ctx, name: str, raw: dict[str, Any], scope: str, *,
                      client_secret: str | None = None) -> None:
    if not valid_server_name(name):
        _err(f"Invalid name {name}. Names can only contain letters, numbers, hyphens, and underscores.")
        return
    try:
        spec = ServerSpec.from_dict(name, raw, scope)
    except ValueError as e:
        _err(f"could not save '{name}': {e}")
        return
    blocked = ctx.config.add_block_reason(spec)
    if blocked:
        _err(blocked)
        return
    if any(sc == scope for sc, _p, _r in ctx.config.definitions(name)):
        _err(f"MCP server {name} already exists in {scope} config (remove it first: /mcp remove {name} --scope {scope})")
        return
    try:
        path = ctx.config.add_raw(name, raw, scope)
    except (ValueError, OSError) as e:
        _err(f"could not save '{name}': {e}")
        return
    if scope == "project":
        state.set_project_choice(ctx.workspace, name, "approved")  # you just wrote it yourself
    if client_secret and spec.oauth.get("clientId"):
        store_client_secret(spec, client_secret)
    kind = {"http": "HTTP", "sse": "SSE", "ws": "WebSocket"}.get(spec.type, "stdio")
    _ok(f"Saved MCP server '{name}' to {path}")
    _out(f"  {DIM}Added {kind} MCP server {name} with {'URL' if spec.remote else 'command'}: "
         f"{spec.describe_target()} to {scope} config{RST}")
    for w in spec.config_warnings():
        _out(f"  {GOLD}Warning: {w}{RST}")
    winner = ctx.config.get_spec(name)
    if winner is not None and winner.source != path:
        _out(f"  {GOLD}Note: '{name}' is also defined in {winner.source}, which takes precedence.{RST}")
    ctx.connect(name)


def _cmd_add_json(ctx: _Ctx, rest: str) -> None:
    tokens = rest.split(None, 1)
    if len(tokens) < 2:
        _err("usage: /mcp add-json <name> '<json>' [--scope user|project|local] [--client-secret]")
        return
    name, payload = tokens[0], tokens[1].strip()
    scope = "user"
    secret_flag = False
    if payload.endswith(" --client-secret"):
        secret_flag, payload = True, payload[:-len(" --client-secret")].strip()
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
    if payload.endswith(" --client-secret"):
        secret_flag, payload = True, payload[:-len(" --client-secret")].strip()
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
    _save_and_connect(ctx, name, raw, scope, client_secret=_client_secret(secret_flag))


def _cmd_add_json_tokens(ctx: _Ctx, tokens: list[str]) -> None:
    """argv form: add-json <name> <json> [--scope S] [--client-secret]."""
    scope = "user"
    secret_flag = "--client-secret" in tokens
    tokens = [t for t in tokens if t != "--client-secret"]
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
        _err("usage: axon mcp add-json <name> '<json>' [--scope user|project|local] [--client-secret]")
        return
    try:
        raw = json.loads(" ".join(tokens[1:]))
    except ValueError as e:
        _err(f"invalid JSON: {e}")
        return
    if not isinstance(raw, dict):
        _err("the JSON spec must be an object")
        return
    _save_and_connect(ctx, tokens[0], raw, scope, client_secret=_client_secret(secret_flag))


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
    spec = ctx.config.get_spec(name)
    if spec is not None and spec.managed:
        _err(f'MCP server "{name}" is provided by your organization (managed settings) and cannot be removed locally.')
        return
    if scope is None:
        scopes = sorted({sc for sc, _p, _r in ctx.config.definitions(name) if sc != "plugin"})
        if len(scopes) > 1:
            _err(f"MCP server {name} exists in multiple scopes: {', '.join(scopes)}. "
                 f"Pass --scope to choose which copy to delete.")
            return
    ctx.bridge.disconnect_server(name)
    if ctx.config.remove_server(name, scope):
        if spec is not None and spec.remote:
            remaining = ctx.config.get_spec(name)
            if remaining is None or remaining.endpoint_key() != spec.endpoint_key():
                forget(spec.resolved(ctx.workspace))  # stored OAuth tokens and client registration
        _ok(f"Removed MCP server '{name}' and unregistered its tools.")
    else:
        _err(f"MCP server '{name}' not found in config.")


def _cmd_enable(ctx: _Ctx, args: list[str], enable: bool) -> None:
    if not args:
        _err(f"usage: /mcp {'enable' if enable else 'disable'} <name>")
        return
    name = args[0]
    if ctx.config.toggle(name, not enable) is None:
        _err(f"MCP server '{name}' is not configured (or is managed by your organization).")
        return
    if enable:
        _ok(f"Enabled '{name}'.")
        ctx.connect(name)
    else:
        ctx.bridge.disconnect_server(name)
        _ok(f"Disabled '{name}' (tools unregistered).")


def _cmd_restart(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp restart <name|all>")
        return
    if args[0] == "all":
        specs = ctx.config.load_specs()
        done = ctx.bridge.reconnect_all(specs)
        _ok(f"Reconnected {len(done)} server(s)" + (f": {', '.join(done)}" if done else " (everything was already connected)"))
        return
    ctx.connect(args[0])


def _no_browser_available() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _oauth_spec(ctx: _Ctx, name: str) -> ServerSpec | None:
    spec = ctx.config.get_spec(name)
    if spec is None:
        _err(f"MCP server '{name}' is not configured.")
        return None
    if spec.type not in ("http", "sse"):
        _err(f"'{name}' is a {spec.type} server; OAuth works with HTTP and SSE servers only.")
        return None
    if spec.has_authorization_header:
        _err(f"'{name}' has an Authorization header configured, so OAuth is not used. "
             f"Fix the header or remove it to sign in with OAuth.")
        return None
    return spec


def _cmd_login(ctx: _Ctx, args: list[str]) -> None:
    no_browser = "--no-browser" in args
    names = [a for a in args if not a.startswith("--")]
    if not names:
        _err("usage: /mcp login <name> [--no-browser]")
        return
    spec = _oauth_spec(ctx, names[0])
    if spec is None:
        return
    mgr = OAuthManager(spec.resolved(ctx.workspace))
    conn = ctx.bridge.connections.get(spec.name)
    if conn is not None:
        mgr.challenge = dict(conn.auth_challenge)
    manual = no_browser or _no_browser_available()
    paste = None
    if manual or _interactive():
        paste = lambda: input("  Paste the full redirect URL from your browser's address bar here "  # noqa: E731
                              "(or wait for the browser): ")
    _out(f"  {DIM}Signing in to '{spec.name}'...{RST}")
    try:
        msg = mgr.login(open_browser=not manual, ask_paste=paste if _interactive() else None, notify=_out)
    except OAuthError as e:
        _err(str(e))
        return
    _ok(msg)
    ctx.connect(spec.name)


def _cmd_logout(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp logout <name>")
        return
    spec = _oauth_spec(ctx, args[0])
    if spec is None:
        return
    removed = OAuthManager(spec.resolved(ctx.workspace)).logout()
    ctx.bridge.disconnect_server(spec.name)
    _ok(f"Cleared stored authentication for '{spec.name}'." if removed
        else f"'{spec.name}' had no stored authentication.")


def _cmd_approve(ctx: _Ctx, args: list[str]) -> None:
    specs = ctx.config.load_specs()
    project = {n: s for n, s in specs.items() if s.scope == "project"}
    if not project:
        _out(f"  {SLATE}This project has no .mcp.json servers.{RST}")
        return
    if args and args[0] == "all":
        state.approve_all(ctx.workspace, True)
        _ok(f"Approved all project servers ({', '.join(sorted(project))}).")
        names = sorted(project)
    elif args:
        missing = [a for a in args if a not in project]
        if missing:
            _err(f"not a project (.mcp.json) server: {', '.join(missing)}")
            return
        for a in args:
            state.set_project_choice(ctx.workspace, a, "approved")
        _ok(f"Approved {', '.join(args)}.")
        names = list(args)
    else:
        specs = ctx.config.load_specs()
        if not approval.pending(specs):
            _out(f"  {SLATE}Nothing is waiting for approval.{RST}")
            return
        approval.review(ctx.workspace, specs, out=_out)
        names = [n for n, s in specs.items() if s.approval == "approved" and s.scope == "project"]
    for n in names:
        sp = ctx.config.get_spec(n)
        if sp is not None and not sp.disabled and sp.approval == "approved":
            ctx.connect(n)


def _cmd_reject(ctx: _Ctx, args: list[str]) -> None:
    if not args:
        _err("usage: /mcp reject <name>")
        return
    for a in args:
        state.set_project_choice(ctx.workspace, a, "rejected")
        ctx.bridge.disconnect_server(a)
    _ok(f"Rejected {', '.join(args)} (undo with /mcp reset-project-choices).")


def _cmd_reset_choices(ctx: _Ctx, _args: list[str]) -> None:
    n = state.reset_project_choices(ctx.workspace)
    _ok(f"Reset {n} project server approval choice(s); you will be asked again.")


def _cmd_serve(ctx: _Ctx, _args: list[str]) -> None:
    from axon.mcp.serve import serve_stdio
    serve_stdio(ctx.workspace)


def _cmd_add_from_desktop(ctx: _Ctx, args: list[str]) -> None:
    path = ctx.config.find_claude_desktop_config()
    if path is None:
        _err("Claude Desktop's configuration was not found (macOS, Windows and WSL only).")
        return
    scope = "user"
    if "--scope" in args and args.index("--scope") + 1 < len(args):
        try:
            scope = normalize_scope(args[args.index("--scope") + 1])
        except ValueError as e:
            _err(str(e))
            return
    from axon.mcp.config import _read_json, _servers_in
    existing = set(ctx.config.load_specs())
    added = 0
    for name, raw in _servers_in(_read_json(path)).items():
        if not isinstance(raw, dict):
            continue
        if not valid_server_name(str(name)):
            _err(f"skipped '{name}': names can contain only letters, numbers, hyphens and underscores")
            continue
        final, n = str(name), 1
        while final in existing:
            final, n = f"{name}_{n}", n + 1
        try:
            ctx.config.add_raw(final, raw, scope)
        except (ValueError, OSError) as e:
            _err(f"skipped '{name}': {e}")
            continue
        existing.add(final)
        added += 1
        _ok(f"Imported '{name}' as '{final}'")
    _out(f"  {DIM}Imported {added} server(s) from {path} into {scope} scope.{RST}")


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
        extra = f" {GOLD}[asks every call]{RST}" if getattr(t, "requires_user_interaction", False) else ""
        _out(f"  {TEAL}{t.name}{RST}{flag}{extra} {SLATE}{desc}{RST}")
    for srv, left in sorted(ctx.bridge.excluded.items()):
        if server is None or srv == server:
            for tool, why in left:
                _out(f"  {GOLD}left out: {srv}/{tool} - {why}{RST}")
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


def _delegate(verb: str) -> Any:
    """A /mcp subcommand that runs the same-named /marketplace one (one implementation for both)."""
    def run(ctx: _Ctx, args: list[str]) -> None:
        from axon.marketplace.cli import run_command
        outcome = run_command([verb, "--tab", "connectors", *args], ctx.workspace,
                              interactive=_interactive(), agent=None if ctx.cli else ctx.agent)
        if ctx.cli:
            return
        for name in dict.fromkeys(outcome.installed + outcome.reconnect):
            ctx.connect(name)
    return run


def _cmd_import(ctx: _Ctx, args: list[str]) -> None:
    if args and not args[0].startswith("-"):
        _delegate("import")(ctx, args)
        return
    _import_from_other_tools(ctx, args)


def _import_from_other_tools(ctx: _Ctx, _args: list[str]) -> None:
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
    """Guided /mcp add: name, command or URL, transport, keys, scope, confirm. Choices are menus."""
    from axon.ui.menus import ask_text, choose_value, confirm
    name = ask_text("Server name")
    if not name:
        return
    if not valid_server_name(name):
        _err(f"Invalid name {name}. Names can only contain letters, numbers, hyphens, and underscores.")
        return
    target = ask_text("Command (e.g. npx -y pkg) or URL")
    if not target:
        return
    tokens = _split(target)
    raw: dict[str, Any]
    if tokens[0].startswith(("http://", "https://")):
        kind = choose_value("Transport", [("HTTP (streamable)", "http", "most remote servers"),
                                          ("SSE", "sse", "older remote servers")])
        if not kind:
            return
        raw = {"type": kind, "url": tokens[0]}
        auth = ask_text("Authorization header value (Enter to skip; OAuth servers sign in later)", secret=True)
        if auth is None:
            return
        if auth:
            raw["headers"] = {"Authorization": auth}
    else:
        env_raw = ask_text("Env vars KEY=VALUE, space separated (Enter to skip)")
        if env_raw is None:
            return
        env = dict(p.split("=", 1) for p in _split(env_raw) if "=" in p)
        raw = {"command": tokens[0], "args": tokens[1:], "env": env}
    scope = choose_value("Scope", [("user", "user", "every project on this machine"),
                                   ("project", "project", "this project, shared in .mcp.json"),
                                   ("local", "local", "this project, only you")], current="user")
    if not scope:
        return
    if not confirm(f"Save '{name}' and connect now (runs `{target}`)?"):
        _out(f"  {SLATE}Cancelled.{RST}")
        return
    _save_and_connect(ctx, name, raw, scope)


def _server_status(ctx: _Ctx, name: str, spec: ServerSpec) -> str:
    """The status word _cmd_list shows for one server."""
    if spec.disabled:
        return "disabled"
    if spec.approval in ("pending", "rejected"):
        return "pending-approval" if spec.approval == "pending" else "rejected"
    conn = ctx.bridge.connections.get(name)
    return conn.status if conn else "pending"


def _server_rows(ctx: _Ctx) -> list[Any]:
    from axon.ui.picker import SelectItem
    rows = []
    for name, spec in sorted(ctx.config.load_specs().items()):
        status = _server_status(ctx, name, spec)
        tools = _tools_of(ctx, name)
        hint = status_label(status) + (f" · {len(tools)} tools" if status == "connected" else "")
        origin = "managed" if spec.managed else spec.scope
        rows.append(SelectItem(label=name, value=name, badge=_dot(status),
                               hint=f"{hint} · {spec.type} · {origin}"))
    return rows


def _tools_of(ctx: _Ctx, server: str) -> list[Any]:
    return sorted((t for t in ctx.bridge.active_tools if getattr(t, "server_name", None) == server),
                  key=lambda t: t.name)


def _uses_oauth(spec: ServerSpec) -> bool:
    return spec.type in ("http", "sse") and not spec.has_authorization_header


def _server_actions(ctx: _Ctx, name: str) -> list[Any]:
    """What can be done to one server, in the order a user usually wants it."""
    from axon.ui.menus import Action
    spec = ctx.config.get_spec(name)
    if spec is None:
        return []
    actions = [
        Action("Details", lambda: _cmd_get(ctx, [name]), "status, endpoint, auth"),
        Action("Tools", lambda: _tools_menu(ctx, name), "pick one to see its description and inputs"),
        Action("Reconnect", lambda: _cmd_restart(ctx, [name])),
        Action("Logs", lambda: _cmd_logs(ctx, [name])),
        Action("Test", lambda: _delegate("test")(ctx, [name]), "connect once and list the tools"),
    ]
    if _uses_oauth(spec):
        actions.append(Action("Sign in", lambda: _cmd_login(ctx, [name]), "OAuth"))
        actions.append(Action("Sign out", lambda: _cmd_logout(ctx, [name]), "clear stored tokens"))
    if spec.approval == "pending":
        actions.append(Action("Approve", lambda: _cmd_approve(ctx, [name]), ".mcp.json server"))
    if not spec.managed:
        if spec.disabled:
            actions.append(Action("Enable", lambda: _cmd_enable(ctx, [name], True)))
        else:
            actions.append(Action("Disable", lambda: _cmd_enable(ctx, [name], False)))
        actions.append(Action("Remove", lambda: _cmd_remove(ctx, [name]), "delete from the config",
                              confirm=f"Remove MCP server '{name}'?"))
    return actions


def _menu_extra_actions(ctx: _Ctx) -> list[Any]:
    from axon.ui.menus import Action
    actions = [
        Action("Add a server", lambda: _add_wizard(ctx), "command or URL, step by step"),
        Action("Install a preset", lambda: _cmd_install(ctx, []), "GitHub, SQLite, Postgres, ..."),
        Action("Reconnect all", lambda: _cmd_restart(ctx, ["all"])),
        Action("Import from other tools", lambda: _cmd_import(ctx, []), "Claude, Cursor, Windsurf, VS Code"),
        Action("Open the marketplace", lambda: _open_marketplace(ctx), "browse connectors"),
    ]
    if approval.pending(ctx.config.load_specs()):
        actions.insert(0, Action("Approve project servers", lambda: _cmd_approve(ctx, []), ".mcp.json"))
    return actions


def _open_marketplace(ctx: _Ctx) -> None:
    from axon.marketplace.cli import run_command
    outcome = run_command(["--tab", "connectors"], ctx.workspace, interactive=True, agent=ctx.agent)
    for name in dict.fromkeys(outcome.installed + outcome.reconnect):
        ctx.connect(name)


def _tools_menu(ctx: _Ctx, server: str) -> None:
    """Pick a tool of one server and print its description and input schema; Esc goes back."""
    from axon.ui.menus import choose_value
    tools = _tools_of(ctx, server)
    if not tools:
        _out(f"  {SLATE}No tools from {server!r} are registered (is it connected?).{RST}")
        return
    options = []
    for t in tools:
        desc = t.description.split("] ", 1)[-1].splitlines()[0][:60] if t.description else ""
        options.append((t.name, t, desc))
    while True:
        tool = choose_value(f"{server} tools", options, subtitle=f"{len(tools)} tools")
        if tool is None:
            return
        _show_tool(tool)


def _show_tool(tool: Any) -> None:
    desc = getattr(tool, "full_description", "") or tool.description or ""
    _out(f"\n  {TEAL}{BOLD}{tool.name}{RST}{' ' + MINT + '[read-only]' + RST if tool.readonly else ''}")
    for line in desc.split("] ", 1)[-1].splitlines():
        _out(f"    {line}")
    schema = getattr(tool, "schema", None) or {}
    _out(f"  {GOLD}Input schema{RST}")
    for line in json.dumps(schema, indent=2).splitlines():
        _out(f"    {DIM}{line}{RST}")
    _out()


def _menu(ctx: _Ctx) -> None:
    """/mcp in a terminal: the server list itself is the menu (no text list printed first, which
    used to show the same block twice when "List servers" was picked)."""
    from axon.ui.menus import browse
    if not ctx.config.load_specs():
        from axon.ui.menus import pick_and_run
        _out(f"  {SLATE}No MCP servers configured.{RST}")
        pick_and_run("MCP", _menu_extra_actions(ctx))
        return
    browse("MCP servers", lambda: _server_rows(ctx), lambda name: _server_actions(ctx, name),
           extra_actions=lambda: _menu_extra_actions(ctx))


def _cmd_servers(ctx: _Ctx, args: list[str]) -> None:
    """Connectors at a glance: state and tool counts; tool names only inside an expanded server."""
    from axon.commands import workbench_cmds as wb
    if _interactive():
        wb.run_connectors(ctx.agent)
    else:
        wb.print_connectors(ctx.agent, 100, show_all=True, expand=set(args))


def _cmd_help(_ctx: _Ctx, _args: list[str]) -> None:
    _out(f"\n  {GOLD}{BOLD}/mcp{RST}")
    for line in (__doc__ or "").strip().splitlines()[2:]:
        _out(f"  {line.strip()}")
    _out()


_COMMANDS = {
    "servers": lambda c, a: _cmd_servers(c, a), "list": _cmd_list, "ls": _cmd_list, "status": _cmd_list, "get": _cmd_get, "show": _cmd_get,
    "login": _cmd_login, "authenticate": _cmd_login, "auth": _cmd_login,
    "logout": _cmd_logout, "clear-auth": _cmd_logout,
    "approve": _cmd_approve, "reject": _cmd_reject, "reset-project-choices": _cmd_reset_choices,
    "serve": _cmd_serve, "add-from-claude-desktop": _cmd_add_from_desktop,
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
    "test": _delegate("test"), "check": _delegate("test"), "scope": _delegate("scope"),
    "env": _delegate("env"), "timeout": _delegate("timeout"), "tool": _delegate("tool"),
    "dashboard": _delegate("status"), "export": _delegate("export"), "auto": _delegate("auto"),
    "suggest": _delegate("suggest"), "updates": _delegate("updates"),
}


def handle_mcp_command(agent: "Agent", arg: "str | list[str]" = "") -> None:
    """Entry point for every `/mcp ...` subcommand.

    ``arg`` is the raw slash-command text (split with :func:`_split`) or, for ``axon mcp ...``, the
    already-split argv list, which is used as-is so arguments containing spaces stay intact."""
    cli = isinstance(arg, (list, tuple))
    ctx = _Ctx(agent)
    ctx.cli = cli
    if cli:
        before = state._session["interactive"]
        if before is None:  # `axon mcp ...` from a terminal shows pending approvals like the REPL would
            state.set_interactive(state.terminal_attached())
        try:
            _dispatch(ctx, [str(a) for a in arg])
        finally:
            state.set_interactive(before)
            try:  # `axon mcp ...` ends here: do not leave server processes behind
                ctx.bridge.disconnect_all()
            except Exception:
                pass
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
    _run(ctx, sub, tokens)


def _dispatch(ctx: _Ctx, argv: list[str]) -> None:
    if not argv:
        _cmd_list(ctx, [])
        return
    sub, tokens = argv[0].lower(), argv[1:]
    if sub == "add-json":
        _cmd_add_json_tokens(ctx, tokens)
        return
    _run(ctx, sub, tokens)


def _run(ctx: _Ctx, sub: str, tokens: list[str]) -> None:
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
