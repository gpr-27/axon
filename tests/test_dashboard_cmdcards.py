"""Command cards: structured results for the common commands, the fallback frame, pickers, confirm-first, the
composer popup and the Commands page. Page scripts are checked with node and a fake DOM (no browser)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_cmdcards as cc  # noqa: E402
from test_dashboard import c, env, llm, server  # noqa: E402,F401


def run(c, line, session=""):
    st, _, body = c.post("/api/commands/run", {"line": line, "session": session, "cards": True})
    assert st == 200, (st, body[:300])
    return json.loads(body)


def card(c, line, **opts):
    q = "&".join(f"o.{k}={v}" for k, v in opts.items())
    st, _, body = c.get("/api/cmdcard?line=" + line.replace("/", "%2F").replace(" ", "%20") + ("&" + q if q else ""))
    assert st == 200, (st, body[:300])
    return json.loads(body)["card"]


def flat(obj):
    """Every string in a card, to check what it says."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from flat(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from flat(v)


def shown(obj):
    """The strings a person reads in a block (not its type, tone or command fields)."""
    skip = {"t", "tone", "mark", "type", "line", "prefix", "key", "options", "href", "i", "r", "value"}
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip or (k == "value" and isinstance(v, str) and obj.get("label")):
                yield from shown(v)
    elif isinstance(obj, list):
        if len(obj) == 2 and isinstance(obj[0], str) and obj[1] in cc.TONES + ("",):
            yield obj[0]                                    # a table cell with a tone
            return
        for v in obj:
            yield from shown(v)


STRUCTURED = ["/usage", "/cost", "/status", "/context", "/ctxmap", "/model", "/mcp", "/memory", "/skills", "/plugins", "/agents", "/tools",
              "/hooks", "/permissions", "/todos", "/tasks", "/doctor", "/config", "/sessions", "/insights", "/receipts", "/whatif",
              "/budget", "/sandbox", "/trace", "/effort", "/mode", "/theme"]


@pytest.mark.parametrize("line", STRUCTURED)
def test_structured_commands_return_a_card(c, env, llm, line):
    d = card(c, line)
    assert d and d["title"] and d["line"], (line, d)
    assert d["v"] == 1 and isinstance(d["blocks"], list)
    text = "\n".join(flat(d))
    assert "\x1b" not in text and "[0m" not in text, "raw escape text in a card"
    json.dumps(d)


def test_diff_card_needs_a_repo_and_lists_changes(c, env, llm):
    import subprocess as sp
    sp.run(["git", "init", "-q"], cwd=env.ws, check=True)
    (env.ws / "note.txt").write_text("one\n")
    sp.run(["git", "add", "-A"], cwd=env.ws, check=True)
    sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], cwd=env.ws, check=True)
    (env.ws / "note.txt").write_text("one\ntwo\nthree\n")
    d = card(c, "/diff")
    assert d["title"] == "Changes"
    rows = [b for b in d["blocks"] if b["t"] == "table"][0]["rows"]
    assert rows[0][0] == "note.txt" and rows[0][1] == "+2"


def test_run_returns_the_card_instead_of_terminal_text(c, env, llm):
    r = run(c, "/status")
    assert r["ok"] and r["card"]["title"] == "Status" and not r.get("html")


def test_a_command_with_arguments_falls_back_to_terminal_text(c, env, llm):
    r = run(c, "/cost calls")
    assert "card" not in r and r["done"]


def test_rate_limit_bars_only_with_real_quota_data(c, env, llm):
    from axon.features import quota
    quota.clear()
    d = card(c, "/usage")
    assert not [b for b in d["blocks"] if b["t"] == "bars" and any(r["label"] in ("Requests", "Tokens") for r in b["rows"])]
    assert any("No rate-limit data" in s for s in flat(d))
    quota.record_rate_limits({"anthropic-ratelimit-requests-limit": "100", "anthropic-ratelimit-requests-remaining": "25",
                              "anthropic-ratelimit-requests-reset": "2099-01-01T00:00:00Z"}, "t")
    try:
        d = card(c, "/usage")
        rows = [r for b in d["blocks"] if b["t"] == "bars" for r in b["rows"] if r["label"] == "Requests"]
        assert rows and abs(rows[0]["frac"] - 0.75) < 1e-9 and rows[0]["value"] == "75% used"
    finally:
        quota.clear()


def test_usage_dropdowns_requery(c, env, llm):
    d = card(c, "/usage", period="24h", scope="project")
    sel = {s["key"]: s["value"] for s in d["selects"]}
    assert sel == {"period": "24h", "scope": "project"}
    assert {s["key"] for s in card(c, "/insights")["selects"]} == {"period", "scope"}


def test_pickers_mark_the_current_choice(c, env, llm):
    d = card(c, "/mode")
    p = d["blocks"][0]
    assert p["t"] == "pick" and p["prefix"] == "/mode " and p["current"] in [o["value"] for o in p["options"]]
    assert {o["value"] for o in p["options"]} >= {"default", "plan"}
    m = card(c, "/model")
    assert any(b["t"] == "pick" and b["prefix"] == "/model " for b in m["blocks"]) or any(b["t"] == "text" for b in m["blocks"])


def test_picking_a_value_runs_the_command_and_changes_it(c, env, llm):
    r = run(c, "/mode plan")
    assert r["ok"]
    assert card(c, "/mode")["blocks"][0]["current"] == "plan"


def test_destructive_commands_ask_in_the_card_first(c, env, llm):
    r = run(c, "/clear")
    assert r["card"]["confirm"]["line"] == "/clear" and "conversation" in r["card"]["confirm"]["text"]
    st, _, _ = c.post("/api/cmdcard/confirm", {"line": "/clear"})
    assert st == 200
    r2 = run(c, "/clear")
    assert "card" not in r2 and r2["ok"]
    assert "card" in run(c, "/clear")                       # the approval is used up


def test_confirm_for_a_safe_line_approves_nothing(c, env, llm):
    c.post("/api/cmdcard/confirm", {"line": "/status"})
    assert not cc._APPROVED


def test_audit_counts_every_listed_command(c, env, llm):
    st, _, body = c.get("/api/cmdcard?audit=1")
    a = json.loads(body)
    assert a["total"] > 100 and len(a["structured"]) + len(a["text"]) == a["total"]
    assert len(a["structured"]) >= 26 and {"usage", "cost", "plugin", "receipts"} <= set(a["structured"])


def test_choices_for_plain_text_commands(c, env, llm):
    st, _, body = c.get("/api/cmdcard?choices=1&line=%2Ffind")
    assert st == 200 and isinstance(json.loads(body)["choices"], list)


def test_card_route_needs_the_login_cookie(server):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server.port)
    conn.request("GET", "/api/cmdcard?line=/status")
    assert conn.getresponse().status in (401, 403)


# ─── the page script, run in node against a small fake DOM ──────────────────

from axon.features.dash_js_cmdcards import CMDCARDS_CSS, JS_CMDCARDS  # noqa: E402
from axon.features.dashboard_web import APP_JS  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page script")

HARNESS = r"""
class Text { constructor(s){this.s=s;this.nodeType=3;this.parentNode=null;} get textContent(){return this.s;} }
class Node {
  constructor(t){this.tag=t;this.kids=[];this.attrs={};this.parentNode=null;this.nodeType=1;this.listeners={};this.hidden=false;this.value='';this.dataset={};this._cls=new Set();this.style={};
    const self=this; this.classList={add:(...c)=>c.forEach(x=>self._cls.add(x)),remove:(...c)=>c.forEach(x=>self._cls.delete(x)),contains:(c)=>self._cls.has(c),toggle:(c,f)=>{const on=f===undefined?!self._cls.has(c):!!f;on?self._cls.add(c):self._cls.delete(c);return on;}};}
  get className(){return [...this._cls].join(' ');} set className(v){this._cls=new Set(String(v).split(/\s+/).filter(Boolean));}
  _adopt(x){if(x.parentNode)x.parentNode.kids=x.parentNode.kids.filter(k=>k!==x);x.parentNode=this;return x;}
  append(...k){for(const x of k){if(x==null||x===false)continue;const n=typeof x==='string'?new Text(x):x;this._adopt(n);this.kids.push(n);}}
  prepend(...k){for(const x of k.reverse()){this._adopt(x);this.kids.unshift(x);}}
  replaceChildren(...k){this.kids.forEach(x=>x.parentNode=null);this.kids=[];this.append(...k);}
  replaceWith(n){const p=this.parentNode;if(!p)return;const i=p.kids.indexOf(this);p.kids[i]=n;if(n.parentNode&&n.parentNode!==p)n.parentNode.kids=n.parentNode.kids.filter(k=>k!==n);n.parentNode=p;this.parentNode=null;}
  after(n){const p=this.parentNode;const i=p.kids.indexOf(this);p.kids.splice(i+1,0,n);n.parentNode=p;}
  remove(){if(this.parentNode){this.parentNode.kids=this.parentNode.kids.filter(k=>k!==this);this.parentNode=null;}}
  setAttribute(k,v){this.attrs[k]=String(v);if(k==='class')this.className=v;} getAttribute(k){return this.attrs[k]===undefined?null:this.attrs[k];}
  addEventListener(t,f){(this.listeners[t]=this.listeners[t]||[]).push(f);} fire(t,ev){(this.listeners[t]||[]).forEach(f=>f(ev||{preventDefault(){}}));}
  click(){this.fire('click');}
  get textContent(){return this.kids.map(k=>k.textContent).join('');}
  set textContent(v){this.kids=[new Text(String(v))];this.kids[0].parentNode=this;}
  get scrollHeight(){return 0;} get childNodes(){return this.kids;} get children(){return this.kids.filter(k=>k.nodeType===1);}
  _match(sel){const m=/^([a-z0-9]*)((?:\.[\w-]+)*)$/.exec(sel.trim());if(!m)return false;if(m[1]&&m[1]!==this.tag)return false;return m[2].split('.').filter(Boolean).every(c=>this._cls.has(c));}
  querySelectorAll(sel){if(!sel.startsWith(':scope') && sel.includes(' ')){const [a,...rest]=sel.split(' ');return this.querySelectorAll(a).flatMap(n=>n.querySelectorAll(rest.join(' ')));}
    const out=[];const direct=sel.startsWith(':scope >');const s=direct?sel.slice(8).trim():sel;
    const walk=(n,top)=>{for(const k of n.kids){if(k.nodeType!==1)continue;if(k._match(s))out.push(k);if(!direct)walk(k);}};walk(this);return out;}
  querySelector(sel){return this.querySelectorAll(sel)[0]||null;}
  scrollIntoView(){}
}
function h(tag, props, ...kids){const el=new Node(tag);for(const [k,v] of Object.entries(props||{})){if(v==null||v===false)continue;
  if(k==='class')el.className=v;else if(k==='html')el.innerHTML=v;else if(k.startsWith('on'))el.addEventListener(k.slice(2),v);else if(k==='hidden')el.hidden=true;else el.setAttribute(k,v===true?'':v);}
  el.append(...kids.flat(3));return el;}
globalThis.document={createComment:()=>{const n=new Node('#comment');n.nodeType=8;return n;},createElement:(t)=>new Node(t),createTextNode:(s)=>new Text(s)};
globalThis.requestAnimationFrame=undefined;
const calls=[]; let toasts=[]; let copied=[]; let apiQueue={};
function lsGet(){return null;} function toast(m){toasts.push(m);} function decorate(r){return r;} function stick(){} function onLeave(){}
function notice(t,c){const n=h('div',{class:'notice '+(c||'')},t);return n;} function errorBox(m){return h('div',{class:'err-box'},m);}
function copyText(t){copied.push(t);} function termHint(){} function execLine(l){calls.push(['exec',l]);} function refreshState(){}
async function runSlash(line){calls.push(['run',line]);const q=apiQueue.run&&apiQueue.run.shift();return q||{done:true,ok:true,html:'',message:'',ms:1};}
async function api(path,body){calls.push([body?'post':'get',path,body]);const k=path.split('?')[0];const q=apiQueue[k]&&apiQueue[k].shift();return q||{};}
function commandResultCard(){calls.push(['old-card']);}
const text=(n)=>n.textContent; const find=(n,sel)=>n.querySelector(sel); const all=(n,sel)=>n.querySelectorAll(sel);
const tick=()=>new Promise(r=>setTimeout(r,0));
"""


def node(body: str) -> object:
    src = HARNESS + JS_CMDCARDS + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e.stack); process.exit(1); });"
    p = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@needs_node
def test_page_script_is_valid_javascript(tmp_path):
    f = tmp_path / "app.js"
    f.write_text(APP_JS, encoding="utf-8")
    p = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    for hook in ("cmdCardShow(C, line, r)", "cmdCardInto(r, into)", "cmdSuggest(C.cmds, first, C.hist)", "cmdPopSelect(pop, sel)", "cmdCatalogRow(c"):
        assert hook in APP_JS, hook
    assert "CMDCARDS_CSS" not in APP_JS and ".cc-top" in dash_css_text()


def dash_css_text():
    from axon.features.dashboard_web import asset
    return asset("app.css")[0]


@needs_node
@pytest.mark.parametrize("line", ["/usage", "/cost", "/context", "/tools", "/mode", "/config", "/doctor", "/insights"])
def test_every_block_kind_renders_with_text_nodes(c, env, llm, line):
    d = card(c, line)
    out = node(f"""
const card = {json.dumps(d)};
const ctx = {{ C: null, session: '', opts: {{}}, emit() {{}} }};
const root = ccFromResult({{ card, ok: true, done: true }}, {json.dumps(line)}, ctx);
const t = text(root);
console.log(JSON.stringify({{ text: t, plain: ccText(card), htmlAttr: all(root, 'div').some(n => n.innerHTML !== undefined) }}));""")
    assert d["title"] in out["text"] and d["title"] in out["plain"]
    assert not out["htmlAttr"], "a card must not use innerHTML"
    for s in shown(d["blocks"]):
        if len(s) < 40 and s.strip() and "\n" not in s:
            assert s in out["text"] or s in out["plain"], s


@needs_node
def test_bars_tables_and_long_lists_collapse(c, env, llm):
    out = node("""
const card = { v: 1, title: 'T', sub: '', selects: [], actions: [], tip: 'Trim it', blocks: [
  { t: 'bars', rows: [{ label: 'Requests', frac: 0.75, value: '75% used', tone: 'warn', note: '100 limit' }, { label: 'Open', frac: null, value: '5 left', note: '' }] },
  { t: 'table', cols: [{ l: 'Name', r: false }, { l: 'N', r: true }], rows: Array.from({ length: 14 }, (_, i) => ['row' + i, String(i)]), note: 'n' },
  { t: 'list', rows: Array.from({ length: 12 }, (_, i) => ({ mark: 'ok', text: 'item' + i })), empty: '' }] };
const root = ccStructured(card, { C: null, session: '', opts: {}, emit() {} });
const bar = find(root, '.cc-bt i');
const trs = all(root, 'tr'); const lis = all(root, 'li');
const more = all(root, '.cc-more');
const hiddenRows = trs.filter(r => r.hidden).length, hiddenLis = lis.filter(r => r.hidden).length;
more[0].click();
console.log(JSON.stringify({ w: bar.attrs.style, cls: bar.className, hiddenRows, hiddenLis,
  afterOpen: trs.filter(r => r.hidden).length, tip: text(find(root, '.cc-tip')), moreLabel: more.map(m => m.textContent), noTrack: all(root, '.cc-bt.none').length }));""")
    assert out["w"] == "width:75.0%" and "warn" in out["cls"]
    assert out["hiddenRows"] == 4 + 0 and out["afterOpen"] == 0 and out["hiddenLis"] == 2
    assert out["moreLabel"][0] == "Show fewer" and out["noTrack"] == 1 and out["tip"].endswith("Trim it")


@needs_node
def test_pick_runs_the_command_then_redraws_the_card(c, env, llm):
    out = node("""
const card = { v: 1, title: 'Mode', sub: '', selects: [], actions: [], tip: '', line: '/mode', blocks: [
  { t: 'pick', label: 'Mode', current: 'default', prefix: '/mode ', options: [{ value: 'default', label: 'default', hint: '' }, { value: 'plan', label: 'plan', hint: 'read only' }] }] };
apiQueue['/api/cmdcard'] = [{ card: Object.assign({}, card, { blocks: [Object.assign({}, card.blocks[0], { current: 'plan' })] }) }];
apiQueue.run = [{ done: true, ok: true, html: '', message: 'Mode set to plan', ms: 1 }];
const feed = h('div', { class: 'feed' });
const C = { feed, sid: 's1', atBottom: true };
const r = { done: true, ok: true, card, line: '/mode' };
cmdCardShow(C, '/mode', r);
const root = find(feed, '.cc');
const opts = all(root, '.cc-opt');
opts[1].click(); await tick(); await tick(); await tick();
const now = find(feed, '.cc');
console.log(JSON.stringify({ calls, said: text(find(feed, '.cc-said')), on: all(now, '.cc-opt.on').map(n => text(n)), checked: all(now, '.cc-opt').map(n => n.attrs['aria-checked']) }));""")
    assert out["said"] == "/mode"
    assert ["run", "/mode plan"] in [x[:2] for x in out["calls"]]
    assert any(x[0] == "get" and "line=%2Fmode" in x[1] for x in out["calls"]), out["calls"]
    assert out["checked"] == ["false", "true"] and "plan" in out["on"][0]


@needs_node
def test_confirm_card_asks_then_runs_only_after_yes(c, env, llm):
    out = node("""
const conf = { v: 1, title: '/clear', line: '/clear', blocks: [], selects: [], actions: [], tip: '', confirm: { text: 'This clears the conversation.', line: '/clear' } };
const feed = h('div', { class: 'feed' }); const C = { feed, sid: 's', atBottom: true };
cmdCardShow(C, '/clear', { done: true, ok: true, card: conf });
const root = find(feed, '.cc');
const before = calls.length;
const btns = all(root, 'button').filter(b => /Yes|Cancel/.test(text(b)));
const ran0 = calls.filter(c => c[0] === 'run').length;
apiQueue.run = [{ done: true, ok: true, html: '', message: 'Started a fresh session.', ms: 2 }];
btns[0].click(); await tick(); await tick(); await tick();
console.log(JSON.stringify({ labels: btns.map(b => text(b)), ran0, posted: calls.filter(c => c[0] === 'post').map(c => c[1]), ran: calls.filter(c => c[0] === 'run').map(c => c[1]), shown: text(feed) }));""")
    assert out["labels"] == ["Yes, run /clear", "Cancel"] and out["ran0"] == 0
    assert out["posted"] == ["/api/cmdcard/confirm"] and out["ran"] == ["/clear"] and "fresh session" in out["shown"]


@needs_node
def test_text_results_keep_the_frame_and_never_show_escape_text(c, env, llm):
    r = run(c, "/find nothing-here-xyz")
    out = node(f"""
const r = {json.dumps(r)};
const feed = h('div', {{ class: 'feed' }}); const C = {{ feed, sid: 's', atBottom: true }};
const handled = cmdCardShow(C, '/find nothing-here-xyz', r);
const root = find(feed, '.cc');
console.log(JSON.stringify({{ handled, title: text(find(root, 'h3')), copy: !!all(root, 'button').find(b => text(b) === 'Copy'), pin: !!all(root, 'button').find(b => text(b) === 'Pin'), body: text(root) }}));""")
    assert out["handled"] and out["title"] == "/find" and out["copy"] and out["pin"]
    assert "\x1b" not in out["body"] and "[0m" not in out["body"]


@needs_node
def test_pin_moves_the_card_to_the_top_and_back(c, env, llm):
    out = node("""
const feed = h('div', { class: 'feed' }); const C = { feed, sid: 's', atBottom: true };
feed.append(h('p', null, 'earlier message'));
cmdCardShow(C, '/status', { done: true, ok: true, ms: 1, html: 'hello', message: '' });
const root = find(feed, '.cc');
const pin = all(root, 'button').find(b => text(b) === 'Pin');
pin.click();
const pinned = find(feed, '.cc-pins .cc');
const label = text(pin);
pin.click();
console.log(JSON.stringify({ pinned: !!pinned, label, back: !!find(feed, '.cc-pins'), labelAfter: text(pin), inFeed: feed.children.includes(root) }));""")
    assert out == {"pinned": True, "label": "Unpin", "back": False, "labelAfter": "Pin", "inFeed": True}


@needs_node
def test_card_text_copies_as_aligned_plain_text():
    out = node("""
const card = { title: 'Usage', sub: 'm', line: '/usage', selects: [{ key: 'p', label: 'Period', value: '7d', options: [['24h', 'Last 24 hours'], ['7d', 'Last 7 days']] }], tip: 'x',
  blocks: [{ t: 'table', cols: [{ l: 'Model', r: false }, { l: 'Cost', r: true }], rows: [['gpt', '$1.00'], ['long-model-name', '$12.50']], note: '' }, { t: 'kv', rows: [{ k: 'Total', v: '$13' }] }] };
console.log(JSON.stringify(ccText(card)));""")
    lines = out.splitlines()
    assert lines[0] == "/usage" and "Period: Last 7 days" in out and "Tip: x" in lines[-1]
    t = [ln for ln in lines if ln.startswith(("Model", "gpt", "long-model-name"))]
    assert len({len(x) for x in t}) == 1 and t[1].endswith("$1.00") and t[2].endswith("$12.50")


@needs_node
def test_slash_popup_is_fuzzy_grouped_with_recent_first():
    out = node("""
const cmds = [
 { name: '/cost', aliases: ['/ledger'], category: 'Context & Cost', description: 'Cost by source', usage: '/cost            session breakdown\\n/cost calls', kind: 'builtin' },
 { name: '/context', aliases: [], category: 'Context & Cost', description: 'Context usage', usage: '/context', kind: 'builtin' },
 { name: '/model', aliases: [], category: 'Model & Modes', description: 'Switch model', usage: '/model <name>', kind: 'builtin' },
 { name: '/mode', aliases: [], category: 'Model & Modes', description: 'Permission mode', usage: '/mode [default|plan]', kind: 'builtin' },
 { name: '/review', aliases: [], category: 'Code & Review', description: 'Review code', usage: '/review', kind: 'builtin' },
 { name: '/my-skill', aliases: [], category: 'Custom and skills', description: 'A skill', usage: '/my-skill', kind: 'extension' }];
const hist = ['hello', '/review', '/mode plan', '/review'];
const empty = cmdSuggest(cmds, '/', hist);
const typed = cmdSuggest(cmds, '/mo', hist);
const fuzzy = cmdSuggest(cmds, '/cst', hist);
const alias = cmdSuggest(cmds, '/led', hist);
const pop = h('div', { class: 'pop' });
pop.replaceChildren(...cmdPopNodes(typed, () => {}));
cmdPopSelect(pop, 1);
console.log(JSON.stringify({ empty: empty.map(i => i.group + ':' + i.label), typed: typed.map(i => i.label), fuzzy: fuzzy.map(i => i.label), alias: alias.map(i => i.label + '|' + (i.alias || '')),
  arg: typed.find(i => i.label === '/model').arg, groups: all(pop, '.grp').map(n => text(n)), sel: all(pop, '.it').map(n => n.attrs['aria-selected']) }));""")
    assert out["empty"][:2] == ["Recent:/mode", "Recent:/review"] or out["empty"][0].startswith("Recent:")
    assert out["empty"][0] == "Recent:/review"
    assert out["typed"][0] == "/mode" and "/model" in out["typed"]
    assert out["fuzzy"] == ["/cost"] and out["alias"] == ["/cost|/ledger"]
    assert out["arg"] == "<name>" and out["groups"] == ["Model & Modes"] and out["sel"] == ["false", "true"]


@needs_node
def test_commands_page_rows_have_a_run_button():
    out = node("""
let picked = 0, ran = 0;
const row = cmdCatalogRow({ name: '/cost', aliases: [], description: 'Cost by source', usage: '/cost [calls]', terminal_only: '' }, () => { picked++; }, () => { ran++; });
const btns = all(row, 'button'); btns[0].click(); btns[1].click();
const term = cmdCatalogRow({ name: '/exit', aliases: [], description: 'x', usage: '/exit', terminal_only: 'Ends the terminal session.' }, () => {}, () => {});
console.log(JSON.stringify({ picked, ran, labels: btns.map(b => text(b)), termButtons: term.tag === 'button' ? 1 : all(term, 'button').length, desc: text(btns[0]) }));""")
    assert out["picked"] == 1 and out["ran"] == 1 and out["labels"][1] == "Run" and out["termButtons"] == 1
    assert "Cost by source" in out["desc"] and "[calls]" in out["desc"]


def test_css_has_dark_light_safe_tokens_and_phone_rules():
    assert "var(--surface)" in CMDCARDS_CSS and "@media (max-width:560px)" in CMDCARDS_CSS and "animation" not in CMDCARDS_CSS
    assert "#" not in CMDCARDS_CSS.replace("&#", ""), "colours must come from the theme variables"


def test_without_the_cards_flag_the_terminal_renderer_answers(c, env, llm):
    r = json.loads(c.post("/api/commands/run", {"line": "/status"})[2])
    assert "card" not in r and "Axon status" in r["html"]
    assert "card" not in json.loads(c.post("/api/commands/run", {"line": "/clear"})[2])


@pytest.mark.parametrize("line", ["/delete session_x", "/delete --all", "/memory forget x", "/memory del x", "/plugin uninstall p", "/plugin install p@m",
                                  "/plugin marketplace add some/repo", "/agents delete a", "/keys unset ANTHROPIC_API_KEY",
                                  "/endpoint forget openai", "/worktree remove w"])
def test_data_removing_or_code_installing_commands_ask_first(c, env, llm, line):
    """The page has no keyboard prompt, so a non-terminal confirm() takes its default (Yes). The card must ask."""
    r = run(c, line)
    assert r["card"]["confirm"]["line"] == line, r


@pytest.mark.parametrize("line", ["/memory list", "/plugin list", "/plugin marketplace list", "/keys list", "/worktree"])
def test_read_only_forms_do_not_ask(c, env, llm, line):
    r = run(c, line)
    assert "confirm" not in (r.get("card") or {})


def test_terminal_screens_get_unicode_not_question_marks(c, env, llm):
    r = run(c, "/budget 5")
    assert " ? " not in (r.get("html") or ""), r["html"]


@pytest.mark.parametrize("line", ["/paste", "/terminal-setup"])
def test_terminal_only_commands_say_so_plainly(c, env, llm, line):
    r = run(c, line)
    assert r["ok"] is False and r["terminal_only"] and "terminal" in r["message"].lower() and not r["html"]


def test_setup_points_to_settings_providers(c, env, llm):
    r = run(c, "/setup")
    assert r["open"] == "#/settings/providers"


@pytest.mark.parametrize("line", ["/", "/ ", "#", "/   "])
def test_a_bare_slash_is_answered_not_a_server_error(c, env, llm, line):
    st, _, body = c.post("/api/commands/run", {"line": line, "session": "", "cards": True})
    assert st == 200, (st, body[:200])


def test_slash_popup_ignores_a_command_list_that_arrives_after_the_box_changed():
    """The first '/' fetches the command list. If the line was sent meanwhile, the late answer must not reopen the popup."""
    i = APP_JS.index("async function suggest()")
    body = APP_JS[i:i + 1800]
    assert body.index("await api('/api/commands?") < body.index("!== first) return") < body.index("showPop(cmdSuggest(")


def test_shell_output_is_added_in_batches_and_trimmed_in_both_places():
    """A command printing megabytes used to freeze the page: every 32 KB piece forced a layout of the whole block."""
    for name in ("function mountShell", "async function shellFromChat"):
        body = APP_JS[APP_JS.index(name):]
        body = body[:body.index("\n}\n")]
        assert "setTimeout(" in body and "300000" in body, name
    assert "api('/api/shell/kill'" in APP_JS[APP_JS.index("async function shellFromChat"):][:2500]


@pytest.mark.parametrize("line", ["/resume", "/delete"])
def test_resume_and_delete_without_an_id_open_sessions_instead_of_guessing(c, env, llm, line):
    r = run(c, line)
    assert r["open"] == "#/sessions" and "Sessions" in r["message"] and "card" not in r
