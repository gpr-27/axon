"""The app side of Compare models, the cost forecast and the project budgets: routes, script and styles.

Routes (all behind the dashboard's Host, cookie, Origin and CSRF checks):
  GET  /api/compare/models?session=    the models this chat's provider offers (and which have a known price)
  GET  /api/compare/state?id=&offs=    columns of one comparison; ``offs`` is how much text of each column the page has
  POST /api/compare/start              {session, prompt, models[], context}
  POST /api/compare/stop               {id, column?}  one column, or all of them
  POST /api/compare/continue           {id, column}  fork the chat with that model and its answer
  GET  /api/cost/forecast?session=&chars=   the estimate under the message box (null when nothing should show)
  GET  /api/cost/budget                the project's daily and monthly budgets with what was spent
  POST /api/cost/budget                {scope: day|month, amount}  set or (amount "off") clear one

The data lives in compare_models.py and cost_forecast.py; nothing here decides money or models.
"""
from __future__ import annotations

import contextlib
import copy
import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer

PREFIXES = ("/api/compare/", "/api/cost/")


def _s(body: dict[str, Any], key: str, limit: int = 100_000) -> str:
    v = body.get(key, "")
    if not isinstance(v, str):
        raise DashError(f"'{key}' must be a string")
    if len(v) > limit:
        raise DashError(f"'{key}' is too long", 413)
    return v


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v))) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _clean(text: Any, limit: int = 200_000) -> str:
    from axon.features.dashboard import redact_text
    return redact_text(str(text or ""))[:limit]


def _view(snap: dict[str, Any]) -> dict[str, Any]:
    """The comparison as the page gets it: secrets removed from every text the models or providers wrote."""
    for c in snap["columns"]:
        c["delta"] = _clean(c["delta"])
        c["error"] = _clean(c["error"], 400)
        c["tools"] = [{"name": _clean(t["name"], 60), "summary": _clean(t["summary"], 120)} for t in c["tools"]]
    return snap


def _runtime(srv: "DashServer", sid: str) -> Any:
    rt = srv.runtime(sid, create=False)
    if rt is None:
        raise DashError("Open a chat first: a comparison starts from a chat.", 404)
    return rt


def models_view(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import dash_providers as dp
    from axon.providers.pricing import is_priced
    rt = srv.runtime(qs.get("session", ""), create=False)
    data = dp.current_models(srv, rt)
    models = list(data.get("models") or [])
    return {"models": models, "current": data.get("current", ""), "live": bool(data.get("live")),
            "priced": {m: bool(is_priced(m)) for m in models[:400]}, "min": 2, "max": 4}


def start(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import compare_models as cm
    from axon.features import dash_providers as dp
    rt = _runtime(srv, _s(body, "session", 300))
    ready, why = dp.provider_ready(srv, rt)
    if not ready:
        raise DashError(why, 412)
    try:
        run = cm.start_compare(rt.agent, _s(body, "prompt", 60_000), body.get("models"), with_context=body.get("context") is not False,
                               session=rt.session_id)
    except ValueError as e:
        raise DashError(str(e)) from None
    return _view(run.snapshot())


def state(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import compare_models as cm
    run = cm.get_run(qs.get("id", ""))
    if run is None:
        raise DashError("That comparison is gone (the app was restarted). Start it again.", 404)
    offs = [_int(x, 0, 0, 10**9) for x in (qs.get("offs") or "").split(",") if x != ""]
    return _view(run.snapshot(offs))


def stop(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import compare_models as cm
    run = cm.get_run(_s(body, "id", 40))
    if run is None:
        raise DashError("That comparison is gone.", 404)
    col = body.get("column")
    if col is None:
        run.stop_all()
        return {"ok": True}
    if isinstance(col, bool) or not isinstance(col, int):
        raise DashError("column must be a number")
    return {"ok": run.stop(col)}


def continue_with(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    """Fork the chat the comparison started from: its messages, the prompt, and the chosen model's answer, with that model."""
    from axon.agent.state import Conversation
    from axon.commands.model_cmds import switch_model
    from axon.features import compare_models as cm
    from axon.session.interactive import handle_branch
    run = cm.get_run(_s(body, "id", 40))
    if run is None:
        raise DashError("That comparison is gone.", 404)
    col = body.get("column")
    if isinstance(col, bool) or not isinstance(col, int):
        raise DashError("column must be a number")
    if not 0 <= col < len(run.columns):
        raise DashError("There is no such column.")
    try:
        model, prompt, reply = cm.continuation(run, col)
    except ValueError as e:
        raise DashError(str(e), 409) from None
    parent_ws = Path(getattr(run.parent.settings, "workspace", srv.workspace))
    rt = srv._build_runtime(parent_ws if parent_ws.is_dir() else srv.workspace, None)
    try:
        messages = copy.deepcopy(list(run.base))
        messages += [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
        rt.agent.conversation = Conversation(messages)
        with contextlib.redirect_stdout(io.StringIO()):
            if switch_model(rt.agent, model, persist=False) is False:
                raise DashError(f"Could not switch to {model}.", 409)
            handle_branch(rt.agent, f"{model} (from a comparison)")
    except Exception:
        rt.close()
        raise
    rt.key = rt.session_id
    with srv.lock:
        srv.rts[rt.key] = rt
        srv.default_rt = rt
    return rt.state()


def forecast(srv: "DashServer", qs: dict[str, str]) -> dict[str, Any]:
    from axon.features import cost_forecast as cf
    rt = srv.runtime(qs.get("session", ""), create=False)
    if rt is None:
        return {"forecast": None, "none": True}
    try:
        f = cf.forecast_for_agent(rt.agent, _int(qs.get("chars"), 0, 0, 10**7))
    except Exception:
        f = None
    return {"forecast": f if f and f["show"] else None, "threshold": cf.threshold_usd(rt.agent.settings)}


def budget_view(srv: "DashServer") -> dict[str, Any]:
    from axon.features import cost_forecast as cf
    return {"project": srv.workspace.name, **cf.budget_status(srv.workspace), "spend": cf.spend_summary(srv.workspace)}


def budget_set(srv: "DashServer", body: dict[str, Any]) -> dict[str, Any]:
    from axon.features import cost_forecast as cf
    scope = _s(body, "scope", 10)
    if scope not in cf.SCOPES:
        raise DashError("scope must be day or month")
    try:
        cf.set_project_budget(srv.workspace, scope, body.get("amount"))
    except ValueError as e:
        raise DashError(str(e)) from None
    except OSError as e:
        raise DashError(str(e), 500) from None
    return {"ok": True, **budget_view(srv)}


def handle_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any:
    if route == "/api/compare/models":
        return models_view(srv, qs)
    if route == "/api/compare/state":
        return state(srv, qs)
    if route == "/api/cost/forecast":
        return forecast(srv, qs)
    if route == "/api/cost/budget":
        return budget_view(srv)
    raise DashError("not found", 404)


def handle_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any:
    if route == "/api/compare/start":
        return start(srv, body)
    if route == "/api/compare/stop":
        return stop(srv, body)
    if route == "/api/compare/continue":
        return continue_with(srv, body)
    if route == "/api/cost/budget":
        return budget_set(srv, body)
    raise DashError("not found", 404)


COMPARE_CSS = r"""
.cf-fc{box-sizing:border-box;width:calc(100% - 48px);max-width:var(--measure-chat,820px);margin:0 0 6px;padding:0 14px;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:2px 10px;line-height:1.4}
.cf-fc b{color:var(--ink2);font-weight:600}
.cf-dlg{width:min(1180px,100%)}
.cf-dlg .cf-setup{display:grid;gap:10px;margin:10px 0}
.cf-dlg .cf-pick{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.cf-dlg .cf-pick select{max-width:100%;min-width:0}
.cf-dlg textarea{width:100%;min-height:72px;resize:vertical;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.cf-dlg .cf-ro{border:1px solid var(--line);border-left:3px solid var(--ok);border-radius:4px;padding:6px 10px;font-size:12.5px;color:var(--ink2)}
.cf-cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px;margin-top:12px;align-items:start}
.cf-col{border:1px solid var(--line);border-radius:8px;background:var(--surface);min-width:0;display:flex;flex-direction:column}
.cf-col header{padding:8px 10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:2px}
.cf-col header b{overflow-wrap:anywhere}
.cf-col .cf-meta{font-size:12px;color:var(--ink2);display:flex;flex-wrap:wrap;gap:2px 10px;font-variant-numeric:tabular-nums}
.cf-col .cf-tools{font-size:12px;color:var(--muted);padding:6px 10px 0;overflow-wrap:anywhere}
.cf-col pre.cf-out{margin:0;border:0;border-radius:0;background:none;padding:8px 10px;max-height:52vh;min-height:90px;font-family:inherit;font-size:13.5px;line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere}
.cf-col footer{padding:8px 10px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px}
.cf-col .cf-err{color:var(--bad);font-size:12.5px;padding:6px 10px 0}
.cf-total{margin-top:10px;font-size:12.5px;color:var(--ink2)}
.cf-brow{display:grid;grid-template-columns:minmax(0,1fr);gap:6px;padding:10px 0;border-bottom:1px solid var(--line)}
@media (min-width:700px){.cf-brow{grid-template-columns:minmax(150px,1fr) minmax(0,2fr) auto;align-items:center}}
"""


JS_COMPARE = r"""
// ---------- cost forecast under the message box ----------
function cfMoney(n) { return n >= 100 ? '$' + Math.round(n).toLocaleString() : n >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(4); }
function cfForecastLine(C, ta) {
  const box = h('div', { class: 'cf-fc', hidden: true, role: 'note', 'aria-live': 'polite' });
  let timer = 0, seq = 0, last = '';
  const draw = (f) => {
    if (!f) { box.hidden = true; box.replaceChildren(); return; }
    const lo = cfMoney(f.low), hi = cfMoney(f.high);
    box.replaceChildren(h('b', null, 'Estimate ' + (lo === hi ? 'about ' + hi : lo + ' to ' + hi) + ' for this turn'),
      h('span', { title: 'An estimate from the size of this chat and the model price. The real cost can differ. Nothing is blocked.' }, f.basis + '. Only an estimate.'));
    box.hidden = false;
  };
  const ask = async () => {
    if (C.closed || !C.sid || document.hidden) return;
    const mine = ++seq; const key = C.sid + ':' + ta.value.length + ':' + ((C.st && C.st.model) || '') + ':' + ((C.st && C.st.cost) || 0);
    if (key === last && !box.hidden) return; last = key;
    try { const r = await api('/api/cost/forecast?session=' + encodeURIComponent(C.sid) + '&chars=' + ta.value.length); if (mine === seq) draw(r.forecast); } catch (e) { if (mine === seq) draw(null); }
  };
  const soon = (ms) => { clearTimeout(timer); timer = setTimeout(ask, ms); };
  ta.addEventListener('input', () => soon(700));
  const poll = setInterval(() => { if (!(C.st && C.st.busy)) soon(50); else draw(null); }, 5000);
  if (typeof onLeave === 'function') onLeave(() => { clearInterval(poll); clearTimeout(timer); });
  setTimeout(ask, 600);
  return box;
}

// ---------- Today, this week, this month in the Progress panel ----------
function cfSpendRows(spend, kv) {
  const p = spend && spend.project; if (!p) return [];
  const b = spend.budgets || {};
  const row = (k, label, scope) => { const lim = scope && b[scope]; return kv(label, usd(p[k].cost) + (lim ? ' of ' + usd(lim.limit) : ''), 'Spent in this project, from the usage log' + (lim ? ' (budget ' + usd(lim.limit) + ')' : '') + (k === 'week' ? '. The week starts on Monday.' : '')); };
  return [row('today', 'Today', 'day'), row('week', 'This week', ''), row('month', 'This month', 'month')];
}

// ---------- daily and monthly budget of the project (Usage page) ----------
function cfProjectBudgetSection() {
  const sec = h('section', { class: 'panel mg-budget' }, h('h2', null, 'Daily and monthly budget'), h('p', { class: 'muted' }, 'Loading...'));
  const draw = async () => {
    let d; try { d = await api('/api/cost/budget'); } catch (e) { sec.replaceChildren(h('h2', null, 'Daily and monthly budget'), errorBox(e.message)); return; }
    sec.replaceChildren(h('h2', null, 'Daily and monthly budget'),
      h('p', { class: 'muted' }, 'A budget for the whole project, counted from the usage log: every chat and every comparison in ' + d.project + '. New turns stop once it is used up, and you get a quiet notice at 50%, 80% and 100%.'));
    for (const [scope, title] of [['day', 'Daily budget'], ['month', 'Monthly budget']]) {
      const r = d[scope];
      const input = h('input', { type: 'text', inputmode: 'decimal', placeholder: 'e.g. 5', 'aria-label': title + ' in dollars', size: 8, value: r ? String(r.limit) : '' });
      const act = async (amount) => { try { await api('/api/cost/budget', { scope, amount }); mgNotice(amount === 'off' ? title + ' removed.' : title + ' set to ' + usd(Number(amount)) + '.'); draw(); } catch (e) { mgNotice(e.message, { error: true }); } };
      const frac = r ? Math.min(1, r.frac) : 0;
      const sp = d.spend.project[scope === 'day' ? 'today' : 'month'].cost;
      sec.append(h('div', { class: 'cf-brow mg-brow' },
        h('div', { class: 'mg-bt' }, h('b', null, title), h('span', { class: 'muted' }, scope === 'day' ? ' (today)' : ' (this month)')),
        h('div', { class: 'mg-bn' }, 'Spent ' + usd(sp) + ' · ' + (r ? 'limit ' + usd(r.limit) + ', ' + usd(r.remaining) + ' left' : 'no limit set'),
          r ? h('div', { class: 'gauge' + (frac >= 1 ? ' bad' : frac > 0.75 ? ' warn' : ''), style: 'margin-left:10px' }, h('i', { style: 'width:' + Math.round(frac * 100) + '%' })) : null),
        h('div', { class: 'mg-bc' }, h('span', null, '$'), input,
          h('button', { class: 'btn primary', type: 'button', onclick: () => { if (!input.value.trim()) { mgNotice('Type an amount in dollars.', { error: true }); return; } act(input.value.trim().replace(/^\$/, '')); } }, 'Set limit'),
          r ? h('button', { class: 'btn', type: 'button', onclick: () => act('off') }, 'Remove limit') : null)));
    }
  };
  draw();
  return sec;
}

// ---------- Compare models ----------
function cfCompareOpen(C) {
  if (!C || !C.sid) { toast('Open a chat first.'); return; }
  if (document.querySelector('.cf-dlg')) return;
  const NOTE = 'Read-only: each model can read files and search this project, but cannot change files, run commands or use the network. Nothing in your project changes.';
  let run = null, offs = [], poll = 0, closed = false;
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Compare models' });
  const picks = h('div', { class: 'cf-pick' });
  const prompt = h('textarea', { 'aria-label': 'Prompt for every model', placeholder: 'Ask something. Every model gets exactly this.', maxlength: 50000 });
  prompt.value = (C.ta && C.ta.value) || '';
  const ctx = h('input', { type: 'checkbox', id: 'cf-ctx', checked: true });
  const go = h('button', { class: 'btn primary', type: 'button' }, 'Compare');
  const out = h('div', { class: 'cf-cols' }); const total = h('div', { class: 'cf-total', hidden: true });
  const choices = []; let all = [];
  const drawPicks = () => {
    picks.replaceChildren(...choices.map((c, i) => h('span', { class: 'cf-pick' },
      h('select', { 'aria-label': 'Model ' + (i + 1), onchange: (e) => { choices[i] = e.target.value; } }, ...all.map(m => h('option', { value: m.name, selected: m.name === c }, m.name + (m.priced ? '' : '  (no price known)')))),
      choices.length > 2 ? h('button', { class: 'btn', type: 'button', 'aria-label': 'Remove model ' + (i + 1), onclick: () => { choices.splice(i, 1); drawPicks(); } }, 'Remove') : null)),
      choices.length < 4 ? h('button', { class: 'btn', type: 'button', onclick: () => { choices.push((all.find(m => !choices.includes(m.name)) || all[0]).name); drawPicks(); } }, 'Add a model') : null);
  };
  const close = () => { closed = true; clearTimeout(poll); if (run) api('/api/compare/stop', { id: run }).catch(() => { }); scrim.remove(); document.removeEventListener('keydown', onkey, true); };
  const onkey = (e) => { if (e.key === 'Escape' && document.body.contains(scrim)) { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', onkey, true);
  scrim.addEventListener('mousedown', (e) => { if (e.target === scrim) close(); });
  const cols = {};
  const colEl = (c) => {
    if (cols[c.idx]) return cols[c.idx];
    const el = { root: h('section', { class: 'cf-col', 'aria-label': c.model }), name: h('b', null, c.model), meta: h('div', { class: 'cf-meta' }), tools: h('div', { class: 'cf-tools', hidden: true }),
      err: h('div', { class: 'cf-err', hidden: true, role: 'alert' }), pre: h('pre', { class: 'cf-out', tabindex: '0', 'aria-live': 'off' }), foot: h('footer'), text: '' };
    el.root.append(h('header', null, el.name, el.meta), el.tools, el.err, el.pre, el.foot); cols[c.idx] = el; return el;
  };
  const stopBtn = (c) => h('button', { class: 'btn', type: 'button', onclick: async () => { try { await api('/api/compare/stop', { id: run, column: c.idx }); } catch (e) { toast(e.message); } } }, 'Stop');
  const contBtn = (c) => h('button', { class: 'btn primary', type: 'button', title: 'Open a new chat with the conversation so far, this prompt and this answer, using ' + c.model, onclick: async () => {
    try { toast('Forking this chat…'); const st = await api('/api/compare/continue', { id: run, column: c.idx }); close(); location.hash = '#/chat/' + encodeURIComponent(st.session || st.key); }
    catch (e) { toast(e.message); } } }, 'Continue with this one');
  const paint = (s) => {
    const word = { queued: 'waiting', running: 'answering', done: 'done', error: 'failed', stopped: 'stopped' };
    for (const c of s.columns) {
      const el = colEl(c); if (!out.contains(el.root)) out.append(el.root);
      el.text += c.delta; offs[c.idx] = c.length;
      if (c.delta) { el.pre.textContent = el.text; if (c.running) el.pre.scrollTop = el.pre.scrollHeight; }
      if (!c.running && !el.text) el.pre.textContent = c.status === 'stopped' ? 'Stopped before an answer.' : (c.error ? '' : '(no answer)');
      el.meta.replaceChildren(h('span', null, word[c.status] || c.status), h('span', null, c.seconds.toFixed(1) + 's' + (c.first_token_s != null && c.first_token_s >= 0.1 ? ' (first words after ' + c.first_token_s.toFixed(1) + 's)' : '')),
        h('span', null, tok(c.input) + ' in, ' + tok(c.output) + ' out'), h('span', null, c.cost == null ? 'no price known' : usd(c.cost)));
      el.tools.hidden = !c.tools.length; if (c.tools.length) el.tools.textContent = 'Looked at: ' + c.tools.slice(-5).map(t => (t.name + ' ' + t.summary).trim()).join(', ');
      el.err.hidden = !c.error; el.err.textContent = c.error || '';
      el.foot.replaceChildren(...(c.running ? [stopBtn(c)] : (c.status !== 'error' && el.text.trim() ? [contBtn(c)] : [])));
    }
    total.hidden = false;
    total.textContent = (s.done ? 'Done. ' : 'Running. ') + 'Cost of this comparison ' + usd(s.total_cost) + ', counted in Usage as compare.' + (s.unpriced ? ' ' + s.unpriced + ' model(s) have no known price and are not counted.' : '');
  };
  const tick = async () => {
    if (closed || !run) return;
    try { const s = await api('/api/compare/state?id=' + run + '&offs=' + offs.join(',')); paint(s); go.disabled = !s.done; go.textContent = 'Compare'; if (!s.done) poll = setTimeout(tick, 350); }
    catch (e) { total.hidden = false; total.textContent = e.message; go.disabled = false; }
  };
  go.addEventListener('click', async () => {
    const text = prompt.value.trim(); if (!text) { toast('Type the prompt first.'); prompt.focus(); return; }
    if (new Set(choices).size < 2) { toast('Pick two different models.'); return; }
    go.disabled = true; out.replaceChildren(); for (const k of Object.keys(cols)) delete cols[k]; offs = [];
    try { const s = await api('/api/compare/start', { session: C.sid, prompt: text, models: choices, context: ctx.checked }); run = s.id; paint(s); poll = setTimeout(tick, 350); }
    catch (e) { go.disabled = false; toast(e.message); total.hidden = false; total.textContent = e.message; }
  });
  scrim.append(h('div', { class: 'modal cf-dlg' }, h('h3', null, 'Compare models'),
    h('div', { class: 'cf-ro' }, NOTE),
    h('div', { class: 'cf-setup' }, picks, prompt,
      h('label', { for: 'cf-ctx' }, ctx, ' Include this chat so far, as plain text, so every model answers in the same situation'),
      h('div', { class: 'acts' }, go, h('button', { class: 'btn', type: 'button', onclick: close }, 'Close'), h('span', { class: 'muted' }, 'Each answer costs what its model charges. Closing stops any model still answering.'))),
    out, total));
  document.body.append(scrim);
  (async () => {
    try {
      const d = await api('/api/compare/models?session=' + encodeURIComponent(C.sid));
      all = d.models.map(n => ({ name: n, priced: !!d.priced[n] }));
      if (all.length < 2) { total.hidden = false; total.textContent = 'This provider lists fewer than two models. Add another provider in Settings, then compare.'; go.disabled = true; return; }
      const first = d.current && all.find(m => m.name === d.current) ? d.current : all[0].name;
      choices.push(first, (all.find(m => m.name !== first && m.priced) || all.find(m => m.name !== first)).name); drawPicks(); prompt.focus();
    } catch (e) { total.hidden = false; total.textContent = e.message; go.disabled = true; }
  })();
}
"""
