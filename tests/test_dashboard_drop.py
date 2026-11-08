"""Drag and drop in the web app: files, folders and pictures from the tree, the search results and the operating
system reach the model. Server side through the test client and the fake LLM; page side with node and a fake DOM
(nothing here was seen in a real browser)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_attach  # noqa: E402
from axon.features.dash_css import APP_LAYOUT_CSS  # noqa: E402
from axon.features.dashboard_web import APP_JS  # noqa: E402
from test_dashboard import Client, c, env, llm, server  # noqa: E402,F401
from test_dashboard_app import _upload, types, wait_for  # noqa: E402

NODE = shutil.which("node")
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def _items(c: Client, *paths: str) -> list[dict]:
    st, _, body = c.post("/api/attach/paths", {"paths": list(paths)})
    assert st == 200, body
    return json.loads(body)["items"]


def _send_and_wait(c: Client, llm, text: str) -> str:
    """Send *text* as the page does, let the fake model answer, and return everything the model was sent."""
    llm.enqueue([{"text": "seen"}])
    sid = json.loads(c.post("/api/chat/send", {"text": text})[2])["session"]
    rt = c.srv.runtime(sid, create=False)
    wait_for(lambda: not rt.busy and "idle" in types(rt.hub), 40)
    return json.dumps(llm.completion_requests()[-1].body)


# ─── server: what each kind of item becomes ─────────────────────────────────

def test_a_tree_file_becomes_an_at_reference_the_model_reads(c, env, llm):
    (env.ws / "notes.py").write_text("SECRET_MARKER = 'blue-heron-42'\n")
    [it] = _items(c, "notes.py")
    assert it["kind"] == "ref" and it["chip"] == "@notes.py" and it["path"] == "notes.py" and it["size"] > 0
    sent = _send_and_wait(c, llm, f"what is in {it['chip']} ?")
    assert "blue-heron-42" in sent and "[File: notes.py]" in sent


def test_a_nested_file_and_a_large_file(c, env, llm):
    d = env.ws / "a" / "b" / "c"
    d.mkdir(parents=True)
    (d / "deep.txt").write_text("deep-marker-77")
    (env.ws / "big.txt").write_text("x" * (dash_attach.INLINE_BYTES + 10))
    deep, big = _items(c, "a/b/c/deep.txt", "big.txt")
    assert "deep-marker-77" in dash_attach.expand(deep["chip"])
    out = dash_attach.expand(big["chip"])
    assert "too large" in out and "Read big.txt" in out and "xxxx" not in out


def test_a_folder_is_a_short_flat_listing_with_a_limit(c, env, llm):
    big = env.ws / "many"
    big.mkdir()
    (big / "sub").mkdir()
    (big / "sub" / "hidden_inner.txt").write_text("not listed")
    for i in range(150):
        (big / f"f{i:03}.txt").write_text("x")
    [it] = _items(c, "many")
    assert it["kind"] == "folder" and it["chip"].startswith("[Folder #") and it["entries"] == 151
    sent = _send_and_wait(c, llm, f"look at {it['chip']}")
    assert "many/ at " in sent and json.dumps(str(big.resolve()))[1:-1] in sent                       # the path is told to the model
    assert "sub/" in sent and "hidden_inner" not in sent                            # one level, not a recursive dump
    assert "f000.txt" in sent and "f149.txt" not in sent                            # capped
    assert f"only the first {dash_attach.LIST_SHOWN} are listed" in sent


def test_an_image_from_the_tree_reaches_the_model_as_a_picture(c, env, llm):
    (env.ws / "shot.png").write_bytes(PNG)
    [it] = _items(c, "shot.png")
    assert it["kind"] == "image" and it["chip"].startswith("[Image #") and it["thumb"].startswith("/api/attachment/image?n=")
    assert c.get(it["thumb"])[2] == PNG                                              # the chip's thumbnail
    sent = _send_and_wait(c, llm, f"describe {it['chip']}")
    assert "data:image/png;base64," in sent                                          # a real image block on the wire


def test_an_os_dropped_image_takes_the_same_road(c, env, llm):
    c.srv.runtime("")
    st, j = _upload(c, "from-finder.png", PNG)
    assert st == 200 and j["kind"] == "image"
    assert "data:image/png;base64," in _send_and_wait(c, llm, f"look {j['chip']}")


def test_a_pdf_gets_a_reference_that_points_at_read_with_pages(c, env):
    (env.ws / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    [it] = _items(c, "paper.pdf")
    assert it["kind"] == "pdf" and it["chip"] == "@paper.pdf"
    out = dash_attach.expand("see @paper.pdf.")
    assert "Read tool" in out and 'pages="1-5"' in out


def test_credential_files_outside_files_and_missing_ones_say_why(c, env):
    (env.ws / ".env").write_text("API_KEY=abc")
    (env.ws / ".ssh").mkdir()
    (env.ws / ".ssh" / "id_rsa").write_text("PRIVATE")
    outside = env.tmp / "elsewhere.txt"
    outside.write_text("hi")
    items = _items(c, ".env", ".ssh/id_rsa", str(outside), "nope.txt")
    assert all("error" in i and "chip" not in i for i in items), items
    assert "credential" in items[0]["error"] and "credential" in items[1]["error"]
    assert "outside the project" in items[2]["error"] and "no longer exists" in items[3]["error"]
    assert "API_KEY" not in dash_attach.expand("@.env")                              # never registered, never read
    c.srv.runtime("")
    st, j = _upload(c, ".env", b"API_KEY=abc")
    assert st == 403 and "credential" in j["error"]


def test_several_items_in_one_drop_and_a_bad_one_does_not_stop_the_rest(c, env):
    (env.ws / "one.txt").write_text("1")
    (env.ws / "two.txt").write_text("2")
    items = _items(c, "one.txt", "missing.txt", "two.txt", "one.txt")
    assert [i.get("chip") for i in items] == ["@one.txt", None, "@two.txt"]          # duplicate dropped, error kept in place


def test_a_finder_folder_keeps_names_only_and_says_so(c, env, llm):
    st, _, body = c.post("/api/attach/folder", {"name": "Holiday", "entries": ["a.jpg", "b/", "b/c.txt"], "truncated": False})
    j = json.loads(body)
    assert st == 200 and j["kind"] == "folder" and "names only" in j["note"]
    sent = _send_and_wait(c, llm, f"what is {j['chip']}")
    assert "b/c.txt" in sent and "location is unknown" in sent
    assert c.post("/api/attach/folder", {"name": "x", "entries": "nope"})[0] == 400
    assert c.post("/api/attach/paths", {"paths": []})[0] == 400


def test_the_new_routes_need_the_cookie_and_origin(c):
    assert c.post("/api/attach/paths", {"paths": ["a"]}, cookie=False)[0] == 401
    assert c.post("/api/attach/paths", {"paths": ["a"]}, origin="http://evil.example")[0] in (403, 400)
    assert c.post("/api/attach/folder", {"name": "a", "entries": []}, cookie=False)[0] == 401


def test_typed_at_mentions_that_were_never_attached_are_left_alone(c, env):
    (env.ws / "plain.txt").write_text("PLAIN-CONTENT")
    assert dash_attach.expand("mail me at a@plain.txt or @plain.txt") == "mail me at a@plain.txt or @plain.txt"


# ─── page: the script, with a fake DOM ──────────────────────────────────────

def _fn(name: str) -> str:
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(.*?^}\n", APP_JS, re.S | re.M)
    assert m, name
    return m.group(0)


def _const(name: str) -> str:
    m = re.search(r"^const " + re.escape(name) + r" = .*$", APP_JS, re.M)
    assert m, name
    return m.group(0)


FAKE = r"""
const mk = (tag) => ({ tag, attrs: {}, children: [], listeners: {}, cls: new Set(), parent: null,
  classList: { add(c) { this._e.cls.add(c); }, remove(c) { this._e.cls.delete(c); }, contains(c) { return this._e.cls.has(c); } },
  append(...k) { for (const x of k) { if (x && typeof x === 'object') x.parent = this; this.children.push(x); } },
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); },
  querySelector(sel) { return this.children.find(c => c && c.attrs && c.attrs.class && sel.includes(c.attrs.class)) || null; },
  contains(o) { let n = o; while (n) { if (n === this) return true; n = n.parent; } return false; },
  closest(sel) { let n = this; while (n) { if (n.attrs && n.attrs.class === sel.slice(1)) return n; n = n.parent; } return null; },
  click() { for (const f of this.listeners.click || []) f({}); }, focus() {}, remove() {}, setAttribute(k, v) { this.attrs[k] = v; } });
function h(tag, props, ...kids) { const el = mk(tag); el.classList._e = el; for (const [k, v] of Object.entries(props || {})) { if (v == null || v === false) continue; if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else el.attrs[k] = v; if (k === 'class') String(v).split(' ').forEach(c => el.cls.add(c)); } el.append(...kids.flat(3)); return el; }
const toasts = []; const toast = (m) => toasts.push(m);
const fmtBytes = (n) => n + ' B'; const tok = (n) => String(n);
const doc = { listeners: {}, addEventListener(t, f) { (this.listeners[t] ||= []).push(f); } };
const document = doc; const window = {}; const UI = { tabs: [], active: null };
const navigator = { platform: 'MacIntel' }; const IS_MAC = true; const state = { ws: { workspace: '/w' } };
const location = { hash: '' };
function fire(type, ev) { for (const f of doc.listeners[type] || []) f(ev); }
function dt(types, extra) { const store = {}; return Object.assign({ types, dropEffect: '', effectAllowed: '', setData(k, v) { store[k] = v; this.types = [...this.types, k]; }, getData(k) { return store[k] || ''; }, store }, extra || {}); }
function ev(target, d) { return { target, dataTransfer: d, defaultPrevented: false, relatedTarget: {}, preventDefault() { this.defaultPrevented = true; } }; }
"""


def _run(body: str, *needs: str, consts: tuple[str, ...] = ()) -> dict:
    if not NODE:
        pytest.skip("node is not installed")
    code = FAKE + "\n".join(_const(n) for n in consts) + "\n" + "\n".join(_fn(n) for n in needs) + "\nconst out = {};\n" + body + "\nconsole.log(JSON.stringify(out));\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_page_script_still_parses():
    if not NODE:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "app.js"
        f.write_text(APP_JS, encoding="utf-8")
        assert subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30).returncode == 0


def test_the_drop_overlay_and_old_handlers(c):
    chat_src = APP_JS[APP_JS.index("function buildComposer"):APP_JS.index("// popups: / commands")]
    assert "root.addEventListener('dragover'" not in chat_src and "C.feed.addEventListener('drop'" not in chat_src
    assert "axComposerDrop(C," in chat_src                                           # one controller for the whole chat view
    assert ".chat.dropping .dropveil{display:flex}" in APP_LAYOUT_CSS and "pointer-events:none" in APP_LAYOUT_CSS.split(".dropveil{")[1].split("}")[0]
    assert "Drop to attach" in APP_JS


def test_dragover_is_cancelled_everywhere_for_files_and_the_overlay_shows_over_the_chat():
    r = _run(r"""
axDropInit();
const chat = h('div', { class: 'chat' }); const feed = h('div', { class: 'feed' }); chat.append(feed);
const side = h('div', { class: 'side' });
UI.tabs.push({ C: { addPaths() {}, feed } });
const a = ev(feed, dt(['Files'])); fire('dragover', a);
out.files_over_chat = [a.defaultPrevented, a.dataTransfer.dropEffect, chat.classList.contains('dropping'), chat.children.some(c => c.attrs && c.attrs.class === 'dropveil')];
const b = ev(side, dt(['Files'])); fire('dragover', b);
out.files_elsewhere = [b.defaultPrevented, b.dataTransfer.dropEffect, chat.classList.contains('dropping')];
const t = ev(feed, dt(['text/plain'])); fire('dragover', t); out.text_untouched = t.defaultPrevented;
const tree = ev(feed, dt(['application/x-axon-paths', 'text/plain'])); fire('dragover', tree); out.tree_over_chat = [tree.defaultPrevented, tree.dataTransfer.dropEffect];
const lost = ev(feed, dt(['Files'])); lost.relatedTarget = null; fire('dragleave', lost); out.after_leave = chat.classList.contains('dropping');
const off = ev(side, dt(['Files'])); fire('drop', off); out.drop_elsewhere = [off.defaultPrevented, toasts.length];
""", "axDropInit", consts=("AX_DRAG",))
    assert r["files_over_chat"] == [True, "copy", True, True]
    assert r["files_elsewhere"] == [True, "none", False]          # cancelled so the browser never opens the file
    assert r["text_untouched"] is False
    assert r["tree_over_chat"] == [True, "copy"]
    assert r["after_leave"] is False
    assert r["drop_elsewhere"] == [True, 1]


def test_dropping_on_any_part_of_the_chat_attaches_os_files_and_tree_paths():
    r = _run(r"""
axDropInit();
const chat = h('div', { class: 'chat' }); const feed = h('div', { class: 'feed' }); const composer = h('div', { class: 'composer' }); chat.append(feed); chat.append(composer);
const calls = [];
const C = { addPaths: (p) => calls.push(['paths', p]), uploadFile: (f) => calls.push(['file', f.name]), addFolder: (e) => calls.push(['folder', e.name]), feed };
UI.tabs.push({ C });
const file = { name: 'cat.png', size: 4 };
const d1 = dt(['Files'], { items: [{ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: false }), getAsFile: () => file }, { kind: 'file', webkitGetAsEntry: () => ({ isDirectory: true, name: 'Pics' }), getAsFile: () => ({ name: 'Pics', size: 0 }) }], files: [] });
const e1 = ev(composer, d1); fire('drop', e1);
const d2 = dt([]); d2.setData('application/x-axon-paths', JSON.stringify(['src/a.py', 'docs'])); d2.setData('text/plain', 'src/a.py\ndocs');
const e2 = ev(feed, d2); fire('drop', e2);
const d3 = dt(['Files'], { items: [], files: [{ name: 'old.txt', size: 3 }] }); fire('drop', ev(feed, d3));
out.calls = calls; out.prevented = [e1.defaultPrevented, e2.defaultPrevented];
""", "axDropInit", "axHandleDrop", consts=("AX_DRAG",))
    assert r["calls"] == [["file", "cat.png"], ["folder", "Pics"], ["paths", ["src/a.py", "docs"]], ["file", "old.txt"]]
    assert r["prevented"] == [True, True]


def test_tree_rows_are_draggable_with_a_custom_type_and_plain_text():
    r = _run(r"""
const GIT_LABEL = {}; function treePaint() { out.painted = (out.painted || 0) + 1; }
function togglePanel() {} function treeFlatten() { return TREE.rows; } async function treeLoad() {}
const TREE = { open: new Set(), sel: '', rows: [], multi: new Set(), anchor: '', cache: {} };
const mkRow = (path, type) => ({ depth: 0, e: { path, name: path.split('/').pop(), type } });
TREE.rows = [mkRow('a', 'dir'), mkRow('a/x.py', 'file'), mkRow('a/y.png', 'file'), mkRow('b.md', 'file')];
const nodes = TREE.rows.map(treeNode);
out.draggable = nodes.map(n => n.attrs.draggable);
const drag = (n, path) => { const d = dt([]); n.listeners.dragstart[0]({ dataTransfer: d }); return d; };
let d = drag(nodes[1]); out.single = [d.store['application/x-axon-paths'], d.store['text/plain'], d.effectAllowed];
nodes[1].listeners.click[0]({ metaKey: true });                       // Cmd-click starts a multi-selection
nodes[3].listeners.click[0]({ ctrlKey: true });
out.multi = [...TREE.multi];
d = drag(nodes[3]); out.multi_drag = JSON.parse(d.store['application/x-axon-paths']);
d = drag(nodes[0]); out.unselected_drag = JSON.parse(d.store['application/x-axon-paths']);   // a row outside the selection drags alone
TREE.multi = new Set(); TREE.anchor = 'a/x.py';
nodes[3].listeners.click[0]({ shiftKey: true });                      // Shift-click takes the range
out.range = [...TREE.multi];
d = drag(nodes[2]); out.range_drag = JSON.parse(d.store['application/x-axon-paths']);
""", "treeNode", "treeSelection", "axSetDrag", "axMenu", "axAddToChat", "axChatTab", "axCopy", consts=("AX_DRAG",))
    assert r["draggable"] == ["true"] * 4
    assert r["single"] == ['["a/x.py"]', "a/x.py", "copy"]
    assert r["multi"] == ["a/x.py", "b.md"] and r["multi_drag"] == ["a/x.py", "b.md"] and r["unselected_drag"] == ["a"]
    assert r["range"] == ["a/x.py", "a/y.png", "b.md"] and r["range_drag"] == ["a/x.py", "a/y.png", "b.md"]


def test_rows_that_stay_on_screen_keep_their_element_while_scrolling():
    r = _run(r"""
const GIT_LABEL = {}; const RH = 28;
function treeFlatten() { return TREE.rows; } async function treeLoad() {} function togglePanel() {}
const mkRow = (i) => ({ depth: 0, e: { path: 'f' + i, name: 'f' + i, type: 'file' } });
const win = { children: [], style: {}, firstChild: null,
  insertBefore(n, ref) { const i = this.children.indexOf(n); if (i >= 0) this.children.splice(i, 1); const j = ref ? this.children.indexOf(ref) : this.children.length; this.children.splice(j, 0, n); this.sync(); },
  replaceChildren() { throw new Error('whole-list replace would drop the dragged row'); }, sync() { this.firstChild = this.children[0] || null; this.children.forEach((c, i) => c.nextSibling = this.children[i + 1] || null); } };
const orig = mk('x').remove; const wrapRemove = (n) => { n.remove = () => { const i = win.children.indexOf(n); if (i >= 0) win.children.splice(i, 1); win.sync(); }; return n; };
const rows = Array.from({ length: 200 }, (_, i) => mkRow(i));
const TREE = { open: new Set(), sel: '', rows, multi: new Set(), anchor: '', cache: {}, nodes: new Map(), win, inner: { style: {} }, vp: { isConnected: true, scrollTop: 0, clientHeight: 280 } };
const oldH = h;
treePaint();
const first = [...win.children]; const f12 = win.children.find(n => n.attrs['data-path'] === 'f12');
TREE.vp.scrollTop = 28 * 5; treePaint();
out.same_element = win.children.find(n => n.attrs['data-path'] === 'f12') === f12;
out.window = [win.children[0].attrs['data-path'], win.children.length];
""".replace("const oldH = h; ", ""), "treeNode", "treePaint", "treeSelection", "axSetDrag", "axMenu", "axAddToChat", "axChatTab", "axCopy", consts=("AX_DRAG",))
    assert r["same_element"] is True


def test_chips_show_thumbnail_or_icon_name_size_open_and_remove():
    r = _run(r"""
const removed = [];
const img = axChipEl('[Image #3]', { kind: 'image', name: 'cat.png', size: 2048 }, () => removed.push('img'));
const code = axChipEl('@src/a.py', { kind: 'ref', name: 'a.py', path: 'src/a.py', size: 100 }, () => removed.push('code'));
const dir = axChipEl('[Folder #4: src/]', { kind: 'folder', name: 'src/', entries: 12, more: true, path: 'src' }, () => {});
const text = (e) => JSON.stringify(e, (k, v) => (k === 'parent' || k === 'listeners' || k === 'cls' || k === 'classList') ? undefined : v);
out.img = text(img); out.code = text(code); out.dir = text(dir);
const btns = (e) => e.children.filter(c => c && c.tag === 'button');
btns(code).find(b => b.attrs['aria-label']).listeners.click[0]({}); out.removed = removed;
btns(code).find(b => b.attrs.class === 'copen').listeners.click[0]({}); out.hash = location.hash;
out.dir_open = btns(dir).filter(b => b.attrs.class === 'copen').length;
""", "axChipEl", consts=())
    assert '"src":"/api/attachment/image?n=3"' in r["img"] and "2048 B" in r["img"] and "cat.png" in r["img"]
    assert "Remove a.py" in r["code"] and '"a.py"' in r["code"] and "100 B" in r["code"] and ">" not in r["code"][:0]
    assert "12+ items" in r["dir"]
    assert r["removed"] == ["code"] and r["hash"] == "#/files/src%2Fa.py" and r["dir_open"] == 0


def test_a_pasted_screenshot_becomes_a_named_file_and_empty_drops_say_why():
    r = _run(r"""
globalThis.File = class { constructor(parts, name, o) { this.name = name; this.type = o.type; this.size = 1; } };
const shot = { name: 'image.png', type: 'image/png', size: 9 };
const got = axClipboardFiles({ files: [], items: [{ kind: 'file', getAsFile: () => shot }, { kind: 'string' }] });
out.pasted = got.map(f => [f.name.startsWith('pasted-image-'), f.name.endsWith('.png'), f.type]);
const doc2 = axClipboardFiles({ files: [{ name: 'report.pdf', type: 'application/pdf' }], items: [] }); out.keeps = doc2[0].name;
const C = {}; let uploaded = 0; axComposerDrop(C, { upload: () => { uploaded++; }, chips: h('div'), ta: h('textarea'), addChip() {} });
C.uploadFile({ name: 'Folder', size: 0 }); C.uploadFile({ name: 'ok.txt', size: 5 });
out.uploaded = uploaded; out.toast = toasts[0];
""", "axClipboardFiles", "axComposerDrop", "axReadFolder", consts=())
    assert r["pasted"] == [[True, True, "image/png"]] and r["keeps"] == "report.pdf"
    assert r["uploaded"] == 1 and "nothing was attached" in r["toast"] and "folder" in r["toast"]


def test_reading_a_finder_folder_is_limited_in_depth_and_count():
    if not NODE:
        pytest.skip("node is not installed")
    code = FAKE + "\n".join(_const(n) for n in ("AX_FOLDER_MAX",)) + "\n" + _fn("axReadFolder") + r"""
const file = (name) => ({ isDirectory: false, name });
const dir = (name, kids) => ({ isDirectory: true, name, createReader() { let done = false; return { readEntries(ok) { if (done) ok([]); else { done = true; ok(kids); } } }; } });
const root = dir('root', [file('b.txt'), dir('sub', [file('inner.txt'), dir('l2', [file('never.txt')])]), dir('node_modules', [file('skip.js')]), ...Array.from({ length: 300 }, (_, i) => file('f' + String(i).padStart(3, '0')))]);
axReadFolder(root).then(r => console.log(JSON.stringify({ n: r.names.length, truncated: r.truncated, head: r.names.slice(0, 3), deep: r.names.some(n => n.includes('never')), junk: r.names.some(n => n.includes('node_modules')) })));
"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "t.js"
        f.write_text(code, encoding="utf-8")
        res = subprocess.run([NODE, str(f)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    r = json.loads(res.stdout.strip().splitlines()[-1])
    assert r["n"] == 120 and r["truncated"] is True and r["head"] == ["sub/", "sub/l2/", "sub/inner.txt"]
    assert r["deep"] is False and r["junk"] is False


def test_search_rows_the_viewer_button_the_menu_and_the_keyboard_way():
    files = APP_JS
    assert files.count("axDragProps([") >= 2 and "draggable: 'true'" in files            # names and contents results in the Files panel (mountSearch, the old left search tab, was dead code and is removed)
    assert "onclick: () => axAddToChat([f.path])" in files and "'Add to chat'" in files  # viewer toolbar
    for label in ("'Open'", "'Copy path'", "IS_MAC ? 'Reveal in Finder'"):
        assert label in files
    assert "ev.key === 'a' || ev.key === 'A'" in files and "axAddToChat(treeSelection(e.path))" in files
    assert "contextmenu" in files


def test_adding_to_chat_from_anywhere_uses_the_open_chat_or_opens_one():
    r = _run(r"""
const added = [];
axAddToChat(['x.py']); out.no_chat = [window.AX_PENDING, location.hash];
const t = { hash: '#/chat/s1', C: { addPaths: (p) => added.push(p) } }; UI.tabs.push(t); UI.active = { hash: '#/files/x' };
axAddToChat(['y.py']); out.with_chat = [JSON.parse(JSON.stringify(added)), location.hash];
UI.active = t; location.hash = '#/chat/s1'; axAddToChat(['z.py', '']); out.active = added;
""", "axAddToChat", "axChatTab", consts=())
    assert r["no_chat"] == [["x.py"], "#/chat"]
    assert r["with_chat"] == [[["y.py"]], "#/chat/s1"] and r["active"] == [["y.py"], ["z.py"]]
