"""
``CLAUDE_ENV_FILE`` support: variables a hook persists for the Bash commands that run later.

SessionStart, Setup, CwdChanged and FileChanged hooks get ``CLAUDE_ENV_FILE`` (``AXON_ENV_FILE``
too), the path of a file they append ``export NAME=value`` lines to. The Bash tool reads the file
before each command (:func:`read_env`) and adds the variables to the command's environment. Only
plain assignments are honored (``export``/``declare -x``/bare ``NAME=value``; ``$VAR`` references
expand against the current environment); any other line is ignored, so a hook cannot run code
through the file.
"""
from __future__ import annotations

import os
import re
import shlex
import tempfile
from pathlib import Path

_ASSIGN = re.compile(r"^(?:export\s+|declare\s+-x\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_current: Path | None = None


def file_for(session_id: str) -> Path:
    """Create (once) and return the env file of ``session_id``; later calls in the process reuse it."""
    global _current
    if _current is not None:
        return _current
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "") or "session"
    # mkdtemp: a private (0700) directory with an unguessable name, so another local user cannot
    # pre-create or write the file whose variables every later Bash command inherits.
    base = Path(tempfile.mkdtemp(prefix=f"axon-hook-env-{os.getpid()}-"))
    _current = base / f"{safe}.env"
    _current.touch(exist_ok=True)
    return _current


def current_path() -> Path | None:
    return _current


def clear() -> None:
    """Forget every persisted variable (CwdChanged does this before its hooks run)."""
    if _current is not None:
        try:
            _current.write_text("", encoding="utf-8")
        except OSError:
            pass


def reset() -> None:
    """Drop the module state (tests)."""
    global _current
    _current = None


def _expand(value: str, env: dict[str, str]) -> str:
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), ""), value)


def read_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The variables hooks persisted, in file order (later lines win)."""
    if _current is None:
        return {}
    try:
        lines = _current.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    env = dict(base if base is not None else os.environ)
    out: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if not m:
            continue
        name, rest = m.group(1), m.group(2).strip()
        try:
            parts = shlex.split(rest, posix=True)
        except ValueError:
            continue
        if len(parts) > 1:
            continue
        value = _expand(parts[0], env) if parts and not rest.startswith("'") else (parts[0] if parts else "")
        out[name] = value
        env[name] = value
    return out
