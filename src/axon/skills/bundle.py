"""
A whole skill folder (SKILL.md plus scripts/, references/, assets/...) read from a GitHub tarball.

Nothing is executed and nothing is written here: :func:`read_tarball` turns the archive bytes into an
in-memory :class:`SkillBundle` after these checks, and :func:`write_bundle` puts it on disk atomically.

* every path stays inside the skill folder (no absolute paths, no ``..``, no backslashes or NUL);
* a symlink or hard link whose target leaves the folder rejects the whole skill; one that stays inside is
  skipped (links are never created on disk);
* device files, FIFOs and the like reject the skill;
* caps: ``MAX_FILES`` files, ``MAX_FILE_BYTES`` per file, ``MAX_TOTAL_BYTES`` in all.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILES = 300
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_ARCHIVE_BYTES = 40_000_000
ORIGIN_FILE = ".axon-source.json"
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1",
                   ".bat", ".cmd", ".command", ".lua", ".r"}
SCRIPT_DIRS = {"scripts", "bin"}


class BundleError(ValueError):
    """The archive does not hold a safe skill folder."""


@dataclass
class SkillBundle:
    source: str
    commit: str = ""
    files: dict[str, bytes] = field(default_factory=dict)       # relative posix path -> content
    modes: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)            # links that stay inside the folder (not created)

    @property
    def skill_md(self) -> str:
        return self.files.get("SKILL.md", b"").decode("utf-8", errors="replace")

    def executables(self) -> list[str]:
        return sorted(p for p, data in self.files.items() if is_executable(p, data, self.modes.get(p, 0)))

    def total_bytes(self) -> int:
        return sum(len(d) for d in self.files.values())

    def content_hash(self) -> str:
        return hash_files(self.files)

    def listing(self) -> list[str]:
        """One line per file, scripts flagged, for the install confirmation."""
        run = set(self.executables())
        lines = []
        for path in sorted(self.files):
            flag = "  [EXECUTABLE SCRIPT]" if path in run else ""
            lines.append(f"    {path}  ({len(self.files[path])} bytes){flag}")
        for path in self.skipped:
            lines.append(f"    {path}  [symlink inside the folder: not installed]")
        return lines


def is_executable(path: str, data: bytes, mode: int = 0) -> bool:
    parts = path.split("/")
    suffix = posixpath.splitext(parts[-1])[1].lower()
    return bool(mode & 0o111) or suffix in SCRIPT_SUFFIXES or data[:2] == b"#!" or (len(parts) > 1 and parts[0] in SCRIPT_DIRS)


def hash_files(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        if path == ORIGIN_FILE:
            continue
        h.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).digest())
    return h.hexdigest()


def hash_dir(folder: Path, exclude: frozenset[str] = frozenset()) -> str:
    return hash_files(read_dir(folder, exclude))


def read_dir(folder: Path, exclude: frozenset[str] = frozenset()) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for root, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in names:
            p = Path(root) / name
            if p.is_symlink() or name == ORIGIN_FILE or p.relative_to(folder).as_posix() in exclude:
                continue
            try:
                files[p.relative_to(folder).as_posix()] = p.read_bytes()
            except OSError:
                continue
    return files


def _clean_rel(rel: str) -> str:
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        raise BundleError(f"unsafe path in archive: {rel!r}")
    norm = posixpath.normpath(rel)
    if norm.startswith("..") or norm == "." or any(part == ".." for part in rel.split("/")):
        raise BundleError(f"path escapes the skill folder: {rel!r}")
    return norm


def read_tarball(data: bytes, subpath: str, source: str = "") -> SkillBundle:
    """Extract ``<top>/<subpath>/**`` of a GitHub tar.gz into a bundle (see the module checks)."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise BundleError("archive is larger than the download cap")
    sub = subpath.strip("/")
    bundle = SkillBundle(source=source)
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BundleError(f"not a readable tar.gz archive: {e}") from e
    total = 0
    with tf:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, OSError, EOFError) as e:
            raise BundleError(f"damaged archive: {e}") from e
        comment = (tf.pax_headers or {}).get("comment", "")
        if isinstance(comment, str) and len(comment.strip()) == 40:
            bundle.commit = comment.strip()
        if not members:
            raise BundleError("empty archive")
        top = members[0].name.split("/")[0]
        prefix = top + "/" + (sub + "/" if sub else "")
        for m in members:
            if not (m.name + ("/" if m.isdir() else "")).startswith(prefix) or m.name.rstrip("/") + "/" == prefix:
                continue
            rel = _clean_rel(m.name[len(prefix):])
            if m.isdir():
                continue
            if m.issym() or m.islnk():
                if m.issym():
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), m.linkname))
                    inside = not (target.startswith("..") or posixpath.isabs(m.linkname))
                else:
                    inside = posixpath.normpath(m.linkname).startswith(prefix.rstrip("/") + "/")
                if not inside:
                    raise BundleError(f"symlink {rel!r} points outside the skill folder ({m.linkname!r})")
                bundle.skipped.append(rel)
                continue
            if not m.isreg():
                raise BundleError(f"{rel!r} is not a regular file")
            if m.size > MAX_FILE_BYTES:
                raise BundleError(f"{rel!r} is larger than {MAX_FILE_BYTES // 1000} KB")
            total += m.size
            if total > MAX_TOTAL_BYTES:
                raise BundleError(f"skill is larger than {MAX_TOTAL_BYTES // 1_000_000} MB")
            if len(bundle.files) >= MAX_FILES:
                raise BundleError(f"skill has more than {MAX_FILES} files")
            fh = tf.extractfile(m)
            if fh is None:
                raise BundleError(f"cannot read {rel!r}")
            bundle.files[rel] = fh.read()
            bundle.modes[rel] = m.mode
    if "SKILL.md" not in bundle.files:
        raise BundleError("no SKILL.md at the top of that folder")
    return bundle


def write_bundle(bundle: SkillBundle, target: Path, overwrite: bool = False) -> None:
    """Write the bundle to ``target`` atomically (temp folder next to it, then rename)."""
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists (use --force to replace)")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.axon-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        root = tmp.resolve()
        for rel, data in bundle.files.items():
            dest = (tmp / rel).resolve()
            if root not in dest.parents:
                raise BundleError(f"path escapes the skill folder: {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o755 if bundle.modes.get(rel, 0) & 0o111 else 0o644)
        backup = None
        if target.exists():
            backup = target.parent / f".{target.name}.axon-old"
            shutil.rmtree(backup, ignore_errors=True)
            target.rename(backup)
        try:
            tmp.rename(target)
        except Exception:
            if backup is not None:
                backup.rename(target)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def diff_summary(old: dict[str, bytes], new: dict[str, bytes], modes: dict[str, int] | None = None) -> list[str]:
    """Plain lines for "what changes": added / removed / changed files, executables flagged, plus a short
    unified diff of SKILL.md."""
    import difflib
    modes = modes or {}
    lines: list[str] = []
    for path in sorted(set(old) | set(new)):
        if path == ORIGIN_FILE:
            continue
        if path not in old:
            kind = "added"
        elif path not in new:
            kind = "removed"
        elif old[path] != new[path]:
            kind = "changed"
        else:
            continue
        data = new.get(path, old.get(path, b""))
        flag = "  [EXECUTABLE SCRIPT]" if is_executable(path, data, modes.get(path, 0)) else ""
        lines.append(f"    {kind:<8} {path}{flag}")
    if "SKILL.md" in old and "SKILL.md" in new and old["SKILL.md"] != new["SKILL.md"]:
        diff = list(difflib.unified_diff(old["SKILL.md"].decode("utf-8", "replace").splitlines(),
                                         new["SKILL.md"].decode("utf-8", "replace").splitlines(),
                                         "installed/SKILL.md", "new/SKILL.md", lineterm="", n=1))
        lines.append("    --- SKILL.md diff (first lines) ---")
        lines += ["    " + d for d in diff[:30]]
        if len(diff) > 30:
            lines.append(f"    ... {len(diff) - 30} more diff line(s)")
    return lines
