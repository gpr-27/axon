"""Storage tree, orphan flags, secret permissions, state files and legacy root-dir migration."""
import os
import stat
from pathlib import Path

import pytest

from axon import storage
from axon.session import layout


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "axhome"
    h.mkdir()
    monkeypatch.setenv("AXON_HOME", str(h))
    return h


def test_tree_flags_unknown_legacy_and_temp(home):
    (home / "settings.json").write_text("{}")
    (home / "stray.txt").write_text("x")
    (home / "images").mkdir()
    (home / "images" / "a.png").write_bytes(b"1234")
    p = storage.project_dir(Path("/tmp/proj-x"))
    (p / "sessions" / "s1").mkdir(parents=True)
    (p / "sessions" / "s1" / "session.jsonl").write_text("{}\n")
    (p / "sessions" / "s1" / "weird.bin").write_text("zz")
    tree = storage.storage_tree()
    status = {r["name"]: r["status"] for r in tree["entries"]}
    assert status["settings.json"] == "known"
    assert status["stray.txt"] == "unknown"
    assert status["images"] == "legacy"
    orphans = tree["projects"][0]["session_orphans"]
    assert [o["name"] for o in orphans] == ["weird.bin"]
    text = storage.render_storage_tree(tree)
    assert "UNKNOWN" in text and "stray.txt" in text and "weird.bin" in text
    assert any(r["name"] == "stray.txt" for r in storage.storage_summary())


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_secure_home_tightens_only(home):
    env = home / ".env"
    env.write_text("K=1")
    env.chmod(0o644)
    home.chmod(0o755)
    changed = storage.secure_home()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert str(env) in changed
    assert storage.secure_home() == []
    assert storage.storage_tree()["exposed_secrets"] == []
    env.chmod(0o644)
    assert storage.storage_tree()["exposed_secrets"] == [str(env)]


def test_state_roundtrip(home):
    assert storage.read_state("x", 5) == 5
    assert storage.write_state("x", {"a": [1]})
    assert storage.read_state("x") == {"a": [1]}
    assert (home / "state" / "x.json").is_file()


def test_legacy_root_dirs_migrated_non_destructively(home):
    (home / "images").mkdir()
    (home / "images" / "a.png").write_bytes(b"AAA")
    (home / "paste-cache").mkdir()
    (home / "paste-cache" / "p.txt").write_text("hello")
    rep = layout.migrate_legacy_root_dirs()
    assert rep["ran"] and len(rep["moved"]) == 2 and not rep["errors"]
    dest = home / "projects" / "_unsorted" / "sessions" / "_unattached"
    assert (dest / "images" / "a.png").read_bytes() == b"AAA"
    assert (dest / "paste-cache" / "p.txt").read_text() == "hello"
    for m in rep["moved"]:
        assert Path(m["backup"]).is_file()
    assert not (home / "images").exists()
    assert layout.migrate_legacy_root_dirs()["ran"] is False  # idempotent via marker


def test_legacy_root_name_clash_keeps_both(home):
    dest = home / "projects" / "_unsorted" / "sessions" / "_unattached" / "images"
    dest.mkdir(parents=True)
    (dest / "a.png").write_bytes(b"OLD")
    (home / "images").mkdir()
    (home / "images" / "a.png").write_bytes(b"NEW")
    layout.migrate_legacy_root_dirs()
    assert (dest / "a.png").read_bytes() == b"OLD"
    assert (dest / "a_legacy.png").read_bytes() == b"NEW"


def test_readme_documents_every_known_entry():
    text = layout.render_home_readme()
    for name in list(storage.HOME_ENTRIES) :
        if name == "README.md":
            continue
        assert name in text, name
    proj = layout.render_home_readme()
    for name in storage.PROJECT_ENTRIES:
        assert name in proj, name


def test_startup_creates_layout_and_tightens(home):
    ws = home.parent / "ws"
    ws.mkdir()
    layout.startup(ws)
    for d in ("logs", "cache", "state", "migrations"):
        assert (home / d).is_dir()
    assert (home / "README.md").is_file()
