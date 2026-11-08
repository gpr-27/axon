"""Plugin source pins: a sha that is not the branch tip installs; HEAD is verified."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axon.commands import marketplace as mk


def git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(cwd)}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text("one")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "one")
    first = git(r, "rev-parse", "HEAD")
    (r / "f.txt").write_text("two")
    git(r, "commit", "-qam", "two")
    return r, first, git(r, "rev-parse", "HEAD")


def test_pin_to_an_old_commit_installs_that_commit(repo, tmp_path):
    r, first, tip = repo
    dest = tmp_path / "out"
    mk.git_clone(r.as_uri(), dest, "main", first)
    assert (dest / "f.txt").read_text() == "one"
    assert git(dest, "rev-parse", "HEAD") == first


def test_pin_without_ref_and_abbreviated_sha(repo, tmp_path):
    r, first, tip = repo
    dest = tmp_path / "out"
    mk.git_clone(r.as_uri(), dest, "", first[:12])
    assert git(dest, "rev-parse", "HEAD") == first


def test_pin_that_does_not_exist_is_an_error_and_cleans_up(repo, tmp_path):
    r, *_ = repo
    dest = tmp_path / "out"
    with pytest.raises(mk.MarketplaceError):
        mk.git_clone(r.as_uri(), dest, "main", "f" * 40)
    assert not dest.exists()


def test_clone_without_pin_still_works(repo, tmp_path):
    r, first, tip = repo
    dest = tmp_path / "out"
    mk.git_clone(r.as_uri(), dest, "main")
    assert git(dest, "rev-parse", "HEAD") == tip


def test_clone_reports_the_commit(repo, tmp_path):
    r, first, tip = repo
    dest = tmp_path / "out"
    mk.git_clone(r.as_uri(), dest, "main")
    assert mk.head_sha(dest) == tip
