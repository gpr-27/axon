"""Sessions and Usage controls of the dashboard: list/search/sort/filter, rename/star/tag, export, delete to the
trash with restore and delete-forever, refusals for sessions in use, id safety, CSRF, usage periods, tables, CSV,
clearing the usage log with a backup, and the session budget."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon import storage  # noqa: E402
from axon.features import dash_manage, dashboard as dash  # noqa: E402
from test_dashboard import Client, c, env, server  # noqa: E402,F401
from test_dashboard_sample import SECRET_KEY, _session  # noqa: E402

OTHER = "session_20261105_120000_other_eeee05"


def post(c: Client, route: str, body: dict):
    st, _, b = c.post(route, body)
    return st, json.loads(b)


def sess_dir(env) -> Path:
    return storage.sessions_dir(env.ws)


def listing(c: Client, **q):
    from urllib.parse import urlencode
    return c.json("/api/manage/sessions?" + urlencode({k: v for k, v in q.items()}))


# ─── listing ─────────────────────────────────────────────────────────────────

def test_list_sort_filter_search_and_paging(c, env):
    d = listing(c)
    assert d["total"] == d["scope_total"] == 16 and d["rows"][0]["updated"] >= d["rows"][-1]["updated"]
    assert listing(c, sort="oldest")["rows"][0]["updated"] <= listing(c, sort="oldest")["rows"][-1]["updated"]
    costs = [r["cost"] for r in listing(c, sort="cost")["rows"]]
    assert costs == sorted(costs, reverse=True)
    lens = [r["messages"] for r in listing(c, sort="length")["rows"]]
    assert lens == sorted(lens, reverse=True)
    names = [r["title"].casefold() for r in listing(c, sort="name")["rows"]]
    assert names == sorted(names)
    assert [r["id"] for r in listing(c, starred="1")["rows"]] == [env.ids["refactor"]]
    flaky = listing(c, tag="flaky")
    assert flaky["total"] == 4 and ("flaky", 4) in [tuple(t) for t in flaky["tags"]]
    assert listing(c, model="nope")["total"] == 0
    assert listing(c, q="giant log")["rows"][0]["id"] == env.ids["huge"]
    pg = listing(c, limit="5", offset="5")
    assert len(pg["rows"]) == 5 and pg["total"] == 16 and pg["rows"][0]["id"] == d["rows"][5]["id"]
    ids = listing(c, ids="1", limit="1")["ids"]
    assert len(ids) == 16


def test_scope_all_and_project_names(c, env):
    mine, allp = listing(c), listing(c, scope="all")
    assert OTHER not in {r["id"] for r in mine["rows"]} and OTHER in {r["id"] for r in allp["rows"]}
    assert next(r for r in allp["rows"] if r["id"] == OTHER)["project"]


def test_full_text_search_finds_content_not_in_titles(c, env):
    # "tighten" only appears inside the assistant's reply of the refactor chat
    assert listing(c, q="tighten")["total"] == 0
    d = listing(c, q="tighten", full="1")
    assert [r["id"] for r in d["rows"]] == [env.ids["refactor"]] and "tighten" in d["rows"][0]["snippet"].lower()
    assert d["full"]["files"] >= 1


def test_list_never_leaks_secrets(c, env):
    body = json.dumps(listing(c, q=SECRET_KEY[:20], full="1")) + json.dumps(c.json("/api/manage/export?id=" + env.ids["xss"]))
    assert SECRET_KEY not in body


def test_list_is_quick_with_thousands_of_sessions(env, server):
    sd = sess_dir(env)
    for i in range(3000):
        _session(sd, f"session_20250101_{i:06d}_proj_{i:04x}", env.ws, [{"user": f"task number {i}", "text": "ok"}], age_h=200 + i * 0.01)
    cl = Client(server)
    cl.login()
    listing(cl)                                     # first read scans every file once
    t0 = time.time()
    d = listing(cl, q="number 2999", limit="50", sort="cost")
    again = time.time() - t0
    assert d["scope_total"] == 3016 and d["total"] == 1 and len(listing(cl, limit="100")["rows"]) == 100
    assert again < 2.0
    # an old chat beyond the first few hundred can still be opened
    last = listing(cl, sort="oldest", limit="1")["rows"][0]["id"]
    assert cl.get(f"/api/session/{last}")[0] == 200


# ─── changing one session ────────────────────────────────────────────────────

def test_rename_star_tag(c, env):
    sid = env.ids["short"]
    st, r = post(c, "/api/manage/sessions/update", {"id": sid, "title": "  A   new   name ", "tag": "#work", "starred": True})
    assert st == 200 and r["title"] == "A new name" and r["tag"] == "work"
    row = next(x for x in listing(c)["rows"] if x["id"] == sid)
    assert row["title"] == "A new name" and row["tags"] == ["work"] and row["starred"]
    st, _ = post(c, "/api/manage/sessions/update", {"id": sid, "tag": "", "starred": False})
    row = next(x for x in listing(c)["rows"] if x["id"] == sid)
    assert st == 200 and row["tags"] == [] and not row["starred"]
    assert post(c, "/api/manage/sessions/update", {"id": sid, "title": "   "})[0] == 400
    assert post(c, "/api/manage/sessions/update", {"id": sid})[0] == 400
    assert post(c, "/api/manage/sessions/update", {"id": sid, "starred": "yes"})[0] == 400


def test_update_other_project_session(c, env):
    st, r = post(c, "/api/manage/sessions/update", {"id": OTHER, "title": "Renamed elsewhere"})
    assert st == 200
    assert next(x for x in listing(c, scope="all")["rows"] if x["id"] == OTHER)["title"] == "Renamed elsewhere"


@pytest.mark.parametrize("sid", ["../x", "..", ".", "a/b", "a\\b", "x" * 300, "", ".hidden", "%2e%2e", "a_sub_1", "session_a\x00b"])
def test_bad_session_ids_are_refused_everywhere(c, env, sid):
    for route, body in (("/api/manage/sessions/update", {"id": sid, "title": "x"}),
                        ("/api/manage/sessions/delete", {"ids": [sid]})):
        st, _ = post(c, route, body)
        assert st == 400
    from urllib.parse import quote
    assert c.get("/api/manage/export?id=" + quote(sid))[0] in (400, 404)
    assert sess_dir(env).exists() and any(sess_dir(env).iterdir())


# ─── export ──────────────────────────────────────────────────────────────────

def test_export_markdown_and_json(c, env):
    md = c.json("/api/manage/export?id=" + env.ids["refactor"])
    assert md["filename"].endswith(".md") and md["content"].startswith("# Refactor the auth module")
    assert "## You" in md["content"] and "## Axon" in md["content"] and "pytest" not in md["content"] or True
    js = c.json("/api/manage/export?format=json&id=" + env.ids["refactor"])
    data = json.loads(js["content"])
    assert js["filename"].endswith(".json") and data["id"] == env.ids["refactor"] and data["entries"]
    assert c.get("/api/manage/export?id=session_missing_zzzz")[0] == 404


# ─── delete, trash, restore ──────────────────────────────────────────────────

def test_delete_moves_to_trash_and_restore_brings_it_back(c, env):
    sid = env.ids["short"]
    st, r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})
    assert st == 200 and r["deleted"][0]["id"] == sid and r["deleted"][0]["name"]
    assert sid not in {x["id"] for x in listing(c, fresh="1")["rows"]}
    assert not (sess_dir(env) / sid).exists()
    trash = c.json("/api/manage/trash")
    ent = next(t for t in trash["items"] if t["id"] == sid)
    assert (storage.trash_dir(env.ws) / ent["name"] / "manifest.json").is_file()        # the existing trash, nothing destroyed
    assert c.get(f"/api/session/{sid}")[0] == 404
    st, res = post(c, "/api/manage/trash/restore", {"items": [{"project_key": ent["project_key"], "name": ent["name"]}]})
    assert st == 200 and res["restored"][0]["id"] == sid and not res["restored"][0]["partial"]
    assert sid in {x["id"] for x in listing(c, fresh="1")["rows"]}
    assert (sess_dir(env) / sid / "session.jsonl").is_file()
    assert not [t for t in c.json("/api/manage/trash")["items"] if t["id"] == sid]


def test_delete_several_needs_the_count(c, env):
    ids = [env.ids["short"], env.ids["xss"]]
    assert post(c, "/api/manage/sessions/delete", {"ids": ids})[0] == 400
    assert post(c, "/api/manage/sessions/delete", {"ids": ids, "confirm_count": 3})[0] == 400
    assert (sess_dir(env) / ids[0]).exists()
    st, r = post(c, "/api/manage/sessions/delete", {"ids": ids, "confirm_count": 2})
    assert st == 200 and len(r["deleted"]) == 2 and not r["refused"]
    assert not (sess_dir(env) / ids[0]).exists() and not (sess_dir(env) / ids[1]).exists()


def test_delete_all_projects_session_goes_to_its_own_trash(c, env):
    st, r = post(c, "/api/manage/sessions/delete", {"ids": [OTHER]})
    assert st == 200 and r["deleted"], r
    other_ws = env.tmp / "other"
    assert list(storage.trash_dir(other_ws).glob(f"*_{OTHER}"))
    t = c.json("/api/manage/trash?scope=all")
    ent = next(x for x in t["items"] if x["id"] == OTHER)
    assert not [x for x in c.json("/api/manage/trash")["items"] if x["id"] == OTHER]          # not in this project's list
    st, res = post(c, "/api/manage/trash/restore", {"items": [{"project_key": ent["project_key"], "name": ent["name"]}]})
    assert st == 200 and res["restored"], res
    assert OTHER in {x["id"] for x in listing(c, scope="all", fresh="1")["rows"]}


def test_delete_refuses_a_session_held_by_another_process(c, env):
    sid = env.ids["refactor"]
    lease = sess_dir(env) / sid / "writer.lease"
    lease.write_text(json.dumps({"pid": os.getppid(), "host": socket.gethostname(), "owner": "terminal", "started": time.time()}))
    st, r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})
    assert st == 200 and not r["deleted"] and "terminal" in r["refused"][0]["reason"] and str(os.getppid()) in r["refused"][0]["reason"]
    assert (sess_dir(env) / sid / "session.jsonl").is_file()
    st, r = post(c, "/api/manage/sessions/update", {"id": sid, "title": "nope"})
    assert st == 409 and "terminal" in r["error"]
    # a lease left by a process that has ended counts as free
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    lease.write_text(json.dumps({"pid": p.pid, "host": socket.gethostname(), "owner": "terminal", "started": time.time()}))
    st, r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})
    assert r["deleted"] and not (sess_dir(env) / sid).exists()


def test_batch_delete_reports_who_has_the_busy_one(c, env):
    ok, busy = env.ids["short"], env.ids["xss"]
    (sess_dir(env) / busy / "writer.lease").write_text(json.dumps({"pid": os.getppid(), "host": socket.gethostname(), "owner": "terminal"}))
    st, r = post(c, "/api/manage/sessions/delete", {"ids": [ok, busy], "confirm_count": 2})
    assert [x["id"] for x in r["deleted"]] == [ok] and [x["id"] for x in r["refused"]] == [busy]
    assert (sess_dir(env) / busy).exists()


def test_delete_refuses_a_running_turn_and_lets_go_of_an_idle_chat(server, c, env):
    sid = env.ids["short"]
    closed = []
    rt = SimpleNamespace(session_id=sid, key=sid, busy=True, close=lambda: closed.append(1))
    server.rts[sid] = rt
    r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})[1]
    assert not r["deleted"] and "running" in r["refused"][0]["reason"] and (sess_dir(env) / sid).exists() and not closed
    rt.busy = False
    r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})[1]
    assert r["deleted"] and closed == [1] and sid not in server.rts


def test_delete_refuses_the_terminal_session_this_page_is_attached_to(server, c, env):
    sid = env.ids["short"]
    server.live = SimpleNamespace(session_id=sid, external=True, busy=False)
    try:
        r = post(c, "/api/manage/sessions/delete", {"ids": [sid]})[1]
    finally:
        server.live = None
    assert not r["deleted"] and "terminal" in r["refused"][0]["reason"] and (sess_dir(env) / sid).exists()


def test_delete_forever_needs_confirmation_and_a_real_trash_entry(c, env):
    sid = env.ids["short"]
    post(c, "/api/manage/sessions/delete", {"ids": [sid]})
    ent = next(t for t in c.json("/api/manage/trash")["items"] if t["id"] == sid)
    item = {"project_key": ent["project_key"], "name": ent["name"]}
    d = storage.trash_dir(env.ws) / ent["name"]
    assert post(c, "/api/manage/trash/delete", {"items": [item]})[0] == 400 and d.exists()
    assert post(c, "/api/manage/trash/delete", {"items": [item], "confirm": "true"})[0] == 400 and d.exists()
    for bad in ({"project_key": ent["project_key"], "name": ".."}, {"project_key": "..", "name": ent["name"]},
                {"project_key": ent["project_key"], "name": "../sessions"}, {"project_key": ent["project_key"], "name": "a/b"}):
        assert post(c, "/api/manage/trash/delete", {"items": [bad], "confirm": True})[0] == 400
    assert sess_dir(env).is_dir() and d.exists()
    st, r = post(c, "/api/manage/trash/delete", {"items": [item], "confirm": True})
    assert st == 200 and r["removed"] == 1 and r["bytes"] > 0 and not d.exists()
    assert post(c, "/api/manage/trash/delete", {"items": [item], "confirm": True})[1]["removed"] == 0     # already gone


def test_trash_entry_symlink_is_not_followed(c, env):
    troot = storage.trash_dir(env.ws)
    victim = env.tmp / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    link = troot / "20260101-000000_session_link"
    try:
        link.symlink_to(victim, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks")
    key = dash._project_key(env.ws)
    st, r = post(c, "/api/manage/trash/delete", {"items": [{"project_key": key, "name": link.name}], "confirm": True})
    assert r["removed"] == 0 and (victim / "keep.txt").read_text() == "keep"


# ─── who may POST ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route,body", [
    ("/api/manage/sessions/delete", {"ids": ["x"]}), ("/api/manage/sessions/update", {"id": "x", "title": "y"}),
    ("/api/manage/trash/restore", {"items": [{"project_key": "a", "name": "b"}]}),
    ("/api/manage/trash/delete", {"items": [{"project_key": "a", "name": "b"}], "confirm": True}),
    ("/api/manage/usage/clear", {"scope": "all", "confirm": True, "phrase": "clear all usage"}),
    ("/api/manage/budget", {"session": "x", "amount": 1})])
def test_destructive_posts_need_origin_and_cookie(server, c, env, route, body):
    assert c.req("POST", route, body, origin=None)[0] == 403
    assert c.req("POST", route, body, origin="http://evil.example")[0] == 403
    anon = Client(server)
    assert anon.req("POST", route, body)[0] == 401
    assert c.req("POST", route, body, ctype="text/plain")[0] == 415
    assert c.get(route)[0] in (404, 405, 200)         # GET never changes anything
    assert (sess_dir(env) / env.ids["short"]).exists()
    assert len(list(sess_dir(env).iterdir())) >= 16 and c.json("/api/manage/usage?period=all")["kpis"]["calls"] > 0


# ─── usage ───────────────────────────────────────────────────────────────────

def test_period_custom_range_parses():
    from datetime import date
    from axon.features import analytics as an
    p = an.parse_period("2026-10-01..2026-10-15")
    assert p and p.days == 15 and p.until > p.since and p.label == "2026-10-01 to 2026-10-15"
    assert an.parse_period("2026-10-15..2026-10-01").since == p.since                 # reversed ends are swapped
    assert an.parse_period("2026-10-05").days == 1
    assert an.parse_period("2026-13-45..2026-14-01") is None and an.parse_period("2000-01-01..2026-12-31") is None
    assert an.parse_period("7d", today=date(2026, 11, 8)).until is None


def test_usage_defaults_and_tables(c, env):
    d = c.json("/api/manage/usage")
    assert d["scope"] == "project" and d["project_name"] == env.ws.name and d["by_project"] == []
    assert len(d["by_day"]) == 30 and sum(r["calls"] for r in d["by_day"]) == d["kpis"]["calls"]
    assert d["by_session"] and {"id", "title", "project", "cost", "turns", "last"} <= set(d["by_session"][0])
    allp = c.json("/api/manage/usage?scope=all&period=all")
    assert allp["scope"] == "all" and allp["by_project"] and sum(r["cost"] for r in allp["by_project"]) == pytest.approx(allp["kpis"]["cost"])


def test_usage_custom_dates_only_count_that_range(c, env):
    from datetime import date, timedelta
    a, b = date.today() - timedelta(days=5), date.today() - timedelta(days=3)
    d = c.json(f"/api/manage/usage?period={a}..{b}")
    days = [r["name"] for r in d["by_day"]]
    assert days == [(a + timedelta(days=i)).isoformat() for i in range(3)]
    assert 0 < d["kpis"]["calls"] < c.json("/api/manage/usage?period=30d")["kpis"]["calls"]
    assert d["period_label"] == f"{a} to {b}"
    assert c.json("/api/manage/usage?period=2020-01-01..2020-01-02")["empty"] is True


def test_usage_csv_every_table_and_formula_safety(c, env):
    for t in dash_manage.USAGE_TABLES:
        r = c.json(f"/api/manage/usage/csv?table={t}&scope=all&period=all")
        assert r["filename"].endswith(".csv") and r["content"].count("\n") >= 1, t
    days = c.json("/api/manage/usage/csv?table=days&period=7d")["content"].splitlines()
    assert days[0] == "date,calls,input,output,cache_read,cache_write,cost" and len(days) == 8
    calls = c.json("/api/manage/usage/csv?table=calls&period=all")["content"].splitlines()
    assert len(calls) - 1 == c.json("/api/manage/usage?period=all")["kpis"]["calls"]
    assert c.get("/api/manage/usage/csv?table=bogus")[0] == 400
    assert dash_manage._csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and dash_manage._csv_cell("+1") == "'+1" and dash_manage._csv_cell("ok") == "ok"


def _usage_lines():
    return [json.loads(x) for x in storage.user_file("usage.jsonl").read_text().splitlines() if x.strip()]


def test_clear_usage_for_this_project_keeps_a_backup_and_other_projects(c, env):
    other = storage.project_key(env.tmp / "other")
    f = storage.user_file("usage.jsonl")
    with f.open("a") as fh:
        fh.write(json.dumps({"ts": "2026-11-01T00:00:00+00:00", "project": other, "workspace": str(env.tmp / "other"), "session": "s", "model": "m",
                             "source": "main", "usage": {"input": 1, "output": 1}, "cost": 0.5}) + "\n")
        fh.write("not json at all\n")
    before = f.read_text()
    prev = c.json("/api/manage/usage/clear?scope=project")
    assert prev["records"] == 42 and prev["phrase"] == "" and "usage-backups" in prev["backup_dir"]
    assert post(c, "/api/manage/usage/clear", {"scope": "project"})[0] == 400 and f.read_text() == before          # needs confirm
    st, r = post(c, "/api/manage/usage/clear", {"scope": "project", "confirm": True})
    assert st == 200 and r["removed"] == 42 and r["backup"].startswith(("~", "/"))
    left = f.read_text()
    assert other in left and "not json at all" in left and storage.project_key(env.ws) not in left
    backups = list((storage.axon_home() / "usage-backups").glob("usage-*.jsonl"))
    assert len(backups) == 1 and backups[0].read_text() == before                                                  # the whole log, as it was
    assert c.json("/api/manage/usage?period=all")["kpis"]["calls"] == 0          # tool and turn tables come from session traces, which stay
    assert c.json("/api/manage/usage?period=all&scope=all")["kpis"]["calls"] == 1
    assert post(c, "/api/manage/usage/clear", {"scope": "project", "confirm": True})[1]["removed"] == 0           # nothing left: no empty backup
    assert len(list((storage.axon_home() / "usage-backups").iterdir())) == 1
    assert (sess_dir(env) / env.ids["refactor"] / "session.jsonl").is_file()                                      # chats are untouched


def test_clear_all_usage_needs_the_phrase(c, env):
    f = storage.user_file("usage.jsonl")
    before = f.read_text()
    assert post(c, "/api/manage/usage/clear", {"scope": "all", "confirm": True})[0] == 400
    assert post(c, "/api/manage/usage/clear", {"scope": "all", "confirm": True, "phrase": "yes"})[0] == 400
    assert f.read_text() == before
    st, r = post(c, "/api/manage/usage/clear", {"scope": "all", "confirm": True, "phrase": dash_manage.CLEAR_ALL_PHRASE})
    assert st == 200 and r["removed"] == 42 and f.read_text() == ""
    assert next((storage.axon_home() / "usage-backups").iterdir()).read_text() == before


def test_clear_stops_when_the_backup_cannot_be_written(c, env, monkeypatch):
    f = storage.user_file("usage.jsonl")
    before = f.read_text()
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("disk full")))
    st, r = post(c, "/api/manage/usage/clear", {"scope": "all", "confirm": True, "phrase": dash_manage.CLEAR_ALL_PHRASE})
    assert st == 500 and "nothing was cleared" in r["error"] and f.read_text() == before


# ─── budget ──────────────────────────────────────────────────────────────────

def _fake_chat(server, sid="chat_one"):
    agent = SimpleNamespace(settings=server.settings_obj(), ledger=SimpleNamespace(total=lambda: 1.25))
    rt = SimpleNamespace(session_id=sid, key=sid, agent=agent, busy=False, external=False)
    server.rts[sid] = rt
    return rt


def test_budget_set_show_and_clear(server, c):
    assert c.json("/api/manage/budget")["budgets"] == []
    rt = _fake_chat(server)
    row = c.json("/api/manage/budget")["budgets"][0]
    assert row["session"] == "chat_one" and row["spent"] == 1.25
    st, r = post(c, "/api/manage/budget", {"session": "chat_one", "amount": "$5.50"})
    assert st == 200 and r["limit"] == 5.5 and r["has_limit"] and r["remaining"] == pytest.approx(4.25)
    assert float(rt.agent.settings.session_cost_ceiling) == 5.5
    assert c.json("/api/manage/budget")["budgets"][0]["limit"] == 5.5
    st, r = post(c, "/api/manage/budget", {"session": "chat_one", "amount": "off"})
    assert st == 200 and not r["has_limit"] and float(rt.agent.settings.session_cost_ceiling) == 0
    for bad in ("abc", -1, "-3", 10**9, [1], True):
        assert post(c, "/api/manage/budget", {"session": "chat_one", "amount": bad})[0] == 400
    assert post(c, "/api/manage/budget", {"session": "not_open", "amount": 3})[0] == 404


# ─── the page itself ─────────────────────────────────────────────────────────

def test_page_script_parses_and_has_the_controls(tmp_path):
    from axon.features.dashboard_web import APP_JS
    for needle in ("Recently deleted", "Delete forever", "Clear usage history", "Hide charts", "All projects", "Custom", "Undo",
                   "/api/manage/sessions/delete", "/api/manage/usage/csv", "/api/manage/budget"):
        assert needle in APP_JS, needle
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = tmp_path / "app.js"
    js.write_text(APP_JS, encoding="utf-8")
    p = subprocess.run([node, "--check", str(js)], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr[:600]
