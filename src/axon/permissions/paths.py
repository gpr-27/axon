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

# Credential folders that sit one level below a shared folder: ~/.config/gh holds the GitHub token.
_CREDENTIAL_PAIRS = {(".config", "gh"), (".config", "gcloud"), (".config", "op"), (".docker", "config.json")}

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
    for a, b in zip(parts, parts[1:]):
        if (a, b) in _CREDENTIAL_PAIRS:
            return f"{a}/{b}"
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


def _expand(raw: str | Path) -> Path:
    """``Path(raw).expanduser()`` that keeps an unknown ``~user`` literal, as a shell does, instead of raising."""
    p = Path(str(raw).strip())
    try:
        return p.expanduser()
    except RuntimeError:
        return p


def credential_location(raw: str | Path, root: Path | None = None) -> str | None:
    """The credential component (".ssh", ".aws", ".axon/.env", ...) ``raw`` points into, else None."""
    p = _expand(raw)
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    try:
        return _is_credential_path(_real(p))
    except (OSError, ValueError):
        return None


def credential_stores() -> list[Path]:
    """Credential files and directories that exist on this machine."""
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    names = sorted(n for n in _CREDENTIAL_PARTS if n.startswith("."))
    stores = [home / n for n in names] if home is not None else []
    stores.extend(home / a / b for a, b in sorted(_CREDENTIAL_PAIRS)) if home is not None else None
    stores.append(_axon_home() / ".env")
    return [s for s in stores if s.exists()]


def holds_credentials(raw: str | Path, root: Path | None = None) -> str | None:
    """The credential store a directory contains, for commands that read a whole tree (``grep -r``, ``tar``)."""
    p = _expand(raw)
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    try:
        if not p.is_dir():
            return None
        for store in credential_stores():
            if is_within(p, store):
                return ".axon/.env" if store.name == ".env" else store.name
    except OSError:
        return None
    return None


def is_protected_path(raw: str | Path, root: Path | None = None) -> str | None:
    """Return a reason string when ``raw`` points at a credential store or system dir."""
    p = _expand(raw)
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    resolved = _real(p)
    part = _is_credential_path(resolved)
    if part:
        return f"protected credential location '{part}'"
    if _is_system_path(resolved):
        return "protected system directory"
    return None


# Writes here are never auto-approved except in bypass mode: repository state, tool configuration and
# files a shell or build tool runs on its own.
PROTECTED_WRITE_DIRS = frozenset({".git", ".vscode", ".idea", ".husky", ".cargo", ".devcontainer", ".yarn", ".mvn", ".axon", ".claude"})
PROTECTED_WRITE_FILES = frozenset({
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".bash_login", ".bash_aliases", ".bash_logout", ".zshrc", ".zprofile",
    ".zshenv", ".zlogin", ".zlogout", ".profile", ".envrc", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs", ".pnp.loader.mjs",
    ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml", ".bazelrc", ".bazelversion", ".bazeliskrc", ".pre-commit-config.yaml",
    "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml", "gradle-wrapper.properties", "maven-wrapper.properties",
    ".devcontainer.json", ".ripgreprc", "pyrightconfig.json", ".mcp.json", ".claude.json", ".axon.json",
})


def _protected_exception(parts: list[str], i: int) -> bool:
    """Axon's own working files under .axon: worktrees, plan files, and Markdown memory."""
    rest = parts[i + 1:]
    if rest[:1] in (["worktrees"], ["plans"], ["jobs"]):
        return True
    return bool(rest) and rest[-1].endswith(".md") and ("agent-memory" in rest or "memory" in rest)


def protected_write(raw: str | Path, root: Path | None = None) -> str | None:
    """The protected directory or file a write would touch, else None. Checks the requested path and what it resolves to."""
    p = _expand(raw)
    if not p.is_absolute() and root is not None:
        p = Path(root) / p
    for candidate in (Path(os.path.normpath(str(p))), _real(p)):
        parts = [x.lower() for x in candidate.parts] if IS_WINDOWS else list(candidate.parts)
        if parts and parts[-1] in PROTECTED_WRITE_FILES:
            return parts[-1]
        for a, b in zip(parts, parts[1:]):
            if (a, b) == (".config", "git"):
                return ".config/git"
        for i, part in enumerate(parts[:-1] if parts else []):
            if part in PROTECTED_WRITE_DIRS and not (part in (".axon", ".claude") and _protected_exception(parts, i)):
                return part
    return None


def is_network_path(raw: str | Path) -> bool:
    """A path whose lookup can contact another computer: a UNC share, or a /net automount."""
    s = str(raw).strip()
    if s.startswith("\\\\") or (IS_WINDOWS and s.startswith("//") and not s.startswith("//?")):
        return True
    return s == "/net" or s.startswith("/net/")


def critical_removal_targets(workspace: Path, extra_dirs: list[Path] | None = None) -> tuple[list[str], list[str]]:
    """(paths whose removal is critical, directories where only a glob below them is)."""
    home = _real(Path.home())
    ws = _real(workspace)
    exact = [str(home), str(ws)] + [str(x) for x in ws.parents]
    globs = [str(_real(d)) for d in (extra_dirs or [])]
    return list(dict.fromkeys(exact)), globs


def too_broad_for_workspace(raw: str | Path) -> str | None:
    """A plain reason when ``raw`` is the filesystem root or the home folder itself (a project lives inside them)."""
    try:
        p = _real(_expand(raw))
    except Exception:
        return None
    if p.parent == p:
        return "That is the whole disk, which is too broad to work in. Pick a project folder instead."
    try:
        if p in (_real(Path.home()), _real(os.path.expanduser("~"))):
            return "That is your home folder, which is too broad to work in. Pick a project folder inside it instead."
    except Exception:
        pass
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
    p = _expand(raw)
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
    raw_p = _expand(raw)
    target = raw_p if raw_p.is_absolute() else Path(root) / raw_p
    return is_within(root, target) or in_additional_dir(target)
