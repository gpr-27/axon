#!/usr/bin/env bash
# Axon installer for macOS, Linux, WSL and Git Bash.
#
# One line, no clone needed:
#   curl -fsSL https://raw.githubusercontent.com/gpr-27/axon/main/install.sh | bash
# Options go after "bash -s --", for example:
#   curl -fsSL https://raw.githubusercontent.com/gpr-27/axon/main/install.sh | bash -s -- --uninstall
#
# From a clone (editable install into ./.venv, for development):
#   ./install.sh [--no-shim]
#
# Run "install.sh --help" for every option and environment variable.
#
# The whole script is wrapped in main() and only runs on the last line, so a download that is cut
# off half way does nothing at all.
set -euo pipefail

AXON_PACKAGE="axon-terminal"
AXON_DEFAULT_REPO="https://github.com/gpr-27/axon.git"
AXON_RAW_BASE="https://raw.githubusercontent.com/gpr-27/axon"

DRY_RUN=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '[OK] %s\n' "$*"; }
step() { printf '[..] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

# Print a command, then run it (or only print it with --dry-run).
run() {
    printf '     $'
    printf ' %s' "$@"
    printf '\n'
    if [ "$DRY_RUN" = 1 ]; then
        return 0
    fi
    "$@"
}

usage() {
    cat <<'EOF'
Axon installer for macOS, Linux, WSL and Git Bash.

Usage:
  curl -fsSL https://raw.githubusercontent.com/gpr-27/axon/main/install.sh | bash
  curl -fsSL https://raw.githubusercontent.com/gpr-27/axon/main/install.sh | bash -s -- [options]
  ./install.sh [options]          (from a clone: editable install into ./.venv)

Run outside a clone (the usual case), the installer picks the first available method:
  uv     uv tool install   (when uv is on PATH; uv can also fetch Python for you)
  pipx   pipx install      (when pipx is on PATH)
  venv   a private virtual environment in ~/.axon/venv plus an `axon` launcher in ~/.local/bin
Running it again upgrades Axon in place.

Options:
  --method M        auto (default), uv, pipx or venv           [AXON_INSTALL_METHOD]
  --ref REF         branch, tag or commit to install (main)    [AXON_INSTALL_REF]
  --source SPEC     install from SPEC instead of GitHub: a local folder, a git+ URL,
                    an archive URL or a PyPI requirement such as axon-terminal
                                                               [AXON_INSTALL_SOURCE]
  --dir DIR         virtual environment for the venv method (~/.axon/venv)
                                                               [AXON_INSTALL_DIR]
  --bin-dir DIR     where the `axon` launcher goes (~/.local/bin)
                                                               [AXON_BIN_DIR]
  --python PATH     Python 3.10+ interpreter to use            [AXON_PYTHON]
  --no-modify-path  never edit shell startup files             [AXON_NO_MODIFY_PATH=1]
  --standalone      install from GitHub even when run from inside a clone
  --no-shim         (clone mode) do not create the `axon` launcher
  --uninstall       remove Axon (your settings and sessions in ~/.axon are kept)
  --dry-run         show what would happen without changing anything
  -h, --help        show this help

After installing, run `axon setup` once to add an API key, then `axon` in any project folder.
EOF
}

# ---------------------------------------------------------------------------------------------
# helpers

is_windows_shell() {
    case "$(uname -s 2>/dev/null || echo unknown)" in
        MINGW* | MSYS* | CYGWIN*) return 0 ;;
    esac
    return 1
}

python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)' >/dev/null 2>&1
}

find_python() {
    if [ -n "${AXON_PYTHON:-}" ]; then
        if python_ok "$AXON_PYTHON"; then
            printf '%s\n' "$AXON_PYTHON"
            return 0
        fi
        die "AXON_PYTHON=$AXON_PYTHON is not a working Python 3.10 or newer."
    fi
    local cand
    for cand in python3.13 python3.12 python3.11 python3.10 python3.14 python3 python; do
        if command -v "$cand" >/dev/null 2>&1 && python_ok "$cand"; then
            command -v "$cand"
            return 0
        fi
    done
    return 1
}

python_missing_help() {
    say "        Install Python 3.10 or newer, or install uv (which fetches Python for you):"
    say "          curl -LsSf https://astral.sh/uv/install.sh | sh"
    say "        macOS:          brew install python"
    say "        Debian/Ubuntu:  sudo apt install python3 python3-venv"
    say "        Fedora:         sudo dnf install python3"
}

# Is $1 on PATH?
on_path() {
    case ":${PATH}:" in
        *":$1:"*) return 0 ;;
    esac
    return 1
}

# Show $1 with $HOME abbreviated.
pretty_path() {
    # shellcheck disable=SC2016  # a literal $HOME is what goes into the startup file
    case "$1" in
        "$HOME"/*) printf '$HOME/%s\n' "${1#"$HOME"/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Make sure $1 is on PATH for future shells.
ensure_on_path() {
    local dir="$1" rc="" line shell_name
    if on_path "$dir"; then
        return 0
    fi
    line="export PATH=\"$(pretty_path "$dir"):\$PATH\""
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        zsh) rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash)
            if [ "$(uname -s)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
        fish) rc="" ;;
        *) rc="$HOME/.profile" ;;
    esac
    if [ "${AXON_NO_MODIFY_PATH:-0}" = 1 ] || [ -z "$rc" ]; then
        warn "$dir is not on your PATH. Add it, for example:"
        if [ "$shell_name" = fish ]; then
            say "         fish_add_path $(pretty_path "$dir")"
        else
            say "         $line"
        fi
        return 0
    fi
    if [ -f "$rc" ] && grep -Fqx "$line" "$rc" 2>/dev/null; then
        ok "$rc already adds $(pretty_path "$dir") to PATH (open a new terminal to pick it up)"
        return 0
    fi
    step "Adding $(pretty_path "$dir") to PATH in $rc"
    if [ "$DRY_RUN" = 0 ]; then
        printf '\n# Added by the Axon installer\n%s\n' "$line" >>"$rc"
    fi
    PATH_HINT="$line"
}

# ---------------------------------------------------------------------------------------------
# install methods

# What pip / uv / pipx should install.
resolve_spec() {
    local src="${AXON_INSTALL_SOURCE:-}"
    if [ -n "$src" ]; then
        if [ -d "$src" ]; then
            (cd "$src" && pwd -P)
        else
            printf '%s\n' "$src"
        fi
        return 0
    fi
    local repo="${AXON_REPO_URL:-$AXON_DEFAULT_REPO}"
    if command -v git >/dev/null 2>&1 || [ "$repo" != "$AXON_DEFAULT_REPO" ]; then
        printf 'git+%s@%s\n' "$repo" "$REF"
    else
        # No git: GitHub serves any branch, tag or commit as a source archive.
        printf 'https://github.com/gpr-27/axon/archive/%s.tar.gz\n' "$REF"
    fi
}

install_with_uv() {
    local spec="$1"
    if [ -n "${AXON_BIN_DIR:-}" ]; then
        export UV_TOOL_BIN_DIR="$AXON_BIN_DIR"
    fi
    # --force replaces an older `axon` launcher; --reinstall-package and --refresh-package make a
    # re-run pick up new commits even when the version number did not change.
    run uv tool install --force --python ">=3.10" \
        --reinstall-package "$AXON_PACKAGE" --refresh-package "$AXON_PACKAGE" "$spec"
    if [ "$DRY_RUN" = 1 ]; then
        LAUNCHER_DIR="${AXON_BIN_DIR:-$HOME/.local/bin}"
    else
        LAUNCHER_DIR="$(uv tool dir --bin 2>/dev/null || printf '%s' "${AXON_BIN_DIR:-$HOME/.local/bin}")"
    fi
}

install_with_pipx() {
    local spec="$1" py
    if [ -n "${AXON_BIN_DIR:-}" ]; then
        export PIPX_BIN_DIR="$AXON_BIN_DIR"
    fi
    if py="$(find_python)"; then
        run pipx install --force --python "$py" "$spec"
    else
        run pipx install --force "$spec"
    fi
    if [ "$DRY_RUN" = 1 ]; then
        LAUNCHER_DIR="${AXON_BIN_DIR:-$HOME/.local/bin}"
    else
        LAUNCHER_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || printf '%s' "${AXON_BIN_DIR:-$HOME/.local/bin}")"
    fi
}

install_with_venv() {
    local spec="$1" py vpy shim
    if ! py="$(find_python)"; then
        say "[ERROR] Python 3.10 or newer was not found." >&2
        python_missing_help
        exit 1
    fi
    ok "Using Python: $py ($("$py" -c 'import platform; print(platform.python_version())'))"
    vpy="$VENV_DIR/bin/python"
    if [ -x "$vpy" ] && ! python_ok "$vpy"; then
        step "Recreating the broken or outdated virtual environment in $VENV_DIR"
        if [ -f "$VENV_DIR/.axon-installer" ]; then
            run rm -rf "$VENV_DIR"
        else
            die "$VENV_DIR exists but was not created by this installer. Remove it or pick another --dir."
        fi
    fi
    if [ ! -x "$vpy" ]; then
        if [ -e "$VENV_DIR" ] && [ -n "$(ls -A "$VENV_DIR" 2>/dev/null)" ] && [ ! -f "$VENV_DIR/.axon-installer" ]; then
            die "$VENV_DIR exists and is not an Axon virtual environment. Remove it or pick another --dir."
        fi
        step "Creating a virtual environment in $VENV_DIR"
        run mkdir -p "$(dirname "$VENV_DIR")"
        if ! run "$py" -m venv "$VENV_DIR"; then
            warn "python -m venv failed (on Debian/Ubuntu: sudo apt install python3-venv)."
            die "Could not create $VENV_DIR. Install python3-venv, or install uv or pipx and re-run."
        fi
    else
        ok "Existing virtual environment: $VENV_DIR"
    fi
    if [ "$DRY_RUN" = 0 ]; then
        printf 'Created by the Axon installer. Remove with: install.sh --uninstall\n' >"$VENV_DIR/.axon-installer"
    fi
    step "Installing Axon from $spec"
    run "$vpy" -m pip install --quiet --disable-pip-version-check --upgrade pip
    if [ "$DRY_RUN" = 0 ] && "$vpy" -m pip show --quiet "$AXON_PACKAGE" >/dev/null 2>&1; then
        # Upgrade: pip skips a git or folder source whose version number did not change, so
        # reinstall Axon itself, then bring its dependencies up to date.
        run "$vpy" -m pip install --disable-pip-version-check --force-reinstall --no-deps "$spec"
        run "$vpy" -m pip install --quiet --disable-pip-version-check --upgrade "$spec"
    else
        run "$vpy" -m pip install --disable-pip-version-check "$spec"
    fi

    shim="$BIN_DIR/axon"
    if [ -L "$shim" ] || { [ -e "$shim" ] && ! grep -Eq 'Axon installer|-m axon' "$shim" 2>/dev/null; }; then
        warn "Replacing the existing $shim"
    fi
    step "Writing the launcher $shim"
    if [ "$DRY_RUN" = 0 ]; then
        mkdir -p "$BIN_DIR"
        rm -f "$shim"
        printf '#!/bin/sh\n# axon launcher, written by the Axon installer (install.sh --uninstall removes it)\nexec "%s" "$@"\n' \
            "$VENV_DIR/bin/axon" >"$shim"
        chmod 755 "$shim"
    fi
    LAUNCHER_DIR="$BIN_DIR"
}

# ---------------------------------------------------------------------------------------------
# uninstall

uninstall() {
    local removed=0 shim
    say ""
    say "  Uninstalling Axon"
    say ""
    if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "^$AXON_PACKAGE "; then
        run uv tool uninstall "$AXON_PACKAGE" && removed=1
    fi
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^$AXON_PACKAGE "; then
        run pipx uninstall "$AXON_PACKAGE" && removed=1
    fi
    if [ -d "$VENV_DIR" ]; then
        if [ -f "$VENV_DIR/.axon-installer" ]; then
            run rm -rf "$VENV_DIR" && removed=1
        else
            warn "Leaving $VENV_DIR alone: it was not created by this installer."
        fi
    fi
    shim="$BIN_DIR/axon"
    if [ -f "$shim" ] && [ ! -L "$shim" ] && grep -Eq 'Axon installer|-m axon' "$shim" 2>/dev/null; then
        run rm -f "$shim" && removed=1
    fi
    if [ "$removed" = 1 ]; then
        ok "Axon was removed."
    else
        ok "No installer-managed Axon installation was found."
    fi
    if command -v axon >/dev/null 2>&1 && [ "$DRY_RUN" = 0 ]; then
        warn "An \`axon\` command is still on PATH: $(command -v axon)"
        say "       If you installed it with pip, run:  python3 -m pip uninstall $AXON_PACKAGE"
    fi
    say ""
    say "  Your settings, keys and sessions in ~/.axon were kept. To delete them too:  rm -rf ~/.axon"
    say "  A source checkout's ./.venv is not touched; delete the clone to remove it."
    say ""
}

# ---------------------------------------------------------------------------------------------

main() {
    local method="${AXON_INSTALL_METHOD:-auto}" do_uninstall=0 standalone=0 script_path script_dir="" in_clone
    local -a passthrough=()
    REF="${AXON_INSTALL_REF:-main}"
    VENV_DIR="${AXON_INSTALL_DIR:-$HOME/.axon/venv}"
    BIN_DIR="${AXON_BIN_DIR:-$HOME/.local/bin}"
    LAUNCHER_DIR=""
    PATH_HINT=""

    while [ $# -gt 0 ]; do
        case "$1" in
            -h | --help) usage; return 0 ;;
            --method) method="${2:?--method needs a value}"; shift ;;
            --method=*) method="${1#*=}" ;;
            --ref) REF="${2:?--ref needs a value}"; standalone=1; shift ;;
            --ref=*) REF="${1#*=}"; standalone=1 ;;
            --source) AXON_INSTALL_SOURCE="${2:?--source needs a value}"; shift ;;
            --source=*) AXON_INSTALL_SOURCE="${1#*=}" ;;
            --dir) VENV_DIR="${2:?--dir needs a value}"; shift ;;
            --dir=*) VENV_DIR="${1#*=}" ;;
            --bin-dir) BIN_DIR="${2:?--bin-dir needs a value}"; AXON_BIN_DIR="$BIN_DIR"; shift ;;
            --bin-dir=*) BIN_DIR="${1#*=}"; AXON_BIN_DIR="$BIN_DIR" ;;
            --python) AXON_PYTHON="${2:?--python needs a value}"; shift ;;
            --python=*) AXON_PYTHON="${1#*=}" ;;
            --no-modify-path) AXON_NO_MODIFY_PATH=1 ;;
            --standalone) standalone=1 ;;
            --uninstall) do_uninstall=1 ;;
            --dry-run) DRY_RUN=1 ;;
            --no-shim | --no-path) passthrough+=("$1") ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    case "$method" in
        auto | uv | pipx | venv) ;;
        *) die "--method must be auto, uv, pipx or venv (got: $method)" ;;
    esac
    if [ "$method" != auto ] || [ -n "${AXON_INSTALL_SOURCE:-}" ] || [ -n "${AXON_INSTALL_REF:-}" ]; then
        standalone=1
    fi
    case "$VENV_DIR" in /*) ;; *) VENV_DIR="$(pwd -P)/$VENV_DIR" ;; esac
    case "$BIN_DIR" in /*) ;; *) BIN_DIR="$(pwd -P)/$BIN_DIR" ;; esac


    # Run from a clone? (When piped from curl, BASH_SOURCE is empty and this is skipped.)
    script_path="${BASH_SOURCE[0]:-}"
    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
    fi
    in_clone=0
    if [ "$standalone" = 0 ] && [ -n "$script_dir" ] && [ -f "$script_dir/scripts/setup_env.py" ] \
        && [ -f "$script_dir/pyproject.toml" ]; then
        in_clone=1
    fi

    if is_windows_shell && { [ "$in_clone" = 0 ] || [ "$do_uninstall" = 1 ]; }; then
        # Git Bash / MSYS: a POSIX launcher would only work inside this shell. Use the Windows
        # installer so `axon` works in PowerShell, cmd and Git Bash alike.
        command -v powershell.exe >/dev/null 2>&1 || die "Use install.ps1 from PowerShell on Windows."
        step "Windows detected: running the PowerShell installer"
        export AXON_INSTALL_REF="$REF" AXON_INSTALL_METHOD="$method"
        if [ "$do_uninstall" = 1 ]; then export AXON_UNINSTALL=1; fi
        run powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
            "irm $AXON_RAW_BASE/$REF/install.ps1 | iex; exit \$AxonInstallExitCode"
        return 0
    fi

    if [ "$do_uninstall" = 1 ]; then
        uninstall
        return 0
    fi

    if [ "$in_clone" = 1 ]; then
        local py
        if ! py="$(find_python)"; then
            say "[ERROR] Python 3.10 or newer was not found." >&2
            python_missing_help
            exit 1
        fi
        ok "Using Python: $py"
        if [ "$DRY_RUN" = 1 ]; then
            say "     \$ $py $script_dir/scripts/setup_env.py ${passthrough[*]+"${passthrough[*]}"}"
            return 0
        fi
        exec "$py" "$script_dir/scripts/setup_env.py" ${passthrough[@]+"${passthrough[@]}"}
    fi


    say ""
    say "  Axon installer  -  $(uname -s) $(uname -m)"
    say ""

    if [ "$method" = auto ]; then
        if command -v uv >/dev/null 2>&1; then
            method=uv
        elif command -v pipx >/dev/null 2>&1; then
            method=pipx
        else
            method=venv
        fi
    fi
    if [ "$method" != venv ] && ! command -v "$method" >/dev/null 2>&1; then
        die "--method $method was requested but $method is not on PATH."
    fi

    local spec
    spec="$(resolve_spec)"
    case "$spec" in
        git+*) command -v git >/dev/null 2>&1 || die "Installing from $spec needs git. Install git, or pass --source with an archive URL." ;;
    esac
    ok "Method: $method"
    ok "Source: $spec"

    case "$method" in
        uv) install_with_uv "$spec" ;;
        pipx) install_with_pipx "$spec" ;;
        venv) install_with_venv "$spec" ;;
    esac

    if [ "$DRY_RUN" = 1 ]; then
        say ""
        say "  Dry run: nothing was changed."
        return 0
    fi

    local version
    if version="$("$LAUNCHER_DIR/axon" --version 2>/dev/null)"; then
        ok "Installed: $version  ($LAUNCHER_DIR/axon)"
    else
        die "The axon launcher in $LAUNCHER_DIR did not run. See the messages above."
    fi
    mkdir -p "$HOME/.axon"
    ensure_on_path "$LAUNCHER_DIR"

    say ""
    say "  Done. Next steps:"
    if [ -n "$PATH_HINT" ] || ! on_path "$LAUNCHER_DIR"; then
        say "    0. Open a new terminal (or run:  export PATH=\"$LAUNCHER_DIR:\$PATH\")"
    fi
    say "    1. axon setup      add an API key and pick a model"
    say "    2. axon            start Axon in any project folder"
    say ""
    say "  Upgrade:    run this installer again"
    say "  Uninstall:  curl -fsSL $AXON_RAW_BASE/main/install.sh | bash -s -- --uninstall"
    say ""
}

main "$@"
