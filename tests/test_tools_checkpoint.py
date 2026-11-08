"""CheckpointList / CheckpointUndo over the real CheckpointManager."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.session.checkpoint import CheckpointManager
from axon.tools import create_default_registry
from axon.tools.base import ToolContext


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=Settings(workspace=workspace), checkpoints=CheckpointManager(workspace))


@pytest.fixture
def reg():
    return create_default_registry()


def test_permissions(reg):
    assert reg.get("CheckpointList").readonly and reg.get("CheckpointList").default_permission == "allow"
    assert not reg.get("CheckpointUndo").readonly and reg.get("CheckpointUndo").default_permission == "ask"


def test_list_empty_then_undo_error(reg, ctx):
    assert "No checkpoints" in reg.get("CheckpointList").run({}, ctx)
    with pytest.raises(ToolError, match="Nothing to undo"):
        reg.get("CheckpointUndo").run({}, ctx)


def test_undo_edit_and_created_file(reg, ctx, workspace):
    f = workspace / "a.txt"
    f.write_text("original\n", encoding="utf-8")
    reg.get("Read").run({"path": "a.txt"}, ctx)
    reg.get("Edit").run({"path": "a.txt", "old_string": "original", "new_string": "changed"}, ctx)
    ctx.checkpoints.commit_turn_snapshot(label="turn one")
    reg.get("Write").run({"path": "new.txt", "content": "hi"}, ctx)
    ctx.checkpoints.commit_turn_snapshot(label="turn two")
    listing = reg.get("CheckpointList").run({}, ctx)
    assert "2 checkpoints" in listing and "new.txt (new)" in listing and "turn one" in listing
    out = reg.get("CheckpointUndo").run({}, ctx)
    assert not (workspace / "new.txt").exists() and "Deleted newly created" in out
    assert f.read_text(encoding="utf-8") == "changed\n"
    reg.get("CheckpointUndo").run({"steps": 1}, ctx)
    assert f.read_text(encoding="utf-8") == "original\n"
    # the file must be re-read before the next edit
    with pytest.raises(Exception, match="not read|have not read"):
        reg.get("Edit").run({"path": "a.txt", "old_string": "original", "new_string": "x"}, ctx)


def test_undo_uncommitted_current_turn(reg, ctx, workspace):
    reg.get("Write").run({"path": "tmp.txt", "content": "x"}, ctx)
    reg.get("CheckpointUndo").run({}, ctx)
    assert not (workspace / "tmp.txt").exists()


def test_bad_steps_and_no_manager(reg, ctx):
    with pytest.raises(ToolError, match="steps"):
        reg.get("CheckpointUndo").run({"steps": 0}, ctx)
    ctx.checkpoints = None
    with pytest.raises(ToolError, match="not available"):
        reg.get("CheckpointList").run({}, ctx)
