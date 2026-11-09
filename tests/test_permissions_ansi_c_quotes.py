"""$'...' quoting lets a backslash escape the closing quote. The rule scanners must read it the way bash does,
or a command hides a second command (or a substitution, or a redirect) inside what looks like one quoted word."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import has_substitution, redirect_targets, split_shell_commands, substitution_bodies
from axon.tools import create_default_registry

REG = create_default_registry()


def test_split_sees_the_command_after_an_escaped_quote():
    assert split_shell_commands("ls $'a\\'b' ; touch pwned") == ["ls $'a\\'b'", "touch pwned"]
    assert split_shell_commands("ls $'\\'' ; touch pwned ; echo ''") == ["ls $'\\''", "touch pwned", "echo ''"]


def test_split_keeps_a_separator_inside_ansi_c_quotes_together():
    assert split_shell_commands("echo $'a\\'; b'") == ["echo $'a\\'; b'"]


def test_plain_single_quote_still_ends_at_the_next_quote():
    assert split_shell_commands("echo 'a\\' ; ls") == ["echo 'a\\'", "ls"]


def test_substitution_after_an_escaped_quote_is_found():
    cmd = "ls $'\\'' $(touch pwned) ''"
    assert has_substitution(cmd)
    assert substitution_bodies(cmd) == ["touch pwned"]


def test_substitution_inside_ansi_c_quotes_is_text():
    assert not has_substitution("echo $'\\'$(x)'")


def test_redirect_after_an_escaped_quote_is_found():
    assert redirect_targets("echo $'\\'' > /etc/x ''") == ["/etc/x"]


@pytest.mark.parametrize("cmd", [
    "ls $'a\\'b' ; touch pwned",
    "ls $'\\'' $(touch pwned) ''",
    "ls $'\\'' > /etc/x ''",
])
def test_allow_rule_does_not_approve_a_command_hidden_by_ansi_c_quotes(workspace: Path, cmd: str):
    e = PermissionEngine(Settings(workspace=workspace, mode="default", permissions={"allow": ["Bash(ls *)"]}))
    assert e.check(REG.get("Bash"), {"command": cmd}).outcome == "ask"


def test_deny_rule_sees_a_command_hidden_by_ansi_c_quotes(workspace: Path):
    e = PermissionEngine(Settings(workspace=workspace, mode="bypass", permissions={"deny": ["Bash(rm *)"]}))
    assert e.check(REG.get("Bash"), {"command": "ls $'a\\'b' ; rm x"}).outcome == "deny"
