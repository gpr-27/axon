"""
Export and import the whole connector / skill / plugin setup as one JSON file.

    /marketplace export [file] [--include-secrets]
    /marketplace import <file> [--dry-run] [--overwrite]

Secrets are left out of the export: values of keys, tokens and passwords become ``${NAME}`` references
and the file lists the names under ``needs_keys``. ``--include-secrets`` writes them as they are
(and makes the file owner-only). The import never installs the ``auto_install`` / ``auto_update`` switches
from a file.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.marketplace import automation, catalog, control, plugin_tools, skill_tools
from axon.mcp.config import MCPConfig, ServerSpec, valid_server_name

FORMAT = "axon-setup"
VERSION = 1
MAX_SKILL_FILE_BYTES = 100_000
MAX_SKILL_FILES = 20
SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
PluginInstaller = Callable[[catalog.PluginEntry, Path, str], Path]


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)      # connector names written or changed

    def add(self, text: str) -> None:
        self.lines.append(text)


# ─── export ──────────────────────────────────────────────────────────────────

def mask_connector(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Replace literal secrets in a raw server spec with ``${NAME}``; return the copy and the names."""
    masked = json.loads(json.dumps(raw))
    needs: list[str] = []
    env = masked.get("env")
    if isinstance(env, dict):
        for var, value in env.items():
            if control.is_secret_name(var) and "${" not in str(value):
                env[var] = control.reference_for(var)
                needs.append(var)
    headers = masked.get("headers")
    if isinstance(headers, dict):
        for header, value in headers.items():
            if control.is_secret_name(header) and "${" not in str(value):
                var = re.sub(r"[^A-Z0-9]+", "_", header.upper()).strip("_")
                prefix = "Bearer " if str(value).lower().startswith("bearer ") else ""
                headers[header] = prefix + control.reference_for(var)
                needs.append(var)
    return masked, needs


def export_connectors(workspace: Path, include_secrets: bool) -> list[dict[str, Any]]:
    config = MCPConfig(workspace)
    items = []
    for name in sorted(config.load_specs()):
        for scope, _path, raw in config.definitions(name):
            if scope not in control.OWN_SCOPES:
                continue
            masked, needs = (dict(raw), []) if include_secrets else mask_connector(raw)
            items.append({"name": name, "scope": scope, "config": masked, "needs_keys": needs})
    return items


def skill_files(folder: Path) -> dict[str, str]:
    """Text files of a skill folder (SKILL.md first), small ones only."""
    files: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if not path.is_file() or any(part.startswith(".") for part in rel.split("/")):
            continue
        if len(files) >= MAX_SKILL_FILES or path.stat().st_size > MAX_SKILL_FILE_BYTES:
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def export_skills(workspace: Path) -> list[dict[str, Any]]:
    items = []
    for name, (scope, skill_file) in sorted(skill_tools.installed_skills(workspace).items()):
        origin = skill_tools.read_origin(skill_file.parent)
        items.append({"name": name, "scope": scope, "source": origin.get("source", ""),
                      "files": skill_files(skill_file.parent)})
    return items


def export_plugins(workspace: Path) -> list[dict[str, Any]]:
    items = []
    for name, plugin in sorted(plugin_tools.installed_plugins(workspace).items()):
        items.append({"name": name, "scope": plugin.scope, "enabled": plugin.enabled,
                      "marketplace": plugin.marketplace, "source": plugin.record.get("source", {})})
    return items


def export_permissions(workspace: Path) -> dict[str, list[str]]:
    engine = control.permission_engine(workspace)
    rules: dict[str, list[str]] = {kind: [] for kind in control.POLICIES}
    for entry in engine.list_rules():
        rule = str(entry.rule)
        if rule.startswith("mcp__") and rule not in rules[entry.kind]:
            rules[entry.kind].append(rule)
    return rules


def disabled_skill_names(workspace: Path) -> list[str]:
    from axon.skills.manager import SkillManager
    return sorted(SkillManager(workspace).disabled)


def export_setup(workspace: Path, include_secrets: bool = False) -> dict[str, Any]:
    return {
        "format": FORMAT, "version": VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "includes_secrets": include_secrets,
        "connectors": export_connectors(workspace, include_secrets),
        "skills": export_skills(workspace),
        "disabled_skills": disabled_skill_names(workspace),
        "plugins": export_plugins(workspace),
        "permissions": export_permissions(workspace),
        "automation": asdict(automation.load_automation(workspace)),
    }


def write_export(workspace: Path, path: Path, include_secrets: bool = False) -> Path:
    from axon.session.store import atomic_write_text
    data = export_setup(workspace, include_secrets)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    if include_secrets:
        path.chmod(0o600)
    return path


# ─── import ──────────────────────────────────────────────────────────────────

def read_setup(path: Path) -> dict[str, Any]:
    """Load and check an export file. Raises ValueError with a plain message."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{path} is not an Axon setup export (format must be '{FORMAT}').")
    if data.get("version") != VERSION:
        raise ValueError(f"unsupported export version {data.get('version')!r}; this Axon reads version {VERSION}.")
    return data


def import_connectors(workspace: Path, items: list[Any], report: Report, overwrite: bool, dry_run: bool) -> None:
    config = MCPConfig(workspace)
    existing = config.load_specs()
    for item in items:
        name = str(item.get("name", "")) if isinstance(item, dict) else ""
        try:
            if not valid_server_name(name):
                raise ValueError("not a valid server name (letters, digits, - and _)")
            spec = ServerSpec.from_dict(name, item["config"], control.normalize_scope(item.get("scope", "user")))
        except (KeyError, ValueError, TypeError) as e:
            report.add(f"  skipped connector '{name or '?'}': {e}")
            continue
        current = existing.get(name)
        if current is not None and current.endpoint_key() == spec.endpoint_key() and not overwrite:
            report.add(f"  connector '{name}': already installed")
            continue
        if current is not None and not overwrite:
            report.add(f"  connector '{name}': a different server uses this name (use --overwrite to replace it)")
            continue
        blocked = config.add_block_reason(spec)
        if blocked:
            report.add(f"  connector '{name}': {blocked}")
            continue
        if not dry_run:
            config.save_spec(spec, spec.scope)
            report.changed.append(name)
        report.add(f"  connector '{name}': {'would add' if dry_run else 'added'} ({spec.scope} scope)")
        for var in item.get("needs_keys", []):
            report.add(f"      needs a key: /marketplace env {name} {var}=<value>")


def safe_files(files: Any) -> dict[str, str]:
    if not isinstance(files, dict):
        return {}
    return {rel: text for rel, text in files.items()
            if isinstance(rel, str) and isinstance(text, str) and SAFE_RELATIVE.match(rel) and ".." not in rel.split("/")}


def import_skills(workspace: Path, items: list[Any], report: Report, overwrite: bool, dry_run: bool) -> None:
    for item in items:
        name = str(item.get("name", "")) if isinstance(item, dict) else ""
        files = safe_files(item.get("files")) if isinstance(item, dict) else {}
        if not skill_tools.NAME_RE.match(name) or "SKILL.md" not in files:
            report.add(f"  skipped skill '{name or '?'}': no valid name or SKILL.md")
            continue
        scope = "project" if item.get("scope") == "project" else "user"
        folder = skill_tools.skills_root(workspace, scope) / name
        if (folder / "SKILL.md").exists() and not overwrite:
            report.add(f"  skill '{name}': already installed")
            continue
        report.add(f"  skill '{name}': {'would add' if dry_run else 'added'} ({scope} scope)")
        if not dry_run:
            write_skill_folder(folder, files, report, name)


def write_skill_folder(folder: Path, files: dict[str, str], report: Report, name: str) -> None:
    for rel, text in files.items():
        target = folder / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    problems = skill_tools.validate_skill_file(folder / "SKILL.md")
    if problems:
        report.add(f"      warning: '{name}' did not validate: {'; '.join(problems)}")


def import_disabled_skills(workspace: Path, names: Any, report: Report, dry_run: bool) -> None:
    for name in names if isinstance(names, list) else []:
        if not isinstance(name, str):
            continue
        if dry_run:
            report.add(f"  skill '{name}': would be disabled")
            continue
        result = control.set_skill_enabled(workspace, name, False)
        report.add(f"  skill '{name}': {'disabled' if result.ok else result.message}")


def import_plugins(workspace: Path, items: list[Any], report: Report, dry_run: bool,
                   installer: PluginInstaller) -> None:
    known = {entry.name: entry for entry in catalog.load_plugins()}
    installed = plugin_tools.installed_plugins(workspace)
    for item in items:
        name = str(item.get("name", "")) if isinstance(item, dict) else ""
        if name in installed:
            report.add(f"  plugin '{name}': already installed")
        elif name in known and item.get("marketplace") == plugin_tools.MARKETPLACE_NAME:
            report.add(f"  plugin '{name}': {'would install' if dry_run else 'installing'} from the catalog")
            if not dry_run:
                install_catalog_plugin(workspace, known[name], item, report, installer)
        else:
            report.add(f"  plugin '{name}': install it yourself: /plugin install {name}@{item.get('marketplace', '')}")


def install_catalog_plugin(workspace: Path, entry: catalog.PluginEntry, item: dict[str, Any], report: Report,
                           installer: PluginInstaller) -> None:
    try:
        installer(entry, workspace, str(item.get("scope", "user")))
    except Exception as e:  # noqa: BLE001 - one broken plugin must not stop the import
        report.add(f"      could not install '{entry.name}': {e}")
        return
    if item.get("enabled") is False:
        plugin_tools.set_plugin_enabled(entry.name, workspace, False)


def import_permissions(workspace: Path, rules: Any, report: Report, dry_run: bool) -> None:
    if not isinstance(rules, dict):
        return
    path = control.settings_file(workspace, user_wide=False)
    for kind in control.POLICIES:
        for rule in rules.get(kind, []):
            if not isinstance(rule, str) or not rule.startswith("mcp__"):
                continue
            if not dry_run:
                try:
                    control.write_rule(path, kind, rule)
                except ValueError as e:
                    report.add(f"  permission: {kind} {rule} not saved: {e}")
                    continue
            report.add(f"  permission: {kind} {rule}")


def import_automation(block: Any, report: Report, dry_run: bool) -> None:
    if not isinstance(block, dict):
        return
    keep = {key: value for key, value in block.items()
            if key in automation.setting_names() + ["enabled"] and key not in automation.USER_ONLY_KEYS}
    if any(key in block for key in automation.USER_ONLY_KEYS):
        report.add("  automation: auto_install and auto_update are never imported; set them yourself "
                   "with /marketplace auto set")
    if keep and not dry_run:
        try:
            automation.save_automation(keep)
        except ValueError as e:
            report.add(f"  automation: not imported ({e})")
            return
    if keep:
        report.add(f"  automation: {'would set' if dry_run else 'set'} {', '.join(sorted(keep))}")


def import_setup(workspace: Path, data: dict[str, Any], *, overwrite: bool = False, dry_run: bool = False,
                 plugin_installer: PluginInstaller = plugin_tools.install_plugin) -> Report:
    report = Report()
    import_connectors(workspace, list(data.get("connectors", [])), report, overwrite, dry_run)
    import_skills(workspace, list(data.get("skills", [])), report, overwrite, dry_run)
    import_disabled_skills(workspace, data.get("disabled_skills", []), report, dry_run)
    import_plugins(workspace, list(data.get("plugins", [])), report, dry_run, plugin_installer)
    import_permissions(workspace, data.get("permissions"), report, dry_run)
    import_automation(data.get("automation"), report, dry_run)
    return report
