"""`/mcp` and `axon mcp ...` Claude Code parity: get, scopes, approvals, policy, OAuth commands, prompts."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

import axon.mcp.bridge as bridge_mod
from axon.mcp import approval, state
from axon.mcp.bridge import get_bridge, initialize_mcp_bridge, shutdown_mcp_bridge
from axon.mcp.commands import _parse_add, handle_mcp_command, status_label
from axon.mcp.config import MCPConfig, ServerSpec
from axon.mcp.oauth import OAuthManager, TokenStore
from tests.suite.test_mcp_config_and_commands import _agent
from tests.suite.test_mcp_fakes import fake_server  # noqa: F401


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    d = tmp_path / "managed"
    d.mkdir()
    monkeypatch.setenv("AXON_MANAGED_DIR", str(d))
    shutdown_mcp_bridge()
    state.set_interactive(False)
    yield d
    state.set_interactive(None)
    shutdown_mcp_bridge()


def _out(capsys) -> str:
    return capsys.readouterr().out


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ─── add ────────────────────────────────────────────────────────────────────

def test_parse_add_oauth_flags_and_multi_env():
    o = _parse_add(["--transport", "http", "--client-id", "cid", "--client-secret", "--callback-port", "8080",
                    "my-server", "https://mcp.example.com/mcp"])
    assert o["client_id"] == "cid" and o["client_secret"] is True and o["callback_port"] == 8080
    assert o["positional"] == ["my-server", "https://mcp.example.com/mcp"] and o["transport"] == "http"
    o = _parse_add(["--env", "A=1", "B=2", "--transport", "stdio", "srv", "--", "npx", "-y", "pkg"])
    assert o["env"] == {"A": "1", "B": "2"} and o["positional"] == ["srv", "npx"] and o["command_args"] == ["-y", "pkg"]
    assert _parse_add(["-t", "ws", "ev", "wss://h/s"])["transport"] == "ws"


def test_add_validates_name_policy_and_duplicates(workspace: Path, capsys, _env: Path):
    agent = _agent(workspace)
    handle_mcp_command(agent, ["add-json", "bad name", '{"command":"x"}'])
    assert "Invalid name" in _out(capsys)
    handle_mcp_command(agent, 'add-json ok {"command":"definitely-missing-xyz"} --scope local')
    assert "Saved MCP server 'ok'" in _out(capsys)
    handle_mcp_command(agent, 'add-json ok {"command":"other"} --scope local')
    assert "already exists in local config" in _out(capsys)
    # a denylist entry blocks adding; an exclusive managed-mcp.json blocks everything
    _write(workspace / ".axon" / "settings.json", {"deniedMcpServers": [{"serverName": "evil"}]})
    handle_mcp_command(agent, 'add-json evil {"command":"x"}')
    assert 'Cannot add MCP server "evil": server is explicitly blocked by enterprise policy' in _out(capsys)
    _write(workspace / ".axon" / "settings.json", {"allowedMcpServers": [{"serverUrl": "https://ok.example/*"}]})
    handle_mcp_command(agent, 'add-json n2 {"type":"http","url":"https://nope.example/mcp"}')
    assert 'Cannot add MCP server "n2": not allowed by enterprise policy' in _out(capsys)
    _write(_env / "managed-mcp.json", {"mcpServers": {}})
    handle_mcp_command(agent, 'add-json n3 {"command":"x"}')
    assert "exclusive control" in _out(capsys)


def test_add_http_with_oauth_app_stores_secret_outside_the_config(workspace: Path, capsys, monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_SECRET", "s3cret")
    agent = _agent(workspace)
    handle_mcp_command(agent, ["add", "--transport", "http", "--scope", "project", "--client-id", "cid",
                               "--client-secret", "--callback-port", "8123", "svc", "https://127.0.0.1:9/mcp"])
    cfg = json.loads((workspace / ".mcp.json").read_text())["mcpServers"]["svc"]
    assert cfg["oauth"] == {"clientId": "cid", "callbackPort": 8123} and "s3cret" not in json.dumps(cfg)
    spec = MCPConfig(workspace).get_spec("svc")
    rec = TokenStore().get(OAuthManager(spec).key)
    assert rec["client"]["client_secret"] == "s3cret" and rec["client"]["client_id"] == "cid"
    assert spec.approval == "approved"            # you added it yourself
    out = _out(capsys)
    assert "HTTP MCP server svc" in out


def test_add_secret_without_client_id_is_refused(workspace: Path, capsys, monkeypatch):
    monkeypatch.setenv("MCP_CLIENT_SECRET", "x")
    handle_mcp_command(_agent(workspace), ["add", "--transport", "http", "--client-secret", "s", "https://h/mcp"])
    assert "needs --client-id" in _out(capsys)


def test_add_ws_server(workspace: Path, capsys):
    handle_mcp_command(_agent(workspace), 'add-json ev {"type":"ws","url":"ws://127.0.0.1:9/s"} --scope local')
    assert json.loads((workspace / ".axon" / "mcp.json").read_text())["mcpServers"]["ev"]["type"] == "ws"
    assert "WebSocket MCP server ev" in _out(capsys)


# ─── get / list ─────────────────────────────────────────────────────────────

def test_get_shows_scope_endpoint_headers_and_oauth(workspace: Path, capsys):
    _write(workspace / ".mcp.json", {"mcpServers": {"api": {
        "type": "http", "url": "https://127.0.0.1:9/mcp", "headers": {"X-Key": "${KEY:-k}"},
        "oauth": {"clientId": "c", "scopes": "a b"}, "timeout": 120000, "alwaysLoad": False}}})
    handle_mcp_command(_agent(workspace), "get api")
    out = _out(capsys)
    for needle in ("api:", "Scope: project", "Type: http", "URL: https://127.0.0.1:9/mcp", "X-Key: ${KEY:-k}",
                   "OAuth: not authenticated", "client ID configured", "scopes: a b", "Timeout: 120s", "alwaysLoad: false"):
        assert needle in out, (needle, out)
    handle_mcp_command(_agent(workspace), "get nope")
    assert "No MCP server found with name: nope" in _out(capsys)


def test_get_hides_plugin_and_managed_values(workspace: Path, capsys, _env: Path):
    _write(_env / "managed-settings.json", {"managedMcpServers": {
        "corp": {"type": "http", "url": "https://corp.example/secret/path", "headers": {"X-K": "topsecret"}}}})
    handle_mcp_command(_agent(workspace), "get corp")
    out = _out(capsys)
    assert "managed by your organization" in out and "corp.example" in out
    assert "secret/path" not in out and "topsecret" not in out and "X-K" in out


def test_list_shows_status_labels_warnings_conflicts_and_blocked(workspace: Path, capsys, _env: Path):
    state.set_interactive(True)
    cfg = MCPConfig(workspace)
    _write(cfg.global_config_file, {"mcpServers": {"dup": {"command": "a"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {
        "dup": {"command": "b"}, "pend": {"command": "x"}, "ws": {"command": "echo ", "args": []},
        "blocked": {"command": "z"}, "sdk": {"type": "sdk"}}})
    _write(workspace / ".axon" / "settings.json", {"deniedMcpServers": [{"serverName": "blocked"}]})
    handle_mcp_command(_agent(workspace), "list")
    out = _out(capsys)
    assert "Pending approval" in out and "Leading or trailing whitespace in: command" in out
    assert "defined in more than one scope" in out and "'blocked' is not loaded: blocked by enterprise policy" in out
    assert "Skipped 'sdk'" in out
    assert status_label("needs-auth").endswith("Needs authentication") and "Failed" in status_label("failed")


def test_axon_mcp_list_health_checks_servers_and_cleans_up(workspace: Path, fake_server: Path, capsys):
    cfg = MCPConfig(workspace)
    cfg.save_spec(ServerSpec(name="fk", command=sys.executable, args=[str(fake_server)]), "local")
    handle_mcp_command(_agent(workspace), ["list"])
    out = _out(capsys)
    assert "Connected" in out and "7 tools" in out
    assert get_bridge().connections == {}                    # `axon mcp ...` leaves no server processes behind
    handle_mcp_command(_agent(workspace), ["get", "fk"])
    out = _out(capsys)
    assert "Status: ✔ Connected" in out and "Tools: 7" in out


# ─── remove / enable / disable ──────────────────────────────────────────────

def test_remove_multi_scope_needs_a_scope_and_clears_oauth(workspace: Path, capsys):
    agent = _agent(workspace)
    cfg = MCPConfig(workspace)
    _write(cfg.global_config_file, {"mcpServers": {"s": {"type": "http", "url": "https://h.example/mcp"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"s": {"type": "http", "url": "https://h.example/mcp"}}})
    handle_mcp_command(agent, "remove s")
    assert "exists in multiple scopes" in _out(capsys)
    spec = cfg.get_spec("s")
    TokenStore().update(OAuthManager(spec).key, tokens={"access_token": "t"})
    handle_mcp_command(agent, "remove s --scope project")
    assert "Removed" in _out(capsys) and MCPConfig(workspace).get_spec("s").scope == "user"
    assert TokenStore().get(OAuthManager(spec).key) != {}      # the user-scope copy still uses it
    handle_mcp_command(agent, "remove s")
    assert "Removed" in _out(capsys) and TokenStore().get(OAuthManager(spec).key) == {}


def test_remove_refuses_managed_servers(workspace: Path, capsys, _env: Path):
    _write(_env / "managed-settings.json", {"managedMcpServers": {"corp": {"type": "http", "url": "https://c.example/mcp"}}})
    handle_mcp_command(_agent(workspace), "remove corp")
    assert 'is provided by your organization (managed settings) and cannot be removed locally' in _out(capsys)


def test_disable_a_project_server_does_not_touch_the_shared_file(workspace: Path, capsys):
    _write(workspace / ".mcp.json", {"mcpServers": {"p": {"command": "definitely-missing-xyz"}}})
    before = (workspace / ".mcp.json").read_text()
    agent = _agent(workspace)
    handle_mcp_command(agent, "disable p")
    assert (workspace / ".mcp.json").read_text() == before
    assert MCPConfig(workspace).get_spec("p").disabled
    handle_mcp_command(agent, "list")
    assert "Disabled for this project" in _out(capsys)
    handle_mcp_command(agent, "enable p")
    assert not MCPConfig(workspace).get_spec("p").disabled


# ─── approvals ──────────────────────────────────────────────────────────────

def test_unapproved_project_server_does_not_start_until_approved(workspace: Path, fake_server: Path, monkeypatch, capsys):
    _write(workspace / ".mcp.json", {"mcpServers": {"fk": {"command": sys.executable, "args": [str(fake_server)]}}})
    state.set_interactive(True)
    monkeypatch.setattr("builtins.input", lambda _p="": "")         # Enter: decide later
    initialize_mcp_bridge(workspace, timeout=5)
    bridge = get_bridge()
    bridge.wait_until_settled(5)
    assert bridge.connections["fk"].status == "pending-approval" and "approval" in bridge.startup_summary()
    handle_mcp_command(_agent(workspace), "approve fk")
    out = _out(capsys)
    assert "Approved fk" in out and "connected" in out
    assert state.project_choice(workspace, "fk") == "approved"
    handle_mcp_command(_agent(workspace), "reset-project-choices")
    assert "Reset 1" in _out(capsys) and state.project_choice(workspace, "fk") is None


def test_startup_prompt_approves_everything_with_a_(workspace: Path, fake_server: Path, monkeypatch):
    _write(workspace / ".mcp.json", {"mcpServers": {"fk": {"command": sys.executable, "args": [str(fake_server)]}}})
    state.set_interactive(True)
    monkeypatch.setattr("builtins.input", lambda _p="": "a")
    initialize_mcp_bridge(workspace, timeout=10)
    assert get_bridge().connections["fk"].status == "connected"


def test_reject_and_approve_all(workspace: Path, capsys):
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    state.set_interactive(True)
    agent = _agent(workspace)
    handle_mcp_command(agent, "reject a")
    assert "Rejected a" in _out(capsys) and MCPConfig(workspace).get_spec("a").approval == "rejected"
    handle_mcp_command(agent, "approve all")
    assert state.project_choice(workspace, "b") == "approved"
    assert approval.pending(MCPConfig(workspace).load_specs()) == []


def test_rejected_server_is_never_started(workspace: Path, fake_server: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"fk": {"command": sys.executable, "args": [str(fake_server)]}}})
    state.set_interactive(True)
    state.set_project_choice(workspace, "fk", "rejected")
    initialize_mcp_bridge(workspace, timeout=5)
    conn = get_bridge().connections["fk"]
    assert conn.status == "rejected" and conn.client is None


def test_policy_is_rechecked_when_a_server_is_started(workspace: Path, fake_server: Path):
    from axon.mcp.bridge import MCPServerConnection
    conn = MCPServerConnection("fk", spec=ServerSpec(name="fk", command=sys.executable, args=[str(fake_server)]),
                               workspace=workspace)
    _write(workspace / ".axon" / "settings.json", {"deniedMcpServers": [{"serverName": "fk"}]})
    assert conn.start() is False and conn.status == "blocked"
    assert conn.error == "MCP server fk is blocked by enterprise managed policy"


# ─── OAuth commands ─────────────────────────────────────────────────────────

def test_login_and_logout_guard_rails(workspace: Path, capsys):
    agent = _agent(workspace)
    _write(workspace / ".mcp.json", {"mcpServers": {
        "local": {"command": "x"}, "hdr": {"type": "http", "url": "https://h.example/mcp",
                                            "headers": {"Authorization": "Bearer t"}},
        "plain": {"type": "http", "url": "https://h.example/mcp"}}})
    handle_mcp_command(agent, "login local")
    assert "OAuth works with HTTP and SSE servers only" in _out(capsys)
    handle_mcp_command(agent, "login hdr")
    assert "Authorization header configured" in _out(capsys)
    handle_mcp_command(agent, "login nope")
    assert "is not configured" in _out(capsys)
    handle_mcp_command(agent, "logout plain")
    assert "had no stored authentication" in _out(capsys)


def test_login_runs_the_flow_and_reconnects(workspace: Path, capsys, monkeypatch):
    _write(workspace / ".mcp.json", {"mcpServers": {"plain": {"type": "http", "url": "https://127.0.0.1:9/mcp"}}})
    calls = {}

    def fake_login(self, **kw):
        calls["kw"] = kw
        TokenStore().update(self.key, tokens={"access_token": "abc"})
        return "Signed in to 'plain'."

    monkeypatch.setattr(OAuthManager, "login", fake_login)
    handle_mcp_command(_agent(workspace), ["login", "plain", "--no-browser"])
    out = _out(capsys)
    assert "Signed in to 'plain'" in out and calls["kw"]["open_browser"] is False
    handle_mcp_command(_agent(workspace), ["logout", "plain"])
    assert "Cleared stored authentication" in _out(capsys)


# ─── reconnect all / import ─────────────────────────────────────────────────

def test_reconnect_all_only_touches_failed_servers(workspace: Path, fake_server: Path, capsys):
    cfg = MCPConfig(workspace)
    cfg.save_spec(ServerSpec(name="good", command=sys.executable, args=[str(fake_server)]), "local")
    cfg.save_spec(ServerSpec(name="bad", command="definitely-missing-xyz"), "local")
    agent = _agent(workspace)
    handle_mcp_command(agent, "connect")
    _out(capsys)
    conns = get_bridge().connections
    good_client = conns["good"].client
    handle_mcp_command(agent, "reconnect all")
    out = _out(capsys)
    assert "Reconnected 1 server(s): bad" in out
    assert get_bridge().connections["good"].client is good_client


def test_add_from_claude_desktop_renames_duplicates(workspace: Path, tmp_path: Path, capsys, monkeypatch):
    desktop = tmp_path / "claude_desktop_config.json"
    _write(desktop, {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "fs"]}, "has space": {"command": "x"}}})
    monkeypatch.setattr(MCPConfig, "find_claude_desktop_config", lambda self: desktop)
    cfg = MCPConfig(workspace)
    cfg.add_server("fs", "mine", scope="user")
    handle_mcp_command(_agent(workspace), ["add-from-claude-desktop"])
    out = _out(capsys)
    assert "Imported 'fs' as 'fs_1'" in out and "skipped 'has space'" in out
    assert MCPConfig(workspace).get_spec("fs_1").command == "npx"


# ─── prompts ────────────────────────────────────────────────────────────────

def test_prompts_run_as_server_colon_prompt_and_mcp_double_underscore(workspace: Path, fake_server: Path, monkeypatch):
    from axon.commands import registry
    from axon.commands.builtin import dispatch_command
    ran = []
    monkeypatch.setattr(registry, "_mcp_prompts", lambda: [
        {"server": "fk", "name": "review", "description": "d", "arguments": [], "command": "mcp__fk__review"}])
    import axon.commands.agent_cmds as agent_cmds
    monkeypatch.setattr(agent_cmds, "run_mcp_prompt", lambda agent, p, text: ran.append((p["name"], text)))
    agent = _agent(workspace)
    assert dispatch_command("/fk:review rust", agent) is not None
    assert dispatch_command("/mcp__fk__review go", agent) is not None
    assert ran == [("review", "rust"), ("review", "go")]


def test_reserved_prompt_server_name_is_hidden(workspace: Path, fake_server: Path):
    from axon.mcp.bridge import MCPToolBridge
    bridge = MCPToolBridge(workspace)
    bridge.connect_server(ServerSpec(name="anthropic-skills", command=sys.executable, args=[str(fake_server)]), timeout=10)
    try:
        assert bridge.connections["anthropic-skills"].connected and bridge.list_prompts() == []
    finally:
        bridge.disconnect_all()


def test_cli_rejects_mcp_config_next_to_managed_mcp_json(workspace: Path, _env: Path, capsys):
    from axon.cli import main
    _write(_env / "managed-mcp.json", {"mcpServers": {}})
    code = main(["--workspace", str(workspace), "--mcp-config", '{"mcpServers":{"x":{"command":"y"}}}', "-p", "hi"])
    assert code == 2
    captured = capsys.readouterr()
    assert "enterprise MCP config is present" in captured.out + captured.err
