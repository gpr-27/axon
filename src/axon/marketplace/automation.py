"""
The ``automation`` settings block: what Axon may do for you without being asked.

Everything is off until you turn it on (``/marketplace auto on``). The block lives in
``~/.axon/settings.json`` (or ``[automation]`` in ``~/.axon/config.toml``); a project's
``.axon/settings.json`` may change the harmless keys, but never ``auto_install`` or ``auto_update``,
so a repository you cloned cannot install things for you.

    "automation": {
      "enabled": true,            master switch
      "auto_connect": true,       connect enabled MCP servers in the background at startup
      "startup_wait": 2,          seconds the prompt waits for servers before it appears
      "connect_retries": 3,       extra attempts, with growing pauses, when the first connect fails
      "auto_reconnect": true,     reconnect a server that drops mid-session (local ones too)
      "auto_disable_after": 3,    switch a server off after this many failures in a row (0 = never)
      "auto_skills": true,        add the skills that match your prompt to that prompt
      "max_auto_skills": 2,
      "suggest": true,            suggest connectors that fit the project, once per project
      "auto_install": false,      install suggested connectors that need no key you do not have yet
      "check_updates": true,      report catalog / skill / plugin updates now and then
      "update_interval_days": 7,
      "auto_update": false        apply updated skills and plugins (never connectors)
    }
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from axon.storage import axon_home

KEY = "automation"
USER_ONLY_KEYS = ("auto_install", "auto_update")


@dataclass
class Automation:
    enabled: bool = False
    auto_connect: bool = True
    startup_wait: float = 2.0
    connect_retries: int = 3
    auto_reconnect: bool = True
    auto_disable_after: int = 3
    auto_skills: bool = True
    max_auto_skills: int = 2
    suggest: bool = True
    auto_install: bool = False
    check_updates: bool = True
    update_interval_days: float = 7.0
    auto_update: bool = False

    def on(self, feature: str) -> bool:
        """True when the master switch and the named feature are both on."""
        return self.enabled and bool(getattr(self, feature))


DEFAULTS = Automation()
FEATURE_NOTES = {
    "auto_connect": "connect enabled MCP servers in the background at startup",
    "startup_wait": "seconds the first prompt waits for servers",
    "connect_retries": "extra connect attempts with growing pauses",
    "auto_reconnect": "reconnect servers that drop mid-session",
    "auto_disable_after": "switch a server off after N failures in a row (0 = never)",
    "auto_skills": "add skills that match your prompt",
    "max_auto_skills": "most skills added to one prompt",
    "suggest": "suggest connectors for the project, once per project",
    "auto_install": "install suggested connectors that need nothing more from you",
    "check_updates": "report catalog, skill and plugin updates",
    "update_interval_days": "days between update checks",
    "auto_update": "apply skill and plugin updates (never connectors)",
}


def user_settings_path() -> Path:
    return axon_home() / "settings.json"


def project_settings_paths(workspace: Path) -> list[Path]:
    base = Path(workspace) / ".axon"
    return [base / "settings.json", base / "settings.local.json"]


def _read_json_block(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    block = data.get(KEY) if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def _read_toml_block(path: Path) -> dict[str, Any]:
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # noqa: BLE001 - a broken config file must never stop startup
        return {}
    block = data.get(KEY)
    return block if isinstance(block, dict) else {}


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Convert a stored value to the type of its default; None when it does not fit."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(default, int):
        return max(0, int(value))
    return max(0.0, float(value))


def _apply(result: Automation, block: dict[str, Any], user_level: bool) -> None:
    for item in fields(Automation):
        if item.name not in block:
            continue
        if item.name in USER_ONLY_KEYS and not user_level:
            continue
        value = _coerce(item.name, block[item.name], getattr(DEFAULTS, item.name))
        if value is not None:
            setattr(result, item.name, value)


def load_automation(workspace: Path | None = None) -> Automation:
    """The effective settings. Never raises: unreadable files are skipped."""
    result = Automation()
    _apply(result, _read_toml_block(axon_home() / "config.toml"), user_level=True)
    _apply(result, _read_json_block(user_settings_path()), user_level=True)
    if workspace is not None:
        _apply(result, _read_toml_block(Path(workspace) / ".axon" / "config.toml"), user_level=False)
        for path in project_settings_paths(workspace):
            _apply(result, _read_json_block(path), user_level=False)
    return result


def save_automation(changes: dict[str, Any]) -> Path:
    """Write the given keys into the user's settings.json, keeping everything else in the file."""
    from axon.session.store import atomic_write_text
    path = user_settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    block = data.get(KEY) if isinstance(data.get(KEY), dict) else {}
    block.update(changes)
    data[KEY] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return path


def parse_value(name: str, text: str) -> Any:
    """Turn the text of ``/marketplace auto set <name> <value>`` into the right type."""
    default = getattr(DEFAULTS, name)
    word = text.strip().lower()
    if isinstance(default, bool):
        if word in ("on", "true", "yes", "1"):
            return True
        if word in ("off", "false", "no", "0"):
            return False
        raise ValueError(f"{name} must be on or off")
    try:
        number = float(word)
    except ValueError as e:
        raise ValueError(f"{name} must be a number") from e
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return int(number) if isinstance(default, int) else number


def setting_names() -> list[str]:
    return [item.name for item in fields(Automation) if item.name != "enabled"]


def status_lines(workspace: Path | None = None) -> list[str]:
    """A readable table of every setting, its value and what it does."""
    current = load_automation(workspace)
    lines = [f"  Automation is {'ON' if current.enabled else 'OFF'}."
             + ("" if current.enabled else "  Turn it on with: /marketplace auto on")]
    values = asdict(current)
    for name in setting_names():
        shown = values[name]
        if isinstance(shown, bool):
            shown = "on" if shown else "off"
        active = current.enabled and (not isinstance(getattr(current, name), bool) or getattr(current, name))
        mark = "*" if active else " "
        lines.append(f"  {mark} {name:<22}{str(shown):<8}{FEATURE_NOTES[name]}")
    lines.append(f"  Settings file: {user_settings_path()}")
    return lines
