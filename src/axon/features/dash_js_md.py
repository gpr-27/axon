"""A small, safe Markdown formatter for text as it streams, so a reply looks finished while it is still arriving.
Everything is escaped first; only http(s) links become links."""

JS_MD = r"""
// ---------- live markdown ----------
function mdEsc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function mdInline(raw) {
  const codes = [];
  let s = mdEsc(raw).replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, u) => '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*([^*\s][^*\n]*)\*/g, '$1<em>$2</em>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + codes[+i] + '</code>');
}
function mdLite(src, live) {
  const lines = String(src).replace(/\u0000/g, '').split('\n'); const out = []; let i = 0;
  const isTableRule = l => /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/.test(l) && l.includes('-') && l.includes('|');
  if (live) mdLiveTable(lines, isTableRule);
  const cells = l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  while (i < lines.length) {
    const l = lines[i]; let m;
    if ((m = l.match(/^\s*(```+|~~~+)\s*(\S*)/))) {
      const fence = m[1]; const code = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) code.push(lines[i++]);
      i++; out.push('<pre><code>' + mdEsc(code.join('\n')) + '</code></pre>'); continue;
    }
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { const n = Math.min(m[1].length + 1, 5); out.push('<h' + n + '>' + mdInline(m[2]) + '</h' + n + '>'); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { out.push('<hr>'); i++; continue; }
    if (l.includes('|') && i + 1 < lines.length && isTableRule(lines[i + 1])) {
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push('<table><thead><tr>' + head.map(c => '<th>' + mdInline(c) + '</th>').join('') + '</tr></thead><tbody>' + rows.map(r => '<tr>' + r.map(c => '<td>' + mdInline(c) + '</td>').join('') + '</tr>').join('') + '</tbody></table>'); continue;
    }
    if (/^\s*>/.test(l)) { const q = []; while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, '')); out.push('<blockquote>' + mdInline(q.join(' ')) + '</blockquote>'); continue; }
    if ((m = l.match(/^\s*([-*+]|\d+[.)])\s+/))) {
      const LI = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/; const items = [];      // every list line, with its indent: deeper lines nest
      while (i < lines.length && LI.test(lines[i])) { const x = lines[i++].match(LI); items.push({ ind: x[1].replace(/\t/g, '    ').length, ord: /\d/.test(x[2]), t: x[3] }); }
      const stack = []; let html = '';
      const closeTo = (ind) => { while (stack.length && stack[stack.length - 1].ind > ind) { html += '</li></' + (stack.pop().ord ? 'ol' : 'ul') + '>'; } };
      for (const it of items) {
        closeTo(it.ind);
        const top = stack[stack.length - 1];
        if (!top || top.ind < it.ind) { html += (top ? '' : '') + '<' + (it.ord ? 'ol' : 'ul') + '>'; stack.push({ ind: it.ind, ord: it.ord }); }
        else html += '</li>';
        html += '<li>' + mdInline(it.t);
      }
      closeTo(-1);
      out.push(html); continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    out.push('<p>' + mdInline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
  }
  return out.join('');
}
// A table that is still arriving shows as a table from its first row, so it never flips from raw pipes to a grid:
// the row being typed gets the rule line it is about to receive.
function mdLiveTable(lines, isTableRule) {
  const n = lines.length; if (!n) return;
  let fence = ''; for (const l of lines) { const m = l.match(/^\s*(```+|~~~+)/); if (fence) { if (l.trim().startsWith(fence)) fence = ''; } else if (m) fence = m[1]; }
  if (fence) return;
  const cand = l => !!l.trim() && !isTableRule(l) && (/^\s*\|.*[^|\s]/.test(l) || l.split('|').length > 2);
  const rule = l => '|' + l.trim().replace(/^\||\|$/g, '').split('|').map(() => '---').join('|') + '|';
  const last = lines[n - 1], prev = n > 1 ? lines[n - 2] : '', before = n > 2 ? lines[n - 3] : '';
  if (cand(prev) && !before.trim() && (!last.trim() || (/^[\s|:-]*$/.test(last) && /[|-]/.test(last) && !isTableRule(last)))) lines[n - 1] = rule(prev);
  else if (cand(last) && !prev.trim()) lines.push(rule(last));
}
// ---------- paced, incremental live reply ----------
// A reply is cut at blank lines outside code fences. Finished blocks are rendered once and kept; only the open
// last block is rendered again, so the work per frame stays small however long the reply gets. Text is released
// by requestAnimationFrame at a steady rate that catches up quickly, so bursts read smoothly.
const LIVE_TAU = 0.09, LIVE_MAX_LAG = 0.25;
function liveReduced() { try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { return false; } }
function liveScan(st, text) {
  let p = st.pos;
  for (;;) {
    const nl = text.indexOf('\n', p); if (nl < 0) break;
    const line = text.slice(p, nl);
    if (st.fence) { if (line.trim().startsWith(st.fence)) st.fence = ''; }
    else { const m = line.match(/^\s*(```+|~~~+)\s*(\S*)/); if (m) st.fence = m[1]; else if (!line.trim()) st.cut = nl + 1; }
    p = nl + 1;
  }
  st.pos = p;
}
function liveDraw(cur, text, final) {
  const el = cur.prose, st = cur.mdst || (cur.mdst = { pos: 0, fence: '', cut: 0, done: 0, nodes: 0, tail: '' });
  liveScan(st, text);
  const drop = () => { while (el.childNodes.length > st.nodes) el.removeChild(el.lastChild); };
  if (st.cut > st.done) {
    drop(); el.insertAdjacentHTML('beforeend', mdLite(text.slice(st.done, st.cut)));
    st.nodes = el.childNodes.length; st.done = st.cut; st.tail = '';
  }
  const html = mdLite(text.slice(st.done), !final);
  if (html !== st.tail || el.childNodes.length < st.nodes) { drop(); el.insertAdjacentHTML('beforeend', html); st.tail = html; }
}
function liveStep(C, cur, now) {
  cur.liveRaf = 0;
  const L = cur.live, total = cur.proseText.length; if (!L || !cur.prose) return;
  const dt = Math.min(0.1, Math.max(0.001, (now - L.t) / 1000)); L.t = now;
  let back = total - L.shown;
  if (back > 0) {
    let n = Math.max(Math.ceil(back * (1 - Math.exp(-dt / LIVE_TAU))), Math.ceil(back * dt / LIVE_MAX_LAG), 1);
    if (n > back) n = back;
    if (n < back && /[\ud800-\udbff]/.test(cur.proseText[L.shown + n - 1] || '')) n++;      // never cut a surrogate pair
    L.shown += n;
  }
  liveDraw(cur, cur.proseText.slice(0, L.shown), false);
  stick(C);
  if (L.shown < total) cur.liveRaf = requestAnimationFrame(t => liveStep(C, cur, t));
}
function liveText(C, cur) {
  if (cur.proseText.length > 300000) { cur.prose.style.whiteSpace = 'pre-wrap'; cur.prose.textContent = cur.proseText; stick(C); return; }
  const L = cur.live || (cur.live = { shown: 0, t: performance.now() }); L.done = false;
  if (C.replaying || liveReduced() || document.hidden || typeof requestAnimationFrame !== 'function') {
    if (!cur.mdTimer) cur.mdTimer = setTimeout(() => { cur.mdTimer = 0; liveFlush(C, cur); }, 40);
    return;
  }
  if (!cur.liveRaf) { L.t = performance.now(); cur.liveRaf = requestAnimationFrame(t => liveStep(C, cur, t)); }
}
// Show everything now: at the end of the turn, on Stop, before a tool card, in reduced motion, in a hidden tab.
function liveFlush(C, cur) {
  if (!cur || !cur.live || !cur.prose || cur.live.done) return;
  if (cur.liveRaf) { cancelAnimationFrame(cur.liveRaf); cur.liveRaf = 0; }
  if (cur.mdTimer) { clearTimeout(cur.mdTimer); cur.mdTimer = 0; }
  if (cur.proseText.length > 300000) return;
  cur.live.shown = cur.proseText.length; cur.live.done = true;
  liveDraw(cur, cur.proseText, true);
  if (C) stick(C);
}
// ---------- follow the bottom only while the reader is there ----------
function followWire(C, feed) {
  C.lastTop = 0;
  feed.addEventListener('scroll', () => {
    const top = feed.scrollTop, gap = feed.scrollHeight - top - feed.clientHeight;
    if (gap < 24 || (gap < 80 && top > C.lastTop)) { C.atBottom = true; followJump(C, false); }
    else if (top < C.lastTop - 1) C.atBottom = false;           // the reader moved up: stop following
    C.lastTop = top;
  });
  feed.addEventListener('wheel', e => { if (e.deltaY < 0 && feed.scrollTop > 0) C.atBottom = false; }, { passive: true });   // at the very top nothing moves, so nothing is left behind
  feed.addEventListener('touchmove', () => { if (feed.scrollHeight - feed.scrollTop - feed.clientHeight > 24) C.atBottom = false; }, { passive: true });
}
function followStick(C) {
  const feed = C.feed; if (!feed) return;
  if (C.atBottom) { feed.scrollTop = feed.scrollHeight; C.lastTop = feed.scrollTop; }
  else if (feed.scrollHeight - feed.scrollTop - feed.clientHeight > 80) followJump(C, true);
}
function followJump(C, show) {
  const feed = C.feed; if (!feed || !feed.parentNode) return;
  if (!C.jump) {
    if (!show) return;
    const b = document.createElement('button'); b.type = 'button'; b.className = 'jump-latest'; b.textContent = 'Jump to latest'; b.hidden = true;
    b.addEventListener('click', () => { C.atBottom = true; feed.scrollTop = feed.scrollHeight; C.lastTop = feed.scrollTop; followJump(C, false); });
    const par = feed.parentNode; if (getComputedStyle(par).position === 'static') par.style.position = 'relative';
    par.append(b); C.jump = b;
  }
  if (show) C.jump.style.bottom = Math.max(8, C.jump.parentNode.clientHeight - feed.offsetTop - feed.offsetHeight + 12) + 'px';
  C.jump.hidden = !show;
}
function turnWord(reason) { return ({ end_turn: 'Done', tool_use: 'Done', stop_sequence: 'Done', max_tokens: 'Stopped at the length limit', refusal: 'The model declined', interrupted: 'Stopped', declined: 'Stopped at your No', budget_exceeded: 'Stopped at the budget', error: 'Failed', model_context_window_exceeded: 'Context full' }[reason]) || reason || 'Done'; }
"""
