"""
System clipboard copy and export integration for Axon.
Supports macOS (pbcopy), Linux (xclip, xsel, wl-copy), and Windows (clip.exe, powershell).
"""
from __future__ import annotations
import shutil
import subprocess
import sys


def copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard across macOS, Linux, and Windows."""
    if not text:
        return False

    # 1. macOS pbcopy
    if sys.platform == "darwin":
        if shutil.which("pbcopy"):
            try:
                proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, timeout=3)
                return proc.returncode == 0
            except Exception:
                pass

    # 2. Linux Wayland / X11
    if sys.platform.startswith("linux"):
        for tool in ("wl-copy", "xclip", "xsel"):
            if shutil.which(tool):
                try:
                    if tool == "wl-copy":
                        cmd = ["wl-copy"]
                    elif tool == "xclip":
                        cmd = ["xclip", "-selection", "clipboard"]
                    else:
                        cmd = ["xsel", "--clipboard", "--input"]
                    proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=3)
                    if proc.returncode == 0:
                        return True
                except Exception:
                    pass

    # 3. Windows clip / PowerShell
    if sys.platform == "win32":
        if shutil.which("clip.exe") or shutil.which("clip"):
            try:
                proc = subprocess.run(["clip"], input=text, text=True, capture_output=True, timeout=3)
                return proc.returncode == 0
            except Exception:
                pass
        try:
            cmd = ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"]
            proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=3)
            return proc.returncode == 0
        except Exception:
            pass

    return False
