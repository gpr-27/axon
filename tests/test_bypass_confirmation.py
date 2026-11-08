"""Choosing "Runs without asking" in the app asks once per chat; other modes switch at once."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from axon.features.dashboard_web import APP_JS

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node runs the page script")


def _fn(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^}\n", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def _node(body: str) -> str:
    p = subprocess.run(["node", "-"], input=body, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


HARNESS = """
const BYPASS_OK = new Set(); let dialogs = 0, answer = true; const made = [];
const document = { body: { append(n) { dialogs++; const b = made.filter(m => m.tag === 'button'); setTimeout(() => (answer ? b[b.length - 1] : b[b.length - 2]).attrs.onclick(), 0); } } };
function h(tag, attrs, ...kids) { const n = { tag, attrs: attrs || {}, kids, remove() {}, addEventListener() {}, focus() {}, append() {} }; made.push(n); return n; }
"""


@needs_node
def test_the_confirmation_is_asked_once_per_chat_and_only_for_bypass():
    out = _node(HARNESS + _fn("confirmBypass") + """
    (async () => {
      const A = { sid: 'a', st: { mode: 'default' } }, B = { sid: 'b', st: { mode: 'default' } };
      const r = [];
      r.push(await confirmBypass(A, 'plan'), dialogs);          // other modes: no dialog
      r.push(await confirmBypass(A, 'bypass'), dialogs);        // first time in this chat: one dialog
      r.push(await confirmBypass(A, 'bypass'), dialogs);        // same chat: no second dialog
      r.push(await confirmBypass(B, 'bypass'), dialogs);        // another chat asks again
      console.log(r.join(','));
    })();
    """)
    assert out == "true,0,true,1,true,1,true,2"


@needs_node
def test_declining_keeps_the_mode_and_asks_again_next_time():
    out = _node(HARNESS + "answer = false;\n" + _fn("confirmBypass") + """
    (async () => {
      const A = { sid: 'a', st: { mode: 'default' } };
      const first = await confirmBypass(A, 'bypass'); const second = await confirmBypass(A, 'bypass');
      console.log([first, second, dialogs].join(','));
    })();
    """)
    assert out == "false,false,2"


def test_every_place_that_sets_the_mode_asks_first():
    for needle in ("confirmBypass(C, v)", "confirmBypass(C, nx[0])", "confirmBypass(C, sel.value)",
                   "confirmBypass(UI.chat, modeSel.value)", "confirmBypass(UI.chat, v)"):
        assert needle in APP_JS, needle
