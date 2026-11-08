"""Client script, part 3b: views for documents, spreadsheets, decks, notebooks, databases, archives and binaries.

Everything is built from text nodes (never innerHTML); the data comes from /api/file/extract, which is the same
extraction the Read tool uses."""

JS_FORMATS = r"""
// ---------- extracted file formats ----------
const EXT_LIMIT = 300;
function extUrl(f, o) {
  o = o || {};
  return '/api/file/extract?path=' + encodeURIComponent(f.path) + (o.sheet ? '&sheet=' + encodeURIComponent(o.sheet) : '') + (o.pages ? '&pages=' + encodeURIComponent(o.pages) : '') + '&offset=' + (o.offset || 0) + '&limit=' + (o.limit || EXT_LIMIT);
}
function askAxonBtn(f) {
  return h('button', { class: 'btn', type: 'button', title: 'Reference this file in the chat', onclick: () => axAsk(f.path) }, 'Ask Axon');
}
function extNotes(meta) {
  const n = (meta && meta.notes) || [];
  return n.length ? h('div', { class: 'notice', style: 'margin:8px 14px' }, n.join(' ')) : null;
}
function extTable(columns, rows, nums) {
  const t = h('table', { class: 'csv' }, h('thead', null, h('tr', null, h('th', { class: 'rn' }, '#'), ...columns.map(c => h('th', null, String(c))))),
    h('tbody', null, ...rows.map((r, i) => h('tr', null, h('td', { class: 'rn' }, nums && nums[i] != null ? nums[i] : i + 1), ...r.map(c => h('td', { title: String(c).length > 60 ? String(c) : null }, String(c)))))));
  return h('div', { class: 'preview tw csvwrap' }, t);
}
function extPager(state, meta, reload) {
  const prev = h('button', { class: 'btn', type: 'button', disabled: state.hist.length === 0, onclick: () => { state.offset = state.hist.pop(); reload(); } }, 'Previous');
  const hasNext = !!(meta && meta.next);
  const next = h('button', { class: 'btn', type: 'button', disabled: !hasNext, onclick: () => { state.hist.push(state.offset); state.offset = meta.next - 1; reload(); } }, 'Next');
  const label = meta && meta.total ? (meta.unit || 'item') + 's ' + (state.offset + 1) + '+ of ' + meta.total.toLocaleString() : '';
  return h('div', { class: 'ftoolbar' }, prev, next, h('span', { class: 'muted' }, label));
}
function docBlocks(blocks) {
  const box = h('div', { class: 'preview md', style: 'max-width:860px;margin:0 auto;padding:14px 18px' });
  for (const b of blocks || []) {
    if (/^h[1-6]$/.test(b.t)) box.append(h(b.t, null, b.text));
    else if (b.t === 'li') box.append(h('div', { style: 'margin-left:' + (18 + 18 * (b.level || 0)) + 'px' }, '• ' + b.text));
    else if (b.t === 'table') {
      const rows = b.rows || []; if (!rows.length) continue;
      const w = Math.max(...rows.map(r => r.length)); const pad = (r) => r.concat(Array(w - r.length).fill(''));
      box.append(extTable(pad(rows[0]), rows.slice(1).map(pad), null));
      if (b.more) box.append(h('p', { class: 'muted' }, b.more + ' more rows not shown'));
    } else box.append(h('p', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, b.text || ''));
  }
  return box;
}
function slideCards(slides) {
  const box = h('div', { style: 'display:flex;flex-direction:column;gap:14px;padding:14px;max-width:900px;margin:0 auto' });
  for (const s of slides || []) {
    const card = h('div', { class: 'fcard', style: 'max-width:none;margin:0;width:100%' }, h('div', { class: 'muted' }, 'Slide ' + s.n), h('h3', null, s.title || '(untitled)'),
      ...(s.paras || []).map(t => h('div', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, t)), ...(s.tables || []).map(rows => rows.length ? extTable(rows[0], rows.slice(1), null) : null));
    if (s.notes) card.append(h('div', { class: 'notice', style: 'margin-top:10px;white-space:pre-wrap' }, 'Notes: ' + s.notes));
    box.append(card);
  }
  return box;
}
function archiveTree(f, entries, showFile) {
  const root = { kids: new Map(), e: null };
  for (const e of entries) {
    const parts = e.name.split('/').filter(Boolean); let cur = root;
    parts.forEach((p, i) => { if (!cur.kids.has(p)) cur.kids.set(p, { kids: new Map(), e: null }); cur = cur.kids.get(p); if (i === parts.length - 1) cur.e = e; });
  }
  const draw = (node, name, depth) => {
    const e = node.e; const isDir = node.kids.size > 0 || (e && e.dir);
    if (isDir) {
      const d = h('details', { open: depth < 2 }, h('summary', { style: 'cursor:pointer' }, name + '/'));
      const inner = h('div', { style: 'margin-left:18px' });
      for (const [k, v] of [...node.kids].sort((a, b) => (b[1].kids.size > 0) - (a[1].kids.size > 0) || a[0].localeCompare(b[0]))) inner.append(draw(v, k, depth + 1));
      d.append(inner); return d;
    }
    const unsafe = e && e.unsafe; const label = h('span', null, name, h('span', { class: 'muted' }, '  ' + fmtBytes(e ? e.size : 0) + (unsafe ? '  unsafe path, not readable' : '')));
    if (unsafe || !e) return h('div', null, label);
    return h('div', null, h('button', { class: 'btn', type: 'button', style: 'padding:0 6px;margin-right:6px', title: 'Read this file without extracting the archive', onclick: () => showFile(e.name) }, 'Open'), label);
  };
  const box = h('div', { style: 'padding:12px 16px;font:13px/1.6 ui-monospace,Menlo,monospace;overflow:auto' });
  for (const [k, v] of [...root.kids].sort((a, b) => (b[1].kids.size > 0) - (a[1].kids.size > 0) || a[0].localeCompare(b[0]))) box.append(draw(v, k, 0));
  return box;
}
function nbCells(cells) {
  const box = h('div', { style: 'max-width:960px;margin:0 auto;padding:14px' });
  for (const c of cells || []) {
    const head = h('div', { class: 'muted', style: 'font-size:12px;margin-top:12px' }, (c.type === 'code' ? 'In [' + (c.exec == null ? ' ' : c.exec) + ']' : c.type) + '  cell ' + c.n);
    const src = h('pre', { class: 'code', style: 'padding:8px 12px;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:6px;margin:2px 0' }, c.source || '');
    box.append(head, src);
    for (const o of c.outputs || []) {
      if (o.kind === 'image' && /^image\/(png|jpeg)$/.test(o.mime) && /^[A-Za-z0-9+\/=]+$/.test(o.b64 || '')) box.append(h('img', { src: 'data:' + o.mime + ';base64,' + o.b64, alt: o.label || 'output', style: 'max-width:100%;display:block;margin:4px 0' }));
      else box.append(h('pre', { style: 'margin:2px 0 2px 14px;white-space:pre-wrap;overflow-wrap:anywhere;' + (o.kind === 'error' ? 'color:var(--danger,#c33)' : 'color:var(--muted)') }, o.text || ''));
    }
  }
  return box;
}
function extractedView(f) {
  const body = h('div', { class: 'vbody' }); const st = { sheet: '', pages: '', offset: 0, hist: [] }; let member = null;
  async function load() {
    body.replaceChildren(h('p', { class: 'muted', style: 'padding:14px' }, 'Reading ' + f.name + '...'));
    let d; try { d = await api(extUrl(f, { sheet: st.sheet, pages: st.pages, offset: st.offset })); } catch (e) { body.replaceChildren(h('div', { style: 'padding:14px' }, errorBox(e.message))); return; }
    paint(d);
  }
  function switcher(names, current, pick, label) {
    return h('div', { class: 'ftoolbar', role: 'tablist', 'aria-label': label }, ...names.map(n => h('button', { class: 'btn' + (n === current ? ' primary' : ''), type: 'button', role: 'tab', 'aria-selected': n === current ? 'true' : 'false', onclick: () => { pick(n); } }, n)));
  }
  function paint(d) {
    const m = d.meta || {}; const parts = [];
    if (d.error) { body.replaceChildren(h('div', { class: 'preview' }, h('div', { class: 'fcard' }, h('h3', null, f.name), h('p', { class: 'notice warn' }, d.error), h('div', { class: 'acts' }, ...fileActions(f, true))))); return; }
    const k = d.kind;
    if (k === 'xlsx' || k === 'ods' || (k === 'sqlite' && m.current)) {
      const names = m.sheets || [];
      if (names.length > 1 || k === 'sqlite') parts.push(switcher(k === 'sqlite' ? ['Overview', ...names] : names, k === 'sqlite' ? (m.current || 'Overview') : m.current, (n) => { st.sheet = (k === 'sqlite' && n === 'Overview') ? '' : n; st.offset = 0; st.hist = []; load(); }, 'Sheets'));
      const t = (m.tables || [])[0] || { columns: [], rows: [] };
      parts.push(extPager(st, m, load), extTable(t.columns, t.rows, t.row_numbers));
    } else if (k === 'sqlite') {
      parts.push(switcher(['Overview', ...(m.sheets || [])], 'Overview', (n) => { st.sheet = n === 'Overview' ? '' : n; st.offset = 0; st.hist = []; load(); }, 'Tables'));
      for (const t of m.tables || []) {
        parts.push(h('div', { style: 'padding:6px 14px' }, h('h3', { style: 'margin:10px 0 2px' }, t.name + '  ', h('span', { class: 'muted' }, (t.kind || 'table') + ', ' + (t.total == null ? '?' : t.total.toLocaleString()) + ' rows')),
          h('details', null, h('summary', { class: 'muted', style: 'cursor:pointer' }, 'Schema'), h('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, t.schema || '')), t.rows.length ? extTable(t.columns, t.rows, null) : h('p', { class: 'muted' }, 'No rows')));
      }
    } else if (k === 'pptx' || k === 'odp') {
      const total = m.total || 0; const first = m.first || 1; const last = m.last || (m.slides || []).length;
      const nav = h('div', { class: 'ftoolbar' }, h('button', { class: 'btn', type: 'button', disabled: first <= 1, onclick: () => { st.pages = Math.max(1, first - 50) + '-' + (first - 1); load(); } }, 'Previous slides'),
        h('button', { class: 'btn', type: 'button', disabled: !m.next_pages, onclick: () => { st.pages = m.next_pages; load(); } }, 'Next slides'), h('span', { class: 'muted' }, 'Slides ' + first + '-' + last + ' of ' + total));
      parts.push(nav, slideCards(m.slides));
    } else if (k === 'epub') {
      const first = m.first || 1; const last = m.last || first;
      parts.push(h('div', { class: 'ftoolbar' }, h('button', { class: 'btn', type: 'button', disabled: first <= 1, onclick: () => { st.pages = Math.max(1, first - 5) + '-' + (first - 1); load(); } }, 'Previous chapters'),
        h('button', { class: 'btn', type: 'button', disabled: !m.next_pages, onclick: () => { st.pages = m.next_pages; load(); } }, 'Next chapters'), h('span', { class: 'muted' }, 'Chapters ' + first + '-' + last + ' of ' + (m.total || '?'))), docBlocks(m.blocks));
    } else if (m.blocks) {
      if (m.total > EXT_LIMIT || st.offset) parts.push(extPager(st, m, load));
      parts.push(docBlocks(m.blocks));
    } else if (k === 'ipynb' || m.cells) {
      parts.push(nbCells(m.cells));
    } else if (m.entries) {
      const out = h('div', { style: 'border-top:1px solid var(--line)' });
      const show = async (name) => { out.replaceChildren(h('p', { class: 'muted', style: 'padding:10px 16px' }, 'Reading ' + name + '...')); try { const r = await api(extUrl(f, { sheet: name })); out.replaceChildren(h('div', { class: 'ftoolbar' }, h('strong', null, name), h('span', { class: 'muted' }, 'read from inside the archive; nothing is extracted to disk')), r.error ? h('p', { class: 'notice warn', style: 'margin:8px 16px' }, r.error) : h('pre', { style: 'padding:8px 16px;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,Menlo,monospace' }, r.text)); } catch (e) { out.replaceChildren(errorBox(e.message)); } };
      parts.push(h('div', { class: 'notice' + (m.bomb ? ' warn' : ''), style: 'margin:8px 14px' }, (m.notes || []).join(' ')), archiveTree(f, m.entries, show), out);
      body.replaceChildren(...parts); return;
    } else {
      for (const im of d.images || []) if (/^image\/(png|jpeg)$/.test(im.mime) && /^[A-Za-z0-9+\/=]+$/.test(im.b64)) parts.push(h('div', { class: 'imgstage fitmode' }, h('img', { src: 'data:' + im.mime + ';base64,' + im.b64, alt: im.label || f.name })));
      parts.push(h('pre', { style: 'padding:12px 16px;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,Menlo,monospace;margin:0' }, d.text || ''));
    }
    const nt = extNotes(m); if (nt) parts.unshift(nt);
    body.replaceChildren(...parts);
  }
  load();
  return body;
}
// A second view for text files that have a readable form (HTML, XML, YAML, TOML, JSON Lines): text nodes only.
function textFormatView(f, srcEl) {
  const body = h('div', { class: 'vbody' }, srcEl); let on = false; let fmt = null;
  const label = { html: 'Readable text', xml: 'Pretty XML', yaml: 'Checked', toml: 'Checked', jsonl: 'Pretty records' }[f.fmt] || 'Formatted';
  const tg = h('button', { class: 'btn', type: 'button', title: 'Switch between the file as written and a readable form. HTML is never rendered.', onclick: async () => {
    on = !on; tg.textContent = on ? 'Source' : label;
    if (!on) { body.replaceChildren(srcEl); return; }
    if (!fmt) { fmt = h('div', null, h('p', { class: 'muted', style: 'padding:10px 16px' }, 'Reading...')); body.replaceChildren(fmt);
      try { const d = await api(extUrl(f, { limit: 3000 })); const m = d.meta || {};
        const info = [m.title ? 'Title: ' + m.title : '', m.shape ? 'Shape: ' + m.shape : '', m.root ? 'Root element: ' + m.root : '', ...(m.notes || [])].filter(Boolean);
        fmt.replaceChildren(info.length ? h('div', { class: 'notice', style: 'margin:8px 14px;white-space:pre-wrap' }, info.join('\n')) : null, h('pre', { style: 'padding:8px 16px;white-space:pre-wrap;overflow-wrap:anywhere;font:12.5px/1.5 ui-monospace,Menlo,monospace;margin:0' }, d.text || '')); }
      catch (e) { fmt.replaceChildren(errorBox(e.message)); } }
    body.replaceChildren(fmt);
  } }, label);
  return { extra: [tg], body };
}
"""
