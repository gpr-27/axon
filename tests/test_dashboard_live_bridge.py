"""A page attached to a real REPL (through a pseudo-terminal): the terminal's main loop does what the page asks, so the
two never draw over each other, and chats and folders changed on either side show on both."""
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

from fake_llm import FakeLLMServer  # noqa: E402
from harness import create_axon_env  # noqa: E402
from test_dashboard_mirror import Web, _wait  # noqa: E402
from test_e2e_d_helpers import open_repl, posix_only  # noqa: E402

pytestmark = [pytest.mark.e2e, posix_only]


def _read_in_background(s) -> threading.Event:
    """A real terminal always reads. These tests block in HTTP calls that wait for the REPL, and a REPL whose output
    nobody reads stalls when the pty buffer fills, so the reading moves to its own thread."""
    pump, stop = s._pump, threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                pump(0.05)
            except OSError:      # the session was closed
                return

    def wait_for_output(timeout: float) -> bool:
        before, deadline = len(s._raw), time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(s._raw) != before:
                return True
            time.sleep(0.01)
        return False

    s._pump = wait_for_output
    threading.Thread(target=reader, daemon=True).start()
    return stop


@pytest.fixture
def rig(tmp_path):
    llm = FakeLLMServer(model_ids=("gpt-4o",)).start()
    env = create_axon_env(tmp_path, llm)
    s = open_repl(env)
    stop = _read_in_background(s)
    try:
        mark = s.mark()
        s.send("/open --no-open\r")
        screen = s.expect(r"Axon dashboard: http://127\.0\.0\.1:\d+/\?t=\S+", timeout=20, since=mark)
        web = Web(re.search(r"http://127\.0\.0\.1:\d+/\?t=\S+", screen[mark:]).group(0))
        s.ready(since=mark)
        yield s, web, llm, env
    finally:
        stop.set()
        s.close()
        llm.stop()


def _state(web: Web, sid: str = "") -> dict:
    return web.req("GET", "/api/chat/state?create=0" + (f"&session={sid}" if sid else ""))[1]


def _idle(web: Web, sid: str) -> None:
    _wait(lambda: not _state(web, sid).get("busy"))


def test_a_page_turn_does_not_draw_over_the_prompt_and_keeps_the_draft(rig):
    s, web, llm, _ = rig
    sid = _state(web)["session"]
    s.type("half typed draft")
    llm.enqueue([{"text": "PAGE-REPLY"}, {"delay": 0.3}, {"text": " MORE"}])
    mark = s.mark()
    code, res = web.req("POST", "/api/chat/send", {"session": sid, "text": "from the page"})
    assert code == 200 and res["queued"] is False
    assert _state(web, sid)["busy"] is True          # a second message sent right now would queue
    s.expect("PAGE-REPLY", timeout=30, since=mark)
    _idle(web, sid)
    s.expect("half typed draft", timeout=15, since=mark + 1)
    s.drain(0.4)
    shown = s.screen[mark:]
    assert "From the web page" in shown
    between = shown[shown.index("From the web page"):shown.index("PAGE-REPLY")]
    assert "Ask anything" not in between and "half typed draft" not in between   # the prompt stepped aside first
    llm.enqueue([{"text": "DRAFT-REPLY"}])
    mark = s.mark()
    s.send("\r")
    s.expect("DRAFT-REPLY", timeout=30, since=mark)
    assert llm.completion_requests()[-1].last_user_text() == "half typed draft"


def test_a_message_sent_during_a_page_turn_queues_and_then_runs(rig):
    s, web, llm, _ = rig
    sid = _state(web)["session"]
    llm.enqueue([{"text": "FIRST"}, {"delay": 1.0}, {"text": " DONE"}], [{"text": "SECOND-REPLY"}])
    mark = s.mark()
    web.req("POST", "/api/chat/send", {"session": sid, "text": "one"})
    code, res = web.req("POST", "/api/chat/send", {"session": sid, "text": "two", "steer": False})
    assert code == 200 and res["queued"] is True
    s.expect("SECOND-REPLY", timeout=40, since=mark)
    _wait(lambda: not _state(web, sid).get("busy") and not _state(web, sid).get("queue"))


def test_chats_switched_on_the_page_move_the_terminal_and_back(rig):
    s, web, llm, env = rig
    first = _state(web)["session"]
    seen: list[dict] = []
    web.stream(f"/api/events?session={first}&since=0", seen)
    llm.enqueue([{"text": "ANSWER-IN-FIRST-CHAT"}])
    mark = s.mark()
    web.req("POST", "/api/chat/send", {"session": first, "text": "the first chat"})
    s.expect("ANSWER-IN-FIRST-CHAT", timeout=30, since=mark)
    _idle(web, first)

    mark = s.mark()
    code, st = web.req("POST", "/api/chat/new", {})
    assert code == 200 and st["external"] is True and st["session"] != first
    second = st["session"]
    s.expect("New session", timeout=15, since=mark)
    assert _state(web)["session"] == second
    _wait(lambda: any(e["type"] == "session_changed" and e["session"] == second for e in seen))

    llm.enqueue([{"text": "ANSWER-IN-SECOND-CHAT"}])
    mark = s.mark()
    web.req("POST", "/api/chat/send", {"session": second, "text": "the second chat"})
    s.expect("ANSWER-IN-SECOND-CHAT", timeout=30, since=mark)
    _idle(web, second)

    mark = s.mark()
    code, st = web.req("POST", "/api/live/switch", {"session": first})
    assert code == 200 and st["session"] == first
    s.expect("ANSWER-IN-FIRST-CHAT", timeout=15, since=mark)     # the terminal shows the chat it moved to
    assert _state(web)["session"] == first

    # sending to the other chat moves the terminal there, and only one agent ever wrote
    llm.enqueue([{"text": "BACK-IN-SECOND"}])
    mark = s.mark()
    code, _ = web.req("POST", "/api/chat/send", {"session": second, "text": "continue the second"})
    assert code == 200
    s.expect("BACK-IN-SECOND", timeout=30, since=mark)
    transcript = next(Path(env.axon_home).rglob(f"{second}/session.jsonl"))
    users = [json.loads(line)["data"]["content"] for line in transcript.read_text().splitlines() if '"user_message"' in line]
    assert users == ["the second chat", "continue the second"]


def test_a_terminal_session_change_reaches_the_page(rig):
    s, web, _, _ = rig
    first = _state(web)["session"]
    seen: list[dict] = []
    web.stream(f"/api/events?session={first}&since=0", seen)
    s.command("/clear", "Run it")       # a typed /clear asks first, then asks to confirm
    mark = s.mark()
    s.send("\r")
    s.expect("Yes", timeout=15, since=mark)
    s.send("\x1b[B\r")
    s.expect("New session", timeout=15, since=mark)
    now = _wait(lambda: next((e["session"] for e in seen if e["type"] == "session_changed"), None))
    assert now != first and _state(web)["session"] == now


def test_switching_chats_is_refused_while_the_terminal_works(rig):
    s, web, llm, _ = rig
    sid = _state(web)["session"]
    llm.enqueue([{"text": "SLOW"}, {"delay": 2.0}, {"text": " END"}])
    mark = s.mark()
    web.req("POST", "/api/chat/send", {"session": sid, "text": "go"})
    code, res = web.req("POST", "/api/live/switch", {"session": "session_other"})
    assert code == 409 and "working" in res["error"]
    s.expect("SLOW", timeout=30, since=mark)
    _idle(web, sid)


def test_any_folder_can_be_opened_from_the_page_and_the_terminal_moves(rig, tmp_path):
    s, web, llm, env = rig
    other = tmp_path / "other project"
    other.mkdir()
    (other / "marker.txt").write_text("x")
    info = web.req("GET", "/api/ws")[1]
    assert info["can_switch"] is True and Path(info["workspace"]) == Path(env.workspace).resolve()
    first = _state(web)["session"]

    mark = s.mark()
    code, res = web.req("POST", "/api/ws/set", {"path": str(other)})
    assert code == 200, res
    s.expect("New session", timeout=15, since=mark)
    info = web.req("GET", "/api/ws")[1]
    assert Path(info["workspace"]) == other.resolve() and info["live"] is True
    st = _state(web)
    assert st["session"] != first and st["external"] is True
    assert any(e["name"] == "marker.txt" for e in web.req("GET", "/api/tree")[1]["entries"])

    llm.enqueue([{"tool": "Bash", "args": {"command": "pwd", "description": "where"}}], [{"text": "IN-THE-NEW-FOLDER"}])
    mark = s.mark()
    web.req("POST", "/api/chat/send", {"session": st["session"], "text": "where are you"})
    s.expect("IN-THE-NEW-FOLDER", timeout=30, since=mark)
    assert str(other.resolve()) in "\n".join(llm.completion_requests()[-1].tool_results())


def test_a_terminal_cd_reaches_the_page(rig, tmp_path):
    s, web, _, _ = rig
    other = tmp_path / "elsewhere"
    other.mkdir()
    s.command(f"/cd {other}", "Memory, permissions")
    assert Path(web.req("GET", "/api/ws")[1]["workspace"]) == other.resolve()


def test_protected_and_missing_folders_are_still_refused(rig, tmp_path):
    _, web, _, env = rig
    code, _ = web.req("POST", "/api/ws/set", {"path": str(tmp_path / "does-not-exist")})
    assert code == 404
    code, _ = web.req("POST", "/api/ws/set", {"path": "/"})
    assert code in (400, 403)
    assert Path(web.req("GET", "/api/ws")[1]["workspace"]) == Path(env.workspace).resolve()
