"""Client script: Settings (Providers, Preferences, Permissions and details)."""

JS_SETTINGS = r"""
// ---------- settings ----------
async function viewSettings(rest) {
  const tab = (rest || 'appearance').split('?')[0];
  return shell('settings', async (pane) => {
    const tabs = h('div', { class: 'tabs', role: 'tablist' }, ...[['appearance', 'Appearance'], ['providers', 'Providers'], ['preferences', 'Preferences'], ['details', 'Permissions and details']].map(([k, l]) => h('button', { role: 'tab', 'aria-selected': String(k === tab), onclick: () => { location.hash = '#/settings/' + k; } }, l)));
    const body = h('div');
    pane.append(h('div', { class: 'head' }, h('div', null, h('h1', null, 'Settings'), h('div', { class: 'sub' }, 'How Axon looks, how it connects to a model, and how it behaves. Keys are saved on this computer and never shown again.'))), tabs, body);
    if (tab === 'appearance') return appearanceTab(body);
    if (tab === 'providers') return providersTab(body, providersOrigin(rest));
    if (tab === 'preferences') return preferencesTab(body);
    return detailsTab(body);
  }, { key: 'settings:' + (rest || 'appearance').split('?')[0], hash: '#/settings/' + (rest || 'appearance').split('?')[0], title: 'Settings' });
}

// "#/settings/providers?from=%2Fmodel": the command that sent the person here, shown as a banner and cleared by Back.
function providersOrigin(rest) {
  const m = /[?&]from=([^&]*)/.exec(rest || ''); if (!m) return '';
  let v = ''; try { v = decodeURIComponent(m[1]); } catch (e) { return ''; }
  return /^\/[a-z-]{1,24}$/i.test(v) ? v : '';
}
async function providersTab(body, from) {
  if (from) body.append(h('div', { class: 'notice ok', id: 'from-cmd' }, h('b', null, 'You ran ' + from + '. '), 'This is where you choose the provider and model. Pick one below and choose Use this provider. ',
    h('button', { class: 'mini', type: 'button', onclick: () => history.back() }, 'Back')));
  let d; try { d = await api('/api/providers'); } catch (e) { body.append(errorBox(e.message)); return; }
  const cur = d.current;
  const ready = await api('/api/provider/ready').catch(() => ({ ready: false, reason: '' }));
  const status = h('div', { class: 'notice ' + (ready.ready ? 'ok' : 'warn') }, ready.ready ? h('span', null, 'Chat is ready. Using ', h('b', null, cur.model), '. ', h('a', { class: 'plink', href: '#/chat' }, 'Go to the chat')) : h('span', null, h('b', null, 'Chat is not set up yet. '), ready.reason || 'Pick a provider below, add its key, then choose Use this provider.'));
  body.append(status);
  const note = (el, msg, cls) => { el.replaceChildren(notice(msg, cls)); };
  const sections = {};
  d.providers.forEach(p => (sections[p.category] = sections[p.category] || []).push(p));
  // custom endpoint
  const cbase = h('input', { type: 'text', placeholder: 'http://localhost:8080/v1', 'aria-label': 'Endpoint URL', value: ''}), cmodel = h('input', { type: 'text', placeholder: 'model name', 'aria-label': 'Model name' }), ckey = h('input', { type: 'password', autocomplete: 'off', placeholder: 'API key (leave empty if it needs none)', 'aria-label': 'API key' });
  const cout = h('div');
  const customCard = h('div', { class: 'card' }, h('h3', null, 'Your own endpoint'), h('p', { class: 'muted' }, 'Any OpenAI-compatible server: a local model, a gateway or a proxy.'),
    h('div', { class: 'formrow' }, cbase, cmodel), h('div', { class: 'formrow' }, ckey),
    h('div', { class: 'formrow' }, h('button', { class: 'btn', onclick: async () => { note(cout, 'Testing...'); try { const r = await api('/api/provider/test', { preset: 'agentrouter', key: ckey.value, base_url: cbase.value, model: cmodel.value }); note(cout, r.message, r.ok ? 'ok' : 'bad'); } catch (e) { note(cout, e.message, 'bad'); } } }, 'Test'),
      h('button', { class: 'btn primary', onclick: async () => { try { const r = await api('/api/provider/apply', { preset: 'custom', base_url: cbase.value, model: cmodel.value, key: ckey.value }); ckey.value = ''; note(cout, r.message, 'ok'); toast(r.message); viewSettings('providers'); } catch (e) { note(cout, e.message, 'bad'); } } }, 'Use this endpoint')), cout);
  body.append(h('details', { class: 'adv' }, h('summary', null, 'Advanced: connection details and your own endpoint'), h('p', { class: 'muted' }, 'Endpoint in use: ', h('code', null, cur.base_url || 'default')), customCard));
  for (const [cat, list] of Object.entries(sections)) {
    body.append(h('h2', { style: 'margin:22px 0 8px' }, cat));
    for (const p of list) body.append(providerRow(p, note));
  }
}
function providerRow(p, note) {
  const chip = p.active ? h('span', { class: 'gbadge', style: 'border-color:var(--primary);color:var(--primary)' }, 'in use') : null;
  const keyState = !p.requires_key ? 'No key needed' : (p.key_set ? 'Key set (' + p.key_source + ')' : 'Needs a key');
  const det = h('details', { class: 'tool provider', open: p.active }, h('summary', null, h('span', { class: 'tn' }, p.name), h('span', { class: 'ta' }, p.description), chip, h('span', { class: 'ts ' + (p.requires_key && !p.key_set ? 'bad' : '') }, keyState)));
  const key = h('input', { type: 'password', autocomplete: 'off', placeholder: p.key_set ? 'Enter a new key to replace the saved one' : 'Paste your ' + p.name + ' key', 'aria-label': p.name + ' API key' });
  const base = h('input', { type: 'text', value: p.base_url, 'aria-label': 'Endpoint URL' });
  const model = h('input', { type: 'text', value: p.default_model, list: 'm-' + p.id, 'aria-label': 'Model' }); const dl = h('datalist', { id: 'm-' + p.id }, ...p.models.map(m => h('option', { value: m })));
  const out = h('div');
  const loadModels = h('button', { class: 'btn', onclick: async () => { note(out, 'Reading the provider\'s model list...'); try { const r = await api('/api/provider/models?preset=' + p.id + '&base_url=' + encodeURIComponent(base.value)); dl.replaceChildren(...r.models.map(m => h('option', { value: m }))); note(out, (r.live ? 'Live list: ' : 'Built-in list (the provider did not answer): ') + r.models.length + ' models. Click the model box to choose.'); model.focus(); } catch (e) { note(out, e.message, 'bad'); } } }, 'Load models');
  det.append(h('div', { style: 'padding:8px 12px 12px' },
    p.notice ? notice(p.notice, 'warn') : null,
    p.requires_key ? h('div', { class: 'formrow' }, key, h('button', { class: 'btn', onclick: async () => { if (!key.value.trim()) return note(out, 'Paste a key first.', 'warn'); try { const r = await api('/api/provider/key', { preset: p.id, key: key.value }); key.value = ''; note(out, 'Key saved on this computer (' + r.key_source + ').', 'ok'); } catch (e) { note(out, e.message, 'bad'); } } }, 'Save key')) : null,
    h('div', { class: 'formrow' }, h('label', { class: 'muted' }, 'Model'), model, dl, loadModels),
    h('details', { class: 'adv' }, h('summary', null, 'Advanced'), h('div', { class: 'formrow' }, h('label', { class: 'muted' }, 'Endpoint URL'), base)),
    h('div', { class: 'formrow' }, h('button', { class: 'btn', onclick: async () => { note(out, 'Testing the key...'); try { const r = await api('/api/provider/test', { preset: p.id, key: key.value, base_url: base.value, model: model.value }); note(out, r.message, r.ok ? 'ok' : 'bad'); } catch (e) { note(out, e.message, 'bad'); } } }, 'Test'),
      h('button', { class: 'btn primary', onclick: async () => { try { const r = await api('/api/provider/apply', { preset: p.id, base_url: base.value === p.base_url ? '' : base.value, model: model.value, key: key.value }); key.value = ''; toast(r.message); viewSettings('providers'); } catch (e) { note(out, e.message, 'bad'); } } }, 'Use this provider'),
      p.docs_url ? h('a', { class: 'btn', href: p.docs_url, target: '_blank', rel: 'noopener noreferrer' }, 'Get a key') : null), out));
  return det;
}

async function preferencesTab(body) {
  let d; try { d = await api('/api/settings/panel?session=' + encodeURIComponent(UI.chat ? UI.chat.sid : '')); } catch (e) { body.append(errorBox(e.message)); return; }
  body.append(h('p', { class: 'muted' }, d.live ? 'Changes apply to the running chat and are saved to settings.json.' : 'No chat is open, so changes are saved and apply to the next chat.'));
  const rows = d.settings.filter(s => s.key !== 'model');
  const res = h('div');
  body.append(h('div', { class: 'card' }, ...rows.map(s => {
    const save = async (v) => { try { const r = await api('/api/settings/set', { session: UI.chat ? UI.chat.sid : '', key: s.key, value: v }); toast((s.label) + ' saved'); res.replaceChildren(notice(r.message || 'Saved', 'ok')); if (s.key === 'mode' || s.key === 'trace_level' || s.key === 'effort') refreshStatusSoon();
      if (s.key === 'trace_level' && ['compact', 'normal', 'detailed', 'debug'].includes(v)) { state.trace = v; lsSet('axon-trace', v); document.querySelectorAll('.feed').forEach(f => { f.className = 'feed t-' + v; }); if (typeof drawStatus === 'function') drawStatus(); } } catch (e) { res.replaceChildren(notice(e.message, 'bad')); } };
    let ctl;
    if (s.type === 'bool') ctl = h('input', { type: 'checkbox', checked: !!s.value, onchange: (e) => save(e.target.checked), 'aria-label': s.label });
    else if (s.type === 'choice') ctl = h('select', { onchange: (e) => save(e.target.value), 'aria-label': s.label }, ...s.choices.map(c => h('option', { value: c, selected: String(s.value) === c }, prefWord(s.key, c))));
    else { ctl = h('input', { type: 'text', value: s.value == null ? '' : String(s.value), onchange: (e) => save(e.target.value), 'aria-label': s.label }); }
    const label = s.key === 'theme' ? 'Terminal colour theme' : s.label;
    if (s.key === 'theme') ctl.setAttribute('aria-label', label);
    return h('div', { class: 'formrow', style: 'justify-content:space-between;border-bottom:1px solid var(--line);padding:8px 0;margin:0' }, s.key === 'theme' ? h('label', null, label, h('span', { class: 'muted', style: 'display:block;font-size:13px' }, 'For the terminal app. The web app\'s own colours are in Appearance.')) : h('label', null, label), ctl);
  })), res);
}
// The words the composer and status bar use for a stored setting value.
function prefWord(key, v) {
  if (key === 'mode' && typeof WB_MODES !== 'undefined') { const m = WB_MODES.find(x => x[0] === v); if (m) return m[1]; }
  if (key === 'effort' && typeof effortWord === 'function') return effortWord(v);
  return v;
}
function refreshStatusSoon() { setTimeout(() => { if (UI.chat) refreshState(UI.chat, true); }, 300); }

async function detailsTab(body) {
  let d; try { d = await api('/api/extensions'); } catch (e) { body.append(errorBox(e.message)); return; }
  const sid = () => (UI.chat ? UI.chat.sid : '');
  const permsCard = h('div', { class: 'card' });
  const draw = async () => {
    let perms = null; try { perms = await api('/api/permissions?session=' + encodeURIComponent(sid())); } catch (e) { permsCard.replaceChildren(errorBox(e.message)); return; }
    const out = h('div');
    const run = async (line) => { try { const r = await api('/api/commands/run', { session: sid(), line }); if (r.ok === false) throw new Error(r.message || 'Not saved'); await draw(); } catch (e) { out.replaceChildren(notice(e.message, 'bad')); } };
    const kind = h('select', { 'aria-label': 'Rule type' }, h('option', { value: 'allow' }, 'Allow'), h('option', { value: 'ask' }, 'Always ask'), h('option', { value: 'deny' }, 'Deny'));
    const rule = h('input', { type: 'text', placeholder: 'Bash(git status:*)   Edit(src/**)   Read(~/.ssh/**)', 'aria-label': 'Permission rule', spellcheck: 'false', autocomplete: 'off' });
    const add = () => { const v = rule.value.trim(); if (!v) { out.replaceChildren(notice('Type a rule first, for example Bash(git status:*).', 'warn')); return; } run('/permissions ' + kind.value + ' ' + v); };
    rule.addEventListener('keydown', (e) => { if (e.key === 'Enter') add(); });
    const rows = (perms.rules || []).map(r => h('div', { class: 'formrow', style: 'justify-content:space-between;border-bottom:1px solid var(--line);padding:6px 0;margin:0' },
      h('span', null, h('b', null, { allow: 'Allow', ask: 'Always ask', deny: 'Deny' }[r.kind] || r.kind), ' ', h('code', null, r.rule), ' ', h('span', { class: 'muted' }, r.source)),
      h('button', { class: 'mini', type: 'button', 'aria-label': 'Remove ' + r.kind + ' rule ' + r.rule, onclick: () => run('/permissions remove ' + r.kind + ' ' + r.rule) }, 'Remove')));
    permsCard.replaceChildren(h('h3', null, 'Permissions'),
      h('dl', { class: 'kv' }, h('dt', null, 'mode'), h('dd', null, perms.mode), h('dt', null, 'extra folders'), h('dd', null, perms.additional_dirs.join('\n') || '(none)'), h('dt', null, 'shell sandbox'), h('dd', null, perms.sandbox ? 'on' : 'off')),
      h('h3', { style: 'margin-top:14px' }, 'Rules'), rows.length ? h('div', null, ...rows) : h('p', { class: 'muted' }, 'No rules yet. Axon asks before edits and commands, as the mode says.'),
      h('div', { class: 'formrow', style: 'margin-top:10px' }, kind, rule, h('button', { class: 'btn primary', type: 'button', onclick: add }, 'Add rule')), out,
      h('p', { class: 'muted' }, 'Rules are saved in this folder\'s .axon/settings.local.json. Deny beats Always ask, and Always ask beats Allow.'));
  };
  body.append(permsCard, h('div', { class: 'card' }, h('h3', null, 'All settings'), h('dl', { class: 'kv' }, ...Object.entries(d.settings || {}).flatMap(([k, v]) => [h('dt', null, k), h('dd', null, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v))]))));
  await draw();
}

// ---------- appearance ----------
const AP_ICON = {
  system: 'M4 5h16v11H4zM9 20h6M12 16v4',
  light: 'M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5',
  dark: 'M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z'
};
function appearanceTab(body) {
  const AP = window.AxonAppearance;
  if (!AP) { body.append(errorBox('The appearance script did not load. Reload the page.')); return; }
  const syncs = [];
  const row = (label, desc, ...ctl) => h('div', { class: 'ap-row' }, h('div', { class: 'ap-lab' }, h('b', null, label), desc ? h('span', null, desc) : null), h('div', { class: 'ap-ctl' }, ...ctl));
  const group = (title, ...rows) => h('section', { class: 'ap-group' }, h('h2', null, title), h('div', { class: 'ap-card' }, ...rows));
  // a one-of-many button group; opts are [value, label, iconPath?]
  const seg = (name, key, opts) => {
    const btns = opts.map(([v, l, ic]) => h('button', { type: 'button', 'data-v': String(v), 'aria-pressed': 'false', onclick: () => AP.set({ [key]: v }) }, ic ? svgIcon(ic) : null, l));
    const el = h('div', { class: 'seg', role: 'group', 'aria-label': name }, ...btns);
    syncs.push(() => { const cur = String(AP.get()[key]); btns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === cur))); });
    return el;
  };
  const select = (name, key, opts) => {
    const el = h('select', { 'aria-label': name, onchange: (e) => AP.set({ [key]: e.target.value }) }, ...opts.map(([v, l]) => h('option', { value: String(v) }, l)));
    syncs.push(() => { el.value = String(AP.get()[key]); });
    return el;
  };
  // Theme
  const contrast = h('input', { type: 'checkbox', 'aria-label': 'High contrast', onchange: (e) => AP.set({ contrast: e.target.checked }) });
  syncs.push(() => { contrast.checked = AP.get().contrast; });
  // Fonts
  const uiOpts = Object.entries(AP.UI_FONTS).filter(([k, f]) => !f.family || AP.installed(f.family)).map(([k, f]) => [k, f.label]);
  const serifName = h('b', null, AP.serifInUse());
  const sample = h('div', { class: 'ap-preview', 'aria-label': 'Preview of the response font' }, 'The quick brown fox jumps over the lazy dog. Axon read three files, changed one, and ran the tests: 42 passed.');
  syncs.push(() => { sample.style.fontFamily = 'var(--font-response)'; sample.style.fontSize = 'var(--size-response)'; });
  // Code themes: both palettes are always shown, so the one for the other scheme can be judged too
  const codeRow = (label, key, mode) => {
    const ids = Object.keys(AP.CODE_THEMES).filter(k => AP.CODE_THEMES[k].mode === mode);
    const pre = h('pre', { class: 'ap-code', 'aria-label': label + ' preview' });
    const paint = () => {
      const id = AP.get()[key], t = AP.CODE_THEMES[id];
      pre.setAttribute('data-code-theme', id); pre.style.color = mode === 'dark' ? '#ECEFF3' : '#14171C';
      Object.entries(AP.paletteVars(id)).forEach(([n, v]) => pre.style.setProperty(n, v));
      const L = (cls, ...kids) => h('span', { class: 'ln ' + cls }, ...kids);
      pre.replaceChildren(h('span', { class: 'cap' }, t.label),
        L('', h('span', { class: 'tk-c' }, '# round to cents')),
        L('del', '- ', h('span', { class: 'tk-k' }, 'return'), ' total'),
        L('add', '+ ', h('span', { class: 'tk-k' }, 'return'), ' ', h('span', { class: 'tk-f' }, 'round'), '(total, ', h('span', { class: 'tk-n' }, '2'), ')'),
        L('', h('span', { class: 'tk-k' }, 'print'), '(', h('span', { class: 'tk-s' }, '"done"'), ')'));
    };
    syncs.push(paint);
    return [row(label, null, select(label, key, ids.map(k => [k, AP.CODE_THEMES[k].label]))), h('div', { class: 'ap-row' }, h('div', { class: 'ap-previews' }, pre))];
  };
  const cfont = h('input', { type: 'text', placeholder: 'e.g. JetBrains Mono', 'aria-label': 'Code font name', autocomplete: 'off', spellcheck: 'false', maxlength: '60' });
  const cnote = h('span', null, '');
  const commitFont = () => { const clean = AP.sanitizeFont(cfont.value); cfont.setAttribute('aria-invalid', String(clean !== cfont.value.trim())); cfont.value = clean; AP.set({ cfont: clean }); };
  cfont.addEventListener('change', commitFont);
  cfont.addEventListener('keydown', (e) => { if (e.key === 'Enter') commitFont(); });
  syncs.push(() => { const c = AP.get().cfont; if (document.activeElement !== cfont) cfont.value = c; cnote.textContent = c ? (AP.installed(c) ? c + ' is installed and used first.' : c + ' was not found on this computer, so the default monospace font shows instead.') : 'Leave empty for the default monospace font. Letters, digits, spaces and hyphens only.'; });
  const codeLabel = h('div', { class: 'ap-lab' }, h('b', null, 'Code font'), cnote);

  body.append(
    group('Theme',
      row('Colour theme', 'Follows your system until you pick one. Theme in the More menu (top right) changes the same setting.',
        seg('Colour theme', 'theme', [['system', 'System', AP_ICON.system], ['light', 'Light', AP_ICON.light], ['dark', 'Dark', AP_ICON.dark]])),
      row('High contrast', 'Stronger text and borders in either theme.', contrast)),
    group('Text',
      row('Interface font', uiOpts.length > 1 ? 'Inter and SF Pro show only when installed on this computer.' : 'Other fonts such as Inter or SF Pro appear here when they are installed on this computer.', select('Interface font', 'ui', uiOpts)),
      row('Response font', null, seg('Response font', 'response', [['serif', 'Serif'], ['match', 'Match interface']])),
      h('div', { class: 'ap-row' }, h('div', { class: 'ap-lab' }, h('span', null, 'Serif in use: ', serifName, ' (first installed of ' + AP.SERIFS.join(', ') + ', then the browser serif)')), sample),
      row('Response text size', null, seg('Response text size', 'rsize', AP.SIZES.map(([v, l]) => [v, l + ' ' + v]))),
      row('Conversation width', 'The chat never grows wider than the room left beside the side panels.', seg('Conversation width', 'width', AP.WIDTHS.map(([v, l]) => [v, v === 'full' ? l : l + ' ' + v])))),
    group('Motion',
      row('Animations', 'Reduced turns off transitions and animation.', seg('Motion', 'motion', [['system', 'System'], ['reduced', 'Reduced'], ['on', 'On']]))),
    group('Code',
      ...codeRow('Light code theme', 'codeLight', 'light'),
      ...codeRow('Dark code theme', 'codeDark', 'dark'),
      h('div', { class: 'ap-row' }, codeLabel, h('div', { class: 'ap-ctl' }, cfont)),
      row('Code font size', 'Also sets the Terminal tab font.', seg('Code font size', 'csize', AP.CODE_SIZES.map(n => [n, String(n)])))),
    h('div', { class: 'ap-foot' }, h('button', { class: 'btn', type: 'button', onclick: () => { AP.reset(); toast('Appearance reset to defaults'); } }, 'Reset to defaults'),
      h('span', { class: 'muted' }, 'Saved in this browser.')));
  const sync = () => syncs.forEach(f => f());
  sync();
  const off = AP.subscribe(() => { if (!body.isConnected) { off(); return; } sync(); });
}
"""
