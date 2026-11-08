"""
Git Worktree Isolation for parallel subagents.
Creates lightweight, isolated worktrees for subagents to safely read and edit
code without colliding with the main working tree or other concurrent subagents.
"""
from __future__ import annotations
import logging
import os
from axon.platform import rmtree as _rmtree
from typing import Any
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class WorktreeInfo:
    task_id: str
    worktree_path: Path
    branch_name: str
    base_workspace: Path
    created: bool = False
    by_hook: bool = False       # made by a WorktreeCreate hook (git is not involved; see hook_remove_worktree)
    base_commit: str = ""       # the commit the worktree branched from (what merge_back diffs against)


class WorktreeManager:
    """Manages creation, synchronization, and cleanup of git worktrees for subagents."""

    @staticmethod
    def is_git_repo(workspace: Path) -> bool:
        """Check if workspace is a git repository with at least one commit."""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
            )
            if res.returncode != 0:
                return False
            # Check if there is at least one commit (HEAD exists)
            head_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
            )
            return head_res.returncode == 0
        except Exception:
            return False

    @staticmethod
    def head_commit(path: Path) -> str:
        """The commit HEAD points at in *path* ('' when git cannot say)."""
        try:
            res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=5)
        except Exception:
            return ""
        return res.stdout.strip() if res.returncode == 0 else ""

    @classmethod
    def create_worktree(cls, workspace: Path, task_id: str) -> WorktreeInfo | None:
        """Create an ephemeral worktree for a subagent task."""
        if not cls.is_git_repo(workspace):
            return None

        import uuid
        # Task ids ("sub-1") repeat across sessions and /clear: a unique suffix keeps another session's live
        # worktree from being found "existing" below and wiped.
        clean_id = "".join(c for c in task_id if c.isalnum() or c in ("-", "_")) + f"-{uuid.uuid4().hex[:8]}"
        branch_name = f"axon-subagent-{clean_id}"
        try:
            from axon import storage
            worktree_dir = storage.project_dir(workspace) / "worktrees" / clean_id
        except Exception:
            worktree_dir = workspace / ".axon" / "worktrees" / clean_id

        # Clean existing if dirty
        if worktree_dir.exists():
            cls.cleanup_worktree_path(workspace, worktree_dir, branch_name)

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Create worktree on a new branch from HEAD
            cmd = ["git", "-c", "core.longpaths=true", "worktree", "add", "-b", branch_name, str(worktree_dir), "HEAD"]
            res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True,
                encoding="utf-8",
                errors="replace", timeout=10)
            if res.returncode != 0:
                # If branch already exists, try without -b
                if "already exists" in res.stderr:
                    cmd = ["git", "-c", "core.longpaths=true", "worktree", "add", "--force", str(worktree_dir), "HEAD"]
                    res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True,
                encoding="utf-8",
                errors="replace", timeout=10)
                if res.returncode != 0:
                    logger.debug(f"Failed to create git worktree: {res.stderr}")
                    cls.cleanup_worktree_path(workspace, worktree_dir, branch_name)
                    return None

            return WorktreeInfo(
                task_id=task_id,
                worktree_path=worktree_dir,
                branch_name=branch_name,
                base_workspace=workspace,
                created=True,
                base_commit=cls.head_commit(worktree_dir),
            )
        except Exception as e:
            logger.debug(f"Exception creating worktree: {e}")
            cls.cleanup_worktree_path(workspace, worktree_dir, branch_name)   # e.g. git timed out half-way
            return None

    @classmethod
    def get_diff(cls, info: WorktreeInfo) -> str:
        """Return the uncommitted git diff inside the worktree."""
        if not info.created or not info.worktree_path.exists():
            return ""
        try:
            res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            return res.stdout if res.returncode == 0 else ""
        except Exception:
            return ""

    @classmethod
    def merge_back(cls, info: WorktreeInfo) -> tuple[bool, str]:
        """
        Merge changes made inside the worktree back into the main workspace: edits left uncommitted and
        commits the subagent made itself are applied together as one patch (git diff / git apply).
        """
        if not info.created or not info.worktree_path.exists():
            return False, "Worktree does not exist"

        try:
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            has_uncommitted = bool(status_res.stdout.strip())
            has_commits = bool(info.base_commit) and cls.head_commit(info.worktree_path) != info.base_commit
            if not has_uncommitted and not has_commits:
                return True, "No changes made in worktree"

            if has_uncommitted:
                subprocess.run(["git", "add", "-A"], cwd=str(info.worktree_path), capture_output=True, timeout=30)
                commit_res = subprocess.run(
                    ["git", "-c", "user.name=Axon", "-c", "user.email=axon@localhost", "commit", "--no-verify", "-m", f"subagent({info.task_id}): automated modifications"],
                    cwd=str(info.worktree_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
                if commit_res.returncode != 0:
                    # Without a fresh commit, the diff below would re-apply the user's last real commit.
                    return False, f"Could not commit worktree changes: {(commit_res.stderr or commit_res.stdout).strip()}"

            patch_res = subprocess.run(
                ["git", "diff", "--binary", info.base_commit or "HEAD~1", "HEAD"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if patch_res.returncode == 0 and patch_res.stdout.strip():
                apply_res = subprocess.run(
                    ["git", "apply", "--3way", "--whitespace=nowarn"],
                    input=patch_res.stdout,
                    cwd=str(info.base_workspace),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                if apply_res.returncode == 0:
                    return True, "Changes successfully applied to workspace"
                return False, f"Merge conflict applying worktree changes: {apply_res.stderr}"

            return True, "Worktree committed"
        except Exception as e:
            return False, f"Failed to merge worktree changes: {e}"

    @classmethod
    def cleanup_worktree_path(cls, base_workspace: Path, worktree_path: Path, branch_name: str | None = None) -> None:
        """Clean up worktree directory and branch."""
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(base_workspace),
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

        if worktree_path.exists():
            try:
                _rmtree(worktree_path, ignore_errors=True)
            except Exception:
                pass

        if branch_name:
            try:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    cwd=str(base_workspace),
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass

    @classmethod
    def cleanup(cls, info: WorktreeInfo) -> None:
        """Clean up an active WorktreeInfo."""
        cls.cleanup_worktree_path(info.base_workspace, info.worktree_path, info.branch_name)


def _worktree_hooks(workspace: Path) -> Any:
    try:
        from axon.hooks import HookManager
        return HookManager(None, workspace)
    except Exception:
        return None


def hook_create_worktree(workspace: Path, name: str) -> WorktreeInfo | None | bool:
    """Create a worktree through a ``WorktreeCreate`` hook, which replaces the git behavior.

    The hook prints the worktree path as the last non-empty line of stdout (an HTTP hook returns
    ``hookSpecificOutput.worktreePath``). Returns ``False`` when no hook is configured, ``None`` when
    the hook failed or gave no usable path, else the :class:`WorktreeInfo`."""
    import re
    hm = _worktree_hooks(workspace)
    if hm is None or not hm.has("WorktreeCreate"):
        return False
    res = hm.run("WorktreeCreate", {"name": name})
    if res.blocked or res.warnings:
        logger.debug("WorktreeCreate hook failed: %s %s", res.message, res.warnings)
        return None
    raw = res.worktree_path
    if not raw:
        for out in res.outputs:
            lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", ln).strip() for ln in out.splitlines()]
            lines = [ln for ln in lines if ln]
            if lines:
                raw = lines[-1]
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        if any(part in (".", "..") for part in raw.replace("\\", "/").split("/")[1:]):
            return None                     # an absolute path with `.` or `..` segments is refused
    else:
        path = Path(os.path.normpath(workspace / path))
    if not path.is_dir():
        return None
    return WorktreeInfo(task_id=name, worktree_path=path, branch_name="", base_workspace=workspace,
                        created=True, by_hook=True)


def hook_remove_worktree(info: WorktreeInfo) -> bool:
    """Run ``WorktreeRemove`` for a worktree a hook created. True when it counts as removed (hook
    exited 0, or the directory is gone); without such a hook the path is removed like a git worktree."""
    hm = _worktree_hooks(info.base_workspace)
    if hm is None or not hm.has("WorktreeRemove"):
        WorktreeManager.cleanup_worktree_path(info.base_workspace, info.worktree_path, None)
        return not info.worktree_path.exists()
    res = hm.run("WorktreeRemove", {"worktree_path": str(info.worktree_path)})
    return not info.worktree_path.exists() or not (res.blocked or res.warnings)


def create_session_worktree(workspace: Path, name: str | None = None) -> WorktreeInfo | None:
    """Worktree for the main session (``axon --worktree [name]``): reused when it already exists,
    otherwise created on branch ``axon/<name>`` from HEAD under ~/.axon/projects/<p>/worktrees/."""
    import re
    import time as _time
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-.") or _time.strftime("wt-%Y%m%d-%H%M%S")
    hooked = hook_create_worktree(workspace, clean)      # a WorktreeCreate hook replaces git
    if hooked is not False:
        return hooked or None
    if not WorktreeManager.is_git_repo(workspace):
        return None
    try:
        from axon import storage
        wt_dir = storage.project_dir(workspace) / "worktrees" / clean
    except Exception:
        return None
    branch = f"axon/{clean}"
    if (wt_dir / ".git").exists():
        return WorktreeInfo(task_id=clean, worktree_path=wt_dir, branch_name=branch, base_workspace=workspace, created=False)
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    attempts = (["git", "-c", "core.longpaths=true", "worktree", "add", "-b", branch, str(wt_dir), "HEAD"],
                ["git", "-c", "core.longpaths=true", "worktree", "add", str(wt_dir), branch])
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=20)
        except Exception as e:
            logger.debug(f"worktree add failed: {e}")
            return None
        if res.returncode == 0:
            return WorktreeInfo(task_id=clean, worktree_path=wt_dir, branch_name=branch, base_workspace=workspace, created=True)
    logger.debug(f"Failed to create session worktree: {res.stderr}")
    return None
