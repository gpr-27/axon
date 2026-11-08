"""Client script and CSS for command cards: the card frame and its blocks, the composer's slash popup and the
Commands page rows. Everything is built with text nodes; the only innerHTML is the server-escaped ANSI output of a
command that has no structured card (it appears inside the same frame)."""

JS_CMDCARDS = r"""
// ---------- command cards ----------
const CC_MARK = { ok: '✓', bad: '✕', warn: '!', run: '▶', off: '○', info: '•' };
function ccName(line) { return String(line || '').trim().split(/\s+/)[0] || ''; }
async function ccQuery(line, session, opts) {
  const q = Object.entries(opts || {}).map(([k, v]) => '&o.' + encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('');
  return (await api('/api/cmdcard?line=' + encodeURIComponent(line) + '&session=' + encodeURIComponent(session || '') + q)).card;
}
async function ccRunLine(line, session) {
  let r = await runSlash(line, session);
  for (let i = 0; r.done === false && i < 400; i++) { await new Promise(res => setTimeout(res, 700)); const j = await api('/api/command/job?id=' + encodeURIComponent(r.id)); r = Object.assign({}, r, j, { done: j.done }); }
  return r;
}
function ccCell(v) {
  if (Array.isArray(v)) return h('span', { class: 'cc-t-' + (v[1] || 'none') }, String(v[0]));
  return String(v == null ? '' : v);
}
function ccBlock(b, ctx) {
  switch (b.t) {
    case 'h': return h('div', { class: 'cc-h' }, h('b', null, b.text), b.note ? h('span', null, b.note) : null);
    case 'text': return h('p', { class: 'cc-p ' + (b.tone || '') }, b.text);
    case 'kv': return h('dl', { class: 'cc-kv' }, b.rows.map(r => [h('dt', null, r.k), h('dd', { class: r.tone || '' }, r.v)]));
    case 'table': {
      if (!b.rows.length) return h('p', { class: 'cc-p muted' }, b.empty || 'Nothing here.');
      const rows = b.rows.map(r => h('tr', null, r.map((v, i) => h(i === 0 ? 'th' : 'td', { class: (b.cols[i] && b.cols[i].r ? 'r' : '') + (i === 0 ? ' first' : ''), scope: i === 0 ? 'row' : null }, ccCell(v)))));
      const tb = h('tbody', null, rows);
      const wrap = h('div', { class: 'cc-tw' }, h('table', { class: 'cc-table' }, h('thead', null, h('tr', null, b.cols.map(c => h('th', { class: c.r ? 'r' : '' }, c.l)))), tb));
      const out = h('div', null, wrap);
      if (rows.length > 10) {
        rows.slice(10).forEach(r => { r.hidden = true; });
        const more = h('button', { type: 'button', class: 'cc-more' }, 'Show ' + (rows.length - 10) + ' more');
        more.addEventListener('click', () => { const open = more.dataset.open !== '1'; rows.slice(10).forEach(r => { r.hidden = !open; }); more.dataset.open = open ? '1' : ''; more.textContent = open ? 'Show fewer' : 'Show ' + (rows.length - 10) + ' more'; });
        out.append(more);
      }
      if (b.note) out.append(h('p', { class: 'cc-note' }, b.note));
      return out;
    }
    case 'bars': return h('div', { class: 'cc-bars' }, b.rows.map(r => {
      const pctW = r.frac == null ? 0 : Math.max(0, Math.min(1, r.frac)) * 100;
      return h('div', { class: 'cc-bar-row' },
        h('span', { class: 'cc-bl' }, r.label),
        r.frac == null ? h('span', { class: 'cc-bt none' }) : h('span', { class: 'cc-bt', role: 'img', 'aria-label': r.label + ' ' + (r.value || '') }, h('i', { class: r.tone || 'info', style: 'width:' + pctW.toFixed(1) + '%' })),
        h('span', { class: 'cc-bv' }, r.value || ''),
        r.note ? h('span', { class: 'cc-bn' }, r.note) : null);
    }));
    case 'list': {
      if (!b.rows.length) return h('p', { class: 'cc-p muted' }, b.empty || 'Nothing here.');
      const mk = (r) => { const body = [h('span', { class: 'cc-lt' }, r.text), r.detail ? h('span', { class: 'cc-ld' }, r.detail) : null, r.sub ? h('span', { class: 'cc-ls' }, r.sub) : null];
        return h('li', { class: 'cc-li' }, h('span', { class: 'cc-mk ' + (r.mark || 'info'), 'aria-hidden': 'true' }, CC_MARK[r.mark] || CC_MARK.info), r.href ? h('a', { class: 'cc-lb', href: r.href }, body) : h('span', { class: 'cc-lb' }, body)); };
      const lis = b.rows.map(mk);
      const ul = h('ul', { class: 'cc-list' }, lis);
      const out = h('div', null, ul);
      if (lis.length > 10) {
        lis.slice(10).forEach(li => { li.hidden = true; });
        const more = h('button', { type: 'button', class: 'cc-more' }, 'Show ' + (lis.length - 10) + ' more');
        more.addEventListener('click', () => { const open = more.dataset.open !== '1'; lis.slice(10).forEach(li => { li.hidden = !open; }); more.dataset.open = open ? '1' : ''; more.textContent = open ? 'Show fewer' : 'Show ' + (lis.length - 10) + ' more'; });
        out.append(more);
      }
      return out;
    }
    case 'stack': return h('div', { class: 'cc-stack' },
      h('div', { class: 'cc-strip', role: 'img', 'aria-label': b.label || 'Share of the window' }, b.parts.map(p => p.frac > 0 ? h('i', { class: 's' + (p.i % 6), style: 'width:' + (Math.max(p.frac, 0.004) * 100).toFixed(2) + '%', title: p.label + ' ' + p.value }) : null)),
      b.label ? h('div', { class: 'cc-sl' }, b.label) : null,
      h('div', { class: 'cc-legend' }, b.parts.map(p => h('span', null, h('i', { class: 'sw s' + (p.i % 6) }), p.label + ' ', h('b', null, p.value)))));
    case 'pick': return h('div', { class: 'cc-pick', role: 'radiogroup', 'aria-label': b.label },
      h('div', { class: 'cc-h' }, h('b', null, b.label)),
      b.options.map(o => h('button', { type: 'button', class: 'cc-opt' + (o.value === b.current ? ' on' : ''), role: 'radio', 'aria-checked': String(o.value === b.current),
        onclick: () => { if (o.value !== b.current) ctx.run(b.prefix + o.value, { refresh: true }); } },
        h('span', { class: 'cc-dot' }), h('span', { class: 'cc-on' }, o.label), o.hint ? h('span', { class: 'cc-oh' }, o.hint) : null)));
    case 'controls': return h('div', { class: 'cc-ctl' }, b.rows.map(r => {
      let inp;
      if (r.type === 'text') inp = h('code', { class: 'cc-cv' }, r.value || '-');
      else { inp = h('select', { 'aria-label': r.label }, (r.options || []).map(v => h('option', { value: v }, v))); inp.value = r.value; inp.addEventListener('change', () => ctx.run(r.line + inp.value, { refresh: true })); }
      return h('label', { class: 'cc-crow' }, h('span', null, r.label), inp);
    }));
  }
  return null;
}
function ccPlain(b) {
  switch (b.t) {
    case 'h': return '\n' + b.text + (b.note ? ' (' + b.note + ')' : '');
    case 'text': return b.text;
    case 'kv': { const w = Math.max(...b.rows.map(r => r.k.length)); return b.rows.map(r => r.k.padEnd(w) + '  ' + r.v).join('\n'); }
    case 'table': {
      if (!b.rows.length) return b.empty || '';
      const cell = (v) => String(Array.isArray(v) ? v[0] : v == null ? '' : v);
      const w = b.cols.map((c, i) => Math.max(c.l.length, ...b.rows.map(r => cell(r[i]).length)));
      const line = (cells) => cells.map((v, i) => b.cols[i].r ? v.padStart(w[i]) : v.padEnd(w[i])).join('  ').trimEnd();
      return [line(b.cols.map(c => c.l)), ...b.rows.map(r => line(r.map(cell)))].join('\n') + (b.note ? '\n' + b.note : '');
    }
    case 'bars': { const w = Math.max(...b.rows.map(r => r.label.length)); return b.rows.map(r => r.label.padEnd(w) + '  ' + (r.value || '') + (r.note ? '  ' + r.note : '')).join('\n'); }
    case 'list': return b.rows.length ? b.rows.map(r => '- ' + r.text + (r.detail ? ' (' + r.detail + ')' : '')).join('\n') : (b.empty || '');
    case 'stack': return (b.label || '') + '\n' + b.parts.map(p => p.label + ': ' + p.value).join('\n');
    case 'pick': return b.label + ': ' + b.options.map(o => (o.value === b.current ? '[' + o.label + ']' : o.label)).join(', ');
    case 'controls': return b.rows.map(r => r.label + ': ' + r.value).join('\n');
  }
  return '';
}
function ccText(card) {
  const head = [card.line ? card.line : '', card.title + (card.sub ? ' - ' + card.sub : '')].filter(Boolean).join('\n');
  const sel = (card.selects || []).map(s => s.label + ': ' + ((s.options.find(o => o[0] === s.value) || [])[1] || s.value)).join(', ');
  return [head, sel, ...(card.blocks || []).map(ccPlain), card.tip ? 'Tip: ' + card.tip : ''].filter(x => x !== '').join('\n').replace(/\n{3,}/g, '\n\n').trim();
}
// ctx: { C (chat or null), line, session, opts, emit(newCardNode), refresh() }
function ccFrame(title, sub, ctx) {
  const root = h('section', { class: 'cc', 'aria-label': title });
  const copy = h('button', { type: 'button', class: 'cc-ib', title: 'Copy this as text', onclick: () => copyText(root.ccText ? root.ccText() : root.textContent) }, 'Copy');
  const again = h('button', { type: 'button', class: 'cc-ib', title: 'Run it again', onclick: () => ctx.refresh() }, 'Refresh');
  const pin = ctx.C ? h('button', { type: 'button', class: 'cc-ib', title: 'Keep this at the top of the chat', onclick: () => ccPin(root, ctx.C, pin) }, 'Pin') : null;
  root.append(h('header', { class: 'cc-top' }, h('div', { class: 'cc-tt' }, h('h3', null, title), sub ? h('span', { class: 'cc-sub' }, sub) : null), h('div', { class: 'cc-tools' }, copy, again, pin)));
  return root;
}
function ccPin(root, C, btn) {
  let bar = C.feed.querySelector(':scope > .cc-pins');
  if (!root.classList.contains('pinned')) {
    if (!bar) { bar = h('div', { class: 'cc-pins' }); C.feed.prepend(bar); }
    root.ccHome = document.createComment('pinned'); root.replaceWith(root.ccHome); bar.append(root); root.classList.add('pinned'); btn.textContent = 'Unpin'; C.feed.scrollTop = 0;
  } else {
    if (root.ccHome && root.ccHome.parentNode) root.ccHome.replaceWith(root); else C.feed.append(root);
    root.classList.remove('pinned'); btn.textContent = 'Pin'; if (bar && !bar.children.length) bar.remove();
  }
}
function ccCollapse(root) {
  const body = root.querySelector('.cc-body'); if (!body || !body.scrollHeight) return;
  if (body.scrollHeight > 640 && !body.dataset.cc) {
    body.dataset.cc = '1'; body.classList.add('cut');
    const btn = h('button', { type: 'button', class: 'cc-more cc-all' }, 'Show all'); btn.addEventListener('click', () => { const cut = body.classList.toggle('cut'); btn.textContent = cut ? 'Show all' : 'Show less'; });
    body.after(btn);
  }
}
function ccStructured(card, ctx) {
  const root = ccFrame(card.title, card.sub, ctx);
  root.ccText = () => ccText(card);
  if (card.confirm) return ccConfirm(card, root, ctx);
  const note = h('div', { class: 'cc-result', hidden: true, role: 'status' });
  const sub = Object.assign({}, ctx, {
    run: async (line, o) => {
      const busy = h('span', { class: 'muted' }, 'Running ' + line + '...'); note.hidden = false; note.replaceChildren(busy);
      try {
        const r = await ccRunLine(line, ctx.session);
        if (r.switched) { toast(r.message || 'Switched session'); }
        if (r.open) { location.hash = r.open; return; }
        if (!(o && o.refresh) && (r.card || r.html)) { note.hidden = true; ctx.emit(ccFromResult(r, line, ctx)); return; }   // a view: its own card below
        note.replaceChildren(...ccResultNodes(r, line, ctx, true)); note.hidden = !note.childNodes.length;
        ctx.refresh(note);                                                                                                 // a change: redraw this card with the new state
      } catch (e) { note.replaceChildren(errorBox(e.message)); note.hidden = false; }
    } });
  if (card.selects && card.selects.length) root.append(h('div', { class: 'cc-sel' }, card.selects.map(s => { const sel = h('select', { 'aria-label': s.label }, s.options.map(o => h('option', { value: o[0] }, o[1]))); sel.value = s.value;
    sel.addEventListener('change', () => { ctx.opts = Object.assign({}, ctx.opts, { [s.key]: sel.value }); ctx.refresh(); }); return h('label', null, s.label + ' ', sel); })));
  const body = h('div', { class: 'cc-body' }, card.blocks.map(b => ccBlock(b, sub)));
  root.append(body, note);
  if (card.actions && card.actions.length) root.append(h('div', { class: 'cc-acts' }, card.actions.map(a => ccAction(a, sub, root))));
  if (card.tip) root.append(h('p', { class: 'cc-tip' }, h('b', null, 'Tip '), card.tip));
  typeof requestAnimationFrame === 'function' && requestAnimationFrame(() => ccCollapse(root));
  return root;
}
function ccAction(a, ctx, root) {
  if (a.href) return h('a', { class: 'btn', href: a.href }, a.label);
  const b = h('button', { type: 'button', class: 'btn' }, a.label);
  b.addEventListener('click', () => {
    if (!a.confirm) return ctx.run(a.line);
    const bar = h('div', { class: 'cc-ask', role: 'alertdialog', 'aria-label': 'Confirm ' + a.label }, h('span', null, a.confirm),
      h('button', { type: 'button', class: 'btn danger', onclick: () => { bar.remove(); b.hidden = false; ctx.run(a.line, { refresh: true }); } }, 'Yes, run ' + ccName(a.line)),
      h('button', { type: 'button', class: 'btn', onclick: () => { bar.remove(); b.hidden = false; } }, 'Cancel'));
    b.hidden = true; b.after(bar);
  });
  return b;
}
function ccConfirm(card, root, ctx) {
  root.classList.add('confirm');
  const out = h('div', { class: 'cc-body' });
  const yes = h('button', { type: 'button', class: 'btn danger' }, 'Yes, run ' + ccName(card.confirm.line));
  const no = h('button', { type: 'button', class: 'btn' }, 'Cancel');
  yes.addEventListener('click', async () => {
    yes.disabled = no.disabled = true;
    try { await api('/api/cmdcard/confirm', { line: card.confirm.line }); const r = await ccRunLine(card.confirm.line, ctx.session); root.replaceWith(ccFromResult(r, card.confirm.line, ctx)); document.dispatchEvent(new CustomEvent('axon-cmd-done', { detail: { line: card.confirm.line } })); }
    catch (e) { out.append(errorBox(e.message)); yes.disabled = no.disabled = false; }
  });
  no.addEventListener('click', () => { out.replaceChildren(h('p', { class: 'cc-p muted' }, 'Cancelled. Nothing was run.')); });
  out.append(notice(card.confirm.text, 'warn'), h('div', { class: 'cc-acts' }, yes, no));
  root.append(out); return root;
}
function ccResultNodes(r, line, ctx, brief) {
  const out = [];
  if (r.message) out.push(notice(r.message, r.ok === false ? 'warn' : 'ok'));
  if (r.needs_arg) out.push(h('p', { class: 'cc-p muted' }, 'This needs: ' + r.needs_arg));
  if (r.html) {
    const o = h('div', { class: 'cmd-out', html: r.html });
    const one = String(o.textContent || '').trim();
    if (brief && one && one.length < 300 && !one.includes('\n')) out.push(notice(one, r.ok === false ? 'warn' : 'ok'));   // a change: one line is enough
    else { decorate(o); out.push(o); }
  }
  return out;
}
// A result with no structured card: the terminal text inside the same frame, never raw escape codes.
function ccText2(r, line, ctx) {
  const root = ccFrame(ccName(line), r.ok === false ? 'did not finish' : (r.ms ? r.ms + ' ms' : ''), ctx);
  const body = h('div', { class: 'cc-body' }); root.append(body);
  root.ccText = () => (line + '\n' + (r.message ? r.message + '\n' : '') + (body.querySelector('.cmd-out') ? body.querySelector('.cmd-out').textContent : '')).trim();
  body.append(...ccResultNodes(r, line, ctx));
  if (!r.html && !r.message) body.append(h('p', { class: 'cc-p muted' }, 'Done. This command printed nothing.'));
  if (r.action) { const a = r.action; body.append(h('button', { type: 'button', class: 'btn primary', onclick: async () => { try { if (a.shell) { await api('/api/shell/run', { command: a.shell }); toast('Done'); } else if (a.line) ctx.emit(ccFromResult(await ccRunLine(a.line, ctx.session), a.line, ctx)); } catch (e) { toast(e.message); } } }, a.label)); }
  if (r.copy) body.append(h('div', { class: 'cc-acts' }, h('code', null, r.copy), h('button', { type: 'button', class: 'btn', onclick: () => copyText(r.copy) }, 'Copy address')));
  if (r.html) termHint(body, r);
  if (/^\/\S+$/.test(String(line).trim()) && r.html) ccChoices(root, line, ctx);
  typeof requestAnimationFrame === 'function' && requestAnimationFrame(() => ccCollapse(root));
  return root;
}
async function ccChoices(root, line, ctx) {
  let ch; try { ch = (await api('/api/cmdcard?choices=1&line=' + encodeURIComponent(line))).choices; } catch (e) { return; }
  if (!ch || !ch.length) return;
  const box = h('details', { class: 'cc-ways' }, h('summary', null, 'Other ways to run ' + ccName(line)));
  const ask = h('div', { class: 'cc-askrow', hidden: true });
  for (const c of ch) {
    const b = h('button', { type: 'button', class: 'btn', title: c.hint || '' }, c.label);
    b.addEventListener('click', () => {
      const go = (text) => ctx.emit(null, (c.line + ' ' + text).trim());
      if (c.ask) { const inp = h('input', { type: 'text', placeholder: c.ask.replace(/^\[|\]$/g, ''), 'aria-label': c.ask }); const run = h('button', { type: 'button', class: 'btn primary', onclick: () => { if (inp.value.trim() || c.ask[0] === '[') go(inp.value.trim()); } }, 'Run');
        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') run.click(); }); ask.hidden = false; ask.replaceChildren(h('label', null, c.label + ' ', inp), run); inp.focus(); }
      else go('');
    });
    box.append(b);
  }
  box.append(ask); root.append(box);
}
function ccFromResult(r, line, ctx) {
  const c2 = Object.assign({}, ctx, { line });
  c2.refresh = async (note) => {
    const old = c2.node;
    try {
      let n;
      if (r.card && !r.card.confirm) { const card = await ccQuery(line, ctx.session, c2.opts); n = card ? ccStructured(card, c2) : ccText2(await ccRunLine(line, ctx.session), line, c2); }
      else n = ccText2(await ccRunLine(line, ctx.session), line, c2);
      n.classList.toggle('pinned', old.classList.contains('pinned'));
      if (old.classList.contains('pinned')) { const pb = n.querySelector('.cc-tools .cc-ib:last-child'); if (pb) pb.textContent = 'Unpin'; n.ccHome = old.ccHome; }
      c2.node = n; old.replaceWith(n);
      if (note && note.childNodes.length) { const keep = h('div', { class: 'cc-result' }, ...note.childNodes); n.querySelector('.cc-body') && n.querySelector('.cc-body').after(keep); }
    } catch (e) { toast(e.message); }
  };
  c2.emit = (node, newLine) => ctx.emit(node, newLine);
  const node = r.card ? ccStructured(r.card, c2) : ccText2(r, line, c2);
  c2.node = node; return node;
}
// Chat: the command as a small right-aligned message, then the card. Returns true when it drew the result.
function cmdCardShow(C, line, r) {
  if (r.open || r.needs_arg || r.chat || r.terminal_only) return false;
  C.feed.querySelector('.empty-chat')?.remove();
  const said = h('div', { class: 'cc-said' }, h('span', null, line));
  const ctx = { C, session: C.sid, opts: {}, emit: (node, newLine) => {
    if (node) { C.feed.append(node); decorate(node); stick(C); return; }
    if (newLine) runCommandInChat(C, newLine);
  } };
  if (!r.done) {
    const wait = ccFrame(ccName(line), 'running...', ctx); wait.append(h('div', { class: 'cc-body' }, h('p', { class: 'cc-p muted' }, 'Running ' + line + '...')));
    C.feed.append(said, wait); stick(C);
    const poll = setInterval(async () => { try { const j = await api('/api/command/job?id=' + r.id); if (j.done) { clearInterval(poll); wait.replaceWith(ccFromResult(Object.assign({}, r, j, { done: true }), line, ctx)); stick(C); } } catch (e) { clearInterval(poll); } }, 800);
    onLeave(() => clearInterval(poll)); return true;
  }
  const node = ccFromResult(r, line, ctx);
  C.feed.append(said, node); decorate(node); stick(C);
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { if (node.scrollHeight > C.feed.clientHeight - 40) said.scrollIntoView({ block: 'start' }); });   // a tall card opens at its top
  return true;
}
async function runCommandInChat(C, line) {
  try { const r = await api('/api/chat/send', { session: C.sid, text: line, steer: true, cards: true }); if (r.command) { if (!cmdCardShow(C, line, r)) commandResultCard(C, line, r); if (r.state) { C.st = r.state; } } else if (!C.runtime) { C.runtime = true; refreshState(C); } }
  catch (e) { toast(e.message); }
}
// Commands page: the result goes into the panel the page gave us.
function cmdCardInto(r, into) {
  if (r.open || r.needs_arg || r.chat || r.terminal_only || r.action) return false;
  const line = r.line || '';
  const ctx = { C: null, session: '', opts: {}, emit: (node, newLine) => { if (node) into.replaceChildren(node); else if (newLine) execLine(newLine, into); } };
  into.replaceChildren(ccFromResult(r, line, ctx)); decorate(into); return true;
}

// ---------- composer: slash popup ----------
function cmdArgHint(c) {
  const first = String(c.usage || '').split('\n')[0].trim();
  const names = [c.name, ...(c.aliases || [])];
  for (const n of names) if (first.startsWith(n + ' ')) { const rest = first.slice(n.length).trim().split(/\s{2,}/)[0]; return rest.length > 34 ? rest.slice(0, 33) + '…' : rest; }
  return '';
}
function cmdScore(q, c) {
  if (!q) return 1;
  const name = c.name.slice(1).toLowerCase();
  if (name === q) return 1000;
  if (name.startsWith(q)) return 900 - name.length;
  if ((c.aliases || []).some(a => a.slice(1).toLowerCase().startsWith(q))) return 800;
  const at = name.indexOf(q); if (at >= 0) return 700 - at;
  if (c.description.toLowerCase().includes(q)) return 300;
  let j = 0, gaps = 0, last = -1;
  for (let i = 0; i < name.length && j < q.length; i++) if (name[i] === q[j]) { if (last >= 0 && i - last > 1) gaps++; last = i; j++; }
  return j === q.length ? 200 - gaps * 10 : 0;
}
function cmdRecent(hist) {
  const seen = []; for (let i = (hist || []).length - 1; i >= 0 && seen.length < 8; i--) { const m = /^(\/[\w:.-]+)/.exec(hist[i]); if (m && !seen.includes(m[1].toLowerCase())) seen.push(m[1].toLowerCase()); }
  return seen;
}
// Items for the popup: [{label, value, hint, arg, group, recent, alias}]
function cmdSuggest(cmds, first, hist) {
  const q = first.slice(1).toLowerCase(); const recent = cmdRecent(hist);
  const byName = new Map(cmds.map(c => [c.name.toLowerCase(), c]));
  const mk = (c, group) => ({ label: c.name, value: c.name, hint: c.terminal_only ? 'terminal only' : c.description, arg: cmdArgHint(c), group, recent: recent.includes(c.name.toLowerCase()) });
  if (!q) {
    const out = recent.map(n => byName.get(n)).filter(Boolean).slice(0, 5).map(c => mk(c, 'Recent'));
    const groups = new Map();
    for (const c of cmds) { if (recent.slice(0, 5).includes(c.name.toLowerCase())) continue; const g = c.kind === 'extension' ? 'Skills and custom' : (c.category || 'Other'); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
    for (const [g, list] of groups) for (const c of list.slice(0, 4)) out.push(mk(c, g));
    return out;
  }
  const scored = cmds.map(c => { const s0 = cmdScore(q, c); return [s0 > 0 ? s0 + (recent.includes(c.name.toLowerCase()) ? 60 : 0) : 0, c]; }).filter(x => x[0] > 0).sort((a, b) => b[0] - a[0] || a[1].name.localeCompare(b[1].name)).slice(0, 14);
  const groups = new Map();
  for (const [, c] of scored) { const g = c.kind === 'extension' ? 'Skills and custom' : (c.category || 'Other'); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
  const out = []; for (const [g, list] of groups) for (const c of list) { const it = mk(c, g); const a = (c.aliases || []).find(x => x.slice(1).toLowerCase().startsWith(q) && !c.name.slice(1).toLowerCase().startsWith(q)); if (a) it.alias = a; out.push(it); }
  return out;
}
function cmdPopNodes(list, accept) {
  const out = []; let g = null; let n = 0;
  list.forEach((it, i) => {
    if (it.group && it.group !== g) { g = it.group; out.push(h('div', { class: 'grp', role: 'presentation' }, g)); }
    out.push(h('div', { class: 'it', role: 'option', 'data-i': i, 'aria-selected': String(i === 0), onmousedown: (ev) => { ev.preventDefault(); accept(i); } },
      h('div', { class: 'l1' }, h('b', null, it.label), it.arg ? h('i', { class: 'arg' }, it.arg) : null, it.alias ? h('i', { class: 'arg' }, 'also ' + it.alias) : null, it.recent && it.group !== 'Recent' ? h('em', { class: 'rec' }, 'recent') : null),
      h('span', null, it.hint || '')));
    n++;
  });
  return out;
}
function cmdPopSelect(pop, sel) {
  for (const el of pop.querySelectorAll('.it')) { const on = Number(el.getAttribute('data-i')) === sel; el.setAttribute('aria-selected', String(on)); if (on && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' }); }
}

// ---------- Commands page rows ----------
function cmdCatalogRow(c, pick, run) {
  const hint = cmdArgHint(c);
  const main = h('button', { class: 'c', type: 'button', onclick: pick }, h('b', null, c.name, hint ? h('i', { class: 'arg' }, ' ' + hint) : null), c.terminal_only ? h('span', { class: 'bad' }, 'terminal only') : h('span', null, c.description));
  if (c.terminal_only) return main;
  return h('div', { class: 'cc-crow2' }, main, h('button', { class: 'cc-runb', type: 'button', title: 'Run ' + c.name, 'aria-label': 'Run ' + c.name, onclick: run }, 'Run'));
}
function cmdRecentSeed(groups, hit) {
  let hist = []; try { hist = JSON.parse(lsGet('axon-prompts') || '[]'); } catch (e) { hist = []; }
  const names = cmdRecent(hist).slice(0, 5); const rows = names.map(n => hit.find(c => c.name.toLowerCase() === n)).filter(Boolean);
  if (rows.length) groups.set('Recent', rows);
}
"""

CMDCARDS_CSS = r"""
.cc-said{display:flex;justify-content:flex-end;margin:14px 0 6px;max-width:880px}
.cc-said span{background:var(--raised2);border-radius:12px;padding:4px 12px;font:12.5px ui-monospace,Menlo,monospace;max-width:80%;overflow-wrap:anywhere}
.cc{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin:6px 0 14px;padding:0;max-width:880px;min-width:0;overflow:hidden}
.cc.pinned{margin:0 0 6px;max-height:42vh;overflow:auto;border-color:var(--primary)}
.cc-pins{position:sticky;top:0;z-index:3;background:var(--page);padding:4px 0}
.cc-top{display:flex;justify-content:space-between;align-items:center;gap:8px 12px;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line)}
.cc-tt{min-width:0;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.cc-tt h3{margin:0;font-size:15px;font-weight:650}
.cc-sub{color:var(--ink2);font-size:12.5px;overflow-wrap:anywhere}
.cc-tools{display:flex;gap:2px}
.cc-ib{background:none;border:0;border-radius:6px;padding:3px 9px;color:var(--ink2);cursor:pointer;font-size:12.5px}
.cc-ib:hover{background:var(--raised);color:var(--ink)}
.cc-sel{display:flex;flex-wrap:wrap;gap:8px 16px;padding:10px 14px 0;font-size:12.5px;color:var(--ink2)}
.cc-sel select,.cc-ctl select{background:var(--raised);border:1px solid var(--line);border-radius:7px;padding:4px 8px;color:var(--ink)}
.cc-body{padding:4px 14px 8px}
.cc-body.cut{max-height:560px;overflow:hidden;-webkit-mask-image:linear-gradient(rgb(0 0 0) 85%,transparent);mask-image:linear-gradient(rgb(0 0 0) 85%,transparent)}
.cc-more{display:block;margin:6px 14px 8px;background:none;border:0;color:var(--info);cursor:pointer;padding:2px 0;font-size:12.5px}
.cc-more:hover{text-decoration:underline}
.cc-h{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;margin:16px 0 6px}
.cc-h b{font-size:13.5px}.cc-h span{color:var(--muted);font-size:12px}
.cc-p{margin:8px 0;color:var(--ink)}.cc-p.muted,.cc-note,.cc-lb .cc-ls{color:var(--muted)}
.cc-p.warn{color:var(--warn)}.cc-note{font-size:12px;margin:6px 0}
.cc-kv{display:grid;grid-template-columns:minmax(110px,max-content) minmax(0,1fr);gap:5px 18px;margin:8px 0;font-size:13px}
.cc-kv dt{color:var(--ink2)}.cc-kv dd{margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.cc-kv dd.ok,.cc-t-ok{color:var(--ok)}.cc-kv dd.warn,.cc-t-warn{color:var(--warn)}.cc-kv dd.bad,.cc-t-bad{color:var(--bad)}.cc-kv dd.muted,.cc-t-muted{color:var(--muted)}.cc-kv dd.info{color:var(--info)}
.cc-tw{overflow-x:auto}
.cc-table{width:100%;border-collapse:collapse;font-size:13px}
.cc-table th,.cc-table td{padding:6px 10px 6px 0;text-align:left;border-bottom:1px solid color-mix(in srgb,var(--line) 50%,transparent);vertical-align:top}
.cc-table thead th{color:var(--ink2);font-weight:500;white-space:nowrap}
.cc-table th.first{font-weight:500;overflow-wrap:anywhere}
.cc-table .r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;padding-right:0;padding-left:10px}
.cc-table tr:last-child td,.cc-table tr:last-child th{border-bottom:0}
.cc-bars{display:grid;gap:10px;margin:8px 0}
.cc-bar-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;align-items:baseline;font-size:13px}
.cc-bar-row .cc-bt{grid-column:1 / -1;order:3}
.cc-bt{height:8px;background:var(--grid);border-radius:4px;overflow:hidden;display:block}.cc-bt.none{background:none}
.cc-bt i{display:block;height:100%;border-radius:4px;background:var(--primary)}
.cc-bt i.ok{background:var(--ok)}.cc-bt i.warn{background:var(--warn)}.cc-bt i.bad{background:var(--bad)}.cc-bt i.info{background:var(--info)}
.cc-bv{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cc-bn{grid-column:1 / -1;order:4;color:var(--muted);font-size:12px}
.cc-bl{overflow-wrap:anywhere}
.cc-list{list-style:none;margin:6px 0;padding:0}
.cc-li{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;padding:6px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 40%,transparent)}
.cc-li:last-child{border-bottom:0}
.cc-lb{display:flex;flex-direction:column;min-width:0;color:inherit;text-decoration:none}.cc-lb:hover .cc-lt{text-decoration:underline}
.cc-lt{overflow-wrap:anywhere}.cc-ld{font-size:12.5px;color:var(--ink2)}.cc-ls{font-size:12px;overflow-wrap:anywhere}
.cc-mk{font-size:12px;text-align:center;color:var(--muted)}
.cc-mk.ok{color:var(--ok)}.cc-mk.bad{color:var(--bad)}.cc-mk.warn{color:var(--warn)}.cc-mk.run{color:var(--info)}.cc-mk.info{color:var(--info)}
.cc-strip{display:flex;height:12px;background:var(--grid);border-radius:6px;overflow:hidden;gap:1px;margin:10px 0 6px}
.cc-strip i{display:block;height:100%}
.s0{background:var(--primary)}.s1{background:var(--accent)}.s2{background:var(--info)}.s3{background:var(--warn)}.s4{background:var(--ok)}.s5{background:var(--muted)}
.cc-sl{font-size:13px;margin-bottom:6px}
.cc-legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--ink2)}
.cc-legend .sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}.cc-legend b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.cc-pick{margin:8px 0}
.cc-opt{display:grid;grid-template-columns:18px auto minmax(0,1fr);align-items:baseline;gap:8px;width:100%;text-align:left;background:none;border:0;border-radius:8px;padding:7px 8px;cursor:pointer;color:var(--ink)}
.cc-opt:hover{background:var(--raised)}.cc-opt.on{background:var(--raised)}
.cc-dot{width:10px;height:10px;border-radius:50%;border:2px solid var(--muted);align-self:center}
.cc-opt.on .cc-dot{border-color:var(--primary);background:var(--primary)}
.cc-on{font-family:ui-monospace,Menlo,monospace;font-size:13px;overflow-wrap:anywhere}.cc-oh{color:var(--muted);font-size:12.5px;overflow-wrap:anywhere}
.cc-ctl{display:grid;gap:4px;margin:8px 0}
.cc-crow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:5px 0;border-bottom:1px solid color-mix(in srgb,var(--line) 40%,transparent)}
.cc-cv{overflow-wrap:anywhere}
.cc-acts{display:flex;flex-wrap:wrap;gap:8px;padding:8px 14px 12px;align-items:center}
.cc-ask{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-left:3px solid var(--warn);background:var(--raised);border-radius:8px;padding:8px 12px;font-size:13px}
.cc-result{padding:0 14px 8px}.cc-result .notice{margin:4px 0}
.cc-tip{margin:0;padding:10px 14px;border-top:1px solid var(--line);color:var(--ink2);font-size:13px;background:var(--raised)}
.cc-tip b{color:var(--ink)}
.cc-ways{padding:6px 14px 12px;font-size:13px}.cc-ways summary{cursor:pointer;color:var(--ink2);margin-bottom:6px}.cc-ways .btn{margin:0 6px 6px 0}
.cc-askrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}.cc-askrow input{flex:1 1 220px;min-width:0}
.cc.confirm{border-color:var(--warn)}
.cc .cmd-out{margin:8px 0;max-height:60vh}
.cmd-panel>.cc{border:0;border-radius:0;margin:0;max-width:none}
.pop .grp{padding:6px 12px 2px;font-size:11.5px;color:var(--muted);letter-spacing:.02em;position:sticky;top:0;background:inherit}
.pop .it .arg{font-style:normal;color:var(--info);font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.pop .it .rec{font-style:normal;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 5px;margin-left:auto}
.pop{max-height:340px}
.pop .it{display:block;padding:6px 12px}.pop .it .l1{display:flex;gap:4px 10px;align-items:baseline;flex-wrap:wrap}
.pop .it b{min-width:0}.pop .it>span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
.cc-crow2{display:flex;align-items:center;gap:4px}.cc-crow2 button.c{flex:1;min-width:0}
.cc-runb{background:var(--raised);border:0;border-radius:7px;padding:4px 10px;cursor:pointer;color:var(--ink2);font-size:12px;margin-right:4px}
.cc-runb:hover{background:var(--raised2);color:var(--ink)}
.cmd-list .c .arg{font-style:normal;color:var(--info);font-weight:400;font-size:12px}
.cmd-grid{grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr))}.cmd-right{min-width:0}
@media (max-width:560px){
 .cc-kv{grid-template-columns:minmax(0,1fr)}.cc-kv dt{margin-top:6px}
 .cc-opt{grid-template-columns:18px minmax(0,1fr)}.cc-oh{grid-column:2}
 .cc-said span{max-width:92%}
}
"""
