"""The last hook points: a blocked settings write, and the event for a displayed answer."""
from __future__ import annotations

from pathlib import Path

from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.commands import common


def test_config_change_hook_can_refuse_a_settings_write(tmp_path: Path, monkeypatch):
    from axon.hooks import manager
    monkeypatch.setenv("AXON_HOME", str(tmp_path))

    def refuse(path, kind="settings"):
        raise ValueError("blocked")
    monkeypatch.setattr(manager, "config_change", refuse)
    assert common.update_settings_file({"theme": "axon"}) is None
    assert not (tmp_path / "settings.json").exists()


def test_settings_write_proceeds_without_a_refusal(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path))
    assert common.update_settings_file({"theme": "axon"}) == tmp_path / "settings.json"


def test_message_display_fires_with_the_answer(workspace: Path, monkeypatch):
    agent = make_agent(FakeProvider(scripted("the answer")), workspace)
    seen = []
    original = agent._hook

    def spy(event, payload=None, *a, **k):
        seen.append((event, payload))
        return original(event, payload, *a, **k)
    monkeypatch.setattr(agent, "_hook", spy)
    agent.run_turn("hello")
    assert ("MessageDisplay", {"message": "the answer"}) in seen
