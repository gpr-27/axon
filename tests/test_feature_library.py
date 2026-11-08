"""Prompt library (fill-in fields, global/project, import and export) and the checkpoint timeline with a safe restore.

The restore tests use a real CheckpointManager on real files in a temporary folder, and the
routes are driven through a real dashboard server."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from test_dashboard import c, env, server  # noqa: E402,F401
from axon.features import checkpoint_view as cv  # noqa: E402
from axon.features import prompt_library as pl  # noqa: E402
from axon.session.checkpoint import CheckpointManager  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "axhome"
    monkeypatch.setenv("AXON_HOME", str(h))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    return h


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "proj"
    w.mkdir()
    return w


# ─── fields ──────────────────────────────────────────────────────────────────

def test_fields_are_found_once_in_order_with_kinds_and_defaults():
    fs = pl.parse_fields("Port {{file}} to {{language|Python}} then test {{file}} and {{config_path}} {{x:file}} {{note}}")
    assert [(f.name, f.kind, f.default) for f in fs] == [
        ("file", "file", ""), ("language", "text", "Python"), ("config_path", "file", ""), ("x", "file", ""), ("note", "text", "")]


def test_fill_uses_values_then_defaults_and_names_what_is_missing():
    t = "Port {{file}} to {{language|Python}}"
    assert pl.fill(t, {"file": "a.js"}) == "Port a.js to Python"
    assert pl.fill(t, {"file": " a.js ", "language": "Go"}) == "Port a.js to Go"
    with pytest.raises(pl.PromptError, match="file"):
        pl.fill(t, {"language": "Go"})


def test_a_value_that_looks_like_a_field_is_not_filled_again():
    assert pl.fill("{{a}} {{b}}", {"a": "{{b}}", "b": "x"}) == "{{b}} x"


def test_older_single_brace_templates_still_fill():
    assert [f.name for f in pl.parse_fields("Trace {feature} in {path}")] == ["feature", "path"]
    assert pl.fill("Trace {feature}", {"feature": "login"}) == "Trace login"
    assert pl.parse_fields("json {\"a\": 1} and {{x}}")[0].name == "x"      # braces in code are left alone


# ─── storage ─────────────────────────────────────────────────────────────────

def test_starters_exist_and_none_is_a_command_or_skill_name(home, ws):
    names = {p.name for p in pl.list_prompts(ws) if p.scope == "starter"}
    assert len(names) >= 6 and any(f.kind == "file" for p in pl.list_prompts(ws) for f in p.fields)
    assert any(f.name == "language" for p in pl.list_prompts(ws) for f in p.fields)
    assert not [n for n in names if pl.is_reserved(ws, n)]


def test_save_list_get_search_and_project_overrides_global(home, ws):
    pl.save(ws, "Review API", "Review {{file}} for API mistakes", "api review", "global")
    pl.save(ws, "review-api", "Project version of {{file}}", "", "project")
    got = pl.get(ws, "review-api")
    assert got.scope == "project" and got.text.startswith("Project version")
    assert (ws / ".axon" / "prompts" / "review-api.md").is_file() and (home / "prompts" / "review-api.md").is_file()
    assert [p.name for p in pl.search(ws, "api")][0] == "review-api"
    assert pl.search(ws, "zzzz-nothing") == []
    assert {p.name for p in pl.search(ws, "", "project")} == {"review-api"}
    pl.delete(ws, "review-api", "project")
    assert pl.get(ws, "review-api").scope == "global"


def test_edit_renames_and_moves_without_leaving_the_old_file(home, ws):
    pl.save(ws, "one", "text {{a}}", "d", "global")
    p = pl.save(ws, "two", "new text", "d2", "project", replace="one", replace_scope="global")
    assert p.scope == "project" and pl.get(ws, "one") is None and not (home / "prompts" / "one.md").exists()
    with pytest.raises(pl.PromptError, match="already exists"):
        pl.save(ws, "other", "x", "", "project")
        pl.save(ws, "other", "y", "", "project")


def test_editing_a_starter_saves_your_copy_and_deleting_hides_it(home, ws):
    first = next(p for p in pl.list_prompts(ws) if p.scope == "starter")
    pl.save(ws, first.name, "mine {{file}}", "mine", "global", replace=first.name, replace_scope="starter")
    assert pl.get(ws, first.name).scope == "global"
    pl.delete(ws, first.name, "global")
    assert pl.get(ws, first.name).scope == "starter"               # the starter shows again once your copy is gone
    msg = pl.delete(ws, first.name)
    assert "Removed starter" in msg and pl.get(ws, first.name) is None
    assert pl.restore_starters() == 1 and pl.get(ws, first.name) is not None


def test_names_that_are_commands_custom_commands_or_skills_are_refused(home, ws):
    with pytest.raises(pl.PromptError, match="command or skill"):
        pl.save(ws, "review", "x", "", "global")
    with pytest.raises(pl.PromptError, match="command or skill"):
        pl.save(ws, "/commit", "x", "", "global")                   # the slash is ignored, the name is taken
    (ws / ".axon" / "commands").mkdir(parents=True)
    (ws / ".axon" / "commands" / "deployit.md").write_text("Deploy $ARGUMENTS\n", encoding="utf-8")
    with pytest.raises(pl.PromptError, match="command or skill"):
        pl.save(ws, "deployit", "x", "", "global")
    with pytest.raises(pl.PromptError):
        pl.save(ws, "", "x", "", "global")


def test_secrets_empty_and_huge_prompts_are_refused(home, ws):
    with pytest.raises(pl.PromptError, match="secret"):
        pl.save(ws, "keyed", "use key sk-ant-api03-" + "a" * 40, "", "global")
    with pytest.raises(pl.PromptError, match="empty"):
        pl.save(ws, "blank", "   \n", "", "global")
    with pytest.raises(pl.PromptError, match="too long"):
        pl.save(ws, "huge", "x" * (pl.MAX_TEXT + 1), "", "global")
    assert not (home / "prompts").exists() or not list((home / "prompts").glob("*.md"))


def test_old_markdown_prompt_files_and_corrupt_files_are_read(home, ws):
    d = home / "prompts"
    d.mkdir(parents=True)
    (d / "legacy.md").write_text("---\ndescription: Old one\n---\nTrace {feature}\n", encoding="utf-8")
    (d / "broken.md").write_bytes(b"\xff\xfe\x00\x00 not text")
    (d / "empty.md").write_text("", encoding="utf-8")
    got = pl.get(ws, "legacy")
    assert got.description == "Old one" and [f.name for f in got.fields] == ["feature"]
    assert pl.get(ws, "empty") is None


def test_a_saved_prompt_that_is_also_a_command_is_flagged_not_lost(home, ws):
    d = home / "prompts"
    d.mkdir(parents=True)
    (d / "commit.md").write_text("my commit prompt\n", encoding="utf-8")
    p = pl.get(ws, "commit")
    assert p.scope == "global" and p.reserved is True


def test_export_import_round_trip_with_conflicts(home, ws, tmp_path):
    pl.save(ws, "alpha", "A {{file}}", "first", "global")
    pl.save(ws, "beta", "B", "", "project")
    bundle = pl.export_bundle(ws)
    assert bundle["axon_prompts"] == 1 and {p["name"] for p in bundle["prompts"]} == {"alpha", "beta"}
    assert "scope" not in json.dumps(bundle) and str(tmp_path) not in json.dumps(bundle)
    other = tmp_path / "other"
    other.mkdir()
    res = pl.import_bundle(other, json.dumps(bundle), "project")
    assert sorted(res["added"]) == ["alpha", "beta"] and res["skipped"] == []
    again = pl.import_bundle(other, bundle, "project")
    assert again["added"] == [] and len(again["skipped"]) == 2
    ren = pl.import_bundle(other, bundle, "project", "rename")
    assert sorted(ren["added"]) == ["alpha-2", "beta-2"]
    rep = pl.import_bundle(other, {"prompts": [{"name": "alpha", "text": "changed"}]}, "project", "replace")
    assert rep["added"] == ["alpha"] and pl.get(other, "alpha").text == "changed"


def test_import_rejects_junk_and_skips_bad_items_with_reasons(home, ws):
    for bad in ("not json", "{}", json.dumps({"prompts": "x"}), json.dumps({"prompts": {}})):
        with pytest.raises(pl.PromptError):
            pl.import_bundle(ws, bad)
    res = pl.import_bundle(ws, {"prompts": [
        {"name": "ok", "text": "fine"}, {"name": "review", "text": "taken by a command"},
        {"name": "leak", "text": "password = hunter2hunter2"}, "string", {"name": "notext"}]})
    assert res["added"] == ["ok"] and len(res["skipped"]) == 4
    assert any("command or skill" in s["why"] for s in res["skipped"]) and any("secret" in s["why"] for s in res["skipped"])
    with pytest.raises(pl.PromptError):
        pl.import_bundle(ws, {"prompts": [{"name": f"p{i}", "text": "x"} for i in range(pl.MAX_BUNDLE_PROMPTS + 1)]})


# ─── checkpoints on real files ───────────────────────────────────────────────

def _turn(cp: CheckpointManager, files: dict[str, str | None], label: str = "") -> None:
    """One agent turn: capture, write (None deletes), commit. Exactly what the Write/Edit tools do."""
    for p, text in files.items():
        path = Path(p)
        cp.capture_before_edit(path)
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
    cp.commit_turn_snapshot(label=label)
    time.sleep(0.01)


@pytest.fixture
def two_turns(home, ws):
    a, b = ws / "a.txt", ws / "b.txt"
    a.write_text("one\ntwo\nthree\n", encoding="utf-8")
    cp = CheckpointManager(ws, "sess-1")
    _turn(cp, {str(a): "one\nTWO\nthree\nfour\n", str(b): "brand new\n"})      # checkpoint 1: edits a, creates b
    _turn(cp, {str(a): "ONE\nTWO\nthree\nfour\n"})                              # checkpoint 2: edits a again
    return cp, a, b


def test_timeline_has_time_prompt_files_and_counts(two_turns, ws):
    cp, a, b = two_turns
    t0 = cp.history[0].timestamp
    rows = cv.timeline(cp, [(t0 - 5, "first prompt"), (cp.history[1].timestamp - 0.005, "second prompt")], ws)
    assert [r["n"] for r in rows] == [1, 2]
    r1, r2 = rows
    assert r1["prompt"] == "first prompt" and r2["prompt"] == "second prompt" and r1["time"] == t0
    f = {x["rel"]: x for x in r1["files"]}
    assert f["a.txt"]["status"] == "modified" and (f["a.txt"]["added"], f["a.txt"]["removed"]) == (2, 1)
    assert f["b.txt"]["status"] == "new" and f["b.txt"]["added"] == 1 and f["b.txt"]["removed"] == 0
    assert (r1["added"], r1["removed"]) == (3, 1)
    assert [(x["added"], x["removed"]) for x in r2["files"]] == [(1, 1)]        # checkpoint 2 only counts its own turn


def test_checkpoint_diff_shows_what_that_turn_changed(two_turns, ws):
    cp, a, b = two_turns
    d1 = {x["rel"]: x for x in cv.checkpoint_diff(cp, 1, ws)}
    assert {r["text"] for r in d1["a.txt"]["rows"] if r["kind"] == "add"} == {"TWO", "four"}
    assert [r["text"] for r in d1["a.txt"]["rows"] if r["kind"] == "del"] == ["two"]       # not the later ONE edit
    assert d1["b.txt"]["status"] == "new"
    assert [r["text"] for r in cv.checkpoint_diff(cp, 2, ws, "a.txt")[0]["rows"] if r["kind"] == "add"] == ["ONE"]
    with pytest.raises(cv.RestoreError):
        cv.checkpoint_diff(cp, 9, ws)


def test_restore_puts_real_files_back_deletes_created_ones_and_can_be_undone(two_turns, ws):
    cp, a, b = two_turns
    plan = cv.plan_restore(cp, 1, ws)
    assert {f["rel"]: f["action"] for f in plan["files"]} == {"a.txt": "restore", "b.txt": "delete"}
    assert plan["conflicts"] == [] and plan["checkpoints"] == 2
    text = "\n".join(cv.summary_lines(plan))
    assert "a.txt" in text and "b.txt" in text and "deleted" in text
    before = (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8"))
    res = cv.restore(cp, 1, ws)
    assert res["ok"] and sorted(res["restored"]) == ["a.txt"] and res["deleted"] == ["b.txt"]
    assert a.read_text(encoding="utf-8") == "one\ntwo\nthree\n" and not b.exists()
    assert res["checkpoint"] == 3 and cp.list_snapshots()[-1].label.startswith(cv.RESTORE_LABEL)
    assert cv.timeline(cp, None, ws)[-1]["kind"] == "restore"
    # undo the restore: restore to before the restore checkpoint
    undo = cv.restore(cp, res["checkpoint"], ws)
    assert undo["ok"] and (a.read_text(encoding="utf-8"), b.read_text(encoding="utf-8")) == before


def test_restoring_only_the_newest_checkpoint_leaves_older_edits(two_turns, ws):
    cp, a, b = two_turns
    res = cv.restore(cp, 2, ws)
    assert res["restored"] == ["a.txt"] and a.read_text(encoding="utf-8") == "one\nTWO\nthree\nfour\n"
    assert b.read_text(encoding="utf-8") == "brand new\n"


def test_a_file_edited_outside_axon_is_a_conflict_and_nothing_is_overwritten(two_turns, ws):
    cp, a, b = two_turns
    a.write_text("MY OWN EDIT\n", encoding="utf-8")              # the user, in their editor
    plan = cv.plan_restore(cp, 1, ws)
    assert [x["rel"] for x in plan["conflicts"]] == ["a.txt"] and "outside Axon" in plan["conflicts"][0]["why"]
    res = cv.restore(cp, 1, ws)
    assert res["refused"] and not res["ok"] and res["restored"] == [] and res["deleted"] == []
    assert a.read_text(encoding="utf-8") == "MY OWN EDIT\n" and b.exists()       # not even the clean file was touched
    assert len(cp.list_snapshots()) == 2                                          # and no checkpoint was added


def test_keep_mine_restores_the_rest_and_overwrite_is_undoable(two_turns, ws):
    cp, a, b = two_turns
    a.write_text("MY OWN EDIT\n", encoding="utf-8")
    kept = cv.restore(cp, 1, ws, conflicts="keep")
    assert kept["ok"] and kept["kept"] == ["a.txt"] and kept["deleted"] == ["b.txt"]
    assert a.read_text(encoding="utf-8") == "MY OWN EDIT\n" and not b.exists()
    cv.restore(cp, kept["checkpoint"], ws)                       # undo
    assert b.exists()
    a.write_text("EDIT TWO\n", encoding="utf-8")
    forced = cv.restore(cp, 1, ws, conflicts="overwrite")
    assert forced["ok"] and a.read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    cv.restore(cp, forced["checkpoint"], ws)                     # undo brings the user's text back
    assert a.read_text(encoding="utf-8") == "EDIT TWO\n"


def test_a_file_the_user_deleted_is_a_conflict_too(two_turns, ws):
    cp, a, b = two_turns
    b.unlink()
    plan = cv.plan_restore(cp, 1, ws)
    assert [x["rel"] for x in plan["conflicts"]] == []           # b is gone and was going to be deleted anyway
    a.unlink()
    plan = cv.plan_restore(cp, 1, ws)
    assert [x["rel"] for x in plan["conflicts"]] == ["a.txt"] and plan["files"][0]["action"] == "recreate"


def test_conflict_detection_survives_a_reload_of_the_session(home, ws):
    a = ws / "a.txt"
    a.write_text("v0\n", encoding="utf-8")
    cp = CheckpointManager(ws, "sess-reload")
    _turn(cp, {str(a): "v1\n"})
    again = CheckpointManager(ws, "sess-reload")                 # a resumed session reads the saved checkpoints
    assert len(again.history) == 1 and again.history[0].after
    assert cv.plan_restore(again, 1, ws)["conflicts"] == []
    a.write_text("edited elsewhere\n", encoding="utf-8")
    assert [x["rel"] for x in cv.plan_restore(again, 1, ws)["conflicts"]] == ["a.txt"]


def test_old_checkpoints_without_a_record_are_unverified_not_blocking(home, ws):
    a = ws / "a.txt"
    a.write_text("v0\n", encoding="utf-8")
    cp = CheckpointManager(ws, "sess-old")
    _turn(cp, {str(a): "v1\n"})
    cp.history[0].after = {}
    plan = cv.plan_restore(cp, 1, ws)
    assert plan["conflicts"] == [] and [x["rel"] for x in plan["unverified"]] == ["a.txt"]


def test_restore_refuses_mid_turn_and_unknown_checkpoints(two_turns, ws):
    cp, a, b = two_turns
    cp.capture_before_edit(a)                                    # the agent is part way through a turn
    with pytest.raises(cv.RestoreError, match="still working"):
        cv.restore(cp, 1, ws)
    cp._pending_files.clear()
    for n in (0, 3, -1):
        with pytest.raises(cv.RestoreError):
            cv.plan_restore(cp, n, ws)


def test_files_outside_the_project_are_never_touched(home, ws, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep\n", encoding="utf-8")
    cp = CheckpointManager(ws, "sess-out")
    _turn(cp, {str(outside): "agent edit\n"})
    plan = cv.plan_restore(cp, 1, ws)
    assert plan["files"] == [] and plan["skipped"][0]["why"] == "outside this project"
    assert cv.restore(cp, 1, ws)["restored"] == [] and outside.read_text(encoding="utf-8") == "agent edit\n"


def test_binary_and_crlf_files_round_trip_exactly(home, ws):
    f, g = ws / "img.bin", ws / "win.txt"
    f.write_bytes(b"\x00\x01\xff\xfe")
    g.write_bytes(b"a\r\nb\r\n")
    cp = CheckpointManager(ws, "sess-bin")
    for p, data in ((f, b"\x00\x02"), (g, b"a\r\nc\r\n")):
        cp.capture_before_edit(p)
        p.write_bytes(data)
    cp.commit_turn_snapshot()
    assert cv.timeline(cp, None, ws)[0]["files"][0]["binary"] is True
    cv.restore(cp, 1, ws)
    assert f.read_bytes() == b"\x00\x01\xff\xfe" and g.read_bytes() == b"a\r\nb\r\n"


# ─── the routes ──────────────────────────────────────────────────────────────

def _body(r):
    return json.loads(r[2])


def test_prompt_routes_save_list_edit_delete_export_import(c, env):
    st = c.post("/api/library/prompt_save", {"name": "Fix Login", "text": "Fix {{file}}", "description": "d", "scope": "project"})
    assert st[0] == 200 and _body(st)["prompt"]["name"] == "fix-login"
    rows = c.json("/api/library/prompts?q=login")["prompts"]
    assert rows[0]["name"] == "fix-login" and rows[0]["fields"][0] == {"name": "file", "kind": "file", "default": ""}
    assert (env.ws / ".axon" / "prompts" / "fix-login.md").is_file()
    ed = c.post("/api/library/prompt_save", {"name": "fix-signup", "text": "Fix {{file}} now", "scope": "global", "replace": "fix-login", "replace_scope": "project"})
    assert ed[0] == 200 and not (env.ws / ".axon" / "prompts" / "fix-login.md").exists()
    export = c.json("/api/library/export")
    assert [p["name"] for p in export["prompts"]] == ["fix-signup"]
    assert c.post("/api/library/prompt_delete", {"name": "fix-signup", "scope": "global"})[0] == 200
    imp = _body(c.post("/api/library/prompts_import", {"data": export, "scope": "project"}))
    assert imp["added"] == ["fix-signup"]
    assert c.post("/api/library/prompt_delete", {"name": "nope"})[0] == 400


def test_prompt_routes_refuse_bad_input(c):
    assert c.post("/api/library/prompt_save", {"name": "review", "text": "x"})[0] == 400
    assert c.post("/api/library/prompt_save", {"name": "k", "text": "token ghp_" + "a" * 40})[0] == 400
    assert c.post("/api/library/prompt_save", {"name": 5, "text": "x"})[0] == 400
    assert c.post("/api/library/prompt_save", {"name": "ok", "text": "x", "scope": "../../etc"})[0] == 400
    assert c.post("/api/library/prompts_import", {"data": 5})[0] == 400
    assert c.post("/api/library/prompt_save", {"name": "ok2", "text": "x"}, origin="http://evil.example")[0] == 403
    assert c.post("/api/library/prompt_save", {"name": "ok3", "text": "x"}, cookie=False)[0] == 401
    assert c.get("/api/library/nothing")[0] == 404


def test_checkpoint_routes_list_diff_plan_and_restore(c, server, env):
    st = c.json("/api/chat/state?session=&create=1")
    rt = server.runtime("", create=True)
    cp = rt.agent.checkpoints
    f = env.ws / "work.txt"
    f.write_text("before\n", encoding="utf-8")
    _turn(cp, {str(f): "after\nmore\n"})
    sid = st["session"]
    tl = c.json("/api/library/checkpoints?session=" + sid)
    assert tl["busy"] is False and tl["checkpoints"][0]["files"][0]["rel"] == "work.txt"
    assert (tl["checkpoints"][0]["added"], tl["checkpoints"][0]["removed"]) == (2, 1)
    d = c.json(f"/api/library/checkpoint_diff?session={sid}&n=1")
    assert d["files"][0]["rows"]
    plan = c.json(f"/api/library/restore_plan?session={sid}&n=1")
    assert plan["files"][0]["action"] == "restore" and "work.txt" in plan["lines"][0]
    f.write_text("typed in my editor\n", encoding="utf-8")
    refused = _body(c.post("/api/library/restore", {"session": sid, "n": 1}))
    assert refused["refused"] and f.read_text(encoding="utf-8") == "typed in my editor\n"
    ok = _body(c.post("/api/library/restore", {"session": sid, "n": 1, "conflicts": "overwrite"}))
    assert ok["ok"] and f.read_text(encoding="utf-8") == "before\n"
    back = _body(c.post("/api/library/restore", {"session": sid, "n": ok["checkpoint"]}))
    assert back["ok"] and f.read_text(encoding="utf-8") == "typed in my editor\n"
    assert c.post("/api/library/restore", {"session": sid, "n": 99})[0] == 400
    assert c.post("/api/library/restore", {"session": sid, "n": "x"})[0] == 400
    assert c.post("/api/library/restore", {"session": sid, "n": 1, "conflicts": "weird"})[0] == 400


def test_checkpoint_routes_without_a_chat_are_empty_not_errors(c):
    assert c.json("/api/library/checkpoints?session=nonexistent")["checkpoints"] == []
    assert c.post("/api/library/restore", {"session": "nonexistent", "n": 1})[0] == 404


# ─── the terminal: /rewind and /prompt ───────────────────────────────────────

import re as _re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

ANSI = _re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _agent(ws, cp, prompts=()):
    msgs = [{"role": "user", "content": p} for p in prompts]
    return SimpleNamespace(settings=SimpleNamespace(workspace=ws), conversation=SimpleNamespace(messages=msgs), session=None,
                           traces=[], checkpoints=cp, file_state=SimpleNamespace(clear=lambda: None, invalidate=lambda p: None),
                           run_turn=lambda text: ran.append(text))


ran: list[str] = []


def _out(capsys):
    return ANSI.sub("", capsys.readouterr().out)


def test_rewind_list_shows_time_files_and_counts_and_show_prints_the_diff(two_turns, ws, capsys):
    from axon.commands.session_cmds import handle_rewind
    cp, a, b = two_turns
    handle_rewind(_agent(ws, cp), "list")
    out = _out(capsys)
    assert "Checkpoints" in out and "2 file(s)" in out and "a.txt" in out and "+3" in out and "-1" in out
    handle_rewind(_agent(ws, cp), "show 1")
    out = _out(capsys)
    assert "a.txt" in out and "four" in out and "b.txt" in out


def test_rewind_restore_needs_yes_refuses_conflicts_and_restores(two_turns, ws, capsys, monkeypatch):
    from axon.commands.session_cmds import handle_rewind
    monkeypatch.setattr("axon.commands.session_cmds.is_tty", lambda: False)
    cp, a, b = two_turns
    ag = _agent(ws, cp)
    handle_rewind(ag, "restore 1")
    assert "Add --yes" in _out(capsys) and b.exists()                 # nothing happens without --yes
    a.write_text("mine\n", encoding="utf-8")
    handle_rewind(ag, "restore 1 --yes")
    out = _out(capsys)
    assert "changed outside Axon" in out and a.read_text(encoding="utf-8") == "mine\n" and b.exists()
    handle_rewind(ag, "restore 1 --yes --keep-mine")
    assert a.read_text(encoding="utf-8") == "mine\n" and not b.exists()
    assert "Undo it by restoring checkpoint 3" in _out(capsys)
    handle_rewind(ag, "restore 3 --yes")
    assert b.exists()


def test_typed_rewind_n_and_rewind_to_refuse_to_overwrite_outside_edits(home, ws, capsys, monkeypatch):
    from axon.commands.session_cmds import handle_rewind
    monkeypatch.setattr("axon.commands.session_cmds.is_tty", lambda: False)
    f = ws / "f.txt"
    f.write_text("v0\n", encoding="utf-8")
    cp = CheckpointManager(ws, "sess-term")
    _turn(cp, {str(f): "v1\n"})
    ag = _agent(ws, cp, ["edit f"])
    f.write_text("hand edit\n", encoding="utf-8")
    handle_rewind(ag, "1")
    assert "changed outside Axon" in _out(capsys) and f.read_text(encoding="utf-8") == "hand edit\n"
    handle_rewind(ag, "1 --overwrite")
    assert f.read_text(encoding="utf-8") == "v0\n"


def test_prompt_command_list_show_save_use_delete_export_import(home, ws, capsys, monkeypatch):
    from axon.commands.memory_cmds import handle_prompt
    monkeypatch.setattr("axon.commands.memory_cmds.is_tty", lambda: False)
    ran.clear()
    ag = _agent(ws, None)
    handle_prompt(ag, "")
    assert "write-tests" in _out(capsys)
    handle_prompt(ag, 'save greet -d "Say hi" Greet {{who}} in {{language|English}}')
    assert "Saved prompt" in _out(capsys) and (ws / ".axon" / "prompts" / "greet.md").is_file()
    handle_prompt(ag, "show greet")
    assert "Say hi" in _out(capsys)
    handle_prompt(ag, "greet who=Ada")
    assert ran == ["Greet Ada in English"]
    handle_prompt(ag, "greet Grace")                                   # one gap: free words fill it
    assert ran[-1] == "Greet Grace in English"
    handle_prompt(ag, "greet")
    assert "Fill in: who" in _out(capsys) and len(ran) == 2
    handle_prompt(ag, "save review nope")                              # a command name
    assert "command or skill" in _out(capsys) and not (ws / ".axon" / "prompts" / "review.md").exists()
    handle_prompt(ag, "export out.json")
    assert json.loads((ws / "out.json").read_text(encoding="utf-8"))["prompts"][0]["name"] == "greet"
    monkeypatch.setattr("axon.commands.memory_cmds.confirm", lambda *a, **k: True)
    handle_prompt(ag, "delete greet")
    assert not (ws / ".axon" / "prompts" / "greet.md").exists()
    handle_prompt(ag, "import out.json --global")
    assert (home / "prompts" / "greet.md").is_file()
    handle_prompt(ag, "delete write-tests")
    assert "Removed starter" in _out(capsys)
    handle_prompt(ag, "restore-starters")
    assert "Restored 1" in _out(capsys)


def test_prompt_fill_route_uses_the_same_filler_and_validates(c):
    c.post("/api/library/prompt_save", {"name": "port", "text": "Port {{file}} to {{language|Go}}", "scope": "project"})
    ok = _body(c.post("/api/library/prompt_fill", {"name": "port", "values": {"file": "src/a.py"}}))
    assert ok["text"] == "Port src/a.py to Go"
    assert c.post("/api/library/prompt_fill", {"name": "port", "values": {}})[0] == 400
    assert c.post("/api/library/prompt_fill", {"name": "port", "values": {"file": 5}})[0] == 400
    assert c.post("/api/library/prompt_fill", {"name": "missing", "values": {}})[0] == 404


def test_skill_names_are_reserved_too(home, ws):
    sk = ws / ".axon" / "skills" / "polisher"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: polisher\ndescription: polish things\n---\nDo it.\n", encoding="utf-8")
    assert pl.is_reserved(ws, "polisher")
    with pytest.raises(pl.PromptError, match="command or skill"):
        pl.save(ws, "polisher", "x", "", "global")


def test_the_page_script_has_the_library_and_checkpoint_pieces_and_no_innerhtml():
    from axon.features.dash_js_library import JS_LIBRARY
    from axon.features.dashboard_web import APP_JS, asset
    for name in ("axPromptLibrary", "axPromptItems", "axPromptSlash", "axCheckpoints", "/api/library/restore_plan", "/api/library/restore"):
        assert name in APP_JS
    assert "innerHTML" not in JS_LIBRARY and "html:" not in JS_LIBRARY
    assert ".cp-sec" in asset("app.css")[0]
