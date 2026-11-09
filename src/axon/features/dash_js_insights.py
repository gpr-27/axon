"""Client script: Insights (review hunks, decision trail, timeline, context map, pins, what-if, context diff, receipts, search)."""

JS_INSIGHTS = r"""
// ---------- charts: one small kit for Usage, Insights and Progress ----------
// Marks wear series colours; every label is text. Values also live in the tables below each chart, so a tooltip never gates anything.
function vzNice(max) { if (!(max > 0)) return 1; const m = Math.pow(10, Math.floor(Math.log10(max))), r = max / m; return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * m; }
function vzMoney(n) { return n === 0 ? '$0' : n >= 100 ? '$' + Math.round(n).toLocaleString() : n >= 1 ? '$' + n.toFixed(n % 1 ? 2 : 0) : n >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(4).replace(/0+$/, ''); }
function vzDay(s) { const d = new Date(String(s) + 'T00:00:00'); return isNaN(d) ? String(s) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
// Columns over time, one series or stacked. rows: [{ name, ...values }]; series: [{ key, label, color }].
function vzColumns(rows, series, o) {
  const opts = o || {}; const total = (r) => series.reduce((a, s) => a + (r[s.key] || 0), 0);
  const top = vzNice(Math.max(0, ...rows.map(total))), fy = opts.fmtY || String;
  const pct = (v) => (100 * v / top) + '%';
  const grid = h('div', { class: 'vz-grid', 'aria-hidden': 'true' }, [0, 0.5, 1].map(f => h('div', { class: 'vz-gl' + (f ? '' : ' base'), style: 'bottom:' + (f * 100) + '%' }, h('span', null, fy(top * f)))));
  const slots = rows.map((r, i) => {
    const t = total(r);
    const stack = t > 0 ? h('div', { class: 'vz-stack', style: 'height:' + pct(t) }, series.slice().reverse().filter(s => r[s.key] > 0).map(s => h('i', { style: 'flex:' + r[s.key] + ' 0 0;background:' + s.color }))) : null;
    return h('div', { class: 'vz-slot', 'data-i': i }, stack);
  });
  const bars = h('div', { class: 'vz-bars' }, slots);
  const tip = h('div', { class: 'vz-tip', hidden: true });
  const plot = h('div', { class: 'vz-plot' }, grid, bars, tip);
  let cur = -1;
  const show = (i) => {
    cur = i; slots.forEach((s, k) => s.classList.toggle('on', k === i)); if (i < 0) { tip.hidden = true; return; }
    const r = rows[i]; const multi = series.length > 1;
    tip.replaceChildren(h('b', null, multi ? (opts.fmtV || String)(total(r)) : (opts.fmtV || String)(r[series[0].key] || 0)), h('span', { class: 'vz-tipd' }, vzDay(r.name)), multi ? series.map(s => h('div', { class: 'vz-tipr' }, h('i', { style: 'background:' + s.color }), h('span', null, s.label), h('em', null, (opts.fmtV || String)(r[s.key] || 0)))) : null, opts.note ? h('span', { class: 'vz-tipd' }, opts.note(r)) : null);
    tip.hidden = false;
    const pr = plot.getBoundingClientRect(), sr = slots[i].getBoundingClientRect(), w = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(pr.width - w, sr.left - pr.left + sr.width / 2 - w / 2)) + 'px';
  };
  bars.addEventListener('pointermove', (e) => { const s = e.target.closest('.vz-slot'); if (s && +s.dataset.i !== cur) show(+s.dataset.i); });
  bars.addEventListener('pointerleave', () => show(-1));
  const lab = rows.length ? [rows[0], rows[Math.floor((rows.length - 1) / 2)], rows[rows.length - 1]].filter((r, i, a) => a.indexOf(r) === i) : [];
  const el = h('div', { class: 'vz', tabindex: '0', role: 'img', 'aria-label': (opts.label || 'Chart') + '. Use the left and right arrow keys to read each day. The same numbers are in the table below.' }, plot,
    h('div', { class: 'vz-x', 'aria-hidden': 'true' }, lab.map(r => h('span', null, vzDay(r.name)))));
  el.addEventListener('keydown', (e) => { if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return; e.preventDefault(); show(Math.max(0, Math.min(rows.length - 1, (cur < 0 ? rows.length : cur) + (e.key === 'ArrowLeft' ? -1 : 1)))); });
  el.addEventListener('blur', () => show(-1));
  return el;
}
// Ranked bars. parts: [{ name, value, text, hi }]. One hue; with an emphasised row the others go grey.
function vzRanked(parts, o) {
  const max = Math.max(0, ...parts.map(p => p.value)) || 1; const em = parts.some(p => p.hi);
  return h('div', { class: 'vz-rank' + ((o && o.cls) ? ' ' + o.cls : '') }, parts.map(p => h('div', { class: 'vz-r' + (em && !p.hi ? ' dim' : '') },
    h('span', { class: 'vz-rn', title: p.name }, p.name), h('span', { class: 'vz-rv' }, p.text), h('span', { class: 'vz-rb' }, h('i', { style: 'width:' + Math.max(0.5, 100 * p.value / max) + '%' })))));
}
// One whole split into parts: a segmented bar and its legend, in fixed slot order. parts: [{ name, value, text }]; tall prints each share inside its block.
function vzSegments(parts, o) {
  const opts = o || {}; const sum = opts.total || parts.reduce((a, p) => a + p.value, 0) || 1;
  const list = parts.filter(p => p.value > 0).map((p, i) => Object.assign({ color: p.color || 'var(--s' + (i + 1) + ')' }, p));
  const name = list.map(p => p.name + ' ' + (p.text || '') ).join(', ');
  const bar = h('div', { class: 'vz-seg' + (opts.tall ? ' tall' : ''), role: 'img', 'aria-label': (opts.label ? opts.label + ': ' : '') + name, style: opts.total ? '' : null }, list.map(p => {
    const share = 100 * p.value / sum;
    return h('i', { style: 'flex:' + share + ' 0 0;background:' + p.color + (p.ink ? ';color:' + p.ink : ''), title: p.name + (p.text ? ': ' + p.text : '') }, opts.tall && share >= 12 ? Math.round(share) + '%' : null);
  }), opts.total && sum > parts.reduce((a, p) => a + p.value, 0) ? h('i', { class: 'rest', style: 'flex:' + (100 * (sum - parts.reduce((a, p) => a + p.value, 0)) / sum) + ' 0 0' }) : null);
  return h('div', { class: 'vz-whole' }, bar, opts.legend === false ? null : h('ul', { class: 'vz-legend' }, list.map(p => h('li', null, h('i', { style: 'background:' + p.color }), h('span', null, p.name), p.text ? h('em', null, p.text) : null))));
}
function vzMeter(frac, cls, label) { const p = Math.max(0, Math.min(100, Math.round(frac * 100))); return h('div', { class: 'vz-meter ' + (cls || ''), role: 'img', 'aria-label': (label || '') + ' ' + p + '%' }, h('i', { style: 'width:' + p + '%' })); }
function vzStat(label, value, sub) { return h('div', { class: 'vz-stat' }, h('div', { class: 'l' }, label), h('div', { class: 'v' }, value), sub ? h('div', { class: 'h' }, sub) : null); }
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
    pane.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Insights'), h('div', { class: 'sub' }, 'What the agent did and why, what fills the context window, and what it cost. Read from the live session' + (sid() ? '.' : ': open a chat first.')))), typeof overviewSection === 'function' ? overviewSection() : null, tabs, body);
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
        const llmT = tl.rows.filter(r => r.kind === 'llm').reduce((a, r) => a + (r.duration_s || 0), 0), toolT = tl.rows.filter(r => r.kind !== 'llm').reduce((a, r) => a + (r.duration_s || 0), 0);
        body.append(h('div', { class: 'vz-stats' }, vzStat('Time', dur(tl.total_s), 'turn ' + tl.turn + ' of ' + d.turns), vzStat('Cost', usd(tl.cost), tl.model), vzStat('Steps', String(tl.rows.length), dur(llmT) + ' model, ' + dur(toolT) + ' tools')),
          h('h2', null, 'Where the time went'),
          table([{ label: 'Step', key: 'label' }, { label: 'Time', cls: 'r', fmt: r => r.duration_s != null ? dur(r.duration_s) : '' }, { label: '', cls: 'vz-cell', fmt: r => h('div', { class: 'vz-track' }, h('i', { class: r.kind === 'llm' ? 'llm' : 'tool', style: 'width:' + Math.max(1, 100 * (r.duration_s || 0) / maxd) + '%' + (r.cache_miss_cause && r.kind === 'llm' && r.cache_hit_pct === 0 ? ';background:var(--warn)' : '') })) },
            { label: 'In', cls: 'r', fmt: r => r.tokens_in != null ? tok(r.tokens_in) : '' }, { label: 'Out', cls: 'r', fmt: r => r.tokens_out != null ? tok(r.tokens_out) : '' }, { label: 'Cost', cls: 'r', fmt: r => r.cost != null ? usd(r.cost) : '' }, { label: 'Cache hit', cls: 'r', fmt: r => r.cache_hit_pct != null ? r.cache_hit_pct.toFixed(0) + '%' : '' }, { label: 'Cache miss cause', cls: 't', key: 'cache_miss_cause' }], tl.rows));
      } else if (tab === 'ctxmap') {
        const kinds = {}; d.items.forEach(i => { kinds[i.kind] = (kinds[i.kind] || 0) + i.tokens; });
        const ranked = Object.entries(kinds).sort((a, b) => b[1] - a[1]); const shown = ranked.slice(0, 5).map(([k, v]) => ({ name: k, value: v, text: tok(v) }));
        if (ranked.length > 5) { const rest = ranked.slice(5).reduce((a, x) => a + x[1], 0); shown.push({ name: 'other', value: rest, text: tok(rest) }); }
        body.append(h('div', { class: 'vz-stats' }, vzStat('In use', Math.round(100 * d.total / d.window) + '%', tok(d.total) + ' of ' + tok(d.window) + ' tokens'), vzStat('Compaction starts at', tok(d.threshold), 'tokens in the window')),
          h('h2', null, 'What fills the window'),
          vzSegments(shown, { total: Math.max(d.window, d.total), tall: true, label: 'The context window' }),
          table([{ label: 'What', cls: 't', fmt: r => r.label }, { label: 'Kind', key: 'kind' }, { label: 'Tokens', cls: 'r', fmt: r => tok(r.tokens) }, { label: 'Share', cls: 'vz-cell', fmt: r => h('div', { class: 'vz-track' }, h('i', { style: 'width:' + Math.max(1, 100 * r.share) + '%' })) },
            { label: '', fmt: r => h('div', { class: 'formrow', style: 'margin:0' }, r.pinned ? h('span', { class: 'muted' }, 'pinned') : null, r.droppable ? h('button', { class: 'mini', title: 'Replace with a short stub; the full text is saved to disk', onclick: () => post('ctxmap_drop', { id: r.id }) }, 'Drop') : null, (r.droppable && !r.pinned) ? h('button', { class: 'mini', onclick: () => post('pin', { target: r.id }) }, 'Pin') : null, r.droppable ? null : h('span', { class: 'muted', title: r.why }, 'fixed')) }], d.items));
      } else if (tab === 'pins') {
        const t = h('input', { type: 'text', placeholder: 'A file path or a tool result id', 'aria-label': 'What to pin' });
        body.append(h('p', null, 'Pinned results stay in the context window when it is compacted or trimmed. ', h('b', null, tok(d.total_tokens)), ' pinned.'), h('div', { class: 'formrow' }, t, h('button', { class: 'btn primary', onclick: () => t.value.trim() && post('pin', { target: t.value.trim() }) }, 'Pin'), h('button', { class: 'btn', onclick: () => post('unpin', { target: '' }) }, 'Unpin all')),
          d.pins.length ? table([{ label: 'Pinned', key: 'label' }, { label: 'Tokens', cls: 'r', fmt: r => tok(r.tokens) }, { label: 'State', key: 'state' }, { label: '', fmt: r => h('button', { class: 'mini', onclick: () => post('unpin', { target: r.id }) }, 'Unpin') }], d.pins) : h('p', { class: 'muted' }, 'Nothing is pinned.'),
          d.recent.length ? h('div', null, h('h2', { style: 'margin-top:16px' }, 'Recent results you could pin'), ...d.recent.map(r => h('div', { class: 'row', style: 'display:flex;gap:10px;padding:3px 0' }, h('span', { style: 'flex:1' }, r.label), h('button', { class: 'mini', onclick: () => post('pin', { target: r.id }) }, 'Pin')))) : null);
      } else if (tab === 'whatif') {
        if (d.empty) { body.append(h('p', { class: 'muted' }, 'No model calls yet, so there is nothing to reprice.')); return; }
        const priced = d.rows.filter(r => r.priced);
        body.append(h('p', null, 'This session used ', h('b', null, tok(d.input)), ' input and ', h('b', null, tok(d.output)), ' output tokens over ' + d.calls + ' calls' + (d.actual_known ? ' and cost ' : '') + (d.actual_known ? usd(d.actual) : '') + '. What the same tokens would cost on other models:'),
          priced.length ? vzRanked(priced.slice().sort((a, b) => b.with_cache - a.with_cache).slice(0, 10).map(r => ({ name: r.model + (r.used ? ' (used here)' : ''), value: r.with_cache, text: usd(r.with_cache), hi: !!r.used }))) : null,
          table([{ label: 'Model', key: 'model' }, { label: 'With cache', cls: 'r', fmt: r => r.priced ? usd(r.with_cache) : 'no price' }, { label: 'Without cache', cls: 'r', fmt: r => r.priced ? usd(r.without_cache) : '' }, { label: 'Against actual', cls: 'r', fmt: r => r.priced && r.vs_actual_pct != null ? (r.vs_actual_pct > 0 ? '+' : '') + r.vs_actual_pct + '%' : '' }, { label: '', fmt: r => r.used ? 'used here' : '' }], d.rows));
      } else if (tab === 'ctxdiff') {
        body.append(d.ok && (d.lines || []).length ? h('pre', { class: 'out' }, d.lines.join('\n')) : notice(d.message || 'Nothing to compare yet: it needs two model calls.', d.ok ? 'info' : 'warn'));
      } else if (tab === 'receipts') {
        const okN = (d.claims || []).filter(c => c.supported).length;
        if (d.answer_found && d.claims.length) body.append(h('div', { class: 'vz-stats' }, vzStat('Claims with a tool call behind them', okN + ' of ' + d.claims.length, null), h('div', { class: 'vz-stat wide' }, vzMeter(okN / d.claims.length, okN === d.claims.length ? '' : 'warn', 'Supported claims'))));
        body.append(!d.answer_found ? h('p', { class: 'muted' }, 'No finished answer to check yet.') : (d.claims.length ? table([{ label: 'Claim in the last answer', cls: 't', key: 'claim' }, { label: 'Supported', fmt: r => r.supported ? h('span', { class: 'good' }, 'yes') : h('span', { class: 'bad' }, 'no') }, { label: 'Behind it', cls: 't', key: 'call' }, { label: 'Note', cls: 't', key: 'note' }], d.claims) : h('p', { class: 'muted' }, 'The last answer made no claims that can be checked against a tool call.')), h('p', { class: 'muted' }, 'A heuristic: it matches claims about files and commands to the tool calls of that turn.'));
      } else if (tab === 'compaction') {
        if (!d.receipt) body.append(h('p', { class: 'muted' }, 'This conversation has not been compacted.'));
        else {
          const r = d.receipt; const rep = Array.isArray(r.replaced) ? r.replaced : [];
          body.append(kvTable({ 'When': r.t ? new Date(r.t * 1000).toLocaleString() : '', 'Started by': r.trigger === 'manual' ? 'you (/compact)' : (r.trigger === 'auto' ? 'automatic compaction' : (r.trigger || '')),
            'Messages': r.messages_before + ' to ' + r.messages_after, 'Tokens (estimated)': (r.tokens_before || 0).toLocaleString() + ' to ' + (r.tokens_after || 0).toLocaleString() }));
          if (r.summary) body.append(h('h3', null, 'Summary kept in place of the old messages'), h('pre', { class: 'out' }, String(r.summary)));
          if (rep.length) body.append(h('h3', null, 'Messages that were replaced (' + rep.length + ')'), table([{ label: '#', key: 'index' }, { label: 'What', cls: 't', key: 'label' }, { label: 'Tokens', key: 'tokens' }], rep));
        }
      }
    } catch (e) { body.append(h('div', { class: 'notice warn' }, e.message)); }
  }, { key: 'insights:' + tab, hash: '#/insights/' + tab, title: 'Insights' });
}
"""
