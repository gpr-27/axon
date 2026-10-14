"""Tests for the central ~/.axon storage layout."""
from pathlib import Path

from axon import storage


def test_home_override_and_project_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "my proj"
    ws.mkdir()
    key = storage.project_key(ws)
    assert all(c.isalnum() or c == "-" for c in key)
    pd = storage.project_dir(ws)
    assert pd == tmp_path / "home" / "projects" / key
    assert (pd / "project.json").exists()
    assert storage.sessions_dir(ws).is_dir()
    assert storage.traces_dir(ws).parent == pd
    assert storage.checkpoints_dir(ws, "s1").name == "s1"
    # nothing written into the workspace itself
    assert list(ws.iterdir()) == []


def test_project_key_uses_git_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert storage.project_key(sub) == storage.project_key(repo)


def test_jsonl_roundtrip_skips_corrupt_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    storage.append_jsonl(p, {"a": 1, "text": "héllo ✓"})
    with open(p, "a", encoding="utf-8") as f:
        f.write("{broken\n\n")
    storage.append_jsonl(p, {"a": 2})
    rows = storage.read_jsonl(p)
    assert [r["a"] for r in rows] == [1, 2]
    assert rows[0]["text"] == "héllo ✓"
    assert storage.read_jsonl(p, limit=1) == [{"a": 2}]
    assert storage.read_jsonl(tmp_path / "missing.jsonl") == []


def test_usage_and_history_records(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "w"
    ws.mkdir()
    storage.record_usage(workspace=ws, session_id="s", model="m", source="main",
                         usage={"input": 10, "output": 5}, cost=0.0012)
    storage.record_history(ws, "hello", "s")
    storage.record_history(ws, "   ", "s")
    usage = storage.read_jsonl(storage.user_file("usage.jsonl"))
    hist = storage.read_jsonl(storage.user_file("history.jsonl"))
    assert usage[0]["cost"] == 0.0012 and usage[0]["source"] == "main"
    assert [h["display"] for h in hist] == ["hello"]
    names = {r["name"] for r in storage.storage_summary()}
    assert {"projects", "usage.jsonl"} <= names
