"""
Manual control over installed connectors (MCP servers), skills and plugins.

Every function takes the workspace and a name, changes the real config files and returns a
:class:`Done` with a one-line message. Nothing here prints, so the slash commands, the ``axon
marketplace`` command and the tests all use the same code.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from axon.marketplace import installer
from axon.mcp import health as mcp_health
from axon.mcp.bridge import mcp_server_prefix, mcp_tool_name
from axon.mcp.config import MCPConfig, ServerSpec, normalize_scope
from axon.mcp.transports import log_path_for

OWN_SCOPES = ("user", "project", "local")
POLICIES = ("allow", "ask", "deny")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "AUTH")
MAX_LOG_LINES = 400


@dataclass
class Done:
    ok: bool
    message: str


def failed(message: str) -> Done:
    return Done(False, message)


# ─── finding and rewriting a server definition ───────────────────────────────

def own_definition(config: MCPConfig, name: str, scope: str | None = None) -> tuple[str, dict[str, Any]] | str:
    """The (scope, raw spec) that wins for ``name`` in your own files, or an error message.
    With ``scope`` that scope's copy is returned."""
    own = [(sc, raw) for sc, _path, raw in config.definitions(name) if sc in OWN_SCOPES]
    if not own:
        return f"'{name}' is not defined in your own config files (it may come from a plugin or your organization)."
    if scope is None:
        return own[-1]
    for sc, raw in own:
        if sc == scope:
            return sc, raw
    return f"'{name}' has no definition in {scope} scope."


def defined_in(config: MCPConfig, name: str, scope: str) -> bool:
    return any(sc == scope for sc, _path, _raw in config.definitions(name))


def edit_server(workspace: Path, name: str, change: Any, scope: str | None = None) -> Done:
    """Load the winning definition, let ``change(spec)`` modify it (return a message), save it back."""
    config = MCPConfig(workspace)
    found = own_definition(config, name, scope)
    if isinstance(found, str):
        return failed(found)
    where, raw = found
    spec = ServerSpec.from_dict(name, raw, where)
    message = change(spec)
    if isinstance(message, Done):
        return message
    config.save_spec(spec, where)
    return Done(True, message)


def set_timeout(workspace: Path, name: str, seconds: float) -> Done:
    if seconds <= 0:
        return failed("The timeout must be a number of seconds above zero.")

    def change(spec: ServerSpec) -> str:
        spec.timeout = float(seconds)
        spec.timeout_explicit = True
        return f"Timeout for '{name}' is now {seconds:g}s ({spec.scope} scope)."

    return edit_server(workspace, name, change)


def reference_for(var: str) -> str:
    return "${" + var + "}"


def is_secret_name(var: str) -> bool:
    upper = var.upper()
    return any(word in upper for word in SECRET_WORDS)


def save_key(var: str, value: str) -> None:
    """Keep a key in the owner-only ~/.axon/.env and export it for this process."""
    from axon.providers.keystore import set_env_value, user_env_path
    set_env_value(user_env_path(), var, value)
    os.environ[var] = value


def uses_variable(spec: ServerSpec, var: str) -> bool:
    ref = reference_for(var)
    texts = [spec.url, *spec.args, *spec.env.values(), *spec.headers.values()]
    return any(ref in text or ref[:-1] + ":-" in text for text in texts)


def set_env(workspace: Path, name: str, values: dict[str, str], unset: list[str] | None = None,
            literal: bool = False) -> Done:
    """Set or remove environment variables / keys of a server.

    A secret (name with KEY, TOKEN, SECRET ...) is saved in ~/.axon/.env and the server config keeps only
    ``${NAME}``. With ``literal`` the value is written into the config as typed."""
    unset = unset or []
    notes: list[str] = []

    def change(spec: ServerSpec) -> str:
        for var, value in values.items():
            if literal:
                spec.env[var] = value
                continue
            save_key(var, value)
            if spec.type == "stdio":
                spec.env[var] = reference_for(var)
            elif not uses_variable(spec, var):
                notes.append(f"'{name}' does not mention ${{{var}}} in its url or headers yet")
        for var in unset:
            spec.env.pop(var, None)
        return f"Updated {', '.join(list(values) + unset)} for '{name}'."

    result = edit_server(workspace, name, change)
    if result.ok and notes:
        result.message += " Note: " + "; ".join(notes) + "."
    return result


def move_scope(workspace: Path, name: str, target: str, source: str | None = None) -> Done:
    """Move a server definition to another scope (user, project or local)."""
    try:
        target = normalize_scope(target)
    except ValueError as e:
        return failed(str(e))
    config = MCPConfig(workspace)
    found = own_definition(config, name, source)
    if isinstance(found, str):
        return failed(found)
    current, raw = found
    if current == target:
        return Done(True, f"'{name}' is already in {target} scope.")
    if defined_in(config, name, target):
        return failed(f"'{name}' is already defined in {target} scope; remove that copy first.")
    spec = ServerSpec.from_dict(name, raw, target)
    blocked = config.add_block_reason(spec)
    if blocked:
        return failed(blocked)
    config.save_spec(spec, target)
    config.remove_server(name, current)
    warning = ""
    if target == "project" and literal_secrets(spec):
        warning = (" Warning: .mcp.json is meant to be committed and holds literal values for "
                   f"{', '.join(literal_secrets(spec))}; use /marketplace env {name} to move them to ~/.axon/.env.")
    return Done(True, f"Moved '{name}' from {current} to {target} scope.{warning}")


def literal_secrets(spec: ServerSpec) -> list[str]:
    """Names of env variables and headers whose value looks like a secret typed straight into the config."""
    names = [var for var, value in spec.env.items() if is_secret_name(var) and "${" not in value]
    names += [header for header, value in spec.headers.items() if is_secret_name(header) and "${" not in value]
    return names


# ─── health check and logs ───────────────────────────────────────────────────

def probe_server(workspace: Path, name: str, timeout: float = installer.DEFAULT_CHECK_TIMEOUT,
                health_check: installer.HealthCheck = installer.check_server) -> tuple[Done, list[str]]:
    """Connect once and list the tools. Returns the verdict and the tool names."""
    spec = MCPConfig(workspace).get_spec(name)
    if spec is None:
        return failed(f"'{name}' is not configured."), []
    if spec.disabled:
        return failed(f"'{name}' is disabled. Enable it first: /marketplace enable {name}"), []
    started = time.monotonic()
    health = health_check(spec, workspace, timeout)
    seconds = time.monotonic() - started
    if health.status == "connected":
        mcp_health.record_success(name)
        return Done(True, f"'{name}' is healthy: {len(health.tools)} tool(s) in {seconds:.1f}s."), health.tools
    reason = health.error or health.status
    mcp_health.note_error(name, reason)
    if health.status == "needs-auth":
        return failed(f"'{name}' needs you to sign in: /mcp login {name}"), []
    return failed(f"'{name}' did not connect ({health.status}): {reason}"), []


def read_logs(name: str, lines: int = 40) -> list[str]:
    path = log_path_for(name)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-max(1, min(lines, MAX_LOG_LINES)):]


# ─── per-tool permissions ────────────────────────────────────────────────────

def permission_engine(workspace: Path, live: Any = None) -> Any:
    """The running session's permission engine, or a fresh one that reads and writes the same files."""
    if live is not None:
        return live
    from axon.permissions.engine import PermissionEngine
    return PermissionEngine(SimpleNamespace(workspace=Path(workspace), permissions=None))


def rule_name(server: str, tool: str) -> str:
    """``mcp__github`` for a whole server (tool ``*`` or empty), ``mcp__github__create_issue`` for one tool."""
    if tool in ("", "*"):
        return mcp_server_prefix(server)
    return mcp_tool_name(server, tool)


def settings_file(workspace: Path, user_wide: bool) -> Path:
    if user_wide:
        from axon.storage import axon_home
        return axon_home() / "settings.json"
    return Path(workspace) / ".axon" / "settings.local.json"


def write_rule(path: Path, kind: str, rule: str) -> None:
    import json
    from axon.session.store import atomic_write_text
    data: Any = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as e:
            raise ValueError(f"{path} is not valid JSON ({e}); fix it by hand: Axon will not overwrite it") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object; fix it by hand: Axon will not overwrite it")
    perms = data.get("permissions")
    perms = perms if isinstance(perms, dict) else {}
    rules = perms.get(kind)
    rules = rules if isinstance(rules, list) else []
    if rule not in rules:
        rules.append(rule)
    perms[kind] = rules
    data["permissions"] = perms
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def set_tool_policy(workspace: Path, server: str, tool: str, policy: str, engine: Any = None,
                    user_wide: bool = False) -> Done:
    """Always allow, always ask or always deny one tool (or every tool) of a server.
    ``policy`` ``reset`` removes the rules so the normal permission mode decides again."""
    if policy not in POLICIES + ("reset",):
        return failed("Choose allow, ask, deny or reset.")
    if MCPConfig(workspace).get_spec(server) is None:
        return failed(f"'{server}' is not configured.")
    rule = rule_name(server, tool)
    engine = permission_engine(workspace, engine)
    for kind in POLICIES:
        engine.remove_rule(kind, rule)
    if policy == "reset":
        engine.reload()
        return Done(True, f"Removed the allow/ask/deny rules for {rule}.")
    path = settings_file(workspace, user_wide)
    try:
        write_rule(path, policy, rule)
    except ValueError as e:
        return failed(str(e))
    engine.reload()
    return Done(True, f"{policy.capitalize()} {rule}  (saved in {path})")


def tool_policies(workspace: Path, server: str, engine: Any = None) -> list[tuple[str, str, str]]:
    """(kind, rule, source) for every permission rule that mentions the server."""
    engine = permission_engine(workspace, engine)
    prefix = mcp_server_prefix(server)
    return [(entry.kind, str(entry.rule), entry.source) for entry in engine.list_rules()
            if str(entry.rule).startswith(prefix)]


# ─── skills ──────────────────────────────────────────────────────────────────

def set_skill_enabled(workspace: Path, name: str, enabled: bool, scope: str = "user") -> Done:
    """Switch a skill on or off. The choice is saved in settings.json and the skill list and the
    system prompt follow it."""
    from axon.skills import settings as skill_settings
    from axon.skills.manager import SkillManager
    manager = SkillManager(workspace)
    skill = manager.get(name) or manager.disabled.get(name)
    if skill is None:
        return failed(f"No skill called '{name}'. See: /marketplace list skills --installed")
    if skill.scope == "plugin":
        return failed(f"'{name}' comes from a plugin ({skill.plugin}); enable or disable the plugin instead.")
    try:
        if enabled:
            skill_settings.enable_skill(workspace, skill.name)
            return Done(True, f"Enabled skill '{skill.name}'.")
        path = skill_settings.disable_skill(workspace, skill.name, scope)
    except ValueError as e:      # an unparsable settings.json is left as it is
        return failed(str(e))
    return Done(True, f"Disabled skill '{skill.name}' (saved in {path}). Turn it on again: /marketplace enable {skill.name}")


# ─── moving skills and plugins between user and project scope ────────────────

def folder_scopes(target: str) -> str | None:
    """'user' or 'project' for a skill or plugin; None (with the caller reporting) for anything else."""
    return target if target in ("user", "project") else None


def move_skill_scope(workspace: Path, name: str, target: str) -> Done:
    import shutil
    from axon.marketplace import skill_tools
    if folder_scopes(target) is None:
        return failed("Skills live in user or project scope (local is only for connectors).")
    found = skill_tools.installed_skills(workspace).get(name)
    if found is None:
        return failed(f"Skill '{name}' is not installed in your own folders.")
    current, skill_file = found
    if current == target:
        return Done(True, f"Skill '{name}' is already in {target} scope.")
    destination = skill_tools.skills_root(workspace, target) / name
    if destination.exists():
        return failed(f"{destination} already exists; remove that copy first.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(skill_file.parent), str(destination))
    return Done(True, f"Moved skill '{name}' from {current} to {target} scope ({destination}).")


def move_plugin_scope(workspace: Path, name: str, target: str) -> Done:
    import shutil
    from axon.commands import marketplace as plugin_market
    from axon.commands import plugins as plugin_core
    from axon.marketplace import plugin_tools
    if folder_scopes(target) is None:
        return failed("Plugins live in user or project scope (local is only for connectors).")
    plugin = plugin_tools.installed_plugins(workspace).get(name)
    if plugin is None:
        return failed(f"Plugin '{name}' is not installed.")
    if plugin.scope == target:
        return Done(True, f"Plugin '{name}' is already in {target} scope.")
    destination = plugin_market.install_root(workspace, target) / plugin.path.name
    if destination.exists():
        return failed(f"{destination} already exists; remove that copy first.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:  # settings first: an unparsable settings.json must stop the move before anything changed
        plugin_core.set_enabled_in_settings(workspace, plugin.id, plugin.enabled, target)
    except ValueError as e:
        return failed(str(e))
    shutil.move(str(plugin.path), str(destination))
    try:
        plugin_core.set_enabled_in_settings(workspace, plugin.id, None, plugin.scope)
    except ValueError as e:  # undo: the plugin goes back and the new scope forgets it
        shutil.move(str(destination), str(plugin.path))
        try:
            plugin_core.set_enabled_in_settings(workspace, plugin.id, None, target)
        except ValueError:
            pass
        return failed(str(e))
    return Done(True, f"Moved plugin '{name}' from {plugin.scope} to {target} scope. Run /reload-plugins.")


# ─── health check for skills and plugins ─────────────────────────────────────

def check_skill(workspace: Path, name: str) -> Done:
    """Read the skill the way Axon does and report what is wrong with it."""
    from axon.marketplace import skill_tools
    found = skill_tools.installed_skills(workspace).get(name)
    if found is None:
        return failed(f"Skill '{name}' is not installed in your own folders.")
    problems = skill_tools.validate_skill_file(found[1])
    if problems:
        return failed(f"Skill '{name}' has problems: " + "; ".join(problems))
    return Done(True, f"Skill '{name}' is valid.")


def check_plugin(workspace: Path, name: str) -> Done:
    from axon.commands import plugins as plugin_core
    from axon.marketplace import plugin_tools
    plugin = plugin_tools.installed_plugins(workspace).get(name)
    if plugin is None:
        return failed(f"Plugin '{name}' is not installed.")
    errors, warnings = plugin_core.validate_plugin_dir(plugin.path)
    if errors:
        return failed(f"Plugin '{name}' has problems: " + "; ".join(errors))
    note = f" ({len(warnings)} warning(s))" if warnings else ""
    return Done(True, f"Plugin '{name}' is valid{note}.")
