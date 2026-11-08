"""Client script: the IDE frame (rail, resizable side panels, tabs and splits, bottom panel, status bar)."""

JS_FRAME = r"""
// ---------- IDE frame ----------
const UI = { tabs: [], active: null, split: null, building: null, bg: false, built: false, left: 320, right: 340, bottom: 300,
  leftOpen: true, rightOpen: true, bottomOpen: false, leftTab: 'files', chat: null, status: {} };
const TAB_ICON = { chat: 'chat', files: 'file', agents: 'sessions', agent: 'sessions', sessions: 'sessions', usage: 'usage', commands: 'commands', memory: 'memory', extensions: 'extensions', settings: 'settings', shell: 'shell', terminal: 'terminal', insights: 'usage' };
const PAGE_TITLE = { chat: 'Chat', sessions: 'Sessions', usage: 'Usage', commands: 'Commands', memory: 'Memory', extensions: 'Extensions', settings: 'Settings', agents: 'Subagents', insights: 'Insights' };
// Plain words for the reasoning-effort tiers; the raw tier name is shown only at the detailed trace levels.
const EFFORT_WORD = { reflex: 'Low', low: 'Low', balanced: 'Medium', medium: 'Medium', synapse: 'High', high: 'High', quantum: 'Highest', xhigh: 'Highest', max: 'Highest', hyper: 'Highest' };
function effortWord(e) { if (!e) return '—'; const w = EFFORT_WORD[e] || e; return (state.trace === 'detailed' || state.trace === 'debug') && w !== e ? w + ' (' + e + ')' : w; }
function prefKey() { return 'axon-ui:v2:' + ((state.ws || {}).workspace || ''); }
function loadPrefs() { try { return JSON.parse(lsGet(prefKey()) || '{}'); } catch (e) { return {}; } }
function savePrefs() {
  // At phone width the panels are sheets that always start closed; keep the desktop choice for when the window is wide again.
  const keep = innerWidth <= 900 ? loadPrefs() : {}; const open = (k) => (typeof keep[k] === 'boolean' ? keep[k] : UI[k]);
  lsSet(prefKey(), JSON.stringify({ left: UI.left, right: UI.right, bottom: UI.bottom, leftOpen: open('leftOpen'), rightOpen: open('rightOpen'), bottomOpen: open('bottomOpen'), leftTab: UI.leftTab,
    tabs: UI.tabs.filter(t => t.hash).map(t => t.hash).slice(0, 12), active: UI.active ? UI.active.hash : '' }));
}
// Widths of Progress and the side panels. The chat keeps at least 340px: when the window is too narrow the wide panel shrinks first, then the narrow one, then Progress.
function rightLayout() {
  let prog = UI.rightOpen ? UI.right : 0, wn = WB.narrow.length ? WB.wN : 0, ww = WB.wide.length ? WB.wW : 0;
  if (innerWidth <= 900) return { prog, wn, ww };
  if (WB.max) return { prog: 0, wn: 0, ww: 0 };
  let over = prog + wn + ww - Math.max(0, innerWidth - 64 - (UI.leftOpen ? UI.left : 0) - 340);
  const shrink = (v, min) => { const d = Math.min(Math.max(0, over), Math.max(0, v - min)); over -= d; return v - d; };
  if (over > 0 && ww) ww = shrink(ww, 340); if (over > 0 && wn) wn = shrink(wn, 220); if (over > 0 && prog) prog = shrink(prog, 220);
  return { prog, wn, ww };
}
function applySizes() {
  const body = document.getElementById('ide-body'); if (!body) return;
  body.style.setProperty('--lw', UI.leftOpen ? UI.left + 'px' : '0px');
  const L = rightLayout(); const wbw = L.wn + L.ww; const ext = L.prog + wbw;
  body.style.setProperty('--rw', ext + 'px'); body.style.setProperty('--wbw', wbw + 'px'); body.style.setProperty('--wn', L.wn + 'px'); body.style.setProperty('--ww', L.ww + 'px');
  const wrap = document.getElementById('rwrap'); if (wrap) wrap.classList.toggle('no-prog', !UI.rightOpen);
  body.classList.toggle('no-left', !UI.leftOpen); body.classList.toggle('no-right', ext === 0 && !UI.rightOpen);
  const c = document.getElementById('center'); if (c) { c.style.setProperty('--bh', UI.bottomOpen ? UI.bottom + 'px' : '0px'); c.classList.toggle('no-bottom', !UI.bottomOpen); }
}
function togglePanel(which, force) {
  const k = which + 'Open'; UI[k] = force != null ? force : !UI[k]; applySizes(); savePrefs();
  document.body.classList.toggle('drawer-left', which === 'left' && UI.leftOpen && innerWidth <= 900);
  document.body.classList.toggle('drawer-right', which === 'right' && UI.rightOpen && innerWidth <= 900);
  document.body.classList.toggle('dock-open', !!UI.bottomOpen);
  if (which === 'bottom' && UI.bottomOpen) ensureBottom();
  syncToggles();
}
function syncToggles() { document.querySelectorAll('[data-toggle]').forEach(b => b.setAttribute('aria-pressed', String(!!UI[b.dataset.toggle + 'Open']))); if (typeof wbSync === 'function') wbSync(); }
function dragResizer(el, axis, get, set) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault(); el.setPointerCapture(e.pointerId); const start = axis === 'x' ? e.clientX : e.clientY, base = get(); document.body.classList.add('resizing');
    const move = (ev) => { set(base, (axis === 'x' ? ev.clientX : ev.clientY) - start); applySizes(); };
    const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); document.body.classList.remove('resizing'); savePrefs(); };
    el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
  });
  el.addEventListener('dblclick', () => { if (axis === 'x') { UI[get.side] = get.def; } applySizes(); savePrefs(); });
}
function ensureFrame() {
  if (UI.built) return;
  UI.built = true;
  const p = loadPrefs(); for (const k of ['left', 'right', 'bottom']) if (typeof p[k] === 'number') UI[k] = Math.max(160, Math.min(900, p[k]));
  for (const k of ['leftOpen', 'rightOpen', 'bottomOpen']) if (typeof p[k] === 'boolean') UI[k] = p[k];
  if (p.leftTab) UI.leftTab = p.leftTab; UI.restore = p;
  if (innerWidth <= 900) { UI.leftOpen = false; UI.rightOpen = false; UI.bottomOpen = false; }
  const ws = state.ws || {};
  const logo = h('span', { html: state.logo || '' });
  const nav = h('nav', { 'aria-label': 'Sections', class: 'side-nav' }, ...NAV.map(([id, label, keys]) => h('a', { href: id === 'files' ? '#/files' : '#/' + id, 'data-nav': id, title: label + (NAV_HELP[id] ? ': ' + NAV_HELP[id] : '') + '  (' + keys + ')', onclick: (e) => { if (id === 'files') { e.preventDefault(); UI.leftTab = 'files'; togglePanel('left', true); drawLeftTabs(); } else if (id === 'shell') { e.preventDefault(); togglePanel('bottom', true); } } }, svgIcon(ICONS[id]), h('span', { class: 'lbl' }, label))));
  const chip = h('a', { class: 'ws-chip', href: '#/files', id: 'ws-chip', title: ws.workspace || '', onclick: (e) => { e.preventDefault(); folderChooser(); } });
  const top = h('header', { class: 'top' }, h('button', { class: 'tbi sidebtn', type: 'button', 'data-toggle': 'left', title: 'Show or hide the left panel (files)', 'aria-label': 'Left panel', onclick: () => togglePanel('left') }, svgIcon(WB_ICON.left)),
    h('a', { class: 'brand', href: '#/chat' }, logo, h('span', null, 'axon')), chip,
    h('button', { class: 'ctitle', id: 'chat-title', type: 'button', 'aria-haspopup': 'menu', onclick: (e) => wbTitleMenu(e.currentTarget) }),
    wbToolbar());
  const lp = h('aside', { class: 'lpanel', id: 'lpanel' }, h('div', { class: 'ptabs', id: 'lptabs' }), h('div', { class: 'pbody', id: 'lpbody' }));
  const rp = h('aside', { class: 'rpanel', id: 'rpanel' }, h('div', { class: 'pbody', id: 'rpbody' }));
  const rwrap = h('div', { class: 'rwrap', id: 'rwrap' }, rp, wbBuild());
  const rzl = h('div', { class: 'rz rz-l', role: 'separator', 'aria-orientation': 'vertical', title: 'Drag to resize' }), rzr = h('div', { class: 'rz rz-r', role: 'separator', 'aria-orientation': 'vertical', title: 'Drag to resize' });
  const tabscroll = h('div', { class: 'tabscroll', id: 'tabscroll' });
  tabscroll.addEventListener('wheel', (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { tabscroll.scrollLeft += e.deltaY; e.preventDefault(); } }, { passive: false });
  tabscroll.addEventListener('scroll', () => tabArrows());
  const tabstrip = h('div', { class: 'tabstrip', id: 'tabstrip' },
    h('button', { class: 'mini tsl', id: 'tab-l', type: 'button', 'aria-label': 'Scroll tabs left', hidden: true, onclick: () => { tabscroll.scrollBy({ left: -240 }); } }, '‹'), tabscroll,
    h('button', { class: 'mini tsr', id: 'tab-r', type: 'button', 'aria-label': 'Scroll tabs right', hidden: true, onclick: () => { tabscroll.scrollBy({ left: 240 }); } }, '›'),
    h('button', { class: 'mini', id: 'tab-menu', type: 'button', 'aria-haspopup': 'menu', title: 'All open tabs, and close the others', onclick: (e) => openTabMenu(e.currentTarget, null) }),
    h('button', { class: 'mini', id: 'tab-split', type: 'button', title: 'Show the previous tab beside this one', onclick: () => { if (UI.split) { UI.split = null; } else { const other = UI.tabs.filter(x => x !== UI.active).pop(); UI.split = other || null; } if (UI.active) setActive(UI.active); } }));
  const panes = h('div', { class: 'panes', id: 'panes' });
  const rzb = h('div', { class: 'rz rz-b', role: 'separator', 'aria-orientation': 'horizontal', title: 'Drag to resize' });
  const bottom = h('div', { class: 'bpanel', id: 'bpanel' }, h('div', { class: 'phead' }, h('b', null, 'Shell'), h('span', { class: 'muted hint' }, NAV_HELP.shell), h('span', { style: 'flex:1' }), h('button', { class: 'mini', onclick: () => togglePanel('bottom', false) }, 'Close')), h('div', { class: 'pbody', id: 'bpbody' }));
  const center = h('div', { class: 'center', id: 'center' }, tabstrip, panes, rzb, bottom);
  const body = h('div', { class: 'ide-body', id: 'ide-body' }, nav, lp, rzl, center, rzr, rwrap);
  const status = h('footer', { class: 'statusbar', id: 'statusbar' });
  const scrim = h('div', { class: 'drawer-scrim', onclick: () => { document.body.classList.remove('drawer-left', 'drawer-right', 'dock-open'); UI.leftOpen = UI.rightOpen = false; if (innerWidth <= 900) UI.bottomOpen = false; wbCloseAll(); applySizes(); syncToggles(); } });
  app.replaceChildren(h('div', { class: 'ide' }, top, body, status), scrim);
  dragResizer(rzl, 'x', Object.assign(() => UI.left, { side: 'left', def: 320 }), (b, d) => { UI.left = Math.max(180, Math.min(760, b + d)); });
  dragResizer(rzr, 'x', Object.assign(() => (UI.rightOpen ? UI.right : WB.narrow.length ? WB.wN : WB.wW), { side: 'right', def: 340 }), (b, d) => { const v = Math.max(220, Math.min(1000, b - d)); if (UI.rightOpen) UI.right = v; else if (WB.narrow.length) WB.wN = Math.min(720, v); else WB.wW = v; });
  dragResizer(rzb, 'y', Object.assign(() => UI.bottom, { side: 'bottom', def: 300 }), (b, d) => { UI.bottom = Math.max(120, Math.min(innerHeight - 200, b - d)); });
  applySizes(); syncToggles(); drawLeftTabs(); drawStatus(); renderRight(); drawChip(); wbRender(); wbTitleSync(); document.body.classList.toggle('dock-open', !!UI.bottomOpen);
  if (UI.bottomOpen) ensureBottom();
  window.addEventListener('resize', () => { tabArrows(); const small = innerWidth <= 900; const was = document.body.classList.contains('small'); document.body.classList.toggle('small', small);
    if (small && !was) { UI.leftOpen = UI.rightOpen = UI.bottomOpen = false; document.body.classList.remove('drawer-left', 'drawer-right', 'dock-open', 'drawer-wb'); applySizes(); syncToggles(); } });
  document.body.classList.toggle('small', innerWidth <= 900);
}
function drawChip() {
  const ws = state.ws || {}; const chip = document.getElementById('ws-chip'); if (!chip) return;
  chip.title = (ws.workspace || '') + '  (click to change folder)';
  chip.replaceChildren(h('span', { class: 'ws-dot' }), h('b', null, ws.name || 'workspace'), h('span', { class: 'ws-path' }, (ws.workspace || '').replace(/^\/Users\/[^/]+/, '~')), ws.git && ws.git.repo ? h('span', { class: 'ws-git' }, '⎇ ' + ws.git.branch + (ws.git.changed ? ' · ' + ws.git.changed + ' changed' : '')) : null);
}
function drawStatus() {
  const el = document.getElementById('statusbar'); if (!el) return; const s = UI.status || {}; const ws = state.ws || {}; const info = state.info || {};
  const item = (label, val, cls, title) => h('span', { class: 'sb ' + (cls || ''), title: title || '' }, label ? h('span', { class: 'muted' }, label + ' ') : null, h('b', null, val));
  el.replaceChildren(item('', s.busy ? '● working' : '○ idle', s.busy ? 'live' : ''), item('model', s.model || info.model || '—'), item('mode', s.mode || info.mode || '—', '', 'What Axon may do without asking'),
    !(s.context && s.context.window) ? null : item('context', s.context.pct + '%', s.context && s.context.pct > 90 ? 'bad' : s.context && s.context.pct > 75 ? 'warn' : '', s.context && s.context.window ? s.context.used.toLocaleString() + ' of ' + s.context.window.toLocaleString() + ' tokens' : ''),
    s.cost == null ? null : item('cost', usd(s.cost)), item('trace', state.trace), item('effort', effortWord(s.effort || info.effort), '', 'How much thinking the model does before it answers'),
    h('span', { class: 'sp', style: 'flex:1' }), s.external ? item('', 'terminal session') : null, s.sandbox ? item('', 'sandbox on') : null,
    ws.git && ws.git.repo && ws.git.branch ? item('git', ws.git.branch) : null, item('', 'axon ' + ((state.info || {}).version || '')));
}
function setStatus(st) { UI.status = st || {}; drawStatus(); }

// ----- tabs -----
function closeOthers(keep) {
  if (!keep) return;
  location.hash = keep.hash || '#/chat'; setActive(keep);
  for (const x of UI.tabs.filter(t => t !== keep)) closeTab(x);
}
function tabArrows() {
  const sc = document.getElementById('tabscroll'); if (!sc) return;
  const over = sc.scrollWidth > sc.clientWidth + 2;
  const l = document.getElementById('tab-l'), r = document.getElementById('tab-r');
  if (l) l.hidden = !over || sc.scrollLeft < 2; if (r) r.hidden = !over || sc.scrollLeft + sc.clientWidth >= sc.scrollWidth - 2;
}
function closeMenu() { document.getElementById('tab-pop')?.remove(); }
// anchor: the button or tab the menu hangs from; only: a single tab (right-click) or null for the full list
function openTabMenu(anchor, only) {
  if (document.getElementById('tab-pop')) { closeMenu(); return; }
  const r = anchor.getBoundingClientRect();
  const pop = h('div', { class: 'tab-pop', id: 'tab-pop', role: 'menu', style: 'top:' + (r.bottom + 2) + 'px;right:' + Math.max(8, innerWidth - r.right) + 'px' });
  const item = (label, fn, cls) => h('button', { class: 'tmi ' + (cls || ''), type: 'button', role: 'menuitem', onclick: () => { closeMenu(); fn(); } }, label);
  if (!only) for (const t of UI.tabs) pop.append(h('div', { class: 'tirow' + (t === UI.active ? ' on' : '') },
    h('button', { class: 'tmi', type: 'button', role: 'menuitem', onclick: () => { closeMenu(); location.hash = t.hash || '#/chat'; setActive(t); } }, svgIcon(ICONS[TAB_ICON[t.route]] || ICONS.files), h('span', null, t.title)),
    h('button', { class: 'tx', type: 'button', 'aria-label': 'Close ' + t.title, onclick: (e) => { e.stopPropagation(); closeTab(t); closeMenu(); if (UI.tabs.length > 1) openTabMenu(anchor, null); } }, '×')));
  const target = only || UI.active;
  if (target) { pop.append(h('hr')); pop.append(item('Close this tab', () => closeTab(target))); if (UI.tabs.length > 1) pop.append(item('Close the other ' + (UI.tabs.length - 1) + ' tabs', () => closeOthers(target))); }
  document.body.append(pop);
  setTimeout(() => { const off = (e) => { if (!pop.contains(e.target)) { closeMenu(); document.removeEventListener('mousedown', off, true); document.removeEventListener('keydown', esc, true); } }; const esc = (e) => { if (e.key === 'Escape') { closeMenu(); document.removeEventListener('mousedown', off, true); document.removeEventListener('keydown', esc, true); } }; document.addEventListener('mousedown', off, true); document.addEventListener('keydown', esc, true); }, 0);
}
const SINGLE_TAB = ['settings', 'agents', 'insights', 'commands', 'usage', 'sessions', 'memory', 'extensions'];
function labelFor(route) { return PAGE_TITLE[route] || route; }
function newTab(key, title, route, hash, persist) {
  const wide = route === 'chat' || route === 'files' || route === 'agent';
  const pane = h('div', { class: 'pane' + (wide ? ' wide' : ''), 'data-key': key });
  const t = { key, title, route, hash, pane, cleanup: [], persist, wide };
  UI.tabs.push(t); document.getElementById('panes').append(pane); return t;
}
function closeTab(t) {
  for (const fn of t.cleanup.splice(0)) { try { fn(); } catch (e) { /* gone */ } }
  t.pane.remove(); UI.tabs = UI.tabs.filter(x => x !== t); if (UI.split === t) UI.split = null;
  if (UI.chat && UI.chat.tab === t) UI.chat = null;
  if (UI.active === t) { const next = UI.tabs[UI.tabs.length - 1]; if (next) { location.hash = next.hash || '#/chat'; setActive(next); } else { UI.active = null; location.hash = '#/chat'; } }
  drawTabs(); savePrefs();
}
function setActive(t) {
  UI.active = t; drawTabs();
  document.querySelectorAll('.pane').forEach(p => { const on = p === t.pane || (UI.split && p === UI.split.pane && UI.split !== t); p.classList.toggle('on', on); });
  document.getElementById('panes').classList.toggle('split', !!(UI.split && UI.split !== t));
  document.querySelectorAll('.side-nav a').forEach(a => a.removeAttribute('aria-current'));
  const nav = document.querySelector('.side-nav a[data-nav="' + (t.route === 'agent' || t.route === 'agents' ? 'chat' : t.route) + '"]'); if (nav) nav.setAttribute('aria-current', 'page');
  if (t.route === 'chat' && t.C) { UI.chat = t.C; setStatus(t.C.st); renderRight(); if (t.C.checkReady) t.C.checkReady(); }
  wbTitleSync(); savePrefs();
}
function drawTabs() {
  const sc = document.getElementById('tabscroll'); if (!sc) return;
  sc.replaceChildren(...UI.tabs.map(t => {
    const el = h('div', { class: 'tab' + (t === UI.active ? ' on' : '') + (UI.split === t ? ' split' : ''), role: 'tab', 'aria-selected': String(t === UI.active), title: t.title },
      h('button', { class: 'tl', type: 'button', onclick: () => { location.hash = t.hash || '#/chat'; setActive(t); } }, svgIcon(ICONS[TAB_ICON[t.route]] || ICONS.files), h('span', null, t.title)),
      h('button', { class: 'tx', type: 'button', 'aria-label': 'Close ' + t.title, onclick: (e) => { e.stopPropagation(); closeTab(t); } }, '×'));
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); openTabMenu(el, t); });
    el.addEventListener('auxclick', (e) => { if (e.button === 1) { e.preventDefault(); closeTab(t); } });
    return el;
  }));
  const m = document.getElementById('tab-menu'); if (m) m.textContent = 'Tabs (' + UI.tabs.length + ')';
  const sp = document.getElementById('tab-split'); if (sp) sp.textContent = UI.split ? 'Unsplit' : 'Split';
  queueMicrotask(() => { const on = sc.querySelector('.tab.on'); if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest', inline: 'nearest' }); tabArrows(); });
}
function setTabTitle(t, title) { if (t) { t.title = title; drawTabs(); } }
// Every page runs through here: it makes (or reuses) a tab and gives the page its pane.
function shell(route, bodyFn, opts) {
  ensureFrame();
  const o = opts || {};
  const key = SINGLE_TAB.includes(route) ? route : (o.key || route);   // pages with sub-pages keep one tab
  let t = UI.tabs.find(x => x.key === key); const fresh = !t;
  if (fresh) t = newTab(key, o.title || labelFor(route), route, o.hash || ('#/' + route), !!o.persist);
  else if (SINGLE_TAB.includes(route)) { t.hash = o.hash || t.hash; t.title = o.title || t.title; }
  if (!UI.bg) setActive(t); else drawTabs();
  if (!fresh && o.persist) return Promise.resolve();
  for (const fn of t.cleanup.splice(0)) { try { fn(); } catch (e) { /* gone */ } }
  t.pane.replaceChildren(); UI.building = t; t.pane.classList.toggle('wide', !!o.wide);
  const r = bodyFn(t.pane, t);
  return Promise.resolve(r).then(() => {
    if (PAGE_HINT[route] && !t.pane.querySelector(':scope > .page-hint')) t.pane.prepend(h('div', { class: 'page-hint' }, h('b', null, labelFor(route) + ': '), PAGE_HINT[route]));
    savePrefs();
  });
}
function onLeave(fn) { if (UI.building) UI.building.cleanup.push(fn); }
function leave() { /* tabs keep their own cleanups; closing a tab runs them */ }
function currentTab() { return UI.building; }

// ----- bottom panel (shell) -----
function ensureBottom() {
  const body = document.getElementById('bpbody'); if (!body || body.dataset.ready) return; body.dataset.ready = '1';
  UI.building = { cleanup: [], pane: body, route: 'shell' }; UI.bottomTab = UI.building;
  mountShell(body);
}
// ----- left panel: files tree and search -----
function drawLeftTabs() {
  const tabs = document.getElementById('lptabs'); if (!tabs) return;
  tabs.replaceChildren(...[['files', 'Files'], ['search', 'Search']].map(([k, l]) => h('button', { type: 'button', 'aria-selected': String(UI.leftTab === k), onclick: () => { UI.leftTab = k; drawLeftTabs(); savePrefs(); } }, l)), h('span', { style: 'flex:1' }), h('button', { class: 'mini', onclick: () => togglePanel('left', false) }, 'Hide'));
  const body = document.getElementById('lpbody'); body.replaceChildren();
  if (UI.leftTab === 'files') mountTree(body); else mountSearch(body);
}
// ----- right panel: live progress -----
function renderRight() {
  const b = document.getElementById('rpbody'); if (!b) return;
  if (UI.chat) { drawRail(UI.chat); return; }
  // no chat open (a settings page, say): the panel still shows the folder, the connectors and the changes
  wbRail({ st: {}, steps: [], subs: [], touched: {}, sid: '' });
}
"""
