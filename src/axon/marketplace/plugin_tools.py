"""
Plugins for the marketplace: scaffold a new plugin, install one from a git repository, list, remove.

Installing reuses the plugin folder layout and the ``.axon-install.json`` record that
``axon.commands.marketplace`` writes, so ``/plugin`` sees these plugins like any other.
"""
from __future__ import annotations

import json
import shutil
from axon.platform import rmtree as _rmtree
import tempfile
import time
from pathlib import Path

from axon.commands import marketplace as plugin_market
from axon.commands import plugins as plugin_core
from axon.marketplace.catalog import PluginEntry

MARKETPLACE_NAME = "axon-marketplace"


class PluginInstallError(RuntimeError):
    """Raised when a plugin cannot be installed."""


def create_new_plugin(name: str, workspace: Path, scope: str = "user") -> Path:
    """Scaffold a plugin folder (manifest, example command, skills folder) and validate it."""
    root = plugin_market.install_root(workspace, "user" if scope == "user" else "project")
    root.mkdir(parents=True, exist_ok=True)
    folder = plugin_core.create_plugin(name, root)
    errors, _warnings = plugin_core.validate_plugin_dir(folder)
    if errors:
        raise ValueError("The plugin was written but failed validation: " + "; ".join(errors))
    return folder


def install_plugin(entry: PluginEntry, workspace: Path, scope: str = "user", review=None) -> Path:
    """Fetch the plugin from its git source, copy it into the plugins folder and enable it.
    ``review(lines) -> bool`` sees what the plugin would run (hooks, MCP commands) before it is copied in."""
    root = plugin_market.install_root(workspace, "user" if scope == "user" else "project")
    destination = root / entry.name
    if destination.exists():
        raise PluginInstallError(f"Plugin '{entry.name}' is already installed at {destination}")
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="axon-mkt-plugin-"))
    try:
        source_dir = _fetch(entry, work)
        _check_safe(source_dir)
        commit = plugin_market.read_commit(work)
        lines = plugin_market.install_review_lines(entry.id, plugin_market.plugin_runs(source_dir, workspace, entry.name), commit)
        if review is not None and not review(lines):
            raise PluginInstallError("installation cancelled; nothing was installed")
        from axon.hooks import trust
        was_ok = scope != "user" and trust.status(workspace) in ("none", "trusted")
        shutil.copytree(source_dir, destination)
    finally:
        _rmtree(work, ignore_errors=True)
    _write_install_record(entry, destination, scope, commit)
    if was_ok:
        trust.trust(workspace)
    try:
        plugin_core.set_enabled_in_settings(workspace, f"{entry.name}@{MARKETPLACE_NAME}", True, scope)
    except ValueError as e:  # settings.json cannot be updated: leave nothing half-installed
        _rmtree(destination, ignore_errors=True)
        raise PluginInstallError(str(e)) from e
    return destination


def _fetch(entry: PluginEntry, work: Path) -> Path:
    plugin_entry = {"name": entry.name, "source": entry.source}
    try:
        return plugin_market.materialize(MARKETPLACE_NAME, plugin_entry, work)
    except plugin_market.MarketplaceError as e:
        raise PluginInstallError(str(e)) from e


def _check_safe(source_dir: Path) -> None:
    errors, _warnings = plugin_core.validate_plugin_dir(source_dir)
    for error in errors:
        if "path escapes" in error or "traversal" in error:
            raise PluginInstallError("Plugin rejected: " + error)


def _write_install_record(entry: PluginEntry, destination: Path, scope: str, commit: str = "") -> None:
    record = {"name": entry.name, "marketplace": MARKETPLACE_NAME, "scope": scope,
              "source": entry.source, "installed_at": int(time.time()), "commit": commit,
              "content_hash": plugin_market.content_hash(destination)}
    text = json.dumps(record, indent=2) + "\n"
    (destination / plugin_core.INSTALL_RECORD).write_text(text, encoding="utf-8")


def installed_plugins(workspace: Path) -> dict[str, plugin_core.Plugin]:
    """All installed plugins by name."""
    return {plugin.name: plugin for plugin in plugin_core.load_plugins(workspace)}


def remove_plugin(name: str, workspace: Path) -> bool:
    plugin = installed_plugins(workspace).get(name)
    if plugin is None:
        return False
    plugin_market.uninstall(plugin, workspace)
    return True


def set_plugin_enabled(name: str, workspace: Path, enabled: bool) -> bool:
    if name not in installed_plugins(workspace):
        return False
    plugin_core.set_enabled(name, enabled, workspace)
    return True
