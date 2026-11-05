"""Regression: a hook process is killed when waiting on it fails for a reason other than a timeout."""
from pathlib import Path
from types import SimpleNamespace

from axon.hooks import manager as hook_manager
from axon.hooks.manager import HookCommand, HookManager


def test_command_hook_is_killed_when_communicate_fails(tmp_path: Path, monkeypatch) -> None:
    killed = []

    class FakeProc:
        returncode = None

        def communicate(self, data, timeout=None):
            raise OSError("broken pipe")

    monkeypatch.setattr(hook_manager.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(hook_manager, "_kill_tree", lambda proc: killed.append(proc))

    manager = HookManager(SimpleNamespace(hooks={}, workspace=tmp_path), tmp_path)
    hook = HookCommand("PreToolUse", "*", "echo hi")
    outcome = manager._exec_command(hook, "PreToolUse", "{}", 5)

    assert "broken pipe" in outcome.failure
    assert len(killed) == 1
