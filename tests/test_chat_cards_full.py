"""The chat page shows every tool step completely (full input and output, one reason per batch, a hand-off line) and the
numbers of every model call. The page script is a string, so these check the generated JS/CSS and the server's slimming."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from axon.features import dashboard as dash
from axon.features.dash_js_chatx import CHATX_CSS
from axon.features.dashboard_web import APP_JS

NODE = shutil.which("node")


def test_slim_keeps_whole_tool_input_and_output():
    big = "x" * 50_000
    out = dash._slim({"type": "tool_result", "content": big})
    assert out["content"] == big and "truncated_from" not in out
    huge = dash._slim({"type": "tool_result", "content": "y" * 250_000})
    assert len(huge["content"]) == 200_000 and huge["truncated_from"] == 250_000
    use = dash._slim({"type": "tool_use", "input": {"content": big}})
    assert use["input"] == {"content": big}


def test_cards_open_by_default_and_remember_state():
    assert "open: axCardsOpen()" in APP_JS
    assert "lsGet('axon-cards-open') !== '0'" in APP_JS


def test_no_per_step_stats_rows_and_one_usage_summary_per_turn():
    # nothing about tokens/s, context or first-token between steps; one table of usage closes the turn
    for gone in ("First token after", "Thinking tokens", "'sstats'"):
        assert gone not in APP_JS
    assert "el.insertBefore(st.stats" not in APP_JS and "hd, detail, body" not in APP_JS
    assert "function axTurnUsage(" in APP_JS and "axTurnUsage(cur)" in APP_JS


def test_batch_reason_is_gone_and_the_handoff_is_one_short_line():
    assert "Why these calls" not in APP_JS and "axGroupWhy" not in APP_JS and "pwhy" not in APP_JS
    assert "Results go to step " in APP_JS and "sent back to the model in step" not in APP_JS
    assert "cur.thinkText.slice(-30000)" not in APP_JS


def test_input_is_shown_once_with_raw_json_collapsed_and_output_is_a_short_preview():
    assert "Input (exactly what the model sent)" not in APP_JS and "'Show raw'" in APP_JS
    assert "function axMainInput(" in APP_JS and "axPager(lines, axPlainLine, 'out', 4, true)" in APP_JS
    assert "'Show all (' + lines.length.toLocaleString() + ' lines)'" in APP_JS
    assert "Exit code ' + d.code" not in APP_JS          # the exit chip already says it


def test_parallel_calls_have_a_live_header_and_the_board_has_a_timeline():
    assert "' calls running in parallel'" in APP_JS and "' calls ran in parallel'" in APP_JS
    assert "function saTimeline(" in APP_JS and "function saPageBoard(" in APP_JS


def test_css_keeps_the_feed_in_phone_width_and_motion_stoppable():
    assert ".tool.both>.tdet .tpane{display:block!important" in CHATX_CSS
    assert "@media (prefers-reduced-motion:reduce)" in CHATX_CSS and ".fo-tl{overflow-x:auto" in CHATX_CSS
    assert "minmax(0,1fr)" in CHATX_CSS


def test_page_script_parses():
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "app.js"
        f.write_text(APP_JS, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr


def test_json_colorizer_and_pager_in_node():
    if not NODE:
        pytest.skip("node is not installed")
    m = re.search(r"^const AX_JSON_RE = .*?;\n", APP_JS, re.M)
    fn = re.search(r"^function axJsonLine\(.*?^}\n", APP_JS, re.S | re.M)
    assert m and fn
    code = (m.group(0) + fn.group(0) + r"""
const kids = []; const pre = { append: (...a) => kids.push(...a) };
const h = (t, a, ...c) => ({ cls: a.class, text: c.join('') }); global.document = { createTextNode: (s) => s };
axJsonLine(pre, '  "a": "b c", "n": 12, "t": true');
console.log(JSON.stringify(kids.map(k => typeof k === 'string' ? k : k.cls + ':' + k.text)));
""")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert 'jk:"a"' in got and 'js:"b c"' in got and "jn:12" in got and "jb:true" in got
