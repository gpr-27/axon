#!/usr/bin/env bash
# ==============================================================================
# Axon Installer & Initializer Script (macOS / Linux / Git Bash / WSL)
# ==============================================================================

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Check for python3 or python
if command -v python3 &> /dev/null; then
    PY_BIN="python3"
elif command -v python &> /dev/null; then
    PY_BIN="python"
else
    echo "❌ Error: Python 3 is not installed or not in PATH."
    exit 1
fi

"$PY_BIN" "$SCRIPT_DIR/setup_env.py"

