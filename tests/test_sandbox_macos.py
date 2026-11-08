"""The Bash sandbox, run for real: sandbox-exec on macOS. The text-based shell guard cannot see a script
that opens a credential file itself; the sandbox can."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.permissions import sandbox
from axon.tools.base import ToolContext
from axon.tools.shell import BashTool

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                                reason="sandbox-exec is macOS only")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "fakehome"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_rsa").write_text("PRIVATE-KEY-MATERIAL")
    (h / ".aws").mkdir()
    (h / ".aws" / "credentials").write_text("aws-secret")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))   # conftest isolates Path.home too
    monkeypatch.setenv("AXON_HOME", str(h / ".axon"))
    return h


def _run(ws: Path, cmd: str, **settings) -> str:
    s = Settings(workspace=ws, sandbox_enabled=True, **settings)
    ctx = ToolContext(workspace=ws, file_state=FileState(), todos=TodoState(), settings=s)
    return BashTool().run({"command": cmd}, ctx)


def test_write_inside_workspace_works(workspace: Path, home: Path):
    out = _run(workspace, "echo hello > inside.txt && cat inside.txt")
    assert "hello" in out and (workspace / "inside.txt").read_text().strip() == "hello"


def test_write_outside_workspace_fails(workspace: Path, home: Path):
    outside = Path("/Users/Shared") / f"axon_sandbox_probe_{os.getpid()}.txt"
    try:
        out = _run(workspace, f"echo x > {outside}; echo rc=$?")
        assert not outside.exists()
        assert "rc=0" not in out and "sandbox" in out.lower()
    finally:
        outside.unlink(missing_ok=True)


def test_write_to_temp_dir_works(workspace: Path, home: Path, tmp_path: Path):
    t = tmp_path / "scratch.txt"
    _run(workspace, f"echo ok > {t}")
    assert t.read_text().strip() == "ok"


def test_reading_ssh_under_fake_home_fails(workspace: Path, home: Path):
    out = _run(workspace, f"cat {home}/.ssh/id_rsa; echo rc=$?")
    assert "PRIVATE-KEY-MATERIAL" not in out


def test_python_open_of_credential_store_fails(workspace: Path, home: Path):
    """The gap the text guard cannot close: the path never appears in the command line."""
    py = ("import os; p=os.path.join(os.environ['HOME'], '.a'+'ws', 'credentials'); "
          "print(open(p).read())")
    out = _run(workspace, f'python3 -c "{py}"; echo rc=$?')
    assert "aws-secret" not in out
    assert "PermissionError" in out or "Operation not permitted" in out


def test_reading_outside_credentials_still_works(workspace: Path, home: Path):
    out = _run(workspace, "cat /etc/hosts | head -1; ls /usr/bin | head -1")
    assert out.strip()


def test_network_is_off_by_default(workspace: Path, home: Path):
    out = _run(workspace, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\"; echo rc=$?")
    assert "rc=0" not in out


def test_denied_write_failure_is_labelled_as_sandbox(workspace: Path, home: Path):
    out = _run(workspace, "touch /Users/Shared/axon_probe_denied")
    assert "[sandbox]" in out


def test_profile_error_is_reported_as_a_sandbox_failure_not_a_command_failure(workspace: Path, home: Path, monkeypatch):
    monkeypatch.setattr(sandbox, "build_profile", lambda *a, **k: "(this is not valid sbpl")
    with pytest.raises(ToolError) as e:
        _run(workspace, "echo hi")
    assert "sandbox could not start" in str(e.value).lower()


def test_unavailable_backend_refuses_instead_of_running_unsandboxed(workspace: Path, home: Path, monkeypatch):
    monkeypatch.setattr(sandbox, "backend", lambda: (None, "bwrap not found"))
    with pytest.raises(ToolError) as e:
        _run(workspace, "echo should-not-run")
    assert "refus" in str(e.value).lower() and "bwrap not found" in str(e.value)


def test_off_by_default(workspace: Path, home: Path):
    s = Settings(workspace=workspace)
    assert s.sandbox_enabled is False and s.sandbox_allow_network is False


def test_status_says_what_is_and_is_not_enforced(workspace: Path, home: Path):
    text = sandbox.status_text(Settings(workspace=workspace, sandbox_enabled=True))
    assert "sandbox-exec" in text and "not enforced" in text.lower() and "Read" in text
