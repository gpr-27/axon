"""
Git Worktree Isolation for parallel subagents.
Creates lightweight, isolated worktrees for subagents to safely read and edit
code without colliding with the main working tree or other concurrent subagents.
"""
from __future__ import annotations
import logging
import os
import shutil
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
                timeout=3,
            )
            return head_res.returncode == 0
        except Exception:
            return False

    @classmethod
    def create_worktree(cls, workspace: Path, task_id: str) -> WorktreeInfo | None:
        """Create an ephemeral worktree for a subagent task."""
        if not cls.is_git_repo(workspace):
            return None

        clean_id = "".join(c for c in task_id if c.isalnum() or c in ("-", "_"))
        branch_name = f"axon-subagent-{clean_id}"
        worktree_dir = workspace / ".axon" / "worktrees" / clean_id

        # Clean existing if dirty
        if worktree_dir.exists():
            cls.cleanup_worktree_path(workspace, worktree_dir, branch_name)

        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Create worktree on a new branch from HEAD
            cmd = ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), "HEAD"]
            res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                # If branch already exists, try without -b
                if "already exists" in res.stderr:
                    cmd = ["git", "worktree", "add", "--force", str(worktree_dir), "HEAD"]
                    res = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=10)
                if res.returncode != 0:
                    logger.debug(f"Failed to create git worktree: {res.stderr}")
                    return None

            return WorktreeInfo(
                task_id=task_id,
                worktree_path=worktree_dir,
                branch_name=branch_name,
                base_workspace=workspace,
                created=True,
            )
        except Exception as e:
            logger.debug(f"Exception creating worktree: {e}")
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
                timeout=5,
            )
            return res.stdout if res.returncode == 0 else ""
        except Exception:
            return ""

    @classmethod
    def merge_back(cls, info: WorktreeInfo) -> tuple[bool, str]:
        """
        Merge changes made inside the worktree back into the main workspace.
        Applies changes cleanly using git diff / patch or cherry-pick.
        """
        if not info.created or not info.worktree_path.exists():
            return False, "Worktree does not exist"

        try:
            # Check for changes in worktree
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if not status_res.stdout.strip():
                return True, "No changes made in worktree"

            # Stage and commit in worktree branch
            subprocess.run(["git", "add", "-A"], cwd=str(info.worktree_path), capture_output=True, timeout=5)
            commit_res = subprocess.run(
                ["git", "commit", "-m", f"subagent({info.task_id}): automated modifications"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Generate patch from worktree
            patch_res = subprocess.run(
                ["git", "format-patch", "-1", "HEAD", "--stdout"],
                cwd=str(info.worktree_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if patch_res.returncode == 0 and patch_res.stdout.strip():
                # Apply patch to base workspace
                apply_res = subprocess.run(
                    ["git", "apply", "--3way", "--whitespace=nowarn"],
                    input=patch_res.stdout,
                    cwd=str(info.base_workspace),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if apply_res.returncode == 0:
                    return True, "Changes successfully applied to workspace"
                else:
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
                shutil.rmtree(worktree_path, ignore_errors=True)
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
