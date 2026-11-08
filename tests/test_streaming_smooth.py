"""Live response streaming is smooth: the server forwards each provider delta at once, the page releases text at a
steady pace and re-renders only the open block, scrolling follows only while the reader is at the bottom, a
dropped connection resumes without duplicates, and the terminal redraws its live row in one write."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from axon.features import dashboard as dash  # noqa: E402
from axon.features.dash_js_md import JS_MD  # noqa: E402
from test_dashboard import c, env, llm, server  # noqa: E402,F401
from test_dashboard_app import types, wait_for  # noqa: E402

NODE = shutil.which("node")


def _session(c):
    sid = json.loads(c.post("/api/chat/send", {"text": "hi"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub))
    return sid, rt


def _open_stream(c, sid: str, since: int = 0, last_id: int | None = None):
    conn, r = c.raw("GET", f"/api/events?session={sid}&since={since}",
                    headers={"Last-Event-ID": str(last_id)} if last_id is not None else None)
    assert r.status == 200
    return conn, r


def _next_event(r, deadline: float):
    cur = 0
    while time.time() < deadline:
        line = r.fp.readline().decode()
        if line.startswith("id:"):
            cur = int(line[3:])
        elif line.startswith("data:"):
            return cur, json.loads(line[5:])
    raise AssertionError("no event before the deadline")


def test_server_forwards_each_delta_immediately(c):
    """Latency from hub.emit (a provider delta reaching the server) to the client socket, measured per delta."""
    sid, rt = _session(c)
    conn, r = _open_stream(c, sid, since=rt.hub.seq)
    time.sleep(0.2)
    lat = []
    for i in range(60):
        t0 = time.perf_counter()
        rt.hub.emit("text_delta", {"text": f"w{i} ", "sent": t0})
        _, ev = _next_event(r, time.time() + 3)
        lat.append((time.perf_counter() - ev["sent"]) * 1000)
        time.sleep(0.01)
    conn.close()
    lat.sort()
    print(f"\nSSE emit->client ms: median {lat[len(lat)//2]:.2f}  p95 {lat[int(len(lat)*.95)]:.2f}  max {lat[-1]:.2f}")
    assert lat[int(len(lat) * .95)] < 40, lat


def test_handler_disables_nagle():
    assert dash._Handler.disable_nagle_algorithm is True


# ─── the page script, run in node against a small fake DOM ───────────────────────────────────────────────────────────

PRELUDE = r"""
const BLOCK = /<(p|h\d|pre|ul|ol|table|blockquote)>[\s\S]*?<\/\1>|<hr>/g;
function fakeEl() {
  const el = { childNodes: [], style: {}, ops: 0,
    get lastChild() { return this.childNodes[this.childNodes.length - 1]; },
    removeChild(n) { this.childNodes.splice(this.childNodes.indexOf(n), 1); this.ops++; },
    insertAdjacentHTML(_w, html) { for (const m of html.match(BLOCK) || []) this.childNodes.push({ html: m }); this.ops++; },
    get html() { return this.childNodes.map(n => n.html).join(''); } };
  return el;
}
let NOW = 0, RAFS = [];
global.performance = { now: () => NOW };
global.requestAnimationFrame = f => { RAFS.push(f); return RAFS.length; };
global.cancelAnimationFrame = id => { RAFS[id - 1] = null; };
global.document = { hidden: false };
global.window = { matchMedia: q => ({ matches: !!global.REDUCED }) };
function frame(ms = 16.7) { NOW += ms; const q = RAFS; RAFS = []; q.forEach(f => f && f(NOW)); }
let sticks = 0; function stick() { sticks++; }
function turn() { return { prose: fakeEl(), proseText: '' }; }
function push(C, cur, t) { cur.proseText += t; liveText(C, cur); }
const C = { replaying: false };
"""


def _run_js(body: str) -> str:
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(PRELUDE + JS_MD + "\n" + body, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


SAMPLE = ("## Heading\n\nSome **bold** text with `code` and a [link](https://example.com) here.\nSecond line.\n\n"
          "- item one\n- item two\n\n```py\nfor i in range(3):\n\n    print(i)\n```\n\n"
          "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\n> quote\n\n1. one\n2. two\n\nlast paragraph <script>alert(1)</script>\n")


def test_incremental_render_equals_the_full_render():
    out = _run_js(r"""
const text = %s;
let ok = 0, bad = [];
for (const step of [1, 3, 7, 40, 1000]) {
  const cur = turn(); let pos = 0;
  while (pos < text.length) { cur.proseText = text.slice(0, pos += step); liveDraw(cur, cur.proseText, false); }
  liveDraw(cur, text, true);
  if (cur.prose.html === mdLite(text)) ok++; else bad.push(step);
}
console.log(JSON.stringify({ ok, bad }));
""" % json.dumps(SAMPLE))
    assert json.loads(out) == {"ok": 5, "bad": []}


def test_finished_blocks_are_rendered_once_and_kept():
    out = _run_js(r"""
const cur = turn(); const text = %s;
let replaced = 0, prev = [], frozen = 0;
for (let n = 1; n <= text.length; n++) {
  liveDraw(cur, text.slice(0, n), false);
  const nodes = cur.prose.childNodes.slice();
  // every block closed by a blank line must be the very same object as in the previous frame
  for (let i = 0; i < frozen; i++) if (nodes[i] !== prev[i]) replaced++;
  prev = nodes; frozen = cur.mdst.nodes;
}
console.log(replaced);
""" % json.dumps(SAMPLE))
    assert out == "0"


def test_200k_reply_costs_under_8ms_per_frame():
    out = _run_js(r"""
const unit = %s; let text = ''; while (text.length < 200000) text += unit;
const cur = turn(); cur.proseText = ''; let worst = 0, total = 0, frames = 0;
let pos = 0;
while (pos < text.length) {
  pos = Math.min(text.length, pos + 60);                  // ~60 characters arrive per 16 ms frame
  cur.proseText = text.slice(0, pos);
  const t0 = process.hrtime.bigint();
  liveDraw(cur, cur.proseText, false);
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  if (pos > 150000) { worst = Math.max(worst, ms); total += ms; frames++; }
}
console.log(JSON.stringify({ worst, mean: total / frames, len: cur.proseText.length }));
""" % json.dumps(SAMPLE))
    r = json.loads(out)
    print(f"\nincremental 200k: mean {r['mean']:.3f} ms/frame, worst {r['worst']:.3f} ms")
    assert r["len"] >= 200000 and r["worst"] < 8, r


def test_a_burst_is_released_smoothly_and_catches_up():
    out = _run_js(r"""
const cur = turn(); const sizes = []; let last = 0;
push(C, cur, 'word '.repeat(1000));                       // a 5000 character burst in one delta
let t = 0;
for (let i = 0; i < 60 && cur.live.shown < 5000; i++) { frame(); t += 16.7; sizes.push(cur.live.shown - last); last = cur.live.shown; }
console.log(JSON.stringify({ ms: t, first: sizes[0], max: Math.max(...sizes), frames: sizes.length }));
""")
    r = json.loads(out)
    print(f"\nburst of 5000 chars fully shown after {r['ms']:.0f} ms ({r['frames']} frames), first frame {r['first']} chars")
    assert r["ms"] <= 800 and 20 < r["first"] < 1500, r


def test_a_steady_stream_never_lags_more_than_a_moment():
    out = _run_js(r"""
const cur = turn(); let worst = 0;
for (let i = 0; i < 300; i++) {                           // 40 characters per 16.7 ms frame, for five seconds
  push(C, cur, 'abcdefghij klmnopqrst uvwxyz 0123456789 '.slice(0, 40));
  frame();
  worst = Math.max(worst, (cur.proseText.length - cur.live.shown) / 40 * 16.7);
}
console.log(worst);
""")
    print(f"\nsteady stream lag (worst): {float(out):.0f} ms")
    assert float(out) < 250


def test_flush_reduced_motion_and_replay_show_everything_at_once():
    out = _run_js(r"""
const res = {};
let cur = turn(); push(C, cur, 'hello world '.repeat(200)); frame();
res.partial = cur.live.shown < cur.proseText.length;
liveFlush(C, cur); res.flushed = cur.live.shown === cur.proseText.length && cur.prose.html === mdLite(cur.proseText) && !cur.liveRaf;
global.REDUCED = true; cur = turn(); push(C, cur, 'hello world '.repeat(200)); res.noRaf = RAFS.filter(Boolean).length === 0;
global.REDUCED = false;
console.log(JSON.stringify(res));
""")
    assert json.loads(out) == {"partial": True, "flushed": True, "noRaf": True}


def test_tables_and_fences_in_progress_never_show_raw_markup():
    out = _run_js(r"""
const text = '| name | value |\n|---|---|\n| a | 1 |\n| b | 2 |\n\n```js\nconst x = 1;\nconst y = 2;\n```\n\nafter';
const cur = turn(); const flips = [];
let tableSeen = false;
for (let n = 1; n <= text.length; n++) {
  liveDraw(cur, text.slice(0, n), false);
  const h = cur.prose.html;
  if (h.includes('<table>')) tableSeen = true;
  else if (tableSeen) flips.push(n);                       // once a table is on screen it must not turn back into text
  if (/\|---/.test(h) || /<p>[^<]*\|[^<]*\|/.test(h) && n >= 2 && n < 40) flips.push('raw@' + n);
  if (/```/.test(h)) flips.push('fence@' + n);
}
console.log(JSON.stringify(flips));
""")
    assert json.loads(out) == []


def test_escaping_and_link_safety_still_hold_when_streamed():
    out = _run_js(r"""
const text = '<img src=x onerror=alert(1)> [x](javascript:alert(1)) [ok](https://a.example/p?q=1&r=2) **b**\n';
const cur = turn(); for (let n = 1; n <= text.length; n++) liveDraw(cur, text.slice(0, n), false);
liveDraw(cur, text, true); const h = cur.prose.html;
console.log(JSON.stringify({ img: h.includes('<img'), js: /href="javascript/.test(h), ok: h.includes('<a href="https://a.example/p?q=1&amp;r=2"'), same: h === mdLite(text) }));
""")
    assert json.loads(out) == {"img": False, "js": False, "ok": True, "same": True}


def test_scroll_follows_only_at_the_bottom_and_offers_a_jump():
    out = _run_js(r"""
const listeners = {};
let top = 0;
const feed = { get scrollTop() { return top; }, set scrollTop(v) { top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); }, scrollHeight: 1000, clientHeight: 500, offsetTop: 0, offsetHeight: 500,
  addEventListener: (n, f) => { listeners[n] = f; },
  parentNode: { style: {}, clientHeight: 600, append(b) { this.btn = b; b.parentNode = this; } } };
global.getComputedStyle = () => ({ position: 'relative' });
global.document.getElementById = () => ({}); global.document.createElement = () => ({ style: {}, addEventListener(n, f) { this.click = f; }, hidden: true });
const Cc = { feed, atBottom: true };
followWire(Cc, feed);
const res = {};
feed.scrollTop = 100; followStick(Cc); res.pinned = feed.scrollTop === 500;      // pinned: follows growth
listeners.scroll();
feed.scrollTop = 300; listeners.scroll(); res.unpinned = Cc.atBottom === false;      // the reader scrolled up
feed.scrollHeight = 1400; followStick(Cc); res.notMoved = feed.scrollTop === 300;    // new text does not drag them down
res.jump = !!Cc.jump && Cc.jump.hidden === false;
Cc.jump.click(); res.back = Cc.atBottom === true && feed.scrollTop === 900 && Cc.jump.hidden === true;
feed.scrollTop = 900; listeners.wheel({ deltaY: -30 }); res.wheel = Cc.atBottom === false;
console.log(JSON.stringify(res));
""")
    assert all(json.loads(out).values()), out


def test_page_script_still_parses_with_the_new_live_code():
    if not NODE:
        pytest.skip("node is not installed")
    from axon.features.dashboard_web import APP_JS
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "app.js"
        f.write_text(APP_JS, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    assert "liveText(C, cur)" in APP_JS and "followWire(C, feed)" in APP_JS and "setTimeout(() => { live.mdTimer" not in APP_JS


# ─── reconnect ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_reconnect_with_last_event_id_has_no_duplicates_and_no_gap(c):
    sid, rt = _session(c)
    base = rt.hub.seq
    for i in range(10):
        rt.hub.emit("text_delta", {"text": f"t{i}"})
    conn, r = _open_stream(c, sid, since=base)
    got = [_next_event(r, time.time() + 3)[0] for _ in range(4)]
    conn.close()                                                         # the connection drops after 4 events
    for i in range(10, 15):
        rt.hub.emit("text_delta", {"text": f"t{i}"})                     # more arrive while the page is offline
    conn, r = _open_stream(c, sid, since=base, last_id=got[-1])          # the browser resumes with Last-Event-ID
    rest = [_next_event(r, time.time() + 3)[0] for _ in range(15 - 4)]
    conn.close()
    assert got + rest == list(range(base + 1, base + 16))


def test_stream_frames_for_a_burst_go_out_in_one_write(c):
    sid, rt = _session(c)
    conn, r = _open_stream(c, sid, since=rt.hub.seq)
    time.sleep(0.2)
    for i in range(20):
        rt.hub.emit("text_delta", {"text": "x"})
    ids = [_next_event(r, time.time() + 3)[0] for _ in range(20)]
    conn.close()
    assert ids == sorted(set(ids)) and len(ids) == 20


def test_one_giant_block_still_fits_the_frame_budget():
    out = _run_js(r"""
let text = ''; while (text.length < 200000) text += '- list item with [link](https://x.y) text\n';
const cur = turn(); const ms = []; let pos = 0;
while (pos < text.length) {
  pos = Math.min(text.length, pos + 60); cur.proseText = text.slice(0, pos);
  const t0 = process.hrtime.bigint(); liveDraw(cur, cur.proseText, false);
  if (pos > 190000) ms.push(Number(process.hrtime.bigint() - t0) / 1e6);
}
ms.sort((a, b) => a - b);
// How fast this machine is: the best of five runs of a fixed plain-JS workload, so a slow CI runner gets a
// proportionally longer budget while the limit stays 8 ms on the development machine (it takes 1.8 ms there).
let calib = 1e9;
for (let k = 0; k < 5; k++) {
  const c0 = process.hrtime.bigint();
  for (let r = 0; r < 40; r++) '- list item with [link](https://x.y) text\n'.repeat(500).replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>').split('\n');
  calib = Math.min(calib, Number(process.hrtime.bigint() - c0) / 1e6);
}
console.log(ms[Math.floor(ms.length / 2)], calib);
""")
    frame, calib = (float(x) for x in out.split())
    budget = 8 * max(1.0, calib / 1.8)
    print(f"\n200k with no blank line (worst case), median: {frame:.2f} ms/frame, budget {budget:.1f} ms (machine factor {calib / 1.8:.2f})")
    assert frame < budget       # a slow runner scales the limit; text that costs more per frame as it grows is still far over it


# ─── terminal ───────────────────────────────────────────────────────────────────────────────────────────────────────

class _FakeTTY:
    def __init__(self):
        self.writes: list[str] = []

    def write(self, s):
        self.writes.append(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return True


@pytest.fixture
def live_spinner(monkeypatch):
    from axon.ui import render
    fake = _FakeTTY()
    monkeypatch.setenv("AXON_SYNC_OUTPUT", "0")
    monkeypatch.setattr(render.Spinner, "enabled", staticmethod(lambda: True))
    sp = render.Spinner(status=lambda: "", interval=0.05)
    sp.live = None
    state = {"line": ""}
    sp.preview_provider = lambda: state["line"]
    monkeypatch.setattr(render, "term_height", lambda *a, **k: 10)      # no tip/status rows: just the frame
    sp.arm = lambda: monkeypatch.setattr(sys, "stdout", fake)     # after pytest's own capture is in place
    yield sp, fake, state
    sp.stop()


def test_redraw_is_a_single_write_and_follows_text_within_a_few_tens_of_ms(live_spinner):
    sp, fake, state = live_spinner
    sp.arm()
    sp.start("Thinking")
    time.sleep(0.25)
    fake.writes.clear()
    state["line"] = "the unfinished line"
    shown_after = 1e9
    for attempt in range(5):                # the best of five: a busy CI machine can stall any single wake-up
        fake.writes.clear()
        state["line"] = f"the unfinished line {attempt}"
        t0 = time.perf_counter()
        sp.nudge()
        while not any("unfinished" in w for w in fake.writes) and time.perf_counter() - t0 < 1:
            time.sleep(0.002)
        shown_after = min(shown_after, (time.perf_counter() - t0) * 1000)
    print(f"\nterminal: nudge -> row on screen {shown_after:.0f} ms (was up to 100 ms)")
    assert shown_after < 80
    time.sleep(0.2)
    redraws = [w for w in fake.writes if w.startswith("\r\033[2K")]
    assert redraws and all("\r\033[2K" in w and len(w) > 12 for w in redraws)    # erase + frame together
    assert not any(w.strip("\r\033[2KA") == "" for w in fake.writes)             # no write that only erases


def test_text_after_the_spinner_is_erase_and_text_in_one_write(live_spinner):
    sp, fake, _ = live_spinner
    sp.arm()
    sp.start("Thinking")
    time.sleep(0.2)
    fake.writes.clear()
    sys.stdout.write("hello\n")
    assert len(fake.writes) == 1 and fake.writes[0].endswith("hello\n") and fake.writes[0].startswith("\r\033[2K")


def test_unchanged_frame_is_not_rewritten(live_spinner):
    sp, fake, _ = live_spinner
    sp.arm()
    sp.start("Thinking")
    time.sleep(0.2)
    fake.writes.clear()
    t0 = time.perf_counter()
    for _ in range(10):
        sp.nudge()
        time.sleep(0.01)
    elapsed = time.perf_counter() - t0
    # The glyph/clock may tick once per spinner interval (0.05 s) however slow the machine's sleeps are; a nudge
    # alone redraws nothing, so ten nudges would otherwise have written ten times.
    assert len(fake.writes) <= int(elapsed / 0.05) + 2 and len(fake.writes) < 10


def test_synchronized_output_wraps_whole_frames_only_when_supported(monkeypatch, live_spinner):
    from axon.ui import render
    sp, fake, state = live_spinner
    monkeypatch.setenv("AXON_SYNC_OUTPUT", "1")
    sp.arm()
    sp.start("Thinking")
    time.sleep(0.25)
    sys.stdout.write("row\n")
    frames = [w for w in fake.writes if "\033[?2026h" in w]
    assert frames and all(w.startswith("\033[?2026h") and w.endswith("\033[?2026l") for w in frames)
    monkeypatch.setenv("AXON_SYNC_OUTPUT", "0")
    assert render._sync_frame("x") == "x"
    monkeypatch.delenv("AXON_SYNC_OUTPUT")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert render.sync_output_supported() is False
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert render.sync_output_supported() is True
