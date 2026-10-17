"""Tests for axon.features.gitops using throwaway git repositories and a fake provider."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.features import gitops as go

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


class FakeTurn:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = SimpleNamespace(input=10, output=5, cache_read=0, cache_write=0)
        self.model = "fake-model"


class FakeProvider:
    name = "fake"

    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text, self.fail, self.calls = text, fail, []

    def stream(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("boom")
        yield from ()

    def finalize(self):
        return FakeTurn(self.text)


class FakeLedger:
    def __init__(self) -> None:
        self.records = []

    def record(self, model, usage, source="main"):
        self.records.append((model, source))


def _g(ws: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    _g(ws, "init", "-q", "-b", "main")
    _g(ws, "config", "user.email", "t@example.com")
    _g(ws, "config", "user.name", "Tester")
    _g(ws, "config", "commit.gpgsign", "false")
    (ws / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _g(ws, "add", "app.py")
    _g(ws, "commit", "-qm", "init")
    return ws


def _agent(ws: Path, provider=None, **kw):
    return SimpleNamespace(settings=SimpleNamespace(workspace=ws, model="fake-model"), provider=provider,
                           ledger=FakeLedger(), **kw)


def test_status_and_numstat(repo):
    (repo / "app.py").write_text("print('hi')\nprint('x')\n", encoding="utf-8")
    (repo / "new.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "staged.py").write_text("b = 1\nc = 2\n", encoding="utf-8")
    _g(repo, "add", "staged.py")
    st = go.repo_status(repo)
    assert st.branch == "main"
    assert st.staged == ["staged.py"] and st.unstaged == ["app.py"] and st.untracked == ["new.py"]
    staged = go.staged_changes(repo)
    assert [(c.path, c.added, c.status) for c in staged] == [("staged.py", 2, "A")]
    assert [(c.path, c.added, c.deleted) for c in go.numstat(repo)] == [("app.py", 1, 0)]


def test_commit_with_llm_message(repo, monkeypatch, capsys):
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _g(repo, "add", "app.py")
    prov = FakeProvider("```\nfix(app): correct greeting text\n\n- say hello ünïcode\n```")
    agent = _agent(repo, prov)
    monkeypatch.setattr(go, "confirm", lambda q, default=False: True)
    go.handle_commit(agent, "greeting")
    out = capsys.readouterr().out
    assert "Committed" in out
    msg = _g(repo, "log", "-1", "--format=%B")
    assert msg.startswith("fix(app): correct greeting text") and "ünïcode" in msg
    assert "app.py" in prov.calls[0]["messages"][0]["content"]
    assert agent.ledger.records == [("fake-model", "git_commit")]


def test_commit_heuristic_fallback_and_decline(repo, monkeypatch, capsys):
    (repo / "README.md").write_text("# docs\n", encoding="utf-8")
    _g(repo, "add", "README.md")
    answers = iter([False])
    monkeypatch.setattr(go, "confirm", lambda q, default=False: next(answers))
    go.handle_commit(_agent(repo, FakeProvider(fail=True)), "")
    out = capsys.readouterr().out
    assert "heuristic" in out and "docs" in out and "Aborted" in out
    assert _g(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_commit_stages_tracked_when_confirmed(repo, monkeypatch, capsys):
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.setattr(go, "_is_tty", lambda: True)
    monkeypatch.setattr(go, "confirm", lambda q, default=False: True)
    go.handle_commit(_agent(repo, None), "")
    assert "Committed" in capsys.readouterr().out
    assert _g(repo, "status", "--porcelain").strip() == ""


def test_commit_non_tty_aborts_without_staging(repo, monkeypatch, capsys):
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    monkeypatch.setattr(go, "_is_tty", lambda: False)
    go.handle_commit(_agent(repo, None), "")
    assert "Aborted" in capsys.readouterr().out
    assert go.repo_status(repo).staged == []


def test_confirm_non_tty_is_false(monkeypatch):
    monkeypatch.setattr(go, "_is_tty", lambda: False)
    assert go.confirm("really?") is False


def test_heuristic_messages():
    C = go.FileChange
    assert go.heuristic_commit_message([C("tests/test_a.py", 3, 0, "M")]).startswith("test(")
    m = go.heuristic_commit_message([C("src/auth/a.py", 1, 0, "A"), C("src/auth/b.py", 1, 0, "A")])
    assert m.startswith("feat(auth): add a.py, b.py")
    assert go.heuristic_commit_message([C("x.py", 1, 1)], "Fix crash on start").startswith("fix(x): fix crash")
    assert go._CC_RE.match(go.heuristic_commit_message([C("x.py", 1, 1)]))


def test_pr_prints_without_gh(repo, monkeypatch, capsys):
    _g(repo, "checkout", "-qb", "feature/login")
    (repo / "login.py").write_text("def login():\n    pass\n", encoding="utf-8")
    _g(repo, "add", "login.py")
    _g(repo, "commit", "-qm", "feat: add login")
    real_which = shutil.which
    monkeypatch.setattr(go.shutil, "which", lambda n: None if n == "gh" else real_which(n))
    go.handle_pr(_agent(repo, FakeProvider(fail=True)), "main")
    out = capsys.readouterr().out
    assert "feat: add login" in out and "## Summary" in out and "gh) not found" in out
    go.handle_pr(_agent(repo, None), "nonexistent-base")
    assert "not found" in capsys.readouterr().out


def test_git_dashboard(repo, capsys):
    (repo / "u.txt").write_text("x\n", encoding="utf-8")
    go.handle_git(_agent(repo), "")
    out = capsys.readouterr().out
    assert "main" in out and "untracked" in out and "init" in out and "stashes" in out


def test_changes_from_checkpoints_and_git(repo, capsys):
    (repo / "app.py").write_text("print('hi')\nline2\nline3\n", encoding="utf-8")
    cp = SimpleNamespace(history=[SimpleNamespace(files={(repo / "app.py"): b"print('hi')\n"})],
                         _pending_files={(repo / "brand_new.py"): None})
    (repo / "brand_new.py").write_text("a\nb\n", encoding="utf-8")
    go.handle_changes(_agent(repo, checkpoints=cp), "")
    out = capsys.readouterr().out
    assert "edited this session" in out and "app.py" in out and "+2" in out and "brand_new.py" in out
    go.handle_changes(_agent(repo), "")
    out = capsys.readouterr().out
    assert "vs HEAD" in out and "app.py" in out and "brand_new.py" in out


def test_not_a_repo(tmp_path, capsys):
    go.handle_git(_agent(tmp_path), "")
    assert "Not inside a git repository" in capsys.readouterr().out
