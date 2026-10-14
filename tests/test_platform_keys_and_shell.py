"""
Tests for axon.platform: key decoding (POSIX escape sequences, Windows console records, msvcrt
prefixes), display width helpers, Windows console mode handling, shell selection (simulated
Windows) and process-tree killing.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from collections import deque

import pytest

from axon import platform as plat


def _source(text: str):
    chars = deque(text)

    def getc(timeout: float):
        return chars.popleft() if chars else None
    return getc


def decode(seq: str) -> str:
    getc = _source(seq[1:])
    return plat._decode_sequence(seq[0], getc)


@pytest.mark.parametrize("seq,name", [
    ("\x1b[A", "up"), ("\x1b[B", "down"), ("\x1b[C", "right"), ("\x1b[D", "left"),
    ("\x1bOA", "up"), ("\x1b[H", "home"), ("\x1b[F", "end"), ("\x1b[1~", "home"), ("\x1b[4~", "end"),
    ("\x1b[3~", "delete"), ("\x1b[5~", "pageup"), ("\x1b[6~", "pagedown"), ("\x1b[Z", "shift-tab"),
    ("\x1b[1;5D", "ctrl-left"), ("\x1b[1;3C", "ctrl-right"), ("\x1bb", "ctrl-left"), ("\x1bf", "ctrl-right"),
    ("\x1b[13;2u", "shift-enter"), ("\x1b[27;2;13~", "shift-enter"), ("\x1b\r", "alt-enter"),
    ("\x1b[9;2u", "shift-tab"), ("\x1b[99;5u", "ctrl-c"), ("\x1bp", "alt-p"), ("\x1b", "esc"),
    ("\r", "enter"), ("\n", "ctrl-j"), ("\x7f", "backspace"), ("\x08", "backspace"), ("\t", "tab"),
    ("\x03", "ctrl-c"), ("\x04", "ctrl-d"), ("\x12", "ctrl-r"), ("\x1f", "ctrl-_"), ("a", "a"), ("界", "界"),
])
def test_escape_sequences(seq: str, name: str) -> None:
    assert decode(seq) == name


def test_bracketed_paste_is_one_event() -> None:
    assert decode("\x1b[200~line1\r\nline2\x1b[201~") == plat.PASTE_PREFIX + "line1\nline2"


def test_unknown_sequence_is_ignored() -> None:
    assert decode("\x1b[?1;2c") == ""


# ── Windows console input records ──────────────────────────────────────────

@pytest.mark.parametrize("vk,ch,state,expected", [
    (0x0D, "\r", 0, ["enter"]),
    (0x0D, "\r", 0x10, ["shift-enter"]),
    (0x0D, "\r", 0x02, ["alt-enter"]),
    (0x09, "\t", 0x10, ["shift-tab"]),
    (0x26, "\x00", 0, ["up"]),
    (0x25, "\x00", 0x08, ["ctrl-left"]),
    (0x2E, "\x00", 0, ["delete"]),
    (0x1B, "\x1b", 0, ["esc"]),
    (0x43, "\x03", 0x08, ["ctrl-c"]),
    (0x41, "a", 0, ["a"]),
    (0x51, "@", 0x0A, ["@"]),        # AltGr (ctrl+alt) stays a character
    (0x50, "p", 0x02, ["alt-p"]),
    (0x10, "\x00", 0x10, []),        # bare Shift
])
def test_windows_key_records(vk: int, ch: str, state: int, expected: list[str]) -> None:
    assert plat.win_key_units(vk, ch, state, True) == expected


def test_windows_key_up_ignored_except_alt_numpad() -> None:
    assert plat.win_key_units(0x41, "a", 0, False) == []
    assert plat.win_key_units(0x12, "é", 0, False) == ["é"]


def test_windows_surrogate_pair_emoji() -> None:
    hi, lo = "\ud83d", "\ude00"
    assert plat.win_key_units(0, hi, 0, True) == []
    assert plat.win_key_units(0, lo, 0, True) == ["😀"]


def test_coalesce_paste_burst() -> None:
    units = list("ab") + ["enter"] + list("cd")
    assert plat.coalesce_paste(units) == [plat.PASTE_PREFIX + "ab\ncd"]
    assert plat.coalesce_paste(["a", "b", "c"]) == ["a", "b", "c"]          # typing, no newline
    assert plat.coalesce_paste(["up", "enter", "a"]) == ["up", "enter", "a"]  # real keys stay keys


class FakeMsvcrt:
    def __init__(self, chars: str) -> None:
        self.chars = deque(chars)

    def kbhit(self) -> bool:
        return bool(self.chars)

    def getwch(self) -> str:
        return self.chars.popleft()


@pytest.fixture
def windows_msvcrt(monkeypatch):
    def install(chars: str) -> None:
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        monkeypatch.setattr(plat, "_win_failed", True)   # force the msvcrt backend
        monkeypatch.setattr(plat, "HAS_MSVCRT", True)
        monkeypatch.setattr(plat, "msvcrt", FakeMsvcrt(chars))
        monkeypatch.setattr(plat, "_check_resize", lambda: False)
        plat._events.clear()
        plat._pending_chars.clear()
    yield install
    plat._events.clear()
    plat._pending_chars.clear()


@pytest.mark.parametrize("chars,expected", [
    ("\xe0H", "up"), ("\xe0P", "down"), ("\xe0K", "left"), ("\xe0M", "right"), ("\xe0G", "home"),
    ("\xe0O", "end"), ("\xe0S", "delete"), ("\x00\x0f", "shift-tab"), ("\xe0s", "ctrl-left"),
    ("\r", "enter"), ("\x08", "backspace"), ("\x1b", "esc"), ("\x03", "ctrl-c"), ("x", "x"),
    ("\x1b[A", "up"),  # VT input mode sequences are understood too
])
def test_msvcrt_backend(windows_msvcrt, chars: str, expected: str) -> None:
    windows_msvcrt(chars)
    assert plat.read_key(timeout=0.5) == expected


def test_msvcrt_literal_a_grave(windows_msvcrt) -> None:
    windows_msvcrt("\xe0")  # 'à' typed alone, nothing follows
    assert plat.read_key(timeout=0.3) == "à"


def test_read_key_timeout_returns_none(windows_msvcrt) -> None:
    windows_msvcrt("")
    t0 = time.monotonic()
    assert plat.read_key(timeout=0.15) is None
    assert time.monotonic() - t0 < 1.0


# ── width helpers ──────────────────────────────────────────────────────────

def test_display_width() -> None:
    assert plat.str_width("abc") == 3
    assert plat.str_width("世界") == 4
    assert plat.str_width("😀") == 2
    assert plat.str_width("e\u0301") == 1
    assert plat.str_width("\x1b[31mred\x1b[0m") == 3


def test_truncate_ansi_keeps_codes_and_resets() -> None:
    out = plat.truncate_ansi("\x1b[31m" + "x" * 50, 10)
    assert plat.str_width(out) == 10
    assert out.startswith("\x1b[31m") and out.endswith("\x1b[0m")
    assert plat.truncate_ansi("世界世界", 5).count("世") + plat.truncate_ansi("世界世界", 5).count("界") == 2


# ── Windows console mode ───────────────────────────────────────────────────

class FakeKernel32:
    def __init__(self, mode: int) -> None:
        self.modes = {-11: mode, -12: mode}
        self.set_calls: list[tuple[int, int]] = []

    def GetStdHandle(self, n):
        return n

    def GetConsoleMode(self, handle, ref):
        if handle not in self.modes:
            return 0
        ref._obj.value = self.modes[handle]
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append((handle, mode))
        self.modes[handle] = mode
        return 1


def test_enable_vt_mode_sets_0x4_and_clears_0x8(monkeypatch) -> None:
    fake = FakeKernel32(0x0003 | 0x0008)
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "_kernel32", lambda: fake)
    assert plat.enable_vt_mode() is True
    for handle in (-11, -12):
        assert fake.modes[handle] & 0x0004
        assert not fake.modes[handle] & 0x0008


def test_enable_vt_mode_falls_back_to_colorama(monkeypatch) -> None:
    class NoConsole(FakeKernel32):
        def GetConsoleMode(self, handle, ref):
            return 0
    called = []
    fake_colorama = type(sys)("colorama")
    fake_colorama.just_fix_windows_console = lambda: called.append(True)
    monkeypatch.setitem(sys.modules, "colorama", fake_colorama)
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setattr(plat, "_kernel32", lambda: NoConsole(0))
    assert plat.enable_vt_mode() is False
    assert called == [True]
    monkeypatch.setattr(plat, "VT_ENABLED", True)


# ── shell selection (simulated Windows) ────────────────────────────────────

@pytest.fixture
def fake_windows_shells(monkeypatch, tmp_path):
    def setup(available: dict[str, str], override: str = "") -> None:
        monkeypatch.setattr(plat, "IS_WINDOWS", True)
        monkeypatch.setattr(plat.shutil, "which", lambda name: available.get(name))
        monkeypatch.setattr(plat, "_pwsh_version", lambda path: "7.4")
        monkeypatch.setenv("AXON_SHELL", override)
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        plat._detect_shell.cache_clear()
    yield setup
    plat._detect_shell.cache_clear()


def test_windows_prefers_pwsh(fake_windows_shells) -> None:
    fake_windows_shells({"pwsh": r"C:\pwsh\pwsh.exe", "powershell": r"C:\ps\powershell.exe"})
    assert plat.shell_name() == "PowerShell 7.4"
    assert plat.default_shell()[0].endswith("pwsh.exe")
    assert "-Command" in plat.default_shell()


def test_windows_falls_back_to_windows_powershell_then_cmd(fake_windows_shells) -> None:
    fake_windows_shells({"powershell": r"C:\ps\powershell.exe"})
    assert plat.shell_name() == "Windows PowerShell 5.1"
    fake_windows_shells({})
    assert plat.shell_info().kind == "cmd"
    assert plat.default_shell()[1:] == ["/d", "/s", "/c"]


def test_axon_shell_override(fake_windows_shells) -> None:
    fake_windows_shells({"pwsh": r"C:\pwsh\pwsh.exe", "powershell": r"C:\ps\powershell.exe"}, override="powershell")
    assert plat.shell_info().kind == "powershell"


def test_wsl_launcher_is_never_used_as_git_bash(monkeypatch) -> None:
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(plat.os.path, "normcase", lambda p: p.lower().replace("/", "\\"))
    monkeypatch.setattr(plat.os.path, "abspath", lambda p: p)
    assert plat._is_wsl_launcher(r"C:\Windows\System32\bash.exe") is True
    assert plat._is_wsl_launcher(r"C:\Program Files\Git\bin\bash.exe") is False


def test_powershell_command_is_encoded_with_utf8_prelude() -> None:
    info = plat.ShellInfo("pwsh", r"C:\pwsh\pwsh.exe", "PowerShell 7.4")
    argv = plat.shell_command_argv('Write-Output "hi \'there\'"', info)
    assert isinstance(argv, list) and "-EncodedCommand" in argv
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8" in script
    assert script.endswith('Write-Output "hi \'there\'"')


def test_cmd_command_line_sets_utf8_codepage() -> None:
    info = plat.ShellInfo("cmd", r"C:\Windows\System32\cmd.exe", "cmd.exe")
    line = plat.shell_command_argv("dir\necho done", info)
    assert isinstance(line, str)
    assert "chcp 65001>nul" in line and "dir & echo done" in line


def test_decode_output_falls_back_from_utf8() -> None:
    assert plat.decode_output("héllo".encode("utf-8")) == "héllo"
    assert "h" in plat.decode_output(b"h\xe9llo")  # invalid UTF-8 never raises


# ── processes and raw mode (real POSIX) ────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_kill_process_tree_kills_grandchildren(tmp_path) -> None:
    marker = tmp_path / "child.pid"
    proc = subprocess.Popen(["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"], **plat.new_process_group_kwargs())
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.02)
    child = int(marker.read_text().strip())
    plat.kill_process_tree(proc, grace=0.5)
    assert proc.poll() is not None
    time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX pty")
def test_raw_terminal_restores_mode_and_reads_keys() -> None:
    import pty
    import termios
    master, slave = pty.openpty()
    f = os.fdopen(slave, "r", closefd=False)
    old_stdin = sys.stdin
    sys.stdin = f
    try:
        pendin = getattr(termios, "PENDIN", 0)

        def mode() -> list:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~pendin  # the kernel may flag pending input after a mode switch
            return attrs[:6]
        before = mode()
        with plat.raw_terminal() as active:
            assert active
            raw = termios.tcgetattr(slave)
            assert not raw[3] & termios.ICANON and not raw[3] & termios.ISIG
            os.write(master, b"\x1b[Ax")
            assert plat.read_key(timeout=1.0, report_resize=False) == "up"
            assert plat.read_key(timeout=1.0, report_resize=False) == "x"
        assert mode() == before
        with pytest.raises(RuntimeError):
            with plat.raw_terminal():
                raise RuntimeError("boom")
        assert mode() == before
        assert plat._raw_stack == []
    finally:
        sys.stdin = old_stdin
        os.close(master)
        os.close(slave)
