"""/permissions refuses text that is not a rule (the web app's rule box sends the same command)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axon.commands.perm_cmds import handle_permissions
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import validate_rule_spec

BAD = ["NotARule(((", "Bash(git status", "(x)", "two words", "Bash(", "rm -rf /"]
GOOD = ["Bash(git status:*)", "Edit(src/**)", "Read", "WebFetch(domain:docs.python.org)", "mcp__github", "Agent(Explore)", 'Bash(echo "(")']


@pytest.mark.parametrize("spec", BAD)
def test_junk_is_refused_with_a_plain_message(spec):
    with pytest.raises(ValueError, match="is not a rule"):
        validate_rule_spec(spec)


@pytest.mark.parametrize("spec", GOOD)
def test_real_rules_pass(spec):
    assert validate_rule_spec(spec) == spec


@pytest.mark.parametrize("sub", ["allow", "deny", "ask", "session allow"])
def test_command_refuses_junk_and_writes_nothing(workspace, capsys, sub):
    s = Settings(workspace=workspace, mode="default", api_key="k")
    agent = SimpleNamespace(settings=s, permissions=PermissionEngine(s))
    handle_permissions(agent, f"{sub} NotARule(((")
    out = capsys.readouterr().out
    assert "is not a rule" in out and "Traceback" not in out
    assert agent.permissions.list_rules() == []
    assert not (workspace / ".axon" / "settings.local.json").exists()


def test_bad_rules_already_in_a_settings_file_do_not_crash_loading(workspace):
    d = workspace / ".axon"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({"permissions": {"allow": ["NotARule(((", 5, None, "Read"], "deny": [{"x": 1}]}}))
    engine = PermissionEngine(Settings(workspace=workspace, mode="default", api_key="k"))
    assert "Read" in [str(r) for r in engine.allow_rules]
