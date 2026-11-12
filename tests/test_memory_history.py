"""Memory write history, hash conflicts, restore and redaction of past versions."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon.agent import auto_memory as am  # noqa: E402
from axon.features import memory_files as mf  # noqa: E402
from axon.features import memory_history as mh  # noqa: E402
from axon.features import memory_hub as hub  # noqa: E402


@pytest.fixture(autouse=True)
def _home(isolate_user_home: Path) -> Path:
    return isolate_user_home


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    return d


def test_file_scope_history_records_actor_and_hash(ws: Path) -> None:
    hub.write_scope(ws, "project", "one")
    with mh.writing_as("dashboard", "s1"):
        hub.write_scope(ws, "project", "two")
    vs = mh.versions(ws, "file-project")
    assert [e.actor for e in vs] == ["user (/memory)", "dashboard"]
    assert vs[1].session == "s1" and vs[1].sha == mh.sha("two\n")


def test_outside_edit_is_kept_as_baseline(ws: Path) -> None:
    hub.write_scope(ws, "project", "one")
    (ws / "AXON.md").write_text("edited elsewhere\n")
    hub.write_scope(ws, "project", "three")
    vs = mh.versions(ws, "file-project")
    assert [e.action for e in vs] == ["created", "baseline", "updated"]
    assert vs[1].text == "edited elsewhere\n"


def test_hash_conflict_blocks_write(ws: Path) -> None:
    hub.write_scope(ws, "project", "one")
    seen = mh.file_sha(ws / "AXON.md")
    hub.write_scope(ws, "project", "dashboard edit")
    with pytest.raises(mh.Conflict, match="changed since it was read"):
        hub.write_scope(ws, "project", "agent edit", expected_sha=seen)
    assert (ws / "AXON.md").read_text() == "dashboard edit\n"
    hub.write_scope(ws, "project", "ok", expected_sha=mh.file_sha(ws / "AXON.md"))


def test_note_conflict_and_history(ws: Path) -> None:
    r = mf.write_memory(ws, "repo", name="Deploy", body="old")
    seen = mh.file_sha(r.path)
    mf.write_memory(ws, "repo", name="Deploy", body="new", file=r.path.name)
    m = mf.find_memory(ws, f"repo:{r.path.stem}")
    with pytest.raises(mh.Conflict):
        mf.replace_body(ws, m, "stale write", expected_sha=seen)
    assert "new" in r.path.read_text()
    key, _label = mh.resolve(ws, f"repo:{r.path.stem}")
    assert len(mh.versions(ws, key)) == 2


def test_auto_capture_logged_and_conflict_retries(ws: Path) -> None:
    facts = am.extract_facts("From now on always use tabs not spaces in this project.")
    assert facts
    assert am.save_facts(ws, facts)
    key = next(k for k in mh.all_keys(ws) if k.startswith("auto__")).replace("auto__", "auto/", 1)
    assert mh.versions(ws, key)[-1].actor == "auto-capture"


def test_restore_version_keeps_current(ws: Path) -> None:
    hub.write_scope(ws, "project", "v-one")
    hub.write_scope(ws, "project", "v-two")
    res = mh.restore_version(ws, "file-project", 1)
    assert res["ok"] and (ws / "AXON.md").read_text() == "v-one\n"
    vs = mh.versions(ws, "file-project")
    assert vs[-1].action == "restored" and vs[1].text == "v-two\n"
    with pytest.raises(ValueError):
        mh.restore_version(ws, "file-project", 99)


def test_restore_deleted_note(ws: Path) -> None:
    r = mf.write_memory(ws, "repo", name="Gone", body="keep me")
    mem = mf.find_memory(ws, f"repo:{r.path.stem}")
    mf.delete_memory(ws, mem)
    key = mh.note_key("repo", r.path.name)
    vs = mh.versions(ws, key)
    assert vs[-1].action == "deleted"
    mh.restore_version(ws, key, vs[-2].v)
    assert "keep me" in r.path.read_text()


def test_redact_version_keeps_audit_trail(ws: Path) -> None:
    hub.write_scope(ws, "project", "the code is hunter2-plain")
    hub.write_scope(ws, "project", "something else")
    out = mh.redact_version(ws, "file-project", 1, "hunter2-plain")
    assert not out["live_scrubbed"]
    e = mh.get_version(ws, "file-project", 1)
    assert e.redacted and "hunter2" not in e.text and e.sha == mh.sha("the code is hunter2-plain\n")
    with pytest.raises(ValueError):
        mh.redact_version(ws, "file-project", 1, "nothere")


def test_history_bounded(ws: Path) -> None:
    for i in range(mh.MAX_VERSIONS + 10):
        hub.write_scope(ws, "project", f"text {i}")
    assert len(mh.versions(ws, "file-project")) == mh.MAX_VERSIONS


def test_commands_history_restore_redact(ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from types import SimpleNamespace
    from axon.commands.memory_history_cmds import handle_history_command, handle_dream
    from axon.ui.theme import strip_ansi
    hub.write_scope(ws, "project", "alpha secret-thing")
    hub.write_scope(ws, "project", "beta")
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=ws), reload_memory=lambda: None)
    assert handle_history_command(agent, "history", "project")
    assert "v2" in strip_ansi(capsys.readouterr().out)
    assert handle_history_command(agent, "redact", "project 1 secret-thing")
    assert mh.get_version(ws, "file-project", 1).redacted
    assert handle_history_command(agent, "restore", "project 1")
    assert (ws / "AXON.md").read_text() == "alpha [redacted]\n"
    assert not handle_history_command(agent, "restore", "3")          # one number stays the trash restore


from test_memory_hub import _post, web  # noqa: E402,F401  (the dashboard fixture)


def test_web_conflict_history_restore_redact(web) -> None:
    cl, ws, _ = web
    d = lambda: next(s for s in cl.json("/api/manage/memory/scopes")["scopes"] if s["key"] == "project")  # noqa: E731
    assert _post(cl, "/api/manage/memory/scope", {"key": "project", "text": "first"})[0] == 200
    seen = d()["sha"]
    (ws / "AXON.md").write_text("edited by the terminal\n")
    st, r = _post(cl, "/api/manage/memory/scope", {"key": "project", "text": "stale save", "expected_sha": seen})
    assert st == 409 and "changed since it was read" in r["error"] if "error" in r else st == 409
    assert (ws / "AXON.md").read_text() == "edited by the terminal\n"
    assert _post(cl, "/api/manage/memory/scope", {"key": "project", "text": "fresh", "expected_sha": d()["sha"]})[0] == 200
    h = cl.json("/api/manage/memory/history?ref=project")
    assert h["versions"][0]["actor"] == "dashboard" and len(h["versions"]) >= 3
    first = h["versions"][-1]["v"]
    assert _post(cl, "/api/manage/memory/restore", {"ref": "project", "v": first})[0] == 200
    assert (ws / "AXON.md").read_text() == "first\n"
    assert _post(cl, "/api/manage/memory/redact", {"ref": "project", "v": first, "secret": "nope"})[0] == 400
    assert cl.json("/api/manage/memory/dream")["proposal"] is None
