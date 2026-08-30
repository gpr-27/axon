"""
Workspace Path Resolution and Sensitive Path Guards.
"""
from __future__ import annotations
from pathlib import Path
from axon.errors import PermissionDenied

_BLOCKED_PARTS = {".ssh", ".aws", ".gnupg", ".netrc", ".vault", "shadow"}
_SYSTEM_PREFIXES = (
    "/etc",
    "/private/etc",
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/private/var/root",
    "/private/var/db",
)

def resolve_in_workspace(root: Path, raw: str | Path, allow_git_read: bool = False) -> Path:
    """
    Resolves path inside the workspace or user development environment.
    Guards against sensitive credential access (.ssh, .aws) and OS system directories (/etc, /System).
    """
    root_resolved = root.resolve()
    raw_str = str(raw).strip()
    p = Path(raw_str).expanduser()
    if not p.is_absolute():
        p = root_resolved / p
    resolved = p.resolve()

    # Block sensitive system prefix paths
    res_str = str(resolved)
    for sys_prefix in _SYSTEM_PREFIXES:
        if res_str.startswith(sys_prefix) or raw_str.startswith(sys_prefix):
            raise PermissionDenied(f"Access to system path '{raw}' is blocked.")

    # Block sensitive credential directories and files
    for part in resolved.parts:
        if part in _BLOCKED_PARTS:
            raise PermissionDenied(f"Path '{raw}' accesses protected component '{part}'.")
        if part == ".git" and not allow_git_read:
            raise PermissionDenied(f"Path '{raw}' accesses protected component '.git'.")

    return resolved

def is_in_workspace(root: Path, raw: str | Path) -> bool:
    """Check if a path resolves cleanly inside the workspace root without escaping."""
    try:
        resolved = resolve_in_workspace(root, raw)
        root_resolved = root.resolve()
        return resolved == root_resolved or root_resolved in resolved.parents
    except Exception:
        return False

