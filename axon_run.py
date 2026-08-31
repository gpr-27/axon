#!/usr/bin/env python3
"""
Axon Cross-Platform Universal Launcher & Auto-Bootstrapper.
Works seamlessly on Windows (PowerShell, Command Prompt), macOS, and Linux.

Usage:
    python axon_run.py
    python axon_run.py -p "Your prompt"
    python axon_run.py --help
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

# Required core dependencies to check
REQUIRED_PACKAGES = [
    ("anthropic", "anthropic"),
    ("httpx", "httpx"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("tomli_w", "tomli-w"),
    ("bs4", "beautifulsoup4"),
]

def ensure_dependencies() -> None:
    """Verify core dependencies and automatically install if missing."""
    missing: list[str] = []
    for mod_name, pkg_name in REQUIRED_PACKAGES:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print(f"\n📦 Installing missing Axon dependencies: {', '.join(missing)}...")
        req_file = Path(__file__).resolve().parent / "requirements.txt"
        try:
            if req_file.exists():
                cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
            else:
                cmd = [sys.executable, "-m", "pip", "install"] + missing
            subprocess.check_call(cmd)
            print("✓ Dependencies installed successfully.\n")
        except subprocess.CalledProcessError as e:
            print(f"⚠️ Warning: Auto-installation returned error {e}. Trying to proceed...\n")


def ensure_env_file() -> None:
    """Ensure a local .env exists if .env.example is present."""
    base_dir = Path(__file__).resolve().parent
    local_env = base_dir / ".env"
    example_env = base_dir / ".env.example"
    global_env = Path.home() / ".axon" / ".env"

    if not local_env.exists() and not global_env.exists() and example_env.exists():
        try:
            with open(example_env, "r", encoding="utf-8") as f_src:
                content = f_src.read()
            with open(local_env, "w", encoding="utf-8") as f_dst:
                f_dst.write(content)
        except Exception:
            pass


def main() -> int:
    ensure_dependencies()
    ensure_env_file()

    # Ensure src is on sys.path
    pkg_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "src"))
    if pkg_src not in sys.path:
        sys.path.insert(0, pkg_src)

    from axon.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())

