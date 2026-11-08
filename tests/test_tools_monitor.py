"""Monitor waits on background shells started by Bash."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.tools import create_default_registry
from axon.tools.base import ToolContext


@pytest.fixture
def ctx(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=workspace))


def _start(reg, ctx, cmd: str) -> str:
    out = reg.get("Bash").run({"command": cmd, "run_in_background": True}, ctx)
    import re
    return re.search(r"bash_\d+", out).group(0)


@pytest.fixture
def reg():
    return create_default_registry()


def test_registered_readonly_allow(reg):
    t = reg.get("Monitor")
    assert t.readonly and t.default_permission == "allow"


def test_returns_on_match_before_exit(reg, ctx):
    sid = _start(reg, ctx, f'\"{sys.executable}\" -c "import time; print(\'booting\', flush=True); time.sleep(0.5); print(\'ready on 8080\', flush=True); time.sleep(30)"')
    t0 = time.time()
    out = reg.get("Monitor").run({"shell_id": sid, "pattern": r"ready on \d+", "timeout_s": 20}, ctx)
    assert "matched" in out and "ready on 8080" in out and "booting" in out
    assert time.time() - t0 < 15
    reg.get("KillShell").run({"shell_id": sid}, ctx)


def test_returns_on_exit(reg, ctx):
    sid = _start(reg, ctx, f'\"{sys.executable}\" -c "print(\'done\'); raise SystemExit(3)"')
    out = reg.get("Monitor").run({"shell_id": sid, "pattern": "never-appears", "timeout_s": 20}, ctx)
    assert "exited with code 3" in out and "done" in out


def test_timeout(reg, ctx):
    sid = _start(reg, ctx, f'\"{sys.executable}\" -c "import time; time.sleep(30)"')
    out = reg.get("Monitor").run({"shell_id": sid, "pattern": "x", "timeout_s": 1}, ctx)
    assert "timed out after 1s" in out and "running" in out
    reg.get("KillShell").run({"shell_id": sid}, ctx)


def test_bad_arguments(reg, ctx):
    with pytest.raises(ToolError, match="No background shell"):
        reg.get("Monitor").run({"shell_id": "bash_999"}, ctx)
    sid = _start(reg, ctx, f'\"{sys.executable}\" -c "import time; time.sleep(5)"')
    with pytest.raises(ToolError, match="regex"):
        reg.get("Monitor").run({"shell_id": sid, "pattern": "("}, ctx)
    with pytest.raises(ToolError, match="timeout_s"):
        reg.get("Monitor").run({"shell_id": sid, "timeout_s": "soon"}, ctx)
    reg.get("KillShell").run({"shell_id": sid}, ctx)
