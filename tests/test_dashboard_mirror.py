"""`/open` from a real REPL (through a pseudo-terminal): the page mirrors the live session, whether a turn starts in the
terminal or in the browser, and there is only ever one writer."""
from __future__ import annotations

import http.client
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
from test_e2e_d_helpers import PROMPT_READY, open_repl, posix_only, tool_turn  # noqa: E402

pytestmark = [pytest.mark.e2e, posix_only]


class Web:
    def __init__(self, url: str) -> None:
        m = re.match(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", url)
        self.port, self.token = int(m.group(1)), m.group(2)  # type: ignore[union-attr]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", f"/?t={self.token}", headers={"Host": f"127.0.0.1:{self.port}"})
        r = conn.getresponse()
        r.read()
        self.cookie = re.search(r"axon_dash=([^;]+)", r.getheader("Set-Cookie")).group(1)  # type: ignore[union-attr]
        conn.close()

    def req(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Host": f"127.0.0.1:{self.port}", "Cookie": f"axon_dash={self.cookie}", "Origin": f"http://127.0.0.1:{self.port}"}
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.read()
        conn.close()
        return r.status, json.loads(out) if out[:1] in (b"{", b"[") else out

    def stream(self, path: str, sink: list) -> threading.Thread:
        def run() -> None:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "Cookie": f"axon_dash={self.cookie}"})
            r = conn.getresponse()
            while True:
                line = r.fp.readline()
                if not line:
                    return
                if line.startswith(b"data:"):
                    sink.append(json.loads(line[5:]))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t


def _wait(fn, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_open_from_a_running_repl_mirrors_terminal_and_web_turns(tmp_path):
    llm = FakeLLMServer(model_ids=("gpt-4o",)).start()
    env = create_axon_env(tmp_path, llm)
    s = open_repl(env)
    try:
        mark = s.mark()
        s.send("/open --no-open\r")
        screen = s.expect(r"Axon dashboard: http://127\.0\.0\.1:\d+/\?t=\S+", timeout=20, since=mark)
        url = re.search(r"http://127\.0\.0\.1:\d+/\?t=\S+", screen[mark:]).group(0)
        web = Web(url)
        assert web.req("GET", "/api/info")[1]["live"] is True
        st = web.req("GET", "/api/chat/state?create=0")[1]
        assert st["external"] is True and st["session"]
        sid = st["session"]
        seen: list[dict] = []
        web.stream(f"/api/events?session={sid}&since=0", seen)

        # 1. a turn started in the terminal shows up in the page as it happens
        llm.enqueue(*tool_turn("echo mirrored-ok", "MIRROR-DONE"))
        mark = s.mark()
        s.send("run it please\r")
        s.expect("MIRROR-DONE", timeout=40, since=mark)
        kinds = _wait(lambda: [e["type"] for e in seen] if any(e["type"] == "idle" for e in seen) else None)
        for must in ("user", "turn_start", "llm_call_start", "tool_use", "tool_result", "text_delta", "turn_end"):
            assert must in kinds, (must, kinds)
        tool = next(e for e in seen if e["type"] == "tool_use")
        assert tool["name"] == "Bash" and tool["input"]["command"] == "echo mirrored-ok"
        assert "mirrored-ok" in next(e for e in seen if e["type"] == "tool_result")["content"]
        assert next(e for e in seen if e["type"] == "user")["text"] == "run it please"
        n_before = len(seen)

        # 2. a turn started from the page runs on the same agent: the terminal shows it and nothing else writes
        llm.enqueue([{"text": "WEB-REPLY-SEEN"}])
        mark = s.mark()
        code, res = web.req("POST", "/api/chat/send", {"session": sid, "text": "hello from the page"})
        assert code == 200 and res["queued"] is False
        s.expect("WEB-REPLY-SEEN", timeout=40, since=mark)
        _wait(lambda: "WEB-REPLY-SEEN" in "".join(e.get("text", "") for e in seen[n_before:] if e["type"] == "text_delta"))
        # the terminal's main loop runs this turn, so keep reading its output, as a real terminal does
        _wait(lambda: s.drain(0.02) is not None and not web.req("GET", f"/api/chat/state?session={sid}")[1]["busy"])
        transcript = next(Path(env.axon_home).rglob(f"{sid}/session.jsonl"))
        users = [json.loads(line)["data"]["content"] for line in transcript.read_text().splitlines()
                 if '"user_message"' in line]
        assert users == ["run it please", "hello from the page"]            # one ordered history, no second writer
        assert transcript.parent.joinpath("web-events.jsonl").exists()        # and a replayable record of both turns
        log = web.req("GET", f"/api/session/{sid}/events")[1]
        assert [e["type"] for e in log["events"]].count("user") == 2

        # 3. a second /open reuses the running server
        mark = s.mark()
        s.send("/open --no-open\r")
        s.expect(r"reusing it", timeout=20, since=mark)
        s.expect(PROMPT_READY, timeout=20, since=mark)
        # 4. a message sent while idle is queued onto the live agent (the REPL's own queue), never a second agent
        code, res = web.req("POST", "/api/chat/send", {"session": "nope-not-live", "text": "x"})
        assert code in (404, 400, 423)
    finally:
        s.close()
        llm.stop()
