"""
Workspace file enumeration shared by Glob, Grep (fallback), CodeSearch and UI pickers.

Public contract:
    workspace_files(root, limit=...) -> list[str]
        Workspace-relative POSIX paths. Uses `git ls-files` when `root` is inside a
        git work tree, otherwise a pure-Python walk that honours .gitignore files.
    iter_files(root, ...) -> Iterator[Path]
        The ignore-aware walk itself (with hidden-file and heavy-dir control).
    glob_to_regex(pattern) -> re.Pattern
        Git-style glob translation (supports **, *, ?, [..], {a,b}).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Iterator

# Directories that are never worth descending into for code search.
HEAVY_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".gradle", ".idea", ".next",
    ".turbo", ".parcel-cache", "bower_components", ".eggs", ".terraform",
})
JUNK_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


# ─── glob → regex ────────────────────────────────────────────────────────────

def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[: m.start()], pattern[m.end():]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(head + alt + tail))
    return out


def _translate(pat: str) -> str:
    i, n, res = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                # "**/" matches zero or more directories; trailing "**" matches everything.
                if pat[i + 2 : i + 3] == "/":
                    res.append("(?:.*/)?")
                    i += 3
                else:
                    res.append(".*")
                    i += 2
                continue
            res.append("[^/]*")
        elif c == "?":
            res.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                res.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                res.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            res.append(re.escape(c))
        i += 1
    return "".join(res)


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str, ignore_case: bool = False) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/") if os.sep == "\\" else pattern
    alts = [_translate(p) for p in _expand_braces(pattern)]
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile("(?:" + "|".join(alts) + r")\Z", flags)


# ─── .gitignore ──────────────────────────────────────────────────────────────

class _Rule:
    __slots__ = ("regex", "negate", "dir_only", "anchored")

    def __init__(self, regex: re.Pattern[str], negate: bool, dir_only: bool, anchored: bool) -> None:
        self.regex, self.negate, self.dir_only, self.anchored = regex, negate, dir_only, anchored


def _parse_gitignore(text: str) -> list[_Rule]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        if line.startswith("\\"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        rules.append(_Rule(glob_to_regex(line), negate, dir_only, anchored))
    return rules


class IgnoreMatcher:
    """Hierarchical .gitignore evaluation (last matching rule wins, nested files override)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._by_dir: dict[Path, list[_Rule]] = {}

    def load_dir(self, d: Path) -> None:
        if d in self._by_dir:
            return
        rules: list[_Rule] = []
        for name in (".gitignore", ".ignore", ".axonignore"):
            f = d / name
            if f.is_file():
                try:
                    rules.extend(_parse_gitignore(f.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass
        if d == self.base:
            excl = d / ".git" / "info" / "exclude"
            if excl.is_file():
                try:
                    rules = _parse_gitignore(excl.read_text(encoding="utf-8", errors="replace")) + rules
                except OSError:
                    pass
        self._by_dir[d] = rules

    def ignored(self, path: Path, is_dir: bool) -> bool:
        result = False
        # Evaluate from the outermost directory inward so deeper rules win.
        try:
            rel_parts = path.relative_to(self.base).parts
        except ValueError:
            return False
        d = self.base
        dirs = [d]
        for part in rel_parts[:-1]:
            d = d / part
            dirs.append(d)
        for d in dirs:
            rules = self._by_dir.get(d)
            if not rules:
                continue
            rel = path.relative_to(d).as_posix()
            name = path.name
            for r in rules:
                if r.dir_only and not is_dir:
                    continue
                target = rel if r.anchored else name
                if r.regex.match(target):
                    result = not r.negate
        return result


def _find_ignore_base(root: Path) -> Path:
    """Topmost directory whose .gitignore applies: the git work-tree root if any."""
    cur = root
    for parent in [root, *root.parents]:
        if (parent / ".git").exists():
            return parent
    return cur


def iter_files(
    root: Path,
    *,
    include_hidden: bool = True,
    respect_gitignore: bool = True,
    skip_heavy: bool = True,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """Walk `root` yielding files, pruning ignored/heavy directories early."""
    root = root.resolve()
    if root.is_file():
        yield root
        return
    matcher: IgnoreMatcher | None = None
    if respect_gitignore:
        base = _find_ignore_base(root)
        matcher = IgnoreMatcher(base)
        # Load ignore files of every ancestor between the repo root and `root`.
        chain = [root, *[p for p in root.parents if p == base or base in p.parents]]
        for d in reversed(chain):
            matcher.load_dir(d)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        d = Path(dirpath)
        if matcher is not None:
            matcher.load_dir(d)
        keep = []
        for name in sorted(dirnames):
            if skip_heavy and name in HEAVY_DIRS:
                continue
            if name == ".git":
                continue
            if not include_hidden and name.startswith("."):
                continue
            if matcher is not None and matcher.ignored(d / name, True):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name in JUNK_FILES:
                continue
            if not include_hidden and name.startswith("."):
                continue
            fp = d / name
            if matcher is not None and matcher.ignored(fp, False):
                continue
            yield fp


def _git_ls_files(root: Path, limit: int) -> list[str] | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        res = subprocess.run(
            [git, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(root), capture_output=True, timeout=15, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in res.stdout.decode("utf-8", errors="replace").split("\0"):
        if not item or item in seen:
            continue
        seen.add(item)
        rel = item.replace("\\", "/")
        if not os.path.isfile(os.path.join(root, rel)):
            continue  # deleted from the work tree but still in the index
        out.append(rel)
        if len(out) >= limit:
            break
    return out


def workspace_files(root: Path | str, limit: int = 20_000) -> list[str]:
    """Return up to `limit` workspace-relative POSIX paths of non-ignored files."""
    root_p = Path(root).resolve()
    files = _git_ls_files(root_p, limit)
    if files is not None:
        return files
    out: list[str] = []
    for fp in iter_files(root_p):
        try:
            out.append(fp.relative_to(root_p).as_posix())
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
