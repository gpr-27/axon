"""Hook events that used to be accepted but never fired: Setup, FileChanged, ConfigChange, Elicitation(Result)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from axon.hooks import HookManager, trust
from axon.hooks import manager as hm


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "axon_home"
    home.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home))
    trust.configure(False, None)
    return home


class S:               # minimal settings object: a non-None settings makes the manager the session's
    mode = "default"
    hooks: dict = {}
    disable_all_hooks = False


def make(tmp_path: Path, home: Path, event: str, matcher: str = "", exit_code: int = 0, out: str = "") -> tuple[HookManager, Path]:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    log = tmp_path / "log.txt"
    script = tmp_path / "hook.py"
    script.write_text("import sys, json\nd = json.load(sys.stdin)\n"
                      f"open({str(log)!r}, 'a').write(json.dumps(d) + '\\n')\n"
                      f"sys.stdout.write({out!r})\nsys.exit({exit_code})\n")
    import shlex
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    (home / "settings.json").write_text(json.dumps({"hooks": {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}]}}))
    mgr = HookManager(S(), ws)
    return mgr, log


def events(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_setup_fires_once_per_folder_on_first_start(tmp_path, _home):
    mgr, log = make(tmp_path, _home, "Setup")
    mgr.run("SessionStart", {"source": "resume"})
    assert events(log) == []                                   # only a fresh start counts
    mgr.run("SessionStart", {"source": "startup"})
    mgr.run("SessionStart", {"source": "startup"})
    got = events(log)
    assert len(got) == 1 and got[0]["hook_event_name"] == "Setup" and got[0]["trigger"] == "init"


def test_file_changed_fires_when_a_watched_file_changes(tmp_path, _home):
    mgr, log = make(tmp_path, _home, "FileChanged", matcher=".envrc|other.txt")
    ws = tmp_path / "ws"
    mgr.run("UserPromptSubmit", {"prompt": "hi"})              # baseline (file absent)
    assert events(log) == []
    (ws / ".envrc").write_text("export A=1")
    mgr.run("UserPromptSubmit", {"prompt": "hi"})
    (ws / ".envrc").write_text("export A=2")
    import os
    os.utime(ws / ".envrc", ns=(1, 2_000_000_000))
    mgr.run("PostToolUse", {"tool_name": "Bash"})
    mgr.run("PostToolUse", {"tool_name": "Bash"})               # unchanged: silent
    got = events(log)
    assert [(g["hook_event_name"], g["file_name"], g["event"]) for g in got] == [
        ("FileChanged", ".envrc", "add"), ("FileChanged", ".envrc", "change")]


def test_config_change_fires_on_settings_write_and_can_block(tmp_path, _home):
    from axon.commands import plugins as pl
    mgr, log = make(tmp_path, _home, "ConfigChange")
    pl.set_enabled_in_settings(tmp_path / "ws", "demo", True, "user")
    got = events(log)
    assert got and got[0]["hook_event_name"] == "ConfigChange" and got[0]["source"] == "user_settings"
    blocker, _ = make(tmp_path, _home, "ConfigChange", exit_code=2)
    with pytest.raises(ValueError, match="ConfigChange hook blocked"):
        pl.set_enabled_in_settings(tmp_path / "ws", "demo2", True, "user")
    assert "demo2" not in (_home / "settings.json").read_text()
    del blocker


def test_elicitation_hooks_wrap_the_dialog(tmp_path, _home):
    from axon.mcp.elicit import with_hooks
    mgr, log = make(tmp_path, _home, "Elicitation")
    called: list = []
    wrapped = with_hooks(lambda server, params: called.append(server) or {"action": "accept", "content": {"a": 1}})
    out = wrapped("srv", {"message": "need a", "requestedSchema": {"properties": {"a": {}}}})
    assert out["action"] == "accept" and called == ["srv"]
    got = events(log)
    assert got[0]["hook_event_name"] == "Elicitation" and got[0]["mcp_server_name"] == "srv" and got[0]["message"] == "need a"


def test_elicitation_hook_can_decline_without_a_dialog(tmp_path, _home):
    from axon.mcp.elicit import with_hooks
    mgr, _ = make(tmp_path, _home, "Elicitation", exit_code=2)
    wrapped = with_hooks(lambda s, p: pytest.fail("dialog must not open"))
    assert wrapped("srv", {"message": "m"})["action"] == "decline"


def test_elicitation_result_fires_after_the_answer(tmp_path, _home):
    from axon.mcp.elicit import with_hooks
    mgr, log = make(tmp_path, _home, "ElicitationResult")
    wrapped = with_hooks(lambda s, p: {"action": "cancel"})
    assert wrapped("srv", {"message": "m"})["action"] == "cancel"
    got = events(log)
    assert got[0]["hook_event_name"] == "ElicitationResult" and got[0]["action"] == "cancel"
