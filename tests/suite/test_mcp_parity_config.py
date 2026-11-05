"""Claude Code parity for MCP configuration: scopes, env expansion, approvals, policy, managed servers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.mcp import approval, state
from axon.mcp.config import MCPConfig, ServerSpec, expand_vars, normalize_url, valid_server_name
from axon.mcp.policy import cli_conflict, load_policy, url_matches


@pytest.fixture(autouse=True)
def _managed_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "managed"
    d.mkdir()
    monkeypatch.setenv("AXON_MANAGED_DIR", str(d))
    state.set_interactive(None)
    yield d
    state.set_interactive(None)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ─── spec fields ────────────────────────────────────────────────────────────

def test_ws_type_and_new_fields_round_trip():
    spec = ServerSpec.from_dict("ev", {"type": "ws", "url": "wss://x/socket", "headers": {"A": "b"},
                                       "headersHelper": "echo {}", "alwaysLoad": True, "timeout": 5000,
                                       "oauth": {"clientId": "c", "callbackPort": 8080}})
    assert spec.type == "ws" and spec.headers_helper == "echo {}" and spec.always_load is True
    assert spec.timeout == 5.0 and spec.timeout_explicit
    again = ServerSpec.from_dict("ev", spec.to_dict())
    assert again.to_dict() == spec.to_dict()
    assert ServerSpec.from_dict("x", {"url": "ws://h/p"}).type == "ws"
    assert ServerSpec.from_dict("x", {"type": "streamable-http", "url": "https://h"}).type == "http"


def test_sdk_type_is_skipped_with_a_message(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"sdk1": {"type": "sdk", "name": "x"}, "ok": {"command": "x"}}})
    cfg = MCPConfig(workspace)
    specs = cfg.load_specs()
    assert "ok" in specs and "sdk1" not in specs
    assert any(n == "sdk1" and "SDK host" in msg for n, _src, msg in cfg.problems)


def test_valid_server_names():
    assert valid_server_name("my-server_1") and not valid_server_name("my server") and not valid_server_name("")


# ─── env expansion ──────────────────────────────────────────────────────────

def test_credential_variables_read_empty_in_remote_url_and_headers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "mine")
    spec = ServerSpec.from_dict("r", {"type": "http", "url": "https://h/mcp", "headers": {
        "Authorization": "Bearer ${ANTHROPIC_API_KEY:-fallback}", "X": "${MY_TOKEN}"}})
    res = spec.resolved()
    assert res.headers["Authorization"] == "Bearer " and res.headers["X"] == "mine"
    assert spec.unresolved_problems() == []
    assert any("ANTHROPIC_API_KEY" in w for w in spec.config_warnings())
    # a stdio server's env is not a remote request: the variable expands as written
    local = ServerSpec.from_dict("l", {"command": "x", "env": {"K": "${ANTHROPIC_API_KEY}"}})
    assert local.resolved().env["K"] == "sk-secret"


def test_project_dir_variable_resolves_to_the_project(workspace: Path):
    spec = ServerSpec.from_dict("p", {"command": "${CLAUDE_PROJECT_DIR:-.}/bin/srv", "args": ["${AXON_PROJECT_DIR}"]})
    res = spec.resolved(workspace)
    assert res.command == f"{workspace}/bin/srv" and res.args == [str(workspace)]
    assert spec.unresolved_problems() == []


def test_hidden_whitespace_warning_names_fields_not_values():
    spec = ServerSpec.from_dict("w", {"type": "http", "url": "https://h/mcp ", "headers": {"Authorization": "Bearer t\n"}})
    (warning,) = spec.config_warnings()
    assert "url" in warning and "headers.Authorization" in warning and "Bearer" not in warning


def test_expand_vars_mask():
    assert expand_vars("${A:-d}", {"A": "x"}, frozenset({"A"})) == ""


# ─── precedence, dedupe, conflicts ──────────────────────────────────────────

def test_same_name_in_two_scopes_warns_and_dedupes_plugin_endpoint(workspace: Path):
    cfg = MCPConfig(workspace)
    _write(cfg.global_config_file, {"mcpServers": {"db": {"type": "http", "url": "https://a/mcp"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"db": {"type": "http", "url": "https://b/mcp"}}})
    specs = cfg.load_specs()
    assert specs["db"].url == "https://b/mcp"          # project beats user
    assert set(cfg.conflicts["db"]) == {("user", "https://a/mcp"), ("project", "https://b/mcp")}
    assert normalize_url("HTTPS://Host.com:443/x/") == normalize_url("https://host.com/x")
    assert ServerSpec.from_dict("a", {"type": "http", "url": "https://H.com:443/m/"}).endpoint_key() == \
        ServerSpec.from_dict("b", {"type": "http", "url": "https://h.com/m"}).endpoint_key()


# ─── per-project state: off switch and approvals ────────────────────────────

def test_toggle_project_server_does_not_edit_the_shared_file(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"s": {"command": "x"}}})
    before = (workspace / ".mcp.json").read_text()
    cfg = MCPConfig(workspace)
    assert cfg.toggle("s", True) == "project state"
    assert cfg.get_spec("s").disabled and cfg.get_spec("s").disabled_by == "project"
    assert (workspace / ".mcp.json").read_text() == before
    cfg.toggle("s", False)
    assert not cfg.get_spec("s").disabled


def test_project_servers_wait_for_approval_in_an_interactive_session(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"proj": {"command": "x"}}})
    cfg = MCPConfig(workspace)
    state.set_interactive(False)                       # -p / SDK / tests: loaded without asking
    assert cfg.get_spec("proj").approval == "approved"
    state.set_interactive(True)
    assert cfg.get_spec("proj").approval == "pending"
    answers = iter(["a"])
    specs = cfg.load_specs()
    assert approval.review(workspace, specs, ask=lambda _p: next(answers), out=lambda _m: None) == 1
    assert specs["proj"].approval == "approved"
    assert cfg.get_spec("proj").approval == "approved"      # remembered
    assert state.reset_project_choices(workspace) == 1
    assert cfg.get_spec("proj").approval == "pending"


def test_review_enter_leaves_undecided_and_n_rejects(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    state.set_interactive(True)
    cfg = MCPConfig(workspace)
    specs = cfg.load_specs()
    assert approval.review(workspace, specs, ask=lambda _p: "", out=lambda _m: None) == 0
    assert approval.review(workspace, specs, ask=lambda _p: "n", out=lambda _m: None) == 2
    assert cfg.get_spec("a").approval == "rejected"


def test_settings_can_approve_and_reject(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    _write(workspace / ".axon" / "settings.local.json", {"enabledMcpjsonServers": ["a"], "disabledMcpjsonServers": ["b"]})
    state.set_interactive(True)
    specs = MCPConfig(workspace).load_specs()
    assert specs["a"].approval == "approved" and specs["b"].approval == "rejected"


def test_committed_project_settings_cannot_approve_their_own_servers(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}}})
    _write(workspace / ".claude" / "settings.json", {"enableAllProjectMcpServers": True})
    state.set_interactive(True)
    assert MCPConfig(workspace).load_specs()["a"].approval == "pending"


# ─── allow / deny lists ─────────────────────────────────────────────────────

def test_url_pattern_matching_follows_the_documented_table():
    assert url_matches("https://mcp.example.com/*", "https://mcp.example.com/api")
    assert not url_matches("https://mcp.example.com/*", "https://mcp.example.com:8443/api")
    assert url_matches("https://mcp.example.com", "https://mcp.example.com/any/path")
    assert url_matches("https://mcp.example.com:8443/*", "https://mcp.example.com:8443/x")
    assert url_matches("https://mcp.example.com:*/*", "https://mcp.example.com/x")
    assert url_matches("https://mcp.example.com:*/*", "https://mcp.example.com:9999/x")
    assert url_matches("https://*.example.com/*", "https://a.example.com:9999/x")
    assert not url_matches("https://*.example.com/*", "https://example.org/x")
    assert url_matches("http://localhost:*/*", "http://localhost:3000/mcp")
    assert url_matches("*://mcp.example.com/*", "http://mcp.example.com/x")
    assert not url_matches("*://mcp.example.com/*", "http://mcp.example.com:81/x")
    assert url_matches("https://Mcp.Example.com/*", "https://mcp.example.com/api")


def _settings(workspace: Path, data: dict) -> None:
    _write(workspace / ".axon" / "settings.json", data)


def test_allowlist_and_denylist(workspace: Path):
    _settings(workspace, {
        "allowedMcpServers": [{"serverUrl": "https://*.example.com/*"}, {"serverCommand": ["npx", "-y", "ok"]}],
        "deniedMcpServers": [{"serverUrl": "https://staging.example.com/*"}, {"serverName": "bad"}]})
    _write(workspace / ".mcp.json", {"mcpServers": {
        "good": {"type": "http", "url": "https://mcp.example.com/api"},
        "staging": {"type": "http", "url": "https://staging.example.com/api"},
        "staging8443": {"type": "http", "url": "https://staging.example.com:8443/api"},
        "other": {"type": "http", "url": "https://other.com/mcp"},
        "cmd-ok": {"command": "npx", "args": ["-y", "ok"]},
        "cmd-no": {"command": "node", "args": ["s.js"]},
        "bad": {"type": "http", "url": "https://mcp.example.com/x"}}})
    cfg = MCPConfig(workspace)
    specs = cfg.load_specs()
    assert set(specs) == {"good", "staging8443", "cmd-ok"}
    assert dict(cfg.blocked) == {"staging": "denied", "other": "not-allowed", "cmd-no": "not-allowed", "bad": "denied"}
    pol = load_policy(workspace)
    assert "explicitly blocked" in pol.reason(ServerSpec.from_dict("bad", {"command": "x"}))
    assert "not allowed by enterprise policy" in pol.reason(ServerSpec.from_dict("n", {"command": "zzz"}))


def test_server_name_allow_entries_only_count_without_typed_entries(workspace: Path):
    _settings(workspace, {"allowedMcpServers": [{"serverName": "github"}]})
    _write(workspace / ".mcp.json", {"mcpServers": {"github": {"command": "any"}, "other": {"command": "x"}}})
    assert set(MCPConfig(workspace).load_specs()) == {"github"}
    _settings(workspace, {"allowedMcpServers": [{"serverName": "github"}, {"serverCommand": ["node", "a.js"]}]})
    assert set(MCPConfig(workspace).load_specs()) == set()   # stdio must match a command entry


def test_empty_allowlist_blocks_everything_unset_allows_all(workspace: Path):
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}}})
    assert set(MCPConfig(workspace).load_specs()) == {"a"}
    _settings(workspace, {"allowedMcpServers": []})
    assert set(MCPConfig(workspace).load_specs()) == set()


def test_allow_managed_only_ignores_user_allowlists(workspace: Path, _managed_dir: Path):
    _settings(workspace, {"allowedMcpServers": [{"serverName": "a"}]})
    _write(workspace / ".mcp.json", {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    _write(_managed_dir / "managed-settings.json", {"allowManagedMcpServersOnly": True,
                                                     "allowedMcpServers": [{"serverName": "b"}]})
    assert set(MCPConfig(workspace).load_specs()) == {"b"}


def test_managed_mcp_json_has_exclusive_control(workspace: Path, _managed_dir: Path):
    _write(_managed_dir / "managed-mcp.json", {"mcpServers": {"corp": {"type": "http", "url": "https://corp/mcp"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"mine": {"command": "x"}}})
    cfg = MCPConfig(workspace)
    specs = cfg.load_specs()
    assert set(specs) == {"corp"} and specs["corp"].managed and specs["corp"].scope == "managed"
    assert "exclusive control" in (cfg.add_block_reason(ServerSpec.from_dict("n", {"command": "x"})) or "")
    assert "enterprise MCP config" in (cli_conflict(workspace, cli_servers=True, strict=False) or "")
    assert cli_conflict(workspace, cli_servers=False, strict=True)
    (_managed_dir / "managed-mcp.json").write_text('{"mcpServers": {}}')
    assert MCPConfig(workspace).load_specs() == {}      # an empty map disables MCP


def test_managed_servers_setting_is_validated_and_outranks_user_servers(workspace: Path, _managed_dir: Path):
    _write(_managed_dir / "managed-settings.json", {"managedMcpServers": {
        "search": {"type": "http", "url": "https://search.example.com/mcp"},
        "plain": {"type": "http", "url": "http://insecure/mcp"},
        "prog": {"type": "http", "url": "https://x/mcp", "command": "rm"},
        "vars": {"type": "http", "url": "https://x/${HOME}"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"search": {"command": "mine"}}})
    cfg = MCPConfig(workspace)
    specs = cfg.load_specs()
    assert set(specs) == {"search"} and specs["search"].managed and specs["search"].url.startswith("https://search")
    assert sum("managedMcpServers: dropped" in n for n in cfg.notices) == 3
    assert "provided by your organization" in cfg.add_block_reason(ServerSpec.from_dict("search", {"command": "x"}))
    assert cfg.toggle("search", True) is None


def test_denylist_applies_to_managed_servers_too(workspace: Path, _managed_dir: Path):
    _write(_managed_dir / "managed-mcp.json", {"mcpServers": {"corp": {"type": "http", "url": "https://corp/mcp"}}})
    _settings(workspace, {"deniedMcpServers": [{"serverName": "corp"}]})
    assert MCPConfig(workspace).load_specs() == {}
