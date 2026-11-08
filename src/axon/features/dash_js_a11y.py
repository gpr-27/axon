"""Accessibility layer of the web app: landmarks, a skip link, announcements of turn state, focus handling for dialogs and
menus, keyboard use of the file tree, tab lists and toolbars, plus the CSS for focus, hit targets, contrast and reduced motion.

It works from the outside: one observer finds the widgets that every page script already builds (``[role=dialog]``, ``[role=menu]``,
tab lists, the file tree) and gives each the behaviour the ARIA practices ask for, so a new page or menu gets it without extra code.
Page scripts still own their own labels; ``tests/test_feature_i18n.py`` fails on a button or icon without a name in the scripts.
"""

JS_A11Y = r"""
// ---------- accessibility layer (see dash_js_a11y.py) ----------
(function a11yLayer() {
  const I = window.AxonI18n; const L = (s) => (I ? I.tr(s) : s);
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';
  const shown = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter(e => shown(e) && !e.closest('[hidden],[inert]'));
  let seq = 0; const uid = (p) => p + (++seq);
  const body = document.body;

  // A polite status line for turn state. Streamed tokens never go here: only "working", "finished" and the like.
  const live = h('div', { id: 'sr-live', class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
  body.append(live);
  let liveT = 0;
  function announce(msg) { clearTimeout(liveT); live.textContent = ''; liveT = setTimeout(() => { live.textContent = msg; }, 60); }
  window.axAnnounce = announce;

  // ----- landmarks, names and one h1 per page -----
  const named = [];   // [element, english label]: re-applied when the language changes
  function label(el, text) { if (!el) return; named.push([el, text]); el.setAttribute('aria-label', L(text)); }
  function landmarks() {
    const panes = document.getElementById('panes');
    if (panes && !panes.hasAttribute('role')) { panes.setAttribute('role', 'main'); panes.setAttribute('tabindex', '-1'); panes.dataset.main = '1'; }
    const rp = document.getElementById('rpanel'); if (rp && !rp.hasAttribute('aria-label')) label(rp, 'Progress panel');
    const sb = document.getElementById('statusbar'); if (sb && !sb.hasAttribute('aria-label')) label(sb, 'Status bar');
    const tb = document.querySelector('.tools.tbicons'); if (tb && !tb.hasAttribute('role')) { tb.setAttribute('role', 'toolbar'); label(tb, 'Panels'); }
    const ts = document.getElementById('tabscroll'); if (ts && !ts.dataset.a11y) { ts.dataset.a11y = '1'; label(ts, 'Open tabs'); }
    // a visually hidden h1 for panes that have none (the chat), so every page has one heading to land on
    document.querySelectorAll('.pane').forEach((p) => {
      if (p.querySelector('h1,.sr-h1')) return;
      const tab = typeof UI !== 'undefined' ? UI.tabs.find(t => t.pane === p) : null; if (!tab) return;
      const h1 = h('h1', { class: 'sr-only sr-h1' }, tab.title); p.prepend(h1);
    });
    document.querySelectorAll('.pane .sr-h1').forEach((h1) => { const tab = typeof UI !== 'undefined' ? UI.tabs.find(t => t.pane === h1.parentElement) : null; if (tab && h1.textContent !== L(tab.title) && h1.textContent !== tab.title) h1.textContent = tab.title; });
    // on a narrow window the Progress panel slides in from the side: while it is off-screen it must not take Tab stops
    if (rp) { if (body.classList.contains('small') && !body.classList.contains('drawer-right')) rp.setAttribute('inert', ''); else rp.removeAttribute('inert'); }
    // icons are decoration: the button or link around them carries the name
    document.querySelectorAll('svg:not([aria-hidden]):not([role]):not([aria-label])').forEach((v) => { v.setAttribute('aria-hidden', 'true'); v.setAttribute('focusable', 'false'); });
    // a help button inside a heading must not become part of the heading's name
    document.querySelectorAll('h1 .helptip,h2 .helptip,h3 .helptip').forEach((tip) => {
      const hd = tip.closest('h1,h2,h3'); const c = hd.cloneNode(true); c.querySelectorAll('.helptip,a,button').forEach(x => x.remove());
      const txt = c.textContent.replace(/\s+/g, ' ').trim(); if (txt && hd.getAttribute('aria-label') !== txt) hd.setAttribute('aria-label', txt);
    });
    document.querySelectorAll('.brand svg').forEach((v) => { v.setAttribute('aria-hidden', 'true'); v.removeAttribute('role'); v.removeAttribute('aria-label'); });
    // the chat feed: entries are articles named by who spoke; the feed is marked busy while a turn streams
    document.querySelectorAll('.feed').forEach((f) => { if (!f.hasAttribute('role')) f.setAttribute('role', 'feed'); if (!f.hasAttribute('aria-label')) label(f, 'Conversation'); });
    document.querySelectorAll('article.entry').forEach((a) => {
      if (a.hasAttribute('aria-labelledby')) return; const who = a.querySelector(':scope > .who'); if (!who) return;
      if (!who.id) who.id = uid('who-'); a.setAttribute('aria-labelledby', who.id);
    });
    document.querySelectorAll('section.step').forEach((s) => { if (s.hasAttribute('aria-labelledby')) return; const t = s.querySelector('.stitle'); if (!t) return; if (!t.id) t.id = uid('stp-'); s.setAttribute('role', 'group'); s.setAttribute('aria-labelledby', t.id); });
    document.querySelectorAll('.pane .tabs[role=tablist]').forEach((tl) => { if (!tl.hasAttribute('aria-label')) { const h1 = tl.closest('.pane').querySelector('h1'); label(tl, (h1 && h1.textContent) || 'Sections'); } });
    document.querySelectorAll('.vwin').forEach((w) => { if (!w.hasAttribute('role')) { w.setAttribute('role', 'tree'); label(w, 'Project files'); } });
    document.querySelectorAll('.box .pop').forEach((p) => { if (!p.hasAttribute('role')) { p.setAttribute('role', 'listbox'); p.id = p.id || uid('pop-'); label(p, 'Suggestions'); } });
    const ta = document.getElementById('msg'); const pop = ta && ta.closest('.box') && ta.closest('.box').querySelector('.pop');
    if (ta && pop) {
      ta.setAttribute('aria-autocomplete', 'list'); ta.setAttribute('aria-controls', pop.id);
      const sel = pop.hidden ? null : pop.querySelector('[role=option][aria-selected=true]');
      if (sel) { if (!sel.id) sel.id = uid('opt-'); ta.setAttribute('aria-activedescendant', sel.id); } else ta.removeAttribute('aria-activedescendant');
    }
    document.querySelectorAll('.tabs[role=tablist]').forEach((tl) => {
      const tabs = [...tl.querySelectorAll('[role=tab]')];
      tabs.forEach((t) => { if (!t.id) t.id = uid('tab-'); t.setAttribute('tabindex', t.getAttribute('aria-selected') === 'true' || !tabs.some(x => x.getAttribute('aria-selected') === 'true') && t === tabs[0] ? '0' : '-1'); });
      const sel = tabs.find(t => t.getAttribute('aria-selected') === 'true'); const panel = tl.nextElementSibling;
      if (sel && panel && !panel.matches('[role]') && !panel.matches('.tabs,script')) { panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', sel.id); }
      else if (sel && panel && panel.getAttribute('role') === 'tabpanel') panel.setAttribute('aria-labelledby', sel.id);
    });
    const strip = document.getElementById('tabscroll');
    if (strip) { const tabs = [...strip.querySelectorAll('[role=tab]')]; const any = tabs.some(t => t.getAttribute('aria-selected') === 'true'); tabs.forEach((t, i) => t.setAttribute('tabindex', t.getAttribute('aria-selected') === 'true' || (!any && i === 0) ? '0' : '-1')); }
    // expanded/collapsed folders and levels in the file tree
    document.querySelectorAll('.vwin .node').forEach((n) => {
      if (n.getAttribute('role') === 'treeitem') return;
      n.setAttribute('role', 'treeitem'); const p = n.dataset.path || ''; n.setAttribute('aria-level', String(p.split('/').filter(Boolean).length || 1));
      const nm = n.querySelector('.nm'); if (nm) n.setAttribute('aria-label', nm.textContent); const ar = n.querySelector('.ar'); if (ar) ar.setAttribute('aria-hidden', 'true');
    });
    document.querySelectorAll('.vwin .node.dir').forEach((n) => { const ar = n.querySelector('.ar'); n.setAttribute('aria-expanded', String(!!ar && /▾|▼|v/.test(ar.textContent) || n.classList.contains('open'))); });
    const nodes = [...document.querySelectorAll('.vwin .node')];
    if (nodes.length && !nodes.some(n => n.tabIndex === 0 && n.getAttribute('tabindex') === '0')) nodes.forEach((n, i) => n.setAttribute('tabindex', i === 0 ? '0' : '-1'));
    document.querySelectorAll('.vwin .node').forEach((n) => { if (!n.hasAttribute('tabindex')) n.setAttribute('tabindex', '-1'); });
    if (typeof UI !== 'undefined' && UI.active && UI.active.title) { const t = UI.active.title + ' · Axon'; if (document.title !== t) document.title = t; }
  }
  function relabel() { named.forEach(([el, text]) => { if (el.isConnected) el.setAttribute('aria-label', L(text)); }); }
  if (I) I.subscribe(() => { relabel(); landmarks(); });

  // The skip link moves focus; it must not change the address, which is the page router.
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a.skip-link'); if (!a) return;
    e.preventDefault(); const m = document.querySelector('[data-main]'); if (m) { m.focus(); m.scrollIntoView({ block: 'start' }); }
  });

  // ----- turn state: "working" and "finished", read from the status bar; never the token stream -----
  let lastState = null;
  function statusState() { const sb = document.querySelector('#statusbar .sb'); if (!sb) return null; return /working/.test(sb.textContent) ? 'working' : 'idle'; }
  function onStatus() {
    const s = statusState(); if (!s || s === lastState) return;
    if (lastState !== null) announce(s === 'working' ? L('Axon is working') : L('Axon has finished'));
    lastState = s;
    document.querySelectorAll('.feed').forEach(f => f.setAttribute('aria-busy', String(s === 'working')));
  }

  // ----- dialogs: focus in, Tab kept inside, Escape closes, focus back where it came from -----
  let lastOutside = null, lastMenuOpener = null;
  document.addEventListener('focusin', (e) => { const t = e.target; if (t.closest && !t.closest('[role=dialog][aria-modal=true],[role=menu]')) lastOutside = t; }, true);
  const dialogs = new Map(), menus = new Map();
  const modals = () => [...document.querySelectorAll('[role=dialog][aria-modal=true]')].filter(d => d.isConnected);
  function setInert() {
    const top = modals().pop();
    [...body.children].forEach((c) => {
      if (top && !c.contains(top) && c !== top && !c.matches('.toast,#sr-live,script,style,[role=menu],.wbmenu,.tab-pop,.mg-pop,.ctxmenu')) { if (!c.hasAttribute('inert')) { c.setAttribute('inert', ''); c.dataset.a11yInert = '1'; } }
      else if (c.dataset.a11yInert) { c.removeAttribute('inert'); delete c.dataset.a11yInert; }
    });
  }
  function dialogOpened(d) {
    if (dialogs.has(d)) return; const opener = lastOutside && lastOutside.isConnected ? lastOutside : (lastMenuOpener && lastMenuOpener.isConnected ? lastMenuOpener : document.activeElement);
    dialogs.set(d, opener); setInert();
    if (!d.contains(document.activeElement)) { const f = d.querySelector('[autofocus]') || focusables(d)[0]; if (f) f.focus(); else { d.setAttribute('tabindex', '-1'); d.focus(); } }
  }
  function dialogClosed(d) {
    const opener = dialogs.get(d); dialogs.delete(d); setInert();
    setTimeout(() => {
      if (modals().length) return;
      const ae = document.activeElement; if (ae && ae !== document.body && ae.isConnected && !d.contains(ae)) return;   // the page already put focus somewhere
      const to = [opener, lastMenuOpener, lastOutside].find(x => x && x.isConnected && shown(x)); if (to) to.focus();
    }, 0);
  }
  function closeDialog(d) {
    d.dispatchEvent(new MouseEvent('click', { bubbles: true }));    // the backdrop click every dialog already treats as "cancel"
    setTimeout(() => { if (d.isConnected) d.remove(); }, 30);
  }

  // ----- menus: first item focused, arrows, Home/End, typeahead, Tab leaves, focus back to the button -----
  const items = (m) => [...m.querySelectorAll('[role=menuitem]:not([disabled]),[role=menuitemradio]:not([disabled]),[role=menuitemcheckbox]:not([disabled])')].filter(shown);
  function menuOpened(m) {
    if (menus.has(m)) return; const opener = lastOutside && lastOutside.isConnected ? lastOutside : null;
    menus.set(m, opener); lastMenuOpener = opener;
    if (opener && opener.getAttribute && opener.getAttribute('aria-haspopup')) opener.setAttribute('aria-expanded', 'true');
    if (!m.contains(document.activeElement)) { const f = items(m)[0]; if (f) f.focus(); }
  }
  function menuClosed(m) {
    const opener = menus.get(m); menus.delete(m);
    if (opener && opener.getAttribute && opener.getAttribute('aria-haspopup')) opener.setAttribute('aria-expanded', 'false');
    setTimeout(() => { if ([...menus.keys()].some(x => x.isConnected) || modals().length) return; const ae = document.activeElement; if (ae && ae !== document.body && ae.isConnected) return; if (opener && opener.isConnected && shown(opener)) opener.focus(); }, 0);
  }
  function closeMenu(m) {
    body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));    // the click-away every menu already listens for
    setTimeout(() => { if (m.isConnected) m.remove(); }, 30);
  }
  document.addEventListener('keydown', (e) => {
    const ae = document.activeElement; const m = ae && ae.closest ? ae.closest('[role=menu]') : null;
    if (e.key === 'Tab') {
      if (m) { const o = menus.get(m); closeMenu(m); if (o && o.isConnected) o.focus(); return; }
      const top = modals().pop(); if (!top) return;
      const f = focusables(top); if (!f.length) { e.preventDefault(); top.focus(); return; }
      const first = f[0], last = f[f.length - 1];
      if (!top.contains(ae)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && (ae === first || ae === top)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && ae === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if (m) {
      const list = items(m); if (!list.length) return; const i = list.indexOf(ae);
      let to = null;
      if (e.key === 'ArrowDown') to = list[(i + 1) % list.length]; else if (e.key === 'ArrowUp') to = list[(i - 1 + list.length) % list.length];
      else if (e.key === 'Home') to = list[0]; else if (e.key === 'End') to = list[list.length - 1];
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { const c = e.key.toLowerCase(); to = [...list.slice(i + 1), ...list.slice(0, i + 1)].find(x => (x.textContent || '').trim().toLowerCase().startsWith(c)) || null; }
      if (to) { e.preventDefault(); e.stopImmediatePropagation(); to.focus(); return; }
      if (e.key === 'Escape') setTimeout(() => { if (m.isConnected) closeMenu(m); }, 0);
      return;
    }
    if (e.key === 'Escape') { const top = modals().pop(); if (top) setTimeout(() => { if (top.isConnected) closeDialog(top); }, 0); }
  }, true);

  // ----- tab lists, toolbars and the file tree: arrow keys -----
  document.addEventListener('keydown', (e) => {
    const t = e.target; if (!t || !t.closest || e.ctrlKey || e.metaKey || e.altKey) return;
    const tab = t.matches('[role=tab]') ? t : null;
    if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      const list = tab.closest('[role=tablist]'); const tabs = list ? [...list.querySelectorAll('[role=tab]')].filter(shown) : []; if (!tabs.length) return;
      const i = tabs.indexOf(tab); const to = e.key === 'Home' ? tabs[0] : e.key === 'End' ? tabs[tabs.length - 1] : tabs[(i + (e.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
      e.preventDefault(); tabs.forEach(x => x.setAttribute('tabindex', x === to ? '0' : '-1')); to.focus(); return;
    }
    const tb = t.closest('[role=toolbar]');
    if (tb && (e.key === 'ArrowLeft' || e.key === 'ArrowRight') && t.matches('button,a[href]')) {
      const b = [...tb.querySelectorAll('button:not([disabled]),a[href]')].filter(shown); const i = b.indexOf(t); if (i < 0) return;
      e.preventDefault(); b[(i + (e.key === 'ArrowRight' ? 1 : -1) + b.length) % b.length].focus(); return;
    }
    const node = t.matches('.vwin .node') ? t : null;
    if (node) {
      const all = [...node.closest('.vwin').querySelectorAll('.node')]; const i = all.indexOf(node); let to = null;
      const move = (x) => { all.forEach(n => n.setAttribute('tabindex', n === x ? '0' : '-1')); x.focus(); };
      if (e.key === 'ArrowDown') to = all[i + 1]; else if (e.key === 'ArrowUp') to = all[i - 1]; else if (e.key === 'Home') to = all[0]; else if (e.key === 'End') to = all[all.length - 1];
      else if (e.key === 'ArrowRight' && node.classList.contains('dir')) { e.preventDefault(); if (node.getAttribute('aria-expanded') !== 'true') node.click(); else if (all[i + 1] && +all[i + 1].getAttribute('aria-level') > +node.getAttribute('aria-level')) move(all[i + 1]); return; }
      else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        if (node.classList.contains('dir') && node.getAttribute('aria-expanded') === 'true') { node.click(); return; }
        const lv = +node.getAttribute('aria-level'); for (let j = i - 1; j >= 0; j--) if (+all[j].getAttribute('aria-level') < lv) { move(all[j]); return; }
        return;
      }
      if (to) { e.preventDefault(); move(to); }
    }
  });
  // the tree redraws its rows when a folder opens or closes: keep keyboard focus on the same path
  let treeKeep = null, treeT = 0;
  document.addEventListener('keydown', (e) => { const n = e.target; if (n && n.matches && n.matches('.vwin .node')) { treeKeep = n.dataset.path; clearTimeout(treeT); treeT = setTimeout(() => { treeKeep = null; }, 1500); } }, true);
  function restoreTree() {
    if (treeKeep == null) return; const ae = document.activeElement; if (ae && ae !== body && ae.isConnected) return;
    const n = [...document.querySelectorAll('.vwin .node')].find(x => x.dataset.path === treeKeep); if (n) { n.setAttribute('tabindex', '0'); n.focus(); }
  }
  document.addEventListener('focusin', (e) => { const n = e.target; if (n.matches && n.matches('.vwin .node') && n.getAttribute('tabindex') !== '0') { n.closest('.vwin').querySelectorAll('.node').forEach(x => x.setAttribute('tabindex', x === n ? '0' : '-1')); } });

  // A permission request is an alert dialog: bring keyboard focus to it, and back to the message box afterwards.
  function approvalAdded(card) { if (card.dataset.a11yFocus) return; card.dataset.a11yFocus = '1'; const b = card.querySelector('button'); if (b && (!document.activeElement || document.activeElement === body || document.activeElement.id === 'msg')) b.focus(); }

  // When a tab list was used from the keyboard and the page redrew it, put focus back on the selected tab.
  let keptTab = null;
  document.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('.pane [role=tab]')) { const list = e.target.closest('[role=tablist]'); const pane = e.target.closest('.pane'); keptTab = { pane, idx: [...pane.querySelectorAll('[role=tablist]')].indexOf(list) }; setTimeout(() => { keptTab = null; }, 1500); } }, true);
  function restoreTab() {
    if (!keptTab || (document.activeElement && document.activeElement !== body)) return;
    const lists = keptTab.pane.isConnected ? keptTab.pane.querySelectorAll('[role=tablist]') : []; const l = lists[keptTab.idx]; const s = l && l.querySelector('[role=tab][aria-selected=true]');
    if (s) { s.focus(); keptTab = null; }
  }

  let raf = 0; const later = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; landmarks(); onStatus(); restoreTab(); restoreTree(); }); };
  new MutationObserver((muts) => {
    for (const m of muts) {
      m.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        n.querySelectorAll && n.querySelectorAll('[role=dialog][aria-modal=true]').forEach(dialogOpened); if (n.matches('[role=dialog][aria-modal=true]')) dialogOpened(n);
        const ms = n.matches('[role=menu]') ? [n] : (n.querySelectorAll ? [...n.querySelectorAll('[role=menu]')] : []); ms.forEach(menuOpened);
        const ap = n.matches('.approve[role=alertdialog]') ? n : (n.querySelector && n.querySelector('.approve[role=alertdialog]')); if (ap) approvalAdded(ap);
      });
      m.removedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        dialogs.forEach((_o, d) => { if (!d.isConnected) dialogClosed(d); });
        menus.forEach((_o, mm) => { if (!mm.isConnected) menuClosed(mm); });
      });
    }
    later();
  }).observe(body, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-selected', 'hidden'] });
  new MutationObserver(later).observe(body, { attributes: true, attributeFilter: ['class'] });
  landmarks(); later();
})();
"""

A11Y_CSS = r"""
/* ── Accessibility: skip link, hidden text, focus, hit targets, reduced motion ── */
.sr-only{position:absolute!important;width:1px!important;height:1px!important;margin:-1px!important;padding:0!important;overflow:hidden!important;clip:rect(0 0 0 0)!important;white-space:nowrap!important;border:0!important}
.skip-link{position:fixed;left:8px;top:-60px;z-index:100;background:var(--surface);color:var(--ink);border:2px solid var(--primary);border-radius:6px;padding:8px 14px;text-decoration:none;font-weight:600}
.skip-link:focus{top:8px}
[tabindex="-1"]:focus{outline:none}
/* a visible ring everywhere, on a halo so it shows on any background */
:focus-visible{outline:2px solid var(--primary);outline-offset:2px;box-shadow:0 0 0 4px color-mix(in srgb,var(--page) 85%,transparent)}
.tl:focus-visible,.tx:focus-visible,.node:focus-visible{outline-offset:-2px}
/* hit targets of at least 24 by 24 CSS pixels */
button,a.brand,a.plink,summary,select,[role=tab],[role=menuitem],.mini,.btn,.tbi,.wpb,.tx,.tl,.rfid,.mg-star,.mg-th,.mg-more-btn,.fw,.cplus,.cmic,.node{min-height:24px}
button:not(.node):not(.btn):not(.fw):not(.turnbar):not(.foldbtn),.tbi,.wpb,.tx,.cplus,.cmic{min-width:24px}
a.brand,a.plink{display:inline-flex;align-items:center}
a[href]:not(.skip-link){min-height:24px;min-width:24px}
input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=range]):not([type=file]),select,textarea{min-height:24px}
input[type=checkbox],input[type=radio]{width:24px;height:24px;margin:2px;vertical-align:middle}
/* reduced motion: the system setting turns animation and transitions off, unless the person chose On in Appearance */
@media (prefers-reduced-motion:reduce){html:not([data-motion=on]) *,html:not([data-motion=on]) *::before,html:not([data-motion=on]) *::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}}
/* zoom to 200% and small windows: nothing forces the page wider than the window */
html,body{max-width:100%}
.modal{max-width:calc(100vw - 16px)}
"""


# ─── a check on the page scripts: a control with no name ───────────────────

import re  # noqa: E402
from pathlib import Path  # noqa: E402

_ICON_KID = re.compile(r"^(?:svgIcon\(|wbIco\(|ico\(|icon\(|h\('svg'|[`'\"][^A-Za-z0-9]{0,3}[`'\"]$|null$|false$)")
_NAMING = ("aria-label", "aria-labelledby")


def _split_args(text: str) -> list[str]:
    """Top-level comma-separated arguments of a call body, skipping strings and brackets."""
    args, depth, cur, quote, i = [], 0, [], "", 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\":
                i += 1
                cur.append(text[i] if i < len(text) else "")
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        args.append("".join(cur).strip())
    return args


def _call_body(src: str, start: int) -> str | None:
    """The text between the parentheses of the call whose "(" is at *start*."""
    depth, quote, i = 0, "", start
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
        i += 1
    return None


def find_unlabelled(src: str) -> list[str]:
    """Controls built with ``h('button' | 'input' | 'select' | 'textarea', {...}, ...kids)`` that cannot get an accessible name:
    a button whose children are only an icon or a symbol and has no aria-label, or a field with no aria-label, aria-labelledby or id
    (an id may be the target of a label's ``for``). Detectable cases only: a child that is a variable is assumed to be text."""
    found = []
    for m in re.finditer(r"\bh\(\s*'(button|input|select|textarea)'", src):
        tag = m.group(1)
        body = _call_body(src, src.index("(", m.start()))
        if body is None:
            continue
        args = _split_args(body)
        props = args[1] if len(args) > 1 and args[1].startswith("{") else ""
        kids = args[2:] if props else args[1:]
        line = src.count("\n", 0, m.start()) + 1
        if any(k in props for k in _NAMING):
            continue
        if "aria-hidden" in props:
            continue
        if tag == "button":
            if re.search(r"\bid:", props) or all(k in ("''", '""') for k in kids) and kids:
                continue      # text filled in by code afterwards: the live accessibility tree test checks these
            if all(_ICON_KID.match(k) for k in kids):
                found.append(f"line {line}: <button> with only an icon or symbol and no aria-label")
        else:
            if re.search(r"type:\s*'(?:hidden|file|submit|button)'", props) or re.search(r"\bid:", props) or "hidden" in props:
                continue
            if re.search(r"\bh\('label'", src[max(0, m.start() - 160):m.start() + len(body) + 700]):
                continue      # sits inside, or right beside, a <label> with text
            found.append(f"line {line}: <{tag}> without aria-label, aria-labelledby or id")
    return found


def scan_scripts(root: Path | None = None) -> dict[str, list[str]]:
    """find_unlabelled over every page script module, by file name."""
    root = root or Path(__file__).parent
    out = {}
    for f in sorted(root.glob("dash_js_*.py")):
        if f.name in ("dash_js_a11y.py", "dash_js_i18n.py"):
            continue
        hits = find_unlabelled(f.read_text(encoding="utf-8"))
        if hits:
            out[f.name] = hits
    return out
