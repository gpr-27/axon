"""Plugin agents/hooks/MCP merging, todo persistence, MCP argv handling and menu safety."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.agent.state import TodoState


def _plugin(ws: Path, name: str = "pl") -> Path:
    root = ws / ".axon" / "plugins" / name
    (root / "agents").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "agents" / "pagent.md").write_text("---\nname: pagent\ndescription: d\n---\nHi\n", encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "echo ${CLAUDE_PLUGIN_ROOT}/x"}]}]}}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"psrv": {
        "command": "python", "args": ["${AXON_PLUGIN_ROOT}/s.py"]}}}), encoding="utf-8")
    return root


def _disable(ws: Path, name: str) -> None:
    (ws / ".axon" / "settings.json").write_text(json.dumps({"enabledPlugins": {name: False}}), encoding="utf-8")


def test_plugin_agents_loaded_and_disabled(workspace: Path) -> None:
    from axon.agent.subagent import load_agent_definitions
    _plugin(workspace)
    d = {a.name: a for a in load_agent_definitions(workspace)}
    assert d["pagent"].source == "plugin:pl"
    _disable(workspace, "pl")
    assert "pagent" not in {a.name for a in load_agent_definitions(workspace)}


def test_plugin_hooks_merged_with_root_expansion(workspace: Path) -> None:
    from axon.hooks import HookManager
    root = _plugin(workspace)
    hooks = [h for h in HookManager(None, workspace).describe() if h["source"] == "plugin:pl"]
    assert hooks and hooks[0]["event"] == "UserPromptSubmit"
    assert hooks[0]["command"] == f"echo {root}/x"
    _disable(workspace, "pl")
    assert not [h for h in HookManager(None, workspace).describe() if h["source"] == "plugin:pl"]


def test_plugin_mcp_lowest_precedence(workspace: Path) -> None:
    from axon.mcp.config import MCPConfig
    root = _plugin(workspace)
    cfg = MCPConfig(workspace)
    spec = cfg.load_specs()["psrv"]
    assert spec.scope == "plugin" and spec.args == [f"{root}/s.py"]
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"psrv": {"command": "node"}}}), encoding="utf-8")
    assert cfg.load_specs()["psrv"].command == "node"
    assert cfg.set_disabled("psrv", True) is not None  # project file wins, so it is editable
    (workspace / ".mcp.json").unlink()
    assert cfg.set_disabled("psrv", True) is None  # plugin specs are read-only


def test_todos_persist_and_restore_per_session(workspace: Path) -> None:
    sess = SimpleNamespace(active_session_id="s1")
    t = TodoState().bind(workspace, lambda: sess.active_session_id)
    t.replace([{"content": "a", "status": "in_progress", "activeForm": "Doing a"}, {"content": "b", "status": "pending"}])
    from axon.storage import todos_dir
    assert (todos_dir(workspace) / "s1.json").is_file()
    sess.active_session_id = "s2"
    assert t.items == []
    fresh = TodoState().bind(workspace, lambda: "s1")
    assert [x.content for x in fresh.items] == ["a", "b"] and fresh.items[0].active_form == "Doing a"
    assert "a" in fresh.render()


def test_todowrite_binds_via_agent_session(workspace: Path) -> None:
    from axon.agent.state import FileState
    from axon.tools.base import ToolContext
    from axon.tools.todo import TodoWriteTool
    agent = SimpleNamespace(session=SimpleNamespace(active_session_id="sx"))
    todos = TodoState()
    ctx = ToolContext(workspace=workspace, file_state=FileState(), todos=todos, settings=None, agent=agent)  # type: ignore[arg-type]
    TodoWriteTool().run({"todos": [{"content": "x", "status": "pending"}]}, ctx)
    assert [x.content for x in TodoState().bind(workspace, "sx").items] == ["x"]


def test_mcp_split_and_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.mcp import commands as c
    assert c._split('add s py "/d with space/s.py"') == ["add", "s", "py", "/d with space/s.py"]
    monkeypatch.setattr(c.os, "name", "nt")
    assert c._split(r'add s py "C:\d x\s.py" C:\a\b') == ["add", "s", "py", r"C:\d x\s.py", r"C:\a\b"]


def test_mcp_argv_list_keeps_spaces(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.mcp import commands as c
    seen: dict = {}
    monkeypatch.setattr(c, "_Ctx", lambda agent: SimpleNamespace())
    monkeypatch.setitem(c._COMMANDS, "add", lambda ctx, args: seen.setdefault("args", args))
    c.handle_mcp_command(SimpleNamespace(), ["add", "srv", "python", "/dir with space/s.py"])
    assert seen["args"] == ["srv", "python", "/dir with space/s.py"]


def test_mcp_menu_default_is_harmless_and_install_needs_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.mcp import commands as c
    picked: list[list[str]] = []
    monkeypatch.setattr(c, "_pick", lambda options, title: (picked.append(options), options[0])[1])
    monkeypatch.setattr(c, "_cmd_list", lambda ctx, a: None)
    c._menu(SimpleNamespace())
    assert picked[0][0] == "Close"
    # Installing a preset interactively: a stray Enter at the confirm prompt cancels.
    saved: list = []
    ctx = SimpleNamespace(config=SimpleNamespace(add_preset=lambda *a, **k: saved.append(a), global_config_file="x"),
                          connect=lambda n: saved.append(n))
    monkeypatch.setattr(c, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    c._install_preset(ctx, c.MCP_CATALOG[0])
    assert saved == []
    # The install picker's default entry is Cancel.
    picked.clear()
    c._cmd_install(ctx, [])
    assert picked[0][0] == "Cancel" and saved == []


def test_mcp_add_parses_double_dash_as_stdio_command() -> None:
    from axon.mcp.commands import _parse_add
    o = _parse_add(["github", "--env", "TOKEN=${TOKEN}", "--", "npx", "-y", "server-github"])
    assert o["transport"] == "stdio"
    assert o["positional"] == ["github", "npx"] and o["command_args"] == ["-y", "server-github"]
    assert o["env"] == {"TOKEN": "${TOKEN}"}
    o = _parse_add(["--transport", "http", "docs", "https://example.com/mcp", "--header", "A: b"])
    assert o["transport"] == "http" and o["headers"] == {"A": "b"}
