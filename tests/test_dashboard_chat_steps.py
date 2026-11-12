"""The web chat shows every step of a turn, live and after a reload.

A real dashboard server is attached to a real agent whose model is a script (no network, no cost). The server tests
read its API; the browser tests load the page in headless Chromium (driven through node and playwright-core) and
read what is on screen. The browser tests skip when node, playwright-core or its Chromium is not installed."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import wait
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.providers.base import (  # noqa: E402
    AssistantTurn, TextBlock, TextDelta, ThinkingBlock, ThinkingDelta, ToolArgsDelta, ToolUseBlock, ToolUseComplete,
    ToolUseStart, TurnComplete, Usage,
)
from fakes import FakeProvider  # noqa: E402
from test_core_helpers import make_agent  # noqa: E402
from test_dashboard import Client  # noqa: E402


def _text(m: dict[str, Any]) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def _is_result(m: dict[str, Any]) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


class Scripted(FakeProvider):
    """Answers from the conversation alone, so parent and subagents can share it from several threads."""

    def __init__(self, ws: Path) -> None:
        super().__init__([])
        self.ws = ws
        self.lock = threading.Lock()
        self.count = 0
        self.local = threading.local()
        self.gate: threading.Event | None = None      # when set, subagents wait here before their last call

    def _turn(self, prompt: str, k: int, uid: int) -> AssistantTurn:
        u = Usage(input=1200 + 300 * k, output=80, cache_read=900 if k else 0)

        def tu(name: str, inp: dict[str, Any], j: int = 0) -> ToolUseBlock:
            return ToolUseBlock(id=f"tu{uid}_{j}", name=name, input=inp)

        def say(text: str) -> AssistantTurn:
            return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=u)

        def use(*blocks: Any) -> AssistantTurn:
            return AssistantTurn(blocks=list(blocks), stop_reason="tool_use", usage=u)
        a = str(self.ws / "a.py")
        if "SUBTASK" in prompt:
            if k == 0:
                return use(TextBlock(text="reading"), tu("Read", {"file_path": a}))
            if self.gate is not None:
                self.gate.wait(20)
            return say("sub report: " + prompt[:30])
        if prompt.startswith("fan"):
            if k == 0:
                return use(TextBlock(text="Fanning out."), *[tu("Task", {"description": f"job {j}", "prompt": f"SUBTASK {j}: read a.py"}, j) for j in range(3)])
            return say("All three subagents reported.")
        if prompt.startswith("one"):
            return use(tu("Task", {"description": "solo job", "prompt": "SUBTASK solo: read a.py"})) if k == 0 else say("The subagent reported.")
        if k == 0:
            return use(ThinkingBlock(text="Read both files first."), TextBlock(text="I will read the files."),
                       tu("Read", {"file_path": a}, 0), tu("Read", {"file_path": str(self.ws / "b.py")}, 1))
        if k == 1:
            return use(tu("Bash", {"command": "echo hello"}))
        return say("Done.")

    def stream(self, *, model, system, messages, tools, max_tokens, effort=None, thinking=True):  # type: ignore[override]
        users = [i for i, m in enumerate(messages) if m.get("role") == "user" and not _is_result(m) and _text(m).strip()]
        last = users[-1] if users else 0
        k = sum(1 for m in messages[last + 1:] if _is_result(m))
        with self.lock:
            self.count += 1
            uid = self.count
        time.sleep(0.03)
        turn = self._turn(_text(messages[last]), k, uid)
        self.local.turn = turn
        for b in turn.blocks:
            if isinstance(b, TextBlock):
                yield TextDelta(b.text)
            elif isinstance(b, ThinkingBlock):
                yield ThinkingDelta(b.text)
            elif isinstance(b, ToolUseBlock):
                yield ToolUseStart(b.id, b.name)
                yield ToolArgsDelta(b.id, json.dumps(b.input))
                yield ToolUseComplete(b.id)
        yield TurnComplete(turn.stop_reason, turn.usage)

    def finalize(self) -> AssistantTurn:
        return self.local.turn


@pytest.fixture
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A dashboard attached to a running agent (as `axon` with /web is), on session ``sess_x``."""
    from axon.features.dashboard import _bind
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXON_NO_BROWSER", "1")
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "a.py").write_text("print('a')\n")
    (ws / "b.py").write_text("print('b')\n")
    provider = Scripted(ws)
    agent = make_agent(provider, ws, events=[], mode="acceptEdits", permissions={"allow": ["Bash(echo:*)"]})
    agent.session.open("sess_x")
    # the terminal's live subagent frame needs a terminal: here it only waits for the batch, as the brief's harness does
    import axon.ui.subagent_monitor as mon
    monkeypatch.setattr(agent, "_interactive_monitor", lambda: True, raising=False)
    monkeypatch.setattr(mon, "run_live_subagent_monitor", lambda future_map, mgr, agent=None: wait(list(future_map)))
    srv = _bind(0, ws, agent)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    cl = Client(srv)
    cl.login()
    yield SimpleNamespace(srv=srv, agent=agent, c=cl, ws=ws, provider=provider, url=srv.open_url + "#/chat/sess_x")
    srv.shutdown_runs()
    srv.shutdown()
    srv.server_close()


# ─── server ──────────────────────────────────────────────────────────────────

def test_a_chat_with_no_transcript_yet_answers_an_empty_page_not_a_404(live):
    """The page asks for the saved transcript as soon as it opens. For the terminal's own chat that has not saved a
    message yet, a 404 put an error in the browser console on every first load."""
    st, _, body = live.c.get("/api/session/sess_x?limit=30")
    assert st == 200, body[:200]
    assert json.loads(body) == {"entries": [], "total": 0, "first": 0, "has_before": False}
    assert live.c.get("/api/session/sess_nobody?limit=30")[0] == 404          # an unknown chat is still not found


def test_the_event_record_carries_what_each_step_needs(live):
    """What the page draws for a step comes from these events; a reload replays them from the record."""
    live.agent.run_turn("tour")
    evs = live.c.json("/api/session/sess_x/events?limit=20000")["events"]
    by: dict[str, list[dict[str, Any]]] = {}
    for e in evs:
        by.setdefault(e["type"], []).append(e["data"])
    req = by["llm_request"][0]
    assert req["system_tokens"] > 0 and req["tool_schema_tokens"] > 0 and req["effort"] and req["tool_count"] > 0
    perms = {p["tool"]: p for p in by["permission"]}
    assert perms["Read"]["source"] == "mode" and perms["Read"]["mode"] == "acceptEdits"
    assert perms["Bash"]["source"] == "rule" and perms["Bash"]["rule"] == "Bash(echo:*)"
    assert [c["miss"] for c in by["cache_status"]][:2] == [True, False] and by["cache_status"][1]["hit_pct"] > 0
    assert all(e["cost"] > 0 and e["duration_s"] > 0 for e in by["llm_call_end"])
    assert len(by["llm_call_start"][1]["fed_back"]) == 2                       # the two Read results sent back to the model


# ─── browser ─────────────────────────────────────────────────────────────────

NODE = shutil.which("node")
try:      # the account's real home: tests may run with HOME pointed at a temp folder, where no browser is installed
    import pwd
    REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
except Exception:
    REAL_HOME = Path.home()


def _browser_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(REAL_HOME)
    return env

DRIVER = r"""
// Reads commands on stdin (one per line), answers each with one JSON line. Its own headless browser.
const pw = require(process.argv[2]);
const rl = require('readline').createInterface({ input: process.stdin });
const ROWS = () => [...document.querySelectorAll('.feed .shead, .feed .sline, .feed summary.xrow, .feed .steprow, .feed .pgroup > .phead, .feed .fanout > h5, .feed .fo-lane, .feed .sa-live, .feed .notice')]
  .filter(e => e.offsetParent !== null).map(e => e.className.split(' ')[0] + ' | ' + e.innerText.replace(/\s*\n+\s*/g, ' ; '));
(async () => {
  const browser = await pw.chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('response', r => { if (r.status() >= 400) errors.push('http ' + r.status() + ' ' + r.url().replace(/^https?:\/\/[^/]+/, '')); });
  const settle = async () => { await page.waitForSelector('.feed'); await page.waitForTimeout(900); };
  await page.goto(process.argv[3]); await settle();
  console.log(JSON.stringify({ ready: true }));
  for await (const line of rl) {
    const [cmd, ...rest] = line.trim().split(' '); const arg = rest.join(' ');
    let out = null;
    try {
      if (cmd === 'rows') { await page.waitForTimeout(700); out = await page.evaluate(ROWS); }
      else if (cmd === 'reload') { await page.reload(); await settle(); out = true; }
      else if (cmd === 'trace') { await page.evaluate((v) => { localStorage.setItem('axon-trace2', v); }, arg); await page.reload(); await settle(); out = true; }
      else if (cmd === 'eval') out = await page.evaluate(arg);
      else if (cmd === 'offline') { await page.context().setOffline(arg === '1'); await page.waitForTimeout(400); out = true; }
      else if (cmd === 'shot') { await page.screenshot({ path: arg }); out = true; }
      else if (cmd === 'errors') out = errors.splice(0);
      else if (cmd === 'quit') break;
    } catch (e) { out = { error: String(e && e.message || e) }; }
    console.log(JSON.stringify({ out }));
  }
  await browser.close(); process.exit(0);
})().catch(e => { console.log(JSON.stringify({ fatal: String(e && e.message || e) })); process.exit(1); });
"""


def _playwright_core() -> str | None:
    """A playwright-core package whose Chromium is installed, or None."""
    if not NODE:
        return None
    roots = [os.environ.get("AXON_PLAYWRIGHT_CORE", "")]
    roots += sorted(glob.glob(str(REAL_HOME /".npm/_npx/*/node_modules/playwright-core")))
    for base in ("/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules", "/usr/lib/node_modules"):
        roots += [f"{base}/playwright-core", f"{base}/playwright/node_modules/playwright-core"]
    for r in roots:
        if not r or not Path(r, "package.json").is_file():
            continue
        try:
            p = subprocess.run([NODE, "-e", "const p=require(process.argv[1]);const fs=require('fs');process.exit(fs.existsSync(p.chromium.executablePath())?0:1)", r],
                               capture_output=True, timeout=20, env=_browser_env())
        except Exception:
            continue
        if p.returncode == 0:
            return r
    return None


class Page:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    def _read(self) -> dict[str, Any]:
        line = self.proc.stdout.readline()        # type: ignore[union-attr]
        assert line, "the browser driver exited: " + (self.proc.stderr.read() if self.proc.stderr else "")
        d = json.loads(line)
        assert "fatal" not in d, d
        return d

    def cmd(self, line: str) -> Any:
        self.proc.stdin.write(line + "\n")        # type: ignore[union-attr]
        self.proc.stdin.flush()                   # type: ignore[union-attr]
        out = self._read()["out"]
        assert not (isinstance(out, dict) and "error" in out), out
        return out

    def rows(self) -> list[str]:
        return self.cmd("rows")

    def wait_rows(self, want: str, timeout: float = 15.0) -> list[str]:
        end = time.time() + timeout
        rows: list[str] = []
        while time.time() < end:
            rows = self.rows()
            if any(want in r for r in rows):
                return rows
        raise AssertionError(f"{want!r} never appeared in:\n" + "\n".join(rows))


@pytest.fixture
def page(live, tmp_path: Path):
    core = _playwright_core()
    if not core:
        pytest.skip("node with playwright-core and its Chromium is not installed")
    drv = tmp_path / "driver.js"
    drv.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.Popen([NODE, str(drv), core, live.url], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=_browser_env())
    pg = Page(proc)
    try:
        assert pg._read().get("ready")
        yield pg
    finally:
        try:
            proc.stdin.write("quit\n")            # type: ignore[union-attr]
            proc.stdin.flush()                    # type: ignore[union-attr]
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _has(rows: list[str], *parts: str) -> bool:
    return any(all(p in r for p in parts) for r in rows)


def test_opening_a_new_chat_logs_no_error(live, page):
    assert page.cmd("errors") == []


def test_every_step_of_a_turn_is_on_screen_at_the_default_level_live_and_after_a_reload(live, page):
    live.agent.run_turn("tour")
    rows = page.wait_rows("Step 3")
    text = "\n".join(rows)
    # each model call: its number, tools, time and cost
    assert _has(rows, "shead | Step 1", "2 tools", "$") and _has(rows, "shead | Step 2", "1 tool", "$"), text
    # no Sent / Got / stats rows between steps: one usage summary closes the turn instead
    assert not _has(rows, "sline") and not _has(rows, "cache miss"), text
    # which rule or mode let each tool run
    assert _has(rows, "xrow", "Read", "allowed by acceptEdits mode") and _has(rows, "xrow", "Bash", "allowed by rule Bash(echo:*)"), text
    assert _has(rows, "phead | 2 calls ran in parallel"), text
    page.cmd("reload")
    again = page.wait_rows("Step 3")
    assert again == rows, "after a reload the turn reads differently:\n" + "\n".join(again) + "\n--- was\n" + text
    assert page.cmd("errors") == []


def test_compact_is_the_only_level_that_leaves_step_detail_out(live, page):
    live.agent.run_turn("tour")
    page.wait_rows("Step 3")
    page.cmd("trace compact")
    rows = page.rows()
    assert not _has(rows, "shead") and not _has(rows, "sline") and not _has(rows, "allowed by"), "\n".join(rows)


CTL = "eval [...document.querySelectorAll('.sa-ctl')].filter(e => e.offsetParent !== null).length"


def test_a_turn_that_ran_while_the_connection_was_down_is_drawn_whole_when_it_comes_back(live, page):
    live.agent.run_turn("tour")
    want = page.wait_rows("Step 3")
    page.cmd("offline 1")
    live.agent.run_turn("tour")                           # the page sees none of this turn's events as they happen
    page.cmd("offline 0")
    end = time.time() + 20
    rows: list[str] = []
    while time.time() < end:
        rows = page.rows()
        if sum(1 for r in rows if r.startswith("shead | Step 3")) == 2:
            break
    heads = [r.split(";")[0] for r in rows if r.startswith("shead")]
    assert heads == ["shead | Step 1 ", "shead | Step 2 ", "shead | Step 3 "] * 2, "\n".join(rows)     # each step once, none missing
    assert sum(1 for r in rows if r.startswith("xrow") and not r.startswith("xrow | Thinking")) == 6, "\n".join(rows)
    assert len(want) < len(rows)


def _lanes(rows: list[str]) -> list[str]:
    return [r for r in rows if r.startswith("fo-lane")]


def test_a_fan_out_is_drawn_as_lanes_live_and_a_reload_mid_run_keeps_tell_and_stop(live, page):
    live.provider.gate = threading.Event()                # the subagents wait before their last model call
    t = threading.Thread(target=lambda: live.agent.run_turn_threaded("fan out"), daemon=True)     # on a worker thread, as the terminal runs it
    t.start()
    try:
        page.wait_rows("fo-lane")
        time.sleep(1.0)
        rows = page.rows()
        lanes = _lanes(rows)
        assert len(lanes) == 3 and all("running" in x and "model call" in x and "Read" in x for x in lanes), "\n".join(rows)
        assert page.cmd(CTL) == 3                         # Tell and Stop for each running subagent
        page.cmd("reload")
        rows = page.rows()
        lanes = _lanes(rows)
        assert len(lanes) == 3 and all("running" in x and "Read" in x for x in lanes), "\n".join(rows)
        assert page.cmd(CTL) == 3, "a reload mid-run lost Tell and Stop"
    finally:
        live.provider.gate.set()
        t.join(30)
    rows = page.wait_rows("all finished")
    lanes = _lanes(rows)
    assert len(lanes) == 3 and all("done" in x for x in lanes), "\n".join(rows)
    assert page.cmd(CTL) == 0                             # nothing left to tell or stop
    # the three Task calls start on three threads; whichever starts first, the rows keep the order the model asked in
    tasks = [r for r in rows if r.startswith("xrow") and "a subagent: job" in r]
    assert [r.split("job ")[1][0] for r in tasks] == ["0", "1", "2"], "\n".join(tasks)
    assert _has(rows, "xrow", "Task", "Ran a subagent: job 0") and _has(rows, "shead | Step 2"), "\n".join(rows)
    page.cmd("reload")
    again = page.wait_rows("all finished")
    assert _lanes(again) == lanes, "\n".join(again)
    assert page.cmd("errors") == []


def test_a_single_subagent_gets_a_lane_too_and_keeps_its_steps_after_a_reload(live, page):
    live.agent.run_turn("one sub")
    rows = page.wait_rows("fo-lane")
    lanes = _lanes(rows)
    assert len(lanes) == 1 and "solo job" in lanes[0] and "done" in lanes[0] and "Read" in lanes[0] and "model call" in lanes[0], "\n".join(rows)
    assert not _has(rows, "Fanned out"), "\n".join(rows)   # one subagent is not a fan-out
    page.cmd("reload")
    again = page.wait_rows("fo-lane")
    assert _lanes(again) == lanes, "after a reload the subagent's steps are gone:\n" + "\n".join(again)

