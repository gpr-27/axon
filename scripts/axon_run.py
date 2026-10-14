#!/usr/bin/env python3
"""
Run Axon straight from a source checkout on any OS:

    python scripts/axon_run.py [axon arguments...]

Uses ./.venv when it exists (re-executing with its interpreter); otherwise runs from ./src with
the current interpreter and tells you how to install missing dependencies.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repository root
REQUIRED = [("anthropic", "anthropic"), ("httpx", "httpx"), ("pydantic", "pydantic"),
            ("pydantic_settings", "pydantic-settings"), ("tomli_w", "tomli-w"), ("bs4", "beautifulsoup4")]


def _fix_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _venv_python() -> Path | None:
    py = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return py if py.exists() else None


def main() -> int:
    _fix_streams()
    venv_py = _venv_python()
    in_that_venv = venv_py is not None and Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    if venv_py is not None and not in_that_venv and not os.environ.get("AXON_RUN_NO_VENV"):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if p)
        try:
            return subprocess.call([str(venv_py), "-m", "axon", *sys.argv[1:]], env=env)
        except KeyboardInterrupt:
            return 130

    missing = []
    for mod, pkg in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install them with:  \"{sys.executable}\" -m pip install -e \"{ROOT}\"")
        print("or run the installer for your OS (install.ps1, install.bat or install.sh).")
        return 1

    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from axon.cli import main as cli_main
    return int(cli_main() or 0)


if __name__ == "__main__":
    sys.exit(main())
