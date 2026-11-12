"""The web chat's done-when goal: the GOAL block of a grader check and the goal chip in the composer footer.

``goalCheckEvent`` draws one block per check (per-criterion ✓/✗ with its one-line evidence or gap, round N of M, and the
grader's own model/tokens/cost on one line). ``goalChipDraw`` shows the active goal next to the mode button; its menu
shows the last verdict and clears the goal. Setting a goal is the ``/goal`` command typed in the composer.
Appended to the app script by ``dash_js_chatx``.
"""
from __future__ import annotations

GOAL_CSS = r"""
.goalblk{border:1px solid var(--line);border-left:3px solid var(--primary);border-radius:6px;padding:8px 12px;margin:10px 0;background:var(--surface);font-size:13px}
.goalblk.met{border-left-color:var(--ok)}.goalblk.rev{border-left-color:var(--warn)}.goalblk.bad{border-left-color:var(--bad)}.goalblk.skip{border-left-color:var(--line);color:var(--muted)}
.goalblk .gh{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.goalblk .gh b{color:var(--primary);letter-spacing:.06em;font-size:12px}
.goalblk .gs{color:var(--ink)}.goalblk .gr{color:var(--muted);font-size:12px}
.goalblk ul{list-style:none;margin:6px 0 0;padding:0;display:grid;gap:4px}
.goalblk li{display:grid;grid-template-columns:1.2em 1fr;gap:0 6px;align-items:baseline}
.goalblk li.ok .gm{color:var(--ok)}.goalblk li.no .gm{color:var(--bad)}
.goalblk .gw{display:block;color:var(--muted);font-size:12.5px;grid-column:2}
.goalblk .gmeta{margin-top:6px;color:var(--muted);font-size:12px}
.fw-goal{max-width:11rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fw-goal.met{color:var(--ok)}.fw-goal.rev{color:var(--warn)}.fw-goal.bad{color:var(--bad)}
@media (max-width:640px){.fw-goal{max-width:7rem}}
"""

JS_GOAL = r"""
// ----- done-when goal: the GOAL block and the composer chip (agent/goal.py is the grader) -----
function goalCheckEvent(C, ev) {
  if (ev.phase === 'start') return;                       // the block below says it once, with the verdict
  const host = C.cur ? hostOf(C) : C.feed;
  if (ev.phase === 'skipped') { host.append(h('div', { class: 'goalblk skip' }, h('b', null, 'GOAL'), ' not checked: ' + (ev.note || ''))); return; }
  const label = { satisfied: 'goal met', needs_revision: 'not done yet', failed_unrecoverable: 'cannot be met as it stands', interrupted: 'check cancelled' }[ev.verdict] || String(ev.verdict || '');
  const cls = ev.verdict === 'satisfied' ? 'met' : ev.verdict === 'needs_revision' ? 'rev' : 'bad';
  const rows = (ev.criteria || []).map(c => { const why = c.met ? c.evidence : (c.gap || c.evidence);
    return h('li', { class: c.met ? 'ok' : 'no' }, h('span', { class: 'gm', 'aria-label': c.met ? 'met' : 'not met' }, c.met ? '✓' : '✗'), h('span', null, c.criterion, why ? h('span', { class: 'gw' }, why) : null)); });
  const u = ev.usage || {};
  const meta = ['goal check', ev.model, (u.input_tokens != null ? tok(u.input_tokens || 0) + ' in / ' + tok(u.output_tokens || 0) + ' out' : ''), ev.cost ? usd(ev.cost) : '', ev.duration_s ? Number(ev.duration_s).toFixed(1) + 's' : ''].filter(Boolean).join(' · ');
  host.append(h('div', { class: 'goalblk ' + cls, role: 'group', 'aria-label': 'Goal check' },
    h('div', { class: 'gh' }, h('b', null, 'GOAL'), h('span', { class: 'gs' }, label), ev.round && ev.verdict !== 'interrupted' ? h('span', { class: 'gr' }, 'round ' + ev.round + '/' + ev.max_rounds) : null),
    rows.length ? h('ul', null, ...rows) : (ev.summary ? h('span', { class: 'gw' }, ev.summary) : null),
    ev.note && ev.note !== 'goal met' && ev.verdict !== 'needs_revision' ? h('span', { class: 'gw' }, ev.note) : null,
    h('div', { class: 'gmeta' }, meta)));
  if (C.cur && ev.cost) { C.cur.cost = (C.cur.cost || 0) + ev.cost; try { headMeta(C.cur, false); } catch (e) { /* header not drawn yet */ } }
  if (C.st) { try { refreshState(C, true); } catch (e) { /* the next state poll catches up */ } }
}
const goalFooterDraw0 = wbFooterDraw;
wbFooterDraw = function (C) { goalFooterDraw0(C); goalChipDraw(C); };
function goalChipDraw(C) {
  const f = C.foot; if (!f) return; const g = C.st && C.st.goal;
  if (!f.goal) {
    f.goal = h('button', { class: 'fw fw-goal', type: 'button', 'aria-haspopup': 'menu', hidden: true });
    f.mode.after(f.goal);
    f.goal.addEventListener('click', () => { const s = C.st && C.st.goal; if (!s) return; const last = s.last;
      const items = [{ head: 'Goal' }, { label: s.text, hint: 'Checked after each turn, up to ' + s.rounds + ' rounds' }];
      if (last) { items.push('-', { head: 'Last check: ' + String(last.verdict || '').replace(/_/g, ' ') + ' (round ' + last.round + '/' + last.max_rounds + ')' }); (last.per_criterion || []).forEach(c => items.push({ label: (c.met ? '✓ ' : '✗ ') + c.criterion, hint: c.met ? c.evidence : c.gap })); }
      items.push('-', { label: 'Clear goal', run: async () => { try { await runCmd(C, '/goal clear'); toast('Goal cleared'); } catch (e) { toast(e.message); } } });
      wbMenu(f.goal, items, { up: true }); });
  }
  const b = f.goal; b.hidden = !g;
  if (!g) return;
  const v = g.last && g.last.verdict;
  b.className = 'fw fw-goal' + (v === 'satisfied' ? ' met' : v === 'needs_revision' ? ' rev' : v ? ' bad' : '');
  b.textContent = 'Goal' + (g.last ? (v === 'satisfied' ? ' ✓' : ' ✗ ' + g.last.round + '/' + g.last.max_rounds) : '');
  b.title = 'Goal: ' + g.text;
}
"""
