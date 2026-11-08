"""Regression tests from the Sessions / Usage / Memory end-to-end round."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from axon import storage  # noqa: E402
from test_dashboard import Client, c, env, server  # noqa: E402,F401
from test_dashboard_manage import listing, post, sess_dir  # noqa: E402,F401


def test_bulk_delete_names_each_trash_folder_without_rescanning_the_trash(c, env, monkeypatch):
    from axon.session.store import SessionStore
    calls = []
    orig = SessionStore.list_trash
    monkeypatch.setattr(SessionStore, "list_trash", lambda self: calls.append(1) or orig(self))
    ids = [env.ids["short"], env.ids["xss"]]
    st, r = post(c, "/api/manage/sessions/delete", {"ids": ids, "confirm_count": 2})
    assert st == 200 and not calls                      # a scan per delete made 5,000 deletes take minutes
    names = {x["id"]: x["name"] for x in r["deleted"]}
    for sid in ids:
        assert (storage.trash_dir(env.ws) / names[sid] / "manifest.json").is_file()
    trash = c.json("/api/manage/trash")["items"]
    st, res = post(c, "/api/manage/trash/restore", {"items": [{"project_key": t["project_key"], "name": t["name"]} for t in trash if t["id"] in ids]})
    assert st == 200 and len(res["restored"]) == 2 and not calls
    assert (sess_dir(env) / ids[0] / "session.jsonl").is_file()


def test_restore_never_walks_out_of_the_trash(env):
    from axon.session.store import SessionStore
    store = SessionStore(env.ws)
    assert store.restore_session("..") is None and store.restore_session(".") is None


def test_a_bad_period_is_refused_not_replaced_by_30_days(c, env):
    for bad in ("2026-13-45..2026-11-08", "abc", "1999-01-01..2026-11-08"):
        assert c.get(f"/api/manage/usage?period={bad}")[0] == 400, bad
        assert c.get(f"/api/manage/usage/csv?table=days&period={bad}")[0] == 400, bad
    assert c.get("/api/manage/usage?period=7d")[0] == 200


def test_memory_page_lists_every_scope_and_edits_and_deletes(c, env):
    for scope in ("global", "project", "repo"):
        st, r = post(c, "/api/manage/memory/save", {"scope": scope, "body": f"{scope} note about tabs sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV123456"})
        assert st == 200 and r["action"] == "created"
    d = c.json("/api/manage/memory")
    assert d["counts"] == {"global": 1, "project": 1, "repo": 1} and "sk-ant" not in str(d)
    assert c.json("/api/manage/memory?scope=repo")["total"] == 1 and c.json("/api/manage/memory?q=zzz")["total"] == 0
    m = d["memories"][0]
    st, r = post(c, "/api/manage/memory/update", {"scope": m["scope"], "id": m["id"], "body": "edited"})
    assert st == 200 and r["action"] == "updated"
    assert c.json("/api/manage/memory?q=edited")["total"] == 1
    assert post(c, "/api/manage/memory/save", {"scope": "nope", "body": "x"})[0] == 400
    assert post(c, "/api/manage/memory/save", {"scope": "repo", "body": "  "})[0] == 400
    assert post(c, "/api/manage/memory/delete", {"scope": "repo", "id": "../../x"})[0] == 404
    st, _ = post(c, "/api/manage/memory/delete", {"scope": m["scope"], "id": m["id"]})
    assert st == 200 and c.json("/api/manage/memory")["total"] == 2


def test_instruction_files_come_from_the_axon_home_like_the_terminal(env):
    from axon.features.dash_api import instruction_files
    (storage.axon_home() / "AXON.md").write_text("global rule\n", encoding="utf-8")
    rows = instruction_files(env.ws)
    assert any(r["path"] == str(storage.axon_home() / "AXON.md") and r["scope"] == "user" and not r["editable"] for r in rows)
