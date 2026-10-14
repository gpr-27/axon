"""Workspace commands: /cd /add-dir /loop, plus ``purge(scope)`` for ``axon purge``."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from axon.commands.common import (
    confirm, err, header, hint, info, is_tty, kv, ok, update_settings, update_settings_file, usage, ws_of,
)
from axon.commands.registry import CommandResult, command
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
            print(f"    {SLATE}No additional directories. {ADD_DIR_USAGE.splitlines()[1].strip()}{RST}")
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

MIN_INTERVAL_S = 10.0
LOOP_WAKE = threading.Event()   # set when a loop queued a prompt (input loops may poll it to wake up)


@dataclass
class LoopJob:
    id: int
    prompt: str
    interval_s: float
    max_runs: int | None = None
    runs: int = 0
    next_at: float = 0.0
    queued_ids: list[int] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return not self.stop.is_set()


def parse_interval(token: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(s|sec|secs|m|min|mins|h|hr|hrs)", token.strip().lower())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2)[0]
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


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
    if not job.active or now < job.next_at:
        return False
    q = _queue(agent)
    if _pending(q, job.queued_ids):
        return False  # the REPL has not picked up the previous run yet: never stack runs
    item = q.push(job.prompt)
    job.queued_ids = [item.id]
    job.runs += 1
    job.next_at = now + job.interval_s
    LOOP_WAKE.set()
    if job.max_runs is not None and job.runs >= job.max_runs:
        job.stop.set()
    return True


def start_interval_loop(agent: Any, prompt: str, interval_s: float, max_runs: int | None = None,
                        clock: Callable[[], float] = time.time, start_thread: bool = True) -> LoopJob:
    jobs = _loops(agent)
    job = LoopJob(id=max((j.id for j in jobs), default=0) + 1, prompt=prompt, interval_s=max(MIN_INTERVAL_S, interval_s),
                  max_runs=max_runs, next_at=clock())
    jobs.append(job)
    _tick(agent, job, clock())  # first run right away
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


def stop_loops(agent: Any, job_id: int | None = None) -> int:
    n = 0
    q = getattr(agent, "message_queue", None)
    for job in _loops(agent):
        if job.active and (job_id is None or job.id == job_id):
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
/loop <interval> <prompt>    re-run every interval while idle: 30s, 5m, 1h (min 10s)
/loop <interval> <N>x <prompt>   ... at most N times
/loop                         list loops
/loop stop [id]               stop all loops (or one)"""


@command("loop", category="Session", description="Repeat a prompt N times or on an interval",
         aliases=("repeat",), usage=LOOP_USAGE)
def handle_loop(agent: Any, arg: str) -> CommandResult:
    words = arg.split()
    if not words:
        jobs = [j for j in _loops(agent) if j.active]
        header("Loops", f"{len(jobs)} active")
        for j in jobs:
            left = max(0.0, j.next_at - time.time())
            cap = f"/{j.max_runs}" if j.max_runs else ""
            print(f"    {CYAN}#{j.id}{RST} every {WHITE}{_fmt_interval(j.interval_s)}{RST} · run {j.runs}{cap} · next in {left:.0f}s  {SLATE}{j.prompt[:60]}{RST}")
        if not jobs:
            print(f"    {SLATE}No active loops.{RST}")
        print()
        hint("    " + LOOP_USAGE.replace("\n", "\n    "))
        print()
        return _done()
    if words[0].lower() in ("stop", "off", "cancel", "clear"):
        jid = int(words[1].lstrip("#")) if len(words) > 1 and words[1].lstrip("#").isdigit() else None
        n = stop_loops(agent, jid)
        (ok if n else info)(f"Stopped {n} loop(s)" if n else "No active loops.")
        return _done()
    interval = parse_interval(words[0])
    count: int | None = None
    rest = words[1:]
    if interval is None:
        count = parse_count(words[0])
        if count is None:
            usage(LOOP_USAGE)
            return _done()
    elif rest and parse_count(rest[0]) is not None and rest[0].lower().endswith("x"):
        count = parse_count(rest[0])
        rest = rest[1:]
    prompt = " ".join(rest).strip()
    if not prompt or prompt.lower().startswith("/loop"):
        usage(LOOP_USAGE)
        return _done()
    if interval is None:
        n = max(1, min(int(count or 1), 100))
        q = _queue(agent)
        for _ in range(n):
            q.push(prompt)
        ok(f"Queued {BOLD}{n}×{RST} {WHITE}{prompt}{RST} {SLATE}(runs now, one after another · /queue clear stops it){RST}")
        return _done()
    job = start_interval_loop(agent, prompt, interval, max_runs=count)
    cap = f", at most {count} times" if count else ""
    ok(f"Loop #{job.id}: {WHITE}{prompt}{RST} every {BOLD}{_fmt_interval(job.interval_s)}{RST}{cap}")
    hint("    Runs while the prompt is idle (press Enter on an empty prompt to pick up a due run) · /loop stop")
    print()
    return _done()


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
