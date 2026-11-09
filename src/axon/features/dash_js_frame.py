"""Client script: the IDE frame (rail, resizable side panels, tabs and splits, bottom panel, status bar)."""

JS_FRAME = r"""
// ---------- IDE frame ----------
const UI = { tabs: [], active: null, split: null, building: null, bg: false, built: false, left: 320, right: 300, bottom: 300,
  leftOpen: false, rightOpen: true, bottomOpen: false, chat: null, status: {} };
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
  lsSet(prefKey(), JSON.stringify({ left: UI.left, right: UI.right, bottom: UI.bottom, rightOpen: open('rightOpen'),
    tabs: UI.tabs.filter(t => t.hash && !/^#\/(files|shell|terminal)?$/.test(t.hash)).map(t => t.hash).slice(0, 12), active: UI.active && !/^#\/(files|shell|terminal)?$/.test(UI.active.hash || '') ? UI.active.hash : '' }));
}
// Widths of Progress and the side panels. The chat keeps at least 340px: when the window is too narrow the wide panel shrinks first, then the narrow one, then Progress.
function rightLayout() {
  let prog = UI.rightOpen ? UI.right : 0, wn = WB.narrow.length ? WB.wN : 0, ww = WB.wide.length ? WB.wW : 0;
  if (innerWidth <= 900) return { prog, wn, ww };
  if (WB.max) return { prog: 0, wn: 0, ww: 0 };
  let over = prog + wn + ww - Math.max(0, innerWidth - 64 - 340);
  const shrink = (v, min) => { const d = Math.min(Math.max(0, over), Math.max(0, v - min)); over -= d; return v - d; };
  if (over > 0 && ww) ww = shrink(ww, 340); if (over > 0 && wn) wn = shrink(wn, 220); if (over > 0 && prog) prog = shrink(prog, 220);
  return { prog, wn, ww };
}
function applySizes() {
  const body = document.getElementById('ide-body'); if (!body) return;
  body.style.setProperty('--lw', '0px');
  const L = rightLayout(); const wbw = L.wn + L.ww; const ext = L.prog + wbw;
  body.style.setProperty('--rw', ext + 'px'); body.style.setProperty('--wbw', wbw + 'px'); body.style.setProperty('--wn', L.wn + 'px'); body.style.setProperty('--ww', L.ww + 'px');
  const wrap = document.getElementById('rwrap'); if (wrap) wrap.classList.toggle('no-prog', !UI.rightOpen);
  body.classList.add('no-left'); body.classList.toggle('no-right', ext === 0 && !UI.rightOpen);
  const c = document.getElementById('center'); if (c) { c.style.setProperty('--bh', '0px'); c.classList.add('no-bottom'); }
}
function togglePanel(which, force) {
  if (which !== 'right') return;                       // Progress is the only panel of this kind now; Files and Terminal are workbench panels
  UI.rightOpen = force != null ? force : !UI.rightOpen; applySizes(); savePrefs(); drawStatus();
  document.body.classList.toggle('drawer-right', UI.rightOpen && innerWidth <= 900);
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
  const p = loadPrefs(); if (typeof p.right === 'number') UI.right = Math.max(160, Math.min(900, p.right));
  if (typeof p.rightOpen === 'boolean') UI.rightOpen = p.rightOpen;   // older saved layouts also carry left and bottom entries: ignored
  UI.restore = p;
  if (innerWidth <= 900) UI.rightOpen = false;
  const ws = state.ws || {};
  const logo = h('span', { html: state.logo || '' });
  const nav = h('nav', { 'aria-label': 'Sections', class: 'side-nav' }, ...NAV.map(([id, label, keys]) => h('a', { href: '#/' + id, 'data-nav': id, title: label + (NAV_HELP[id] ? ': ' + NAV_HELP[id] : '') + '  (' + keys + ')' }, svgIcon(ICONS[id]), h('span', { class: 'lbl' }, label))));
  const chip = h('a', { class: 'ws-chip', href: '#/chat', id: 'ws-chip', title: ws.workspace || '', 'aria-haspopup': 'menu', onclick: (e) => { e.preventDefault(); if (typeof projectMenu === 'function') projectMenu(e.currentTarget); else folderChooser(); } });
  const top = h('header', { class: 'top' }, h('a', { class: 'brand', href: '#/chat' }, logo, h('span', null, 'axon')), chip,
    h('button', { class: 'ctitle', id: 'chat-title', type: 'button', 'aria-haspopup': 'menu', onclick: (e) => wbTitleMenu(e.currentTarget) }),
    wbToolbar());
  const rp = h('aside', { class: 'rpanel', id: 'rpanel' }, h('div', { class: 'pbody', id: 'rpbody' }));
  const rwrap = h('div', { class: 'rwrap', id: 'rwrap' }, rp, wbBuild());
  const rzr = h('div', { class: 'rz rz-r', role: 'separator', 'aria-orientation': 'vertical', title: 'Drag to resize' });
  const tabscroll = h('div', { class: 'tabscroll', id: 'tabscroll', role: 'tablist', 'aria-label': 'Open tabs' });
  tabscroll.addEventListener('wheel', (e) => { if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { tabscroll.scrollLeft += e.deltaY; e.preventDefault(); } }, { passive: false });
  tabscroll.addEventListener('scroll', () => tabArrows());
  const tabstrip = h('div', { class: 'tabstrip', id: 'tabstrip' },
    h('button', { class: 'mini tsl', id: 'tab-l', type: 'button', 'aria-label': 'Scroll tabs left', hidden: true, onclick: () => { tabscroll.scrollBy({ left: -240 }); } }, '‹'), tabscroll,
    h('button', { class: 'mini tsr', id: 'tab-r', type: 'button', 'aria-label': 'Scroll tabs right', hidden: true, onclick: () => { tabscroll.scrollBy({ left: 240 }); } }, '›'),
    h('button', { class: 'mini', id: 'tab-menu', type: 'button', 'aria-haspopup': 'menu', title: 'All open tabs, and close the others', onclick: (e) => openTabMenu(e.currentTarget, null) }),
    h('button', { class: 'mini', id: 'tab-split', type: 'button', title: 'Show the previous tab beside this one', onclick: () => { if (UI.split) { UI.split = null; } else { const other = UI.tabs.filter(x => x !== UI.active).pop(); UI.split = other || null; } if (UI.active) setActive(UI.active); } }));
  const panes = h('div', { class: 'panes', id: 'panes' });
  const center = h('div', { class: 'center', id: 'center' }, tabstrip, panes);
  const body = h('div', { class: 'ide-body', id: 'ide-body' }, nav, center, rzr, rwrap);
  const status = h('footer', { class: 'statusbar', id: 'statusbar' });
  const scrim = h('div', { class: 'drawer-scrim', onclick: () => { document.body.classList.remove('drawer-right'); UI.rightOpen = false; wbCloseAll(); applySizes(); syncToggles(); } });
  app.replaceChildren(h('div', { class: 'ide' }, top, body, status), scrim);
  dragResizer(rzr, 'x', Object.assign(() => (UI.rightOpen ? UI.right : WB.narrow.length ? WB.wN : WB.wW), { side: 'right', def: 340 }), (b, d) => { const v = Math.max(220, Math.min(1000, b - d)); if (UI.rightOpen) UI.right = v; else if (WB.narrow.length) WB.wN = Math.min(720, v); else WB.wW = v; });
  applySizes(); syncToggles(); drawStatus(); renderRight(); drawChip(); wbRender(); wbTitleSync();
  let keepT = 0; const keepRoom = () => { clearTimeout(keepT); keepT = setTimeout(() => wbKeepChatRoom(null), 250); }; keepRoom();   // a saved layout or a smaller window must not squeeze the chat out
  window.addEventListener('resize', () => { keepRoom(); tabArrows(); const small = innerWidth <= 900; const was = document.body.classList.contains('small'); document.body.classList.toggle('small', small);
    if (small && !was) { UI.rightOpen = false; document.body.classList.remove('drawer-right', 'drawer-wb'); applySizes(); syncToggles(); } });
  document.body.classList.toggle('small', innerWidth <= 900);
}
function drawChip() {
  const ws = state.ws || {}; const chip = document.getElementById('ws-chip'); if (!chip) return;
  chip.title = (ws.workspace || '') + '  (recent projects and change folder)';
  chip.replaceChildren(h('span', { class: 'ws-dot' }), h('b', null, ws.name || 'workspace'));   // the full path and the branch are in Progress, under Environment
}
function drawStatus() {
  const el = document.getElementById('statusbar'); if (!el) return; const s = UI.status || {}; const info = state.info || {};
  el.classList.toggle('in-chat', !!(UI.active && UI.active.route === 'chat') || !!UI.rightOpen);   // the message box and Progress already show all of this: the bar steps aside
  const item = (label, val, cls, title) => h('span', { class: 'sb ' + (cls || ''), title: title || '' }, label ? h('span', { class: 'muted' }, label + ' ') : null, h('b', null, val));
  // In a chat the model, mode, effort and context sit under the message box and cost and branch in Progress; other pages keep them here.
  const chat = !!(UI.active && UI.active.route === 'chat'); const ws = state.ws || {};
  el.replaceChildren(item('', s.busy ? '● working' : '○ idle', s.busy ? 'live' : ''),
    chat ? null : item('model', s.model || info.model || '—'), chat ? null : item('mode', s.mode || info.mode || '—', '', 'What Axon may do without asking'),
    chat || !(s.context && s.context.window) ? null : item('context', s.context.pct + '%', s.context.pct > 90 ? 'bad' : s.context.pct > 75 ? 'warn' : '', s.context.used.toLocaleString() + ' of ' + s.context.window.toLocaleString() + ' tokens'),
    chat || s.cost == null ? null : item('cost', usd(s.cost)), item('trace', state.trace), chat ? null : item('effort', effortWord(s.effort || info.effort), '', 'How much thinking the model does before it answers'),
    h('span', { class: 'sp', style: 'flex:1' }), s.external ? item('', 'terminal session') : null, s.sandbox ? item('', 'sandbox on') : null,
    chat || !(ws.git && ws.git.repo && ws.git.branch) ? null : item('git', ws.git.branch), item('', 'axon ' + ((state.info || {}).version || '')));
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
  sc.classList.toggle('fade-l', over && sc.scrollLeft >= 2); sc.classList.toggle('fade-r', over && sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 2);
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
  UI.active = t; drawTabs(); drawStatus();
  document.querySelectorAll('.pane').forEach(p => { const on = p === t.pane || (UI.split && p === UI.split.pane && UI.split !== t); p.classList.toggle('on', on); });
  document.getElementById('panes').classList.toggle('split', !!(UI.split && UI.split !== t));
  document.querySelectorAll('.side-nav a').forEach(a => a.removeAttribute('aria-current'));
  const nav = document.querySelector('.side-nav a[data-nav="' + (t.route === 'agent' || t.route === 'agents' ? 'chat' : t.route) + '"]'); if (nav) nav.setAttribute('aria-current', 'page');
  if (t.route === 'chat' && t.C) { UI.chat = t.C; setStatus(t.C.st); renderRight(); if (t.C.checkReady) t.C.checkReady(); if (t.C.runtime && !t.C.es && typeof refreshState === 'function') refreshState(t.C, true); }
  wbTitleSync(); savePrefs();
}
function drawTabs() {
  const sc = document.getElementById('tabscroll'); if (!sc) return;
  const strip = document.getElementById('tabstrip'); if (strip) strip.hidden = UI.tabs.length < 2;   // one tab needs no tab bar
  sc.replaceChildren(...UI.tabs.map(t => {
    const el = h('div', { class: 'tab' + (t === UI.active ? ' on' : '') + (UI.split === t ? ' split' : ''), role: 'presentation', title: t.title },
      h('button', { class: 'tl', type: 'button', role: 'tab', 'aria-selected': String(t === UI.active), onclick: () => { location.hash = t.hash || '#/chat'; setActive(t); } }, svgIcon(ICONS[TAB_ICON[t.route]] || ICONS.files), h('span', null, t.title)),
      UI.tabs.length > 1 ? h('button', { class: 'tx', type: 'button', 'aria-label': 'Close ' + t.title, onclick: (e) => { e.stopPropagation(); closeTab(t); } }, '×') : null);   // the only tab has nothing to close into
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
    if (typeof helpPageHook === 'function') helpPageHook(t.pane, route);
    if (PAGE_HINT[route] && !t.pane.querySelector(':scope > .page-hint')) { const hint = h('div', { class: 'page-hint' }, PAGE_HINT[route]); const hd = t.pane.querySelector(':scope > .head'); if (hd) hd.after(hint); else t.pane.prepend(hint); }
    savePrefs();
  });
}
function onLeave(fn) { if (UI.building) UI.building.cleanup.push(fn); }
function leave() { /* tabs keep their own cleanups; closing a tab runs them */ }

// The project tree lives in the Files panel; this name stays for the callers that reload it after Change folder.
function drawLeftTabs() { if (typeof wbRefreshFiles === 'function') wbRefreshFiles(); }
// ----- right panel: live progress -----
function renderRight() {
  const b = document.getElementById('rpbody'); if (!b) return;
  if (UI.chat) { drawRail(UI.chat); return; }
  // no chat open (a settings page, say): the panel still shows the folder, the connectors and the changes
  wbRail({ st: {}, steps: [], subs: [], touched: {}, sid: '' });
}
"""
