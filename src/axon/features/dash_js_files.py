"""Client script, part 3: Files (folder chooser, lazy tree, viewer with syntax colour and git, jailed editor)."""

JS_FILES = r"""
// ---------- files ----------
const KW = {
  python: 'def class return if elif else for while import from as with try except finally raise yield lambda pass break continue in not and or is None True False async await global nonlocal assert del self',
  js: 'function return if else for while const let var class new import from export default async await try catch finally throw typeof instanceof switch case break continue null undefined true false this extends interface type enum implements public private static',
  sh: 'if then else elif fi for while do done case esac function return export local echo cd exit in set unset',
  go: 'func return if else for range package import type struct interface map chan go defer var const switch case break continue nil true false',
  rust: 'fn let mut pub use mod struct enum impl trait match if else for while loop return self Self true false const static async await',
  c: 'int char float double void struct enum typedef return if else for while switch case break continue static const include define NULL class public private namespace template',
  java: 'public private protected class interface extends implements static final void int new return if else for while try catch throw import package this null true false',
  ruby: 'def end class module if elsif else unless while for do return yield require include nil true false self',
  sql: 'select from where insert into update delete create table join on group by order having limit and or not null as values set',
  json: 'true false null', yaml: 'true false null yes no', toml: 'true false',
};
const COM = { python: '#', sh: '#', yaml: '#', toml: '#', ruby: '#', js: '//', go: '//', rust: '//', c: '//', java: '//', sql: '--' };
const hlCache = {};
function hlRegex(lang) {
  if (hlCache[lang]) return hlCache[lang];
  const com = COM[lang] ? '(' + COM[lang].replace(/[/#-]/g, '\\$&') + '.*$)' : '(?!x)x';
  return (hlCache[lang] = new RegExp(com + '|("(?:[^"\\\\]|\\\\.)*"?|\'(?:[^\'\\\\]|\\\\.)*\'?|`(?:[^`\\\\]|\\\\.)*`?)|(\\b\\d[\\d._xa-fA-F]*\\b)|([A-Za-z_$][\\w$]*)', 'g'));
}
function hlLine(text, lang) {
  const frag = document.createDocumentFragment();
  if (lang === 'text' || text.length > 2000) { frag.append(text); return frag; }
  if (lang === 'md') { const cls = /^\s{0,3}#{1,6}\s/.test(text) ? 'tk-k' : /^\s*([-*+]|\d+\.)\s/.test(text) ? 'tk-f' : ''; const s = h('span', { class: cls }); s.textContent = text; frag.append(s); return frag; }
  const kw = new Set((KW[lang] || '').split(' ')); const re = hlRegex(lang); re.lastIndex = 0;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const cls = m[1] ? 'tk-c' : m[2] ? 'tk-s' : m[3] ? 'tk-n' : (kw.has(lang === 'sql' ? m[4].toLowerCase() : m[4]) ? 'tk-k' : (text[m.index + m[0].length] === '(' ? 'tk-f' : ''));
    if (cls) { const s = h('span', { class: cls }); s.textContent = m[0]; frag.append(s); } else frag.append(m[0]);
    last = m.index + m[0].length; if (m[0] === '') re.lastIndex++;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}
const GIT_LABEL = { M: 'modified', A: 'added', D: 'deleted', U: 'untracked', R: 'renamed', C: 'copied' };

// ----- tree (virtual rows: only what is on screen exists in the page) -----
const RH = 28;
const TREE = { cache: {}, trunc: {}, open: new Set(), ignored: false, sel: '', rows: [], vp: null, win: null, inner: null, filter: '', multi: new Set(), anchor: '', nodes: new Map() };
async function treeLoad(path) {
  try { const d = await api('/api/tree?path=' + encodeURIComponent(path) + (TREE.ignored ? '&ignored=1' : '')); TREE.cache[path] = d.entries; TREE.trunc[path] = d.truncated; }
  catch (e) { TREE.cache[path] = []; TREE.trunc[path] = false; toast(e.message); }
}
function treeFlatten() {
  const rows = [];
  const walk = (path, depth) => {
    const es = TREE.cache[path]; if (!es) { rows.push({ loading: true, depth }); return; }
    for (const e of es) { rows.push({ e, depth }); if (e.type === 'dir' && TREE.open.has(e.path)) walk(e.path, depth + 1); }
    if (!es.length) rows.push({ note: depth ? '(empty)' : 'This folder has no visible files.', depth });
    if (TREE.trunc[path]) rows.push({ note: 'Only the first entries are shown. Use search.', depth });
  };
  walk('', 0); return rows;
}
function treePaint() {
  if (!TREE.vp || !TREE.vp.isConnected) return;
  const rows = TREE.rows; const top = TREE.vp.scrollTop, hgt = TREE.vp.clientHeight || 400;
  const start = Math.max(0, Math.floor(top / RH) - 10), end = Math.min(rows.length, Math.ceil((top + hgt) / RH) + 10);
  TREE.inner.style.height = rows.length * RH + 'px'; TREE.win.style.transform = 'translateY(' + start * RH + 'px)';
  // Rows that stay on screen keep their element, so a drag that started on one survives a scroll or a repaint.
  const next = new Map(); const list = [];
  rows.slice(start, end).forEach((r, i) => {
    if (r.loading || r.note) { list.push(h('div', { class: 'node note', style: '--d:' + r.depth }, r.loading ? 'Loading...' : r.note)); return; }
    const e = r.e; const key = [e.path, r.depth, e.type, TREE.open.has(e.path) ? 1 : 0, e.path === TREE.sel ? 1 : 0, TREE.multi.has(e.path) ? 1 : 0, e.git || '', e.ignored ? 1 : 0].join('|');
    const node = TREE.nodes.get(key) || treeNode(r); next.set(key, node); list.push(node);
  });
  TREE.nodes = next;
  const keep = new Set(list);
  for (const c of [...TREE.win.children]) if (!keep.has(c)) c.remove();
  let ref = TREE.win.firstChild;
  for (const n of list) { if (n === ref) ref = ref.nextSibling; else TREE.win.insertBefore(n, ref); }
}
function treeSelection(path) {
  if (TREE.multi.size > 1 && TREE.multi.has(path)) return TREE.rows.filter(r => r.e && TREE.multi.has(r.e.path)).map(r => r.e.path);
  return [path];
}
function treeNode(r) {
  const e = r.e;
  const node = h('button', { class: 'node ' + e.type + (e.ignored ? ' ign' : '') + (e.path === TREE.sel ? ' sel' : '') + (TREE.multi.has(e.path) ? ' msel' : ''), type: 'button', style: '--d:' + r.depth, title: e.path + '  (drag into the chat, right-click for more, A adds it to the chat)', 'data-path': e.path, draggable: 'true' },
    h('span', { class: 'ar' }, e.type === 'dir' ? (TREE.open.has(e.path) ? '▾' : '▸') : ''), h('span', { class: 'nm' }, e.name), h('span', { class: 'g', title: GIT_LABEL[e.git] || '' }, e.git === 'U' ? '?' : e.git || ''));
  node.addEventListener('dragstart', (ev) => axSetDrag(ev, treeSelection(e.path)));
  node.addEventListener('click', async (ev) => {
    if (ev.metaKey || ev.ctrlKey) {
      if (!TREE.multi.size && TREE.sel) TREE.multi.add(TREE.sel);
      if (TREE.multi.has(e.path)) TREE.multi.delete(e.path); else TREE.multi.add(e.path);
      TREE.anchor = e.path; treePaint(); return;
    }
    if (ev.shiftKey && (TREE.anchor || TREE.sel)) {
      const from = TREE.anchor || TREE.sel; const ents = TREE.rows.filter(x => x.e); const i = ents.findIndex(x => x.e.path === from), j = ents.findIndex(x => x.e.path === e.path);
      if (i >= 0 && j >= 0) { TREE.multi = new Set(ents.slice(Math.min(i, j), Math.max(i, j) + 1).map(x => x.e.path)); treePaint(); return; }
    }
    TREE.multi = new Set(); TREE.anchor = e.path;
    if (e.type === 'dir') { if (TREE.open.has(e.path)) TREE.open.delete(e.path); else { TREE.open.add(e.path); if (!TREE.cache[e.path]) { TREE.rows = treeFlatten(); treePaint(); await treeLoad(e.path); } } TREE.rows = treeFlatten(); treePaint(); }
    else { TREE.sel = e.path; location.hash = '#/files/' + encodeURIComponent(e.path); treePaint(); if (innerWidth <= 900) togglePanel('left', false); }
  });
  node.addEventListener('keydown', (ev) => { if ((ev.key === 'a' || ev.key === 'A') && !ev.metaKey && !ev.ctrlKey && !ev.altKey) { ev.preventDefault(); axAddToChat(treeSelection(e.path)); } });
  node.addEventListener('contextmenu', (ev) => {
    ev.preventDefault(); const paths = treeSelection(e.path);
    axMenu(ev, [
      ['Add to chat' + (paths.length > 1 ? ' (' + paths.length + ' items)' : ''), () => axAddToChat(paths)],
      [e.type === 'dir' ? (TREE.open.has(e.path) ? 'Close folder' : 'Open folder') : 'Open', () => node.click()],
      ['Copy path', () => axCopy(((state.ws || {}).workspace || '').replace(/\/$/, '') + '/' + e.path)],
      ['Copy relative path', () => axCopy(e.path)],
      [IS_MAC ? 'Reveal in Finder' : (/Win/i.test(navigator.platform || '') ? 'Show in Explorer' : 'Show in folder'), async () => { try { await api('/api/file/reveal', { path: e.path }); } catch (er) { toast(er.message); } }],
    ]);
  });
  return node;
}
async function treeReload() { TREE.cache = {}; TREE.trunc = {}; await treeLoad(''); for (const p of [...TREE.open]) { if (!TREE.cache[p]) await treeLoad(p); } TREE.rows = treeFlatten(); treePaint(); }
async function mountTree(body) {
  const ws = state.ws || {};
  const head = h('div', { class: 'fhead' }, h('div', { class: 'cur' }, ws.name || 'Project'), h('div', { class: 'full' }, ws.workspace || ''),
    h('div', { class: 'formrow', style: 'margin:0' }, h('button', { class: 'btn', onclick: () => folderChooser() }, 'Change folder'), h('button', { class: 'btn', title: 'Reload the tree', onclick: () => treeReload() }, 'Reload'),
      h('label', { class: 'muted', style: 'font-size:12px;display:flex;gap:4px;align-items:center' }, h('input', { type: 'checkbox', checked: TREE.ignored, onchange: (e) => { TREE.ignored = e.target.checked; treeReload(); } }), 'ignored files')));
  const vp = h('div', { class: 'vlist' }); const inner = h('div', { class: 'vinner' }); const win = h('div', { class: 'vwin' }); inner.append(win); vp.append(inner);
  Object.assign(TREE, { vp, win, inner });
  body.append(head, vp);
  vp.addEventListener('scroll', () => requestAnimationFrame(treePaint));
  new ResizeObserver(() => treePaint()).observe(vp);
  if (!TREE.cache['']) await treeLoad('');
  TREE.rows = treeFlatten(); treePaint();
}
function mountSearch(body) {
  const q = h('input', { type: 'search', class: 'search', placeholder: 'Search file names or text', 'aria-label': 'Search the project' });
  const mode = h('select', { 'aria-label': 'Search in' }, h('option', { value: 'names' }, 'File names'), h('option', { value: 'text' }, 'File contents'));
  const res = h('div', { class: 'sres' });
  const run = async () => {
    const v = q.value.trim(); res.replaceChildren(); if (v.length < (mode.value === 'text' ? 2 : 1)) return;
    res.append(h('p', { class: 'muted', style: 'padding:6px 14px' }, 'Searching...'));
    try {
      if (mode.value === 'names') { const d = await api('/api/files/search?q=' + encodeURIComponent(v)); res.replaceChildren(...d.files.map(f => h('a', Object.assign({ class: 'hit', href: '#/files/' + encodeURIComponent(f), draggable: 'true', title: 'Drag into the chat to attach' }, axDragProps([f])), h('b', null, f.split('/').pop()), h('span', { class: 'muted' }, f))), d.files.length ? '' : h('p', { class: 'muted', style: 'padding:6px 14px' }, 'No file matches.')); }
      else { const d = await api('/api/files/grep?q=' + encodeURIComponent(v)); res.replaceChildren(h('p', { class: 'muted', style: 'padding:4px 14px;font-size:12px' }, d.matches.length + ' matches in ' + d.files + ' files searched' + (d.partial ? ' (stopped early; refine the search)' : '')), ...d.matches.map(m => h('a', Object.assign({ class: 'hit', href: '#/files/' + encodeURIComponent(m.path) + '?line=' + m.line, draggable: 'true', title: 'Drag into the chat to attach' }, axDragProps([m.path])), h('b', null, m.path.split('/').pop() + ':' + m.line), h('span', { class: 'muted' }, m.text)))); }
    } catch (e) { res.replaceChildren(errorBox(e.message)); }
  };
  let t; q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(run, 200); }); mode.addEventListener('change', run);
  body.append(h('div', { class: 'fhead' }, q, h('div', { class: 'formrow', style: 'margin:6px 0 0' }, mode)), res); setTimeout(() => q.focus(), 30);
}

// ----- a file in its own tab -----
const PS = 1000;                       // lines per page; a 100,000-line file is never in the page at once
const FIRST_LOAD = 3000;
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent || '');
const rawUrl = (p, dl) => '/api/file/raw?path=' + encodeURIComponent(p) + (dl ? '&download=1' : '');
function fmtBytes(n) { if (n < 1024) return n + ' bytes'; const u = ['KB', 'MB', 'GB', 'TB']; let i = -1; do { n /= 1024; i++; } while (n >= 1024 && i < 3); return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i]; }
function parseDelim(text, delim, maxRows) {
  const rows = []; let row = [], cur = '', q = false;
  for (let i = 0; i < text.length && rows.length < maxRows; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
    else if (c === '"') q = true; else if (c === delim) { row.push(cur); cur = ''; } else if (c === '\n') { row.push(cur.replace(/\r$/, '')); rows.push(row); row = []; cur = ''; } else cur += c;
  }
  if (cur || row.length) { row.push(cur); rows.push(row); } return rows;
}
const parseCsv = (text, maxRows) => parseDelim(text, ',', maxRows);

// A small clickable preview of a file, for chat attachments and anywhere else a file is mentioned. Click opens the viewer.
function fileThumb(path, opts) {
  opts = opts || {}; const name = opts.name || String(path).split('/').pop() || 'file';
  const ext = (name.split('.').pop() || '').toLowerCase(); const isImg = /^(png|jpe?g|gif|webp|avif|bmp|ico|svg)$/.test(ext);
  const label = () => h('span', { class: 'fext' }, (ext && ext.length < 6 ? ext : 'file').toUpperCase());
  const el = h('button', { class: 'fthumb', type: 'button', title: 'Open ' + name, 'aria-label': 'Open ' + name, onclick: opts.onopen || (() => { location.hash = '#/files/' + encodeURIComponent(path); }) },
    isImg ? h('img', { src: rawUrl(path), alt: '', loading: 'lazy' }) : label(), h('span', { class: 'fname' }, name));
  const im = el.querySelector('img'); if (im) im.addEventListener('error', () => im.replaceWith(label()));
  return el;
}
window.fileThumb = fileThumb;

// Pages of lines: only one page is ever in the document. render(lines, firstLineIndex) draws it.
function paged(f, o) {
  const local = o.lines || null; const psz = o.ps || PS; const total = local ? local.length : f.total_lines; const cache = {}; let page = 0, hasNext = false;
  if (!local && !f.offset && f.text !== undefined) {
    const all = f.lines ? f.text.split('\n') : [];
    for (let i = 0; i * psz < all.length; i++) { const part = all.slice(i * psz, (i + 1) * psz); if (part.length === psz || f.lines < FIRST_LOAD) cache[i] = part; }
  }
  const body = h('div', { class: 'pbody' }); const root = h('div', { class: 'pview' + (o.cls ? ' ' + o.cls : '') });
  const prev = h('button', { class: 'btn', type: 'button', onclick: () => show(page - 1) }, 'Previous');
  const next = h('button', { class: 'btn', type: 'button', onclick: () => show(page + 1) }, 'Next');
  const lab = h('span', { class: 'muted plabel' });
  const go = h('input', { type: 'number', min: '1', class: 'goto', placeholder: 'Go to line', title: 'Type a line number and press Enter to jump to it', 'aria-label': 'Go to line' });
  go.addEventListener('keydown', (e) => { if (e.key === 'Enter' && go.value) { const n = Math.max(1, +go.value | 0); show(Math.floor((n - 1) / psz), n); } });
  const bar = h('div', { class: 'pagebar' }, prev, next, lab, h('span', { style: 'flex:1' }), o.extras || null, o.noGoto ? null : go);
  root.append(bar, body);
  async function fetchPage(p) {
    if (cache[p]) return cache[p];
    if (local) cache[p] = local.slice(p * psz, (p + 1) * psz);
    else { const g = await api('/api/file?path=' + encodeURIComponent(f.path) + '&offset=' + (p * psz) + '&limit=' + psz); cache[p] = g.lines ? g.text.split('\n') : []; }
    return cache[p];
  }
  async function show(p, focus) {
    p = Math.max(0, total != null ? Math.min(p, Math.max(0, Math.ceil(total / psz) - 1)) : p);
    let arr; bar.classList.add('busy');
    try { arr = await fetchPage(p); } catch (e) { toast(e.message); bar.classList.remove('busy'); return; }
    bar.classList.remove('busy');
    if (!arr.length && p > 0) { toast('There are no more lines.'); return; }
    page = p; hasNext = total != null ? (p + 1) * psz < total : arr.length === psz;
    const node = await o.render(arr, p * psz, fetchPage); body.replaceChildren(node);
    prev.disabled = p === 0; next.disabled = !hasNext;
    const a = p * psz + 1, b = p * psz + arr.length;
    lab.textContent = total != null ? (total <= psz ? total.toLocaleString() + ' lines' : 'Lines ' + a.toLocaleString() + ' to ' + b.toLocaleString() + ' of ' + total.toLocaleString()) : 'Lines ' + a.toLocaleString() + ' to ' + b.toLocaleString() + ' (large file, shown in pages)';
    if (total != null && total <= psz) bar.classList.add('single'); else bar.classList.remove('single');
    if (focus) { const t = body.querySelector('[data-ln="' + focus + '"]'); if (t) { t.classList.add('hl'); t.scrollIntoView({ block: 'center' }); } }
  }
  root.show = show; root.start = (line) => show(line ? Math.floor((line - 1) / psz) : 0, line);
  return root;
}
function codeRender(lang, ln) {
  return (arr, from) => {
    const code = h('div', { class: 'code' }); const frag = document.createDocumentFragment();
    arr.forEach((t, i) => { const n = from + i + 1; const row = h('div', { class: 'ln' + (ln === n ? ' hl' : ''), 'data-ln': n }, h('span', { class: 'no' }, n), h('span', { class: 'tx' })); row.lastChild.append(hlLine(t, lang)); frag.append(row); });
    code.append(frag); if (!arr.length) code.append(h('p', { class: 'muted', style: 'padding:14px' }, 'This file is empty.')); return code;
  };
}
function wrapToggle(root) {
  const on = lsGet('axon-wrap') === '1'; if (on) root.classList.add('wrap');
  const b = h('button', { class: 'btn', type: 'button', 'aria-pressed': String(on), title: 'Wrap long lines', onclick: () => { const w = root.classList.toggle('wrap'); b.setAttribute('aria-pressed', String(w)); lsSet('axon-wrap', w ? '1' : '0'); } }, 'Wrap lines');
  return b;
}
function codePaged(f, ln, lines, lang) {
  const root = paged(f, { lines, render: codeRender(lang || f.language, ln), extras: null, cls: 'codeview' });
  root.querySelector('.pagebar').insertBefore(wrapToggle(root), root.querySelector('.goto'));
  root.start(ln); return root;
}
function csvPaged(f, delim) {
  const root = paged(f, { ps: 500, cls: 'csvview', render: async (arr, from, fetchPage) => {
    const rows = parseDelim(arr.join('\n'), delim, 100000); let head = rows[0] || [];
    if (from > 0) { const p0 = await fetchPage(0); head = parseDelim(p0.slice(0, 1).join('\n'), delim, 1)[0] || []; }
    const data = from === 0 ? rows.slice(1) : rows; const n0 = from === 0 ? 1 : from;
    const t = h('table', { class: 'csv' }, h('thead', null, h('tr', null, h('th', { class: 'rn' }, '#'), ...head.map(c => h('th', null, c)))),
      h('tbody', null, ...data.map((r, i) => h('tr', null, h('td', { class: 'rn' }, n0 + i), ...r.map(c => h('td', null, c))))));
    return h('div', { class: 'preview tw csvwrap' }, t);
  } });
  root.start(0); return root;
}
function fileInfoCard(f, why, acts) {
  const rows = [['Name', f.name], ['Size', fmtBytes(f.size) + ' (' + f.size.toLocaleString() + ' bytes)'], ['Type', f.mime || 'unknown'], ['Modified', f.mtime ? new Date(f.mtime * 1000).toLocaleString() : 'unknown'], ['Location', f.abs || f.path]];
  return h('div', { class: 'preview' }, h('div', { class: 'fcard' }, h('h3', null, f.name), why ? h('p', { class: 'notice' }, why) : null,
    h('dl', { class: 'kv' }, ...rows.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])), h('div', { class: 'acts' }, ...(acts || []))));
}
function fileActions(f, primary) {
  const reveal = h('button', { class: 'btn', type: 'button', title: 'Show this file in your file manager', onclick: async () => { try { await api('/api/file/reveal', { path: f.path }); toast(IS_MAC ? 'Shown in Finder' : 'Opened the folder'); } catch (e) { toast(e.message); } } }, IS_MAC ? 'Reveal in Finder' : (/Win/i.test(navigator.platform || '') ? 'Show in Explorer' : 'Show in folder'));
  const dl = h('a', { class: 'btn' + (primary ? ' primary' : ''), title: 'Save a copy of this file to your Downloads folder', href: rawUrl(f.path, true), download: f.name }, 'Download');
  return [dl, reveal];
}
function imageView(f) {
  const img = h('img', { alt: f.name }); const stage = h('div', { class: 'imgstage fitmode' }, img); let fit = true, z = 1;
  const lab = h('span', { class: 'zlab muted' }, 'Fit'); const dims = h('span', { class: 'muted' });
  const apply = () => { stage.classList.toggle('fitmode', fit); img.style.width = fit || !img.naturalWidth ? '' : Math.round(img.naturalWidth * z) + 'px'; lab.textContent = fit ? 'Fit' : Math.round(z * 100) + '%'; };
  const cur = () => (fit && img.naturalWidth ? img.clientWidth / img.naturalWidth : z);
  const zoom = (k) => { z = Math.min(16, Math.max(0.05, cur() * k)); fit = false; apply(); };
  const bar = h('div', { class: 'ftoolbar zoombar' }, h('button', { class: 'btn', type: 'button', onclick: () => { fit = true; apply(); } }, 'Fit to window'), h('button', { class: 'btn', type: 'button', onclick: () => { fit = false; z = 1; apply(); } }, 'Actual size'),
    h('button', { class: 'btn', type: 'button', 'aria-label': 'Zoom out', onclick: () => zoom(1 / 1.25) }, '-'), lab, h('button', { class: 'btn', type: 'button', 'aria-label': 'Zoom in', onclick: () => zoom(1.25) }, '+'), dims,
    /\.svg$/i.test(f.name) ? h('span', { class: 'muted' }, 'Shown as a picture; scripts in an SVG never run.') : null);
  stage.addEventListener('wheel', (e) => { if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1); } }, { passive: false });
  img.addEventListener('load', () => { dims.textContent = img.naturalWidth && img.naturalHeight ? img.naturalWidth + ' x ' + img.naturalHeight + ' px' : ''; apply(); });
  img.addEventListener('error', () => stage.replaceChildren(h('div', { class: 'fcard' }, h('h3', null, 'This image could not be shown'), h('p', { class: 'notice warn' }, 'The file may be damaged, or your browser may not support the ' + (f.name.split('.').pop() || '').toUpperCase() + ' format.'), h('div', { class: 'acts' }, ...fileActions(f, true)))));
  img.src = rawUrl(f.path);
  return h('div', { class: 'vbody' }, bar, stage);
}
function mediaView(f, tag) {
  const el = h(tag, { controls: true, preload: 'metadata', class: 'fmedia', 'aria-label': f.name }); const msg = h('div', { class: 'notice warn', hidden: true });
  const sorry = () => { msg.hidden = false; msg.replaceChildren('Your browser cannot play this ' + (f.name.split('.').pop() || '').toUpperCase() + ' file (' + (f.mime || 'unknown type') + '). Download it and open it in a media player' + (/\.mov$/i.test(f.name) ? '. Chrome only plays some .mov files (those using the H.264 video codec); Safari plays most of them' : '') + '.  ', ...fileActions(f, true)); };
  if (el.canPlayType && el.canPlayType(f.mime) === '') sorry();
  el.addEventListener('error', sorry); el.src = rawUrl(f.path);
  return h('div', { class: 'preview mediabox' }, msg, el, h('p', { class: 'muted' }, f.name + '  ' + fmtBytes(f.size) + '. Seeking works without downloading the whole file.'));
}
function pdfView(f) {
  const fr = h('iframe', { class: 'pdfframe', title: f.name, src: rawUrl(f.path), referrerpolicy: 'no-referrer' });
  return h('div', { class: 'vbody' }, h('div', { class: 'ftoolbar' }, h('span', { class: 'muted' }, 'Shown by your browser\'s own PDF viewer. If it stays blank, open it in a tab.'), h('a', { class: 'btn primary', href: rawUrl(f.path), target: '_blank', rel: 'noopener noreferrer' }, 'Open in a new tab')), fr);
}

async function viewFile(rest) {
  const [rawPath, q] = (rest || '').split('?'); const path = decodeURIComponent(rawPath || '');
  const line = +((q || '').match(/line=(\d+)/) || [0, 0])[1]; const wantDiff = /diff=1/.test(q || '');
  const title = path.split('/').pop() || 'file';
  return shell('files', async (pane, tab) => {
    const F = { cur: null, editing: false, dirty: false };
    const view = h('div', { class: 'fview' }); pane.append(view);
    TREE.sel = path; treePaint();
    const GL = (g) => GIT_LABEL[g] || g;
    function toolbar(f, extra, text) {
      const bar = h('div', { class: 'ftoolbar' }, h('span', { class: 'p', title: f.abs }, f.path), f.git ? h('span', { class: 'gbadge' }, GL(f.git)) : null, h('span', { class: 'muted', title: f.binary ? 'No lines to count: this is not a text file' : '' }, fmtBytes(f.size) + (f.binary || f.kind === 'image' || f.kind === 'video' || f.kind === 'audio' || f.kind === 'pdf' ? '  ' + (f.dimensions ? f.dimensions[0] + ' x ' + f.dimensions[1] + ' px  ' : '') + (f.mime || 'unknown type') : (f.total_lines != null ? '  ' + f.total_lines.toLocaleString() + ' lines' : ''))));
      if (extra) bar.append(...extra);
      if (text && f.redacted) bar.append(h('span', { class: 'notice warn', style: 'flex-basis:100%' }, 'Secrets in this file are hidden and it is read-only. ' + (/^\.env/i.test(f.name) ? 'Download is blocked for credential files too. ' : '') + 'To change it, open it in your own editor.'));
      if (text) {
        if (f.git) bar.append(h('button', { class: 'btn', onclick: () => showDiff(f) }, 'Diff'));
        if (f.editable) bar.append(h('button', { class: 'btn', onclick: () => edit(f) }, 'Edit')); else bar.append(h('span', { class: 'muted', title: f.why_read_only || (f.total_lines == null ? 'Too large to edit here' : '') }, 'read-only'));
        bar.append(h('button', { class: 'btn', title: 'Reference this file in the chat', onclick: () => { lsSet('axon-prefill', '@' + f.path + ' '); location.hash = '#/chat'; } }, 'Ask Axon'));
      }
      if (/\.ipynb$/i.test(f.path)) { const raw = F.asText && F.asText.has(path); bar.append(h('button', { class: 'btn', type: 'button', title: 'Switch between the notebook cells and its JSON source, which you can edit', onclick: () => { F.asText = F.asText || new Set(); if (raw) F.asText.delete(path); else F.asText.add(path); load(); } }, raw ? 'View cells' : 'Edit source')); }
      bar.append(h('button', { class: 'btn', type: 'button', title: 'Attach this file to the chat', onclick: () => axAddToChat([f.path]) }, 'Add to chat'));
      bar.append(...fileActions(f), h('button', { class: 'btn', title: 'Read the file again from disk', onclick: () => load() }, 'Reload'));
      return bar;
    }
    async function load() {
      view.replaceChildren(h('p', { class: 'muted', style: 'padding:14px' }, 'Loading...'));
      let f; try { f = await api('/api/file?path=' + encodeURIComponent(path) + '&limit=' + FIRST_LOAD + (F.asText && F.asText.has(path) ? '&as_text=1' : '')); } catch (e) { view.replaceChildren(h('div', { style: 'padding:14px' }, errorBox(e.message))); return; }
      F.cur = f; draw(f);
    }
    async function draw(f) {
      const kind = f.kind || 'binary'; const ext = (f.name.split('.').pop() || '').toLowerCase();
      if (f.extractable) { view.replaceChildren(toolbar(f, [askAxonBtn(f)]), extractedView(f)); return; }
      if (kind === 'image') { view.replaceChildren(toolbar(f), imageView(f)); return; }
      if (kind === 'video' || kind === 'audio') { view.replaceChildren(toolbar(f), mediaView(f, kind)); return; }
      if (kind === 'pdf') { view.replaceChildren(toolbar(f), pdfView(f)); return; }
      if (f.binary) { view.replaceChildren(toolbar(f), fileInfoCard(f, 'Axon cannot show this kind of file here (' + (ext ? '.' + ext : 'no extension') + '). You can open it with another app.', fileActions(f, true))); return; }
      if (kind === 'markdown') {
        const mdBox = h('div', { class: 'preview md' }); let src = null; let showSrc = false;
        const body = h('div', { class: 'vbody' }, mdBox);
        const tg = h('button', { class: 'btn', title: 'Switch between the formatted view and the plain text of the file', onclick: () => { showSrc = !showSrc; tg.textContent = showSrc ? 'Rendered' : 'Raw'; if (showSrc && !src) src = codePaged(f, line, null, 'md'); body.replaceChildren(showSrc ? src : mdBox); } }, 'Raw');
        view.replaceChildren(toolbar(f, [tg], true), body);
        try { const d = await api('/api/file/preview?path=' + encodeURIComponent(f.path)); mdBox.innerHTML = d.html; decorate(mdBox); } catch (e) { mdBox.replaceChildren(errorBox(e.message)); }
        return;
      }
      if (kind === 'csv') {
        const csv = csvPaged(f, f.delimiter || (ext === 'tsv' ? '\t' : ',')); let src = null; let showSrc = false; const body = h('div', { class: 'vbody' }, csv);
        const tg = h('button', { class: 'btn', title: 'Switch between the formatted view and the plain text of the file', onclick: () => { showSrc = !showSrc; tg.textContent = showSrc ? 'Table' : 'Raw'; if (showSrc && !src) src = codePaged(f, 0, null, 'text'); body.replaceChildren(showSrc ? src : csv); } }, 'Raw');
        view.replaceChildren(toolbar(f, [tg], true), body); return;
      }
      if (kind === 'json' && f.total_lines != null && f.lines === f.total_lines && !f.clipped_lines && f.text.trim()) {
        let pretty = null; try { pretty = JSON.stringify(JSON.parse(f.text), null, 2).split('\n'); } catch (e) { pretty = null; }
        if (pretty) {
          const fake = Object.assign({}, f, { total_lines: pretty.length }); const prettyEl = codePaged(fake, 0, pretty, 'json'); let raw = null; let showRaw = false; const body = h('div', { class: 'vbody' }, prettyEl);
          const tg = h('button', { class: 'btn', title: 'Switch between the formatted view and the plain text of the file', onclick: () => { showRaw = !showRaw; tg.textContent = showRaw ? 'Pretty' : 'Raw'; if (showRaw && !raw) raw = codePaged(f, line, null, 'json'); body.replaceChildren(showRaw ? raw : prettyEl); } }, 'Raw');
          view.replaceChildren(toolbar(f, [h('span', { class: 'muted' }, 'Pretty-printed'), tg], true), body); return;
        }
      }
      const note = f.clipped_lines ? h('div', { class: 'notice warn', style: 'margin:8px 14px' }, 'Some very long lines are cut at 20,000 characters.') : null;
      const tf = f.fmt ? textFormatView(f, codePaged(f, line, null, f.language)) : null;
      view.replaceChildren(toolbar(f, tf ? tf.extra : null, true), note, tf ? tf.body : codePaged(f, line, null, f.language));
    }
    async function showDiff(f) {
      try { const d = await api('/api/git/diff?path=' + encodeURIComponent(f.path)); const box = h('div', { class: 'code', style: 'padding:10px 14px' }); if (!d.diff) box.append(h('p', { class: 'muted' }, d.note || 'No changes against the last commit.')); else box.append(diffEl(d.diff.split('\n'))); if (d.truncated) box.append(h('p', { class: 'muted' }, 'Diff truncated.')); view.replaceChildren(toolbar(f, null, true), h('div', { class: 'ftoolbar' }, h('button', { class: 'btn', onclick: () => draw(f) }, 'Back to the file'), h('span', { class: 'muted' }, 'Changes against the last commit')), box); } catch (e) { toast(e.message); }
    }
    async function edit(f) {
      F.editing = true; F.dirty = false;
      let full = f; if (f.total_lines != null && f.lines < f.total_lines) full = await api('/api/file?path=' + encodeURIComponent(f.path) + '&limit=3000');
      if (!full.editable || full.redacted || full.lines !== full.total_lines || full.clipped_lines) { F.editing = false; toast(full.why_read_only || 'Only part of this file is loaded, so it cannot be edited here.'); return; }
      const ta = h('textarea', { class: 'editor', spellcheck: 'false', 'aria-label': 'Edit ' + f.path }); ta.value = full.text;
      const msg = h('span', { class: 'muted' }, 'Editing. Ctrl/Cmd+S saves.');
      const save = async (force) => {
        try {
          let sha = full.sha; if (force) { const cur = await api('/api/file?path=' + encodeURIComponent(f.path) + '&limit=3000'); sha = cur.sha; }
          const r = await api('/api/file/save', { path: f.path, content: ta.value, sha, lines: full.lines }); full.sha = r.sha; F.dirty = false; msg.textContent = 'Saved ' + new Date().toLocaleTimeString(); toast('Saved ' + f.path); GIT_CACHE.t = 0; api('/api/ws').then(w => { state.ws = w; drawChip(); });
        } catch (e) { if (/changed on disk/.test(e.message)) { msg.replaceChildren(h('span', { class: 'bad' }, 'This file changed on disk since you opened it. '), h('button', { class: 'btn', onclick: () => load() }, 'Reload (discard my edits)'), ' ', h('button', { class: 'btn danger', onclick: () => save(true) }, 'Overwrite anyway')); } else toast(e.message); }
      };
      ta.addEventListener('input', () => { F.dirty = true; msg.textContent = 'Unsaved changes. Ctrl/Cmd+S saves.'; });
      ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(false); } if (e.key === 'Tab') { e.preventDefault(); ta.setRangeText('    ', ta.selectionStart, ta.selectionEnd, 'end'); F.dirty = true; } });
      view.replaceChildren(h('div', { class: 'ftoolbar' }, h('span', { class: 'p' }, f.path), msg, h('button', { class: 'btn primary', onclick: () => save(false) }, 'Save'), h('button', { class: 'btn', onclick: () => { if (!F.dirty || confirm('Discard unsaved changes?')) load(); } }, 'Close editor')), ta);
      ta.focus();
    }
    await load();
    if (wantDiff && F.cur && F.cur.git) showDiff(F.cur);
  }, { wide: true, persist: true, key: 'file:' + path, hash: '#/files/' + encodeURIComponent(path), title });
}

async function folderChooser() {
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Choose a folder' });
  scrim.addEventListener('click', (e) => { if (e.target === scrim) scrim.remove(); });
  const box = h('div', { class: 'modal' }); scrim.append(box); document.body.append(scrim);
  const ws = state.ws || {};
  const locked = !ws.can_switch;
  const pick = async (path) => { try { const r = await api('/api/ws/set', { path }); scrim.remove(); state.ws = await api('/api/ws'); toast('Now working in ' + r.name); for (const t of [...UI.tabs]) closeTab(t); TREE.cache = {}; TREE.open = new Set(); drawChip(); drawLeftTabs(); shellFolderChanged(); location.hash = '#/chat'; route(); } catch (e) { toast(e.message); } };
  async function show(path) {
    let d; try { d = await api('/api/fs/dirs?path=' + encodeURIComponent(path || '')); } catch (e) { box.append(errorBox(e.message)); return; }
    box.replaceChildren(h('h3', null, 'Choose the project folder'),
      locked ? h('p', { class: 'notice warn' }, 'This page is attached to a running terminal session, so its folder cannot change here. Run axon open --workspace DIR (or /open in another project) to look at a different folder.') : null,
      h('p', { class: 'muted' }, 'Current: ', h('b', null, ws.workspace || '')),
      ws.recent && ws.recent.length ? h('div', null, h('div', { class: 'lbl' }, 'Recent projects'), h('div', { class: 'dirpick' }, ...ws.recent.map(r => h('button', { class: 'node', type: 'button', disabled: locked, onclick: () => pick(r.path) }, h('span', { class: 'nm' }, r.name), h('span', { class: 'muted' }, r.path))))) : null,
      h('div', { class: 'lbl' }, 'Browse'), h('div', { class: 'formrow' }, h('code', { style: 'overflow-wrap:anywhere' }, d.path), h('span', { style: 'flex:1' }), h('button', { class: 'btn', onclick: () => show(d.home) }, 'Home')),
      h('div', { class: 'dirpick' }, d.parent ? h('button', { class: 'node', type: 'button', onclick: () => show(d.parent) }, h('span', { class: 'nm' }, '..')) : null,
        ...d.dirs.map(x => h('button', { class: 'node', type: 'button', onclick: () => show(x.path) }, h('span', { class: 'nm' }, x.name), x.git ? h('span', { class: 'gbadge' }, 'git') : null)), !d.dirs.length ? h('div', { class: 'muted', style: 'padding:8px 12px' }, 'No sub-folders.') : null),
      h('p', { class: 'muted', style: 'font-size:12px' }, 'Credential folders such as .ssh and .aws are never listed.'),
      h('div', { class: 'acts' }, h('button', { class: 'btn primary', disabled: locked, onclick: () => pick(d.path) }, 'Use this folder'), h('button', { class: 'btn', onclick: () => scrim.remove() }, 'Cancel')));
  }
  show(ws.workspace || '');
}

// ----- drag and drop: files from the tree, the search results and the operating system go into the chat -----
const AX_DRAG = 'application/x-axon-paths';
const AX_FOLDER_MAX = 120, AX_FOLDER_DEPTH = 1, AX_SKIP = new Set(['.git', 'node_modules', '__pycache__', '.venv', '.DS_Store']);
function axSetDrag(ev, paths) {
  const dt = ev.dataTransfer; if (!dt) return;
  dt.effectAllowed = 'copy'; dt.setData(AX_DRAG, JSON.stringify(paths)); dt.setData('text/plain', paths.join('\n'));
}
function axDragProps(paths) { return { ondragstart: (ev) => axSetDrag(ev, paths) }; }
async function axCopy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied ' + text); return; } catch (e) { /* not allowed here: try the older way */ }
  const t = h('textarea', { style: 'position:fixed;opacity:0', 'aria-hidden': 'true' }); t.value = text; document.body.append(t); t.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch (e) { ok = false; } t.remove();
  toast(ok ? 'Copied ' + text : 'The browser did not allow copying. The path is ' + text);
}
function axMenu(ev, items) {
  document.querySelectorAll('.ctxmenu').forEach(m => m.remove());
  const menu = h('div', { class: 'ctxmenu', role: 'menu' });
  const close = () => { menu.remove(); document.removeEventListener('mousedown', away, true); document.removeEventListener('keydown', key, true); window.removeEventListener('blur', close); };
  const away = (e) => { if (!menu.contains(e.target)) close(); };
  const key = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const bs = [...menu.querySelectorAll('button')]; const i = bs.indexOf(document.activeElement); bs[(i + (e.key === 'ArrowDown' ? 1 : -1) + bs.length) % bs.length].focus(); }
  };
  for (const [label, fn] of items) menu.append(h('button', { type: 'button', role: 'menuitem', onclick: () => { close(); fn(); } }, label));
  document.body.append(menu);
  const x = Math.min(ev.clientX, innerWidth - menu.offsetWidth - 6), y = Math.min(ev.clientY, innerHeight - menu.offsetHeight - 6);
  menu.style.left = Math.max(4, x) + 'px'; menu.style.top = Math.max(4, y) + 'px';
  document.addEventListener('mousedown', away, true); document.addEventListener('keydown', key, true); window.addEventListener('blur', close);
  const first = menu.querySelector('button'); if (first) first.focus();
}
// Put files in the chat's message box. Works from the file tree, a search result, the viewer or the context menu.
function axChatTab() {
  const live = (t) => t && t.C && typeof t.C.addPaths === 'function';
  if (live(UI.active)) return UI.active;
  return UI.tabs.find(live) || null;
}
function axAddToChat(paths) {
  paths = (paths || []).filter(Boolean); if (!paths.length) return;
  const t = axChatTab();
  if (!t) { window.AX_PENDING = paths; location.hash = '#/chat'; return; }
  if (UI.active !== t) location.hash = t.hash || '#/chat';
  t.C.addPaths(paths);
}
function axChipEl(chip, meta, onRemove) {
  const kind = meta.kind || 'file'; const name = meta.name || chip;
  const m = /^\[Image #(\d+)\]$/.exec(chip); const thumb = meta.thumb || (kind === 'image' && m ? '/api/attachment/image?n=' + m[1] : '');
  const icon = h('span', { class: 'cicon' }, kind === 'folder' ? 'DIR' : kind === 'pdf' ? 'PDF' : kind === 'text' ? 'TXT' : ((name.split('.').pop() || 'file').slice(0, 4).toUpperCase()));
  const lead = thumb ? h('img', { class: 'cthumb', src: thumb, alt: '' }) : icon;
  if (thumb) lead.addEventListener('error', () => lead.replaceWith(icon));
  const size = meta.tokens ? '~' + tok(meta.tokens) + ' tokens' : kind === 'folder' ? (meta.entries != null ? meta.entries + (meta.more ? '+' : '') + ' items' : 'folder') : meta.size != null ? fmtBytes(meta.size) : '';
  const open = kind !== 'folder' && (meta.path || thumb) ? h('button', { class: 'copen', type: 'button', title: 'Open ' + name, onclick: () => { if (meta.path) location.hash = '#/files/' + encodeURIComponent(meta.path); else window.open(thumb, '_blank', 'noopener'); } }, 'Open') : null;
  return h('span', { class: 'chip rich ' + (kind === 'image' ? 'img' : kind === 'text' ? 'txt' : ''), title: chip },
    lead, h('span', { class: 'cname' }, h('b', null, name), h('span', null, size)), open,
    h('button', { type: 'button', title: 'Remove', 'aria-label': 'Remove ' + name, onclick: onRemove }, '×'));
}
function axClipboardFiles(cd) {
  const out = [...(cd.files || [])];
  if (!out.length) for (const it of [...(cd.items || [])]) { if (it.kind === 'file') { const f = it.getAsFile(); if (f) out.push(f); } }
  return out.map((f, i) => (/^image\.(png|jpe?g|gif|webp)$/i.test(f.name) || !f.name) ? new File([f], 'pasted-image-' + Date.now() + (i ? '-' + i : '') + '.' + ((f.type || 'image/png').split('/')[1] || 'png').replace('jpeg', 'jpg'), { type: f.type }) : f);
}
async function axReadFolder(root) {
  const names = []; let truncated = false;
  const readAll = (reader) => new Promise((res, rej) => { const out = []; const next = () => reader.readEntries((b) => { if (!b.length) res(out); else { out.push(...b); next(); } }, rej); next(); });
  const walk = async (dir, prefix, depth) => {
    let kids; try { kids = await readAll(dir.createReader()); } catch (e) { return; }
    kids.sort((a, b) => (a.isDirectory === b.isDirectory ? 0 : a.isDirectory ? -1 : 1) || a.name.localeCompare(b.name));
    for (const k of kids) {
      if (AX_SKIP.has(k.name)) continue;
      if (names.length >= AX_FOLDER_MAX) { truncated = true; return; }
      names.push(prefix + k.name + (k.isDirectory ? '/' : ''));
      if (k.isDirectory && depth < AX_FOLDER_DEPTH) await walk(k, prefix + k.name + '/', depth + 1);
    }
  };
  await walk(root, '', 0); return { names, truncated };
}
// Called by the composer: gives chat C its ways of taking files in. addChip puts a chip in the box; upload sends one OS file.
function axComposerDrop(C, o) {
  const note = (msg) => toast(msg);
  C.uploadFile = (file) => {
    if (!file.size) { note((file.name || 'That item') + ' is empty, or it is a folder the browser would not let the page read, so nothing was attached.'); return; }
    return o.upload(file);
  };
  C.addPaths = async (paths) => {
    const tag = h('span', { class: 'chip' }, h('span', { class: 'spin' }), 'adding ' + paths.length + (paths.length === 1 ? ' item' : ' items')); o.chips.append(tag);
    try {
      const r = await api('/api/attach/paths', { session: C.sid, paths });
      const bad = [];
      for (const it of r.items) { if (it.error) bad.push((it.name || it.path) + ': ' + it.error); else if (!(C.chips[it.chip] && o.ta.value.includes(it.chip))) o.addChip(it); }
      if (bad.length) toast('Not attached. ' + bad.join('. ') + '.');
    } catch (e) { toast('Could not attach: ' + e.message); }
    tag.remove(); o.ta.focus();
  };
  C.addFolder = async (entry) => {
    const tag = h('span', { class: 'chip' }, h('span', { class: 'spin' }), 'reading folder ' + entry.name); o.chips.append(tag);
    try {
      const { names, truncated } = await axReadFolder(entry);
      const j = await api('/api/attach/folder', { name: entry.name, entries: names, truncated }); o.addChip(Object.assign({ path: '' }, j));
      toast(j.note);
    } catch (e) { toast('Could not read the folder ' + entry.name + ': ' + e.message); }
    tag.remove();
  };
  if (window.AX_PENDING) { const p = window.AX_PENDING; window.AX_PENDING = null; setTimeout(() => C.addPaths(p), 60); }
}
function axHandleDrop(C, dt) {
  let raw = ''; try { raw = dt.getData(AX_DRAG); } catch (e) { raw = ''; }
  if (raw) { try { const p = JSON.parse(raw); if (Array.isArray(p) && p.length) { C.addPaths(p.filter(x => typeof x === 'string')); return; } } catch (e) { /* not ours */ } }
  const picked = [...(dt.items || [])].filter(i => i.kind === 'file').map(i => ({ entry: i.webkitGetAsEntry ? i.webkitGetAsEntry() : null, file: i.getAsFile() }));
  if (!picked.length) for (const f of [...(dt.files || [])]) picked.push({ entry: null, file: f });
  if (!picked.length) { toast('There was nothing to attach in that drop.'); return; }
  for (const p of picked) {
    if (p.entry && p.entry.isDirectory) C.addFolder(p.entry);
    else if (p.file) C.uploadFile(p.file);
  }
}
function axDropInit() {
  if (window.__axDrop) return; window.__axDrop = true;
  let shown = null, hideT = 0;
  const hide = () => { clearTimeout(hideT); if (shown) { shown.classList.remove('dropping'); shown = null; } };
  const ours = (e) => { const t = [...((e.dataTransfer && e.dataTransfer.types) || [])]; return t.includes('Files') || t.includes(AX_DRAG); };
  const chatAt = (el) => {
    const chat = el && el.closest ? el.closest('.chat') : null; if (!chat) return null;
    const t = UI.tabs.find(x => x.C && typeof x.C.addPaths === 'function' && x.C.feed && chat.contains(x.C.feed)); return t ? { chat, C: t.C } : null;
  };
  const over = (e) => {
    if (!ours(e)) return;
    e.preventDefault();                 // everywhere: otherwise the browser opens a dropped file and leaves the app
    const hit = chatAt(e.target); if (e.dataTransfer) e.dataTransfer.dropEffect = hit ? 'copy' : 'none';
    if (!hit) { hide(); return; }
    if (shown !== hit.chat) { hide(); shown = hit.chat; if (!hit.chat.querySelector(':scope > .dropveil')) hit.chat.append(h('div', { class: 'dropveil', 'aria-hidden': 'true' }, h('b', null, 'Drop to attach'))); hit.chat.classList.add('dropping'); }
    clearTimeout(hideT); hideT = setTimeout(hide, 1200);
  };
  document.addEventListener('dragenter', over); document.addEventListener('dragover', over);
  document.addEventListener('dragleave', (e) => { if (!e.relatedTarget) hide(); });
  document.addEventListener('dragend', hide);
  document.addEventListener('drop', (e) => {
    if (!ours(e)) return;
    e.preventDefault(); hide();
    const hit = chatAt(e.target);
    if (!hit) { toast('Drop files on the chat to attach them.'); return; }
    axHandleDrop(hit.C, e.dataTransfer);
  });
}
axDropInit();
"""

from axon.features.dash_js_formats import JS_FORMATS  # noqa: E402

JS_FILES = JS_FILES + JS_FORMATS
