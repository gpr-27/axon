"""/sandbox on|off|status and the sandbox.enabled setting."""
from __future__ import annotations

from types import SimpleNamespace

from axon.commands.registry import dispatch_command
from axon.config import Settings, map_json_settings
from axon.permissions import sandbox


def _agent(ws, **kw):
    return SimpleNamespace(settings=Settings(workspace=ws, **kw))


def test_status_text_is_printed(workspace, capsys):
    dispatch_command("/sandbox status", _agent(workspace))
    out = capsys.readouterr().out
    assert "Bash sandbox: off" in out and "Not enforced" in out


def test_on_off_round_trip_and_persists(workspace, capsys, monkeypatch):
    saved = {}
    monkeypatch.setattr("axon.commands.sandbox_cmds.update_settings_file", lambda u, **k: saved.update(u))
    monkeypatch.setattr(sandbox, "backend", lambda: ("sandbox-exec", ""))
    a = _agent(workspace)
    dispatch_command("/sandbox on", a)
    assert a.settings.sandbox_enabled is True and saved == {"sandbox.enabled": True}
    dispatch_command("/sandbox off", a)
    assert a.settings.sandbox_enabled is False


def test_on_refuses_when_no_backend(workspace, capsys, monkeypatch):
    monkeypatch.setattr(sandbox, "backend", lambda: (None, "bwrap (bubblewrap) is not installed"))
    a = _agent(workspace)
    dispatch_command("/sandbox on", a)
    assert a.settings.sandbox_enabled is False
    assert "unavailable" in capsys.readouterr().out


def test_settings_json_maps_nested_sandbox_keys():
    out = map_json_settings({"sandbox": {"enabled": True, "allowNetwork": True}, "lspDiagnosticsAfterEdit": False})
    assert out == {"sandbox_enabled": True, "sandbox_allow_network": True, "lsp_diagnostics_after_edit": False}


def test_linux_bwrap_argv_shape(workspace, monkeypatch):
    monkeypatch.setattr(sandbox, "backend", lambda: ("bwrap", ""))
    argv = sandbox.wrap_argv(["sh", "-c", "true"], Settings(workspace=workspace, sandbox_enabled=True))
    assert argv[0] == "bwrap" and "--unshare-net" in argv and argv[-3:] == ["sh", "-c", "true"]
    assert str(workspace.resolve()) in argv


def test_disabled_sandbox_leaves_argv_alone(workspace):
    assert sandbox.wrap_argv(["sh"], Settings(workspace=workspace)) == ["sh"]
