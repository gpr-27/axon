"""
Workspace path resolution, workspace jail checks and sensitive-path guards.

Design:
- ``resolve_in_workspace`` resolves a user/model supplied path (relative paths are
  anchored at the workspace), follows symlinks, and *always* refuses credential stores
  (``~/.ssh``, ``~/.aws`` ...) and OS system directories.  With ``allow_outside=False``
  it additionally refuses anything that resolves outside the workspace (except the
  small read allowlist returned by :func:`outside_allowlist`).
- ``is_within`` / ``is_in_workspace`` use ``os.path.commonpath`` on normalised,
  case-folded (Windows) real paths, never ``str.startswith`` - so ``/work2`` is not
  "inside" ``/work`` and ``C:\\Work`` equals ``c:/work``.  Different drives or UNC
  shares are simply "outside".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from axon.storage import axon_home as _axon_home
from axon.errors import PermissionDenied

IS_WINDOWS = sys.platform == "win32"

# Directory / file names that hold credentials. Compared case-insensitively on Windows.
# "shadow"/"gshadow" are the POSIX password hashes; the Windows SAM/SYSTEM hives live under
# %SystemRoot% which is blocked as a system directory (so no false positives for folders
# that merely happen to be called "system").
_CREDENTIAL_PARTS = {".ssh", ".aws", ".gnupg", ".netrc", ".vault", ".git-credentials", ".pgpass", ".azure", ".kube", "shadow", "gshadow"}

_POSIX_SYSTEM_DIRS = [
    "/etc",
    "/private/etc",
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/boot",
    "/private/var/root",
    "/private/var/db",
]


def _windows_system_dirs() -> list[str]:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    dirs = [root]
    for var, default in (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        dirs.append(os.environ.get(var) or default)
    return dirs


def system_dirs() -> list[str]:
    """OS directories that tools must never read or write."""
    return _windows_system_dirs() if IS_WINDOWS else list(_POSIX_SYSTEM_DIRS)


def _norm(p: str | Path) -> str:
    """Normalise for comparison: absolute, collapsed, case-folded on Windows."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def _real(p: str | Path) -> Path:
    try:
        return Path(p).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(os.path.expanduser(str(p))))


def is_within(root: str | Path, path: str | Path) -> bool:
    """True when ``path`` is ``root`` or lies below it (symlinks resolved)."""
    r = _norm(_real(root))
    p = _norm(_real(path))
    try:
        return os.path.commonpath([r, p]) == r
    except ValueError:  # different drives / UNC share vs local path
        return False


def _is_credential_path(resolved: Path) -> str | None:
    parts = [part.lower() for part in resolved.parts] if IS_WINDOWS else list(resolved.parts)
    blocked = {b.lower() for b in _CREDENTIAL_PARTS} if IS_WINDOWS else _CREDENTIAL_PARTS
    for part in parts:
        if part in blocked:
            return part
    # ~/.axon/.env holds provider API keys
    if is_within(_axon_home(), resolved) and resolved.name.lower() == ".env":
        return ".axon/.env"
    return None


def _is_system_path(resolved: Path) -> bool:
    return any(is_within(d, resolved) for d in system_dirs())


def outside_allowlist() -> list[Path]:
    """Locations outside the workspace that may be *read* without a jail error."""
    return [_axon_home()]


# Extra working directories (``/add-dir``, ``--add-dir``, ``permissions.additionalDirectories``).
# They are treated exactly like the workspace by the jail: readable and writable.
_ADDITIONAL_DIRS: list[Path] = []


def add_additional_dir(path: str | Path) -> Path:
    """Register an extra working directory for this process (idempotent). Returns the resolved path."""
    p = _real(path)
    if not any(_norm(p) == _norm(d) for d in _ADDITIONAL_DIRS):
        _ADDITIONAL_DIRS.append(p)
    return p


def remove_additional_dir(path: str | Path) -> bool:
    p = _norm(_real(path))
    before = len(_ADDITIONAL_DIRS)
    _ADDITIONAL_DIRS[:] = [d for d in _ADDITIONAL_DIRS if _norm(d) != p]
    return len(_ADDITIONAL_DIRS) != before


def additional_dirs(extra: list[str | Path] | tuple[str | Path, ...] | None = None) -> list[Path]:
    """Registered extra dirs plus ``extra`` (e.g. ``settings.additional_dirs``), de-duplicated."""
    out: list[Path] = []
    seen: set[str] = set()
    for d in list(_ADDITIONAL_DIRS) + [_real(x) for x in (extra or []) if str(x).strip()]:
        key = _norm(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def in_additional_dir(path: str | Path, extra: list[str | Path] | tuple[str | Path, ...] | None = None) -> bool:
    return any(is_within(d, path) for d in additional_dirs(extra))


def credential_location(raw: str | Path, root: Path | None = None) -> str | None:
    """The credential component (".ssh", ".aws", ".axon/.env", ...) ``raw`` points into, else None."""
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    try:
        return _is_credential_path(_real(p))
    except (OSError, ValueError):
        return None


def is_protected_path(raw: str | Path, root: Path | None = None) -> str | None:
    """Return a reason string when ``raw`` points at a credential store or system dir."""
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    resolved = _real(p)
    part = _is_credential_path(resolved)
    if part:
        return f"protected credential location '{part}'"
    if _is_system_path(resolved):
        return "protected system directory"
    return None


def resolve_in_workspace(
    root: Path,
    raw: str | Path,
    allow_git_read: bool = False,
    allow_outside: bool = True,
) -> Path:
    """
    Resolve ``raw`` against the workspace ``root``.

    Raises PermissionDenied for credential stores, OS system directories, ``.git``
    internals (unless ``allow_git_read``) and - when ``allow_outside`` is False -
    for anything outside the workspace that is not on the read allowlist.
    The PermissionEngine asks the user before tools touch outside paths, so tools
    that run after an approval may keep the default ``allow_outside=True``.
    """
    root_resolved = _real(root)
    raw_str = str(raw).strip()
    p = Path(raw_str).expanduser()
    if not p.is_absolute():
        p = root_resolved / p
    resolved = _real(p)

    if _is_system_path(resolved):
        raise PermissionDenied(f"Access to system path '{raw}' is blocked.")

    part = _is_credential_path(resolved)
    if part:
        raise PermissionDenied(f"Path '{raw}' accesses protected component '{part}'.")

    if not allow_git_read:
        rel_parts = resolved.parts
        if any((x.lower() if IS_WINDOWS else x) == ".git" for x in rel_parts):
            raise PermissionDenied(f"Path '{raw}' accesses protected component '.git'.")

    if not allow_outside and not is_within(root_resolved, resolved) and not in_additional_dir(resolved):
        if not any(is_within(a, resolved) for a in outside_allowlist()):
            raise PermissionDenied(f"Path '{raw}' is outside the workspace {root_resolved}.")

    return resolved


def is_in_workspace(root: Path, raw: str | Path) -> bool:
    """Check that a path resolves inside the workspace root and is not protected."""
    try:
        resolve_in_workspace(root, raw, allow_outside=False)
    except PermissionDenied:
        return False
    except Exception:
        return False
    raw_p = Path(str(raw).strip()).expanduser()
    target = raw_p if raw_p.is_absolute() else Path(root) / raw_p
    return is_within(root, target) or in_additional_dir(target)
