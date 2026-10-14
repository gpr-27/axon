"""
Interactive fuzzy file finder (Ctrl+P / /find), built on the shared Selector.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from axon.ui.picker import SelectItem, Selector

_SKIP_DIRS = {
    ".git", ".axon", "node_modules", "__pycache__", "venv", ".venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "dist", "build", ".eggs", "target", ".cache", ".idea", ".vscode", "site-packages",
}
_SKIP_EXTS = {".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".class", ".whl", ".zip",
              ".tgz", ".gz", ".dmg", ".iso", ".pkg", ".mp4", ".mov"}
_cache: dict[str, tuple[float, list[str]]] = {}


def list_workspace_files(root: Path, limit: int = 8000, ttl: float = 5.0) -> list[str]:
    """Workspace-relative POSIX paths (git-aware when axon.tools._files is available). Cached."""
    key = str(Path(root).resolve())
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    files: list[str] = []
    try:
        from axon.tools._files import workspace_files  # shared, .gitignore-aware
        files = list(workspace_files(Path(root), limit=limit))
    except (ImportError, AttributeError, TypeError):
        files = _walk(Path(root), limit)
    except Exception:
        files = _walk(Path(root), limit)
    files = [f for f in files if os.path.splitext(f)[1].lower() not in _SKIP_EXTS]
    _cache[key] = (time.monotonic(), files)
    return files


def _walk(root: Path, limit: int, budget_s: float = 0.5) -> list[str]:
    out: list[str] = []
    t0 = time.monotonic()
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/"))
            if len(out) >= limit:
                return out
        if time.monotonic() - t0 > budget_s:
            break
    return out


def _fuzzy_score(query: str, target: str) -> float:
    """Fuzzy match score (0 = no match). Substring hits in the file name rank highest."""
    q = query.lower()
    t = target.lower()
    if not q:
        return 1.0
    if q in t:
        name = Path(target).name.lower()
        if q in name:
            return 10.0 + (1.0 / (name.find(q) + 1))
        return 5.0 + (1.0 / (t.find(q) + 1))
    score = 0.0
    t_idx = 0
    consecutive = 0
    for ch in q:
        found = t.find(ch, t_idx)
        if found == -1:
            return 0.0
        if found == t_idx:
            consecutive += 1
            score += 2.0 * consecutive
        else:
            consecutive = 0
            score += 1.0 / (found - t_idx + 1)
        t_idx = found + 1
    return score


def rank_files(query: str, files: list[str], limit: int = 50) -> list[str]:
    if not query:
        return files[:limit]
    scored = [(s, f) for f in files if (s := _fuzzy_score(query, f)) > 0]
    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [f for _, f in scored[:limit]]


def run_fuzzy_file_finder(workspace: Path) -> str | None:
    """Interactive fuzzy finder. Returns the selected workspace-relative path, or None."""
    files = list_workspace_files(Path(workspace))
    if not files:
        return None

    def filter_fn(query: str) -> list[SelectItem]:
        out = []
        for f in rank_files(query, files):
            parent, _, name = f.rpartition("/")
            out.append(SelectItem(label=name, value=f, hint=parent + "/" if parent else ""))
        return out

    sel = Selector(title="Find file", subtitle=f"{len(files)} files", filterable=True, filter_fn=filter_fn,
                   placeholder="type to fuzzy-search files", footer="↑↓ move · enter insert @path · esc cancel")
    chosen = sel.run()
    return chosen.value if isinstance(chosen, SelectItem) else None
