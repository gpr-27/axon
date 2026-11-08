"""Dashboard tool cards, notifications, ghost suggestion and trace levels.

The card model and renderer run in node against a tiny fake DOM, so these test the real page script (no browser)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_css  # noqa: E402
from axon.features.dash_js_cards import JS_CARDS  # noqa: E402
from axon.features.dash_js_chat import JS_CHAT  # noqa: E402
from axon.features.dashboard_web import APP_JS  # noqa: E402
from test_dashboard import c, env, llm, server  # noqa: E402,F401

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page script")
ROOT = Path(__file__).parent.parent

HARNESS = r"""
class Node { constructor(t){this.tag=t;this.kids=[];this.cls='';this.attrs={};} append(...k){for(const x of k){if(x!=null&&x!==false)this.kids.push(typeof x==='string'||typeof x==='number'?new Text(String(x)):x);}}
  get firstChild(){return this.kids[0]||null;} replaceChildren(){this.kids=[];} prepend(...k){this.kids.unshift(...k.map(x=>typeof x==='string'?new Text(x):x));}
  set className(v){this.cls=v;} set textContent(v){this.kids=[new Text(String(v))];} get textContent(){return this.kids.map(k=>k.textContent).join('');}
  setAttribute(k,v){this.attrs[k]=v;} addEventListener(){} classList={add:()=>{}};
  querySelector(sel){const want=sel.slice(1);const walk=(n)=>{for(const k of n.kids){if(k.cls&&k.cls.split(' ').includes(want))return k;const r=k.kids&&walk(k);if(r)return r;}return null;};return walk(this);}
  html(){const e=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    return '<'+this.tag+(this.cls?' class="'+e(this.cls)+'"':'')+'>'+this.kids.map(k=>k.html()).join('')+'</'+this.tag+'>';} }
class Text { constructor(s){this.s=s;this.kids=null;} get textContent(){return this.s;} html(){return this.s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');} }
globalThis.document = { createElement:(t)=>new Node(t), createTextNode:(s)=>new Text(s) };
function h(tag, props, ...kids){const el=new Node(tag);for(const [k,v] of Object.entries(props||{})){if(v==null||v===false)continue;if(k==='class')el.cls=v;else if(!k.startsWith('on')&&k!=='html')el.attrs[k]=v;}el.append(...kids);return el;}
function diffEl(lines){return h('pre',{class:'diff'},lines.join('\n'));}
function lineDiff(a,b){return a.split('\n').map(x=>'-'+x).concat(b.split('\n').map(x=>'+'+x));}
var lsGet=()=>null, lsSet=()=>{}, toast=()=>{}, usd=(x)=>'$'+x, onLeave=()=>{}, turnWord=(x)=>x;
"""


def run_js(body: str) -> object:
    src = HARNESS + JS_CARDS + "\n" + body
    p = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def model(name, inp, content, err=False, dur=None):
    return run_js(f"console.log(JSON.stringify(axCardModel({json.dumps(name)},{json.dumps(inp)},{json.dumps(content)},{json.dumps(err)},{json.dumps(dur)})))")


def test_page_script_is_valid_javascript(tmp_path):
    f = tmp_path / "app.js"
    f.write_text(APP_JS, encoding="utf-8")
    p = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    assert "function axCard(" in APP_JS and "axOnEvent(C, ev)" in APP_JS and "axGhost(C, ta)" in APP_JS and "axNotifyPill()" in APP_JS


def test_every_tool_has_a_sentence_and_unknown_and_mcp_fall_back():
    names = set()
    for f in (ROOT / "src/axon/tools").glob("*.py"):
        names.update(re.findall(r'^\s+name: ClassVar\[str\] = "(\w+)"', f.read_text(encoding="utf-8"), re.M))
    assert len(names) >= 30
    got = run_js(f"console.log(JSON.stringify({json.dumps(sorted(names))}.map(n=>[n,axCardModel(n,{{}},'',false,null).sentence])))")
    bad = [n for n, s in got if not s or s.startswith("Used ")]
    assert not bad, bad
    assert model("Frobnicate", {}, "x")["sentence"] == "Used Frobnicate"
    assert model("mcp__github__list_issues", {}, "x")["sentence"] == "Called list issues on the github server"
    assert model("Bash", {"command": "ls"}, "x", err=True)["sentence"].endswith("failed with exit code 1")


def test_test_results_become_a_pass_fail_summary():
    m = model("Bash", {"command": "pytest"}, "====== 2 failed, 10 passed, 1 skipped in 3.20s ======")
    assert m["kind"] == "tests" and m["data"]["tests"] == {"pass": 10, "fail": 2, "skip": 1}
    assert m["sentence"] == "Tests failed: 10 passed, 2 failed, 1 skipped"
    assert model("Bash", {"command": "jest"}, "Tests:       1 failed, 4 passed, 5 total")["data"]["tests"] == {"pass": 4, "fail": 1, "skip": 0}
    ok = model("Bash", {"command": "go test"}, "ok  \tpkg\t0.2s\n")
    assert ok["data"]["tests"]["pass"] == 1
    view = run_js("console.log(JSON.stringify(axView(axCardModel('Bash',{},'3 passed, 1 failed in 1s',false,null)).html()))")
    assert 'class="tbar"' in view and "3 passed" in view


def test_bash_exit_code_and_duration():
    ok = model("Bash", {"command": "make"}, "(Command completed with exit code 0 and no output in 2.5s)")
    assert ok["data"] == {"code": 0, "dur": 2.5} and ok["chip"] == "exit 0 · 2.5s"
    bad = model("Bash", {"command": "false"}, "Command exited with non-zero code 2.\nOutput:\nboom", dur=0.4)
    assert bad["data"]["code"] == 2 and "exit code 2" in bad["sentence"] and bad["chip"].startswith("exit 2")


def test_edit_write_multiedit_count_added_and_removed():
    e = model("Edit", {"file_path": "/a/b/c.py", "old_string": "a\nb\nc", "new_string": "a\nB\nc\nd"}, "ok")
    assert e["data"]["added"] == 2 and e["data"]["removed"] == 1 and e["chip"] == "+2 −1"
    assert e["sentence"] == "Edited a/b/c.py: 2 lines added, 1 line removed"
    w = model("Write", {"file_path": "n.txt", "content": "x\ny\nz"}, "ok")
    assert w["data"]["added"] == 3 and w["data"]["removed"] == 0
    # a trailing newline ends the last line, it does not start another one
    assert model("Write", {"file_path": "n.txt", "content": "hello\n"}, "ok")["data"]["added"] == 1
    assert model("Write", {"file_path": "n.txt", "content": "x\ny\n"}, "ok")["data"]["added"] == 2
    mu = model("MultiEdit", {"file_path": "f", "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c\nd", "new_string": "c"}]}, "ok")
    assert (mu["data"]["added"], mu["data"]["removed"]) == (1, 2)


def test_grep_matches_files_and_todos():
    g = model("Grep", {"pattern": "foo"}, "src/a.py:3:foo = 1\nsrc/b.py:9:def foo():\n")
    assert g["kind"] == "grep" and len(g["data"]["hits"]) == 2 and g["data"]["files"] == 2
    assert g["sentence"].endswith("2 matches in 2 files")
    assert model("Grep", {"pattern": "q"}, "No matches found for pattern 'q'.")["sentence"].endswith("no matches")
    fl = model("Glob", {"pattern": "*.py"}, "a.py\nb.py\nFound 2 files")
    assert fl["kind"] == "files" and fl["data"]["names"] == ["a.py", "b.py"]
    t = model("TodoWrite", {"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]}, "Updated todos")
    assert t["sentence"] == "Updated the todo list: 1 of 2 done" and t["kind"] == "todos"


def test_rendered_cards_escape_all_tool_text():
    evil = "<script>alert(1)</script><img src=x onerror=1>"
    html = run_js(f"""
const e = {json.dumps(evil)};
const outs = [
  axView(axCardModel('Grep', {{pattern: 'script'}}, 'f.py:1:' + e, false, null)).html(),
  axView(axCardModel('Glob', {{}}, e + '.py', false, null)).html(),
  axView(axCardModel('TodoWrite', {{todos: [{{content: e, status: 'pending'}}]}}, '', false, null)).html(),
  axView(axCardModel('Bash', {{command: e}}, e, false, null)).html(),
  axView(axCardModel('Edit', {{old_string: e, new_string: 'x'}}, '', false, null)).html()];
console.log(JSON.stringify(outs));""")
    for h_ in html:
        assert "<script" not in h_ and "<img" not in h_, h_
    assert "<mark>script</mark>" in html[0] and "&lt;" in html[0]


def test_grep_highlight_survives_a_bad_pattern():
    out = run_js("const n=h('div');axMark(n,'a(b','(');console.log(JSON.stringify(n.html()))")
    assert "a(b" in out


def test_trace_levels_change_what_is_shown():
    css = dash_css.APP_LAYOUT_CSS
    assert ".t-normal .dtl" in css and ".t-compact .trail dt:nth-of-type(-n+2)" in css and ".t-debug .rawev{display:block}" in css
    # the picker offers all four levels and sets the class the CSS keys on
    assert "'normal', 'Normal'" in JS_CHAT and "'detailed', 'Detailed'" in JS_CHAT and "'debug', 'Debug (raw events)'" in JS_CHAT
    assert "C.feed.className = 'feed t-' + sel.value" in JS_CHAT


def test_notifications_only_when_opted_in_and_hidden():
    r = run_js("""
const calls=[]; let granted=true, store='1';
globalThis.Notification=function(t,o){calls.push(t);this.close=()=>{};}; Notification.permission='granted';
globalThis.window={focus(){}}; globalThis.document.hidden=true;
lsGet=()=>store;
const C={replaying:false};
axOnEvent(C,{type:'turn_end',stop_reason:'end_turn',cost_usd:0.01});
axOnEvent(C,{type:'approval_request',tool:'Bash',id:'1'});
store='0'; axOnEvent(C,{type:'turn_end'});          // toggled off
store='1'; document.hidden=false; axOnEvent(C,{type:'turn_end'});   // tab visible
C.replaying=true; document.hidden=true; axOnEvent(C,{type:'turn_end'});  // replay of history
console.log(JSON.stringify(calls));""")
    assert r == ["Axon finished", "Axon needs permission"]


def test_ghost_suggestion_shows_when_empty_and_tab_accepts():
    r = run_js("""
const timers=[]; globalThis.setInterval=(f)=>timers.push(f)&&1; globalThis.clearInterval=()=>{};
const box=new Node('div'); const ghost={};
const ta=new Node('textarea'); ta.value=''; ta.parentElement=box; let keyh=null, inputs=0;
ta.addEventListener=(t,f)=>{ if(t==='keydown') keyh=f; }; ta.dispatchEvent=()=>{inputs++;}; ta.setSelectionRange=()=>{};
const C={st:{suggestion:'run the tests', busy:false}};
axGhost(C, ta); const g=box.kids[0];
timers[0]();                                   // poll tick
const shown=g.hidden===false && g.textContent.startsWith('run the tests');
let prevented=false; keyh({key:'Tab',preventDefault(){prevented=true},stopImmediatePropagation(){}});
const accepted=ta.value;
ta.value=''; C.st.busy=true; timers[0](); const hiddenBusy=g.hidden;
console.log(JSON.stringify({shown,prevented,accepted,inputs,hiddenBusy}));""")
    assert r["shown"] and r["prevented"] and r["accepted"] == "run the tests" and r["inputs"] == 1 and r["hiddenBusy"] is True


def test_ghost_suggestion_replaces_the_placeholder_instead_of_overlapping_it():
    r = run_js("""
const timers=[]; globalThis.setInterval=(f)=>timers.push(f)&&1; globalThis.clearInterval=()=>{};
const box=new Node('div'); const ta=new Node('textarea'); ta.value=''; ta.placeholder='Message Axon'; ta.parentElement=box;
ta.addEventListener=()=>{}; ta.dispatchEvent=()=>{}; ta.setSelectionRange=()=>{};
const C={st:{suggestion:'run the tests', busy:false}};
axGhost(C, ta); timers[0]();
const during=ta.placeholder;
ta.value='typing'; timers[0](); const whileTyping=ta.placeholder; const hidden=box.kids[0].hidden;
console.log(JSON.stringify({during,whileTyping,hidden}));""")
    assert r == {"during": "", "whileTyping": "Message Axon", "hidden": True}


def test_state_carries_the_suggestion(c, env, llm):
    llm.enqueue([{"text": "hello"}])
    sid = json.loads(c.post("/api/chat/send", {"text": "hi"})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    assert "suggestion" in rt.state() and isinstance(rt.state()["suggestion"], str)
    rt.agent.suggestions._text = "now add tests"
    assert rt.state()["suggestion"] == "now add tests"
