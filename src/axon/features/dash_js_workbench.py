"""Client script: the workbench. Side panels (Terminal, Files, Changes, Browser and a few more) that stack beside the chat,
the top-right toolbar, the chat title menu, the composer footer, and the Progress panel sections (Environment, Context,
Usage, Plan, Connectors, Changes). Text from the server is only ever put in with text nodes (h children), never innerHTML.
"""

JS_WORKBENCH = r"""
// ---------- workbench: side panels, toolbar, menus ----------
const WB_ICON = {
  progress: 'M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01', terminal: 'M4 5h16v14H4zM8 10l3 2-3 2M13 14h4', changes: 'M5 4h14v16H5zM9 10h6M12 7v6M9 16h6',
  browser: 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3.2 3.6 3.2 14.4 0 18M12 3c-3.2 3.6-3.2 14.4 0 18', files: 'M4 6h6l2 2h8v10H4z', more: 'M5 12h.01M12 12h.01M19 12h.01',
  left: 'M4 5h16v14H4zM9 5v14', max: 'M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7', min: 'M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7', close: 'M6 6l12 12M18 6L6 18',
  plus: 'M12 5v14M5 12h14', chev: 'M9 6l6 6-6 6', chevd: 'M6 9l6 6 6-6', computer: 'M4 5h16v11H4zM9 20h6M12 16v4', folder: 'M4 6h6l2 2h8v10H4z', branch: 'M7 4v12M7 16a2 2 0 100 4 2 2 0 000-4zM17 4a2 2 0 100 4 2 2 0 000-4zM17 8c0 5-10 3-10 8',
  search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4', external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6', reload: 'M20 5v5h-5M4 19v-5h5M5.6 9A8 8 0 0119 10M18.4 15A8 8 0 015 14', mic: 'M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM6 11a6 6 0 0012 0M12 17v4', collapse: 'M6 9l6-5 6 5M6 15l6 5 6-5', list: 'M4 6h16M4 12h16M4 18h16',
  tree: 'M4 5h6M7 5v14M7 12h6M7 19h6M13 12h7M13 19h7', pop: 'M4 6h16v12H4zM13 10h5v5h-5z',
};
const wbIco = (n) => svgIcon(WB_ICON[n] || '');
const WB = { narrow: [], wide: [], wN: 380, wW: 600, h: {}, max: null, nodes: {}, el: null, cols: null, built: false, ctx: null, ctxT: 0, ctxSid: '', conn: null, connT: 0, lim: null, limT: 0 };
const WB_DEF = {
  terminal: { title: 'Terminal', kind: 'narrow', icon: 'terminal', mount: (P) => wbTerminal(P) },
  files: { title: 'Files', kind: 'narrow', icon: 'files', mount: (P) => wbFiles(P) },
  changes: { title: 'Changes', kind: 'wide', icon: 'changes', mount: (P) => wbChanges(P) },
  browser: { title: 'Browser', kind: 'wide', icon: 'browser', mount: (P) => wbBrowser(P) },
  tasks: { title: 'Tasks', kind: 'narrow', icon: 'list', mount: (P) => wbTasks(P) },
  plan: { title: 'Plan and todos', kind: 'narrow', icon: 'list', mount: (P) => wbPlan(P) },
  memory: { title: 'Memory', kind: 'narrow', icon: 'list', mount: (P) => wbMemory(P) },
  hooks: { title: 'Hooks', kind: 'narrow', icon: 'list', mount: (P) => wbHooks(P) },
};
function wbKey() { return 'axon-wb:v1:' + ((state.ws || {}).workspace || ''); }
function wbSave() { try { lsSet(wbKey(), JSON.stringify({ narrow: WB.narrow, wide: WB.wide, wN: WB.wN, wW: WB.wW, h: WB.h })); } catch (e) { /* storage blocked */ } }
function wbLoad() {
  let p = {}; try { p = JSON.parse(lsGet(wbKey()) || '{}'); } catch (e) { p = {}; }
  const ok = (a) => (Array.isArray(a) ? a.filter(id => WB_DEF[id]) : []);
  if (typeof p.wN === 'number') WB.wN = Math.max(240, Math.min(720, p.wN)); if (typeof p.wW === 'number') WB.wW = Math.max(320, Math.min(1100, p.wW));
  if (p.h && typeof p.h === 'object') for (const k of Object.keys(p.h)) if (WB_DEF[k] && typeof p.h[k] === 'number') WB.h[k] = Math.max(0.2, Math.min(6, p.h[k]));
  if (innerWidth > 900) { WB.narrow = ok(p.narrow); WB.wide = ok(p.wide); }   // first run: the chat stands alone with Progress; Files is one click or Ctrl+\\ away
}
function wbOpenIds() { return [...WB.narrow, ...WB.wide]; }
function wbWidth() { const L = rightLayout(); return L.wn + L.ww; }
function wbRefreshFiles() { const n = WB.nodes.files; if (n && n.P && n.P.refresh) n.P.refresh(); }
function wbIsOpen(id) { return WB.narrow.includes(id) || WB.wide.includes(id); }
function wbOpen(id) {
  const d = WB_DEF[id]; if (!d) return; if (wbIsOpen(id)) { wbRender(); return; }
  const col = d.kind === 'wide' ? WB.wide : WB.narrow; col.push(id); wbRender(); wbSave(); if (innerWidth <= 900) document.body.classList.add('drawer-wb');
  wbKeepChatRoom(id);
}
// The chat is the point of the page. Opening a panel folds Progress, then the oldest other panel,
// until the chat has room again; each stays one click away on its button.
const WB_CHAT_MIN = 560;
function wbKeepChatRoom(keep) {
  if (innerWidth <= 900 || WB.max) return;
  const center = document.getElementById('center'); if (!center || !center.offsetParent) return;
  for (let i = 0; i < 8 && center.getBoundingClientRect().width < WB_CHAT_MIN; i++) {
    if (UI.rightOpen) { togglePanel('right', false); continue; }
    const other = wbOpenIds().find(x => x !== keep); if (!other) return;
    wbClose(other);
  }
}
function wbClose(id) {
  WB.narrow = WB.narrow.filter(x => x !== id); WB.wide = WB.wide.filter(x => x !== id); if (WB.max === id) WB.max = null;
  const n = WB.nodes[id]; if (n && n.P && n.P.onClose) { try { n.P.onClose(); } catch (e) { /* gone */ } }
  wbRender(); wbSave(); if (!wbOpenIds().length) document.body.classList.remove('drawer-wb');
}
function wbToggle(id) { if (wbIsOpen(id)) wbClose(id); else wbOpen(id); }
function wbCloseAll() { for (const id of wbOpenIds()) wbClose(id); }
function wbMax(id) { WB.max = WB.max === id ? null : id; wbRender(); }
function wbNode(id) {
  if (WB.nodes[id]) return WB.nodes[id];
  const d = WB_DEF[id];
  const P = { id, def: d, body: h('div', { class: 'wp-body' }), titleEl: h('div', { class: 'wp-title' }, d.title), tools: h('div', { class: 'wp-tools' }), onClose: null };
  P.setHead = (title, tools) => { P.titleEl.replaceChildren(...(title == null ? [d.title] : [].concat(title))); P.tools.replaceChildren(...(tools || [])); };
  const maxBtn = h('button', { class: 'wpb', type: 'button', title: 'Expand this panel', 'aria-label': 'Expand ' + d.title, onclick: () => wbMax(id) }, wbIco('max'));
  const closeBtn = h('button', { class: 'wpb', type: 'button', title: 'Close', 'aria-label': 'Close ' + d.title, onclick: () => wbClose(id) }, wbIco('close'));
  P.maxBtn = maxBtn;
  const head = h('div', { class: 'wp-head' }, P.titleEl, h('span', { style: 'flex:1' }), P.tools, maxBtn, closeBtn);
  P.el = h('section', { class: 'wp wp-' + id, 'data-panel': id, 'aria-label': d.title }, head, P.body);
  WB.nodes[id] = { P, el: P.el, mounted: false };
  return WB.nodes[id];
}
function wbRender() {
  const root = WB.el; if (!root) return;
  const ids = wbOpenIds();
  root.classList.toggle('has', ids.length > 0); root.classList.toggle('has-narrow', WB.narrow.length > 0); root.classList.toggle('has-wide', WB.wide.length > 0);
  const body = document.getElementById('ide-body'); if (body) body.classList.toggle('wbmax', !!(WB.max && ids.includes(WB.max)));
  for (const [kind, colEl, list] of [['narrow', WB.cols.narrow, WB.narrow], ['wide', WB.cols.wide, WB.wide]]) {
    const kids = [];
    list.forEach((id, i) => {
      const n = wbNode(id); const P = n.P;
      n.el.style.flex = (WB.h[id] || 1) + ' 1 0'; n.el.classList.toggle('max', WB.max === id); n.el.hidden = !!(WB.max && WB.max !== id);
      P.maxBtn.replaceChildren(wbIco(WB.max === id ? 'min' : 'max')); P.maxBtn.title = WB.max === id ? 'Restore the size' : 'Expand this panel'; P.maxBtn.setAttribute('aria-label', (WB.max === id ? 'Restore ' : 'Expand ') + P.def.title);
      if (i > 0) kids.push(wbDivider(list[i - 1], id));
      kids.push(n.el);
      if (!n.mounted) { n.mounted = true; try { const r = d_mount(id, P); if (r && r.then) r.catch(e => P.body.append(errorBox(e.message))); } catch (e) { P.body.append(errorBox(e.message)); } }
      else if (P.onShow) { try { P.onShow(); } catch (e) { /* ignore */ } }
    });
    colEl.replaceChildren(...kids); colEl.hidden = !list.length || !!(WB.max && !list.includes(WB.max));
  }
  WB.cols.rz.hidden = !(WB.narrow.length && WB.wide.length) || !!WB.max;
  applySizes(); wbSync();
}
function d_mount(id, P) { return WB_DEF[id].mount(P); }
function wbDivider(a, b) {
  const d = h('div', { class: 'rz-h', role: 'separator', 'aria-orientation': 'horizontal', title: 'Drag to resize the panels' });
  d.addEventListener('pointerdown', (e) => {
    e.preventDefault(); d.setPointerCapture(e.pointerId); document.body.classList.add('resizing');
    const A = WB.nodes[a].el, B = WB.nodes[b].el; const ha = A.getBoundingClientRect().height, hb = B.getBoundingClientRect().height, y0 = e.clientY;
    const move = (ev) => { const dy = Math.max(-ha + 90, Math.min(hb - 90, ev.clientY - y0)); const tot = (WB.h[a] || 1) + (WB.h[b] || 1), sum = ha + hb; WB.h[a] = tot * (ha + dy) / sum; WB.h[b] = tot * (hb - dy) / sum; A.style.flex = WB.h[a] + ' 1 0'; B.style.flex = WB.h[b] + ' 1 0'; };
    const up = () => { d.removeEventListener('pointermove', move); d.removeEventListener('pointerup', up); document.body.classList.remove('resizing'); wbSave(); };
    d.addEventListener('pointermove', move); d.addEventListener('pointerup', up);
  });
  d.addEventListener('dblclick', () => { WB.h[a] = 1; WB.h[b] = 1; wbRender(); wbSave(); });
  return d;
}
function wbBuild() {
  if (WB.built) return WB.el; WB.built = true; wbLoad();
  const narrow = h('div', { class: 'wcol wcol-narrow' }), wide = h('div', { class: 'wcol wcol-wide' });
  const rz = h('div', { class: 'rz rz-w', role: 'separator', 'aria-orientation': 'vertical', title: 'Drag to resize' });
  WB.cols = { narrow, wide, rz };
  WB.el = h('div', { class: 'wpanel', id: 'wpanel' }, narrow, rz, wide);
  rz.addEventListener('pointerdown', (e) => {
    e.preventDefault(); rz.setPointerCapture(e.pointerId); document.body.classList.add('resizing'); const x0 = e.clientX, base = WB.wN;
    const move = (ev) => { WB.wN = Math.max(240, Math.min(720, base + (ev.clientX - x0))); applySizes(); };
    const up = () => { rz.removeEventListener('pointermove', move); rz.removeEventListener('pointerup', up); document.body.classList.remove('resizing'); wbSave(); };
    rz.addEventListener('pointermove', move); rz.addEventListener('pointerup', up);
  });
  return WB.el;
}
function wbSync() {
  document.querySelectorAll('[data-wb]').forEach(b => b.setAttribute('aria-pressed', String(wbIsOpen(b.dataset.wb))));
  document.querySelectorAll('[data-toggle="right"]').forEach(b => b.setAttribute('aria-pressed', String(!!UI.rightOpen)));
}
// ----- menus -----
function wbMenu(anchor, items, opts) {
  document.querySelectorAll('.wbmenu').forEach(m => m.remove());
  const o = opts || {};
  const menu = h('div', { class: 'wbmenu', role: 'menu' });
  const close = () => { menu.remove(); document.removeEventListener('mousedown', away, true); document.removeEventListener('keydown', key, true); };
  const away = (e) => { if (!menu.contains(e.target) && !anchor.contains(e.target)) close(); };
  const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); anchor.focus && anchor.focus(); } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const bs = [...menu.querySelectorAll('button:not([disabled])')]; const i = bs.indexOf(document.activeElement); if (bs.length) bs[(i + (e.key === 'ArrowDown' ? 1 : -1) + bs.length) % bs.length].focus(); } };
  for (const it of items) {
    if (!it) continue;
    if (it === '-') { menu.append(h('hr')); continue; }
    if (it.head) { menu.append(h('div', { class: 'mh' }, it.head)); continue; }
    if (it.node) { menu.append(it.node); continue; }
    menu.append(h('button', { type: 'button', role: 'menuitem', class: (it.cls || '') + (it.on ? ' on' : ''), disabled: !!it.disabled, title: it.title || '', onclick: () => { if (!it.keep) close(); if (it.run) it.run(); } },
      h('span', { class: 'mt' }, it.label), it.hint ? h('span', { class: 'mhint' }, it.hint) : null));
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const left = o.alignRight ? Math.max(8, r.right - menu.offsetWidth) : Math.min(r.left, innerWidth - menu.offsetWidth - 8);
  let top = r.bottom + 4; if (o.up || top + menu.offsetHeight > innerHeight - 8) top = Math.max(8, r.top - menu.offsetHeight - 4);
  menu.style.left = Math.max(8, left) + 'px'; menu.style.top = top + 'px';
  setTimeout(() => { document.addEventListener('mousedown', away, true); document.addEventListener('keydown', key, true); }, 0);
  const f = menu.querySelector('button:not([disabled])'); if (f && o.focus !== false) f.focus();
  return { close, el: menu };
}
function wbConfirm(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': title });
    const done = (v) => { scrim.remove(); resolve(v); };
    scrim.addEventListener('click', (e) => { if (e.target === scrim) done(false); });
    scrim.addEventListener('keydown', (e) => { if (e.key === 'Escape') done(false); });
    scrim.append(h('div', { class: 'modal wbconfirm' }, h('h3', null, title), ...[].concat(text).map(t => (typeof t === 'string' ? h('p', { class: 'muted' }, t) : t)),
      h('div', { class: 'acts' }, h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => done(true) }, okLabel || 'Confirm'), h('button', { class: 'btn', onclick: () => done(false) }, 'Cancel'))));
    document.body.append(scrim); const inp = scrim.querySelector('input[type=text]'); const b = scrim.querySelector('.btn:last-child');
    if (inp) { inp.focus(); inp.select(); inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); done(true); } }); } else if (b) b.focus();
  });
}
// ----- toolbar (top right) -----
function wbToolbar() {
  const tb = (id, label, key, icon, fn, extra) => h('button', Object.assign({ class: 'tbi', type: 'button', title: label, 'aria-label': label, 'aria-pressed': 'false', onclick: fn }, extra || {}), wbIco(icon));
  const more = h('button', { class: 'tbi', type: 'button', title: 'More', 'aria-label': 'More', 'aria-haspopup': 'menu', onclick: (e) => wbMoreMenu(e.currentTarget) }, wbIco('more'));
  return h('div', { class: 'tools tbicons' },
    tb('progress', 'Progress panel', '', 'progress', () => togglePanel('right'), { 'data-toggle': 'right' }),
    tb('terminal', 'Terminal', '', 'terminal', () => wbToggle('terminal'), { 'data-wb': 'terminal' }),
    tb('changes', 'Changes', '', 'changes', () => wbToggle('changes'), { 'data-wb': 'changes' }),
    tb('browser', 'Browser (preview a local dev server)', '', 'browser', () => wbToggle('browser'), { 'data-wb': 'browser' }),
    tb('files', 'Files', '', 'files', () => wbToggle('files'), { 'data-wb': 'files' }), more);
}
async function wbMoreMenu(anchor) {
  const C = UI.chat; const st = (C && C.st) || {};
  const items = [{ label: 'Search commands and pages', hint: IS_MAC_WB() ? '⌘K' : 'Ctrl+K', run: () => openPalette() },
    { label: 'Theme: switch light or dark', run: () => toggleTheme() }, { label: 'Keyboard shortcuts', hint: '?', run: () => helpModal() }, { label: 'Welcome and setup guide', run: () => welcomeOpen(true) }];
  const panels = [];
  panels.push({ label: 'Tasks', hint: (st.background || []).length ? String(st.background.length) + ' running' : 'scheduled and background', run: () => wbOpen('tasks') });
  if ((st.todos || []).length) panels.push({ label: 'Plan and todos', hint: String(st.todos.length), run: () => wbOpen('plan') });
  const mem = WB.ctx && WB.ctx.lists && WB.ctx.lists.memory_files || [];
  if (mem.length) panels.push({ label: 'Memory', hint: String(mem.length), run: () => wbOpen('memory') });
  let nh = 0, np = 0; try { const hd = await api('/api/workbench/hooks?session=' + encodeURIComponent((C && C.sid) || '')); nh = (hd.hooks || []).length; np = (hd.problems || []).length; } catch (e) { /* none */ }
  if (nh || np) panels.push({ label: 'Hooks', hint: np ? (nh ? nh + ' · ' : '') + 'needs attention' : String(nh), run: () => wbOpen('hooks') });
  wbMenu(anchor, [...items, panels.length ? '-' : null, panels.length ? { head: 'Panels' } : null, ...panels], { alignRight: true });
}
function IS_MAC_WB() { return /Mac|iPhone|iPad/i.test(navigator.platform || ''); }
// ----- the chat title and its menu -----
function wbTitleSync() {
  const b = document.getElementById('chat-title'); if (!b) return;
  const t = UI.active; const C = UI.chat;
  const isChat = !!(t && t.route === 'chat' && C);
  if (isChat && C.tab === t && C.meta && C.meta.title && t.title !== C.meta.title.slice(0, 26)) { t.title = C.meta.title.slice(0, 26); drawTabs(); }   // the tab carries the chat's name
  b.hidden = !isChat; b.disabled = !isChat; b.title = 'Chat options: rename, fork, export, find, bookmarks, delete'; b.setAttribute('aria-label', 'Chat options');
  const name = isChat ? ((C.meta && C.meta.title) || C.liveName || 'New chat') : '';
  b.replaceChildren(h('span', { class: 'ct-t' }, name), wbIco('chevd'));
}
function wbTitleMenu(anchor) {
  const C = UI.chat; if (!C) return; const sid = C.sid;
  const trace = [['compact', 'Compact'], ['normal', 'Normal'], ['detailed', 'Detailed'], ['debug', 'Debug (raw events)']];
  wbMenu(anchor, [
    { label: 'New chat', run: () => newChat(C) },
    '-', { label: 'Rename…', disabled: !sid, run: () => wbRename(C) },
    { label: 'Fork into a new chat', disabled: !sid, run: async () => { try { toast('Forking this chat…'); const st = await api('/api/chat/fork', { session: sid }); location.hash = '#/chat/' + encodeURIComponent(st.session); } catch (e) { toast(e.message); } } },
    { label: 'Export or share…', disabled: !(C.meta && C.meta.share !== false && sid), title: 'Markdown, web page, PDF or JSON, with secrets removed. A file on this computer; nothing is uploaded.', run: () => shareModal({ kind: 'session', id: sid }) },
    { label: 'Find in chat', hint: IS_MAC_WB() ? '⌘F' : 'Ctrl+F', run: () => cxFind(C) },
    { label: 'Bookmarks…', disabled: !sid, run: () => cxBookmarksMenu(C, anchor) },
    { label: 'Compare models…', disabled: !sid, title: 'Ask two to four models the same question side by side. Read-only: nothing in your project changes.', run: () => cfCompareOpen(C) },
    '-', { head: 'Trace detail' }, ...trace.map(([v, l]) => ({ label: l, on: state.trace === v, keep: true, run: () => { state.trace = v; lsSet('axon-trace2', v); document.querySelectorAll('.feed').forEach(f => { f.className = 'feed t-' + v; }); drawStatus(); document.querySelectorAll('.wbmenu .on').forEach(x => x.classList.remove('on')); } })),
    '-', { label: 'Notification settings…', title: 'Choose when Axon tells you a turn finished or needs you', run: () => { location.hash = '#/settings/preferences'; } },
    '-', { label: 'Delete this chat…', cls: 'danger', disabled: !sid, run: () => wbDelete(C) }]);
}
async function wbRename(C) {
  const cur = (C.meta && C.meta.title) || '';
  const input = h('input', { type: 'text', class: 'search', value: cur, maxlength: 64, 'aria-label': 'Chat name' });
  const ok = await wbConfirm('Rename this chat', [input], 'Rename', false);
  if (!ok || !input.value.trim()) return;
  try { const r = await api('/api/workbench/session_rename', { session: C.sid, title: input.value }); C.meta = Object.assign(C.meta || {}, { title: r.title }); if (C.tab) setTabTitle(C.tab, r.title.slice(0, 26)); wbTitleSync(); } catch (e) { toast(e.message); }
}
async function wbDelete(C) {
  const ok = await wbConfirm('Delete this chat?', 'It moves to the project trash and nothing is destroyed. /restore brings it back.', 'Delete', true);
  if (!ok) return;
  try { await api('/api/workbench/session_delete', { session: C.sid, confirm: true }); toast('Moved to the trash. /restore undoes it.'); const t = C.tab; if (t) closeTab(t); location.hash = '#/chat'; route(); } catch (e) { toast(e.message); }
}
// ----- folder, branch, worktrees -----
async function wbSwitchFolder(path) {
  try { const r = await api('/api/ws/set', { path }); state.ws = await api('/api/ws'); toast('Now working in ' + r.name); for (const t of [...UI.tabs]) closeTab(t); if (typeof TREE !== 'undefined') { TREE.cache = {}; TREE.open = new Set(); } drawChip(); drawLeftTabs(); if (typeof shellFolderChanged === 'function') shellFolderChanged(); WB.ctx = null; WB.nodes = {}; location.hash = '#/chat'; route(); }
  catch (e) { toast(e.message); }
}
async function wbBranchMenu(anchor) {
  let b, w; try { [b, w] = await Promise.all([api('/api/workbench/branches'), api('/api/workbench/worktrees')]); } catch (e) { toast(e.message); return; }
  if (!b.repo) { toast('This folder is not a git repository.'); return; }
  const dirty = (state.ws && state.ws.git && state.ws.git.changed) || Object.keys(GIT_CACHE.files || {}).length;   // the same count as the header and the Changes panel
  const items = [{ head: 'Branches' }, ...b.branches.slice(0, 40).map(x => ({ label: x.name, hint: x.current ? 'current' : x.when, on: x.current, disabled: x.current,
    run: async () => { if (dirty && !(await wbConfirm('Switch to ' + x.name + '?', dirty + ' uncommitted change' + (dirty > 1 ? 's' : '') + ' stay in your working tree. git refuses the switch if they would be overwritten.', 'Switch'))) return;
      try { await api('/api/workbench/checkout', { branch: x.name }); toast('Now on ' + x.name); state.ws = await api('/api/ws'); drawChip(); GIT_CACHE.t = 0; renderRight(); wbRefreshChanges(true); } catch (e) { toast(e.message); } } }))];
  const others = (w.worktrees || []);
  if (others.length > 1) {
    items.push('-', { head: 'Worktrees' });
    for (const t of others) items.push({ label: (t.branch || 'worktree') + (t.main ? ' (main)' : ''), hint: t.current ? 'open' : (t.changes ? t.changes + ' changes' : 'clean'), title: t.path, disabled: t.current || t.missing, keep: false,
      run: () => { wbMenu(anchor, [{ head: t.path.replace(/^\/Users\/[^/]+/, '~') }, { label: 'Switch to this worktree', run: () => wbSwitchFolder(t.path) },
        t.main ? null : { label: t.changes ? 'Remove (has ' + t.changes + ' uncommitted change' + (t.changes > 1 ? 's' : '') + ': not allowed)' : 'Remove…', cls: 'danger', disabled: !!t.changes, run: async () => { if (await wbConfirm('Remove this worktree?', t.path + ' will be deleted from disk. Its branch is kept.', 'Remove', true)) { try { await api('/api/workbench/worktree_remove', { path: t.path }); toast('Worktree removed'); } catch (e) { toast(e.message); } } } }]); } });
  }
  items.push('-', { label: 'New worktree…', run: async () => { const input = h('input', { type: 'text', class: 'search', placeholder: 'name, for example fix-login', maxlength: 60, 'aria-label': 'Worktree name' });
    if (!(await wbConfirm('New worktree', ['A separate checkout on its own branch (axon/NAME), so a second task cannot disturb this one.', input], 'Create'))) return;
    try { const r = await api('/api/workbench/worktree_create', { name: input.value }); toast('Created ' + r.branch); if (await wbConfirm('Open it now?', r.path, 'Switch')) wbSwitchFolder(r.path); } catch (e) { toast(e.message); } } });
  wbMenu(anchor, items);
}
"""

JS_WORKBENCH_PANELS = r"""
// ---------- panel: terminal (real PTY, several tabs) ----------
function wbTerminal(P) {
  const T = { tabs: [], cur: null, n: 0, ok: false, consent: false };
  const strip = h('div', { class: 'wtabs', role: 'tablist' }); const host = h('div', { class: 'wterms' });
  P.body.append(host);
  const plus = h('button', { class: 'wpb', type: 'button', title: 'New terminal', 'aria-label': 'New terminal', 'aria-haspopup': 'menu', onclick: (e) => wbMenu(e.currentTarget, [{ label: 'Shell on this computer', run: () => start('shell') }, { label: 'Axon in a terminal', run: () => start('axon') }]) }, wbIco('plus'));
  const full = h('button', { class: 'wpb', type: 'button', title: 'Move this terminal to a full tab', 'aria-label': 'Move to a full tab', onclick: () => { const t = T.tabs.find(x => x.id === T.cur); if (t) { location.hash = '#/terminal/' + encodeURIComponent(t.id); wbClose('terminal'); } } }, wbIco('pop'));
  const dots = h('button', { class: 'wpb', type: 'button', title: 'More', 'aria-label': 'More', 'aria-haspopup': 'menu', onclick: (e) => wbMenu(e.currentTarget, [{ label: 'Close this terminal', disabled: !T.cur, run: () => closeTerm(T.cur) }, { label: 'Close all terminals', disabled: !T.tabs.length, run: () => T.tabs.slice().forEach(t => closeTerm(t.id)) }], { alignRight: true }) }, wbIco('more'));
  const head = () => P.setHead([strip], [plus, dots, full]);
  const drawTabs = () => {
    strip.replaceChildren(...T.tabs.map(t => h('div', { class: 'wtab' + (t.id === T.cur ? ' on' : ''), role: 'tab', 'aria-selected': String(t.id === T.cur) },
      h('button', { type: 'button', class: 'wtl', onclick: () => select(t.id), title: t.title }, t.title), h('button', { type: 'button', class: 'wtx', 'aria-label': 'Close ' + t.title, onclick: () => closeTerm(t.id) }, '×'))));
    if (!T.tabs.length) strip.replaceChildren(h('span', { class: 'wt-none' }, 'Terminal'));
  };
  const select = (id) => { T.cur = id; for (const t of T.tabs) t.box.hidden = t.id !== id; drawTabs(); const t = T.tabs.find(x => x.id === id); if (t) setTimeout(() => { t.view.fit(); t.view.focus(); }, 30); };
  const closeTerm = async (id) => { const i = T.tabs.findIndex(t => t.id === id); if (i < 0) return; const t = T.tabs[i]; await api('/api/term/close', { id }).catch(() => { }); t.view.destroy(); t.box.remove(); T.tabs.splice(i, 1);
    if (T.cur === id) T.cur = T.tabs.length ? T.tabs[Math.max(0, i - 1)].id : null; if (T.cur) select(T.cur); else { drawTabs(); empty(); } };
  const start = async (kind) => {
    try {
      const s = await api('/api/term/start', { kind, cols: 90, rows: 28, initial: '', theme: curTheme() });
      const box = h('div', { class: 'wterm' }); host.querySelector('.wt-empty')?.remove(); host.append(box); const t = { id: s.id, title: (kind === 'axon' ? 'Axon ' : 'Terminal ') + (++T.n), box };
      t.view = termView(box, s.id, { bar: false, onTitle: (x) => { if (x && x.length < 28 && t.title !== x) { t.title = x; drawTabs(); } } }); T.tabs.push(t); select(s.id);
    } catch (e) { toast(e.message); }
  };
  const empty = () => {
    if (host.querySelector('.wt-empty')) return;
    if (!T.ok) {   // no PTY here (Windows, say): the line-based Shell runs inside this same panel
      if (!T.lineShell) { T.lineShell = true; host.append(h('p', { class: 'muted fp-note', title: T.why || '' }, 'A full terminal is not available on this system. This runs one command at a time.')); UI.building = { cleanup: [], pane: host, route: 'shell' }; mountShell(host); P.setHead(null, []); }
      return;
    }
    host.append(h('div', { class: 'wt-empty' }, h('strong', null, 'A shell on this computer'),
      h('p', { class: 'muted' }, 'This is a real shell in ', h('b', null, T.cwd || ''), '. What you type runs with your own permissions, with no shell guard and no approval step. Open one only if you want that.'),
      h('div', { class: 'formrow' }, h('button', { class: 'btn primary', onclick: () => start('shell') }, 'Open a shell'), h('button', { class: 'btn', onclick: () => start('axon') }, 'Axon in a terminal'))));
  };
  P.onShow = () => { const t = T.tabs.find(x => x.id === T.cur); if (t) setTimeout(() => { t.view.fit(); }, 30); };
  P.onClose = () => { /* shells keep running: the panel only hides; closing a tab ends its shell */ };
  head(); drawTabs();
  return api('/api/term/list').then(a => { T.ok = a.available; T.why = a.reason; T.cwd = a.cwd; empty(); }).catch(e => { P.body.append(errorBox(e.message)); });
}
// ---------- panel: files (the one place for the project tree) ----------
// Uses the virtual tree of dash_js_files (drag into the chat, multi-select, right-click menu, A key, git marks, huge folders).
function wbFiles(P) {
  const input = h('input', { type: 'search', class: 'fp-q', placeholder: 'Filter, or ? to search in files', 'aria-label': 'Filter files' });
  const colBtn = h('button', { class: 'wpb', type: 'button', title: 'Collapse all folders', 'aria-label': 'Collapse all', onclick: () => { TREE.open = new Set(); TREE.rows = treeFlatten(); treePaint(); } }, wbIco('collapse'));
  const modeBtn = h('button', { class: 'wpb', type: 'button', title: 'Show a flat list or the tree', 'aria-label': 'List or tree', 'aria-pressed': 'false', onclick: () => { TREE.list = !TREE.list; modeBtn.setAttribute('aria-pressed', String(TREE.list)); TREE.rows = treeFlatten(); treePaint(); } }, wbIco('list'));
  const vp = h('div', { class: 'vlist' }); const inner = h('div', { class: 'vinner' }); const win = h('div', { class: 'vwin' }); inner.append(win); vp.append(inner);
  Object.assign(TREE, { vp, win, inner });
  const res = h('div', { class: 'sres fp-res', hidden: true });
  const ctl = h('div', { class: 'fp-ctl' }, h('button', { class: 'btn', type: 'button', onclick: () => folderChooser() }, 'Change folder'), h('button', { class: 'btn', type: 'button', title: 'Reload the tree', onclick: () => P.refresh() }, 'Reload'),
    h('label', { class: 'muted fp-ign' }, h('input', { type: 'checkbox', checked: TREE.ignored, onchange: (e) => { TREE.ignored = e.target.checked; P.refresh(); } }), 'ignored files'));
  P.body.append(ctl, h('label', { class: 'fp-find' }, wbIco('search'), input), vp, res);
  vp.addEventListener('scroll', () => requestAnimationFrame(treePaint));
  new ResizeObserver(() => treePaint()).observe(vp);
  const showRes = (kids) => { res.replaceChildren(...kids); res.hidden = !kids.length; vp.hidden = !!kids.length; };
  const hit = (f) => h('a', Object.assign({ class: 'hit', href: '#/files/' + encodeURIComponent(f), draggable: 'true', title: 'Drag into the chat to attach' }, axDragProps([f])), h('b', null, f.split('/').pop()), h('span', { class: 'muted' }, f));
  let tm = 0;
  const grep = async () => {
    const q = input.value.trim().slice(1).trim(); if (q.length < 2) { showRes([]); return; }
    try { const d = await api('/api/files/grep?q=' + encodeURIComponent(q)); showRes(d.matches.length ? d.matches.slice(0, 200).map(m => h('a', Object.assign({ class: 'hit', href: '#/files/' + encodeURIComponent(m.path), draggable: 'true', title: m.path + ':' + m.line + '  (drag into the chat to attach the file)' }, axDragProps([m.path])), h('b', null, m.path.split('/').pop() + ':' + m.line), h('span', { class: 'muted' }, m.text))).concat(d.skipped_large ? [h('p', { class: 'muted fp-note' }, d.skipped_large + ' large file' + (d.skipped_large === 1 ? ' was' : 's were') + ' not searched.')] : []) : [h('p', { class: 'muted fp-note' }, d.skipped_large ? 'Nothing found. ' + d.skipped_large + ' large file' + (d.skipped_large === 1 ? ' was' : 's were') + ' not searched.' : 'Nothing found.')]); } catch (er) { toast(er.message); }
  };
  input.addEventListener('input', () => { clearTimeout(tm); const v = input.value; showRes([]); if (v.trim().startsWith('?')) { tm = setTimeout(grep, 350); return; } TREE.filter = v; TREE.rows = treeFlatten(); treePaint(); });
  input.addEventListener('keydown', async (e) => { const v = input.value.trim(); if (e.key !== 'Enter' || v.startsWith('?') || !v) return; try { const d = await api('/api/files/search?q=' + encodeURIComponent(v)); showRes(d.files.length ? d.files.slice(0, 200).map(hit) : [h('p', { class: 'muted fp-note' }, 'No file matches.')]); } catch (er) { toast(er.message); } });
  P.refresh = async () => { await treeReload(); };
  P.onShow = () => { if (!TREE.cache['']) P.refresh(); else { TREE.rows = treeFlatten(); treePaint(); } };
  P.setHead(null, [colBtn, modeBtn]);
  return P.refresh();
}
// ---------- panel: changes (working tree) ----------
const CH = { mode: 'working', base: '', files: [], open: new Set(), diffs: {}, q: '', data: null, err: '', loading: false, fixed: null };
const CH_FILE_H = 30, CH_LINE_H = 19;
function wbLang(path) { const e = (path.split('.').pop() || '').toLowerCase(); return ({ py: 'python', js: 'js', mjs: 'js', ts: 'js', tsx: 'js', jsx: 'js', json: 'json', md: 'md', sh: 'sh', zsh: 'sh', yml: 'yaml', yaml: 'yaml', toml: 'toml', go: 'go', rs: 'rust', c: 'c', h: 'c', cpp: 'c', java: 'java', rb: 'ruby', sql: 'sql' })[e] || 'text'; }
function wbRows(d) {
  const rows = (d.rows || []).slice(0, 3000).map(r => ({ t: r.kind === 'meta' ? 'note' : r.kind, n: r.kind === 'del' ? r.old : (r.new != null ? r.new : r.old), text: r.text }));
  if ((d.rows || []).length > 3000 || d.truncated) rows.push({ t: 'note', text: 'Diff cut here. Open the file in the viewer to see the rest.' });
  return rows;
}
function wbChanges(P) {
  const search = h('input', { type: 'search', class: 'ch-q', placeholder: 'Filter files', 'aria-label': 'Filter changed files', hidden: true });
  const scroller = h('div', { class: 'ch-scroll' }); const inner = h('div', { class: 'ch-inner' }); scroller.append(inner);
  const sticky = h('div', { class: 'ch-sticky', hidden: true });
  const note = h('div', { class: 'ch-note', hidden: true });
  P.body.append(search, note, axCheckpoints(P), h('div', { class: 'ch-wrap' }, sticky, scroller));
  let flat = [], offs = [0], total = 0;
  const visible = () => { const q = CH.q.toLowerCase().trim(); return q ? CH.files.filter(f => f.path.toLowerCase().includes(q)) : CH.files; };
  const rebuild = () => {
    flat = []; for (const f of visible()) { flat.push({ k: 'file', f }); if (CH.open.has(f.path)) { const d = CH.diffs[f.path]; if (!d) flat.push({ k: 'wait', f }); else if (d.err) flat.push({ k: 'err', f, text: d.err }); else if (!d.rows.length) flat.push({ k: 'empty', f, text: d.note }); else for (const r of d.rows) flat.push({ k: 'line', f, r }); } }
    offs = [0]; for (const x of flat) offs.push(offs[offs.length - 1] + (x.k === 'file' ? CH_FILE_H : CH_LINE_H)); total = offs[offs.length - 1]; inner.style.height = total + 'px'; paint();
  };
  const fileRow = (f, st) => {
    const dir = f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : ''; const name = f.path.split('/').pop(); const cls = f.code === 'U' || f.code === 'A' ? 'new' : f.code === 'D' ? 'del' : 'mod';
    const open = CH.open.has(f.path);
    const r = h('div', { class: 'ch-file ' + cls + (st ? ' stuck' : ''), role: 'button', tabindex: '0', 'aria-expanded': String(open), title: f.path + (f.staged ? ' (staged)' : '') },
      h('span', { class: 'fp-chev' + (open ? ' open' : '') }, wbIco('chev')), h('span', { class: 'ftm' }, (name.split('.').pop() || '').slice(0, 3)),
      h('span', { class: 'ch-name' }, name), dir ? h('span', { class: 'ch-dir' }, dir) : null, f.staged ? h('span', { class: 'ch-st', title: 'Staged' }, 'staged') : null, h('span', { style: 'flex:1' }),
      f.added ? h('span', { class: 'cadd' }, '+' + f.added) : null, f.removed ? h('span', { class: 'cdel' }, '−' + f.removed) : null,
      h('button', { class: 'wpb sm', type: 'button', title: 'Open in the viewer', 'aria-label': 'Open ' + name, onclick: (e) => { e.stopPropagation(); location.hash = '#/files/' + encodeURIComponent(f.path) + (f.code !== 'U' ? '?diff=1' : ''); } }, wbIco('external')),
      h('button', { class: 'wpb sm', type: 'button', title: 'More', 'aria-label': 'More for ' + name, 'aria-haspopup': 'menu', onclick: (e) => { e.stopPropagation(); chMenu(e.currentTarget, f); } }, wbIco('more')));
    r.addEventListener('click', () => toggle(f)); r.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(f); } });
    return r;
  };
  const chMenu = (anchor, f) => wbMenu(anchor, [
    f.staged ? { label: 'Unstage', run: () => act(f, 'unstage') } : { label: 'Stage', run: () => act(f, 'stage') },
    { label: 'Discard changes…', cls: 'danger', run: async () => { if (await wbConfirm('Discard changes to ' + f.path + '?', f.code === 'U' ? 'This file is new. Discarding deletes it from disk and cannot be undone.' : 'The file goes back to the last commit. Your edits are lost and this cannot be undone.', 'Discard', true)) act(f, 'discard', true); } },
    '-', { label: 'Copy path', run: () => axCopy(f.path) }, { label: 'Add to chat', run: () => axAddToChat([f.path]) }], { alignRight: true });
  const act = async (f, action, confirm) => { try { await api('/api/workbench/stage', { path: f.path, action, confirm: !!confirm, code: f.code }); delete CH.diffs[f.path]; GIT_CACHE.t = 0; await P.refresh(true); } catch (e) { toast(e.message); } };
  const toggle = async (f) => { if (CH.open.has(f.path)) { CH.open.delete(f.path); rebuild(); return; } CH.open.add(f.path); rebuild(); await loadDiff(f); };
  const loadDiff = async (f) => {
    try { const d = await api('/api/workbench/diff?path=' + encodeURIComponent(f.path) + '&mode=' + CH.mode + '&ref=' + encodeURIComponent(CH.base) + '&code=' + encodeURIComponent(f.code)); CH.diffs[f.path] = { rows: wbRows(d), trunc: d.truncated, lang: wbLang(f.path), note: d.note }; }
    catch (e) { CH.diffs[f.path] = { err: e.message, rows: [] }; }
    rebuild();
  };
  const lineRow = (x, top) => {
    const r = x.r; const el = h('div', { class: 'ch-line ' + r.t, style: 'top:' + top + 'px;height:' + CH_LINE_H + 'px' });
    if (r.t === 'hunk' || r.t === 'note') { el.append(h('span', { class: 'ln' }), h('span', { class: 'lt' }, r.text)); return el; }
    el.append(h('span', { class: 'ln' }, String(r.n)), h('span', { class: 'lm' }, r.t === 'add' ? '+' : r.t === 'del' ? '−' : ' '), h('span', { class: 'lt' }, hlLine(r.text, (CH.diffs[x.f.path] || {}).lang || 'text')));
    return el;
  };
  const paint = () => {
    const top = scroller.scrollTop, hgt = scroller.clientHeight || 600;
    let lo = 0, hi = flat.length; while (lo < hi) { const m = (lo + hi) >> 1; if (offs[m + 1] <= top) lo = m + 1; else hi = m; }
    const from = Math.max(0, lo - 6); const els = [];
    for (let i = from; i < flat.length && offs[i] < top + hgt + 200; i++) {
      const x = flat[i]; let el;
      if (x.k === 'file') { el = fileRow(x.f); el.style.top = offs[i] + 'px'; el.style.height = CH_FILE_H + 'px'; }
      else if (x.k === 'line') el = lineRow(x, offs[i]);
      else { el = h('div', { class: 'ch-line note', style: 'top:' + offs[i] + 'px;height:' + CH_LINE_H + 'px' }, h('span', { class: 'ln' }), h('span', { class: 'lt' }, x.k === 'wait' ? 'Loading the diff…' : x.k === 'empty' ? (x.text || 'No textual changes (binary file, mode change or empty).') : x.text)); }
      els.push(el);
    }
    inner.replaceChildren(...els);
    // sticky header: the file whose open diff covers the top edge
    let owner = null; for (let i = Math.min(lo, flat.length - 1); i >= 0; i--) { if (flat[i].k === 'file') { owner = flat[i].f; if (offs[i] < top) break; owner = null; break; } }
    const f0 = flat[Math.min(lo, flat.length - 1)];
    if (owner && CH.open.has(owner.path) && f0 && f0.k !== 'file') { sticky.hidden = false; sticky.replaceChildren(fileRow(owner, true)); } else sticky.hidden = true;
  };
  let raf = 0; scroller.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; paint(); }); });
  new ResizeObserver(() => paint()).observe(scroller);
  search.addEventListener('input', () => { CH.q = search.value; rebuild(); });
  const head = () => {
    const d = CH.data || {}; const label = d.label || 'working tree';
    const big = (d.added || 0) + (d.removed || 0) > 5000 || CH.files.length > 200;
    const cmp = h('button', { class: 'ch-cmp', type: 'button', 'aria-haspopup': 'menu', title: 'What to compare', onclick: async (e) => {
      const anchor = e.currentTarget; let br = []; try { br = (await api('/api/workbench/branches')).branches; } catch (er) { /* none */ }
      wbMenu(anchor, [{ head: 'Compare against' }, { label: 'Working tree (all changes)', on: CH.mode === 'working', run: () => setMode('working') }, { label: 'Staged changes', on: CH.mode === 'staged', run: () => setMode('staged') }, { label: 'Last commit', on: CH.mode === 'commit', run: () => setMode('commit') },
        ...(br.length ? ['-', { head: 'Branches' }, ...br.slice(0, 30).map(b => ({ label: b.name, on: CH.mode === 'branch' && CH.base === b.name, run: () => setMode('branch', b.name) }))] : [])]); } },
      h('span', null, label), wbIco('chevd'));
    const sb = h('button', { class: 'wpb', type: 'button', title: 'Filter files', 'aria-label': 'Filter files', onclick: () => { search.hidden = !search.hidden; if (!search.hidden) search.focus(); else { CH.q = ''; search.value = ''; rebuild(); } } }, wbIco('search'));
    const rf = h('button', { class: 'wpb', type: 'button', title: 'Refresh', 'aria-label': 'Refresh', onclick: () => P.refresh(true) }, wbIco('reload'));
    P.setHead([cmp, big ? h('span', { class: 'ch-chip', title: (d.added || 0) + ' lines added, ' + (d.removed || 0) + ' removed in ' + CH.files.length + ' files. Diffs load one file at a time.' }, 'Large diff') : null], [sb, rf]);
  };
  const setMode = (m, b) => { CH.mode = m; CH.base = b || ''; CH.diffs = {}; CH.open = new Set(); P.refresh(true); };
  P.refresh = async (force) => {
    if (CH.loading && !force) return; CH.loading = true;
    try {
      const d = await api('/api/workbench/changes?mode=' + CH.mode + '&ref=' + encodeURIComponent(CH.base)); CH.data = d; CH.files = d.files || []; CH.err = '';
      for (const p of [...CH.open]) { if (!CH.files.find(f => f.path === p)) { CH.open.delete(p); delete CH.diffs[p]; } else { delete CH.diffs[p]; } }
      note.hidden = true;
      if (!d.repo) { note.hidden = false; note.textContent = 'This folder is not a git repository, so there is nothing to compare. Axon’s own edits for this chat are listed in the Progress panel.'; }
      else if (!CH.files.length) { note.hidden = false; note.textContent = CH.mode === 'working' ? 'The working tree is clean: no changes since the last commit.' : 'No differences for this comparison.'; }
      head(); rebuild(); for (const p of [...CH.open]) { const f = CH.files.find(x => x.path === p); if (f) loadDiff(f); }
      wbChangesSummary(d); if (P.cpRefresh) P.cpRefresh();
    } catch (e) { note.hidden = false; note.textContent = e.message; }
    CH.loading = false;
  };
  P.onShow = () => { P.refresh(true); };
  head();
  return P.refresh(true);
}
let WB_CHG = null;
function wbSyncGit(d) {   // keep the header chip's branch and count in step with the Changes figures
  const g = state.ws && state.ws.git; if (!g || !g.repo || !d || !d.repo || d.target !== 'working') return;
  const n = (d.files || []).length; if (g.changed !== n || (d.branch && g.branch !== d.branch)) { g.changed = n; if (d.branch) g.branch = d.branch; drawChip(); }
}
function wbChangesSummary(d) { WB_CHG = d; if (d.target === 'working') wbSyncGit(d); if (UI.chat) drawRail(UI.chat); }
function wbRefreshChanges(force) { const n = WB.nodes.changes; if (n && n.P.refresh && wbIsOpen('changes')) n.P.refresh(force); }
// ---------- panel: browser (preview of local dev servers) ----------
const BR = { url: '', frame: null, ports: [] };
function wbNormalizeUrl(raw) {
  let s = (raw || '').trim(); if (!s) return { err: 'Type an address, for example 3000 or localhost:5173.' };
  if (/\s/.test(s)) return { err: 'That is not a web address.' };   // Chrome's URL() accepts a space in a host and encodes it
  if (/^:?\d{2,5}$/.test(s)) s = 'http://localhost:' + s.replace(':', '');
  else if (/^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/.*)?$/i.test(s)) s = 'http://' + s;
  else if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = (/^[\w-]+(\.[\w-]+)+/.test(s) ? 'https:' + '//' : 'http:' + '//') + s;
  let u; try { u = new URL(s); } catch (e) { return { err: 'That is not a web address.' }; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return { err: 'Only http and https addresses can be opened.' };
  const loop = u.protocol === 'http:' && (u.hostname === 'localhost' || u.hostname === '127.0.0.1') && u.port;
  const own = loop && u.port === String(location.port) && (u.hostname === location.hostname || u.hostname === 'localhost' || u.hostname === '127.0.0.1');
  return { url: u.href, frame: !!loop && !own, own: !!own };
}
function wbBrowser(P) {
  const input = h('input', { type: 'text', class: 'br-url', placeholder: 'localhost:3000', 'aria-label': 'Address', spellcheck: 'false', autocomplete: 'off' });
  const msg = h('div', { class: 'br-msg' }); const chips = h('div', { class: 'br-ports' });
  const stage = h('div', { class: 'br-stage' }, h('div', { class: 'wt-empty' }, h('strong', null, 'Preview a local app'), h('p', { class: 'muted' }, 'Type the address of a dev server running on this computer (localhost or 127.0.0.1 with a port), or pick one Axon found below. It opens in a sandboxed frame.')));
  const reload = h('button', { class: 'wpb', type: 'button', title: 'Reload', 'aria-label': 'Reload', onclick: () => { if (BR.frame) { BR.frame.src = 'about:blank'; setTimeout(() => { BR.frame.src = BR.url; }, 30); } } }, wbIco('reload'));
  const ext = h('button', { class: 'wpb', type: 'button', title: 'Open in a new tab', 'aria-label': 'Open in a new tab', onclick: () => { if (BR.url) window.open(BR.url, '_blank', 'noopener,noreferrer'); } }, wbIco('external'));
  P.body.append(h('div', { class: 'br-bar' }, h('form', { class: 'br-form', onsubmit: (e) => { e.preventDefault(); go(input.value); } }, input), reload, ext), chips, msg, stage);
  const go = (raw) => {
    const r = wbNormalizeUrl(raw); msg.textContent = '';
    if (r.err) { msg.textContent = r.err; return; }
    if (r.own) { msg.textContent = 'That is this Axon page itself. It cannot be shown inside itself.'; return; }
    if (!r.frame) { input.value = r.url; BR.url = r.url; window.open(r.url, '_blank', 'noopener,noreferrer'); msg.textContent = 'Opened in a new tab. Most websites refuse to be shown inside another page, and only local dev servers (http on localhost or 127.0.0.1 with a port) are previewed here.'; return; }
    BR.url = r.url; input.value = r.url;
    const f = h('iframe', { class: 'br-frame', src: r.url, title: 'Preview of ' + r.url, sandbox: 'allow-scripts allow-forms allow-same-origin allow-popups allow-modals', referrerpolicy: 'no-referrer', loading: 'eager' });
    BR.frame = f; stage.replaceChildren(f); msg.textContent = 'If the frame stays blank, the app may refuse to be framed or is not running. Use Open in a new tab.';
  };
  const drawPorts = () => { chips.replaceChildren(...BR.ports.map(p => h('button', { class: 'br-chip', type: 'button', title: p.command + ' (pid ' + p.pid + ') is listening on port ' + p.port, onclick: () => go('localhost:' + p.port) }, ':' + p.port, h('span', { class: 'muted' }, p.command)))); chips.hidden = !BR.ports.length; };
  const poll = async () => { try { BR.ports = (await api('/api/workbench/ports')).ports; drawPorts(); } catch (e) { /* none */ } };
  poll(); const iv = setInterval(() => { if (wbIsOpen('browser') && !document.hidden) poll(); }, 7000);
  P.onClose = () => { /* keep the page loaded while hidden */ };
  P.setHead(null, []); if (BR.url) input.value = BR.url;
}
// ---------- small panels with real data ----------
function wbTasks(P) {
  // Scheduled tasks first (tasksMount), then the shell commands the agent started in the background.
  const bgNode = () => {
    const bg = ((UI.chat && UI.chat.st) || {}).background || [];
    return h('div', null, ...(bg.length ? bg.map(b => h('div', { class: 'row' }, h('span', { class: 'dot ' + (b.status === 'running' ? 'running' : 'finished') }), h('span', { class: 't', title: b.command }, b.command), h('span', { class: 'muted' }, b.status),
      b.status === 'running' ? h('button', { class: 'mini', onclick: async () => { try { await api('/api/shell/bg/kill', { id: b.id }); toast('Stopped'); refreshState(UI.chat, true); } catch (e) { toast(e.message); } } }, 'Stop') : null)) : [h('p', { class: 'muted' }, 'No background commands are running.')]));
  };
  const holder = h('div'); tasksMount(P, holder);
  const redrawBg = () => holder.replaceChildren(bgNode());
  const baseRedraw = P.redraw; P.redraw = () => { redrawBg(); if (baseRedraw) baseRedraw(); }; redrawBg();
}
function wbPlan(P) {
  const draw = () => {
    const todos = ((UI.chat && UI.chat.st) || {}).todos || [];
    P.body.replaceChildren(h('div', { class: 'wp-pad' }, ...(todos.length ? todos.map(t => h('div', { class: 'todo ' + (t.status === 'completed' ? 'done' : t.status === 'in_progress' ? 'now' : '') }, h('span', null, t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '▶' : '○'), h('span', null, t.status === 'in_progress' && t.active ? t.active : t.content))) : [h('p', { class: 'muted' }, 'No plan yet. Axon writes one for multi-step work.')])));
  };
  P.onShow = draw; P.redraw = draw; draw();
}
function wbMemory(P) {
  const draw = () => {
    const files = (WB.ctx && WB.ctx.lists && WB.ctx.lists.memory_files) || [];
    P.body.replaceChildren(h('div', { class: 'wp-pad' }, ...(files.length ? files.map(f => h('div', { class: 'row' }, h('span', { class: 't', title: f.name }, f.name), h('span', { class: 'muted' }, f.scope), h('span', { class: 'num' }, '~' + tok(f.tokens)))) : [h('p', { class: 'muted' }, 'No memory files are loaded.')]),
      h('p', { class: 'muted' }, h('a', { href: '#/memory' }, 'Open the Memory page'), ' to edit them. Token counts are estimates.')));
  };
  P.onShow = draw; P.redraw = draw; draw();
}
function hooksView(list, problems) {
  const warn = (problems || []).map(t => h('div', { class: 'bad hk-warn' }, t));
  if (!list.length) return [...warn, h('p', { class: 'muted' }, 'No hooks are running.'), h('p', { class: 'muted' }, 'A hook is a command that runs on an event such as PreToolUse. Add one under "hooks" in .axon/settings.json, then reopen this panel.')];
  const by = {}; list.forEach(k => { (by[k.event] = by[k.event] || []).push(k); });
  const out = [...warn, h('p', { class: 'muted' }, list.length + (list.length === 1 ? ' hook' : ' hooks') + ' across ' + Object.keys(by).length + (Object.keys(by).length === 1 ? ' event' : ' events'))];
  Object.keys(by).forEach(ev => {
    out.push(h('div', { class: 'hk-ev' }, ev));
    by[ev].forEach(k => {
      const bits = [k.type, k.timeout ? k.timeout + 's timeout' : '', k.async ? 'async' : '', k.if ? 'if ' + k.if : '', k.source].filter(Boolean).join(' · ');
      out.push(h('div', { class: 'hk' }, h('b', null, k.matcher && k.matcher !== '*' ? 'matches ' + k.matcher : 'every call'), h('code', null, k.command), k.statusMessage ? h('span', { class: 'muted' }, '“' + k.statusMessage + '”') : null, h('span', { class: 'muted' }, bits)));
    });
  });
  return out;
}
async function wbHooks(P) {
  P.onShow = () => wbHooks(P);
  try { const d = await api('/api/workbench/hooks?session=' + encodeURIComponent((UI.chat && UI.chat.sid) || ''));
    P.body.replaceChildren(h('div', { class: 'wp-pad' }, ...hooksView(d.hooks || [], d.problems || []))); }
  catch (e) { P.body.replaceChildren(errorBox(e.message)); }
}
"""

JS_WORKBENCH_RAIL = r"""
// ---------- Progress panel: Environment, Context, Usage, Plan, Connectors, Changes ----------
const WB_CTX_COLORS = { messages: 'var(--c-msg)', system_tools: 'var(--c-sys)', mcp_tools: 'var(--c-mcp)', skills: 'var(--c-skill)', system_prompt: 'var(--c-prompt)', mcp_instructions: 'var(--c-minst)', custom_agents: 'var(--c-agent)', memory_files: 'var(--c-mem)', autocompact: 'var(--c-buf)', free: 'transparent' };
const WB_CTX_HELP = {
  messages: 'Everything said so far: your messages, the replies and every tool result. This is the part that grows. Expand it to drop or pin the largest results.',
  system_tools: 'The definitions of Axon’s own tools (read, edit, shell, search and so on) that are sent with every request.',
  mcp_tools: 'Tool definitions from connected MCP servers that are loaded into every request. Tools behind ToolSearch are listed as deferred and cost nothing until fetched.',
  skills: 'The names and one-line descriptions of installed skills. A skill’s full text is loaded only when it is used.',
  system_prompt: 'Axon’s own instructions, the environment, git status and the repo map.',
  mcp_instructions: 'Usage notes that connected MCP servers ask to be included in the prompt.',
  custom_agents: 'The list of custom sub-agents Axon can start with the Task tool.',
  memory_files: 'Your AXON.md style instruction files and saved memories that were loaded at the start.',
  autocompact: 'Room held back so a long reply fits and Axon can compact the conversation before the window is full.',
  free: 'What is left before the conversation has to be compacted.',
};
const WB_EXP = new Set(); const WB_BUSY = {};
function wbPctTxt(v) { return (v || 0).toFixed(1) + '%'; }
function wbTok(n) { return n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'k' : String(Math.round(n || 0)); }
async function wbFetch(key, url, ttl, set, C) {
  const now = Date.now(); const rec = WB_BUSY[key] || (WB_BUSY[key] = { t: 0, on: false });
  if (rec.on || now - rec.t < ttl) return; rec.on = true; rec.t = now;
  try { set(await api(url)); } catch (e) { /* the section stays as it was */ } rec.on = false; renderRightSoon();
}
function wbRailData(C) {
  const st = C.st || {}; const busy = !!st.busy; const sig = (C.steps || []).length + ':' + busy + ':' + ((st.context || {}).used || 0);
  const sidq = encodeURIComponent(C.sid || '');
  if (C.sid) { const force = C.wbSig !== sig; C.wbSig = sig; if (force) { WB_BUSY.ctx = Object.assign(WB_BUSY.ctx || { on: false }, { t: 0 }); WB_BUSY.usage = Object.assign(WB_BUSY.usage || { on: false }, { t: 0 }); }   // a turn starting or ending also refreshes the spending
    wbFetch('ctx', '/api/workbench/context?session=' + sidq, busy ? 2500 : 20000, (d) => { WB.ctx = d.none ? null : d; const n = WB.nodes.memory; if (n && n.P.redraw) n.P.redraw(); }, C);
    wbFetch('usage', '/api/workbench/usage?session=' + sidq, 45000, (d) => { WB.lim = d; }, C); }
  wbFetch('conn', '/api/workbench/connectors', 20000, (d) => { WB.conn = d; }, C);
  wbFetch('chg', '/api/workbench/changes', busy ? 5000 : 15000, (d) => { WB_CHG = d; wbSyncGit(d); }, C);
  if (C.wbChgSig !== sig) { C.wbChgSig = sig; clearTimeout(C.wbT); C.wbT = setTimeout(() => wbRefreshChanges(true), 1200); for (const id of ['tasks', 'plan']) { const n = WB.nodes[id]; if (n && n.P.redraw && wbIsOpen(id)) n.P.redraw(); } }
}
function wbRail(C) {
  const r = document.getElementById('rpbody'); if (!r) return;
  wbRailData(C); wbFooterDraw(C);
  const st = C.st || {};
  const sec = (title, kids, right, cls) => h('section', { class: 'rs ' + (cls || '') }, h('h3', { class: 'rh' }, h('span', null, title, typeof helpTip === 'function' ? helpTip('progress:' + title) : null), right || null), ...[].concat(kids).filter(Boolean));
  const row = (icon, left, right, fn, title, cls) => { const e = h(fn ? 'button' : 'div', { class: 'rrow ' + (cls || ''), type: fn ? 'button' : null, title: title || '', onclick: fn || null }, icon ? wbIco(icon) : null, left, h('span', { style: 'flex:1' }), right || null); return e; };
  const kids = [];
  // Environment
  const ws = state.ws || {}; const path = (st.workspace || ws.workspace || '').replace(/^\/Users\/[^/]+/, '~');
  const git = ws.git || {};
  kids.push(sec('Environment', [
    row('computer', h('span', { class: 'rl' }, 'This computer'), h('span', { class: 'rr ' + (st.sandbox ? 'ok' : ''), title: st.sandbox ? 'Shell commands run inside the OS sandbox' : 'Shell commands are not sandboxed' }, st.sandbox ? 'Sandbox on' : 'Sandbox off')),
    row('folder', h('span', { class: 'rl' }, h('b', null, ws.name || 'workspace'), h('span', { class: 'muted rp' }, ' · ' + path)), null, () => folderChooser(), (st.workspace || ws.workspace || '') + '  (click to change folder)'),
    git.repo ? row('branch', h('span', { class: 'rl' }, git.branch || 'detached'), git.changed ? h('span', { class: 'rr muted' }, git.changed + ' changed') : null, (e) => wbBranchMenu(e.currentTarget), 'Branches and worktrees', 'branchrow') : null,
    st.external ? h('div', { class: 'rnote' }, 'Attached to the running terminal session.') : null]));
  // Context
  kids.push(wbContextSection(C, sec));
  // Usage
  kids.push(wbUsageSection(C, sec));
  // Plan, running tools, sub-agents, queue, background shells: only with content
  const running = C.feed ? [...C.feed.querySelectorAll('.live-tool')].filter(d => d.querySelector('.spin')) : [];
  const todos = st.todos || []; const done = todos.filter(t => t.status === 'completed').length;
  if (todos.length) kids.push(sec('Plan', h('div', null, h('div', { class: 'bar thin' }, h('i', { style: 'width:' + Math.round(100 * done / todos.length) + '%' })), ...todos.slice(0, 12).map(t => h('div', { class: 'todo ' + (t.status === 'completed' ? 'done' : t.status === 'in_progress' ? 'now' : '') }, h('span', null, t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '▶' : '○'), h('span', null, t.status === 'in_progress' && t.active ? t.active : t.content))), todos.length > 12 ? h('button', { class: 'plink', type: 'button', onclick: () => wbOpen('plan') }, '+' + (todos.length - 12) + ' more') : null), h('span', { class: 'muted' }, done + '/' + todos.length)));
  if (running.length) kids.push(sec('Running tools', running.map(d => h('div', { class: 'rrow static' }, h('span', { class: 'dot running' }), h('span', { class: 'rl' }, d.querySelector('.tn').textContent + ' ' + (d.querySelector('.ta').textContent || ''))))));
  const subs = C.subs || [];
  if (subs.length) kids.push(sec('Sub-agents', subs.map(s => h('a', { class: 'rrow', href: s.session_id ? '#/agent/' + encodeURIComponent(s.session_id) : '#/agents/' + encodeURIComponent(C.sid) }, h('span', { class: 'dot ' + (s.live ? 'running' : s.status) }), h('span', { class: 'rl' }, (s.type ? s.type + ' · ' : '') + s.title), h('span', { class: 'rr muted' }, s.live && s.max_steps ? s.steps + '/' + s.max_steps : (s.cost != null ? usd(s.cost) : '')))), h('a', { class: 'plink', href: '#/agents/' + encodeURIComponent(C.sid || '') }, 'open')));
  const queue = st.queue || [];
  if (queue.length) kids.push(sec('Queue', queue.map(q => h('div', { class: 'rrow static' }, h('span', { class: 'rl', title: q.text }, (q.steer ? '↪ ' : '') + q.text), h('button', { class: 'mini', onclick: async () => { await api('/api/chat/queue/remove', { session: C.sid, id: q.id }); refreshState(C, true); } }, 'Remove')))));
  const bg = st.background || [];
  if (bg.length) kids.push(sec('Background shells', bg.map(b => h('button', { class: 'rrow', type: 'button', title: b.command, onclick: () => wbOpen('tasks') }, h('span', { class: 'dot ' + (b.status === 'running' ? 'running' : 'finished') }), h('span', { class: 'rl' }, b.command), h('span', { class: 'rr muted' }, b.status === 'running' ? 'running' : 'done')))));
  kids.push(wbConnectorsSection(C, sec));
  kids.push(wbChangesSection(C, sec));
  kids.push(h('div', { class: 'rfoot' }, h('button', { class: 'rfid', type: 'button', title: 'Copy the session id', onclick: () => axCopy(C.sid || '') }, C.sid ? C.sid : 'New chat, nothing saved yet'), h('span', null, 'axon ' + ((state.info || {}).version || ''))));
  r.replaceChildren(...kids.filter(Boolean));
}
function wbContextSection(C, sec) {
  const st = C.st || {}; const d = WB.ctx; const real = st.context || {};
  if (!d) {
    const w = real.window; return sec('Context', w ? [h('div', { class: 'cx-head' }, h('b', null, real.pct + '% used'), h('span', { class: 'muted num' }, wbTok(real.used) + ' / ' + wbTok(w))), h('div', { class: 'cx-bar' }, h('i', { style: 'width:' + Math.min(100, real.pct) + '%;background:var(--c-msg)' }))]
      : [h('p', { class: 'rnote' }, C.sid ? 'Reading the context…' : 'Nothing in context yet. Send a message and the breakdown appears here.')]);
  }
  const used = real.used && real.used > d.used ? real.used : d.used; const pct = d.window ? Math.round(100 * used / d.window) : d.pct;
  const head = h('div', { class: 'cx-head', title: d.estimate }, h('b', null, pct + '% used'), h('span', { class: 'muted num' }, (d.calibrated || real.used ? '' : '~') + wbTok(used) + ' / ' + wbTok(d.window)));
  const bar = h('div', { class: 'cx-bar', role: 'img', 'aria-label': pct + '% of the context window is used' }, ...d.rows.filter(x => x.key !== 'free' && x.tokens > 0).map(x => h('i', { class: x.key === 'autocompact' ? 'buf' : '', style: 'width:' + Math.max(0.4, 100 * x.tokens / d.window) + '%;background:' + WB_CTX_COLORS[x.key], title: x.label + ': ' + wbTok(x.tokens) })));
  const expandable = { messages: 1, skills: 1, mcp_tools: 1, memory_files: 1, custom_agents: 1 };
  const ins = (key) => '#/context/' + key + (C.sid ? '?s=' + encodeURIComponent(C.sid) : '');
  const legend = d.rows.map(x => {
    const can = expandable[x.key] && (d.lists[x.key] || []).length; const open = can && WB_EXP.has(x.key);
    const peek = can ? h('button', { class: 'cx-peek', type: 'button', 'aria-expanded': String(!!open), 'aria-label': 'Quick peek at ' + x.label, title: 'Quick peek here without leaving the chat', onclick: () => { open ? WB_EXP.delete(x.key) : WB_EXP.add(x.key); wbRail(C); } }, '▶') : null;
    return h('div', { class: 'cx-row lnk', title: WB_CTX_HELP[x.key] + (x.key === 'messages' || x.key === 'free' || x.key === 'autocompact' ? '' : ' (estimate)') + '  Click to see exactly what is inside.' },
      h('span', { class: 'sq' + (x.key === 'free' ? ' free' : ''), style: x.key === 'free' ? '' : 'background:' + WB_CTX_COLORS[x.key] }),
      h('span', { class: 'cl' }, h('a', { class: 'cl', href: ins(x.key), 'aria-label': 'Open ' + x.label + ' in the context inspector, ' + wbTok(x.tokens) + ' tokens' }, x.label), can ? h('span', { class: 'cx-n' }, ' ' + (x.key === 'mcp_tools' ? d.lists.mcp_tools.reduce((a, g) => a + (g.loaded === undefined ? g.count : g.loaded), 0) : d.lists[x.key].length)) : null, peek),
      h('span', { class: 'ct num' }, wbTok(x.tokens)), h('span', { class: 'cp num' }, wbPctTxt(x.pct)));
  });
  const out = [head, bar, h('div', { class: 'cx-legend' }, ...legend)];
  if (d.deferred && d.deferred.mcp_count) out.push(h('div', { class: 'cx-row def', title: 'These MCP tools are listed to the model by name only (the names are inside the ToolSearch entry under System tools). The figure is what their definitions would add if all were loaded; each one is sent only when the model fetches it with ToolSearch.' }, h('span', { class: 'sq dim' }), h('span', { class: 'cl' }, 'MCP tools (deferred) ', h('span', { class: 'cx-n' }, d.deferred.mcp_count)), h('span', { class: 'ct num' }, wbTok(d.deferred.mcp_tools) + ' if loaded'), h('span', { class: 'cp num' }, '—')));
  out.push(h('div', { class: 'cx-more' }, h('a', { href: ins('permissions'), title: 'The mode, every allow, ask and deny rule, and the folders Axon may touch' }, 'Permissions'), h('a', { href: ins('hooks'), title: 'Commands that run on events such as before a tool call' }, 'Hooks'), h('a', { href: ins('overview') }, 'Everything')));
  for (const key of ['messages', 'mcp_tools', 'skills', 'memory_files', 'custom_agents']) {
    if (!WB_EXP.has(key) || !(d.lists[key] || []).length) continue;
    out.push(h('div', { class: 'cx-list' }, ...(key === 'messages' ? wbMsgItems(C, d.lists.messages) : key === 'mcp_tools' ? d.lists.mcp_tools.map(g => h('div', { class: 'cx-item', title: g.count + ' tools from ' + g.name }, h('span', { class: 'ci-n' }, g.name), h('span', { class: 'muted' }, g.count + ' tools' + (g.deferred ? ', ' + g.deferred + ' deferred' : '')), h('span', { class: 'num ci-t' }, '~' + wbTok(g.tokens))))
      : d.lists[key].map(i => h('div', { class: 'cx-item', title: i.name }, h('span', { class: 'ci-n' }, i.name), i.scope ? h('span', { class: 'muted' }, i.scope) : null, h('span', { class: 'num ci-t' }, '~' + wbTok(i.tokens)))))));
  }
  return sec('Context', out, h('a', { class: 'plink', href: ins('overview'), title: 'See every message, tool, memory file, rule and hook the model receives' }, 'open inspector'));
}
function wbMsgItems(C, items) {
  const post = async (name, id) => { try { await api('/api/insights/' + name, { session: C.sid, id: name === 'ctxmap_drop' ? id : undefined, target: id }); WB_BUSY.ctx.t = 0; wbRail(C); } catch (e) { toast(e.message); } };
  return [h('div', { class: 'cx-sub' }, 'Largest messages and tool results'), ...items.map(i => h('div', { class: 'cx-item', title: i.why }, h('span', { class: 'ci-n' }, i.label), h('span', { class: 'num ci-t' }, '~' + wbTok(i.tokens)),
    i.dropped ? h('span', { class: 'muted' }, 'dropped') : i.pinned ? h('button', { class: 'mini', type: 'button', onclick: () => post('unpin', i.id) }, 'Unpin') : null,
    i.droppable && i.tokens > 150 ? h('button', { class: 'mini', type: 'button', title: 'Replace with a short stub. The full text is saved to disk.', onclick: () => post('ctxmap_drop', i.id) }, 'Drop') : null,
    i.droppable ? h('button', { class: 'mini', type: 'button', title: 'Keep this in full, always', onclick: () => post('pin', i.id) }, 'Pin') : null))];
}
function wbUsageSection(C, sec) {
  const st = C.st || {}; const steps = C.steps || []; const lim = WB.lim || {};
  const tin = steps.reduce((a, s) => a + (s.tin || 0), 0), tout = steps.reduce((a, s) => a + (s.tout || 0), 0), cread = steps.reduce((a, s) => a + (s.cread || 0), 0);
  const cost = steps.reduce((a, s) => a + (s.cost || 0), 0), time = steps.reduce((a, s) => a + (s.dur || 0), 0);
  const kv = (k, v, t) => h('div', { class: 'kvr', title: t || '' }, h('span', { class: 'muted' }, k), h('span', { class: 'num' }, v));
  const out = [kv('This turn', steps.length ? usd(cost) + '  ·  ' + dur(time) + ' model time' : (st.busy ? 'starting' : (st.last && st.last.cost != null ? usd(st.last.cost) + ' (last)' : 'idle'))), kv('Session', usd(st.cost || 0))];
  if (steps.length) { out.push(kv('Tokens', 'in ' + wbTok(tin) + '  out ' + wbTok(tout) + '  cached ' + wbTok(cread), 'Input, output and cache-read tokens of this turn')); out.push(kv('Cache hit', tin ? Math.round(100 * cread / tin) + '%' : '—', 'Share of input served from the prompt cache')); }
  if (steps.length > 1) { const mx = Math.max(...steps.map(s => s.cost || 0), 0.0001); out.push(h('div', { class: 'steps', 'aria-label': 'Cost per model call' }, ...steps.slice(-28).map(s => h('i', { style: 'height:' + Math.max(8, 100 * (s.cost || 0) / mx) + '%' + (s.miss ? ';background:var(--warn)' : ''), title: '#' + s.n + '  ' + usd(s.cost || 0) + '  ' + dur(s.dur || 0) + (s.miss && (state.trace === 'detailed' || state.trace === 'debug') ? '  cache miss: ' + s.miss : '') })))); }
  out.push(...cfSpendRows(lim.spend, kv));
  for (const l of (lim.limits || [])) { const p = Math.round(100 * l.used); out.push(h('div', { class: 'lim', title: l.remaining != null ? l.remaining.toLocaleString() + ' of ' + (l.limit || 0).toLocaleString() + ' left' : '' }, h('div', { class: 'kvr' }, h('span', { class: 'muted' }, l.label), h('span', { class: 'num' }, p + '%')), h('div', { class: 'bar thin ' + (p > 90 ? 'bad' : p > 75 ? 'warn' : '') }, h('i', { style: 'width:' + Math.min(100, p) + '%' })), l.reset ? h('div', { class: 'muted lr' }, l.reset) : null)); }
  const b = lim.budget; if (b && b.session_usd) { const p = Math.min(100, Math.round(100 * (st.cost || 0) / b.session_usd)); out.push(h('div', { class: 'lim' }, h('div', { class: 'kvr' }, h('span', { class: 'muted' }, 'Session budget'), h('span', { class: 'num' }, usd(st.cost || 0) + ' of ' + usd(b.session_usd))), h('div', { class: 'bar thin ' + (p > 90 ? 'bad' : p > 75 ? 'warn' : '') }, h('i', { style: 'width:' + p + '%' })))); }
  return sec('Usage', out, h('a', { class: 'plink', href: '#/usage' }, 'details'));
}
// What a Reconnect click ended in, in words: the button alone looked the same after a failure as before the click.
function wbReconnectWord(name, r) {
  if (r.status === 'connected') return name + ' connected' + (r.tools != null ? ', ' + r.tools + ' tools' : '');
  if (r.status === 'failed') return name + ' did not connect: ' + (r.error || 'no reason given').slice(0, 160);
  if (r.status === 'needs-auth') return name + ' needs you to sign in. Use Authenticate.';
  return 'Reconnecting ' + name;
}
function wbConnectorsSection(C, sec) {
  const d = WB.conn; if (!d || !d.servers.length) return d ? sec('Connectors', h('div', { class: 'rnote' }, 'No MCP servers are set up. ', h('a', { href: '#/extensions' }, 'Add one'))) : null;
  const all = d.servers.slice().sort((a, b) => (b.needs_reconnect - a.needs_reconnect) || a.name.localeCompare(b.name));
  const showAll = WB_EXP.has('conn:all'); const list = showAll ? all : all.slice(0, 3);
  const tools = (name, sv) => { if (sv && sv.tool_names && sv.tool_names.length) return sv.tool_names.map(n => ({ name: n })); const g = ((WB.ctx && WB.ctx.lists.mcp_tools) || []).find(x => x.name.toLowerCase() === name.replace(/[^A-Za-z0-9_]/g, '_').toLowerCase()); return g ? g.tools : []; };
  const rows = list.map(s => {
    const open = WB_EXP.has('conn:' + s.name); const needsAuth = s.status === 'needs-auth';
    const act = s.needs_approval ? h('a', { class: 'cn-act warn', href: '#/extensions', title: 'This project server waits for your approval before it starts. Approve it on the Extensions page.', onclick: (e) => e.stopPropagation() }, 'Needs approval')
      : needsAuth ? h('button', { class: 'cn-act warn', type: 'button', onclick: (e) => { e.stopPropagation(); openInTerminal('/mcp'); } }, 'Authenticate')
      : s.status === 'disabled' ? h('span', { class: 'rr muted' }, 'Off') : s.needs_reconnect ? h('button', { class: 'cn-act warn', type: 'button', onclick: async (e) => { e.stopPropagation(); try { toast(wbReconnectWord(s.name, await api('/api/workbench/connector', { name: s.name, action: 'reconnect' }))); WB_BUSY.conn.t = 0; renderRightSoon(); } catch (er) { toast(er.message); } } }, 'Reconnect')
        : h('span', { class: 'rr muted' }, s.status === 'connecting' || s.status === 'reconnecting' ? 'connecting' : (s.tools != null ? s.tools + ' tools' : 'on'));
    const r = h('div', { class: 'cn' + (open ? ' open' : '') }, h('div', { class: 'cn-row', role: 'button', tabindex: '0', 'aria-expanded': String(open), title: s.error || s.status, onclick: () => { open ? WB_EXP.delete('conn:' + s.name) : WB_EXP.add('conn:' + s.name); wbRail(C); }, onkeydown: (e) => { if (e.key === 'Enter') e.currentTarget.click(); } },
      h('span', { class: 'badge-i' }, (s.name[0] || '?').toUpperCase()), h('span', { class: 'rl' }, s.name), act));
    if (open) {
      const q = h('input', { type: 'search', class: 'cn-q', placeholder: 'Search tools', 'aria-label': 'Search tools of ' + s.name }); const tl = h('div', { class: 'cn-tools' }); const ts = tools(s.name, s);
      const fill = () => { const v = q.value.toLowerCase(); const m = ts.filter(t => t.name.toLowerCase().includes(v)); tl.replaceChildren(...(m.slice(0, 60).map(t => h('div', { class: 'cn-tool' }, t.name, t.deferred ? h('span', { class: 'muted' }, ' deferred') : null)).concat(m.length > 60 ? [h('div', { class: 'muted' }, '+' + (m.length - 60) + ' more, refine the search')] : [])), ...(ts.length ? [] : [h('div', { class: 'muted' }, s.tools ? s.tools + ' tools. Names appear once the context is read.' : 'No tools loaded.')])); };
      q.addEventListener('input', fill); fill();
      const meta = [s.transport, s.scope + ' scope', s.managed ? 'managed by your organization' : '', s.timeout ? s.timeout + 's timeout' : ''].filter(Boolean).join(' · ');
      r.append(h('div', { class: 'cn-body' }, h('div', { class: 'muted cn-meta' }, meta), s.target ? h('code', { class: 'cn-target' }, s.target) : null, s.error ? h('div', { class: 'bad cn-err' }, s.error) : null, ts.length > 8 ? q : null, tl, h('div', { class: 'formrow' },
        h('button', { class: 'mini', type: 'button', onclick: async () => { try { await api('/api/workbench/connector', { name: s.name, action: s.status === 'disabled' ? 'enable' : 'disable' }); WB_BUSY.conn.t = 0; WB_BUSY.ctx.t = 0; renderRightSoon(); } catch (er) { toast(er.message); } } }, s.status === 'disabled' ? 'Enable' : 'Disable'),
        s.status !== 'disabled' ? h('button', { class: 'mini', type: 'button', onclick: async () => { try { toast(wbReconnectWord(s.name, await api('/api/workbench/connector', { name: s.name, action: 'reconnect' }))); WB_BUSY.conn.t = 0; renderRightSoon(); } catch (er) { toast(er.message); } } }, 'Reconnect') : null)));
    }
    return r;
  });
  const right = d.approval && !d.reconnect ? h('span', { class: 'rh-note warn' }, d.approval + ' to approve') : d.reconnect ? h('span', { class: 'rh-note warn' }, d.reconnect + ' to reconnect') : h('span', { class: 'rh-note' }, d.connected + ' connected');
  return sec('Connectors', [...rows, all.length > 3 ? h('button', { class: 'cn-more', type: 'button', onclick: () => { showAll ? WB_EXP.delete('conn:all') : WB_EXP.add('conn:all'); wbRail(C); } }, showAll ? 'Show less' : 'Show more (' + (all.length - 3) + ')') : null], right);
}
function wbChangesSection(C, sec) {
  const d = WB_CHG; const touched = C.touched || {}; const gitFiles = (d && d.files) || []; const inGit = new Set(gitFiles.map(f => f.path));
  const extra = Object.entries(touched).filter(([p]) => !inGit.has(p));
  const added = d ? d.added : 0, removed = d ? d.removed : 0; const n = gitFiles.length + extra.length;
  const out = [];
  if (!d || !d.repo) {
    if (!n) return sec('Changes', h('div', { class: 'rnote' }, d && !d.repo ? 'This folder is not a git repository. Files Axon edits in this chat are listed here.' : 'No changes yet.'));
    out.push(...extra.slice(0, 8).map(([p, c]) => h('a', { class: 'rrow', href: '#/files/' + encodeURIComponent(p), title: 'Changed by Axon in this chat' }, h('span', { class: 'g gbadge' }, c === 'U' ? '?' : c === 'A' ? '+' : c), h('span', { class: 'rl' }, p))));
    return sec('Changes', out);
  }
  out.push(h('div', { class: 'rrow chrow' }, h('button', { class: 'rrow in', type: 'button', title: 'Open the changes panel', onclick: () => wbOpen('changes') }, wbIco('changes'),
    n ? h('span', { class: 'rl' }, h('span', { class: 'cadd' }, '+' + added.toLocaleString()), ' ', h('span', { class: 'cdel' }, '−' + removed.toLocaleString()), h('span', { class: 'muted' }, '  ·  ' + n + ' file' + (n > 1 ? 's' : ''))) : h('span', { class: 'rl muted' }, 'Working tree is clean')),
    d.gh ? h('button', { class: 'btn pr-btn', type: 'button', title: 'Open a pull request for this branch with the GitHub CLI. You confirm before anything is pushed.', onclick: () => wbCreatePr(C) }, 'Create PR') : null));
  if (extra.length) out.push(h('div', { class: 'rnote' }, extra.length + ' file' + (extra.length > 1 ? 's' : '') + ' changed by Axon outside git.'));
  return sec('Changes', out, h('a', { class: 'plink', href: '#/insights/review' }, 'review hunks'));
}
async function wbCreatePr(C) {
  let p; try { p = await api('/api/workbench/pr'); } catch (e) { toast(e.message); return; }
  if (!p.repo) { toast('This folder is not a git repository.'); return; }
  if (!p.gh) { toast('The GitHub CLI (gh) is not installed.'); return; }
  if (p.base && p.branch === p.base) { toast('You are on ' + p.base + ', the branch a pull request would go into. Switch to another branch first.'); return; }
  const title = h('input', { type: 'text', class: 'search', placeholder: 'Leave empty to use the commit messages', maxlength: 200, 'aria-label': 'Pull request title' });
  const push = h('input', { type: 'checkbox', id: 'pr-push' });
  const lines = [h('p', { class: 'muted' }, 'Branch ', h('b', null, p.branch), p.base ? ' into ' : '', p.base ? h('b', null, p.base) : null, '. ', p.dirty ? p.dirty + ' uncommitted change' + (p.dirty > 1 ? 's are' : ' is') + ' not part of it: commit them first.' : ''), title];
  if (p.needs_push) lines.push(h('label', { class: 'pushrow' }, push, h('span', null, p.upstream ? 'Push ' + p.ahead + ' unpushed commit' + (p.ahead > 1 ? 's' : '') + ' to ' + p.upstream : 'Push ' + p.branch + ' to origin (it has no upstream yet)')));
  const ok = await wbConfirm('Create a pull request?', lines, 'Create pull request', false); if (!ok) return;
  if (p.needs_push && !push.checked) { toast('Nothing was pushed or created: the branch has to be pushed first, and you did not confirm that.'); return; }
  try { const r = await api('/api/workbench/pr', { confirm: true, push: push.checked, title: title.value }); toast('Pull request created'); if (r.url) window.open(r.url, '_blank', 'noopener,noreferrer'); } catch (e) { toast(e.message); }
}
// ---------- composer footer: model, effort, mode, context ring ----------
function wbFooter(C) {
  const mk = (cls, label) => h('button', { class: 'fw ' + cls, type: 'button', 'aria-haspopup': 'menu', title: label }, '');
  const model = mk('fw-model', 'Model'), effort = mk('fw-effort', 'How much the model thinks before it answers'), mode = mk('fw-mode', 'What Axon may do without asking');
  const ring = h('span', { class: 'ring', role: 'img' });
  const root = h('div', { class: 'cfoot' }, model, effort, mode, typeof helpTip === 'function' ? helpTip('mode') : null, h('span', { style: 'flex:1' }), ring);
  C.foot = { root, model, effort, mode, ring };
  model.addEventListener('click', async () => {
    let list = []; try { list = (await api('/api/models?session=' + encodeURIComponent(C.sid))).models; } catch (e) { toast(e.message); return; }
    const cur = (C.st && C.st.model) || '';
    wbMenu(model, [{ head: 'Model' }, ...list.slice(0, 40).map(m => ({ label: m, on: m === cur, run: async () => { try { await runCmd(C, '/model ' + m); toast('Model: ' + m); } catch (e) { toast(e.message); } } })), '-', { label: 'Set up another provider…', run: () => { location.hash = '#/settings/providers'; } }], { up: true });
  });
  effort.addEventListener('click', () => { const cur = (C.st && C.st.effort) || ''; wbMenu(effort, [{ head: 'Effort' }, ...[['reflex', 'Low'], ['balanced', 'Medium'], ['synapse', 'High'], ['quantum', 'Highest']].map(([v, l]) => ({ label: l, on: effortWord(cur).startsWith(l), run: async () => { try { await runCmd(C, '/effort ' + v); toast('Effort: ' + l); } catch (e) { toast(e.message); } } }))], { up: true }); });
  mode.addEventListener('click', () => { const cur = (C.st && C.st.mode) || 'default';
    wbMenu(mode, [{ head: 'Permission mode' }, ...WB_MODES.map(([v, w, d]) => ({ label: w, hint: d, on: v === cur, cls: 'mode-' + v, run: async () => { try { if (!(await confirmBypass(C, v))) return; await runCmd(C, '/mode ' + v); toast('Mode: ' + w); } catch (e) { toast(e.message); } } }))], { up: true }); });
  return root;
}
// Choosing "Runs without asking" takes one confirmation per chat; every other mode switches at once.
const BYPASS_OK = new Set();
function confirmBypass(C, mode) {
  const key = (C && C.sid) || 'new-chat';
  if (mode !== 'bypass' || BYPASS_OK.has(key) || ((C && C.st && C.st.mode) || 'default') === 'bypass') return Promise.resolve(true);
  return new Promise((resolve) => {
    const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Run without asking' });
    const done = (yes) => { scrim.remove(); if (yes) BYPASS_OK.add(key); resolve(yes); };
    const no = h('button', { class: 'btn primary', type: 'button', onclick: () => done(false) }, 'Keep asking first');
    const yes = h('button', { class: 'btn', type: 'button', onclick: () => done(true) }, 'Run without asking in this chat');
    scrim.addEventListener('click', (e) => { if (e.target === scrim) done(false); });
    scrim.addEventListener('keydown', (e) => { if (e.key === 'Escape') done(false); });
    scrim.append(h('div', { class: 'modal' }, h('h3', null, 'Run without asking?'),
      h('p', { style: 'color:var(--bad)' }, 'Axon will edit files and run commands in this chat without asking you first. Rules that deny a command still apply.'),
      h('div', { class: 'acts' }, no, yes)));
    document.body.append(scrim); no.focus();
  });
}
const WB_MODES = [['default', 'Asks first', 'Ask before edits and commands'], ['acceptEdits', 'Edits allowed', 'Allow file edits, ask for commands'], ['plan', 'Plan only', 'Read-only, writes a plan'], ['bypass', 'Runs without asking', 'No approval prompts']];
function wbFooterDraw(C) {
  const f = C.foot; if (!f) return; const st = C.st || {};
  const info = state.info || {}; const us = UI.status || {}; f.model.textContent = st.model || us.model || info.model || 'model'; f.effort.textContent = effortWord(st.effort || us.effort || info.effort); const m = WB_MODES.find(x => x[0] === (st.mode || 'default')) || (st.mode === 'dontAsk' ? ['dontAsk', 'Never asks', 'Denies anything that would ask. Set in settings.json or with --permission-mode.'] : [st.mode, st.mode, '']);   // a mode the menu does not offer is still named truthfully
  f.mode.textContent = m[1]; f.mode.className = 'fw fw-mode mode-' + m[0]; f.mode.title = m[2];
  const c = st.context || {}; const p = Math.min(100, Math.round(c.pct || 0));
  f.ring.title = c.window ? 'Context: ' + p + '% used, ' + (c.used || 0).toLocaleString() + ' of ' + c.window.toLocaleString() + ' tokens' : 'Context window';
  f.ring.setAttribute('aria-label', f.ring.title);
  f.ring.textContent = c.window ? p + '%' : ''; f.ring.hidden = !c.window; f.ring.className = 'ring' + (p > 90 ? ' bad' : p > 75 ? ' warn' : '');   // context in use, plainly: a tiny arc read as a stray spinner
}
// ---------- the + menu and dictation ----------
function wbPlus(C, ta) {
  const file = h('input', { type: 'file', multiple: true, hidden: true, 'aria-label': 'Attach files' }); const img = h('input', { type: 'file', multiple: true, accept: 'image/*', hidden: true, 'aria-label': 'Attach images' });
  const onPick = (inp) => () => { const fs = [...inp.files]; inp.value = ''; if (C.upload) fs.forEach(f => C.upload(f)); };
  file.addEventListener('change', onPick(file)); img.addEventListener('change', onPick(img));
  const btn = h('button', { class: 'cplus', type: 'button', title: 'Attach', 'aria-label': 'Attach', 'aria-haspopup': 'menu', onclick: (e) => wbMenu(e.currentTarget, [{ label: 'Attach a file', run: () => file.click() }, { label: 'Attach an image', run: () => img.click() }, { label: 'Add a project file (@)', run: () => { ta.value += (ta.value && !/\s$/.test(ta.value) ? ' ' : '') + '@'; ta.focus(); ta.dispatchEvent(new Event('input')); } }, { label: 'Saved prompts (#)', run: () => axPromptLibrary(C, ta) }], { up: true }) }, wbIco('plus'));
  return h('span', { class: 'cplus-wrap' }, btn, file, img);
}
function wbMic(C, ta) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition; if (!SR) return null;
  let rec = null, on = false;
  const b = h('button', { class: 'cmic', type: 'button', title: 'Dictate. Speech is sent to your browser’s speech service (in Chrome, to Google) to be turned into text; Axon never receives audio.', 'aria-label': 'Dictate', 'aria-pressed': 'false' }, wbIco('mic'));
  const stop = () => { on = false; b.setAttribute('aria-pressed', 'false'); try { rec && rec.stop(); } catch (e) { /* idle */ } };
  b.addEventListener('click', () => {
    if (on) { stop(); return; }
    if (!lsGet('axon-speech-note')) { lsSet('axon-speech-note', '1'); toast('Dictation uses your browser’s speech service, so your speech leaves this computer. Click the microphone again to stop.'); }
    rec = new SR(); rec.interimResults = true; rec.continuous = true; rec.lang = navigator.language || 'en-US'; const base = ta.value + (ta.value && !/\s$/.test(ta.value) ? ' ' : '');
    rec.onresult = (e) => { let t = ''; for (const r of e.results) t += r[0].transcript; ta.value = base + t; ta.dispatchEvent(new Event('input')); };
    rec.onerror = (e) => { toast(e.error === 'not-allowed' ? 'The browser did not allow the microphone.' : 'Dictation stopped: ' + e.error); stop(); }; rec.onend = () => stop();
    try { rec.start(); on = true; b.setAttribute('aria-pressed', 'true'); } catch (e) { toast(e.message); }
  });
  return b;
}
"""
