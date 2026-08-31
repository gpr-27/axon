#!/usr/bin/env python3
"""
Axon Multi-Platform Environment & Dependency Initializer.
Sets up virtual environment, installs dependencies, and initializes configuration.
Works identically on Windows (CMD & PowerShell), macOS, and Linux.

Usage:
    python setup_env.py
"""
from __future__ import annotations
import os
import platform
import subprocess
import sys
import venv
from pathlib import Path


def print_banner() -> None:
    print("\n" + "=" * 65)
    print("  ▲█▲  Axon Universal Environment & Dependency Setup")
    print(f"  █⚡█  Platform: {platform.system()} ({platform.release()}) · Python: {sys.version.split()[0]}")
    print("=" * 65 + "\n")


def check_python_version() -> bool:
    if sys.version_info < (3, 10):
        print(f"❌ Error: Axon requires Python >= 3.10. Found {sys.version.split()[0]}.")
        print("   Please upgrade Python: https://www.python.org/downloads/")
        return False
    print(f"✓ Python {sys.version.split()[0]} verified.")
    return True


def setup_virtualenv(root_dir: Path) -> Path:
    """Create a virtual environment if not already present or active."""
    # Check if already inside a virtualenv
    if sys.prefix != sys.base_prefix:
        print(f"✓ Active virtual environment detected: {sys.prefix}")
        return Path(sys.executable)

    venv_dir = root_dir / ".venv"
    if not venv_dir.exists():
        print(f"⚙️  Creating new virtual environment in {venv_dir}...")
        venv.create(venv_dir, with_pip=True)
        print("✓ Virtual environment created.")
    else:
        print(f"✓ Existing virtual environment found at {venv_dir}.")

    # Get python executable path inside venv
    if sys.platform == "win32":
        venv_py = venv_dir / "Scripts" / "python.exe"
        if not venv_py.exists():
            venv_py = venv_dir / "python.exe"
    else:
        venv_py = venv_dir / "bin" / "python"

    if not venv_py.exists():
        print(f"⚠️ Could not locate Python inside venv at {venv_py}. Using {sys.executable}.")
        return Path(sys.executable)

    return venv_py


def install_requirements(python_exe: Path, root_dir: Path) -> bool:
    """Install requirements.txt and package in editable mode."""
    import shutil
    req_file = root_dir / "requirements.txt"

    # Check if pip is available in target python
    pip_avail = subprocess.run([str(python_exe), "-m", "pip", "--version"], capture_output=True).returncode == 0
    if not pip_avail:
        print("⚙️  Bootstrapping pip into environment via ensurepip...")
        subprocess.run([str(python_exe), "-m", "ensurepip", "--upgrade"], capture_output=True)
        pip_avail = subprocess.run([str(python_exe), "-m", "pip", "--version"], capture_output=True).returncode == 0

    if not pip_avail and shutil.which("uv"):
        print("📦 Detected uv: Installing dependencies into environment with uv...")
        try:
            if req_file.exists():
                subprocess.run(["uv", "pip", "install", "-r", str(req_file), "--python", str(python_exe)], check=True)
            subprocess.run(["uv", "pip", "install", "-e", str(root_dir), "--python", str(python_exe)], check=True)
            print("✓ Dependencies and package installed successfully with uv.")
            return True
        except Exception as e:
            print(f"⚠️ uv install returned: {e}")

    print("\n📦 Upgrading pip...")
    try:
        subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], check=False)
    except Exception:
        pass

    if req_file.exists():
        print(f"📦 Installing dependencies from {req_file.name}...")
        try:
            subprocess.run([str(python_exe), "-m", "pip", "install", "-r", str(req_file)], check=True)
            print("✓ Dependencies installed successfully.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Error installing requirements: {e}")
            return False

    print("📦 Installing Axon in editable mode...")
    try:
        subprocess.run([str(python_exe), "-m", "pip", "install", "-e", str(root_dir)], check=True)
        print("✓ Axon package installed in editable mode.")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Editable install warning: {e}. Direct module execution will be used.")

    return True



def initialize_axon_config() -> None:
    """Initialize ~/.axon storage directories."""
    axon_dir = Path.home() / ".axon"
    for sub in ("sessions", "memory", "skills", "research", "images"):
        (axon_dir / sub).mkdir(parents=True, exist_ok=True)

    # Ensure local .env exists if example is present
    local_env = Path.cwd() / ".env"
    example_env = Path.cwd() / ".env.example"
    if not local_env.exists() and example_env.exists():
        try:
            with open(example_env, "r", encoding="utf-8") as f_src:
                content = f_src.read()
            with open(local_env, "w", encoding="utf-8") as f_dst:
                f_dst.write(content)
            print("✓ Created .env template from .env.example")
        except Exception:
            pass



def main() -> int:
    print_banner()
    if not check_python_version():
        return 1

    root_dir = Path(__file__).resolve().parent
    venv_py = setup_virtualenv(root_dir)

    success = install_requirements(venv_py, root_dir)
    if not success:
        return 1

    initialize_axon_config()

    print("\n" + "=" * 65)
    print("🎉 Axon Environment Setup Complete!")
    print("=" * 65 + "\n")
    print("  To run Axon:")
    if sys.platform == "win32":
        print("    1. Command Prompt:    .venv\\Scripts\\activate && axon")
        print("    2. PowerShell:        .\\.venv\\Scripts\\Activate.ps1; axon")
        print("    3. Direct launcher:   python axon_run.py")
    else:
        print("    1. Terminal:          source .venv/bin/activate && axon")
        print("    2. Direct launcher:   python3 axon_run.py")

    print("\n  To test model connectivity:")
    if sys.platform == "win32":
        print("    python check_models.py")
    else:
        print("    python3 check_models.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
