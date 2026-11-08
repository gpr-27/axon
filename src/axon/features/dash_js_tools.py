"""Client script, part 4: Shell, Commands (palette and forms), Memory, Settings, Subagents, help, router and boot."""

JS_TOOLS = r"""
// ---------- shell ----------
// The dock is built once; after Change folder its header and hint must name the new folder.
function shellFolderChanged() {
  const ws = state.ws || {};
  const c = document.getElementById('shcwd'); if (c) c.textContent = 'in ' + (ws.workspace || '');
  const i = document.getElementById('shinput'); if (i) i.placeholder = 'Run a command in ' + (ws.name || 'the project') + '  (Enter)';
}
function mountShell(main) {
  const S = { closed: false, hist: [], hpos: -1 };
  (async () => {
    onLeave(() => { S.closed = true; });
    const out = h('div', { class: 'sh-out', id: 'shout' });
    const input = h('input', { type: 'text', id: 'shinput', placeholder: 'Run a command in ' + ((state.ws || {}).name || 'the project') + '  (Enter)', 'aria-label': 'Shell command', autocomplete: 'off', spellcheck: 'false' });
    const run = h('button', { class: 'btn primary' }, 'Run');
    const hist = h('aside', { class: 'sh-hist' });
    main.append(h('div', { class: 'sh-side' }, h('div', { class: 'shell' },
      h('div', { class: 'ftoolbar' }, h('b', null, 'Shell'), h('span', { class: 'muted', id: 'shcwd' }, 'in ' + ((state.ws || {}).workspace || '')), h('span', { class: 'sp', style: 'flex:1' }), h('span', { class: 'muted', title: 'Each command runs on its own and streams line by line.' }, 'Line-based, not a full terminal: vim, top and other full-screen programs do not work.'), h('button', { class: 'btn', onclick: () => out.replaceChildren() }, 'Clear')),
      out, h('div', { class: 'sh-in' }, h('span', { class: 'sh-cmd', style: 'align-self:center;font-family:ui-monospace,monospace' }, '$'), input, run)), hist));
    const jobs = {};
    function drawJob(j) {
      const card = h('div', { class: 'sh-job' }); const meta = h('div', { class: 'meta' }, 'starting...'); const body = h('div'); const kill = h('button', { class: 'mini danger', hidden: true }, 'Kill');
      card.append(h('div', null, h('span', { class: 'sh-cmd' }, '$ ' + j.command), ' ', kill), meta, body); out.append(card); out.scrollTop = out.scrollHeight;
      kill.addEventListener('click', () => api('/api/shell/kill', { id: j.id }).catch(e => toast(e.message)));
      jobs[j.id] = { card, meta, body, kill, bytes: 0 };
      const es = new EventSource('/api/shell/stream?id=' + j.id + '&since=0'); onLeave(() => es.close());
      let pre = null, preErr = null;
      // Output is added in batches (one layout per 60 ms, not one per chunk): a command that prints megabytes would otherwise
      // freeze the page for minutes, laying out the whole growing block again for every 32 KB piece.
      const flushOut = (jb) => {
        jb.flushT = 0; const q = jb.q || []; jb.q = []; if (!q.length) return;
        for (const ev of q) {
          const stream = ev.stream; let p = stream === 'err' ? preErr : pre;
          if (!p) { p = h('pre', { class: stream === 'err' ? 'err nolink' : 'nolink' }); jb.body.append(p); if (stream === 'err') preErr = p; else pre = p; }
          const s = document.createElement('span'); s.innerHTML = ev.html; p.append(s); jb.bytes += s.textContent.length;
          while (jb.bytes > 300000 && p.firstChild && p.childNodes.length > 1) { jb.bytes -= (p.firstChild.textContent || '').length; p.removeChild(p.firstChild); jb.trim = true; }
        }
        if (jb.trim && !jb.trimNote) { jb.trimNote = true; jb.meta.append(' (older output trimmed to keep the page fast)'); }
        if (out.scrollHeight - out.scrollTop - out.clientHeight < 200) out.scrollTop = out.scrollHeight;
      };
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data); const jb = jobs[j.id];
        if (ev.type === 'start') { jb.meta.textContent = 'pid ' + ev.pid + (ev.sandbox ? ' · sandboxed' : '') + ' · running'; jb.kill.hidden = false; }
        else if (ev.type === 'out') { (jb.q = jb.q || []).push(ev); if (!jb.flushT) jb.flushT = setTimeout(() => flushOut(jb), 60); }
        else if (ev.type === 'refused') { jb.body.append(h('div', { class: 'refused' }, 'Not run: ' + ev.message)); }
        else if (ev.type === 'note') { clearTimeout(jb.flushT); flushOut(jb); jb.body.append(notice(ev.message, 'warn')); }
        else if (ev.type === 'exit') { es.close(); clearTimeout(jb.flushT); flushOut(jb); jb.kill.hidden = true; if (ev.cwd) { const c = document.getElementById('shcwd'); if (c) c.textContent = 'in ' + ev.cwd; } jb.meta.replaceChildren(ev.refused ? 'refused' : h('span', { class: ev.code === 0 ? 'good' : 'bad' }, 'exit ' + (ev.killed ? 'killed' : ev.code)), ev.duration != null ? ' · ' + dur(ev.duration) : ''); loadHist(); decorate(jb.body); }
      };
    }
    async function go() {
      const cmd = input.value.trim(); if (!cmd) return; S.hist.push(cmd); S.hpos = -1; input.value = '';
      try { const j = await api('/api/shell/run', { command: cmd }); drawJob(j); } catch (e) { out.append(h('div', { class: 'sh-job' }, h('span', { class: 'sh-cmd' }, '$ ' + cmd), h('div', { class: 'refused' }, e.message))); }
    }
    run.addEventListener('click', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); else if (e.key === 'ArrowUp') { e.preventDefault(); if (S.hist.length) { S.hpos = Math.min(S.hist.length - 1, S.hpos + 1); input.value = S.hist[S.hist.length - 1 - S.hpos]; } } else if (e.key === 'ArrowDown') { e.preventDefault(); S.hpos = Math.max(-1, S.hpos - 1); input.value = S.hpos >= 0 ? S.hist[S.hist.length - 1 - S.hpos] : ''; } });
    async function loadHist() {
      try {
        const d = await api('/api/shell/history'); const bg = (await api('/api/shell/background?tail=1')).shells;
        S.hist = [...new Set([...d.history.map(x => x.command), ...S.hist])].slice(-100);
        hist.replaceChildren(h('h3', { style: 'font-size:12px;color:var(--ink2)' }, 'History'), ...d.history.slice().reverse().slice(0, 40).map(x => h('button', { class: 'h', title: x.refused || x.command, onclick: () => { input.value = x.command; input.focus(); } }, (x.refused ? '⊘ ' : x.code === 0 ? '✓ ' : '✗ ') + x.command)),
          h('h3', { style: 'font-size:12px;color:var(--ink2);margin-top:16px' }, 'Background shells (started by the agent)'), ...(bg.length ? bg.map(b => h('details', { class: 'tool' }, h('summary', null, h('span', { class: 'dot ' + (b.status === 'running' ? 'running' : 'finished') }), h('span', { class: 'ta' }, b.command), h('span', { class: 'ts' }, b.status)), h('pre', { class: 'out nolink', html: b.html || '' }), b.status === 'running' ? h('button', { class: 'mini danger', onclick: async () => { await api('/api/shell/bg/kill', { id: b.id }); loadHist(); } }, 'Kill') : null)) : [h('p', { class: 'muted' }, 'None running.')]));
      } catch (e) { /* history is optional */ }
    }
    api('/api/shell/history').then(d => { const c = document.getElementById('shcwd'); if (c && d.cwd) c.textContent = 'in ' + d.cwd; }).catch(() => {}); await loadHist(); const t = setInterval(() => { if (!S.closed) loadHist(); }, 4000); onLeave(() => clearInterval(t));
    { const ae = document.activeElement; if (!(ae && ae.tagName === 'TEXTAREA')) input.focus(); }      // never take the cursor from a message being typed
  })();
}

// ---------- commands ----------
function termHint(into, r) {
  const txt = (r.html || '').replace(/<[^>]+>/g, ' ');
  if (/interactive terminal|needs a (real )?terminal|requires a terminal|needs a tty/i.test(txt) && r.line) {
    into.append(notice('This command wants a real terminal for its picker. Run it in the terminal tab, where it works exactly as in the Axon terminal.', 'warn'),
      h('button', { class: 'btn primary', onclick: () => openInTerminal(r.line) }, 'Run ' + r.line.split(' ')[0] + ' in the terminal'));
  }
}
let CMD_CACHE = null;
async function allCommands() { if (!CMD_CACHE) CMD_CACHE = (await api('/api/commands')).commands; return CMD_CACHE; }
async function runSlash(line, session) { return api('/api/commands/run', { session: session || '', line, cards: true }); }
function copyText(t) { try { navigator.clipboard.writeText(t).then(() => toast('Copied'), () => toast('Could not copy')); } catch (e) { toast('Could not copy'); } }
// Run a line into a panel: shows that it is running with a Stop button, follows a slow command until it ends.
async function execLine(line, into) {
  let stopped = false;
  const stop = h('button', { class: 'btn danger', onclick: async () => { stopped = true; stop.disabled = true; try { await api('/api/chat/stop', { session: '' }); } catch (e) { toast(e.message); } } }, 'Stop');
  into.replaceChildren(h('div', { class: 'cmd-running' }, h('span', { class: 'muted' }, 'Running ' + line + '...'), ' ', stop));
  try {
    let r = await runSlash(line);
    while (r.done === false && !stopped) {
      await new Promise(res => setTimeout(res, 700));
      const j = await api('/api/command/job?id=' + encodeURIComponent(r.id));
      r = Object.assign({}, r, j, { done: j.done });
    }
    if (r.done === false) r = { ok: false, message: 'Stopped.', line, ms: 0, html: '' };
    renderCmdResult(r, into);
    return r;
  } catch (e) { into.replaceChildren(errorBox(e.message)); }
}
function renderCmdResult(r, into) {
  into.replaceChildren();
  if (cmdCardInto(r, into)) return;                   // neat card (dash_js_cmdcards.py)
  const again = (line) => execLine(line, into);
  if (r.message) into.append(notice(r.message, r.ok === false ? 'warn' : ''));
  if (r.open) {
    const dest = r.open + (r.open.includes('?') ? '&' : '?') + 'from=' + encodeURIComponent(((r.line || '').split(' ')[0]) || '/model');
    into.append(h('a', { class: 'btn primary', href: dest }, 'Open Providers'));
    toast(r.message || 'Opening Settings');
    setTimeout(() => { location.hash = dest; }, 700);   // long enough to read the note here before the page changes
    return;
  }
  if (r.chat) {
    const dest = '#/chat/' + encodeURIComponent(r.session || '');
    into.append(h('a', { class: 'btn primary', href: dest }, 'Go to Chat'));
    location.hash = dest; return;
  }
  if (r.copy) into.append(h('div', { class: 'formrow' }, h('code', null, r.copy), h('button', { class: 'btn', onclick: () => copyText(r.copy) }, 'Copy address')));
  if (r.action) {
    const a = r.action;
    into.append(h('button', { class: 'btn primary', onclick: async () => {
      try { if (a.shell) { await api('/api/shell/run', { command: a.shell }); toast('Done'); } else if (a.line) await again(a.line); } catch (e) { toast(e.message); }
    } }, a.label));
  }
  if (r.needs_arg && r.line) {
    const head = r.line.split(' ')[0];
    const arg = h('input', { type: 'text', placeholder: r.needs_arg, 'aria-label': 'What ' + head + ' needs', style: 'min-width:260px;flex:1' });
    const go = () => { if (arg.value.trim()) again(head + ' ' + arg.value.trim()); };
    arg.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    into.append(h('p', { class: 'muted' }, head + ' needs a few words from you:'), h('div', { class: 'formrow' }, arg, h('button', { class: 'btn primary', onclick: go }, 'Run')));
    setTimeout(() => arg.focus(), 0);
    return;
  }
  if (r.html) { const o = h('div', { class: 'cmd-out', html: r.html }); into.append(o); decorate(o); termHint(into, r); }
  else if (!r.message && !r.action && !r.copy) into.append(h('p', { class: 'muted' }, 'Done. This command printed nothing.'));
  into.append(h('div', { class: 'muted', style: 'font-size:12px' }, (r.line || '') + ' · ' + (r.ms || 0) + ' ms'));
}
async function viewCommands() {
  shell('commands', async (main) => {
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Commands'), h('div', { class: 'sub' }, 'Every Axon slash command runs here. Commands that ask the model something continue in Chat.'))));
    const cmds = await allCommands();
    const out = h('div', { id: 'cmdout', class: 'cmd-panel' }, h('p', { class: 'muted' }, 'Pick a command on the left. Its output shows here.'));
    const detail = h('div', { id: 'cmddetail' });
    const search = h('input', { type: 'search', class: 'search cmd-search', placeholder: 'Search commands, e.g. diff, cost, memory', 'aria-label': 'Search commands' });
    const list = h('div', { class: 'cmd-list' });
    const isSkill = (c) => c.kind === 'extension';
    const draw = () => {
      const q = search.value.toLowerCase().replace(/^\//, '').trim(); list.replaceChildren();
      const hit = cmds.filter(c => !q || cmdScore(q, c) > 0);
      const groups = new Map(); if (!q) cmdRecentSeed(groups, hit);
      for (const c of hit) { const g = isSkill(c) ? 'Skills and custom commands' : (c.category || 'Other'); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
      if (!hit.length) list.append(h('p', { class: 'muted', style: 'padding:10px 12px' }, 'No command matches "' + q + '".'));
      for (const [g, items] of groups) {
        const skills = g === 'Skills and custom commands';
        const d = h('details', { class: 'cmd-cat' + (skills ? ' cmd-skills' : '') }, h('summary', { class: 'cat' }, g + ' (' + items.length + ')'));
        if (q || (!skills && groups.size <= 1)) d.open = true;
        for (const c of items) d.append(cmdCatalogRow(c, () => select(c), () => { select(c); execLine(c.name, out); }));
        list.append(d);
      }
    };
    function select(c) {
      const arg = h('input', { type: 'text', placeholder: (c.usage && c.usage !== c.name ? c.usage.split('\n')[0] : 'arguments (optional)'), 'aria-label': 'Arguments', style: 'flex:1;min-width:200px' });
      const go = () => execLine((c.name + ' ' + arg.value).trim(), out);
      arg.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
      detail.replaceChildren(h('div', { class: 'card' }, h('h3', null, c.name, c.aliases.length ? h('span', { class: 'muted', style: 'font-weight:400' }, '  ' + c.aliases.join(' ')) : null), h('p', null, c.description),
        c.terminal_only ? h('p', { class: 'notice warn' }, c.terminal_only) : h('div', { class: 'formrow' }, arg, h('button', { class: 'btn primary', onclick: go }, 'Run'))));
      arg.focus();
    }
    search.addEventListener('input', draw); draw();
    main.append(h('div', { class: 'formrow' }, search), h('div', { class: 'cmd-grid' }, list, h('div', { class: 'cmd-right' }, detail, out)));
    const forms = h('div', { class: 'grid3' }, ...quickForms(out));
    main.append(h('details', { class: 'cmd-quick' }, h('summary', null, 'Quick actions (model, mode, compact, memory, marketplace...)'), forms));
  });
}
function quickForms(out) {
  const show = (line) => { out.scrollIntoView({ block: 'nearest' }); return execLine(line, out); };
  const card = (title, ...kids) => h('div', { class: 'card' }, h('h3', null, title), ...kids);
  const modelSel = h('select', null); api('/api/models').then(d => modelSel.append(...d.models.map(m => h('option', { value: m, selected: m === d.current }, m)))).catch(() => { });
  const modeSel = h('select', null, [['default', 'Ask before edits and commands'], ['acceptEdits', 'Allow file edits'], ['plan', 'Plan only'], ['bypass', 'Run everything']].map(([v, l]) => h('option', { value: v }, l)));
  const focus = h('input', { type: 'text', placeholder: 'What to keep in the summary (optional)' });
  const rn = h('input', { type: 'text', placeholder: 'prompt number', style: 'max-width:120px' }); const rmode = h('select', null, [['code_and_conversation', 'Code and conversation'], ['conversation', 'Conversation only'], ['code', 'Code only'], ['summarize_from_here', 'Summarise from here']].map(([v, l]) => h('option', { value: v }, l)));
  const note = h('input', { type: 'text', placeholder: 'Something Axon should remember' });
  const pr = h('input', { type: 'text', placeholder: 'Bash(git status:*)' }); const pk = h('select', null, ['allow', 'ask', 'deny'].map(v => h('option', { value: v }, v)));
  const mcpName = h('input', { type: 'text', placeholder: 'server name' });
  const mk = h('input', { type: 'search', placeholder: 'Search the marketplace' }); const mres = h('div', { style: 'max-height:260px;overflow:auto;margin-top:8px' });
  const tab = h('select', null, ['connectors', 'skills', 'plugins'].map(v => h('option', { value: v }, v)));
  async function browse() {
    mres.replaceChildren(h('p', { class: 'muted' }, 'Loading...'));
    try { const d = await api('/api/marketplace?tab=' + tab.value + '&q=' + encodeURIComponent(mk.value)); mres.replaceChildren(...d.entries.slice(0, 40).map(e => h('div', { class: 'row', style: 'padding:4px 0;display:flex;gap:8px;align-items:baseline;border-bottom:1px solid var(--line)' }, h('div', { style: 'flex:1;min-width:0' }, h('b', null, e.name), e.verified ? ' ✓' : '', h('div', { class: 'muted', style: 'font-size:12px' }, (e.description || '').slice(0, 120))), h('button', { class: 'mini', onclick: () => show('/marketplace install ' + e.id) }, 'Install'), h('button', { class: 'mini', onclick: () => show('/marketplace remove ' + e.id) }, 'Remove'))), d.entries.length ? null : h('p', { class: 'muted' }, 'Nothing found.')); } catch (e) { mres.replaceChildren(errorBox(e.message)); }
  }
  mk.addEventListener('keydown', (e) => { if (e.key === 'Enter') browse(); });
  const btn = (label, fn, cls) => h('button', { class: 'btn ' + (cls || ''), onclick: fn }, label);
  return [
    card('Model', h('div', { class: 'formrow' }, modelSel, btn('Switch', () => show('/model ' + modelSel.value), 'primary'))),
    card('Permission mode', h('div', { class: 'formrow' }, modeSel, btn('Apply', () => show('/mode ' + modeSel.value)))),
    card('Free up context', h('div', { class: 'formrow' }, focus, btn('Compact', () => show(('/compact ' + focus.value).trim())))),
    card('Start over', h('div', { class: 'formrow' }, btn('Clear conversation', () => confirm('Start a fresh session? The old one stays in Sessions.') && show('/clear')), btn('Show prompts to rewind to', () => show('/rewind points')))),
    card('Rewind', h('div', { class: 'formrow' }, rn, rmode, btn('Rewind', () => rn.value.trim() && confirm('Rewind to prompt ' + rn.value + '?') && show('/rewind to ' + rn.value.trim() + ' ' + rmode.value), 'danger'))),
    card('Memory', h('div', { class: 'formrow' }, note, btn('Remember', () => note.value.trim() && show('#' + note.value.trim()))), h('div', { class: 'formrow' }, btn('List memories', () => show('/memory')), h('a', { class: 'btn', href: '#/memory' }, 'Open Memory'))),
    card('MCP servers', h('div', { class: 'formrow' }, mcpName, btn('Enable', () => mcpName.value && show('/mcp enable ' + mcpName.value)), btn('Disable', () => mcpName.value && show('/mcp disable ' + mcpName.value))), btn('List servers', () => show('/mcp list'))),
    card('Permissions', h('div', { class: 'formrow' }, pk, pr, btn('Add rule', () => pr.value.trim() && show('/permissions ' + pk.value + ' ' + pr.value.trim()))), btn('Show rules', () => show('/permissions list'))),
    card('Marketplace', h('div', { class: 'formrow' }, tab, mk, btn('Browse', browse)), mres),
    card('Usage and settings', h('div', { class: 'formrow' }, h('a', { class: 'btn', href: '#/usage' }, 'Usage charts'), btn('Terminal usage view', () => show('/usage')), btn('Config', () => show('/config')), btn('Cost', () => show('/cost')))),
    card('Files and theme', h('div', { class: 'formrow' }, h('a', { class: 'btn', href: '#/files' }, 'Browse files'), btn('List attachments', () => show('/files')), btn('Light / dark', toggleTheme), btn('Terminal theme', () => show('/theme')))),
    card('Terminal', h('p', { class: 'muted' }, 'A real shell on this computer, or the real axon program, in a terminal tab.'), h('div', { class: 'formrow' }, h('a', { class: 'btn primary', href: '#/terminal' }, 'Open a terminal'), btn('Axon in a terminal', () => openInTerminal('')))),
    card('Help', h('div', { class: 'formrow' }, btn('/help', () => show('/help')), btn('/doctor', () => show('/doctor')), btn('/status', () => show('/status')), btn('/context', () => show('/context')))),
  ];
}
// Cmd/Ctrl+K palette
function openPalette() {
  if (document.querySelector('.palette')) return;
  const scrim = h('div', { class: 'scrim top2', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' });
  const input = h('input', { type: 'text', placeholder: 'Type a command or page...', 'aria-label': 'Command palette' }); const list = h('div', { class: 'list' });
  const box = h('div', { class: 'palette' }, input, list); scrim.append(box); document.body.append(scrim);
  scrim.addEventListener('click', (e) => { if (e.target === scrim) scrim.remove(); });
  let items = [], sel = 0;
  const pages = [...NAV.map(([id, label]) => ({ label: 'Go to ' + label, hint: '#/' + id, run: () => { location.hash = '#/' + id; } })),
    ...NAV_PANELS.map(([id, label, , help]) => ({ label: 'Open ' + label, hint: help, run: () => { location.hash = '#/' + id; } }))];
  allCommands().then((c) => { items = [...pages, ...c.map(x => ({ label: x.name, hint: x.description, cmd: x }))]; draw(); }).catch(() => { items = pages; draw(); });
  function matches() { const q = input.value.toLowerCase().trim(); return items.filter(i => !q || i.label.toLowerCase().includes(q.replace(/^\//, '')) || (i.hint || '').toLowerCase().includes(q)).slice(0, 40); }
  function draw() { const m = matches(); sel = Math.min(sel, Math.max(0, m.length - 1)); list.replaceChildren(...m.map((i, k) => h('div', { class: 'it', 'aria-selected': String(k === sel), onclick: () => choose(i) }, h('b', null, i.label), h('span', null, i.hint || '')))); }
  function choose(i) { scrim.remove(); if (i.run) return i.run(); if (i.cmd.terminal_only) { toast(i.cmd.terminal_only); return; } location.hash = '#/commands'; setTimeout(() => { const el = [...document.querySelectorAll('.cmd-list .c b')].find(b => b.textContent === i.cmd.name); el && el.parentElement.click(); }, 400); }
  input.addEventListener('input', () => { sel = 0; draw(); });
  input.addEventListener('keydown', (e) => { const m = matches(); if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(m.length - 1, sel + 1); draw(); } else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(0, sel - 1); draw(); } else if (e.key === 'Enter') { e.preventDefault(); if (m[sel]) choose(m[sel]); } else if (e.key === 'Escape') scrim.remove(); });
  input.focus();
}
function helpModal() {
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Keyboard shortcuts' }); scrim.addEventListener('click', (e) => { if (e.target === scrim) scrim.remove(); });
  const rows = [['Ctrl/Cmd+K', 'Command palette'], ['/', 'Focus search or the message box'], ['g then c / f / h / x / m / s / u / i / y / e / t', 'Chat, Files panel, Shell (Terminal panel), Terminal panel, Commands, Sessions, Usage, Insights, Memory, Extensions, Settings'], ['Ctrl+P', 'Choose the model for this chat'], ['Shift+Tab in the message box', 'Cycle the permission mode'], ['Ctrl/Cmd+J', 'Show or hide the Terminal panel'], ['Ctrl/Cmd+\\', 'Show or hide the Files panel'], ['Enter', 'Send (Shift+Enter for a new line)'], ['/ in the message box', 'Slash commands'], ['@ in the message box', 'Mention a file'], ['! at the start', 'Run a shell command, then Axon sees the output'], ['Up in an empty box', 'Previous prompt'], ['Ctrl/Cmd+S', 'Save the file you are editing'], ['Esc', 'Close a dialog'], ['?', 'This list']];
  scrim.append(h('div', { class: 'modal' }, h('h3', null, 'Keyboard shortcuts'), h('dl', { class: 'kv' }, ...rows.flatMap(([k, v]) => [h('dt', null, h('span', { class: 'kbd' }, k)), h('dd', null, v)])), h('div', { class: 'acts' }, h('button', { class: 'btn', onclick: () => scrim.remove() }, 'Close'))));
  document.body.append(scrim);
}

// ---------- memory & settings ----------
async function viewMemory() {
  shell('memory', async (main) => {
    main.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Memory'), h('div', { class: 'sub' }, 'What Axon reads at the start of every session, and the notes it keeps.'))));
    let d; try { d = await api('/api/memory'); } catch (e) { main.append(errorBox(e.message)); return; }
    main.append(h('h2', null, 'Instruction files'), d.files.length ? table([{ label: 'File', key: 'name' }, { label: 'Scope', key: 'scope' }, { label: 'Size', cls: 'r', fmt: r => (r.size / 1024).toFixed(1) + ' KB' }, { label: '', fmt: r => r.editable ? h('a', { class: 'btn', href: '#/files/' + encodeURIComponent(r.rel) }, 'Open and edit') : h('span', { class: 'muted' }, r.path) }], d.files) : h('p', { class: 'muted' }, 'No AXON.md, CLAUDE.md or AGENTS.md here. Run /init from the Commands page to create one.'));
    mgMemoryPanel(main);
  });
}
// ---------- router & keys ----------
function openHash(hash, activate) {
  UI.bg = !activate;
  try {
    const clean = (hash || '').replace(/^#\/?/, '') || 'chat';
    const [name, ...restParts] = clean.split('/'); const rest = restParts.join('/');
    document.querySelector('.scrim')?.remove();
    if (name === 'chat') return viewChat(rest ? decodeURIComponent(rest) : '');
    if (name === 'files') { if (rest) return viewFile(rest); wbOpen('files'); if (!UI.active) return viewChat(''); return; }
    if (name === 'shell' || (name === 'terminal' && !rest)) { wbOpen('terminal'); if (!UI.active) return viewChat(''); return; }   // old links: the Terminal panel
    if (name === 'terminal') return viewTerminal(rest);
    if (name === 'insights') return viewInsights(rest);
    if (name === 'commands') return viewCommands();
    if (name === 'sessions') return viewSessions();
    if (name === 'session' && rest) { location.replace('#/chat/' + rest); return; }
    if (name === 'usage' || name === 'overview') return viewOverview();
    if (name === 'memory') return viewMemory();
    if (name === 'extensions') return viewExtensions();
    if (name === 'settings') return viewSettings(rest);
    if (name === 'agents') return viewAgents(rest ? decodeURIComponent(rest) : '');
    if (name === 'agent' && rest) return viewAgent(decodeURIComponent(rest));
    return viewChat('');
  } finally { UI.bg = false; }
}
function route() { return openHash(location.hash, true); }
let chord = '', chordT = 0;
// True while focus is somewhere that owns the keyboard: the terminal, an input, a textarea, a select or an editable region.
function ownsKeys(t) { return !!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable || (t.closest && t.closest('.tscreen')))); }
function activePane() { return (UI.active && UI.active.pane) || document; }
// Shift+Tab cycles the permission mode and Ctrl+P opens the model picker, as in the terminal app.
function cycleMode() {
  const sel = activePane().querySelector('select[aria-label="Permission mode"]');
  if (!sel) {   // the composer footer has a mode button instead of a select
    const C = UI.chat; if (!C || !C.foot || !UI.active || UI.active.route !== 'chat' || typeof WB_MODES === 'undefined') return false;
    const i = WB_MODES.findIndex(m => m[0] === ((C.st && C.st.mode) || 'default')), nx = WB_MODES[(i + 1) % WB_MODES.length];
    runCmd(C, '/mode ' + nx[0]).then(() => toast('Mode: ' + nx[1])).catch((e) => toast(e.message)); return true;
  }
  if (sel.disabled) return false;
  const opts = [...sel.options].filter(o => !o.disabled); if (opts.length < 2) return false;
  const i = opts.findIndex(o => o.value === sel.value); sel.value = opts[(i + 1) % opts.length].value; sel.dispatchEvent(new Event('change', { bubbles: true })); return true;
}
function openModelPicker() {
  const sel = activePane().querySelector('select[aria-label="Model"]');
  if (!sel) { const C = UI.chat; if (C && C.foot && UI.active && UI.active.route === 'chat') { C.foot.model.focus(); C.foot.model.click(); return true; } return false; }
  sel.focus(); try { if (sel.showPicker) sel.showPicker(); } catch (e) { /* needs a user gesture; focus is enough */ } return true;
}
document.addEventListener('keydown', (e) => {
  const inTerm = !!(e.target && e.target.closest && e.target.closest('.tscreen'));
  if (inTerm) return;                                    // the terminal gets every key, including Ctrl+K, Shift+Tab and g-then-s
  if (e.key === 'Tab' && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t0 = e.target, tag = t0 && t0.tagName;
    const inComposer = !!(t0 && t0.closest && t0.closest('.composer') && tag === 'TEXTAREA');
    if ((inComposer || !t0 || t0 === document.body || t0 === document.documentElement) && !document.querySelector('.scrim') && cycleMode()) { e.preventDefault(); return; }
  }
  if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'p' && !document.querySelector('.scrim')) { if (openModelPicker()) e.preventDefault(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); wbToggle('terminal'); return; }
  if ((e.ctrlKey || e.metaKey) && e.key === '\\') { e.preventDefault(); wbToggle('files'); return; }
  if (e.key === 'Escape') { document.querySelector('.scrim')?.remove(); return; }
  const t = e.target, typing = ownsKeys(t);
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); const s = document.getElementById('search') || document.querySelector('.composer textarea'); if (s) s.focus(); else { location.hash = '#/sessions'; setTimeout(() => document.getElementById('search')?.focus(), 400); } return; }
  if (e.key === '?') { e.preventDefault(); helpModal(); return; }
  if (chord === 'g' && Date.now() - chordT < 1200) { chord = ''; const dest = { c: 'chat', f: 'files', h: 'shell', x: 'terminal', m: 'commands', s: 'sessions', u: 'usage', i: 'insights', o: 'usage', y: 'memory', e: 'extensions', t: 'settings' }[e.key]; if (dest) { e.preventDefault(); location.hash = '#/' + dest; } return; }
  if (e.key === 'g') { chord = 'g'; chordT = Date.now(); } else chord = '';
});
window.addEventListener('hashchange', route);
const savedTheme = lsGet('axon-theme'); if (savedTheme === 'dark' || savedTheme === 'light') setTheme(savedTheme);
const savedTrace = lsGet('axon-trace'); if (['compact', 'normal', 'detailed', 'debug'].includes(savedTrace)) state.trace = savedTrace;
Promise.all([api('/api/info'), api('/api/ws')]).then(async ([i, w]) => {
  state.info = i; state.ws = w;
  try { const r = await fetch('/static/logo.svg', { credentials: 'same-origin' }); state.logo = await r.text(); } catch (e) { state.logo = ''; }
  ensureFrame();
  const here = location.hash;
  for (const hh of (UI.restore && UI.restore.tabs) || []) { if (hh && hh !== here && !/^#\/(commands|sessions|usage|memory|extensions|settings)$/.test(hh)) { try { openHash(hh, false); } catch (e) { /* stale tab */ } } }
  route();
}).catch((e) => { app.replaceChildren(h('main', null, errorBox(e.message + '. Open the dashboard using the URL printed in your terminal.'))); });
"""
