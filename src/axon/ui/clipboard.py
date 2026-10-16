"""
System clipboard copy/paste: macOS (pbcopy/pbpaste), Linux (wl-clipboard, xclip, xsel) and
Windows (clip.exe / PowerShell).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Callable

from axon import platform as plat


def _clipboard_file() -> str:
    """AXON_CLIPBOARD_FILE redirects copy/paste to a file (tests, headless use) instead of the system clipboard."""
    return os.environ.get("AXON_CLIPBOARD_FILE", "")


def _run(argv: list[str], data: bytes | None = None, timeout: float = 3.0) -> subprocess.CompletedProcess[bytes] | None:
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("AXON_ALLOW_REAL_CLIPBOARD"):
        return None   # a test must never overwrite the user's real clipboard
    try:
        return subprocess.run(argv, input=data, capture_output=True, timeout=timeout,
                              stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on success."""
    if not text:
        return False
    if _clipboard_file():
        try:
            with open(_clipboard_file(), "w", encoding="utf-8") as fh:
                fh.write(text)
            return True
        except OSError:
            return False
    if plat.IS_WINDOWS:
        clip = shutil.which("clip") or shutil.which("clip.exe")
        if clip:
            # clip.exe decodes UTF-16LE when the data starts with a BOM.
            res = _run([clip], ("﻿" + text).encode("utf-16-le"))
            if res is not None and res.returncode == 0:
                return True
        ps = _windows_powershell()
        if ps:
            script = "$t=[Console]::In.ReadToEnd(); Set-Clipboard -Value $t"
            res = _run([ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", plat.powershell_encoded(script)],
                       text.encode("utf-8"))
            return res is not None and res.returncode == 0
        return False
    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["pbcopy"])
    candidates += [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for argv in candidates:
        if shutil.which(argv[0]):
            res = _run(argv, text.encode("utf-8"))
            if res is not None and res.returncode == 0:
                return True
    return False


def paste_from_clipboard() -> str:
    """Return clipboard text ('' when unavailable)."""
    if _clipboard_file():
        try:
            with open(_clipboard_file(), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""
    if plat.IS_WINDOWS:
        ps = _windows_powershell()
        if not ps:
            return ""
        res = _run([ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", plat.powershell_encoded("Get-Clipboard -Raw")])
        return plat.decode_output(res.stdout).rstrip("\r\n") if res is not None and res.returncode == 0 else ""
    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["pbpaste"])
    candidates += [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]
    for argv in candidates:
        if shutil.which(argv[0]):
            res = _run(argv, timeout=1.0)
            if res is not None and res.returncode == 0:
                return plat.decode_output(res.stdout)
    return ""


_IMAGE_MIME = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp")


def paste_image_to(dest: Any) -> bool:
    """Save an image from the clipboard to *dest* via wl-paste / xclip (Linux, BSD; WSLg).
    macOS and Windows are handled by axon.agent.images.save_clipboard_image. Returns True on success."""
    from pathlib import Path as _P
    if plat.IS_WINDOWS or sys.platform == "darwin":
        return False
    dest = _P(dest)
    probes: list[tuple[list[str], Callable[[str], list[str]]]] = []
    if shutil.which("wl-paste"):
        probes.append((["wl-paste", "--list-types"], lambda m: ["wl-paste", "--no-newline", "--type", m]))
    if shutil.which("xclip"):
        probes.append((["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
                       lambda m: ["xclip", "-selection", "clipboard", "-t", m, "-o"]))
    for list_argv, fetch in probes:
        res = _run(list_argv, timeout=1.0)
        if res is None or res.returncode != 0:
            continue
        types = plat.decode_output(res.stdout).split()
        mime = next((m for m in _IMAGE_MIME if m in types), None)
        if mime is None:
            continue
        data = _run(fetch(mime), timeout=2.0)
        if data is None or data.returncode != 0 or not data.stdout:
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if mime != "image/png":
                dest = dest.with_suffix("." + mime.split("/")[1].replace("jpeg", "jpg"))
            dest.write_bytes(data.stdout)
            _LAST_IMAGE_PATH[0] = str(dest)
            return True
        except OSError:
            return False
    return False


_LAST_IMAGE_PATH: list[str] = [""]
