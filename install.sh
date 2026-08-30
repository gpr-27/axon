#!/usr/bin/env bash
# ==============================================================================
# Axon Installer & Initializer Script
# ==============================================================================
# Sets up Python virtual environment, installs dependencies, and creates the
# global 'axon' command alias so you can use Axon in any directory.
# ==============================================================================

set -e

echo ""
echo "  ▲█▲  Axon Installer"
echo "  █⚡█  Terminal-Native Agentic Coding Assistant"
echo ""

# 1. Check Python version (>= 3.11)
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed or not in PATH."
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
    echo "❌ Error: Axon requires Python >= 3.11. Found Python $PY_VER."
    exit 1
fi

echo "✓ Python $PY_VER detected."

# 2. Install editable package
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "📦 Installing Axon in editable mode from $SCRIPT_DIR..."
python3 -m pip install -e "$SCRIPT_DIR"

# 3. Initialize Global ~/.axon Configuration if not present
GLOBAL_AXON="$HOME/.axon"
mkdir -p "$GLOBAL_AXON/sessions" "$GLOBAL_AXON/memory" "$GLOBAL_AXON/skills"

if [ ! -f "$GLOBAL_AXON/config.toml" ]; then
    cat << 'EOF' > "$GLOBAL_AXON/config.toml"
# Axon Global Configuration
# This file configures default model, reasoning tier, and global preferences.

model = "deepseek-v4-flash"
effort = "quantum"
thinking = true
mode = "default"
parallel_tools = 6
compact_at = 0.85
EOF
    echo "✓ Initialized global config at ~/.axon/config.toml"
fi

echo ""
echo "=============================================================================="
echo "🎉 Axon setup complete!"
echo "=============================================================================="
echo ""
echo "  To start Axon in any repository or directory:"
echo "    $ axon"
echo ""
echo "  Or run directly with a prompt:"
echo "    $ axon -p 'Review this codebase and summarize architecture'"
echo ""
echo "  To configure your API keys, copy .env.example to .env (or create ~/.axon/.env):"
echo "    cp .env.example .env"
echo "    # Then edit .env with your AXON_API_KEY"
echo ""
echo "  Or export directly in your shell:"
echo "    export AXON_API_KEY=\"sk-...\""
echo "    export AXON_BASE_URL=\"https://agentrouter.org\""
echo ""
