"""First-run welcome, starter prompts, "?" help, the project overview and recent projects.

The data module (axon.features.project_overview) is tested on its own; the routes through the real dashboard server;
the page script is checked for the hooks and parsed with node when it is installed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from axon import storage  # noqa: E402
from axon.features import project_overview as po  # noqa: E402
from test_dashboard import c, env, server  # noqa: E402,F401

GIT = shutil.which("git")


def git(ws: Path, *args: str) -> None:
    subprocess.run([GIT, "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=ws, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "axhome"
    monkeypatch.setenv("AXON_HOME", str(h))
    return h


def make_project(root: Path, name: str = "app", repo: bool = False) -> Path:
    ws = root / name
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "core.py").write_text("def run(x):\n    return x * 2\n" * 40, encoding="utf-8")
    (ws / "src" / "util.py").write_text("def helper(x):\n    return x + 1\n" * 30, encoding="utf-8")
    (ws / "tests" / "test_util.py").write_text("from src.util import helper\n\n\ndef test_h():\n    assert helper(1) == 2\n" + "# pad\n" * 40, encoding="utf-8")
    (ws / "README.md").write_text("# app\n", encoding="utf-8")
    if repo:
        git(ws, "init", "-q")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "first commit")
    return ws


# ─── starter prompts ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not GIT, reason="git is not installed")
def test_starters_come_from_the_real_project(home, tmp_path):
    ws = make_project(tmp_path, repo=True)
    (ws / "src" / "core.py").write_text("# changed\n" + "def run(x):\n    return x * 3\n" * 40, encoding="utf-8")        # a real uncommitted change
    prompts = po.starter_prompts(ws)
    ids = [p["id"] for p in prompts]
    assert ids[0] == "explain" and len(prompts) <= 4
    assert "recent" in ids and "bugs" in ids
    target = next(p for p in prompts if p["id"] == "tests")
    # the file is real, and it is the one without a test (util.py has tests/test_util.py)
    named = target["label"].removeprefix("Write tests for ")
    assert named == "src/core.py" and (ws / named).is_file()        # util.py already has tests/test_util.py
    assert named in target["prompt"]


def test_starters_do_not_claim_changes_outside_git(home, tmp_path):
    ws = make_project(tmp_path)
    assert "recent" not in [p["id"] for p in po.starter_prompts(ws)]


def test_empty_folder_gets_one_honest_starter(home, tmp_path):
    ws = tmp_path / "empty"
    ws.mkdir()
    prompts = po.starter_prompts(ws)
    assert [p["id"] for p in prompts] == ["start"]


def test_failed_test_run_adds_a_fix_starter(home, tmp_path):
    ws = make_project(tmp_path)
    po.record_test_result(ws, "pytest -q", 1)
    ids = [p["id"] for p in po.starter_prompts(ws)]
    assert "fixtests" in ids
    po.record_test_result(ws, "pytest -q", 0)
    assert "fixtests" not in [p["id"] for p in po.starter_prompts(ws)]


# ─── overview parts ──────────────────────────────────────────────────────────

def test_languages_count_files(home, tmp_path):
    ws = make_project(tmp_path)
    files, cut = po.list_files(ws)
    assert not cut and "src/core.py" in files
    langs = {x["name"]: x for x in po.languages(files)}
    assert langs["Python"]["files"] == 3 and langs["Markdown"]["files"] == 1
    assert abs(sum(x["pct"] for x in langs.values()) - 100) < 0.5


def test_skip_dirs_are_not_counted(home, tmp_path):
    ws = make_project(tmp_path)
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "i.js").write_text("x", encoding="utf-8")
    assert not any("node_modules" in f for f in po.list_files(ws)[0])


def test_test_command_detection(home, tmp_path):
    assert po.detect_test_command(make_project(tmp_path))["name"] == "pytest"
    js = tmp_path / "js"
    js.mkdir()
    (js / "package.json").write_text("{}", encoding="utf-8")
    assert po.detect_test_command(js)["command"] == "npm test"
    bare = tmp_path / "bare"
    bare.mkdir()
    assert po.detect_test_command(bare) == {}


def test_last_test_result_roundtrip_and_corrupt_file(home, tmp_path):
    ws = make_project(tmp_path)
    assert po.last_test_result(ws) is None
    po.record_test_result(ws, "pytest -q", 0)
    r = po.last_test_result(ws)
    assert r and r["ok"] is True and r["command"] == "pytest -q" and r["source"] == "/test"
    po.record_test_result(ws, "pytest -q", 2)
    assert po.last_test_result(ws)["ok"] is False
    po._test_file(ws).write_text("{not json", encoding="utf-8")            # corrupt: reads as nothing
    assert po.last_test_result(ws) is None


def test_test_run_in_a_chat_trace_counts(home, tmp_path):
    ws = make_project(tmp_path)
    tdir = storage.sessions_dir(ws) / "session_x1"
    tdir.mkdir(parents=True)
    step = {"index": 1, "kind": "tool", "started_at": time.time() - 5, "ended_at": time.time() - 4,
            "data": {"name": "Bash", "input": {"command": "cd x && pytest -q tests"}, "is_error": True}}
    other = {"index": 2, "kind": "tool", "started_at": time.time(), "ended_at": time.time(), "data": {"name": "Bash", "input": {"command": "ls"}, "is_error": False}}
    (tdir / "trace.jsonl").write_text(json.dumps({"turn": 1, "steps": [step, other]}) + "\n", encoding="utf-8")
    r = po.last_test_result(ws)
    assert r and r["ok"] is False and r["source"] == "chat" and "pytest" in r["command"]


@pytest.mark.skipif(not GIT, reason="git is not installed")
def test_git_state(home, tmp_path):
    ws = make_project(tmp_path, repo=True)
    g = po.git_state(ws)
    assert g["repo"] and g["changed"] == 0 and g["commits"][0]["subject"] == "first commit"
    (ws / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert po.git_state(ws)["changed"] == 1
    plain = tmp_path / "plain"
    plain.mkdir()
    assert po.git_state(plain) == {"repo": False, "branch": "", "changed": 0, "commits": []}


def test_spend_week_reads_only_this_project_and_week(home, tmp_path):
    ws = make_project(tmp_path)
    other = make_project(tmp_path, "other")
    for w, ago_days, cost in ((ws, 1, 0.5), (ws, 20, 9.0), (other, 1, 7.0)):
        storage.append_jsonl(storage.user_file("usage.jsonl"), {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_days * 86400)), "project": storage.project_key(w),
            "workspace": str(w), "session": "s1", "model": "m", "source": "main", "usage": {"input": 100, "output": 10}, "cost": cost})
    s = po.spend_week(ws)
    assert s["cost"] == 0.5 and s["calls"] == 1 and s["tokens"] == 110


def test_overview_has_every_part_and_renders_within_width(home, tmp_path):
    ws = make_project(tmp_path)
    d = po.overview(ws)
    assert {"files", "git", "tests", "spend", "chats", "memory"} <= set(d)
    for width in (40, 80):
        lines = po.render_text(d, width)
        assert lines and all(len(x) <= width for x in lines)
    assert any(x.startswith("Tests") and "pytest" in x for x in po.render_text(d, 100))


# ─── recent projects ─────────────────────────────────────────────────────────

def add_history(ws: Path, age: float = 0) -> None:
    sdir = storage.sessions_dir(ws)
    s = sdir / f"session_{int(time.time() * 1000)}_{ws.name}"
    s.mkdir()
    (s / "session.jsonl").write_text("{}\n", encoding="utf-8")
    t = time.time() - age
    os.utime(s, (t, t))
    os.utime(sdir, (t, t))


def test_recent_projects_flag_missing_and_remove(home, tmp_path):
    a, b = make_project(tmp_path, "alpha"), make_project(tmp_path, "beta")
    add_history(a, age=100)
    add_history(b, age=10)
    rows = po.recent_projects()
    assert [r["name"] for r in rows] == ["beta", "alpha"] and all(r["exists"] for r in rows)
    shutil.rmtree(a)
    rows = po.recent_projects()
    gone = next(r for r in rows if r["name"] == "alpha")
    assert gone["exists"] is False                                     # kept, so it can be removed
    assert po.remove_recent(str(a)) is True
    assert [r["name"] for r in po.recent_projects()] == ["beta"]
    assert po.remove_recent("/not/in/the/list") is False
    assert po.remove_recent(str(a)) is False


def test_removed_project_returns_when_used_again(home, tmp_path):
    a = make_project(tmp_path, "alpha")
    add_history(a, age=50)
    assert po.remove_recent(str(a.resolve())) is True
    assert po.recent_projects() == []
    time.sleep(0.05)
    add_history(a, age=0)
    assert [r["name"] for r in po.recent_projects()] == ["alpha"]


def test_recent_projects_skip_corrupt_and_excluded(home, tmp_path):
    a = make_project(tmp_path, "alpha")
    add_history(a)
    bad = home / "projects" / "broken"
    (bad / "sessions").mkdir(parents=True)
    (bad / "project.json").write_text("{nope", encoding="utf-8")
    assert [r["name"] for r in po.recent_projects()] == ["alpha"]
    assert po.recent_projects(exclude=a) == []
    po._removed_file().parent.mkdir(parents=True, exist_ok=True)
    po._removed_file().write_text("[1,2", encoding="utf-8")            # corrupt removal list reads as empty
    assert [r["name"] for r in po.recent_projects()] == ["alpha"]


# ─── welcome state ───────────────────────────────────────────────────────────

def test_welcome_state_is_remembered(home):
    assert po.welcome_state() == {"done": False, "skipped": False, "at": None}
    assert po.mark_welcome(skipped=True)["skipped"] is True
    assert po.welcome_state()["done"] is True
    po.reset_welcome()
    assert po.welcome_state()["done"] is False
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "state" / "welcome.json").write_text("[[", encoding="utf-8")
    assert po.welcome_state()["done"] is False


# ─── routes ──────────────────────────────────────────────────────────────────

def test_welcome_routes(c, env):
    w = c.json("/api/welcome")
    assert w["done"] is False and isinstance(w["ready"], bool)
    st, _, _ = c.post("/api/welcome/done", {"skipped": True}, origin="http://evil.example")
    assert st == 403 and c.json("/api/welcome")["done"] is False       # the Origin check applies
    st, _, _ = c.post("/api/welcome/done", {"skipped": True}, cookie=False)
    assert st in (401, 403)
    st, _, _ = c.post("/api/welcome/done", {"skipped": True})
    assert st == 200 and c.json("/api/welcome")["done"] is True
    assert c.get("/api/welcome/nope")[0] == 404


def test_overview_routes_return_real_data(c, env):
    d = c.json("/api/project/overview")
    assert d["name"] == env.ws.name and d["files"]["count"] >= 4
    assert {"git", "tests", "spend", "chats", "memory"} <= set(d)
    starters = c.json("/api/project/starters")["prompts"]
    assert starters[0]["id"] == "explain" and all(p["label"] and p["prompt"] for p in starters)
    assert any(ch["title"] for ch in d["chats"])


def test_recent_route_and_remove(c, env):
    other = env.tmp / "other"
    other.mkdir(exist_ok=True)
    add_history(other, age=5)
    rows = c.json("/api/project/recent")["projects"]
    assert any(r["path"] == str(other.resolve()) or r["name"] == "other" for r in rows)
    cur = [r for r in rows if r.get("current")]
    assert len(cur) <= 1 and (not cur or cur[0]["name"] == env.ws.name)
    path = next(r["path"] for r in rows if r["name"] == "other")
    shutil.rmtree(other)
    assert next(r for r in c.json("/api/project/recent")["projects"] if r["name"] == "other")["exists"] is False
    st, _, body = c.post("/api/project/recent/remove", {"path": path})
    assert st == 200 and all(r["name"] != "other" for r in json.loads(body)["projects"])
    assert c.post("/api/project/recent/remove", {"path": path})[0] == 404
    for bad in ({"path": 5}, {"path": ""}, {}, {"path": "x" * 5000}):
        assert c.post("/api/project/recent/remove", bad)[0] == 400
    assert c.post("/api/project/recent/remove", {"path": path}, origin="http://evil.example")[0] == 403


def test_overview_never_shows_a_secret(c, env):
    from test_dashboard_sample import SECRET_KEY
    (env.ws / "x.py").write_text("a = 1\n" * 100, encoding="utf-8")
    body = c.get("/api/project/overview")[2].decode() + c.get("/api/project/recent")[2].decode() + c.get("/api/project/starters")[2].decode()
    assert SECRET_KEY not in body


# ─── the page script ─────────────────────────────────────────────────────────

def test_page_script_has_the_hooks_and_no_data_in_html():
    from axon.features.dash_js_welcome import JS_WELCOME
    from axon.features.dashboard_web import APP_CSS, APP_JS, asset
    for name in ("welcomeBoot", "welcomeEmptyChat", "helpTip", "helpPageHook", "overviewSection", "projectMenu", "projPaletteItems", "fillChatBox"):
        assert f"function {name}" in JS_WELCOME and name in APP_JS
    assert "html:" not in JS_WELCOME and "innerHTML" not in JS_WELCOME and "insertAdjacentHTML" not in JS_WELCOME
    # the one-line hooks are in place
    assert "helpTip('progress:' + title)" in APP_JS and "welcomeOpen(true)" in APP_JS and "welcomeBoot()" in APP_JS
    assert "providerRow(p, note, after)" in APP_JS and "overviewSection()" in APP_JS and "projectMenu(e.currentTarget)" in APP_JS
    assert ".helptip" in asset("app.css")[0]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_page_script_parses(tmp_path):
    from axon.features.dashboard_web import APP_JS
    f = tmp_path / "app.js"
    f.write_text(APP_JS, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_help_texts_are_short_and_plain():
    from axon.features.dash_js_welcome import JS_WELCOME
    import re
    texts = re.findall(r"^  '([^']+)': \['([^']+)', '((?:[^'\\]|\\.)*)'\]", JS_WELCOME, re.M)
    keys = {k for k, _, _ in texts}
    assert {"progress:Context", "progress:Usage", "mode", "progress:Environment"} <= keys
    for k, title, body in texts:
        assert body.count(". ") <= 2 and len(body) < 330, k            # one or two sentences
        assert body == body.strip() and not title.isupper()


# ─── the terminal command ────────────────────────────────────────────────────

def test_overview_command(home, tmp_path, capsys):
    from axon.commands.registry import resolve_builtin
    ws = make_project(tmp_path)
    add_history(ws)
    cmd = resolve_builtin("overview")
    assert cmd is not None
    cmd.handler(SimpleNamespace(settings=SimpleNamespace(workspace=ws)), "")
    out = capsys.readouterr().out
    assert "Files" in out and "Tests" in out and "pytest" in out and "Last 7 days" in out


def test_spend_week_does_not_count_cached_tokens_twice(home, tmp_path):
    # `input` already includes the tokens read from and written to the cache, as on the Usage page.
    ws = make_project(tmp_path)
    storage.append_jsonl(storage.user_file("usage.jsonl"), {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)), "project": storage.project_key(ws),
        "workspace": str(ws), "session": "s1", "model": "m", "source": "main",
        "usage": {"input": 1000, "output": 50, "cache_read": 700, "cache_write": 200}, "cost": 0.1})
    assert po.spend_week(ws)["tokens"] == 1050
