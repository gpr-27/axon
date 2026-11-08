"""Regressions from the win3 round: the chat list says when old chats are not listed, the Recent projects rows in
Change folder keep their names visible, and a typed @file with a secret in it is checked before the message is sent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_css, dash_js_chat, dash_js_files, dash_js_manage, dashboard as dash  # noqa: E402
from test_dashboard import Client, c, env, server  # noqa: E402,F401


def test_sessions_list_says_when_older_chats_are_not_listed(c, env, monkeypatch):
    assert "cut_off" not in c.json("/api/manage/sessions")
    monkeypatch.setattr(dash.SessionIndex, "LIMIT_PER_PROJECT", 5)
    d = c.json("/api/manage/sessions?fresh=1")
    assert d["scope_total"] == 5
    assert d["cut_off"]["limit"] == 5 and d["cut_off"]["projects"] == [env.ws.name]


def test_the_sessions_page_shows_the_cut_off_note():
    assert "d.cut_off" in dash_js_manage.__dict__.get("MANAGE_JS", "") or "cut_off" in Path(dash_js_manage.__file__).read_text(encoding="utf-8")
    assert "are listed here; older ones are still on disk" in Path(dash_js_manage.__file__).read_text(encoding="utf-8")


def test_recent_project_names_are_not_squeezed_out_by_the_path():
    js = Path(dash_js_files.__file__).read_text(encoding="utf-8")
    css = Path(dash_css.__file__).read_text(encoding="utf-8")
    assert "'nm rn'" in js and "'muted rpath'" in js
    assert ".dirpick .node .nm.rn{flex:0 0 auto" in css and ".dirpick .node .rpath{flex:1;min-width:0" in css


def test_a_typed_at_file_with_a_secret_is_checked_before_sending(c, env):
    (env.ws / "leak.txt").write_text("token sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789\n", encoding="utf-8")
    (env.ws / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    st, _, body = c.post("/api/attach/paths", {"paths": ["leak.txt", "clean.txt"]})
    import json
    items = {i["name"]: i for i in json.loads(body)["items"]}
    assert st == 200 and items["leak.txt"]["secrets"] and not items["clean.txt"].get("secrets")
    js = Path(dash_js_chat.__file__).read_text(encoding="utf-8")
    assert "typedFilesAreSafe(text)" in js and "Send a file that may hold a secret?" in js
