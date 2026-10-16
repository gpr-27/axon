"""Project-organised storage: project.json, index.json, generated READMEs, project_info, the legacy
session migration, /project and `axon projects`."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from axon import storage
from axon.session import activity, layout


@pytest.fixture(autouse=True)
def _unbind():
    activity.unbind()
    yield
    activity.unbind()


def _repo(tmp_path: Path, name: str = "shop") -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:me/shop.git\n',
                                          encoding="utf-8")
    return repo


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_project_json_and_info(tmp_path: Path, isolate_user_home: Path) -> None:
    repo = _repo(tmp_path)
    sub = repo / "src"
    sub.mkdir()
    meta = layout.touch_project(sub)
    assert meta["name"] == "shop" and meta["workspace"] == str(repo.resolve())
    assert meta["git_branch"] == "main" and meta["git_remote"] == "git@github.com:me/shop.git"
    first = meta["last_opened"]
    created = meta["created"]
    (storage.sessions_dir(repo) / "s1.jsonl").write_text("{}\n", encoding="utf-8")
    (storage.sessions_dir(repo) / "s1_sub_1.jsonl").write_text("{}\n", encoding="utf-8")
    (storage.memory_dir(repo) / "MEMORY.md").write_text("x", encoding="utf-8")
    activity.record("prompt", {"text": "hi"}, workspace=repo)
    meta2 = layout.touch_project(repo)
    assert meta2["created"] == created and meta2["sessions"] == 1 and meta2["last_opened"] >= first
    info = layout.project_info(sub, detailed=True)
    assert info["name"] == "shop" and info["sessions"] == 1 and info["memory_files"] == 1
    assert info["data_dir"] == str(isolate_user_home / ".axon" / "projects" / storage.project_key(repo))
    assert info["data_dir_display"].startswith("~/.axon/projects/")
    assert info["journal_entries"] == 1 and info["size_bytes"] > 0 and info["last_opened"]


def test_index_lists_every_project(tmp_path: Path, isolate_user_home: Path) -> None:
    a, b = _repo(tmp_path, "alpha"), _repo(tmp_path, "beta")
    layout.touch_project(a)
    layout.touch_project(b)
    (storage.sessions_dir(b) / "x.jsonl").write_text("{}\n", encoding="utf-8")
    data = layout.update_index(b)
    names = {p["name"] for p in data["projects"].values()}
    assert {"alpha", "beta"} <= names
    beta = data["projects"][storage.project_key(b)]
    assert beta["sessions"] == 1 and beta["size_bytes"] > 0 and beta["last_opened"]
    on_disk = json.loads(layout.index_path().read_text(encoding="utf-8"))
    assert on_disk == data


def test_readmes_are_pure_and_written_only_on_change(tmp_path: Path, isolate_user_home: Path) -> None:
    home_text = layout.render_home_readme()
    assert home_text == layout.render_home_readme()
    for needle in ("activity.jsonl", "screen/", "trash/", "outputs/", "cleanup_period_days", "axon purge",
                   "/restore", "projects/index.json", "Global", "Per project"):
        assert needle in home_text, needle
    info = {"name": "shop", "key": "k", "workspace": "/w/shop", "sessions": 3, "memory_files": 1,
            "screen_logs": 2, "trash": 0, "git_remote": "r", "git_branch": "main"}
    proj = layout.render_project_readme(info)
    assert proj.startswith("# shop") and "Sessions: 3" in proj and "/w/shop" in proj
    assert layout.render_project_readme(info) == proj
    repo = _repo(tmp_path)
    written = layout.write_readmes(repo)
    assert storage.axon_home() / "README.md" in written and len(written) == 2
    assert layout.write_readmes(repo) == []            # unchanged -> not rewritten
    (storage.sessions_dir(repo) / "s.jsonl").write_text("{}\n", encoding="utf-8")
    assert layout.write_readmes(repo) == [storage.project_dir(repo) / "README.md"]


def _legacy(home: Path, name: str, workspace: str | None, extra: str = "") -> Path:
    d = home / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    if workspace:
        lines.append(json.dumps({"type": "session_meta", "timestamp": 1.0, "data": {"workspace": workspace}}))
    lines.append(json.dumps({"type": "user_message", "timestamp": 2.0, "data": {"role": "user", "content": "hi " + extra}}))
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_migration_copies_verifies_and_backs_up(tmp_path: Path, isolate_user_home: Path) -> None:
    home = storage.axon_home()
    repo = _repo(tmp_path)
    known = _legacy(home, "session_a.jsonl", str(repo))
    sub = _legacy(home, "session_a_sub_1.jsonl", None, "sub")
    trace = home / "sessions" / "session_a.traces.jsonl"
    trace.write_text('{"turn": 1}\n', encoding="utf-8")
    orphan = _legacy(home, "session_b.jsonl", None, "who")
    via_history = _legacy(home, "session_c.jsonl", None, "hist")
    storage.append_jsonl(storage.user_file("history.jsonl"), {"session": "session_c", "project": str(repo), "display": "x"})
    digests = {p.name: _sha(p) for p in (known, sub, trace, orphan, via_history)}
    (home / "memory").mkdir()
    (home / "memory" / "prefer-concise.md").write_text("x", encoding="utf-8")

    rep = layout.migrate_legacy_sessions()
    assert rep["ran"] and not rep["errors"] and len(rep["moved"]) == 5
    pdir = storage.project_dir(repo)
    assert _sha(pdir / "sessions" / "session_a.jsonl") == digests["session_a.jsonl"]
    assert _sha(pdir / "sessions" / "session_a_sub_1.jsonl") == digests["session_a_sub_1.jsonl"]
    assert _sha(pdir / "traces" / "session_a.jsonl") == digests["session_a.traces.jsonl"]
    assert _sha(pdir / "sessions" / "session_c.jsonl") == digests["session_c.jsonl"]
    unsorted = home / "projects" / layout.UNSORTED / "sessions" / "session_b.jsonl"
    assert _sha(unsorted) == digests["session_b.jsonl"]
    # originals are kept in the backup, never deleted
    backup = Path(rep["backup"])
    assert {p.name for p in backup.iterdir()} == set(digests)
    assert all(_sha(backup / n) == d for n, d in digests.items())
    assert not list((home / "sessions").glob("*.jsonl"))
    report = Path(rep["report"]).read_text(encoding="utf-8")
    assert "session_a.jsonl" in report and "prefer-concise.md" in report
    assert (home / "migrations" / layout.MIGRATION_MARKER).exists()
    mig = activity.read(repo, kinds=["migration"])
    assert {Path(e["data"]["from"]).name for e in mig} == {"session_a.jsonl", "session_a_sub_1.jsonl",
                                                          "session_a.traces.jsonl", "session_c.jsonl"}
    # the session is listed and resumable from its project
    from axon.session.store import SessionStore
    store = SessionStore(repo)
    assert "session_a" in [m.session_id for m in store.list_recent()]
    assert store.read_conversation("session_a").messages[0]["content"].startswith("hi")

    # idempotent: guarded by the marker, and a forced re-run finds nothing to move
    assert layout.migrate_legacy_sessions() == {"ran": False, "moved": [], "errors": [], "report": "", "backup": ""}
    assert layout.migrate_legacy_sessions(force=True)["moved"] == []


def test_migration_conflict_keeps_both(tmp_path: Path, isolate_user_home: Path) -> None:
    home = storage.axon_home()
    repo = _repo(tmp_path)
    existing = storage.sessions_dir(repo) / "session_x.jsonl"
    existing.write_text('{"type": "user_message", "data": {"content": "new"}}\n', encoding="utf-8")
    legacy = _legacy(home, "session_x.jsonl", str(repo), "old")
    old_digest = _sha(legacy)
    rep = layout.migrate_legacy_sessions()
    assert not rep["errors"]
    assert "new" in existing.read_text(encoding="utf-8")
    assert _sha(storage.sessions_dir(repo) / "session_x_legacy.jsonl") == old_digest


def test_startup_runs_everything_and_never_raises(tmp_path: Path, isolate_user_home: Path) -> None:
    repo = _repo(tmp_path)
    out = layout.startup(repo)
    assert "errors" not in out
    assert (storage.axon_home() / "README.md").is_file()
    assert (storage.project_dir(repo) / "README.md").is_file()
    assert layout.index_path().is_file()
    assert json.loads((storage.project_dir(repo) / "project.json").read_text(encoding="utf-8"))["name"] == "shop"


def test_project_command_and_projects_cli(tmp_path: Path, isolate_user_home: Path, capsys) -> None:
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent
    from axon.commands.builtin import dispatch_command
    from axon.config import Settings
    from axon.features.journal import projects_main
    from axon.permissions.engine import PermissionEngine
    from axon.session.ledger import Ledger
    from axon.session.store import SessionStore
    from axon.tools import create_default_registry
    from fakes import FakeProvider
    repo = _repo(tmp_path)
    layout.startup(repo)
    s = Settings(workspace=repo, mode="default")
    agent = Agent(provider=FakeProvider([]), tools=create_default_registry(), permissions=PermissionEngine(s),
                  context=ContextManager(s), session=SessionStore(repo), ledger=Ledger(), settings=s)
    capsys.readouterr()
    dispatch_command("/project", agent)
    out = capsys.readouterr().out
    assert "shop" in out and storage.project_key(repo) in out and "Journal" in out and "Memory" in out
    dispatch_command("/where", agent)
    assert "Current project" in capsys.readouterr().out
    assert projects_main([]) == 0
    out = capsys.readouterr().out
    assert "shop" in out and str(repo.resolve()) in out
    assert projects_main(["--json"]) == 0
    assert storage.project_key(repo) in json.loads(capsys.readouterr().out)["projects"]


def test_cli_dispatches_new_subcommands(tmp_path: Path, isolate_user_home: Path, monkeypatch, capsys) -> None:
    from axon.cli import main
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    activity.record("prompt", {"text": "from cli"}, workspace=repo)
    assert main(["activity", "--kind", "prompt"]) == 0
    assert "from cli" in capsys.readouterr().out
    assert main(["projects"]) == 0
    assert "shop" in capsys.readouterr().out


def test_project_specific_files_live_in_the_project(tmp_path: Path, isolate_user_home: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    from axon.agent.images import get_images_cache_dir
    pdir = storage.project_dir(repo)
    assert get_images_cache_dir() == pdir / "images"
    from axon.ui.input import _save_paste
    _save_paste("a large paste")
    assert list((pdir / "pastes").glob("*.txt"))
