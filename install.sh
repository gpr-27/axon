#!/usr/bin/env bash
# Axon installer for macOS, Linux, WSL and Git Bash.
#   ./install.sh [--no-shim]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY_BIN=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python py; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)' >/dev/null 2>&1; then
        PY_BIN="$cand"
        break
    fi
done

if [ -z "$PY_BIN" ]; then
    echo "[ERROR] Python 3.10 or newer was not found."
    echo "        macOS: brew install python   Debian/Ubuntu: sudo apt install python3 python3-venv"
    exit 1
fi

echo "[OK] Using Python: $(command -v "$PY_BIN")"
exec "$PY_BIN" "$SCRIPT_DIR/scripts/setup_env.py" "$@"
