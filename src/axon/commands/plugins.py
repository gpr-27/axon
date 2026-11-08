"""
Plugins: a directory with a ``plugin.json`` manifest (or ``.claude-plugin/plugin.json``, or no
manifest at all when it uses the standard layout) that bundles any of:

  commands/        markdown slash commands      -> /<plugin>:<name>   (loaded by axon.commands.custom)
  skills/          <skill>/SKILL.md folders     -> /<plugin>:<skill>  (loaded by axon.skills.manager)
  SKILL.md         a single skill at the plugin root (when there is no ``skills/`` folder)
  agents/          markdown subagent definitions                       (axon.agent.subagent)
  hooks            ``hooks/hooks.json`` + manifest "hooks"            (axon.hooks.manager)
  MCP              ``.mcp.json`` + manifest "mcpServers"              (axon.mcp.config)
  output-styles/   markdown output styles                              (axon.commands.output_styles)

Manifest keys (Claude Code compatible): ``name`` ``displayName`` ``version`` ``description`` ``author``
``defaultEnabled`` ``dependencies`` ``userConfig`` and the component keys ``skills`` (adds to the default
scan), ``commands`` (path, paths or an object map of ``source`` / ``content``; replaces the default),
``agents`` (replaces), ``outputStyles`` (replaces), ``hooks`` and ``mcpServers`` (path, inline object or
an array of either; merged with the default file). Every component path must start with ``./``, exist
and stay inside the plugin. ``/plugin validate`` reports what does not.

``${AXON_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_ROOT}``, ``${CLAUDE_PLUGIN_DATA}``, ``${CLAUDE_PROJECT_DIR}``
and ``${user_config.KEY}`` expand in hook commands, MCP server specs and skill / command bodies.

Roots: ~/.axon/plugins/<name>/ (user) and <ws>/.axon/plugins/<name>/ (project / local scope).
Installing from a marketplace (:mod:`axon.commands.marketplace`) copies the plugin there and writes
``.axon-install.json`` next to it. ``enabledPlugins`` in settings.json (``name`` or ``name@marketplace``)
and ``~/.axon/plugins/disabled.json`` switch a plugin off.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from axon.platform import rmtree as _rmtree
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from axon.storage import axon_home as _axon_home

TRUSTED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
INSTALL_RECORD = ".axon-install.json"
STANDARD_COMPONENTS = ("commands", "skills", "agents", "hooks", "output-styles", ".mcp.json", "SKILL.md")
KNOWN_MANIFEST_KEYS = {
    "$schema", "name", "displayName", "version", "description", "author", "homepage", "repository", "license",
    "keywords", "metadata", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl",
    "defaultEnabled", "dependencies", "settings", "userConfig", "types", "channels", "skills", "commands",
    "agents", "hooks", "mcpServers", "lspServers", "outputStyles", "workflows", "experimental", "themes",
    "monitors",
}
_NAME_BAD = re.compile(r"[\s@:/\\\x00-\x1f​-‏‪-‮⁦-⁩]")


@dataclass
class Plugin:
    name: str
    path: Path
    scope: str  # "user" | "project"
    version: str = ""
    description: str = ""
    enabled: bool = True
    manifest: dict[str, Any] = field(default_factory=dict)
    marketplace: str = ""
    display_name: str = ""
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.name}@{self.marketplace}" if self.marketplace else self.name

    @property
    def title(self) -> str:
        return self.display_name or self.name

    def components(self) -> dict[str, int]:
        out: dict[str, int] = {}
        out["commands"] = len(plugin_command_files(self)) + len(plugin_inline_commands(self))
        out["skills"] = len(plugin_skill_files(self))
        out["agents"] = len(plugin_agent_files(self))
        out["output-styles"] = len(plugin_output_style_files(self))
        if plugin_hooks(self):
            out["hooks"] = 1
        if plugin_mcp_servers(self):
            out["mcp"] = 1
        return {k: v for k, v in out.items() if v}


def plugin_roots(workspace: Path) -> list[tuple[Path, str]]:
    return [(_axon_home() / "plugins", "user"), (Path(workspace) / ".axon" / "plugins", "project")]


def _manifest_path(d: Path) -> Path | None:
    for cand in (d / ".claude-plugin" / "plugin.json", d / "plugin.json"):
        if cand.is_file():
            return cand
    return None


def _has_standard_layout(d: Path) -> bool:
    return any((d / c).exists() for c in STANDARD_COMPONENTS)


def _disabled_file() -> Path:
    return _axon_home() / "plugins" / "disabled.json"


def _disabled() -> set[str]:
    try:
        data = json.loads(_disabled_file().read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _write_disabled(d: set[str]) -> None:
    from axon.session.store import atomic_write_text
    atomic_write_text(_disabled_file(), json.dumps(sorted(d), indent=2) + "\n")


# ─── settings (enabledPlugins / pluginConfigs) ──────────────────────────────

def scope_settings_file(workspace: Path, scope: str) -> Path:
    """settings.json that holds ``enabledPlugins`` for an install scope (user | project | local)."""
    if scope == "user":
        return _axon_home() / "settings.json"
    if scope == "local":
        return Path(workspace) / ".axon" / "settings.local.json"
    return Path(workspace) / ".axon" / "settings.json"


def _update_settings_json(path: Path, mutate: Any) -> None:
    from axon.session.store import atomic_write_text
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as e:
            raise ValueError(f"{path} is not valid JSON ({e}); fix it by hand: Axon will not overwrite it") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object; fix it by hand: Axon will not overwrite it")
    mutate(data)
    if not data and not path.exists():
        return      # nothing to record: do not create an empty settings file
    from axon.hooks.manager import config_change
    config_change(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def set_enabled_in_settings(workspace: Path, plugin_id: str, enabled: bool | None, scope: str = "user") -> Path:
    """Write (or with ``None`` remove) ``enabledPlugins[plugin_id]`` in the settings file of ``scope``."""
    path = scope_settings_file(workspace, scope)

    def mutate(data: dict[str, Any]) -> None:
        ep = data.get("enabledPlugins")
        if not isinstance(ep, dict):
            ep = {}
        if enabled is None:
            ep.pop(plugin_id, None)
        else:
            ep[plugin_id] = bool(enabled)
        data["enabledPlugins"] = ep
        if not ep:
            data.pop("enabledPlugins", None)

    _update_settings_json(path, mutate)
    return path


def enabled_plugins_setting(workspace: Path) -> dict[str, bool]:
    """``enabledPlugins`` from user < project < local settings.json (Claude-Code key; keys may be
    ``name`` or ``name@marketplace``; both stay addressable by the full key and the bare name).
    Both the ``.claude`` and ``.axon`` settings files are read."""
    from axon.skills.settings import read_json, settings_files
    out: dict[str, bool] = {}
    for path in settings_files(Path(workspace)):
        ep = read_json(path).get("enabledPlugins")
        if isinstance(ep, dict):
            for k, v in ep.items():
                out[str(k)] = bool(v)
                out[str(k).split("@", 1)[0]] = bool(v)
    return out


def set_enabled(name: str, enabled: bool, workspace: Path | None = None, scope: str | None = None) -> None:
    """Enable / disable a plugin. With a ``scope`` the ``enabledPlugins`` entry of that scope's settings
    file is written; otherwise ``plugins/disabled.json`` is used."""
    d = _disabled()
    if scope is not None and workspace is not None:
        set_enabled_in_settings(Path(workspace), name, enabled, scope)
        d.discard(name.split("@", 1)[0])
        _write_disabled(d)
        return
    if enabled:
        d.discard(name)
    else:
        d.add(name)
    _write_disabled(d)


# ─── loading ────────────────────────────────────────────────────────────────

def read_install_record(d: Path) -> dict[str, Any]:
    return _read_json_dict(d / INSTALL_RECORD) or {}


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def merge_marketplace_entry(manifest: dict[str, Any], entry: dict[str, Any], has_manifest: bool) -> dict[str, Any]:
    """Combine a marketplace entry with ``plugin.json`` (Claude Code's ``strict`` rules): the entry is the
    manifest when there is no ``plugin.json``; otherwise it appends ``commands`` / ``agents`` / ``skills`` /
    ``outputStyles``, replaces the manifest's matchers for an event it declares, and overrides display
    fields and ``defaultEnabled``. The manifest ``version`` wins."""
    if not isinstance(entry, dict) or not entry:
        return manifest
    skip = {"source", "strict", "category", "tags"}
    if not has_manifest:
        return {k: v for k, v in entry.items() if k not in skip}
    out = dict(manifest)
    for k in ("displayName", "defaultEnabled", "description", "author", "homepage", "license", "keywords"):
        if k in entry and (k in ("displayName", "defaultEnabled") or k not in out):
            out[k] = entry[k]
    if entry.get("strict", True) is False:
        return out
    for k in ("commands", "agents", "skills", "outputStyles"):
        if k in entry:
            cur, add = out.get(k), entry[k]
            if isinstance(cur, dict) and isinstance(add, dict):
                out[k] = {**cur, **add}
            elif cur is None:
                out[k] = add
            else:
                out[k] = _as_list(cur) + _as_list(add)
    if isinstance(entry.get("hooks"), dict):
        base = out.get("hooks")
        merged = dict(base) if isinstance(base, dict) else {}
        merged.update(entry["hooks"])
        out["hooks"] = merged
    if "mcpServers" in entry:
        cur = out.get("mcpServers")
        if isinstance(cur, dict) and isinstance(entry["mcpServers"], dict):
            out["mcpServers"] = {**cur, **entry["mcpServers"]}
        elif cur is None:
            out["mcpServers"] = entry["mcpServers"]
        else:
            out["mcpServers"] = _as_list(cur) + _as_list(entry["mcpServers"])
    return out


def load_plugins(workspace: Path) -> list[Plugin]:
    """All installed plugins; a project plugin overrides a user plugin of the same name.
    A plugin is enabled unless /plugin disable'd or ``enabledPlugins: {name: false}`` in settings;
    a manifest ``defaultEnabled: false`` makes it start disabled until the user enables it."""
    disabled = _disabled()
    setting = enabled_plugins_setting(workspace)
    found: dict[str, Plugin] = {}
    for root, scope in plugin_roots(workspace):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name in ("cache", "data", "marketplaces"):
                continue
            mp = _manifest_path(d)
            record = read_install_record(d)
            manifest: dict[str, Any] = {}
            if mp is not None:
                try:
                    manifest = json.loads(mp.read_text(encoding="utf-8-sig"))
                    if not isinstance(manifest, dict):
                        continue
                except (OSError, ValueError):
                    continue
            elif not _has_standard_layout(d) and not record:
                continue
            if isinstance(record.get("entry"), dict):
                manifest = merge_marketplace_entry(manifest, record["entry"], mp is not None)
            name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(manifest.get("name") or record.get("name") or d.name)).strip("-") or d.name
            marketplace = str(record.get("marketplace") or "")
            pid = f"{name}@{marketplace}" if marketplace else name
            default_enabled = manifest.get("defaultEnabled", True) is not False
            if pid in setting:
                enabled = setting[pid]
            elif name in setting:
                enabled = setting[name]
            else:
                enabled = default_enabled
            enabled = enabled and name not in disabled
            found[name] = Plugin(
                name=name, path=d, scope=scope, version=str(manifest.get("version") or record.get("version") or ""),
                description=str(manifest.get("description", "")), enabled=enabled, manifest=manifest,
                marketplace=marketplace, display_name=str(manifest.get("displayName") or ""), record=record,
            )
    return sorted(found.values(), key=lambda p: p.name)


def plugin_dirs(workspace: Path) -> list[Path]:
    """Directories of enabled plugins (for loading agents/, hooks and MCP configs)."""
    return [p.path for p in load_plugins(workspace) if p.enabled]


def enabled_plugins(workspace: Path) -> list[Plugin]:
    try:
        return [p for p in load_plugins(workspace) if p.enabled]
    except OSError:
        return []


# ─── variables / user config ────────────────────────────────────────────────

_ROOT_VARS = ("${AXON_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}", "$AXON_PLUGIN_ROOT", "$CLAUDE_PLUGIN_ROOT")
_UC_RE = re.compile(r"\$\{user_config\.([A-Za-z_][A-Za-z0-9_]*)\}")


def plugin_data_dir(plugin: Plugin) -> Path:
    """Persistent data directory (``${CLAUDE_PLUGIN_DATA}``): survives plugin updates; created on first use."""
    ident = re.sub(r"[^A-Za-z0-9_-]", "-", plugin.id)
    d = _axon_home() / "plugins" / "data" / ident
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _secrets_file() -> Path:
    return _axon_home() / "plugins" / "secrets.json"


def plugin_user_config_schema(plugin: Plugin) -> dict[str, dict[str, Any]]:
    uc = plugin.manifest.get("userConfig")
    return {str(k): v for k, v in uc.items() if isinstance(v, dict)} if isinstance(uc, dict) else {}


def _cfg_str(v: Any) -> str:
    if isinstance(v, list):
        return ",".join(map(str, v))
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def plugin_user_config_values(plugin: Plugin, workspace: Path | None = None, include_sensitive: bool = True) -> dict[str, str]:
    """Saved ``userConfig`` values: manifest defaults < ``pluginConfigs`` in settings < secrets (sensitive).
    With ``include_sensitive=False`` a sensitive option becomes a placeholder (skill / agent content)."""
    from axon.skills.settings import get_dict_setting
    schema = plugin_user_config_schema(plugin)
    vals: dict[str, str] = {}
    for k, spec in schema.items():
        if spec.get("default") is not None:
            vals[k] = _cfg_str(spec["default"])
    cfg = get_dict_setting(workspace or Path.cwd(), "pluginConfigs")
    for key in (plugin.name, plugin.id):
        entry = cfg.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("options"), dict):
            for k, v in entry["options"].items():
                vals[str(k)] = _cfg_str(v)
    secrets = _read_json_dict(_secrets_file()) or {}
    mine = secrets.get(plugin.id) or secrets.get(plugin.name)
    if isinstance(mine, dict):
        for k, v in mine.items():
            vals[str(k)] = str(v)
    if not include_sensitive:
        for k, spec in schema.items():
            if spec.get("sensitive"):
                vals[k] = f"<{k}: sensitive value not shown>"
    return vals


def save_user_config(plugin: Plugin, workspace: Path, values: dict[str, str]) -> list[str]:
    """Persist ``userConfig`` values (sensitive ones to ``plugins/secrets.json``, 0600). Returns problems."""
    schema = plugin_user_config_schema(plugin)
    problems: list[str] = []
    plain: dict[str, Any] = {}
    secret: dict[str, str] = {}
    for k, v in values.items():
        spec = schema.get(k)
        if spec is None:
            problems.append(f"unknown option '{k}'")
            continue
        t = spec.get("type", "string")
        if t == "number":
            try:
                num = float(v)
            except ValueError:
                problems.append(f"{k}: '{v}' is not a number")
                continue
            if ("min" in spec and num < spec["min"]) or ("max" in spec and num > spec["max"]):
                problems.append(f"{k}: {v} is out of range")
                continue
        if t == "boolean" and v.strip().lower() not in ("true", "false", "yes", "no", "1", "0"):
            problems.append(f"{k}: '{v}' is not a boolean")
            continue
        opts = spec.get("options")
        if isinstance(opts, list) and opts and v not in opts and not spec.get("multiple"):
            problems.append(f"{k}: must be one of {', '.join(map(str, opts))}")
            continue
        (secret if spec.get("sensitive") else plain)[k] = v
    if plain:
        def mutate(data: dict[str, Any]) -> None:
            cfg = data.get("pluginConfigs")
            cfg = cfg if isinstance(cfg, dict) else {}
            entry = cfg.get(plugin.id)
            entry = entry if isinstance(entry, dict) else {}
            opts = entry.get("options")
            opts = opts if isinstance(opts, dict) else {}
            opts.update(plain)
            entry["options"] = opts
            cfg[plugin.id] = entry
            data["pluginConfigs"] = cfg

        _update_settings_json(scope_settings_file(workspace, "user"), mutate)
    if secret:
        from axon.session.store import atomic_write_text
        sf = _secrets_file()
        data = _read_json_dict(sf) or {}
        mine = data.get(plugin.id)
        mine = mine if isinstance(mine, dict) else {}
        mine.update(secret)
        data[plugin.id] = mine
        sf.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(sf, json.dumps(data, indent=2) + "\n")
        try:
            os.chmod(sf, 0o600)
        except OSError:
            pass
    return problems


def expand_plugin_root(value: Any, root: Path, *, plugin: Plugin | None = None, workspace: Path | None = None,
                       user_config: dict[str, str] | None = None) -> Any:
    """Recursively replace ``${AXON_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_ROOT}`` (and, with ``plugin`` /
    ``workspace`` / ``user_config``, ``${CLAUDE_PLUGIN_DATA}``, ``${CLAUDE_PROJECT_DIR}`` and
    ``${user_config.KEY}``) in the strings of ``value``."""
    if isinstance(value, str):
        for var in _ROOT_VARS:
            value = value.replace(var, str(root))
        if "PLUGIN_DATA" in value and plugin is not None:
            data = str(plugin_data_dir(plugin))
            value = value.replace("${CLAUDE_PLUGIN_DATA}", data).replace("${AXON_PLUGIN_DATA}", data)
        if "PROJECT_DIR" in value:
            proj = str(workspace or Path.cwd())
            value = value.replace("${CLAUDE_PROJECT_DIR}", proj).replace("${AXON_PROJECT_DIR}", proj)
        if user_config and "${user_config." in value:
            value = _UC_RE.sub(lambda m: user_config.get(m.group(1), m.group(0)), value)
        return value
    if isinstance(value, list):
        return [expand_plugin_root(v, root, plugin=plugin, workspace=workspace, user_config=user_config) for v in value]
    if isinstance(value, dict):
        return {k: expand_plugin_root(v, root, plugin=plugin, workspace=workspace, user_config=user_config)
                for k, v in value.items()}
    return value


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ─── component resolution ───────────────────────────────────────────────────

def resolve_inside(plugin_root: Path, rel: str) -> Path | None:
    """``rel`` (relative to the plugin root) if it stays inside it, else None."""
    try:
        base = plugin_root.resolve()
        p = (base / rel).resolve()
        p.relative_to(base)
        return p
    except (OSError, ValueError):
        return None


def _paths(plugin: Plugin, key: str) -> list[Path] | None:
    """Existing, contained paths named by a path-valued manifest key; None when the key is absent."""
    if key not in plugin.manifest:
        return None
    out: list[Path] = []
    for rel in _as_list(plugin.manifest.get(key)):
        if not isinstance(rel, str) or not rel.strip():
            continue
        p = resolve_inside(plugin.path, rel.strip())
        if p is not None and p.exists():
            out.append(p)
    return out


def plugin_skill_files(plugin: Plugin) -> list[Path]:
    """SKILL.md files: ``skills/*/SKILL.md`` plus the manifest ``skills`` directories (each a folder of
    skill folders, or one folder holding SKILL.md) and a root SKILL.md when there is no ``skills/``."""
    files: list[Path] = []
    default = plugin.path / "skills"
    extra = _paths(plugin, "skills")
    if default.is_dir():
        try:
            files.extend(sorted(default.glob("*/SKILL.md")))
        except OSError:
            pass
    elif extra is None and (plugin.path / "SKILL.md").is_file():
        files.append(plugin.path / "SKILL.md")
    for d in extra or []:
        if d.is_dir():
            if (d / "SKILL.md").is_file():
                files.append(d / "SKILL.md")
            try:
                files.extend(sorted(d.glob("*/SKILL.md")))
            except OSError:
                pass
    seen: set[Path] = set()
    out: list[Path] = []
    for f in files:
        try:
            k = f.resolve()
        except OSError:
            k = f
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def plugin_command_files(plugin: Plugin) -> list[tuple[Path, Path]]:
    """(base directory, command .md file). The manifest ``commands`` paths replace ``commands/``."""
    if isinstance(plugin.manifest.get("commands"), dict):
        return []
    paths = _paths(plugin, "commands")
    if paths is None:
        paths = [plugin.path / "commands"]
    out: list[tuple[Path, Path]] = []
    for p in paths:
        if p.is_dir():
            out.extend((p, f) for f in sorted(p.rglob("*.md")) if not f.name.startswith("."))
        elif p.is_file() and p.suffix.lower() == ".md":
            out.append((p.parent, p))
    return out


def plugin_inline_commands(plugin: Plugin) -> dict[str, dict[str, Any]]:
    """Object-map ``commands``: name -> {body, path, description, argument_hint, model, allowed_tools}."""
    cmds = plugin.manifest.get("commands")
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(cmds, dict):
        return out
    for name, spec in cmds.items():
        if not isinstance(spec, dict):
            continue
        has_src, has_content = isinstance(spec.get("source"), str), isinstance(spec.get("content"), str)
        if has_src == has_content:
            continue  # exactly one of source / content
        path: Path | None = None
        if has_src:
            path = resolve_inside(plugin.path, spec["source"])
            if path is None or not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        else:
            body = spec["content"]
        tools = spec.get("allowedTools")
        out[str(name)] = {
            "body": body, "path": path, "description": str(spec.get("description") or ""),
            "argument_hint": str(spec.get("argumentHint") or ""),
            "model": spec.get("model") if isinstance(spec.get("model"), str) else None,
            "allowed_tools": [str(t) for t in tools] if isinstance(tools, list) else [],
        }
    return out


def plugin_agent_files(plugin: Plugin) -> list[Path]:
    """Agent .md files: manifest ``agents`` (files; replaces ``agents/``) else ``agents/**/*.md``."""
    paths = _paths(plugin, "agents")
    files: list[Path] = []
    if paths is None:
        d = plugin.path / "agents"
        if d.is_dir():
            files = sorted(f for f in d.rglob("*.md") if not f.name.startswith("."))
    else:
        for p in paths:
            if p.is_file() and p.suffix.lower() == ".md":
                files.append(p)
            elif p.is_dir():
                files.extend(sorted(p.rglob("*.md")))
    return files


def plugin_output_style_files(plugin: Plugin) -> list[Path]:
    paths = _paths(plugin, "outputStyles")
    if paths is None:
        d = plugin.path / "output-styles"
        return sorted(d.glob("*.md")) if d.is_dir() else []
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.glob("*.md")))
    return files


def _json_sources(plugin: Plugin, default_rel: str, key: str, wrapper: str) -> list[dict[str, Any]]:
    """Config objects (already unwrapped) from the default file, then the manifest value(s)."""
    out: list[dict[str, Any]] = []

    def unwrap(data: dict[str, Any]) -> dict[str, Any]:
        inner = data.get(wrapper)
        return inner if isinstance(inner, dict) else data

    default = plugin.path / default_rel
    if default.is_file():
        d = _read_json_dict(default)
        if d:
            out.append(unwrap(d))
    for item in _as_list(plugin.manifest.get(key)):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip().lower().endswith(".json"):
            p = resolve_inside(plugin.path, item.strip())
            if p is not None and p.is_file() and p != default.resolve():
                d = _read_json_dict(p)
                if d:
                    out.append(unwrap(d))
    return out


def plugin_hooks(plugin: Plugin, workspace: Path | None = None) -> dict[str, Any]:
    """Event -> groups mapping from ``hooks/hooks.json`` merged with the manifest ``hooks`` (a path, an
    inline event map, or an array of either); groups for the same event are concatenated."""
    merged: dict[str, list[Any]] = {}
    for section in _json_sources(plugin, "hooks/hooks.json", "hooks", "hooks"):
        for event, groups in section.items():
            merged.setdefault(event, []).extend(groups if isinstance(groups, list) else [groups])
    if not merged:
        return {}
    uc = plugin_user_config_values(plugin, workspace) if plugin.manifest.get("userConfig") else None
    return expand_plugin_root(merged, plugin.path, plugin=plugin, workspace=workspace, user_config=uc)


def plugin_mcp_servers(plugin: Plugin, workspace: Path | None = None) -> dict[str, dict[str, Any]]:
    """Server specs from ``.mcp.json`` then the manifest ``mcpServers`` (path / inline map / array); a name
    declared later replaces an earlier one. ``.mcpb`` bundles are not supported."""
    servers: dict[str, dict[str, Any]] = {}
    for section in _json_sources(plugin, ".mcp.json", "mcpServers", "mcpServers"):
        for k, v in section.items():
            if isinstance(v, dict):
                servers[str(k)] = v
    if not servers:
        return {}
    uc = plugin_user_config_values(plugin, workspace) if plugin.manifest.get("userConfig") else None
    return {k: expand_plugin_root(v, plugin.path, plugin=plugin, workspace=workspace, user_config=uc)
            for k, v in servers.items()}


# ─── validation ─────────────────────────────────────────────────────────────

_RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
_RESERVED_NAMES = {"claude", "anthropic", "anthropics", "claude-code", "claude-mods"}
OFFICIAL_MARKETPLACES = {
    "claude-plugins-official", "claude-code-marketplace", "claude-code-plugins", "anthropic-marketplace",
    "anthropic-plugins", "agent-skills", "knowledge-work-plugins", "first-party-plugins",
}


def plugin_name_problems(name: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not name or _NAME_BAD.search(name):
        errors.append("name must be non-empty with no spaces, '@', ':', path separators or control characters")
        return errors, warnings
    low = re.sub(r"[-_.\s]+", "-", name.lower())
    words = set(low.split("-"))
    if low.startswith(_RESERVED_PREFIXES) or low in _RESERVED_NAMES or (
            "official" in words and words & {"claude", "anthropic"}):
        errors.append(f'Plugin name "{name}" is reserved: it passes as one of Anthropic\'s own')
    elif words & {"claude", "anthropic", "anthropics"}:
        warnings.append(f'Plugin name "{name}" reads as one of Anthropic\'s own')
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        warnings.append("name is not kebab-case")
    return errors, warnings


def validate_plugin_dir(d: Path) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a plugin directory: manifest fields, component paths (``./`` prefix,
    containment, existence), command maps, userConfig, hooks files, skill frontmatter."""
    errors: list[str] = []
    warnings: list[str] = []
    d = Path(d)
    mp = _manifest_path(d)
    manifest: dict[str, Any] = {}
    if mp is None:
        if not _has_standard_layout(d):
            return [f"{d}: no plugin manifest and no standard component folders"], warnings
        warnings.append("no plugin.json (components are loaded from the standard layout)")
    else:
        try:
            raw = json.loads(mp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            return [f"json: Invalid JSON syntax: {e}"], warnings
        if not isinstance(raw, dict):
            return ["manifest must be a JSON object"], warnings
        manifest = raw
        if mp.parent.name != ".claude-plugin":
            warnings.append("manifest is at the plugin root; the standard place is .claude-plugin/plugin.json")
    name = manifest.get("name")
    if mp is not None:
        if not isinstance(name, str) or not name:
            errors.append("name: required")
        else:
            e, w = plugin_name_problems(name)
            errors += [f"name: {x}" for x in e]
            warnings += [f"name: {x}" for x in w]
        for k in ("version", "description", "author"):
            if k not in manifest:
                warnings.append(f"{k}: recommended")
    for k in manifest:
        if k not in KNOWN_MANIFEST_KEYS:
            warnings.append(f"Unknown field '{k}' is ignored")
    homepage = manifest.get("homepage")
    if homepage is not None and not (isinstance(homepage, str) and re.match(r"^[a-z][a-z0-9+.-]*://\S+$", homepage)):
        errors.append("homepage: must be a URL")
    if "defaultEnabled" in manifest and not isinstance(manifest["defaultEnabled"], bool):
        errors.append("defaultEnabled: must be true or false")

    def check_path(key: str, rel: Any, must_be_md: bool = False, allow_dot: bool = False) -> None:
        if not isinstance(rel, str) or not rel:
            errors.append(f"{key}: path must be a non-empty string")
            return
        if allow_dot and rel in (".", "./"):
            return
        if not rel.startswith("./"):
            errors.append(f'{key}: path must start with "./" ({rel})')
        if ".." in Path(rel).parts:
            errors.append(f'{key}: Path contains ".." which could be a path traversal attempt ({rel})')
            return
        p = resolve_inside(d, rel)
        if p is None:
            errors.append(f"{key} path escapes plugin directory: {rel}")
        elif not p.exists():
            errors.append(f"{key} path not found: {rel}")
        elif must_be_md and p.is_file() and p.suffix.lower() != ".md":
            errors.append(f"{key}: {rel} must be a .md file")

    for key in ("skills", "agents", "outputStyles"):
        if key in manifest:
            for rel in _as_list(manifest[key]):
                check_path(key, rel, must_be_md=(key == "agents"), allow_dot=(key == "skills"))
    cmds = manifest.get("commands")
    if isinstance(cmds, dict):
        for cname, spec in cmds.items():
            if not isinstance(spec, dict):
                errors.append(f"commands.{cname}: must be an object")
                continue
            if ("source" in spec) == ("content" in spec):
                errors.append(f"commands.{cname}: set exactly one of source or content")
            elif "source" in spec:
                check_path(f"commands.{cname}.source", spec["source"])
    elif cmds is not None:
        for rel in _as_list(cmds):
            check_path("commands", rel)
    for key in ("hooks", "mcpServers"):
        for item in _as_list(manifest.get(key)):
            if isinstance(item, str):
                check_path(key, item)
                p = resolve_inside(d, item)
                if key == "hooks" and p is not None and p.is_file():
                    data = _read_json_dict(p)
                    if data is None:
                        errors.append(f"hooks: {item} is not valid JSON")
                    elif "hooks" not in data:
                        errors.append(f'hooks: {item} must wrap the event map in a top-level "hooks" key')
            elif not isinstance(item, dict):
                errors.append(f"{key}: must be a path or an object")
    uc = manifest.get("userConfig")
    if isinstance(uc, dict):
        allowed = {"type", "title", "description", "required", "default", "options", "multiple", "sensitive", "min", "max"}
        for k, spec in uc.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)):
                errors.append(f"userConfig.{k}: key must be letters, digits and underscores")
            if not isinstance(spec, dict):
                errors.append(f"userConfig.{k}: must be an object")
                continue
            for need in ("type", "title", "description"):
                if need not in spec:
                    errors.append(f"userConfig.{k}.{need}: required")
            if spec.get("type") not in (None, "string", "number", "boolean", "directory", "file"):
                errors.append(f"userConfig.{k}.type: invalid")
            for extra in set(spec) - allowed:
                errors.append(f"userConfig.{k}: unknown key '{extra}'")
    if (d / "CLAUDE.md").is_file():
        warnings.append("CLAUDE.md at the plugin root is not loaded as context; put the instructions in a skill")
    probe = Plugin(name=str(name or d.name), path=d, scope="user", manifest=manifest)
    for sf in plugin_skill_files(probe):
        try:
            label = str(sf.relative_to(d))
        except ValueError:
            label = str(sf)
        errors += [f"{label}: {p}" for p in validate_skill_file(sf)]
    return errors, warnings


def validate_skill_file(path: Path) -> list[str]:
    """Problems in one SKILL.md: frontmatter that does not open on line 1 / is not closed, bad name."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").removeprefix("﻿")
    except OSError as e:
        return [str(e)]
    problems: list[str] = []
    if not text.startswith("---"):
        if re.search(r"(?m)^(name|description):", text[:600]) and "\n---" in text[:2000]:
            problems.append("frontmatter must start on the first line of the file")
        return problems
    from axon.skills.manager import name_refused, parse_frontmatter
    fm, _ = parse_frontmatter(text)
    if not any(ln.strip() == "---" for ln in text.splitlines()[1:]):
        problems.append("frontmatter is not closed with ---")
    name = fm.get("name")
    if isinstance(name, str) and name_refused(name):
        problems.append("frontmatter name contains characters that are not allowed")
    return problems


def validate_path(path: Path) -> tuple[str, list[str], list[str]]:
    """Validate a marketplace, a plugin, or a skills directory. Returns (kind, errors, warnings)."""
    p = Path(path).expanduser()
    if p.is_file() and p.name == "SKILL.md":
        return "skill", validate_skill_file(p), []
    if (p / ".claude-plugin" / "marketplace.json").is_file() and not (p / ".claude-plugin" / "plugin.json").is_file():
        from axon.commands.marketplace import validate_marketplace
        e, w = validate_marketplace(p)
        return "marketplace", e, w
    if p.is_dir() and (_manifest_path(p) is not None or _has_standard_layout(p)):
        e, w = validate_plugin_dir(p)
        return "plugin", e, w
    if p.is_dir() and any(p.glob("*/SKILL.md")):
        errs: list[str] = []
        for sf in sorted(p.glob("*/SKILL.md")):
            errs += [f"{sf.parent.name}/SKILL.md: {x}" for x in validate_skill_file(sf)]
        return "skills", errs, []
    return "unknown", [f"{p}: not a plugin, marketplace or skills directory"], []


# ─── create / install / remove ──────────────────────────────────────────────

def create_plugin(name: str, root: Path) -> Path:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not clean:
        raise ValueError("Plugin name must contain letters or digits")
    d = root / clean
    if d.exists():
        raise FileExistsError(f"{d} already exists")
    (d / "commands").mkdir(parents=True)
    (d / "skills").mkdir()
    manifest = {"name": clean, "version": "0.1.0", "description": f"{clean} plugin for Axon"}
    (d / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (d / "commands" / "hello.md").write_text(
        "---\ndescription: Example command from the "
        f"{clean} plugin\nargument-hint: [name]\n---\nGreet $ARGUMENTS and summarise this repository in two sentences.\n",
        encoding="utf-8",
    )
    return d


def install_plugin(source: str, root: Path) -> Plugin:
    """Install from a local directory (copied) or an https git URL (shallow clone)."""
    root.mkdir(parents=True, exist_ok=True)
    src = Path(source).expanduser()
    if src.is_dir():
        mp = _manifest_path(src)
        if mp is None and not _has_standard_layout(src):
            raise ValueError(f"{src} has no plugin.json")
        manifest = json.loads(mp.read_text(encoding="utf-8-sig")) if mp else {}
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(manifest.get("name") or src.name)).strip("-")
        dest = root / name
        if dest.exists():
            raise FileExistsError(f"Plugin '{name}' already installed at {dest}")
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    else:
        parsed = urlparse(source)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in TRUSTED_GIT_HOSTS:
            raise ValueError(f"Only https git URLs on {', '.join(sorted(TRUSTED_GIT_HOSTS))} or local folders are supported")
        git = shutil.which("git")
        if not git:
            raise RuntimeError("git is required to install plugins from a URL")
        repo = Path(parsed.path).name.removesuffix(".git")
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo).strip("-")
        if not name:
            raise ValueError("Could not derive a plugin name from the URL")
        dest = root / name
        if dest.exists():
            raise FileExistsError(f"Plugin '{name}' already installed at {dest}")
        proc = subprocess.run([git, "clone", "--depth", "1", "--", source, str(dest)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            _rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        if _manifest_path(dest) is None and not _has_standard_layout(dest):
            _rmtree(dest, ignore_errors=True)
            raise ValueError("Repository has no plugin.json - not an Axon/Claude plugin")
    mp = _manifest_path(dest)
    manifest = json.loads(mp.read_text(encoding="utf-8-sig")) if mp else {}
    return Plugin(name=dest.name, path=dest, scope="user", version=str(manifest.get("version", "")),
                  description=str(manifest.get("description", "")), manifest=manifest)


def remove_plugin(plugin: Plugin) -> None:
    _rmtree(plugin.path)
