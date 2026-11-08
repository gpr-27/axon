"""The turn view of the web app: one header, steps per model call, aligned tool rows, honest times, one renderer
for live, replayed and stored turns, and the type scale of the whole page.

The page script is a string: these tests run its pure helpers in node (when installed) and check the generated CSS."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from axon.features import dashboard as dash
from axon.features.dash_design import DESIGN_CSS, TYPE_TAIL, type_overrides
from axon.features.dashboard_web import APP_JS, asset

NODE = shutil.which("node")


def _fn(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^}\n", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def _node(code: str) -> str:
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_durations_are_honest():
    out = _node(_fn("axDur") + "console.log(JSON.stringify([null, undefined, '', NaN, -1, 0, 0.04, 0.0999, 0.1, 2.04, 9.96, 38.4, 59.6, 61, 125, 3725].map(axDur)))")
    assert json.loads(out) == ["", "", "", "", "", "<0.1s", "<0.1s", "<0.1s", "0.1s", "2.0s", "10.0s", "38s", "1m 00s", "1m 01s", "2m 05s", "1h 02m"]


def test_a_running_sentence_and_chips_replace_the_raw_log_line():
    src = "\n".join(_fn(n) for n in ("axQ", "axPath", "axPlural", "axLines", "axCount", "axExit", "axTests", "axGrep", "axFirstLine", "axCardModel", "axRunning"))
    ax_sent = re.search(r"^const AX_SENT = \{.*?^\};\n", APP_JS, re.S | re.M).group(0)
    out = _node(ax_sent + src + """
const e = axCardModel('Edit', {file_path: 'a/b.py', old_string: 'x\\ny', new_string: 'x\\nz\\nw'}, 'Edited', false, null);
const g = axCardModel('Grep', {pattern: 'q'}, 'a.py:1:q\\nb.py:2:q', false, null);
const b = axCardModel('Bash', {command: 'ls'}, 'ok', false, null);
console.log(JSON.stringify([e.chips, g.chips, b.chips, axRunning('Bash', {command: 'sleep 2'}), axRunning('Read', {file_path: '/w/README.md'}), axRunning('mcp__a__do_it', {})]));
""")
    e, g, b, run_bash, run_read, run_mcp = json.loads(out)
    assert e == [{"t": "+2", "c": "add"}, {"t": "−1", "c": "del"}]
    assert g == [{"t": "2 matches"}] and b == []                 # a successful Bash prints no chip: the marker says it worked
    assert run_bash == "Running “sleep 2”" and run_read == "Reading /w/README.md" and run_mcp.startswith("Called do it")


def test_parallel_and_step_times_come_from_the_tool_clock():
    funcs = "\n".join(_fn(n) for n in ("groupTotal", "stepToolTime"))
    out = _node(funcs + """
const par = { cards: [{ axT: { s: 10, e: 10.5, d: 0.5, par: true } }, { axT: { s: 10, e: 11, d: 1, par: true } }] };
const seq = { cards: [{ axT: { s: 10, e: 11, d: 1, par: false } }, { axT: { s: 11, e: 12.5, d: 1.5, par: false } }] };
const part = { cards: [{ axT: { s: 1, e: 2, d: 1 } }, {}] };
console.log(JSON.stringify([groupTotal(par), groupTotal(seq), groupTotal(part), stepToolTime({ standalone: [{ axT: { d: 0.25 } }, {}], groups: [par] })]));
""")
    assert json.loads(out) == [1, 2.5, None, 1.25]


def test_the_tool_clock_excludes_the_wait_for_permission():
    """tool_timing is measured around the whole call, permission prompt included (loop._run_one); the page starts a
    row's clock when the user answered, so a Write approved after 5.8 s shows <0.1s, not 5.8s."""
    body = _fn("toolTiming")
    assert "card.axApproved" in body and "Math.max(s != null ? s : 0, card.axApproved)" in body
    assert "ev.duration_s" in body


def test_a_stored_turn_is_replayed_through_the_live_events():
    out = _node(_fn("storedEvents") + """
const evs = storedEvents([
  { i: 4, data: { model: 'm', usage: { input: 10, output: 2 }, cost: 0.01, duration: 1.5, stop: 'tool_use', thinking: 'hm', text: 'I will look.',
    tools: [{ id: 'a', name: 'Read', input: {}, result: 'x', result_total: 1, is_error: false, permission: null, duration: 0.2 }, { id: 'b', name: 'Bash', input: {}, result: 'no', result_total: 2, is_error: true, permission: { outcome: 'allow', source: 'user', approval: 'once' }, duration: 0.3 }] } },
  { i: 5, data: { model: 'm', usage: {}, cost: 0.02, duration: 0.5, stop: 'end_turn', thinking: '', text: 'Done.', tools: [] } }]);
console.log(JSON.stringify(evs.map(e => e.type)) + '|' + JSON.stringify(evs[evs.length - 1]) + '|' + evs.every(e => e.synthetic));
""")
    types, last, synthetic = out.split("|")
    assert json.loads(types) == ["turn_start", "llm_call_start", "thinking_delta", "text_delta", "tool_batch", "tool_use", "tool_result", "tool_timing",
                                 "tool_use", "permission", "tool_result", "tool_timing", "llm_call_end", "llm_call_start", "text_delta", "llm_call_end", "turn_end"]
    end = json.loads(last)
    assert end["cost_usd"] == pytest.approx(0.03) and end["duration_s"] == pytest.approx(2.5) and end["iterations"] == 2 and end["tool_calls"] == 2
    assert synthetic == "true"


def test_every_event_type_is_handled_by_the_one_renderer():
    sw = APP_JS[APP_JS.index("function applyEvent(C, ev)"):]
    sw = sw[:sw.index("\n}\n")]
    handled = set(re.findall(r"case '([a-z_]+)'", sw))
    wanted = {"user", "turn_start", "llm_call_start", "thinking_delta", "text_delta", "tool_use", "tool_result", "tool_timing", "permission", "approval_request",
              "approval_resolved", "question_request", "question_resolved", "llm_call_end", "cache_status", "subagent", "compaction", "large_input", "budget", "retry",
              "steer", "queued", "memory_loaded", "hook", "tool_rationale", "tool_batch", "llm_request", "turn_end", "interrupted", "error", "idle", "stop_requested",
              "note", "shell_command"}
    assert wanted <= handled, wanted - handled
    assert "default:" in sw                                                    # an unknown event is still shown, never dropped


def test_live_replay_and_history_share_the_code():
    assert "historyNodes(page.entries, C)" in APP_JS and "historyNodes(p.entries, C)" in APP_JS
    assert "applyEvent(F, ev)" in APP_JS                                       # a stored turn is fed through applyEvent
    assert "await tailHistory(C, true)" in APP_JS                              # no swap for HTML after a live turn
    assert "function turnToggle" not in APP_JS and "'Expand turn'" not in APP_JS


def test_the_page_script_parses():
    if not NODE:
        pytest.skip("node is not installed")
    _node("")  # node itself works
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "app.js"
        f.write_text(APP_JS, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr


def test_stored_entries_carry_plain_data_redacted_and_cut():
    big = "x" * (dash.INLINE_TOOL_CHARS + 50)
    e = {"kind": "assistant", "i": 3, "ts": 1.0, "text": "hello", "thinking": "think", "model": "m", "cost": 0.5, "usage": {"input": 3}, "duration": 2.0, "stop": "tool_use",
         "tools": [{"id": "t1", "name": "Bash", "input": {"command": "echo " + big}, "result": big, "is_error": False, "duration": 0.7,
                    "permission": {"outcome": "allow", "reason": "ok", "source": "user", "approval": "once"}}]}
    d = dash.entry_data(e)
    t = d["tools"][0]
    assert d["text"] == "hello" and d["duration"] == 2.0 and t["duration"] == 0.7
    assert len(t["result"]) == dash.INLINE_TOOL_CHARS and t["result_total"] == len(big)
    assert len(t["input"]["command"]) <= dash.INLINE_TOOL_CHARS
    assert t["permission"]["source"] == "user" and t["permission"]["approval"] == "once"
    assert dash.entry_data({"kind": "user", "text": "hi"}) is None


def test_the_turn_view_css_covers_the_states_in_both_themes():
    for sel in (".thead", ".mk.ok", ".mk.bad", ".mk.wait", ".mk.run", ".stime", ".td", ".chip2", ".pgroup", ".tanswer", ".tdet", ".tab2"):
        assert sel.split(".")[1] in DESIGN_CSS or sel in DESIGN_CSS, sel
    assert ".trow" not in DESIGN_CSS                                            # that class belongs to the terminal grid
    assert "font-variant-numeric:tabular-nums" in DESIGN_CSS
    assert "@media (max-width:560px)" in DESIGN_CSS
    # the only animation is the spinner, a quick open and the chevron
    assert "animation:" not in DESIGN_CSS.split("/* ── the turn view")[1]
    for var in ("--font-ui", "--font-response", "--font-code", "--size-response", "--size-code", "--measure-chat"):
        assert var + ":" in DESIGN_CSS
    assert ":root:not([data-motion=on])" in DESIGN_CSS


def test_type_scale_and_one_code_face():
    css, _ = asset("app.css")
    over = type_overrides(css)
    sizes = {float(x) for x in re.findall(r"font-size:([\d.]+)px", over)}
    assert sizes <= {12, 13, 14, 15, 16, 17, 22}, sizes
    assert set(re.findall(r"font-weight:(\w+)", over)) <= {"400", "500", "600"}
    assert "ui-monospace" not in over and "italic" not in over
    assert css.endswith(TYPE_TAIL)
    # labels and figures: the interface face; only code keeps the code face
    assert re.search(r"\.num,\.kpi \.v[^{]*\{font-family:var\(--font-ui\)\}", TYPE_TAIL)
    assert "pre,code,.code,.editor" in TYPE_TAIL and "var(--font-code)" in TYPE_TAIL
    off = type_overrides("a{font:600 10.5px ui-monospace,Menlo,monospace}\n@media (max-width:5px){b{font-size:11px;font-weight:700}}\n@keyframes k{from{font-size:11px}}")
    assert "a{font-size:12px;font-family:var(--font-code)}" in off and "@media (max-width:5px){b{font-size:12px;font-weight:600}}" in off and "keyframes" not in off


def test_reply_prose_alone_uses_the_response_font():
    assert re.search(r"\.entry\.a \.tanswer \.md[^{]*\{font-family:var\(--font-response\);font-size:var\(--size-response\)", DESIGN_CSS)
    assert ".entry.u .md{" in DESIGN_CSS and "--font-response" not in DESIGN_CSS.split(".entry.u .md{")[1].split("}")[0]
    assert ".entry.a .md code" in DESIGN_CSS and "font-family:var(--font-code)" in DESIGN_CSS
