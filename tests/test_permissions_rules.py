"""Permission rule syntax, invariants, modes, persistence and the path jail."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from axon.config import Settings
from axon.errors import PermissionDenied
from axon.permissions.engine import PermissionEngine, catastrophic_command
from axon.permissions.paths import is_in_workspace, is_within, resolve_in_workspace, system_dirs
from axon.permissions.rules import Rule, parse_rule, split_shell_commands
from axon.tools import create_default_registry

REG = create_default_registry()


def eng(workspace: Path, mode: str = "default", allow=(), deny=()) -> PermissionEngine:
    s = Settings(workspace=workspace, mode=mode, permissions={"allow": list(allow), "deny": list(deny)})
    return PermissionEngine(s)


@pytest.mark.parametrize("cmd,expected", [
    ("git status", True), ("git status -s", True), ("git statusx", False), ("git commit", False),
    ("git status && rm -rf build", False), ("git status; git status -s", True), ("git status $(whoami)", False),
])
def test_bash_prefix_allow(cmd: str, expected: bool):
    assert Rule("Bash", "git status:*").matches("Bash", {"command": cmd}) is expected


def test_deny_matches_any_subcommand_and_wrappers():
    r = parse_rule("Bash(rm:*)")
    assert r.matches("Bash", {"command": "echo hi && rm -rf x"}, for_deny=True)
    assert r.matches("Bash", {"command": "sudo rm file"}, for_deny=True)
    assert r.matches("Bash", {"command": "FOO=1 rm file"}, for_deny=True)
    assert not r.matches("Bash", {"command": "echo rm"}, for_deny=True)


def test_split_respects_quotes():
    assert split_shell_commands("echo 'a && b' && ls | wc -l") == ["echo 'a && b'", "ls", "wc -l"]
    assert split_shell_commands("make 2>&1") == ["make 2>&1"]


@pytest.mark.parametrize("cmd", ["rm -rf /", "sudo rm -rf /", "rm -fr /*", "rm -r -f ~", "rm --recursive --force $HOME",
                                 "ls && rm -rf /", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:"])
def test_catastrophic_commands(cmd: str):
    assert catastrophic_command(cmd)


@pytest.mark.parametrize("cmd", ["rm -rf build", "rm -rf /tmp/x/y", "rm file", "ls /"])
def test_safe_commands_not_invariant(cmd: str):
    assert catastrophic_command(cmd) is None


def test_path_rules_edit_glob_and_home(workspace: Path):
    e = eng(workspace, allow=["Edit(src/**)"], deny=["Read(~/secrets/**)"])
    write = REG.get("Write")
    assert e.check(write, {"path": "src/a/b.py"}).outcome == "allow"
    assert e.check(write, {"path": "docs/x.md"}).outcome == "ask"
    read = REG.get("Read")
    assert e.check(read, {"path": str(Path.home() / "secrets" / "k.txt")}).outcome == "deny"


def test_domain_and_mcp_rules(workspace: Path):
    assert Rule("WebFetch", "domain:python.org").matches("WebFetch", {"url": "https://docs.python.org/3/"})
    assert not Rule("WebFetch", "domain:python.org").matches("WebFetch", {"url": "https://evilpython.org/"})
    assert parse_rule("mcp__github").matches("mcp__github__create_issue", {})
    assert not parse_rule("mcp__github").matches("mcp__gitlab__x", {})


def test_modes(workspace: Path):
    e = eng(workspace)
    edit, bash, read, task = (REG.get(n) for n in ("Edit", "Bash", "Read", "Task"))
    assert e.check(edit, {"path": "a.py"}, "acceptEdits").outcome == "allow"
    assert e.check(edit, {"path": "../outside.py"}, "acceptEdits").outcome == "ask"
    assert e.check(bash, {"command": "ls"}, "plan").outcome == "deny"
    assert e.check(task, {"prompt": "x"}, "plan").outcome == "allow"
    assert e.check(read, {"path": str(Path(system_dirs()[0]) / "passwd")}, "bypass").outcome == "deny"
    assert e.check(read, {"path": str(Path.home() / ".ssh" / "id_rsa")}, "bypass").outcome == "deny"
    assert e.check(bash, {"command": "rm -rf /"}, "bypass").outcome == "deny"
    e2 = eng(workspace, deny=["Bash(curl:*)"])
    assert e2.check(bash, {"command": "curl x"}, "bypass").outcome == "deny"


def test_read_outside_workspace_asks(workspace: Path, tmp_path: Path):
    other = tmp_path / "other.txt"
    other.write_text("x", encoding="utf-8")
    assert eng(workspace).check(REG.get("Read"), {"path": str(other)}).outcome == "ask"


def test_grant_for_is_narrow_and_persisted(workspace: Path):
    e = eng(workspace)
    bash = REG.get("Bash")
    rule = e.grant_for(bash, {"command": "npm run build --prod"}, workspace)
    assert str(rule) == "Bash(npm run:*)"
    assert e.check(bash, {"command": "npm run test"}).outcome == "allow"
    assert e.check(bash, {"command": "npm install evil"}).outcome == "ask"
    r2 = e.grant_for(REG.get("Edit"), {"path": "src/app.py"}, workspace)
    assert str(r2) == "Edit(src/app.py)"
    saved = json.loads((workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))
    assert saved["permissions"]["allow"] == ["Bash(npm run:*)", "Edit(src/app.py)"]
    assert str(PermissionEngine.narrow_rule_for("Bash", {"command": "ls -la"}, workspace)) == "Bash(ls:*)"
    assert str(PermissionEngine.narrow_rule_for("Bash", {"command": "a && b"}, workspace)) == "Bash(a && b)"


def test_legacy_project_config_rules_are_loaded(workspace: Path):
    (workspace / ".axon").mkdir()
    (workspace / ".axon" / "config.toml").write_text('model = "x"\n[permissions]\ndeny = ["Bash(git push:*)"]\n', encoding="utf-8")
    e = eng(workspace)
    assert e.check(REG.get("Bash"), {"command": "git push origin"}, "bypass").outcome == "deny"
    assert e.remove_rule("deny", "Bash(git push:*)") == ["project-config"]
    assert 'model = "x"' in (workspace / ".axon" / "config.toml").read_text(encoding="utf-8")


def test_jail_uses_commonpath(workspace: Path):
    sibling = workspace.parent / (workspace.name + "2")
    sibling.mkdir()
    assert not is_within(workspace, sibling / "f")
    assert is_within(workspace, workspace / "a" / ".." / "b")
    assert is_in_workspace(workspace, "src/x.py")
    assert not is_in_workspace(workspace, str(sibling / "f"))
    with pytest.raises(PermissionDenied):
        resolve_in_workspace(workspace, str(sibling / "f"), allow_outside=False)
    resolve_in_workspace(workspace, str(sibling / "f"))  # default: engine gates outside paths


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "symlink"), reason="POSIX symlink test")
def test_symlink_escape_detected(workspace: Path, tmp_path: Path):
    target = tmp_path / "outside_dir"
    target.mkdir()
    (workspace / "link").symlink_to(target)
    assert not is_in_workspace(workspace, "link/file.txt")


def test_folder_named_system_is_not_blocked(workspace: Path):
    p = resolve_in_workspace(workspace, "system/SAM/config.py")
    assert p.name == "config.py"
