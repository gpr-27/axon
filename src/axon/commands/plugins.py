"""
Plugins: a directory with a ``plugin.json`` manifest (or ``.claude-plugin/plugin.json``)
that bundles any of:

  commands/   markdown slash commands      -> /<plugin>:<name>    (loaded by axon.commands.custom)
  skills/     <skill>/SKILL.md folders                             (loaded by axon.skills.manager)
  agents/     markdown subagent definitions                         (loaded by axon.agent.subagent)
  hooks       ``hooks/hooks.json`` or manifest "hooks"             (merged by axon.hooks.manager)
  MCP         ``.mcp.json`` or manifest "mcpServers"               (merged by axon.mcp.config)

``${AXON_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_ROOT}`` in hook commands and MCP server specs expand
to the plugin's directory.

Roots: ~/.axon/plugins/<name>/ (user) and <ws>/.axon/plugins/<name>/ (project).
Disabled plugins are listed in ~/.axon/plugins/disabled.json.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from axon.storage import axon_home as _axon_home

TRUSTED_GIT_HOSTS = {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}


@dataclass
class Plugin:
    name: str
    path: Path
    scope: str  # "user" | "project"
    version: str = ""
    description: str = ""
    enabled: bool = True
    manifest: dict[str, Any] = field(default_factory=dict)

    def components(self) -> dict[str, int]:
        p = self.path
        out: dict[str, int] = {}
        if (p / "commands").is_dir():
            out["commands"] = sum(1 for _ in (p / "commands").rglob("*.md"))
        if (p / "skills").is_dir():
            out["skills"] = sum(1 for _ in (p / "skills").glob("*/SKILL.md"))
        if (p / "agents").is_dir():
            out["agents"] = sum(1 for _ in (p / "agents").glob("*.md"))
        if (p / "hooks" / "hooks.json").is_file() or self.manifest.get("hooks"):
            out["hooks"] = 1
        if (p / ".mcp.json").is_file() or self.manifest.get("mcpServers"):
            out["mcp"] = 1
        return {k: v for k, v in out.items() if v}


def plugin_roots(workspace: Path) -> list[tuple[Path, str]]:
    return [(_axon_home() / "plugins", "user"), (Path(workspace) / ".axon" / "plugins", "project")]


def _manifest_path(d: Path) -> Path | None:
    for cand in (d / "plugin.json", d / ".claude-plugin" / "plugin.json"):
        if cand.is_file():
            return cand
    return None


def _disabled_file() -> Path:
    return _axon_home() / "plugins" / "disabled.json"


def _disabled() -> set[str]:
    try:
        data = json.loads(_disabled_file().read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def set_enabled(name: str, enabled: bool) -> None:
    from axon.session.store import atomic_write_text
    d = _disabled()
    if enabled:
        d.discard(name)
    else:
        d.add(name)
    atomic_write_text(_disabled_file(), json.dumps(sorted(d), indent=2) + "\n")


def enabled_plugins_setting(workspace: Path) -> dict[str, bool]:
    """``enabledPlugins`` from user < project < local settings.json (Claude-Code key; keys may be
    ``name`` or ``name@marketplace``)."""
    out: dict[str, bool] = {}
    for path in (_axon_home() / "settings.json", Path(workspace) / ".axon" / "settings.json",
                 Path(workspace) / ".axon" / "settings.local.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ep = data.get("enabledPlugins") if isinstance(data, dict) else None
        if isinstance(ep, dict):
            for k, v in ep.items():
                out[str(k).split("@", 1)[0]] = bool(v)
    return out


def load_plugins(workspace: Path) -> list[Plugin]:
    """All installed plugins; a project plugin overrides a user plugin of the same name.
    A plugin is enabled unless /plugin disable'd or ``enabledPlugins: {name: false}`` in settings."""
    disabled = _disabled()
    setting = enabled_plugins_setting(workspace)
    found: dict[str, Plugin] = {}
    for root, scope in plugin_roots(workspace):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            mp = _manifest_path(d)
            if mp is None:
                continue
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    continue
            except (OSError, ValueError):
                continue
            name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(manifest.get("name") or d.name)).strip("-") or d.name
            found[name] = Plugin(
                name=name, path=d, scope=scope, version=str(manifest.get("version", "")),
                description=str(manifest.get("description", "")), enabled=name not in disabled and setting.get(name, True), manifest=manifest,
            )
    return sorted(found.values(), key=lambda p: p.name)


def plugin_dirs(workspace: Path) -> list[Path]:
    """Directories of enabled plugins (for loading agents/, hooks and MCP configs)."""
    return [p.path for p in load_plugins(workspace) if p.enabled]


_ROOT_VARS = ("${AXON_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}", "$AXON_PLUGIN_ROOT", "$CLAUDE_PLUGIN_ROOT")


def expand_plugin_root(value: Any, root: Path) -> Any:
    """Recursively replace ``${AXON_PLUGIN_ROOT}``/``${CLAUDE_PLUGIN_ROOT}`` in strings of ``value``."""
    if isinstance(value, str):
        for var in _ROOT_VARS:
            value = value.replace(var, str(root))
        return value
    if isinstance(value, list):
        return [expand_plugin_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: expand_plugin_root(v, root) for k, v in value.items()}
    return value


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _component(plugin: Plugin, default_rel: str, key: str) -> dict[str, Any]:
    """A plugin component config: the manifest value (inline dict or relative path), else the default file."""
    val = plugin.manifest.get(key)
    if isinstance(val, dict):
        return val
    rel = val if isinstance(val, str) and val.strip() else default_rel
    path = (plugin.path / rel).resolve()
    try:
        path.relative_to(plugin.path.resolve())
    except ValueError:
        return {}
    return _read_json_dict(path) or {}


def plugin_hooks(plugin: Plugin) -> dict[str, Any]:
    """Event -> groups mapping from ``hooks/hooks.json`` (``{"hooks": {...}}`` or a bare event map)."""
    data = _component(plugin, "hooks/hooks.json", "hooks")
    section = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    return expand_plugin_root(section, plugin.path) if isinstance(section, dict) else {}


def plugin_mcp_servers(plugin: Plugin) -> dict[str, dict[str, Any]]:
    """Server specs from ``.mcp.json`` (``{"mcpServers": {...}}`` or a bare name -> spec map)."""
    data = _component(plugin, ".mcp.json", "mcpServers")
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else data
    if not isinstance(servers, dict):
        return {}
    return {str(k): expand_plugin_root(v, plugin.path) for k, v in servers.items() if isinstance(v, dict)}


def enabled_plugins(workspace: Path) -> list[Plugin]:
    try:
        return [p for p in load_plugins(workspace) if p.enabled]
    except OSError:
        return []


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
        if mp is None:
            raise ValueError(f"{src} has no plugin.json")
        manifest = json.loads(mp.read_text(encoding="utf-8"))
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
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        if _manifest_path(dest) is None:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError("Repository has no plugin.json - not an Axon/Claude plugin")
    mp = _manifest_path(dest)
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp else {}
    return Plugin(name=dest.name, path=dest, scope="user", version=str(manifest.get("version", "")),
                  description=str(manifest.get("description", "")), manifest=manifest)


def remove_plugin(plugin: Plugin) -> None:
    shutil.rmtree(plugin.path)
