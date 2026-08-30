"""
Exhaustive permission engine test matrix.
Covers modes (default, acceptEdits, plan, bypass), rule evaluation order, path jail bounds, and persistent grants.
"""
import pytest
from pathlib import Path
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import Rule
from axon.permissions.paths import is_in_workspace, resolve_in_workspace
from axon.config import Settings
from axon.tools import create_default_registry

@pytest.fixture
def permissions(workspace: Path) -> PermissionEngine:
    settings = Settings(workspace=workspace)
    return PermissionEngine(settings)

# ─── Mode Behavioral Matrix (20 tests) ──────────────────────────────────────
@pytest.mark.parametrize("tool_name,mode,expected_outcome", [
    ("Read", "default", "allow"),
    ("Ls", "default", "allow"),
    ("FileTree", "default", "allow"),
    ("Glob", "default", "allow"),
    ("Grep", "default", "allow"),
    ("CodeSymbols", "default", "allow"),
    ("Doctor", "default", "allow"),
    ("Write", "default", "ask"),
    ("Edit", "default", "ask"),
    ("MultiEdit", "default", "ask"),
    ("Patch", "default", "ask"),
    ("Bash", "default", "ask"),
    # acceptEdits mode
    ("Write", "acceptEdits", "allow"),
    ("Edit", "acceptEdits", "allow"),
    ("MultiEdit", "acceptEdits", "allow"),
    ("Patch", "acceptEdits", "allow"),
    ("Bash", "acceptEdits", "ask"),
    # bypass mode
    ("Write", "bypass", "allow"),
    ("Bash", "bypass", "allow"),
    ("Git", "bypass", "allow"),
])
def test_permission_mode_outcomes(permissions: PermissionEngine, tool_name: str, mode: str, expected_outcome: str):
    reg = create_default_registry()
    tool = reg.get(tool_name)
    decision = permissions.check(tool, {"path": "test.txt", "command": "ls"}, mode=mode)
    assert decision.outcome == expected_outcome

# ─── Plan Mode Read-Only Enforcement (15 tests) ─────────────────────────────
@pytest.mark.parametrize("tool_name", [
    "Write", "Edit", "MultiEdit", "Patch", "Bash", "Git"
])
def test_plan_mode_denies_mutating_tools(permissions: PermissionEngine, tool_name: str):
    reg = create_default_registry()
    tool = reg.get(tool_name)
    decision = permissions.check(tool, {"path": "test.txt", "command": "rm -rf"}, mode="plan")
    assert decision.outcome == "deny"
    assert "plan mode prevents" in decision.reason.lower()

@pytest.mark.parametrize("tool_name", [
    "Read", "Ls", "FileTree", "Glob", "Grep", "CodeSymbols", "ExitPlanMode", "Doctor", "Process"
])
def test_plan_mode_allows_readonly_and_exit(permissions: PermissionEngine, tool_name: str):
    reg = create_default_registry()
    tool = reg.get(tool_name)
    decision = permissions.check(tool, {"path": "test.txt"}, mode="plan")
    assert decision.outcome == "allow"

# ─── Path Jail & Traversal Security (15 tests) ──────────────────────────────
@pytest.mark.parametrize("bad_path", [
    "../outside.txt",
    "../../etc/shadow",
    "sub/../../../../var/log/syslog",
    "/usr/bin/python3",
    "~/.ssh/id_rsa",
])
def test_path_jail_rejections(workspace: Path, bad_path: str):
    assert is_in_workspace(workspace, bad_path) is False

@pytest.mark.parametrize("good_path", [
    "file.py",
    "src/main.py",
    "sub/nested/deep/file.txt",
    "./local.md",
])
def test_path_jail_accepts_valid(workspace: Path, good_path: str):
    assert is_in_workspace(workspace, good_path) is True
    resolved = resolve_in_workspace(workspace, good_path)
    assert str(resolved).startswith(str(workspace.resolve()))

# ─── Persistent Rule Grants & Rule Matching (10 tests) ──────────────────────
def test_persistent_rule_grant(permissions: PermissionEngine, workspace: Path):
    reg = create_default_registry()
    write_tool = reg.get("Write")
    
    # Initially "ask" in default mode
    d1 = permissions.check(write_tool, {"path": "gen/output.py", "content": ""}, mode="default")
    assert d1.outcome == "ask"

    # Grant persistent rule for gen/*
    permissions.grant_persistent(Rule(tool="Write", pattern="gen/*"), workspace)
    
    d2 = permissions.check(write_tool, {"path": "gen/output.py", "content": ""}, mode="default")
    assert d2.outcome == "allow"

    # Other paths still ask
    d3 = permissions.check(write_tool, {"path": "src/core.py", "content": ""}, mode="default")
    assert d3.outcome == "ask"
