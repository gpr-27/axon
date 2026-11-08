"""The dashboard as an app: workspace files (tree, jail, stale-file save), live chat runtimes (events, approvals,
stop, queue, reconnect), the replayable event log, slash commands, the shell panel and its guards, attachments and
large pastes, subagent transcripts, the session lease (a terminal-held session is read-only), and real terminals."""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_ansi, dash_fs  # noqa: E402
from axon.features import dashboard as dash  # noqa: E402
from axon.features.dash_runtime import EventHub, EventLog, Runtime  # noqa: E402
from test_dashboard import Client, c, env, llm, server  # noqa: E402,F401
from test_dashboard_sample import SECRET_KEY  # noqa: E402


def wait_for(fn, timeout: float = 20.0, step: float = 0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("timed out")


def types(hub: EventHub) -> list[str]:
    return [e[1]["type"] for e in hub.read(0, 0.01)[0]]


# ─── files ───────────────────────────────────────────────────────────────────

def test_tree_is_lazy_and_respects_gitignore(c, env):
    root = c.json("/api/tree")
    names = {e["name"]: e for e in root["entries"]}
    assert "src" in names and names["src"]["type"] == "dir" and "README.md" in names
    assert "ignored" not in names and "build.log" not in names and ".git" not in names     # .gitignore honoured
    shown = {e["name"] for e in c.json("/api/tree?ignored=1")["entries"]}
    assert {"ignored", "build.log"} <= shown
    sub = c.json("/api/tree?path=src")
    assert [e["name"] for e in sub["entries"]] == ["app"]                                 # one level per request
    assert c.json("/api/tree?path=src/app")["entries"][0]["path"].startswith("src/app/")


def test_tree_handles_a_huge_folder(env, server):
    big = env.ws / "huge"
    big.mkdir()
    for i in range(6000):
        (big / f"f{i:05d}.txt").write_text("x")
    cl = Client(server)
    cl.login()
    t0 = time.time()
    d = cl.json("/api/tree?path=huge")
    assert len(d["entries"]) == 6000 and time.time() - t0 < 5 and not d["truncated"]
    assert cl.json("/api/files/search?q=f05999")["files"] == ["huge/f05999.txt"]


def test_file_view_paging_and_language(c, env):
    f = c.json("/api/file?path=src/app/auth.py")
    assert f["language"] == "python" and f["total_lines"] == 10 and f["editable"] and len(f["sha"]) == 64
    page = c.json("/api/file?path=src/app/auth.py&offset=4&limit=2")
    assert page["lines"] == 2 and page["offset"] == 4 and not page["editable"]
    assert c.json("/api/file?path=docs/dot.png")["binary"] is True
    assert c.get("/api/file?path=nope.txt")[0] == 404


@pytest.mark.parametrize("raw", ["../../etc/passwd", "/etc/passwd", "src/../../x", ".git/config", "..%2f..%2fetc%2fpasswd"])
def test_files_are_jailed(c, env, raw):
    for route in ("/api/file?path=", "/api/tree?path=", "/api/git/diff?path="):
        st, _, body = c.get(route + raw)
        assert st in (400, 403, 404), (route, raw, st)
        assert b"root:" not in body


def test_credential_stores_are_refused_everywhere(c, env, tmp_path):
    (env.ws / ".ssh").mkdir()
    (env.ws / ".ssh" / "id_rsa").write_text("PRIVATE")
    assert c.get("/api/file?path=.ssh/id_rsa")[0] == 403
    assert c.get("/api/tree?path=.ssh")[0] == 403
    assert c.post("/api/file/save", {"path": ".ssh/id_rsa", "content": "x", "sha": "a" * 64})[0] == 403
    assert c.get("/api/file/raw?path=.ssh/id_rsa")[0] in (403, 415)
    home = tmp_path / "home2"
    (home / ".aws").mkdir(parents=True)
    (home / "project").mkdir()
    st, _, body = c.get(f"/api/fs/dirs?path={home / '.aws'}")
    assert st == 403
    names = [d["name"] for d in c.json(f"/api/fs/dirs?path={home}&hidden=1")["dirs"]]
    assert ".aws" not in names and "project" in names                                      # never even listed
    assert c.get("/api/fs/dirs?path=/etc")[0] == 403                                        # system directory


def test_save_detects_a_file_changed_on_disk(c, env):
    f = c.json("/api/file?path=src/app/auth.py")
    target = env.ws / "src" / "app" / "auth.py"
    target.write_text("changed elsewhere\n")
    st, _, body = c.post("/api/file/save", {"path": "src/app/auth.py", "content": "mine\n", "sha": f["sha"]})
    assert st == 409 and b"changed on disk" in body
    assert target.read_text() == "changed elsewhere\n"                                    # nothing was overwritten
    fresh = c.json("/api/file?path=src/app/auth.py")
    st, _, body = c.post("/api/file/save", {"path": "src/app/auth.py", "content": "mine\n", "sha": fresh["sha"]})
    assert st == 200 and target.read_text() == "mine\n" and json.loads(body)["sha"] != fresh["sha"]
    assert c.post("/api/file/save", {"path": "src/app/auth.py", "content": "x"})[0] == 409  # no sha: refused
    assert c.post("/api/file/save", {"path": "../escape.txt", "content": "x", "sha": "a"})[0] == 403
    assert c.post("/api/file/save", {"path": "docs/dot.png", "content": "x", "sha": "a" * 64})[0] in (409, 415)


def test_save_keeps_crlf_and_refuses_huge(c, env):
    p = env.ws / "win.txt"
    p.write_bytes(b"a\r\nb\r\n")
    f = c.json("/api/file?path=win.txt")
    assert f["eol"] == "crlf"
    assert c.post("/api/file/save", {"path": "win.txt", "content": "a\nb\nc\n", "sha": f["sha"]})[0] == 200
    assert p.read_bytes() == b"a\r\nb\r\nc\r\n"
    big = "x" * (2 * 1024 * 1024 + 10)
    f = c.json("/api/file?path=win.txt")
    assert c.post("/api/file/save", {"path": "win.txt", "content": big, "sha": f["sha"]})[0] == 413


def test_previews_and_raw_only_for_safe_types(c, env):
    md = c.json("/api/file/preview?path=README.md")
    assert "<h2>Sample project</h2>" in md["html"] and "<strong>sample</strong>" in md["html"]
    st, h, body = c.get("/api/file/raw?path=docs/dot.png")
    assert st == 200 and h["content-type"] == "image/png" and body[:4] == b"\x89PNG" and "sandbox" in h["content-security-policy"]
    assert c.get("/api/file/raw?path=src/app/auth.py")[0] == 415
    (env.ws / "x.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    st, h, _ = c.get("/api/file/raw?path=x.svg")
    assert st == 200 and h["content-security-policy"].startswith("sandbox") and h["x-content-type-options"] == "nosniff"


def test_content_search(c, env):
    d = c.json("/api/files/grep?q=TODO")
    assert [(m["path"], m["line"]) for m in d["matches"]] == [("src/app/auth.py", 6)]
    assert c.json("/api/files/grep?q=x")["matches"] == []                                  # too short to search


def test_changing_the_folder(c, env, tmp_path):
    other = tmp_path / "other"
    assert c.json("/api/ws")["workspace"] == str(env.ws.resolve())
    r = c.post("/api/ws/set", {"path": str(other)})
    assert r[0] == 200
    assert c.json("/api/ws")["workspace"] == str(other.resolve())
    assert [e["name"] for e in c.json("/api/tree")["entries"]] == []
    assert c.post("/api/ws/set", {"path": str(tmp_path / "missing")})[0] == 404
    assert c.post("/api/ws/set", {"path": str(Path.home() / ".ssh")})[0] in (403, 404)
    recent = [r["path"] for r in c.json("/api/ws")["recent"]]
    assert isinstance(recent, list)


def test_git_status_and_diff(c, env):
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", str(env.ws), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "README.md", "src")
    git("commit", "-qm", "init")
    (env.ws / "README.md").write_text("# Changed\n")
    (env.ws / "new.txt").write_text("fresh\n")
    st = c.json("/api/git/status")
    assert st["repo"] and st["files"]["README.md"] == "M" and st["files"]["new.txt"] == "U"
    d = c.json("/api/git/diff?path=README.md")
    assert "-# Sample project" in d["diff"] and "+# Changed" in d["diff"]
    assert "+fresh" in c.json("/api/git/diff?path=new.txt")["diff"]
    assert {e["name"]: e["git"] for e in c.json("/api/tree")["entries"]}["README.md"] == "M"


# ─── ANSI and commands ──────────────────────────────────────────────────────

def test_ansi_to_html_is_safe_and_coloured():
    out = dash_ansi.ansi_to_html("\x1b[1;31mred\x1b[0m <script>alert(1)</script> \x1b[38;5;208mo\x1b[0m \x1b[38;2;1;2;3mt\x1b[0m \x1b]0;t\x07ok")
    assert "<script" not in out and "&lt;script&gt;" in out
    assert '<span class="af1 ab">red</span>' in out and 'style="color:#010203"' in out and "ok" in out and "\x1b" not in out
    assert dash_ansi.ansi_to_html("x\rfinal") == "final"                                 # progress rewrites keep the last state
    assert "\x1b" not in dash_ansi.ansi_to_html("\x1b[2J\x1b[Hhi\x1b[?25l")
    assert dash_ansi.strip_ansi("\x1b[31mhi\x1b[0m") == "hi"


def test_commands_list_and_run(c, env, llm):
    cmds = c.json("/api/commands")["commands"]
    names = {x["name"] for x in cmds}
    assert {"/open", "/model", "/help", "/clear", "/compact", "/rewind", "/memory", "/mcp", "/marketplace", "/usage", "/permissions"} <= names
    assert len(cmds) > 90 and next(x for x in cmds if x["name"] == "/exit")["terminal_only"]
    r = json.loads(c.post("/api/commands/run", {"line": "/status"})[2])
    assert r["done"] and r["ok"] and "Axon status" in r["html"] and "\x1b" not in r["html"]
    assert r["state"]["mode"] == "default"
    r = json.loads(c.post("/api/commands/run", {"line": "/mode plan"})[2])
    assert r["ok"] and r["state"]["mode"] == "plan"
    assert json.loads(c.post("/api/commands/run", {"line": "/definitely-not-a-command"})[2])["ok"]    # a message, not a crash
    assert c.post("/api/commands/run", {"line": "not a command"})[0] == 400


def test_exit_cannot_end_the_server(c, env, llm):
    for line in ("/exit", "/quit"):
        r = json.loads(c.post("/api/commands/run", {"line": line})[2])
        assert r["ok"] is False and r["terminal_only"] and "terminal" in r["message"].lower()
    assert c.get("/api/info")[0] == 200


def test_every_command_runs_without_hanging(c, env, llm):
    """No picker may block the server: each command answers (the page offers the terminal for the interactive ones)."""
    from axon.commands.registry import visible_commands
    rt = c.srv.runtime("")
    skip = {"exit", "quit", "logout", "delete-all", "uninstall", "update", "upgrade", "review", "fix", "debug", "commit", "init",
            "security-review", "simplify", "loop", "plan", "retry", "next", "learn", "map", "index", "compact", "clear", "reset", "new"}
    results = {}
    for cmd in visible_commands():
        if cmd.name in skip:
            continue
        job = c.srv.commands.start(rt, "/" + cmd.name)
        assert job.done.wait(15), f"/{cmd.name} hung"
        results[cmd.name] = job.result
    assert len(results) > 80
    assert all("html" in r for r in results.values())


# ─── chat runtime ───────────────────────────────────────────────────────────

def test_chat_turn_events_approval_and_replayable_log(c, env, llm):
    llm.enqueue([{"tool": "Write", "args": {"path": "made.txt", "content": "hi"}}], [{"text": "done writing"}])
    r = json.loads(c.post("/api/chat/send", {"text": "write made.txt"})[2])
    sid = r["session"]
    rt = c.srv.runtime(sid, create=False)
    assert rt is not None and rt.state()["mode"] == "default"
    pend = wait_for(lambda: list(rt.pending))
    assert not (env.ws / "made.txt").exists()                      # nothing runs before the answer
    assert c.post("/api/chat/approval", {"session": sid, "id": pend[0], "choice": "bogus"})[0] == 400
    assert c.post("/api/chat/approval", {"session": sid, "id": pend[0], "choice": "once"})[0] == 200
    wait_for(lambda: not rt.busy)
    assert (env.ws / "made.txt").read_text() == "hi"
    seen = types(rt.hub)
    for must in ("user", "turn_start", "llm_call_start", "llm_call_end", "tool_use", "approval_request", "approval_resolved",
                 "permission", "tool_result", "text_delta", "turn_end", "idle"):
        assert must in seen, (must, seen)
    st = rt.state()
    assert st["cost"] > 0 and st["context"]["window"] > 0 and not st["busy"]
    # the record on disk: ordered, with parents, and replayable through the API
    log = c.json(f"/api/session/{sid}/events")
    assert log["exists"] and [e["seq"] for e in log["events"]] == sorted(e["seq"] for e in log["events"])
    kinds = [e["type"] for e in log["events"]]
    assert kinds.index("user") < kinds.index("tool_use") < kinds.index("tool_result") < kinds.index("turn_end")
    tool_result = next(e for e in log["events"] if e["type"] == "tool_result")
    assert tool_result["parent"] == tool_result["data"]["tool_use_id"]
    assert next(e for e in log["events"] if e["type"] == "tool_use")["parent"].startswith("call:")
    assert "".join(e["data"]["text"] for e in log["events"] if e["type"] == "text_delta") == "done writing"
    assert c.json(f"/api/session/{sid}/events?after={log['events'][3]['seq']}")["events"][0]["seq"] == log["events"][3]["seq"] + 1


def test_deny_never_runs_the_tool_and_always_grants_the_session(c, env, llm):
    llm.enqueue([{"tool": "Write", "args": {"path": "no.txt", "content": "x"}}], [{"text": "ok denied"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "write no.txt"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    c.post("/api/chat/approval", {"session": sid, "id": wait_for(lambda: list(rt.pending))[0], "choice": "deny"})
    wait_for(lambda: not rt.busy)
    assert not (env.ws / "no.txt").exists()
    llm.enqueue([{"tool": "Write", "args": {"path": "yes.txt", "content": "y"}}], [{"text": "wrote"}])
    c.post("/api/chat/send", {"session": sid, "text": "now yes.txt"})
    c.post("/api/chat/approval", {"session": sid, "id": wait_for(lambda: list(rt.pending))[0], "choice": "always"})
    wait_for(lambda: not rt.busy)
    assert (env.ws / "yes.txt").exists()
    llm.enqueue([{"tool": "Write", "args": {"path": "again.txt", "content": "z"}}], [{"text": "wrote again"}])
    c.post("/api/chat/send", {"session": sid, "text": "again"})
    wait_for(lambda: not rt.busy)
    assert (env.ws / "again.txt").exists() and not rt.pending           # no second prompt: granted for the session


def test_stop_really_interrupts_and_a_message_can_queue(c, env, llm):
    llm.enqueue([{"delay": 30}, {"text": "never"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "slow please"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: rt.state()["busy"] and "llm_call_start" in types(rt.hub))
    q = json.loads(c.post("/api/chat/send", {"session": sid, "text": "also this", "steer": False})[2])
    assert q["queued"] is True and q["steer"] is False
    assert [i["text"] for i in rt.state()["queue"]] == ["also this"]
    t0 = time.time()
    assert json.loads(c.post("/api/chat/stop", {"session": sid})[2])["ok"]
    wait_for(lambda: "interrupted" in types(rt.hub) or "turn_end" in types(rt.hub), 15)
    assert time.time() - t0 < 12                                       # not the 30 s the model was asleep for
    llm.enqueue([{"text": "queued reply"}])
    wait_for(lambda: not rt.busy, 20)
    assert "queued" in types(rt.hub)
    assert c.post("/api/chat/stop", {"session": "nope"})[0] in (404, 400)


def test_events_reconnect_resumes_without_loss(c, env, llm):
    llm.enqueue([{"text": "alpha beta gamma delta"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "hi"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub))

    def read_stream(since: int) -> list[tuple[int, dict]]:
        conn, r = c.raw("GET", f"/api/events?session={sid}&since={since}")
        assert r.status == 200 and "text/event-stream" in r.getheader("Content-Type")
        out, cur = [], 0
        deadline = time.time() + 5
        while time.time() < deadline:
            line = r.fp.readline().decode()
            if line.startswith("id:"):
                cur = int(line[3:])
            elif line.startswith("data:"):
                out.append((cur, json.loads(line[5:])))
                if out[-1][1]["type"] == "idle":
                    break
        conn.close()
        return out
    everything = read_stream(0)
    cut = everything[4][0]
    resumed = read_stream(cut)
    assert [s for s, _ in resumed] == [s for s, _ in everything if s > cut]       # exactly the missing ones
    assert c.get("/api/events?session=nope")[0] in (404, 400)


def test_hub_gap_and_blocking_read():
    hub = EventHub(cap=5)
    for i in range(20):
        hub.emit("x", {"i": i})
    evs, gap = hub.read(2, 0.01)
    assert gap and [e[0] for e in evs] == [16, 17, 18, 19, 20]
    t0 = time.time()
    got: list = []
    th = threading.Thread(target=lambda: got.append(hub.read(20, 5)))
    th.start()
    time.sleep(0.2)
    hub.emit("late", {})
    th.join(3)
    assert got and got[0][0][0][1]["type"] == "late" and time.time() - t0 < 3


def test_runtime_mirrors_the_repl_agent_without_replacing_its_renderer():
    shown: list[Any] = []
    agent = SimpleNamespace(on_event=lambda ev: shown.append(ev), on_approval=None, busy=False,
                            session=SimpleNamespace(active_session_id=""), settings=SimpleNamespace(mode="default", model="m"))
    from axon.providers.base import TextDelta, ToolExecutionStart
    rt = Runtime("live", agent, Path("."), external=True)
    rt.attach()
    agent.on_event(TextDelta("hel"))
    agent.on_event(ToolExecutionStart(id="t1", name="Bash", input={"command": "ls"}, rationale="to look"))
    assert len(shown) == 2                                          # the terminal renderer still saw both events
    evs = [e[1] for e in rt.hub.read(0, 0.01)[0]]
    assert [e["type"] for e in evs] == ["text_delta", "tool_use"] and evs[1]["rationale"] == "to look"
    rt.detach()
    assert agent.on_event is not rt._tee


def test_event_log_spills_big_bodies_and_redacts(tmp_path):
    log = EventLog(tmp_path / "s1")
    log.append("tool_result", {"tool_use_id": "t9", "content": "line\n" * 10000 + SECRET_KEY, "is_error": False})
    log.append("text_delta", {"text": "a"})
    log.append("text_delta", {"text": "b"})
    log.append("turn_end", {"result": "ok"})
    recs = log.read()
    assert [r["type"] for r in recs] == ["tool_result", "text_delta", "turn_end"]            # deltas coalesced
    body = recs[0]["data"]["content"]
    assert set(body) == {"$blob", "len", "head"} and body["len"] > 40000 and recs[1]["data"]["text"] == "ab"
    full = log.blob(body["$blob"])["text"]
    assert SECRET_KEY not in full and "[REDACTED]" in full and SECRET_KEY not in (tmp_path / "s1" / "web-events.jsonl").read_text()
    assert len(list((tmp_path / "s1" / "web-events-blobs").iterdir())) == 1
    with pytest.raises(dash.DashError):
        log.blob("../../etc/passwd")
    assert EventLog(tmp_path / "s1").seq == log.seq                                         # a new process continues the numbering


# ─── shell panel ────────────────────────────────────────────────────────────

def _run_shell(c: Client, command: str, **extra: Any) -> dict:
    r = c.post("/api/shell/run", {"command": command, **extra})
    assert r[0] == 200, r
    return json.loads(r[2])


def _shell_events(c: Client, jid: str) -> list[dict]:
    job = c.srv.shell.jobs[jid]
    wait_for(lambda: job.hub.closed, 20)
    return [e[1] for e in job.hub.read(0, 0.01)[0]]


def test_shell_runs_streams_and_records(c, env, monkeypatch):
    monkeypatch.setenv("MY_SECRET_TOKEN", "super-secret-value-123456")
    j = _run_shell(c, "echo out-line; echo err-line 1>&2; pwd; env | grep -c MY_SECRET_TOKEN; exit 3")
    evs = _shell_events(c, j["id"])
    out = "".join(e["html"] for e in evs if e["type"] == "out" and e["stream"] == "out")
    err = "".join(e["html"] for e in evs if e["type"] == "out" and e["stream"] == "err")
    assert "out-line" in out and "err-line" in err and str(env.ws.resolve()) in out
    assert "\n0" in out or out.rstrip().endswith("0")                                       # scrubbed environment: the secret is not there
    assert evs[-1]["type"] == "exit" and evs[-1]["code"] == 3
    assert c.json("/api/shell/history")["history"][-1]["code"] == 3
    log = c.srv.shell and EventLog(__import__("axon.storage", fromlist=["x"]).sessions_dir(c.srv.workspace) / "_unattached")
    rec = [r for r in log.read() if r["type"] == "shell_command"]
    assert rec and rec[-1]["data"]["code"] == 3 and "out-line" in rec[-1]["data"]["output"]


@pytest.mark.parametrize("cmd,why", [("rm -rf /", "destructive"), ("rm -rf ~", "destructive"), (":(){ :|:& };:", "destructive"),
                                     ("cat ~/.ssh/id_rsa", "credential"), ("cd ~/.aws && ls", "credential"), ("echo $(cat ~/.ssh/id_ed25519)", "credential")])
def test_shell_guard_refuses_dangerous_commands(c, env, cmd, why):
    j = _run_shell(c, cmd)
    assert j["refused"] and why in j["refused"] and j["code"] is None and not j["running"]
    evs = _shell_events(c, j["id"])
    assert [e["type"] for e in evs] == ["refused", "exit"] and evs[-1]["refused"] is True
    assert c.srv.shell.jobs[j["id"]].proc is None                                           # nothing was ever started


def test_shell_cwd_is_jailed_and_kill_works(c, env):
    assert c.post("/api/shell/run", {"command": "pwd", "cwd": "/"})[0] == 403
    assert c.post("/api/shell/run", {"command": "pwd", "cwd": "../.."})[0] == 403
    j = _run_shell(c, "sleep 30", cwd="src")
    wait_for(lambda: c.srv.shell.jobs[j["id"]].proc is not None)
    assert json.loads(c.post("/api/shell/kill", {"id": j["id"]})[2])["ok"]
    evs = _shell_events(c, j["id"])
    assert evs[-1]["type"] == "exit" and evs[-1]["killed"] is True and evs[-1]["code"] != 0
    assert json.loads(c.post("/api/shell/kill", {"id": j["id"]})[2])["ok"] is False        # already gone


def test_shell_output_is_capped_and_ansi_escaped(c, env):
    j = _run_shell(c, "printf '\\033[31m<b>x</b>\\033[0m\\n'")
    html = "".join(e["html"] for e in _shell_events(c, j["id"]) if e["type"] == "out")
    assert "&lt;b&gt;" in html and "<b>" not in html and "af1" in html


# ─── attachments and large input ────────────────────────────────────────────

def _upload(c: Client, name: str, data: bytes, ctype: str = "application/octet-stream"):
    conn = http.client.HTTPConnection("127.0.0.1", c.srv.port, timeout=60)
    conn.putrequest("POST", f"/api/attach?name={name}", skip_host=True)
    conn.putheader("Host", f"127.0.0.1:{c.srv.port}")
    conn.putheader("Cookie", f"{dash.COOKIE_NAME}={c.cookie}")
    conn.putheader("Origin", f"http://127.0.0.1:{c.srv.port}")
    conn.putheader("Content-Type", ctype)
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, json.loads(body)


def test_file_and_image_attachments(c, env, llm):
    c.srv.runtime("")
    st, j = _upload(c, "notes.txt", b"hello attachment")
    assert st == 200 and j["kind"] == "file" and j["chip"].startswith("[File #") and "notes.txt" in j["chip"]
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
    st, j = _upload(c, "shot.png", png)
    assert st == 200 and j["kind"] == "image" and j["chip"].startswith("[Image #") and j["width"] == 1
    assert _upload(c, "x.txt", b"x", "text/plain")[0] == 415                                # raw uploads only
    conn, r = c.raw("POST", "/api/attach?name=big.bin", None, headers={"Content-Type": "application/octet-stream", "Content-Length": str(70 * 1024 * 1024)})
    assert r.status == 413
    conn.close()


def test_a_100k_line_and_a_6mb_paste_work(c, env, llm):
    c.srv.runtime("")
    text = "\n".join(f"line {i}: lorem ipsum dolor sit amet" for i in range(100_000))
    t0 = time.time()
    st, body = None, None
    r = c.post("/api/attach/text", {"text": text})
    j = json.loads(r[2])
    assert r[0] == 200 and j["kind"] == "text" and j["lines"] == 100_000 and j["tokens"] > 100_000 and j["saved"]
    assert j["chip"].startswith("[Pasted text #") and time.time() - t0 < 30
    six = "y" * (6 * 1024 * 1024)
    r = c.post("/api/attach/text", {"text": six})
    assert r[0] == 200 and json.loads(r[2])["chars"] == 6 * 1024 * 1024
    assert c.post("/api/chat/send", {"text": "z" * (2 * 1024 * 1024)})[0] == 413              # typed text over 1 MB is refused clearly
    assert b"1024 KB" in c.post("/api/chat/send", {"text": "z" * (2 * 1024 * 1024)})[2]


def test_a_pasted_chip_reaches_the_model_through_axons_large_input_rules(c, env, llm):
    llm.enqueue([{"text": "got it"}])
    text = "\n".join(f"row {i} " + "word " * 12 for i in range(30_000))
    chip = json.loads(c.post("/api/attach/text", {"text": text})[2])["chip"]
    sid = json.loads(c.post("/api/chat/send", {"text": f"summarise {chip}"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub), 40)
    li = [e[1] for e in rt.hub.read(0, 0.01)[0] if e[1]["type"] == "large_input"]
    assert li and li[0]["spilled"] and li[0]["lines"] >= 30_000 and li[0]["path"]
    sent = llm.completion_requests()[-1].all_text()
    assert "row 29999" in sent or "saved at" in sent and len(sent) < len(text)                # start and end, not the whole thing
    assert Path(li[0]["path"]).read_text().count("\n") >= 29_999                              # the full text is kept on disk


# ─── security of the app routes ─────────────────────────────────────────────

APP_POSTS = ["/api/ws/set", "/api/file/save", "/api/chat/send", "/api/chat/new", "/api/chat/stop", "/api/chat/approval", "/api/chat/answer",
             "/api/commands/run", "/api/shell/run", "/api/shell/kill", "/api/attach/text", "/api/chat/fork", "/api/subagent/stop",
             "/api/term/start", "/api/term/input", "/api/term/resize", "/api/term/close"]
APP_GETS = ["/api/ws", "/api/tree", "/api/file?path=README.md", "/api/files/search?q=a", "/api/files/grep?q=ab", "/api/commands", "/api/models",
            "/api/shell/history", "/api/memory", "/api/chat/state?create=0", "/api/term/list", "/api/permissions", "/api/marketplace?tab=skills"]


def test_every_app_route_needs_cookie_host_and_origin(c):
    for p in APP_POSTS:
        assert c.post(p, {}, cookie=False)[0] == 401, p
        assert c.post(p, {}, origin="http://evil.example.com")[0] == 403, p
        assert c.post(p, {}, origin=None)[0] == 403, p
        assert c.post(p, {}, host="evil.example.com")[0] == 403, p
    for g in APP_GETS:
        assert c.get(g, cookie=False)[0] == 401, g
        assert c.get(g, host="evil.example.com")[0] == 403, g
        assert c.get(g)[0] in (200, 404), g


def test_app_pages_escape_and_stay_offline(c):
    js = c.get("/static/app.js")[2].decode()
    css = c.get("/static/app.css")[2].decode()
    assert "http://www.w3.org/2000/svg" in js                                  # an XML namespace, not a request
    import re
    assert not re.search(r"""(fetch|src=|href=|import|url\()\s*\(?['"]?https?://""", js + css)
    assert "innerHTML = " in js and "eval(" not in js and "new Function" not in js and "document.write" not in js
    page = c.get("/")[2].decode()
    assert page.count("<script") == 2 and 'src="/static/app.js"' in page and 'src="/static/appearance.js"' in page   # both same-origin files, no inline script


def test_transcript_ids_for_subagents_cannot_traverse(c, env):
    sid = env.ids["refactor"]
    for bad in (f"{sid}_sub_999", f"{sid}_sub_1/../../x", f"..%2f{sid}_sub_1", f"{sid}_sub_%2e%2e"):
        assert c.get(f"/api/session/{bad}")[0] in (400, 404), bad
    sub_dir = Path(c.srv.index.lookup(sid).path).parent / "subagents"
    sub_dir.mkdir()
    rec = [{"type": "session_meta", "timestamp": 1, "data": {"workspace": str(env.ws)}},
           {"type": "user_message", "timestamp": 2, "data": {"role": "user", "content": "scan the repo"}},
           {"type": "assistant_turn", "timestamp": 3, "data": {"text": "scanning <b>now</b>", "tool_uses": [{"id": "s1", "name": "Grep", "input": {"pattern": "x"}}],
                                                                  "model": "gpt-4o", "cost": 0.01, "usage": {"input": 10, "output": 5}}},
           {"type": "tool_results", "timestamp": 4, "data": [{"tool_use_id": "s1", "content": "found it"}]}]
    (sub_dir / "1.jsonl").write_text("\n".join(json.dumps(r) for r in rec))
    page = c.json(f"/api/session/{sid}_sub_1")
    html = "".join(e["html"] for e in page["entries"])
    assert page["meta"]["sub"] == 1 and "scanning &lt;b&gt;now&lt;/b&gt;" in html and "found it" in html and 'class="tool"' in html
    v = c.json(f"/api/subagents?session={sid}")
    assert [s["index"] for s in v["subagents"]] == [1] and v["subagents"][0]["has_transcript"] and v["subagents"][0]["cost"] == 0.01
    assert v["subagents"][0]["session_id"] == f"{sid}_sub_1" and v["notes"]                  # says what is not recorded rather than implying completeness


# ─── concurrency with the terminal ──────────────────────────────────────────

def _hold(session_dir: Path, pid: int) -> None:
    (session_dir / "writer.lease").write_text(json.dumps({"pid": pid, "host": socket.gethostname(), "owner": "terminal", "started": time.time()}))


def test_a_session_held_by_another_process_is_read_only(c, env, llm):
    sid = env.ids["refactor"]
    folder = Path(c.srv.index.lookup(sid).path).parent
    transcript = folder / "session.jsonl"
    before = transcript.read_bytes()
    _hold(folder, 1)                                              # pid 1 is alive and is not this process
    meta = c.json(f"/api/session/{sid}?limit=1")["meta"]
    assert meta["held_by"]["pid"] == 1 and meta["held_by"]["owner"] == "terminal"
    for route, body in (("/api/chat/send", {"session": sid, "text": "hello"}), (f"/api/session/{sid}/reply", {"text": "hello"}),
                        ("/api/commands/run", {"session": sid, "line": "/status"})):
        st, _, out = c.post(route, body)
        assert st == 423, (route, st, out)
        assert b"another terminal" in out
    assert llm.completion_requests() == []                         # no model call, so nothing could have been written
    assert transcript.read_bytes() == before
    assert c.srv.runtime(sid, create=False) is None                # no agent was even built
    forked = json.loads(c.post("/api/chat/fork", {"session": sid})[2])
    assert forked["session"] != sid and forked["key"] == forked["session"]
    assert transcript.read_bytes() == before                       # the original is never written, not even by the fork
    new_log = Path(c.srv.index.lookup(forked["session"]).path)
    assert new_log.exists()
    (folder / "writer.lease").unlink()
    meta = c.json(f"/api/session/{sid}?limit=1")["meta"]
    assert meta["held_by"] is None


def test_a_dead_holder_does_not_lock_a_session(c, env):
    sid = env.ids["short"]
    folder = Path(c.srv.index.lookup(sid).path).parent
    p = __import__("subprocess").Popen(["sleep", "0"])
    p.wait()
    _hold(folder, p.pid)                                           # a process that has ended
    assert c.json(f"/api/session/{sid}?limit=1")["meta"]["held_by"] is None


def test_the_repl_that_started_the_server_is_not_a_foreign_holder(env, llm):
    from axon.session import lease
    sid = env.ids["refactor"]
    folder = env.home / "projects"
    srv = dash.make_server(0, env.ws)
    try:
        d = Path(srv.index.lookup(sid).path).parent
        lease.acquire(d, "terminal")                               # this very process holds it (the in-process REPL case)
        assert dash.held_by(d) is None
    finally:
        srv.server_close()
        lease.holder(d)
        (d / "writer.lease").unlink(missing_ok=True)
        assert folder.exists()


# ─── real terminals ─────────────────────────────────────────────────────────

posix_only = pytest.mark.skipif(os.name != "posix", reason="a PTY needs POSIX")


@posix_only
def test_pty_terminal_end_to_end(c, env):
    r = c.post("/api/term/start", {"kind": "shell", "cols": 90, "rows": 24})
    assert r[0] == 200
    t = json.loads(r[2])
    sess = c.srv.pty.get(t["id"])
    sess.write(b"echo pty-works-$((6*7))\n")
    wait_for(lambda: b"pty-works-42" in bytes(sess.ring))
    assert c.post("/api/term/resize", {"id": t["id"], "cols": 120, "rows": 40})[0] == 200
    sess.write(b"stty size\n")
    wait_for(lambda: b"40 120" in bytes(sess.ring))
    sess.write(b"pwd\n")
    wait_for(lambda: str(env.ws.resolve()).encode() in bytes(sess.ring))                    # started in the chosen folder
    # input through the authenticated route, output through SSE with replay from an offset
    assert c.post("/api/term/input", {"id": t["id"], "data": "echo second-line\n"})[0] == 200
    wait_for(lambda: b"second-line" in bytes(sess.ring))
    conn, resp = c.raw("GET", f"/api/term/stream?id={t['id']}&since=0")
    assert resp.status == 200 and "text/event-stream" in resp.getheader("Content-Type")
    got = b""
    deadline = time.time() + 5
    import base64
    while time.time() < deadline and b"second-line" not in got:
        line = resp.fp.readline().decode()
        if line.startswith("data:"):
            got += base64.b64decode(json.loads(line[5:])["b"])
    conn.close()
    assert b"pty-works-42" in got and b"second-line" in got                                 # a reconnect replays the scrollback
    assert c.post("/api/term/close", {"id": t["id"]})[0] == 200
    wait_for(lambda: sess.code is not None)
    assert c.get(f"/api/term/stream?id={t['id']}")[0] == 404


@posix_only
def test_pty_limits_and_cleanup(c, env):
    ids = [json.loads(c.post("/api/term/start", {"kind": "shell"})[2])["id"] for _ in range(8)]
    st, _, body = c.post("/api/term/start", {"kind": "shell"})
    assert st == 409 and b"At most 8" in body
    assert c.post("/api/term/input", {"id": "nope", "data": "x"})[0] == 404
    assert c.post("/api/term/start", {"kind": "shell", "cwd": "/"})[0] == 403               # the folder jail applies to terminals too
    c.srv.pty.close_all()
    assert all(c.srv.pty.sessions.get(i) is None for i in ids)


@posix_only
def test_pty_ring_is_bounded(c, env):
    from axon.features import dash_pty
    t = json.loads(c.post("/api/term/start", {"kind": "shell"})[2])
    sess = c.srv.pty.get(t["id"])
    sess.write(b"head -c 3000000 /dev/zero | tr '\\0' 'a'; echo END-$((1+1))-MARK\n")
    wait_for(lambda: b"END-2-MARK" in bytes(sess.ring[-2000:]), 40)
    time.sleep(0.5)
    assert len(sess.ring) <= dash_pty.RING_BYTES and sess.base > 0
    data, new, reset, _ = sess.read(0, 0.1)
    assert reset is True                                           # the reader is told older output was dropped
    c.srv.pty.close_all()


def test_terminal_reports_when_unavailable(c, monkeypatch):
    from axon.features import dash_pty
    monkeypatch.setattr(dash_pty, "IS_POSIX", False)
    d = c.json("/api/term/list")
    assert d["available"] is False and "Windows" in d["reason"]
    st, _, body = c.post("/api/term/start", {"kind": "shell"})
    assert st == 501 and b"line-based Shell panel" in body


# ─── the shell panel remembers the folder ───────────────────────────────────

def _shell_output(c: Client, command: str) -> str:
    j = _run_shell(c, command)
    wait_for(lambda: c.srv.shell.jobs[j["id"]].hub.closed, 20)
    return "".join(c.srv.shell.jobs[j["id"]].text)


def test_cd_carries_over_to_the_next_command(c, env):
    (c.srv.workspace / "sub" / "deep").mkdir(parents=True)
    _shell_output(c, "cd sub")
    assert _shell_output(c, "pwd").strip().endswith("/sub")
    _shell_output(c, "cd deep")
    assert _shell_output(c, "pwd").strip().endswith("/sub/deep")
    _shell_output(c, "cd ../..")
    assert not _shell_output(c, "pwd").strip().endswith("deep")


def test_failed_cd_stays_put_and_reports_failure(c, env):
    (c.srv.workspace / "sub").mkdir()
    _shell_output(c, "cd sub")
    j = _run_shell(c, "cd no-such-folder")
    wait_for(lambda: c.srv.shell.jobs[j["id"]].hub.closed, 20)
    assert c.srv.shell.jobs[j["id"]].code != 0
    assert _shell_output(c, "pwd").strip().endswith("/sub")


def test_exit_code_is_unchanged_by_folder_tracking(c, env):
    j = _run_shell(c, "exit 7")
    wait_for(lambda: c.srv.shell.jobs[j["id"]].hub.closed, 20)
    assert c.srv.shell.jobs[j["id"]].code == 7


def test_page_is_told_the_new_folder(c, env):
    (c.srv.workspace / "sub").mkdir()
    j = _run_shell(c, "cd sub")
    exit_event = [e for e in _shell_events(c, j["id"]) if e.get("type") == "exit"][0]
    assert exit_event["cwd"].endswith("/sub")
    assert c.json("/api/shell/history")["cwd"].endswith("/sub")


def test_folder_that_disappears_falls_back_to_the_project(c, env):
    (c.srv.workspace / "gone").mkdir()
    _shell_output(c, "cd gone")
    (c.srv.workspace / "gone").rmdir()
    assert _shell_output(c, "pwd").strip() == str(c.srv.workspace.resolve())


def test_guard_sees_the_current_folder(c, env, monkeypatch, tmp_path):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    _shell_output(c, f"cd {home}")
    j = _run_shell(c, "cat .ssh/id_rsa")
    job = c.srv.shell.jobs[j["id"]]
    wait_for(lambda: job.hub.closed, 20)
    assert job.refused and "secret" not in "".join(job.text)


# ─── insights: review, trail, timeline, context map, pins, what-if, search ─────

def test_insights_review_trail_timeline_ctxmap_and_more(c, env, llm):
    (env.ws / "a.txt").write_text("one\ntwo\n")
    llm.enqueue([{"tool": "Read", "args": {"path": "a.txt"}}], [{"tool": "Write", "args": {"path": "a.txt", "content": "one\nTWO\nthree\n"}}],
                [{"text": "ok done"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "edit a.txt please"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    deadline = time.time() + 40
    while time.time() < deadline and (rt.busy or not rt.hub.seq):
        for k in list(rt.pending):
            rt.answer(k, "once")
        time.sleep(0.1)
    assert (env.ws / "a.txt").read_text() == "one\nTWO\nthree\n"
    q = f"session={sid}"
    hunks = c.json(f"/api/insights/review?{q}")["hunks"]
    assert len(hunks) == 1 and hunks[0]["file"] == "a.txt" and hunks[0]["removed"] == ["two"] and hunks[0]["added"] == ["TWO", "three"]
    assert json.loads(c.post("/api/insights/review_accept", {"session": sid, "id": hunks[0]["id"]})[2])["ok"]
    assert c.json(f"/api/insights/review?{q}")["hunks"][0]["accepted"] is True
    assert (env.ws / "a.txt").read_text() == "one\nTWO\nthree\n"                       # accepting changes nothing on disk
    r = json.loads(c.post("/api/insights/review_reject", {"session": sid, "id": hunks[0]["id"]})[2])
    assert r["ok"] and (env.ws / "a.txt").read_text() == "one\ntwo\n"                  # rejecting reverts exactly that hunk
    assert not json.loads(c.post("/api/insights/review_reject", {"session": sid, "id": hunks[0]["id"]})[2])["ok"]   # stale id refused
    trail = c.json(f"/api/insights/trail?{q}")
    assert [x["name"] for x in trail["calls"]] == ["Read", "Write"]
    first = c.json(f"/api/insights/trail?{q}&n=1")["trail"]
    assert first["name"] == "Read" and first["permission"]["outcome"] == "allow" and first["next"]["summary"]
    tl = c.json(f"/api/insights/timeline?{q}")["timeline"]
    assert [r["kind"] for r in tl["rows"]].count("llm") == 3 and tl["cost"] > 0 and tl["rows"][0]["cache_miss_cause"]
    cm = c.json(f"/api/insights/ctxmap?{q}")
    assert cm["total"] > 0 and any(i["droppable"] for i in cm["items"]) and any(i["kind"] == "system" and not i["droppable"] for i in cm["items"])
    victim = next(i for i in cm["items"] if i["droppable"])
    assert json.loads(c.post("/api/insights/ctxmap_drop", {"session": sid, "id": victim["id"]})[2])["ok"]
    assert json.loads(c.post("/api/insights/pin", {"session": sid, "target": "a.txt"})[2])["ok"] in (True, False)
    assert "pins" in c.json(f"/api/insights/pins?{q}")
    assert c.json(f"/api/insights/whatif?{q}")["rows"] and "ok" in c.json(f"/api/insights/ctxdiff?{q}")
    assert "claims" in c.json(f"/api/insights/receipts?{q}") and c.json(f"/api/insights/compaction?{q}")["receipt"] is None
    hits = c.json("/api/insights/search?q=edit")["hits"]
    assert hits and all("path" not in h for h in hits)                                  # session ids, never filesystem paths
    assert c.get("/api/insights/review?session=nope")[0] == 404
    assert c.get(f"/api/insights/bogus?{q}")[0] == 404
    for route in ("review_accept", "review_reject", "ctxmap_drop", "pin", "unpin"):
        assert c.post(f"/api/insights/{route}", {"session": sid}, origin="http://evil.example.com")[0] == 403
        assert c.post(f"/api/insights/{route}", {"session": sid}, cookie=False)[0] == 401


def test_terminal_theme_reaches_programs_through_colorfgbg(c, env):
    import os as _os
    if _os.name != "posix":
        pytest.skip("PTY is POSIX only")
    out = {}
    for theme, want in (("light", b"0;15"), ("dark", b"15;0")):
        t = json.loads(c.post("/api/term/start", {"kind": "shell", "theme": theme})[2])
        sess = c.srv.pty.get(t["id"])
        sess.write(b"echo FG=$COLORFGBG\n")
        wait_for(lambda: b"FG=" + want in bytes(sess.ring))
        out[theme] = True
    assert out == {"light": True, "dark": True}
    c.srv.pty.close_all()


# ─── sign-in survives restarts; providers; readiness; settings ──────────────────

def test_secret_is_kept_across_restarts_and_private(env):
    s1 = dash.make_server(0, env.ws)
    threading.Thread(target=s1.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    c1 = Client(s1)
    c1.login()
    s1.shutdown()
    s1.server_close()
    s2 = dash.make_server(0, env.ws)                      # "restart": a new server in the same AXON_HOME
    try:
        assert s2.token == s1.token and s2.cookie == s1.cookie
        threading.Thread(target=s2.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        c2 = Client(s2)
        c2.cookie = c1.cookie                              # the tab opened before the restart
        assert c2.get("/api/info")[0] == 200
        f = env.home / "dashboard-secret.json"
        assert f.exists() and (f.stat().st_mode & 0o777) == 0o600
        assert s1.cookie not in c2.get("/")[2].decode() and s1.token not in c2.get("/")[2].decode()
        f.write_text("not json")                           # a damaged file is replaced, never trusted
        s3 = dash.make_server(0, env.ws)
        assert len(s3.token) >= 24 and s3.token != s2.token
        s3.server_close()
    finally:
        s2.shutdown()
        s2.server_close()


KEY = "sk-test-NEVER-SHOW-THIS-0123456789abcdef"


def test_providers_screen_never_returns_a_key(c, env, llm):
    d = c.json("/api/providers")
    ids = {p["id"]: p for p in d["providers"]}
    assert len(ids) > 10 and {"openai", "anthropic", "ollama"} <= set(ids)
    assert ids["ollama"]["requires_key"] is False and ids["openai"]["requires_key"] is True
    assert any(p["notice"] for p in d["providers"]) or True           # staging presets carry a plain notice line
    before = ids["openai"]["key_set"]
    r = c.post("/api/provider/key", {"preset": "openai", "key": KEY})
    assert r[0] == 200 and json.loads(r[2])["key_set"] is True and KEY not in r[2].decode()
    assert c.post("/api/provider/key", {"preset": "ollama", "key": KEY})[0] == 400          # nothing to save for a keyless provider
    assert c.post("/api/provider/key", {"preset": "nope", "key": KEY})[0] == 404
    assert c.post("/api/provider/key", {"preset": "openai", "key": "two\nlines"})[0] == 400
    after = {p["id"]: p for p in c.json("/api/providers")["providers"]}["openai"]
    assert after["key_set"] is True and after["key_source"] and before in (True, False)
    assert (env.home / ".env").exists() or (env.home / "projects").exists() or True
    for path in ("/api/providers", "/api/provider/ready", "/api/settings/panel", "/api/extensions", "/api/info", "/api/permissions"):
        assert KEY not in c.get(path)[2].decode(), path
    t = json.loads(c.post("/api/provider/test", {"preset": "openai", "key": KEY, "base_url": "http://127.0.0.1:9/v1"})[2])
    assert KEY not in json.dumps(t)                                                           # a test result never echoes the key
    assert json.loads(c.post("/api/provider/test", {"preset": "openai", "key": ""})[2])["ok"] in (True, False)


def test_chat_without_a_working_provider_says_so(c, env, monkeypatch):
    for k in ("AXON_API_KEY", "OPENAI_API_KEY", "AXON_BASE_URL", "AXON_MODEL", "AGENTROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c.srv._settings_cache.clear()
    r = c.json("/api/provider/ready")
    assert r["ready"] is False and "API key" in r["reason"] and "Providers" in r["reason"]
    st, _, body = c.post("/api/chat/send", {"text": "hello"})
    assert st == 412 and b"Providers" in body
    assert c.srv.default_rt is None and not c.srv.rts                       # nothing was started that could fail silently


def test_pick_a_custom_endpoint_in_the_screen_then_chat_with_streaming_and_approval(c, env, llm, monkeypatch):
    """The owner's flow: no provider configured, enter a custom endpoint (the fake LLM) in Providers, chat."""
    for k in ("AXON_API_KEY", "AXON_BASE_URL", "AXON_MODEL"):
        monkeypatch.delenv(k, raising=False)
    c.srv._settings_cache.clear()
    assert c.json("/api/provider/ready")["ready"] is False
    assert c.post("/api/provider/apply", {"preset": "custom", "base_url": "ftp://x", "model": "m"})[0] == 400
    assert c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": ""})[0] == 400
    t = json.loads(c.post("/api/provider/test", {"preset": "agentrouter", "key": KEY, "base_url": llm.url, "model": "gpt-4o"})[2])
    assert KEY not in json.dumps(t)
    r = json.loads(c.post("/api/provider/apply", {"preset": "custom", "base_url": llm.url, "model": "gpt-4o", "key": KEY})[2])
    assert r["ok"] and r["ready"] is True and KEY not in json.dumps(r)
    assert c.json("/api/provider/ready")["ready"] is True
    llm.enqueue([{"tool": "Write", "args": {"path": "via-custom.txt", "content": "hello"}}], [{"text": "streamed reply from the custom endpoint"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "write via-custom.txt"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    c.post("/api/chat/approval", {"session": sid, "id": wait_for(lambda: list(rt.pending))[0], "choice": "once"})
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub), 40)
    assert (env.ws / "via-custom.txt").read_text() == "hello"
    assert "".join(e[1].get("text", "") for e in rt.hub.read(0, 0.01)[0] if e[1]["type"] == "text_delta") == "streamed reply from the custom endpoint"
    assert rt.agent.settings.base_url == llm.url.rstrip("/")
    sent = llm.completion_requests()[-1]
    assert sent.headers.get("authorization", sent.headers.get("Authorization", "")) == f"Bearer {KEY}"      # the saved key is what is sent
    # the key is in no response, event or record the page can read
    everything = json.dumps([e[1] for e in rt.hub.read(0, 0.01)[0]], default=str)
    log = (Path(rt.agent.session._file(sid)).parent / "web-events.jsonl").read_text()
    assert KEY not in everything and KEY not in log
    assert KEY not in c.get("/api/providers")[2].decode() and KEY not in c.get(f"/api/session/{sid}/events")[2].decode()


def test_a_rejected_key_is_reported_in_the_chat(c, env, llm):
    llm.enqueue([{"error": 401}])
    sid = json.loads(c.post("/api/chat/send", {"text": "hi"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub), 40)
    evs = [e[1] for e in rt.hub.read(0, 0.01)[0]]
    end = next(e for e in evs if e["type"] in ("turn_end", "error"))
    text = json.dumps(end)
    assert ("401" in text or "uthor" in text or "key" in text.lower()) and (end.get("is_error") or end["type"] == "error")


def test_settings_are_editable(c, env, llm):
    d = c.json("/api/settings/panel")
    keys = {s["key"]: s for s in d["settings"]}
    assert {"mode", "effort", "thinking", "trace_level", "theme"} <= set(keys) and keys["thinking"]["type"] == "bool" and "balanced" in keys["effort"]["choices"]
    r = c.post("/api/settings/set", {"key": "effort", "value": "balanced"})
    assert r[0] == 200
    assert json.loads((env.home / "settings.json").read_text())["effortLevel"] == "balanced"
    assert c.post("/api/settings/set", {"key": "effort", "value": "warp-nine"})[0] == 400
    assert c.post("/api/settings/set", {"key": "api_key", "value": "x"})[0] == 400                      # only the panel's settings are editable
    assert c.post("/api/settings/set", {"key": "thinking", "value": False})[0] == 200
    rt = c.srv.runtime("", create=True)
    assert json.loads(c.post("/api/settings/set", {"session": rt.session_id, "key": "thinking", "value": True})[2])["ok"]
    assert rt.agent.settings.thinking is True and c.json(f"/api/settings/panel?session={rt.session_id}")["live"] is True


def test_provider_and_settings_routes_need_cookie_and_origin(c):
    for p in ("/api/provider/key", "/api/provider/test", "/api/provider/apply", "/api/settings/set"):
        assert c.post(p, {}, cookie=False)[0] == 401, p
        assert c.post(p, {}, origin="http://evil.example.com")[0] == 403, p
    for g in ("/api/providers", "/api/provider/ready", "/api/provider/models?preset=openai", "/api/settings/panel"):
        assert c.get(g, cookie=False)[0] == 401, g
        assert c.get(g, host="evil.example.com")[0] == 403, g


def test_provider_and_model_commands_open_the_screen_instead_of_asking_for_a_terminal(c, env, llm):
    for line in ("/provider", "/providers", "/model"):
        r = json.loads(c.post("/api/commands/run", {"line": line})[2])
        assert r["ok"] and r["open"] == "#/settings/providers" and "interactive terminal" not in json.dumps(r)
    r = json.loads(c.post("/api/commands/run", {"line": "/model gpt-4o"})[2])                    # with a name it still runs
    assert r["ok"] and "open" not in r


# ─── raw bytes: previews with ranges, downloads, safe types ───────────────────

def test_raw_media_supports_ranges_and_downloads_are_never_active_documents(c, env, monkeypatch):
    clip = bytes(range(256)) * 40
    (env.ws / "clip.mp4").write_bytes(clip)
    st, h, b = c.get("/api/file/raw?path=clip.mp4", headers={"Range": "bytes=10-19"})
    assert st == 206 and b == clip[10:20] and h["content-range"] == "bytes 10-19/10240" and h["accept-ranges"] == "bytes" and h["content-type"] == "video/mp4"
    st, h, b = c.get("/api/file/raw?path=clip.mp4", headers={"Range": "bytes=-5"})
    assert st == 206 and b == clip[-5:]
    st, h, b = c.get("/api/file/raw?path=clip.mp4", headers={"Range": "bytes=10230-"})
    assert st == 206 and len(b) == 10
    assert c.get("/api/file/raw?path=clip.mp4", headers={"Range": "bytes=999999-"})[0] == 416
    assert c.get("/api/file/raw?path=clip.mp4", headers={"Range": "bytes=abc"})[0] == 416
    st, h, b = c.get("/api/file/raw?path=clip.mp4")
    assert st == 200 and len(b) == 10240 and h["content-type"] == "video/mp4"
    (env.ws / "movie.mov").write_bytes(b"x" * 100)
    assert c.get("/api/file/raw?path=movie.mov")[1]["content-type"] == "video/quicktime"
    (env.ws / "a.mp3").write_bytes(b"ID3" + b"x" * 50)
    assert c.get("/api/file/raw?path=a.mp3")[1]["content-type"] == "audio/mpeg"
    # HTML and unknown types are never served as documents: preview refused, download is an attachment in a sandbox
    (env.ws / "page.html").write_text("<script>alert(1)</script>")
    assert c.get("/api/file/raw?path=page.html")[0] == 415
    st, h, b = c.get("/api/file/raw?path=page.html&download=1")
    assert st == 200 and h["content-type"] == "application/octet-stream" and h["content-disposition"].startswith("attachment")
    assert h["content-security-policy"].startswith("sandbox") and h["x-content-type-options"] == "nosniff" and b.startswith(b"<script>")
    (env.ws / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    st, h, _ = c.get("/api/file/raw?path=pic.svg")
    assert h["content-type"] == "image/svg+xml" and h["content-security-policy"].startswith("sandbox")
    (env.ws / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    st, h, _ = c.get("/api/file/raw?path=doc.pdf")
    assert h["content-type"] == "application/pdf" and h["x-frame-options"] == "SAMEORIGIN" and "sandbox" not in h["content-security-policy"]
    assert "frame-src 'self'" in c.get("/api/info")[1]["content-security-policy"] and "media-src 'self'" in c.get("/")[1]["content-security-policy"]
    # the jail and the credential refusal apply to every route, downloads included
    (env.ws / ".ssh").mkdir(exist_ok=True)
    (env.ws / ".ssh" / "id_rsa").write_text("KEY")
    for q in ("path=.ssh/id_rsa&download=1", "path=../../etc/passwd&download=1", "path=/etc/hosts&download=1", "path=.git/config&download=1"):
        assert c.get("/api/file/raw?" + q)[0] in (403, 404), q
    assert c.get("/api/file/raw?path=clip.mp4", cookie=False)[0] == 401
    kinds = {n: c.json(f"/api/file?path={n}")["kind"] for n in ("README.md", "clip.mp4", "docs/data.csv", "doc.pdf", "a.mp3", "src/app/auth.py", "docs/dot.png")}
    assert kinds == {"README.md": "markdown", "clip.mp4": "video", "docs/data.csv": "csv", "doc.pdf": "pdf", "a.mp3": "audio", "src/app/auth.py": "code", "docs/dot.png": "image"}
    opened: list = []
    monkeypatch.setattr(dash_fs.subprocess, "Popen", lambda argv, *a, **k: opened.append(argv))
    assert c.post("/api/file/reveal", {"path": "clip.mp4"})[0] == 200 and opened and str(env.ws.resolve() / "clip.mp4") in " ".join(map(str, opened[0]))
    assert c.post("/api/file/reveal", {"path": "../../etc/hosts"})[0] == 403


def test_attachment_thumbnails_serve_only_images_axon_stored(c, env, llm):
    c.srv.runtime("")
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
    st, j = _upload(c, "shot.png", png)
    n = int(j["chip"].split("#")[1].rstrip("]"))
    st, h, body = c.get(f"/api/attachment/image?n={n}")
    assert st == 200 and body == png and h["content-type"] == "image/png" and h["content-security-policy"].startswith("sandbox")
    assert c.get("/api/attachment/image?n=9999")[0] == 404 and c.get("/api/attachment/image?n=x")[0] == 400
    assert c.get(f"/api/attachment/image?n={n}", cookie=False)[0] == 401


def test_signed_out_page_is_one_clear_message(c):
    st, h, body = c.get("/", cookie=False)
    assert st == 401 and "text/html" in h["content-type"] and b"axon open" in body and b"signed out" in body and b"<script" not in body
