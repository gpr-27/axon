"""Scheduled tasks: run a prompt in a folder daily, weekly, at an interval, or once.

Plain data module: no web and no terminal imports. The app (``dash_js_tasks``) and the terminal (``/schedule``)
both read and write the same files, so they always agree.

Tasks run **only while Axon is running** (the app, or a terminal session). There is no system service and no login
item: that would change the computer and needs the owner's approval. Whatever was due while Axon was closed is
recorded as missed and is not replayed (at most one catch-up run per task).

Storage, under ``AXON_HOME/schedule/``: ``tasks.json`` (atomic writes, a corrupt file is set aside), ``runs.jsonl``
(history: one line per state change of a run, the last line of a run wins) and ``locks/<task id>/`` (one lease per
task, the same lease as ``session/lease.py``, so two Axon processes never run one task together and a lock left by
a dead process is ignored, on Windows too).

Each run is a normal session in the task's folder (the SDK builds an agent exactly as ``axon`` does), so it can be
opened, resumed and exported like any chat. Unattended runs cannot ask questions: anything that would need an
answer is denied. The permission mode defaults to read-only; full access is never a default and must be
acknowledged. Every run has a spending limit.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

KINDS = ("interval", "daily", "weekly", "once", "cron")
# name -> (permission mode of the SDK, plain label, one-line explanation)
MODES: dict[str, tuple[str, str, str]] = {
    "readonly": ("plan", "Read-only", "Can read and search the folder. Cannot change anything or run commands."),
    "edits": ("acceptEdits", "Can edit files", "Can also create and change files in the folder. Asks nothing, denies anything else."),
    "bypass": ("bypass", "Full access", "Runs every tool and command with no checks at all. Nobody is watching. Use only in a throwaway folder."),
}
DEFAULT_MODE = "readonly"
DEFAULT_BUDGET_USD = 1.0
MAX_BUDGET_USD = 100.0
MIN_INTERVAL_S = 60.0
MAX_INTERVAL_S = 366 * 86400.0
MISSED_GRACE_S = 120.0        # a run started within this long of its time is on time, later than that it was missed
RUN_TIMEOUT_S = 30 * 60.0     # one run never goes on longer than this
MAX_PARALLEL = 3              # tasks running at once in one process
MAX_TASKS = 100
MAX_PROMPT = 8000
MAX_NAME = 80
HISTORY_KEEP = 1000
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RUNNING_STATUSES = ("running",)
UNATTENDED_NOTE = ("This is a scheduled run and nobody is available to answer. Do not ask questions: make a reasonable "
                   "choice, say what you assumed, and finish. Keep the final answer short.")


class ScheduleError(ValueError):
    """The task as given cannot be saved. The message is plain words for the person."""


# ─── times ────────────────────────────────────────────────────────────────────

def _hhmm(text: Any) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError("The time of day must look like 09:30 (24 hour clock).")
    return int(m.group(1)), int(m.group(2))


def next_after(task: dict[str, Any], after: float) -> float | None:
    """The first time this task is due strictly after *after* (epoch seconds, local calendar), or None."""
    kind = task.get("kind")
    if kind == "cron":
        from axon.features import schedule_ext as ext
        return ext.cron_next(str(task.get("cron") or ""), after, task.get("tz"))
    if kind == "interval":
        return after + float(task.get("every_s") or MIN_INTERVAL_S)
    if kind == "once":
        at = task.get("once_at")
        return float(at) if isinstance(at, (int, float)) and float(at) > after else None
    if kind not in ("daily", "weekly"):
        return None
    h, m = _hhmm(task.get("at"))
    days = set(task.get("days") or range(7)) if kind == "weekly" else set(range(7))
    from axon.features import schedule_ext as ext
    tz = ext.zone(task.get("tz"))
    start = (datetime.fromtimestamp(after, tz) if tz else datetime.fromtimestamp(after)).replace(hour=h, minute=m, second=0, microsecond=0)
    for i in range(0, 9):
        cand = start + timedelta(days=i)
        if tz is not None:
            cand = cand.replace(tzinfo=tz)
        if cand.weekday() in days and cand.timestamp() > after:
            return cand.timestamp()
    return None


def _every_words(s: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size and s % size == 0:
            n = int(s // size)
            return f"every {unit}" if n == 1 else f"every {n} {unit}s"
    return f"every {int(s)} seconds"


def describe(task: dict[str, Any]) -> str:
    kind = task.get("kind")
    if kind == "cron":
        return f"Cron {task.get('cron')}" + (f" ({task['tz']})" if task.get("tz") else "")
    if kind == "interval":
        return _every_words(float(task.get("every_s") or 0)).capitalize()
    if kind == "daily":
        return f"Every day at {task.get('at')}"
    if kind == "weekly":
        days = sorted(set(int(d) for d in task.get("days") or []))
        names = ", ".join(WEEKDAYS[d] for d in days if 0 <= d <= 6)
        return f"Every {names} at {task.get('at')}"
    if kind == "once":
        at = task.get("once_at")
        return "Once, " + (datetime.fromtimestamp(float(at)).strftime("%a %d %b %H:%M") if isinstance(at, (int, float)) else "?")
    return "?"


def parse_every(text: str) -> float:
    """'30m', '2h', '1d', '90s' -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*", str(text or "").lower())
    if not m:
        raise ScheduleError("An interval looks like 30m, 2h or 1d.")
    unit = m.group(2)[0]
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# ─── validation ───────────────────────────────────────────────────────────────

def _secrets(text: str) -> list[str]:
    try:
        from axon.features.memory_files import scan_secrets
        return scan_secrets(text)
    except Exception:
        return []


def check_folder(raw: Any) -> str:
    """The folder as an absolute path string, refusing credential stores, system folders, the disk root and the home folder."""
    if not isinstance(raw, str) or not raw.strip():
        raise ScheduleError("Choose the folder the task runs in.")
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise ScheduleError("That folder cannot be read.") from None
    if not p.is_dir():
        raise ScheduleError("That folder does not exist.")
    try:
        from axon.permissions.paths import is_protected_path, too_broad_for_workspace
        why = is_protected_path(p)
        if why:
            raise ScheduleError(f"Refused: {why}.")
        broad = too_broad_for_workspace(p)
        if broad:
            raise ScheduleError(broad)
    except ImportError:
        pass
    return str(p)


def clean_task(fields: dict[str, Any], base: dict[str, Any] | None = None, now: float | None = None) -> dict[str, Any]:
    """Validate *fields* laid over *base* (an existing task) and return the full task. Raises ScheduleError."""
    t = dict(base or {})
    src = {k: v for k, v in fields.items() if k in _EDITABLE}
    t.update(src)
    now = time.time() if now is None else now
    name = str(t.get("name") or "").strip()
    prompt = str(t.get("prompt") or "").strip()
    if not prompt:
        raise ScheduleError("Write what the task should do.")
    if len(prompt) > MAX_PROMPT:
        raise ScheduleError(f"The prompt is too long (limit {MAX_PROMPT} characters).")
    if _secrets(prompt):
        raise ScheduleError("The prompt looks like it contains a key or password. Leave secrets out; the task can read them from the folder if it needs to.")
    if not name:
        name = " ".join(prompt.split())[:40]
    if len(name) > MAX_NAME:
        raise ScheduleError(f"The name is too long (limit {MAX_NAME} characters).")
    t["name"], t["prompt"] = name, prompt
    t["folder"] = check_folder(t.get("folder"))
    kind = t.get("kind")
    if kind not in KINDS:
        raise ScheduleError("Choose how often: every so often, daily, weekly, once or a cron schedule.")
    from axon.features import schedule_ext as ext
    try:
        t["tz"] = ext.check_tz(t.get("tz"))
        if kind == "cron":
            t["cron"] = " ".join(str(t.get("cron") or "").split())
            ext.parse_cron(t["cron"])
    except ext.CronError as e:
        raise ScheduleError(str(e)) from None
    if kind == "cron":
        pass
    elif kind == "interval":
        try:
            every = float(t.get("every_s"))
        except (TypeError, ValueError):
            raise ScheduleError("Say how often, for example every 30 minutes.") from None
        if every < MIN_INTERVAL_S:
            raise ScheduleError("The shortest interval is 1 minute.")
        if every > MAX_INTERVAL_S:
            raise ScheduleError("The longest interval is a year.")
        t["every_s"] = every
    elif kind in ("daily", "weekly"):
        h, m = _hhmm(t.get("at"))
        t["at"] = f"{h:02d}:{m:02d}"
        if kind == "weekly":
            days = t.get("days")
            if not isinstance(days, list) or not days or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
                raise ScheduleError("Choose at least one day of the week.")
            t["days"] = sorted(set(days))
    else:
        at = t.get("once_at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            raise ScheduleError("Choose the date and time.")
        if float(at) <= now and ("once_at" in src or "kind" in src):
            raise ScheduleError("That time is already past.")
        t["once_at"] = float(at)
    mode = t.get("mode") or DEFAULT_MODE
    if not isinstance(mode, str) or mode not in MODES:
        raise ScheduleError("Choose read-only, can edit files, or full access.")
    t["mode"] = mode
    if mode == "bypass" and not (t.get("bypass_ack") or fields.get("bypass_ack") is True):
        raise ScheduleError("Full access needs your confirmation: it runs every command with nobody watching.")
    t["bypass_ack"] = bool(mode == "bypass")
    try:
        budget = float(t.get("budget_usd", DEFAULT_BUDGET_USD))
    except (TypeError, ValueError):
        raise ScheduleError("The spending limit must be a number of dollars.") from None
    if not 0.01 <= budget <= MAX_BUDGET_USD:
        raise ScheduleError(f"The spending limit per run must be between $0.01 and ${MAX_BUDGET_USD:g}.")
    t["budget_usd"] = round(budget, 4)
    t["catch_up"] = bool(t.get("catch_up", kind not in ("interval", "cron")))     # cron runs are never replayed unless asked
    t["cap_off"] = bool(t.get("cap_off", False))
    mt = t.get("max_tokens")
    if mt in (None, "", 0):
        t["max_tokens"] = None
    else:
        try:
            t["max_tokens"] = int(mt)
        except (TypeError, ValueError):
            raise ScheduleError("The token cap must be a whole number.") from None
        if t["max_tokens"] < 1000:
            raise ScheduleError("The token cap must be at least 1000.")
    t["enabled"] = bool(t.get("enabled", True))
    return t


_EDITABLE = {"name", "prompt", "folder", "kind", "every_s", "at", "days", "once_at", "mode", "budget_usd", "catch_up", "enabled", "bypass_ack", "cron", "tz", "cap_off", "max_tokens"}


# ─── safe text for notices ────────────────────────────────────────────────────

def notice_name(task_or_name: Any) -> str:
    """A task name that is safe to show in a notification: single line, short, and nothing that looks like a secret."""
    name = task_or_name.get("name", "") if isinstance(task_or_name, dict) else str(task_or_name or "")
    name = " ".join(name.split())[:40]
    return "A task" if (not name or _secrets(name)) else name


# ─── running ──────────────────────────────────────────────────────────────────

class RunControl:
    """Lets the scheduler stop a run that is in progress."""

    def __init__(self, run_id: str, task_id: str) -> None:
        self.run_id, self.task_id = run_id, task_id
        self.started = time.time()
        self.reason = ""                 # why it was cancelled: "stopped" or "timeout"
        self._cancel: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def attach(self, cancel: Callable[[], None]) -> None:
        with self._lock:
            self._cancel = cancel
            if self.reason:
                cancel()

    def cancel(self, reason: str = "stopped") -> None:
        with self._lock:
            self.reason = self.reason or reason
            fn = self._cancel
        if fn is not None:
            try:
                fn()
            except Exception:
                pass


def sdk_runner(task: dict[str, Any], ctl: RunControl) -> dict[str, Any]:
    """Run the task's prompt as a normal session in its folder. Returns status, cost, session id and error."""
    from axon.features.sdk import AxonOptions, AxonSession, SDKError
    from axon.features import schedule_ext as ext
    cap = None if task.get("cap_off") else float(task["budget_usd"])
    over = {"turn_token_budget": int(task["max_tokens"])} if task.get("max_tokens") else {}
    opts = AxonOptions(workspace=task["folder"], mode=MODES[task["mode"]][0], max_budget_usd=cap,
                       append_system_prompt=UNATTENDED_NOTE, settings_overrides=over or None)
    try:
        session = AxonSession(opts)
    except SDKError as e:
        return {"status": "failed", "error": str(e)[:300], "cost_usd": 0.0, "session_id": "", "failure": "setup_error"}
    st = session.agent.settings
    why = ext.cap_problem(task, str(getattr(st, "model", "") or ""), getattr(st, "provider", None))
    if why:                                    # fail closed: no enforceable cap, so the run does not start
        session.close()
        return {"status": "skipped", "error": why, "cost_usd": 0.0, "session_id": "", "failure": "price_unknown"}
    timer = threading.Timer(RUN_TIMEOUT_S, lambda: ctl.cancel("timeout"))
    timer.daemon = True
    timer.start()
    ctl.attach(session.cancel)
    try:
        res = session.run(task["prompt"])
    finally:
        timer.cancel()
        session.close()
    stop = res.stop_reason or ""
    if ctl.reason:
        status = "timeout" if ctl.reason == "timeout" else "stopped"
    elif stop in ("budget_exceeded", "token_budget"):
        status = "budget"
    elif res.is_error:
        status = "failed"
    else:
        status = "ok"
    err = ""
    if status in ("failed", "budget"):
        err = " ".join(str(res.error or res.text or stop).split())[:300]
    usage = res.usage or {}
    failure = {"budget": "cap_reached", "timeout": "timeout", "stopped": "cancelled"}.get(status, "")
    if status == "failed":
        failure = "max_iterations" if stop == "max_iterations" else "model_error"
    return {"status": status, "cost_usd": float(res.cost_usd or 0.0), "session_id": res.session_id, "error": err, "stop_reason": stop,
            "failure": failure, "tokens_in": int(usage.get("input_tokens", 0) or 0), "tokens_out": int(usage.get("output_tokens", 0) or 0)}


# ─── the scheduler ────────────────────────────────────────────────────────────

class Scheduler:
    """Tasks, their history, and the loop that starts what is due. ``clock`` and ``runner`` can be replaced (tests)."""

    def __init__(self, home: Path | None = None, clock: Callable[[], float] = time.time,
                 runner: Callable[[dict[str, Any], RunControl], dict[str, Any]] | None = None) -> None:
        self._home = Path(home) if home is not None else None
        self.clock = clock
        self.runner = runner or sdk_runner
        self._lock = threading.RLock()
        self.running: dict[str, RunControl] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- paths
    @property
    def folder(self) -> Path:
        if self._home is not None:
            base = self._home
        else:
            from axon import storage
            base = storage.axon_home()
        return base / "schedule"

    def _lockdir(self, task_id: str) -> Path:
        return self.folder / "locks" / re.sub(r"[^A-Za-z0-9_-]", "_", task_id)

    def _file(self, name: str) -> Path:
        return self.folder / name

    @staticmethod
    def _mutex(folder: Path):
        from axon.session import lease
        folder.mkdir(parents=True, exist_ok=True)
        return lease._exclusive(folder)

    # -- tasks file
    def _read(self) -> list[dict[str, Any]]:
        path = self._file("tasks.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            try:
                path.replace(path.with_name(f"tasks.json.bad-{int(self.clock())}"))
            except OSError:
                pass
            return []
        items = data.get("tasks") if isinstance(data, dict) else None
        return [t for t in items or [] if isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("kind") in KINDS]

    def _write(self, tasks: list[dict[str, Any]]) -> None:
        path = self._file("tasks.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".tasks.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps({"version": 1, "tasks": tasks}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise ScheduleError(f"Could not save the tasks: {e.strerror or e}") from None

    def tasks(self) -> list[dict[str, Any]]:
        """Every task with its current state (running, next time, last result)."""
        out = []
        for t in self._read():
            t = dict(t)
            t["when"] = describe(t)
            t["running"] = self.is_running(t["id"])
            t["state"] = "running" if t["running"] else ("paused" if not t.get("enabled", True) else ("done" if t.get("next_at") is None else "waiting"))
            out.append(t)
        return out

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tasks() if t["id"] == task_id), None)

    def add(self, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._mutex(self.folder):
            tasks = self._read()
            if len(tasks) >= MAX_TASKS:
                raise ScheduleError(f"There are already {MAX_TASKS} tasks. Delete some first.")
            now = self.clock()
            t = clean_task(fields, None, now)
            t["id"] = uuid.uuid4().hex[:8]
            t["created"] = now
            t["next_at"] = next_after(t, now) if t["enabled"] else None
            tasks.append(t)
            self._write(tasks)
            return dict(t)

    def update(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._mutex(self.folder):
            tasks = self._read()
            for i, old in enumerate(tasks):
                if old["id"] == task_id:
                    now = self.clock()
                    t = clean_task(fields, old, now)
                    for k in ("id", "created", "last_run", "last_status"):
                        if k in old:
                            t[k] = old[k]
                    schedule_changed = any(k in fields for k in ("kind", "every_s", "at", "days", "once_at", "enabled", "cron", "tz"))
                    if schedule_changed or t.get("next_at") is None:
                        t["next_at"] = next_after(t, now) if t["enabled"] else None
                    tasks[i] = t
                    self._write(tasks)
                    return dict(t)
        raise ScheduleError("No such task.")

    def set_enabled(self, task_id: str, enabled: bool) -> dict[str, Any]:
        t = self.get(task_id)
        if t is None:
            raise ScheduleError("No such task.")
        if enabled and t["kind"] == "once" and not (isinstance(t.get("once_at"), (int, float)) and t["once_at"] > self.clock()):
            raise ScheduleError("This task was one-time and its time has passed. Edit it to choose a new time.")
        return self.update(task_id, {"enabled": bool(enabled)})

    def delete(self, task_id: str) -> bool:
        with self._lock, self._mutex(self.folder):
            tasks = self._read()
            keep = [t for t in tasks if t["id"] != task_id]
            if len(keep) == len(tasks):
                return False
            self._write(keep)
        ctl = self.running.get(task_id)
        if ctl is not None:
            ctl.cancel("stopped")
        return True

    # -- history
    def _append(self, rec: dict[str, Any]) -> None:
        path = self._file("runs.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._mutex(self.folder):
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
                if path.stat().st_size > 400_000:
                    self._compact(path)
            except OSError:
                pass

    def prune(self) -> None:
        """Apply the log's retention (age and count) now."""
        from axon.features import schedule_ext as ext
        path = self._file("runs.jsonl")
        with self._mutex(self.folder):
            recs = self._records(path)
            keep = ext.retain(recs, self.clock(), ext.RETAIN_DAYS, HISTORY_KEEP)
            if len(keep) != len(recs):
                self._rewrite(path, keep)

    def _rewrite(self, path: Path, keep: list[dict[str, Any]]) -> None:
        tmp = path.with_name(f".runs.{os.getpid()}.tmp")
        try:
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def _compact(self, path: Path) -> None:
        keep = self._records(path)[-HISTORY_KEEP:]
        tmp = path.with_name(f".runs.{os.getpid()}.tmp")
        try:
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _records(path: Path) -> list[dict[str, Any]]:
        """One record per run: the last line written for it, in the order the runs began. Bad lines are skipped."""
        order: list[str] = []
        by: dict[str, dict[str, Any]] = {}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("run"), str):
                continue
            if rec["run"] not in by:
                order.append(rec["run"])
                by[rec["run"]] = {}
            by[rec["run"]].update(rec)
        return [by[r] for r in order]

    def history(self, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. A run still marked running whose lock nobody holds ended without a record: shown as interrupted."""
        out = []
        for rec in reversed(self._records(self._file("runs.jsonl"))):
            if task_id and rec.get("task") != task_id:
                continue
            rec = dict(rec)
            if rec.get("status") == "running" and not self._run_alive(rec):
                rec["status"] = "interrupted"
                rec["error"] = rec.get("error") or "Axon closed before the run finished."
            from axon.features import schedule_ext as ext
            out.append(ext.view(rec))
            if len(out) >= limit:
                break
        return out

    def _run_alive(self, rec: dict[str, Any]) -> bool:
        ctl = self.running.get(str(rec.get("task")))
        if ctl is not None and ctl.run_id == rec.get("run"):
            return True
        from axon.session import lease
        info = lease.holder(self._lockdir(str(rec.get("task"))))
        return info is not None and info.get("pid") != os.getpid() and info.get("run") in (None, rec.get("run"))

    def is_running(self, task_id: str) -> bool:
        if task_id in self.running:
            return True
        from axon.session import lease
        return lease.holder(self._lockdir(task_id)) is not None

    # -- starting runs
    def run_now(self, task_id: str, wait: bool = False) -> dict[str, Any]:
        """Start the task now, whatever its schedule. Raises ScheduleError when it is already running or does not exist."""
        t = self.get(task_id)
        if t is None:
            raise ScheduleError("No such task.")
        rec = self._start(t, "manual")
        if rec is None:
            raise ScheduleError("This task is already running (here or in another Axon window).")
        if wait:
            th = self.threads.get(task_id)
            if th is not None:
                th.join()
        return rec

    def stop_run(self, task_id: str) -> bool:
        ctl = self.running.get(task_id)
        if ctl is None:
            return False
        ctl.cancel("stopped")
        return True

    def _start(self, task: dict[str, Any], reason: str) -> dict[str, Any] | None:
        """Take the task's lock and run it on a thread. None when it is already running."""
        from axon.session import lease
        with self._lock:
            tid = task["id"]
            if tid in self.running:
                return None
            lk = lease.acquire(self._lockdir(tid), owner="scheduler")
            if lk is None:
                return None
            run_id = uuid.uuid4().hex[:10]
            ctl = RunControl(run_id, tid)
            ctl.started = self.clock()
            self.running[tid] = ctl
            rec = {"run": run_id, "task": tid, "name": task.get("name", ""), "folder": task.get("folder", ""), "status": "running",
                   "reason": reason, "started": ctl.started, "mode": task.get("mode"), "budget_usd": task.get("budget_usd")}
            self._append(rec)
            th = threading.Thread(target=self._work, args=(task, ctl, lk, rec), name=f"axon-task-{tid}", daemon=True)
            self.threads[tid] = th
            th.start()
            return rec

    def _work(self, task: dict[str, Any], ctl: RunControl, lk: Any, rec: dict[str, Any]) -> None:
        try:
            try:
                res = self.runner(task, ctl) or {}
            except BaseException as e:      # a crashing runner must not take the scheduler with it
                res = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            final = {"run": rec["run"], "task": rec["task"], "status": str(res.get("status") or "ok"), "ended": self.clock(),
                     "cost_usd": round(float(res.get("cost_usd") or 0.0), 6), "session_id": str(res.get("session_id") or ""),
                     "error": str(res.get("error") or "")[:300], "failure": str(res.get("failure") or ""),
                     "tokens_in": int(res.get("tokens_in") or 0), "tokens_out": int(res.get("tokens_out") or 0)}
            self._append(final)
            try:
                with self._mutex(self.folder):
                    tasks = self._read()
                    for t in tasks:
                        if t["id"] == rec["task"]:
                            t["last_run"], t["last_status"] = ctl.started, final["status"]
                            if t["kind"] == "once":
                                t["enabled"], t["next_at"] = False, None
                    self._write(tasks)
            except ScheduleError:
                pass
            merged = {**rec, **final}
            for fn in list(self.listeners):
                try:
                    fn(merged)
                except Exception:
                    pass
        finally:
            with self._lock:
                self.running.pop(rec["task"], None)
                self.threads.pop(rec["task"], None)
            lk.release()

    # -- the loop
    def tick(self) -> list[str]:
        """Start whatever is due. Returns the ids of the tasks started. Safe to call from several processes."""
        now = self.clock()
        started: list[str] = []
        if now - getattr(self, "_pruned", 0.0) > 3600:
            self._pruned = now
            self.prune()
        for snap in self._read():
            if not snap.get("enabled", True):
                continue
            if snap.get("next_at") is not None and now < float(snap["next_at"]):
                continue
            with self._lock:
                if len(self.running) >= MAX_PARALLEL:
                    break
            claim = self._claim(snap["id"], now)
            if claim is None:
                continue
            task, reason, missed = claim
            if missed:
                self._append({"run": uuid.uuid4().hex[:10], "task": task["id"], "name": task.get("name", ""), "status": "missed",
                              "started": float(missed["due"]), "ended": now, "missed": missed["count"],
                              "error": (f"{missed['count']} scheduled run{'s' if missed['count'] != 1 else ''} did not happen because Axon was not running."
                                        + (" It ran once now to catch up." if reason == "catch-up" else " Missed runs are not replayed."))})
            if reason is None:
                continue
            if self._start(task, reason) is None:
                self._append({"run": uuid.uuid4().hex[:10], "task": task["id"], "name": task.get("name", ""), "status": "skipped",
                              "started": now, "ended": now, "error": "skipped: previous run still active", "failure": "overlap"})
            else:
                started.append(task["id"])
        return started

    def _claim(self, task_id: str, now: float) -> tuple[dict[str, Any], str | None, dict[str, Any] | None] | None:
        """Under the file lock: if the task is still due, move its next time forward and say what to do about it.
        Returns (task, reason to run or None, missed info or None). Another process that got there first leaves None."""
        with self._mutex(self.folder):
            tasks = self._read()
            for t in tasks:
                if t["id"] != task_id:
                    continue
                if not t.get("enabled", True):
                    return None
                due = t.get("next_at")
                if due is None:
                    t["next_at"] = next_after(t, now) if t.get("kind") != "once" else None
                    if t["next_at"] is None and t.get("kind") == "once":
                        t["enabled"] = False
                    self._write(tasks)
                    return None
                due = float(due)
                if now < due:
                    return None
                late = now - due
                missed = None
                reason: str | None = "schedule"
                if late > MISSED_GRACE_S:
                    count, cur = 1, due
                    while count < 1000:
                        nxt = next_after(t, cur) if t["kind"] != "interval" else cur + float(t["every_s"])
                        if nxt is None or nxt > now:
                            break
                        count, cur = count + 1, nxt
                    missed = {"count": count, "due": due}
                    reason = "catch-up" if t.get("catch_up") else None
                t["next_at"] = next_after(t, now)
                if t["kind"] == "once" and reason is None:
                    t["enabled"] = False
                self._write(tasks)
                return dict(t), reason, missed
        return None

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        if fn not in self.listeners:
            self.listeners.append(fn)

    def start(self, every: float = 1.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()

            def loop() -> None:
                while not self._stop.wait(every):
                    try:
                        self.tick()
                    except Exception:
                        pass
            self._thread = threading.Thread(target=loop, name="axon-scheduler", daemon=True)
            self._thread.start()

    def stop(self, cancel_runs: bool = True) -> None:
        self._stop.set()
        if cancel_runs:
            for ctl in list(self.running.values()):
                ctl.cancel("stopped")
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ─── one scheduler per process ────────────────────────────────────────────────

_SCHEDULER: Scheduler | None = None
_SLOCK = threading.Lock()


def get_scheduler() -> Scheduler:
    global _SCHEDULER
    with _SLOCK:
        if _SCHEDULER is None:
            _SCHEDULER = Scheduler()
        return _SCHEDULER


def ensure_started() -> Scheduler | None:
    """Start the loop for this process (the app or a terminal session). Off in tests, CI and with AXON_NO_SCHEDULER=1."""
    if os.environ.get("AXON_NO_SCHEDULER") or os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    s = get_scheduler()
    s.start()
    try:
        from axon.features import notify
        notify.install_scheduler()          # webhooks and open pages hear about finished scheduled tasks
    except Exception:
        pass
    return s
