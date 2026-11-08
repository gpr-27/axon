"""Git views for the Changes and Worktrees screens: plain functions, no terminal and no web imports.

The terminal screens (``axon.commands.workbench_cmds``) and the web workbench both read these, so the
two show the same numbers. Everything shells out to ``git`` once per call (never once per file), so a
working tree with hundreds of changed files stays quick.

* ``changes(ws, target, ref)``: the changed files for a compare target (``working`` tree vs HEAD,
  ``staged``, ``commit`` = the last commit, ``branch`` = working tree vs the merge-base with *ref*),
  each with a status code, folder, ``+added -removed`` and the totals.
* ``file_diff`` / ``parse_diff``: one file's diff as numbered rows.
* ``stage`` / ``unstage`` / ``discard``: act on one path (paths are checked to stay inside the repo).
* ``list_worktrees`` / ``create_worktree`` / ``remove_worktree``: ``git worktree`` with change counts.
"""
from __future__ import annotations

import os
import re
import shutil
from axon.platform import rmtree as _rmtree
import subprocess
from pathlib import Path
from typing import Any

TARGETS = ("working", "staged", "commit", "branch")
TARGET_LABEL = {"working": "working tree", "staged": "staged", "commit": "last commit", "branch": "branch"}
STATUS_WORD = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "U": "untracked", "C": "conflict"}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_DIFF_CHARS = 400_000
MAX_COUNTED_UNTRACKED = 5000         # untracked files whose lines are counted (over 1 MB or binary counts 0)


def _run(ws: Path | str, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[bytes] | None:
    git = shutil.which("git")
    if not git:
        return None
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    try:
        return subprocess.run([git, "-C", str(ws), *args], capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def git_text(ws: Path | str, *args: str, timeout: float = 15.0) -> str | None:
    """Standard output of a git command, or None when it failed."""
    r = _run(ws, *args, timeout=timeout)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def git_error(ws: Path | str, *args: str, timeout: float = 30.0) -> tuple[bool, str]:
    r = _run(ws, *args, timeout=timeout)
    if r is None:
        return False, "git is not available"
    msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
    return r.returncode == 0, msg


def repo_info(ws: Path | str) -> dict[str, Any]:
    top = git_text(ws, "rev-parse", "--show-toplevel")
    if top is None:
        return {"repo": False, "root": "", "branch": "", "has_head": False}
    branch = (git_text(ws, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    has_head = git_text(ws, "rev-parse", "--verify", "-q", "HEAD") is not None
    if not has_head:
        branch = (git_text(ws, "symbolic-ref", "--short", "HEAD") or branch or "").strip()
    elif branch == "HEAD":
        branch = "detached " + (git_text(ws, "rev-parse", "--short", "HEAD") or "").strip()
    return {"repo": True, "root": top.strip(), "branch": branch, "has_head": has_head}


def branch_names(ws: Path | str) -> list[str]:
    out = git_text(ws, "for-each-ref", "--format=%(refname:short)", "--sort=-committerdate", "refs/heads", "refs/remotes") or ""
    return [b for b in out.split() if b and not b.endswith("/HEAD")]


def _count_lines(p: Path) -> int:
    try:
        if p.stat().st_size > 1_000_000:
            return 0
        data = p.read_bytes()
        return 0 if b"\0" in data[:4096] else data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    except OSError:
        return 0


def valid_ref(ws: Path | str, ref: str) -> bool:
    """True when *ref* names a commit. A ref that starts with a dash is an option to git (``--output=FILE`` would
    overwrite a file), so it is refused before git ever sees it."""
    if not ref or ref.startswith("-") or any(c in ref for c in "\0\n\r"):
        return False
    return git_text(ws, "rev-parse", "--verify", "-q", ref + "^{commit}") is not None


def _base_args(ws: Path | str, target: str, ref: str, has_head: bool) -> tuple[list[str] | None, str]:
    """git-diff arguments for a compare target and the label of what is being compared with."""
    if target == "staged":
        return ["--cached"], "HEAD"
    if target == "commit":
        if not has_head:
            return None, ""
        parent = "HEAD~1" if git_text(ws, "rev-parse", "--verify", "-q", "HEAD~1") is not None else EMPTY_TREE
        return [parent, "HEAD"], "HEAD~1"
    if target == "branch":
        if not valid_ref(ws, ref):
            return None, ""
        base = (git_text(ws, "merge-base", ref, "HEAD") or "").strip()
        return ([base] if base else [ref]), ref
    return (["HEAD"] if has_head else []), "HEAD"


def _stat_code(c: str) -> str:
    c = c.strip()[:1] or "M"
    return c if c in STATUS_WORD else "M"


_COUNT_CACHE: dict[str, tuple[float, int]] = {}


def changed_count(ws: Path | str) -> int:
    """Number of changed files, the same figure the Changes panel and ``/diff`` show (cached for 4 seconds)."""
    import time
    key, now = str(ws), time.time()
    hit = _COUNT_CACHE.get(key)
    if hit and now - hit[0] < 4:
        return hit[1]
    n = len(changes(ws).get("files") or [])
    _COUNT_CACHE[key] = (now, n)
    return n


def changes(ws: Path | str, target: str = "working", ref: str = "") -> dict[str, Any]:
    """Changed files for a compare target. Paths are relative to *ws*; sorted by folder then name.

    ``{"repo", "branch", "target", "base", "label", "files": [{"path", "dir", "name", "code", "status",
    "added", "removed", "staged", "binary"}], "added", "removed", "error"}``"""
    ws = Path(ws)
    target = target if target in TARGETS else "working"
    info = repo_info(ws)
    out: dict[str, Any] = {"repo": info["repo"], "branch": info["branch"], "target": target, "base": "", "files": [],
                           "added": 0, "removed": 0, "error": "", "ref": ref}
    if not info["repo"]:
        out["error"] = "This folder is not a git repository."
        out["label"] = ""
        return out
    base, base_label = _base_args(ws, target, ref, info["has_head"])
    out["base"] = base_label
    if target == "working":
        out["label"] = f"{info['branch']} → working tree"
    elif target == "staged":
        out["label"] = "HEAD → staged"
    elif target == "commit":
        out["label"] = "HEAD~1 → HEAD"
    else:
        out["label"] = f"{ref or '?'} → working tree"
    if base is None:
        out["error"] = "Pick a branch to compare with." if target == "branch" else "There is no commit yet."
        return out
    top = Path(info["root"])
    stat: dict[str, tuple[int, int, bool]] = {}
    txt = git_text(ws, "diff", *base, "--numstat", "-z", "--no-renames", "--relative") or ""
    for ent in txt.split("\0"):
        m = re.match(r"(\d+|-)\t(\d+|-)\t(.+)", ent)
        if m:
            a, d, p = m.groups()
            stat[p] = (0 if a == "-" else int(a), 0 if d == "-" else int(d), a == "-")
    codes: dict[str, str] = {}
    ns = git_text(ws, "diff", *base, "--name-status", "-z", "--no-renames", "--relative") or ""
    parts = ns.split("\0")
    for i in range(0, len(parts) - 1, 2):
        codes[parts[i + 1]] = _stat_code(parts[i])
    staged: set[str] = set()
    untracked: list[str] = []
    if target in ("working", "staged"):
        st = git_text(ws, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
        sp = st.split("\0")
        i = 0
        while i < len(sp):
            ent = sp[i]
            i += 1
            if len(ent) < 4:
                continue
            xy, path = ent[:2], ent[3:]
            if ent[0] in "RC":
                i += 1
            try:
                rel = (top / path).resolve().relative_to(Path(ws).resolve()).as_posix()
            except ValueError:
                continue
            if xy == "??":
                if target == "working":
                    untracked.append(rel)
            elif xy[0] not in " ?":
                staged.add(rel)
            if xy in ("UU", "AA", "DD") or "U" in xy:
                codes[rel] = "C"
    files: list[dict[str, Any]] = []
    for p, (a, d, binary) in stat.items():
        files.append({"path": p, "code": codes.get(p, "M"), "added": a, "removed": d, "binary": binary})
    for p in codes:
        if p not in stat:
            files.append({"path": p, "code": codes[p], "added": 0, "removed": 0, "binary": False})
    for n, p in enumerate(untracked):
        files.append({"path": p, "code": "U", "added": _count_lines(Path(ws) / p) if n < MAX_COUNTED_UNTRACKED else 0,
                      "removed": 0, "binary": False})
    seen: set[str] = set()
    rows = []
    for f in sorted(files, key=lambda f: (str(Path(f["path"]).parent), Path(f["path"]).name)):
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        p = Path(f["path"])
        f["dir"] = "" if str(p.parent) == "." else p.parent.as_posix()
        f["name"] = p.name
        f["status"] = STATUS_WORD.get(f["code"], "modified")
        f["staged"] = f["path"] in staged
        rows.append(f)
        out["added"] += f["added"]
        out["removed"] += f["removed"]
    out["files"] = rows
    return out


def parse_diff(text: str) -> list[dict[str, Any]]:
    """Unified diff text -> rows ``{"kind": "hunk"|"add"|"del"|"ctx"|"meta", "old", "new", "text"}``.
    Line numbers come from the hunk headers; file headers are dropped."""
    rows: list[dict[str, Any]] = []
    old = new = 0
    in_hunk = False
    for line in text.rstrip("\n").split("\n"):
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            if m:
                old, new, in_hunk = int(m.group(1)), int(m.group(2)), True
                rows.append({"kind": "hunk", "old": None, "new": None, "text": line})
            continue
        if not in_hunk:
            if line.startswith("Binary files") or line.startswith("GIT binary patch"):
                rows.append({"kind": "meta", "old": None, "new": None, "text": "Binary file"})
            continue
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if line.startswith("+"):
            rows.append({"kind": "add", "old": None, "new": new, "text": line[1:]})
            new += 1
        elif line.startswith("-"):
            rows.append({"kind": "del", "old": old, "new": None, "text": line[1:]})
            old += 1
        elif line.startswith("\\"):
            rows.append({"kind": "meta", "old": None, "new": None, "text": line[1:].strip()})
        elif line.startswith(" ") or line == "":
            rows.append({"kind": "ctx", "old": old, "new": new, "text": line[1:]})
            old += 1
            new += 1
    return rows


def file_diff(ws: Path | str, path: str, target: str = "working", ref: str = "", code: str = "") -> dict[str, Any]:
    """``{"rows", "truncated", "note"}`` for one file. Untracked files are shown as all-added."""
    ws = Path(ws)
    info = repo_info(ws)
    if not info["repo"]:
        return {"rows": [], "truncated": False, "note": "This folder is not a git repository."}
    if code == "U":
        p = _inside(ws, path)
        if p is None:
            return {"rows": [], "truncated": False, "note": "Outside the project."}
        try:
            data = p.read_bytes()[:MAX_DIFF_CHARS]
        except OSError:
            return {"rows": [], "truncated": False, "note": "Could not read the file."}
        if b"\0" in data[:4096]:
            return {"rows": [{"kind": "meta", "old": None, "new": None, "text": "Binary file"}], "truncated": False, "note": ""}
        lines = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")        # a Windows file ends lines in CRLF
        if lines and lines[-1] == "":
            lines.pop()
        return {"rows": [{"kind": "add", "old": None, "new": i, "text": t} for i, t in enumerate(lines, 1)],
                "truncated": p.stat().st_size > MAX_DIFF_CHARS, "note": ""}
    base, _ = _base_args(ws, target, ref, info["has_head"])
    if base is None:
        return {"rows": [], "truncated": False, "note": "Nothing to compare."}
    text = git_text(ws, "diff", *base, "--no-color", "--no-ext-diff", "--no-renames", "--relative", "--", path) or ""
    truncated = len(text) > MAX_DIFF_CHARS
    return {"rows": parse_diff(text[:MAX_DIFF_CHARS]), "truncated": truncated, "note": "" if text.strip() else "No textual changes."}


def _inside(ws: Path, path: str) -> Path | None:
    """*path* resolved against *ws*, only when it stays inside it."""
    try:
        p = (ws / path).resolve()
        p.relative_to(ws.resolve())
        return p
    except (ValueError, OSError):
        return None


def stage(ws: Path | str, path: str) -> tuple[bool, str]:
    if _inside(Path(ws), path) is None:
        return False, "Outside the project."
    ok, msg = git_error(ws, "add", "--", path)
    return ok, msg or f"Staged {path}"


def unstage(ws: Path | str, path: str) -> tuple[bool, str]:
    if _inside(Path(ws), path) is None:
        return False, "Outside the project."
    has_head = repo_info(ws)["has_head"]
    ok, msg = git_error(ws, *(["restore", "--staged", "--", path] if has_head else ["rm", "--cached", "-q", "--", path]))
    return ok, msg or f"Unstaged {path}"


def discard(ws: Path | str, path: str, code: str = "") -> tuple[bool, str]:
    """Throw away the working-tree change of one file: restores a tracked file from the index/HEAD, deletes an
    untracked one. Never touches anything outside the project."""
    ws = Path(ws)
    p = _inside(ws, path)
    if p is None:
        return False, "Outside the project."
    if code == "U" and (p == ws.resolve() or ".git" in p.relative_to(ws.resolve()).parts):
        return False, "That path cannot be discarded."      # never the project folder itself, never git's own files
    if code == "U":
        try:
            if p.is_dir():
                _rmtree(p)
            else:
                p.unlink()
        except OSError as e:
            return False, str(e)
        return True, f"Deleted {path}"
    has_head = repo_info(ws)["has_head"]
    if code == "A" and not has_head:
        ok, msg = git_error(ws, "rm", "-f", "-q", "--", path)
    else:
        ok, msg = git_error(ws, "restore", "--source=HEAD", "--staged", "--worktree", "--", path)
    return ok, msg or f"Discarded changes to {path}"


# ─── worktrees ──────────────────────────────────────────────────────────────

def _counts(path: str) -> dict[str, int]:
    """{"files", "added", "removed"} of the uncommitted change in one worktree."""
    ch = changes(path, "working")
    return {"files": len(ch["files"]), "added": ch["added"], "removed": ch["removed"]}


def list_worktrees(ws: Path | str) -> list[dict[str, Any]]:
    """Every worktree of the repository: ``{"path", "branch", "head", "current", "main", "prunable", "locked",
    "files", "added", "removed", "dirty"}``. The main worktree comes first."""
    ws = Path(ws)
    txt = git_text(ws, "worktree", "list", "--porcelain")
    if txt is None:
        return []
    here = ws.resolve()
    rows: list[dict[str, Any]] = []
    for block in txt.strip().split("\n\n"):
        d: dict[str, Any] = {"path": "", "branch": "", "head": "", "prunable": False, "locked": False}
        for line in block.splitlines():
            key, _, val = line.partition(" ")
            if key == "worktree":
                d["path"] = val
            elif key == "HEAD":
                d["head"] = val[:7]
            elif key == "branch":
                d["branch"] = val.removeprefix("refs/heads/")
            elif key == "detached":
                d["branch"] = "detached"
            elif key == "bare":
                d["branch"] = "bare"
            elif key == "prunable":
                d["prunable"] = True
            elif key == "locked":
                d["locked"] = True
        if d["path"]:
            rows.append(d)
    for i, d in enumerate(rows):
        d["main"] = i == 0
        try:
            d["current"] = Path(d["path"]).resolve() == here or here.is_relative_to(Path(d["path"]).resolve()) and not any(
                r is not d and here.is_relative_to(Path(r["path"]).resolve()) and len(r["path"]) > len(d["path"]) for r in rows)
        except (OSError, ValueError):
            d["current"] = False
        if Path(d["path"]).is_dir() and not d["prunable"] and d["branch"] != "bare":
            d.update(_counts(d["path"]))
        else:
            d.update({"files": 0, "added": 0, "removed": 0})
        d["dirty"] = d["files"] > 0
    return rows


def create_worktree(ws: Path | str, name: str) -> tuple[bool, str, str]:
    """New worktree on branch ``axon/<name>`` from HEAD (the same place ``axon --worktree`` uses). -> (ok, message, path)"""
    from axon.agent.worktree import create_session_worktree
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-.")
    if not clean:
        return False, "Give the worktree a name.", ""
    if any(w["path"].rstrip("/").endswith("/" + clean) for w in list_worktrees(ws)):
        return False, f"A worktree named {clean} already exists.", ""
    info = create_session_worktree(Path(ws), clean)
    if info is None:
        return False, "Could not create the worktree (needs a git repository with at least one commit).", ""
    return True, f"Created {info.branch_name}", str(info.worktree_path)


def remove_worktree(ws: Path | str, path: str, force: bool = False) -> tuple[bool, str]:
    """Remove a worktree. Refuses the main worktree, the one you are in and any with uncommitted changes
    (unless *force*)."""
    rows = {Path(w["path"]).resolve(): w for w in list_worktrees(ws)}
    w = rows.get(Path(path).resolve())
    if w is None:
        return False, "No such worktree."
    if w["main"]:
        return False, "This is the main worktree; it cannot be removed."
    if w["current"]:
        return False, "You are working in this worktree. Switch to another one first."
    if w["dirty"] and not force:
        return False, f"{w['files']} uncommitted change(s) in it. Commit or discard them first."
    if w["locked"]:
        return False, "This worktree is locked."
    ok, msg = git_error(ws, "worktree", "remove", *(["--force"] if force else []), str(w["path"]))
    return ok, msg or f"Removed {w['path']}"
