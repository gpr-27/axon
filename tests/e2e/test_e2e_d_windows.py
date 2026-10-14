"""
Agent-D e2e: cross-platform behaviour, with Windows simulated where it cannot run natively.

* legacy code pages (cp1252 / ascii) and ``AXON_ASCII`` on real ``axon -p`` runs,
* Windows shell selection (pwsh > powershell > cmd, WSL bash skipped) and command building,
* msvcrt / Win32 console key decoding tables,
* installers are ASCII-only with the right line endings,
* every module imports without POSIX-only modules, unicode / space workspace paths end to end.
"""
from __future__ import annotations

import base64
import ntpath
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from harness import REPO_ROOT, SRC, AxonEnv, IS_WINDOWS, create_axon_env
from test_e2e_d_helpers import TRACEBACK, open_repl, posix_only, run_raw, run_script, tool_turn

pytestmark = pytest.mark.e2e

UNICODE_ANSWER = "ok “quotes” ✓ — 中文 🎉"


def _turn_scripts(answer: str = "fin\n\n- a\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nx=1\n```"):
    return [[{"text": UNICODE_ANSWER}, {"tool": "Bash", "args": {"command": "echo hi", "description": "x"}}],
            [{"text": answer}]]


# ─── legacy code pages / ASCII ─────────────────────────────────────────────

@pytest.mark.parametrize("level", ["compact", "normal", "detailed", "debug"])
def test_cp1252_piped_stdout_no_unicode_error(axon_env: AxonEnv, level: str) -> None:
    """PYTHONIOENCODING=cp1252 on a pipe (the Windows default) never raises UnicodeEncodeError."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "--trace", level, "hi"],
                extra_env={"PYTHONIOENCODING": "cp1252"}, drop=("PYTHONUTF8",))
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    assert b"UnicodeEncodeError" not in r.stderr and TRACEBACK.encode() not in r.stderr, err
    assert b"Done" in r.stdout


def test_cp1252_output_is_consistently_cp1252(axon_env: AxonEnv) -> None:
    """With cp1252 requested, stdout is one consistent encoding: the requested cp1252 (no UTF-8 mojibake)."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "hi"], extra_env={"PYTHONIOENCODING": "cp1252"},
                drop=("PYTHONUTF8",))
    text = r.stdout.decode("cp1252")  # raises if bytes outside cp1252 were written
    assert "\u00e2\u20ac" not in text  # UTF-8 bytes of “ / ” / — read as cp1252


def test_cp1252_pipe_uses_ascii_glyph_fallback(axon_env: AxonEnv) -> None:
    """When stdout is cp1252, the trace uses the ASCII glyph fallback (output decodes as cp1252 without mojibake)."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "hi"], extra_env={"PYTHONIOENCODING": "cp1252"},
                drop=("PYTHONUTF8",))
    text = r.stdout.decode("cp1252", "replace")
    assert "| # Step 1" in text and "⎿" not in r.stdout.decode("utf-8", "replace")


def test_ascii_pythonioencoding_no_crash(axon_env: AxonEnv) -> None:
    """PYTHONIOENCODING=ascii (strict) does not crash a unicode-heavy turn."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "hi"], extra_env={"PYTHONIOENCODING": "ascii"},
                drop=("PYTHONUTF8",))
    assert r.returncode == 0 and TRACEBACK.encode() not in r.stderr, r.stderr[-2000:]


_LEGACY_WRAPPER = """
import io, sys
sys.path.insert(0, {src!r})
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding={enc!r}, errors="strict", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding={enc!r}, errors="strict", line_buffering=True)
import axon.platform as _p
_p._reconfigure_streams = lambda: None   # a legacy console that cannot be switched to UTF-8
from axon.cli import main
sys.exit(main(sys.argv[1:]))
"""


@pytest.mark.parametrize("level", ["compact", "normal", "detailed", "debug"])
def test_legacy_cp1252_console_uses_ascii_glyphs(axon_env: AxonEnv, tmp_path: Path, level: str) -> None:
    """On a strict cp1252 console that cannot be reconfigured, every trace level renders with ASCII glyphs."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_script(axon_env, _LEGACY_WRAPPER.format(src=str(SRC), enc="cp1252"),
                   ["-p", "--mode", "bypass", "--trace", level, "hi"], tmp_path, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    out, err = r.stdout.decode("cp1252"), r.stderr.decode("cp1252", "replace")
    assert r.returncode == 0, err
    assert "UnicodeEncodeError" not in err and TRACEBACK not in err, err
    assert "+ Done" in out and "✓" not in out and "⎿" not in out
    if level != "compact":
        assert "| # Step 1" in out and "+- Response" in out


def test_legacy_cp1252_console_replaces_unencodable_answer_chars(axon_env: AxonEnv, tmp_path: Path) -> None:
    """Model text with CJK/emoji on a cp1252 console is replaced ('?'), while cp1252 punctuation survives."""
    axon_env.llm.enqueue([{"text": UNICODE_ANSWER}])
    r = run_script(axon_env, _LEGACY_WRAPPER.format(src=str(SRC), enc="cp1252"), ["-p", "--trace", "compact", "hi"],
                   tmp_path, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    out = r.stdout.decode("cp1252")
    assert r.returncode == 0
    assert "“quotes”" in out and "—" in out and "中" not in out


def test_legacy_cp1252_console_tables_use_ascii_box(axon_env: AxonEnv, tmp_path: Path) -> None:
    """Markdown tables fall back to +---+ ASCII boxes on a cp1252 console."""
    axon_env.llm.enqueue([{"text": "| a | b |\n|---|---|\n| 1 | 2 |\n"}])
    r = run_script(axon_env, _LEGACY_WRAPPER.format(src=str(SRC), enc="cp1252"), ["-p", "--trace", "compact", "hi"],
                   tmp_path, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    out = r.stdout.decode("cp1252")
    assert "+---+---+" in out and "| a | b |" in out


def test_legacy_ascii_console_whole_turn(axon_env: AxonEnv, tmp_path: Path) -> None:
    """Even a strict pure-ASCII console renders a tool turn without errors."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_script(axon_env, _LEGACY_WRAPPER.format(src=str(SRC), enc="ascii"), ["-p", "--mode", "bypass", "hi"],
                   tmp_path, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    assert r.returncode == 0, r.stderr.decode("ascii", "replace")[-2000:]
    r.stdout.decode("ascii")


@pytest.mark.parametrize("level", ["compact", "normal", "detailed", "debug"])
def test_axon_ascii_env_output_is_pure_ascii(axon_env: AxonEnv, level: str) -> None:
    """AXON_ASCII=1 makes all of Axon's own chrome ASCII (answer text kept ASCII here)."""
    axon_env.llm.enqueue([{"text": "ok"}, {"tool": "Bash", "args": {"command": "echo hi", "description": "x"}}],
                         [{"text": "fin\n\n- a\n\n| a | b |\n|---|---|\n| 1 | 2 |"}])
    r = axon_env.run(["-p", "--mode", "bypass", "--trace", level, "hi"], env={"AXON_ASCII": "1"})
    assert r.code == 0, str(r)
    bad = sorted({c for c in r.stdout if ord(c) > 127})
    assert not bad, bad


@posix_only
def test_axon_ascii_env_in_repl_banner(axon_env: AxonEnv) -> None:
    """With AXON_ASCII=1 the REPL banner uses the ASCII logo and prompt glyph."""
    with open_repl(axon_env, extra_env={"AXON_ASCII": "1"}) as s:
        scr = s.screen
        assert "/^\\" in scr and "|A|" in scr
        assert "▲█▲" not in scr


def test_python_utf8_mode_disabled_runs(axon_env: AxonEnv) -> None:
    """`python -X utf8=0 -m axon -p` works with a unicode-heavy turn."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "hi"], py_flags=("-X", "utf8=0"),
                drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    assert r.returncode == 0, r.stderr[-2000:]
    assert "中文".encode() in r.stdout


def test_c_locale_without_utf8_mode_runs(axon_env: AxonEnv) -> None:
    """LC_ALL=C with UTF-8 mode off (ASCII locale) still completes the turn."""
    axon_env.llm.enqueue(*_turn_scripts())
    r = run_raw(axon_env, ["-p", "--mode", "bypass", "hi"], py_flags=("-X", "utf8=0"),
                extra_env={"LC_ALL": "C", "LANG": "C"}, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    assert r.returncode == 0 and TRACEBACK.encode() not in r.stderr, r.stderr[-2000:]


def test_unicode_prompt_round_trip_without_utf8_mode(axon_env: AxonEnv) -> None:
    """A non-ASCII prompt from argv reaches the provider intact with `-X utf8=0`."""
    axon_env.llm.enqueue({"steps": [{"text": "ok"}],
                          "expect": [lambda q: "héllo 世界" in q.last_user_text() or "prompt mangled"]})
    r = run_raw(axon_env, ["-p", "héllo 世界"], py_flags=("-X", "utf8=0"), drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    assert r.returncode == 0 and not axon_env.llm.failures, axon_env.llm.failures


# ─── POSIX-only modules blocked ────────────────────────────────────────────

_BLOCK_SCRIPT = """
import importlib, pkgutil, sys
sys.path.insert(0, {src!r})
BLOCK = {{"termios", "tty", "fcntl", "pty", "resource", "grp", "pwd", "readline", "curses"}}
for name in list(BLOCK):
    sys.modules.pop(name, None)
class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ImportError("blocked for the Windows simulation: " + name)
        return None
sys.meta_path.insert(0, Blocker())
import axon
bad, n = [], 0
for mod in pkgutil.walk_packages(axon.__path__, "axon."):
    if mod.name.endswith("__main__"):
        continue
    n += 1
    try:
        importlib.import_module(mod.name)
    except Exception as exc:
        bad.append(f"{{mod.name}}: {{exc!r}}")
{extra}
print("IMPORTED", n)
print("BAD", bad)
"""


def test_every_module_imports_without_posix_modules(axon_env: AxonEnv, tmp_path: Path) -> None:
    """With termios/tty/fcntl/pty (and friends) blocked, every axon module still imports."""
    r = run_script(axon_env, _BLOCK_SCRIPT.format(src=str(SRC), extra=""), [], tmp_path)
    out = r.stdout.decode()
    assert r.returncode == 0, r.stderr.decode()[-2000:]
    assert "BAD []" in out, out
    assert int(out.split("IMPORTED")[1].split()[0]) > 50


def test_platform_degrades_without_termios(axon_env: AxonEnv, tmp_path: Path) -> None:
    """Without termios, axon.platform reports HAS_TERMIOS False and raw_terminal() is a safe no-op."""
    extra = ("import axon.platform as p\nassert p.HAS_TERMIOS is False\n"
             "with p.raw_terminal() as ok:\n    pass\nprint('RAW', ok)\n")
    r = run_script(axon_env, _BLOCK_SCRIPT.format(src=str(SRC), extra=extra), [], tmp_path)
    assert r.returncode == 0, r.stderr.decode()[-2000:]
    assert b"BAD []" in r.stdout and b"RAW" in r.stdout


def test_print_mode_runs_with_posix_modules_blocked(axon_env: AxonEnv, tmp_path: Path) -> None:
    """A full `-p` tool turn runs with termios/tty/fcntl/pty unavailable."""
    extra = ("from axon.cli import main\nrc = main(['-p', '--mode', 'bypass', 'hi'])\nprint('RC', rc)\n")
    axon_env.llm.enqueue(*tool_turn("echo blocked-ok", "BLOCKED-FINE"))
    r = run_script(axon_env, _BLOCK_SCRIPT.format(src=str(SRC), extra=extra), [], tmp_path)
    out = r.stdout.decode("utf-8", "replace")
    assert "RC 0" in out and "BLOCKED-FINE" in out, out[-1500:] + r.stderr.decode()[-1500:]


# ─── Windows shell selection (simulated in-process) ────────────────────────

class _WinSim:
    """Patch axon.platform so it believes it runs on Windows with a fake PATH / filesystem."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, which: dict[str, str], files: tuple[str, ...] = (),
                 env: dict[str, str] | None = None) -> None:
        import axon.platform as plat
        self.plat = plat
        files_l = {ntpath.normcase(f) for f in (*files, *which.values())}
        fake_path = types.SimpleNamespace(**{k: getattr(ntpath, k) for k in dir(ntpath) if not k.startswith("__")})
        fake_path.isfile = lambda p: ntpath.normcase(str(p)) in files_l
        fake_path.exists = fake_path.isfile
        environ = {"SystemRoot": r"C:\Windows", "COMSPEC": r"C:\Windows\System32\cmd.exe",
                   "ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\u\AppData\Local", **(env or {})}

        class _FakeOS:
            path = fake_path
            sep, altsep, name = "\\", "/", "nt"

            def __getattr__(self, item):
                return getattr(os, item)

        _FakeOS.environ = environ
        self.environ = environ
        monkeypatch.setattr(plat, "os", _FakeOS())
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        monkeypatch.setattr(plat.shutil, "which", lambda name, *a, **k: which.get(name.lower().removesuffix(".exe")))
        monkeypatch.setattr(plat, "_pwsh_version", lambda path: "7.4")
        plat._detect_shell.cache_clear()
        monkeypatch.setattr(plat, "_detect_shell", plat._detect_shell)

    def detect(self, override: str = ""):
        self.plat._detect_shell.cache_clear()
        return self.plat._detect_shell(override, True)


PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"
WINPS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
WSL_BASH = r"C:\Windows\System32\bash.exe"
GIT = r"C:\Program Files\Git\cmd\git.exe"
GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


@pytest.fixture(autouse=True)
def _reset_shell_cache():
    yield
    try:
        import axon.platform as plat
        plat._detect_shell.cache_clear()
    except Exception:
        pass


def test_windows_prefers_pwsh_over_powershell_and_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """With pwsh, powershell and cmd all present, PowerShell 7 (pwsh) is chosen."""
    info = _WinSim(monkeypatch, {"pwsh": PWSH, "powershell": WINPS, "cmd": r"C:\Windows\System32\cmd.exe"}).detect()
    assert (info.kind, info.path, info.name) == ("pwsh", PWSH, "PowerShell 7.4")
    assert info.is_powershell


def test_windows_falls_back_to_windows_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pwsh, Windows PowerShell 5.1 is chosen."""
    info = _WinSim(monkeypatch, {"powershell": WINPS}).detect()
    assert (info.kind, info.name) == ("powershell", "Windows PowerShell 5.1")


def test_windows_falls_back_to_cmd_via_comspec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without any PowerShell, cmd.exe from %COMSPEC% is used."""
    info = _WinSim(monkeypatch, {}, env={"COMSPEC": r"D:\Win\cmd.exe"}).detect()
    assert (info.kind, info.path, info.name) == ("cmd", r"D:\Win\cmd.exe", "cmd.exe")


def test_windows_never_picks_bash_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with Git Bash on PATH the default Windows shell is PowerShell, not bash."""
    info = _WinSim(monkeypatch, {"bash": GIT_BASH, "powershell": WINPS}).detect()
    assert info.kind == "powershell"


def test_axon_shell_bash_skips_system32_wsl_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXON_SHELL=bash resolves Git Bash next to git.exe, never C:\\Windows\\System32\\bash.exe (WSL)."""
    sim = _WinSim(monkeypatch, {"bash": WSL_BASH, "git": GIT, "pwsh": PWSH}, files=(GIT_BASH,))
    info = sim.detect("bash")
    assert (info.kind, info.path, info.name) == ("bash", GIT_BASH, "Git Bash")


def test_find_git_bash_rejects_windowsapps_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WindowsApps bash alias is treated like the WSL launcher and skipped."""
    apps = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe"
    sim = _WinSim(monkeypatch, {"bash": apps}, files=(r"C:\Program Files\Git\bin\bash.exe",))
    assert sim.plat.find_git_bash() == GIT_BASH


def test_axon_shell_bash_with_only_wsl_falls_back_to_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the only bash is the WSL launcher, AXON_SHELL=bash falls back to the PowerShell default."""
    info = _WinSim(monkeypatch, {"bash": WSL_BASH, "pwsh": PWSH}).detect("bash")
    assert info.kind == "pwsh"


def test_axon_shell_cmd_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXON_SHELL=cmd forces cmd.exe even when pwsh exists."""
    info = _WinSim(monkeypatch, {"pwsh": PWSH}).detect("cmd")
    assert info.kind == "cmd"


def test_axon_shell_missing_pwsh_override_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """AXON_SHELL=pwsh when pwsh is not installed falls back to Windows PowerShell."""
    info = _WinSim(monkeypatch, {"powershell": WINPS}).detect("pwsh")
    assert info.kind == "powershell"


def test_shell_info_reads_axon_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """shell_info() honours the AXON_SHELL environment variable."""
    sim = _WinSim(monkeypatch, {"pwsh": PWSH, "powershell": WINPS})
    sim.environ["AXON_SHELL"] = "powershell"
    sim.plat._detect_shell.cache_clear()
    assert sim.plat.shell_info().kind == "powershell"
    assert sim.plat.shell_name() == "Windows PowerShell 5.1"


def test_default_shell_argv_for_powershell_and_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """default_shell() gives `-NoLogo -NoProfile -NonInteractive -Command` for PowerShell and `/d /s /c` for cmd."""
    sim = _WinSim(monkeypatch, {"pwsh": PWSH})
    sim.plat._detect_shell.cache_clear()
    assert sim.plat.default_shell() == [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    sim.environ["AXON_SHELL"] = "cmd"
    sim.plat._detect_shell.cache_clear()
    assert sim.plat.default_shell()[1:] == ["/d", "/s", "/c"]


def _decode_encoded(argv: list[str]) -> str:
    i = argv.index("-EncodedCommand")
    return base64.b64decode(argv[i + 1]).decode("utf-16-le")


@pytest.mark.parametrize("script", ['echo "hi there"', "Get-ChildItem | Select-Object -First 3\nWrite-Output 'two'",
                                    "Write-Output 'héllo 世界 ✓'", "$x = \"a`\"b\"; echo $x"])
def test_powershell_encoded_command_round_trip(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """-EncodedCommand carries the exact script (UTF-16LE base64) after the UTF-8 output prelude."""
    sim = _WinSim(monkeypatch, {"pwsh": PWSH})
    argv = sim.plat.shell_command_argv(script, sim.detect())
    assert argv[:4] == [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert argv[4:6] == ["-ExecutionPolicy", "Bypass"]
    decoded = _decode_encoded(argv)
    assert decoded.startswith("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")
    assert "$ProgressPreference='SilentlyContinue'" in decoded
    assert decoded.endswith("\n" + script)


def test_powershell_encoded_is_ascii_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """The encoded command argument is plain ASCII base64 (safe for CreateProcess quoting)."""
    sim = _WinSim(monkeypatch, {"powershell": WINPS})
    argv = sim.plat.shell_command_argv("echo ✓ 'q' \"dq\" & | <>", sim.detect())
    enc = argv[argv.index("-EncodedCommand") + 1]
    assert enc.isascii() and all(c.isalnum() or c in "+/=" for c in enc)


def test_cmd_command_line_sets_utf8_codepage(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd.exe gets a single command line with `chcp 65001>nul` and multi-line scripts joined by ` & `."""
    sim = _WinSim(monkeypatch, {}, env={"COMSPEC": r"C:\Windows\System32\cmd.exe"})
    line = sim.plat.shell_command_argv("echo one\n\necho two", sim.detect("cmd"))
    assert isinstance(line, str)
    assert line == '"C:\\Windows\\System32\\cmd.exe" /d /s /c "chcp 65001>nul & echo one & echo two"'


def test_posix_shell_argv_is_dash_c() -> None:
    """On POSIX the command runs as `<shell> -c <command>` unchanged."""
    import axon.platform as plat
    info = plat.ShellInfo("bash", "/bin/bash", "bash")
    assert plat.shell_command_argv("echo 'a b'", info) == ["/bin/bash", "-c", "echo 'a b'"]


@pytest.mark.skipif(IS_WINDOWS, reason="real POSIX shell")
def test_axon_shell_sh_runs_bash_tool_end_to_end(axon_env: AxonEnv) -> None:
    """AXON_SHELL=sh is honoured by the real Bash tool in a `-p` run."""
    axon_env.llm.enqueue(*tool_turn("echo shell-is-$0", "done"))
    r = axon_env.run(["-p", "--mode", "bypass", "hi"], env={"AXON_SHELL": "sh"})
    assert r.code == 0, str(r)
    res = axon_env.llm.completion_requests()[1].tool_results()[0]
    assert "shell-is-" in res and "sh" in res.split("shell-is-")[1]


# ─── Windows key decoding tables ───────────────────────────────────────────

class _FakeMsvcrt:
    def __init__(self, chars: str) -> None:
        self.buf = list(chars)

    def kbhit(self) -> bool:
        return bool(self.buf)

    def getwch(self) -> str:
        return self.buf.pop(0)


def _msvcrt_keys(monkeypatch: pytest.MonkeyPatch, chars: str) -> list[str]:
    import axon.platform as plat
    fake = _FakeMsvcrt(chars)
    monkeypatch.setattr(plat, "msvcrt", fake)
    monkeypatch.setattr(plat, "ESC_TIMEOUT", 0.01)
    plat._pending_chars.clear()
    keys = []
    for _ in range(10):
        if not fake.buf and not plat._pending_chars:
            break
        keys.append(plat._msvcrt_read(0.01))
    plat._pending_chars.clear()
    return keys


@pytest.mark.parametrize("chars,expected", [
    ("\xe0H", ["up"]), ("\x00H", ["up"]), ("\xe0P", ["down"]), ("\xe0K", ["left"]), ("\xe0M", ["right"]),
    ("\xe0G", ["home"]), ("\xe0O", ["end"]), ("\xe0S", ["delete"]), ("\xe0R", ["insert"]),
    ("\xe0I", ["pageup"]), ("\xe0Q", ["pagedown"]), ("\xe0s", ["ctrl-left"]), ("\xe0t", ["ctrl-right"]),
    ("\x00\x0f", ["shift-tab"]), ("\x03", ["ctrl-c"]), ("\r", ["enter"]), ("\x08", ["backspace"]),
    ("\t", ["tab"]), ("\x1b", ["esc"]), ("a", ["a"]), ("\x04", ["ctrl-d"]), ("\x12", ["ctrl-r"]),
    ("\x0f", ["ctrl-o"]), ("\xe0", ["à"]), ("\xe0x", ["à", "x"]), ("é", ["é"]), ("\x1b\r", ["alt-enter"]),
])
def test_msvcrt_key_decoding(monkeypatch: pytest.MonkeyPatch, chars: str, expected: list[str]) -> None:
    """msvcrt.getwch() sequences (0x00/0xE0 scan prefixes, control chars) map to Axon key names."""
    assert _msvcrt_keys(monkeypatch, chars) == expected


@pytest.mark.parametrize("vk,ch,state,down,expected", [
    (0x0D, "\r", 0, True, ["enter"]), (0x0D, "\r", 0x10, True, ["shift-enter"]), (0x0D, "\r", 0x02, True, ["alt-enter"]),
    (0x09, "\t", 0x10, True, ["shift-tab"]), (0x08, "\b", 0x08, True, ["alt-backspace"]), (0x26, "\x00", 0, True, ["up"]),
    (0x25, "\x00", 0x08, True, ["ctrl-left"]), (0x41, "a", 0, False, []), (0x10, "\x00", 0x10, True, []),
    (0x43, "\x03", 0x08, True, ["ctrl-c"]), (0x51, "@", 0x0A, True, ["@"]), (0x46, "f", 0x02, True, ["alt-f"]),
    (0x12, "é", 0, False, ["é"]), (0x1B, "\x1b", 0, True, ["esc"]),
])
def test_win32_key_event_units(vk: int, ch: str, state: int, down: bool, expected: list[str]) -> None:
    """Win32 KEY_EVENT records (VK code, char, modifier state) translate to Axon key names; AltGr stays a char."""
    import axon.platform as plat
    assert plat.win_key_units(vk, ch, state, down) == expected


def test_win32_surrogate_pair_emoji() -> None:
    """An emoji typed on Windows arrives as two UTF-16 surrogate events and is merged into one character."""
    import axon.platform as plat
    units = "🎉".encode("utf-16-le")
    hi = units[:2].decode("utf-16-le", "surrogatepass")
    lo = units[2:].decode("utf-16-le", "surrogatepass")
    assert plat.win_key_units(0, hi, 0, True) == []
    assert plat.win_key_units(0, lo, 0, True) == ["🎉"]


def test_win32_paste_burst_coalesced() -> None:
    """A burst of chars containing Enter (a console paste) is merged into a single paste unit."""
    import axon.platform as plat
    assert plat.coalesce_paste(["a", "b", "enter", "c"]) == [plat.PASTE_PREFIX + "ab\nc"]
    assert plat.coalesce_paste(["a", "enter"]) == ["a", "enter"]
    assert plat.coalesce_paste(["up", "a", "enter", "b"]) == ["up", "a", "enter", "b"]


@pytest.mark.parametrize("seq,expected", [
    ("[Z", "shift-tab"), ("[1;5C", "ctrl-right"), ("[1;5D", "ctrl-left"), ("[3~", "delete"), ("[13;2u", "shift-enter"),
    ("[27;5;13~", "shift-enter"), ("OA", "up"), ("[H", "home"), ("\r", "alt-enter"), ("b", "ctrl-left"), ("x", "alt-x"),
])
def test_vt_escape_sequence_decoding(seq: str, expected: str) -> None:
    """VT sequences sent by Windows Terminal / xterm decode to the same key names on every OS."""
    import axon.platform as plat
    buf = list(seq)
    plat._pending_chars.clear()
    assert plat._decode_sequence("\x1b", lambda t: buf.pop(0) if buf else None) == expected


# ─── installers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["install.bat", "install.ps1", "install.sh"])
def test_installer_is_ascii_only(name: str) -> None:
    """Installer scripts contain only ASCII bytes (no BOM) so legacy code pages run them unchanged."""
    data = (REPO_ROOT / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    bad = [(i, b) for i, b in enumerate(data) if b > 127]
    assert not bad, bad[:10]


@pytest.mark.parametrize("name", ["install.bat", "install.ps1"])
def test_windows_installers_use_crlf(name: str) -> None:
    """Windows installers use CRLF line endings throughout (no bare LF)."""
    data = (REPO_ROOT / name).read_bytes()
    assert data.count(b"\r\n") > 5
    assert data.count(b"\n") == data.count(b"\r\n"), "bare LF found"


def test_shell_installer_uses_lf() -> None:
    """install.sh uses LF only (a CR would break `#!/bin/sh`)."""
    data = (REPO_ROOT / "install.sh").read_bytes()
    assert b"\r" not in data and data.startswith(b"#!")


def test_gitattributes_pins_installer_line_endings() -> None:
    """.gitattributes keeps CRLF for .ps1/.bat/.cmd and LF for .sh."""
    text = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for rule in ("*.ps1 text eol=crlf", "*.bat text eol=crlf", "*.sh text eol=lf"):
        assert rule in text


# ─── unicode / space workspace paths end to end ────────────────────────────

@pytest.fixture
def unicode_env(tmp_path: Path, fake_llm) -> AxonEnv:
    root = tmp_path / "dir with spaces ünïcødé 文件"
    root.mkdir()
    return create_axon_env(root, fake_llm)


def test_unicode_workspace_write_read_round_trip(unicode_env: AxonEnv) -> None:
    """Write then Read a unicode-named file inside a workspace whose path has spaces and non-ASCII chars."""
    unicode_env.llm.enqueue(
        [{"tool": "Write", "args": {"path": "sub dir/naïve 名前.txt", "content": "contenu ✓ 内容\n"}}],
        [{"tool": "Read", "args": {"path": "sub dir/naïve 名前.txt"}}],
        [{"text": "round trip done"}])
    r = unicode_env.run(["-p", "--mode", "bypass", "go"])
    assert r.code == 0, str(r)
    target = unicode_env.workspace / "sub dir" / "naïve 名前.txt"
    assert target.read_text(encoding="utf-8") == "contenu ✓ 内容\n"
    assert "contenu ✓ 内容" in unicode_env.llm.completion_requests()[2].all_text()


def test_unicode_workspace_bash_pwd(unicode_env: AxonEnv) -> None:
    """The Bash tool runs inside the unicode/space workspace and its path round-trips to the model."""
    unicode_env.llm.enqueue(*tool_turn("pwd", "pwd ok"))
    r = unicode_env.run(["-p", "--mode", "bypass", "go"])
    assert r.code == 0, str(r)
    res = unicode_env.llm.completion_requests()[1].tool_results()[0]
    assert "dir with spaces ünïcødé 文件" in res, res


def test_unicode_workspace_session_persisted_and_continued(unicode_env: AxonEnv) -> None:
    """Sessions for a unicode workspace are saved and `--continue` resumes them."""
    unicode_env.llm.enqueue([{"text": "code word ZEBRA"}])
    assert unicode_env.run(["-p", "first"]).code == 0
    unicode_env.llm.enqueue({"steps": [{"text": "yes"}],
                             "expect": [lambda q: "ZEBRA" in q.all_text() or "history missing"]})
    r = unicode_env.run(["-p", "--continue", "again"])
    assert r.code == 0 and not unicode_env.llm.failures, (str(r), unicode_env.llm.failures)


def test_workspace_flag_with_unicode_path(axon_env: AxonEnv, tmp_path: Path) -> None:
    """`--workspace "<path with spaces/unicode>"` targets that directory for file tools."""
    ws = tmp_path / "другой проект x"
    ws.mkdir()
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "out.txt", "content": "here"}}], [{"text": "ok"}])
    r = axon_env.run(["-p", "--mode", "bypass", "--workspace", str(ws), "go"])
    assert r.code == 0, str(r)
    assert (ws / "out.txt").read_text() == "here"


def test_unicode_workspace_at_mention_attaches_file(unicode_env: AxonEnv) -> None:
    """An `@file` mention of a unicode filename in print mode attaches its content."""
    (unicode_env.workspace / "données.txt").write_text("PAYLOAD-ÜNÏ-42", encoding="utf-8")
    unicode_env.llm.enqueue([{"text": "seen"}])
    r = unicode_env.run(["-p", "summarize @données.txt"])
    assert r.code == 0, str(r)
    assert "PAYLOAD-ÜNÏ-42" in unicode_env.llm.completion_requests()[0].all_text()


@posix_only
def test_unicode_workspace_repl_banner_and_turn(unicode_env: AxonEnv) -> None:
    """The REPL starts in a unicode/space workspace, shows the path and completes a turn."""
    unicode_env.llm.enqueue([{"text": "repl in unicode ws"}])
    with open_repl(unicode_env) as s:
        assert "ünïcødé" in s.screen
        out = s.turn("hello")
        assert "repl in unicode ws" in out


def test_unicode_workspace_with_cp1252_console(unicode_env: AxonEnv, tmp_path: Path) -> None:
    """A unicode workspace on a legacy cp1252 console still runs a tool turn (path chars replaced, no crash)."""
    unicode_env.llm.enqueue(*tool_turn("pwd", "ok"))
    r = run_script(unicode_env, _LEGACY_WRAPPER.format(src=str(SRC), enc="cp1252"), ["-p", "--mode", "bypass", "hi"],
                   tmp_path, drop=("PYTHONUTF8", "PYTHONIOENCODING"))
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")[-2000:]
