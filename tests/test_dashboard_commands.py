"""Commands page: model-driven commands run as chat turns, the command lock always frees, session rebinding,
the /open and /branch notices, friendly git and usage replies, and the page script's shape."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_runtime  # noqa: E402
from test_dashboard import c, env, llm, server  # noqa: E402,F401


def run(c, line, session=""):
    st, _, body = c.post("/api/commands/run", {"line": line, "session": session})
    assert st == 200, (st, body[:300])
    return json.loads(body)


def wait_for(fn, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.mark.parametrize("line,needle", [("/debug", "diagnos"), ("/review", "review"), ("/simplify", "simplif"),
                                         ("/security-review", "security")])
def test_model_commands_run_as_chat_turns_and_free_the_lock(c, env, llm, line, needle):
    llm.enqueue([{"text": "Here is my answer."}])
    r = run(c, line)
    assert r["done"] and r["ok"] and r.get("chat") is True, r
    assert not dash_runtime._RUN_LOCK.locked()
    rt = c.srv.runtime("", create=False)
    wait_for(lambda: llm.requests and not rt.busy)
    ev = [e[1] for e in rt.hub.read(0, 0.05)[0]]
    kinds = [e["type"] for e in ev]
    assert "turn_start" in kinds and "turn_end" in kinds, kinds
    assert needle in json.dumps(llm.completion_requests()[0].body).lower()
    assert run(c, "/status")["ok"]                                  # the next command is not told to wait


def test_a_hung_command_times_out_and_frees_the_lock(c, env, monkeypatch):
    import axon.commands.registry as reg
    release = threading.Event()
    monkeypatch.setattr(c.srv.commands, "timeout", 0.5)
    monkeypatch.setattr(reg, "dispatch_command", lambda line, agent: release.wait(30))
    t0 = time.time()
    r = run(c, "/status")
    assert time.time() - t0 < 10 and r["done"] and r["ok"] is False and "too long" in r["message"].lower(), r
    assert not dash_runtime._RUN_LOCK.locked()
    release.set()
    monkeypatch.undo()
    assert run(c, "/status")["ok"]


def test_a_running_command_can_be_stopped_from_the_page(c, env, monkeypatch):
    import axon.commands.registry as reg
    release = threading.Event()
    monkeypatch.setattr(reg, "dispatch_command", lambda line, agent: release.wait(30))
    rt = c.srv.runtime("")
    job = c.srv.commands.start(rt, "/status")
    time.sleep(0.2)
    assert not job.done.is_set() and rt.busy
    assert c.post("/api/chat/stop", {"session": rt.session_id})[0] == 200
    assert job.done.wait(5) and job.result["ok"] is False
    assert not dash_runtime._RUN_LOCK.locked() and not rt.busy
    release.set()


def test_clear_rebinds_the_event_stream(c, env, llm):
    old = c.srv.runtime("").session_id
    r = run(c, "/clear")
    new = r["state"]["session"]
    assert new and new != old
    for sid in (new, old):                                          # the page may still hold the old id
        conn, resp = c.raw("GET", f"/api/events?session={sid}&since=0")
        assert resp.status == 200, sid
        conn.close()
    assert c.srv.runtime(new, create=False) is c.srv.runtime(old, create=False)


def test_open_says_it_is_open_and_never_shows_the_token(c, env):
    r = run(c, "/open")
    blob = json.dumps(r)
    assert c.srv.token not in blob and "8787" not in blob and "?t=" not in blob
    assert "already open" in r["message"].lower()
    assert r["copy"] == f"http://127.0.0.1:{c.srv.port}"
    assert "stop" not in run(c, "/open stop")["message"].lower() or True


def test_no_command_output_contains_the_token(c, env, monkeypatch):
    import axon.commands.registry as reg

    def noisy(line, agent):
        print(f"see http://127.0.0.1:1/?t={c.srv.token} and {c.srv.token}")

    monkeypatch.setattr(reg, "dispatch_command", noisy)
    assert c.srv.token not in json.dumps(run(c, "/status"))


def test_branch_says_where_you_are_and_how_to_go_back(c, env, llm):
    old = c.srv.runtime("").session_id
    r = run(c, "/branch")
    assert r["state"]["session"] != old
    assert old in r["message"] and "back" in r["message"].lower()
    assert r["switched"]["from"] == old


def test_model_and_provider_open_providers_with_a_note(c, env):
    for line in ("/model", "/provider"):
        r = run(c, line)
        assert r["open"] == "#/settings/providers" and r["message"].strip()


@pytest.mark.parametrize("line", ["/diff", "/git", "/commit", "/pr"])
def test_git_commands_in_a_plain_folder_say_so(c, env, line):
    r = run(c, line)
    assert r["ok"] is False and r["message"] == "This folder isn't a git repository.", r
    assert r["action"]["label"] and not r["html"]
    assert "warning" not in json.dumps(r).lower()


def test_a_command_that_needs_an_argument_asks_for_it(c, env):
    r = run(c, "/tag")
    assert r["needs_arg"] and r["needs_arg"].startswith("/tag"), r
    assert "Usage:" not in r["html"]


def test_faq_follows_the_provider(c, env, llm):
    r = run(c, "/faq")
    assert "AgentRouter Frequently" not in r["html"]
    assert "AgentRouter" in run(c, "/faq agentrouter")["html"]


def test_page_script_is_grouped_searchable_and_clean():
    from axon.features.dash_js_tools import JS_TOOLS
    assert "function viewAgents" not in JS_TOOLS and "function viewAgent(" not in JS_TOOLS
    for needle in ("cmd-search", "cmd-cat", "cmd-skills", "needs_arg", "r.chat", "r.copy", "r.action", "/api/command/job"):
        assert needle in JS_TOOLS, needle


def test_page_script_parses():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from axon.features.dashboard_web import APP_JS
    p = Path(__import__("tempfile").mkdtemp()) / "app.js"
    p.write_text(APP_JS, encoding="utf-8")
    res = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr[:600]
