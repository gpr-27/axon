"""/sandbox on|off|status: run Bash under the operating system's sandbox."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, info, ok, typed_only, update_settings, update_settings_file, usage, warn
from axon.commands.registry import CommandResult, command
from axon.permissions import sandbox


def _done() -> CommandResult:
    return CommandResult(handled=True)


@command("sandbox", category="System", description="Run Bash under an OS sandbox (writes confined to the workspace)",
         usage="/sandbox [on|off|status]   (setting sandbox.enabled; sandbox.allowNetwork lets commands use the network)")
def handle_sandbox(agent: Any, arg: str) -> CommandResult:
    a = arg.strip().lower()
    if a in ("", "status"):
        print("\n" + "\n".join("  " + line for line in sandbox.status_text(agent.settings).splitlines()) + "\n")
        return _done()
    if a not in ("on", "off"):
        usage("/sandbox [on|off|status]")
        return _done()
    if a == "on":
        name, why = sandbox.backend()
        if name is None:
            err(f"The sandbox is unavailable on this machine: {why}. It stays off; Bash runs unsandboxed.")
            return _done()
    update_settings(agent, sandbox_enabled=(a == "on"))
    update_settings_file({"sandbox.enabled": a == "on"})
    if a == "on":
        ok("Bash sandbox on: writes only inside the workspace, additional directories and temp dir; credential "
           "stores unreadable; network blocked." + typed_only(" /sandbox status lists what is and is not enforced."))
        if sandbox.policy_for(agent.settings).allow_network:
            warn("Network access is allowed by sandbox.allowNetwork.")
    else:
        info("Bash sandbox off.")
    return _done()
