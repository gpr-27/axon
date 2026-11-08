"""``/schedule``: run a prompt in a folder daily, weekly, at an interval, or once, while Axon is open.

The data and the rules live in ``axon.features.scheduler`` (shared with the app's Tasks panel); this module only
asks, prints and lays out. Tasks run only while an Axon terminal or the app is open: there is no system service.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from axon.commands.common import err, header, info, ok, usage, ws_of
from axon.commands.registry import CommandResult, command
from axon.features import scheduler as sch
from axon.ui.theme import CYAN, MINT, RST, SLATE, WHITE

USAGE = """/schedule                                    open the task menu
/schedule list                               every task, when it runs next and how the last run went
/schedule add every 30m <prompt>             run every 30 minutes (m, h, d; at least 1m)
/schedule add daily 09:00 <prompt>           run every day at 09:00
/schedule add weekly mon,wed 09:00 <prompt>  run on those days
/schedule add once 2026-11-09 14:30 <prompt> run once (or: once in 2h)
    options: --mode readonly|edits   --budget 1.50   --folder DIR   (default: read-only, $1.00, this folder)
/schedule run|pause|resume|delete|history <id>
Tasks run only while Axon is open (this terminal or the app)."""

_SHORT = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_WORDS = {"weekdays": [0, 1, 2, 3, 4], "weekends": [5, 6], "everyday": list(range(7))}


def _done() -> CommandResult:
    return CommandResult(handled=True)


def _stamp(ts: Any) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%a %d %b %H:%M") if isinstance(ts, (int, float)) and ts else "-"


def _money(v: Any) -> str:
    v = float(v or 0)
    return f"${v:.2f}" if v >= 1 else f"${v:.4f}"


def _word(status: str) -> str:
    return {"ok": "succeeded", "failed": "failed", "budget": "hit its spending limit", "stopped": "stopped",
            "timeout": "timed out", "missed": "missed", "skipped": "skipped", "running": "running",
            "interrupted": "interrupted"}.get(status, status)


def _line(t: dict[str, Any]) -> str:
    state = "running" if t["running"] else "paused" if not t.get("enabled", True) else (f"next {_stamp(t['next_at'])}" if t.get("next_at") else "finished")
    last = f" · last {_word(t['last_status'])}" if t.get("last_status") else ""
    return f"{t['name']} · {t['when']} · {state}{last}"


def parse_days(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", text.lower().strip()):
        if not part:
            continue
        if part in _DAY_WORDS:
            out += _DAY_WORDS[part]
        elif part[:3] in _SHORT:
            out.append(_SHORT[part[:3]])
        else:
            raise sch.ScheduleError(f"'{part}' is not a day. Use mon, tue, wed, thu, fri, sat, sun, weekdays or weekends.")
    if not out:
        raise sch.ScheduleError("Choose at least one day.")
    return sorted(set(out))


def parse_once(words: list[str], now: float) -> tuple[float, list[str]]:
    """'in 2h ...' or '2026-11-09 14:30 ...' or '14:30 ...' (today, or tomorrow if past) -> (epoch, rest)."""
    if words and words[0].lower() == "in" and len(words) > 1:
        return now + sch.parse_every(words[1]), words[2:]
    if words and re.fullmatch(r"\d{4}-\d{2}-\d{2}", words[0]) and len(words) > 1:
        try:
            return datetime.strptime(words[0] + " " + words[1], "%Y-%m-%d %H:%M").timestamp(), words[2:]
        except ValueError:
            raise sch.ScheduleError("A date and time look like 2026-11-09 14:30.") from None
    if words and re.fullmatch(r"\d{1,2}:\d{2}", words[0]):
        h, m = sch._hhmm(words[0])
        at = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
        if at.timestamp() <= now:
            at += timedelta(days=1)
        return at.timestamp(), words[1:]
    raise sch.ScheduleError("Say when: 'once in 2h', 'once 14:30' or 'once 2026-11-09 14:30'.")


def parse_add(agent: Any, arg: str, now: float | None = None) -> dict[str, Any]:
    """The fields of a new task from the typed form (everything after 'add')."""
    now = time.time() if now is None else now
    words = arg.split()
    opts: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(words):
        if words[i] in ("--mode", "--budget", "--folder") and i + 1 < len(words):
            opts[words[i][2:]] = words[i + 1]
            i += 2
        elif words[i] == "--i-understand":
            opts["ack"] = "1"
            i += 1
        else:
            rest.append(words[i])
            i += 1
    if not rest:
        raise sch.ScheduleError("Say how often. " + USAGE.splitlines()[2].strip())
    kind = rest[0].lower()
    f: dict[str, Any] = {}
    if kind in ("every", "interval"):
        if len(rest) < 2:
            raise sch.ScheduleError("Say how often, for example: every 30m.")
        f["kind"], f["every_s"] = "interval", sch.parse_every(rest[1])
        rest = rest[2:]
    elif kind == "daily":
        if len(rest) < 2:
            raise sch.ScheduleError("Say the time, for example: daily 09:00.")
        f["kind"], f["at"] = "daily", rest[1]
        rest = rest[2:]
    elif kind == "weekly":
        if len(rest) < 3:
            raise sch.ScheduleError("Say the days and the time, for example: weekly mon,wed 09:00.")
        f["kind"], f["days"], f["at"] = "weekly", parse_days(rest[1]), rest[2]
        rest = rest[3:]
    elif kind == "once":
        f["kind"] = "once"
        f["once_at"], rest = parse_once(rest[1:], now)
    else:
        raise sch.ScheduleError("Choose every, daily, weekly or once.")
    f["prompt"] = " ".join(rest)
    f["folder"] = opts.get("folder") or str(ws_of(agent))
    mode = opts.get("mode", sch.DEFAULT_MODE)
    f["mode"] = {"read-only": "readonly", "readonly": "readonly", "edits": "edits", "bypass": "bypass"}.get(mode, mode)
    if "budget" in opts:
        try:
            f["budget_usd"] = float(opts["budget"].lstrip("$"))
        except ValueError:
            raise sch.ScheduleError("The budget is a number of dollars, for example --budget 0.50.") from None
    if opts.get("ack"):
        f["bypass_ack"] = True
    return f


def _print_list(tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        info("No scheduled tasks yet.")
    for t in tasks:
        mark = f"{MINT}●{RST}" if t["running"] else f"{SLATE}○{RST}"
        print(f"    {mark} {CYAN}{t['id']}{RST}  {WHITE}{_line(t)}{RST}")
        print(f"        {SLATE}{t['folder']} · {sch.MODES[t['mode']][1]} · up to {_money(t['budget_usd'])} per run{RST}")


def _print_history(runs: list[dict[str, Any]]) -> None:
    if not runs:
        info("Nothing has run yet.")
    for r in runs:
        sid = f"  session {r['session_id']}" if r.get("session_id") else ""
        cost = "" if r.get("status") in ("missed", "skipped") else f"  {_money(r.get('cost_usd'))}"
        print(f"    {CYAN}{_stamp(r.get('started'))}{RST}  {WHITE}{_word(str(r.get('status')))}{RST}{cost}{SLATE}{sid}{RST}")
        if r.get("error"):
            print(f"        {SLATE}{r['error']}{RST}")


def _find(s: sch.Scheduler, ident: str) -> dict[str, Any] | None:
    ident = ident.strip().lstrip("#")
    tasks = s.tasks()
    exact = [t for t in tasks if t["id"] == ident]
    if exact:
        return exact[0]
    byname = [t for t in tasks if t["name"].lower() == ident.lower()]
    return byname[0] if len(byname) == 1 else None


# ─── menus ────────────────────────────────────────────────────────────────────

def _ask_schedule(now: float) -> dict[str, Any] | None:
    from axon.ui.menus import ask_text, choose_value
    kind = choose_value("How often?", [("Every so often", "interval", "every 30 minutes, every 2 hours"), ("Every day", "daily", "at a time of day"),
                                       ("On certain days", "weekly", "for example Monday and Thursday"), ("Once", "once", "at one date and time")])
    if kind is None:
        return None
    if kind == "interval":
        v = ask_text("Every how long? (30m, 2h, 1d)", "1h")
        return None if v is None else {"kind": "interval", "every_s": sch.parse_every(v)}
    if kind in ("daily", "weekly"):
        days = None
        if kind == "weekly":
            d = ask_text("Which days? (mon,wed or weekdays)", "weekdays")
            if d is None:
                return None
            days = parse_days(d)
        at = ask_text("At what time? (24 hour clock, 09:30)", "09:00")
        if at is None:
            return None
        return {"kind": kind, "at": at, **({"days": days} if days else {})}
    v = ask_text("When? (in 2h, 14:30 or 2026-11-09 14:30)", "in 1h")
    if v is None:
        return None
    return {"kind": "once", "once_at": parse_once(v.split(), now)[0]}


def _ask_mode() -> tuple[str, bool] | None:
    from axon.ui.menus import choose_value, confirm
    mode = choose_value("What may the task do?", [(v[1], k, v[2]) for k, v in sch.MODES.items()], current=sch.DEFAULT_MODE)
    if mode is None:
        return None
    if mode == "bypass":
        if not confirm("Full access runs every command with nobody watching. Allow it?"):
            return None
        return mode, True
    return mode, False


def _new_task(agent: Any, s: sch.Scheduler) -> None:
    from axon.ui.menus import ask_text
    now = time.time()
    prompt = ask_text("What should the task do?")
    if not prompt:
        return
    when = _ask_schedule(now)
    if when is None:
        return
    mode = _ask_mode()
    if mode is None:
        return
    budget = ask_text("Spending limit per run, in dollars", f"{sch.DEFAULT_BUDGET_USD:g}")
    if budget is None:
        return
    folder = ask_text("Folder it runs in", str(ws_of(agent)))
    if folder is None:
        return
    try:
        t = s.add({"prompt": prompt, "folder": folder, "mode": mode[0], "bypass_ack": mode[1], "budget_usd": float(budget.lstrip("$") or 0), **when})
        ok(f"Added task {t['id']}: {sch.describe(t)}. It runs only while Axon is open.")
    except (sch.ScheduleError, ValueError) as e:
        err(str(e))


def _task_menu(agent: Any, s: sch.Scheduler, task_id: str) -> None:
    from axon.ui.menus import ask_text, choose_value, confirm
    while True:
        t = s.get(task_id)
        if t is None:
            return
        pick = choose_value(t["name"], [
            ("Run it now", "run", "ignores the schedule"),
            *([("Stop the run", "stop", "")] if t["running"] else []),
            ("Pause" if t["enabled"] else "Resume", "pause", ""),
            ("History", "history", "status, time and cost of each run"),
            ("Change the prompt…", "prompt", ""), ("Change the schedule…", "when", ""),
            ("Change what it may do…", "mode", t["mode"]), ("Change the spending limit…", "budget", _money(t["budget_usd"])),
            ("Delete…", "delete", "")], subtitle=_line(t))
        if pick is None:
            return
        try:
            if pick == "run":
                s.run_now(task_id)
                ok("Started. It runs in the background and is saved as a session.")
            elif pick == "stop":
                s.stop_run(task_id)
            elif pick == "pause":
                s.set_enabled(task_id, not t["enabled"])
            elif pick == "history":
                _history_menu(agent, s, task_id)
            elif pick == "prompt":
                v = ask_text("New prompt", t["prompt"])
                if v:
                    s.update(task_id, {"prompt": v})
            elif pick == "when":
                w = _ask_schedule(time.time())
                if w:
                    s.update(task_id, w)
            elif pick == "mode":
                m = _ask_mode()
                if m:
                    s.update(task_id, {"mode": m[0], "bypass_ack": m[1]})
            elif pick == "budget":
                v = ask_text("Spending limit per run, in dollars", f"{t['budget_usd']:g}")
                if v:
                    s.update(task_id, {"budget_usd": float(v.lstrip("$"))})
            elif pick == "delete":
                if confirm(f"Delete the task '{t['name']}'? Its sessions stay."):
                    s.delete(task_id)
                    ok("Deleted.")
                    return
        except (sch.ScheduleError, ValueError) as e:
            err(str(e))


def _history_menu(agent: Any, s: sch.Scheduler, task_id: str) -> None:
    from axon.ui.menus import choose_value
    runs = s.history(task_id, 20)
    _print_history(runs)
    opts = [(f"{_stamp(r.get('started'))} · {_word(str(r.get('status')))} · {_money(r.get('cost_usd'))}", r["session_id"], "open it as a session")
            for r in runs if r.get("session_id")]
    if not opts:
        return
    sid = choose_value("Open a run", opts)
    if sid:
        from axon.commands.session_cmds import handle_resume_cmd
        handle_resume_cmd(agent, sid)


def _menu(agent: Any) -> None:
    from axon.ui.menus import choose_value
    s = sch.get_scheduler()
    while True:
        tasks = s.tasks()
        opts = [(f"{t['name']}", t["id"], _line(t).split(" · ", 1)[1]) for t in tasks] + [("New task…", "+new", "")]
        pick = choose_value("Scheduled tasks", opts, subtitle="Tasks run only while Axon is open.")
        if pick is None:
            return
        if pick == "+new":
            _new_task(agent, s)
        else:
            _task_menu(agent, s, pick)


# ─── the command ──────────────────────────────────────────────────────────────

@command("schedule", category="Memory & Project", description="Run a prompt in a folder daily, weekly, on an interval or once (while Axon is open)",
         aliases=("cron",), usage=USAGE)
def handle_schedule(agent: Any, arg: str) -> CommandResult:
    from axon.ui.menus import menus_enabled
    sch.ensure_started()
    s = sch.get_scheduler()
    words = arg.split(None, 1)
    sub = words[0].lower() if words else ""
    rest = words[1].strip() if len(words) > 1 else ""
    try:
        if not sub:
            if menus_enabled():
                _menu(agent)
                return _done()
            sub = "list"
        if sub in ("list", "ls"):
            header("Scheduled tasks", "run only while Axon is open")
            _print_list(s.tasks())
            print()
            return _done()
        if sub == "add":
            t = s.add(parse_add(agent, rest))
            ok(f"Added task {t['id']}: {sch.describe(t)} ({sch.MODES[t['mode']][1]}, up to {_money(t['budget_usd'])} per run). It runs only while Axon is open.")
            return _done()
        if sub in ("run", "pause", "resume", "delete", "rm", "history", "stop"):
            t = _find(s, rest)
            if t is None:
                err("No such task. /schedule list shows them." if rest else f"Usage: /schedule {sub} <id>")
                return _done()
            if sub == "run":
                s.run_now(t["id"])
                ok(f"Started '{t['name']}'. It is saved as a session when it finishes.")
            elif sub == "stop":
                ok("Stopping." if s.stop_run(t["id"]) else "It is not running here.")
            elif sub == "pause":
                s.set_enabled(t["id"], False)
                ok(f"Paused '{t['name']}'.")
            elif sub == "resume":
                s.set_enabled(t["id"], True)
                ok(f"Resumed '{t['name']}'.")
            elif sub == "history":
                header(t["name"], "run history")
                _print_history(s.history(t["id"], 20))
                print()
            else:
                from axon.ui.menus import confirm
                if menus_enabled() and not confirm(f"Delete the task '{t['name']}'? Its sessions stay."):
                    info("Kept.")
                    return _done()
                s.delete(t["id"])
                ok(f"Deleted '{t['name']}'.")
            return _done()
    except sch.ScheduleError as e:
        err(str(e))
        return CommandResult(handled=True, success=False)
    usage(USAGE)
    return _done()


def start_in_terminal(agent: Any) -> None:
    """Called when the terminal opens: start the scheduler and tell the person (through the usual notifications) when a run ends."""
    s = sch.ensure_started()
    if s is None:
        return

    def finished(rec: dict[str, Any]) -> None:
        try:
            from axon.ui.notify import notify_event
            notify_event("background_task", f"{sch.notice_name(rec.get('name'))}: {_word(str(rec.get('status')))}", settings=getattr(agent, "settings", None))
        except Exception:
            pass
    s.add_listener(finished)


__all__ = ["handle_schedule", "parse_add", "start_in_terminal"]
