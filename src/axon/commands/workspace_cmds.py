"""Workspace commands: /cd /add-dir /loop, plus ``purge(scope)`` for ``axon purge``."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from axon.commands.common import (
    confirm, err, header, hint, info, is_tty, kv, ok, update_settings, update_settings_file, usage, ws_of,
    typed_hint,
    typed_only,
)
from axon.commands.registry import CommandResult, command
from axon.tools.cron import cron_disabled
from axon.ui.theme import BOLD, CYAN, RST, SLATE, WHITE

CAT = "Memory & Project"


def _done() -> CommandResult:
    return CommandResult(handled=True)


def _resolve_dir(agent: Any, raw: str) -> Path | None:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(os.path.expandvars(raw)).expanduser()
    if not p.is_absolute():
        p = ws_of(agent) / p
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


# ─── /add-dir ──────────────────────────────────────────────────────────────

ADD_DIR_USAGE = """/add-dir                     list extra working directories
/add-dir <path> [--save]     allow reading/editing <path> this session (--save: .axon/settings.local.json)
/add-dir remove <path>       stop allowing it"""


def add_working_dir(agent: Any, path: Path, save: bool = False) -> Path | None:
    """Allow ``path`` like the workspace: settings.additional_dirs + the path jail (+ optional local settings)."""
    from axon.permissions.paths import add_additional_dir
    p = add_additional_dir(path)
    dirs = [Path(d) for d in getattr(agent.settings, "additional_dirs", []) or []]
    if not any(str(d) == str(p) for d in dirs):
        update_settings(agent, additional_dirs=dirs + [p])
    saved = None
    if save:
        from axon.commands.common import read_settings_file, settings_file
        sf = settings_file("local", ws_of(agent))
        data = read_settings_file(sf) or {}
        cur = list(((data.get("permissions") or {}) if isinstance(data.get("permissions"), dict) else {}).get("additionalDirectories") or [])
        if str(p) not in cur:
            cur.append(str(p))
        saved = update_settings_file({"permissions.additionalDirectories": cur}, "local", ws_of(agent))
    _fire_hook(agent, "DirectoryAdded", {"directory": str(p)})
    return saved


def _fire_hook(agent: Any, event: str, payload: dict[str, Any]) -> None:
    hooks = getattr(agent, "hooks", None)
    if hooks is None:
        return
    try:
        if hasattr(hooks, "has") and not hooks.has(event):
            return
        hooks.run(event, payload)
    except Exception:
        pass


@command("add-dir", category=CAT, description="Allow another directory for reading and editing",
         usage=ADD_DIR_USAGE)
def handle_add_dir(agent: Any, arg: str) -> CommandResult:
    from axon.permissions.paths import remove_additional_dir
    words = arg.split()
    save = "--save" in words
    rest = " ".join(w for w in words if w != "--save").strip()
    perms = getattr(agent, "permissions", None)
    if not rest:
        dirs = perms.working_dirs() if hasattr(perms, "working_dirs") else [Path(d) for d in getattr(agent.settings, "additional_dirs", []) or []]
        header("Working directories")
        kv("Workspace", ws_of(agent))
        for d in dirs:
            kv("Additional", d)
        if not dirs:
            print(f"    {SLATE}No additional directories.{typed_only(' ' + ADD_DIR_USAGE.splitlines()[1].strip())}{RST}")
        print()
        return _done()
    if rest.lower().startswith(("remove ", "rm ")):
        target = rest.split(maxsplit=1)[1]
        p = _resolve_dir(agent, target) or Path(target).expanduser()
        removed = remove_additional_dir(p)
        dirs = [Path(d) for d in getattr(agent.settings, "additional_dirs", []) or [] if str(Path(d)) != str(p)]
        update_settings(agent, additional_dirs=dirs)
        from axon.commands.common import read_settings_file, settings_file
        data = read_settings_file(settings_file("local", ws_of(agent))) or {}
        cur = (data.get("permissions") or {}).get("additionalDirectories") if isinstance(data.get("permissions"), dict) else None
        if isinstance(cur, list) and str(p) in cur:
            update_settings_file({"permissions.additionalDirectories": [c for c in cur if c != str(p)]}, "local", ws_of(agent))
            removed = True
        if perms is not None and hasattr(perms, "reload"):
            perms.reload()
        (ok if removed else info)(f"{'Removed' if removed else 'Was not allowed:'} {p}")
        return _done()
    p = _resolve_dir(agent, rest)
    if p is None:
        err(f"Not a directory: {rest}")
        return _done()
    from axon.permissions.paths import is_protected_path
    why = is_protected_path(p)
    if why:
        err(f"Refusing to add {p}: {why}.")
        return _done()
    saved = add_working_dir(agent, p, save=save)
    skills = getattr(agent, "skills", None)
    if skills is not None and hasattr(skills, "add_directory"):
        try:
            skills.add_directory(p)          # skills under <dir>/.claude/skills become available
        except Exception:
            pass
    ok(f"Added working directory {WHITE}{p}{RST}" + (f" {SLATE}(saved to {saved}){RST}" if saved else ""))
    return _done()


# ─── /cd ───────────────────────────────────────────────────────────────────

def change_workspace(agent: Any, new_ws: Path) -> str:
    """Move the session to ``new_ws``: cwd, settings, permissions, session storage, checkpoints, memory.

    The conversation continues; its transcript is copied into the new project's session dir."""
    old_ws = ws_of(agent)
    os.chdir(new_ws)
    update_settings(agent, workspace=new_ws)
    sess = getattr(agent, "session", None)
    sid = getattr(sess, "active_session_id", "") or ""
    if sess is not None and sid:
        try:
            from axon.session.store import SessionStore
            new_store = SessionStore(new_ws)
            new_store.model = getattr(sess, "model", None)
            old_file = getattr(sess, "active_file", None)
            new_store.open(sid)
            if old_file is not None and Path(old_file).exists() and not new_store.active_file.exists():
                new_store.active_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_file, new_store.active_file)
            agent.session = new_store
        except Exception:
            pass
    try:
        from axon.session.checkpoint import CheckpointManager
        old_cp = getattr(agent, "checkpoints", None)
        if old_cp is not None:
            try:
                old_cp.commit_turn_snapshot(label="(before /cd)")
            except Exception:
                pass
        agent.checkpoints = CheckpointManager(new_ws, sid or None)
    except Exception:
        pass
    perms = getattr(agent, "permissions", None)
    if perms is not None and hasattr(perms, "reload"):
        try:
            perms.reload()
        except Exception:
            pass
    try:
        from axon.skills.manager import SkillManager
        if getattr(agent, "skills", None) is not None:
            agent.skills = SkillManager(new_ws)
    except Exception:
        pass
    if hasattr(agent, "reload_memory"):
        try:
            agent.reload_memory()
        except Exception:
            pass
    _fire_hook(agent, "CwdChanged", {"old_cwd": str(old_ws), "new_cwd": str(new_ws)})
    return sid


@command("cd", category=CAT, description="Move the session to another workspace directory",
         usage="/cd <directory>    (project memory, permissions, skills and session storage follow)")
def handle_cd(agent: Any, arg: str) -> CommandResult:
    if not arg.strip():
        kv("Workspace", ws_of(agent))
        print()
        return _done()
    p = _resolve_dir(agent, arg)
    if p is None:
        err(f"Not a directory: {arg.strip()}")
        return _done()
    from axon.permissions.paths import is_protected_path
    why = is_protected_path(p)
    if why:
        err(f"Refusing to move into {p}: {why}.")
        return _done()
    from axon.permissions.paths import too_broad_for_workspace
    broad = too_broad_for_workspace(p)
    if broad:
        err(broad)
        return _done()
    old = ws_of(agent)
    if str(p) == str(old):
        info(f"Already in {p}")
        return _done()
    change_workspace(agent, p)
    ok(f"Workspace {SLATE}{old}{RST} → {BOLD}{WHITE}{p}{RST}")
    hint("    Memory, permissions, skills and session storage now use the new directory.")
    print()
    return _done()


# ─── /loop ─────────────────────────────────────────────────────────────────

_CLEAN_MINUTES = (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, *(1440 * d for d in range(1, 31)))
SELF_PACED_MIN_S = 60                # the model picks the next delay of a self-paced loop inside these limits
SELF_PACED_MAX_S = 3600
FALLBACK_WAKE_S = 1200               # an iteration that neither rescheduled nor stopped: one more try after 20 minutes
MAX_SCHEDULED = 50                   # per session, /loop jobs and CronCreate jobs together
EXPIRY_S = 7 * 86400                 # a recurring job fires one final time after this, then deletes itself
LOOP_FILE_MAX_BYTES = 25_000
LOOP_WAKE = threading.Event()   # set when a loop queued a prompt (input loops may poll it to wake up)

# What a bare /loop does each iteration when no loop.md exists.
MAINTENANCE_PROMPT = """Maintenance pass. Work through these in order and stop at the first that has something to do:
1. Continue any unfinished work from this conversation.
2. Tend to the current branch's pull request: review comments, failed CI runs, merge conflicts.
3. Run a cleanup pass, such as a bug hunt or a simplification, in code this conversation touched.
Do not start new initiatives outside that scope. Irreversible actions, such as pushing or deleting, go ahead only when they continue something this conversation already authorized."""
_PACING_NOTE = ("<system-reminder>\nThis is one iteration of a self-paced /loop. When you finish it, call ScheduleWakeup: "
                f"delay_seconds from {SELF_PACED_MIN_S} to {SELF_PACED_MAX_S} based on what you saw (short while something is "
                "about to finish, long when nothing is pending) and a one-line reason, or stop=true when the task is complete.\n</system-reminder>")


class LoopLimitError(ValueError):
    pass


@dataclass
class LoopJob:
    id: int
    prompt: str
    interval_s: float
    max_runs: int | None = None
    runs: int = 0
    next_at: float = 0.0
    nominal_at: float = 0.0              # the cron time the job last waited for (so a busy session never catches up)
    queued_ids: list[int] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])   # the id CronList and CronDelete use
    cron: Any = None                     # axon.tools.cron.CronSpec for a CronCreate job
    recurring: bool = True
    expires_at: float | None = None
    plain: bool = False                  # not typed by the person: reaches the model as text, never as a command
    default_prompt: bool = False         # a bare /loop: read loop.md (or the built-in prompt) again on every run
    self_paced: bool = False             # the model picks each delay with ScheduleWakeup
    running: bool = False                # self-paced: an iteration is being worked on now
    rescheduled: bool = False
    fallback_used: bool = False
    reason: str = ""

    @property
    def active(self) -> bool:
        return not self.stop.is_set()


def parse_interval(token: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)", token.strip().lower())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)[0]
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_count(token: str) -> int | None:
    m = re.fullmatch(r"(\d+)x?", token.strip().lower())
    return int(m.group(1)) if m else None


def _loops(agent: Any) -> list[LoopJob]:
    jobs = getattr(agent, "loops", None)
    if not isinstance(jobs, list):
        jobs = []
        try:
            agent.loops = jobs
        except Exception:
            pass
    return jobs


def _queue(agent: Any) -> Any:
    q = getattr(agent, "message_queue", None)
    if q is None:
        from axon.agent.state import MessageQueue
        q = agent.message_queue = MessageQueue()
    return q


def _pending(q: Any, ids: list[int]) -> bool:
    return any(getattr(it, "id", None) in ids for it in list(getattr(q, "items", []) or []))


def _tick(agent: Any, job: LoopJob, now: float | None = None) -> bool:
    """Queue the job's prompt when due and the previous run is no longer waiting. Returns True if queued."""
    now = time.time() if now is None else now
    if not job.active or now < job.next_at or cron_disabled():
        return False
    q = _queue(agent)
    if _pending(q, job.queued_ids):
        return False  # the REPL has not picked up the previous run yet: never stack runs
    item = q.push(_fire_text(agent, job), plain=job.plain)
    job.queued_ids = [item.id]
    job.runs += 1
    job.rescheduled = False
    following = _following_run(job, now)
    job.next_at = following if following is not None else math.inf
    LOOP_WAKE.set()
    if (job.max_runs is not None and job.runs >= job.max_runs) or not job.recurring or following is None \
            or (job.expires_at is not None and now >= job.expires_at):
        job.stop.set()
    if job.cron is not None and (job.stop.is_set() or job.max_runs is not None):
        save_cron_jobs(agent)
    return True


def _following_run(job: LoopJob, now: float) -> float | None:
    """When the job runs after a run at *now*; missed times are not caught up. A self-paced job waits for its model."""
    if job.self_paced:
        return math.inf
    if job.cron is None:
        return now + job.interval_s
    return _cron_run_after(job, max(now, job.nominal_at))


def _cron_run_after(job: LoopJob, after: float) -> float | None:
    """The next time the job's cron expression matches after *after*. Missed times are never caught up."""
    at = job.cron.next_after(after)
    if at is not None:
        job.nominal_at = at
    return at


def _fire_text(agent: Any, job: LoopJob) -> str:
    """The queued text. Text the person did not type starts with a label, so whatever reads the queue can never take
    it for a command, and the model can tell it from a new message."""
    if job.default_prompt:
        return f"[/loop] {load_default_prompt(ws_of(agent))[0]}"
    return f"[Scheduled task {job.uid}, created earlier in this session] {job.prompt}" if job.plain else job.prompt


def load_default_prompt(workspace: Path) -> tuple[str, str]:
    """(prompt, where it came from) for a bare /loop: the first non-empty loop.md (project before user), cut at
    LOOP_FILE_MAX_BYTES, else the built-in maintenance prompt."""
    from axon import storage
    for path in (Path(workspace) / ".axon" / "loop.md", Path(workspace) / ".claude" / "loop.md",
                 storage.axon_home() / "loop.md", Path.home() / ".claude" / "loop.md"):
        try:
            if path.is_file():
                with path.open("rb") as f:
                    text = f.read(LOOP_FILE_MAX_BYTES).decode("utf-8", "ignore").strip()
                if text:
                    return text, str(path)
        except OSError:
            continue
    return MAINTENANCE_PROMPT, "the built-in maintenance prompt"


def active_jobs(agent: Any) -> list[LoopJob]:
    return [j for j in _loops(agent) if j.active]


def _check_room(agent: Any) -> None:
    if len(active_jobs(agent)) >= MAX_SCHEDULED:
        raise LoopLimitError(f"This session already has {MAX_SCHEDULED} scheduled tasks, the most it can hold. Cancel one first.")


def _start_job(agent: Any, job: LoopJob, clock: Callable[[], float], start_thread: bool) -> LoopJob:
    _loops(agent).append(job)
    _tick(agent, job, clock())  # runs now when next_at has come (always for a /loop)
    if start_thread and job.active:
        def run() -> None:
            while not job.stop.wait(min(1.0, job.interval_s)):
                try:
                    _tick(agent, job, clock())
                except Exception:
                    pass
        job.thread = threading.Thread(target=run, daemon=True, name=f"axon-loop-{job.id}")
        job.thread.start()
    return job


def _new_id(agent: Any) -> int:
    return max((j.id for j in _loops(agent)), default=0) + 1


def interval_cron(seconds: float) -> tuple[str, float]:
    """The cron expression for a /loop interval and the interval it really gives. Cron works in whole minutes, so
    seconds round up; an interval no cron step can hold (7m, 90m) goes to the nearest one that can."""
    asked = max(1, math.ceil(seconds / 60))
    minutes = min(_CLEAN_MINUTES, key=lambda c: (abs(c - asked), c))
    if minutes == 1:
        expr = "* * * * *"
    elif minutes < 60:
        expr = f"*/{minutes} * * * *"
    elif minutes == 60:
        expr = "0 * * * *"
    elif minutes < 1440:
        expr = f"0 */{minutes // 60} * * *"
    elif minutes == 1440:
        expr = "0 0 * * *"
    else:
        expr = f"0 0 */{minutes // 1440} * *"
    return expr, minutes * 60.0


def start_interval_loop(agent: Any, prompt: str, interval_s: float, max_runs: int | None = None,
                        clock: Callable[[], float] = time.time, start_thread: bool = True,
                        default_prompt: bool = False) -> LoopJob:
    """/loop 5m: the first run goes now, then the cron step for that interval takes over."""
    from axon.tools.cron import CronSpec
    expr, seconds = interval_cron(interval_s)
    now = clock()
    job = LoopJob(id=_new_id(agent), prompt=prompt, interval_s=seconds, max_runs=max_runs, next_at=now, cron=CronSpec(expr),
                  expires_at=now + EXPIRY_S, plain=default_prompt, default_prompt=default_prompt)
    _start_job(agent, job, clock, start_thread)
    save_cron_jobs(agent)
    return job


def start_self_paced_loop(agent: Any, prompt: str, default_prompt: bool = False, clock: Callable[[], float] = time.time,
                          start_thread: bool = True) -> LoopJob:
    """/loop with no interval: the first run goes now, then the model picks each delay with ScheduleWakeup."""
    job = LoopJob(id=_new_id(agent), prompt=prompt, interval_s=float(SELF_PACED_MIN_S), next_at=clock(),
                  expires_at=clock() + EXPIRY_S, plain=default_prompt, default_prompt=default_prompt, self_paced=True)
    return _start_job(agent, job, clock, start_thread)


def start_cron_job(agent: Any, spec: Any, prompt: str, recurring: bool = True, clock: Callable[[], float] = time.time,
                   start_thread: bool = True) -> LoopJob:
    """A CronCreate job: written by the model, so it always fires as plain text."""
    _check_room(agent)
    now = clock()
    job = LoopJob(id=_new_id(agent), prompt=prompt, interval_s=60.0, next_at=math.inf, cron=spec, recurring=recurring,
                  expires_at=now + EXPIRY_S if recurring else None, plain=True)
    following = _cron_run_after(job, now)
    if following is None:
        raise LoopLimitError(f"'{spec.text}' never fires again.")
    job.next_at = following
    _start_job(agent, job, clock, start_thread)
    save_cron_jobs(agent)
    return job


# ─── CronCreate jobs across a resume ───────────────────────────────────────

def _cron_file(agent: Any, session_id: str | None = None) -> Path | None:
    from axon import storage
    store = getattr(agent, "session", None)
    root, sid = getattr(store, "session_dir", None), session_id or getattr(store, "active_session_id", None)
    if not isinstance(root, Path) or not isinstance(sid, str) or not sid:
        return None
    return root / storage._sid_dirname(sid) / "scheduled_tasks.json"


def save_cron_jobs(agent: Any) -> None:
    """Record this session's scheduled jobs (not self-paced loops, which have no schedule to keep). Called when the list really changes, never when a session is merely
    left, which ends the jobs in this process but must leave them to come back on a resume."""
    path = _cron_file(agent)
    if path is None:
        return
    rows = [{"id": j.uid, "cron": j.cron.text, "prompt": j.prompt, "recurring": j.recurring, "expires_at": j.expires_at,
             "at": j.nominal_at, "plain": j.plain, "default_prompt": j.default_prompt, "max_runs": j.max_runs, "runs": j.runs,
             "interval_s": j.interval_s} for j in active_jobs(agent) if j.cron is not None]
    try:
        if not rows and not path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # the prompts are the person's words
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, path)
    except OSError:
        pass


def restore_cron_jobs(agent: Any, session_id: str | None = None, clock: Callable[[], float] = time.time,
                      start_thread: bool = True) -> int:
    """Bring back a resumed session's scheduled jobs, except recurring jobs past their expiry and one-shots whose time
    has passed. Missed runs are not caught up: the next run is the first one due from now, with its jitter."""
    from axon.tools.cron import CronError, CronSpec
    path = _cron_file(agent, session_id)
    if path is None or cron_disabled() or not getattr(agent, "fires_scheduled_tasks", False):
        return 0
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    now, n = clock(), 0
    have = {j.uid for j in active_jobs(agent)}
    for row in rows if isinstance(rows, list) else []:
        try:
            uid, prompt, recurring = str(row["id"]), str(row["prompt"]), bool(row["recurring"])
            expires = None if row.get("expires_at") is None else float(row["expires_at"])
            spec = CronSpec(str(row["cron"]))
            at = float(row.get("at") or 0)
            max_runs = None if row.get("max_runs") is None else int(row["max_runs"])
            runs, interval = int(row.get("runs") or 0), float(row.get("interval_s") or 60.0)
        except (KeyError, TypeError, ValueError, AttributeError, CronError):
            continue
        if uid in have or (recurring and expires is not None and expires <= now) or (not recurring and at <= now):
            continue
        if len(active_jobs(agent)) >= MAX_SCHEDULED:
            break
        job = LoopJob(id=_new_id(agent), prompt=prompt, interval_s=interval, next_at=math.inf, uid=uid, cron=spec, runs=runs,
                      max_runs=max_runs, recurring=recurring, expires_at=expires, plain=row.get("plain") is not False,
                      default_prompt=row.get("default_prompt") is True)
        following = _cron_run_after(job, now)
        if following is None:
            continue
        job.next_at = following
        _start_job(agent, job, clock, start_thread)
        n += 1
    if n:
        info(f"Restored {n} scheduled task{'' if n == 1 else 's'} from this session. /loop list shows them.")
    return n


# ─── self-paced loops: the iteration around each queued run ────────────────

def self_paced_job(agent: Any) -> LoopJob | None:
    return next((j for j in active_jobs(agent) if j.self_paced), None)


def schedule_wakeup(job: LoopJob, delay_s: float, reason: str = "", clock: Callable[[], float] = time.time) -> None:
    job.next_at = clock() + delay_s
    job.reason = reason
    job.rescheduled = True
    job.fallback_used = False


def _job_for_item(agent: Any, item_id: int) -> LoopJob | None:
    return next((j for j in active_jobs(agent) if j.self_paced and item_id in j.queued_ids), None)


def before_queued_run(agent: Any, item_id: int) -> None:
    """The REPL is about to run a queued prompt. If a self-paced loop queued it, the model is told how to pace itself."""
    job = _job_for_item(agent, item_id)
    if job is None:
        return
    job.running = True
    pending = getattr(agent, "_pending_context", None)
    if isinstance(pending, list):
        pending.append(_PACING_NOTE)


def after_queued_run(agent: Any, item_id: int, clock: Callable[[], float] = time.time) -> None:
    """The queued prompt has run. Report the model's choice, or fall back when it chose nothing."""
    job = _job_for_item(agent, item_id)
    if job is None:
        return
    job.running = False
    if job.rescheduled:
        why = f": {job.reason}" if job.reason else ""
        info(f"Next loop run in {fmt_wait(job.next_at - clock())}{why}")
    elif job.fallback_used:
        stop_loops(agent, job.uid)
        info("Loop ended: this run did not schedule another.")
    else:
        job.fallback_used = True
        job.next_at = clock() + FALLBACK_WAKE_S
        info(f"No next run was chosen: one more in {fmt_wait(FALLBACK_WAKE_S)}, and the loop ends if that one chooses none either.")


def pending_wakeup(agent: Any, clock: Callable[[], float] = time.time) -> LoopJob | None:
    """The self-paced loop waiting for its next run (not one being worked on or already queued)."""
    job = self_paced_job(agent)
    if job is None or job.running or _pending(_queue(agent), job.queued_ids):
        return None
    return job


def cancel_wakeup(agent: Any) -> bool:
    """Esc or Ctrl+C at the prompt during the wait."""
    job = pending_wakeup(agent)
    if job is None:
        return False
    stop_loops(agent, job.uid)
    return True


def loop_status_rows(agent: Any, clock: Callable[[], float] = time.time) -> list[str]:
    job = pending_wakeup(agent)
    if job is None or job.next_at == math.inf:
        return []
    return [f"  {SLATE}↻ next loop run in {fmt_wait(job.next_at - clock())} · esc stops it{RST}"]


def fmt_wait(seconds: float) -> str:
    s = max(0, int(seconds + 0.5))
    return f"{s // 60}m" if s >= 90 else f"{s}s"


def job_schedule(job: LoopJob) -> str:
    if job.self_paced:
        return "self-paced"
    if job.cron is not None:
        return f"{job.cron.text} ({'recurring' if job.recurring else 'once'})"
    return f"every {_fmt_interval(job.interval_s)}"


def job_prompt(job: LoopJob) -> str:
    return "loop.md or the built-in maintenance prompt" if job.default_prompt else job.prompt


def job_next(job: LoopJob) -> str:
    if job.next_at == math.inf:
        return "after this run"
    return datetime.fromtimestamp(job.next_at).strftime("%a %d %b %H:%M:%S")


def stop_loops(agent: Any, job_id: int | str | None = None) -> int:
    n = 0
    q = getattr(agent, "message_queue", None)
    for job in _loops(agent):
        if job.active and (job_id is None or str(job_id) in (str(job.id), job.uid)):
            job.stop.set()
            n += 1
            if q is not None:
                for qid in job.queued_ids:
                    try:
                        q.remove(qid)
                    except Exception:
                        pass
    _loops(agent)[:] = [j for j in _loops(agent) if j.active]
    return n


LOOP_USAGE = """/loop <N>x <prompt>          run a prompt N times in a row (e.g. /loop 3x run the tests and fix one failure)
/loop <interval> <prompt>    re-run on a schedule while idle: 5m, 1h, 1d (1 minute at least); or end with "every 2 hours"
/loop <interval> <N>x <prompt>   ... at most N times
/loop <prompt>               self-paced: after each run the model picks the next delay (1 minute to 1 hour); esc stops it
/loop [interval]             the same with .axon/loop.md (or .claude/loop.md, ~/.axon/loop.md), else a built-in maintenance prompt
/loop list                   list loops and scheduled tasks
/loop stop [id]              stop all loops (or one)"""
_TRAILING_EVERY = re.compile(r"^(.*\S)\s+every\s+(\d+(?:\.\d+)?\s*[a-z]+)\s*$", re.I | re.S)
_JOB_ID = re.compile(r"#?\d+|[0-9a-f]{8}", re.I)


def _start_message(job: LoopJob, source: str, cap: str = "") -> None:
    what = f"{SLATE}{source}{RST}" if job.default_prompt else f"{WHITE}{job.prompt}{RST}"
    if job.self_paced:
        ok(f"Loop #{job.id}: {what}, self-paced")
        typed_hint("    After each run the model picks the next delay, 1 minute to 1 hour · esc during the wait stops it · /loop stop")
    else:
        ok(f"Loop #{job.id}: {what} every {BOLD}{_fmt_interval(job.interval_s)}{RST}{cap} {SLATE}· {job.cron.text if job.cron is not None else ''} · id {job.uid}{RST}")
        typed_hint("    Runs while the prompt is idle (press Enter on an empty prompt to pick up a due run) · /loop stop")
    print()


def _start_self_paced(agent: Any, prompt: str) -> CommandResult:
    running = self_paced_job(agent)
    if running is not None:
        err(f"Loop #{running.id} is already self-paced. Stop it first: /loop stop {running.id}")
        return _done()
    source = "" if prompt else load_default_prompt(ws_of(agent))[1]
    _start_message(start_self_paced_loop(agent, prompt, default_prompt=not prompt), source)
    return _done()


@command("loop", category="Session", description="Repeat a prompt N times, on an interval, or at a pace the model picks",
         aliases=("repeat",), usage=LOOP_USAGE)
def handle_loop(agent: Any, arg: str) -> CommandResult:
    if cron_disabled():
        err("Loops are turned off (AXON_DISABLE_CRON).")
        return _done()
    words = arg.split()
    if len(words) == 1 and words[0].lower() in ("list", "ls"):
        jobs = active_jobs(agent)
        header("Loops", f"{len(jobs)} active")
        for j in jobs:
            cap = f"/{j.max_runs}" if j.max_runs else ""
            print(f"    {CYAN}#{j.id}{RST} {SLATE}{j.uid}{RST} {WHITE}{job_schedule(j)}{RST} · run {j.runs}{cap} · next {job_next(j)}  {SLATE}{job_prompt(j)[:60]}{RST}")
        if not jobs:
            print(f"    {SLATE}No active loops.{RST}")
        print()
        typed_hint("    " + LOOP_USAGE.replace("\n", "\n    "))
        print()
        return _done()
    # "stop" or "stop 2" only: "clear the old caches" is a prompt.
    if words and words[0].lower() in ("stop", "off", "cancel", "clear") and (len(words) == 1 or (len(words) == 2 and _JOB_ID.fullmatch(words[1]))):
        jid = words[1].lstrip("#") if len(words) > 1 else None
        n = stop_loops(agent, jid)
        save_cron_jobs(agent)
        (ok if n else info)(f"Stopped {n} loop(s)" if n else "No active loops.")
        return _done()
    trailing = _TRAILING_EVERY.match(arg.strip())
    if trailing and parse_interval(trailing.group(2)) is not None:
        words = [trailing.group(2).replace(" ", ""), *trailing.group(1).split()]
    interval = parse_interval(words[0]) if words else None
    count = parse_count(words[0]) if words and interval is None else None
    if interval is None and count is None:       # no interval: the model paces it
        prompt = " ".join(words)
        return _guarded_start(agent, lambda: _start_self_paced(agent, prompt), prompt)
    rest = words[1:]
    if interval is not None and rest and parse_count(rest[0]) is not None and rest[0].lower().endswith("x"):
        count = parse_count(rest[0])
        rest = rest[1:]
    prompt = " ".join(rest).strip()
    if interval is None and not prompt:
        usage(LOOP_USAGE)
        return _done()
    if interval is None:
        n = max(1, min(int(count or 1), 100))
        q = _queue(agent)
        for _ in range(n):
            q.push(prompt)
        ok(f"Queued {BOLD}{n}×{RST} {WHITE}{prompt}{RST} {SLATE}(runs now, one after another{typed_only(' · /queue clear stops it')}){RST}")
        return _done()
    cap = f", at most {count} times" if count else ""
    source = "" if prompt else load_default_prompt(ws_of(agent))[1]

    def start() -> CommandResult:
        job = start_interval_loop(agent, prompt, interval, max_runs=count, default_prompt=not prompt)
        _start_message(job, source, cap)
        if job.interval_s != interval:
            info(f"Rounded from {_fmt_interval(interval)}: a schedule works in whole minutes and even steps, so {_fmt_interval(job.interval_s)} is the nearest.")
        return _done()
    return _guarded_start(agent, start, prompt)


def _guarded_start(agent: Any, start: Callable[[], CommandResult], prompt: str) -> CommandResult:
    if prompt.lower().startswith("/loop"):
        usage(LOOP_USAGE)
        return _done()
    try:
        _check_room(agent)
    except LoopLimitError as e:
        err(str(e))
        return _done()
    return start()


def _fmt_interval(s: float) -> str:
    if s >= 3600 and s % 3600 == 0:
        return f"{int(s // 3600)}h"
    if s >= 60 and s % 60 == 0:
        return f"{int(s // 60)}m"
    return f"{int(s)}s"


# ─── purge (for `axon purge`, exposed by the CLI) ──────────────────────────

PURGE_SCOPES = ("project", "caches", "all")


def purge_targets(scope: str, workspace: Path | None = None) -> list[Path]:
    """Directories ``purge`` would delete. Settings, keys (.env), memories in AXON.md and
    user extensions (agents/commands/skills/plugins/output-styles) are never touched."""
    from axon import storage
    home = storage.axon_home()
    if scope == "project":
        if workspace is None:
            raise ValueError("purge('project') needs a workspace")
        return [storage.project_dir(Path(workspace), create=False)]
    if scope == "caches":
        return [home / "cache"]
    if scope == "all":
        return [home / "projects", home / "cache", home / "sessions", home / "exports", home / "logs",
                home / "usage.jsonl", home / "history.jsonl"]
    raise ValueError(f"unknown purge scope {scope!r} (use {', '.join(PURGE_SCOPES)})")


def purge(scope: str = "project", workspace: Path | None = None, *, assume_yes: bool = False,
          confirm_fn: Callable[[str], bool] | None = None) -> list[Path]:
    """Delete Axon state for ``scope`` after confirmation. Returns the paths actually removed.

    scope: "project" (~/.axon/projects/<key> for ``workspace``: sessions, traces, checkpoints, auto memory),
    "caches" (~/.axon/cache) or "all" (every project, cache, legacy sessions, exports, logs, usage and history)."""
    targets = [p for p in purge_targets(scope, workspace) if p.exists()]
    if not targets:
        return []
    question = "Permanently delete " + ", ".join(str(t) for t in targets) + "?"
    if not assume_yes:
        asker = confirm_fn or (lambda q: confirm(q, default=False))
        if not asker(question):
            return []
    removed: list[Path] = []
    for t in targets:
        try:
            if t.is_dir():
                shutil.rmtree(t)
            else:
                t.unlink()
            removed.append(t)
        except OSError:
            continue
    return removed


def purge_main(argv: list[str]) -> int:
    """`axon purge [project|caches|all] [--yes]` entry point (P-CORE wires it into cli.py)."""
    args = [a for a in argv if not a.startswith("-")]
    yes = any(a in ("-y", "--yes") for a in argv)
    scope = args[0] if args else "project"
    if scope not in PURGE_SCOPES:
        print(f"usage: axon purge [{'|'.join(PURGE_SCOPES)}] [--yes]")
        return 2
    if not yes and not is_tty():
        print("axon purge: refusing to delete without a terminal; pass --yes")
        return 2
    try:
        removed = purge(scope, Path.cwd(), assume_yes=yes)
    except ValueError as e:
        print(f"axon purge: {e}")
        return 2
    if removed:
        for p in removed:
            print(f"  removed {p}")
    else:
        print("  nothing removed")
    return 0
