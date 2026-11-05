"""Connector install/remove with a fake health check, a temporary HOME and no network."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from axon.marketplace import catalog, installer
from axon.marketplace.catalog import EnvVar
from axon.marketplace.installer import HealthResult


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home / ".axon"))
    for name in ("GITHUB_PERSONAL_ACCESS_TOKEN", "BRAVE_API_KEY", "RENDER_API_KEY"):
        monkeypatch.setenv(name, "")  # empty counts as unset; restored after the test
    return home


def connector(entry_id: str):
    return catalog.find_entry(catalog.load_connectors(), entry_id)


def healthy(spec, workspace, timeout) -> HealthResult:
    return HealthResult("connected", tools=["one", "two"])


def broken(spec, workspace, timeout) -> HealthResult:
    return HealthResult("failed", "connection refused")


def user_config(home: Path) -> dict:
    return json.loads((home / ".axon" / "mcp.json").read_text(encoding="utf-8"))


def test_install_stdio_writes_config_and_reports_tools(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("memory"), workspace, health_check=healthy)
    assert result.ok and result.tools == ["one", "two"]
    entry = user_config(private_home)["mcpServers"]["memory"]
    assert entry["command"] == "npx"


def test_install_remote_writes_type_and_url(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("linear"), workspace, health_check=healthy)
    assert result.ok
    entry = user_config(private_home)["mcpServers"]["linear"]
    assert entry["type"] == "http" and entry["url"] == "https://mcp.linear.app/mcp"


def test_install_twice_is_idempotent(private_home: Path, workspace: Path) -> None:
    installer.install_connector(connector("memory"), workspace, health_check=healthy)
    again = installer.install_connector(connector("memory"), workspace, health_check=broken)
    assert again.ok and again.already_installed
    assert list(user_config(private_home)["mcpServers"]) == ["memory"]


def test_name_clash_with_different_server_is_refused(workspace: Path) -> None:
    installer.install_connector(connector("memory"), workspace, health_check=healthy)
    result = installer.install_connector(connector("fetch"), workspace, name="memory", health_check=healthy)
    assert not result.ok and "different server" in result.message


def test_project_scope_writes_project_file(workspace: Path) -> None:
    result = installer.install_connector(connector("memory"), workspace, scope="project", health_check=healthy)
    assert result.ok
    assert "memory" in json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_failed_health_check_rolls_back(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("linear"), workspace, health_check=broken)
    assert not result.ok and result.rolled_back
    assert "Could not reach https://mcp.linear.app/mcp" in result.message
    assert "linear" not in user_config(private_home)["mcpServers"]


def test_failed_stdio_check_names_the_command(workspace: Path) -> None:
    result = installer.install_connector(connector("memory"), workspace, health_check=broken)
    assert "npx" in result.message and "PATH" in result.message


def test_keep_flag_keeps_config_after_failure(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("linear"), workspace, keep=True, health_check=broken)
    assert not result.ok and not result.rolled_back
    assert "linear" in user_config(private_home)["mcpServers"]


def test_needs_auth_is_a_successful_install(private_home: Path, workspace: Path) -> None:
    def needs_login(spec, ws, timeout) -> HealthResult:
        return HealthResult("needs-auth", "401")
    result = installer.install_connector(connector("notion"), workspace, health_check=needs_login)
    assert result.ok and result.needs_login and "/mcp login notion" in result.message
    assert "notion" in user_config(private_home)["mcpServers"]


def test_missing_required_key_fails_before_writing(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("github"), workspace, health_check=healthy)
    assert not result.ok and "GITHUB_PERSONAL_ACCESS_TOKEN" in result.message
    assert not (private_home / ".axon" / "mcp.json").exists()


def test_prompted_key_is_stored_privately_and_referenced(private_home: Path, workspace: Path) -> None:
    asked: list[EnvVar] = []

    def prompt(var: EnvVar) -> str:
        asked.append(var)
        return "ghp_secret_value"

    result = installer.install_connector(connector("github"), workspace, prompt=prompt, health_check=healthy)
    assert result.ok and [v.name for v in asked] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    env_file = private_home / ".axon" / ".env"
    assert "ghp_secret_value" in env_file.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    config_text = (private_home / ".axon" / "mcp.json").read_text(encoding="utf-8")
    assert "ghp_secret_value" not in config_text
    assert "${GITHUB_PERSONAL_ACCESS_TOKEN}" in config_text


def test_env_option_skips_the_prompt(workspace: Path) -> None:
    def prompt(var: EnvVar) -> str:
        raise AssertionError("should not ask")
    env = {"RENDER_API_KEY": "rnd_123"}
    result = installer.install_connector(connector("render"), workspace, env=env, prompt=prompt, health_check=healthy)
    assert result.ok and result.saved_env == ["RENDER_API_KEY"]


def test_optional_variables_may_stay_empty(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("mongodb"), workspace, health_check=healthy)
    assert result.ok
    assert "env" not in user_config(private_home)["mcpServers"]["mongodb"]


def test_stdio_secret_goes_into_env_reference(private_home: Path, workspace: Path) -> None:
    result = installer.install_connector(connector("brave-search"), workspace, env={"BRAVE_API_KEY": "b-1"},
                                         health_check=healthy)
    assert result.ok
    env = user_config(private_home)["mcpServers"]["brave-search"]["env"]
    assert env == {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}


def test_remove_and_toggle(private_home: Path, workspace: Path) -> None:
    installer.install_connector(connector("memory"), workspace, health_check=healthy)
    assert installer.set_connector_enabled("memory", workspace, False).ok
    assert user_config(private_home)["mcpServers"]["memory"]["disabled"] is True
    assert installer.set_connector_enabled("memory", workspace, True).ok
    assert "disabled" not in user_config(private_home)["mcpServers"]["memory"]
    assert installer.remove_connector("memory", workspace).ok
    assert not installer.remove_connector("memory", workspace).ok


def test_invalid_name_and_scope_are_rejected(workspace: Path) -> None:
    assert not installer.install_connector(connector("memory"), workspace, name="bad name!").ok
    assert not installer.install_connector(connector("memory"), workspace, scope="galaxy").ok


def test_custom_connector_from_url_and_command(workspace: Path) -> None:
    web = installer.custom_connector("mine", "https://example.com/mcp")
    assert web.remote and web.url == "https://example.com/mcp"
    local = installer.custom_connector("tool", "python -m my_server --flag")
    assert local.command == "python" and local.args == ["-m", "my_server", "--flag"]
    assert installer.install_connector(web, workspace, health_check=healthy).ok
    with pytest.raises(ValueError):
        installer.custom_connector("x", "   ")


def test_health_check_never_hangs(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    def slow(spec, ws, timeout) -> HealthResult:
        time.sleep(5)
        return HealthResult("connected")
    monkeypatch.setattr(installer, "_connect_and_list", slow)
    monkeypatch.setattr(installer, "THREAD_GRACE_SECONDS", 0.1)
    spec = installer.ServerSpec.from_dict("slow", {"command": "x"})
    started = time.monotonic()
    result = installer.check_server(spec, workspace, 0.1)
    assert time.monotonic() - started < 2
    assert result.status == "failed" and "Timed out" in result.error
