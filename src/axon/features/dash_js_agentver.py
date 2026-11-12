"""Extensions > Agents > Versions panel (history, diff, roll back, pin). Server text only goes in as text nodes.
Reading works everywhere; rollback and pin are refused by the server on the phone listener (a note says so)."""
from __future__ import annotations

JS_AGENTVER = r"""
function agentVersions(r) {
  if (!r || r.source === 'builtin') return null;
  const body = h('div', { class: 'muted', style: 'font-size:13px;padding:6px 0' }, 'Loading...');
  let loaded = false;
  const mini = (label, fn) => h('button', { class: 'mini', type: 'button', style: 'padding:6px 12px;min-height:32px', onclick: fn }, label);
  const act = async (action, v) => {
    try { await api('/api/agentver/act', { name: r.name, action, version: v }); toast(action === 'rollback' ? 'Rolled back to v' + v : action === 'pin' ? 'Pinned to v' + v : 'Unpinned'); load(); }
    catch (e) { toast(e.message); }
  };
  const diff = async (v, latest) => {
    try {
      const d = await api('/api/agentver/diff?name=' + encodeURIComponent(r.name) + '&a=' + v + '&b=' + latest);
      show.replaceChildren(h('pre', { style: 'font:12px ui-monospace,Menlo,monospace;overflow-x:auto;white-space:pre-wrap;margin:6px 0' }, d.lines.length ? d.lines.join('\n') : 'v' + v + ' and v' + latest + ' are identical.'));
    } catch (e) { toast(e.message); }
  };
  const show = h('div');
  async function load() {
    try {
      const d = await api('/api/agentver/list?name=' + encodeURIComponent(r.name));
      const top = d.versions.length ? d.versions[0].v : 0;
      body.replaceChildren(
        d.note ? h('p', { class: 'muted', style: 'margin:4px 0' }, d.note) : null,
        d.pinned ? h('div', { class: 'formrow' }, h('span', { class: 'muted' }, 'This session is pinned to v' + d.pinned + '.'), mini('Unpin', () => act('unpin'))) : null,
        ...d.versions.map(v => h('div', { style: 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--line)' },
          h('b', null, 'v' + v.v), h('span', { class: 'muted', style: 'flex:1;min-width:140px;font-size:12px' }, v.ts + ' · ' + v.who + ' · ' + v.sha + ' · ' + v.size + ' chars'),
          v.v !== top ? mini('Diff vs latest', () => diff(v.v, top)) : null,
          v.v !== top && !d.plugin ? mini('Roll back', () => { if (confirm('Write v' + v.v + ' back to the agent file? The current text stays as its own version.')) act('rollback', v.v); }) : null,
          d.pinned !== v.v ? mini('Pin', () => act('pin', v.v)) : null)),
        d.versions.length ? null : h('p', { class: 'muted' }, 'No versions recorded yet.'), show);
    } catch (e) { body.replaceChildren(h('p', { class: 'muted' }, e.message)); }
  }
  return h('details', { style: 'margin-top:4px', ontoggle: (ev) => { if (ev.target.open && !loaded) { loaded = true; load(); } } },
    h('summary', { style: 'cursor:pointer;font-size:13px;min-height:32px' }, 'Versions'), body);
}
"""
