"""/marketplace install and friends make the running session pick up new hooks, skills and plugin servers."""
from types import SimpleNamespace

from axon.commands import marketplace_cmds


def _agent(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path))


def test_changing_commands_refresh_the_session(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("axon.marketplace.cli.run_command", lambda words, ws, agent=None: SimpleNamespace(code=0, removed=[], installed=[], reconnect=[]))
    monkeypatch.setattr("axon.commands.agent_cmds._refresh_extensions", lambda a: seen.append("refresh"))
    for line in ("install fx", "list plugins", "remove fx", "status"):
        marketplace_cmds.handle_marketplace(_agent(tmp_path), line)
    assert seen == ["refresh", "refresh"]


def test_failed_change_does_not_refresh(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("axon.marketplace.cli.run_command", lambda words, ws, agent=None: SimpleNamespace(code=1, removed=[], installed=[], reconnect=[]))
    monkeypatch.setattr("axon.commands.agent_cmds._refresh_extensions", lambda a: seen.append("refresh"))
    marketplace_cmds.handle_marketplace(_agent(tmp_path), "install nope")
    assert seen == []
