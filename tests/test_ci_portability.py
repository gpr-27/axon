"""Windows-shaped inputs, checked on every platform: git path arguments, the lease mutex, tidy paths, worktree folder."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from axon.permissions.rules import tool_paths


@pytest.mark.parametrize("arg", [r"C:\Users\me\.ssh\id_rsa", "C:/Users/me/.ssh/id_rsa", r"..\outside.txt", r"\\server\share\key"])
def test_git_path_arguments_in_windows_spelling_are_seen_as_paths(arg: str) -> None:
    assert arg in tool_paths("Git", {"subcommand": "diff", "args": ["HEAD", "--", arg]})
    assert arg in tool_paths("Git", {"subcommand": "blame", "args": [f"--contents={arg}", "a.txt"]})


def test_plain_git_arguments_are_not_paths() -> None:
    assert tool_paths("Git", {"subcommand": "log", "args": ["-n", "3", "main", "src/a.py"]}) == []


def test_create_lock_admits_one_process_at_a_time(tmp_path: Path) -> None:
    src = str(Path(__file__).resolve().parent.parent / "src")
    code = ("import sys, time\nsys.path.insert(0, %r)\nfrom pathlib import Path\nfrom axon.session import lease\n"
            "d, go = Path(sys.argv[1]), float(sys.argv[2])\nwhile time.time() < go: pass\n"
            "with lease._create_lock(d):\n    n = len(list(d.glob('in-*')))\n    (d / ('in-' + sys.argv[3])).write_text('x')\n"
            "    time.sleep(0.05)\n    (d / ('in-' + sys.argv[3])).unlink()\nprint(n, flush=True)\n") % src
    import time
    go = time.time() + 1.5
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(go), str(i)], stdout=subprocess.PIPE, text=True)
             for i in range(6)]
    assert [p.communicate()[0].strip() for p in procs] == ["0"] * 6      # nobody ever saw another inside


def test_stale_create_lock_is_taken_over(tmp_path: Path) -> None:
    import os
    from axon.session import lease
    stale = tmp_path / (lease.LOCK_FILE + ".d")
    stale.write_text("")
    os.utime(stale, (1, 1))
    with lease._create_lock(tmp_path, wait=2):
        assert stale.exists()
    assert not stale.exists()


def test_tidy_paths_accepts_either_separator() -> None:
    from axon.ui import tool_cards
    assert tool_cards.tidy_paths(r"x C:\w\proj/src C:/w/proj/b C:\w\proj\a.py", r"C:\w\proj") == "x src b a.py"


def test_windows_worktrees_live_in_a_short_folder(monkeypatch, tmp_path: Path) -> None:
    from axon import storage
    from axon.agent import worktree
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "h"))
    ws = tmp_path / ("deep-" * 20) / "repo"
    ws.mkdir(parents=True)
    legacy = worktree.worktrees_root(ws)
    monkeypatch.setattr(worktree, "IS_WINDOWS", True)
    short = worktree.worktrees_root(ws)
    assert short.parent == storage.axon_home() / "wt" and len(str(short)) < len(str(legacy))
    (legacy / "old" / ".git").mkdir(parents=True)
    assert worktree.worktrees_root(ws, "old") == legacy                  # a worktree made earlier is still found


def test_powershell_run_hands_on_the_programs_exit_code() -> None:
    import base64
    from axon import platform as plat
    info = plat.ShellInfo("pwsh", r"C:\pwsh\pwsh.exe", "PowerShell 7.4")
    argv = plat.shell_command_argv("python -c 'raise SystemExit(3)'", info)
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "exit $LASTEXITCODE" in script and argv[argv.index("-OutputFormat") + 1] == "Text"
