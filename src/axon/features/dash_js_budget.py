"""Web side of the spend cap and the notification sinks: the remaining-budget chip, the 'Raise budget' action when the
work is paused, and in-page alerts for events the page has no other signal for. Appended to the app script by
``dash_js_chatx``. The page only ever shows the one-line summaries the server already redacted.

iOS Safari delivers no web push unless the page is an installed app, so the promise here is only what works with the
tab open: a notice in the page, a changed tab title, and a browser notification where the browser allows one.
"""
from __future__ import annotations

BUDGET_CSS = r"""
.fw-budget{max-width:9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fw-budget.paused{color:var(--warn)}
.budgetnote{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
"""

JS_BUDGET = r"""
// ----- spend cap: one chip, and a Raise action when the work is paused (agent/budget_pause.py) -----
async function budgetRaise(C, usd) {
  try { await runCmd(C, '/budget raise ' + usd); toast('Budget raised by $' + usd + '; continuing'); } catch (e) { toast(e.message); }
}
function budgetMenu(C, anchor) {
  const b = C.st && C.st.budget; if (!b) return;
  const items = [{ head: b.paused ? 'Session budget reached' : 'Budget' },
    { label: b.message || (b.chip + (b.limit ? ' of $' + Number(b.limit).toFixed(2) : '')) }, '-'];
  [1, 5, 20].forEach(n => items.push({ label: 'Raise by $' + n + (b.paused ? ' and continue' : ''), run: () => budgetRaise(C, n) }));
  items.push('-', { label: 'Remove the cap', run: async () => { try { await runCmd(C, '/budget off'); toast('Cap removed'); } catch (e) { toast(e.message); } } });
  wbMenu(anchor, items, { up: true });
}
function budgetPaused(C, ev) {
  const host = C.cur ? hostOf(C) : C.feed;
  const raise = h('button', { class: 'btn', type: 'button', onclick: () => budgetRaise(C, 5) }, 'Raise budget by $5');
  host.append(h('div', { class: 'notice warn budgetnote' }, h('span', null, ev.message || 'Session budget reached'), C.replaying ? null : raise));
  if (C.st) { try { refreshState(C, true); } catch (e) { /* the next state poll catches up */ } }
}
const budgetFooterDraw0 = wbFooterDraw;
wbFooterDraw = function (C) { budgetFooterDraw0(C); budgetChipDraw(C); };
function budgetChipDraw(C) {
  const f = C.foot; if (!f) return; const b = C.st && C.st.budget;
  if (!f.budget) {
    f.budget = h('button', { class: 'fw fw-budget', type: 'button', 'aria-haspopup': 'menu', hidden: true });
    f.mode.after(f.budget);
    f.budget.addEventListener('click', () => budgetMenu(C, f.budget));
  }
  f.budget.hidden = !b || !b.chip;
  if (!b || !b.chip) return;
  f.budget.className = 'fw fw-budget' + (b.paused ? ' paused' : '');
  f.budget.textContent = b.chip;
  f.budget.title = b.message || 'Remaining session budget';
}
// A notification sink event for this session (server: features/notify.py). Approvals, questions and errors already
// raise their own alerts, so only the events the page has no other signal for are handled here.
function budgetNotify(C, ev) {
  const e = ev.event || {}; if (C.replaying) return;
  const t = e.type;
  if (t === 'budget_reached') axnTell('Axon budget reached', e.summary, 'axon-budget');
  else if (t === 'subagent_finished') axnTell('Axon subagent finished', e.summary, 'axon-sub');
  else if (t === 'run_failed') axnTell('Axon stopped', e.summary, 'axon-turn');
  else if (t === 'test') axnTell('Axon test', e.summary, 'axon-test', true);
}
"""
