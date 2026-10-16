#!/usr/bin/env python3
"""
Axon installer core (used by install.ps1, install.bat and install.sh, or run directly).

    python scripts/setup_env.py [--no-shim] [--no-path]

1. Creates ./.venv (unless already running inside a virtual environment).
2. Installs Axon into it in editable mode (pip, or uv when pip is unavailable).
3. Creates an `axon` launcher: ~/.axon/bin/axon.cmd on Windows (added to the user PATH),
   ~/.local/bin/axon on macOS/Linux.
4. Creates the ~/.axon state directories.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import venv
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def _fix_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def say(msg: str) -> None:
    print(msg, flush=True)


def check_python_version() -> bool:
    if sys.version_info < (3, 10):
        say(f"[ERROR] Axon needs Python 3.10 or newer; this is {sys.version.split()[0]}.")
        say("        Install it from https://www.python.org/downloads/ and re-run the installer.")
        return False
    say(f"[OK] Python {sys.version.split()[0]} ({sys.executable})")
    return True


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def setup_virtualenv(root: Path) -> Path:
    if sys.prefix != sys.base_prefix:
        say(f"[OK] Using the active virtual environment: {sys.prefix}")
        return Path(sys.executable)
    venv_dir = root / ".venv"
    py = venv_python(venv_dir)
    if py.exists():
        say(f"[OK] Existing virtual environment: {venv_dir}")
        return py
    say(f"[..] Creating virtual environment in {venv_dir}")
    try:
        venv.create(venv_dir, with_pip=True, upgrade_deps=False)
    except Exception as e:  # e.g. Debian without python3-venv
        say(f"[WARN] venv with pip failed ({e}); retrying without pip")
        venv.create(venv_dir, with_pip=False)
    if not py.exists():
        say(f"[ERROR] Could not find {py} after creating the virtual environment.")
        if not IS_WINDOWS:
            say("        On Debian/Ubuntu install it with: sudo apt install python3-venv")
        sys.exit(1)
    return py


def _run(cmd: list[str]) -> bool:
    say("     $ " + " ".join(cmd))
    return subprocess.run(cmd).returncode == 0


def install_package(py: Path, root: Path) -> bool:
    has_pip = subprocess.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode == 0
    if not has_pip:
        subprocess.run([str(py), "-m", "ensurepip", "--upgrade"], capture_output=True)
        has_pip = subprocess.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode == 0
    if has_pip:
        say("[..] Installing Axon (editable) and its dependencies")
        _run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"])
        if _run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-e", str(root)]):
            return True
        say("[ERROR] pip install failed (see the messages above).")
        return False
    if shutil.which("uv"):
        say("[..] pip is unavailable; installing with uv")
        return _run(["uv", "pip", "install", "--python", str(py), "-e", str(root)])
    say("[ERROR] Neither pip nor uv is available for the virtual environment.")
    return False


def _windows_add_to_user_path(directory: Path) -> bool:
    import ctypes
    import winreg  # type: ignore[import-not-found]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in str(current).split(";") if p]
        if any(os.path.normcase(os.path.expandvars(p)).rstrip("\\") == os.path.normcase(str(directory)) for p in parts):
            return False
        parts.append(str(directory))
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ,
                          ";".join(parts))
    # Tell Explorer and new terminals that the environment changed.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    return True


def create_shim(py: Path, add_path: bool) -> None:
    if IS_WINDOWS:
        bin_dir = Path.home() / ".axon" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim = bin_dir / "axon.cmd"
        shim.write_text(f'@echo off\r\nREM axon launcher, written by the Axon installer\r\n"{py}" -m axon %*\r\n',
                        encoding="ascii", errors="replace", newline="")
        say(f"[OK] Launcher: {shim}")
        if add_path:
            try:
                if _windows_add_to_user_path(bin_dir):
                    say(f"[OK] Added {bin_dir} to your user PATH. Open a NEW terminal window to use `axon`.")
                else:
                    say(f"[OK] {bin_dir} is already on your user PATH.")
            except Exception as e:
                say(f"[WARN] Could not update PATH automatically ({e}). Add {bin_dir} to PATH manually.")
        return
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "axon"
    shim.write_text(f'#!/bin/sh\n# axon launcher, written by the Axon installer\nexec "{py}" -m axon "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    say(f"[OK] Launcher: {shim}")
    on_path = any(Path(p).expanduser() == bin_dir for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if not on_path:
        rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
        say(f"[WARN] {bin_dir} is not on your PATH. Add this line to {rc}:")
        say('         export PATH="$HOME/.local/bin:$PATH"')


def init_state_dirs() -> None:
    base = Path.home() / ".axon"
    for sub in ("sessions", "memory", "skills", "commands", "agents", "images"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def verify(py: Path) -> bool:
    res = subprocess.run([str(py), "-c", "import axon, sys; print(getattr(axon, '__version__', 'ok'))"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode == 0:
        say(f"[OK] Axon imports correctly ({res.stdout.strip()})")
        return True
    say("[ERROR] Axon failed to import after installation:")
    say(res.stderr.strip()[-2000:])
    return False


def main(argv: list[str] | None = None) -> int:
    _fix_streams()
    ap = argparse.ArgumentParser(description="Install Axon into a local virtual environment.")
    ap.add_argument("--no-shim", action="store_true", help="do not create the `axon` launcher")
    ap.add_argument("--no-path", action="store_true", help="do not modify PATH (Windows)")
    opts = ap.parse_args(argv)
    say("")
    say(f"  Axon setup  -  {platform.system()} {platform.release()}  -  Python {sys.version.split()[0]}")
    say("")
    if not check_python_version():
        return 1
    root = Path(__file__).resolve().parent.parent  # repository root
    py = setup_virtualenv(root)
    if not install_package(py, root) or not verify(py):
        return 1
    init_state_dirs()
    if not opts.no_shim:
        create_shim(py, add_path=not opts.no_path)
    say("")
    say("  Done. Start Axon with:   axon")
    if IS_WINDOWS:
        say(f"  (or without PATH:       \"{py}\" -m axon)")
    else:
        say(f"  (or without PATH:       {py} -m axon)")
    say("  First run:  axon setup     (adds an API key and picks a model)")
    say("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
