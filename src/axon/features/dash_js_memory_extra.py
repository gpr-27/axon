"""Client script: memory history / restore / redact and the Dream panel on the Memory page.
Server side: dash_memory_extra.py (routes under /api/manage/memory/)."""

JS_MEMORY_EXTRA = r"""
// ---------- memory history, restore, redact (dash_memory_extra.py) ----------
function mgMemHistory(host, ref, label, onChange) {
  const box = h('div', { class: 'mg-hist' });
  const close = h('button', { class: 'btn', type: 'button', onclick: () => box.remove() }, 'Close history');
  async function load() {
    let d; try { d = await api('/api/manage/memory/history?' + mgQs({ ref })); } catch (e) { box.replaceChildren(errorBox(e.message), close); return; }
    box.replaceChildren(h('p', { class: 'muted' }, 'History of ' + label + ': ' + d.versions.length + ' version(s) kept, newest first.'),
      ...d.versions.map(v => {
        const view = h('pre', { class: 'out', style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-width:100%', hidden: true });
        const row = h('div', { class: 'mg-hist-row', style: 'margin:6px 0' },
          h('div', null, h('b', null, 'v' + v.v), ' ', v.when, ' ', h('span', { class: 'muted' }, v.action + ' by ' + v.actor + (v.session ? ' (session ' + v.session.slice(0, 8) + ')' : '') + ' · ' + v.sha + (v.redacted ? ' · redacted' : ''))),
          h('div', { class: 'acts' },
            h('button', { class: 'btn', type: 'button', onclick: async () => {
              if (!view.hidden) { view.hidden = true; return; }
              try { const t = await api('/api/manage/memory/history?' + mgQs({ ref, v: v.v })); view.textContent = t.text || '(empty)'; view.hidden = false; } catch (e) { mgNotice(e.message, { error: true }); } } }, 'View'),
            h('button', { class: 'btn', type: 'button', disabled: v.action === 'deleted', onclick: async () => {
              if (!await mgConfirm('Restore v' + v.v + '?', 'It becomes the current text. The text it replaces stays in the history.', 'Restore')) return;
              try { await api('/api/manage/memory/restore', { ref, v: v.v }); mgNotice('Restored v' + v.v + '.'); if (onChange) onChange(); load(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Restore'),
            h('button', { class: 'btn danger', type: 'button', disabled: v.action === 'deleted', onclick: async () => {
              const secret = window.prompt('Value to scrub from v' + v.v + ' (leave empty to remove anything that looks like a secret). The history entry itself is kept.', '');
              if (secret === null) return;
              try { await api('/api/manage/memory/redact', { ref, v: v.v, secret }); mgNotice('Scrubbed v' + v.v + '.'); if (onChange) onChange(); load(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Redact')),
          view);
        return row;
      }), close);
  }
  host.append(box); load();
}

// ---------- Dreams: consolidate notes from past sessions (dry run, then apply) ----------
function mgDreamPanel(host) {
  const box = h('div', { class: 'mg-dream' });
  const status = h('div', { class: 'muted', 'aria-live': 'polite' });
  const out = h('div');
  let timer = null;
  const start = h('button', { class: 'btn primary', type: 'button', onclick: async () => {
    try { await api('/api/manage/memory/dream/start', {}); mgNotice('Dreaming: reading past sessions and notes.'); poll(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Dream: consolidate notes');
  const cancel = h('button', { class: 'btn', type: 'button', hidden: true, onclick: async () => { try { await api('/api/manage/memory/dream/cancel', {}); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Cancel');
  async function poll() {
    clearTimeout(timer);
    let d; try { d = await api('/api/manage/memory/dream'); } catch (e) { status.textContent = e.message; return; }
    const j = d.job, running = !!j && j.state === 'running';
    start.disabled = running; cancel.hidden = !running;
    status.textContent = j ? (running ? 'Dreaming with ' + j.model + ': ' + j.message + ' (' + j.elapsed + 's)' : j.state === 'failed' ? j.error : j.state === 'cancelled' ? 'Cancelled. Nothing changed.' : '') : '';
    out.replaceChildren();
    const p = d.proposal;
    if (p) {
      out.append(h('p', null, h('b', null, 'Dream ' + p.id), ' ', h('span', { class: 'muted' }, p.status + ' · ' + p.summary + ' · ' + p.model + ' · ' + p.sessions + ' session(s) read · ' + (p.usage.input || 0) + ' in / ' + (p.usage.output || 0) + ' out · $' + (p.cost || 0).toFixed(4))));
      for (const x of p.diff.filter(x => x.kind !== 'unchanged')) {
        out.append(h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'tn' }, ({ added: '+ ', changed: '~ ', removed: '- ' })[x.kind] + x.name), h('span', { class: 'ts' }, x.kind + ' · ' + x.file)),
          h('pre', { class: 'out', style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, x.lines.join('\n') + (x.more ? '\n... ' + x.more + ' more lines' : ''))));
      }
      for (const i of p.insights) out.append(h('p', { class: 'muted' }, 'Insight: ' + i));
      if (p.status === 'proposed') out.append(h('p', { class: 'muted' }, 'Nothing has changed yet.'), h('div', { class: 'acts' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
        if (!await mgConfirm('Apply this dream?', p.summary + '. Notes it drops go to the memory trash, and the original set stays restorable.', 'Apply')) return;
        try { const r = await api('/api/manage/memory/dream/apply', { id: p.id }); mgNotice('Applied: ' + r.written + ' written, ' + r.removed + ' removed.'); poll(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Apply')));
      if (p.status === 'applied') out.append(h('div', { class: 'acts' }, h('button', { class: 'btn', type: 'button', onclick: async () => {
        try { await api('/api/manage/memory/dream/apply', { id: p.id, undo: true }); mgNotice('Original notes restored.'); poll(); } catch (e) { mgNotice(e.message, { error: true }); } } }, 'Undo (restore the original notes)')));
    }
    if (running) timer = setTimeout(poll, 1500);
  }
  onLeave(() => clearTimeout(timer));
  host.append(h('h2', { style: 'margin-top:22px' }, 'Dream'),
    h('p', { class: 'muted' }, 'Reads a bounded, secret-free sample of this project\'s past chats plus the notes, and proposes a tidier notes set: duplicates merged, stale notes replaced, insights surfaced. It is a dry run until you apply it, and the original is always kept.'),
    h('div', { class: 'acts' }, start, cancel), status, out);
  poll();
}
"""
