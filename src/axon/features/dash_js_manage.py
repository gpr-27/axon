"""Client script: the Sessions and Usage pages with their controls, and the rename / export / delete menu on chat tabs
and the Chat page. Server side: dash_manage.py (routes under /api/manage/)."""

JS_MANAGE = r"""
// ---------- controls for Sessions, Usage and chats (dash_manage.py) ----------
const MG = { toasts: null };
function mgDownload(name, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: (mime || 'text/plain') + ';charset=utf-8' }));
  const a = h('a', { href: url, download: name, style: 'display:none' });
  document.body.append(a); a.click();
  setTimeout(() => { a.remove(); URL.revokeObjectURL(url); }, 4000);
}
const mgQs = (o) => Object.entries(o).filter(([, v]) => v !== '' && v != null && v !== false).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v === true ? '1' : v)).join('&');
// A message at the bottom left that stays long enough to use its buttons (Undo). actions: [[label, fn]]
function mgNotice(msg, opts) {
  const o = opts || {};
  if (!MG.toasts || !MG.toasts.isConnected) { MG.toasts = h('div', { class: 'mg-toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(MG.toasts); }
  const t = h('div', { class: 'mg-toast' + (o.error ? ' bad' : '') }, h('span', null, msg));
  const kill = () => { t.remove(); };
  for (const [label, fn] of (o.actions || [])) t.append(h('button', { class: 'btn', type: 'button', onclick: () => { kill(); fn(); } }, label));
  t.append(h('button', { class: 'mini', type: 'button', 'aria-label': 'Dismiss', onclick: kill }, '×'));
  MG.toasts.append(t);
  setTimeout(kill, o.ttl || (o.error ? 12000 : (o.actions ? 12000 : 4500)));
  return t;
}
function mgModal(title, body, buttons, opts) {
  const scrim = h('div', { class: 'scrim mg-scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
  const close = () => { scrim.remove(); document.removeEventListener('keydown', onKey, true); };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onKey, true);
  scrim.addEventListener('mousedown', (e) => { if (e.target === scrim) close(); });
  const box = h('div', { class: 'modal mg-modal' }, h('h3', null, title), body);
  const acts = h('div', { class: 'acts' });
  for (const b of buttons) acts.append(h('button', { class: 'btn ' + (b.cls || ''), type: 'button', onclick: () => b.fn(close, acts) }, b.label));
  box.append(acts); scrim.append(box); document.body.append(scrim);
  const first = box.querySelector('input,select') || acts.querySelector('button:last-child'); if (first) first.focus();
  return { close, box, acts };
}
function mgConfirm(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    let done = false; const fin = (v) => { if (!done) { done = true; resolve(v); } };
    const m = mgModal(title, h('p', null, text), [
      { label: 'Cancel', fn: (close) => { fin(false); close(); } },
      { label: okLabel, cls: danger ? 'danger' : 'primary', fn: (close) => { fin(true); close(); } }]);
    new MutationObserver((_, ob) => { if (!document.body.contains(m.box)) { fin(false); ob.disconnect(); } }).observe(document.body, { childList: true });
  });
}
function mgAsk(title, label, value, okLabel, maxLen) {
  return new Promise((resolve) => {
    let done = false; const fin = (v) => { if (!done) { done = true; resolve(v); } };
    const input = h('input', { type: 'text', value: value || '', maxlength: maxLen || 64, 'aria-label': label, style: 'width:100%' });
    const m = mgModal(title, h('div', { class: 'formrow' }, h('label', null, label), input), [
      { label: 'Cancel', fn: (close) => { fin(null); close(); } },
      { label: okLabel || 'Save', cls: 'primary', fn: (close) => { fin(input.value); close(); } }]);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); fin(input.value); m.close(); } });
    new MutationObserver((_, ob) => { if (!document.body.contains(m.box)) { fin(null); ob.disconnect(); } }).observe(document.body, { childList: true });
    input.select();
  });
}
// A small menu hanging from a button. items: [label, fn, {danger}] or '-' for a divider.
function mgMenu(anchor, items) {
  document.querySelectorAll('.mg-pop').forEach(p => p.remove());
  const r = anchor.getBoundingClientRect();
  const pop = h('div', { class: 'mg-pop', role: 'menu', style: 'top:' + Math.min(innerHeight - 20, r.bottom + 2) + 'px;right:' + Math.max(8, innerWidth - r.right) + 'px' });
  const off = (e) => { if (!pop.contains(e.target)) end(); };
  const esc = (e) => { if (e.key === 'Escape') { end(); anchor.focus(); } };
  function end() { pop.remove(); document.removeEventListener('mousedown', off, true); document.removeEventListener('keydown', esc, true); }
  for (const it of items) {
    if (it === '-') { pop.append(h('hr')); continue; }
    pop.append(h('button', { type: 'button', role: 'menuitem', class: it[2] && it[2].danger ? 'danger' : '', onclick: () => { end(); it[1](); } }, it[0]));
  }
  document.body.append(pop);
  setTimeout(() => { document.addEventListener('mousedown', off, true); document.addEventListener('keydown', esc, true); }, 0);
  const f = pop.querySelector('button'); if (f) f.focus();
  return pop;
}

// ----- acting on chats (shared by the Sessions page, chat tabs and the Chat page) -----
function mgTabSid(t) {
  if (!t || t.route !== 'chat') return '';
  if (t.C && t.C.sid) return t.C.sid;
  const m = /^chat:(.+)$/.exec(t.key || ''); return m && m[1] !== 'new' ? m[1] : '';
}
function mgCloseChatTabs(ids) {
  const set = new Set(ids);
  for (const t of (UI.tabs || []).slice()) { const sid = mgTabSid(t); if (sid && set.has(sid)) closeTab(t); }
}
async function mgRename(sid, current) {
  const v = await mgAsk('Rename chat', 'Name', current, 'Rename', 64);
  if (v == null) return null;
  const title = v.trim();
  if (!title || title === current) return null;
  try { const r = await api('/api/manage/sessions/update', { id: sid, title }); return r.title; }
  catch (e) { mgNotice('Could not rename: ' + e.message, { error: true }); return null; }
}
async function mgPatch(sid, patch) {
  try { await api('/api/manage/sessions/update', Object.assign({ id: sid }, patch)); return true; }
  catch (e) { mgNotice('Could not change that: ' + e.message, { error: true }); return false; }
}
async function mgRestore(items, after) {
  let prog = null;
  try {
    const r = { results: [], restored: [] };
    for (let i = 0; i < items.length; i += 250) {       // a few hundred at a time so a big restore shows how far it got
      if (items.length > 250) { if (prog) prog.remove(); prog = mgNotice('Restoring chats: ' + i.toLocaleString() + ' of ' + items.length.toLocaleString() + '...', { ttl: 600000 }); }
      const rr = await api('/api/manage/trash/restore', { items: items.slice(i, i + 250).map(x => ({ project_key: x.project_key, name: x.name })) });
      r.results.push(...rr.results); r.restored.push(...rr.restored);
    }
    if (prog) prog.remove();
    const bad = r.results.filter(x => !x.ok), part = r.results.filter(x => x.ok && x.partial);
    mgNotice(r.restored.length ? 'Restored ' + r.restored.length + (r.restored.length === 1 ? ' chat.' : ' chats.') + (part.length ? ' Some files could not go back because something new is there; they are still in Recently deleted.' : '') + (bad.length ? ' ' + bad.length + ' could not be restored: ' + bad[0].reason : '') : (bad[0] ? bad[0].reason : 'Nothing was restored.'), { error: !r.restored.length });
    if (after) after(r);
  } catch (e) { if (prog) prog.remove(); mgNotice('Could not restore: ' + e.message, { error: true }); if (after) after({ results: [], restored: [] }); }
}
// Delete chats (to Recently deleted). One chat needs no question because Undo is offered; several ask first and say how many.
async function mgDelete(ids, o) {
  const opts = o || {};
  if (!ids.length) return null;
  if (ids.length > 1) {
    const ok = await mgConfirm('Delete ' + ids.length + ' chats?', 'This moves ' + ids.length + ' chats to Recently deleted. Nothing is erased yet: you can restore them from there, or delete them for good.', 'Delete ' + ids.length + ' chats', true);
    if (!ok) return null;
  }
  let r = { results: [], deleted: [], refused: [] }, prog = null;
  try {
    for (let i = 0; i < ids.length; i += 250) {       // a few hundred at a time so a big delete shows how far it got
      const part = ids.slice(i, i + 250);
      if (ids.length > 250) { if (prog) prog.remove(); prog = mgNotice('Deleting chats: ' + i.toLocaleString() + ' of ' + ids.length.toLocaleString() + '...', { ttl: 600000 }); }
      const rr = await api('/api/manage/sessions/delete', part.length > 1 ? { ids: part, confirm_count: part.length } : { ids: part });
      r.results.push(...rr.results); r.deleted.push(...rr.deleted); r.refused.push(...rr.refused);
    }
  } catch (e) {
    if (prog) prog.remove();
    if (r.deleted.length) mgCloseChatTabs(r.deleted.map(x => x.id));
    mgNotice('Could not finish deleting' + (r.deleted.length ? ' (' + r.deleted.length.toLocaleString() + ' were moved to Recently deleted)' : '') + ': ' + e.message, { error: true });
    if (opts.after) opts.after(r);
    return null;
  }
  if (prog) prog.remove();
  if (r.deleted.length) mgCloseChatTabs(r.deleted.map(x => x.id));
  if (r.refused.length) mgNotice((r.refused.length === 1 ? 'Not deleted. ' : r.refused.length + ' chats were not deleted. ') + r.refused[0].reason + (r.refused.length > 1 ? ' (and others in use or missing)' : ''), { error: true });
  if (r.deleted.length) {
    const what = r.deleted.length === 1 ? 'Deleted "' + (r.deleted[0].title || 'chat').slice(0, 50) + '".' : 'Deleted ' + r.deleted.length + ' chats.';
    mgNotice(what, { actions: [['Undo', () => mgRestore(r.deleted, (rr) => { if (opts.after) opts.after(r, rr); })]] });
  }
  if (opts.after) opts.after(r);
  return r;
}
function mgChatItems(sid, getTitle, hooks) {
  const h2 = hooks || {};
  return [
    ['Rename...', async () => { const t = await mgRename(sid, getTitle()); if (t && h2.renamed) h2.renamed(t); }],
    ['Export or share...', () => shareModal({ kind: 'session', id: sid })],
    '-',
    ['Delete chat', () => mgDelete([sid], { after: h2.deleted }), { danger: true }]];
}
function mgTabMenu(anchor, t) {
  const sid = mgTabSid(t); if (!sid) return;
  mgMenu(anchor, mgChatItems(sid, () => (t.C && t.C.meta && t.C.meta.title) || t.title, {
    renamed: (title) => { if (t.C && t.C.meta) t.C.meta.title = title; setTabTitle(t, title.slice(0, 26)); if (t.C && typeof drawHead === 'function') drawHead(t.C); },
    deleted: (r) => { if (r.deleted && r.deleted.length) mgCloseChatTabs([sid]); } }));
}
if (typeof drawTabs === 'function') {          // a small menu on every chat tab
  const baseDrawTabs = drawTabs;
  drawTabs = function () {
    baseDrawTabs.apply(this, arguments);
    const sc = document.getElementById('tabscroll'); if (!sc) return;
    const els = sc.querySelectorAll(':scope > .tab');
    (UI.tabs || []).forEach((t, i) => {
      const el = els[i]; if (!el || t.route !== 'chat' || !mgTabSid(t) || el.querySelector('.mg-tabmenu')) return;
      const label = el.querySelector('.tl span'); if (label && label.textContent !== t.title) return;
      const close = el.querySelector('.tx');
      const btn = h('button', { class: 'tx mg-tabmenu', type: 'button', 'aria-haspopup': 'menu', 'aria-label': 'Menu for ' + t.title, title: 'Rename, export or delete this chat',
        onclick: (e) => { e.stopPropagation(); mgTabMenu(e.currentTarget, t); } }, '⋯');
      if (close) el.insertBefore(btn, close); else el.append(btn);
    });
  };
}
// The Chat page's own menu is the title in the top bar (wbTitleMenu): rename, export, delete and the rest.

// ---------- Sessions ----------
const SS = Object.assign({ q: '', full: false, sort: 'newest', group: true, starred: false, tag: '', model: '' }, (() => { try { return JSON.parse(lsGet('axon-sessions-ui') || '{}'); } catch (e) { return {}; } })());
SS.sel = new Set();
function mgSaveSess() { lsSet('axon-sessions-ui', JSON.stringify({ sort: SS.sort, group: SS.group, full: SS.full })); }
function mgDateGroup(ts) {
  const n = new Date(), sod = new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime(), t = ts * 1000;
  return t >= sod ? 'Today' : t >= sod - 864e5 ? 'Yesterday' : t >= sod - 6 * 864e5 ? 'This week' : 'Older';
}
async function viewSessions() {
  shell('sessions', async (main) => {
    SS.sel = new Set();
    const sub = h('div', { class: 'sub', id: 'sess-sub' }, 'Loading...');
    const scopeSeg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Which projects' }, [['project', 'This project'], ['all', 'All projects']].map(([v, l]) => h('button', { type: 'button', 'aria-pressed': String(state.sessionScope === v), onclick: () => { state.sessionScope = v; SS.tag = SS.model = ''; viewSessions(); } }, l)));
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Sessions'), sub), h('div', { class: 'controls' }, scopeSeg)));
    const search = h('input', { class: 'search', id: 'search', type: 'search', placeholder: 'Search chats  ( / )', value: SS.q, 'aria-label': 'Search chats' });
    const fullBox = h('input', { type: 'checkbox', id: 'sess-full', checked: SS.full });
    const groupBox = h('input', { type: 'checkbox', id: 'sess-group', checked: SS.group });
    const sortSel = h('select', { 'aria-label': 'Sort by' }, [['newest', 'Newest first'], ['oldest', 'Oldest first'], ['cost', 'Most expensive'], ['length', 'Longest'], ['name', 'Name A to Z']].map(([v, l]) => h('option', { value: v, selected: SS.sort === v }, l)));
    const tagSel = h('select', { 'aria-label': 'Tag' }), modelSel = h('select', { 'aria-label': 'Model' });
    const starBtn = h('button', { class: 'btn', type: 'button', 'aria-pressed': String(SS.starred) }, 'Starred only');
    main.append(h('div', { class: 'filters' }, search, sortSel, starBtn, tagSel, modelSel));
    main.append(h('div', { class: 'filters mg-opts' },
      h('label', { class: 'mg-check', title: 'Reads the text of every chat, so it is slower with many chats' }, fullBox, ' Also search inside chats'),
      h('label', { class: 'mg-check' }, groupBox, ' Group by date'), h('span', { class: 'muted', id: 'sess-note' })));
    const selbar = h('div', { class: 'mg-selbar', hidden: true });
    const allBox = h('input', { type: 'checkbox', 'aria-label': 'Select all shown' });
    const list = h('div', { class: 'rows mg-rows' });
    const more = h('div', { class: 'mg-more' });
    main.append(selbar, h('div', { class: 'mg-head' }, allBox, h('span', { class: 'muted' }, 'Select')), list, more);
    const trashBox = h('details', { class: 'mg-trash panel' });
    main.append(trashBox);

    let rows = [], total = 0, gen = 0, lastData = null, loading = false;
    const params = (extra) => Object.assign({ scope: state.sessionScope, q: SS.q.trim(), full: SS.full && SS.q.trim() ? 1 : '', sort: SS.sort, starred: SS.starred ? 1 : '', tag: SS.tag, model: SS.model, limit: 100 }, extra || {});
    async function load(reset, fresh) {
      const my = ++gen; loading = true;
      if (reset) { rows = []; }
      let d;
      try { d = await api('/api/manage/sessions?' + mgQs(params({ offset: reset ? 0 : rows.length, fresh: fresh ? 1 : '' }))); }
      catch (e) { if (my === gen) { list.replaceChildren(errorBox(e.message)); } loading = false; return; }
      if (my !== gen) return;
      loading = false; lastData = d; total = d.total;
      rows = reset ? d.rows : rows.concat(d.rows);
      facets(d); draw();
    }
    function facets(d) {
      const fill = (sel, label, items, cur) => { sel.replaceChildren(h('option', { value: '' }, label), items.map(([n, c]) => h('option', { value: n, selected: n === cur }, n + ' (' + c + ')'))); };
      fill(tagSel, 'Any tag', d.tags, SS.tag); fill(modelSel, 'Any model', d.models, SS.model);
      tagSel.hidden = !d.tags.length; modelSel.hidden = d.models.length < 2 && !SS.model;
    }
    function selbarDraw() {
      selbar.hidden = !SS.sel.size; selbar.replaceChildren();
      allBox.checked = rows.length > 0 && rows.every(r => SS.sel.has(r.id));
      allBox.indeterminate = !allBox.checked && rows.some(r => SS.sel.has(r.id));
      if (!SS.sel.size) return;
      selbar.append(h('b', null, SS.sel.size + ' selected'),
        SS.sel.size < total ? h('button', { class: 'btn', type: 'button', onclick: async () => { try { const d = await api('/api/manage/sessions?' + mgQs(params({ ids: 1, limit: 1 }))); d.ids.forEach(i => SS.sel.add(i)); draw(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Select all ' + total.toLocaleString() + ' matching') : null,
        h('button', { class: 'btn', type: 'button', onclick: async () => { for (const id of [...SS.sel]) await mgPatch(id, { starred: true }); mgNotice('Starred ' + SS.sel.size + '.'); load(true, true); } }, 'Star'),
        h('button', { class: 'btn', type: 'button', onclick: async () => { const v = await mgAsk('Tag ' + SS.sel.size + ' chats', 'Tag (empty removes it)', '', 'Apply', 32); if (v == null) return; for (const id of [...SS.sel]) await mgPatch(id, { tag: v.trim() }); load(true, true); } }, 'Tag...'),
        h('button', { class: 'btn danger', type: 'button', onclick: () => mgDelete([...SS.sel], { after: () => { SS.sel.clear(); load(true, true); loadTrash(); } }) }, 'Delete ' + SS.sel.size),
        h('button', { class: 'btn', type: 'button', onclick: () => { SS.sel.clear(); draw(); } }, 'Clear selection'));
    }
    function rowEl(s) {
      const star = h('button', { class: 'mg-star' + (s.starred ? '' : ' off'), type: 'button', 'aria-pressed': String(!!s.starred), title: s.starred ? 'Starred. Click to unstar.' : 'Click to star', 'aria-label': (s.starred ? 'Unstar ' : 'Star ') + s.title,
        onclick: async () => { const want = !s.starred; if (await mgPatch(s.id, { starred: want })) { s.starred = want; draw(); } } }, s.starred ? '★' : '☆');
      const check = h('input', { type: 'checkbox', 'aria-label': 'Select ' + s.title, checked: SS.sel.has(s.id), onchange: (e) => { if (e.target.checked) SS.sel.add(s.id); else SS.sel.delete(s.id); selbarDraw(); row.classList.toggle('sel', e.target.checked); } });
      const menu = h('button', { class: 'mini mg-more-btn', type: 'button', 'aria-haspopup': 'menu', 'aria-label': 'Actions for ' + s.title, onclick: (e) => mgMenu(e.currentTarget, [
        ['Open', () => { location.hash = '#/chat/' + encodeURIComponent(s.id); }],
        ['Rename...', async () => { const t = await mgRename(s.id, s.title); if (t) { s.title = t; draw(); } }],
        [s.starred ? 'Remove star' : 'Star', async () => { if (await mgPatch(s.id, { starred: !s.starred })) { s.starred = !s.starred; draw(); } }],
        ['Set tag...', async () => { const v = await mgAsk('Tag', 'Tag (empty removes it)', s.tags[0] || '', 'Save', 32); if (v != null && await mgPatch(s.id, { tag: v.trim() })) { s.tags = v.trim() ? [v.trim().replace(/^#/, '')] : []; draw(); } }],
        '-', ['Export or share...', () => shareModal({ kind: 'session', id: s.id })],
        '-', ['Delete', () => mgDelete([s.id], { after: () => { SS.sel.delete(s.id); load(true, true); loadTrash(); } }), { danger: true }]]) }, '⋯');
      const row = h('div', { class: 'mg-row' + (SS.sel.has(s.id) ? ' sel' : ''), 'data-id': s.id },
        check, star,
        h('a', { class: 'mg-main', href: '#/chat/' + encodeURIComponent(s.id), title: 'Open this chat' },
          h('div', { class: 'ti' }, s.title, s.live ? h('span', { class: 'live' }, 'live') : null),
          h('div', { class: 'meta' }, state.sessionScope === 'all' ? s.project + ' · ' : '', s.tags.map(t => h('span', { class: 'tag' }, t)), s.snippet ? h('span', { class: 'snip' }, s.snippet) : null)),
        h('span', { class: 'muted hide-s mg-model' }, s.model), h('span', { class: 'num hide-s' }, s.messages + ' msg'), h('span', { class: 'num' }, usd(s.cost)), h('span', { class: 'num hide-s', title: new Date(s.updated * 1000).toLocaleString() }, age(s.updated)), menu);
      return row;
    }
    function draw() {
      const d = lastData;
      sub.textContent = d ? total.toLocaleString() + (total === d.scope_total ? '' : ' of ' + d.scope_total.toLocaleString()) + (d.scope_total === 1 ? ' chat' : ' chats') + (state.sessionScope === 'all' ? ' across all projects' : ' in this project') + (d.cost ? ' · ' + usd(d.cost) : '') : '';
      const note = document.getElementById('sess-note');
      const capNote = d && d.cut_off ? 'Only the newest ' + d.cut_off.limit.toLocaleString() + ' chats of ' + d.cut_off.projects.join(', ') + ' are listed here; older ones are still on disk and are not shown. ' : '';
      if (note) note.textContent = capNote + (d && d.full ? (d.full.truncated ? 'Showing the first matches inside chats; narrow the search to see the rest.' : 'Searched ' + d.full.files.toLocaleString() + ' chats in ' + (d.full.ms / 1000).toFixed(1) + 's') + (d.full.encrypted ? ' · ' + d.full.encrypted + ' encrypted chats cannot be searched' : '') : '');
      list.replaceChildren(); more.replaceChildren();
      if (!rows.length) {
        list.append(h('div', { class: 'empty' }, h('strong', null, d && d.scope_total ? 'Nothing matches' : 'No chats yet'), d && d.scope_total ? 'Clear the search or filters to see everything.' : 'Start Axon in this project and your chats will show up here.'));
        selbarDraw(); return;
      }
      const grouped = SS.group && (SS.sort === 'newest' || SS.sort === 'oldest');
      const frag = document.createDocumentFragment(); let cur = '';
      for (const s of rows) {
        if (grouped) { const g = mgDateGroup(s.updated); if (g !== cur) { cur = g; frag.append(h('div', { class: 'mg-group', role: 'heading', 'aria-level': '2' }, g)); } }
        frag.append(rowEl(s));
      }
      list.append(frag);
      if (rows.length < total) more.append(h('button', { class: 'btn', type: 'button', onclick: () => load(false) }, 'Show ' + Math.min(100, total - rows.length) + ' more'), h('span', { class: 'muted' }, ' ' + rows.length.toLocaleString() + ' of ' + total.toLocaleString() + ' shown'));
      selbarDraw();
    }
    let timer = null;
    const later = (ms) => { clearTimeout(timer); timer = setTimeout(() => load(true), ms); };
    search.addEventListener('input', () => { SS.q = search.value; later(SS.full ? 600 : 200); });
    fullBox.addEventListener('change', () => { SS.full = fullBox.checked; mgSaveSess(); if (SS.q.trim()) load(true); });
    groupBox.addEventListener('change', () => { SS.group = groupBox.checked; mgSaveSess(); draw(); });
    sortSel.addEventListener('change', () => { SS.sort = sortSel.value; mgSaveSess(); load(true); });
    tagSel.addEventListener('change', () => { SS.tag = tagSel.value; load(true); });
    modelSel.addEventListener('change', () => { SS.model = modelSel.value; load(true); });
    starBtn.addEventListener('click', () => { SS.starred = !SS.starred; starBtn.setAttribute('aria-pressed', String(SS.starred)); load(true); });
    allBox.addEventListener('change', () => { if (allBox.checked) rows.forEach(r => SS.sel.add(r.id)); else rows.forEach(r => SS.sel.delete(r.id)); draw(); });
    onLeave(() => clearTimeout(timer));

    // ----- Recently deleted -----
    let trash = null;
    const trashSum = h('summary', null, 'Recently deleted');
    trashBox.append(trashSum);
    const trashBody = h('div', { class: 'mg-trashbody' }); trashBox.append(trashBody);
    async function loadTrash() {
      try { trash = await api('/api/manage/trash?scope=' + state.sessionScope); } catch (e) { trashBody.replaceChildren(errorBox(e.message)); return; }
      trashSum.textContent = 'Recently deleted (' + trash.total + ')';
      trashBody.replaceChildren();
      if (!trash.items.length) { trashBody.append(h('p', { class: 'muted' }, 'Nothing here. Deleted chats wait here until you delete them for good, so you can bring them back.')); return; }
      trashBody.append(h('p', { class: 'muted' }, 'Deleted chats stay here until you delete them for good.'),
        h('div', { class: 'acts' }, h('button', { class: 'btn', type: 'button', onclick: () => mgRestore(trash.items, () => { load(true, true); loadTrash(); }) }, 'Restore all ' + trash.items.length),
          h('button', { class: 'btn danger', type: 'button', onclick: () => forever(trash.items) }, 'Delete all forever')));
      for (const t of trash.items) trashBody.append(h('div', { class: 'mg-trow' },
        h('span', { class: 'mg-ttl' }, h('div', { class: 'ti' }, t.title || t.id), h('div', { class: 'meta muted' }, (state.sessionScope === 'all' && t.project ? t.project + ' · ' : '') + 'deleted ' + age(t.deleted_at) + ' · ' + t.files + ' items')),
        h('button', { class: 'btn', type: 'button', onclick: () => mgRestore([t], () => { load(true, true); loadTrash(); }) }, 'Restore'),
        h('button', { class: 'btn danger', type: 'button', onclick: () => forever([t]) }, 'Delete forever')));
    }
    async function forever(items) {
      const many = items.length > 1;
      const ok = await mgConfirm(many ? 'Delete ' + items.length + ' chats forever?' : 'Delete this chat forever?',
        (many ? 'These ' + items.length + ' chats' : '"' + (items[0].title || items[0].id).slice(0, 60) + '"') + ' will be erased from this computer. This cannot be undone.', many ? 'Delete ' + items.length + ' forever' : 'Delete forever', true);
      if (!ok) return;
      try { const r = await api('/api/manage/trash/delete', { items: items.map(x => ({ project_key: x.project_key, name: x.name })), confirm: true }); mgNotice('Deleted ' + r.removed + (r.removed === 1 ? ' chat' : ' chats') + ' for good.'); }
      catch (e) { mgNotice('Could not delete: ' + e.message, { error: true }); }
      loadTrash();
    }
    trashBox.addEventListener('toggle', () => { if (trashBox.open && !trash) loadTrash(); });
    await load(true);
    loadTrash();
  });
}

// ---------- Memory notes: every scope, add, edit, delete, search (dash_manage.py) ----------
const MEMSC = [['', 'All'], ['global', 'Global'], ['project', 'This project (private)'], ['repo', 'This repo (shared)']];
const MEM = { q: '', scope: '' };
function mgMemoryPanel(host) {
  const list = h('div', { class: 'mg-memlist' });
  const search = h('input', { class: 'search', type: 'search', placeholder: 'Search memories', 'aria-label': 'Search memories', value: MEM.q });
  const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Memory scope' });
  const addText = h('textarea', { rows: 3, placeholder: 'Write what Axon should remember, in your own words', 'aria-label': 'New memory', style: 'width:100%' });
  const addScope = h('select', { 'aria-label': 'Where to keep it' }, [['project', 'This project (private)'], ['repo', 'This repo (shared if you commit .axon)'], ['global', 'Global (every project)']].map(([v, l]) => h('option', { value: v }, l)));
  const note = h('div', { class: 'muted', 'aria-live': 'polite' });
  let gen = 0, counts = {};
  const drawSeg = () => seg.replaceChildren(...MEMSC.map(([v, l]) => h('button', { type: 'button', 'aria-pressed': String(MEM.scope === v), onclick: () => { MEM.scope = v; drawSeg(); load(); } },
    l + (v ? ' (' + (counts[v] || 0) + ')' : ''))));
  async function load() {
    const my = ++gen; let d;
    try { d = await api('/api/manage/memory?' + mgQs({ q: MEM.q.trim(), scope: MEM.scope })); } catch (e) { list.replaceChildren(errorBox(e.message)); return; }
    if (my !== gen) return;
    if (!MEM.q.trim() && !MEM.scope) counts = d.counts; else if (!Object.keys(counts).length) counts = d.counts;
    drawSeg();
    list.replaceChildren();
    if (!d.memories.length) { list.append(h('p', { class: 'muted' }, (MEM.q.trim() || MEM.scope) ? 'No memories match.' : 'No saved memories yet. Add one above, or type /memory add in a chat.')); return; }
    list.append(h('p', { class: 'muted' }, d.total + (d.total === 1 ? ' memory' : ' memories') + (d.total > d.memories.length ? ', showing ' + d.memories.length : '')));
    for (const m of d.memories) list.append(memRow(m));
  }
  function memRow(m) {
    const out = h('pre', { class: 'out' }, m.body + (m.cut ? '\n... (shortened here)' : ''));
    const box = h('details', { class: 'tool mg-mem', 'data-id': m.scope + ':' + m.id },
      h('summary', null, h('span', { class: 'tn' }, m.name), h('span', { class: 'ta' }, m.description === m.name ? '' : m.description), h('span', { class: 'ts' }, m.scope + ' · ' + m.type + (m.updated ? ' · ' + m.updated : ''))), out,
      h('div', { class: 'muted', title: 'Where the file is' }, m.path));
    const acts = h('div', { class: 'acts' });
    const edit = h('button', { class: 'btn', type: 'button', onclick: () => {
      if (m.cut) { mgNotice('That memory is too long to edit here. Open the file instead.', { error: true }); return; }
      const ta = h('textarea', { rows: 8, style: 'width:100%', 'aria-label': 'Edit ' + m.name }); ta.value = m.body;
      const save = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
        try { const r = await api('/api/manage/memory/update', { scope: m.scope, id: m.id, body: ta.value }); mgNotice(r.action === 'unchanged' ? 'Nothing changed.' : 'Saved. The old version is kept.'); load(); }
        catch (e) { mgNotice(e.message, { error: true }); } } }, 'Save');
      const cancel = h('button', { class: 'btn', type: 'button', onclick: () => { ta.replaceWith(out); acts.replaceChildren(edit, del); } }, 'Cancel');
      out.replaceWith(ta); acts.replaceChildren(save, cancel); ta.focus();
    } }, 'Edit');
    const del = h('button', { class: 'btn danger', type: 'button', onclick: async () => {
      if (!await mgConfirm('Delete this memory?', '"' + m.name.slice(0, 80) + '" will no longer be given to Axon. A copy is kept, and /memory restore brings it back.', 'Delete', true)) return;
      try { await api('/api/manage/memory/delete', { scope: m.scope, id: m.id }); mgNotice('Deleted "' + m.name.slice(0, 50) + '".'); load(); }
      catch (e) { mgNotice(e.message, { error: true }); } } }, 'Delete');
    acts.append(edit, del); box.append(acts);
    return box;
  }
  let timer = null;
  search.addEventListener('input', () => { MEM.q = search.value; clearTimeout(timer); timer = setTimeout(load, 200); });
  onLeave(() => clearTimeout(timer));
  const add = async () => {
    if (!addText.value.trim()) { note.textContent = 'Write something first.'; return; }
    try {
      const r = await api('/api/manage/memory/save', { scope: addScope.value, body: addText.value });
      addText.value = ''; note.textContent = (r.action === 'unchanged' ? 'Already saved.' : 'Saved.') + (r.notes && r.notes.length ? ' ' + r.notes[0] : '');
      counts = {}; load();
    } catch (e) { note.textContent = e.message; }
  };
  host.append(h('h2', { style: 'margin-top:22px' }, 'Memories'),
    h('p', { class: 'muted' }, 'Notes Axon keeps and is given at the start of every chat. Global applies to every project; project is private to this one; repo is kept in the project folder so a team can share it. Secrets are removed before saving.'),
    h('div', { class: 'mg-memadd' }, addText, h('div', { class: 'formrow' }, addScope, h('button', { class: 'btn primary', type: 'button', onclick: add }, 'Remember'))), note,
    h('div', { class: 'filters' }, search, seg), list);
  load();
}

// ---------- Usage ----------
const UCH = (() => { try { return JSON.parse(lsGet('axon-usage-charts') || '{}'); } catch (e) { return {}; } })();   // chart name -> true when hidden
const USORT = {};                                                                                                   // table id -> { i, dir }
function usagePeriod() { return state.period === 'custom' ? ((state.from || '') + '..' + (state.to || state.from || '')) : state.period; }
function mgSortable(id, cols, rows, o) {
  const opts = o || {};
  const wrap = h('div', { class: 'mg-sortable' });
  let expanded = false;
  function draw() {
    const srt = USORT[id] || (USORT[id] = { i: opts.sort || 0, dir: opts.dir || -1 });
    const col = cols[srt.i] || cols[0];
    const val = col.get || (() => 0);
    const sorted = rows.slice().sort((a, b) => { const x = val(a), y = val(b); const c = typeof x === 'string' || typeof y === 'string' ? String(x).localeCompare(String(y)) : (x - y); return c * srt.dir; });
    const lim = opts.limit || 20, shown = expanded ? sorted : sorted.slice(0, lim);
    wrap.replaceChildren();
    if (!rows.length) { wrap.append(h('p', { class: 'muted' }, opts.empty || 'Nothing here.')); return; }
    wrap.append(h('div', { class: 'tw' }, h('table', null,
      h('thead', null, h('tr', null, cols.map((c, i) => h('th', { class: c.cls || '', 'aria-sort': i === srt.i ? (srt.dir > 0 ? 'ascending' : 'descending') : 'none' },
        h('button', { class: 'mg-th', type: 'button', title: 'Sort by ' + c.label.toLowerCase(), onclick: () => { const s2 = USORT[id]; if (s2.i === i) s2.dir = -s2.dir; else { s2.i = i; s2.dir = c.cls === 'r' ? -1 : 1; } draw(); } }, c.label, i === srt.i ? (srt.dir > 0 ? ' ▲' : ' ▼') : ''))))),
      h('tbody', null, shown.map(r => h('tr', null, cols.map(c => h('td', { class: c.cls || '' }, c.fmt ? c.fmt(r) : r[c.key]))))))));
    if (sorted.length > lim) wrap.append(h('button', { class: 'btn', type: 'button', onclick: () => { expanded = !expanded; draw(); } }, expanded ? 'Show fewer' : 'Show all ' + sorted.length));
  }
  draw(); return wrap;
}
async function mgCsv(table) {
  try { const r = await api('/api/manage/usage/csv?' + mgQs({ table, period: usagePeriod(), scope: state.scope })); mgDownload(r.filename, r.content, r.mime); }
  catch (e) { mgNotice('Could not make the file: ' + e.message, { error: true }); }
}
async function viewOverview() {
  shell('usage', async (main) => {
    if (!state.scope) state.scope = 'project';
    const head = h('div', { class: 'head' });
    main.append(head);
    const body = h('div'); main.append(body);
    const pname = (state.ws && state.ws.name) || 'this project';
    const redo = () => viewOverview();
    const seg = (opts, cur, set, label) => h('div', { class: 'seg', role: 'group', 'aria-label': label }, opts.map(([v, l]) => h('button', { type: 'button', 'aria-pressed': String(cur === v), onclick: () => { set(v); redo(); } }, l)));
    body.append(h('p', { class: 'muted' }, 'Loading usage...'));
    let d;
    if (state.period === 'custom' && !state.from) { state.period = '30d'; }
    try { d = await api('/api/manage/usage?' + mgQs({ period: usagePeriod(), scope: state.scope })); } catch (e) { body.replaceChildren(errorBox(e.message)); return; }
    const today = new Date().toISOString().slice(0, 10);
    const fromIn = h('input', { type: 'date', 'aria-label': 'From date', value: state.from || '', max: today });
    const toIn = h('input', { type: 'date', 'aria-label': 'To date', value: state.to || '', max: today });
    const custom = h('div', { class: 'mg-custom', hidden: state.period !== 'custom' }, h('label', null, 'From ', fromIn), h('label', null, ' to ', toIn),
      h('button', { class: 'btn primary', type: 'button', onclick: () => { if (!fromIn.value) { mgNotice('Pick a start date.', { error: true }); return; } state.from = fromIn.value; state.to = toIn.value || fromIn.value; state.period = 'custom'; redo(); } }, 'Apply'));
    const csvBtn = h('button', { class: 'btn', type: 'button', 'aria-haspopup': 'menu', onclick: (e) => mgMenu(e.currentTarget, [
      ['Every call (one row each)', () => mgCsv('calls')], ['By day', () => mgCsv('days')], ['By model', () => mgCsv('models')], ['By source', () => mgCsv('sources')],
      ['By tool', () => mgCsv('tools')], ['By session', () => mgCsv('sessions')], state.scope === 'all' ? ['By project', () => mgCsv('projects')] : null, ['Most expensive turns', () => mgCsv('turns')]].filter(Boolean)) }, 'Download CSV');
    const anyHidden = () => ['cost', 'donut', 'tokens'].every(k => UCH[k]);
    head.append(h('div', null, h('h1', null, 'Usage'), h('div', { class: 'sub' }, d.period_label + ' in ' + (state.scope === 'all' ? 'all projects' : pname))),
      h('div', { class: 'controls' },
        seg([['project', 'This project'], ['all', 'All projects']], state.scope, v => state.scope = v, 'Which projects'),
        h('div', { class: 'seg', role: 'group', 'aria-label': 'Period' }, [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All time'], ['custom', 'Custom']].map(([v, l]) => h('button', { type: 'button', 'aria-pressed': String(state.period === v), onclick: () => { if (v === 'custom') { state.period = 'custom'; custom.hidden = false; if (!state.from) { fromIn.focus(); } else redo(); } else { state.period = v; redo(); } } }, l))),
        csvBtn, h('button', { class: 'btn', type: 'button', onclick: () => shareModal({ kind: 'overview', period: usagePeriod(), scope: state.scope }) }, 'Share')));
    main.insertBefore(custom, body);
    body.replaceChildren();
    const k = d.kpis;
    if (d.empty) {
      body.append(h('div', { class: 'empty' }, h('strong', null, 'No usage in this range yet'), 'Axon logs every model call it makes. Run a prompt in the terminal, or widen the range to all time or all projects.'));
    } else {
      const cell = (l, v, hint, extra) => h('div', { class: 'kpi' }, h('div', { class: 'l' }, l), h('div', { class: 'v' }, v), extra || null, h('div', { class: 'h', title: hint }, hint));
      const costDays = d.by_day.map(r => ({ name: r.name, cost: r.cost || 0, calls: r.calls }));
      body.append(h('div', { class: 'u-hero' }, h('div', { class: 'u-fig' }, h('div', { class: 'u-num' }, k.cost_text), h('div', { class: 'u-cap' }, 'Spent, ', d.period_label.toLowerCase()), h('div', { class: 'u-cap dim' }, usd(k.per_day) + ' a day')),
        h('div', { class: 'u-spark' }, UCH.cost ? null : vzColumns(costDays, [{ key: 'cost', label: 'Cost', color: 'var(--primary)' }], { label: 'Cost per day', fmtY: vzMoney, fmtV: usd, note: r => axPlural(r.calls || 0, 'call') }))));
      body.append(h('div', { class: 'strip' },
        cell('Tokens', tok(k.tokens_in + k.tokens_out), tok(k.tokens_in) + ' in, ' + tok(k.tokens_out) + ' out'),
        cell('Cache hit rate', pct(k.hit_pct), tok(k.cache_read) + ' read, ' + tok(k.cache_write) + ' written', vzMeter((k.hit_pct || 0) / 100, '', 'Cache hit rate')),
        cell('Cache savings', k.savings_text, 'against the same calls uncached'),
        cell('Calls', k.calls.toLocaleString(), axPlural(k.sessions, 'session')),
        cell('Model time / total', k.has_latency ? dur(k.api_s) + ' / ' + dur(k.wall_s) : 'no data', k.has_latency ? 'avg ' + dur(k.avg_latency) + ', p95 ' + dur(k.p95_latency) : 'older logs lack timings')));
    }
    // ----- session budget -----
    const budget = h('section', { class: 'panel mg-budget' }, h('h2', null, 'Session budget'), h('p', { class: 'muted' }, 'Loading...'));
    const budgets = h('div', { class: 'u-budgets' }, budget, typeof cfProjectBudgetSection === 'function' ? cfProjectBudgetSection() : null);
    body.append(budgets);
    async function drawBudget() {
      let b; try { b = await api('/api/manage/budget'); } catch (e) { budget.replaceChildren(h('h2', null, 'Session budget'), errorBox(e.message)); return; }
      budget.replaceChildren(h('h2', null, 'Session budget'), h('p', { class: 'muted' }, 'A budget stops new turns in one chat once its spending reaches the limit. It is the same as typing /budget in that chat.'));
      if (!b.budgets.length) { budget.append(h('p', null, 'No chat is open on this page. ', h('a', { href: '#/chat' }, 'Open a chat'), ' and its budget shows up here.')); return; }
      for (const r of b.budgets) {
        const input = h('input', { type: 'text', inputmode: 'decimal', placeholder: 'e.g. 5', 'aria-label': 'Budget in dollars for ' + r.title, size: 8, value: r.has_limit ? String(r.limit) : '' });
        const act = async (amount) => { try { await api('/api/manage/budget', { session: r.session, amount }); mgNotice(amount === 'off' ? 'Budget removed.' : 'Budget set to ' + usd(Number(amount)) + '.'); drawBudget(); } catch (e) { mgNotice(e.message, { error: true }); } };
        const frac = r.has_limit ? Math.min(1, r.spent / r.limit) : 0;
        budget.append(h('div', { class: 'mg-brow' },
          h('div', { class: 'mg-bt' }, h('b', null, r.title), h('span', { class: 'muted' }, r.live ? ' (terminal session)' : '')),
          h('div', { class: 'mg-bn' }, 'Spent ' + usd(r.spent) + ' · ' + (r.has_limit ? 'limit ' + usd(r.limit) + ', ' + usd(r.remaining) + ' left' : 'no limit set'),
            r.has_limit ? h('div', { class: 'gauge' + (frac >= 1 ? ' bad' : frac > 0.75 ? ' warn' : ''), style: 'margin-left:10px' }, h('i', { style: 'width:' + Math.round(frac * 100) + '%' })) : null),
          h('div', { class: 'mg-bc' }, h('span', null, '$'), input,
            h('button', { class: 'btn primary', type: 'button', onclick: () => { if (!input.value.trim()) { mgNotice('Type an amount in dollars.', { error: true }); return; } act(input.value.trim().replace(/^\$/, '')); } }, 'Set limit'),
            r.has_limit ? h('button', { class: 'btn', type: 'button', onclick: () => act('off') }, 'Remove limit') : null)));
      }
    }
    drawBudget();
    if (d.empty) { body.append(usageClearPanel()); return; }
    // ----- charts, each can be hidden; the choice is kept in this browser -----
    const chartBox = (key, title, inner) => {
      const box = h('section', { class: 'chart', 'data-chart': key });
      const render = () => {
        box.replaceChildren(h('h2', null, title, h('button', { class: 'mini mg-chartbtn', type: 'button', 'aria-expanded': String(!UCH[key]), onclick: () => { UCH[key] = !UCH[key]; lsSet('axon-usage-charts', JSON.stringify(UCH)); render(); syncAll(); } }, UCH[key] ? 'Show' : 'Hide')));
        if (UCH[key]) box.append(h('p', { class: 'muted' }, 'Chart hidden.')); else box.append(inner());
      };
      render(); return box;
    };
    const allBtn = h('button', { class: 'btn', type: 'button', onclick: () => { const hide = !anyHidden(); ['cost', 'donut', 'tokens'].forEach(x => { UCH[x] = hide; }); lsSet('axon-usage-charts', JSON.stringify(UCH)); redo(); } }, anyHidden() ? 'Show charts' : 'Hide charts');
    const syncAll = () => { allBtn.textContent = anyHidden() ? 'Show charts' : 'Hide charts'; };
    const modelBars = () => vzRanked(d.charts.donut_parts.map(p => ({ name: p.name, value: p.cost, text: usd(p.cost) + '  ' + p.pct.toFixed(0) + '%' })));
    const TOKEN_SERIES = [{ key: 'input', label: 'Input', color: 'var(--s1)' }, { key: 'output', label: 'Output', color: 'var(--s2)' }, { key: 'cache_read', label: 'Cache read', color: 'var(--s3)' }, { key: 'cache_write', label: 'Cache written', color: 'var(--s4)' }];
    const tokenChart = () => h('div', null, h('ul', { class: 'vz-legend' }, TOKEN_SERIES.map(s => h('li', null, h('i', { style: 'background:' + s.color }), h('span', null, s.label)))), vzColumns(d.by_day, TOKEN_SERIES, { label: 'Tokens per day', fmtY: tok, fmtV: tok }));
    body.append(h('div', { class: 'mg-chartbar' }, h('span', { class: 'muted' }, 'Charts are drawn from the numbers below, so hiding them removes nothing.'), allBtn, h('button', { class: 'mini mg-chartbtn', type: 'button', 'aria-expanded': String(!UCH.cost), onclick: () => { UCH.cost = !UCH.cost; lsSet('axon-usage-charts', JSON.stringify(UCH)); redo(); } }, UCH.cost ? 'Show cost chart' : 'Hide cost chart')));
    body.append(h('div', { class: 'cols' },
      chartBox('tokens', 'Tokens per day', tokenChart),
      chartBox('donut', 'Cost by model', modelBars)));
    body.append(budgets);   // the charts first, then the limits they inform
    // ----- tables: click a column to sort -----
    const section = (title, table, node) => h('section', { class: 'panel' }, h('div', { class: 'mg-sech' }, h('h2', null, title), table ? h('button', { class: 'mini', type: 'button', title: 'Download this table as CSV', onclick: () => mgCsv(table) }, 'CSV') : null), node);
    const aggCols = (first) => [{ label: first, cls: 't', get: r => r.name, fmt: r => r.name }, { label: 'Calls', cls: 'r', get: r => r.calls, fmt: r => r.calls.toLocaleString() }, { label: 'Input', cls: 'r', get: r => r.input, fmt: r => tok(r.input) },
      { label: 'Output', cls: 'r', get: r => r.output, fmt: r => tok(r.output) }, { label: 'Cache read', cls: 'r', get: r => r.cache_read, fmt: r => tok(r.cache_read) }, { label: 'Cache write', cls: 'r', get: r => r.cache_write, fmt: r => tok(r.cache_write) }, { label: 'Cost', cls: 'r', get: r => r.cost, fmt: r => usd(r.cost) }];
    body.append(section('By day', 'days', mgSortable('days', aggCols('Day'), d.by_day.filter(r => r.calls), { sort: 0, dir: -1, limit: 14 })));
    if (state.scope === 'all') body.append(section('By project', 'projects', mgSortable('projects', aggCols('Project'), d.by_project, { sort: 6, dir: -1, limit: 15 })));
    body.append(section('By model', 'models', mgSortable('models', aggCols('Model'), d.by_model, { sort: 6, dir: -1 })));
    body.append(section('By source', 'sources', mgSortable('sources', aggCols('Source'), d.by_source, { sort: 6, dir: -1 })));
    body.append(section('Tools', 'tools', mgSortable('tools', [{ label: 'Tool', cls: 't', get: r => r.name, fmt: r => r.name }, { label: 'Calls', cls: 'r', get: r => r.calls, fmt: r => r.calls }, { label: 'Time', cls: 'r', get: r => r.total_s, fmt: r => dur(r.total_s) },
      { label: 'Errors', cls: 'r', get: r => r.errors, fmt: r => r.errors ? h('span', { class: 'bad' }, r.errors) : 0 }], d.tools, { sort: 1, dir: -1, empty: 'No traced tool calls.' })));
    const scols = [{ label: 'Session', cls: 't', get: r => r.title, fmt: r => h('a', { href: '#/chat/' + encodeURIComponent(r.id), title: 'Open this chat' }, r.title) }];
    if (state.scope === 'all') scols.push({ label: 'Project', get: r => r.project, fmt: r => r.project });
    scols.push({ label: 'Calls', cls: 'r', get: r => r.calls, fmt: r => r.calls.toLocaleString() }, { label: 'Turns', cls: 'r', get: r => r.turns, fmt: r => r.turns }, { label: 'Tokens', cls: 'r', get: r => r.tokens, fmt: r => tok(r.tokens) },
      { label: 'Cost', cls: 'r', get: r => r.cost, fmt: r => usd(r.cost) }, { label: 'Last used', cls: 'r', get: r => r.last, fmt: r => r.last ? age(r.last) : '' });
    body.append(section('By session (' + d.sessions_total.toLocaleString() + ')', 'sessions', mgSortable('sessions', scols, d.by_session, { sort: scols.length - 2, dir: -1, empty: 'No sessions in this range.', limit: 15 })));
    body.append(section('Most expensive turns', 'turns', mgSortable('turns', [{ label: 'Prompt', cls: 't', get: r => r.prompt, fmt: r => r.prompt }, { label: 'Tokens', cls: 'r', get: r => r.tokens, fmt: r => tok(r.tokens) }, { label: 'Cost', cls: 'r', get: r => r.cost, fmt: r => usd(r.cost) }], d.turns, { sort: 2, dir: -1, empty: 'No traced turns.', limit: 10 })));
    body.append(usageClearPanel());
  });
}
function usageClearPanel() {
  const run = async (scope) => {
    let p; try { p = await api('/api/manage/usage/clear?scope=' + scope); } catch (e) { mgNotice(e.message, { error: true }); return; }
    const phrase = h('input', { type: 'text', 'aria-label': 'Type ' + p.phrase + ' to confirm', placeholder: p.phrase, style: 'width:100%' });
    const what = scope === 'all' ? 'every project' : 'the project "' + p.project + '"';
    const body = h('div', null,
      h('p', null, p.records ? 'This removes ' + p.records.toLocaleString() + ' usage records for ' + what + (p.first ? ' (' + String(p.first).slice(0, 10) + (p.last ? ' to ' + String(p.last).slice(0, 10) : '') + ')' : '') + ' from the usage log.' : 'There are no usage records for ' + what + '.'),
      h('p', { class: 'muted' }, 'First, a copy of the whole log is saved in ' + p.backup_dir + '. To undo, copy that file back over usage.jsonl in the Axon folder. Your chats are not touched, and tool and turn tables still come from the chats themselves.'),
      p.phrase ? h('div', { class: 'formrow' }, h('label', null, 'Type "' + p.phrase + '" to confirm'), phrase) : null);
    mgModal('Clear usage history', body, [
      { label: 'Cancel', fn: (close) => close() },
      { label: scope === 'all' ? 'Clear usage for all projects' : 'Clear usage for this project', cls: 'danger', fn: async (close, acts) => {
        if (!p.records) { close(); return; }
        try { const r = await api('/api/manage/usage/clear', { scope, confirm: true, phrase: phrase.value }); close(); mgNotice('Cleared ' + r.removed.toLocaleString() + ' usage records. Backup saved to ' + r.backup, { ttl: 20000 }); viewOverview(); }
        catch (e) { acts.parentNode.append(errorBox(e.message)); } } }]);
  };
  return h('section', { class: 'panel mg-clear' }, h('h2', null, 'Usage history'),
    h('p', { class: 'muted' }, 'The usage log is the record behind everything above. You can clear it for this project or for all projects. A backup copy is kept first.'),
    h('div', { class: 'acts' }, h('button', { class: 'btn danger', type: 'button', onclick: () => run('project') }, 'Clear usage history for this project...'),
      h('button', { class: 'btn danger', type: 'button', onclick: () => run('all') }, 'Clear usage history for all projects...')));
}
"""
