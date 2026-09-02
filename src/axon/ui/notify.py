"""
Cross-platform native desktop notifications for Axon.
Supports macOS (AppleScript display notification), Linux (notify-send), and Windows (PowerShell Toast).
"""
from __future__ import annotations
import shutil
import subprocess
import sys


def send_desktop_notification(title: str, message: str) -> bool:
    """Trigger a native system notification dialog/banner."""
    clean_title = title.replace('"', '\\"').replace("'", "")
    clean_msg = message.replace('"', '\\"').replace("'", "")

    # 1. macOS Notification
    if sys.platform == "darwin":
        script = f'display notification "{clean_msg}" with title "{clean_title}" sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=2)
            return True
        except Exception:
            return False

    # 2. Linux notify-send
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", clean_title, clean_msg], capture_output=True, timeout=2)
            return True
        except Exception:
            return False

    # 3. Windows PowerShell Toast
    if sys.platform == "win32":
        ps_cmd = (
            f'[reflection.assembly]::loadwithpartialname("System.Windows.Forms"); '
            f'$notify = new-object system.windows.forms.notifyicon; '
            f'$notify.icon = [system.drawing.systemicons]::information; '
            f'$notify.visible = $true; '
            f'$notify.showballoontip(10, "{clean_title}", "{clean_msg}", [system.windows.forms.tooltipicon]::info)'
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=3)
            return True
        except Exception:
            return False

    return False


def is_terminal_window_focused() -> bool:
    """
    Check if the user is currently focused/present in the terminal window where Axon is running.
    Returns True if the current terminal/IDE window is active and frontmost.
    Returns False if the user switched away to another application (e.g. Chrome, Safari, Notes, Slack).
    """
    if sys.platform != "darwin":
        return True  # Fallback to safe default on non-macOS platforms

    try:
        import ctypes
        import ctypes.util
        import os

        appkit_path = ctypes.util.find_library("AppKit")
        if not appkit_path:
            return True
        appkit = ctypes.cdll.LoadLibrary(appkit_path)
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))

        objc_msgSend = objc.objc_msgSend
        objc_msgSend.restype = ctypes.c_void_p
        objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        objc_getClass = objc.objc_getClass
        objc_getClass.restype = ctypes.c_void_p
        objc_getClass.argtypes = [ctypes.c_char_p]

        sel_registerName = objc.sel_registerName
        sel_registerName.restype = ctypes.c_void_p
        sel_registerName.argtypes = [ctypes.c_char_p]

        NSWorkspace = objc_getClass(b"NSWorkspace")
        ws = objc_msgSend(NSWorkspace, sel_registerName(b"sharedWorkspace"))
        front_app = objc_msgSend(ws, sel_registerName(b"frontmostApplication"))
        name_ns = objc_msgSend(front_app, sel_registerName(b"localizedName"))

        objc_msgSend_str = objc.objc_msgSend
        objc_msgSend_str.restype = ctypes.c_char_p
        objc_msgSend_str.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        raw_name = objc_msgSend_str(name_ns, sel_registerName(b"UTF8String"))
        if not raw_name:
            return True
        active_app = raw_name.decode("utf-8").lower()

        # Identify terminal / IDE programs
        term_prog = os.environ.get("TERM_PROGRAM", "").lower()
        known_terminals = {
            "terminal", "iterm", "iterm2", "code", "visual studio code",
            "cursor", "windsurf", "ghostty", "alacritty", "kitty",
            "wezterm", "antigravity", "warp", "hyper", "rio",
        }

        # If the active application is in the known terminal/editor list, user is present
        if term_prog and any(t in term_prog for t in (active_app, active_app.replace(" ", ""))):
            return True
        if any(t in active_app for t in known_terminals):
            return True

        return False
    except Exception:
        return True


def notify_if_unfocused(title: str = "Axon Assistant", message: str = "Task finished execution.") -> bool:
    """Send desktop notification only if the user is NOT present in the terminal window."""
    if not is_terminal_window_focused():
        return send_desktop_notification(title, message)
    return False

