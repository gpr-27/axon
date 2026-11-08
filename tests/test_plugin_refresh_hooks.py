"""Installing, enabling or removing a plugin re-reads hooks and plugin MCP servers in the running session."""
import json
from pathlib import Path
from types import SimpleNamespace

from axon.commands.agent_cmds import _refresh_extensions


def test_refresh_reloads_hooks_and_resyncs_servers(tmp_path, monkeypatch):
    calls = []
    agent = SimpleNamespace(
        skills=SimpleNamespace(discover=lambda: calls.append("skills")),
        hooks=SimpleNamespace(reload=lambda: calls.append("hooks")),
        settings=SimpleNamespace(workspace=tmp_path),
    )
    synced = []
    monkeypatch.setattr("axon.commands.agent_cmds._resync_plugin_servers", lambda a: synced.append(a))
    _refresh_extensions(agent)
    assert calls == ["skills", "hooks"] and synced == [agent]


def test_resync_drops_servers_of_a_removed_plugin_and_connects_new_ones(tmp_path, monkeypatch):
    from axon.commands import agent_cmds
    from axon.mcp import bridge as bridge_mod

    class Live:
        def __init__(self):
            self.connections = {"gone": SimpleNamespace(spec=SimpleNamespace(scope="plugin")),
                                "mine": SimpleNamespace(spec=SimpleNamespace(scope="project"))}
            self.dropped, self.connected = [], {}

        def disconnect_server(self, name):
            self.dropped.append(name)

        def connect_all(self, specs, timeout=None):
            self.connected.update(specs)

    live = Live()
    monkeypatch.setattr(bridge_mod, "_bridge", live)
    new_spec = SimpleNamespace(scope="plugin")
    monkeypatch.setattr("axon.mcp.config.MCPConfig.load_specs", lambda self, include_disabled=True: {"fresh": new_spec})
    agent_cmds._resync_plugin_servers(SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path)))
    assert live.dropped == ["gone"] and list(live.connected) == ["fresh"]
