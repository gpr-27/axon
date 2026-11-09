"""Chat round two: export (Markdown, standalone HTML, PDF by print, JSON), share with secrets counted, bookmarks, search,
message actions (rewind, edit and resend, regenerate, branch) and the terminal /bookmark, /bookmarks and /export formats.

The rewind tests use a real session on disk and the fake LLM: the next request the model gets must not hold the messages
that were removed."""
from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import chat_export as cx  # noqa: E402
from axon.features import dashboard as dash  # noqa: E402
from axon.features.dash_js_chatx import apply_rewinds  # noqa: E402
from test_dashboard import Client, c, env, llm, server  # noqa: E402,F401
from test_dashboard_app import types, wait_for  # noqa: E402
from test_dashboard_sample import SECRET_KEY  # noqa: E402

KEY = "sk-ant-api03-" + "a1B2c3D4e5F6g7H8i9J0" * 2


# ─── the data module ─────────────────────────────────────────────────────────

MESSAGES = [
    {"role": "user", "content": "first <b>prompt</b>"},
    {"role": "assistant", "content": [{"type": "text", "text": "Reading it.\n\n- one\n- two\n\n`code` and **bold** [site](https://example.com) [bad](javascript:alert(1))"},
                                      {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"hi\nANTHROPIC_API_KEY={KEY}"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    {"role": "user", "content": "second prompt"},
    {"role": "assistant", "content": "Second answer."},
]


def test_items_from_messages_merges_a_reply_and_attaches_tool_results():
    items = cx.items_from_messages(MESSAGES)
    assert [i["kind"] for i in items] == ["user", "assistant", "user", "assistant"]
    reply = items[1]
    assert "Reading it." in reply["text"] and "Done." in reply["text"]
    assert reply["tools"][0]["name"] == "Bash" and "hi" in reply["tools"][0]["result"]


def test_openai_style_tool_calls_become_tools():
    items = cx.items_from_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{\"path\": \"a.txt\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "file body"},
        {"role": "assistant", "content": "ok"}])
    assert items[1]["tools"] == [{"name": "Read", "input": {"path": "a.txt"}, "result": "file body", "is_error": False}]


def test_redaction_counts_what_it_removes_and_never_edits_the_original():
    items = cx.items_from_messages(MESSAGES)
    out, n = cx.redact_items(items, cx.default_redactor())
    assert n >= 1 and KEY not in json.dumps(out) and KEY in json.dumps(items)
    again, n2 = cx.redact_items(out, cx.default_redactor())
    assert n2 == 0                                              # nothing left to remove the second time


def test_markdown_export_with_and_without_tool_details():
    items = cx.items_from_messages(MESSAGES)
    full = cx.build(items, {"id": "session_x", "title": "My chat", "model": "m"}, "md", True)
    assert full["redacted"] >= 1 and KEY not in full["content"]
    assert "## You" in full["content"] and "**Tool:** `Bash`" in full["content"] and "echo hi" in full["content"]
    lean = cx.build(items, {"id": "session_x", "title": "My chat"}, "md", False)
    assert "Bash" not in lean["content"] and "echo hi" not in lean["content"] and "Second answer." in lean["content"]
    assert lean["filename"].endswith(".md") and lean["filename"].startswith("my-chat-")


def test_markdown_fence_is_longer_than_any_backticks_in_the_content():
    items = cx.items_from_messages([{"role": "user", "content": "x"},
                                    {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "echo '```'"}}]},
                                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "```\ninner\n```"}]}])
    md = cx.to_markdown(items, {"title": "t"})
    assert "````" in md


def test_html_export_is_one_file_without_scripts_and_escapes_everything():
    items = cx.items_from_messages([{"role": "user", "content": "<script>alert(1)</script> & <img src=x onerror=alert(2)>"},
                                    {"role": "assistant", "content": "[click](javascript:alert(3)) [ok](https://example.com/a?b=1&c=2)\n\n```\n</pre><script>x</script>\n```"}])
    out = cx.build(items, {"id": "session_1", "title": "<b>T</b>"}, "html", True)
    doc = out["content"]
    assert "<script" not in doc.lower() and "<img" not in doc and "onerror=" not in doc.replace("&lt;img src=x onerror=alert(2)&gt;", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc and "javascript:" not in doc.replace("[click](javascript:alert(3))", "")
    assert 'href="https://example.com/a?b=1&amp;c=2"' in doc and 'rel="noopener noreferrer"' in doc
    assert "<style>" in doc and "@media print" in doc and "<link" not in doc and not re.search(r"(src|href)=[\"']https?://(?!example\.com)", doc)
    assert "<title>&lt;b&gt;T&lt;/b&gt;</title>" in doc
    pdf = cx.build(items, {"id": "session_1", "title": "T"}, "pdf", True)
    assert pdf["content"].startswith("<!doctype html>") and pdf["filename"].endswith(".html")


def test_html_export_counts_secrets_in_the_footer():
    items = cx.items_from_messages(MESSAGES)
    out = cx.build(items, {"id": "session_x", "title": "t"}, "html", True)
    assert KEY not in out["content"] and re.search(r"\d+ secrets? \(keys, tokens, passwords\) removed", out["content"])


def test_bad_format_is_refused():
    with pytest.raises(ValueError):
        cx.build([], {}, "docx")


def test_search_counts_messages_and_collapsed_tool_text_but_not_empty_queries():
    items = cx.items_from_messages(MESSAGES)
    r = cx.search_items(items, "echo")
    assert r["total"] == 1 and r["messages"][0]["kind"] == "assistant" and r["messages"][0]["where"].startswith("tool")
    assert cx.search_items(items, "PROMPT")["total"] == 2          # case-insensitive
    assert cx.search_items(items, "   ")["total"] == 0 and cx.search_items(items, "nothing-here")["messages"] == []


def test_bookmarks_persist_dedupe_and_survive_a_damaged_file(tmp_path):
    f = tmp_path / "sess"
    b, created = cx.add_bookmark(f, 12.5, "assistant", f"look at {KEY} now")
    assert created and KEY not in json.dumps(b)
    again, created2 = cx.add_bookmark(f, 12.5, "assistant", "same")
    assert not created2 and again["id"] == b["id"]
    cx.add_bookmark(f, 20.0, "user", "u")
    assert [r["ts"] for r in cx.load_bookmarks(f)] == [12.5, 20.0]
    assert cx.remove_bookmark(f, b["id"]) and not cx.remove_bookmark(f, b["id"])
    (f / cx.BOOKMARK_FILE).write_text("{not json", encoding="utf-8")
    assert cx.load_bookmarks(f) == []
    cx.add_bookmark(f, 1.0, "assistant", "fresh")             # a damaged file is simply replaced
    assert len(cx.load_bookmarks(f)) == 1 and not list(f.glob("*.tmp"))
    with pytest.raises(ValueError):
        cx.add_bookmark(f, 2.0, "system", "x")


def test_messages_through_prompt_keeps_the_reply():
    kept = cx.messages_through_prompt(MESSAGES, 1)
    assert len(kept) == 4 and kept[-1]["content"][0]["text"] == "Done."
    assert cx.messages_through_prompt(MESSAGES, 2) == MESSAGES
    with pytest.raises(ValueError):
        cx.messages_through_prompt(MESSAGES, 3)


def test_apply_rewinds_drops_the_removed_turns():
    ev = [{"seq": 1, "type": "user"}, {"seq": 2, "type": "turn_end"}, {"seq": 3, "type": "user"}, {"seq": 4, "type": "turn_end"},
          {"seq": 5, "type": "rewound", "data": {"cut_seq": 3}}, {"seq": 6, "type": "user"}]
    assert [e["seq"] for e in apply_rewinds(ev)] == [1, 2, 5, 6]
    assert apply_rewinds(ev[:4]) == ev[:4]


# ─── a real session on disk ──────────────────────────────────────────────────

def _turn(c: Client, sid: str | None, text: str, script: list, llm) -> str:
    llm.enqueue(*script)
    body = {"text": text, **({"session": sid} if sid else {})}
    sid = json.loads(c.post("/api/chat/send", body)[2])["session"]
    rt = c.srv.runtime(sid, create=False)

    def done() -> bool:
        for aid in list(rt.pending):
            c.post("/api/chat/approval", {"session": sid, "id": aid, "choice": "once"})
        return not rt.busy
    wait_for(done, 30)
    return sid


def _wire(req) -> str:
    return json.dumps(req.messages, ensure_ascii=False)


@pytest.fixture
def chat3(c, env, llm):
    """Three prompts: each writes its own file. Returns the session id."""
    sid = None
    for n in (1, 2, 3):
        sid = _turn(c, sid, f"PROMPT-{n} make f{n}", [[{"tool": "Write", "args": {"path": f"f{n}.txt", "content": f"v{n}"}}], [{"text": f"ANSWER-{n} done"}]], llm)
    return SimpleNamespace(sid=sid, c=c, env=env, llm=llm)


def test_the_message_map_lines_up_with_the_conversation(chat3):
    m = chat3.c.json(f"/api/chatx/map?session={chat3.sid}")
    assert [t["prompt"] for t in m["turns"]] == [1, 2, 3]
    assert [t["removes"] for t in m["turns"]] == [6, 4, 2]
    assert m["turns"][1]["text"].startswith("PROMPT-2") and m["turns"][1]["reply"].startswith("ANSWER-2")
    full = chat3.c.json(f"/api/chatx/message?session={chat3.sid}&n=1&which=reply")
    assert full["text"] == "ANSWER-2 done"
    assert chat3.c.json(f"/api/chatx/message?session={chat3.sid}&n=0&which=user")["text"].startswith("PROMPT-1")


def test_rewind_preview_says_which_files_would_come_back(chat3):
    pv = chat3.c.json(f"/api/chatx/rewind_preview?session={chat3.sid}&prompt=2")
    assert pv["prompt"] == 2 and pv["files"] == ["f2.txt", "f3.txt"] and pv["files_changed"] == 2
    assert pv["can_restore_files"] and not pv["busy"] and not pv["attachment"]


def test_rewind_conversation_only_keeps_files_and_the_model_never_sees_the_removed_messages(chat3):
    c, llm = chat3.c, chat3.llm
    r = json.loads(c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 2, "mode": "conversation"})[2])
    assert r["ok"] and r["removed"] == 8 and r["reverted"] == []        # two turns: prompt, tool call, tool result, answer
    assert (chat3.env.ws / "f2.txt").exists() and (chat3.env.ws / "f3.txt").exists()          # files untouched
    llm.enqueue([{"text": "NEXT-ANSWER"}])
    c.post("/api/chat/send", {"session": chat3.sid, "text": "NEXT-PROMPT"})
    rt = c.srv.runtime(chat3.sid, create=False)
    wait_for(lambda: not rt.busy)
    wire = _wire(llm.requests[-1])
    assert "PROMPT-1" in wire and "ANSWER-1" in wire and "NEXT-PROMPT" in wire
    for gone in ("PROMPT-2", "PROMPT-3", "ANSWER-2", "ANSWER-3", "f2.txt", "f3.txt"):
        assert gone not in wire, gone
    # what the page draws from the saved record agrees
    m = c.json(f"/api/chatx/map?session={chat3.sid}")
    assert [t["text"][:8] for t in m["turns"]] == ["PROMPT-1", "NEXT-PRO"]
    page = c.json(f"/api/session/{chat3.sid}?limit=50")
    assert "PROMPT-2" not in json.dumps(page) and "NEXT-PROMPT" in json.dumps(page)
    assert "rewound" in types(rt.hub)


def test_rewind_with_files_restores_them_and_a_fresh_server_reads_it_from_disk(chat3, env, llm):
    c = chat3.c
    r = json.loads(c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 2, "mode": "code_and_conversation"})[2])
    assert r["ok"] and any("f2.txt" in x for x in r["reverted"])
    assert (env.ws / "f1.txt").read_text() == "v1" and not (env.ws / "f2.txt").exists() and not (env.ws / "f3.txt").exists()
    # a second server (a restart): the session is read back from the file and still shows only the kept prompt
    srv2 = dash.make_server(0, env.ws)
    t = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        c2 = Client(srv2)
        c2.login()
        m = c2.json(f"/api/chatx/map?session={chat3.sid}")
        assert [x["prompt"] for x in m["turns"]] == [1]
        llm.enqueue([{"text": "AFTER-RESTART"}])
        c2.post("/api/chat/send", {"session": chat3.sid, "text": "ANOTHER"})
        rt2 = srv2.runtime(chat3.sid, create=False)
        wait_for(lambda: not rt2.busy)
        wire = _wire(llm.requests[-1])
        assert "PROMPT-1" in wire and "ANOTHER" in wire and "PROMPT-2" not in wire and "ANSWER-3" not in wire
        ev = c2.json(f"/api/session/{chat3.sid}/events")["events"]
        assert "ANSWER-3" not in json.dumps(ev) and "ANSWER-1" in json.dumps(ev)             # the replay drops the removed turns
    finally:
        srv2.shutdown_runs()
        srv2.shutdown()
        srv2.server_close()


def test_edit_and_resend_flow_sends_the_new_text_and_not_the_old(chat3):
    c, llm = chat3.c, chat3.llm
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 3, "mode": "conversation"})[0] == 200
    llm.enqueue([{"text": "EDITED-ANSWER"}])
    c.post("/api/chat/send", {"session": chat3.sid, "text": "EDITED-PROMPT-3"})
    wait_for(lambda: not c.srv.runtime(chat3.sid, create=False).busy)
    wire = _wire(llm.requests[-1])
    assert "EDITED-PROMPT-3" in wire and "PROMPT-3 make f3" not in wire and "ANSWER-3" not in wire and "ANSWER-2" in wire


def test_rewind_is_refused_while_busy_and_for_bad_input(chat3):
    c = chat3.c
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 9, "mode": "conversation"})[0] == 404
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 1, "mode": "summarize_from_here"})[0] == 400
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": "x", "mode": "conversation"})[0] == 400
    assert c.post("/api/chatx/rewind", {"session": "nope", "prompt": 1, "mode": "conversation"})[0] in (400, 404)
    rt = c.srv.runtime(chat3.sid, create=False)
    chat3.llm.enqueue([{"delay": 30}, {"text": "never"}])
    c.post("/api/chat/send", {"session": chat3.sid, "text": "slow"})
    wait_for(lambda: rt.busy)
    st, _, body = c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 1, "mode": "conversation"})
    assert st == 409 and b"working" in body
    c.post("/api/chat/stop", {"session": chat3.sid})
    wait_for(lambda: not rt.busy, 15)


def test_branch_from_here_keeps_the_conversation_through_that_reply(chat3):
    c, llm = chat3.c, chat3.llm
    st = json.loads(c.post("/api/chat/fork", {"session": chat3.sid, "upto": 2})[2])
    assert st["session"] != chat3.sid and "_branch_" in st["session"]
    llm.enqueue([{"text": "BRANCH-ANSWER"}])
    c.post("/api/chat/send", {"session": st["session"], "text": "BRANCH-PROMPT"})
    wait_for(lambda: not c.srv.runtime(st["session"], create=False).busy)
    wire = _wire(llm.requests[-1])
    assert "ANSWER-2" in wire and "PROMPT-3" not in wire and "ANSWER-3" not in wire and "BRANCH-PROMPT" in wire
    assert len(c.json(f"/api/chatx/map?session={chat3.sid}")["turns"]) == 3                 # the original is unchanged
    assert c.post("/api/chat/fork", {"session": chat3.sid, "upto": 9})[0] == 404


def test_bookmarks_through_the_routes_jump_data_and_a_rewind_hides_dead_ones(chat3):
    c = chat3.c
    st, _, body = c.post("/api/chatx/bookmark", {"session": chat3.sid, "n": 2, "kind": "assistant"})
    b = json.loads(body)
    assert st == 200 and b["created"] and "ANSWER-3" in b["bookmark"]["snippet"]
    assert json.loads(c.post("/api/chatx/bookmark", {"session": chat3.sid, "n": 2, "kind": "assistant"})[2])["created"] is False
    m = c.json(f"/api/chatx/map?session={chat3.sid}")
    assert [(x["n"], x["kind"]) for x in m["bookmarks"]] == [(2, "assistant")] and m["turns"][2]["bookmark"] == b["bookmark"]["id"]
    assert (Path(c.srv.index.lookup(chat3.sid).path).parent / cx.BOOKMARK_FILE).is_file()      # kept with the session
    assert c.post("/api/chatx/bookmark", {"session": chat3.sid, "n": 99, "kind": "assistant"})[0] == 404
    c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 3, "mode": "conversation"})
    assert c.json(f"/api/chatx/map?session={chat3.sid}")["bookmarks"] == []                    # its message is gone
    assert json.loads(c.post("/api/chatx/bookmark", {"session": chat3.sid, "n": 0, "kind": "assistant"})[2])["created"]
    bid = c.json(f"/api/chatx/map?session={chat3.sid}")["bookmarks"][0]["id"]
    assert json.loads(c.post("/api/chatx/bookmark/remove", {"session": chat3.sid, "id": bid})[2])["ok"]
    assert c.json(f"/api/chatx/map?session={chat3.sid}")["bookmarks"] == []


def test_search_finds_text_inside_tool_calls(chat3):
    r = chat3.c.json(f"/api/chatx/search?session={chat3.sid}&q=f2.txt")
    assert r["total"] >= 1 and all(m["kind"] == "assistant" for m in r["messages"])
    assert chat3.c.json(f"/api/chatx/search?session={chat3.sid}&q=")["total"] == 0


def test_routes_need_the_cookie_and_the_origin(chat3):
    c = chat3.c
    assert c.post("/api/chatx/bookmark", {"session": chat3.sid, "n": 0}, origin=None)[0] == 403
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 1, "mode": "conversation"}, origin="http://evil.example.com")[0] == 403
    assert c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 1, "mode": "conversation"}, cookie=False)[0] == 401
    assert c.get(f"/api/chatx/map?session={chat3.sid}", cookie=False)[0] == 401
    assert c.get("/api/chatx/nothing")[0] == 404


# ─── export and share in the app ─────────────────────────────────────────────

def test_share_preview_formats_tools_and_secret_count(c, env):
    sid = env.ids["xss"]
    base = json.loads(c.post("/api/share/preview", {"kind": "session", "id": sid})[2])
    assert base["format"] == "html" and base["redacted"] >= 3 and "<script" not in base["content"].lower()
    assert re.search(r"\d+ secrets? \(keys, tokens, passwords\) removed", base["content"])
    lean = json.loads(c.post("/api/share/preview", {"kind": "session", "id": sid, "tools": False})[2])
    assert lean["bytes"] < base["bytes"] and "Bash" not in lean["content"]
    md = json.loads(c.post("/api/share/preview", {"kind": "session", "id": sid, "format": "md"})[2])
    assert md["filename"].endswith(".md") and md["content"].lstrip().startswith("#") and "[REDACTED]" in md["content"]
    js = json.loads(c.post("/api/share/preview", {"kind": "session", "id": sid, "format": "json"})[2])
    assert json.loads(js["content"])["entries"]
    assert c.post("/api/share/preview", {"kind": "session", "id": sid, "format": "docx"})[0] == 400
    for p in (base, md, js):
        assert SECRET_KEY not in p["content"]


def test_share_files_are_served_with_the_right_type_and_a_preview_that_cannot_run(c, env):
    html = json.loads(c.post("/api/share/preview", {"kind": "session", "id": env.ids["xss"]})[2])
    st, h, body = c.get(f"/api/share/file?sha={html['sha256']}")
    assert st == 200 and h["content-type"].startswith("text/html") and h["content-disposition"].startswith("attachment")
    st, h, body = c.get(f"/api/share/file?sha={html['sha256']}&inline=1")
    assert st == 200 and h["content-disposition"] == "inline" and h["x-frame-options"] == "SAMEORIGIN"
    assert "default-src 'none'" in h["content-security-policy"] and "script-src" not in h["content-security-policy"]
    md = json.loads(c.post("/api/share/preview", {"kind": "session", "id": env.ids["xss"], "format": "md"})[2])
    st, h, _ = c.get(f"/api/share/file?sha={md['sha256']}&inline=1")
    assert h["content-type"].startswith("text/markdown") and h["content-disposition"].startswith("attachment")        # never rendered inline


def test_a_rewind_in_the_saved_file_hides_the_removed_prompts_in_the_page(tmp_path):
    from axon.session.store import SessionStore
    ws = tmp_path / "w"
    ws.mkdir()
    store = SessionStore(ws, session_dir=tmp_path / "s")
    sid = store.open(None)
    msgs = []
    for n in (1, 2, 3):
        store.append_user(f"P{n}")
        store.append("assistant_turn", {"text": f"A{n}", "tool_uses": [], "usage": None})
        msgs += [{"role": "user", "content": f"P{n}"}, {"role": "assistant", "content": f"A{n}"}]
    store.append_rewind(msgs[:2], "conversation", 2)
    store.append_user("P4")
    tr = dash.parse_transcript(store._file(sid))
    assert [e["text"] for e in tr.entries if e["kind"] == "user"] == ["P1", "P4"]
    assert [e["text"] for e in tr.entries if e["kind"] == "assistant"] == ["A1"]
    store.append_rewind(msgs[:2], "code", 1)                      # a code-only rewind removes nothing from the conversation
    assert len(dash.parse_transcript(store._file(sid)).entries) == 3


# ─── the page script ─────────────────────────────────────────────────────────

def test_the_script_has_the_actions_find_bookmarks_and_the_dialog(c):
    js = c.get("/static/app.js")[2].decode()
    for needle in ("function cxFind", "function cxBookmarksMenu", "function shareModal", "Edit and resend", "Regenerate", "Branch from here",
                   "Export or share", "/api/chatx/rewind", "nothing is uploaded"):
        assert needle in js, needle
    assert js.count("function shareModal") == 1                  # one Share dialog, not two
    css = c.get("/static/app.css")[2].decode()
    assert ".mxa" in css and "::highlight(cx-match)" in css
    assert "innerHTML" not in re.sub(r"(?s)function h\(.*?\n}\n", "", js[js.index("// ---------- chat extras"):])


# ─── the terminal ────────────────────────────────────────────────────────────

def _agent_with_chat(tmp_path, monkeypatch):
    from test_commands_parity import _make
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    (tmp_path / "ws").mkdir()
    return _make(tmp_path / "ws")


def test_terminal_export_html_and_pdf_and_no_tools(tmp_path, monkeypatch, capsys):
    from axon.commands.registry import dispatch_command
    agent = _agent_with_chat(tmp_path, monkeypatch)
    agent.conversation.messages[:] = [dict(m) for m in MESSAGES]
    out = tmp_path / "x.html"
    dispatch_command(f"/export html {out}", agent)
    doc = out.read_text(encoding="utf-8")
    assert doc.startswith("<!doctype html>") and "<script" not in doc.lower() and KEY not in doc and "echo hi" in doc
    lean = tmp_path / "lean.html"
    dispatch_command(f"/export html {lean} --no-tools", agent)
    assert "echo hi" not in lean.read_text(encoding="utf-8")
    pdf = tmp_path / "print.html"
    dispatch_command(f"/export pdf {pdf}", agent)
    shown = capsys.readouterr().out
    assert pdf.is_file() and "Save as PDF" in shown and "secret" in shown
    old = tmp_path / "x.md"
    dispatch_command(f"/export md {old}", agent)
    assert "## User" in old.read_text(encoding="utf-8")            # the existing Markdown export is unchanged


def test_terminal_bookmarks(tmp_path, monkeypatch, capsys):
    from axon.commands.registry import dispatch_command
    agent = _agent_with_chat(tmp_path, monkeypatch)
    agent.run_turn("remember this one")
    dispatch_command("/bookmarks", agent)
    assert "No bookmarks" in capsys.readouterr().out
    dispatch_command("/bookmark the key answer", agent)
    assert "Bookmarked" in capsys.readouterr().out
    dispatch_command("/bookmark", agent)
    assert "already" in capsys.readouterr().out.lower()
    dispatch_command("/bookmarks", agent)
    listing = capsys.readouterr().out
    assert "the key answer" in listing and "1" in listing
    dispatch_command("/bookmarks show 1", agent)
    assert capsys.readouterr().out.strip()
    dispatch_command("/bookmarks remove 1", agent)
    dispatch_command("/bookmarks", agent)
    assert "No bookmarks" in capsys.readouterr().out


def test_a_chat_resumed_after_a_restart_still_knows_which_files_a_rewind_would_restore(chat3, env, llm):
    srv2 = dash.make_server(0, env.ws)
    t = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        c2 = Client(srv2)
        c2.login()
        pv = c2.json(f"/api/chatx/rewind_preview?session={chat3.sid}&prompt=2")      # no turn has run on this server yet
        assert pv["files"] == ["f2.txt", "f3.txt"] and pv["can_restore_files"]
    finally:
        srv2.shutdown_runs()
        srv2.shutdown()
        srv2.server_close()


# ─── the real terminal program ───────────────────────────────────────────────

def test_real_cli_session_bookmarks_export_and_rewind(tmp_path):
    """`axon -p` processes against the fake model: bookmark, export (md, html with a secret removed, pdf) and a rewind;
    the model's next request holds nothing that was rewound."""
    from fake_llm import FakeLLMServer
    from harness import create_axon_env
    llm = FakeLLMServer(model_ids=("gpt-4o",)).start()
    try:
        e = create_axon_env(tmp_path, llm)
        llm.enqueue([{"tool": "Write", "args": {"path": "f.txt", "content": f"KEY={KEY}\n"}}], [{"text": "FIRST-ANSWER"}])
        sid = e.run(["-p", "--output-format", "json", "PROMPT-ONE"]).json()["session_id"]
        llm.enqueue([{"text": "SECOND-ANSWER"}])
        e.run(["-p", "--continue", "--output-format", "json", "PROMPT-TWO"])
        r = e.run(["-p", "--continue", "/bookmark the second answer"])
        assert "Bookmarked" in r.output, r
        assert "Bookmarked" not in e.run(["-p", "--continue", "/bookmark"]).output
        listing = e.run(["-p", "--continue", "/bookmarks"]).output
        assert "the second answer" in listing and "SECOND-ANSWER" in listing
        assert (e.axon_home / "projects").is_dir() and list((e.axon_home / "projects").glob(f"*/sessions/{sid}/bookmarks.json"))
        out = tmp_path / "chat.html"
        r = e.run(["-p", "--continue", f"/export html {out}"])
        doc = out.read_text(encoding="utf-8")
        assert "secret" in r.output and KEY not in doc and "PROMPT-ONE" in doc and "<script" not in doc.lower()
        pdf = tmp_path / "print.html"
        assert "Save as PDF" in e.run(["-p", "--continue", f"/export pdf {pdf}"]).output and pdf.is_file()
        lean = tmp_path / "lean.md"
        e.run(["-p", "--continue", f"/export md {lean} --no-tools"])
        assert "PROMPT-ONE" in lean.read_text(encoding="utf-8") and "Write" not in lean.read_text(encoding="utf-8")
        e.run(["-p", "--continue", "/rewind to 2 conversation"])
        llm.enqueue([{"text": "AFTER"}])
        e.run(["-p", "--continue", "--output-format", "json", "PROMPT-THREE"])
        wire = _wire(llm.requests[-1])
        assert "PROMPT-ONE" in wire and "PROMPT-THREE" in wire and "PROMPT-TWO" not in wire and "SECOND-ANSWER" not in wire
        assert "the second answer" not in e.run(["-p", "--continue", "/bookmarks"]).output          # its message is gone
    finally:
        llm.stop()


def test_files_changed_outside_axon_stop_a_code_rewind_unless_overwrite_is_allowed(chat3, env):
    c = chat3.c
    (env.ws / "f3.txt").write_text("my own edit")                   # changed by the user after Axon wrote it
    pv = c.json(f"/api/chatx/rewind_preview?session={chat3.sid}&prompt=2")
    assert pv["conflicts"] == ["f3.txt"]
    st, _, body = c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 2, "mode": "code_and_conversation"})
    assert st == 409 and b"f3.txt" in body and b"Nothing was changed" in body
    assert (env.ws / "f3.txt").read_text() == "my own edit" and (env.ws / "f2.txt").exists()           # nothing touched
    assert len(c.json(f"/api/chatx/map?session={chat3.sid}")["turns"]) == 3                             # nor the conversation
    st, _, body = c.post("/api/chatx/rewind", {"session": chat3.sid, "prompt": 2, "mode": "code_and_conversation", "overwrite": True})
    assert st == 200 and not (env.ws / "f3.txt").exists() and not (env.ws / "f2.txt").exists()


def test_share_offers_a_gist_only_as_an_explicit_second_step():
    from axon.features.dash_js_chatx import JS_CHATX
    share = JS_CHATX[JS_CHATX.index("function shareModal(req)"):]
    assert "Create secret GitHub gist..." in share and "Upload exactly this file" in share
    assert share.index("Upload exactly this file") > share.index("Create secret GitHub gist") - 400
    assert "confirm: true" in share and "p.gh && p.gist_ok" in share


def test_a_finished_live_reply_gets_its_hover_actions():
    """The reply entry is 'live' while it streams (actions hidden); it stayed 'live' after the turn ended, so Copy,
    Regenerate and Bookmark never showed until a reload. finishTurn must drop the class."""
    from axon.features.dashboard_web import APP_JS
    seg = APP_JS[APP_JS.index("async function finishTurn"):]
    assert "foldSteps(cur.art)" in seg[:seg.index("C.cur = null;")]
    fold = APP_JS[APP_JS.index("function foldSteps"):]
    assert "art.classList.remove('live')" in fold[:fold.index("function syncFold")]


def test_failures_stay_visible_and_a_nonzero_shell_exit_counts_as_failed():
    from axon.features.dashboard_web import APP_JS
    seg = APP_JS[APP_JS.index("async function finishTurn"):]
    seg = seg[:seg.index("foldSteps(cur.art)")]
    # nothing folds when a turn ends any more, so a failed step is never hidden by the end of the turn
    fold = APP_JS[APP_JS.index("function foldSteps"):]
    assert "animate" not in fold[:fold.index("function stepsHidden")]
    assert "dataset.steps = 'closed'" not in seg
    done = APP_JS[APP_JS.index("function toolDone"):]
    assert "BASH_EXIT_RE.test" in done[:done.index("setToolState")]
