"""
Tests for the shell/process tools and OS integrations (clipboard, notifications), including
simulated Windows code paths.
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.errors import ToolError
from axon.tools.shell import BashOutputTool, BashTool, KillShellTool, _Capture, _find_shell_runner

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell syntax")


def ctx_for(ws: Path, timeout: int = 30, cap: int = 20000) -> SimpleNamespace:
    return SimpleNamespace(workspace=ws, settings=SimpleNamespace(bash_timeout_s=timeout, tool_output_cap=cap))


@posix_only
def test_bash_cwd_persists_and_exit_codes(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = BashTool()
    ctx = ctx_for(tmp_path)
    tool.run({"command": "cd sub", "description": "cd"}, ctx)
    out = tool.run({"command": "pwd", "description": "pwd"}, ctx)
    assert out.splitlines()[0].endswith("sub")
    res = tool.run({"command": "echo oops; exit 4", "description": "fail"}, ctx)
    assert "non-zero code 4" in res and "oops" in res
    assert "__AXON_CWD_" not in res


@posix_only
def test_bash_timeout_kills_tree(tmp_path: Path) -> None:
    tool = BashTool()
    t0 = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        tool.run({"command": "sleep 30 & sleep 30", "description": "hang", "timeout_s": 1}, ctx_for(tmp_path))
    assert time.monotonic() - t0 < 10


@posix_only
def test_bash_output_is_capped_and_utf8(tmp_path: Path) -> None:
    out = BashTool().run({"command": "printf 'é%.0s' $(seq 1 5000)", "description": "big"}, ctx_for(tmp_path, cap=2000))
    assert "elided by Axon" in out and "é" in out


@posix_only
def test_background_shell_tools(tmp_path: Path) -> None:
    ctx = ctx_for(tmp_path)
    started = BashTool().run({"command": "echo ready; sleep 30", "description": "bg", "run_in_background": True}, ctx)
    shell_id = started.split()[3]
    for _ in range(50):
        out = BashOutputTool().run({"shell_id": shell_id}, ctx)
        if "ready" in out:
            break
        time.sleep(0.05)
    assert "ready" in out and "running" in out
    assert "exited" in KillShellTool().run({"shell_id": shell_id}, ctx)
    with pytest.raises(ToolError):
        BashOutputTool().run({"shell_id": "nope"}, ctx)


def test_capture_keeps_head_and_tail() -> None:
    cap = _Capture(1000)
    for i in range(500):
        cap.add(f"{i:04d}\n")
    text = cap.text()
    assert text.startswith("0000") and "0499" in text and "elided" in text


def test_description_names_shell(monkeypatch) -> None:
    monkeypatch.setattr(plat, "shell_info", lambda: plat.ShellInfo("powershell", r"C:\ps\powershell.exe",
                                                                   "Windows PowerShell 5.1"))
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    desc = BashTool.description
    assert "Windows PowerShell 5.1" in desc and "PowerShell" in desc and "&&" in desc
    assert _find_shell_runner()[1] == "powershell"


def test_powershell_wrapper_tracks_cwd_and_exit_code() -> None:
    tool = BashTool()
    info = plat.ShellInfo("pwsh", r"C:\pwsh\pwsh.exe", "PowerShell 7.4")
    argv = tool._wrap("Get-ChildItem # trailing comment", info)
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "Get-ChildItem # trailing comment\n" in script     # comment cannot swallow the sentinel
    assert tool._sentinel in script and "exit $__axon_ec" in script


def test_cmd_wrapper_uses_delayed_expansion_unless_bang() -> None:
    tool = BashTool()
    info = plat.ShellInfo("cmd", r"C:\Windows\System32\cmd.exe", "cmd.exe")
    line = tool._wrap("dir", info)
    assert "/v:on" in line and "!CD!" in line and "chcp 65001" in line
    assert "/v:on" not in tool._wrap("echo hi!", info)


def test_extract_cwd(tmp_path: Path) -> None:
    tool = BashTool()
    text = f"output\n{tool._sentinel}{tmp_path}\n"
    assert tool._extract_cwd(text) == "output"
    assert tool._cwd == str(tmp_path)


def test_process_tool_windows_tasklist_parsing(monkeypatch, tmp_path: Path) -> None:
    import axon.tools.process_tool as pt
    csv = '"python.exe","1234","Console","1","50,000 K"\n"System","4","Services","0","1,000 K"\n'
    monkeypatch.setattr(pt.plat, "IS_WINDOWS", True)
    monkeypatch.setattr(pt.plat, "run_text", lambda argv, timeout=10.0: SimpleNamespace(stdout=csv))
    out = pt.ProcessTool().run({"action": "find", "pattern": "python"}, SimpleNamespace(workspace=tmp_path))
    assert "1234" in out and "python.exe" in out and "System" not in out


def test_process_tool_stats(tmp_path: Path) -> None:
    from axon.tools.process_tool import ProcessTool
    out = ProcessTool().run({"action": "stats"}, SimpleNamespace(workspace=tmp_path))
    assert "CPUs" in out and "Disk" in out


def test_clipboard_windows_uses_utf16_for_clip(monkeypatch) -> None:
    import axon.ui.clipboard as cb
    calls = []
    monkeypatch.setattr(cb.plat, "IS_WINDOWS", True)
    monkeypatch.setattr(cb.shutil, "which", lambda n: r"C:\Windows\System32\clip.exe" if n == "clip" else None)
    monkeypatch.setattr(cb, "_run", lambda argv, data=None, timeout=3.0: calls.append((argv, data)) or SimpleNamespace(returncode=0))
    assert cb.copy_to_clipboard("héllo ✓") is True
    assert calls[0][1] == ("\ufeff" + "héllo ✓").encode("utf-16-le")


def test_windows_notification_passes_text_as_data(monkeypatch) -> None:
    import axon.ui.notify as nt
    launched = {}
    monkeypatch.setattr(nt.sys, "platform", "win32")
    monkeypatch.setattr(nt.plat, "IS_WINDOWS", True)
    monkeypatch.setattr(nt.shutil, "which", lambda n: r"C:\ps\powershell.exe" if n == "powershell" else None)
    monkeypatch.setattr(nt.subprocess, "Popen", lambda argv, **kw: launched.update(argv=argv, env=kw["env"]))
    evil = 'done"; $(Remove-Item C:\\x) #'
    assert nt.send_desktop_notification("Axon", evil) is True
    script = base64.b64decode(launched["argv"][launched["argv"].index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "Remove-Item" not in script
    assert launched["env"]["AXON_NOTIFY_MSG"] == evil


def test_installers_are_windows_safe() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("install.ps1", "install.bat"):
        data = (root / name).read_bytes()
        assert all(b < 0x80 for b in data), f"{name} must be ASCII for PowerShell 5.1 / cmd"
        assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), f"{name} must use CRLF"
    bat = (root / "install.bat").read_text(encoding="ascii")
    assert "%errorlevel%" not in bat.lower()
    assert "py -3" in bat and "py" in (root / "install.ps1").read_text(encoding="ascii")
