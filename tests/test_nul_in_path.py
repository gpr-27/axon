"""A path with a NUL character gets a plain answer, not Python's raw ValueError text."""
import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry
from axon.tools.base import ToolContext, ToolError


@pytest.mark.parametrize("name,args", [("Write", {"path": "a\x00b.txt", "content": "x"}), ("Read", {"path": "a\x00b.txt"}),
                                       ("Edit", {"path": "a\x00b", "old_string": "a", "new_string": "b"})])
def test_nul_path_is_refused_plainly(workspace, name, args):
    st = Settings(workspace=workspace, api_key="k")
    tool = create_default_registry().get(name)
    verdict = PermissionEngine(st).check(tool, args, "default")          # must not raise
    assert verdict.outcome == "deny" and "NUL" in verdict.reason and "ValueError" not in verdict.reason
    ctx = ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=st)
    with pytest.raises(ToolError, match="NUL"):
        tool.run(args, ctx)
    assert list(workspace.iterdir()) == []
