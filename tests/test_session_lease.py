"""One writer per session: a second live process is told, and a crashed one leaves nothing locked."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from axon.session import lease
from axon.session.store import SessionStore


def _other_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _held_by(folder: Path, pid: int, owner: str = "terminal", host: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / lease.LEASE_FILE).write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(), "owner": owner, "started": 1.0}))


def test_free_session_can_be_taken(tmp_path: Path):
    assert lease.acquire(tmp_path) is not None
    assert lease.holder(tmp_path)["pid"] == os.getpid()


def test_second_live_process_is_refused(tmp_path: Path):
    other = _other_process()
    try:
        _held_by(tmp_path, other.pid, "web")
        assert lease.acquire(tmp_path) is None
        assert lease.holder(tmp_path)["owner"] == "web"
    finally:
        other.kill()
        other.wait()


def test_crashed_holder_leaves_it_free(tmp_path: Path):
    other = _other_process()
    other.kill()
    other.wait()
    _held_by(tmp_path, other.pid)
    assert lease.holder(tmp_path) is None
    assert lease.acquire(tmp_path) is not None


def test_same_process_can_take_it_again(tmp_path: Path):
    assert lease.acquire(tmp_path) and lease.acquire(tmp_path)


def test_release_frees_it(tmp_path: Path):
    lease.acquire(tmp_path).release()
    assert lease.holder(tmp_path) is None


def test_release_never_removes_another_process_lease(tmp_path: Path):
    other = _other_process()
    try:
        mine = lease.acquire(tmp_path)
        _held_by(tmp_path, other.pid)
        mine.release()
        assert lease.holder(tmp_path)["pid"] == other.pid
    finally:
        other.kill()
        other.wait()


def test_other_machine_counts_as_live(tmp_path: Path):
    _held_by(tmp_path, 999_999, host="elsewhere")
    assert lease.acquire(tmp_path) is None


def test_corrupt_lease_is_free(tmp_path: Path):
    (tmp_path / lease.LEASE_FILE).write_text("{not json")
    assert lease.holder(tmp_path) is None and lease.acquire(tmp_path) is not None


def test_store_reports_a_conflict(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SessionStore(workspace=ws)
    sid = store.open()
    folder = store.active_file.parent
    assert store.conflict is None
    other = _other_process()
    try:
        _held_by(folder, other.pid, "web")
        fresh = SessionStore(workspace=ws)
        fresh.open(sid)
        assert fresh.conflict["owner"] == "web"
    finally:
        other.kill()
        other.wait()


def test_switching_sessions_releases_the_old_one(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SessionStore(workspace=ws)
    store.open("first")
    first = store.active_file.parent
    store.open("second")
    assert lease.holder(first) is None
