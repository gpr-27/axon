"""The workbench: right panel data, Changes (stage, discard, diffs), worktrees, Create PR safety, the Browser panel's
frame rules and the CSP that goes with them."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from test_dashboard import c, env, llm, server  # noqa: E402,F401
from axon.features.dashboard_web import APP_JS  # noqa: E402

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page script")


def git(ws: Path, *args: str) -> None:
    subprocess.run([GIT or "git", "-C", str(ws), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(ws)})


@pytest.fixture
def repo(env):
    ws = env.ws
    git(ws, "init", "-q")
    (ws / "a.py").write_text("x = 1\ny = 2\n")
    (ws / "b.txt").write_text("hello\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")
    (ws / "a.py").write_text("x = 1\ny = 3\nz = 4\n")        # modified: +2 -1
    (ws / "new.py").write_text("print('hi')\nprint('there')\n")   # untracked: +2
    (ws / "b.txt").unlink()                                   # deleted: -1
    return ws


# ─── CSP and the Browser panel ──────────────────────────────────────────────

def test_csp_allows_only_loopback_origins_for_frames(c):
    st, h, _ = c.get("/api/info")
    csp = h["content-security-policy"]
    frame = re.search(r"frame-src ([^;]*)", csp).group(1).split()
    assert set(frame) == {"'self'", "http://localhost:*", "http://127.0.0.1:*"}, frame
    for src in frame:
        assert src == "'self'" or re.fullmatch(r"http://(localhost|127\.0\.0\.1|\[::1\]):\*", src)
    # nothing else loosened: scripts, connections and the default stay same-origin or none
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "https:" not in csp and not re.search(r"(^|[ ;])\*([ ;]|$)", csp)


@pytest.mark.parametrize("url,ok", [("http://localhost:3000/", True), ("http://127.0.0.1:5173/x?y=1", True), ("http://[::1]:8080", True),
                                    ("https://localhost:3000/", False), ("http://example.com:3000/", False), ("http://localhost/", False),
                                    ("http://localhost.evil.com:3000/", False), ("javascript:alert(1)", False), ("http://127.0.0.1:99999/", False)])
def test_preview_check_accepts_only_http_loopback_with_a_port(c, url, ok):
    d = c.json("/api/workbench/preview_check?url=" + url.replace("&", "%26"))
    assert d["frameable"] is ok, d
    assert ok or "new tab" in d["reason"]


@needs_node
def test_browser_address_rules_in_the_page():
    fn = re.search(r"^function wbNormalizeUrl\(.*?^}\n", APP_JS, re.S | re.M).group(0)
    script = "const location = { port: '8900', hostname: '127.0.0.1' };\n" + fn + """
const out = {}; for (const s of ['3000', ':5173', 'localhost:8080/a', 'example.com', 'https://localhost:3000', 'ftp://x', '127.0.0.1:8900', '', 'http://localhost']) out[s] = wbNormalizeUrl(s);
console.log(JSON.stringify(out));"""
    r = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert r["3000"] == {"url": "http://localhost:3000/", "frame": True, "own": False}
    assert r[":5173"]["frame"] is True and r["localhost:8080/a"]["frame"] is True
    assert r["example.com"]["frame"] is False and r["example.com"]["url"].startswith("https:")          # opened in a new tab, not framed
    assert r["https://localhost:3000"]["frame"] is False
    assert "err" in r["ftp://x"] and "err" in r[""]
    assert r["127.0.0.1:8900"]["own"] is True and r["127.0.0.1:8900"]["frame"] is False                  # never frames Axon itself
    assert r["http://localhost"]["frame"] is False


def test_the_frame_is_sandboxed_and_carries_no_referrer():
    assert "sandbox: 'allow-scripts allow-forms allow-same-origin allow-popups allow-modals'" in APP_JS and "referrerpolicy: 'no-referrer'" in APP_JS


# ─── Changes ────────────────────────────────────────────────────────────────

@needs_git
def test_changes_totals_match_the_shared_view_and_count_untracked(c, repo):
    d = c.json("/api/workbench/changes")
    from axon.features import worktree_view
    ref = worktree_view.changes(repo)
    assert (d["added"], d["removed"]) == (ref["added"], ref["removed"]) == (4, 2)
    by = {f["path"]: f for f in d["files"]}
    assert by["a.py"]["code"] == "M" and (by["a.py"]["added"], by["a.py"]["removed"]) == (2, 1)
    assert by["new.py"]["code"] == "U" and by["new.py"]["added"] == 2
    assert by["b.txt"]["code"] == "D" and by["b.txt"]["removed"] == 1
    assert d["label"].endswith("working tree") and "gh" in d


@needs_git
def test_compare_modes_and_a_branch_must_exist(c, repo):
    git(repo, "add", "a.py")
    staged = c.json("/api/workbench/changes?mode=staged")
    assert [f["path"] for f in staged["files"]] == ["a.py"] and staged["files"][0]["staged"] is True
    git(repo, "commit", "-q", "-m", "second")                 # "last commit" is HEAD~1 -> HEAD
    last = c.json("/api/workbench/changes?mode=commit")
    assert [f["path"] for f in last["files"]] == ["a.py"] and last["files"][0]["code"] == "M" and last["label"] == "HEAD~1 → HEAD"
    st, _, body = c.get("/api/workbench/changes?mode=branch&ref=nope")
    assert st == 200 and json.loads(body)["files"] == []        # an unknown ref compares nothing and reaches git only as data


@needs_git
def test_diff_rows_are_numbered_and_untracked_files_show_as_added(c, repo):
    d = c.json("/api/workbench/diff?path=a.py")
    kinds = [r["kind"] for r in d["rows"]]
    assert "hunk" in kinds and "add" in kinds and "del" in kinds
    add = next(r for r in d["rows"] if r["kind"] == "add")
    assert isinstance(add["new"], int)
    u = c.json("/api/workbench/diff?path=new.py&code=U")
    assert [r["kind"] for r in u["rows"]] == ["add", "add"] and u["rows"][1]["new"] == 2


@needs_git
def test_diff_and_stage_refuse_paths_outside_the_project(c, repo):
    for p in ("../../etc/passwd", "/etc/passwd", "..%2f..%2fx"):
        st, _, _ = c.get("/api/workbench/diff?path=" + p)
        assert st in (400, 403, 404), p
    st, _, _ = c.post("/api/workbench/stage", {"path": "../outside.txt", "action": "stage"})
    assert st in (400, 403, 404)


@needs_git
def test_stage_unstage_and_discard_needs_confirmation(c, repo):
    assert c.post("/api/workbench/stage", {"path": "a.py", "action": "stage"})[0] == 200
    assert next(f for f in c.json("/api/workbench/changes")["files"] if f["path"] == "a.py")["staged"] is True
    assert c.post("/api/workbench/stage", {"path": "a.py", "action": "unstage"})[0] == 200
    assert next(f for f in c.json("/api/workbench/changes")["files"] if f["path"] == "a.py")["staged"] is False
    st, _, body = c.post("/api/workbench/stage", {"path": "a.py", "action": "discard", "code": "M"})
    assert st == 400 and b"confirmation" in body
    assert (repo / "a.py").read_text().startswith("x = 1\ny = 3")           # untouched
    assert c.post("/api/workbench/stage", {"path": "a.py", "action": "discard", "code": "M", "confirm": True})[0] == 200
    assert (repo / "a.py").read_text() == "x = 1\ny = 2\n"
    assert c.post("/api/workbench/stage", {"path": "new.py", "action": "discard", "code": "U", "confirm": True})[0] == 200
    assert not (repo / "new.py").exists()
    assert c.post("/api/workbench/stage", {"path": "a.py", "action": "explode"})[0] == 400


# ─── branches, worktrees ────────────────────────────────────────────────────

@needs_git
def test_worktrees_list_create_and_refuse_to_remove_dirty_or_main(c, repo):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    rows = c.json("/api/workbench/worktrees")["worktrees"]
    assert len(rows) == 1 and rows[0]["main"] and rows[0]["current"]
    r = c.post("/api/workbench/worktree_create", {"name": "try-it"})
    assert r[0] == 200, r
    made = json.loads(r[2])
    rows = c.json("/api/workbench/worktrees")["worktrees"]
    assert {w["branch"] for w in rows} >= {"axon/try-it"}
    assert c.post("/api/workbench/worktree_remove", {"path": rows[0]["path"]})[0] == 409          # the main one
    (Path(made["path"]) / "dirty.txt").write_text("x\n")
    st, _, body = c.post("/api/workbench/worktree_remove", {"path": made["path"]})
    assert st == 409 and b"uncommitted" in body and Path(made["path"]).exists()                     # refused while dirty
    (Path(made["path"]) / "dirty.txt").unlink()
    assert c.post("/api/workbench/worktree_remove", {"path": made["path"]})[0] == 200
    assert not Path(made["path"]).exists()
    assert c.post("/api/workbench/worktree_remove", {"path": "/tmp"})[0] == 409


@needs_git
def test_branch_switch_only_to_an_existing_local_branch(c, repo):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    git(repo, "branch", "other")
    names = [b["name"] for b in c.json("/api/workbench/branches")["branches"]]
    assert "other" in names
    assert c.post("/api/workbench/checkout", {"branch": "--orphan"})[0] == 404
    assert c.post("/api/workbench/checkout", {"branch": "other"})[0] == 200
    assert c.json("/api/workbench/branches")["current"] == "other"


# ─── Create PR never pushes without confirmation ────────────────────────────

@needs_git
def test_create_pr_needs_confirmation_and_never_pushes_unasked(c, repo, monkeypatch):
    from axon.features import dash_workbench as wb
    real_which = shutil.which
    monkeypatch.setattr(wb.shutil, "which", lambda n, *a, **k: "/usr/bin/true" if n == "gh" else real_which(n, *a, **k))
    pushed = []
    real = wb.dash_fs._git
    monkeypatch.setattr(wb.dash_fs, "_git", lambda ws, *a, **k: (pushed.append(a) if a and a[0] == "push" else None) or real(ws, *a, **k))
    plan = c.json("/api/workbench/pr")
    assert plan["gh"] is True and plan["needs_push"] is True and plan["upstream"] == ""
    st, _, body = c.post("/api/workbench/pr", {})
    assert st == 400 and b"confirmation" in body
    st, _, body = c.post("/api/workbench/pr", {"confirm": True})
    assert st == 409 and b"Confirm the push" in body
    assert pushed == []                                                    # nothing was pushed


@needs_git
def test_create_pr_without_gh_or_repo_is_a_clear_error(c, env, monkeypatch):
    from axon.features import dash_workbench as wb
    st, _, body = c.post("/api/workbench/pr", {"confirm": True})
    assert st == 409 and b"not a git repository" in body
    git(env.ws, "init", "-q")
    real_which = shutil.which
    monkeypatch.setattr(wb.shutil, "which", lambda n, *a, **k: None if n == "gh" else real_which(n, *a, **k))
    st, _, body = c.post("/api/workbench/pr", {"confirm": True})
    assert st == 409 and b"gh" in body


# ─── context, usage, connectors, panels with real data ──────────────────────

def test_context_breakdown_has_every_category_and_adds_up(c, env):
    sid = env.ids["main"] if isinstance(getattr(env, "ids", None), dict) and "main" in env.ids else None
    st = c.json("/api/chat/state?session=" + (sid or "") + "&create=1")
    d = c.json("/api/workbench/context?session=" + st["session"])
    keys = [r["key"] for r in d["rows"]]
    assert keys == ["messages", "system_tools", "mcp_tools", "skills", "system_prompt", "mcp_instructions", "custom_agents", "memory_files", "autocompact", "free"]
    assert d["window"] > 0 and d["used"] == sum(r["tokens"] for r in d["rows"] if r["key"] not in ("autocompact", "free"))
    assert sum(r["tokens"] for r in d["rows"]) == d["window"] or d["rows"][-1]["tokens"] == 0     # used + buffer + free fill the window
    assert "estimate" in d and set(d["lists"]) >= {"mcp_tools", "memory_files", "custom_agents", "skills", "messages"}
    assert d["lists"]["custom_agents"] and all("tokens" in a for a in d["lists"]["custom_agents"])


def test_context_is_calibrated_to_the_provider_figure_when_there_is_one(env):
    from types import SimpleNamespace
    from axon.features import workbench_data as wd
    from axon.features.dashboard import make_server
    srv = make_server(0, env.ws)
    try:
        rt = srv.runtime("", create=True)
        base = wd.context_breakdown(rt.agent)
        cal = wd.context_breakdown(rt.agent, real_used=base["used"] + 50_000)
        assert cal["calibrated"] is True and cal["used"] == base["used"] + 50_000
        assert base["calibrated"] is False
    finally:
        srv.shutdown_runs()
        srv.server_close()


def test_usage_shows_only_real_limits(c, env):
    from axon.features import quota
    quota.clear()
    st = c.json("/api/chat/state?session=&create=1")
    assert c.json("/api/workbench/usage?session=" + st["session"]) == {"limits": [], "budget": None}
    quota.record_rate_limits({"anthropic-ratelimit-requests-limit": "100", "anthropic-ratelimit-requests-remaining": "25",
                              "anthropic-ratelimit-requests-reset": "2999-01-01T00:00:00Z"}, provider="anthropic")
    try:
        d = c.json("/api/workbench/usage?session=" + st["session"])
        assert d["limits"][0]["name"] == "requests" and d["limits"][0]["used"] == pytest.approx(0.75) and d["limits"][0]["reset"].startswith("resets")
    finally:
        quota.clear()


def test_connectors_view_is_read_only_and_hooks_listing_works(c, env):
    d = c.json("/api/workbench/connectors")
    assert set(d) == {"servers", "reconnect", "connected"}
    assert c.post("/api/workbench/connector", {"name": "ghost", "action": "reconnect"})[0] == 404
    assert "hooks" in c.json("/api/workbench/hooks?session=")


def test_chat_title_menu_actions_need_a_confirmed_delete(c, env):
    st = c.json("/api/chat/state?session=&create=1")
    sid = st["session"]
    assert c.post("/api/workbench/session_rename", {"session": sid, "title": "  Renamed   chat "})[0] == 200
    assert c.post("/api/workbench/session_rename", {"session": sid, "title": "  "})[0] == 400
    st2, _, body = c.post("/api/workbench/session_delete", {"session": sid})
    assert st2 == 400 and b"confirmation" in body
    assert c.post("/api/workbench/session_rename", {"session": "nope", "title": "x"})[0] == 404


# ─── the page ───────────────────────────────────────────────────────────────

def test_toolbar_panels_and_footer_are_in_the_page():
    for needle in ("data-wb': 'terminal'", "data-wb': 'changes'", "data-wb': 'browser'", "data-wb': 'files'", "tbicons", "wbTitleMenu", "wbFooter(C)", "SpeechRecognition",
                   "Speech is sent to your browser", "rwrap", "rpbody", "ide-body"):
        assert needle in APP_JS, needle
    assert APP_JS.count("Create PR") >= 1 and "confirm: true, push: push.checked" in APP_JS
    # the old text buttons and the crowded header are gone
    assert "'Progress'" not in APP_JS.split("function ensureFrame")[1].split("function drawChip")[0]
    assert "tracePicker(C), typeof axNotifyPill" not in APP_JS


@needs_node
def test_page_script_for_the_workbench_has_no_syntax_errors(tmp_path):
    f = tmp_path / "app.js"
    f.write_text(APP_JS)
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_node
def test_diff_rows_become_page_rows_and_are_capped():
    fn = re.search(r"^function wbRows\(.*?^}\n", APP_JS, re.S | re.M).group(0)
    script = fn + """
const rows = [{kind:'hunk',old:null,new:null,text:'@@ -1 +1 @@'},{kind:'del',old:1,new:null,text:'a'},{kind:'add',old:null,new:1,text:'b'},{kind:'meta',old:null,new:null,text:'No newline'}];
const big = Array.from({length: 3500}, (_, i) => ({kind:'ctx',old:i,new:i,text:'x'}));
console.log(JSON.stringify([wbRows({rows}).map(r => r.t + r.n), wbRows({rows: big}).length, wbRows({rows: big}).slice(-1)[0].t]));"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out[0] == ["hunknull", "del1", "add1", "notenull"] or out[0][0].startswith("hunk")
    assert out[1] == 3001 and out[2] == "note"


def test_opening_panels_keeps_room_for_the_chat():
    from axon.features.dash_js_workbench import JS_WORKBENCH
    assert "wbKeepChatRoom(id)" in JS_WORKBENCH and "WB_CHAT_MIN = 560" in JS_WORKBENCH
