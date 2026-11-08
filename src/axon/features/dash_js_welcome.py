"""Client script and routes for the first-run welcome, starter prompts, "?" help, the project overview and recent projects.

The data comes from ``axon.features.project_overview`` (shared with the terminal's ``/overview``). This module is the web
side: the routes under ``/api/welcome`` and ``/api/project``, and the page script. Everything is drawn with text nodes.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer


def _redact(v: Any) -> Any:
    """Commit subjects, chat titles and note names may mention a key: run every string through the dashboard's redactor."""
    from axon.features.dashboard import redact_text
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, list):
        return [_redact(x) for x in v]
    if isinstance(v, dict):
        return {k: _redact(x) for k, x in v.items()}
    return v


def _same(a: str, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == str(b)


def recent_view(srv: "DashServer") -> dict[str, Any]:
    from axon.features import project_overview as po
    rows = po.recent_projects(limit=12)
    for r in rows:
        r["current"] = _same(r["path"], srv.workspace)
    return {"projects": _redact(rows), "can_switch": srv.live is None, "workspace": str(srv.workspace)}


def handle_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any:
    from axon.features import dash_providers as dp
    from axon.features import project_overview as po
    if route == "/api/welcome":
        ready, why = dp.provider_ready(srv, None)
        return {**po.welcome_state(), "ready": ready, "reason": why}
    if route == "/api/project/overview":
        return _redact(po.overview(srv.workspace))
    if route == "/api/project/starters":
        return {"prompts": _redact(po.starter_prompts(srv.workspace))}
    if route == "/api/project/recent":
        return recent_view(srv)
    raise DashError("not found", 404)


def handle_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any:
    from axon.features import project_overview as po
    if route == "/api/welcome/done":
        return po.mark_welcome(skipped=body.get("skipped") is True)
    if route == "/api/project/recent/remove":
        path = body.get("path")
        if not isinstance(path, str) or not path or len(path) > 4096:
            raise DashError("'path' must be a project folder from the list")
        if not po.remove_recent(path):
            raise DashError("that project is not in the recent list", 404)
        return {"ok": True, **recent_view(srv)}
    raise DashError("not found", 404)


WELCOME_CSS = r"""
.helptip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;margin-left:6px;padding:0;border:1px solid var(--line2,var(--line));border-radius:50%;background:none;color:var(--muted);font:600 10.5px/1 inherit;cursor:pointer;vertical-align:middle}
.helptip:hover,.helptip:focus-visible,.helptip[aria-expanded=true]{color:var(--ink);border-color:var(--primary)}
.helppop{position:fixed;z-index:70;width:min(300px,calc(100vw - 24px));background:var(--raised);border:1px solid var(--line2,var(--line));border-radius:8px;padding:10px 12px;box-shadow:0 10px 30px rgb(0 0 0/.35);font-size:13px;line-height:1.45;color:var(--ink2);font-weight:400;letter-spacing:0}
.helppop b{display:block;color:var(--ink);margin-bottom:3px;font-size:13px}
.starters{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.starters button{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 12px;color:var(--ink);cursor:pointer;font:inherit;font-size:13.5px;text-align:left;max-width:100%;overflow-wrap:anywhere}
.starters button:hover,.starters button:focus-visible{border-color:var(--primary)}
.welcome{width:min(660px,100%)}
.welcome h2{margin:0 0 6px;font-size:20px;letter-spacing:0;color:var(--ink)}
.welcome .wl-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;color:var(--muted);font-size:12.5px}
.welcome .wl-group{margin:14px 0 6px;font-weight:600;color:var(--ink);font-size:13.5px}
.welcome .wl-picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px}
.welcome .wl-picks button{display:flex;flex-direction:column;gap:2px;text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--ink);cursor:pointer;font:inherit;font-size:13.5px}
.welcome .wl-picks button span{color:var(--muted);font-size:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.welcome .wl-picks button:hover,.welcome .wl-picks button:focus-visible{border-color:var(--primary)}
.welcome .wl-path{overflow-wrap:anywhere;background:var(--code-bg);padding:6px 10px;border-radius:6px;margin:10px 0}
.pm-row{display:flex;align-items:center;gap:8px;padding:6px 10px;color:var(--muted);font-size:13px}
.pm-row .pm-n{flex:1 1 auto;white-space:nowrap;min-width:6ch}
.pm-row .pm-w{font-size:12px}
details.ov{border:1px solid var(--line);border-radius:8px;background:var(--surface);margin:12px 0 16px;padding:0}
details.ov>summary{padding:10px 14px;font-weight:600;font-size:14px;color:var(--ink)}
.ov-body{padding:0 14px 12px}
.ov-grid{display:grid;grid-template-columns:110px minmax(0,1fr);gap:8px 14px;margin:0;font-size:13.5px}
.ov-grid dt{color:var(--muted)}.ov-grid dd{margin:0;overflow-wrap:anywhere}
.ov-grid ul{list-style:none;margin:4px 0 0;padding:0}.ov-grid li{display:flex;gap:10px;padding:1px 0}
.ov-grid li code{flex:none}.ov-grid li .muted{flex:none;margin-left:auto}
.linkbtn{background:none;border:0;padding:0;color:var(--primary);cursor:pointer;font:inherit;text-align:left}
.linkbtn:hover,.linkbtn:focus-visible{text-decoration:underline}
@media (max-width:600px){.ov-grid{grid-template-columns:minmax(0,1fr)}.ov-grid dt{margin-top:6px}}
"""

JS_WELCOME = r"""
// ---------- first-run welcome, starter prompts, help tips, project overview, recent projects ----------
const HELP = {
  'progress:Environment': ['Environment', 'The folder Axon works in, whether shell commands run inside a sandbox, and the git branch. Click the folder or branch to change them.'],
  'progress:Context': ['Context', 'Context is what Axon keeps in mind during this chat: your messages, the files it read and what its tools returned. The model can hold only so much, so when the bar is nearly full Axon summarises the older parts to make room.'],
  'progress:Usage': ['Usage', 'Tokens are the small pieces of text a model reads and writes, and what you are charged for. Cache hit is the share of this turn’s input that the provider had already seen, which costs less and is faster.'],
  'progress:Plan': ['Plan', 'The steps Axon wrote down for itself for this task, ticked off as it goes.'],
  'progress:Running tools': ['Running tools', 'What Axon is doing right now, such as reading a file or running a command.'],
  'progress:Sub-agents': ['Sub-agents', 'Helpers Axon started for a side task. Each has its own short conversation and reports back when it is done.'],
  'progress:Queue': ['Queue', 'Messages you sent while Axon was busy. They go out in order when it finishes.'],
  'progress:Background shells': ['Background shells', 'Commands still running behind the scenes, such as a development server.'],
  'progress:Connectors': ['Connectors', 'Outside tools Axon can use, connected through MCP. Each connector adds tools to what Axon can do.'],
  'progress:Changes': ['Changes', 'Files that differ from the last git commit, including edits Axon made. Open the Changes panel to read them.'],
  'mode': ['Mode', 'The mode decides what Axon may do without asking. “Ask before edits and commands” is the safest. “Plan only” lets it read but not change anything, and the last one skips every question, so use it only in a folder you can afford to lose.'],
  'page:usage': ['Usage', 'What your model calls cost, counted in tokens, with the savings from cache. The figures come from Axon’s own log on this computer.'],
  'page:insights': ['Insights', 'An overview of this project, then why the agent did what it did and what fills its context window. Most tabs read the chat you have open.'],
  'page:sessions': ['Sessions', 'Every saved chat in this project. Open one to carry on, or tag, star and delete it.'],
  'page:memory': ['Memory', 'Notes Axon keeps between chats, such as your preferences and facts about the project. You can change or delete any of them.'],
  'page:extensions': ['Extensions', 'Skills, plugins and connectors that give Axon more things it can do.'],
  'page:commands': ['Commands', 'Axon’s own / commands, like /status or /compact. They are not shell commands.'],
};
function helpTip(key) {
  const t = HELP[key]; if (!t) return null;
  const b = h('button', { class: 'helptip', type: 'button', 'aria-label': 'What does ' + t[0] + ' mean?', 'aria-expanded': 'false', title: 'What is this?' }, '?');
  b.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); helpPop(b, t); });
  return b;
}
function helpPop(anchor, t) {
  const old = document.getElementById('helppop');
  if (old) { const same = old.dataset.id === t[0]; old.remove(); document.querySelectorAll('.helptip[aria-expanded=true]').forEach(x => x.setAttribute('aria-expanded', 'false')); if (same) return; }
  const pop = h('div', { class: 'helppop', id: 'helppop', role: 'note', 'data-id': t[0] }, h('b', null, t[0]), t[1]);
  document.body.append(pop); anchor.setAttribute('aria-expanded', 'true');
  const r = anchor.getBoundingClientRect(); const w = pop.offsetWidth;
  pop.style.left = Math.max(12, Math.min(r.left - 8, innerWidth - w - 12)) + 'px';
  let top = r.bottom + 6; if (top + pop.offsetHeight > innerHeight - 8) top = Math.max(8, r.top - pop.offsetHeight - 6); pop.style.top = top + 'px';
  const close = () => { pop.remove(); anchor.setAttribute('aria-expanded', 'false'); document.removeEventListener('mousedown', away, true); document.removeEventListener('keydown', key, true); };
  const away = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };
  const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); anchor.focus(); } };
  setTimeout(() => { document.addEventListener('mousedown', away, true); document.addEventListener('keydown', key, true); }, 0);
}
function helpPageHook(pane, route) {
  const k = 'page:' + route; if (!HELP[k]) return;
  const h1 = pane.querySelector('.head h1'); if (!h1 || h1.querySelector('.helptip')) return;
  h1.append(helpTip(k));
}

// ----- put text in the chat box (a starter prompt, or "ask Axon to run the tests") -----
function fillChatBox(text) {
  const put = () => { const C = UI.chat; if (!C || !C.ta) return false; C.ta.value = text; C.ta.dispatchEvent(new Event('input')); C.ta.focus(); C.ta.setSelectionRange(text.length, text.length); return true; };
  if (UI.active && UI.active.route === 'chat' && put()) return;
  location.hash = '#/chat';
  let n = 0; const t = setInterval(() => { if (put() || ++n > 30) clearInterval(t); }, 100);
}

// ----- the empty chat: starter prompts picked from the real project -----
function welcomeEmptyChat(C) {
  const ws = state.ws || {}; const row = h('div', { class: 'starters', role: 'group', 'aria-label': 'Ways to start' });
  const el = h('div', { class: 'empty-chat' }, h('h2', null, 'Ask Axon about ' + (ws.name || 'this project')),
    h('p', null, 'Pick a start, or type your own below.'), row);
  api('/api/project/starters').then((d) => {
    row.replaceChildren(...d.prompts.map(p => h('button', { type: 'button', title: p.prompt, onclick: () => { const ta = C.ta; if (!ta) return; ta.value = p.prompt; ta.dispatchEvent(new Event('input')); ta.focus(); ta.setSelectionRange(p.prompt.length, p.prompt.length); } }, p.label)));
  }).catch(() => { /* the box still works without suggestions */ });
  return el;
}

// ----- first-run welcome -----
async function welcomeOpen(manual) {
  if (document.querySelector('.welcome-scrim')) return;
  const scrim = h('div', { class: 'scrim welcome-scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Welcome to Axon' });
  const box = h('div', { class: 'modal welcome' }); scrim.append(box); document.body.append(scrim);
  let timer = 0;
  const shut = () => { clearInterval(timer); scrim.remove(); };
  const finish = async (skipped) => { try { await api('/api/welcome/done', { skipped }); } catch (e) { /* asked again next time */ } shut(); };
  scrim.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !document.querySelector('.scrim:not(.welcome-scrim)')) { e.stopPropagation(); manual ? shut() : finish(true); } });
  const note = (el, msg, cls) => { el.replaceChildren(notice(msg, cls)); };
  let providers = [], ready = { ready: false, reason: '' }, picked = null, step = 1, more = false;
  try { [providers, ready] = await Promise.all([api('/api/providers').then(d => d.providers), api('/api/provider/ready').catch(() => ({ ready: false, reason: '' }))]); } catch (e) { box.append(errorBox(e.message)); return; }
  const top = (n) => h('div', { class: 'wl-top' }, h('span', null, 'Step ' + n + ' of 2'), h('button', { class: 'btn', type: 'button', onclick: () => (manual ? shut() : finish(true)) }, manual ? 'Close' : 'Skip for now'));
  const online = () => providers.filter(p => p.requires_key);
  const pick = (p) => h('button', { type: 'button', onclick: () => { picked = p; draw(); } }, p.name, h('span', null, p.description));
  function draw() {
    clearInterval(timer);
    if (step === 2) return drawFolder();
    const kids = [top(1), h('h2', null, manual ? 'Set up Axon' : 'Welcome to Axon')];
    if (!picked) {
      kids.push(h('p', { class: 'muted' }, 'Axon is a helper that reads and changes the files in a folder on this computer, and asks before it does anything risky. It needs an AI model to think with.'));
      if (ready.ready) kids.push(notice('A model is already set up, so chat works. You can change it here.', 'ok'));
      kids.push(h('div', { class: 'wl-group' }, 'Use an online service'), h('p', { class: 'muted' }, 'You need an account with the service and a key from it. Keys stay on this computer.'),
        h('div', { class: 'wl-picks' }, ...online().slice(0, more ? 99 : 6).map(pick)), online().length > 6 && !more ? h('button', { class: 'btn', type: 'button', style: 'margin-top:8px', onclick: () => { more = true; draw(); } }, 'Show all ' + online().length + ' services') : null,
        h('div', { class: 'wl-group' }, 'Or run a model on this computer'), h('p', { class: 'muted' }, 'No key and no cost, but the model program must already be installed and running.'),
        h('div', { class: 'wl-picks' }, ...providers.filter(p => !p.requires_key).map(pick)));
      if (ready.ready) kids.push(h('div', { class: 'acts' }, h('button', { class: 'btn primary', type: 'button', onclick: () => { step = 2; draw(); } }, 'Keep it and choose a folder')));
    } else {
      const out = h('div');
      const row = providerRow(picked, note, () => { step = 2; draw(); }); row.open = true;
      kids.push(h('p', { class: 'muted' }, picked.requires_key ? 'Paste your key, choose Test to check it works, then Use this provider.' : 'Choose Test to check the program is running, then Use this provider.'), row, out,
        h('div', { class: 'acts' }, h('button', { class: 'btn', type: 'button', onclick: () => { picked = null; draw(); } }, 'Choose a different one')));
    }
    box.replaceChildren(...kids); box.querySelector('button.btn.primary, .wl-picks button, summary')?.focus();
  }
  function drawFolder() {
    const path = h('div', { class: 'wl-path' }, (state.ws || {}).workspace || '');
    const locked = !(state.ws || {}).can_switch;
    let shown = (state.ws || {}).workspace; timer = setInterval(() => { const w = (state.ws || {}).workspace; if (w !== shown) { shown = w; path.textContent = w || ''; } }, 400);
    box.replaceChildren(top(2), h('h2', null, 'Pick a folder'), h('p', { class: 'muted' }, 'Axon works inside one folder: your project. It will not look outside it without asking.'), path,
      locked ? notice('This page is attached to a running terminal session, so the folder is the one that session uses.', 'warn') : null,
      h('div', { class: 'acts' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => { await finish(false); location.hash = '#/chat'; toast('Ready. Ask Axon something about your project.'); } }, 'Finish and open the chat'),
        h('button', { class: 'btn', type: 'button', disabled: locked, onclick: () => folderChooser() }, 'Choose a different folder'), h('button', { class: 'btn', type: 'button', onclick: () => { step = 1; draw(); } }, 'Back')));
    box.querySelector('.btn.primary')?.focus();
  }
  draw();
}
async function welcomeBoot() {
  projLoad();
  try {
    const w = await api('/api/welcome'); if (w.done) return;
    if (w.ready) { await api('/api/welcome/done', { skipped: false }); return; }   // a working setup needs no guide
    welcomeOpen(false);
  } catch (e) { /* the banner in the chat still points to Providers */ }
}

// ----- project overview (top of Insights) -----
function ago(sec) { sec = Math.max(0, sec | 0); return sec < 90 ? 'just now' : sec < 5400 ? Math.round(sec / 60) + ' min ago' : sec < 172800 ? Math.round(sec / 3600) + ' h ago' : Math.round(sec / 86400) + ' days ago'; }
function overviewSection() {
  const body = h('div', { class: 'ov-body' }, h('p', { class: 'muted' }, 'Reading the project...'));
  let open = true; try { open = lsGet('axon-ov-open') !== '0'; } catch (e) { open = true; }
  const det = h('details', { class: 'ov', open }, h('summary', null, 'Project overview'), body);
  det.addEventListener('toggle', () => lsSet('axon-ov-open', det.open ? '1' : '0'));
  api('/api/project/overview').then(d => overviewFill(body, d)).catch(e => body.replaceChildren(errorBox(e.message)));
  return det;
}
function overviewFill(body, d) {
  const link = (text, fn, title) => h('button', { class: 'linkbtn', type: 'button', title: title || '', onclick: fn }, text);
  const rows = [];
  const f = d.files; const langs = f.languages.slice(0, 4).map(l => l.name + ' ' + Math.round(l.pct) + '%').join(', ');
  rows.push(['Files', h('span', null, link(f.count.toLocaleString() + (f.truncated ? '+' : '') + ' files', () => wbOpen('files'), 'Open the Files panel'), langs ? ' · ' + langs : '')]);
  const g = d.git;
  if (!g.repo) rows.push(['Git', 'Not a git repository.']);
  else rows.push(['Git', h('div', null, link(g.branch + ' · ' + (g.changed ? g.changed + ' changed file' + (g.changed > 1 ? 's' : '') : 'no uncommitted changes'), () => wbOpen('changes'), 'Open the Changes panel'),
    g.commits.length ? h('ul', null, ...g.commits.map(c => h('li', null, h('code', null, c.hash), h('span', null, c.subject), h('span', { class: 'muted' }, ago(c.age_s))))) : h('div', { class: 'muted' }, 'No commits yet.'))]);
  const t = d.tests;
  if (!t.detected.command) rows.push(['Tests', 'No test runner found (looked for pytest, npm, cargo and go).']);
  else {
    const last = t.last; const cmd = t.detected.command;
    rows.push(['Tests', h('div', null, h('code', null, cmd), ' · ', last ? h('span', { class: last.ok ? 'good' : 'bad' }, (last.ok ? 'passed' : 'failed') + ' ' + ago(Date.now() / 1000 - last.at)) : h('span', { class: 'muted' }, 'Axon has not run it yet'),
      ' ', link('Ask Axon to run it', () => fillChatBox('Run ' + cmd + ' and tell me what fails, if anything.'), 'Puts a request in the chat box')) ]);
  }
  const s = d.spend;
  rows.push(['Last 7 days', s.calls ? link('$' + s.cost.toFixed(2) + ' · ' + s.calls + ' model call' + (s.calls > 1 ? 's' : '') + ' · ' + tok(s.tokens) + ' tokens', () => { location.hash = '#/usage'; }, 'Open Usage') : h('span', { class: 'muted' }, 'No model calls in this project in the last 7 days.')]);
  rows.push(['Last chats', d.chats.length ? h('ul', null, ...d.chats.map(c => h('li', null, h('a', { href: '#/chat/' + encodeURIComponent(c.id) }, c.title), h('span', { class: 'muted' }, ago(Date.now() / 1000 - c.when))))) : h('span', { class: 'muted' }, 'No chats in this project yet.')]);
  const m = d.memory;
  rows.push(['Memory', m.total ? h('span', null, link(m.total + ' note' + (m.total > 1 ? 's' : ''), () => { location.hash = '#/memory'; }, 'Open Memory'), ': ' + m.items.map(i => i.name).join(', ')) : h('span', { class: 'muted' }, 'No notes saved yet.')]);
  body.replaceChildren(h('dl', { class: 'ov-grid' }, ...rows.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v)])));
}

// ----- recent projects: the project name menu and Cmd+K -----
const PROJ = { rows: [], can_switch: false, at: 0 };
async function projLoad() {
  try { const d = await api('/api/project/recent'); PROJ.rows = d.projects; PROJ.can_switch = d.can_switch; PROJ.at = Date.now(); } catch (e) { /* the list stays as it was */ }
}
async function projectMenu(anchor) {
  await projLoad();
  let menu = null; const items = [{ head: 'Recent projects' }];
  const shown = PROJ.rows.slice(0, 8);
  if (!shown.length) items.push({ label: 'No other projects yet', disabled: true });
  for (const p of shown) {
    if (!p.exists) {
      const rm = h('button', { class: 'mini', type: 'button', 'aria-label': 'Remove ' + p.name + ' from the list', onclick: async () => {
        if (!(await wbConfirm('Remove ' + p.name + ' from the list?', 'The folder is gone from ' + p.path + '. Its saved chats are not deleted.', 'Remove', true))) return;
        try { const r = await api('/api/project/recent/remove', { path: p.path }); PROJ.rows = r.projects; if (menu) menu.close(); toast('Removed from the list'); } catch (e) { toast(e.message); } } }, 'Remove');
      items.push({ node: h('div', { class: 'pm-row missing', title: p.path + ' is missing' }, h('span', { class: 'pm-n' }, p.name), h('span', { class: 'pm-w' }, 'missing'), rm) });
    } else items.push({ label: p.name, hint: p.current ? 'open now' : ago(Date.now() / 1000 - p.used), on: p.current, disabled: p.current || !PROJ.can_switch, title: p.path + (PROJ.can_switch ? '' : ' (this page is attached to a terminal session, so the folder cannot change here)'), run: () => wbSwitchFolder(p.path) });
  }
  items.push('-', { label: 'Choose another folder…', run: () => folderChooser() });
  menu = wbMenu(anchor, items);
}
function projPaletteItems() {
  projLoad();
  const out = [{ label: 'Welcome and setup guide', hint: 'choose a model, test it, pick a folder', run: () => welcomeOpen(true) }, { label: 'Project overview', hint: 'files, git, tests, spend, chats', run: () => { location.hash = '#/insights'; } }];
  for (const p of PROJ.rows) if (p.exists && !p.current && PROJ.can_switch) out.push({ label: 'Switch to project ' + p.name, hint: p.path, run: () => wbSwitchFolder(p.path) });
  return out;
}
"""
