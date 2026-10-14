"""MCP config merging/expansion/persistence and the `/mcp` command end to end."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.config import Settings
from axon.mcp.bridge import get_bridge, list_prompts, get_prompt, shutdown_mcp_bridge, initialize_mcp_bridge
from axon.mcp.commands import handle_mcp_command
from axon.mcp.config import MCPConfig, ServerSpec, expand_vars
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from tests.fakes import FakeProvider, scripted
from tests.suite.test_mcp_fakes import fake_server  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_bridge():
    shutdown_mcp_bridge()
    yield
    shutdown_mcp_bridge()


def _agent(ws: Path) -> Agent:
    settings = Settings(workspace=ws)
    return Agent(provider=FakeProvider(scripted([], "ok")), tools=create_default_registry(),
                 permissions=PermissionEngine(settings), context=ContextManager(settings),
                 session=SessionStore(ws), ledger=Ledger(), settings=settings)


def test_expand_vars(monkeypatch):
    monkeypatch.setenv("TOK", "abc")
    monkeypatch.delenv("NOPE", raising=False)
    assert expand_vars("Bearer ${TOK}") == "Bearer abc"
    assert expand_vars("${NOPE:-fallback}/x") == "fallback/x"
    assert expand_vars("${NOPE}") == ""


def test_merge_precedence_and_claude_code_format(workspace: Path):
    cfg = MCPConfig(workspace)
    cfg.global_config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.global_config_file.write_text(json.dumps({"mcpServers": {
        "a": {"command": "user-a"}, "b": {"command": "user-b"}}}), encoding="utf-8")
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "b": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${T}"}},
        "c": {"url": "https://y/sse", "type": "sse", "timeout": 5000}}}), encoding="utf-8")
    (workspace / ".axon").mkdir()
    (workspace / ".axon" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "a": {"command": "local-a", "disabled": True}}}), encoding="utf-8")
    specs = cfg.load_specs()
    assert specs["a"].command == "local-a" and specs["a"].disabled and specs["a"].scope == "local"
    assert specs["b"].type == "http" and specs["b"].scope == "project"
    assert specs["c"].type == "sse" and specs["c"].timeout == 5.0
    assert "a" not in cfg.load_specs(include_disabled=False)


def test_preset_stores_env_reference_not_placeholder(workspace: Path):
    cfg = MCPConfig(workspace)
    assert cfg.add_preset("github")
    raw = cfg.get_all_servers()["github"]
    assert raw["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${GITHUB_PERSONAL_ACCESS_TOKEN}"
    spec = cfg.get_spec("github")
    assert spec is not None and spec.unresolved_problems()  # not set in env -> needs config


def test_import_keeps_workspace_servers_local(workspace: Path):
    cfg = MCPConfig(workspace)
    (workspace / ".cursor").mkdir()
    (workspace / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
        "proj": {"command": "x"}, "web": {"url": "https://z/mcp"}, "broken": {"command": ""}}}),
        encoding="utf-8")
    assert cfg.import_from_all_discovered() == 2
    specs = cfg.load_specs()
    assert specs["proj"].scope == "local" and specs["web"].type == "http"
    assert "broken" not in specs
    assert not cfg.global_config_file.exists()


def test_set_disabled_and_remove(workspace: Path):
    cfg = MCPConfig(workspace)
    cfg.save_spec(ServerSpec(name="s", command="x"), "project")
    assert cfg.set_disabled("s", True) == workspace / ".mcp.json"
    assert cfg.get_spec("s").disabled  # type: ignore[union-attr]
    cfg.set_disabled("s", False)
    assert not cfg.get_spec("s").disabled  # type: ignore[union-attr]
    assert cfg.remove_server("s") and cfg.get_spec("s") is None


def test_mcp_command_lifecycle(workspace: Path, fake_server: Path, capsys):
    agent = _agent(workspace)
    cmd = f"add fake {shlex.quote(sys.executable)} {shlex.quote(str(fake_server))}"
    handle_mcp_command(agent, cmd)
    out = capsys.readouterr().out
    assert "connected" in out, out
    assert "mcp__fake__echo" in agent.registry.tools
    assert MCPConfig(workspace).get_spec("fake").args == [str(fake_server)]  # type: ignore[union-attr]

    handle_mcp_command(agent, "list")
    out = capsys.readouterr().out
    assert "fake" in out and "7 tools" in out

    handle_mcp_command(agent, "tools fake")
    assert "mcp__fake__echo" in capsys.readouterr().out
    handle_mcp_command(agent, "resources")
    assert "file:///readme.md" in capsys.readouterr().out
    handle_mcp_command(agent, "prompts")
    assert "review" in capsys.readouterr().out
    handle_mcp_command(agent, "get-prompt fake review lang=rust")
    assert "Review this rust code" in capsys.readouterr().out
    assert list_prompts()[0][:2] == ("fake", "review")
    assert get_prompt("fake", "review", {"lang": "c"}) == "Review this c code"
    handle_mcp_command(agent, "logs fake")
    assert "fake server starting" in capsys.readouterr().out

    handle_mcp_command(agent, "disable fake")
    assert "mcp__fake__echo" not in agent.registry.tools
    handle_mcp_command(agent, "enable fake")
    assert "mcp__fake__echo" in agent.registry.tools
    handle_mcp_command(agent, "restart fake")
    assert "mcp__fake__echo" in agent.registry.tools
    assert len(get_bridge().connections) == 1

    handle_mcp_command(agent, "remove fake")
    assert "mcp__fake__echo" not in agent.registry.tools
    assert MCPConfig(workspace).get_spec("fake") is None
    assert "read_file" in agent.registry.tools or len(agent.registry.tools) > 3  # builtins untouched


def test_mcp_add_http_and_add_json(workspace: Path, capsys):
    agent = _agent(workspace)
    handle_mcp_command(agent, "add --transport http --scope project api https://127.0.0.1:9/mcp "
                              "--header 'Authorization: Bearer ${API_TOKEN}'")
    raw = json.loads((workspace / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["api"]
    assert raw == {"type": "http", "url": "https://127.0.0.1:9/mcp",
                   "headers": {"Authorization": "Bearer ${API_TOKEN}"}}
    out = capsys.readouterr().out
    assert "needs-config" in out  # ${API_TOKEN} unset -> not even attempted

    handle_mcp_command(agent, 'add-json j {"command": "definitely-missing-cmd-xyz", "args": ["-v"]} --scope local')
    raw = json.loads((workspace / ".axon" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["j"]
    assert raw == {"command": "definitely-missing-cmd-xyz", "args": ["-v"]}
    assert "failed" in capsys.readouterr().out


def test_mcp_add_preset_noninteractive(workspace: Path, capsys, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    agent = _agent(workspace)
    handle_mcp_command(agent, "add brave-search")
    out = capsys.readouterr().out
    assert "BRAVE_API_KEY" in out and "needs-config" in out


def test_mcp_no_args_and_unknown(workspace: Path, capsys):
    agent = _agent(workspace)
    handle_mcp_command(agent, "")
    assert "No MCP servers configured" in capsys.readouterr().out
    handle_mcp_command(agent, "frobnicate")
    assert "unknown subcommand" in capsys.readouterr().out


def test_initialize_bridge_from_config(workspace: Path, fake_server: Path):
    cfg = MCPConfig(workspace)
    cfg.save_spec(ServerSpec(name="fk", command=sys.executable, args=[str(fake_server)]), "local")
    cfg.save_spec(ServerSpec(name="off", command="x", disabled=True), "local")
    reg = create_default_registry()
    tools = initialize_mcp_bridge(workspace, registry=reg, timeout=10)
    assert any(t.name == "mcp__fk__echo" for t in tools)
    assert "mcp__fk__echo" in reg.tools
    st = {r["name"]: r["status"] for r in get_bridge().status_rows()}
    assert st == {"fk": "connected", "off": "disabled"}
