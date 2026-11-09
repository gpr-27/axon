"""permissions.disableBypassPermissionsMode: the engine runs bypass as default, so the UI must not offer or claim it."""
from __future__ import annotations

from types import SimpleNamespace

from axon.commands import model_cmds
from axon.ui import input as ui_input


def test_cycle_skips_a_locked_mode():
    assert ui_input.cycle_mode("plan", 1) == "bypass"
    assert ui_input.cycle_mode("plan", 1, locked=("bypass",)) == "default"
    assert ui_input.cycle_mode("bypass", 1, locked=("bypass",)) == "default"


def test_shift_tab_locks_bypass_when_settings_disable_it():
    agent = SimpleNamespace(permissions=SimpleNamespace(bypass_disabled=True))
    ui_input.set_prompt_agent(agent)
    try:
        assert ui_input.locked_modes() == ("bypass",)
        agent.permissions.bypass_disabled = False
        assert ui_input.locked_modes() == ()
    finally:
        ui_input.set_prompt_agent(None)


def test_mode_command_refuses_a_disabled_bypass(capsys, monkeypatch):
    changes: list[str] = []
    monkeypatch.setattr(model_cmds, "update_settings", lambda agent, **kw: changes.append(kw["mode"]))
    agent = SimpleNamespace(settings=SimpleNamespace(mode="plan"), permissions=SimpleNamespace(bypass_disabled=True))
    model_cmds.set_mode(agent, "bypass")
    assert changes == []
    assert "disableBypassPermissionsMode" in capsys.readouterr().out
    agent.permissions.bypass_disabled = False
    model_cmds.set_mode(agent, "bypass")
    assert changes == ["bypass"]
