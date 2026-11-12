"""Client script, part 2: the live chat view (event-driven agent turns, approvals, composer, rail, subagents)."""

JS_CHAT = r"""
// ---------- chat ----------
const EXPLAIN = {
  Bash: 'Runs a shell command on this computer, in the project folder.', Write: 'Creates a file or overwrites it completely.',
  Edit: 'Changes part of an existing file.', MultiEdit: 'Makes several changes to one file.', NotebookEdit: 'Changes a notebook cell.',
  WebFetch: 'Downloads a web page and reads it.', WebSearch: 'Searches the web.', Task: 'Starts a subagent that works on its own.',
  ExitPlanMode: 'Approves the plan, so the agent may start editing files and running commands.', KillShell: 'Stops a background shell.',
};
function toolSummary(name, inp) {
  for (const k of ['file_path', 'path', 'command', 'pattern', 'query', 'url', 'description', 'prompt', 'plan']) {
    const v = inp && inp[k]; if (typeof v === 'string' && v.trim()) return v.replace(/\s+/g, ' ').slice(0, 110);
  }
  return '';
}
function lineDiff(a, b) {
  const A = a.split('\n'), B = b.split('\n'); const n = A.length, m = B.length;
  if (n * m > 400000) return A.map(x => '-' + x).concat(B.map(x => '+' + x));
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) { if (A[i] === B[j]) { out.push(' ' + A[i]); i++; j++; } else if (L[i + 1][j] >= L[i][j + 1]) out.push('-' + A[i++]); else out.push('+' + B[j++]); }
  while (i < n) out.push('-' + A[i++]); while (j < m) out.push('+' + B[j++]);
  return out;
}
function diffEl(lines, header) {
  const pre = h('pre', { class: 'diff nolink' });
  if (header) pre.append(h('span', { class: 'hdr' }, header));
  const shown = lines.length > 1500 ? lines.slice(0, 1500) : lines;
  for (const ln of shown) pre.append(h('span', { class: ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : ln.startsWith('@@') ? 'hunk' : 'ctx' }, ln || ' '));
  if (lines.length > shown.length) pre.append(h('span', { class: 'ctx' }, '... ' + (lines.length - shown.length) + ' more lines'));
  return pre;
}
function pagedPre(text, cls) {
  const wrap = h('div', { class: 'paged' }); const pre = h('pre', { class: cls || 'out' }); wrap.append(pre);
  let shown = 0; const step = 200000;
  const more = h('button', { class: 'more', type: 'button' }, '');
  const fill = (n) => { pre.append(document.createTextNode(text.slice(shown, shown + n))); shown = Math.min(text.length, shown + n); more.hidden = shown >= text.length; more.textContent = 'Show more (' + (text.length - shown).toLocaleString() + ' characters left)'; if (shown >= text.length) more.remove(); };
  more.addEventListener('click', () => { fill(step); linkifyPaths(pre); });
  fill(20000); if (shown < text.length) wrap.append(h('div', { class: 'clipped' }, h('span', null, text.split('\n').length.toLocaleString() + ' lines, ' + text.length.toLocaleString() + ' characters.'), more));
  return wrap;
}
function inputBlock(name, inp) {
  if (typeof inp.old_string === 'string' && typeof inp.new_string === 'string') return diffEl(lineDiff(inp.old_string, inp.new_string), '--- ' + (inp.file_path || inp.path || 'file'));
  if (name === 'Bash' && typeof inp.command === 'string') return pagedPre(inp.command, 'out');
  if (name === 'Write' && typeof inp.content === 'string') return h('div', null, h('div', { class: 'lbl' }, inp.file_path || inp.path || 'file'), pagedPre(inp.content, 'out'));
  const lines = Object.entries(inp || {}).map(([k, v]) => k + ': ' + (typeof v === 'string' ? v : JSON.stringify(v)));
  return pagedPre(lines.join('\n') || '(no input)', 'out');
}
function changesBlock(changes) {
  const box = h('div', { class: 'changes' });
  for (const c of changes) {
    const head = h('summary', null, h('span', { class: 'tn' }, c.kind), h('span', { class: 'ta' }, c.path), h('span', { class: 'ts' }, (c.added != null ? '+' + c.added : '') + ' ' + (c.removed != null ? '-' + c.removed : '')));
    const d = h('details', { class: 'tool' }, head);
    if (c.new != null || c.old != null) d.append(diffEl(lineDiff(c.old || '', c.new || ''), c.path));
    else d.append(h('p', { class: 'muted' }, 'No text diff was recorded for this change.'));
    box.append(d);
  }
  return box;
}
function turnError(msg) {
  const box = errorBox(msg);
  if (/401|403|unauthori[sz]ed|invalid[ _-]*(api)?[ _-]*key|incorrect api key|authentication|forbidden|api key|no api key/i.test(msg)) box.append(' ', h('a', { class: 'btn', href: '#/settings/providers' }, 'Check the provider and key'));
  return box;
}
function notice(text, cls, detail) {
  const n = h('div', { class: 'notice ' + (cls || '') }, text);
  if (detail) { const d = h('details', null, h('summary', null, 'Details')); d.append(pagedPre(detail, 'out')); n.append(d); }
  return n;
}

// The Progress panel follows the active tab: a tab that finishes loading in the background must not take it over.
function claimRail(C, tab) {
  if (!UI.chat || !UI.active || UI.active === tab) { UI.chat = C; return true; }
  return false;
}
// A turn bar that still spins although nothing is running (a turn replayed from the record, or whose end the page never saw).
function settleSpinners(C, busy) {
  if (!C.feed) return;
  if (!busy && C.cur && Date.now() - (C.lastEv || 0) < 3000) return;   // events are still arriving: the state read may be a moment behind
  const live = C.cur && C.cur.art;
  for (const art of C.feed.querySelectorAll('.entry.a')) {
    if (art === live && busy) continue;
    const sp = art.querySelector('.turnbar .spin'); if (!sp) continue;
    sp.remove(); const tl = art.querySelector('.turnbar .tl'); if (tl) tl.textContent = 'ended';
    if (typeof art.querySelectorAll === 'function') {
      art.querySelectorAll('.mk.run, .mk.wait').forEach(m => setMk(m, 'stop'));
      art.querySelectorAll('.fib i.run, .fib i.wait').forEach(i => { i.className = 'stop'; });
      art.querySelectorAll('[data-t0]').forEach(e => { e.removeAttribute('data-t0'); e.textContent = ''; });
      art.querySelectorAll('.approve').forEach(a => a.remove());
      const hd = art.querySelector('.turnbar .tel'); if (hd) hd.textContent = '';
    }
  }
  if (!busy && C.cur) C.cur = null;
}

// The terminal moved to another chat or folder (from here or by itself): the page goes with it.
function liveMoved(C, ev) {
  api('/api/ws').then(w => { state.ws = w; drawChip(); }).catch(() => { });
  if (!ev.session || ev.session === C.sid) return;
  const old = '#/chat/' + encodeURIComponent(C.sid), here = location.hash === old || location.hash === '#/chat' || !location.hash;
  if (C.es) { C.es.close(); C.es = null; } C.runtime = false; C.st = null;
  if (here) { if (C.tab) closeTab(C.tab); location.hash = '#/chat/' + encodeURIComponent(ev.session); }   // closing the last tab sets its own address, so this comes after
}
async function viewChat(sid) {
  // Attached to a terminal, opening a chat continues it there: one conversation, shown in both. If the terminal is busy, the chat opens to read.
  // A tab restored in the background after a reload neither moves the terminal nor takes the page from the tab being opened.
  if (sid && state.ws && state.ws.live && !UI.bg) { try { await api('/api/live/switch', { session: sid }); } catch (e) { /* busy, or no longer there */ } }
  // The plain chat address means the terminal's chat: open that conversation (its history included) rather than an empty one.
  if (!sid && state.ws && state.ws.live && !UI.bg) { try { const st = await api('/api/chat/state?create=0'); if (st.session) { location.hash = '#/chat/' + encodeURIComponent(st.session); return; } } catch (e) { /* opens below */ } }
  const C = { sid: sid || '', st: null, es: null, seq: 0, cur: null, steps: [], subs: [], chips: {}, closed: false, lastIdx: -1, first: 0,
    cmds: null, runtime: false, meta: null, atBottom: true, hist: [], hpos: -1, files: [], shellMode: false };
  return shell('chat', async (main, tab) => {
    C.tab = tab; tab.C = C;
    onLeave(() => { C.closed = true; if (C.es) C.es.close(); });
    const feed = h('div', { class: 'feed t-' + state.trace, id: 'feed-' + (sid || 'new') });
    C.feed = feed;
    followWire(C, feed);
    const head = h('div', { class: 'chat-head' });
    C.head = head;
    const composerEl = buildComposer(C);
    main.append(h('div', { class: 'chat' }, h('div', { class: 'chat-main' }, feed, composerEl)));
    await loadHistory(C);
    await refreshState(C);
    if (claimRail(C, tab)) setStatus(C.st);
    drawHead(C); drawRail(C);
    if (C.meta && C.meta.title) setTabTitle(tab, C.meta.title.slice(0, 26));
    if (C.missing && !C.runtime && !C.feed.children.length) {
      // a saved tab for a chat that was deleted: say so instead of opening an empty chat that would start a new one
      composerEl.hidden = true;
      feed.append(h('div', { class: 'empty-chat', 'data-missing': '1' }, h('h2', null, 'Chat not found'), h('p', { class: 'muted' }, 'This chat was deleted or never saved. Close this tab, or start a new chat.'),
        h('div', { class: 'formrow' }, h('button', { class: 'btn primary', type: 'button', onclick: () => newChat(C) }, 'New chat'), h('button', { class: 'btn', type: 'button', onclick: () => closeTab(tab) }, 'Close tab'))));
    }
    if (!C.feed.children.length) feed.append(emptyChat(C));
    feed.scrollTop = feed.scrollHeight;
    const poll = setInterval(() => { if (!C.closed && C.runtime && !document.hidden) refreshState(C, true); }, 4000);
    onLeave(() => clearInterval(poll));
  }, { wide: true, persist: true, key: 'chat:' + (sid || 'new'), hash: '#/chat' + (sid ? '/' + encodeURIComponent(sid) : ''), title: sid ? 'Chat' : 'New chat' });
}

function emptyChat(C) {
  if (typeof welcomeEmptyChat === 'function') return welcomeEmptyChat(C);
  const ws = state.ws || {};
  return h('div', { class: 'empty-chat' }, h('h2', null, 'Ask Axon about ' + (ws.name || 'this project')),
    h('p', null, 'Axon works in ', h('b', null, ws.workspace || ''), '. It can read and change files here and run commands, asking you first.'),
    h('div', { class: 'hints' }, h('div', null, h('span', { class: 'kbd' }, '/'), ' commands  ', h('span', { class: 'kbd' }, '@'), ' files  ', h('span', { class: 'kbd' }, '!'), ' shell command'),
      h('div', null, 'Drop or paste files and images into the box. A large paste becomes an attachment.'),
      h('div', null, 'Type while Axon works to steer it; ', h('span', { class: 'kbd' }, 'Stop'), ' interrupts the turn.')));
}

async function loadHistory(C) {
  if (!C.sid) return;
  let log = null;
  try { const r = await api('/api/session/' + encodeURIComponent(C.sid) + '/events?limit=20000'); if (r.events && r.events.length) log = r; } catch (e) { /* no record yet */ }
  try {
    const page = await api('/api/session/' + encodeURIComponent(C.sid) + '?limit=30' + (log ? '&before_ts=' + log.first_ts : ''));
    C.meta = page.meta; C.first = page.first;
    const frag = document.createDocumentFragment();
    if (page.has_before) frag.append(h('button', { class: 'btn earlier', onclick: async (e) => {
      const p = await api('/api/session/' + encodeURIComponent(C.sid) + '?limit=30&before=' + C.first + (log ? '&before_ts=' + log.first_ts : ''));
      const f = document.createDocumentFragment(); historyNodes(p.entries, C).forEach(x => f.append(x)); e.target.after(f); C.first = p.first; e.target.hidden = !p.has_before; } }, 'Load earlier messages'));
    historyNodes(page.entries, C).forEach(x => frag.append(x));
    C.feed.append(frag);
    if (page.entries.length) C.lastIdx = page.entries[page.entries.length - 1].i;
    wireHistory(C);
  } catch (e) { if (/not found/i.test(e.message)) C.missing = true; else C.feed.append(errorBox(e.message)); }
  if (log) replayLog(C, log.events);
  // transcript entries written after the log (a turn the page did not see): fill the gap so nothing is missing
  if (log) { try { const t = await api('/api/session/' + encodeURIComponent(C.sid) + '?limit=1'); C.lastIdx = t.total - 1; } catch (e) { /* ignore */ } }
}
function replayLog(C, events) {
  C.replaying = true;
  try {
    for (const rec of events) {
      C.replayTs = Math.max(C.replayTs || 0, rec.ts || 0);
      const d = Object.assign({}, rec.data, { type: rec.type, ts: rec.ts, seq: rec.seq, parent: rec.parent });
      for (const k of Object.keys(d)) { const v = d[k]; if (v && typeof v === 'object' && v.$blob) { d[k] = v.head; d['blob_' + k] = { id: v.$blob, len: v.len }; } }
      try { applyEvent(C, d); } catch (e) { console.warn('replay', rec.type, e); }
    }
  } finally {
    try { if (typeof saReplayDone === 'function') saReplayDone(C); } catch (e) { console.warn('subagent lanes', e); }
    C.replaying = false; if (C.cur && C.cur.running) { for (const ap of Object.values(C.aps || {})) { if (ap.row && ap.row.dataset.state === 'wait') { try { buildApproval(C, ap.ev, ap.row); } catch (e) { /* the page shows it after the next state read */ } } } } else C.cur = null; C.steps = [];
  }
}
function historyEntry(e) {
  const art = h('article', { class: 'entry ' + (e.kind === 'user' ? 'u' : e.kind === 'assistant' ? 'a' : 'n'), 'data-i': e.i, html: String(e.html || '').replace(/\[Response interrupted by user\]/g, 'Stopped') });
  decorate(art);
  return art;
}
// A stored turn is the saved model calls of one turn. They are turned back into the events the live view receives and
// drawn by the very same code (applyEvent), so a stored turn and a live one cannot look different.
function storedEvents(entries) {
  const S = { synthetic: true }; const evs = [Object.assign({ type: 'turn_start' }, S)];
  let calls = 0, nTools = 0, cost = 0, costOk = true, dur = 0, durOk = true, stop = '';
  for (const e of entries) {
    const d = e.data; if (!d) continue; const u = d.usage || {}; calls++;
    evs.push(Object.assign({ type: 'llm_call_start', iteration: calls, model: d.model, tokens_in: u.input || 0, entry: e.i }, S));
    if (d.thinking) evs.push(Object.assign({ type: 'thinking_delta', text: d.thinking }, S));
    if (d.text) evs.push(Object.assign({ type: 'text_delta', text: d.text, clipped: d.text_total > d.text.length ? { total: d.text_total } : null }, S));
    const tl = d.tools || [];
    if (tl.length > 1) evs.push(Object.assign({ type: 'tool_batch', count: tl.length, tools: [] }, S));
    tl.forEach((t, n) => {
      const id = t.id || ('stored-' + e.i + '-' + n); nTools++;
      evs.push(Object.assign({ type: 'tool_use', id, name: t.name, input: t.input || {}, rationale: '' }, S));
      if (t.permission) evs.push(Object.assign({ type: 'permission', id, tool: t.name }, t.permission, S));
      if (t.result != null) {
        evs.push(Object.assign({ type: 'tool_result', tool_use_id: id, name: t.name, content: t.result, is_error: !!t.is_error, changes: [],
          more: t.result_total > t.result.length ? { entry: e.i, field: 't' + n + '.out', total: t.result_total } : null }, S));
        if (t.duration != null) evs.push(Object.assign({ type: 'tool_timing', id, name: t.name, duration_s: t.duration }, S));
      }
      if (t.duration != null) dur += t.duration; else if (t.result != null) durOk = false;
    });
    if (d.cost != null) cost += d.cost; else costOk = false;
    if (d.duration != null) dur += d.duration; else durOk = false;
    stop = d.stop || (tl.length ? 'tool_use' : 'end_turn');
    evs.push(Object.assign({ type: 'llm_call_end', iteration: calls, model: d.model, usage: { input_tokens: u.input || 0, output_tokens: u.output || 0, cache_read_tokens: u.cache_read || 0, cache_write_tokens: u.cache_write || 0, reasoning_tokens: u.reasoning || 0, _stored: true },
      cost: d.cost, duration_s: d.duration, stop_reason: d.stop || (tl.length ? 'tool_use' : 'end_turn') }, S));
  }
  evs.push(Object.assign({ type: 'turn_end', stop_reason: stop === 'tool_use' ? 'end_turn' : stop, cost_usd: costOk ? cost : null, duration_s: durOk ? dur : null, iterations: calls, tool_calls: nTools }, S));
  return evs;
}
function storedTurn(grp, C) {
  const F = { sid: C ? C.sid : '', feed: h('div'), replaying: true, steps: [], uses: {}, touched: {}, cur: null, stored: true };
  for (const ev of storedEvents(grp)) { try { applyEvent(F, ev); } catch (x) { console.warn('stored turn', ev.type, x); } }
  const art = F.feed.querySelector('article.entry.a'); if (!art) return h('div');
  art.dataset.i = grp[0].i; art.classList.remove('replayed'); art.classList.add('stored');
  return art;
}
// Pages of the saved transcript -> DOM nodes: consecutive model calls form one turn.
function historyNodes(entries, C) {
  const out = []; let grp = [];
  const flush = () => { if (grp.length) { out.push(storedTurn(grp, C)); grp = []; } };
  for (const e of entries) { if (e.kind === 'assistant' && e.data) grp.push(e); else { flush(); out.push(historyEntry(e)); } }
  flush(); return out;
}
function wireHistory(C) {
  if (C.wired) return; C.wired = true;
  C.feed.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button.more[data-entry]'); if (!b) return;
    b.disabled = true;
    try {
      const off = Number(b.dataset.offset);
      const r = await api('/api/session/' + encodeURIComponent(C.sid) + '/text?entry=' + b.dataset.entry + '&field=' + encodeURIComponent(b.dataset.field) + '&offset=' + off + '&limit=400000');
      const wrap = b.closest('.clip'); let pre = wrap.querySelector('pre.plain-full');
      if (!pre) { pre = h('pre', { class: 'out plain-full' }); wrap.querySelector('.cbody').replaceChildren(pre); }
      pre.append(document.createTextNode(r.text)); b.dataset.offset = r.next;
      if (r.done) b.parentElement.remove(); else { b.disabled = false; b.textContent = 'Show more (' + (r.total - r.next).toLocaleString() + ' characters left)'; }
    } catch (e) { b.disabled = false; toast(e.message); }
  });
}

const MAX_STREAMS = 4;
function openStreamCount(except) { return (UI.tabs || []).filter(t => t.C && t.C !== except && t.C.es && t.C.es.readyState !== 2).length; }
async function refreshState(C, quiet) {
  try {
    const st = await api('/api/chat/state?session=' + encodeURIComponent(C.sid) + '&create=0');
    if (st.none) { C.runtime = false; C.st = null; return; }
    C.st = st; const first = !C.runtime;
    C.runtime = true;
    if (!C.sid) C.sid = st.session;
    settleSpinners(C, !!st.busy);
    // A browser keeps about six connections per site, and every open stream takes one: with a handful of chat tabs open,
    // every other request (even Send) waits forever. A tab that is hidden and idle lets its stream go; it is picked up
    // again when the tab is shown or starts working (this poll runs every few seconds).
    const hiddenTab = !!(C.feed && C.feed.isConnected && C.feed.offsetParent === null);
    if (!st.busy && hiddenTab) { if (C.es) { C.es.close(); C.es = null; } }
    // Several chats working at once: a hidden one past the fourth stream lets go too, so two connections stay free for Send and Stop.
    // It picks up again from its last event number when it is shown or when a stream frees up (this poll runs every few seconds).
    else if (hiddenTab && openStreamCount(C) >= MAX_STREAMS) { if (C.es) { C.es.close(); C.es = null; } }
    else if (first || !C.es) connectEvents(C, st);
    if (!quiet) { drawHead(C); }
    drawRail(C); drawHeadStatus(C); if (UI.chat === C || !UI.chat) { UI.chat = C; setStatus(st); }
  } catch (e) { if (!quiet) toast(e.message); }
}

function connectEvents(C, st) {
  if (C.closed) return;
  if (C.es) C.es.close();
  // The first connection starts from now (history was loaded from the transcript). A later one, after the stream was let go of,
  // resumes after the last event this page applied, so a turn that ended in the meantime is not skipped.
  const since = C.streamed ? (C.lastSeq || 0) : (st && st.busy ? 0 : (st ? st.seq : 0));
  C.streamed = true;
  C.lastSeq = Math.max(C.lastSeq || 0, since);        // the newest event applied: a reconnect resumes after it, never replays it
  const es = new EventSource('/api/events?session=' + encodeURIComponent(C.sid) + '&since=' + C.lastSeq);
  C.es = es;
  es.onmessage = (m) => {
    const id = Number(m.lastEventId) || 0; if (id && id <= C.lastSeq) return;
    if (id) C.lastSeq = id;
    try { const ev = JSON.parse(m.data); if (ev.type === 'session_changed') liveMoved(C, ev); else applyEvent(C, ev); } catch (e) { console.warn('bad event', e); }
  };
  es.addEventListener('gap', () => { refreshState(C); tailHistory(C); });
  es.onerror = async () => {
    if (C.closed) return;
    if (es.readyState === 2 && C.es === es) setTimeout(async () => {   // the browser gave up: resume from the last event id
      if (C.closed || C.es !== es) return;
      // the server may have restarted and forgotten this chat: stop asking (each try is a logged 404) until a message wakes it
      try { const s = await api('/api/chat/state?session=' + encodeURIComponent(C.sid) + '&create=0'); if (s.none) { C.es = null; C.runtime = false; C.st = null; C.offline = false; C.offlineSince = 0; drawHeadStatus(C); return; } } catch (e) { /* server down: keep trying */ }
      if (!C.closed && C.es === es) connectEvents(C, C.st || { busy: false, seq: C.lastSeq });
    }, 1500);
    C.offline = true; C.offlineSince = C.offlineSince || Date.now(); drawHeadStatus(C);
    try { const r = await fetch('/api/info', { credentials: 'same-origin' }); if (r.status === 401) authLost(); } catch (e) { /* server down: the pill says so */ }
  };
  es.onopen = () => { if (C.offline) { C.offline = false; C.offlineSince = 0; drawHeadStatus(C); refreshState(C, true); } };   // missed events arrive by id; only a 'gap' reloads history
}

function drawHeadStatus(C) {
  const el = C.head && C.head.querySelector('.chat-status'); if (!el) return;
  const st = C.st || {};
  el.replaceChildren();
  if (C.offline) {
    const long = Date.now() - (C.offlineSince || Date.now()) > 15000;
    el.append(h('span', { class: 'pill' }, long ? 'Connection lost' : h('span', null, h('span', { class: 'spin' }), ' reconnecting'),
      h('button', { class: 'mini', style: 'margin-left:8px', onclick: () => { C.offline = false; C.offlineSince = 0; connectEvents(C, C.st || { busy: false, seq: 0 }); refreshState(C); } }, 'Retry now'),
      h('button', { class: 'mini', onclick: () => location.reload() }, 'Reload page')));
    setTimeout(() => { if (C.offline) drawHeadStatus(C); }, 5000);
  }
  if (st.busy && !C.offline) {
    const quiet = C.lastEv ? Date.now() - C.lastEv : 0;
    const ask = C.feed && C.feed.querySelector('.approve');
    if (ask) {
      el.append(h('button', { class: 'pill wait-pill need', type: 'button', title: 'The turn is paused until you answer', onclick: () => ask.scrollIntoView({ block: 'center', behavior: 'smooth' }) }, 'Waiting for your answer \u2193'));
    } else if (quiet > 12000) {
      const run = C.cur && Object.values(C.cur.tools || {}).filter(c => c.dataset.state === 'run' || c.dataset.state === 'wait');
      const what = run && run.length ? 'Running ' + run.map(c => c.axName).slice(0, 3).join(', ') : 'Waiting for the model';
      el.append(h('span', { class: 'pill wait-pill', role: 'status', title: 'No new activity from the turn. It is still running; press Stop to end it.' }, h('span', { class: 'spin' }), ' ' + what + ' \u00B7 quiet for ' + Math.round(quiet / 1000) + 's'));
    }
    if (!C.stallTimer) C.stallTimer = setInterval(() => { if (C.closed || !(C.st && C.st.busy)) { clearInterval(C.stallTimer); C.stallTimer = 0; drawHeadStatus(C); return; } drawHeadStatus(C); }, 3000);
  }
  if (st.external) el.append(h('span', { class: 'term-note', title: 'Events from the terminal session this page is attached to' }, 'Terminal session'));
  const stop = C.head.querySelector('.stop-btn'); if (stop) stop.hidden = !st.busy;
  C.head.classList.add('slim'); C.head.classList.toggle('idle', !st.busy && !C.offline && !st.external);
  if (UI.chat === C) setStatus(st);
}

function drawHead(C) {
  // No header strip any more: the title and its menu live in the top bar (wbTitleMenu). What stays here shows only while it matters.
  const st = C.st || {};
  C.head.replaceChildren(h('span', { class: 'chat-status', style: 'display:contents' }), h('span', { class: 'sp', style: 'flex:1' }),
    h('button', { class: 'btn steps-all', type: 'button', title: 'Hide or show the steps (thinking, tool calls, notes) of every answer. Remembered in this browser.', onclick: toggleAllSteps }, stepsHidden() ? 'Expand steps' : 'Collapse steps'),
    h('button', { class: 'btn danger stop-btn', hidden: !st.busy, disabled: !!C.stopping, onclick: () => stopTurn(C) }, C.stopping ? 'Stopping' : 'Stop'));
  drawHeadStatus(C);
  if (UI.chat === C) wbTitleSync();
  if (C.meta && C.meta.held_by) lockNote(C);
}
function lockNote(C) {
  if (document.getElementById('lock-note')) return;
  const hb = C.meta.held_by;
  const note = h('div', { class: 'lock-note', id: 'lock-note' }, h('b', null, 'Read-only. '),
    'This session is open in another terminal (' + (hb.owner || 'terminal') + ', pid ' + hb.pid + ', started ' + age(hb.started) + '). The page never writes to a session someone else holds. ',
    h('button', { class: 'btn primary', onclick: async () => { try { const st = await api('/api/chat/fork', { session: C.sid }); location.hash = '#/chat/' + encodeURIComponent(st.session); } catch (e) { toast(e.message); } } }, 'Fork into a new session'));
  C.feed.before(note); C.locked = true;
  const ta = document.getElementById('msg'); if (ta) { ta.disabled = true; ta.placeholder = 'Read-only: held by another terminal'; }
}
async function runCmd(C, line) {
  const r = await api('/api/commands/run', { session: C.sid, line });
  if (r.state) { C.st = r.state; C.runtime = true; if (!C.sid) C.sid = r.state.session; if (!C.es) connectEvents(C, r.state); drawHead(C); drawRail(C); }
  return r;
}
function modelPicker(C) {
  const sel = h('select', { 'aria-label': 'Model', onchange: async () => {
    if (sel.value === '__providers') { sel.value = sel.dataset.cur || ''; location.hash = '#/settings/providers'; return; }
    try { await runCmd(C, '/model ' + sel.value); sel.dataset.cur = sel.value; toast('Model: ' + sel.value); } catch (e) { toast(e.message); }
  } });
  const cur = (C.st && C.st.model) || '';
  sel.dataset.cur = cur;
  sel.append(h('option', { value: cur }, cur || 'model...'));
  // Only the provider in use is listed; every other provider sits behind one link, so the list stays short.
  Promise.all([api('/api/models?session=' + encodeURIComponent(C.sid)), api('/api/providers').catch(() => null)]).then(([d, pv]) => {
    const cm = d.current || cur; sel.dataset.cur = cm;
    const opt = (m) => h('option', { value: m, selected: m === cm }, m);
    const active = pv && (pv.providers || []).find(p => p.active);
    const names = active && (active.models || []).length ? [...new Set([cm, ...active.models].filter(Boolean))] : [cm].filter(Boolean);
    const kids = [];
    if (active && names.length) kids.push(h('optgroup', { label: active.name }, ...names.map(opt))); else kids.push(...names.map(opt));
    kids.push(h('option', { value: '__providers' }, 'Set up another provider...'));
    sel.replaceChildren(...kids);
  }).catch(() => { });
  return h('span', { class: 'pill' }, 'model ', sel);
}
function modePicker(C) {
  const cur = (C.st && C.st.mode) || 'default';
  const labels = { default: 'Ask before edits and commands', acceptEdits: 'Allow file edits, ask for commands', plan: 'Plan only (read-only)', auto: 'Auto: a classifier decides', bypass: 'Run everything without asking' };
  const sel = h('select', { 'aria-label': 'Permission mode', title: 'What the agent may do without asking', onchange: async () => { try { if (!(await confirmBypass(C, sel.value))) { sel.value = (C.st && C.st.mode) || 'default'; return; } await runCmd(C, '/mode ' + sel.value); toast('Mode: ' + labels[sel.value]); } catch (e) { toast(e.message); } } },
    Object.entries(labels).map(([v, l]) => h('option', { value: v, selected: v === cur }, l)));
  return h('span', { class: 'pill' }, 'mode ', sel);
}
function tracePicker(C) {
  const sel = h('select', { 'aria-label': 'Trace detail', title: 'How much of each step to show', onchange: () => { state.trace = sel.value; lsSet('axon-trace2', sel.value); C.feed.className = 'feed t-' + sel.value; } },
    [['compact', 'Compact'], ['normal', 'Normal'], ['detailed', 'Detailed'], ['debug', 'Debug (raw events)']].map(([v, l]) => h('option', { value: v, selected: v === state.trace }, l)));
  return h('span', { class: 'pill' }, 'trace ', sel);
}
async function newChat(C) {
  try { const st = await api('/api/chat/new', {}); location.hash = '#/chat/' + encodeURIComponent(st.session); if (C.closed === false && location.hash === '#/chat/' + encodeURIComponent(st.session)) route(); } catch (e) { toast(e.message); }
}
// Stop interrupts the turn only. Messages waiting in the queue are taken out of it first, so the server does not start
// them by itself; they wait in the "Waiting" list above the box, with Send now and Remove.
async function stopTurn(C, opts) {
  const keep = opts && opts.keep;
  C.stopping = true; drawHead(C);
  try {
    for (const q of ((C.st && C.st.queue) || [])) {
      if (keep != null && q.id === keep) continue;
      try { const r = await api('/api/chat/queue/remove', { session: C.sid, id: q.id }); if (r && r.ok === false) continue; (C.held = C.held || []).push({ id: q.id, text: (C.qtext && C.qtext[q.id]) || q.text, steer: !!q.steer }); } catch (e) { /* it already started */ }
    }
    drawHeld(C);
    await api('/api/chat/stop', { session: C.sid });
  } catch (e) { C.stopping = false; drawHead(C); toast(e.message); }
}
function drawHeld(C) {
  const box = C.heldEl; if (!box) return;
  const list = C.held || [];
  box.hidden = !list.length;
  box.replaceChildren(...(list.length ? [h('div', { class: 'lbl' }, 'Waiting (not sent). The turn was stopped, so these did not run.')] : []), ...list.map(m => h('div', { class: 'held' }, h('span', { class: 't', title: m.text }, m.text),
    h('button', { class: 'mini', type: 'button', onclick: async () => { C.held = C.held.filter(x => x !== m); drawHeld(C); try { await api('/api/chat/send', { session: C.sid, text: m.text, steer: false }); refreshState(C, true); } catch (e) { C.held.push(m); drawHeld(C); toast(e.message); } } }, 'Send now'),
    h('button', { class: 'mini', type: 'button', onclick: () => { C.held = C.held.filter(x => x !== m); drawHeld(C); } }, 'Remove'))));
}

// ----- live events: a turn is one header, then steps (one per model call) with rows, then the answer -----
function stick(C) { followStick(C); }
// Times: every number on a row comes from an event (server clock). Nothing known -> nothing shown.
function nowS(C) { return Date.now() / 1000 - (C.skew || 0); }
function evT(C, ev) { if (ev.synthetic) return null; return typeof ev.ts === 'number' ? ev.ts : (C.replaying ? null : nowS(C)); }
const MK_TITLE = { run: 'Running', ok: 'Done', bad: 'Failed', wait: 'Waiting for your permission', stop: 'Stopped' };
function setMk(m, state) { m.className = 'mk ' + state; m.dataset.s = state; m.title = MK_TITLE[state] || ''; m.replaceChildren(); if (state === 'run') m.append(h('span', { class: 'spin' })); }
function mkEl(state) { const m = h('span', { class: 'mk' }); setMk(m, state); return m; }
function startTick(C) {
  if (C.tickId) return;
  C.tickId = setInterval(() => { if (C.closed) { clearInterval(C.tickId); C.tickId = 0; return; } tickAll(C); }, 250);
}
function tickAll(C) {
  const now = nowS(C); let any = false;
  for (const el of C.feed.querySelectorAll('[data-t0]')) {
    any = true; if (el.dataset.paused) continue;
    el.textContent = axDur(Math.max(0, now - (+el.dataset.t0) - (+el.dataset.sub || 0)));
  }
  const cur = C.cur;
  if (cur && cur.running && cur.t0 != null) { any = true; cur.el.textContent = axDur(Math.max(0, (cur.waitStart || now) - cur.t0 - cur.waited)); }
  if (!any && !C.cur) { clearInterval(C.tickId); C.tickId = 0; }
}
function headMeta(cur, done) {
  const m = cur.meta; m.replaceChildren(); const bits = [];
  if (!done) { if (cur.stepText) bits.push(cur.stepText); }
  else { if (cur.nTools) bits.push(axPlural(cur.nTools, 'tool')); if (cur.endNote) bits.push(cur.endNote); }
  bits.forEach((b, i) => m.append(h('span', { class: 'mi' }, b)));
  if (cur.nFail) m.append(h('span', { class: 'mi bad' }, cur.nFail + ' failed'));
}
// One label per kind of step, so Thinking, a Tool call, Narration between calls and the final Answer differ at a glance.
const KIND_ICON = { think: 'M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5V16h8v-2.5A6 6 0 0012 3z', tool: 'M5 7l5 5-5 5M12 17h7', note: 'M5 7h14M5 12h10M5 17h12', answer: 'M4 5h16v11H9l-5 4z' };
function kindLabel(kind, text) { return h('span', { class: 'kind k-' + kind }, svgIcon(KIND_ICON[kind]), h('span', { class: 'kt' }, text)); }
function toggleSteps(art) {
  const sh = art.querySelector('.tsteps'); const shown = sh && getComputedStyle(sh).display !== 'none';
  art.dataset.steps = shown ? 'closed' : 'open'; const b = art.querySelector('.thead'); if (b) b.setAttribute('aria-expanded', String(!shown));
}
// The turn's fibre: one node per tool call on a hairline, in the turn header, so a folded turn still shows what happened.
function fibAdd(cur, card) { if (!cur.fib) return; const i = h('i', { class: 'run' }); card.axFib = i; cur.fib.append(i); }
// A finished turn keeps every step on screen above its answer. Only the viewer's own "Collapse steps" choice (or the head's chevron) hides them.
function foldSteps(art) { art.classList.remove('live'); syncFold(art); }
// Collapse or expand the steps of every turn at once; the choice is remembered in this browser.
function stepsHidden() { return lsGet('axon-steps-hidden') === '1'; }
function applyStepsView() {
  const hid = stepsHidden(); document.documentElement.dataset.stepsView = hid ? 'hidden' : 'shown';
  document.querySelectorAll('.entry.a').forEach(a => { delete a.dataset.steps; syncFold(a); });
  document.querySelectorAll('.steps-all').forEach(b => { b.textContent = hid ? 'Expand steps' : 'Collapse steps'; b.setAttribute('aria-pressed', String(hid)); });
}
function toggleAllSteps() { lsSet('axon-steps-hidden', stepsHidden() ? '0' : '1'); applyStepsView(); }
function syncFold(art) { const sh = art.querySelector('.tsteps'), b = art.querySelector('.thead'); if (b && sh) b.setAttribute('aria-expanded', String(getComputedStyle(sh).display !== 'none')); }
function ensureTurn(C, ts) {
  if (C.cur) return C.cur;
  C.feed.querySelector('.empty-chat')?.remove();
  const art = h('article', { class: 'entry a ' + (C.replaying ? 'replayed' : 'live fresh') });
  const mk = mkEl('run'), title = h('span', { class: 'tl' }, 'Working'), el = h('span', { class: 'tel' }), meta = h('span', { class: 'tmeta' }), fib = h('span', { class: 'fib', 'aria-hidden': 'true' });
  const head = h('button', { type: 'button', class: 'turnbar thead', 'aria-expanded': 'true', title: 'Show or hide the steps of this turn', onclick: () => toggleSteps(art) }, mk, title, el, fib, meta, h('span', { class: 'chev', 'aria-hidden': 'true' }));
  const steps = h('div', { class: 'tsteps' }), answer = h('div', { class: 'tanswer' });
  art.append(h('div', { class: 'who' }, 'axon'), head, steps, answer);
  C.feed.append(art);
  const t0 = ts != null ? ts : (C.replaying ? null : nowS(C));
  C.cur = { art, head, mk, title, el, meta, fib, body: steps, answer, t0, prose: null, proseText: '', think: null, thinkText: '', tools: {}, step: null, group: null,
    nSteps: 0, n: 0, nTools: 0, nFail: 0, cost: 0, waited: 0, waitStart: 0, stepText: '', running: true };
  headMeta(C.cur, false); startTick(C);
  return C.cur;
}
function hostOf(C) { const cur = ensureTurn(C); return cur.step ? cur.step.body : cur.body; }
function userEntry(C, text, chars) {
  C.feed.querySelector('.empty-chat')?.remove();
  const md = h('div', { class: 'md' }); md.append(pagedPre(text, 'out'));
  const art = h('article', { class: 'entry u ' + (C.replaying ? 'replayed' : 'fresh') }, h('div', { class: 'who' }, 'you'), md);
  C.feed.append(art); decorate(art); stick(C);
}
function raw(C, ev) { if (!C.cur) return; hostOf(C).append(h('details', { class: 'rawev' }, h('summary', null, ev.type), h('pre', { class: 'out nolink' }, JSON.stringify(ev, null, 1).slice(0, 6000)))); }

// ----- steps -----
function newStep(C, cur, ev, ts) {
  const n = ev.iteration || cur.nSteps + 1;
  const mk = mkEl('run'), time = h('span', { class: 'stime' }), cost = h('span', { class: 'scost' }), sub = h('span', { class: 'ssub' });
  const hd = h('div', { class: 'shead' }, mk, h('span', { class: 'stitle' }, 'Step ' + n), sub, time, cost);
  const sent = h('div', { class: 'sline s-sent' }), got = h('div', { class: 'sline s-got' });
  const detail = h('div', { class: 'sdetail' }, sent, got), body = h('div', { class: 'sbody' });
  const el = h('section', { class: 'step', 'data-n': n }, hd, body);   // the Sent/Got/stats rows were removed: one usage summary closes the turn
  if (ev.entry != null) el.dataset.i = ev.entry;
  cur.body.append(el);
  if (ts != null) time.dataset.t0 = ts;
  const st = { n, el, hd, mk, time, cost, sub, detail, sent, got, model: ev.model || '', body, t0: ts, ended: false, expect: false, open: 0, tools: 0, modelDur: null, standalone: [], groups: [], bad: false, waiting: 0 };
  if (ev.model || ev.tokens_in) sent.append(h('b', null, 'Sent'), ev.model ? h('span', null, ev.model) : null, ev.synthetic || !ev.tokens_in ? null : h('span', { title: 'Estimated size of the whole request: conversation, system prompt and tool descriptions' }, '~' + tok(ev.tokens_in) + ' tokens'));
  const fb = ev.fed_back || []; if (fb.length) sent.append(h('span', { title: fb.map(f => f.name + (f.tokens ? ' (' + tok(f.tokens) + ' tokens)' : '')).join(', ') }, axPlural(fb.length, 'tool result') + ' fed back'));
  st.sd = { n, model: ev.model || '', msgs: ev.message_count != null ? ev.message_count : null, tin: ev.tokens_in || null }; st.stats = null;
  cur.step = st; cur.nSteps++; cur.n = cur.nSteps; cur.stepText = 'Step ' + n + ': thinking'; cur.group = null; headMeta(cur, false);
  return st;
}
function groupTotal(g) {
  const ts = g.cards.map(c => c.axT).filter(Boolean); if (ts.length < g.cards.length || !ts.length) return null;
  if (ts.every(t => t.par)) { const s = Math.min(...ts.map(t => t.s)), e = Math.max(...ts.map(t => t.e)); return Math.max(0, e - s); }
  return ts.reduce((a, t) => a + t.d, 0);
}
function stepToolTime(st) {
  let sum = 0;
  for (const c of st.standalone) if (c.axT) sum += c.axT.d;
  for (const g of st.groups) { const v = groupTotal(g); if (v != null) sum += v; }
  return sum;
}
function updateStepTime(st) {
  if (st.waiting) return;
  if (!st.ended || st.open > 0) return;
  delete st.time.dataset.t0; st.time.removeAttribute('data-sub');
  st.time.textContent = st.modelDur == null ? '' : axDur(st.modelDur + stepToolTime(st));
}
function maybeFinishStep(cur, st) {
  if (!st || !st.ended || st.open > 0 || (st.expect && st.tools === 0)) return;
  setMk(st.mk, st.bad ? 'bad' : 'ok'); updateStepTime(st);
  if (cur.step === st && !st.tools) cur.stepText = '';
}
function groupHead(g) {
  const ts = g.cards.map(c => c.axT).filter(Boolean); const all = ts.length === g.n;
  const par = all && ts.every(t => t.par), seq = all && ts.every(t => t.par === false);
  const bad = g.cards.filter(c => c.dataset.state === 'bad').length;
  g.title.textContent = !g.finished ? (g.n - g.done) + ' of ' + g.n + ' calls running in parallel' : par ? g.n + ' calls ran in parallel' : seq ? g.n + ' calls ran one after another' : g.n + ' calls';
  g.el.dataset.state = !g.finished ? 'run' : bad ? 'bad' : 'ok'; setMk(g.gmk, !g.finished ? 'run' : bad ? 'bad' : 'ok');
  g.bar.firstChild.style.width = Math.round(100 * g.done / Math.max(1, g.n)) + '%';
  const v = g.finished ? groupTotal(g) : null;
  if (g.finished) { delete g.time.dataset.t0; g.time.textContent = v == null ? '' : axDur(v); }
}

// ----- prose: the last text of a turn is the answer; text before a tool call is narration inside its step -----
function flushProse(cur) { if (cur.live && cur.prose) { liveFlush(null, cur); return; } if (cur.mdTimer) { clearTimeout(cur.mdTimer); cur.mdTimer = 0; } if (cur.prose && cur.proseText.length <= 300000) cur.prose.innerHTML = mdLite(cur.proseText); }
function moveProse(cur) {
  if (!cur.prose) return;
  flushProse(cur); const p = cur.prose; p.classList.remove('stream'); p.classList.add('narr'); p.prepend(kindLabel('note', 'Narration')); cur.answer.querySelector(':scope > .kind')?.remove();
  (cur.step ? cur.step.body : cur.body).append(p); decorate(p); cur.prose = null; cur.proseText = '';
}
// ----- thinking -----
function thinkStart(C, cur, ts) {
  const pre = h('pre', { class: 'out nolink' }); const mk = mkEl('run');
  const sum = h('summary', { class: 'xrow' }, mk, kindLabel('think', 'Thinking'), h('span', { class: 'ta think-t' }, ''), h('span', { class: 'td' }));
  if (ts != null) sum.querySelector('.td').dataset.t0 = ts;
  const d = h('details', { class: 'tool think kd-think', open: true }, sum, h('div', { class: 'tdet' }, pre));
  if (!cur.step) newStep(C, cur, { iteration: cur.nSteps + 1 }, ts);
  cur.step.body.append(d);
  cur.think = d; cur.thinkPre = pre; cur.thinkMk = mk; cur.thinkT0 = ts; cur.thinkText = '';
  cur.stepText = 'Step ' + cur.step.n + ': thinking'; headMeta(cur, false);
}
function finishThink(C, cur, ts) {
  if (!cur || !cur.think) return;
  const d = cur.think; const dsec = ts != null && cur.thinkT0 != null ? ts - cur.thinkT0 : null;
  const td = d.querySelector('.td'); td.removeAttribute('data-t0'); td.textContent = '';
  d.querySelector('.think-t').textContent = dsec != null && dsec >= 0.1 ? axDur(dsec) : '';
  setMk(cur.thinkMk, 'ok'); cur.think = null;
}

function applyEvent(C, ev) {
  const t = ev.type; const ts = evT(C, ev);
  if (!C.replaying && C.replayTs && typeof ev.ts === 'number' && ev.ts <= C.replayTs) return;   // already drawn from the record
  if (!C.replaying) { C.lastEv = Date.now(); if (C.skew == null && typeof ev.ts === 'number') C.skew = Date.now() / 1000 - ev.ts; }
  if (typeof axOnEvent === 'function') axOnEvent(C, ev);
  if (C.cur && C.cur.live && t !== 'text_delta' && t !== 'thinking_delta') liveFlush(C, C.cur);   // show every word before the next card, the end or Stop
  if (state.trace === 'debug' && C.cur && !['text_delta', 'thinking_delta'].includes(t)) raw(C, ev);
  switch (t) {
    case 'user': userEntry(C, ev.text, ev.chars); C.cur = null; C.steps = []; liveTitle(C, ev.text); drawRail(C); break;
    case 'turn_start': ensureTurn(C, ts); break;
    case 'llm_call_start': { const cur = ensureTurn(C, ts); finishThink(C, cur, ts); moveProse(cur); newStep(C, cur, ev, ts); break; }
    case 'thinking_delta': { const cur = ensureTurn(C, ts);
      if (!cur.think) thinkStart(C, cur, ts);
      cur.thinkText += ev.text; cur.thinkPre.textContent = cur.thinkText; stick(C); break; }
    case 'text_delta': { const cur = ensureTurn(C, ts); finishThink(C, cur, ts);
      if (!cur.prose) { cur.proseText = ''; cur.live = null; cur.mdst = null; cur.prose = h('div', { class: 'md stream' }); if (!cur.answer.querySelector('.kind')) cur.answer.append(kindLabel('answer', 'Answer')); cur.answer.append(cur.prose); }
      cur.proseText += ev.text;
      liveText(C, cur);
      if (ev.clipped) cur.answer.append(h('div', { class: 'muted' }, 'Showing the first ' + ev.text.length.toLocaleString() + ' of ' + ev.clipped.total.toLocaleString() + ' characters.'));
      break; }
    case 'tool_write': liveWrite(C, ev); break;
    case 'tool_use': (C.uses = C.uses || {})[ev.id] = ev; toolStart(C, ev, ts); break;
    case 'tool_result': trackTouched(C, ev); toolDone(C, ev, ts); liveFiles(C, ev); break;
    case 'tool_timing': toolTiming(C, ev); break;
    case 'permission': { const cur = C.cur; const card = cur && cur.tools[ev.id]; if (card) permInfo(card, ev); break; }
    case 'approval_request': approvalCard(C, ev, ts); break;
    case 'approval_resolved': approvalDone(C, ev, ts); break;
    case 'question_request': if (C.replaying) { hostOf(C).append(notice('Asked: ' + ev.question)); break; } questionCard(C, ev); break;
    case 'elicit_request': if (C.replaying) { hostOf(C).append(notice('MCP server ' + ev.server + ' asked: ' + ev.message)); break; } elicitCard(C, ev); break;
    case 'elicit_resolved': document.getElementById('el-' + ev.id)?.replaceWith(notice('Answered the MCP request: ' + ev.action, ev.action === 'accept' ? 'ok' : '')); break;
    case 'question_resolved': document.getElementById('q-' + ev.id)?.replaceWith(notice('Answered: ' + (ev.labels ? ev.labels.join(', ') : 'skipped'), 'ok')); break;
    case 'llm_call_end': { const cur = ensureTurn(C, ts); finishThink(C, cur, ts); const u = ev.usage || {};
      const st = cur.step || newStep(C, cur, { iteration: ev.iteration, model: ev.model }, ts);
      st.ended = true; st.expect = ev.stop_reason === 'tool_use'; st.modelDur = ev.duration_s != null ? ev.duration_s : null;
      cur.cost += ev.cost || 0; axStepEnd(cur, st, ev);
      if (ev.cost) st.cost.textContent = usd(ev.cost);
      st.got.append(h('b', null, 'Got'), h('span', null, tok(u.input_tokens || 0) + ' in / ' + tok(u.output_tokens || 0) + ' out'), u.cache_read_tokens ? h('span', null, tok(u.cache_read_tokens) + ' cached') : null, u.cache_write_tokens ? h('span', null, tok(u.cache_write_tokens) + ' written to cache') : null, u.reasoning_tokens ? h('span', null, tok(u.reasoning_tokens) + ' reasoning') : null, ev.stop_reason ? h('span', null, 'stop: ' + ev.stop_reason) : null);
      if (!st.sent.firstChild && ev.model) st.sent.append(h('b', null, 'Sent'), h('span', null, ev.model));
      st.hd.title = [ev.model || st.model, tok(u.input_tokens || 0) + ' in / ' + tok(u.output_tokens || 0) + ' out tokens', u.cache_read_tokens ? tok(u.cache_read_tokens) + ' cached' : ''].filter(Boolean).join(' · ');
      if (ev.outcome && ev.outcome !== 'ok') { st.bad = true; st.got.append(h('span', { class: 'bad', title: ev.error || '' }, ev.outcome + (ev.error ? ': ' + String(ev.error).slice(0, 160) : ''))); st.sub.textContent = ev.outcome; st.sub.classList.add('bad'); }
      const sd = Object.assign({ n: ev.iteration, model: ev.model, tin: u.input_tokens || 0 }, { dur: ev.duration_s || 0, cost: ev.cost || 0, tout: u.output_tokens || 0, cread: u.cache_read_tokens || 0, stop: ev.stop_reason, miss: ev.cache_miss_cause || '', detail: ev.cache_miss_detail || '' });
      C.steps.push(sd); (cur.stepData = cur.stepData || []).push(sd); maybeFinishStep(cur, st); headMeta(cur, false); drawRail(C); break; }
    case 'cache_status': axCacheStatus(C, ev); { const last = C.steps[C.steps.length - 1]; if (last && ev.miss) { last.miss = ev.cause || 'cache miss'; } const cst = C.cur && C.cur.step; if (cst && !cst.cacheEl && (ev.miss || ev.cached_tokens)) { cst.cacheEl = ev.miss ? h('span', { class: 'miss' }, 'cache miss: ' + (ev.cause || 'unknown') + (ev.resent_tokens ? ' (' + tok(ev.resent_tokens) + ' re-sent)' : '')) : h('span', { class: 'chit', title: tok(ev.cached_tokens) + ' of ' + tok(ev.input_tokens || 0) + ' input tokens were read from the prompt cache' }, 'cache hit ' + Math.round(ev.hit_pct || 0) + '%'); cst.got.append(cst.cacheEl); } break; }
    case 'context_edit': (C.cur ? hostOf(C) : C.feed).append(notice(ev.message || 'cleared old tool results', '')); break;
    case 'subagent_step': subagentStep(C, ev); break;
    case 'subagent': subagentEvent(C, ev); if (ev.phase !== 'start') liveFiles(C, { changes: [1] }); break;
    case 'compaction': if (ev.phase) { (C.cur ? hostOf(C) : C.feed).append(notice(ev.phase === 'start' ? 'compacting in the background…' : ev.phase === 'cancelled' ? 'background compaction cancelled' : 'background compaction ' + ev.phase, 'warn')); break; }
      C.cur ? hostOf(C).append(notice('Context compacted (' + ev.trigger + '): ' + ev.messages_before + ' → ' + ev.messages_after + ' messages, ' + tok(ev.tokens_before) + ' → ' + tok(ev.tokens_after) + ' tokens' + (ev.cost ? ', ' + usd(ev.cost) : ''), 'warn', ev.summary)) : C.feed.append(notice('Context compacted', 'warn', ev.summary)); break;
    case 'large_input': hostOf(C).append(notice((ev.spilled ? 'Large input: only its start and end were sent. ' : 'Large input sent whole. ') + ev.lines.toLocaleString() + ' lines, ~' + tok(ev.tokens_full) + ' tokens' + (ev.spilled ? ' (sent ~' + tok(ev.tokens_sent) + ')' : '') + (ev.path ? '. Full text saved at ' + ev.path : '') + (ev.cost_full ? '. ~' + usd(ev.cost_full) : ''), ev.spilled ? 'warn' : '')); break;
    case 'notify': budgetNotify(C, ev); break;      // dash_js_budget.py
    case 'budget': if (ev.action === 'paused') { budgetPaused(C, ev); break; } hostOf(C).append(notice(ev.message || ('Budget ' + ev.scope + ': ' + ev.action + ' at ' + usd(ev.spent_usd) + ' of ' + usd(ev.limit_usd)), ev.action === 'notice' ? (ev.percent >= 100 ? 'warn' : '') : 'warn')); break;
    case 'retry': axRetry(C, ev); hostOf(C).append(notice('Retrying the model request (' + ev.attempt + '/' + ev.max_attempts + ') in ' + (Math.round(Number(ev.delay_s || 0) * 10) / 10) + 's: ' + ev.error, 'warn')); break;
    case 'thinking_boost': hostOf(C).append(notice('"' + (ev.keyword || 'think') + '" raised the effort from ' + (ev.previous_effort || '?') + ' to ' + (ev.effort || '?') + (ev.thinking ? ' and turned thinking on' : '') + ' for this turn.', 'ok')); break;
    case 'goal_check': goalCheckEvent(C, ev); break;      // dash_js_goal.py
    case 'advisor': break;   // the consult_advisor tool card already shows the question and the advice; its cost is in the turn's usage table
    case 'auto_permission': if (ev.outcome === 'allow' && ev.source !== 'classifier') break; hostOf(C).append(h('div', { class: 'steprow' }, h('b', null, 'Auto mode'), h('span', null, (ev.tool || '') + ': ' + (ev.outcome || '')), ev.reason ? h('span', null, ev.reason) : null)); break;
    case 'steer': hostOf(C).append(notice(ev.shown === true ? "Included in the model's next step" : 'Your message was delivered to the running turn: ' + (ev.texts || []).join(' | '), 'ok')); break;
    case 'queued': if (C.replaying) break; C.feed.append(notice(ev.steer ? '› you: ' + ev.text.slice(0, 200) + (ev.state_text ? ' · ' + ev.state_text : '') : 'Queued for after the turn: ' + ev.text.slice(0, 200), 'ok')); if (C.st) { refreshState(C, true); } break;
    case 'memory_loaded': if (ev.reason === 'saved') { (C.cur ? hostOf(C) : C.feed).append(h('div', { class: 'steprow' }, h('b', null, 'Memory saved'), h('span', null, ev.message || ''))); break; } (C.cur ? hostOf(C) : C.feed).append(h('div', { class: 'steprow' }, h('b', null, 'Memory loaded'), h('span', null, relPath(ev.path)), h('span', null, ev.reason + (ev.trigger ? ' (' + ev.trigger + ')' : '')), ev.tokens ? h('span', null, tok(ev.tokens) + ' tokens') : null)); break;
    case 'hook': hookRow(C, ev); break;
    case 'tool_rationale': break;
    case 'tool_batch': if (ev.count > 1) { const cur = ensureTurn(C, ts); finishThink(C, cur, ts); moveProse(cur); if (!cur.step) newStep(C, cur, { iteration: cur.nSteps + 1 }, ts); toolGroup(C, cur, ev, ts); } break;
    case 'llm_request': axRequest(C, ev); if (C.cur && C.cur.step) { const sl = C.cur.step.sent; if (!sl.firstChild) sl.append(h('b', null, 'Sent'), h('span', null, ev.model || ''));
      sl.append(h('span', { title: 'Tokens in the request, by part' }, 'conversation ' + tok(ev.conversation_tokens || 0) + ' + system ' + tok(ev.system_tokens || 0) + ' + tools ' + tok(ev.tool_schema_tokens || 0)),
        h('span', null, axPlural(ev.message_count || 0, 'message')), h('span', null, axPlural(ev.tool_count || 0, 'tool') + ' offered' + (ev.prompt_tools ? ' (described in the prompt)' : '')),
        ev.effort ? h('span', null, 'effort ' + (typeof effortWord === 'function' ? effortWord(ev.effort) : ev.effort)) : null, h('span', null, ev.thinking ? 'thinking on' : 'thinking off')); } break;
    case 'turn_end': finishTurn(C, ev, ts); break;
    case 'interrupted': if (C.cur) hostOf(C).append(notice('Stopped', '')); finishTurn(C, null, ts); break;
    case 'error': (C.cur ? C.cur.answer : C.feed).append(turnError(ev.message)); finishTurn(C, null, ts, 'error'); break;
    case 'idle': if (!C.replaying) refreshState(C, true); break;
    case 'stop_requested': break;
    case 'note': C.feed.append(notice(ev.message)); break;
    case 'shell_command': { const cur = ensureTurn(C, ts); const card = mkTool('Shell', { command: ev.command }); const failed = !!(ev.refused || ev.code);
      card.querySelector('.ta').textContent = '$ ' + ev.command; card.axPanes.in.append(inputBlock('Bash', { command: ev.command })); setToolState(card, failed ? 'bad' : 'ok');
      const chips = card.querySelector('.chips'); if (ev.refused) chips.append(h('span', { class: 'chip2 del' }, 'refused')); else if (ev.code) chips.append(h('span', { class: 'chip2 del' }, 'exit ' + ev.code));
      card.querySelector('.td').textContent = ev.refused ? '' : axDur(ev.duration);
      const out = ev.refused ? h('div', { class: 'refused' }, ev.refused) : pagedPre(ev.output || '(no output)', 'out'); card.axPanes.out.replaceChildren(out); card.axText = ev.output || '';
      card.axPanes.why.append(h('p', { class: 'why' }, 'You ran this yourself from the message box.'));
      hostOf(C).append(card); decorate(card); if (failed) cur.nFail++; headMeta(cur, false); liveFiles(C, { changes: [1] }); break; }
    default: if (C.cur) hostOf(C).append(h('details', { class: 'rawev dtl' }, h('summary', null, 'event: ' + t), h('pre', { class: 'out nolink' }, JSON.stringify(ev, null, 1).slice(0, 4000))));
  }
  if (!C.replaying && ['tool_result', 'llm_call_end', 'compaction'].includes(t)) refreshState(C, true);
  if (!C.replaying) stick(C);
}

// The tab takes its name from the first message as soon as it is sent; the saved title replaces it once there is one.
function liveTitle(C, text) {
  if (C.replaying || (C.meta && C.meta.title) || C.liveName || !C.tab) return;
  const line = String(text || '').trim().split('\n')[0].replace(/\s+/g, ' '); if (!line) return;
  C.liveName = line; setTabTitle(C.tab, line.slice(0, 26)); if (typeof wbTitleSync === 'function') wbTitleSync();
}
// A tool that may have changed files updates the Files panel, the git marks and the Changes panel while the turn runs, not after it.
const READ_ONLY_TOOLS = ['Read', 'Ls', 'Glob', 'Grep', 'WebFetch', 'WebSearch', 'TodoWrite', 'ToolSearch', 'AskUserQuestion'];
let LIVE_FILES_T = 0;
function liveFiles(C, ev) {
  if (C.replaying) return;
  const name = ev.name || ((C.uses || {})[ev.tool_use_id] || {}).name || '';
  if (!(ev.changes && ev.changes.length) && (ev.is_error || !name || READ_ONLY_TOOLS.includes(name))) return;
  clearTimeout(LIVE_FILES_T);
  LIVE_FILES_T = setTimeout(() => {
    GIT_CACHE.t = 0; gitFiles();
    if (typeof treeReload === 'function' && TREE.cache['']) treeReload().catch(() => { /* the next change or Reload tries again */ });
    if (typeof wbRefreshChanges === 'function') wbRefreshChanges(true);
    if (typeof WB_BUSY !== 'undefined' && WB_BUSY.chg) { WB_BUSY.chg.t = 0; renderRightSoon(); }
  }, 250);
}
// What Axon itself changed in this chat, from its own tool calls: it does not depend on the folder being a git repository.
const EDIT_NAMES = ['Write', 'Edit', 'MultiEdit', 'Patch', 'NotebookEdit', 'Notebook'];
function relPath(p) { const ws = (state.ws || {}).workspace || ''; p = String(p || ''); return ws && p.startsWith(ws + '/') ? p.slice(ws.length + 1) : p; }
function trackTouched(C, ev) {
  if (ev.is_error) return;
  const T = C.touched = C.touched || {}; const use = (C.uses || {})[ev.tool_use_id];
  const code = (k) => /creat|new|add/i.test(k || '') ? 'A' : /delet|remov/i.test(k || '') ? 'D' : 'M';
  if (ev.changes && ev.changes.length) { for (const c of ev.changes) if (c.path) { const p = relPath(c.path); T[p] = T[p] === 'A' && code(c.kind) === 'M' ? 'A' : code(c.kind); } return; }
  if (use && EDIT_NAMES.includes(use.name)) { const inp = use.input || {}; const p0 = inp.file_path || inp.path || inp.notebook_path; if (p0) { const p = relPath(p0); T[p] = T[p] || (use.name === 'Write' ? 'A' : 'M'); } }
}
// ----- tool rows: marker | name | one plain sentence | chips | duration; Input / Output / Why open underneath -----
function setToolState(card, state) { card.dataset.state = state; axToolStat(card); setMk(card.axMk, state); card.classList.toggle('err', state === 'bad'); if (card.axFib) card.axFib.className = state; }
function selectTab(card, k) {
  card.axTab = k;
  card.querySelectorAll('.tab2').forEach(b => b.setAttribute('aria-selected', String(b.dataset.k === k)));
  for (const [kk, p] of Object.entries(card.axPanes)) p.hidden = kk !== k;
}
async function copyTab(card, btn, key) {
  const text = ({ in: card.axInText, out: card.axText, why: card.axWhyText })[key || card.axTab || 'out'] || '';
  try { await navigator.clipboard.writeText(text); }
  catch (e) { const ta = h('textarea', { style: 'position:fixed;opacity:0', 'aria-hidden': 'true' }); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand('copy'); } catch (x) { /* blocked */ } ta.remove(); }
  btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = 'Copy'; }, 1200);
}
function mkTool(name, input) {
  const mk = mkEl('run');
  const sum = h('summary', { class: 'xrow' }, mk, kindLabel('tool', 'Tool call'), h('span', { class: 'tn', title: name }, name), h('span', { class: 'ta' }), h('span', { class: 'chips' }), h('span', { class: 'route' }), h('span', { class: 'td' }), h('span', { class: 'pnote', hidden: true }));
  const panes = { in: h('div', { class: 'tpane p-in', hidden: true }), out: h('div', { class: 'tpane p-out', hidden: true }, h('span', { class: 'muted' }, 'Waiting for the result...')), why: h('div', { class: 'tpane p-why', hidden: true }) };
  const card = h('details', { class: 'tool live-tool kd-tool both', open: axCardsOpen(), 'data-state': 'run' }, sum);
  sum.addEventListener('click', () => { axCardsRemember(!card.open); });   // the click runs before the details toggles, so the new state is the opposite
  const doing = h('div', { class: 'pdoing' });
  const sec = (k, label, extra) => h('div', { class: 'psec', 'data-k': k }, h('b', null, label), extra || null, h('button', { type: 'button', class: 'copy', title: 'Copy ' + label.toLowerCase(), onclick: (e) => copyTab(card, e.currentTarget, k) }, 'Copy'));
  const stat = h('span', { class: 'pstat' });
  card.append(h('div', { class: 'tdet' }, sec('in', 'Input'), panes.in, sec('out', 'Output', stat), panes.out, panes.why));
  card.axDoing = doing; card.axStat = stat;
  card.addEventListener('toggle', () => { if (card.open && !card.axTab) selectTab(card, 'out'); });
  card.axMk = mk; card.axPanes = panes; card.axName = name; card.axIn = input || {}; card.axInText = '';
  const mcp = mcpOf(name);
  if (mcp) { const tn = sum.querySelector('.tn'); tn.textContent = mcp.tool; toolChip(card, 'mcp', 'MCP ' + mcp.server, 'Sent to the MCP server "' + mcp.server + '" as ' + mcp.tool + ' (full name ' + name + ')'); }
  return card;
}
function inputText(name, inp) { return JSON.stringify(inp || {}, null, 2); }
function toolGroup(C, cur, ev, ts) {
  const title = h('span', { class: 'ptitle' }, ev.count + ' calls running in parallel'), time = h('span', { class: 'ptime' });
  if (ts != null) time.dataset.t0 = ts;
  const rows = h('div', { class: 'prows' }); const gmk = mkEl('run'); const bar = h('i', { class: 'pbar', 'aria-hidden': 'true' }, h('b'));
  const el = h('div', { class: 'pgroup', 'data-state': 'run' }, h('div', { class: 'phead' }, gmk, title, bar, time), rows);
  cur.step.body.append(el);
  const g = { el, rows, title, time, gmk, bar, n: ev.count, added: 0, done: 0, cards: [], finished: false, order: (ev.tools || []).map(t => t.id) };
  cur.step.groups.push(g); cur.group = g; return g;
}
const RUNNING_WORD = (name, inp) => { let s = ''; try { s = axRunning(name, inp); } catch (e) { s = ''; } return s; };
// A file being written streams in (fine-grained tool streaming): one quiet row per call, updated in place with the
// path, the lines so far and the tail of the text; the real tool card replaces it when the call is complete.
function liveWrite(C, ev) {
  if (C.replaying) return;
  const cur = ensureTurn(C); if (!cur.step) newStep(C, cur, { iteration: cur.nSteps + 1 });
  cur.lw = cur.lw || {};
  let row = cur.lw[ev.id];
  if (!row) {
    row = h('div', { class: 'steprow livewrite' }, h('b', null, ev.verb || 'writing'), h('span', { class: 'lwp' }), h('span', { class: 'lwn muted' }),
      h('pre', { class: 'out nolink lwt', style: 'max-height:7.5em;overflow:hidden;margin:.25em 0 0;font-size:.85em;opacity:.85;white-space:pre-wrap' }));
    cur.step.body.append(row); cur.lw[ev.id] = row;
  }
  row.querySelector('.lwp').textContent = ev.path;
  row.querySelector('.lwn').textContent = ev.lines + (ev.lines === 1 ? ' line' : ' lines') + ' so far';
  row.querySelector('.lwt').textContent = ev.tail || '';
}
function liveWriteDone(cur, id) {
  if (!cur || !cur.lw) return;
  if (id == null) { for (const r of Object.values(cur.lw)) r.remove(); cur.lw = {}; return; }
  if (cur.lw[id]) { cur.lw[id].remove(); delete cur.lw[id]; }
}
function toolStart(C, ev, ts) {
  const cur = ensureTurn(C, ts); finishThink(C, cur, ts); moveProse(cur);
  if (!cur.step) newStep(C, cur, { iteration: cur.nSteps + 1 }, ts);
  liveWriteDone(cur, ev.id);
  const st = cur.step, inp = ev.input || {};
  const card = mkTool(ev.name, inp);
  axHandReset(st);
  card.querySelector('.ta').textContent = RUNNING_WORD(ev.name, inp) || toolSummary(ev.name, inp);
  card.querySelector('.ta').title = toolSummary(ev.name, inp);
  card.axInText = inputText(ev.name, inp); card.axPanes.in.append(axInputView(ev.name, inp, card.axInText));
  const why = card.axPanes.why;
  const g = cur.group && cur.group.added < cur.group.n ? cur.group : null;
  // One batch of parallel calls shares one labeled reason (written once, on the batch); a lone call shows its own.
  why.append(h('p', { class: 'perm muted' }));   // the model's reason is its thinking and narration, already drawn above; only the permission decision lives here
  if (ev.parse_error) why.append(h('div', { class: 'bad' }, ev.parse_error));
  // tools of one batch start on separate threads, so their events can arrive in any order: the rows keep the order the model asked in
  const ord = g ? g.order.indexOf(ev.id) : -1; card.axOrd = ord < 0 ? 1e9 : ord;
  const later = g ? g.cards.find(c => c.axOrd > card.axOrd && c.parentNode === g.rows) : null;
  if (later) g.rows.insertBefore(card, later); else (g ? g.rows : st.body).append(card);
  if (g) { g.added++; g.cards.push(card); card.axGroup = g; } else st.standalone.push(card);
  cur.tools[ev.id] = card; card.axStep = st; card.axT0 = ts; card.axId = ev.id;
  if (ts != null) card.querySelector('.td').dataset.t0 = ts;
  st.open++; st.tools++; cur.nTools++; fibAdd(cur, card);
  cur.stepText = 'Step ' + st.n + ': ' + (RUNNING_WORD(ev.name, inp) || ev.name).slice(0, 60); headMeta(cur, false);
  st.sub.textContent = axPlural(st.tools, 'tool');
  decorate(card);
  if (ev.name === 'Task') { card.dataset.task = '1'; card.axPanes.why.append(h('div', { class: 'next' }, h('a', { href: '#/agents/' + encodeURIComponent(C.sid), class: 'plink' }, 'View subagents'))); }
}
// A hook run is a step like any other: one row saying which event, for which tool, how many ran and what they decided.
function hookRow(C, ev) {
  const card = C.cur && ev.tool_use_id ? C.cur.tools[ev.tool_use_id] : null;
  const bits = [h('b', null, 'Hook'), h('span', null, ev.event + (ev.tool_name ? ' · ' + ev.tool_name : '')), h('span', null, ev.ran + ' ran')];
  if (ev.blocked) bits.push(h('span', { class: 'bad' }, 'blocked'));
  if (ev.permission_decision) bits.push(h('span', null, 'decided: ' + ev.permission_decision));
  if (ev.additional_context) bits.push(h('span', { title: ev.additional_context }, 'added context for the model'));
  if (ev.message) bits.push(h('span', { title: ev.message }, String(ev.message).slice(0, 200)));
  for (const w of ev.warnings || []) bits.push(h('span', { class: 'bad', title: w }, String(w).slice(0, 200)));
  const row = h('div', { class: 'steprow hookrow' }, ...bits);
  if (card && card.isConnected) { const tail = card.axHookTail || card; tail.after(row); card.axHookTail = row; toolChip(card, 'hook', 'hook ' + ev.event, ev.blocked ? 'A hook blocked this call' : 'A hook ran for this call'); }
  else (C.cur ? hostOf(C) : C.feed).append(row);
}
// Chips that say where a call went and who let it through. They sit beside the result chips and survive the result being drawn.
function toolChip(card, key, text, title, bad) {
  const box = card.querySelector('.route'); if (!box) return;
  let c = box.querySelector('[data-k="' + key + '"]');
  if (!c) { c = h('span', { class: 'chip2 rt', 'data-k': key }); box.append(c); }
  c.textContent = text; c.title = title || ''; c.classList.toggle('bad', !!bad);
}
function mcpOf(name) { const m = /^mcp__(.+?)__(.+)$/.exec(String(name || '')); return m ? { server: m[1], tool: m[2] } : null; }
function permNote(card, text, bad) { const n = card.querySelector('.pnote'); n.hidden = !text; n.textContent = text || ''; n.classList.toggle('bad', !!bad); }
const CHOICE_NOTE = { once: 'You allowed this once', always: 'You allowed this for the session', rule: 'You allowed this and saved a rule', deny: 'You denied this' };
const PERM_SRC = { rule: 'a saved rule', mode: 'the current mode', hook: 'a hook', user: 'you', invariant: 'a built-in safety rule', 'no-approver': 'no approver available', delegation: 'delegation' };
function permInfo(card, ev) {
  const src = PERM_SRC[ev.source] || ev.source || '';
  const line = (ev.outcome === 'deny' ? 'Denied' : 'Allowed') + (src ? ' because ' + src : '') + (ev.reason ? ': ' + ev.reason : '') + (ev.approval ? ' (you chose: ' + ev.approval + ')' : '');
  const p = card.axPanes.why.querySelector('.perm'); if (p) p.textContent = line;
  card.axWhyText = (card.axWhyText ? card.axWhyText + '\n' : '') + line;
  // who decided, on the row itself: the rule that matched, the mode in force, a hook, or you
  const by = ev.source === 'rule' ? 'rule' + (ev.rule ? ' ' + ev.rule : '') : ev.source === 'mode' ? (ev.mode ? ev.mode + ' mode' : 'mode') : ev.source === 'user' ? 'you' : (PERM_SRC[ev.source] || ev.source || '');
  if (ev.outcome && by) toolChip(card, 'perm', (ev.outcome === 'deny' ? 'denied by ' : ev.outcome === 'ask' ? 'asked: ' : 'allowed by ') + by, line, ev.outcome === 'deny');
  if (ev.outcome === 'deny') { card.classList.add('denied'); setToolState(card, 'bad'); permNote(card, ev.source === 'user' ? CHOICE_NOTE.deny : 'Blocked' + (src ? ' by ' + src : '') + (ev.reason ? ': ' + ev.reason : ''), true); }
  else if (ev.source === 'user') permNote(card, CHOICE_NOTE[ev.approval] || 'You allowed this');
}
function toolTiming(C, ev) {
  const cur = C.cur; const card = cur && cur.tools[ev.id || ev.tool_use_id]; if (!card) return;
  let s = ev.started_at, e = ev.ended_at, d = ev.duration_s;
  if (d == null) return;
  // The clock includes the time the tool waited for your permission; the work itself began when you answered.
  if (card.axApproved != null && e != null) { s = Math.max(s != null ? s : 0, card.axApproved); d = Math.max(0, e - s); }
  card.axT = { s: s != null ? s : 0, e: e != null ? e : (s || 0) + d, d, par: ev.parallel === undefined ? undefined : !!ev.parallel };
  const td = card.querySelector('.td'); td.removeAttribute('data-t0'); td.textContent = axDur(d); axToolStat(card);
  const st = card.axStep; if (st) updateStepTime(st);
  const g = card.axGroup; if (g && g.finished) groupHead(g);
}
// A shell command that exited non-zero failed, though the tool call itself worked: the same rule as the terminal (tool_cards.is_failed).
const BASH_TOOLS = new Set(['Bash', 'bash', 'run_command']);
const BASH_EXIT_RE = /^\s*Command exited with non-zero code -?\d+/;
function toolDone(C, ev, ts) {
  const cur = C.cur; const card = cur && cur.tools[ev.tool_use_id]; if (!card) return;
  const failed = !!ev.is_error || (BASH_TOOLS.has(ev.name) && BASH_EXIT_RE.test(String(ev.content || '')));
  setToolState(card, failed ? 'bad' : 'ok'); card.classList.toggle('denied', card.classList.contains('denied'));
  const td = card.querySelector('.td'); td.removeAttribute('data-t0'); if (!card.axT) td.textContent = '';
  const o = card.axPanes.out; o.replaceChildren();
  let text = ev.content || '';
  if (ev.blob_content) text += '\n[showing the start; the full output (' + ev.blob_content.len.toLocaleString() + ' characters) is kept in the session record]';
  if (ev.truncated_from) text += '\n[showing the first ' + text.length.toLocaleString() + ' of ' + ev.truncated_from.toLocaleString() + ' characters; the full output is in the transcript]';
  card.axText = text;
  o.append(/^(@@ -|diff --git |--- \S)/m.test(text.slice(0, 4000)) ? diffEl(text.split('\n')) : axOutView(text));
  if (ev.more) o.append(h('div', { class: 'clipped' }, h('span', null, 'Showing the first ' + text.length.toLocaleString() + ' of ' + ev.more.total.toLocaleString() + ' characters. '),
    h('button', { class: 'more', type: 'button', onclick: async (e) => { const b = e.currentTarget; b.disabled = true; try { let off = 0, all = ''; for (;;) { const r = await api('/api/session/' + encodeURIComponent(C.sid) + '/text?entry=' + ev.more.entry + '&field=' + encodeURIComponent(ev.more.field) + '&offset=' + off + '&limit=400000'); all += r.text; off = r.next; if (r.done) break; } card.axText = all; o.replaceChildren(axOutView(all)); decorate(o); } catch (x) { b.disabled = false; toast(x.message); } } }, 'Show all')));
  if (ev.changes && ev.changes.length) o.append(changesBlock(ev.changes));
  if (typeof axCard === 'function') { try { axCard(card, ev, text); } catch (e) { console.warn('card', e); } }
  decorate(card);
  if (card.axTab) selectTab(card, card.axTab);
  const st = card.axStep, g = card.axGroup;
  if (failed) { cur.nFail++; }
  if (st) { st.open--; st.hp = false; axHandoff(cur, st, card); maybeFinishStep(cur, st); }
  if (g) { g.done++; if (g.done >= g.n) g.finished = true; groupHead(g); }
  if (cur.step === st && st && st.open === 0) cur.stepText = 'Step ' + st.n + ': thinking';
  headMeta(cur, false);
  if (!C.replaying && (ev.name === 'TodoWrite' || ev.name === 'Task')) refreshState(C, true);
}

// The exact allow rule "Always allow" saves (mirrors PermissionEngine.narrow_rule_for).
function narrowRule(tool, a) {
  a = a || {};
  if (tool === 'Bash') {
    const cmd = String(a.command || '').split(/\s+/).filter(Boolean).join(' ');
    if (cmd && !/[;&|`$<>\n]/.test(cmd) && !/["']/.test(cmd)) {
      const argv = cmd.split(' '); const w = [argv[0]];
      if (argv.length > 1 && !argv[1].startsWith('-') && !argv[1].includes('/') && !argv[1].includes('\\') && !argv[1].includes('.')) w.push(argv[1]);
      return 'Bash(' + w.join(' ') + ':*)';
    }
    return 'Bash(' + cmd + ')';
  }
  if (tool === 'Git') { const sub = String(a.subcommand || '').trim(); return sub ? 'Git(' + sub + ':*)' : 'Git'; }
  if (tool === 'WebFetch' || tool === 'Http') { let host = ''; try { host = new URL(String(a.url || '')).hostname; } catch (e) { /* none */ } return host ? tool + '(domain:' + host + ')' : tool; }
  const path = a.path || a.file_path || a.notebook_path;
  if (path && EDIT_NAMES.includes(tool)) return 'Edit(' + relPath(path) + ')';
  if (path && ['Read', 'Ls', 'Glob', 'Grep'].includes(tool)) return 'Read(' + relPath(path) + ')';
  return tool + ' (every call)';
}
// Permission: a request waits on its own tool row (pending ones stay prominent); the decision becomes a small note on that row.
function waitingRow(cur, tool) { return Object.values(cur.tools).reverse().find(c => c.axName === tool && c.axWaitAt == null && c.dataset.state === 'run'); }
function approvalCard(C, ev, ts) {
  const cur = ensureTurn(C, ts);
  const row = waitingRow(cur, ev.tool);
  (C.aps = C.aps || {})[ev.id] = { row: row || null, ev, ts };
  if (row) {
    row.axWaitAt = ts != null ? ts : 0; setToolState(row, 'wait'); row.classList.add('hp');
    const td = row.querySelector('.td'); td.removeAttribute('data-t0'); td.textContent = '';
    const st = row.axStep; if (st) { st.waiting++; st.hp = true; st.el.classList.add('hp'); st.time.dataset.paused = '1'; }
  }
  cur.waitStart = ts != null ? ts : 0; cur.title.textContent = 'Waiting for you';
  if (!C.replaying) buildApproval(C, ev, row);
}
function buildApproval(C, ev, row) {
  const cur = ensureTurn(C);
  const a = ev.args || {};
  const body = [];
  if (ev.tool === 'ExitPlanMode' && a.plan) { const pm = h('div', { class: 'md' }); pm.append(pagedPre(String(a.plan), 'out')); body.push(h('div', { class: 'lbl' }, 'Plan'), pm); }
  else body.push(pagedPre(ev.input, 'out'));
  const card = h('div', { class: 'approve', id: 'ap-' + ev.id, role: 'alertdialog', 'aria-label': 'Permission needed' },
    h('h4', null, 'Axon wants to use ', h('code', null, ev.tool)), h('div', { class: 'why' }, (EXPLAIN[ev.tool] || 'This tool is not covered by your current permission rules.') + (ev.reason ? ' ' + ev.reason : '')), ...body);
  const decide = async (choice) => { card.querySelectorAll('button').forEach(b => b.disabled = true); try { await api('/api/chat/approval', { session: C.sid, id: ev.id, choice }); } catch (e) { toast(e.message); card.querySelectorAll('button').forEach(b => b.disabled = false); } };
  card.append(h('div', { class: 'acts' },
    h('button', { class: 'btn primary', onclick: () => decide('once') }, ev.tool === 'ExitPlanMode' ? 'Approve plan' : 'Allow once'),
    ev.tool === 'ExitPlanMode' ? null : h('button', { class: 'btn', title: 'Allow this tool for the rest of this session', onclick: () => decide('always') }, 'Always this session'),
    ev.tool === 'ExitPlanMode' ? null : h('button', { class: 'btn', title: 'Saves this allow rule in the project file .axon/settings.local.json, so matching calls run without asking: ' + narrowRule(ev.tool, a), onclick: () => decide('rule') }, 'Always allow ' + narrowRule(ev.tool, a)),
    h('button', { class: 'btn danger', onclick: () => decide('deny') }, 'Deny')));
  if (row) row.after(card); else hostOf(C).append(card);
  decorate(card); card.scrollIntoView({ block: 'nearest' });
}
function approvalDone(C, ev, ts) {
  const cur = C.cur; const ap = C.aps && C.aps[ev.id]; const row = ap && ap.row;
  document.getElementById('ap-' + ev.id)?.remove();
  if (cur && cur.waitStart && ts != null) { cur.waited += Math.max(0, ts - cur.waitStart); }
  if (cur) { cur.waitStart = 0; if (cur.running) cur.title.textContent = 'Working'; }
  if (!row) { if (!ap) (cur ? hostOf(C) : C.feed).append(notice((ev.choice === 'deny' ? 'Denied ' : 'Allowed ') + ev.tool + ' (' + ev.choice + ')', ev.choice === 'deny' ? 'bad' : 'ok')); return; }
  row.classList.remove('hp'); row.axApproved = ts;
  const st = row.axStep; if (st) { st.waiting = Math.max(0, st.waiting - 1); if (!st.waiting) { st.hp = false; st.el.classList.remove('hp'); delete st.time.dataset.paused; if (ts != null && ap.ts != null) st.time.dataset.sub = String((+st.time.dataset.sub || 0) + Math.max(0, ts - ap.ts)); } }
  permNote(row, CHOICE_NOTE[ev.choice] || 'You answered', ev.choice === 'deny');
  if (ev.choice === 'deny') { row.classList.add('denied'); setToolState(row, 'bad'); }
  else { setToolState(row, 'run'); if (ts != null) row.querySelector('.td').dataset.t0 = ts; }
}
function elicitCard(C, ev) {
  ensureTurn(C);
  const inputs = {};
  const rows = (ev.fields || []).map((f) => {
    let inp;
    if (f.enum && f.enum.length) inp = h('select', { 'aria-label': f.label }, ...(f.required ? [] : [h('option', { value: '' }, '(none)')]), ...f.enum.map(e => h('option', { value: e, selected: String(f.default) === e }, e)));
    else if (f.type === 'boolean') inp = h('input', { type: 'checkbox', checked: !!f.default, 'aria-label': f.label });
    else inp = h('input', { type: f.type === 'integer' || f.type === 'number' ? 'number' : 'text', value: f.default == null ? '' : String(f.default), 'aria-label': f.label, placeholder: f.description || '' });
    inputs[f.key] = [f, inp];
    return h('div', { class: 'formrow' }, h('label', null, f.label + (f.required ? ' *' : '')), inp, f.description ? h('span', { class: 'muted' }, f.description) : null);
  });
  const read = () => {
    const out = {};
    for (const [k, [f, inp]] of Object.entries(inputs)) {
      if (f.type === 'boolean' && !(f.enum && f.enum.length)) { out[k] = inp.checked; continue; }
      const v = inp.value.trim(); if (v === '') { if (f.required) throw new Error(f.label + ' is required'); continue; }
      out[k] = f.type === 'integer' ? parseInt(v, 10) : f.type === 'number' ? parseFloat(v) : f.type === 'array' ? v.split(',').map(x => x.trim()).filter(Boolean) : v;
    }
    return out;
  };
  const send = async (action, content) => { try { await api('/api/chat/elicit', { session: C.sid, id: ev.id, action, content }); } catch (e) { toast(e.message); } };
  const msg = h('p', null, ev.message || 'The server needs more information.');
  const link = ev.url ? h('p', null, h('a', { href: ev.url, target: '_blank', rel: 'noopener noreferrer' }, ev.url)) : null;
  const card = h('div', { class: 'approve question', id: 'el-' + ev.id }, h('h4', null, 'MCP server ' + ev.server + ' asks for input'), msg, link, ...rows,
    h('div', { class: 'acts' },
      h('button', { class: 'btn primary', onclick: () => { try { send('accept', ev.fields && ev.fields.length ? read() : undefined); } catch (e) { toast(e.message); } } }, ev.mode === 'url' ? 'I opened it' : 'Submit'),
      h('button', { class: 'btn', onclick: () => send('decline') }, 'Decline'), h('button', { class: 'btn', onclick: () => send('cancel') }, 'Cancel')));
  hostOf(C).append(card);
}
function questionCard(C, ev) {
  const cur = ensureTurn(C); const picked = new Set();
  const opts = (ev.options || []).map((o, i) => { const inp = h('input', { type: ev.multi ? 'checkbox' : 'radio', name: 'q' + ev.id, onchange: () => { if (!ev.multi) picked.clear(); inp.checked ? picked.add(o.label) : picked.delete(o.label); } }); return h('label', null, inp, h('span', null, h('b', null, o.label), o.description ? h('div', { class: 'muted' }, o.description) : null)); });
  const send = async (labels) => { try { await api('/api/chat/answer', { session: C.sid, id: ev.id, labels }); } catch (e) { toast(e.message); } };
  const card = h('div', { class: 'approve question', id: 'q-' + ev.id }, h('h4', null, (ev.header ? ev.header + ': ' : '') + ev.question), ...opts,
    h('div', { class: 'acts' }, h('button', { class: 'btn primary', onclick: () => send([...picked]) }, 'Answer'), h('button', { class: 'btn', onclick: () => send(null) }, 'Skip')));
  hostOf(C).append(card);
}

function subagentEvent(C, ev) {
  const cur = C.cur; if (C.replaying) { (cur ? cur.body : C.feed).append(notice((ev.phase === 'start' ? 'Subagent started: ' : 'Subagent ' + ev.status + ': ') + ev.agent_type + ' ' + ev.title)); return; }
  const line = (ev.phase === 'start' ? 'Subagent started: ' : 'Subagent ' + ev.status + ': ') + ev.agent_type + ' · ' + ev.title + (ev.phase === 'end' ? ' · ' + usd(ev.cost) + ' · ' + tok((ev.input_tokens || 0) + (ev.output_tokens || 0)) + ' tokens · ' + dur(ev.duration_s) : '');
  const n = notice('', ev.status === 'error' ? 'bad' : '');
  n.append(h('a', { class: 'plink', href: '#/agents/' + encodeURIComponent(C.sid) }, line));
  (cur ? cur.body : C.feed).append(n);
  refreshSubs(C);
}
async function refreshSubs(C) {
  if (!C.sid) return;
  try { const d = await api('/api/subagents?session=' + encodeURIComponent(C.sid)); C.subs = d.subagents; C.subNotes = d.notes; drawRail(C); } catch (e) { /* none yet */ }
}
async function tailHistory(C, adopt) {
  if (!C.sid) return;
  try {
    const p = await api('/api/session/' + encodeURIComponent(C.sid) + '?after=' + C.lastIdx + '&limit=200');
    if (p.meta) { C.meta = p.meta; if (C.meta.title && C.tab) setTabTitle(C.tab, C.meta.title.slice(0, 26)); if (typeof wbTitleSync === 'function') wbTitleSync(); }   // the chat's name, once it has one
    if (p.entries.length) { if (adopt) { C.lastIdx = p.entries[p.entries.length - 1].i; } else { historyNodes(p.entries, C).forEach(n => C.feed.append(n)); C.lastIdx = p.entries[p.entries.length - 1].i; } }
    return p.entries.length;
  } catch (e) { return 0; }
}
async function finishTurn(C, ev, ts, how) {
  const cur = C.cur;
  if (cur) {
    liveWriteDone(cur); finishThink(C, cur, ts); flushProse(cur);
    cur.running = false; cur.stepText = ''; cur.waitStart = 0;
    const secs = ev && ev.duration_s != null ? Math.max(0, ev.duration_s - cur.waited) : (ts != null && cur.t0 != null ? Math.max(0, ts - cur.t0 - cur.waited) : null);
    if (ev) { cur.cost = ev.cost_usd != null ? ev.cost_usd : cur.cost; cur.costKnown = ev.cost_usd != null || cur.cost > 0; }
    const word = ev ? turnWord(ev.stop_reason) : '';
    const stopped = !!(ev && ev.stop_reason === 'interrupted');   // the user pressed Stop: not a failure, and not an error box
    cur.endNote = ev && !['Done'].includes(word) && !ev.is_error ? word : '';
    cur.title.textContent = ev && !ev.is_error ? (secs != null ? 'Worked for ' + axDur(secs) : 'Worked') : (!stopped && (how === 'error' || (ev && ev.is_error)) ? 'Failed' : 'Stopped') + (secs != null ? ' after ' + axDur(secs) : '');
    cur.el.textContent = ''; cur.el.removeAttribute('data-t0');
    setMk(cur.mk, ev && !ev.is_error ? 'ok' : stopped ? 'stop' : 'bad');
    cur.art.querySelectorAll('[data-t0]').forEach(e => { e.removeAttribute('data-t0'); e.textContent = ''; });
    cur.art.querySelectorAll('.step .mk.run, .step .mk.wait, .tool .mk.run, .tool .mk.wait').forEach(m => setMk(m, 'stop'));
    cur.art.querySelectorAll('.fib i.run, .fib i.wait').forEach(i => { i.className = 'stop'; });
    cur.art.querySelectorAll('.approve').forEach(a => a.remove());
    cur.art.querySelectorAll('.hp').forEach(a => a.classList.remove('hp'));
    cur.art.querySelectorAll('.pgroup .ptitle').forEach(g => { g.textContent = g.textContent.replace(/^\d+ of (\d+) calls running in parallel$/, '$1 calls').replace(/^(\d+) calls running in parallel$/, '$1 calls'); });
    cur.art.querySelectorAll('.pgroup[data-state=run]').forEach(g => { g.dataset.state = 'stop'; });
    headMeta(cur, true);
    try { const u = typeof axTurnUsage === 'function' ? axTurnUsage(cur) : null; if (u && !cur.art.querySelector('.tusage')) cur.art.append(u); } catch (e) { console.warn('usage', e); }
    cur.art.dataset.done = '1';
    foldSteps(cur.art);   // the reply is finished: its steps stay, and its hover actions (Copy, Regenerate, Bookmark...) may show
    if (stopped) cur.answer.append(notice('Stopped', ''));
    else if (ev && ev.is_error) cur.answer.append(turnError(ev.result || ev.error || 'The turn failed.'));
  }
  C.cur = null;
  settleSpinners(C, false);
  if (C.replaying) return;
  if (C.stopping) { C.stopping = false; drawHead(C); }
  if (C.st) { C.st.busy = false; drawHeadStatus(C); }   // the composer goes back to Send at once; the state read below confirms it
  refreshState(C, true); refreshSubs(C); setTimeout(() => { if (!C.closed) refreshState(C, true); }, 900);
  GIT_CACHE.t = 0; if (typeof treeReload === 'function' && TREE.cache['']) treeReload();
  // The turn is already drawn by the same code that draws a stored one: keep it, and only move the stored position forward.
  if (ev && C.sid) await tailHistory(C, true);
  stick(C);
}

// ----- right panel (live progress) -----
let GIT_CACHE = { t: 0, files: {} };
async function gitFiles() { if (Date.now() - GIT_CACHE.t > 6000) { GIT_CACHE.t = Date.now(); try { GIT_CACHE.files = (await api('/api/git/status')).files; renderRightSoon(); } catch (e) { /* no git */ } } return GIT_CACHE.files; }
let rrT = 0; function renderRightSoon() { clearTimeout(rrT); rrT = setTimeout(renderRight, 150); }
// The Progress panel is built by wbRail (dash_js_workbench.py): Environment, Context, Usage, Plan, Connectors and Changes.
function drawRail(C) {
  if (UI.chat !== C) return;
  wbRail(C);
  gitFiles();
  if (!C.subLoaded && C.sid) { C.subLoaded = true; refreshSubs(C); }
}

// ----- composer -----
function buildComposer(C) {
  const ta = h('textarea', { id: 'msg', rows: 1, placeholder: 'Message Axon   (/ commands, @ files, ! shell, drop files here)', 'aria-label': 'Message' });
  const chips = h('div', { class: 'chips' });
  const pop = h('div', { class: 'pop', hidden: true });
  const send = h('button', { class: 'btn primary', id: 'send-btn', type: 'button' }, 'Send');
  const queueBtn = h('button', { class: 'btn', type: 'button', hidden: true, title: 'Wait until the current turn ends, then send' }, 'Send after this turn');
  const sendNow = h('button', { class: 'btn', type: 'button', hidden: true, title: 'Stop the current turn and send this message now' }, 'Stop and send now');
  const status = h('span', { class: 'muted', id: 'cstatus' }, 'Enter sends · Shift+Enter for a new line');
  const banner = h('div', { class: 'notice warn provider-banner', hidden: true });
  const heldEl = h('div', { class: 'heldbox', hidden: true });
  const root = h('div', { class: 'composer' }, banner, heldEl, chips, h('div', { class: 'box' }, pop, ta), status, h('div', { class: 'cbar' }, wbPlus(C, ta), wbFooter(C), h('span', { class: 'sp' }), C.head, queueBtn, sendNow, wbMic(C, ta), send));
  if (typeof axGhost === 'function') axGhost(C, ta);
  C.heldEl = heldEl; C.ta = ta; C.pop = pop; C.chipsEl = chips; C.sendBtn = send;
  const checkReady = async () => {
    try {
      const r = await api('/api/provider/ready?session=' + encodeURIComponent(C.sid)); banner.hidden = r.ready;
      if (!r.ready) banner.replaceChildren(h('b', null, 'Chat is not set up yet. '), r.reason + ' ', h('a', { class: 'btn primary', href: '#/settings/providers' }, 'Open Providers'));
    } catch (e) { /* the status pill shows connection problems */ }
  };
  C.checkReady = checkReady; checkReady();
  const rdy = setInterval(() => { if (!C.closed && !document.hidden) checkReady(); }, 6000); onLeave(() => clearInterval(rdy));
  const histKey = 'axon-prompts';
  try { C.hist = JSON.parse(lsGet(histKey) || '[]'); } catch (e) { C.hist = []; }
  const auto = () => { ta.style.height = 'auto'; ta.style.height = Math.min(260, ta.scrollHeight + 2) + 'px'; };
  const redrawChips = () => {
    chips.replaceChildren();
    for (const [chip, meta] of Object.entries(C.chips)) {
      if (!ta.value.includes(chip)) { delete C.chips[chip]; continue; }
      chips.append(axChipEl(chip, meta, () => { ta.value = ta.value.replace(chip, '').replace(/ {2,}/g, ' '); delete C.chips[chip]; redrawChips(); }));
    }
  };
  const addChip = (meta) => { const sep = ta.value && !/\s$/.test(ta.value) ? ' ' : ''; ta.value += sep + meta.chip + ' '; C.chips[meta.chip] = meta; redrawChips(); auto(); ta.focus(); if (meta.secrets && meta.secrets.length) toast((meta.name || meta.chip) + ' looks like it holds a secret (' + meta.secrets.slice(0, 3).join(', ') + '). It will be sent to the model unless you remove it with the x on its chip.'); };
  async function upload(file) {
    const tag = h('span', { class: 'chip' }, h('span', { class: 'spin' }), file.name); chips.append(tag);
    try {
      if (file.size > 64 * 1024 * 1024) throw new Error(file.name + ' is over 64 MB; reference it by path instead');
      const r = await fetch('/api/attach?name=' + encodeURIComponent(file.name || 'pasted') + '&session=' + encodeURIComponent(C.sid), { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || 'upload failed');
      tag.remove(); addChip(j);
    } catch (e) { tag.remove(); toast(e.message); }
  }
  async function pasteText(text) {
    const tag = h('span', { class: 'chip txt' }, h('span', { class: 'spin' }), 'saving pasted text (' + text.split('\n').length.toLocaleString() + ' lines)'); chips.append(tag);
    try { const j = await api('/api/attach/text', { text }); tag.remove(); addChip(j); toast('Pasted text kept as an attachment: ~' + tok(j.tokens) + ' tokens' + (j.limit_tokens && j.tokens > j.limit_tokens ? '. Axon will send its start and end and read the rest from the saved file.' : '.')); }
    catch (e) { tag.remove(); toast(e.message); }
  }
  ta.addEventListener('paste', (e) => {
    const cd = e.clipboardData; if (!cd) return;
    const files = axClipboardFiles(cd);
    if (files.length) { e.preventDefault(); files.forEach(C.uploadFile); return; }
    const text = cd.getData('text'); if (text && (text.length > 20000 || text.split('\n').length > 200)) { e.preventDefault(); pasteText(text); }
  });
  axComposerDrop(C, { addChip, upload, chips, ta });     // dropping is handled once for the whole chat view (dash_js_files.py)

  // popups: / commands and @ files
  let items = [], sel = 0, mode = '';
  const closePop = () => { pop.hidden = true; items = []; mode = ''; };
  const showPop = (list, kind) => { items = list; sel = 0; mode = kind; pop.replaceChildren(...cmdPopNodes(list, accept)); pop.hidden = !list.length; };
  const accept = (i) => {
    const it = items[i]; if (!it) return;
    const v = ta.value, pos = ta.selectionStart;
    if (mode === 'prompt') { closePop(); axPromptUse(C, ta, it.prompt); return; }
    if (mode === 'cmd') { ta.value = it.value + ' ' + v.slice(pos).replace(/^\S*/, '').trimStart(); }
    else { const before = v.slice(0, pos).replace(/@[^\s]*$/, '@' + it.value + ' '); ta.value = before + v.slice(pos); }
    closePop(); auto(); ta.focus();
  };
  async function suggest() {
    const v = ta.value, pos = ta.selectionStart;
    C.shellMode = v.startsWith('!'); ta.classList.toggle('shellmode', C.shellMode); status.classList.toggle('on', C.shellMode);
    if (C.shellMode) { status.textContent = 'Shell mode: runs the command in the project folder, then Axon sees the output.'; closePop(); return; }
    status.textContent = 'Enter sends · Shift+Enter for a new line';
    const first = v.split('\n')[0];
    if (/^\/\S*$/.test(first) && pos <= first.length) {
      if (!C.cmds) { try { C.cmds = (await api('/api/commands?session=' + encodeURIComponent(C.sid))).commands; } catch (e) { C.cmds = []; } }
      if (ta.value.split('\n')[0] !== first) return;                     // the box changed (or was sent) while the list loaded
      showPop(cmdSuggest(C.cmds, first, C.hist), 'cmd'); return;       // fuzzy, grouped, recent first (dash_js_cmdcards.py)
    }
    if (/^#[^\s#]*$/.test(first) && pos <= first.length) {            // # lists the saved prompts (dash_js_library.py)
      let l = []; try { l = await axPromptItems(C, first.slice(1)); } catch (e) { /* no list */ }
      if (ta.value.split('\n')[0] !== first) return;
      showPop(l, 'prompt'); return;
    }
    const m = v.slice(0, pos).match(/@([^\s@]*)$/);
    if (m) {
      const q = m[1]; clearTimeout(C.ft);
      C.ft = setTimeout(async () => { try { const d = await api('/api/files/search?q=' + encodeURIComponent(q)); showPop(d.files.slice(0, 12).map(f => ({ label: f.split('/').pop(), value: f, hint: f })), 'file'); } catch (e) { closePop(); } }, 120);
      return;
    }
    closePop();
  }
  ta.addEventListener('input', () => { auto(); suggest(); });
  ta.addEventListener('keydown', (e) => {
    if (!pop.hidden && items.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; cmdPopSelect(pop, sel); return; }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey && (mode === 'file' || mode === 'prompt'))) { e.preventDefault(); accept(sel); return; }
      if (e.key === 'Enter' && !e.shiftKey && mode === 'cmd' && ta.value.trim() !== items[sel].value) { e.preventDefault(); accept(sel); return; }
      if (e.key === 'Escape') { closePop(); return; }
    }
    if (e.key === 'ArrowUp' && !ta.value && C.hist.length) { e.preventDefault(); C.hpos = Math.min(C.hist.length - 1, C.hpos + 1); ta.value = C.hist[C.hist.length - 1 - C.hpos]; auto(); return; }
    if (e.key === 'ArrowDown' && C.hpos >= 0) { e.preventDefault(); C.hpos--; ta.value = C.hpos >= 0 ? C.hist[C.hist.length - 1 - C.hpos] : ''; auto(); return; }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(true); }
  });
  // A file typed as @name (not picked as a chip) is sent like any other: warn before it goes if it looks like it holds a secret.
  async function typedFilesAreSafe(text) {
    const typed = [...new Set([...text.matchAll(/(?:^|\s)@([^\s@]+)/g)].map(m => m[1]).filter(n => !C.chips['@' + n]))].slice(0, 8);
    if (!typed.length) return true;
    let items; try { items = (await api('/api/attach/paths', { paths: typed })).items; } catch (e) { return true; }
    const risky = items.filter(i => i.secrets && i.secrets.length);
    if (!risky.length) return true;
    return wbConfirm('Send a file that may hold a secret?', [
      ...risky.map(i => h('p', null, h('b', null, i.name), ' looks like it holds a secret (' + i.secrets.slice(0, 3).join(', ') + ').')),
      'It will be sent to the model with your message. Cancel to edit the message first.'], 'Send anyway', true);
  }
  async function go(steer) {
    const text = ta.value.trim(); if (!text || C.locked) return;
    if (axPromptSlash(C, ta, text)) return;                  // a bare /prompt opens the saved prompts
    C.hist.push(text); C.hist = C.hist.slice(-80); lsSet(histKey, JSON.stringify(C.hist)); C.hpos = -1;
    if (text.startsWith('!') && text.length > 1) { ta.value = ''; auto(); closePop(); return shellFromChat(C, text.slice(1).trim()); }
    if (!(await typedFilesAreSafe(text))) { C.hist.pop(); return; }
    ta.value = ''; auto(); closePop(); redrawChips();
    C.atBottom = true;                       // sending is a reason to look at the newest reply
    send.disabled = true;
    try {
      const r = await api('/api/chat/send', { session: C.sid, text, steer, cards: true });
      if (r.session && !C.sid) { C.sid = r.session; }
      if (r.queued) { (C.qtext = C.qtext || {})[r.id] = text; C.lastQueued = r.id; }
      if (r.command) { commandResultCard(C, text, r); if (r.state) { C.st = r.state; C.runtime = true; if (!C.es) connectEvents(C, r.state); drawHead(C); drawRail(C); } }
      else {
        if (!C.runtime) { C.runtime = true; await refreshState(C); if (!C.es) connectEvents(C, { busy: true, seq: 0 }); }
        if (r.session && (!location.hash || location.hash === '#/chat' || location.hash === '#/chat/')) { history.replaceState(null, '', '#/chat/' + encodeURIComponent(r.session)); if (C.tab) C.tab.hash = '#/chat/' + encodeURIComponent(r.session); savePrefs(); }
      }
    } catch (e) {
      ta.value = text; auto();
      if (/another terminal|read-only/i.test(e.message)) { C.meta = Object.assign(C.meta || {}, { held_by: { pid: '?', owner: 'terminal', started: Date.now() / 1000 } }); lockNote(C); }
      else if (e.status === 412) { checkReady(); C.feed.append(errorBox(e.message)); }
      else if (e.status !== 401) C.feed.append(errorBox('Could not send this message: ' + e.message));
      stick(C); toast(e.status === 412 ? 'Chat is not set up: open Providers' : e.message);
    } finally { send.disabled = false; ta.focus(); }
  }
  send.addEventListener('click', () => go(true));
  queueBtn.addEventListener('click', () => go(false));
  sendNow.addEventListener('click', async () => { C.lastQueued = null; await go(false); await stopTurn(C, { keep: C.lastQueued }); });
  // While Axon works the box offers Stop; once there is text, also the three ways of sending it.
  const sync = setInterval(() => { const busy = !!(C.st && C.st.busy), typed = !!ta.value.trim(); queueBtn.hidden = !(busy && typed); sendNow.hidden = !(busy && typed); send.hidden = busy && !typed; send.textContent = busy ? 'Add to this turn' : 'Send'; }, 150);
  onLeave(() => clearInterval(sync));
  setTimeout(() => { const pre = lsGet('axon-prefill'); if (pre) { lsSet('axon-prefill', ''); ta.value = pre; auto(); } ta.focus(); }, 50);
  C.upload = upload;
  return h('div', { class: 'composer-wrap' }, root, (typeof cfForecastLine === 'function' ? cfForecastLine(C, ta) : null));
}
function commandResultCard(C, line, r) {
  if (r.open) { location.hash = r.open; return; }
  if (cmdCardShow(C, line, r)) return;                // neat card (dash_js_cmdcards.py); falls through for pickers that need the terminal
  const card = h('div', { class: 'cmd-card' }, h('div', { class: 'steprow' }, h('b', null, line), h('span', null, r.done ? (r.ms || 0) + ' ms' : 'still running...'), r.ok === false ? h('span', { class: 'bad' }, 'failed') : null));
  if (r.message) card.append(notice(r.message, r.ok === false ? 'warn' : ''));
  const out = h('div', { class: 'cmd-out', html: r.html || '' }); if (r.html) { card.append(out); termHint(card, r); }
  C.feed.querySelector('.empty-chat')?.remove(); C.feed.append(card); decorate(card); stick(C);
  if (!r.done) { const poll = setInterval(async () => { try { const j = await api('/api/command/job?id=' + r.id); if (j.done) { clearInterval(poll); out.innerHTML = j.html || ''; card.append(out); decorate(card); } } catch (e) { clearInterval(poll); } }, 1000); onLeave(() => clearInterval(poll)); }
}
async function shellFromChat(C, cmd) {
  const stop = h('button', { class: 'mini danger', type: 'button', title: 'Stop this command' }, 'Stop');
  const card = h('div', { class: 'cmd-card' }, h('div', { class: 'steprow' }, h('b', { class: 'sh-cmd' }, '$ ' + cmd), h('span', { class: 'sh-state' }, 'running...'), stop));
  const out = h('pre', { class: 'cmd-out nolink', style: 'max-height:40vh' }); card.append(out);
  C.feed.querySelector('.empty-chat')?.remove(); C.feed.append(card); stick(C);
  let job; try { job = await api('/api/shell/run', { command: cmd }); } catch (e) { out.textContent = e.message; stop.remove(); return; }
  stop.addEventListener('click', () => { stop.disabled = true; api('/api/shell/kill', { id: job.id }).catch(e => toast(e.message)); });
  const plain = []; let q = [], timer = 0, shown = 0, trimmed = false;
  // Batched like the Shell page: one layout per 60 ms, and only the newest 300 KB is kept on screen.
  const flush = () => {
    timer = 0; const batch = q; q = []; if (!batch.length) return;
    for (const ev of batch) { const s = document.createElement('span'); s.innerHTML = ev.html; out.append(s); const t = s.textContent; plain.push(t); shown += t.length; }
    while (shown > 300000 && out.firstChild && out.childNodes.length > 1) { shown -= (out.firstChild.textContent || '').length; out.removeChild(out.firstChild); trimmed = true; }
    if (trimmed && !out.dataset.cut) { out.dataset.cut = '1'; card.querySelector('.sh-state').textContent += ' (older output trimmed here; Axon still gets the end of it)'; }
    stick(C);
  };
  const es = new EventSource('/api/shell/stream?id=' + job.id + '&since=0');
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === 'out') { q.push(ev); if (!timer) timer = setTimeout(flush, 60); }
    else if (ev.type === 'refused') { out.append(h('div', { class: 'refused' }, ev.message)); stop.remove(); }
    else if (ev.type === 'exit') { es.close(); clearTimeout(timer); flush(); stop.remove(); const st = card.querySelector('.sh-state'); st.textContent = ev.refused ? 'not run' : (ev.killed ? 'stopped' : 'exit ' + ev.code) + ' · ' + dur(ev.duration || 0);
      if (!ev.refused) { let tail = plain.join('').slice(-20000); const text = 'I ran `' + cmd + '` (' + (ev.killed ? 'stopped by the user' : 'exit code ' + ev.code) + '). Output:\n```\n' + tail + '\n```'; api('/api/chat/send', { session: C.sid, text, steer: true }).then(r => { if (!C.runtime) refreshState(C); }).catch(() => { }); } }
  };
  es.onerror = () => es.close();
}
"""
