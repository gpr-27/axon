"""Client script: Insights (review hunks, decision trail, timeline, context map, pins, what-if, context diff, receipts, search)."""

JS_INSIGHTS = r"""
// ---------- insights ----------
const INSIGHT_TABS = [['review', 'Review changes'], ['trail', 'Decision trail'], ['timeline', 'Timeline'], ['ctxmap', 'Context map'], ['pins', 'Pins'],
  ['whatif', 'What if'], ['ctxdiff', 'Context diff'], ['receipts', 'Receipts'], ['compaction', 'Compaction'], ['search', 'Search sessions']];
function kvTable(obj) {
  return h('dl', { class: 'kv' }, ...Object.entries(obj || {}).flatMap(([k, v]) => [h('dt', null, k), h('dd', null, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))]));
}
async function viewInsights(rest) {
  const tab = (rest || 'review').split('?')[0];
  return shell('insights', async (pane) => {
    const sid = () => (UI.chat ? UI.chat.sid : '');
    const body = h('div'); const tabs = h('div', { class: 'tabs', role: 'tablist' }, ...INSIGHT_TABS.map(([k, l]) => h('button', { role: 'tab', 'aria-selected': String(k === tab), onclick: () => { location.hash = '#/insights/' + k; } }, l)));
    pane.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Insights'), h('div', { class: 'sub' }, 'What the agent did and why, what fills the context window, and what it cost. Read from the live session' + (sid() ? '.' : ': open a chat first.')))), tabs, body);
    const get = (name, extra) => api('/api/insights/' + name + '?session=' + encodeURIComponent(sid()) + (extra || ''));
    const reload = () => viewInsights(tab);
    const post = async (name, data) => { try { const r = await api('/api/insights/' + name, Object.assign({ session: sid() }, data)); toast(r.message || (r.ok === false ? 'Not done' : 'Done')); } catch (e) { toast(e.message); } reload(); };
    try {
      if (tab === 'search') {
        const q = h('input', { type: 'search', class: 'search', placeholder: 'Search every saved conversation in this project', 'aria-label': 'Search sessions' }); const all = h('label', { class: 'muted' }, h('input', { type: 'checkbox' }), ' all projects'); const res = h('div');
        const run = async () => { if (q.value.trim().length < 2) return; res.replaceChildren(h('p', { class: 'muted' }, 'Searching...')); const d = await api('/api/insights/search?q=' + encodeURIComponent(q.value) + (all.firstChild.checked ? '&scope=all' : '')); res.replaceChildren(h('p', { class: 'muted' }, d.hits.length + ' matches in ' + d.files + ' sessions' + (d.truncated ? ' (more exist: refine the search)' : '')), ...d.hits.map(x => h('a', { class: 'hit', href: '#/chat/' + encodeURIComponent(x.session) }, h('b', null, x.type.replace('_', ' ') + ', prompt ' + x.turn), h('span', { class: 'muted' }, x.snippet)))); };
        q.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); }); body.append(h('div', { class: 'formrow' }, q, all, h('button', { class: 'btn primary', onclick: run }, 'Search')), res, h('div', { class: 'sres' })); q.focus(); return;
      }
      if (!sid()) { body.append(h('div', { class: 'empty' }, h('strong', null, 'No session yet'), 'Send a message in a chat first; this page reads the live session.')); return; }
      const d = await get(tab);
      if (tab === 'review') {
        if (!d.hunks.length) { body.append(h('div', { class: 'empty' }, h('strong', null, 'No changes to review'), 'When the agent edits files this session, each changed hunk appears here. Rejecting a hunk reverts exactly that hunk on disk.')); return; }
        const byFile = {}; d.hunks.forEach(x => (byFile[x.file] = byFile[x.file] || []).push(x));
        for (const [file, hs] of Object.entries(byFile)) {
          body.append(h('h2', { style: 'margin-top:18px' }, h('a', { class: 'plink', href: '#/files/' + encodeURIComponent(file) }, file), '  ', h('span', { class: 'muted' }, hs.length + ' hunk' + (hs.length > 1 ? 's' : ''))));
          for (const x of hs) {
            const lines = x.removed.map(l => '-' + l).concat(x.added.map(l => '+' + l));
            body.append(h('div', { class: 'card' }, h('div', { class: 'formrow', style: 'margin:0 0 6px' }, h('b', null, x.kind + ' at line ' + x.new_start), x.accepted ? h('span', { class: 'good' }, 'accepted') : null, h('span', { style: 'flex:1' }),
              h('button', { class: 'btn', disabled: x.accepted, onclick: () => post('review_accept', { id: x.id }) }, 'Accept'),
              h('button', { class: 'btn danger', onclick: () => { if (confirm('Revert this hunk in ' + file + '? The rejected text is kept on disk.')) post('review_reject', { id: x.id }); } }, 'Reject')), diffEl(lines)));
          }
        }
      } else if (tab === 'trail') {
        const sel = h('select', { 'aria-label': 'Tool call', onchange: () => { location.hash = '#/insights/trail'; sessionStorage.setItem('trail-n', sel.value); reload(); } }, ...d.calls.map(c => h('option', { value: c.n, selected: d.trail && d.trail.n === c.n }, '#' + c.n + '  ' + c.name + '  (turn ' + c.turn + ')')));
        const n = Number(sessionStorage.getItem('trail-n') || 0);
        let t = d.trail; if (n && (!t || t.n !== n)) { t = (await get('trail', '&n=' + n)).trail; sel.value = String(n); }
        if (!t) { body.append(h('div', { class: 'empty' }, h('strong', null, 'No tool calls yet'), 'Each tool call gets a trail: why it was chosen, what allowed it, what it returned and what came next.')); return; }
        const p = t.permission || {};
        body.append(h('div', { class: 'formrow' }, sel), h('div', { class: 'card' }, h('h3', null, t.name + ', call ' + t.n + ' of ' + t.total), h('dl', { class: 'trail', style: 'grid-template-columns:110px 1fr' },
          h('dt', null, 'Why'), h('dd', null, t.rationale ? t.rationale.text + ' (' + t.rationale.source + ')' : 'not recorded'),
          h('dt', null, 'Input'), h('dd', null, pagedPre(JSON.stringify(t.input, null, 2), 'out')),
          h('dt', null, 'Permission'), h('dd', null, p.outcome ? (p.outcome === 'deny' ? 'Denied' : 'Allowed') + ' because ' + (p.source || '?') + (p.reason ? ': ' + p.reason : '') + (p.approval ? ' (you chose ' + p.approval + ')' : '') : 'not recorded'),
          h('dt', null, 'Hooks'), h('dd', null, (t.hooks || []).length ? JSON.stringify(t.hooks) : 'none ran'),
          h('dt', null, 'Output'), h('dd', null, t.result ? h('div', null, pagedPre(t.result.preview || '', 'out'), h('div', { class: 'muted' }, t.result.lines + ' lines, ' + t.result.bytes + ' bytes, about ' + t.result.est_tokens + ' tokens' + (t.result.duration_s != null ? ', ' + dur(t.result.duration_s) : '') + (t.result.is_error ? ', error' : ''))) : 'not recorded'),
          h('dt', null, 'Then'), h('dd', null, t.next ? t.next.summary : 'not recorded'))));
      } else if (tab === 'timeline') {
        const tl = d.timeline; if (!tl) { body.append(h('p', { class: 'muted' }, 'No turn has run yet.')); return; }
        const maxd = Math.max(0.001, ...tl.rows.map(r => r.duration_s || 0));
        body.append(h('p', null, 'Turn ', h('b', null, tl.turn), ' of ' + d.turns + ' with ' + tl.model + ': ', h('b', null, dur(tl.total_s)), ' and ', h('b', null, usd(tl.cost))),
          table([{ label: 'Step', key: 'label' }, { label: 'Time', cls: 'r', fmt: r => r.duration_s != null ? dur(r.duration_s) : '' }, { label: '', fmt: r => h('div', { class: 'bar', style: 'width:160px' }, h('i', { style: 'width:' + Math.max(2, 100 * (r.duration_s || 0) / maxd) + '%' + (r.cache_miss_cause && r.kind === 'llm' && r.cache_hit_pct === 0 ? ';background:var(--warn)' : '') })) },
            { label: 'In', cls: 'r', fmt: r => r.tokens_in != null ? tok(r.tokens_in) : '' }, { label: 'Out', cls: 'r', fmt: r => r.tokens_out != null ? tok(r.tokens_out) : '' }, { label: 'Cost', cls: 'r', fmt: r => r.cost != null ? usd(r.cost) : '' }, { label: 'Cache hit', cls: 'r', fmt: r => r.cache_hit_pct != null ? r.cache_hit_pct.toFixed(0) + '%' : '' }, { label: 'Cache miss cause', cls: 't', key: 'cache_miss_cause' }], tl.rows));
      } else if (tab === 'ctxmap') {
        const kinds = {}; d.items.forEach(i => { kinds[i.kind] = (kinds[i.kind] || 0) + i.tokens; });
        body.append(h('p', null, h('b', null, tok(d.total)), ' of ' + tok(d.window) + ' tokens in use (' + Math.round(100 * d.total / d.window) + '%). Auto-compaction starts at ' + tok(d.threshold) + '.'),
          h('div', { class: 'bar', style: 'height:12px' }, h('i', { style: 'width:' + Math.min(100, 100 * d.total / d.window) + '%' })),
          h('p', { class: 'muted' }, Object.entries(kinds).map(([k, v]) => k + ' ' + tok(v)).join('   ')),
          table([{ label: 'What', cls: 't', fmt: r => r.label }, { label: 'Kind', key: 'kind' }, { label: 'Tokens', cls: 'r', fmt: r => tok(r.tokens) }, { label: 'Share', fmt: r => h('div', { class: 'bar', style: 'width:120px' }, h('i', { style: 'width:' + Math.max(1, 100 * r.share) + '%' })) },
            { label: '', fmt: r => h('div', { class: 'formrow', style: 'margin:0' }, r.pinned ? h('span', { class: 'muted' }, 'pinned') : null, r.droppable ? h('button', { class: 'mini', title: 'Replace with a short stub; the full text is saved to disk', onclick: () => post('ctxmap_drop', { id: r.id }) }, 'Drop') : null, (r.droppable && !r.pinned) ? h('button', { class: 'mini', onclick: () => post('pin', { target: r.id }) }, 'Pin') : null, r.droppable ? null : h('span', { class: 'muted', title: r.why }, 'fixed')) }], d.items));
      } else if (tab === 'pins') {
        const t = h('input', { type: 'text', placeholder: 'A file path or a tool result id', 'aria-label': 'What to pin' });
        body.append(h('p', null, 'Pinned results stay in the context window when it is compacted or trimmed. ', h('b', null, tok(d.total_tokens)), ' pinned.'), h('div', { class: 'formrow' }, t, h('button', { class: 'btn primary', onclick: () => t.value.trim() && post('pin', { target: t.value.trim() }) }, 'Pin'), h('button', { class: 'btn', onclick: () => post('unpin', { target: '' }) }, 'Unpin all')),
          d.pins.length ? table([{ label: 'Pinned', key: 'label' }, { label: 'Tokens', cls: 'r', fmt: r => tok(r.tokens) }, { label: 'State', key: 'state' }, { label: '', fmt: r => h('button', { class: 'mini', onclick: () => post('unpin', { target: r.id }) }, 'Unpin') }], d.pins) : h('p', { class: 'muted' }, 'Nothing is pinned.'),
          d.recent.length ? h('div', null, h('h2', { style: 'margin-top:16px' }, 'Recent results you could pin'), ...d.recent.map(r => h('div', { class: 'row', style: 'display:flex;gap:10px;padding:3px 0' }, h('span', { style: 'flex:1' }, r.label), h('button', { class: 'mini', onclick: () => post('pin', { target: r.id }) }, 'Pin')))) : null);
      } else if (tab === 'whatif') {
        if (d.empty) { body.append(h('p', { class: 'muted' }, 'No model calls yet, so there is nothing to reprice.')); return; }
        body.append(h('p', null, 'This session used ', h('b', null, tok(d.input)), ' input and ', h('b', null, tok(d.output)), ' output tokens over ' + d.calls + ' calls' + (d.actual_known ? ' and cost ' : '') + (d.actual_known ? usd(d.actual) : '') + '. What the same tokens would cost on other models:'),
          table([{ label: 'Model', key: 'model' }, { label: 'With cache', cls: 'r', fmt: r => r.priced ? usd(r.with_cache) : 'no price' }, { label: 'Without cache', cls: 'r', fmt: r => r.priced ? usd(r.without_cache) : '' }, { label: 'Against actual', cls: 'r', fmt: r => r.priced && r.vs_actual_pct != null ? (r.vs_actual_pct > 0 ? '+' : '') + r.vs_actual_pct + '%' : '' }, { label: '', fmt: r => r.used ? 'used here' : '' }], d.rows));
      } else if (tab === 'ctxdiff') {
        body.append(d.ok ? h('pre', { class: 'out' }, (d.lines || []).join('\n')) : notice(d.message || 'Nothing to compare yet: it needs two model calls.', 'warn'));
      } else if (tab === 'receipts') {
        body.append(!d.answer_found ? h('p', { class: 'muted' }, 'No finished answer to check yet.') : (d.claims.length ? table([{ label: 'Claim in the last answer', cls: 't', key: 'claim' }, { label: 'Supported', fmt: r => r.supported ? h('span', { class: 'good' }, 'yes') : h('span', { class: 'bad' }, 'no') }, { label: 'Behind it', cls: 't', key: 'call' }, { label: 'Note', cls: 't', key: 'note' }], d.claims) : h('p', { class: 'muted' }, 'The last answer made no claims that can be checked against a tool call.')), h('p', { class: 'muted' }, 'A heuristic: it matches claims about files and commands to the tool calls of that turn.'));
      } else if (tab === 'compaction') {
        body.append(d.receipt ? kvTable(d.receipt) : h('p', { class: 'muted' }, 'This conversation has not been compacted.'));
      }
    } catch (e) { body.append(h('div', { class: 'notice warn' }, e.message)); }
  }, { key: 'insights:' + tab, hash: '#/insights/' + tab, title: 'Insights' });
}
"""
