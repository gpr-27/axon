"""
Native desktop notifications (macOS, Linux, Windows) and terminal-focus detection, so Axon
only notifies when you are looking at another window. Notification text is always passed as
data (argv / environment), never interpolated into a script.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from axon import platform as plat

_TERMINAL_APPS = {
    "terminal", "iterm", "iterm2", "code", "visual studio code", "cursor", "windsurf", "ghostty", "alacritty",
    "kitty", "wezterm", "wezterm-gui", "warp", "hyper", "rio", "tabby", "windowsterminal", "conhost", "cmd",
    "powershell", "pwsh", "mintty", "konsole", "gnome-terminal", "gnome-terminal-server", "xterm", "tilix",
    "terminator", "foot", "zed", "pycharm64", "idea64", "antigravity",
}


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def send_desktop_notification(title: str, message: str) -> bool:
    """Show a native notification. Returns True when the platform command was launched."""
    title, message = _clean(title, 120), _clean(message, 400)
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = ["-e", "on run argv", "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                      "-e", "end run"]
            subprocess.run(["osascript", *script, title, message], capture_output=True, timeout=3, stdin=subprocess.DEVNULL)
            return True
        if plat.IS_WINDOWS:
            ps = shutil.which("powershell") or shutil.which("pwsh")
            if not ps:
                return False
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;"
                "$n = New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
                "$n.ShowBalloonTip(6000, $env:AXON_NOTIFY_TITLE, $env:AXON_NOTIFY_MSG, 'Info');"
                "Start-Sleep -Seconds 7; $n.Dispose()"
            )
            env = dict(os.environ, AXON_NOTIFY_TITLE=title or "Axon", AXON_NOTIFY_MSG=message or " ")
            subprocess.Popen([ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
                              plat.powershell_encoded(script)], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "--app-name=Axon", title, message], capture_output=True, timeout=3,
                           stdin=subprocess.DEVNULL)
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _mac_frontmost_app() -> str | None:
    import ctypes
    import ctypes.util
    appkit = ctypes.util.find_library("AppKit")
    objc_path = ctypes.util.find_library("objc")
    if not appkit or not objc_path:
        return None
    ctypes.cdll.LoadLibrary(appkit)
    objc = ctypes.cdll.LoadLibrary(objc_path)
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = objc.objc_msgSend
    send.restype = ctypes.c_void_p
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ws = send(objc.objc_getClass(b"NSWorkspace"), objc.sel_registerName(b"sharedWorkspace"))
    app = send(ws, objc.sel_registerName(b"frontmostApplication"))
    name = send(app, objc.sel_registerName(b"localizedName"))
    if not name:
        return None
    send_str = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(ctypes.cast(send, ctypes.c_void_p).value)
    raw = send_str(name, objc.sel_registerName(b"UTF8String"))
    return raw.decode("utf-8", "replace") if raw else None


def _windows_foreground_exe() -> str | None:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    if fg == k32.GetConsoleWindow():
        return "conhost"
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    handle = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
    finally:
        k32.CloseHandle(handle)
    return None


def _linux_active_window_class() -> str | None:
    if shutil.which("xdotool") and os.environ.get("DISPLAY"):
        res = subprocess.run(["xdotool", "getactivewindow", "getwindowclassname"], capture_output=True, timeout=1,
                             stdin=subprocess.DEVNULL)
        if res.returncode == 0:
            return res.stdout.decode("utf-8", "replace").strip()
    return None


def is_terminal_window_focused() -> bool:
    """True if a terminal/editor window is frontmost (or focus cannot be determined)."""
    try:
        if sys.platform == "darwin":
            app = _mac_frontmost_app()
        elif plat.IS_WINDOWS:
            app = _windows_foreground_exe()
        else:
            app = _linux_active_window_class()
    except Exception:
        return True
    if not app:
        return True
    name = app.lower()
    term_prog = os.environ.get("TERM_PROGRAM", "").lower()
    if term_prog and (term_prog in name or name.replace(" ", "") in term_prog):
        return True
    return any(t == name or t in name for t in _TERMINAL_APPS)


def notify_if_unfocused(title: str = "Axon", message: str = "Task finished.") -> bool:
    """Send a desktop notification only when the user is in another application."""
    if not is_terminal_window_focused():
        return send_desktop_notification(title, message)
    return False


# ─── Notification policy ───────────────────────────────────────────────────
# Only things worth leaving another app for reach the desktop:
#   permission_prompt  Axon is waiting for you to approve a tool (only asked in default / acceptEdits / plan mode)
#   question           Axon asked you a question (AskUserQuestion) and is waiting
#   turn_failed        a turn stopped on an error
#   turn_complete      a turn finished - only when it ran at least `notify_after_seconds` ("important" mode)
#   background_task    a turn you sent to the background finished
# `notifications = "all"` notifies after every turn; "off" never. Tests, scripts, print mode and
# subagents never notify, and the same kind is not repeated within a few seconds.
NOTIFY_MODES = ("important", "all", "off")
ALWAYS_IMPORTANT = frozenset({"permission_prompt", "question", "turn_failed", "background_task"})
_DEDUPE_S = 5.0
_policy: dict[str, Any] = {"mode": "important", "after": 30.0}
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def configure(settings: Any) -> None:
    """Remember the user's notification settings (called with the agent's settings)."""
    mode = str(getattr(settings, "notifications", "important") or "important").lower()
    _policy["mode"] = mode if mode in NOTIFY_MODES else "important"
    try:
        _policy["after"] = max(0.0, float(getattr(settings, "notify_after_seconds", 30.0)))
    except (TypeError, ValueError):
        _policy["after"] = 30.0


def _blocked_by_env() -> bool:
    return bool(os.environ.get("AXON_NO_NOTIFY") or os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"))


def notifications_allowed() -> bool:
    """False in tests, CI, scripts and print mode: only a person at an interactive terminal is notified."""
    if _blocked_by_env():
        return False
    if _policy["mode"] == "off":
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def should_notify(kind: str, elapsed: float | None = None) -> bool:
    """The policy alone (no focus check, no side effects)."""
    if not notifications_allowed():
        return False
    if kind in ALWAYS_IMPORTANT:
        return True
    if kind == "turn_complete":
        return _policy["mode"] == "all" or (elapsed is not None and elapsed >= _policy["after"])
    return _policy["mode"] == "all"


def notify_event(kind: str, message: str, *, settings: Any = None, elapsed: float | None = None,
                 title: str = "Axon") -> bool:
    """Send a desktop notification for *kind* if the policy allows it and you are in another app."""
    if settings is not None:
        configure(settings)
    if not should_notify(kind, elapsed):
        return False
    now = time.monotonic()
    with _lock:
        if now - _last_sent.get(kind, -1e9) < _DEDUPE_S:
            return False
        _last_sent[kind] = now
    return notify_if_unfocused(title, message)
