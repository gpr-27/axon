"""Client script: the Context Inspector (``#/context`` and ``#/context/<section>``).

Every row of the right-hand Context panel opens this page. It reads ``POST /api/insights/ctxinspect`` and shows exactly what
the model receives, section by section: the full text of each message, system block, tool definition, memory file, rule and
hook, with syntax colour, a token count and a plain sentence on what it does. Server text only ever goes in as text nodes
(``h`` children) or through the dashboard's escaping markdown renderer; the syntax colour reuses ``hlLine`` from Files.
"""

JS_CONTEXT = r"""
// ---------- context inspector ----------
const CI_ORDER = ['overview', 'messages', 'system_prompt', 'system_tools', 'mcp_tools', 'mcp_instructions', 'skills', 'custom_agents', 'memory_files', 'autocompact', 'free', 'permissions', 'hooks'];
const CI_LABEL = { overview: 'Overview', messages: 'Messages', system_prompt: 'System prompt', system_tools: 'System tools', mcp_tools: 'MCP tools', mcp_instructions: 'MCP server instructions', skills: 'Skills', custom_agents: 'Custom agents', memory_files: 'Memory files', autocompact: 'Autocompact buffer', free: 'Free space', permissions: 'Permissions', hooks: 'Hooks' };
const CI_COLOR = { overview: 'var(--ink2)', messages: 'var(--c-msg)', system_prompt: 'var(--c-prompt)', system_tools: 'var(--c-sys)', mcp_tools: 'var(--c-mcp)', mcp_instructions: 'var(--c-minst)', skills: 'var(--c-skill)', custom_agents: 'var(--c-agent)', memory_files: 'var(--c-mem)', autocompact: 'var(--c-buf)', free: 'transparent', permissions: 'var(--ink2)', hooks: 'var(--ink2)' };
const CI_LANG = { markdown: 'md', md: 'md', bash: 'sh', shell: 'sh', sh: 'sh', zsh: 'sh', python: 'python', py: 'python', json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml', js: 'js', javascript: 'js', typescript: 'js', ts: 'js', diff: 'diff', patch: 'diff', go: 'go', rust: 'rust', sql: 'sql', text: 'text' };
const CI = { sid: '', cache: {}, q: '', open: new Set(), rend: new Set(), shown: 150 };
for (const [arr, v] of [[SINGLE_TAB, 'context']]) if (!arr.includes(v)) arr.push(v);
PAGE_TITLE.context = 'Context'; TAB_ICON.context = 'usage';

const ciN = (n) => n == null ? '—' : Number(n).toLocaleString();
const ciHash = (key, sid) => '#/context/' + key + (sid ? '?s=' + encodeURIComponent(sid) : '');
function ciSession(query) {
  const m = /(?:^|&)s=([^&]*)/.exec(query || ''); let sid = m ? decodeURIComponent(m[1]) : '';
  if (!sid && UI.chat && UI.chat.sid) sid = UI.chat.sid;
  if (!sid) { const t = UI.tabs.find(x => x.C && x.C.sid); if (t) sid = t.C.sid; }
  if (!sid) sid = lsGet('ci-sid') || '';
  if (sid) lsSet('ci-sid', sid);
  return sid;
}
// One source line, coloured by language. Diff lines are coloured by their first character; everything else reuses the Files highlighter.
function ciLine(text, lang) {
  const row = h('span', { class: 'ci-ln' });
  if (lang === 'diff') { row.classList.add(text[0] === '+' ? 'add' : text[0] === '-' ? 'del' : text.startsWith('@@') ? 'hunk' : 'ctx'); row.textContent = text || '​'; return row; }
  if (!text) { row.textContent = '​'; return row; }
  row.append(hlLine(text, lang)); return row;
}
// The full text in a code block. Only the first 300 lines are drawn; the rest arrives on request, so a huge body never stalls the page.
function ciCode(text, lang) {
  let src = String(text);
  if (lang === 'json' && src.length < 200000 && src.indexOf('\n') < 0) { try { src = JSON.stringify(JSON.parse(src), null, 2); } catch (e) { /* shown as it is */ } }
  const lines = src.split('\n'); const pre = h('pre', { class: 'ci-code', tabindex: '0', 'aria-label': 'Full text, ' + lines.length + ' lines' });
  const wrap = h('div', { class: 'ci-codewrap' }, pre); let n = 0;
  const more = h('button', { class: 'ci-more', type: 'button' }, '');
  const paint = (to) => { const frag = document.createDocumentFragment(); for (; n < Math.min(lines.length, to); n++) frag.append(ciLine(lines[n], lang)); pre.append(frag); more.hidden = n >= lines.length; more.textContent = 'Show ' + Math.min(500, lines.length - n).toLocaleString() + ' more lines (' + (lines.length - n).toLocaleString() + ' left)'; };
  more.addEventListener('click', () => paint(n + 500));
  paint(300); wrap.append(more); return wrap;
}
function ciBadge(b) {
  const tone = { pinned: 'ok', loaded: 'ok', enabled: 'ok', dropped: 'bad', deny: 'bad', allow: 'ok', ask: 'warn', deferred: 'warn', project: '', user: '' }[b] || '';
  return h('span', { class: 'ci-badge ' + tone }, b);
}
function ciCopy(text) { return h('button', { class: 'ci-btn', type: 'button', onclick: (e) => { axCopy(text).then(() => { const b = e.currentTarget; b.textContent = 'Copied'; setTimeout(() => { b.textContent = 'Copy'; }, 1200); }); } }, 'Copy'); }

function ciBody(it, reload, goto) {
  const box = h('div', { class: 'ci-open' });
  if (it.explain) box.append(h('div', { class: 'ci-explain' }, h('b', null, 'What this does'), h('span', null, it.explain)));
  if ((it.meta || []).length) box.append(h('dl', { class: 'ci-meta' }, ...it.meta.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, String(v == null ? '' : v))])));
  const text = typeof it.body === 'string' ? it.body : '';
  if (text) {
    const lang = CI_LANG[String(it.lang || 'text').toLowerCase()] || 'text'; const isMd = lang === 'md';
    const slot = h('div', { class: 'ci-slot' });
    const draw = () => { const rawOn = !isMd || !CI.rend.has(it.id) || text.length > 150000; slot.replaceChildren(rawOn ? ciCode(text, lang) : h('div', { class: 'ci-md', html: mdLite(text) })); };
    const seg = isMd ? h('span', { class: 'ci-seg', role: 'group', 'aria-label': 'Markdown view' }, ...[['Rendered', true], ['Raw', false]].map(([l, r]) => h('button', { type: 'button', 'aria-pressed': String(CI.rend.has(it.id) === r), onclick: (e) => { r ? CI.rend.add(it.id) : CI.rend.delete(it.id); [...e.currentTarget.parentNode.children].forEach(b => b.setAttribute('aria-pressed', String(b === e.currentTarget))); draw(); } }, l))) : null;
    const size = it.size_chars != null ? it.size_chars : text.length; const cut = it.body_truncated;
    box.append(h('div', { class: 'ci-codehead' }, h('span', { class: 'ci-lang' }, String(it.lang || 'text')), h('span', { class: 'ci-size' + (cut ? ' warn' : '') }, cut ? 'Cut at ' + text.length.toLocaleString() + ' of ' + size.toLocaleString() + ' characters to keep this page fast. The rest is not shown here.' : size.toLocaleString() + ' characters, shown in full'), h('span', { style: 'flex:1' }), seg, ciCopy(text)), slot);
    draw();
  } else box.append(h('p', { class: 'ci-none' }, 'This item has no text of its own.'));
  const acts = (it.actions || []).map(a => h('button', { class: 'ci-btn' + (/drop/i.test(a.label) ? ' danger' : ''), type: 'button', title: /drop/i.test(a.label) ? 'Replace with a short stub. The full text is saved to disk.' : /^pin$/i.test(a.label) ? 'Keep this in full, always' : '', onclick: async (e) => {
    e.currentTarget.disabled = true;
    try { const p = Object.assign({ session: CI.sid }, a.payload || {}); if (/\/(pin|unpin)$/.test(a.api || '') && p.target == null && p.id != null) p.target = p.id; const r = await api(a.api, p); toast((r && r.message) || (r && r.ok === false ? 'Not done' : 'Done')); CI.cache = {}; reload(true); } catch (er) { toast(er.message); e.currentTarget.disabled = false; }
  } }, a.label));
  const links = (it.links || []).map(l => h('a', { class: 'ci-btn', href: l.href }, l.label));
  const go = goto ? h('button', { class: 'ci-btn primary', type: 'button', onclick: goto.fn }, goto.label) : null;
  if (acts.length || links.length || go) box.append(h('div', { class: 'ci-acts' }, go, ...links, ...acts));
  return box;
}
function ciCard(it, max, reload, goto) {
  const open = CI.open.has(it.id); const body = h('div', { class: 'ci-bodyslot' });
  const card = h('article', { class: 'ci-card' + (open ? ' open' : ''), 'data-kind': it.kind || 'text', id: 'ci-' + String(it.id).replace(/[^\w-]/g, '_') });
  const head = h('button', { class: 'ci-head', type: 'button', 'aria-expanded': String(open), onclick: () => toggle(!card.classList.contains('open')) },
    h('span', { class: 'ci-chev', 'aria-hidden': 'true' }, '▸'),
    h('span', { class: 'ci-ttl' }, h('span', { class: 'ci-t1' }, it.title || '(untitled)'), it.subtitle ? h('span', { class: 'ci-t2' }, it.subtitle) : null),
    h('span', { class: 'ci-bd' }, ...(it.badges || []).map(ciBadge), it._bm ? h('span', { class: 'ci-badge' }, 'match in text') : null),
    it.tokens != null ? h('span', { class: 'ci-tk' }, h('span', { class: 'num' }, tok(it.tokens)), h('i', { class: 'ci-heat', 'aria-hidden': 'true' }, h('b', { style: 'width:' + Math.max(2, Math.round(100 * it.tokens / (max || 1))) + '%' }))) : h('span', { class: 'ci-tk' }));
  function toggle(on) {
    card.classList.toggle('open', on); head.setAttribute('aria-expanded', String(on)); on ? CI.open.add(it.id) : CI.open.delete(it.id);
    if (on && !body.firstChild) body.append(ciBody(it, reload, goto)); else if (!on) body.replaceChildren();
  }
  card.append(head, body); if (open) body.append(ciBody(it, reload, goto)); card.toggle = toggle;
  return card;
}

async function viewContext(rest) {
  const [path, query] = String(rest || '').split('?'); const first = path.split('/')[0];
  const key = CI_ORDER.includes(first) ? first : 'overview';
  return shell('context', async (pane) => {
    const sid = ciSession(query); CI.sid = sid; CI.q = '';
    const root = h('div', { class: 'ci' }); pane.append(root);
    const side = h('nav', { class: 'ci-side', 'aria-label': 'Context sections' }); const main = h('section', { class: 'ci-main', 'aria-live': 'polite' });
    const select = h('select', { class: 'ci-sel', 'aria-label': 'Context section', onchange: () => { location.hash = ciHash(select.value, sid); } }, ...CI_ORDER.map(k => h('option', { value: k, selected: k === key }, CI_LABEL[k])));
    root.append(h('header', { class: 'ci-top' }, h('div', null, h('h1', null, 'Context inspector'), h('p', { class: 'ci-sub' }, 'Everything the model receives with each request, in the order it counts against the window.')), h('a', { class: 'ci-btn', href: sid ? '#/chat/' + encodeURIComponent(sid) : '#/chat' }, 'Back to chat')), select, h('div', { class: 'ci-grid' }, side, main));
    let data = CI.cache[key] || null; let err = ''; let busy = false; let seq = 0;
    const sections = () => { const any = Object.values(CI.cache).find(d => d && d.sections); return any ? any.sections : null; };
    const drawSide = () => {
      const list = sections(); const win = (data && data.window) || (Object.values(CI.cache).find(d => d && d.window) || {}).window;
      side.replaceChildren(...CI_ORDER.map(k => {
        const s = (list || []).find(x => x.key === k) || (k === 'overview' ? { label: 'Overview' } : { label: CI_LABEL[k] });
        const on = k === key; const used = s.tokens != null && k !== 'overview';
        return h('a', { class: 'ci-sec' + (on ? ' on' : ''), href: ciHash(k, sid), 'aria-current': on ? 'page' : null, 'aria-label': (s.label || CI_LABEL[k]) + (s.tokens != null ? ', ' + ciN(s.tokens) + ' tokens' : '') },
          h('span', { class: 'ci-sq' + (k === 'free' ? ' free' : ''), style: 'background:' + CI_COLOR[k] }),
          h('span', { class: 'ci-sl' }, s.label || CI_LABEL[k], s.count != null && s.count > 0 ? h('em', null, s.count) : null),
          h('span', { class: 'ci-st num' }, k === 'overview' ? '' : s.tokens != null ? tok(s.tokens) : ''),
          used && win ? h('i', { class: 'ci-ub' }, h('b', { style: 'width:' + Math.max(s.tokens ? 2 : 0, Math.min(100, 100 * s.tokens / win)) + '%;background:' + (k === 'free' ? 'var(--line2)' : CI_COLOR[k]) })) : null);
      }));
    };
    const draw = () => {
      drawSide();
      if (!sid) { main.replaceChildren(h('div', { class: 'ci-state' }, h('strong', null, 'No chat to inspect'), h('p', null, 'The inspector reads the live conversation. Open a chat, send a message, then open the Context panel.'), h('a', { class: 'ci-btn primary', href: '#/chat' }, 'Open a chat'))); return; }
      if (err && !data) { main.replaceChildren(h('div', { class: 'ci-state bad', role: 'alert' }, h('strong', null, 'Could not read this section'), h('p', null, err), h('button', { class: 'ci-btn primary', type: 'button', onclick: () => load(true) }, 'Try again'))); return; }
      if (!data) { main.replaceChildren(h('div', { class: 'ci-skel', 'aria-busy': 'true', 'aria-label': 'Loading' }, h('i', { style: 'width:40%;height:22px' }), h('i', { style: 'width:80%' }), ...[0, 1, 2, 3, 4].map(() => h('i', { class: 'card' })))); return; }
      drawMain();
    };
    function drawMain() {
      const d = data; const items = d.items || []; const max = Math.max(1, ...items.map(i => i.tokens || 0));
      const shownItems = [];
      const reload = () => load(true);
      const goto = (it) => { const k = CI_ORDER.find(x => x === it.id || x === it.key) || CI_ORDER.find(x => (CI_LABEL[x] || '').toLowerCase() === String(it.title || '').toLowerCase()); return key === 'overview' && k && k !== 'overview' ? { label: 'Open ' + CI_LABEL[k], fn: () => { location.hash = ciHash(k, sid); } } : null; };
      const stat = h('div', { class: 'ci-stat' }, d.tokens != null ? h('b', { class: 'num' }, ciN(d.tokens)) : null, d.tokens != null ? h('span', null, ' tokens' + (d.pct != null ? ', ' + d.pct + '% of ' + tok(d.window || 0) : '') + (d.estimate ? ', estimated' : '')) : h('span', null, (d.items || []).length + ' entries'));
      const kids = [h('div', { class: 'ci-mh' }, h('div', { class: 'ci-mt' }, h('h2', null, d.title || CI_LABEL[key]), stat), h('p', { class: 'ci-intro' }, d.intro || ''))];
      if (key === 'overview' && d.window && (d.sections || []).length) {
        const segs = d.sections.filter(s => s.tokens && s.key !== 'free');
        kids.push(h('div', { class: 'ci-ov' }, h('div', { class: 'ci-stack', role: 'img', 'aria-label': 'How the context window is divided' }, ...segs.map(s => h('i', { style: 'width:' + Math.max(0.5, 100 * s.tokens / d.window) + '%;background:' + CI_COLOR[s.key], title: s.label + ': ' + ciN(s.tokens) + ' tokens' }))),
          h('div', { class: 'ci-leg' }, ...segs.map(s => h('a', { href: ciHash(s.key, sid) }, h('span', { class: 'ci-sq', style: 'background:' + CI_COLOR[s.key] }), s.label + ' ', h('b', { class: 'num' }, tok(s.tokens)))))));
      }
      const q1 = h('input', { type: 'search', class: 'ci-q', placeholder: 'Search titles and full text', 'aria-label': 'Filter items', value: CI.q, autocomplete: 'off' });
      const list = h('div', { class: 'ci-list' }); const count = h('span', { class: 'ci-count' });
      const cards = [];
      const fill = () => {
        const q = CI.q.trim(); cards.length = 0; list.replaceChildren(); const arr = shownItems; const lim = Math.min(arr.length, CI.shown); let grp = null; const frag = document.createDocumentFragment();
        const sums = {}; for (const i of arr) if (i.group) sums[i.group] = (sums[i.group] || 0) + (i.tokens || 0);
        arr.slice(0, lim).forEach(it => {
          if (it.group && it.group !== grp) { grp = it.group; frag.append(h('div', { class: 'ci-gh' }, h('b', null, grp), h('span', { class: 'ci-gc' }, (n => n + (n === 1 ? ' item' : ' items'))(arr.filter(x => x.group === grp).length)), h('span', { style: 'flex:1' }), sums[grp] ? h('span', { class: 'num ci-gt' }, tok(sums[grp]) + ' tokens') : null)); }
          const c = ciCard(it, max, reload, goto(it)); cards.push(c); frag.append(c);
        });
        if (!arr.length) frag.append(h('div', { class: 'ci-state' }, h('strong', null, q ? 'Nothing matches “' + CI.q + '”' : 'Nothing here'), h('p', null, q ? 'Search covers titles, groups and the full text of every item.' : (d.note || 'This section is empty right now. That is normal when the feature is not in use.'))));
        if (arr.length > lim) frag.append(h('button', { class: 'ci-more wide', type: 'button', onclick: () => { CI.shown += 150; fill(); } }, 'Show ' + Math.min(150, arr.length - lim) + ' more of ' + (arr.length - lim).toLocaleString() + ' remaining items'));
        list.append(frag);
        count.textContent = (q ? arr.length + ' of ' + items.length : items.length) + ' item' + (items.length === 1 ? '' : 's');
      };
      q1.addEventListener('input', () => { CI.q = q1.value; CI.shown = 150; drawMainList(); });
      q1.addEventListener('keydown', (e) => { if (e.key === 'Escape' && q1.value) { q1.value = ''; q1.dispatchEvent(new Event('input')); e.stopPropagation(); } });
      const drawMainList = () => { const qq = CI.q.trim().toLowerCase(); shownItems.length = 0; for (const i of items) { i._bm = false; if (!qq) { shownItems.push(i); continue; } const t = ((i.title || '') + ' ' + (i.subtitle || '') + ' ' + (i.group || '') + ' ' + (i.kind || '') + ' ' + (i.description || '') + ' ' + (i.explain || '') + ' ' + (i.meta || []).map(m => m[1]).join(' ')).toLowerCase(); if (t.includes(qq)) shownItems.push(i); else if (typeof i.body === 'string' && i.body.toLowerCase().includes(qq)) { i._bm = true; shownItems.push(i); } } fill(); };
      const expand = h('button', { class: 'ci-btn', type: 'button', onclick: () => { const all = cards.length && cards.every(c => c.classList.contains('open')); const cap = all ? cards : cards.slice(0, 40); cap.forEach(c => c.toggle(!all)); if (!all && cards.length > 40) toast('Opened the first 40 items. Open the rest one by one to keep the page fast.'); expand.textContent = all ? 'Expand all' : 'Collapse all'; } }, 'Expand all');
      const refresh = h('button', { class: 'ci-btn', type: 'button', onclick: () => load(true), 'aria-label': 'Refresh this section' }, busy ? 'Refreshing…' : 'Refresh');
      kids.push(h('div', { class: 'ci-tools' }, q1, count, h('span', { style: 'flex:1' }), expand, refresh));
      kids.push(list); if (d.note) kids.push(h('p', { class: 'ci-note' }, d.note));
      main.replaceChildren(...kids); main.scrollTop = 0; drawMainList();
    }
    async function load(force) {
      if (!sid) { draw(); return; }
      const mine = ++seq; busy = true; err = '';
      if (!data) draw();
      try { const d = await api('/api/insights/ctxinspect', { session: sid, section: key }); if (mine !== seq) return; CI.cache[key] = d; data = d; }
      catch (e) { if (mine !== seq) return; err = e.message || 'Request failed'; if (data) toast(err); }
      busy = false; if (mine === seq && root.isConnected) draw();
    }
    draw(); await load(false);
  }, { wide: true, hash: ciHash(key, ciSession(query)), title: 'Context' });
}
"""

CONTEXT_CSS = r"""
.pane.wide .ci{display:flex;flex-direction:column;height:100%;min-height:0;padding:14px 18px 0}
.ci-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding-bottom:12px}
.ci-top h1{font-size:17px;margin:0}.ci-sub{margin:2px 0 0;color:var(--muted);font-size:13px}
.ci-grid{display:grid;grid-template-columns:232px minmax(0,1fr);gap:0;min-height:0;flex:1;border:1px solid var(--line);border-bottom:0;border-radius:10px 10px 0 0;overflow:hidden;background:var(--surface)}
.ci-side{display:flex;flex-direction:column;gap:1px;padding:8px;border-right:1px solid var(--line);overflow-y:auto;background:var(--surface)}
.ci-sec{position:relative;display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:4px 9px;align-items:center;padding:7px 9px;border-radius:7px;color:var(--ink2);text-decoration:none;font-size:13px;border:0}
.ci-sec:hover{background:var(--raised);color:var(--ink)}
.ci-sec.on{background:var(--raised);color:var(--ink);box-shadow:inset 2px 0 0 var(--primary)}
.ci-sec:focus-visible,.ci-btn:focus-visible,.ci-head:focus-visible,.ci-more:focus-visible,.ci-q:focus-visible,.ci-sel:focus-visible,.ci-seg button:focus-visible{outline:2px solid var(--primary);outline-offset:1px}
.ci-sec:nth-child(1){margin-bottom:6px}.ci-sec:nth-child(12){margin-top:6px}
.ci-sq{width:9px;height:9px;border-radius:3px;display:block}.ci-sq.free{border:1px solid var(--line2)}
.ci-sl{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ci-sl em{font-style:normal;color:var(--muted);font-size:11.5px;margin-left:6px}
.ci-st{font-size:12px;color:var(--muted)}.ci-sec.on .ci-st{color:var(--ink2)}
.ci-ub{grid-column:2/4;height:2px;border-radius:2px;background:var(--raised2);overflow:hidden;display:block}.ci-ub b{display:block;height:100%}
.ci-main{overflow-y:auto;min-width:0;padding:16px 20px 40px;position:relative}
.ci-mh{margin-bottom:10px}.ci-mt{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}.ci-mt h2{margin:0;font-size:16px}
.ci-stat{color:var(--muted);font-size:12.5px}.ci-stat b{color:var(--ink);font-size:14px;font-weight:600}
.ci-intro{margin:6px 0 0;max-width:78ch;color:var(--ink2);font-size:13.5px;line-height:1.55}
.ci-ov{margin:12px 0 4px}.ci-stack{display:flex;gap:1px;height:10px;border-radius:5px;background:var(--raised2);overflow:hidden}.ci-stack i{display:block;height:100%;min-width:2px}
.ci-leg{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:9px;font-size:12.5px}.ci-leg a{display:inline-flex;align-items:center;gap:6px;color:var(--ink2);text-decoration:none}.ci-leg a:hover{color:var(--ink)}.ci-leg b{color:var(--ink);font-weight:500}
.ci-tools{display:flex;align-items:center;gap:8px;margin:14px 0 10px;flex-wrap:wrap}
.ci-q{flex:0 1 320px;min-width:160px;height:32px;box-sizing:border-box;line-height:1.2;background:var(--page);border:1px solid var(--line);border-radius:7px;padding:6px 10px;color:var(--ink);font-size:13px}
.ci-count{color:var(--muted);font-size:12.5px}
.ci-btn{display:inline-flex;align-items:center;gap:6px;background:var(--page);border:1px solid var(--line);border-radius:6px;padding:4px 10px;color:var(--ink2);font-size:12.5px;cursor:pointer;text-decoration:none;line-height:1.3}
.ci-btn:hover{color:var(--ink);border-color:var(--line2)}.ci-btn.primary{border-color:var(--primary);color:var(--ink)}.ci-btn.danger:hover{color:var(--bad);border-color:var(--bad)}.ci-btn:disabled{opacity:.5;cursor:default}
.ci-list{display:flex;flex-direction:column;gap:6px}
.ci-gh{position:sticky;top:-16px;z-index:2;display:flex;align-items:center;gap:10px;margin:10px -4px 0;padding:7px 4px 6px;background:var(--surface);border-bottom:1px solid var(--line);font-size:12px;color:var(--ink2)}
.ci-gh b{color:var(--ink);font-weight:600;letter-spacing:.01em}.ci-gc,.ci-gt{color:var(--muted)}
.ci-card{border:1px solid var(--line);border-radius:8px;background:var(--page);overflow:hidden}
.ci-card.open{border-color:var(--line2)}
.ci-head{display:grid;grid-template-columns:14px minmax(0,1fr) auto auto;gap:10px;align-items:center;width:100%;padding:8px 12px;background:none;border:0;color:var(--ink);text-align:left;cursor:pointer;font:inherit}
.ci-head:hover{background:var(--raised)}
.ci-chev{color:var(--muted);font-size:11px;transition:transform .12s}.ci-card.open .ci-chev{transform:rotate(90deg);color:var(--ink)}
.ci-ttl{display:flex;flex-direction:column;min-width:0;gap:1px}
.ci-t1{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ci-t2{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ci-bd{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}
.ci-badge{border:1px solid var(--line2);border-radius:999px;padding:0 7px;font-size:11px;color:var(--ink2);line-height:17px}
.ci-badge.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 45%,transparent)}.ci-badge.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}.ci-badge.bad{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
.ci-tk{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:54px;font-size:12.5px;color:var(--ink2)}
.ci-heat{display:block;width:54px;height:3px;border-radius:2px;background:var(--raised2);overflow:hidden}.ci-heat b{display:block;height:100%;background:var(--primary);opacity:.8}
.ci-open{border-top:1px solid var(--line);padding:12px 14px 14px;display:flex;flex-direction:column;gap:12px;background:var(--surface)}
.ci-explain{display:flex;flex-direction:column;gap:2px;border-left:2px solid var(--primary);padding:2px 0 2px 10px;font-size:13px;color:var(--ink2);line-height:1.5}.ci-explain b{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.ci-meta{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 16px;margin:0;font-size:12.5px}.ci-meta dt{color:var(--muted)}.ci-meta dd{margin:0;color:var(--ink);overflow-wrap:anywhere;font-family:ui-monospace,Menlo,monospace;font-size:12px}
.ci-codehead{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--muted);margin-bottom:-6px}
.ci-lang{border:1px solid var(--line2);border-radius:4px;padding:0 6px;color:var(--ink2);font-family:ui-monospace,Menlo,monospace;font-size:11px}.ci-size.warn{color:var(--warn)}
.ci-seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}.ci-seg button{background:var(--page);border:0;padding:3px 9px;color:var(--ink2);font-size:12px;cursor:pointer}.ci-seg button[aria-pressed=true]{background:var(--raised2);color:var(--ink)}
.ci-codewrap{border:1px solid var(--line);border-radius:7px;background:var(--page);overflow:hidden}
.ci-code{margin:0;padding:10px 0;max-height:520px;overflow:auto;font:12px/1.55 ui-monospace,Menlo,monospace;color:var(--ink);counter-reset:ln;white-space:pre-wrap;overflow-wrap:anywhere;background:none;border:0;tab-size:4}
.ci-ln{display:block;padding:0 12px 0 52px;position:relative;counter-increment:ln;min-height:1.55em}
.ci-ln::before{content:counter(ln);position:absolute;left:0;width:40px;text-align:right;color:var(--muted);opacity:.6;user-select:none;font-variant-numeric:tabular-nums}
.ci-ln.add{background:color-mix(in srgb,var(--ok) 13%,transparent)}.ci-ln.del{background:color-mix(in srgb,var(--bad) 13%,transparent)}.ci-ln.hunk{color:var(--info)}.ci-ln.ctx{color:var(--ink2)}
.ci-md{padding:10px 14px;font-size:13.5px;line-height:1.6;max-height:520px;overflow:auto;background:var(--page);border:1px solid var(--line);border-radius:7px}.ci-md p{margin:.4em 0}.ci-md pre{overflow:auto}
.ci-more{background:none;border:0;border-top:1px solid var(--line);width:100%;padding:7px;color:var(--info);font-size:12.5px;cursor:pointer}.ci-more:hover{background:var(--raised)}.ci-more.wide{border:1px dashed var(--line2);border-radius:8px;margin-top:4px}
.ci-acts{display:flex;gap:8px;flex-wrap:wrap}.ci-none{margin:0;color:var(--muted);font-size:13px}.ci-note{margin:14px 0 0;color:var(--muted);font-size:12.5px;max-width:78ch}
.ci-state{display:flex;flex-direction:column;align-items:flex-start;gap:6px;padding:26px 22px;border:1px dashed var(--line2);border-radius:10px;color:var(--ink2);font-size:13.5px;margin:6px 0}.ci-state strong{color:var(--ink);font-size:14.5px}.ci-state p{margin:0 0 6px;max-width:60ch;line-height:1.5}.ci-state.bad{border-color:var(--bad)}
.ci-skel{display:flex;flex-direction:column;gap:10px}.ci-skel i{display:block;height:14px;border-radius:6px;background:linear-gradient(90deg,var(--raised) 25%,var(--raised2) 50%,var(--raised) 75%);background-size:200% 100%}.ci-skel i.card{height:40px;border-radius:8px}
@media (prefers-reduced-motion:no-preference){.ci-skel i{animation:ci-sh 1.3s linear infinite}}@keyframes ci-sh{to{background-position:-200% 0}}
.ci-sel{display:none;width:100%;margin:0 0 10px;background:var(--page);border:1px solid var(--line);border-radius:7px;padding:8px 10px;color:var(--ink);font-size:14px}
@media (max-width:760px){
 .pane.wide .ci{padding:10px 12px 0}
 .ci-grid{grid-template-columns:1fr}.ci-side{display:none}.ci-sel{display:block}
 .ci-sub{display:none}.ci-main{padding:12px 12px 32px}
 .ci-head{grid-template-columns:12px minmax(0,1fr) auto}.ci-bd{display:none}
 .ci-q{flex:1 1 100%}.ci-gh{top:-12px}.ci-meta{grid-template-columns:1fr}.ci-meta dt{margin-top:4px}
}
/* the right-hand Context panel: each row is a link to the inspector, with an optional small peek */
.cx-row.lnk{position:relative;cursor:pointer;grid-template-columns:12px minmax(0,1fr) auto 54px}
.cx-row.lnk a.cl{color:inherit;text-decoration:none}.cx-row.lnk a.cl::after{content:'';position:absolute;inset:-1px -4px}
.cx-row.lnk:hover,.cx-row.lnk:focus-within{color:var(--ink)}.cx-row.lnk:hover .cl{text-decoration:underline;text-decoration-color:var(--muted);text-underline-offset:3px}
.cx-row.lnk a.cl:focus-visible{outline:2px solid var(--primary);outline-offset:2px;border-radius:3px}
.cx-peek{position:relative;z-index:1;background:none;border:0;color:var(--muted);cursor:pointer;padding:0 4px;font-size:10px;margin-left:2px;border-radius:3px}.cx-peek:hover{color:var(--ink)}.cx-peek[aria-expanded=true]{transform:rotate(90deg)}
.cx-more{display:flex;gap:12px;margin-top:8px;padding-top:7px;border-top:1px solid var(--line);font-size:12.5px}.cx-more a{color:var(--info);text-decoration:none}.cx-more a:hover{text-decoration:underline}
"""
