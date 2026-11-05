"""
The status dashboard: every connector, skill and plugin on one screen.

    /marketplace status [--check] [--all] [--json]

For each item: enabled, connected, number of tools, the last error and where it is configured.
Connected and tool counts come from the running session. Outside a session (``axon marketplace
status``) nothing is running, so those columns show ``-`` unless you pass ``--check``, which connects
to every enabled server once (with a time limit) and reports what it found.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from axon.marketplace import installer, plugin_tools
from axon.mcp import health as mcp_health
from axon.mcp.config import MCPConfig, ServerSpec

NOT_APPLICABLE = "n/a"
NOT_KNOWN = "-"


@dataclass
class StatusRow:
    kind: str                     # connector | skill | plugin
    name: str
    enabled: bool
    connected: str = NOT_APPLICABLE   # yes | no | - (not running) | n/a
    tools: str = NOT_APPLICABLE
    last_error: str = ""
    scope: str = ""


def live_connections() -> dict[str, Any]:
    """The connections of the running session's bridge (empty outside a session)."""
    from axon.mcp import bridge
    current = getattr(bridge, "_bridge", None)
    return dict(current.connections) if current is not None else {}


def checked_connections(specs: dict[str, ServerSpec], workspace: Path, timeout: float,
                        health_check: installer.HealthCheck) -> dict[str, installer.HealthResult]:
    """Connect to every enabled server at the same time and return what each one said."""
    results: dict[str, installer.HealthResult] = {}

    def check(spec: ServerSpec) -> None:
        results[spec.name] = health_check(spec, workspace, timeout)

    threads = [threading.Thread(target=check, args=(spec,), daemon=True) for spec in specs.values()
               if not spec.disabled]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + installer.THREAD_GRACE_SECONDS + 1)
    return results


def connector_row(spec: ServerSpec, live: Any, checked: installer.HealthResult | None) -> StatusRow:
    record = mcp_health.get(spec.name)
    row = StatusRow("connector", spec.name, not spec.disabled, scope=spec.scope, last_error=record.last_error)
    if spec.disabled:
        row.connected = "no"
        row.last_error = "switched off after repeated failures" if record.auto_disabled else row.last_error
        return row
    if live is not None:
        row.connected = "yes" if live.connected else "no"
        row.tools = str(len(live.tools))
        row.last_error = live.error or ("" if live.connected else row.last_error)
    elif checked is not None:
        row.connected = "yes" if checked.status == "connected" else "no"
        row.tools = str(len(checked.tools))
        row.last_error = "" if checked.status == "connected" else (checked.error or checked.status)
    else:
        row.connected, row.tools = NOT_KNOWN, NOT_KNOWN
    return row


def connector_rows(workspace: Path, check: bool, timeout: float, health_check: installer.HealthCheck) -> list[StatusRow]:
    specs = MCPConfig(workspace).load_specs()
    live = live_connections()
    # --check probes every server the running session is not already connected to (a leftover or
    # partial live bridge must not turn the check into a table of "-")
    pending = {name: spec for name, spec in specs.items() if name not in live}
    checked = checked_connections(pending, workspace, timeout, health_check) if check and pending else {}
    return [connector_row(spec, live.get(name), checked.get(name)) for name, spec in sorted(specs.items())]


def skill_rows(workspace: Path, include_bundled: bool = True) -> list[StatusRow]:
    from axon.skills.manager import SkillManager
    manager = SkillManager(workspace)
    rows = [StatusRow("skill", name, True, scope=skill.scope) for name, skill in sorted(manager.skills.items())]
    rows += [StatusRow("skill", name, False, scope=skill.scope) for name, skill in sorted(manager.disabled.items())]
    if include_bundled:
        return rows
    return [row for row in rows if row.scope != "bundled"]


def bundled_skill_count(workspace: Path) -> int:
    return len(skill_rows(workspace)) - len(skill_rows(workspace, include_bundled=False))


def plugin_rows(workspace: Path) -> list[StatusRow]:
    return [StatusRow("plugin", name, plugin.enabled, scope=plugin.scope)
            for name, plugin in sorted(plugin_tools.installed_plugins(workspace).items())]


def collect(workspace: Path, check: bool = False, timeout: float = installer.DEFAULT_CHECK_TIMEOUT,
            health_check: installer.HealthCheck = installer.check_server,
            include_bundled: bool = True) -> list[StatusRow]:
    return (connector_rows(workspace, check, timeout, health_check)
            + skill_rows(workspace, include_bundled) + plugin_rows(workspace))


def clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def render(rows: list[StatusRow], hidden_bundled: int = 0) -> list[str]:
    lines = table_lines(rows) if rows else ["  Nothing is installed yet. Try: /marketplace list"]
    if hidden_bundled:
        lines.append(f"  {hidden_bundled} built-in skills are not listed; add --all to show them.")
    return lines


def table_lines(rows: list[StatusRow]) -> list[str]:
    header = f"  {'KIND':<10}{'NAME':<26}{'ENABLED':<9}{'CONNECTED':<11}{'TOOLS':<7}{'SCOPE':<10}LAST ERROR"
    lines = [header]
    for row in rows:
        lines.append(f"  {row.kind:<10}{clip(row.name, 24):<26}{'yes' if row.enabled else 'no':<9}"
                     f"{row.connected:<11}{row.tools:<7}{row.scope:<10}{clip(row.last_error, 60)}")
    lines.append("")
    lines.append("  " + summary(rows))
    return lines


def summary(rows: list[StatusRow]) -> str:
    parts = []
    for kind in ("connector", "skill", "plugin"):
        mine = [row for row in rows if row.kind == kind]
        on = [row for row in mine if row.enabled]
        parts.append(f"{len(on)}/{len(mine)} {kind}s enabled")
    return ", ".join(parts)


def as_json(rows: list[StatusRow]) -> list[dict[str, Any]]:
    return [asdict(row) for row in rows]
