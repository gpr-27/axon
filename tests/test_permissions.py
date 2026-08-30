"""
Tests for Permissions and Path jail.
"""
from pathlib import Path
import pytest
from axon.errors import PermissionDenied
from axon.permissions.engine import PermissionEngine
from axon.permissions.paths import resolve_in_workspace
from axon.permissions.rules import Rule
from axon.tools.fs_write import WriteTool
from axon.tools.shell import BashTool

def test_resolve_in_workspace(workspace: Path):
    safe_path = resolve_in_workspace(workspace, "sub/dir/file.txt")
    assert str(safe_path).startswith(str(workspace.resolve()))

    with pytest.raises(PermissionDenied):
        resolve_in_workspace(workspace, "/etc/shadow")

    with pytest.raises(PermissionDenied):
        resolve_in_workspace(workspace, "~/.ssh/id_rsa")

def test_permission_engine_modes(settings):
    # Default mode
    engine = PermissionEngine(settings.model_copy(update={"mode": "default"}))
    write_tool = WriteTool()
    dec = engine.check(write_tool, {"path": "a.txt", "content": "hi"}, "default")
    assert dec.outcome == "ask"

    # acceptEdits mode
    dec_edits = engine.check(write_tool, {"path": "a.txt", "content": "hi"}, "acceptEdits")
    assert dec_edits.outcome == "allow"

    # plan mode
    dec_plan = engine.check(write_tool, {"path": "a.txt", "content": "hi"}, "plan")
    assert dec_plan.outcome == "deny"

    # bypass mode
    dec_bypass = engine.check(write_tool, {"path": "a.txt", "content": "hi"}, "bypass")
    assert dec_bypass.outcome == "allow"

def test_hard_invariant_rm_rf(settings):
    engine = PermissionEngine(settings.model_copy(update={"mode": "bypass"}))
    bash_tool = BashTool()
    # Even in bypass mode, destructive root rm -rf is hard denied
    dec = engine.check(bash_tool, {"command": "rm -rf /", "description": "destroy"}, "bypass")
    assert dec.outcome == "deny"
