"""Client script, part 3: per-tool result cards, desktop notifications, the suggested-next-prompt ghost text.

The card model (``axCardModel``) is pure data in, plain data out, so node can test it without a DOM. All tool text is
shown through text nodes (``h`` children / ``textContent``), never innerHTML, so nothing a tool prints is ever markup.
"""

JS_CARDS = r"""
// ---------- tool cards, notifications, ghost suggestion ----------
const AX_SENT = {
  Read: (i) => 'Read ' + axPath(i), Write: (i) => 'Wrote ' + axPath(i), Edit: (i) => 'Edited ' + axPath(i), MultiEdit: (i) => 'Made several edits to ' + axPath(i),
  NotebookEdit: (i) => 'Changed a notebook cell in ' + axPath(i, 'notebook_path'), Patch: (i) => 'Applied a patch', Bash: (i) => 'Ran ' + axQ(i.command),
  BashOutput: () => 'Read output from a background shell', KillShell: () => 'Stopped a background shell', Process: (i) => 'Managed a process' + (i.action ? ' (' + axQ(i.action) + ')' : ''),
  Monitor: () => 'Watched a background command', Grep: (i) => 'Searched file contents for ' + axQ(i.pattern), Glob: (i) => 'Listed files matching ' + axQ(i.pattern),
  Ls: (i) => 'Listed ' + axPath(i), CodeSearch: (i) => 'Searched the code graph for ' + axQ(i.query || i.name), GoToDefinition: (i) => 'Looked up a definition' + (i.symbol ? ' of ' + axQ(i.symbol) : ''),
  FindReferences: (i) => 'Looked up references' + (i.symbol ? ' to ' + axQ(i.symbol) : ''), SemanticSearch: (i) => 'Searched by meaning for ' + axQ(i.query), LSP: (i) => 'Asked the language server' + (i.operation ? ' (' + axQ(i.operation) + ')' : ''),
  Git: (i) => 'Ran git' + (i.subcommand || i.command ? ' ' + axQ(i.subcommand || i.command) : ''), WebFetch: (i) => 'Fetched ' + axQ(i.url), WebSearch: (i) => 'Searched the web for ' + axQ(i.query),
  Task: (i) => 'Ran a subagent' + (i.description ? ': ' + i.description : ''), SendMessage: () => 'Sent a message to a subagent', TodoWrite: () => 'Updated the todo list',
  ExitPlanMode: () => 'Presented the plan for approval', EnterPlanMode: () => 'Entered plan mode', AskUserQuestion: () => 'Asked you a question', Skill: (i) => 'Loaded a skill' + (i.skill || i.name ? ' ' + axQ(i.skill || i.name) : ''),
  ToolSearch: (i) => 'Looked up tools' + (i.query ? ' for ' + axQ(i.query) : ''), CheckpointList: () => 'Listed checkpoints', CheckpointUndo: () => 'Restored a checkpoint', Doctor: () => 'Checked the setup',
};
function axQ(v) { const s = String(v == null ? '' : v).replace(/\s+/g, ' ').trim(); return s ? '“' + (s.length > 70 ? s.slice(0, 70) + '…' : s) + '”' : 'something'; }
function axPath(i, k) { const p = (i && (i[k || 'file_path'] || i.path || i.file_path || i.notebook_path)) || ''; return p ? String(p).split(/[\\/]/).slice(-3).join('/') : 'the project folder'; }
function axPlural(n, w) { return n + ' ' + (n === 1 ? w : /(ch|sh|s|x)$/.test(w) ? w + 'es' : w + 's'); }
function axLines(s) { return s ? String(s).replace(/\n$/, '').split('\n').length : 0; }
function axCount(oldS, newS) {   // added and removed lines between two texts, by multiset difference (cheap, no LCS)
  const A = {}; for (const l of String(oldS || '').split('\n')) A[l] = (A[l] || 0) + 1;
  let add = 0; for (const l of String(newS || '').split('\n')) { if (A[l] > 0) A[l]--; else add++; }
  let rem = 0; for (const k in A) rem += A[k];
  return [add, rem];
}
function axExit(text, isError) {
  let m = /Command exited with non-zero code (-?\d+)/.exec(text); if (m) return +m[1];
  m = /exit code (-?\d+)/i.exec(text); if (m) return +m[1];
  return isError ? 1 : 0;
}
function axTests(text) {   // pytest, unittest, jest/vitest, go test, cargo test: counts or null
  let m, r = { pass: 0, fail: 0, skip: 0 }, hit = false;
  const tally = (re, key) => { for (const x of text.matchAll(re)) { r[key] += +x[1]; hit = true; } };
  if (/(\d+) (passed|failed|error|errors|skipped)\b/.test(text) && /={3,}|\bin \d+(\.\d+)?s/.test(text)) { tally(/(\d+) passed/g, 'pass'); tally(/(\d+) (?:failed|errors?)\b/g, 'fail'); tally(/(\d+) skipped/g, 'skip'); }
  else if ((m = /Tests:\s+(.*)/.exec(text))) { const t = m[1]; hit = true; r.fail = +((/(\d+) failed/.exec(t) || [0, 0])[1]); r.pass = +((/(\d+) passed/.exec(t) || [0, 0])[1]); r.skip = +((/(\d+) (?:skipped|todo)/.exec(t) || [0, 0])[1]); }
  else if ((m = /^Ran (\d+) tests? in/m.exec(text))) { hit = true; const tot = +m[1]; const f = /FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?/.exec(text); r.fail = f ? (+f[1] || 0) + (+f[2] || 0) : 0; const s = /skipped=(\d+)/.exec(text); r.skip = s ? +s[1] : 0; r.pass = Math.max(0, tot - r.fail - r.skip); }
  else if (/^test result: /m.test(text)) { hit = true; tally(/(\d+) passed/g, 'pass'); tally(/(\d+) failed/g, 'fail'); tally(/(\d+) ignored/g, 'skip'); }
  else if (/^(ok|FAIL|---)\s/m.test(text) && /^--- (PASS|FAIL)|^ok\s|^FAIL\s/m.test(text)) { hit = true; r.pass = (text.match(/^--- PASS/gm) || []).length || (text.match(/^ok\s/gm) || []).length; r.fail = (text.match(/^--- FAIL/gm) || []).length || (text.match(/^FAIL\s/gm) || []).length; }
  return hit && (r.pass + r.fail + r.skip) > 0 ? r : null;
}
function axGrep(text) {
  const hits = []; const files = new Set(); let none = /^No matches/.test(text.trim()) || !text.trim();
  for (const ln of text.split('\n')) {
    if (!ln || /^(Found |\[|--$)/.test(ln)) continue;
    const m = /^(.+?):(\d+)[:-](.*)$/.exec(ln);
    if (m) { hits.push({ file: m[1], line: +m[2], text: m[3] }); files.add(m[1]); } else if (!none) { hits.push({ file: ln.trim(), line: 0, text: '' }); files.add(ln.trim()); }
  }
  return { hits, files: files.size, none: none || !hits.length };
}
function axFirstLine(s) { return String(s || '').split('\n').find(l => l.trim()) || ''; }
// -> { sentence, kind, chip, data }
function axCardModel(name, input, content, isError, durS) {
  const i = input && typeof input === 'object' ? input : {}; const text = String(content == null ? '' : content);
  const fn = AX_SENT[name]; let kind = 'generic', chip = '', data = null, sentence; const chips = [];
  if (fn) { try { sentence = fn(i); } catch (e) { sentence = ''; } }
  if (!sentence) {
    const mm = /^mcp__(.+?)__(.+)$/.exec(name || '');
    sentence = mm ? 'Called ' + mm[2].replace(/_/g, ' ') + ' on the ' + mm[1] + ' server' : 'Used ' + (name || 'a tool');
  }
  if (name === 'Bash' || name === 'BashOutput') {
    const code = axExit(text, isError); const t = axTests(text); data = { code, dur: durS == null ? null : durS };
    if (durS == null) { const m = /in (\d+(?:\.\d+)?)s\b/.exec(text); if (m && /completed with exit code/.test(text)) data.dur = +m[1]; }
    kind = 'bash';
    if (t) { kind = 'tests'; data.tests = t; sentence = (t.fail ? 'Tests failed: ' : 'Tests passed: ') + t.pass + ' passed' + (t.fail ? ', ' + t.fail + ' failed' : '') + (t.skip ? ', ' + t.skip + ' skipped' : ''); chip = t.pass + ' ✓ ' + t.fail + ' ✗'; chips.push({ t: t.pass + ' passed', c: 'add' }); if (t.fail) chips.push({ t: t.fail + ' failed', c: 'del' }); if (t.skip) chips.push({ t: t.skip + ' skipped' }); }
    else { sentence += code ? ' and it failed with exit code ' + code : ' and it succeeded'; chip = 'exit ' + code; if (code) chips.push({ t: 'exit ' + code, c: 'del' }); }
    if (data.dur != null) chip += ' · ' + (data.dur < 10 ? data.dur.toFixed(1) : Math.round(data.dur)) + 's';
  } else if (name === 'Edit' || name === 'Write' || name === 'MultiEdit' || name === 'NotebookEdit') {
    let a = 0, r = 0, parts = [];
    if (name === 'Edit') { [a, r] = axCount(i.old_string, i.new_string); parts.push({ old: i.old_string || '', nw: i.new_string || '' }); }
    else if (name === 'MultiEdit') { for (const e of (i.edits || [])) { const [x, y] = axCount(e.old_string, e.new_string); a += x; r += y; parts.push({ old: e.old_string || '', nw: e.new_string || '' }); } }
    else if (name === 'Write') { a = axLines(i.content); parts.push({ old: '', nw: i.content || '' }); }
    else { a = axLines(i.new_source); r = 0; parts.push({ old: '', nw: i.new_source || '' }); }
    kind = 'diff'; data = { added: a, removed: r, parts };
    sentence += ': ' + axPlural(a, 'line') + ' added, ' + axPlural(r, 'line') + ' removed'; chip = '+' + a + ' −' + r; chips.push({ t: '+' + a, c: 'add' }); if (r) chips.push({ t: '−' + r, c: 'del' });
  } else if (name === 'Patch') {
    const p = String(i.patch || i.diff || text); const a = (p.match(/^\+(?!\+\+)/gm) || []).length, r = (p.match(/^-(?!--)/gm) || []).length;
    kind = 'difftext'; data = { added: a, removed: r }; sentence += ' with ' + a + ' added and ' + r + ' removed'; chip = '+' + a + ' −' + r; chips.push({ t: '+' + a, c: 'add' }, { t: '−' + r, c: 'del' });
  } else if (name === 'Grep') {
    const g = axGrep(text); kind = 'grep'; data = { hits: g.hits, pattern: i.pattern || '', files: g.files };
    sentence += g.none ? ': no matches' : ': ' + axPlural(g.hits.length, 'match') + (g.files > 1 || g.hits[0] && g.hits[0].line ? ' in ' + axPlural(g.files, 'file') : '');
    chip = g.none ? '0' : String(g.hits.length); chips.push({ t: g.none ? 'no matches' : axPlural(g.hits.length, 'match') });
  } else if (name === 'Glob' || name === 'Ls') {
    const names = text.split('\n').map(l => l.replace(/^[│├└─\s]+/, '').replace(/\s+\(\d[\d.,]* ?[KMGB]*\)$/, '').trim()).filter(l => l && !/^(\.\.\.|\(|\[|Found |No files)/.test(l));
    kind = 'files'; data = { names }; sentence += names.length ? ': ' + (names.length === 1 ? '1 entry' : names.length + ' entries') : ': nothing found'; chip = String(names.length); if (names.length) chips.push({ t: axPlural(names.length, 'entry').replace('entrys', 'entries') });
  } else if (name === 'TodoWrite') {
    const todos = Array.isArray(i.todos) ? i.todos : []; const done = todos.filter(t => t.status === 'completed').length;
    kind = 'todos'; data = { todos }; sentence = 'Updated the todo list: ' + done + ' of ' + todos.length + ' done'; chip = done + '/' + todos.length; chips.push({ t: done + '/' + todos.length + ' done' });
  } else if (name === 'Read') {
    const n = text.split('\n').filter(l => /^\s*\d+[\t→ |]/.test(l)).length; if (n) { sentence += ' (' + axPlural(n, 'line') + ')'; chip = String(n); chips.push({ t: axPlural(n, 'line') }); }
  }
  if (isError && kind !== 'bash' && kind !== 'tests') sentence = (name || 'Tool') + ' failed: ' + axFirstLine(text).slice(0, 120);
  else if (isError) sentence = sentence.replace(/ and it succeeded$/, ' and it failed');
  return { sentence, kind, chip, chips, data };
}
// ----- rendering (text nodes only) -----
function axBar(t) {
  const tot = t.pass + t.fail + t.skip || 1; const seg = (n, c, l) => n ? h('i', { class: c, style: 'width:' + (100 * n / tot) + '%', title: n + ' ' + l }) : null;
  return h('div', { class: 'cview' }, h('div', { class: 'tbar' }, seg(t.pass, 'p', 'passed'), seg(t.fail, 'f', 'failed'), seg(t.skip, 's', 'skipped')),
    h('div', { class: 'muted' }, t.pass + ' passed · ' + t.fail + ' failed' + (t.skip ? ' · ' + t.skip + ' skipped' : '')));
}
function axMark(parent, text, pattern) {   // highlight pattern matches inside plain text, without markup
  let re = null; try { re = pattern ? new RegExp(pattern, 'gi') : null; } catch (e) { re = null; }
  if (!re) { parent.append(document.createTextNode(text)); return; }
  let last = 0, m, n = 0;
  while ((m = re.exec(text)) && n++ < 50) { if (!m[0]) { re.lastIndex++; continue; } parent.append(document.createTextNode(text.slice(last, m.index)), h('mark', null, m[0])); last = m.index + m[0].length; }
  parent.append(document.createTextNode(text.slice(last)));
}
function axView(model) {
  const d = model.data;
  if (model.kind === 'tests') return axBar(d.tests);
  if (model.kind === 'diff') {
    const box = h('div', { class: 'cview' }, h('div', { class: 'muted' }, '+' + d.added + ' added, −' + d.removed + ' removed'));
    for (const p of d.parts.slice(0, 20)) box.append(diffEl(lineDiff(p.old, p.nw)));
    return box;
  }
  if (model.kind === 'grep') {
    const box = h('div', { class: 'cview gview' });
    for (const x of d.hits.slice(0, 200)) { const row = h('div', { class: 'ghit' }, h('span', { class: 'gl' }, x.file + (x.line ? ':' + x.line : ''))); if (x.text) { const t = h('span', { class: 'gt' }); axMark(t, x.text.trim(), d.pattern); row.append(' ', t); } box.append(row); }
    if (d.hits.length > 200) box.append(h('div', { class: 'muted' }, '+' + (d.hits.length - 200) + ' more matches in the full output'));
    return box;
  }
  if (model.kind === 'files') { const box = h('ul', { class: 'cview flist' }); for (const n of d.names.slice(0, 300)) box.append(h('li', null, n)); if (d.names.length > 300) box.append(h('li', { class: 'muted' }, '+' + (d.names.length - 300) + ' more')); return box; }
  if (model.kind === 'todos') {
    const box = h('div', { class: 'cview' });
    for (const t of d.todos) box.append(h('div', { class: 'todo ' + (t.status === 'completed' ? 'done' : t.status === 'in_progress' ? 'now' : '') }, h('span', null, t.status === 'completed' ? '☑' : t.status === 'in_progress' ? '▶' : '☐'), h('span', null, String(t.content || ''))));
    return box;
  }
  if (model.kind === 'bash') return h('div', { class: 'cview muted' }, 'Exit code ' + d.code);
  return null;
}
// The sentence of a row while its tool is still running (no output yet to describe).
function axRunning(name, input) {
  const i = input && typeof input === 'object' ? input : {}; const fn = AX_SENT[name]; let s = '';
  if (fn) { try { s = fn(i); } catch (e) { s = ''; } }
  if (!s) { const mm = /^mcp__(.+?)__(.+)$/.exec(name || ''); s = mm ? 'Called ' + mm[2].replace(/_/g, ' ') + ' on the ' + mm[1] + ' server' : 'Used ' + (name || 'a tool'); }
  return s.replace(/^Ran /, 'Running ').replace(/^Read /, 'Reading ').replace(/^Wrote /, 'Writing ').replace(/^Edited /, 'Editing ').replace(/^Searched /, 'Searching ').replace(/^Listed /, 'Listing ').replace(/^Fetched /, 'Fetching ');
}
// One duration format for the whole turn view. Nothing known -> nothing shown; under 0.1 s -> "<0.1s".
function axDur(s) {
  if (s == null || s === '' || !isFinite(s) || s < 0) return '';
  if (s < 0.1) return '<0.1s';
  if (s < 10) return s.toFixed(1) + 's';
  s = Math.round(s);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return m + 'm ' + (r < 10 ? '0' : '') + r + 's';
  return Math.floor(m / 60) + 'h ' + (m % 60 < 10 ? '0' : '') + (m % 60) + 'm';
}
// Called from toolDone once the output is drawn: the row gets a plain sentence (or the reason it failed) and result chips,
// and the Output pane a result view above the raw output. State is the marker's job, so no "ok" or "error" word.
function axCard(card, ev, text) {
  const nm = (ev && ev.name) || card.axName || '';
  const failed = !!(ev && ev.is_error);
  const model = axCardModel(nm, card.axIn || {}, text, failed, null);
  card.axModel = model;
  let line = model.sentence;
  if (failed) line = axFirstLine(text).trim().slice(0, 240) || line;
  else line = line.replace(/ and it succeeded$/, '');
  const ta = card.querySelector('.ta'); if (ta) { ta.textContent = line; ta.title = line; }
  const ch = card.querySelector('.chips'); if (ch) { ch.replaceChildren(); if (!failed || model.kind === 'tests') for (const c of model.chips || []) ch.append(h('span', { class: 'chip2' + (c.c ? ' ' + c.c : '') }, c.t)); }
  const view = axView(model); const o = card.querySelector('.p-out');
  if (view && o) {
    const raw = h('details', { class: 'rawout' }, h('summary', null, 'Full output'));
    while (o.firstChild) raw.append(o.firstChild);
    o.append(view, raw);
  }
  card.classList.add('c-' + model.kind);
}

// ----- desktop notifications (opt in, only while the tab is hidden) -----
function axNotifyOn() { return typeof Notification !== 'undefined' && Notification.permission === 'granted' && lsGet('axon-notify') === '1'; }
function axNotify(title, body, tag) {
  if (!axNotifyOn() || !document.hidden) return;
  try { const n = new Notification(title, { body: String(body || '').slice(0, 160), tag }); n.onclick = () => { window.focus(); n.close(); }; } catch (e) { /* blocked */ }
}
function axNotifyPill() {
  const supported = typeof Notification !== 'undefined';
  const b = h('button', { class: 'mini', type: 'button', 'aria-pressed': 'false', title: 'Show a desktop notification when a turn finishes or Axon needs permission while this tab is in the background' });
  const draw = () => { const on = axNotifyOn(); b.setAttribute('aria-pressed', String(on)); b.textContent = !supported ? 'Notifications unavailable' : on ? 'Notifications on' : Notification.permission === 'denied' ? 'Notifications blocked' : 'Notify me'; b.disabled = !supported || (Notification.permission === 'denied'); };
  b.addEventListener('click', async () => {
    if (axNotifyOn()) { lsSet('axon-notify', '0'); draw(); toast('Notifications off'); return; }
    try { const p = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission(); if (p === 'granted') { lsSet('axon-notify', '1'); toast('You will be told when a turn finishes or Axon needs permission while this tab is hidden'); } else toast('The browser did not allow notifications'); } catch (e) { toast('Notifications are not available here'); }
    draw();
  });
  draw(); return h('span', { class: 'pill' }, b);
}
// One hook for live events (called first thing in applyEvent).
function axOnEvent(C, ev) {
  if (C.replaying) return;
  if (ev.type === 'turn_end') axNotify('Axon finished', turnWord(ev.stop_reason) + (ev.cost_usd != null ? ' · ' + usd(ev.cost_usd) : ''), 'axon-turn');
  else if (ev.type === 'approval_request') axNotify('Axon needs permission', 'It wants to use ' + ev.tool, 'axon-approval-' + ev.id);
  else if (ev.type === 'question_request') axNotify('Axon has a question', ev.question, 'axon-q-' + ev.id);
  else if (ev.type === 'error') axNotify('Axon hit an error', ev.message, 'axon-turn');
  else if (ev.type === 'user' || ev.type === 'turn_start') { C.ghostGone = true; }
  if (ev.type === 'user' || ev.type === 'turn_start' || ev.type === 'idle' || ev.type === 'turn_end') axGhostSoon(C);
}

// ----- ghost text: the suggested next prompt, Tab accepts -----
function axGhostSoon(C) { if (C.ghostRefresh) C.ghostRefresh(); }
function axGhost(C, ta) {
  const box = ta.parentElement; if (!box) return;
  const ghost = h('div', { class: 'ghost', 'aria-hidden': 'true', hidden: true }); box.append(ghost);
  let shown = '', dismissed = '', placeholder = null;
  const hint = h('span', { class: 'ghint' }, 'Tab');
  // The suggestion takes the placeholder's place and its exact position, so the two never overlap.
  const seat = (on) => {
    if (on && placeholder === null) { placeholder = ta.placeholder; ta.placeholder = ''; }
    else if (!on && placeholder !== null) { if (!ta.placeholder) ta.placeholder = placeholder; placeholder = null; }
    if (on && ghost.style) { ghost.style.left = (ta.offsetLeft || 0) + 'px'; ghost.style.top = (ta.offsetTop || 0) + 'px'; ghost.style.width = (ta.offsetWidth || 0) ? ta.offsetWidth + 'px' : ''; }
  };
  const draw = () => {
    const sug = String((C.st && C.st.suggestion) || '').trim(); const busy = !!(C.st && C.st.busy);
    const want = sug && !busy && !ta.value && !C.locked && !C.shellMode && sug !== dismissed && !C.ghostGone ? sug : '';
    if (want === shown && ghost.hidden === !want) return;
    shown = want; ghost.hidden = !want; ghost.replaceChildren(); seat(!!want);
    if (want) ghost.append(document.createTextNode(want), ' ', hint);
  };
  C.ghostRefresh = () => { if (C.st && C.st.suggestion) C.ghostGone = false; draw(); };
  const poll = setInterval(() => { if (C.st && C.st.suggestion && C.st.suggestion !== C.lastSug) { C.lastSug = C.st.suggestion; C.ghostGone = false; dismissed = ''; } draw(); }, 500);
  onLeave(() => clearInterval(poll));
  ta.addEventListener('input', draw);
  ta.addEventListener('keydown', (e) => {
    if (!shown) return;
    if (e.key === 'Tab' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); e.stopImmediatePropagation(); ta.value = shown; ta.dispatchEvent(new Event('input')); ta.setSelectionRange(ta.value.length, ta.value.length); }
    else if (e.key === 'Escape') { dismissed = shown; draw(); }
  }, true);
}
"""
