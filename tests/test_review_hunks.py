"""Per-hunk review of the files Axon changed this session (data functions + the /review hunks command)."""
from __future__ import annotations

from pathlib import Path

from axon.features.review import accept_hunk, list_hunks, reject_hunk
from axon.session.checkpoint import CheckpointManager

BASE = "".join(f"line {i}\n" for i in range(1, 31))


def _edited(tmp_path: Path) -> tuple[CheckpointManager, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.txt"
    f.write_text(BASE, encoding="utf-8")
    mgr = CheckpointManager(ws)
    mgr.capture_before_edit(f)
    text = BASE.replace("line 3\n", "line three\n").replace("line 25\n", "line 25\nadded after 25\n")
    f.write_text(text, encoding="utf-8")
    mgr.commit_turn_snapshot(label="edit")
    return mgr, f


def test_list_hunks_splits_a_file_into_hunks(tmp_path):
    mgr, f = _edited(tmp_path)
    hunks = list_hunks(mgr)
    assert [h.rel for h in hunks] == ["a.txt", "a.txt"]
    assert hunks[0].removed == ["line 3"] and hunks[0].added == ["line three"]
    assert hunks[1].removed == [] and hunks[1].added == ["added after 25"]
    assert len({h.id for h in hunks}) == 2 and all(not h.accepted for h in hunks)


def test_reject_reverts_only_that_hunk(tmp_path):
    mgr, f = _edited(tmp_path)
    first = list_hunks(mgr)[0]
    res = reject_hunk(mgr, first.id)
    assert res.ok, res.message
    text = f.read_text(encoding="utf-8")
    assert "line 3\n" in text and "line three" not in text and "added after 25" in text
    assert len(list_hunks(mgr)) == 1


def test_reject_refuses_when_the_file_changed_since_listing(tmp_path):
    mgr, f = _edited(tmp_path)
    hunks = list_hunks(mgr)
    f.write_text(f.read_text(encoding="utf-8") + "user typed this\n", encoding="utf-8")
    res = reject_hunk(mgr, hunks[0].id)
    assert not res.ok and "changed since" in res.message
    assert "line three" in f.read_text(encoding="utf-8")


def test_reject_a_created_file_deletes_it_and_restores_a_deleted_one(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = CheckpointManager(ws)
    new, old = ws / "new.txt", ws / "old.txt"
    old.write_text("keep me\n", encoding="utf-8")
    mgr.capture_before_edit(new)
    mgr.capture_before_edit(old)
    new.write_text("fresh\n", encoding="utf-8")
    old.unlink()
    mgr.commit_turn_snapshot()
    kinds = {h.rel: h.kind for h in list_hunks(mgr)}
    assert kinds == {"new.txt": "created", "old.txt": "deleted"}
    for h in list_hunks(mgr):
        assert reject_hunk(mgr, h.id).ok
    assert not new.exists() and old.read_text(encoding="utf-8") == "keep me\n"


def test_accept_hides_the_hunk_without_touching_the_file(tmp_path):
    mgr, f = _edited(tmp_path)
    before = f.read_text(encoding="utf-8")
    h = list_hunks(mgr)[0]
    accept_hunk(mgr, h.id)
    assert f.read_text(encoding="utf-8") == before
    shown = list_hunks(mgr)
    assert [x.accepted for x in shown] == [True, False]
    assert len(list_hunks(mgr, include_accepted=False)) == 1


def test_binary_files_are_one_whole_file_hunk(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = CheckpointManager(ws)
    b = ws / "x.bin"
    b.write_bytes(b"\xff\xfe\x00old")
    mgr.capture_before_edit(b)
    b.write_bytes(b"\xff\xfe\x00new")
    mgr.commit_turn_snapshot()
    (h,) = list_hunks(mgr)
    assert h.kind == "binary"
    assert reject_hunk(mgr, h.id).ok and b.read_bytes() == b"\xff\xfe\x00old"


def test_unknown_id_is_a_clear_message(tmp_path):
    mgr, _ = _edited(tmp_path)
    res = reject_hunk(mgr, "zzzzzz.9")
    assert not res.ok and "no hunk" in res.message.lower()
