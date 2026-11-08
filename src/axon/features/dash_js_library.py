"""Client script: saved prompts (the library dialog, the # list in the message box, the fill-in form) and the
checkpoint timeline that sits in the Changes panel.

Everything from the server goes in as text nodes (``h`` children); nothing is put in with innerHTML. The data
comes from /api/library/* (dash_library.py), which reads the same prompt_library and checkpoint_view modules
the terminal's /prompt and /rewind use.
"""
from __future__ import annotations

JS_LIBRARY = r"""
// ---------- saved prompts ----------
const plWhere = (p) => p.scope === 'project' ? 'this project' : p.scope === 'global' ? 'all projects' : 'starter';
const plNames = (p) => p.fields.map(f => f.name).join(', ');
function plModal(label, cls, parent) {
  const scrim = h('div', { class: 'scrim pl-scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': label });
  const modal = h('div', { class: 'modal plmodal ' + (cls || '') }); scrim.append(modal);
  const back = document.activeElement;
  if (parent) parent.hidden = true;                    // a dialog opened from another one hides it until it closes
  const close = () => { scrim.remove(); if (parent) { parent.hidden = false; const f = parent.querySelector('input,button'); if (f) f.focus(); } else if (back && back.focus && document.body.contains(back)) back.focus(); };
  scrim.addEventListener('mousedown', (e) => { if (e.target === scrim) close(); });
  scrim.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
  document.body.append(scrim);
  return { scrim, modal, close };
}
// Put text in the message box: it replaces a lone "#word" or "/prompt", otherwise it goes in at the cursor.
function plPut(ta, text) {
  const v = ta.value;
  if (/^\s*(#\S*|\/prompts?)\s*$/.test(v)) ta.value = text;
  else {
    const s = ta.selectionStart == null ? v.length : ta.selectionStart, e = ta.selectionEnd == null ? s : ta.selectionEnd;
    ta.value = v.slice(0, s) + text + v.slice(e); ta.setSelectionRange(s + text.length, s + text.length);
  }
  ta.dispatchEvent(new Event('input')); ta.focus();
}
async function axPromptItems(C, q) {
  const d = await api('/api/library/prompts?q=' + encodeURIComponent(q || ''));
  return d.prompts.slice(0, 12).map(p => ({ label: p.name, value: p.name, prompt: p, hint: p.description + (p.fields.length ? '  ·  needs ' + plNames(p) : '') }));
}
// "/prompt" typed on its own opens the library instead of printing a list.
function axPromptSlash(C, ta, text) {
  if (!/^\/prompts?$/i.test(text.trim())) return false;
  ta.value = ''; ta.dispatchEvent(new Event('input')); axPromptLibrary(C, ta); return true;
}
function axPromptUse(C, ta, p) {
  if (!p.fields.length) { plPut(ta, p.text); return; }
  plForm(C, ta, p);
}
function plForm(C, ta, p) {
  const m = plModal('Fill in ' + p.name, 'plform');
  const inputs = {}; const err = h('div', { class: 'pl-err', role: 'alert' });
  const rows = p.fields.map((f, i) => {
    const inp = h('input', { type: 'text', id: 'plf' + i, value: f.default || '', autocomplete: 'off', spellcheck: 'false', placeholder: f.kind === 'file' ? 'a file in this project' : '' });
    inputs[f.name] = inp;
    if (f.kind === 'file') {
      const dl = h('datalist', { id: 'plfl' + i }); inp.setAttribute('list', 'plfl' + i); let tmr = 0;
      const look = async () => {
        try { const d = await api('/api/files/search?q=' + encodeURIComponent(inp.value.trim() || '.')); dl.replaceChildren(...d.files.slice(0, 40).map(x => h('option', { value: x }))); } catch (e) { /* the box still takes typed paths */ }
      };
      inp.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(look, 120); }); look();
      return h('div', { class: 'pl-row' }, h('label', { for: 'plf' + i }, f.name), inp, dl);
    }
    return h('div', { class: 'pl-row' }, h('label', { for: 'plf' + i }, f.name, f.default ? h('span', { class: 'muted' }, ' (default: ' + f.default + ')') : null), inp);
  });
  const ready = () => p.fields.every(f => inputs[f.name].value.trim() || f.default);
  const go = h('button', { class: 'btn primary', type: 'button', disabled: !ready() }, 'Fill the message box');
  const sync = () => { go.disabled = !ready(); };
  for (const f of p.fields) inputs[f.name].addEventListener('input', sync);
  const submit = async () => {
    if (!ready()) return; go.disabled = true; err.textContent = '';
    const values = {}; for (const f of p.fields) values[f.name] = inputs[f.name].value;
    try { const d = await api('/api/library/prompt_fill', { name: p.name, values }); m.close(); plPut(ta, d.text); }
    catch (e) { err.textContent = e.message; go.disabled = false; }
  };
  go.addEventListener('click', submit);
  m.modal.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); submit(); } });
  m.modal.append(h('h3', null, p.name), p.description ? h('p', { class: 'muted' }, p.description) : null, h('pre', { class: 'pl-tpl' }, p.text), ...rows, err,
    h('div', { class: 'acts' }, go, h('button', { class: 'btn', type: 'button', onclick: m.close }, 'Cancel')));
  const first = m.modal.querySelector('input'); if (first) first.focus();
}
function plAsk(parent, title, text, buttons) {          // resolves with the chosen button's value, or null on Cancel / Escape
  return new Promise((resolve) => {
    const m = plModal(title, 'plconfirm', parent); let answered = false;
    const done = (v) => { answered = true; m.close(); resolve(v); };
    const cancel = h('button', { class: 'btn', type: 'button', onclick: () => done(null) }, 'Cancel');
    m.modal.append(h('h3', null, title), h('p', { class: 'muted' }, text),
      h('div', { class: 'acts' }, ...buttons.map(b => h('button', { class: 'btn ' + (b.cls || ''), type: 'button', onclick: () => done(b.value) }, b.label)), cancel));
    new MutationObserver((_, o) => { if (!document.body.contains(m.scrim)) { o.disconnect(); if (!answered) resolve(null); } }).observe(document.body, { childList: true });
    cancel.focus();
  });
}
const plConfirm = async (parent, title, text, okLabel) => (await plAsk(parent, title, text, [{ label: okLabel, value: true, cls: 'danger' }])) === true;
function plEdit(p, done, parent) {
  const isNew = !p; const m = plModal(isNew ? 'New prompt' : 'Edit prompt', 'pledit', parent);
  const name = h('input', { type: 'text', id: 'pln', value: p ? p.name : '', maxlength: 40, autocomplete: 'off', spellcheck: 'false' });
  const desc = h('input', { type: 'text', id: 'pld', value: p ? p.description : '', maxlength: 200 });
  const text = h('textarea', { id: 'plt', rows: 8, spellcheck: 'false' }); text.value = p ? p.text : '';
  const scope = h('select', { id: 'pls' }, h('option', { value: 'project' }, 'This project only'), h('option', { value: 'global' }, 'All projects'));
  scope.value = p && p.scope === 'global' ? 'global' : p && p.scope === 'starter' ? 'global' : 'project';
  const found = h('div', { class: 'muted pl-found' }); const err = h('div', { class: 'pl-err', role: 'alert' });
  const showFields = () => { const names = [...new Set([...text.value.matchAll(/\{\{\s*([A-Za-z_][\w-]*)/g)].map(x => x[1]))]; found.textContent = names.length ? 'Fields: ' + names.join(', ') : 'No fields yet. Write {{file}} or {{language}} where a value goes.'; };
  text.addEventListener('input', showFields); showFields();
  const save = h('button', { class: 'btn primary', type: 'button' }, 'Save');
  save.addEventListener('click', async () => {
    err.textContent = ''; save.disabled = true;
    try {
      const r = await api('/api/library/prompt_save', { name: name.value, text: text.value, description: desc.value, scope: scope.value, replace: p ? p.name : '', replace_scope: p ? p.scope : '' });
      m.close(); toast('Saved ' + r.prompt.name); if (done) done(r.prompt);
    } catch (e) { err.textContent = e.message; save.disabled = false; }
  });
  m.modal.append(h('h3', null, isNew ? 'New prompt' : 'Edit prompt'),
    h('div', { class: 'pl-row' }, h('label', { for: 'pln' }, 'Name'), name),
    h('div', { class: 'pl-row' }, h('label', { for: 'pld' }, 'What it is for'), desc),
    h('div', { class: 'pl-row' }, h('label', { for: 'plt' }, 'Prompt'), text, found,
      h('p', { class: 'muted pl-help' }, 'Put {{name}} where a value goes. {{name|default}} gives it a default; {{name:file}} offers the project’s files. Keep keys and passwords out: prompts are saved as plain files.')),
    h('div', { class: 'pl-row' }, h('label', { for: 'pls' }, 'Keep it for'), scope), err,
    h('div', { class: 'acts' }, save, h('button', { class: 'btn', type: 'button', onclick: m.close }, 'Cancel')));
  (isNew ? name : text).focus();
}
function axPromptLibrary(C, ta) {
  const m = plModal('Saved prompts', 'pllib');
  const q = h('input', { type: 'search', class: 'pl-q', placeholder: 'Search saved prompts', 'aria-label': 'Search saved prompts', autocomplete: 'off' });
  const list = h('div', { class: 'pl-list', role: 'list' }); const note = h('div', { class: 'pl-note muted', role: 'status' });
  let rows = [];
  const use = (p) => { m.close(); axPromptUse(C, ta, p); };
  const del = async (p) => {
    const starter = p.scope === 'starter';
    if (!await plConfirm(m.scrim, starter ? 'Remove the starter “' + p.name + '”?' : 'Delete “' + p.name + '”?', starter ? 'It disappears from this list. “Bring back starters” in the menu below restores it.' : 'The prompt file is deleted from ' + plWhere(p) + '. This cannot be undone.', starter ? 'Remove' : 'Delete')) return;
    try { await api('/api/library/prompt_delete', { name: p.name, scope: p.scope }); toast('Deleted'); load(); } catch (e) { toast(e.message); }
  };
  const draw = () => {
    list.replaceChildren(...rows.map(p => h('div', { class: 'pl-item', role: 'listitem' },
      h('button', { class: 'pl-main', type: 'button', title: 'Use this prompt', onclick: () => use(p) },
        h('span', { class: 'pl-name' }, p.name), h('span', { class: 'pl-desc' }, p.description),
        h('span', { class: 'pl-meta muted' }, plWhere(p) + (p.fields.length ? ' · fields: ' + plNames(p) : ''))),
      h('button', { class: 'btn pl-act', type: 'button', 'aria-label': 'Edit ' + p.name, onclick: () => plEdit(p, load, m.scrim) }, 'Edit'),
      h('button', { class: 'btn pl-act', type: 'button', 'aria-label': (p.scope === 'starter' ? 'Remove ' : 'Delete ') + p.name, onclick: () => del(p) }, p.scope === 'starter' ? 'Remove' : 'Delete'))));
    note.textContent = rows.length ? '' : (q.value.trim() ? 'No saved prompt matches that.' : 'No saved prompts yet. Use New prompt to save one.');
  };
  const load = async () => {
    try { rows = (await api('/api/library/prompts?q=' + encodeURIComponent(q.value.trim()))).prompts; draw(); } catch (e) { note.textContent = e.message; }
  };
  let tmr = 0; q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(load, 120); });
  q.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') { const b = list.querySelector('.pl-main'); if (b) { e.preventDefault(); b.focus(); } } else if (e.key === 'Enter' && rows[0]) { e.preventDefault(); use(rows[0]); } });
  list.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return; const bs = [...list.querySelectorAll('.pl-main')]; const i = bs.indexOf(document.activeElement); if (i < 0) return;
    e.preventDefault(); if (e.key === 'ArrowUp' && i === 0) q.focus(); else bs[Math.min(bs.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))].focus();
  });
  const file = h('input', { type: 'file', accept: '.json,application/json', hidden: true, 'aria-label': 'Choose an exported prompt file' });
  file.addEventListener('change', async () => {
    const f = file.files[0]; file.value = ''; if (!f) return;
    if (f.size > 2000000) { toast('That file is too large to be a prompt file.'); return; }
    let data; try { data = JSON.parse(await f.text()); } catch (e) { toast('That is not a prompt file (it is not valid JSON).'); return; }
    const scope = await plAsk(m.scrim, 'Add these prompts to…', 'Choose where to keep the prompts from ' + f.name + '. Names you already have are skipped.', [{ label: 'This project', value: 'project', cls: 'primary' }, { label: 'All projects', value: 'global' }]);
    if (!scope) return;
    try {
      const r = await api('/api/library/prompts_import', { data, scope, on_conflict: 'skip' });
      toast('Added ' + r.added.length + (r.skipped.length ? ', skipped ' + r.skipped.length + ' (' + r.skipped.slice(0, 2).map(s => s.name + ': ' + s.why).join('; ') + ')' : '')); load();
    } catch (e) { toast(e.message); }
  });
  const exportAll = async () => {
    try {
      const b = await api('/api/library/export');
      if (!b.prompts.length) { toast('You have not saved any prompts yet (the starters are built in).'); return; }
      const a = h('a', { href: URL.createObjectURL(new Blob([JSON.stringify(b, null, 1) + '\n'], { type: 'application/json' })), download: 'axon-prompts.json' });
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) { toast(e.message); }
  };
  const more = h('button', { class: 'btn', type: 'button', 'aria-haspopup': 'menu', onclick: (e) => wbMenu(e.currentTarget, [
    { label: 'Import from a file…', run: () => file.click() }, { label: 'Export my prompts to a file', run: exportAll }, '-',
    { label: 'Bring back removed starters', run: async () => { try { const r = await api('/api/library/starters_restore', {}); toast(r.restored ? 'Brought back ' + r.restored : 'No starters were removed'); load(); } catch (e) { toast(e.message); } } }], { up: true }) }, 'More');
  m.modal.append(h('h3', null, 'Saved prompts'), h('p', { class: 'muted' }, 'Pick one to put it in the message box. Prompts with {{fields}} ask for the values first.'), q, list, note,
    h('div', { class: 'acts' }, h('button', { class: 'btn primary', type: 'button', onclick: () => plEdit(null, load, m.scrim) }, 'New prompt'), more, h('span', { style: 'flex:1' }), h('button', { class: 'btn', type: 'button', onclick: m.close }, 'Close'), file));
  load(); q.focus();
}

// ---------- checkpoints (a section of the Changes panel) ----------
const CP = { rows: [], open: new Set(), files: new Set(), diffs: {}, err: '' };
const cpWhen = (t) => { const d = new Date(t * 1000); const same = d.toDateString() === new Date().toDateString(); return same ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
const cpStat = (s) => s === 'new' ? 'new' : s === 'deleted' ? 'deleted' : s === 'unchanged' ? 'same' : 'edited';
function cpCounts(a, r, bin) { return bin ? h('span', { class: 'muted' }, 'binary') : a == null ? h('span', { class: 'muted' }, 'large') : [a ? h('span', { class: 'cadd' }, '+' + a) : null, r ? h('span', { class: 'cdel' }, '−' + r) : null]; }
function axCheckpoints(P) {
  const body = h('div', { class: 'cp-body' }); const count = h('span', { class: 'cp-count muted' });
  const det = h('details', { class: 'cp-sec' }, h('summary', null, h('span', null, 'Checkpoints'), count), body);
  det.open = lsGet('axon-cp-closed') !== '1';
  det.addEventListener('toggle', () => lsSet('axon-cp-closed', det.open ? '0' : '1'));
  const chat = () => UI.chat;
  const busy = () => !!(chat() && chat().st && chat().st.busy);
  const diffOf = async (r, f) => {
    const key = r.n + ':' + f.rel;
    if (CP.diffs[key]) return;
    CP.diffs[key] = { wait: true }; draw();
    try { const d = await api('/api/library/checkpoint_diff?session=' + encodeURIComponent(chat().sid) + '&n=' + r.n + '&path=' + encodeURIComponent(f.rel)); CP.diffs[key] = d.files[0] || { rows: [], note: 'No difference.' }; }
    catch (e) { CP.diffs[key] = { rows: [], note: e.message }; }
    draw();
  };
  const diffNode = (r, f) => {
    const d = CP.diffs[r.n + ':' + f.rel]; if (!d) return null;
    if (d.wait) return h('div', { class: 'cp-note muted' }, 'Loading the diff…');
    if (!d.rows.length) return h('div', { class: 'cp-note muted' }, d.note || 'No textual changes.');
    return h('div', { class: 'cp-diff', tabindex: '0', 'aria-label': 'Diff of ' + f.rel }, ...d.rows.slice(0, 500).map(x => x.kind === 'hunk' || x.kind === 'meta'
      ? h('div', { class: 'cp-line ' + x.kind }, h('span', { class: 'ln' }), h('span', { class: 'lt' }, x.text))
      : h('div', { class: 'cp-line ' + x.kind }, h('span', { class: 'ln' }, String(x.kind === 'del' ? x.old : x.new)), h('span', { class: 'lm' }, x.kind === 'add' ? '+' : x.kind === 'del' ? '−' : ' '), h('span', { class: 'lt' }, x.text))),
      d.rows.length > 500 ? h('div', { class: 'cp-note muted' }, 'Diff cut here. Open the file in the viewer to see the rest.') : null);
  };
  const restore = async (r) => {
    let plan;
    try { plan = await api('/api/library/restore_plan?session=' + encodeURIComponent(chat().sid) + '&n=' + r.n); } catch (e) { toast(e.message); return; }
    const undo = r.kind === 'restore';
    const title = undo ? 'Undo this restore?' : 'Restore to before checkpoint ' + r.n + '?';
    if (!plan.files.length) { toast('Nothing to change: the files already match how they were' + (undo ? ' before that restore.' : ' before this checkpoint.')); return; }
    const m = plModal(title, 'cp-confirm'); const err = h('div', { class: 'pl-err', role: 'alert' });
    const verb = { restore: 'goes back to how it was', recreate: 'is put back (it was deleted)', delete: 'is deleted (Axon created it)' };
    const fileRow = (f) => h('li', { class: f.conflict ? 'cp-conf' : '' }, h('b', null, f.rel), ' ', verb[f.action], ' ', f.action === 'restore' && !f.binary && f.added != null ? h('span', { class: 'muted' }, '(+' + f.added + ' −' + f.removed + ')') : null,
      f.conflict ? h('div', { class: 'cp-why' }, f.why.charAt(0).toUpperCase() + f.why.slice(1) + '. Your version is not what Axon left.') : null);
    const run = async (conflicts) => {
      err.textContent = ''; for (const b of m.modal.querySelectorAll('button')) b.disabled = true;
      try {
        const res = await api('/api/library/restore', { session: chat().sid, n: r.n, conflicts });
        m.close(); toast(res.message + (res.checkpoint ? ' It is the newest checkpoint, so you can undo it there.' : '')); CP.diffs = {}; await load(); if (typeof wbRefreshChanges === 'function') { if (typeof GIT_CACHE !== 'undefined') GIT_CACHE.t = 0; wbRefreshChanges(true); }
        if (res.failed && res.failed.length) toast(res.failed[0]);
      } catch (e) { for (const b of m.modal.querySelectorAll('button')) b.disabled = false; err.textContent = e.message; }
    };
    const conflicts = plan.conflicts.length;
    const clean = plan.files.filter(f => !f.conflict);
    const acts = conflicts
      ? [h('button', { class: 'btn primary', type: 'button', onclick: () => run('keep') }, clean.length ? 'Keep my changes, restore the rest' : 'Keep my changes'),
         h('button', { class: 'btn danger', type: 'button', onclick: () => run('overwrite') }, 'Overwrite my changes too'), h('button', { class: 'btn', type: 'button', onclick: m.close }, 'Cancel')]
      : [h('button', { class: 'btn primary', type: 'button', onclick: () => run('refuse') }, undo ? 'Undo the restore' : 'Restore ' + plan.files.length + (plan.files.length === 1 ? ' file' : ' files')), h('button', { class: 'btn', type: 'button', onclick: m.close }, 'Cancel')];
    m.modal.append(h('h3', null, title),
      h('p', { class: 'muted' }, (plan.files.length === 1 ? 'This file changes' : 'These ' + plan.files.length + ' files change') + ', and nothing else:'),
      h('ul', { class: 'cp-files' }, ...plan.files.map(fileRow)),
      conflicts ? h('p', { class: 'cp-warn' }, conflicts + (conflicts === 1 ? ' file was' : ' files were') + ' changed outside Axon. Axon will not overwrite ' + (conflicts === 1 ? 'it' : 'them') + ' unless you choose to.') : null,
      plan.unverified.length ? h('p', { class: 'muted' }, 'Axon did not record what it left in ' + plan.unverified.length + (plan.unverified.length === 1 ? ' file' : ' files') + ' of an older checkpoint, so edits you made to ' + (plan.unverified.length === 1 ? 'it' : 'them') + ' cannot be checked.') : null,
      plan.skipped.length ? h('p', { class: 'muted' }, 'Left alone: ' + plan.skipped.map(s => s.rel + ' (' + s.why + ')').join(', ') + '.') : null,
      h('p', { class: 'muted' }, 'This is saved as a new checkpoint, so you can undo it from this list.'), err, h('div', { class: 'acts' }, ...acts));
    m.modal.querySelector('.acts .btn:last-child').focus();           // Cancel is where focus starts
  };
  const item = (r) => {
    const open = CP.open.has(r.n); const isRestore = r.kind === 'restore';
    const title = isRestore ? r.label : (r.prompt || 'Edits by Axon');
    const head = h('button', { class: 'cp-head', type: 'button', 'aria-expanded': String(open), title: title, onclick: () => { if (open) CP.open.delete(r.n); else CP.open.add(r.n); draw(); } },
      h('span', { class: 'fp-chev' + (open ? ' open' : '') }, wbIco('chev')), h('span', { class: 'cp-time' }, cpWhen(r.time)), h('span', { class: 'cp-title' }, title),
      h('span', { class: 'cp-stat' }, r.file_count + (r.file_count === 1 ? ' file' : ' files'), r.added ? h('span', { class: 'cadd' }, '+' + r.added) : null, r.removed ? h('span', { class: 'cdel' }, '−' + r.removed) : null));
    const kids = [head];
    if (open) {
      const det2 = h('div', { class: 'cp-detail' });
      if (r.kind === 'current') det2.append(h('div', { class: 'cp-note muted' }, 'Axon is still working on this turn.'));
      for (const f of r.files) {
        const fo = CP.files.has(r.n + ':' + f.rel); const name = f.rel.split('/').pop(); const dir = f.rel.includes('/') ? f.rel.slice(0, f.rel.lastIndexOf('/')) : '';
        det2.append(h('button', { class: 'cp-file ' + f.status, type: 'button', 'aria-expanded': String(fo), title: f.rel, onclick: () => { const k = r.n + ':' + f.rel; if (fo) CP.files.delete(k); else { CP.files.add(k); diffOf(r, f); } draw(); } },
          h('span', { class: 'cp-mark' }, cpStat(f.status)), h('span', { class: 'ch-name' }, name), dir ? h('span', { class: 'ch-dir' }, dir) : null, h('span', { style: 'flex:1' }), cpCounts(f.added, f.removed, f.binary)));
        if (fo) { const dn = diffNode(r, f); if (dn) det2.append(dn); }
      }
      if (r.file_count > r.files.length) det2.append(h('div', { class: 'cp-note muted' }, '+ ' + (r.file_count - r.files.length) + ' more files'));
      if (r.kind !== 'current') {
        const b = busy();
        det2.append(h('div', { class: 'cp-acts' }, h('button', { class: 'btn', type: 'button', disabled: b, title: b ? 'Wait for Axon to finish the current turn' : '', onclick: () => restore(r) }, isRestore ? 'Undo this restore…' : 'Restore to before this…')));
      }
      kids.push(det2);
    }
    return h('div', { class: 'cp-item' + (open ? ' open' : '') }, ...kids);
  };
  function draw() {
    const real = CP.rows.filter(r => r.kind !== 'current' || r.files.length);
    count.textContent = real.length ? String(real.length) : '';
    if (CP.err) { body.replaceChildren(h('div', { class: 'cp-note muted' }, CP.err)); return; }
    body.replaceChildren(...(real.length ? real.map(item) : [h('div', { class: 'cp-note muted' }, 'No checkpoints yet. Each time Axon edits files in a turn, one appears here so you can see what it changed and go back.')]));
  }
  async function load() {
    const C = chat();
    if (!C || !C.sid) { CP.rows = []; CP.err = ''; draw(); return; }
    try { const d = await api('/api/library/checkpoints?session=' + encodeURIComponent(C.sid)); CP.rows = d.checkpoints.slice().reverse(); CP.err = ''; } catch (e) { CP.err = e.status === 404 ? '' : e.message; CP.rows = []; }
    draw();
  }
  P.cpRefresh = () => { CP.diffs = {}; load(); };
  load();
  return det;
}
"""

LIBRARY_CSS = r"""
.pl-scrim{z-index:40}.toast{z-index:50}.plmodal{width:min(640px,100%)}.plmodal h3{margin:0 0 6px}.plmodal .acts{align-items:center}
.pl-q{width:100%;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:7px 10px;margin:6px 0 8px}
.pl-list{display:flex;flex-direction:column;max-height:46vh;overflow:auto;border-top:1px solid var(--line)}
.pl-item{display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--line)}
.pl-main{flex:1;min-width:0;display:grid;gap:1px;text-align:left;background:none;border:0;padding:8px 6px;cursor:pointer;color:var(--ink)}
.pl-main:hover,.pl-main:focus-visible{background:var(--raised)}
.pl-act{flex:none;padding:3px 9px;font-size:12.5px}
@media (max-width:560px){.pl-meta{white-space:normal}.pl-item{flex-wrap:wrap}.pl-main{flex-basis:100%}.pl-act{margin:0 0 6px 6px}}
.pl-name{font-weight:600}.pl-desc{color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.pl-meta{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-note{padding:10px 2px;font-size:13px}.pl-err{color:var(--bad);font-size:13px;min-height:0;margin-top:6px}
.pl-row{display:grid;gap:4px;margin:10px 0}.pl-row label{font-size:13px;color:var(--ink2)}
.pl-row input,.pl-row textarea,.pl-row select{width:100%;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.pl-row textarea{resize:vertical;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.pl-tpl{max-height:140px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:6px 0}.pl-found,.pl-help{font-size:12px;margin:2px 0 0}
.cp-sec{border-bottom:1px solid var(--line);flex:none}
.cp-sec>summary{display:flex;gap:8px;align-items:center;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:550;list-style:none}
.cp-sec>summary::-webkit-details-marker{display:none}.cp-sec>summary::before{content:"+";color:var(--muted);font-family:ui-monospace,monospace;width:10px}
.cp-sec[open]>summary::before{content:"-"}
.cp-body{max-height:min(46vh,420px);overflow:auto;padding:0 0 6px}
.cp-note{padding:6px 14px 8px 32px;font-size:12.5px}
.cp-item{border-top:1px solid color-mix(in srgb,var(--line) 60%,transparent)}
.cp-head,.cp-file{display:flex;align-items:center;gap:8px;width:100%;background:none;border:0;color:var(--ink);cursor:pointer;text-align:left;font-size:13px}
.cp-head{padding:6px 12px 6px 10px}.cp-head:hover,.cp-file:hover{background:var(--raised)}
.cp-time{flex:none;color:var(--ink2);font-variant-numeric:tabular-nums;font-size:12px}
.cp-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cp-stat{flex:none;display:flex;gap:6px;color:var(--ink2);font-size:12px}
.cp-file{padding:4px 12px 4px 32px;font-size:12.5px}
.cp-mark{flex:none;width:44px;color:var(--muted);font-size:11.5px}.cp-file.new .cp-mark{color:var(--ok)}.cp-file.deleted .cp-mark{color:var(--bad)}.cp-file.modified .cp-mark{color:var(--warn)}
.cp-acts{padding:6px 14px 8px 32px}
.cp-diff{margin:2px 0 6px 32px;border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:280px;font:12px/18px ui-monospace,SFMono-Regular,Menlo,monospace}
.cp-line{display:flex;white-space:pre;min-width:max-content}.cp-line .ln{flex:none;width:44px;text-align:right;padding-right:8px;color:var(--muted);user-select:none}
.cp-line .lm{flex:none;width:14px;color:var(--muted);user-select:none}
.cp-line.add{background:var(--add-bg);box-shadow:inset 3px 0 0 var(--add)}.cp-line.del{background:var(--del-bg);box-shadow:inset 3px 0 0 var(--del)}
.cp-line.hunk{color:var(--info)}.cp-line.meta{color:var(--muted);font-style:italic}
.cp-files{margin:6px 0;padding-left:18px;max-height:34vh;overflow:auto}.cp-files li{margin:4px 0;overflow-wrap:anywhere}
.cp-conf{color:var(--ink)}.cp-why,.cp-warn{color:var(--bad);font-size:13px}.cp-why{margin:2px 0 0}
"""
