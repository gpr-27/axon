"""
Install, remove and switch MCP connectors from the marketplace.

An install writes the server into the normal MCP config (through ``axon.mcp.config``), saves any
API keys in the owner-only ``~/.axon/.env`` file, then connects once as a health check. If that
check fails the config is rolled back, unless the caller asked to keep it.
"""
from __future__ import annotations

import os
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from axon.marketplace.catalog import Connector, EnvVar
from axon.mcp import health as mcp_health
from axon.mcp.config import MCPConfig, ServerSpec, normalize_scope, valid_server_name

DEFAULT_CHECK_TIMEOUT = 20.0
# After the connection timeout we wait a little longer before giving up on the worker thread.
THREAD_GRACE_SECONDS = 5.0


@dataclass
class HealthResult:
    """What the health check saw: a status string, an error text and the tool names."""
    status: str
    error: str = ""
    tools: list[str] = field(default_factory=list)


@dataclass
class InstallResult:
    ok: bool
    message: str
    name: str = ""
    path: Path | None = None
    tools: list[str] = field(default_factory=list)
    already_installed: bool = False
    needs_login: bool = False
    rolled_back: bool = False
    saved_env: list[str] = field(default_factory=list)


HealthCheck = Callable[[ServerSpec, Path, float], HealthResult]
SecretPrompt = Callable[[EnvVar], str]


# ─── Building the config entry ───────────────────────────────────────────────

def build_server_dict(entry: Connector, provided_env: list[str]) -> dict:
    """Make the mcp.json entry for a connector. Secrets are written as ${VAR} references."""
    if entry.remote:
        raw: dict = {"type": entry.transport, "url": entry.url}
        if entry.headers:
            raw["headers"] = dict(entry.headers)
        return raw
    raw = {"command": entry.command, "args": list(entry.args)}
    env = {}
    for var in entry.env_vars:
        if var.name in provided_env:
            env[var.name] = "${" + var.name + "}"
    if env:
        raw["env"] = env
    return raw


def custom_connector(name: str, target: str, transport: str = "") -> Connector:
    """Build a connector from what the user typed in 'Add custom': a URL or a command line."""
    target = target.strip()
    if target.lower().startswith(("http://", "https://")):
        kind = transport or "http"
        return Connector(id=name, name=name, publisher="custom", description="Custom connector",
                         category="Custom", transport=kind, auth="none", docs_url="https://modelcontextprotocol.io",
                         url=target)
    words = shlex.split(target)
    if not words:
        raise ValueError("Give a URL (https://...) or a command to run.")
    return Connector(id=name, name=name, publisher="custom", description="Custom connector",
                     category="Custom", transport="stdio", auth="none", docs_url="https://modelcontextprotocol.io",
                     command=words[0], args=words[1:])


# ─── Secrets ─────────────────────────────────────────────────────────────────

def known_value(var: EnvVar, given: dict[str, str]) -> str:
    """Look for a value: given explicitly, in the environment, or in ~/.axon/.env."""
    if given.get(var.name):
        return given[var.name]
    if os.environ.get(var.name):
        return os.environ[var.name]
    try:
        from axon.commands.common import lookup_key
        return lookup_key(var.name)
    except Exception:  # noqa: BLE001 - the key store is a convenience, never a reason to crash
        return ""


def collect_env(entry: Connector, given: dict[str, str],
                prompt: SecretPrompt | None) -> tuple[dict[str, str], list[str], list[str]]:
    """Gather values for the connector's variables.
    Returns (values, names still missing, names that are new and should be saved)."""
    values: dict[str, str] = {}
    missing: list[str] = []
    fresh: list[str] = []
    for var in entry.env_vars:
        value = known_value(var, given)
        if value and given.get(var.name):
            fresh.append(var.name)
        if not value and prompt is not None:
            value = prompt(var).strip()
            if value:
                fresh.append(var.name)
        if value:
            values[var.name] = value
        elif var.required:
            missing.append(var.name)
    return values, missing, fresh


def save_secrets(values: dict[str, str], fresh: list[str]) -> list[str]:
    """Write new values to ~/.axon/.env (owner-only) and export them for this process.
    Values that were already stored or exported are not written again."""
    from axon.providers.keystore import set_env_value, user_env_path
    saved = []
    for name, value in values.items():
        os.environ[name] = value
        if name in fresh:
            set_env_value(user_env_path(), name, value)
            saved.append(name)
    return saved


# ─── Health check ────────────────────────────────────────────────────────────

def check_server(spec: ServerSpec, workspace: Path, timeout: float) -> HealthResult:
    """Connect once and list tools. Never hangs: the work runs in a thread with a hard deadline."""
    box: dict[str, HealthResult] = {}

    def work() -> None:
        box["result"] = _connect_and_list(spec, workspace, timeout)

    thread = threading.Thread(target=work, name="marketplace-health", daemon=True)
    thread.start()
    thread.join(timeout + THREAD_GRACE_SECONDS)
    if "result" in box:
        return box["result"]
    return HealthResult("failed", f"Timed out after {timeout:.0f}s waiting for the server to respond.")


def _connect_and_list(spec: ServerSpec, workspace: Path, timeout: float) -> HealthResult:
    from axon.mcp.bridge import MCPToolBridge
    bridge = MCPToolBridge(workspace)
    try:
        conn = bridge.connect_server(spec, timeout=timeout)
        names = [str(t.get("name", "")) for t in conn.tools]
        return HealthResult(conn.status, conn.error or "", names)
    except Exception as e:  # noqa: BLE001 - report any failure as text
        return HealthResult("failed", f"{type(e).__name__}: {e}")
    finally:
        bridge.disconnect_all(timeout=3.0)


def friendly_error(entry: Connector, health: HealthResult) -> str:
    """Turn a failed health check into a message that says what to do next."""
    reason = health.error or health.status
    if entry.remote:
        return f"Could not reach {entry.url}: {reason}. Check your network and that the URL is still current."
    return (f"The server '{entry.command}' did not start: {reason}. "
            f"Make sure '{entry.command}' is installed and on your PATH.")


# ─── Install / remove / toggle ───────────────────────────────────────────────

def installed_names(workspace: Path) -> dict[str, ServerSpec]:
    """Every configured MCP server by name (user, project and local scopes)."""
    return MCPConfig(workspace).load_specs()


def _same_endpoint(existing: ServerSpec, wanted: ServerSpec) -> bool:
    return existing.endpoint_key() == wanted.endpoint_key()


def install_connector(entry: Connector, workspace: Path, *, scope: str = "user", name: str = "",
                      env: dict[str, str] | None = None, prompt: SecretPrompt | None = None,
                      keep: bool = False, check: bool = True, timeout: float = DEFAULT_CHECK_TIMEOUT,
                      health_check: HealthCheck = check_server) -> InstallResult:
    """Install one connector. Safe to run twice: a second run reports 'already installed'."""
    name = name or entry.id
    if not valid_server_name(name):
        return InstallResult(False, f"'{name}' is not a valid server name (letters, digits, - and _).", name)
    try:
        scope = normalize_scope(scope)
    except ValueError as e:
        return InstallResult(False, str(e), name)

    given = dict(env or {})
    values, missing, fresh = collect_env(entry, given, prompt)
    if missing:
        hint = "Pass them with --env NAME=VALUE or export them first."
        return InstallResult(False, f"Missing required value(s): {', '.join(missing)}. {hint}", name)

    config = MCPConfig(workspace)
    spec = ServerSpec.from_dict(name, build_server_dict(entry, list(values)), scope)
    existing = config.load_specs().get(name)
    if existing is not None:
        if _same_endpoint(existing, spec):
            return InstallResult(True, f"'{name}' is already installed ({existing.scope} scope).",
                                 name, already_installed=True)
        return InstallResult(False, f"A different server named '{name}' already exists. "
                                    f"Use --name to pick another name or remove it first.", name)

    blocked = config.add_block_reason(spec)
    if blocked:
        return InstallResult(False, blocked, name)

    saved = save_secrets(values, fresh)
    path = config.save_spec(spec, scope)
    if scope == "project":  # you just asked for it yourself; do not leave it waiting for /mcp approve
        from axon.mcp import state as mcp_state
        mcp_state.set_project_choice(workspace, name, "approved")
    result = InstallResult(True, f"Installed '{name}' in {path}.", name, path=path, saved_env=saved)
    if not check:
        result.message += " Health check skipped."
        return result
    return _finish_with_health_check(config, entry, spec, scope, result, keep, timeout, health_check, workspace)


def _finish_with_health_check(config: MCPConfig, entry: Connector, spec: ServerSpec, scope: str,
                              result: InstallResult, keep: bool, timeout: float,
                              health_check: HealthCheck, workspace: Path) -> InstallResult:
    health = health_check(spec, workspace, timeout)
    result.tools = health.tools
    if health.status == "connected":
        result.message += f" Connected: {len(health.tools)} tool(s) discovered."
        return result
    if health.status == "needs-auth":
        result.needs_login = True
        result.message += f" Sign in to finish: /mcp login {spec.name}"
        return result
    result.ok = False
    result.message = friendly_error(entry, health)
    if keep:
        result.message += " The config was kept (--keep); fix the problem and run /mcp reconnect " + spec.name + "."
        return result
    config.remove_server(spec.name, scope)
    result.rolled_back = True
    result.message += " The config was rolled back. Use --keep to keep it anyway."
    return result


def remove_connector(name: str, workspace: Path, scope: str | None = None) -> InstallResult:
    """Remove a server from the MCP config. Saved API keys stay in ~/.axon/.env."""
    config = MCPConfig(workspace)
    if config.get_spec(name) is None:
        return InstallResult(False, f"'{name}' is not installed.", name)
    if not config.remove_server(name, scope):
        return InstallResult(False, f"'{name}' is not defined in your own config files "
                                    f"(it may come from a plugin or your organization).", name)
    return InstallResult(True, f"Removed '{name}'. Saved API keys were left in ~/.axon/.env.", name)


def set_connector_enabled(name: str, workspace: Path, enabled: bool) -> InstallResult:
    config = MCPConfig(workspace)
    where = config.toggle(name, disabled=not enabled)
    if where is None:
        return InstallResult(False, f"Cannot change '{name}': unknown or managed by your organization.", name)
    if enabled:
        mcp_health.clear(name)
    word = "Enabled" if enabled else "Disabled"
    return InstallResult(True, f"{word} '{name}' ({where}).", name)
