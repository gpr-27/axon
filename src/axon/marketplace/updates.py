"""
Update check for what you installed from the marketplace. It reports; it changes nothing unless you
run ``/marketplace update <id>`` or have turned on ``automation.auto_update`` (skills and plugins only).

    connectors  the catalog lists a different address than the one you installed (offline check)
    skills      the SKILL.md on GitHub differs from the copy you installed
    plugins     a plugin marketplace you added lists a newer version

``automation.check_updates`` runs the check in the background at most every
``automation.update_interval_days`` days (state: ``~/.axon/state/update-check.json``).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from axon.marketplace import catalog, plugin_tools, skill_tools
from axon.mcp.config import MCPConfig
from axon.storage import read_state, write_state

STATE_NAME = "update-check"
MAX_SKILLS_CHECKED = 10
SECONDS_PER_DAY = 86400.0
Fetcher = Callable[[str], str]


@dataclass
class Update:
    kind: str          # connector | skill | plugin
    name: str
    message: str

    def line(self) -> str:
        return f"{self.kind} {self.name}: {self.message}"


def default_fetcher(source: str) -> str:
    from axon.skills.importer import fetch_skill_text
    return fetch_skill_text(source)


def connector_updates(workspace: Path, data_dir: Path = catalog.DATA_DIR) -> list[Update]:
    specs = MCPConfig(workspace).load_specs()
    updates = []
    for entry in catalog.load_connectors(data_dir):
        spec = specs.get(entry.id)
        if spec is None or spec.scope == "plugin":
            continue
        wanted = entry.url if entry.remote else entry.command
        have = spec.url if spec.remote else spec.command
        if wanted and have and wanted != have:
            updates.append(Update("connector", entry.id, f"the catalog now lists {wanted} (you use {have}). "
                                  f"Review with /marketplace info {entry.id}; to switch, remove and install it again."))
    return updates


def skill_updates(workspace: Path, fetch: Fetcher) -> list[Update]:
    updates = []
    checked = 0
    for name, (_scope, skill_file) in sorted(skill_tools.installed_skills(workspace).items()):
        origin = skill_tools.read_origin(skill_file.parent)
        source = str(origin.get("source", ""))
        if not source or checked >= MAX_SKILLS_CHECKED:
            continue
        checked += 1
        try:
            latest = fetch(source)
        except Exception:  # noqa: BLE001 - offline or moved: say nothing rather than guess
            continue
        if skill_tools.text_hash(latest) != origin.get("sha256"):
            updates.append(Update("skill", name, f"a newer version is on GitHub. Update: /marketplace update {name}"))
    return updates


def plugin_updates(workspace: Path) -> list[Update]:
    from axon.commands import marketplace as plugin_market
    updates = []
    for name, plugin in sorted(plugin_tools.installed_plugins(workspace).items()):
        if not plugin.marketplace or plugin.marketplace == plugin_tools.MARKETPLACE_NAME:
            continue
        try:
            _market, entry = plugin_market.find_plugin_entry(name, plugin.marketplace)
        except plugin_market.MarketplaceError:
            continue
        latest = str(entry.get("version") or "")
        if latest and plugin.version and latest != plugin.version:
            updates.append(Update("plugin", name, f"version {latest} is available (you have {plugin.version}). "
                                  f"Update: /marketplace update {name}"))
    return updates


def check_updates(workspace: Path, fetch: Fetcher = default_fetcher,
                  data_dir: Path = catalog.DATA_DIR) -> list[Update]:
    return connector_updates(workspace, data_dir) + skill_updates(workspace, fetch) + plugin_updates(workspace)


# ─── applying (only on request or with auto_update) ──────────────────────────

def apply_skill_update(workspace: Path, name: str, fetch: Fetcher = default_fetcher) -> str:
    """Replace an installed skill with the latest copy from its source. Returns a message."""
    found = skill_tools.installed_skills(workspace).get(name)
    if found is None:
        raise ValueError(f"skill '{name}' is not installed")
    _scope, skill_file = found
    source = str(skill_tools.read_origin(skill_file.parent).get("source", ""))
    if not source:
        raise ValueError(f"skill '{name}' was not installed from the marketplace, so there is nothing to update")
    text = fetch(source)
    skill_file.write_text(text, encoding="utf-8")
    skill_tools.write_origin(skill_file.parent, source, text)
    return f"Updated skill '{name}'."


def apply_plugin_update(workspace: Path, name: str) -> str:
    from axon.commands import marketplace as plugin_market
    plugin = plugin_tools.installed_plugins(workspace).get(name)
    if plugin is None:
        raise ValueError(f"plugin '{name}' is not installed")
    changed = plugin_market.update_plugin(plugin, workspace)
    return f"Updated plugin '{name}'." if changed else f"Plugin '{name}' is already up to date."


def apply_updates(workspace: Path, updates: list[Update], fetch: Fetcher = default_fetcher) -> list[str]:
    """Apply the skill and plugin updates in the list (connectors are never changed this way)."""
    messages = []
    for update in updates:
        try:
            if update.kind == "skill":
                messages.append(apply_skill_update(workspace, update.name, fetch))
            elif update.kind == "plugin":
                messages.append(apply_plugin_update(workspace, update.name))
        except Exception as e:  # noqa: BLE001 - one failed update must not stop the others
            messages.append(f"Could not update {update.kind} '{update.name}': {e}")
    return messages


# ─── when to check ───────────────────────────────────────────────────────────

def last_checked() -> float:
    state = read_state(STATE_NAME, {})
    value = state.get("last_checked") if isinstance(state, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def is_due(interval_days: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - last_checked() >= max(interval_days, 0.0) * SECONDS_PER_DAY


def mark_checked(updates: list[Update]) -> None:
    write_state(STATE_NAME, {"last_checked": time.time(), "found": [u.line() for u in updates]})


def run_if_due(workspace: Path, interval_days: float, auto_update: bool, post: Callable[[str], None],
               fetch: Fetcher = default_fetcher) -> list[Update]:
    """The periodic check: report what is new and, only with auto_update, apply skills and plugins."""
    if not is_due(interval_days):
        return []
    updates = check_updates(workspace, fetch)
    mark_checked(updates)
    if not updates:
        return []
    if auto_update:
        for message in apply_updates(workspace, updates, fetch):
            post(message)
    reported = [u for u in updates if not auto_update or u.kind == "connector"]
    if reported:
        post(f"{len(reported)} update(s) available: " + "; ".join(u.line() for u in reported[:3])
             + (" ... see /marketplace updates" if len(reported) > 3 else ""))
    return updates
