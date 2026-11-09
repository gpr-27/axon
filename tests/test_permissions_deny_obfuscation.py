"""A Bash deny rule must still catch the command when it is quoted, given by path, or run through a nested shell."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry

REG = create_default_registry()


def eng(workspace: Path, mode: str, deny=()) -> PermissionEngine:
    return PermissionEngine(Settings(workspace=workspace, mode=mode, permissions={"deny": list(deny)}))


@pytest.mark.parametrize("cmd", [
    "bash -c 'rm x'", "sh -c \"echo a; rm x\"", "eval 'rm x'", "/bin/rm x", "\\rm x", "'rm' x", 'r"m" x',
    "echo hi | bash -c 'bash -c \"rm x\"'", "/usr/bin/env rm x",
])
@pytest.mark.parametrize("mode", ["default", "bypass", "acceptEdits"])
def test_bash_deny_rule_sees_through_quotes_paths_and_nested_shells(workspace: Path, cmd: str, mode: str):
    e = eng(workspace, mode, deny=["Bash(rm *)"])
    assert e.check(REG.get("Bash"), {"command": cmd}).outcome == "deny"


@pytest.mark.parametrize("cmd", ["echo rm x", "bash -c 'echo hi'", "git rm x", "ls /bin/rmdir-notes", "echo 'rm x'"])
def test_bash_deny_rule_does_not_catch_other_commands(workspace: Path, cmd: str):
    e = eng(workspace, "bypass", deny=["Bash(rm *)"])
    assert e.check(REG.get("Bash"), {"command": cmd}).outcome != "deny"


def test_read_deny_rule_sees_files_read_inside_bash_c(workspace: Path):
    e = eng(workspace, "bypass", deny=["Read(.env)"])
    assert e.check(REG.get("Bash"), {"command": "bash -c 'cat .env'"}).outcome == "deny"
