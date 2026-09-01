"""
Workspace Path Resolution and Sensitive Path Guards.
"""
from __future__ import annotations
from pathlib import Path
from axon.errors import PermissionDenied

import os
import sys

_BLOCKED_PARTS = {".ssh", ".aws", ".gnupg", ".netrc", ".vault", "shadow", "SAM", "SYSTEM"}
_SYSTEM_PREFIXES = [
    "/etc",
    "/private/etc",
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/private/var/root",
    "/private/var/db",
]
if sys.platform == "win32":
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    _SYSTEM_PREFIXES.extend([
        sys_root,
        os.path.join(sys_root, "System32"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ])

def resolve_in_workspace(root: Path, raw: str | Path, allow_git_read: bool = False) -> Path:
    """
    Resolves path inside the workspace or user development environment.
    Guards against sensitive credential access (.ssh, .aws) and OS system directories (/etc, /System, C:\\Windows).
    """
    root_resolved = root.resolve()
    raw_str = str(raw).strip()
    p = Path(raw_str).expanduser()
    if not p.is_absolute():
        p = root_resolved / p
    resolved = p.resolve()

    # Block sensitive system prefix paths
    res_str = str(resolved).lower() if sys.platform == "win32" else str(resolved)
    raw_cmp = raw_str.lower() if sys.platform == "win32" else raw_str
    for sys_prefix in _SYSTEM_PREFIXES:
        pfx = sys_prefix.lower() if sys.platform == "win32" else sys_prefix
        if res_str.startswith(pfx) or raw_cmp.startswith(pfx):
            raise PermissionDenied(f"Access to system path '{raw}' is blocked.")

    # Block sensitive credential directories and files
    for part in resolved.parts:
        part_cmp = part.lower() if sys.platform == "win32" else part
        blocked_cmp = {b.lower() for b in _BLOCKED_PARTS} if sys.platform == "win32" else _BLOCKED_PARTS
        if part_cmp in blocked_cmp:
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

