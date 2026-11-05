"""Automatic behaviour: the settings block, background connect with retries, reconnect, auto-disable."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from axon import notices
from axon.marketplace import automation, cli
from axon.marketplace.installer import HealthResult
from axon.mcp import bridge as bridge_mod
from axon.mcp import health as mcp_health
from axon.mcp.bridge import MCPServerConnection, MCPToolBridge
from axon.mcp.config import MCPConfig, ServerSpec


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    notices.drain()
    yield home
    notices.drain()


def run(tokens: list[str], workspace: Path):
    lines: list[str] = []
    shop = cli.Marketplace(workspace, out=lines.append,
                           health_check=lambda s, w, t: HealthResult("connected", tools=["t"]))
    outcome = shop.run(tokens)
    return outcome, "\n".join(lines)


def write_settings(path: Path, block: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"automation": block}), encoding="utf-8")


def server(name: str = "demo", **raw: Any) -> ServerSpec:
    return ServerSpec.from_dict(name, raw or {"command": "demo-server"}, "user")


# ─── the settings block ──────────────────────────────────────────────────────

def test_everything_is_off_by_default(workspace: Path) -> None:
    auto = automation.load_automation(workspace)
    assert auto.enabled is False
    assert not any(auto.on(name) for name in automation.setting_names() if isinstance(getattr(auto, name), bool))
    assert auto.auto_install is False and auto.auto_update is False


def test_user_settings_turn_features_on_and_off(workspace: Path, private_home: Path) -> None:
    write_settings(private_home / ".axon" / "settings.json", {"enabled": True, "auto_skills": False, "startup_wait": 5})
    auto = automation.load_automation(workspace)
    assert auto.on("auto_connect") and not auto.on("auto_skills") and auto.startup_wait == 5.0


def test_config_toml_is_read_and_settings_json_wins(workspace: Path, private_home: Path) -> None:
    (private_home / ".axon").mkdir()
    (private_home / ".axon" / "config.toml").write_text("[automation]\nenabled = true\nmax_auto_skills = 4\n")
    assert automation.load_automation(workspace).max_auto_skills == 4
    write_settings(private_home / ".axon" / "settings.json", {"max_auto_skills": 1})
    auto = automation.load_automation(workspace)
    assert auto.enabled and auto.max_auto_skills == 1


def test_a_project_cannot_turn_on_auto_install_or_auto_update(workspace: Path, private_home: Path) -> None:
    write_settings(private_home / ".axon" / "settings.json", {"enabled": True})
    write_settings(workspace / ".axon" / "settings.json", {"auto_install": True, "auto_update": True, "suggest": False})
    auto = automation.load_automation(workspace)
    assert auto.auto_install is False and auto.auto_update is False
    assert auto.suggest is False                     # harmless keys still apply


def test_the_user_can_turn_on_auto_install(workspace: Path, private_home: Path) -> None:
    write_settings(private_home / ".axon" / "settings.json", {"enabled": True, "auto_install": True})
    assert automation.load_automation(workspace).on("auto_install")


def test_bad_values_are_ignored(workspace: Path, private_home: Path) -> None:
    write_settings(private_home / ".axon" / "settings.json",
                   {"enabled": "yes", "startup_wait": "fast", "connect_retries": -2, "suggest": 1})
    auto = automation.load_automation(workspace)
    assert auto.enabled is False and auto.startup_wait == 2.0 and auto.connect_retries == 0 and auto.suggest is True


def test_marketplace_auto_on_off_status_set(workspace: Path, private_home: Path) -> None:
    _, text = run(["auto"], workspace)
    assert "OFF" in text and "/marketplace auto on" in text
    outcome, text = run(["auto", "on"], workspace)
    assert outcome.code == 0 and "ON" in text and "auto_connect" in text
    assert automation.load_automation(workspace).enabled
    run(["auto", "set", "auto_disable_after", "5"], workspace)
    run(["auto", "set", "auto_skills", "off"], workspace)
    auto = automation.load_automation(workspace)
    assert auto.auto_disable_after == 5 and auto.auto_skills is False
    run(["auto", "off"], workspace)
    assert not automation.load_automation(workspace).enabled
    assert json.loads((private_home / ".axon" / "settings.json").read_text())["automation"]["auto_disable_after"] == 5


def test_auto_set_rejects_bad_input(workspace: Path) -> None:
    assert run(["auto", "set", "nonsense", "1"], workspace)[0].code == 1
    assert run(["auto", "set", "auto_skills", "maybe"], workspace)[0].code == 1
    assert run(["auto", "set", "startup_wait", "-1"], workspace)[0].code == 1
    assert run(["auto", "sideways"], workspace)[0].code == 1


def test_saving_keeps_the_rest_of_settings_json(workspace: Path, private_home: Path) -> None:
    path = private_home / ".axon" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark"}))
    run(["auto", "on"], workspace)
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["automation"]["enabled"] is True


# ─── background connect with retries ─────────────────────────────────────────

class Scripted:
    """Replaces MCPServerConnection.start: fails ``failures`` times, then connects."""

    def __init__(self, failures: int, delay: float = 0.0) -> None:
        self.failures, self.calls, self.delay = failures, 0, delay

    def as_method(self) -> Any:
        def start(conn: MCPServerConnection, timeout: float | None = None) -> bool:
            time.sleep(self.delay)
            self.calls += 1
            if self.calls <= self.failures:
                conn.status, conn.error = "failed", f"attempt {self.calls} failed"
                return False
            conn.status, conn.error = "connected", None
            return True
        return start


def automated_bridge(workspace: Path, **changes: Any) -> MCPToolBridge:
    bridge = MCPToolBridge(workspace)
    bridge.automation = automation.Automation(enabled=True, **changes)
    return bridge


def test_failed_first_connect_is_retried_with_backoff(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "AUTO_CONNECT_BACKOFF", (0.01, 0.01, 0.01, 0.01))
    script = Scripted(failures=2)
    monkeypatch.setattr(MCPServerConnection, "start", script.as_method())
    bridge = automated_bridge(workspace, connect_retries=3)
    bridge.connect_all({"demo": server()}, timeout=5)
    assert script.calls == 3
    assert bridge.connections["demo"].status == "connected"
    assert mcp_health.get("demo").failures == 0 and mcp_health.get("demo").last_ok > 0


def test_retries_stop_after_the_configured_number(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "AUTO_CONNECT_BACKOFF", (0.01, 0.01, 0.01, 0.01))
    script = Scripted(failures=99)
    monkeypatch.setattr(MCPServerConnection, "start", script.as_method())
    bridge = automated_bridge(workspace, connect_retries=2, auto_disable_after=0)
    bridge.connect_all({"demo": server()}, timeout=5)
    assert script.calls == 3                       # first try + 2 retries
    assert bridge.connections["demo"].status == "failed"
    assert mcp_health.get("demo").failures == 1    # one failed cycle, however many attempts it had


def test_without_automation_there_is_one_attempt_and_no_ledger(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = Scripted(failures=99)
    monkeypatch.setattr(MCPServerConnection, "start", script.as_method())
    bridge = MCPToolBridge(workspace)
    bridge.connect_all({"demo": server()}, timeout=5)
    assert script.calls == 1
    assert mcp_health.get("demo").failures == 0


def test_retries_do_not_repeat_for_a_server_that_needs_sign_in(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def start(conn: MCPServerConnection, timeout: float | None = None) -> bool:
        calls.append(1)
        conn.status, conn.error = "needs-auth", "sign in"
        return False

    monkeypatch.setattr(bridge_mod, "AUTO_CONNECT_BACKOFF", (0.01, 0.01))
    monkeypatch.setattr(MCPServerConnection, "start", start)
    bridge = automated_bridge(workspace, connect_retries=2)
    bridge.connect_all({"demo": server()}, timeout=5)
    assert len(calls) == 1 and mcp_health.get("demo").failures == 0


def test_connecting_does_not_block_the_caller(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = Scripted(failures=0, delay=0.6)
    monkeypatch.setattr(MCPServerConnection, "start", script.as_method())
    bridge = automated_bridge(workspace)
    started = time.monotonic()
    bridge.connect_all({"demo": server()}, timeout=0.05)
    assert time.monotonic() - started < 0.4
    assert bridge.connections["demo"].status == "connecting"
    bridge.wait_until_settled(3)
    assert bridge.connections["demo"].status == "connected"


def test_startup_waits_only_startup_wait_when_automation_is_on(workspace: Path, private_home: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    write_settings(private_home / ".axon" / "settings.json", {"enabled": True, "startup_wait": 0.25,
                                                              "suggest": False, "check_updates": False})
    MCPConfig(workspace).save_spec(server(), "user")
    seen: list[Any] = []
    monkeypatch.setattr(MCPToolBridge, "connect_all", lambda self, servers, timeout=None: seen.append(timeout) or [])
    monkeypatch.setattr(bridge_mod, "_bridge", None)
    bridge_mod.initialize_mcp_bridge(workspace)
    assert seen == [0.25]
    monkeypatch.setattr(bridge_mod, "_bridge", None)


def test_startup_keeps_the_old_wait_when_automation_is_off(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    seen: list[Any] = []
    monkeypatch.setattr(MCPToolBridge, "connect_all", lambda self, servers, timeout=None: seen.append(timeout) or [])
    monkeypatch.setattr(bridge_mod, "_bridge", None)
    bridge_mod.initialize_mcp_bridge(workspace)
    assert seen == [None]
    monkeypatch.setattr(bridge_mod, "_bridge", None)


# ─── reconnect when a server drops ───────────────────────────────────────────

class FakeClient:
    closed = False


def dropped_connection(spec: ServerSpec, **options: Any) -> tuple[MCPServerConnection, list[int]]:
    conn = MCPServerConnection(spec.name, spec=spec, **options)
    loops: list[int] = []
    conn._reconnect_loop = lambda epoch: loops.append(epoch)  # type: ignore[method-assign]
    conn.client, conn.status = FakeClient(), "connected"       # type: ignore[assignment]
    conn._closed(conn.client, "process exited")                # type: ignore[arg-type]
    time.sleep(0.05)
    return conn, loops


def test_a_dropped_local_server_is_not_reconnected_by_default() -> None:
    _, loops = dropped_connection(server())
    assert loops == []


def test_a_dropped_local_server_is_reconnected_with_automation() -> None:
    _, loops = dropped_connection(server(), reconnect_local=True)
    assert len(loops) == 1


def test_a_dropped_remote_server_is_reconnected_as_before() -> None:
    _, loops = dropped_connection(server("web", type="http", url="https://example.com/mcp"))
    assert len(loops) == 1


def test_turning_auto_reconnect_off_stops_remote_reconnects(workspace: Path) -> None:
    bridge = automated_bridge(workspace, auto_reconnect=False)
    conn = bridge._make_connection(server("web", type="http", url="https://example.com/mcp"))
    assert conn.auto_reconnect is False and conn.reconnect_local is False
    assert MCPToolBridge(workspace)._make_connection(server("web", type="http", url="https://example.com/mcp")).auto_reconnect


def test_automation_makes_local_reconnect_on(workspace: Path) -> None:
    assert automated_bridge(workspace)._make_connection(server()).reconnect_local is True


def test_reconnect_loop_reports_how_it_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "RECONNECT_DELAYS", (0.01, 0.01))
    outcomes: list[str] = []
    conn = MCPServerConnection("demo", spec=server(), reconnect_local=True, on_outcome=lambda c: outcomes.append(c.status))
    attempts = []

    def fake_start(timeout: float | None = None) -> bool:
        attempts.append(1)
        conn.status = "failed" if len(attempts) < 2 else "connected"
        return conn.status == "connected"

    conn._start = fake_start  # type: ignore[method-assign]
    conn.error, conn._epoch = "dropped", 1
    conn._reconnect_loop(1)
    assert outcomes == ["connected"] and len(attempts) == 2
    conn._start = lambda timeout=None: False  # type: ignore[method-assign]
    conn.status = "disconnected"
    conn._reconnect_loop(1)
    assert outcomes[-1] == "failed"


# ─── auto-disable after repeated failures ────────────────────────────────────

class Dead:
    """Stands in for a connection that could not connect."""

    def __init__(self, name: str, status: str = "failed", error: str = "refused") -> None:
        self.name, self.status, self.error = name, status, error


def test_server_is_switched_off_after_n_failures_in_a_row(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    bridge = automated_bridge(workspace, auto_disable_after=3)
    for _ in range(2):
        bridge._record_outcome(Dead("demo"))
    assert not MCPConfig(workspace).get_spec("demo").disabled and notices.peek() == []
    bridge._record_outcome(Dead("demo"))
    assert MCPConfig(workspace).get_spec("demo").disabled
    assert mcp_health.get("demo").auto_disabled
    (note,) = notices.drain()
    assert "demo" in note and "3 failed" in note and "refused" in note and "/marketplace enable demo" in note


def test_a_success_resets_the_count(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    bridge = automated_bridge(workspace, auto_disable_after=2)
    bridge._record_outcome(Dead("demo"))
    bridge._record_outcome(Dead("demo", status="connected", error=""))
    bridge._record_outcome(Dead("demo"))
    assert not MCPConfig(workspace).get_spec("demo").disabled


def test_zero_means_never_and_automation_off_means_never(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    for bridge in (automated_bridge(workspace, auto_disable_after=0), MCPToolBridge(workspace)):
        for _ in range(10):
            bridge._record_outcome(Dead("demo"))
    assert not MCPConfig(workspace).get_spec("demo").disabled


def test_sign_in_and_config_problems_do_not_count(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    bridge = automated_bridge(workspace, auto_disable_after=1)
    bridge._record_outcome(Dead("demo", status="needs-auth"))
    bridge._record_outcome(Dead("demo", status="needs-config"))
    assert mcp_health.get("demo").failures == 0


def test_a_manual_connect_failure_is_not_counted(workspace: Path) -> None:
    bridge = automated_bridge(workspace, auto_disable_after=1)
    bridge._record_outcome(Dead("demo"), count_failure=False)
    assert mcp_health.get("demo").failures == 0


def test_the_switched_off_server_can_be_turned_back_on(workspace: Path) -> None:
    MCPConfig(workspace).save_spec(server(), "user")
    bridge = automated_bridge(workspace, auto_disable_after=1)
    bridge._record_outcome(Dead("demo"))
    assert MCPConfig(workspace).get_spec("demo").disabled
    outcome, text = run(["enable", "demo"], workspace)
    assert outcome.code == 0
    assert not MCPConfig(workspace).get_spec("demo").disabled
    assert mcp_health.get("demo").failures == 0 and not mcp_health.get("demo").auto_disabled
    _, status_text = run(["status"], workspace)
    assert "demo" in status_text


def test_auto_disable_also_works_through_a_real_connect_cycle(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "AUTO_CONNECT_BACKOFF", (0.01,))
    monkeypatch.setattr(MCPServerConnection, "start", Scripted(failures=99).as_method())
    MCPConfig(workspace).save_spec(server(), "user")
    bridge = automated_bridge(workspace, connect_retries=1, auto_disable_after=2)
    for _ in range(2):
        bridge.connect_all({"demo": MCPConfig(workspace).get_spec("demo")}, timeout=5)
        bridge.wait_until_settled(3)
    assert MCPConfig(workspace).get_spec("demo").disabled
    assert "demo" not in bridge.connections


def test_health_ledger_survives_between_calls_and_tolerates_junk(private_home: Path) -> None:
    mcp_health.record_failure("a", "x" * 1000)
    assert len(mcp_health.get("a").last_error) == 300
    path = private_home / ".axon" / "state" / "mcp-health.json"
    path.write_text("not json")
    assert mcp_health.get("a").failures == 0
    path.write_text(json.dumps({"a": "junk", "b": {"failures": 2, "unknown_key": 1}}))
    assert mcp_health.get("a").failures == 0 and mcp_health.get("b").failures == 2


def test_notices_are_deduplicated_and_drained_once() -> None:
    notices.post("hello   world")
    notices.post("hello world")
    notices.post("")
    assert notices.drain() == ["hello world"] and notices.drain() == []


def test_ledger_is_thread_safe(workspace: Path) -> None:
    threads = [threading.Thread(target=mcp_health.record_failure, args=("busy", "x")) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert mcp_health.get("busy").failures == 8
