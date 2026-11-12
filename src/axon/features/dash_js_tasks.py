"""The app side of scheduled tasks and notifications.

* ``api_get`` / ``api_post``: the ``/api/tasks`` routes (called from dash_api.py after the Host, cookie, Origin and
  CSRF checks). All logic lives in ``axon.features.scheduler``; this only checks input and shapes the answers.
* ``JS_TASKS``: the Tasks panel (More menu) and the Notifications card in Settings. Server text is only ever put in
  with text nodes (``h`` children), never innerHTML.
* ``TASKS_CSS``: the few rules those two need.

Notifications never carry file contents or secrets: the words come from this file, plus a task name that has been
checked by ``scheduler.notice_name``.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from axon.features import scheduler as sch
from axon.features.dash_common import DashError

if TYPE_CHECKING:  # pragma: no cover
    from axon.features.dashboard import DashServer

_PUBLIC = ("id", "name", "prompt", "folder", "kind", "every_s", "at", "days", "once_at", "mode", "budget_usd", "catch_up",
           "enabled", "when", "state", "running", "next_at", "last_run", "last_status", "created", "cron", "tz", "cap_off", "max_tokens")
_RUN = ("run", "task", "name", "status", "reason", "started", "ended", "cost_usd", "session_id", "error", "missed", "mode", "outcome", "trigger", "failure", "duration_s", "tokens_in", "tokens_out")


def _task(t: dict[str, Any]) -> dict[str, Any]:
    return {k: t.get(k) for k in _PUBLIC}


def _run(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in _RUN}


def _fields(body: dict[str, Any]) -> dict[str, Any]:
    f = {k: body[k] for k in sch._EDITABLE if k in body}
    if "name" in f and not isinstance(f["name"], str) or "prompt" in f and not isinstance(f["prompt"], str):
        raise DashError("name and prompt must be text")
    return f


def _id(body: dict[str, Any]) -> str:
    v = body.get("id")
    if not isinstance(v, str) or not v or len(v) > 40:
        raise DashError("id must be a task id")
    return v


def api_get(srv: "DashServer", route: str, qs: dict[str, str]) -> Any | None:
    s = sch.get_scheduler()
    if route == "/api/tasks":
        return {"tasks": [_task(t) for t in s.tasks()], "folder": str(srv.workspace), "loop": s.active,
                "modes": [{"id": k, "label": v[1], "help": v[2]} for k, v in sch.MODES.items()],
                "default_mode": sch.DEFAULT_MODE, "default_budget": sch.DEFAULT_BUDGET_USD, "max_budget": sch.MAX_BUDGET_USD,
                "min_interval_s": sch.MIN_INTERVAL_S, "now": time.time()}
    if route == "/api/tasks/history":
        try:
            limit = max(1, min(200, int(qs.get("limit") or 30)))
        except ValueError:
            limit = 30
        return {"runs": [_run(r) for r in s.history(qs.get("id") or None, limit)]}
    if route == "/api/tasks/next":
        from axon.features import schedule_ext as ext
        t = s.get(qs.get("id") or "")
        if t is None:
            raise DashError("No such task", 404)
        fires = ext.next_fires(t, time.time(), 3) if t.get("enabled", True) else []
        return {"fires": [{"at": f, "label": ext.local_wall(f, t.get("tz"))} for f in fires]}
    if route == "/api/tasks/recent":
        try:
            since = float(qs.get("since") or 0)
        except ValueError:
            since = 0.0
        runs = [r for r in s.history(None, 30) if r.get("status") not in ("running",) and float(r.get("ended") or 0) > since]
        return {"runs": [{"run": r["run"], "name": sch.notice_name(r.get("name")), "status": r.get("status"), "ended": r.get("ended")}
                         for r in runs], "now": time.time()}
    return None


def api_post(srv: "DashServer", route: str, body: dict[str, Any]) -> Any | None:
    s = sch.get_scheduler()
    try:
        if route == "/api/tasks/add":
            fields = _fields(body)
            fields.setdefault("folder", str(srv.workspace))
            return _task(s.get(s.add(fields)["id"]) or {})
        if route == "/api/tasks/update":
            tid = _id(body)
            s.update(tid, _fields(body))
            return _task(s.get(tid) or {})
        if route == "/api/tasks/pause":
            tid = _id(body)
            s.set_enabled(tid, body.get("paused") is not True)
            return _task(s.get(tid) or {})
        if route == "/api/tasks/delete":
            if body.get("confirm") is not True:
                raise DashError("Deleting a task needs confirm: true")
            return {"ok": s.delete(_id(body))}
        if route == "/api/tasks/run":
            return {"ok": True, "run": _run(s.run_now(_id(body)))}
        if route == "/api/tasks/stop":
            return {"ok": s.stop_run(_id(body))}
    except sch.ScheduleError as e:
        raise DashError(str(e), 400) from None
    return None


TASKS_CSS = r"""
.tk{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0 0 10px;background:var(--surface)}
.tk.paused{opacity:.75}
.tk h4{margin:0;font-size:13.5px;font-weight:600;overflow-wrap:anywhere}
.tk .meta{color:var(--ink2);font-size:12.5px;margin:2px 0;overflow-wrap:anywhere}
.tk .acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.tk .st{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:0 8px;margin-left:6px;color:var(--ink2);white-space:nowrap}
.tk .st.ok{color:var(--ok);border-color:var(--ok)}.tk .st.bad{color:var(--bad);border-color:var(--bad)}.tk .st.run{color:var(--primary);border-color:var(--primary)}
.tk .runs{margin-top:8px;border-top:1px solid var(--line);padding-top:6px;font-size:12.5px}
.tk .runs .r{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;padding:2px 0}
.tk .runs .r .num{margin-left:auto;color:var(--ink2)}
.tkform{display:grid;gap:10px;margin:8px 0}
.tkform label{display:grid;gap:3px;font-size:12.5px;color:var(--ink2)}
.tkform input,.tkform select,.tkform textarea{font-size:16px}
.tk .runs .r.tap{cursor:pointer}.tk .runs .det{flex-basis:100%;color:var(--ink2);overflow-wrap:anywhere}
.tkform input[type=text],.tkform input[type=number],.tkform input[type=time],.tkform input[type=datetime-local],.tkform select,.tkform textarea{background:var(--page);border:1px solid var(--line);border-radius:5px;padding:6px 9px;color:var(--ink);width:100%}
.tkform textarea{min-height:84px;resize:vertical}
.tkform .inl{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tkform .inl label{display:flex;gap:5px;align-items:center}
.tkform .inl input,.tkform .inl select,.tkform input[type=checkbox]{width:auto}
.tkmodal{width:min(560px,100%)}
.ntf label.chk{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
.ntf input[type=number]{width:84px;background:var(--page);border:1px solid var(--line);border-radius:5px;padding:4px 8px;color:var(--ink)}
"""

JS_TASKS = r"""
// ---------- scheduled tasks (More > Tasks) ----------
const TK = { data: null, open: {}, timer: 0 };
const TK_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function tkWhen(ts) { return ts ? new Date(ts * 1000).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'none'; }
function tkStatusWord(s) { return ({ succeeded: 'Succeeded', budget_exceeded: 'Stopped at its cap', cancelled: 'Cancelled', ok: 'Succeeded', failed: 'Failed', budget: 'Stopped at its spending limit', stopped: 'Stopped', timeout: 'Timed out', missed: 'Missed', skipped: 'Skipped', running: 'Running', interrupted: 'Interrupted' })[s] || s || ''; }
function tkStatusCls(s) { return (s === 'ok' || s === 'succeeded') ? 'ok' : (s === 'failed' || s === 'budget' || s === 'budget_exceeded' || s === 'timeout' || s === 'interrupted') ? 'bad' : s === 'running' ? 'run' : ''; }
function tkLocalInput(ts) { const d = new Date(ts * 1000); const p = (n) => String(n).padStart(2, '0'); return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()); }

function tasksMount(P, extra) {
  const wrap = h('div', { class: 'wp-pad' });
  P.body.replaceChildren(wrap);
  const draw = async () => {
    let d; try { d = await api('/api/tasks'); } catch (e) { wrap.replaceChildren(errorBox(e.message)); return; }
    TK.data = d;
    const list = d.tasks.map(t => tkCard(t, d, draw));
    wrap.replaceChildren(
      h('div', { class: 'formrow', style: 'justify-content:space-between;margin-top:0' }, h('b', null, 'Scheduled tasks'), h('button', { class: 'btn primary', type: 'button', onclick: () => tkEdit(null, d, draw) }, 'New task')),
      h('p', { class: 'muted' }, 'A task runs a prompt in a folder on a schedule. Tasks run only while Axon is open (this app or a terminal window). There is no background service: when Axon is closed nothing runs, and runs that were missed are recorded, not replayed.'),
      ...(list.length ? list : [h('p', { class: 'muted' }, 'No tasks yet.')]),
      extra ? h('h2', { style: 'margin:18px 0 8px' }, 'Background commands') : null, extra || null);
  };
  P.onShow = draw; P.redraw = () => { if (!document.querySelector('.tkmodal')) draw(); };
  clearInterval(TK.timer);
  TK.timer = setInterval(() => { if (!wrap.isConnected) { clearInterval(TK.timer); return; } if (!document.hidden && !document.querySelector('.tkmodal')) draw(); }, 5000);
  draw();
}

function tkCard(t, d, redraw) {
  const act = (label, fn, cls) => h('button', { class: 'mini' + (cls ? ' ' + cls : ''), type: 'button', onclick: async () => { try { await fn(); } catch (e) { toast(e.message); } redraw(); } }, label);
  const modeLabel = (d.modes.find(m => m.id === t.mode) || {}).label || t.mode;
  const state = t.running ? 'Running' : !t.enabled ? 'Paused' : t.next_at ? 'Next ' + tkWhen(t.next_at) : 'Finished';
  const runs = h('div', { class: 'runs', hidden: !TK.open[t.id] });
  const loadRuns = async () => {
    try {
      const r = await api('/api/tasks/history?id=' + encodeURIComponent(t.id) + '&limit=15');
      const quiet = (x) => x.status === 'missed' || x.status === 'skipped';
      runs.replaceChildren(...(r.runs.length ? r.runs.map(x => {
        const det = h('div', { class: 'det', hidden: true }, [x.failure ? 'Reason: ' + x.failure : '', x.duration_s ? 'Took ' + dur(x.duration_s) : '', (x.tokens_in || x.tokens_out) ? ((x.tokens_in || 0) + (x.tokens_out || 0)).toLocaleString() + ' tokens' : '', x.ended ? 'Ended ' + tkWhen(x.ended) : ''].filter(Boolean).join(' · '));
        return h('div', { class: 'r tap', onclick: (e) => { if (e.target.tagName !== 'A') det.hidden = !det.hidden; } },
          h('span', { class: 'st ' + tkStatusCls(x.outcome || x.status) }, tkStatusWord(x.outcome || x.status)), h('span', null, tkWhen(x.started)),
          x.trigger === 'manual' ? h('span', { class: 'muted' }, 'run by hand') : x.reason === 'catch-up' ? h('span', { class: 'muted' }, 'catch-up') : null,
          x.session_id ? h('a', { href: '#/chat/' + encodeURIComponent(x.session_id), title: 'Open this run as a chat' }, 'Transcript') : null,
          h('span', { class: 'num' }, quiet(x) ? '' : usd(x.cost_usd || 0)),
          x.error ? h('div', { class: 'muted', style: 'flex-basis:100%' }, x.error) : null, quiet(x) ? null : det);
      }) : [h('span', { class: 'muted' }, 'Nothing has run yet.')]));
    } catch (e) { runs.replaceChildren(errorBox(e.message)); }
  };
  if (TK.open[t.id]) loadRuns();
  return h('div', { class: 'tk' + (t.enabled ? '' : ' paused') },
    h('h4', null, t.name, t.last_status ? h('span', { class: 'st ' + tkStatusCls(t.last_status) }, tkStatusWord(t.last_status)) : null, t.running ? h('span', { class: 'st run' }, 'Running') : null),
    h('div', { class: 'meta' }, t.when + ' · ' + state),
    h('div', { class: 'meta', title: t.folder }, t.folder),
    h('div', { class: 'meta' }, modeLabel + ' · ' + (t.cap_off ? 'no cap' : 'up to ' + usd(t.budget_usd) + (t.max_tokens ? ' / ' + t.max_tokens.toLocaleString() + ' tokens' : '')) + ' per run'),
    h('div', { class: 'acts' },
      act('Run now', async () => { await api('/api/tasks/run', { id: t.id }); toast('Started'); }),
      t.running ? act('Stop', async () => { await api('/api/tasks/stop', { id: t.id }); }) : null,
      act(t.enabled ? 'Pause' : 'Resume', async () => { await api('/api/tasks/pause', { id: t.id, paused: t.enabled }); }),
      h('button', { class: 'mini', type: 'button', onclick: () => tkEdit(t, d, redraw) }, 'Edit'),
      h('button', { class: 'mini', type: 'button', onclick: () => { TK.open[t.id] = !TK.open[t.id]; runs.hidden = !TK.open[t.id]; if (TK.open[t.id]) loadRuns(); } }, 'History'),
      h('button', { class: 'mini', type: 'button', onclick: async () => {
        if (!(await wbConfirm('Delete this task?', 'It stops running and its schedule is removed. The sessions it already made stay in Sessions.', 'Delete', true))) return;
        try { await api('/api/tasks/delete', { id: t.id, confirm: true }); } catch (e) { toast(e.message); } redraw(); } }, 'Delete')),
    runs);
}

function tkEdit(t, d, redraw) {
  const isNew = !t; t = t || { name: '', prompt: '', folder: d.folder, kind: 'daily', at: '09:00', days: [0, 1, 2, 3, 4], every_s: 3600, mode: d.default_mode, budget_usd: d.default_budget, catch_up: true };
  const name = h('input', { type: 'text', value: t.name, maxlength: 80, placeholder: 'Optional. Defaults to the start of the prompt.', 'aria-label': 'Task name' });
  const prompt = h('textarea', { 'aria-label': 'What the task should do', placeholder: 'For example: Read the changes from the last day and write a short summary to SUMMARY.md' }, t.prompt);
  const folder = h('input', { type: 'text', value: t.folder, 'aria-label': 'Folder' });
  const kind = h('select', { 'aria-label': 'How often' }, ...[['interval', 'Every so often'], ['daily', 'Every day'], ['weekly', 'On certain days'], ['once', 'Once'], ['cron', 'Cron (advanced)']].map(([v, l]) => h('option', { value: v, selected: t.kind === v }, l)));
  const ev = t.every_s || 3600; const unit = ev % 86400 === 0 ? 86400 : ev % 3600 === 0 ? 3600 : 60;
  const num = h('input', { type: 'number', min: 1, step: 1, value: Math.round(ev / unit), 'aria-label': 'Interval length' });
  const per = h('select', { 'aria-label': 'Interval unit' }, ...[[60, 'minutes'], [3600, 'hours'], [86400, 'days']].map(([v, l]) => h('option', { value: v, selected: v === unit }, l)));
  const at = h('input', { type: 'time', value: t.at || '09:00', 'aria-label': 'Time of day' });
  const days = TK_DAYS.map((n, i) => h('input', { type: 'checkbox', checked: (t.days || [0, 1, 2, 3, 4]).includes(i), 'aria-label': n }));
  const when = h('input', { type: 'datetime-local', value: tkLocalInput(t.once_at || (Date.now() / 1000 + 3600)), 'aria-label': 'Date and time' });
  const cron = h('input', { type: 'text', value: t.cron || '30 9 * * 1-5', placeholder: 'minute hour day month weekday', 'aria-label': 'Cron schedule' });
  const tz = h('input', { type: 'text', value: t.tz || '', placeholder: 'Timezone, e.g. Europe/London (blank: this computer)', 'aria-label': 'Timezone' });
  const rowCron = h('div', { class: 'inl' }, cron);
  const capOff = h('input', { type: 'checkbox', checked: !!t.cap_off, 'aria-label': 'No spending cap' });
  const maxTok = h('input', { type: 'number', min: 1000, step: 1000, value: t.max_tokens || '', placeholder: 'Optional', 'aria-label': 'Token cap per run' });
  const rowEvery = h('div', { class: 'inl' }, 'Every', num, per), rowAt = h('div', { class: 'inl' }, 'At', at);
  const rowDays = h('div', { class: 'inl' }, ...TK_DAYS.map((n, i) => h('label', null, days[i], n))), rowOnce = h('div', { class: 'inl' }, 'On', when);
  const mode = h('select', { 'aria-label': 'What the task may do' }, ...d.modes.map(m => h('option', { value: m.id, selected: t.mode === m.id }, m.label)));
  const modeHelp = h('div', { class: 'muted' });
  const ack = h('input', { type: 'checkbox', 'aria-label': 'I understand the risk' });
  const warn = h('div', { class: 'notice bad', hidden: true }, h('b', null, 'Full access runs everything with no checks. '), 'Nobody is watching a scheduled run, so it can change or delete files and run any command in that folder. Use it only in a folder you could lose. ',
    h('label', { class: 'inl', style: 'display:flex;gap:6px;margin-top:6px' }, ack, 'I understand and want this.'));
  const budget = h('input', { type: 'number', min: 0.01, max: d.max_budget, step: 0.05, value: t.budget_usd, 'aria-label': 'Spending limit per run in dollars' });
  const catchUp = h('input', { type: 'checkbox', checked: !!t.catch_up, 'aria-label': 'Run once if missed' });
  const err = h('div');
  const sync = () => {
    rowEvery.hidden = kind.value !== 'interval'; rowAt.hidden = !(kind.value === 'daily' || kind.value === 'weekly'); rowDays.hidden = kind.value !== 'weekly'; rowOnce.hidden = kind.value !== 'once'; rowCron.hidden = kind.value !== 'cron'; tz.hidden = !(kind.value === 'cron' || kind.value === 'daily' || kind.value === 'weekly');
    modeHelp.textContent = (d.modes.find(m => m.id === mode.value) || {}).help || ''; warn.hidden = mode.value !== 'bypass';
  };
  kind.addEventListener('change', sync); mode.addEventListener('change', sync); sync();
  const scrim = h('div', { class: 'scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': isNew ? 'New task' : 'Edit task' });
  const close = () => scrim.remove();
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); }); scrim.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
  const save = async () => {
    const f = { name: name.value, prompt: prompt.value, folder: folder.value, kind: kind.value, mode: mode.value, budget_usd: parseFloat(budget.value), catch_up: catchUp.checked, cap_off: capOff.checked, max_tokens: parseInt(maxTok.value, 10) || null, tz: tz.value.trim() };
    if (kind.value === 'cron') f.cron = cron.value;
    if (kind.value === 'interval') f.every_s = (parseFloat(num.value) || 0) * parseInt(per.value, 10);
    if (kind.value === 'daily' || kind.value === 'weekly') f.at = at.value;
    if (kind.value === 'weekly') f.days = days.map((c, i) => c.checked ? i : -1).filter(i => i >= 0);
    if (kind.value === 'once') f.once_at = when.value ? new Date(when.value).getTime() / 1000 : 0;
    if (mode.value === 'bypass') f.bypass_ack = ack.checked;
    try { await api(isNew ? '/api/tasks/add' : '/api/tasks/update', isNew ? f : Object.assign({ id: t.id }, f)); close(); toast(isNew ? 'Task added' : 'Task saved'); redraw(); }
    catch (e) { err.replaceChildren(notice(e.message, 'bad')); }
  };
  scrim.append(h('div', { class: 'modal tkmodal' }, h('h3', null, isNew ? 'New task' : 'Edit task'),
    h('div', { class: 'tkform' },
      h('label', null, 'What should it do?', prompt), h('label', null, 'Folder it runs in', folder), h('label', null, 'Name', name),
      h('label', null, 'How often', kind), rowEvery, rowAt, rowDays, rowOnce, rowCron, tz,
      h('label', null, 'What it may do', mode), modeHelp, warn,
      h('label', null, 'Spending limit per run (dollars)', budget),
      h('label', null, 'Token cap per run (for models with no known price)', maxTok),
      h('label', { class: 'inl', style: 'display:flex;gap:6px;align-items:center' }, capOff, 'No cap (not recommended)'),
      h('label', { class: 'inl', style: 'display:flex;gap:6px;align-items:center' }, catchUp, 'If Axon was closed at the time, run once when it next opens (otherwise it is only recorded as missed)'),
      h('p', { class: 'muted' }, 'Runs only while Axon is open. It cannot ask questions, so anything that needs your approval is refused. Each run is saved as a chat you can open.')),
    err, h('div', { class: 'acts' }, h('button', { class: 'btn primary', type: 'button', onclick: save }, isNew ? 'Add task' : 'Save'), h('button', { class: 'btn', type: 'button', onclick: close }, 'Cancel'))));
  document.body.append(scrim); prompt.focus();
}

// ---------- notifications: one place decides, Settings holds the choices ----------
// Nothing here ever carries file contents or secrets: the words are fixed, plus a tool name or a checked task name.
const AXN = { flashT: 0, orig: '', seen: null, since: 0 };
function axnAfter() { const v = parseFloat(lsGet('axon-notify-after')); return isFinite(v) && v >= 0 ? v : 30; }
function axnFlashOn() { return lsGet('axon-notify-flash') !== '0'; }
function axnTasksOn() { return lsGet('axon-notify-tasks') !== '0'; }
function axnStopFlash() { clearInterval(AXN.flashT); AXN.flashT = 0; if (AXN.orig) { document.title = AXN.orig; AXN.orig = ''; } }
function axnFlash(text) {
  if (!document.hidden || !axnFlashOn()) return;
  if (!AXN.flashT) {
    AXN.orig = document.title; let on = false;
    AXN.flashT = setInterval(() => { if (!document.hidden) { axnStopFlash(); return; } on = !on; document.title = on ? '(!) ' + text : AXN.orig; }, 1000);
    document.title = '(!) ' + text;
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) axnStopFlash(); });
// A tab that is in the background: flash its title (if allowed) and show a browser notification (if the person turned them on).
function axnTell(title, body, tag, force) {
  if (!document.hidden && !force) return;
  axnFlash(title);
  if (typeof axNotifyOn === 'function' && axNotifyOn()) {
    try { const n = new Notification(title, { body: String(body || '').slice(0, 120), tag }); n.onclick = () => { window.focus(); n.close(); }; } catch (e) { /* blocked */ }
  }
}
// A turn ended: tell the person only if it took long enough. Asking for permission or an answer always counts.
function axnTurnEnd(C, ev) {
  const secs = typeof ev.duration_s === 'number' ? ev.duration_s : (C.turnT0 ? (Date.now() - C.turnT0) / 1000 : 0);
  if (secs < axnAfter()) return;
  axnTell('Axon finished', turnWord(ev.stop_reason) + ' after ' + dur(secs) + (ev.cost_usd != null ? ' · ' + usd(ev.cost_usd) : ''), 'axon-turn');
}
async function axnPollTasks() {
  if (!(axnTasksOn() && ((typeof axNotifyOn === 'function' && axNotifyOn()) || axnFlashOn()))) return;
  try {
    const r = await api('/api/tasks/recent?since=' + AXN.since);
    const first = AXN.seen === null; AXN.seen = AXN.seen || new Set();
    for (const x of r.runs) {
      if (AXN.seen.has(x.run)) continue; AXN.seen.add(x.run);
      if (!first && x.status !== 'missed' && x.status !== 'skipped') axnTell('A scheduled task finished', x.name + ': ' + tkStatusWord(x.status), 'axon-task-' + x.run);
    }
    AXN.since = Math.max(AXN.since, r.now - 3600);
  } catch (e) { /* the page keeps working */ }
}
setInterval(axnPollTasks, 15000); setTimeout(axnPollTasks, 3000);

function axnSettingsCard() {
  const supported = typeof Notification !== 'undefined';
  const status = h('div', { class: 'muted' });
  const drawStatus = () => { status.textContent = !supported ? 'This browser has no notifications.' : Notification.permission === 'denied' ? 'The browser has blocked notifications for this page. Allow them in the browser’s site settings, then turn this on again.' : Notification.permission === 'granted' ? 'The browser allows notifications for this page.' : 'The browser will ask you the first time you turn this on.'; };
  const on = h('input', { type: 'checkbox', checked: axNotifyOn(), disabled: !supported, 'aria-label': 'Browser notifications' });
  on.addEventListener('change', async () => {
    if (!on.checked) { lsSet('axon-notify', '0'); toast('Notifications off'); return; }
    try { const p = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission(); if (p === 'granted') { lsSet('axon-notify', '1'); toast('Notifications on'); } else { on.checked = false; toast('The browser did not allow notifications'); } } catch (e) { on.checked = false; toast('Notifications are not available here'); }
    drawStatus();
  });
  const after = h('input', { type: 'number', min: 0, step: 5, value: axnAfter(), 'aria-label': 'Seconds a turn must take before it notifies' });
  after.addEventListener('change', () => { const v = Math.max(0, parseFloat(after.value) || 0); after.value = v; lsSet('axon-notify-after', String(v)); });
  const flash = h('input', { type: 'checkbox', checked: axnFlashOn(), 'aria-label': 'Flash the tab title' });
  flash.addEventListener('change', () => { lsSet('axon-notify-flash', flash.checked ? '1' : '0'); if (!flash.checked) axnStopFlash(); });
  const tasks = h('input', { type: 'checkbox', checked: axnTasksOn(), 'aria-label': 'Tell me when a scheduled task finishes' });
  tasks.addEventListener('change', () => lsSet('axon-notify-tasks', tasks.checked ? '1' : '0'));
  drawStatus();
  return h('div', { class: 'card ntf' }, h('h3', null, 'Notifications'),
    h('p', { class: 'muted' }, 'Axon can tell you when work finishes or needs you while this tab is in the background. Messages never include your files or answers, only that something happened.'),
    h('label', { class: 'chk' }, on, h('span', null, 'Show a browser notification', h('br'), status)),
    h('label', { class: 'chk' }, h('span', null, 'Only for turns that take longer than ', after, ' seconds (0 for every turn). Permission requests and questions always notify.')),
    h('label', { class: 'chk' }, flash, h('span', null, 'Flash this tab’s title while it is in the background')),
    h('label', { class: 'chk' }, tasks, h('span', null, 'Also tell me when a scheduled task finishes')),
    h('div', { class: 'formrow' }, h('button', { class: 'btn', type: 'button', onclick: () => { axnTell('Axon test', 'Notifications work.', 'axon-test', true); toast(document.hidden ? 'Sent' : 'Sent. Browser notifications show when this tab is in the background; switch tabs to see the title flash.'); } }, 'Send a test')),
    h('p', { class: 'muted' }, 'These choices are kept in this browser. The terminal has its own: /notify.'));
}
"""
