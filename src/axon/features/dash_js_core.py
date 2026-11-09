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
const NAV = [['chat', 'Chat', 'g c'], ['commands', 'Commands', 'g m'], ['sessions', 'Sessions', 'g s'], ['usage', 'Usage', 'g u'], ['insights', 'Insights', 'g i'], ['memory', 'Memory', 'g y'], ['extensions', 'Extensions', 'g e'], ['settings', 'Settings', 'g t']];
// Not in the rail, still one Cmd+K or one key away: Files and Terminal are right-hand panels, Shell is the line-based fallback inside the Terminal panel.
const NAV_PANELS = [['files', 'Files', 'g f', 'the project tree and search'], ['terminal', 'Terminal', 'g x', 'a real shell on this computer'], ['shell', 'Shell (one command at a time)', 'g h', 'the line shell, inside the Terminal panel']];
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
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Extensions'), h('div', { class: 'sub' }, 'MCP servers, skills, plugins, hooks and agents. Every button here runs the same command as in the terminal; its output appears below.'))));
    let d; try { d = await api('/api/extensions'); } catch (e) { main.append(errorBox(e.message)); return; }
    const out = h('div', { 'aria-live': 'polite' });
    const trustBox = h('div'); const tabs = h('div', { class: 'tabs', role: 'tablist' }); const pane = h('div');
    main.append(trustBox, tabs, pane, out);
    let busy = false;
    const reload = async () => { try { d = await api('/api/extensions'); } catch (e) { /* keep the old data */ } drawTrust(); show(state.ext); };
    const onDone = () => { if (!main.isConnected) { document.removeEventListener('axon-cmd-done', onDone); return; } reload(); };   // a confirmed command finished in its card
    document.addEventListener('axon-cmd-done', onDone);
    // Run a slash command, show its output, then re-read the lists.
    const run = async (line) => {   // destructive commands ask for confirmation in their own card
      if (busy) { toast('Another command is still running'); return; }
      busy = true; pane.classList.add('busy');
      try { await execLine(line, out); out.scrollIntoView({ block: 'nearest' }); } finally { busy = false; pane.classList.remove('busy'); }
      await reload();
    };
    const btn = (label, line, ask, cls) => h('button', { class: 'mini' + (cls ? ' ' + cls : ''), style: 'border-color:var(--ink2);padding:2px 9px', type: 'button', onclick: () => run(line, ask) }, label);
    const acts = (...b) => h('div', { class: 'formrow', style: 'gap:4px;flex-wrap:wrap;margin:0' }, ...b.filter(Boolean));
    const field = (ph, w) => h('input', { type: 'text', placeholder: ph, 'aria-label': ph, style: 'flex:1;min-width:' + (w || 140) + 'px', autocomplete: 'off', spellcheck: 'false' });
    const q = (s) => /^[A-Za-z0-9_.@:\/-]+$/.test(s) ? s : "'" + s.replace(/'/g, "'\\''") + "'";

    function drawTrust() {
      const t = d.trust || {}; trustBox.replaceChildren();
      if (!t.items || !t.items.length) return;
      const list = h('pre', { class: 'cmd-out', style: 'max-height:180px' }); list.textContent = t.items.map(i => i.kind + '  ' + i.source + ': ' + i.detail).join('\n');
      if (t.status === 'trusted') {
        trustBox.append(h('div', { class: 'notice' }, 'This folder is trusted: its ' + t.items.length + ' hook, plugin or server entries run. ',
          h('button', { class: 'mini', type: 'button', onclick: async () => { if (!confirm('Stop trusting this folder?')) return; try { const r = await api('/api/extensions/trust', { action: 'revoke' }); toast(r.message); } catch (e) { toast(e.message); } reload(); } }, 'Stop trusting')));
        return;
      }
      const why = t.status === 'changed' ? 'This folder was trusted before, but the list below changed since.' : 'This folder has project hooks, plugins or MCP servers that run programs on this computer.';
      trustBox.append(h('div', { class: 'card' }, h('h3', null, 'Not loaded: this folder is not trusted'), h('p', null, why + (t.gated ? ' They stay off until you allow them.' : '')), list,
        h('div', { class: 'formrow' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => { try { const r = await api('/api/extensions/trust', { action: 'trust', sha: t.sha }); toast(r.message); } catch (e) { toast(e.message); } reload(); } }, 'Trust this folder and load them'),
          h('span', { class: 'muted' }, 'Read the list first: trusting runs exactly these commands.'))));
    }

    const addMcp = () => {
      const name = field('name (letters, digits, - _)', 120), target = field('command and arguments, or a URL starting with http', 260);
      const scope = h('select', { 'aria-label': 'Scope' }, ['local', 'project', 'user'].map(v => h('option', { value: v }, v)));
      return h('details', { class: 'card' }, h('summary', null, 'Add an MCP server'),
        h('div', { class: 'formrow' }, name, target, scope, h('button', { class: 'btn primary', type: 'button', onclick: () => {
          const n = name.value.trim(), t = target.value.trim(); if (!n || !t) { toast('Give it a name and a command or URL'); return; }
          run('/mcp add ' + (/^https?:\/\//i.test(t) ? '--transport http ' + q(n) + ' ' + t : q(n) + ' ' + t) + ' --scope ' + scope.value);
        } }, 'Add')));
    };
    const mcpRow = (r) => acts(
      r.approval === 'pending' ? btn('Approve', '/mcp approve ' + q(r.name), null, 'primary') : null,
      r.approval === 'pending' ? btn('Reject', '/mcp reject ' + q(r.name)) : null,
      r.approval !== 'pending' ? btn(r.disabled ? 'Enable' : 'Disable', '/mcp ' + (r.disabled ? 'enable ' : 'disable ') + q(r.name)) : null,
      !r.disabled && r.approval !== 'pending' ? btn('Reconnect', '/mcp reconnect ' + q(r.name)) : null,
      /auth/i.test(r.status) ? btn('Sign in', '/mcp login ' + q(r.name) + ' --no-browser') : null,
      btn('Tools', '/mcp tools ' + q(r.name)), btn('Logs', '/mcp logs ' + q(r.name) + ' 40'),
      r.scope !== 'plugin' ? btn('Remove', '/mcp remove ' + q(r.name), 'Remove the MCP server ' + r.name + '?', 'danger') : null);

    const addAgent = () => {
      const name = field('name', 120), desc = field('what it is for', 260);
      return h('details', { class: 'card' }, h('summary', null, 'New agent'),
        h('div', { class: 'formrow' }, name, desc, h('button', { class: 'btn primary', type: 'button', onclick: () => { const n = name.value.trim(); if (!n) { toast('Give the agent a name'); return; } run('/agents create ' + q(n) + ' ' + desc.value.trim()); } }, 'Create')));
    };
    const addSkill = () => {
      const name = field('name', 120);
      return h('details', { class: 'card' }, h('summary', null, 'New skill'),
        h('div', { class: 'formrow' }, name, h('button', { class: 'btn primary', type: 'button', onclick: () => { const n = name.value.trim(); if (!n) { toast('Give the skill a name'); return; } run('/skills create ' + q(n)); } }, 'Create')));
    };

    // One block per item: a title line, a small detail line, then its buttons (a table is too narrow for them here).
    const blocks = (rows, title, detail, actions, empty) => !rows.length ? h('p', { class: 'muted' }, empty) :
      h('div', { class: 'xrows' }, ...rows.map(r => h('div', { class: 'xb' },
        h('div', { class: 'xb-t' }, h('b', null, title(r))),
        h('div', { class: 'muted xb-d' }, detail(r)), actions(r))));
    const defs = {
      mcp: ['MCP servers', [{ label: 'Server', key: 'name' }, { label: 'Status', fmt: r => (r.approval === 'pending' ? 'waiting for your approval' : r.status) + (r.error ? ': ' + r.error : '') }, { label: 'Type', key: 'type' }, { label: 'Target', cls: 't', key: 'target' }, { label: 'Scope', key: 'scope' }, { label: 'Tools', cls: 'r', fmt: r => r.tools == null ? '' : r.tools }, { label: '', fmt: mcpRow }], 'No MCP servers are configured. Add one above, or install a connector from the Marketplace tab.', [addMcp, () => acts(btn('Reconnect all', '/mcp reconnect all'), btn('Show all in terminal form', '/mcp list'))]],
      skills: ['Skills', [{ label: 'Skill', key: 'name' }, { label: 'Scope', key: 'scope' }, { label: 'From', fmt: r => r.plugin || '' }, { label: 'Description', cls: 't', key: 'description' }, { label: '', fmt: r => acts(btn('Show', '/skills show ' + q(r.name)), ['personal', 'project', 'user', 'local'].includes(r.scope) && !r.plugin ? btn('Delete', '/skills delete ' + q(r.name), 'Delete the skill ' + r.name + '?', 'danger') : null) }], 'No skills are installed.', [addSkill, () => acts(btn('Reload skills', '/reload-skills'), btn('Token cost of each skill', '/skill-doctor'))]],
      plugins: ['Plugins', [{ label: 'Plugin', key: 'name' }, { label: 'Version', key: 'version' }, { label: 'State', fmt: r => r.enabled ? 'enabled' : 'disabled' }, { label: 'Provides', fmt: r => Object.entries(r.components).map(([k, v]) => v + ' ' + k).join(', ') }, { label: 'Description', cls: 't', key: 'description' }, { label: '', fmt: r => acts(btn(r.enabled ? 'Disable' : 'Enable', '/plugin ' + (r.enabled ? 'disable ' : 'enable ') + q(r.name)), btn('Update', '/plugin update ' + q(r.name)), btn('Details', '/plugin info ' + q(r.name)), btn('Remove', '/plugin uninstall ' + q(r.name), 'Remove the plugin ' + r.name + '?', 'danger')) }], 'No plugins are installed. Find some in the Marketplace tab.', [() => acts(btn('Reload plugins', '/reload-plugins'))]],
      hooks: ['Hooks', [{ label: 'Event', key: 'event' }, { label: 'Matcher', key: 'matcher' }, { label: 'Type', key: 'type' }, { label: 'Command', cls: 't', key: 'command' }, { label: 'Source', cls: 't', key: 'source' }], 'No hooks are configured. Add a "hooks" section to .axon/settings.json, or install a plugin that has hooks.', [() => acts(btn('Reload hooks', '/hooks reload'), btn('All hook events', '/hooks events'))]],
      agents: ['Agents', [{ label: 'Agent', key: 'name' }, { label: 'Model', key: 'model' }, { label: 'Source', cls: 't', key: 'source' }, { label: 'Description', cls: 't', key: 'description' }, { label: '', fmt: r => acts(btn('Show', '/agents show ' + q(r.name)), r.source !== 'builtin' && !/^plugin/.test(r.source) ? btn('Delete', '/agents delete ' + q(r.name), 'Delete the agent ' + r.name + '?', 'danger') : null) }], 'No agents.', [addAgent, () => acts(btn('Check agent files', '/agents check'))]],
      commands: ['Slash commands', [{ label: 'Command', key: 'name' }, { label: 'Description', cls: 't', key: 'description' }], 'None.', []],
    };
    const mcpState = (r) => (r.approval === 'pending' ? 'waiting for your approval' : r.status) + (r.tools != null ? ' · ' + r.tools + ' tools' : '') + (r.error ? ' · ' + r.error : '');
    defs.mcp.push((rows) => blocks(rows, r => r.name + '  (' + r.scope + ', ' + r.type + ')', r => mcpState(r) + '\n' + r.target, mcpRow, defs.mcp[2]));
    defs.skills.push((rows) => blocks(rows, r => '/' + r.name + '  (' + r.scope + (r.plugin ? ', from ' + r.plugin : '') + ')', r => r.description || '', r => defs.skills[1][4].fmt(r), defs.skills[2]));
    defs.plugins.push((rows) => blocks(rows, r => r.name + '  ' + (r.version || '') + '  (' + (r.enabled ? 'enabled' : 'disabled') + ', ' + r.scope + ')', r => Object.entries(r.components).map(([k, v]) => v + ' ' + k).join(', ') + (r.description ? ' · ' + r.description : ''), r => defs.plugins[1][5].fmt(r), defs.plugins[2]));
    defs.agents.push((rows) => blocks(rows, r => r.name + '  (' + r.source.split('/').pop() + ', model ' + r.model + ')', r => r.description || '', r => defs.agents[1][4].fmt(r), defs.agents[2]));
    function marketPane() {
      const kind = h('select', { 'aria-label': 'What to browse' }, [['connectors', 'Connectors (MCP)'], ['skills', 'Skills'], ['plugins', 'Plugins']].map(([v, l]) => h('option', { value: v }, l)));
      const box = field('Search by name or description', 200); const res = h('div'); const src = h('div');
      const was = state.extMk || {}; kind.value = was.kind || 'connectors'; box.value = was.q || '';
      const add = field('Folder or git address of a marketplace you added yourself', 300);
      async function browse() {
        res.replaceChildren(h('p', { class: 'muted' }, 'Loading...')); state.extMk = { kind: kind.value, q: box.value };
        try {
          const m = await api('/api/marketplace?tab=' + kind.value + '&q=' + encodeURIComponent(box.value));
          const shown = m.entries.slice(0, 60);
          res.replaceChildren(h('p', { class: 'muted' }, m.total + ' found' + (m.total > shown.length ? ', showing ' + shown.length + '. Narrow the search to see others.' : '')),
            ...shown.map(e => h('div', { class: 'row', style: 'padding:6px 0;display:flex;gap:8px;align-items:baseline;border-bottom:1px solid var(--line)' },
              h('div', { style: 'flex:1;min-width:0' }, h('b', null, e.name), e.verified ? ' (verified)' : '', e.publisher ? h('span', { class: 'muted' }, '  by ' + e.publisher) : null, h('div', { class: 'muted', style: 'font-size:12px' }, (e.description || '').slice(0, 160) + (e.note ? ' · ' + e.note : ''))),
              e.installed ? h('span', { class: 'muted' }, 'installed') : null,
              e.installed ? btn('Remove', '/marketplace remove ' + q(e.id), 'Remove ' + e.name + '?', 'danger') : btn('Install', '/marketplace install ' + q(e.id), null, 'primary'))));
          if (!shown.length) res.append(h('p', { class: 'muted' }, 'Nothing found.'));
        } catch (e) { res.replaceChildren(errorBox(e.message)); }
      }
      kind.addEventListener('change', browse); box.addEventListener('keydown', (e) => { if (e.key === 'Enter') browse(); });
      add.addEventListener('keydown', (e) => { if (e.key === 'Enter') addBtn.click(); });
      const addBtn = h('button', { class: 'btn', type: 'button', onclick: async () => { if (add.value.trim()) { await run('/marketplace add ' + q(add.value.trim())); add.value = ''; showSources(); browse(); } } }, 'Add marketplace');
      async function showSources() {
        const r = await runSlashQuiet('/marketplace sources'); src.replaceChildren(h('details', null, h('summary', null, 'Marketplaces you added'), h('pre', { class: 'cmd-out' }, r), acts(btn('Refresh all', '/marketplace refresh'))));
      }
      browse(); showSources();
      return h('div', null, h('div', { class: 'formrow' }, kind, box, h('button', { class: 'btn primary', type: 'button', onclick: browse }, 'Search')), res,
        h('div', { class: 'card' }, h('h3', null, 'Add your own marketplace'), h('p', { class: 'muted' }, 'A folder or git repository with a marketplace.json file, or a folder of skills. Nothing is run when you add it; installing asks first.'), h('div', { class: 'formrow' }, add, addBtn)), src);
    }
    async function runSlashQuiet(line) {
      try { let r = await runSlash(line); while (r.done === false) { await new Promise(res => setTimeout(res, 500)); const j = await api('/api/command/job?id=' + encodeURIComponent(r.id)); r = Object.assign({}, r, j, { done: j.done }); }
        const t = document.createElement('div'); t.innerHTML = r.html || ''; return (t.textContent || r.message || '').trim() || 'None added.'; } catch (e) { return e.message; }
    }
    function show(key) {
      const order = Object.keys(defs).concat(['marketplace', 'settings']);
      key = order.includes(key) ? key : (Object.keys(defs).find(k => (d[k] || []).length) || 'mcp');
      state.ext = key;
      tabs.replaceChildren(...order.map(k => h('button', { role: 'tab', 'aria-selected': String(k === key), onclick: () => show(k) }, k === 'settings' ? 'Settings' : k === 'marketplace' ? 'Marketplace' : defs[k][0] + ' (' + (d[k] || []).length + ')')));
      if (key === 'settings') {
        const s = d.settings || {};
        pane.replaceChildren(h('dl', { class: 'kv' }, Object.entries(s).map(([k, v]) => [h('dt', null, k), h('dd', null, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))])));
      } else if (key === 'marketplace') pane.replaceChildren(marketPane());
      else pane.replaceChildren(...defs[key][3].map(f => f()), defs[key][4] ? defs[key][4](d[key] || []) : table(defs[key][1], d[key] || [], defs[key][2]));
    }
    if (d.problems) main.append(h('p', { class: 'muted' }, 'Some sections could not load: ' + d.problems.join('; ')));
    drawTrust(); show(state.ext);
  });
}

// The Share and Export dialog (shareModal) lives in dash_js_chatx.py.



"""
