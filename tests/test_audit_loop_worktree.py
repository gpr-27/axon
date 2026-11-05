"""Worktree regressions: commits made by the subagent are merged back, failed creation leaves nothing behind."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axon.agent import worktree as worktree_module
from axon.agent.worktree import WorktreeManager

GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return res.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    try:
        git(path, "init", "-q")
        (path / "base.txt").write_text("base\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    return path


def new_worktree(repo: Path):
    info = WorktreeManager.create_worktree(repo, "sub-1")
    if info is None:
        pytest.skip("git worktree add is unavailable here")
    return info


def test_commits_made_inside_the_worktree_are_merged_back(repo: Path) -> None:
    info = new_worktree(repo)
    try:
        (info.worktree_path / "one.txt").write_text("committed by the subagent\n", encoding="utf-8")
        git(info.worktree_path, "add", "-A")
        git(info.worktree_path, "commit", "-q", "-m", "subagent commit")
        (info.worktree_path / "two.txt").write_text("left uncommitted\n", encoding="utf-8")

        ok, message = WorktreeManager.merge_back(info)

        assert ok, message
        assert (repo / "one.txt").read_text(encoding="utf-8") == "committed by the subagent\n"
        assert (repo / "two.txt").read_text(encoding="utf-8") == "left uncommitted\n"
    finally:
        WorktreeManager.cleanup(info)


def test_only_committed_changes_are_not_reported_as_no_changes(repo: Path) -> None:
    info = new_worktree(repo)
    try:
        (info.worktree_path / "base.txt").write_text("edited\n", encoding="utf-8")
        git(info.worktree_path, "commit", "-q", "-am", "edit")

        ok, message = WorktreeManager.merge_back(info)

        assert ok and "No changes" not in message
        assert (repo / "base.txt").read_text(encoding="utf-8") == "edited\n"
    finally:
        WorktreeManager.cleanup(info)


def test_untouched_worktree_has_no_changes(repo: Path) -> None:
    info = new_worktree(repo)
    try:
        assert WorktreeManager.merge_back(info) == (True, "No changes made in worktree")
    finally:
        WorktreeManager.cleanup(info)


def test_timed_out_worktree_add_leaves_no_directory_behind(repo: Path, monkeypatch) -> None:
    real_run = subprocess.run
    created: list[Path] = []

    def run(cmd, *args, **kwargs):
        if list(cmd[:3]) == ["git", "worktree", "add"]:
            target = Path(cmd[-2])
            target.mkdir(parents=True)
            created.append(target)
            raise subprocess.TimeoutExpired(cmd, 10)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_module.subprocess, "run", run)

    assert WorktreeManager.create_worktree(repo, "sub-1") is None
    assert created and not created[0].exists()
