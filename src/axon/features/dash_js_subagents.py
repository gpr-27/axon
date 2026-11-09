"""Client script, part 9: the Subagents list and the full view of one subagent.

It is appended after the tools script, so its ``viewAgents`` / ``viewAgent`` / ``subagentEvent`` replace the earlier,
simpler ones. A subagent transcript is drawn with the chat's own ``historyEntry`` / ``wireHistory`` / ``tailHistory`` /
``toggleAll``: the same step and tool components as the main chat, not a copy of them."""

JS_SUBAGENTS = r"""
// ---------- subagents: list, one subagent in full, links to and from the Task step ----------
const SA = { cache: {} };
const saId = (sid, n) => sid + '_sub_' + n;
const saStatusLabel = (r) => r.live ? 'running' + (r.max_steps ? ' (' + r.steps + '/' + r.max_steps + ')' : '') : (r.status || 'unknown');
const saDot = (r) => h('span', { class: 'dot ' + (r.live ? 'running' : (r.status === 'completed' ? 'completed' : r.status === 'error' || r.status === 'cancelled' ? 'error' : r.status)) });
async function saRoster(sid, fresh) {
  const c = SA.cache[sid]; if (c && !fresh && Date.now() - c.t < 2000) return c.d;
  const d = await api('/api/subagents?session=' + encodeURIComponent(sid)); SA.cache[sid] = { t: Date.now(), d }; return d;
}
function saStop(sid, r, again) {
  if (!confirm('Stop subagent ' + r.index + ' (' + r.title + ')? Work it already did is kept.')) return;
  api('/api/subagent/stop', { session: sid, task: r.task }).then(() => { toast('Stopping ' + r.title); again(); }).catch(e => toast(e.message));
}
function saTell(sid, r) {
  const t = prompt('Tell subagent ' + r.index + ' (' + r.title + '). It reads this before its next step:'); if (!t) return;
  api('/api/subagent/tell', { session: sid, task: r.task, text: t }).then(() => toast('Sent')).catch(e => toast(e.message));
}
function saActions(sid, r, again) {
  return h('div', { class: 'formrow', style: 'margin:0' },
    r.session_id ? h('a', { class: 'btn', href: '#/agent/' + encodeURIComponent(r.session_id) }, 'Open') : h('span', { class: 'muted' }, 'no transcript'),
    r.live ? h('button', { class: 'btn danger', onclick: () => saStop(sid, r, again) }, 'Stop') : null,
    r.live ? h('button', { class: 'btn', onclick: () => saTell(sid, r) }, 'Tell') : null,
    r.held_elsewhere ? h('span', { class: 'muted', title: 'Held by another process: stop or tell it from there' }, 'terminal') : null);
}
function saParentLink(parent, r) {
  const p = r.parent_step; if (!p) return h('span', { class: 'muted' }, 'not recorded');
  return h('a', { href: '#/chat/' + encodeURIComponent(parent), title: p.label, onclick: (e) => { e.preventDefault(); saGoto(parent, p); } }, 'step ' + (p.entry + 1) + (p.inferred ? ' (by order)' : ''));
}
// Go to the Task call in the parent chat: open the chat, find the entry (loading earlier pages when needed), open the tool.
async function saGoto(parent, p) {
  location.hash = '#/chat/' + encodeURIComponent(parent);
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 250));
    const art = document.querySelector('section.step[data-i="' + p.entry + '"]') || document.querySelector('article.entry[data-i="' + p.entry + '"]');
    if (art) {
      const tools = art.querySelectorAll('details.tool:not(.think)'); const card = tools[p.tool];
      if (card) card.open = true;
      (card || art).scrollIntoView({ block: 'center' });
      const el = card || art; el.style.outline = '2px solid var(--primary)'; setTimeout(() => { el.style.outline = ''; }, 3000);
      return;
    }
    const more = document.querySelector('button.earlier:not([hidden])'); if (more && i % 4 === 3) more.click();
  }
  toast('The Task step is further back in the chat than is loaded. Use "Load earlier messages".');
}
async function viewAgents(sid) {
  return shell('agents', async (main) => {
    const body = h('div'); const notes = h('div');
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Subagents'), h('div', { class: 'sub' }, 'Every subagent of this session, running or finished. Open one to read everything it did.')), h('div', { class: 'controls' }, h('a', { class: 'btn', href: '#/chat/' + encodeURIComponent(sid || '') }, 'Back to the chat'))), body, notes);
    let closed = false; onLeave(() => { closed = true; });
    async function draw() {
      let d; try { d = await saRoster(sid || '', true); } catch (e) { body.replaceChildren(errorBox(e.message)); return; }
      if (!d.subagents.length) body.replaceChildren(h('div', { class: 'empty' }, h('strong', null, 'No subagents yet'), 'When Axon delegates work with the Task tool, each subagent appears here and streams while it runs.'));
      else body.replaceChildren(table([
        { label: '', fmt: saDot },
        { label: 'Subagent', fmt: r => h('div', null, h('a', { href: r.session_id ? '#/agent/' + encodeURIComponent(r.session_id) : null }, h('b', null, '#' + r.index + ' ' + (r.type ? r.type + ' · ' : '') + r.title)), r.activity ? h('div', { class: 'muted', style: 'font-size:12px' }, r.activity) : null, h('div', { class: 'muted', style: 'font-size:12px;max-width:60ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, r.prompt || '')) },
        { label: 'Status', fmt: saStatusLabel }, { label: 'Model', key: 'model' },
        { label: 'Parent step', fmt: r => saParentLink(sid, r) },
        { label: 'Calls', cls: 'r', fmt: r => r.tool_calls || 0 }, { label: 'Tokens', cls: 'r', fmt: r => tok(r.tokens || 0) }, { label: 'Cost', cls: 'r', fmt: r => r.cost != null ? usd(r.cost) : 'n/a' }, { label: 'Time', cls: 'r', fmt: r => r.elapsed ? dur(r.elapsed) : 'n/a' },
        { label: '', fmt: r => saActions(sid, r, draw) },
      ], d.subagents));
      notes.replaceChildren(...(d.notes || []).map(n => notice(n, 'warn')));
    }
    await draw(); const t = setInterval(() => { if (!closed) draw(); }, 2500); onLeave(() => clearInterval(t));
  }, { key: 'agents:' + sid, hash: '#/agents/' + encodeURIComponent(sid || ''), title: 'Subagents' });
}
function saFact(label, value) { return h('span', { class: 'pill' }, label + ' ', h('b', null, value)); }
function saStepRow(s) {
  return h('div', { class: 'steprow dtl' + (s.is_error ? ' bad' : '') }, h('span', null, s.kind === 'llm' ? 'model call' : 'tool'), h('span', null, s.name || ''), h('span', { class: 'muted' }, s.summary || ''),
    h('span', { class: 'muted' }, [s.cost ? usd(s.cost) : '', (s.input_tokens || s.output_tokens) ? tok((s.input_tokens || 0) + (s.output_tokens || 0)) + ' tokens' : '', s.duration_s ? dur(s.duration_s) : ''].filter(Boolean).join(' · ')));
}
async function viewAgent(subSid) {
  const m0 = /^(.*)_sub_(\d+)$/.exec(subSid) || [];
  const parent = m0[1] || subSid.replace(/_sub_\d+$/, ''); const num = Number(m0[2] || 0);
  return shell('agent', async (main) => {
    const C = { sid: subSid, closed: false, lastIdx: -1, first: 0, feed: null, wired: false, atBottom: true };
    onLeave(() => { C.closed = true; });
    const feed = h('div', { class: 'feed t-' + state.trace, style: 'padding:0 0 30px' }); C.feed = feed;
    let page; try { page = await api('/api/session/' + encodeURIComponent(subSid) + '?limit=40'); } catch (e) { main.append(errorBox(e.message), h('a', { href: '#/agents/' + encodeURIComponent(parent) }, 'Back to subagents')); return; }
    let D = null; try { D = await api('/api/subagent/detail?session=' + encodeURIComponent(parent) + '&n=' + num); } catch (e) { D = null; }
    const m = page.meta; C.first = page.first;
    const facts = h('div', { class: 'sub' }); const controls = h('div', { class: 'controls' }); const panels = h('div'); const live = h('div');
    const earlier = h('button', { class: 'btn earlier', hidden: !page.has_before, onclick: async () => { const p = await api('/api/session/' + encodeURIComponent(subSid) + '?limit=40&before=' + C.first); const f = document.createDocumentFragment(); historyNodes(p.entries, C).forEach(x => f.append(x)); earlier.after(f); C.first = p.first; earlier.hidden = !p.has_before; } }, 'Load earlier messages');
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Subagent ' + num + ': ' + ((D && D.subagent && D.subagent.title) || m.title)), facts), controls), panels, live, earlier, feed, h('p', { class: 'muted', id: 'agent-note' }));
    historyNodes(page.entries, C).forEach(x => feed.append(x)); if (page.entries.length) C.lastIdx = page.entries[page.entries.length - 1].i;
    wireHistory(C);
    const stepBox = h('div'); let stepsShown = new Set();
    function drawHead() {
      const r = D && D.subagent; const status = r ? saStatusLabel(r) : 'unknown';
      facts.replaceChildren(saFact('status', status), ' ', saFact('type', (r && r.type) || 'not recorded'), ' ', saFact('model', m.model || (r && r.model) || '?'), ' ', saFact('cost', usd(m.cost)), ' ', saFact('tokens', tok(m.tokens)), ' ',
        saFact('duration', r && r.elapsed ? dur(r.elapsed) : 'not recorded'), ' ', saFact('model calls', r && r.iterations ? String(r.iterations) : (r && r.live ? String(r.steps) : 'not recorded')), ' ', saFact('entries', String(m.messages)),
        r && r.parent_step ? [' ', h('span', { class: 'pill' }, 'parent ', h('a', { href: '#/chat/' + encodeURIComponent(parent), onclick: (e) => { e.preventDefault(); saGoto(parent, r.parent_step); } }, 'Task step ' + (r.parent_step.entry + 1) + (r.parent_step.inferred ? ' (by order)' : '')))] : null);
      controls.replaceChildren(h('a', { class: 'btn', href: '#/agents/' + encodeURIComponent(parent) }, 'All subagents'), h('a', { class: 'btn', href: '#/chat/' + encodeURIComponent(parent) }, 'Parent session'),
        r && r.live ? h('button', { class: 'btn danger', onclick: () => saStop(parent, r, tick) }, 'Stop') : null, r && r.live ? h('button', { class: 'btn', onclick: () => saTell(parent, r) }, 'Tell') : null,
        h('button', { class: 'btn', onclick: () => toggleAll(feed, true) }, 'Expand all'), h('button', { class: 'btn', onclick: () => toggleAll(feed, false) }, 'Collapse all'), h('button', { class: 'btn', onclick: () => shareModal({ kind: 'session', id: subSid }) }, 'Share'));
    }
    function drawPanels() {
      if (!D) { panels.replaceChildren(notice('The detail of this subagent could not be loaded; its messages below are complete as saved.', 'warn')); return; }
      const perms = D.permissions || [];
      const rec = Object.entries(D.records || {}).map(([k, v]) => k + ' ×' + v).join(', ');
      panels.replaceChildren(
        D.lease ? notice('This session is held by another process (' + (D.lease.owner || 'terminal') + ', pid ' + D.lease.pid + '). This page only reads the subagent; stop or tell it from there.', 'warn') : null,
        h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'tn' }, 'Task given'), h('span', { class: 'ta' }, (D.prompt || '').split('\n')[0].slice(0, 120))), h('pre', { class: 'out' }, D.prompt || 'not recorded')),
        D.result ? h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'tn' }, 'Final report'), h('span', { class: 'ta' }, 'what it handed back to the parent'), h('span', { class: 'ts' }, D.subagent && D.subagent.status)), h('pre', { class: 'out' }, D.result)) : null,
        h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'tn' }, 'Permission decisions'), h('span', { class: 'ta' }, perms.length + ' recorded')),
          perms.length ? h('div', null, ...perms.map(p => h('div', { class: 'steprow dtl' + (/^(den|block|reject)/i.test(p.outcome) ? ' bad' : '') }, h('span', null, p.tool || 'tool'), h('span', null, p.outcome), h('span', { class: 'muted' }, [p.source, p.approval, p.reason].filter(Boolean).join(' · '))))) : h('p', { class: 'muted' }, 'No permission decision was recorded for this subagent.')),
        h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'tn' }, 'What is recorded'), h('span', { class: 'ta' }, 'and what is not')),
          h('p', null, 'Saved in its transcript: ' + (rec || 'nothing yet') + '.'), h('p', { class: 'muted' }, 'Not recorded:'), h('ul', null, ...(D.not_recorded || []).map(x => h('li', { class: 'muted' }, x))), D.step_note ? h('p', { class: 'muted' }, D.step_note) : null,
          D.worktree_note ? h('p', null, D.worktree_note) : null));
    }
    function drawLive() {
      const r = D && D.subagent;
      if (!r || !r.live) { if (!D || !(D.steps || []).length) { live.replaceChildren(); return; } }
      const steps = (D.steps || []); const recent = r && r.live ? steps.slice(-30) : steps.slice(-30);
      live.replaceChildren(h('details', { class: 'tool', open: !!(r && r.live) }, h('summary', null, h('span', { class: 'tn' }, r && r.live ? 'Working now' : 'Steps'), h('span', { class: 'ta' }, (r && r.live && r.activity) || (steps.length + ' step lines')), h('span', { class: 'ts' }, r && r.live ? h('span', { class: 'spin' }) : '')),
        steps.length > recent.length ? h('p', { class: 'muted' }, 'Showing the last ' + recent.length + ' of ' + steps.length + ' step lines; every message is in the transcript below.') : null, ...recent.map(saStepRow)));
    }
    let busy = false, again = false;
    async function tick() {
      if (busy) { again = true; return false; } busy = true;
      try {
        await tailHistory(C);
        try { D = await api('/api/subagent/detail?session=' + encodeURIComponent(parent) + '&n=' + num); drawHead(); drawPanels(); drawLive(); } catch (e) { /* keep the last detail */ }
        const note = document.getElementById('agent-note'); if (note && D) note.textContent = (D.notes || []).filter(x => x.includes('Subagent ' + num)).join(' ');
        return !!(D && D.subagent && D.subagent.live);
      } catch (e) { return false; } finally { busy = false; if (again) { again = false; setTimeout(tick, 300); } }
    }
    drawHead(); drawPanels(); drawLive();
    // live: the parent's own event stream carries this subagent's steps (subagent_step) and its end (subagent)
    if (D && D.subagent && D.subagent.live) {
      let timer = 0;
      try {
        const st = await api('/api/chat/state?session=' + encodeURIComponent(parent) + '&create=0');
        const es = new EventSource('/api/events?session=' + encodeURIComponent(parent) + '&since=' + (st.seq || 0));
        onLeave(() => es.close());
        es.onmessage = (mm) => {
          let ev; try { ev = JSON.parse(mm.data); } catch (e) { return; }
          if ((ev.type !== 'subagent_step' && ev.type !== 'subagent') || ev.task_id !== 'sub-' + num) return;
          if (ev.type === 'subagent_step' && D) { D.steps = (D.steps || []).concat([ev]).slice(-400); drawLive(); }
          clearTimeout(timer); timer = setTimeout(tick, 350);
        };
      } catch (e) { /* polling below still keeps the page current */ }
    }
    await tick(); const t = setInterval(() => { if (!C.closed) tick(); }, 2500); onLeave(() => clearInterval(t));
  }, { wide: false, persist: true, key: 'agent:' + subSid, hash: '#/agent/' + encodeURIComponent(subSid), title: 'Subagent' });
}

// ---- live view of a subagent inside its Task card, plus one "fan-out" board when several run together ----
function saLive(C, n) {
  C.subLive = C.subLive || {};
  let L = C.subLive[n];
  if (L && L.panel.isConnected) return L;
  const cur = C.cur; if (!cur) return null;
  const card = Object.values(cur.tools || {}).find(c => c.dataset.sub === String(n)); if (!card) return null;
  const list = h('div', { class: 'sa-steps' }, h('div', { class: 'sa-empty' }, 'Waiting for the first step...'));
  const stat = h('span', { class: 'sa-stat' }, 'running'), count = h('span', null, '0 steps'), cost = h('span', null, '');
  const panel = h('div', { class: 'sa-live', 'data-state': 'run' }, h('div', { class: 'sa-lh' }, h('b', null, 'Subagent ' + n), count, cost, stat), list);
  const tid = 'sub-' + n;
  const inp = h('input', { type: 'text', class: 'sa-say', placeholder: 'Tell subagent ' + n + ' something (it reads this before its next step)', 'aria-label': 'Message to subagent ' + n, maxlength: 4000 });
  const say = () => {
    const t = inp.value.trim(); if (!t) return; inp.disabled = true;
    api('/api/subagent/tell', { session: C.sid, task: tid, text: t }).then(() => {
      inp.value = ''; if (L.first) { L.list.replaceChildren(); L.first = false; }
      L.list.append(h('div', { class: 'sa-row you' }, h('span', { class: 'sk' }, '\u2192'), h('span', { class: 'sn' }, 'you'), h('span', { class: 'ss', title: t }, t), h('span', { class: 'sd' }, 'sent')));
      L.list.scrollTop = L.list.scrollHeight;
    }).catch(e => toast(e.message)).finally(() => { inp.disabled = false; inp.focus(); });
  };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); say(); } });
  const stop = h('button', { type: 'button', class: 'btn danger sa-stopbtn', onclick: () => {
    if (!confirm('Stop subagent ' + n + '? Work it already did is kept.')) return;
    api('/api/subagent/stop', { session: C.sid, task: tid }).then(() => toast('Stopping subagent ' + n)).catch(e => toast(e.message));
  } }, 'Stop');
  const ctl = h('div', { class: 'sa-ctl' }, inp, h('button', { type: 'button', class: 'btn', onclick: say }, 'Tell'), stop);
  panel.append(ctl);
  card.append(panel);
  L = { panel, list, stat, count, cost, n, steps: 0, tools: 0, usd: 0, card, first: true, ctl };
  C.subLive[n] = L; return L;
}
function subagentStep(C, ev) {
  if (C.replaying) return;
  const n = Number(String(ev.task_id || '').replace(/^sub-/, '')) || 0; if (!n) return;
  const L = saLive(C, n); if (!L) return;
  if (L.first) { L.list.replaceChildren(); L.first = false; }
  const llm = ev.kind === 'llm';
  L.steps++; if (!llm) L.tools++; L.usd += ev.cost || 0;
  L.count.textContent = L.steps + (L.steps === 1 ? ' step' : ' steps') + ' · ' + L.tools + (L.tools === 1 ? ' tool' : ' tools');
  L.cost.textContent = L.usd ? usd(L.usd) : '';
  const name = llm ? 'model call' : (ev.name || 'tool');
  const sum = (llm ? (ev.name ? ev.name + ' · ' : '') : '') + (ev.summary || '');
  const row = h('div', { class: 'sa-row ' + (llm ? 'llm' : 'tool') + (ev.is_error ? ' err' : ''), title: sum },
    h('span', { class: 'sk' }, llm ? '◆' : (ev.is_error ? '✕' : '▸')), h('span', { class: 'sn' }, name), h('span', { class: 'ss' }, sum),
    h('span', { class: 'sd' }, ev.duration_s ? dur(ev.duration_s) : ''));
  const stick = L.list.scrollTop + L.list.clientHeight >= L.list.scrollHeight - 24;
  L.list.append(row);
  while (L.list.children.length > 300) L.list.firstChild.remove();
  if (stick) L.list.scrollTop = L.list.scrollHeight;
  fanoutDraw(C);
}
function saLiveEnd(C, n, ev) {
  const L = C.subLive && C.subLive[n]; if (!L) return;
  const bad = ev.status === 'error' || ev.status === 'cancelled';
  L.panel.dataset.state = bad ? 'bad' : 'ok'; if (L.ctl) L.ctl.hidden = true;
  L.stat.textContent = bad ? ev.status : 'finished in ' + dur(ev.duration_s);
  if (L.first) { L.list.replaceChildren(h('div', { class: 'sa-empty' }, 'No steps were reported.')); L.first = false; }
  if (bad && ev.detail) L.list.append(h('div', { class: 'sa-row err' }, h('span', { class: 'sk' }, '✕'), h('span', { class: 'sn' }, 'error'), h('span', { class: 'ss', title: ev.detail }, ev.detail)));
}
function fanoutDraw(C) {
  const cur = C.cur; if (!cur) return;
  const all = Object.values(C.subBoard || {}).filter(x => x.turn === cur);
  if (all.length < 2) return;
  let b = cur.fanout;
  if (!b || !b.el.isConnected) {
    const el = h('div', { class: 'fanout', role: 'status', 'aria-live': 'polite' }, h('h5', null, ''));
    const host = cur.body || C.feed; host.append(el); b = cur.fanout = { el };
  }
  const run = all.filter(x => x.state === 'run').length;
  const rows = all.map(x => {
    const L = C.subLive && C.subLive[x.n];
    return h('div', { class: 'fo-row ' + x.state }, h('span', { class: 'fo-dot' }), h('a', { href: '#/agent/' + encodeURIComponent(saId(C.sid, x.n)) }, x.type + ' · ' + x.title),
      h('span', { class: 'fo-m' }, L ? L.steps + ' steps' : ''), h('span', { class: 'fo-m' }, x.state === 'run' ? 'running' : x.state === 'bad' ? 'failed' : 'done'));
  });
  b.el.replaceChildren(h('h5', null, 'FANNED OUT · ' + all.length + ' SUBAGENTS · ' + (run ? run + ' RUNNING' : 'ALL FINISHED')), ...rows);
}

// A saved Task card: the subagent's recorded steps and final report, loaded when opened (the same rows as the live panel).
function saHistory(sid, r) {
  const body = h('div', { class: 'sa-hist-body' }, h('div', { class: 'sa-empty' }, 'Loading the recording...'));
  const d = h('details', { class: 'sa-hist' }, h('summary', null, 'Subagent ' + r.index + ' recording · ' + (r.title || '') + ' · ' + saStatusLabel(r)), body);
  let loaded = false;
  d.addEventListener('toggle', async () => {
    if (!d.open || loaded) return; loaded = true;
    try {
      const D = await api('/api/subagent/detail?session=' + encodeURIComponent(sid) + '&n=' + r.index);
      const kids = [];
      if (D.prompt) kids.push(h('div', { class: 'sa-sect' }, 'Task given'), h('pre', { class: 'out sa-pre' }, D.prompt));
      kids.push(h('div', { class: 'sa-sect' }, 'Steps' + (D.steps && D.steps.length ? ' (' + D.steps.length + ')' : '')));
      if (D.steps && D.steps.length) {
        const list = h('div', { class: 'sa-steps' });
        for (const s of D.steps) { const llm = s.kind === 'llm', sum = (llm && s.name ? s.name + ' · ' : '') + (s.summary || '');
          list.append(h('div', { class: 'sa-row ' + (llm ? 'llm' : 'tool') + (s.is_error ? ' err' : ''), title: sum }, h('span', { class: 'sk' }, llm ? '◆' : (s.is_error ? '✕' : '▸')), h('span', { class: 'sn' }, llm ? 'model call' : (s.name || 'tool')), h('span', { class: 'ss' }, sum), h('span', { class: 'sd' }, s.duration_s ? dur(s.duration_s) : ''))); }
        kids.push(list);
      } else kids.push(h('div', { class: 'sa-empty' }, D.step_note || 'No steps were recorded.'));
      if (D.result) kids.push(h('div', { class: 'sa-sect' }, 'Final report'), h('pre', { class: 'out sa-pre' }, D.result));
      kids.push(h('div', { class: 'sa-empty' }, h('a', { class: 'plink', href: '#/agent/' + encodeURIComponent(r.session_id) }, 'Open the full transcript')));
      body.replaceChildren(...kids);
    } catch (e) { loaded = false; body.replaceChildren(h('div', { class: 'sa-empty bad' }, e.message)); }
  });
  return d;
}
// The live "Subagent started/finished" line in the chat links to that subagent, and the running Task card links to it too.
function subagentEvent(C, ev) {
  const n = Number(String(ev.task_id || '').replace(/^sub-/, '')) || 0;
  const href = n ? '#/agent/' + encodeURIComponent(saId(C.sid, n)) : '#/agents/' + encodeURIComponent(C.sid);
  const cur = C.cur;
  const line = (ev.phase === 'start' ? 'Subagent started: ' : 'Subagent ' + ev.status + ': ') + ev.agent_type + ' · ' + ev.title + (ev.phase === 'end' && !C.replaying ? ' · ' + usd(ev.cost) + ' · ' + tok((ev.input_tokens || 0) + (ev.output_tokens || 0)) + ' tokens · ' + dur(ev.duration_s) : '');
  const note = notice('', ev.status === 'error' ? 'bad' : '');
  note.append(h('a', { class: 'plink', href }, line));
  (cur ? cur.body : C.feed).append(note);
  if (ev.phase === 'start' && n && cur) {
    const card = Object.values(cur.tools || {}).find(c => c.dataset.task === '1' && !c.dataset.sub);
    if (card) { card.dataset.sub = String(n); const a = card.querySelector('.next a.plink'); if (a) { a.href = href; a.textContent = 'Open subagent ' + n; } }
  }
  if (!C.replaying && n) {
    C.subBoard = C.subBoard || {};
    const prev = C.subBoard[n] || {};
    C.subBoard[n] = { n, turn: prev.turn || cur, type: ev.agent_type, title: ev.title, state: ev.phase === 'start' ? 'run' : (ev.status === 'error' || ev.status === 'cancelled' ? 'bad' : 'ok') };
    if (ev.phase === 'end') saLiveEnd(C, n, ev); else saLive(C, n);
    fanoutDraw(C);
  }
  if (!C.replaying) refreshSubs(C);
}
// Task steps drawn from the saved transcript get the same link, from the exact record of which call started which subagent.
(function () {
  const done = new WeakSet();
  async function link(art) {
    if (done.has(art)) return; done.add(art);
    const scopes = [...art.querySelectorAll('section.step[data-i]')].map(sc => [sc, Number(sc.dataset.i)]);
    if (!scopes.length) scopes.push([art, Number(art.dataset.i)]);
    const cards = scopes.flatMap(([sc, ent]) => [...sc.querySelectorAll('details.tool:not(.think)')].map((d, i) => [d, i, ent]).filter(([d]) => { const tn = d.querySelector('summary .tn'); return tn && tn.textContent === 'Task'; }));
    if (!cards.length) return;
    const hm = /^#\/chat\/([^/?]*)/.exec(location.hash); const sid = hm ? decodeURIComponent(hm[1]) : (UI.chat && UI.chat.sid) || '';
    if (!sid || art.closest('.sa-nolink')) return;
    let d; try { d = await saRoster(sid, false); } catch (e) { return; }
    for (const [card, idx, i] of cards) {
      const r = (d.subagents || []).find(x => x.parent_step && !x.parent_step.inferred && ((card.axId && x.parent_step.call_id === card.axId) || (x.parent_step.entry === i && x.parent_step.tool === idx))); if (!r || !r.session_id) continue;
      if (card.querySelector('.sa-open')) continue;
      const a = h('a', { class: 'plink sa-open', href: '#/agent/' + encodeURIComponent(r.session_id), title: (r.type ? r.type + ' · ' : '') + r.title + ' · ' + saStatusLabel(r), onclick: (e) => { e.stopPropagation(); } }, 'Open subagent ' + r.index);
      (card.querySelector('.p-why') || card.querySelector('summary')).append(' ', a);
      if (!card.querySelector('.sa-hist')) card.append(saHistory(sid, r));
    }
  }
  const obs = new MutationObserver((muts) => {
    for (const mu of muts) for (const n of mu.addedNodes) {
      if (n.nodeType !== 1) continue;
      if (n.matches && n.matches('article.entry.a')) link(n);
      else if (n.querySelectorAll) n.querySelectorAll('article.entry.a').forEach(link);
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });
})();
"""
