"""
Read-only access to the Claude-Code style ``settings.json`` layers for skills, plugins and
output styles (``skillOverrides``, ``disableBundledSkills``, ``enabledPlugins``, ``outputStyle`` ...).

Layers, lowest to highest precedence::

    ~/.claude/settings.json  ~/.axon/settings.json
    <ws>/.claude/settings.json  <ws>/.axon/settings.json
    <ws>/.claude/settings.local.json  <ws>/.axon/settings.local.json

Unreadable or malformed files are skipped (this never raises).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axon.storage import axon_home as _axon_home


def settings_files(workspace: Path | None) -> list[Path]:
    home = Path.home()
    files = [home / ".claude" / "settings.json", _axon_home() / "settings.json"]
    if workspace is not None:
        ws = Path(workspace)
        for base in (".claude", ".axon"):
            files.append(ws / base / "settings.json")
        for base in (".claude", ".axon"):
            files.append(ws / base / "settings.local.json")
    return files


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_setting(workspace: Path | None, key: str, default: Any = None) -> Any:
    """Value of ``key`` from the highest-precedence layer that sets it."""
    value = default
    for f in settings_files(workspace):
        data = read_json(f)
        if key in data:
            value = data[key]
    return value


def get_dict_setting(workspace: Path | None, key: str) -> dict[str, Any]:
    """Dict-valued setting merged across layers (later layers override per key)."""
    out: dict[str, Any] = {}
    for f in settings_files(workspace):
        v = read_json(f).get(key)
        if isinstance(v, dict):
            out.update(v)
    return out


def get_bool_setting(workspace: Path | None, key: str, default: bool = False) -> bool:
    v = get_setting(workspace, key, None)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "on")
    return default


SKILL_OVERRIDES = "skillOverrides"


def _axon_layer_files(workspace: Path | None) -> list[Path]:
    """The settings files Axon itself writes, lowest precedence first."""
    files = [_axon_home() / "settings.json"]
    if workspace is not None:
        base = Path(workspace) / ".axon"
        files += [base / "settings.json", base / "settings.local.json"]
    return files


def _write_overrides(path: Path, change: Any) -> None:
    from axon.session.store import atomic_write_text
    data = read_json(path)
    overrides = data.get(SKILL_OVERRIDES)
    overrides = dict(overrides) if isinstance(overrides, dict) else {}
    change(overrides)
    if overrides:
        data[SKILL_OVERRIDES] = overrides
    else:
        data.pop(SKILL_OVERRIDES, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def disable_skill(workspace: Path | None, name: str, scope: str = "user") -> Path:
    """Switch a skill off (``skillOverrides[name] = "off"``) for you (user) or for one project."""
    if scope == "user" or workspace is None:
        path = _axon_home() / "settings.json"
    else:
        path = Path(workspace) / ".axon" / "settings.json"

    def change(overrides: dict[str, Any]) -> None:
        overrides[name] = "off"

    _write_overrides(path, change)
    return path


def _override_of(path: Path, name: str) -> str:
    overrides = read_json(path).get(SKILL_OVERRIDES)
    if not isinstance(overrides, dict):
        return ""
    return str(overrides.get(name, "")).strip().lower()


def enable_skill(workspace: Path | None, name: str) -> list[Path]:
    """Switch a skill back on: remove its "off" from every Axon settings file that has one.
    If a Claude settings file still turns it off, an explicit "on" is written into the project's
    highest layer (settings.local.json), which beats it."""
    def remove(overrides: dict[str, Any]) -> None:
        overrides.pop(name, None)

    def force_on(overrides: dict[str, Any]) -> None:
        overrides[name] = "on"

    changed = []
    for path in _axon_layer_files(workspace):
        if _override_of(path, name) == "off":
            _write_overrides(path, remove)
            changed.append(path)
    if str(get_dict_setting(workspace, SKILL_OVERRIDES).get(name, "")).strip().lower() == "off":
        top_layer = _axon_layer_files(workspace)[-1]
        _write_overrides(top_layer, force_on)
        changed.append(top_layer)
    return changed
