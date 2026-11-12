"""/telemetry: OpenTelemetry trace export (status, on, off, test). Metadata only unless captureContent is set."""
from __future__ import annotations

from typing import Any

from axon.commands.common import err, hint, info, ok, update_settings, update_settings_file, ws_of
from axon.commands.registry import CommandResult, command


@command("telemetry", category="Context & Cost", description="OpenTelemetry trace export (off by default): status, on, off, test",
         usage="/telemetry [status|on|off|test]")
def handle_telemetry(agent: Any, arg: str) -> CommandResult:
    from axon.features import otel
    sub = (arg.split() or ["status"])[0].lower()
    if sub == "status":
        for line in otel.status_lines(agent):
            info(line)
        hint("Sent: model, provider, token counts, cost, durations, tool names, statuses. Never prompts, replies or tool input/output unless telemetry.captureContent is on.")
        return CommandResult(handled=True)
    if sub == "test":
        good, msg = otel.send_test(agent.settings)
        (ok if good else err)(f"Test span {'sent' if good else 'failed'}: {msg}")
        return CommandResult(handled=True)
    if sub in ("on", "off"):
        block = dict(getattr(agent.settings, "telemetry", None) or {})
        block["enabled"] = sub == "on"
        update_settings(agent, telemetry=block)
        update_settings_file({"telemetry.enabled": sub == "on"}, workspace=ws_of(agent))
        tr = otel.attach(agent)
        if sub == "on" and tr is None:
            err("No endpoint: set telemetry.endpoint in settings.json or OTEL_EXPORTER_OTLP_ENDPOINT, then run /telemetry on again.")
        else:
            ok(f"Telemetry {sub}")
        return CommandResult(handled=True)
    err("Usage: /telemetry [status|on|off|test]")
    return CommandResult(handled=True)
