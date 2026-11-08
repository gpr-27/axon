"""Git tool: read-only subcommands, blocked flags, and permission decisions."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from axon.agent.state import FileState, TodoState
from axon.config import Settings
from axon.errors import ToolError
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry
from axon.tools.base import ToolContext

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


@pytest.fixture
def repo(workspace: Path, monkeypatch) -> Path:
    for k, v in (("GIT_AUTHOR_NAME", "T"), ("GIT_AUTHOR_EMAIL", "t@e.x"), ("GIT_COMMITTER_NAME", "T"),
                 ("GIT_COMMITTER_EMAIL", "t@e.x"), ("GIT_CONFIG_GLOBAL", "/dev/null"), ("GIT_CONFIG_SYSTEM", "/dev/null")):
        monkeypatch.setenv(k, v)
    def run(*a: str) -> None:
        subprocess.run(["git", *a], cwd=workspace, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (workspace / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-q", "-m", "first")
    return workspace


@pytest.fixture
def ctx(repo) -> ToolContext:
    return ToolContext(workspace=repo, file_state=FileState(), todos=TodoState(), settings=Settings(workspace=repo))


@pytest.fixture
def git():
    return create_default_registry().get("Git")


def test_status_diff_log_blame_show(git, ctx, repo):
    (repo / "a.txt").write_text("one\nTWO\n", encoding="utf-8")
    (repo / "new.txt").write_text("x", encoding="utf-8")
    st = git.run({"subcommand": "status"}, ctx)
    assert "main" in st and "a.txt" in st and "new.txt" in st
    assert "+TWO" in git.run({"subcommand": "diff"}, ctx)
    assert "first" in git.run({"subcommand": "log", "args": ["--oneline"]}, ctx)
    assert "one" in git.run({"subcommand": "blame", "args": ["a.txt"]}, ctx)
    assert "first" in git.run({"subcommand": "show", "args": ["--stat", "HEAD"]}, ctx)


def test_mutating_subcommand_works_and_asks(git, ctx, repo):
    (repo / "b.txt").write_text("b", encoding="utf-8")
    git.run({"subcommand": "add", "args": ["b.txt"]}, ctx)
    git.run({"subcommand": "commit", "args": ["-m", "second"]}, ctx)
    assert "second" in git.run({"subcommand": "log", "args": ["-n", "1", "--format=%s"]}, ctx)
    eng = PermissionEngine(Settings(workspace=repo, mode="default"))
    assert eng.check(git, {"subcommand": "status"}).outcome == "allow"
    assert eng.check(git, {"subcommand": "diff", "args": ["--stat"]}).outcome == "allow"
    for sub in ("commit", "push", "reset", "add"):
        assert eng.check(git, {"subcommand": sub, "args": []}).outcome == "ask"
    assert eng.check(git, {"subcommand": "branch", "args": ["newbranch"]}).outcome == "ask"


@pytest.mark.parametrize("flag", ["--output=/tmp/x", "--no-index", "--ext-diff", "--exec=sh", "-O/etc/passwd", "--git-dir=/x"])
def test_dangerous_flags_blocked(git, ctx, flag):
    with pytest.raises(ToolError, match="not allowed"):
        git.run({"subcommand": "diff", "args": [flag]}, ctx)


def test_unknown_subcommand_and_bad_args(git, ctx):
    with pytest.raises(ToolError, match="Unsupported"):
        git.run({"subcommand": "config", "args": ["user.name", "x"]}, ctx)
    with pytest.raises(ToolError, match="list of strings"):
        git.run({"subcommand": "status", "args": "-s"}, ctx)
    with pytest.raises(ToolError, match="newlines"):
        git.run({"subcommand": "log", "args": ["a\nb"]}, ctx)


def test_git_failure_is_tool_error(git, ctx):
    with pytest.raises(ToolError, match="failed"):
        git.run({"subcommand": "show", "args": ["nonexistent-rev"]}, ctx)


def test_output_flag_does_not_write_file(git, ctx, repo):
    with pytest.raises(ToolError):
        git.run({"subcommand": "log", "args": ["--output=pwned.txt"]}, ctx)
    assert not (repo / "pwned.txt").exists()


def test_folder_argument_runs_git_in_a_sub_repository(git, ctx, repo):
    sub = repo / "inner"
    sub.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "inner-main"], cwd=sub, check=True, capture_output=True)
    assert "inner-main" in git.run({"subcommand": "status", "path": "inner"}, ctx)


def test_a_dash_c_call_is_understood(git, ctx, repo):
    sub = repo / "inner"
    sub.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "inner-main"], cwd=sub, check=True, capture_output=True)
    assert "inner-main" in git.run({"subcommand": "-C", "args": ["inner", "status", "--short", "--branch"]}, ctx)
    assert "inner-main" in git.run({"subcommand": "status", "args": ["-C", "inner", "--short", "--branch"]}, ctx)


def test_folder_must_exist(git, ctx):
    with pytest.raises(ToolError, match="not a folder"):
        git.run({"subcommand": "status", "path": "missing"}, ctx)


def test_dash_c_cannot_reach_a_protected_folder(git, ctx, tmp_path, monkeypatch):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(Exception, match="protected|blocked"):
        git.run({"subcommand": "-C", "args": [str(home / ".ssh"), "status"]}, ctx)


def test_permission_engine_sees_the_folder_from_dash_c(git, repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    eng = PermissionEngine(Settings(workspace=repo, mode="default"))
    assert eng.check(git, {"subcommand": "status", "path": str(outside)}).outcome == "ask"
    assert eng.check(git, {"subcommand": "-C", "args": [str(outside), "status"]}).outcome == "ask"
    assert eng.check(git, {"subcommand": "status", "args": ["-C", str(outside)]}).outcome == "ask"
    assert eng.check(git, {"subcommand": "-C", "args": [str(repo), "status"]}).outcome == "allow"
