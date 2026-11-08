"""Regression tests from the left-panel/browser end-to-end round: concurrency in the page, deleted chats, renamed titles
in Usage, forks that keep replies, Insights empty states, trace menu status, dead code left by the removed left panel."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features.dashboard_web import APP_JS  # noqa: E402
from test_dashboard import Client, c, env, server  # noqa: E402,F401
from test_dashboard_manage import _fake_chat  # noqa: E402


# ─── a deleted chat's saved tab ──────────────────────────────────────────────

def test_a_saved_tab_for_a_deleted_chat_says_not_found_instead_of_opening_empty():
    assert "C.missing = true" in APP_JS and "Chat not found" in APP_JS
    from axon.features.dashboard_web import i18n_asset
    code = APP_JS.replace(i18n_asset("i18n.js")[0], "", 1)      # the translation catalog also holds the words "Chat not found"
    seg = code.split("Chat not found")[0][-420:]
    assert "C.missing && !C.runtime" in seg and "composerEl.hidden = true" in seg     # a chat that is running is never called missing


def test_a_new_tab_does_not_borrow_the_title_of_the_chat_before_it():
    assert "isChat && C.tab === t && C.meta" in APP_JS


# ─── several chats at once ───────────────────────────────────────────────────

def test_hidden_busy_chats_past_four_let_go_of_their_stream_so_send_and_stop_get_through():
    assert "const MAX_STREAMS = 4;" in APP_JS and "openStreamCount(C) >= MAX_STREAMS" in APP_JS
    assert "refreshState(t.C, true)" in APP_JS            # showing a tab picks its stream up again


def test_a_chat_that_reconnects_resumes_after_its_last_event_not_after_the_newest():
    """A hidden tab whose turn ended while its stream was closed stayed on row 13 of 40 and 'running'."""
    assert "C.streamed ? (C.lastSeq || 0)" in APP_JS


def test_trace_menu_redraws_the_status_bar_and_every_feed():
    from axon.features.dash_js_workbench import JS_WORKBENCH
    seg = JS_WORKBENCH.split("{ head: 'Trace detail' }")[1][:420]
    assert "drawStatus()" in seg and "querySelectorAll('.feed')" in seg


# ─── usage titles ────────────────────────────────────────────────────────────

def test_usage_session_table_shows_the_renamed_title(c, env):
    sid = "session_20261101_120000_proj_aaaa01"
    before = next(r["title"] for r in c.json("/api/manage/usage?period=all")["by_session"] if r["id"] == sid)
    from axon import storage
    path = storage.transcript_path(storage.sessions_dir(env.ws), sid)
    with path.open("a", encoding="utf-8") as f:                       # what renaming a chat appends to its transcript
        f.write(json.dumps({"type": "session_rename", "timestamp": 9e9, "data": {"title": "A brand new name"}}) + "\n")
    after = next(r["title"] for r in c.json("/api/manage/usage?period=all")["by_session"] if r["id"] == sid)
    assert before != "A brand new name" and after == "A brand new name"


def test_budget_list_reads_the_fresh_title(server, c):
    _fake_chat(server)
    assert c.json("/api/manage/budget")["budgets"][0]["title"]       # still answers for a chat that has no record yet


# ─── a fork keeps the replies ────────────────────────────────────────────────

def test_a_branch_keeps_the_assistant_text_so_the_forked_chat_shows_the_reply(tmp_path: Path):
    from axon.agent.state import Conversation
    from axon.session.interactive import handle_branch
    from axon.session.store import SessionStore
    store = SessionStore(tmp_path, session_dir=tmp_path / "s")
    msgs = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi there"}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}]}]
    handle_branch(SimpleNamespace(session=store, conversation=Conversation(list(msgs))), "kept")
    from axon.session.store import iter_records
    recs = [r for r in iter_records(store.path_for(store.active_session_id)) if r.get("type") == "assistant_turn"]
    d = recs[0]["data"]
    assert d["text"] == "Hi there" and d["tool_uses"][0]["name"] == "Read"
    assert store.read_conversation(store.active_session_id).messages == msgs        # the model still gets the exact messages


# ─── Insights ────────────────────────────────────────────────────────────────

def test_context_diff_with_no_calls_shows_its_message_not_an_empty_box():
    from axon.features.dash_js_insights import JS_INSIGHTS
    assert "d.ok && (d.lines || []).length ?" in JS_INSIGHTS and "d.message ||" in JS_INSIGHTS


def test_compaction_tab_shows_plain_words_not_raw_json():
    from axon.features.dash_js_insights import JS_INSIGHTS
    assert "'Started by'" in JS_INSIGHTS and "Messages that were replaced" in JS_INSIGHTS and "JSON.stringify" in JS_INSIGHTS.split("function kvTable")[1][:300]
    seg = JS_INSIGHTS.split("tab === 'compaction'")[1][:900]
    assert "kvTable(d.receipt)" not in seg


def test_subagent_page_title_is_the_task_label_not_the_whole_prompt():
    from axon.features.dash_js_subagents import JS_SUBAGENTS
    assert "D.subagent.title) || m.title" in JS_SUBAGENTS


# ─── the first page load ─────────────────────────────────────────────────────

def test_make_server_imports_the_tools_package_before_request_threads_do():
    # Windows needs SystemRoot to start Python at all (random numbers for hash seeding), so it is kept.
    base_env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
    # importing the module alone is allowed to be lazy; the check is on make_server itself
    code2 = ("import sys, tempfile; from pathlib import Path; from axon.features.dashboard import make_server;"
             "s = make_server(0, Path(tempfile.mkdtemp())); print('axon.tools._files' in sys.modules); s.server_close()")
    out = subprocess.run([sys.executable, "-I", "-c", code2], capture_output=True, text=True, timeout=60,
                         env={**base_env, "PATH": "/usr/bin:/bin", "AXON_HOME": tempfile_dir(),
                              "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "AXON_OFFLINE": "1", "AXON_NO_UPDATE_CHECK": "1"})
    assert out.stdout.strip().endswith("True"), out.stderr[-400:]


def tempfile_dir() -> str:
    import tempfile
    return tempfile.mkdtemp()


# ─── dead code from the removed left panel and bottom dock ───────────────────

@pytest.mark.parametrize("name", ["mountTree", "mountSearch", "parseCsv", "currentTab"])
def test_orphaned_functions_are_gone(name):
    assert not re.search(r"\b" + name + r"\b", APP_JS), name


def test_the_page_script_still_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    p = Path(__file__).parent / "_left_browser_parse.js"
    try:
        p.write_text(APP_JS, encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True, timeout=60)
    finally:
        p.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr[:500]
