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
