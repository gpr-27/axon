"""One folder per chat session: layout helpers, SessionStore, and the flat-layout migration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon import storage
from axon.session import layout, relayout
from axon.session.store import SessionStore, iter_records


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    (d / ".git").mkdir(parents=True)
    return d


def _rec(text: str) -> str:
    return json.dumps({"type": "user_message", "timestamp": 1.0, "data": {"role": "user", "content": text}}) + "\n"


def _flat_session(ws: Path, sid: str) -> dict[str, Path]:
    """Write a session the way versions before the per-session folders did."""
    pdir = storage.project_dir(ws)
    files = {
        "main": pdir / "sessions" / f"{sid}.jsonl",
        "sub": pdir / "sessions" / f"{sid}_sub_2.jsonl",
        "subtrace": pdir / "sessions" / f"{sid}_sub_2.traces.jsonl",
        "trace": pdir / "traces" / f"{sid}.jsonl",
        "subtrace2": pdir / "traces" / f"{sid}_sub_2.jsonl",
        "todos": pdir / "todos" / f"{sid}.json",
        "out": pdir / "outputs" / sid / "bash-1.txt",
        "bg": pdir / "outputs" / sid / "background-1.log",
        "cp": pdir / "checkpoints" / sid / "1" / "manifest.json",
        "export": pdir / "exports" / f"{sid}.md",
        "debug": pdir / "debug" / f"{sid}.log",
        "screen": pdir / "screen" / f"20240101-101010-{sid}.log",
        "screen2": pdir / "screen" / f"20240102-101010-{sid}.log",
    }
    for k, p in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_rec(k) if k in ("main", "sub") else f"{k}\n", encoding="utf-8")
    return files


EXPECTED = {
    "main": "session.jsonl", "sub": "subagents/2.jsonl", "subtrace": "subagents/2.trace.jsonl",
    "trace": "trace.jsonl", "subtrace2": "subagents/2.trace.jsonl", "todos": "todos.json",
    "out": "outputs/bash-1.txt", "bg": "outputs/background-1.log", "cp": "checkpoints/1/manifest.json",
    "export": "exports/{sid}.md", "debug": "debug.log", "screen": "screen.log",
}


# ─── helpers ───────────────────────────────────────────────────────────────

def test_helpers_map_everything_into_the_session_folder(ws: Path) -> None:
    base = storage.project_dir(ws) / "sessions"
    assert storage.session_dir(ws, "s1") == base / "s1"
    assert storage.session_dir(ws, "s1_sub_3") == base / "s1"  # a subagent lives in its parent's folder
    assert storage.transcript_path(base, "s1") == base / "s1" / "session.jsonl"
    assert storage.transcript_path(base, "s1_sub_3") == base / "s1" / "subagents" / "3.jsonl"
    assert storage.trace_path(ws, "s1") == base / "s1" / "trace.jsonl"
    assert storage.trace_path(ws, "s1_sub_3") == base / "s1" / "subagents" / "3.trace.jsonl"
    assert storage.todos_path(ws, "s1_sub_3") == base / "s1" / "subagents" / "3.todos.json"
    assert storage.checkpoints_dir(ws, "s1") == base / "s1" / "checkpoints"
    assert storage.outputs_dir(ws, "s1") == base / "s1" / "outputs"
    assert storage.session_subdir(ws, "s1", "exports").is_dir()
    assert storage.split_subagent_id("a_sub_1_sub_12") == ("a_sub_1", 12)
    assert storage.split_subagent_id("plain") == ("plain", None)
    top = {p.name for p in storage.project_dir(ws).iterdir()}
    assert top <= {"project.json", "sessions"}


def test_session_less_artifacts_attach_to_the_active_session(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ws)
    pdir = storage.project_dir(ws)
    assert storage.active_session_subdir("images") == pdir / "sessions" / "_unattached" / "images"  # fallback
    store = SessionStore(ws)
    store.open("sess_a")
    assert storage.active_session_subdir("images") == pdir / "sessions" / "sess_a" / "images"
    assert storage.outputs_dir(ws) == pdir / "sessions" / "sess_a" / "outputs"
    store.open("sess_b")
    assert storage.active_session_subdir("pastes") == pdir / "sessions" / "sess_b" / "pastes"


# ─── SessionStore ──────────────────────────────────────────────────────────

def test_store_writes_one_folder_per_session(ws: Path) -> None:
    store = SessionStore(ws)
    sid = store.active_session_id
    store.append_user("hello")
    folder = store.session_dir / sid
    assert store.active_file == folder / "session.jsonl" and store.active_file.is_file()
    sub = SessionStore(ws, session_dir=store.session_dir)
    sub.open(f"{sid}_sub_1")
    sub.append_user("sub task")
    assert sub.active_file == folder / "subagents" / "1.jsonl" and sub.active_file.is_file()
    assert not list(store.session_dir.glob("*.jsonl"))  # no flat files next to the folders
    # sub transcripts do not show up as sessions; the main one is listed once
    assert [m.session_id for m in store.list_recent()] == [sid]
    assert [p.name for p in store._subagent_files(sid)] == ["1.jsonl"]
    assert store.latest() == sid
    assert store.read_conversation(sid).messages[0]["content"] == "hello"


def test_delete_and_restore_move_the_whole_folder(ws: Path) -> None:
    store = SessionStore(ws)
    sid = store.active_session_id
    store.append_user("keep me")
    for rel in ("outputs/x.txt", "pastes/p.txt", "images/i.png", "screen.log", "subagents/1.jsonl"):
        p = store.session_dir / sid / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("data", encoding="utf-8")
    assert store.delete_session(sid)
    assert not (store.session_dir / sid).exists()
    trash = store.list_trash()
    assert trash[0]["id"] == sid and (trash[0]["path"] / "manifest.json").is_file()
    assert store.restore_session(sid) == sid
    for rel in ("session.jsonl", "outputs/x.txt", "pastes/p.txt", "images/i.png", "screen.log", "subagents/1.jsonl"):
        assert (store.session_dir / sid / rel).is_file(), rel
    assert sid in [m.session_id for m in store.list_recent()]


def test_restore_merges_into_a_recreated_session_folder(ws: Path) -> None:
    store = SessionStore(ws)
    sid = store.active_session_id
    store.append_user("original")
    (store.session_dir / sid / "outputs").mkdir()
    (store.session_dir / sid / "outputs" / "a.txt").write_text("a", encoding="utf-8")
    assert store.delete_session(sid)
    (store.session_dir / sid / "outputs").mkdir(parents=True)  # a straggler recreated the folder
    (store.session_dir / sid / "outputs" / "b.txt").write_text("b", encoding="utf-8")
    assert store.restore_session(sid) == sid
    assert (store.session_dir / sid / "session.jsonl").is_file()
    assert {p.name for p in (store.session_dir / sid / "outputs").iterdir()} == {"a.txt", "b.txt"}
    assert store.list_trash() == []


def test_prune_removes_old_session_folders_but_not_unattached(ws: Path) -> None:
    import os
    import time
    old = storage.session_dir(ws, "ancient")
    (old / "session.jsonl").write_text(_rec("x"), encoding="utf-8")
    (old / "outputs").mkdir()
    (old / "outputs" / "o.txt").write_text("o", encoding="utf-8")
    fallback = storage.session_dir(ws, "")
    then = time.time() - 90 * 86400
    for p in (*old.rglob("*"), old, *fallback.rglob("*"), fallback):
        os.utime(p, (then, then))
    assert storage.prune_old(30) == 1
    assert not old.exists() and fallback.exists()


# ─── migration of the flat layout ──────────────────────────────────────────

def test_opening_a_flat_session_moves_everything_into_its_folder(ws: Path) -> None:
    sid = "session_20240101_proj_abc123"
    files = _flat_session(ws, sid)
    other = _flat_session(ws, "session_other")
    store = SessionStore(ws)
    assert {m.session_id for m in store.list_recent()} == {sid, "session_other"}  # flat sessions are still listed
    store.open(sid)  # lazy migration of exactly this session
    folder = store.session_dir / sid
    assert store.active_file == folder / "session.jsonl"
    for key, rel in EXPECTED.items():
        assert (folder / rel.format(sid=sid)).exists(), key
    # moved, not copied: no original remains
    for key, p in files.items():
        assert not p.exists(), key
    assert not (storage.project_dir(ws) / "outputs" / sid).exists()
    # the second sub trace did not overwrite the first: it was parked, not lost
    assert (folder / "subagents" / "2.trace.jsonl").read_text(encoding="utf-8") == "subtrace\n"
    assert {p.read_text(encoding="utf-8") for p in (folder / "legacy").iterdir()} == {"subtrace2\n"}
    # both screen logs of the resumed run were concatenated into one file
    screen = (folder / "screen.log").read_text(encoding="utf-8")
    assert "screen\n" in screen and "screen2\n" in screen and "continued" in screen
    # the other session is untouched until it is opened
    assert other["main"].is_file()
    # transcript content is intact and readable through the store
    assert store.read_conversation(sid).messages[0]["content"] == "main"
    assert [p.name for p in store._subagent_files(sid)] == ["2.jsonl"]


def test_migration_is_idempotent_and_loses_nothing(ws: Path) -> None:
    sid = "session_x"
    _flat_session(ws, sid)
    pdir = storage.project_dir(ws)
    before = sorted(p.read_text(encoding="utf-8") for p in pdir.rglob("*") if p.is_file())
    assert relayout.migrate_project(pdir) > 0
    snapshot = sorted(str(p.relative_to(pdir)) for p in pdir.rglob("*"))
    assert relayout.migrate_project(pdir) == 0  # nothing left to move
    assert sorted(str(p.relative_to(pdir)) for p in pdir.rglob("*")) == snapshot
    after = sorted(p.read_text(encoding="utf-8") for p in pdir.rglob("*") if p.is_file())
    # every byte is still there (screen logs are concatenated, so compare content tokens)
    assert "".join(after).count("main") == "".join(before).count("main")
    for legacy in ("traces", "outputs", "checkpoints", "todos", "screen", "exports", "debug"):
        assert not (pdir / legacy).exists(), legacy  # empty old folders are removed
    assert (pdir / "sessions" / sid / "session.jsonl").is_file()
    assert not list((pdir / "sessions").glob("*.jsonl"))


def test_migration_never_overwrites_and_drops_only_identical_duplicates(ws: Path) -> None:
    sid = "session_c"
    pdir = storage.project_dir(ws)
    folder = pdir / "sessions" / sid
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(_rec("new"), encoding="utf-8")
    (folder / "todos.json").write_text("same", encoding="utf-8")
    (pdir / "sessions" / f"{sid}.jsonl").write_text(_rec("old"), encoding="utf-8")  # different content
    (pdir / "todos").mkdir()
    (pdir / "todos" / f"{sid}.json").write_text("same", encoding="utf-8")  # identical content
    relayout.migrate_session(pdir / "sessions", sid, pdir)
    assert "new" in (folder / "session.jsonl").read_text(encoding="utf-8")
    parked = list((folder / "legacy").glob("*.jsonl"))
    assert len(parked) == 1 and "old" in parked[0].read_text(encoding="utf-8")
    assert not (pdir / "todos" / f"{sid}.json").exists() and (folder / "todos.json").read_text(encoding="utf-8") == "same"
    # a session that already is in the new layout is never listed twice
    assert [m.session_id for m in SessionStore(ws).list_recent()] == [sid]


def test_startup_migrates_the_project(ws: Path, isolate_user_home: Path) -> None:
    sid = "session_s"
    _flat_session(ws, sid)
    out = layout.startup(ws)
    assert "errors" not in out and out["relayout"] > 0
    pdir = storage.project_dir(ws)
    assert (pdir / "sessions" / sid / "session.jsonl").is_file() and (pdir / "sessions" / sid / "trace.jsonl").is_file()
    assert layout.session_count(pdir) == 1
    assert layout.startup(ws)["relayout"] == 0


def test_delete_works_on_a_session_that_is_still_flat(ws: Path) -> None:
    sid = "session_flat_del"
    files = _flat_session(ws, sid)
    store = SessionStore(ws)
    assert store.delete_session(sid)
    pdir = storage.project_dir(ws)
    assert not any(p.exists() for p in files.values())
    assert not (pdir / "sessions" / sid).exists()
    assert store.restore_session(sid) == sid
    assert (pdir / "sessions" / sid / "session.jsonl").is_file() and (pdir / "sessions" / sid / "outputs" / "bash-1.txt").is_file()


def test_old_transcripts_pointing_at_moved_output_files_still_resolve(ws: Path) -> None:
    sid = "session_o"
    pdir = storage.project_dir(ws)
    recorded = pdir / "outputs" / sid / "bash-1.txt"
    recorded.parent.mkdir(parents=True)
    recorded.write_text("full output", encoding="utf-8")
    relayout.migrate_project(pdir)
    assert not recorded.exists()
    assert storage.relocate_recorded_path(recorded) == pdir / "sessions" / sid / "outputs" / "bash-1.txt"
    assert storage.relocate_recorded_path(recorded).read_text(encoding="utf-8") == "full output"


def test_analytics_reads_the_new_trace_layout(ws: Path, isolate_user_home: Path) -> None:
    from axon.features import analytics
    storage.append_jsonl(storage.trace_path(ws, "sess_t"), {"turn": 1, "started_at": 1.0})
    storage.append_jsonl(storage.trace_path(ws, "sess_t_sub_2"), {"turn": 1, "started_at": 2.0})
    flat = storage.project_dir(ws) / "traces" / "sess_flat.jsonl"
    flat.parent.mkdir()
    flat.write_text(json.dumps({"turn": 1, "started_at": 3.0}) + "\n", encoding="utf-8")
    got = {r["_session"] for r in analytics.load_traces()}
    assert got == {"sess_t", "sess_t_sub_2", "sess_flat"}


def test_iter_records_reads_session_jsonl(ws: Path) -> None:
    store = SessionStore(ws)
    store.append_user("x")
    assert [r["type"] for r in iter_records(store.active_file)] == ["session_meta", "user_message"]


def test_debug_log_moves_into_the_session_folder(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging
    from types import SimpleNamespace

    import axon.cli as cli
    monkeypatch.chdir(ws)
    boot = cli.setup_debug_logging("all", None, "")  # before the session is known
    try:
        assert boot is not None and boot.parent.parent.name == "_unattached"
        logging.getLogger("axon").debug("before-marker")
        store = SessionStore(ws)
        store.open("sess_dbg")
        dest = cli.rehome_debug_log(SimpleNamespace(settings=SimpleNamespace(workspace=ws), session=store))
        logging.getLogger("axon").debug("after-marker")
        assert dest == store.session_dir / "sess_dbg" / "debug.log" and not boot.exists()
        text = dest.read_text(encoding="utf-8")
        assert "before-marker" in text and "after-marker" in text
    finally:
        root = logging.getLogger("axon")
        for h in list(root.handlers):
            if isinstance(h, cli._DebugFileHandler):
                root.removeHandler(h)
                h.close()
