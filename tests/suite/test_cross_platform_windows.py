"""
Unit tests for cross-platform Windows, Linux, and macOS compatibility.
Verifies shell detection, termios/msvcrt import guards, doctor tool diagnostics, and setup scripts.
"""
import sys
from pathlib import Path
from axon.tools.shell import BashTool, _find_shell_runner
from axon.tools.doctor import DoctorTool
from axon.tools.process_tool import ProcessTool
from axon.tools.base import ToolContext
from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.ui.picker import pick
import axon.ui.input as ui_input
import axon.ui.picker as ui_picker
import axon.ui.switcher as ui_switcher
import axon.ui.subagent_monitor as ui_subagent_monitor


def test_find_shell_runner():
    runner, flavor = _find_shell_runner()
    assert isinstance(runner, list)
    assert len(runner) >= 2
    assert flavor in ("bash", "sh", "powershell", "cmd")


def test_bash_tool_execution(tmp_path: Path):
    settings = Settings(api_key="sk-test", workspace=tmp_path)
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    bash = BashTool()

    # Simple echo test
    res = bash.run({"command": "echo 'cross-platform-test'", "description": "test echo"}, ctx)
    assert "cross-platform-test" in res


def test_doctor_tool_platform_info(tmp_path: Path):
    settings = Settings(api_key="sk-test", workspace=tmp_path)
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    doc = DoctorTool()
    out = doc.run({}, ctx)

    assert "Platform       :" in out
    assert "Shell Engine   :" in out
    assert "Python Version :" in out


def test_process_tool_ports_and_list(tmp_path: Path):
    settings = Settings(api_key="sk-test", workspace=tmp_path)
    ctx = ToolContext(
        workspace=tmp_path,
        file_state=FileState(),
        todos=TodoState(),
        settings=settings,
    )
    proc = ProcessTool()

    ports_out = proc.run({"action": "ports"}, ctx)
    assert isinstance(ports_out, str)

    list_out = proc.run({"action": "list"}, ctx)
    assert isinstance(list_out, str)



def test_ui_modules_guarded_flags():
    assert hasattr(ui_input, "_HAS_TERMIOS")
    assert hasattr(ui_picker, "_HAS_TERMIOS")
    assert hasattr(ui_picker, "_HAS_MSVCRT")
    assert hasattr(ui_switcher, "_HAS_TERMIOS")
    assert hasattr(ui_subagent_monitor, "_HAS_TERMIOS")


def test_picker_non_interactive():
    # Non-interactive / non-tty should return default option without error
    chosen = pick(["alpha", "beta", "gamma"], title="Test", current="beta")
    assert chosen == "beta"
