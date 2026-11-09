"""The Hooks panel's data carries timeout, if, async and status message for each hook."""
from types import SimpleNamespace

from axon.features.dash_workbench import hooks_list


def test_hooks_list_has_details(tmp_path):
    cfg = {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "if": "Bash(git *)", "command": "python check.py", "timeout": 60, "statusMessage": "Checking"}]}]}
    srv = SimpleNamespace(runtime=lambda *a, **k: None, settings_obj=lambda: SimpleNamespace(hooks=cfg, workspace=tmp_path), workspace=tmp_path)
    rows = hooks_list(srv, "x")["hooks"]
    row = next(r for r in rows if r["command"] == "python check.py")
    assert row["event"] == "PreToolUse" and row["matcher"] == "Bash"
    assert row["timeout"] == 60 and row["if"] == "Bash(git *)" and row["async"] is False and row["statusMessage"] == "Checking"


def test_hooks_list_reports_why_nothing_loaded(tmp_path):
    srv = SimpleNamespace(runtime=lambda *a, **k: None, settings_obj=lambda: SimpleNamespace(hooks={}, workspace=tmp_path), workspace=tmp_path)
    out = hooks_list(srv, "x")
    assert "problems" in out and isinstance(out["problems"], list)
