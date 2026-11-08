"""Client script, part 1: helpers, app frame, Extensions and Share (Usage and Sessions moved to dash_js_manage.py)."""

JS_CORE = r"""
// DOM insertion that ignores null, undefined and false, so conditional children never print "null".
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const name of ['append', 'prepend', 'replaceChildren']) {
    const orig = proto[name]; if (!orig) continue;
    proto[name] = function (...k) { return orig.apply(this, k.flat(Infinity).filter(x => x != null && x !== false)); };
  }
}
const app = document.getElementById('app');
const state = { info: null, scope: 'project', period: '30d', sessions: null, q: '', starred: false, tag: '', model: '', sessionScope: 'project', ext: null, ws: null, chat: null, trace: 'normal' };
const NAV = [['chat', 'Chat', 'g c'], ['files', 'Files', 'g f'], ['commands', 'Commands', 'g m'], ['shell', 'Shell', 'g h'], ['terminal', 'Terminal', 'g x'], ['sessions', 'Sessions', 'g s'], ['usage', 'Usage', 'g u'], ['insights', 'Insights', 'g i'], ['memory', 'Memory', 'g y'], ['extensions', 'Extensions', 'g e'], ['settings', 'Settings', 'g t']];
// One line each, so the three ways of running things are easy to tell apart.
const NAV_HELP = { shell: 'run one command and read its output (in the project folder)', terminal: 'a full live terminal for editors, top and an interactive Axon', commands: "Axon's own / commands, such as /status or /compact (not shell commands)",
  chat: 'talk to Axon', files: 'browse and edit project files', sessions: 'past chats', usage: 'cost and token use', insights: 'review changes and context', memory: 'notes Axon remembers', extensions: 'plugins, skills and tools', settings: 'provider, model and preferences' };
const PAGE_HINT = { terminal: 'a full live terminal (editors, top, an interactive Axon). For one quick command use Shell.', commands: "Axon's own / commands such as /status or /compact. They are not shell commands; use Shell for those." };
const ICONS = { chat: 'M4 5h16v11H9l-5 4z', files: 'M4 6h6l2 2h8v10H4z', file: 'M7 3h7l4 4v14H7zM14 3v4h4', shell: 'M5 7l5 5-5 5M12 17h7', terminal: 'M4 5h16v14H4zM8 10l3 2-3 2M13 14h4', commands: 'M8 6l-5 6 5 6M16 6l5 6-5 6', insights: 'M4 19l5-7 4 4 7-10M4 5v14h16', sessions: 'M5 5h14M5 12h14M5 19h14', usage: 'M5 19V9M12 19V5M19 19v-7', memory: 'M6 4h12v16l-6-4-6 4z', extensions: 'M9 4h6v4h4v6h-4v6H9v-6H5V8h4z', settings: 'M12 8a4 4 0 100 8 4 4 0 000-8zM12 3v3M12 18v3M3 12h3M18 12h3' };

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;      // only ever server-escaped HTML or SVG
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(3)) { if (kid == null || kid === false) continue; el.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
  return el;
}
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } };

let AUTH_LOST = false;
function authLost() {
  if (AUTH_LOST) return; AUTH_LOST = true;
  app.replaceChildren(h('div', { class: 'authlost' }, h('h1', null, 'This page is signed out'),
    h('p', null, 'Axon no longer recognises this browser tab. That happens when the dashboard was started by someone else, or its secret was reset.'),
    h('p', null, 'In your terminal, run ', h('code', null, 'axon open'), ' (or type ', h('code', null, '/open'), ' inside Axon). It opens a fresh link that signs this tab back in.'),
    h('div', { class: 'formrow' }, h('button', { class: 'btn primary', onclick: () => location.reload() }, 'Try again'))));
}
class ApiError extends Error { constructor(m, status, data) { super(m); this.status = status; this.data = data; } }
async function api(path, body) {
  const opts = { credentials: 'same-origin' };
  if (body !== undefined) { opts.method = 'POST'; opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opts); } catch (e) { throw new ApiError('Could not reach Axon. Is it still running? Run axon open again if not.', 0); }
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-json */ }
  if (r.status === 401) { authLost(); throw new ApiError('Signed out', 401, data); }
  if (!r.ok) throw new ApiError((data && data.error) || ('Request failed (' + r.status + ')'), r.status, data);
  return data;
}
const usd = (n) => n >= 1 ? '$' + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : (n ? '$' + n.toFixed(4) : '$0.00');
const tok = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(Math.round(n || 0));
const dur = (s) => !s ? '0s' : s < 90 ? s.toFixed(1) + 's' : s < 5400 ? Math.round(s / 60) + 'm' : (s / 3600).toFixed(1) + 'h';
const pct = (n) => (n || 0).toFixed(1) + '%';
function age(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 90) return 'just now'; if (d < 5400) return Math.round(d / 60) + 'm ago';
  if (d < 129600) return Math.round(d / 3600) + 'h ago'; if (d < 86400 * 45) return Math.round(d / 86400) + 'd ago';
  return new Date(ts * 1000).toLocaleDateString();
}
function toast(msg) { const t = h('div', { class: 'toast', role: 'status' }, msg); document.body.append(t); setTimeout(() => t.remove(), 3500); }

// ---------- chrome ----------
function theme() { return document.documentElement.getAttribute('data-theme'); }
function setTheme(t) { if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme'); }
function toggleTheme() {
  const dark = theme() ? theme() === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const next = dark ? 'light' : 'dark'; setTheme(next); lsSet('axon-theme', next);
}
function svgIcon(path) { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'ico'); const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', path); s.append(p); return s; }
function errorBox(msg) { return h('div', { class: 'err-box', role: 'alert' }, msg); }
function table(cols, rows, empty) {
  if (!rows.length) return h('p', { class: 'muted' }, empty || 'Nothing here.');
  return h('div', { class: 'tw' }, h('table', null,
    h('thead', null, h('tr', null, cols.map(c => h('th', { class: c.cls || '' }, c.label)))),
    h('tbody', null, rows.map(r => h('tr', null, cols.map((c, i) => h('td', { class: c.cls || '' }, c.fmt ? c.fmt(r) : r[c.key])))))));
}
// A file path in any output becomes a link into the Files viewer.
const PATH_RE = /((?:~\/|\.{1,2}\/|\/)?(?:[\w.@+-]+\/)+[\w.@+-]+(?::\d+(?::\d+)?)?|\b[\w.-]+\.(?:py|js|jsx|ts|tsx|json|md|toml|yaml|yml|sh|css|html|rs|go|c|h|cpp|java|rb|sql|txt|cfg|ini|lock)\b(?::\d+)?)/g;
function linkifyPaths(root) {
  if (!root) return root;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (n) => (n.parentElement && n.parentElement.closest('a,button,textarea,.nolink') ? NodeFilter.FILTER_REJECT : (n.nodeValue.length > 300000 || n.nodeValue.length < 3 ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT)) });
  const nodes = []; let n; while ((n = w.nextNode())) nodes.push(n);
  let budget = 400;
  for (const node of nodes) {
    const text = node.nodeValue; PATH_RE.lastIndex = 0;
    if (!PATH_RE.test(text)) continue;
    PATH_RE.lastIndex = 0;
    const frag = document.createDocumentFragment(); let last = 0, m;
    while ((m = PATH_RE.exec(text)) && budget > 0) {
      let raw = m[0]; const mm = raw.match(/^(.*?)(?::(\d+)(?::\d+)?)?$/); const p = mm[1], line = mm[2];
      if (/^https?:/.test(text.slice(Math.max(0, m.index - 8), m.index)) || /^\d+(\.\d+)+$/.test(p)) continue;
      budget--;
      frag.append(text.slice(last, m.index));
      const a = h('a', { class: 'plink', href: '#/files/' + encodeURIComponent(p) + (line ? '?line=' + line : ''), title: 'Open in Files' }, raw);
      frag.append(a); last = m.index + raw.length;
    }
    if (last) { frag.append(text.slice(last)); node.replaceWith(frag); }
  }
  return root;
}
// Long blocks fold in place: one click to expand or collapse.
function clampLong(root) {
  root.querySelectorAll('.md, pre.out, pre.code, .diff, .think pre').forEach((el) => {
    if (el.dataset.clamp || el.closest('.nolink')) return; el.dataset.clamp = '1';
    if (el.scrollHeight > 460) {
      el.classList.add('clamped');
      const b = h('button', { class: 'foldbtn', type: 'button', onclick: () => { const c = el.classList.toggle('clamped'); b.textContent = c ? 'Expand (' + Math.round(el.scrollHeight / 20) + ' lines)' : 'Collapse'; } }, 'Expand (' + Math.round(el.scrollHeight / 20) + ' lines)');
      el.after(b);
    }
  });
}
function decorate(root) { linkifyPaths(root); clampLong(root); return root; }
function toggleAll(scope, open) { scope.querySelectorAll('details').forEach(d => { d.open = open; }); scope.querySelectorAll('.clamped').forEach(el => { if (open) el.classList.remove('clamped'); }); }

// ---------- usage and sessions: see dash_js_manage.py (viewOverview, viewSessions) ----------

// ---------- extensions ----------
async function viewExtensions() {
  shell('extensions', async (main) => {
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Extensions'), h('div', { class: 'sub' }, 'What Axon can use here. Read-only; change these from the terminal.'))));
    let d; try { d = await api('/api/extensions'); } catch (e) { main.append(errorBox(e.message)); return; }
    const defs = {
      mcp: ['MCP servers', [{ label: 'Server', key: 'name' }, { label: 'Status', fmt: r => r.status + (r.error ? ': ' + r.error : '') }, { label: 'Type', key: 'type' }, { label: 'Target', cls: 't', key: 'target' }, { label: 'Scope', key: 'scope' }, { label: 'Tools', cls: 'r', fmt: r => r.tools == null ? '' : r.tools }]],
      skills: ['Skills', [{ label: 'Skill', key: 'name' }, { label: 'Scope', key: 'scope' }, { label: 'Description', cls: 't', key: 'description' }]],
      plugins: ['Plugins', [{ label: 'Plugin', key: 'name' }, { label: 'Version', key: 'version' }, { label: 'State', fmt: r => r.enabled ? 'enabled' : 'disabled' }, { label: 'Provides', fmt: r => Object.entries(r.components).map(([k, v]) => v + ' ' + k).join(', ') }, { label: 'Description', cls: 't', key: 'description' }]],
      hooks: ['Hooks', [{ label: 'Event', key: 'event' }, { label: 'Matcher', key: 'matcher' }, { label: 'Type', key: 'type' }, { label: 'Command', cls: 't', key: 'command' }, { label: 'Source', cls: 't', key: 'source' }]],
      agents: ['Agents', [{ label: 'Agent', key: 'name' }, { label: 'Model', key: 'model' }, { label: 'Source', cls: 't', key: 'source' }, { label: 'Description', cls: 't', key: 'description' }]],
      commands: ['Slash commands', [{ label: 'Command', key: 'name' }, { label: 'Description', cls: 't', key: 'description' }]],
    };
    const tabs = h('div', { class: 'tabs', role: 'tablist' }); const pane = h('div'); main.append(tabs, pane);
    if (d.problems) main.append(h('p', { class: 'muted' }, 'Some sections could not load: ' + d.problems.join('; ')));
    function show(key) {
      state.ext = key;
      tabs.replaceChildren(...Object.entries({ ...Object.fromEntries(Object.entries(defs).map(([k, v]) => [k, v[0] + ' (' + (d[k] || []).length + ')'])), settings: 'Settings' }).map(([k, l]) => h('button', { role: 'tab', 'aria-selected': String(k === key), onclick: () => show(k) }, l)));
      if (key === 'settings') {
        const s = d.settings || {};
        pane.replaceChildren(h('dl', { class: 'kv' }, Object.entries(s).map(([k, v]) => [h('dt', null, k), h('dd', null, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))])));
      } else pane.replaceChildren(table(defs[key][1], d[key] || [], 'None configured.'));
    }
    show(state.ext && (state.ext in defs || state.ext === 'settings') ? state.ext : (Object.keys(defs).find(k => (d[k] || []).length) || 'mcp'));
  });
}

// ---------- share ----------
function shareModal(req) {
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Share' });
  const close = () => scrim.remove();
  scrim.addEventListener('click', e => { if (e.target === scrim) close(); });
  const box = h('div', { class: 'modal' }, h('p', { class: 'muted' }, 'Preparing a redacted copy...'));
  scrim.append(box); document.body.append(scrim);
  api('/api/share/preview', req).then(p => {
    const pre = h('pre'); pre.textContent = p.content;
    const out = h('div');
    const gistBtn = h('button', { class: 'btn', disabled: !(p.gh && p.gist_ok) }, 'Create secret GitHub gist...');
    gistBtn.addEventListener('click', () => {
      gistBtn.disabled = true;
      const confirmBtn = h('button', { class: 'btn primary' }, 'Confirm: upload exactly this file as a secret gist');
      confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true;
        try { const r = await api('/api/share/gist', { sha256: p.sha256, confirm: true }); out.replaceChildren(h('p', null, 'Created: ', h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer' }, r.url)), h('p', { class: 'muted' }, 'Secret gists are unlisted, not private: anyone with the link can read it.')); }
        catch (e) { confirmBtn.disabled = false; out.append(errorBox(e.message)); }
      });
      out.replaceChildren(h('p', null, 'Everything in the box above (' + p.bytes.toLocaleString() + ' bytes, sha256 ' + p.sha256.slice(0, 12) + '...) will be sent to GitHub through the gh CLI. Nothing is uploaded until you confirm.'), h('div', { class: 'acts' }, confirmBtn, h('button', { class: 'btn', onclick: () => { out.replaceChildren(); gistBtn.disabled = false; } }, 'Cancel')));
    });
    box.replaceChildren(h('h3', null, 'Share a copy'),
      h('p', { class: 'muted' }, 'One self-contained HTML file, no scripts. API keys, tokens, bearer headers and .env values were redacted. Review it before you send it anywhere.'),
      pre,
      h('div', { class: 'acts' }, h('a', { class: 'btn primary', href: '/api/share/file?sha=' + p.sha256, download: p.filename }, 'Download ' + p.filename), gistBtn, h('button', { class: 'btn', onclick: close }, 'Close'),
        h('span', { class: 'muted' }, p.bytes.toLocaleString() + ' bytes' + (p.gh ? '' : ' · gh CLI not found, gist unavailable') + (p.gist_note ? ' · ' + p.gist_note : ''))), out);
  }).catch(e => box.replaceChildren(errorBox(e.message), h('button', { class: 'btn', onclick: close }, 'Close')));
}


"""
