"""Scheduled tasks (scheduler.py, the /api/tasks routes, /schedule) and notifications (app settings, terminal signal).

The scheduler is tested with a fake clock and a fake runner; one test runs a task through the real SDK against the
fake provider, and the app routes are driven over HTTP."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
sys.path.insert(0, str(Path(__file__).parent))

from axon.features import dash_js_tasks  # noqa: E402
from axon.features import scheduler as sch  # noqa: E402
from axon.ui import notify  # noqa: E402
from test_keyboard_menus import picks  # noqa: E402,F401  (fixture)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi).timestamp()


NOON = at(2026, 11, 9, 12, 0)          # a Monday


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / "proj"
    f.mkdir()
    return f


class Runner:
    """Records calls; blocks while ``gate`` is cleared."""

    def __init__(self, status: str = "ok", cost: float = 0.02) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status, self.cost = status, cost
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def __call__(self, task: dict[str, Any], ctl: sch.RunControl) -> dict[str, Any]:
        self.calls.append(task)
        self.entered.set()
        self.gate.wait(10)
        if self.status == "boom":
            raise RuntimeError("the model is gone")
        return {"status": self.status, "cost_usd": self.cost, "session_id": "s-" + task["id"], "error": ""}


def make(tmp_path: Path, clock: Clock | None = None, runner: Any = None) -> tuple[sch.Scheduler, Clock, Any]:
    clock = clock or Clock(NOON)
    runner = runner or Runner()
    return sch.Scheduler(home=tmp_path / "home", clock=clock, runner=runner), clock, runner


def settle(s: sch.Scheduler) -> None:
    for th in list(s.threads.values()):
        th.join(5)


def interval(folder: Path, **kw: Any) -> dict[str, Any]:
    return {"prompt": "check the build", "folder": str(folder), "kind": "interval", "every_s": 600, **kw}


# ─── times ───────────────────────────────────────────────────────────────────

def test_next_run_for_each_kind() -> None:
    assert sch.next_after({"kind": "interval", "every_s": 600}, NOON) == NOON + 600
    daily = {"kind": "daily", "at": "09:30"}
    assert sch.next_after(daily, NOON) == at(2026, 11, 10, 9, 30)             # today's time has passed
    assert sch.next_after(daily, at(2026, 11, 9, 8, 0)) == at(2026, 11, 9, 9, 30)
    assert sch.next_after(daily, at(2026, 11, 9, 9, 30)) == at(2026, 11, 10, 9, 30)    # strictly after
    weekly = {"kind": "weekly", "at": "07:00", "days": [2, 4]}                 # Wed, Fri
    assert sch.next_after(weekly, NOON) == at(2026, 11, 11, 7, 0)
    assert sch.next_after(weekly, at(2026, 11, 13, 8, 0)) == at(2026, 11, 18, 7, 0)
    once = {"kind": "once", "once_at": NOON + 60}
    assert sch.next_after(once, NOON) == NOON + 60 and sch.next_after(once, NOON + 60) is None


def test_describe_in_plain_words() -> None:
    assert sch.describe({"kind": "interval", "every_s": 1800}) == "Every 30 minutes"
    assert sch.describe({"kind": "interval", "every_s": 3600}) == "Every hour"
    assert sch.describe({"kind": "daily", "at": "09:00"}) == "Every day at 09:00"
    assert sch.describe({"kind": "weekly", "at": "09:00", "days": [0, 3]}) == "Every Mon, Thu at 09:00"


# ─── validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fields,message", [
    ({"prompt": "   "}, "what the task should do"),
    ({"prompt": "use key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 to log in"}, "key or password"),
    ({"prompt": "x" * 9000}, "too long"),
    ({"every_s": 30}, "shortest interval"),
    ({"every_s": "soon"}, "how often"),
    ({"kind": "hourly"}, "how often"),
    ({"mode": "root"}, "read-only"),
    ({"mode": ["x"]}, "read-only"),
    ({"mode": "bypass"}, "confirmation"),
    ({"budget_usd": 0}, "spending limit"),
    ({"budget_usd": 5000}, "spending limit"),
    ({"budget_usd": "lots"}, "spending limit"),
    ({"folder": "/definitely/not/here"}, "does not exist"),
    ({"folder": "/"}, "whole disk"),
])
def test_bad_tasks_are_refused_in_plain_words(tmp_path: Path, folder: Path, fields: dict[str, Any], message: str) -> None:
    s, _, _ = make(tmp_path)
    with pytest.raises(sch.ScheduleError, match=message):
        s.add({**interval(folder), **fields})
    assert s.tasks() == []


def test_bad_schedules(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    for fields, message in [({"kind": "daily", "at": "25:00"}, "time of day"), ({"kind": "weekly", "at": "09:00", "days": []}, "day of the week"),
                            ({"kind": "weekly", "at": "09:00", "days": [9]}, "day of the week"), ({"kind": "once", "once_at": NOON - 5}, "past"),
                            ({"kind": "once"}, "date and time")]:
        with pytest.raises(sch.ScheduleError, match=message):
            s.add({**interval(folder), **fields})


def test_defaults_are_read_only_with_a_budget(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    t = s.add(interval(folder))
    assert t["mode"] == "readonly" and t["budget_usd"] == sch.DEFAULT_BUDGET_USD and t["enabled"] and t["bypass_ack"] is False
    assert sch.MODES["readonly"][0] == "plan" and sch.MODES["bypass"][0] == "bypass"
    assert t["next_at"] == NOON + 600


def test_full_access_needs_an_explicit_acknowledgement_and_is_never_the_default(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    t = s.add({**interval(folder), "mode": "bypass", "bypass_ack": True})
    assert t["mode"] == "bypass"
    with pytest.raises(sch.ScheduleError, match="confirmation"):
        s.add({**interval(folder), "mode": "bypass", "bypass_ack": False})
    ro = s.add(interval(folder))
    with pytest.raises(sch.ScheduleError, match="confirmation"):
        s.update(ro["id"], {"mode": "bypass"})                   # moving a task to full access also needs it
    assert s.get(ro["id"])["mode"] == "readonly"
    back = s.update(t["id"], {"mode": "edits"})
    assert back["mode"] == "edits" and back["bypass_ack"] is False        # leaving full access forgets the acknowledgement


def test_a_protected_folder_is_refused(tmp_path: Path) -> None:
    s, _, _ = make(tmp_path)
    bad = tmp_path / ".ssh"
    bad.mkdir()
    with pytest.raises(sch.ScheduleError):
        s.add({"prompt": "look", "folder": str(bad), "kind": "interval", "every_s": 600})


# ─── storage ─────────────────────────────────────────────────────────────────

def test_tasks_survive_a_restart_and_edits_pause_delete(tmp_path: Path, folder: Path) -> None:
    s, clock, _ = make(tmp_path)
    t = s.add(interval(folder, name="Build"))
    s2 = sch.Scheduler(home=tmp_path / "home", clock=clock, runner=Runner())          # a new process
    assert [x["name"] for x in s2.tasks()] == ["Build"] and s2.tasks()[0]["state"] == "waiting"
    s2.update(t["id"], {"name": "Nightly", "kind": "daily", "at": "03:00"})
    assert s.get(t["id"])["when"] == "Every day at 03:00" and s.get(t["id"])["next_at"] == at(2026, 11, 10, 3, 0)
    paused = s2.set_enabled(t["id"], False)
    assert paused["enabled"] is False and paused["next_at"] is None and s2.get(t["id"])["state"] == "paused"
    assert s2.set_enabled(t["id"], True)["next_at"] == at(2026, 11, 10, 3, 0)
    assert s.delete(t["id"]) and s.tasks() == [] and not s.delete(t["id"])


def test_default_name_is_the_start_of_the_prompt(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    assert s.add(interval(folder))["name"] == "check the build"


def test_a_corrupt_tasks_file_is_set_aside_not_fatal(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    s.add(interval(folder))
    (tmp_path / "home" / "schedule" / "tasks.json").write_text("{not json", encoding="utf-8")
    assert s.tasks() == []
    assert list((tmp_path / "home" / "schedule").glob("tasks.json.bad-*"))
    assert s.add(interval(folder))["id"]                                   # and it can be used again


def test_the_history_ignores_bad_lines(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    t = s.add(interval(folder))
    (tmp_path / "home" / "schedule").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "schedule" / "runs.jsonl").write_text('garbage\n{"run": "a", "task": "%s", "status": "ok", "started": 1}\n[1]\n' % t["id"], encoding="utf-8")
    assert [r["run"] for r in s.history()] == ["a"]


# ─── running with a fake clock ───────────────────────────────────────────────

def test_a_task_runs_when_due_and_not_before(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder))
    clock.advance(599)
    assert s.tick() == [] and runner.calls == []
    clock.advance(1)
    assert s.tick() == [t["id"]]
    settle(s)
    assert len(runner.calls) == 1 and runner.calls[0]["prompt"] == "check the build"
    h = s.history(t["id"])
    assert [(r["status"], r["cost_usd"], r["session_id"], r["reason"]) for r in h] == [("ok", 0.02, "s-" + t["id"], "schedule")]
    after = s.get(t["id"])
    assert after["last_status"] == "ok" and after["next_at"] == clock.t + 600
    assert s.tick() == []                                                 # not again until its next time


def test_run_now_ignores_the_schedule(tmp_path: Path, folder: Path) -> None:
    s, _, runner = make(tmp_path)
    t = s.add(interval(folder))
    s.run_now(t["id"], wait=True)
    assert len(runner.calls) == 1 and s.history(t["id"])[0]["reason"] == "manual"
    assert s.get(t["id"])["next_at"] == NOON + 600                         # the schedule is untouched
    with pytest.raises(sch.ScheduleError, match="No such"):
        s.run_now("nope")


def test_one_run_at_a_time_per_task(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    runner.gate.clear()
    t = s.add(interval(folder))
    clock.advance(600)
    s.tick()
    assert runner.entered.wait(5)
    assert s.get(t["id"])["state"] == "running"
    with pytest.raises(sch.ScheduleError, match="already running"):
        s.run_now(t["id"])
    clock.advance(600)                                                    # due again while still running
    assert s.tick() == []
    assert [r["status"] for r in s.history(t["id"])][:2] == ["skipped", "running"]
    runner.gate.set()
    settle(s)
    assert len(runner.calls) == 1


def test_missed_runs_are_recorded_and_not_replayed(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder, catch_up=False))
    clock.advance(600 * 50 + 30)                                          # Axon was closed for fifty intervals
    assert s.tick() == []
    h = s.history(t["id"])
    assert len(h) == 1 and h[0]["status"] == "missed" and h[0]["missed"] == 50 and "not replayed" in h[0]["error"]
    assert runner.calls == []
    assert s.get(t["id"])["next_at"] == clock.t + 600


def test_a_catch_up_task_runs_once_not_fifty_times(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder, catch_up=True))
    clock.advance(600 * 50 + 30)
    assert s.tick() == [t["id"]]
    settle(s)
    assert len(runner.calls) == 1
    assert [r["status"] for r in s.history(t["id"])] == ["ok", "missed"] and s.history(t["id"])[0]["reason"] == "catch-up"
    assert s.tick() == []


def test_a_daily_task_missed_overnight(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add({"prompt": "report", "folder": str(folder), "kind": "daily", "at": "09:00"})
    clock.t = at(2026, 11, 12, 10, 0)                                     # three mornings passed
    s.tick()
    settle(s)
    h = s.history(t["id"])
    assert h[1]["status"] == "missed" and h[1]["missed"] == 3 and len(runner.calls) == 1
    assert s.get(t["id"])["next_at"] == at(2026, 11, 13, 9, 0)


def test_a_late_start_within_the_grace_is_on_time(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    s.add(interval(folder, catch_up=False))
    clock.advance(600 + sch.MISSED_GRACE_S - 1)
    s.tick()
    settle(s)
    assert len(runner.calls) == 1 and all(r["status"] != "missed" for r in s.history())


def test_a_one_time_task_runs_once_then_is_finished(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add({"prompt": "once", "folder": str(folder), "kind": "once", "once_at": NOON + 120})
    clock.advance(120)
    s.tick()
    settle(s)
    clock.advance(3600)
    assert s.tick() == []
    assert len(runner.calls) == 1
    done = s.get(t["id"])
    assert done["state"] == "paused" and done["next_at"] is None
    with pytest.raises(sch.ScheduleError, match="passed"):
        s.set_enabled(t["id"], True)


def test_a_one_time_task_missed_without_catch_up_is_only_recorded(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add({"prompt": "once", "folder": str(folder), "kind": "once", "once_at": NOON + 120, "catch_up": False})
    clock.advance(86400)
    s.tick()
    assert runner.calls == [] and s.history(t["id"])[0]["status"] == "missed" and s.get(t["id"])["enabled"] is False


def test_paused_tasks_do_not_run(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder))
    s.set_enabled(t["id"], False)
    clock.advance(6000)
    assert s.tick() == [] and runner.calls == []


def test_a_crashing_runner_and_other_outcomes_are_recorded(tmp_path: Path, folder: Path) -> None:
    for status, expect in [("boom", "failed"), ("budget", "budget"), ("failed", "failed")]:
        s, clock, runner = make(tmp_path / status, runner=Runner(status))
        t = s.add(interval(folder))
        s.run_now(t["id"], wait=True)
        r = s.history(t["id"])[0]
        assert r["status"] == expect and s.get(t["id"])["last_status"] == expect
        assert (not r["error"]) or "model is gone" in r["error"]
        assert not s.running                                              # the lock and the slot are free again
        s.run_now(t["id"], wait=True)                                     # and it can run again


def test_listeners_hear_about_finished_runs(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    got: list[dict[str, Any]] = []
    s.add_listener(got.append)
    t = s.add(interval(folder, name="Build"))
    s.run_now(t["id"], wait=True)
    time.sleep(0.05)
    assert got and got[0]["status"] == "ok" and got[0]["name"] == "Build"


def test_the_background_loop_starts_due_tasks(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    s.add(interval(folder))
    clock.advance(600)
    s.start(every=0.02)
    try:
        deadline = time.time() + 5
        while not runner.calls and time.time() < deadline:
            time.sleep(0.02)
    finally:
        s.stop()
    assert len(runner.calls) == 1 and not s.active


def test_at_most_a_few_tasks_run_together(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    runner.gate.clear()
    for i in range(sch.MAX_PARALLEL + 2):
        s.add(interval(folder, name=f"t{i}"))
    clock.advance(600)
    assert len(s.tick()) == sch.MAX_PARALLEL
    runner.gate.set()
    settle(s)
    assert len(s.tick()) == 2                                             # the rest start on the next look


# ─── two Axon processes ──────────────────────────────────────────────────────

def test_two_schedulers_on_one_folder_run_a_due_task_once(tmp_path: Path, folder: Path) -> None:
    clock = Clock(NOON)
    r1, r2 = Runner(), Runner()
    a = sch.Scheduler(home=tmp_path / "home", clock=clock, runner=r1)
    b = sch.Scheduler(home=tmp_path / "home", clock=clock, runner=r2)
    t = a.add(interval(folder))
    clock.advance(600)
    go = threading.Barrier(2)
    out: list[Any] = []

    def work(s: sch.Scheduler) -> None:
        go.wait()
        out.append(s.tick())
    ths = [threading.Thread(target=work, args=(x,)) for x in (a, b)]
    [th.start() for th in ths]
    [th.join(10) for th in ths]
    settle(a), settle(b)
    assert len(r1.calls) + len(r2.calls) == 1 and sorted(map(len, out)) == [0, 1]
    assert len(a.history(t["id"])) == 1


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _hold(s: sch.Scheduler, task_id: str, pid: int) -> None:
    d = s._lockdir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    import socket
    (d / "writer.lease").write_text(json.dumps({"pid": pid, "host": socket.gethostname(), "owner": "scheduler", "started": 1.0}), encoding="utf-8")


def test_a_task_held_by_another_live_process_is_not_started(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder))
    other = _sleeper()
    try:
        _hold(s, t["id"], other.pid)
        assert s.get(t["id"])["running"] is True
        clock.advance(600)
        assert s.tick() == []
        settle(s)
        assert runner.calls == []
        with pytest.raises(sch.ScheduleError, match="already running"):
            s.run_now(t["id"])
    finally:
        other.kill()
        other.wait()


def test_a_lock_left_by_a_dead_process_is_ignored(tmp_path: Path, folder: Path) -> None:
    s, clock, runner = make(tmp_path)
    t = s.add(interval(folder))
    dead = _sleeper()
    dead.kill()
    dead.wait()
    _hold(s, t["id"], dead.pid)
    assert s.get(t["id"])["running"] is False
    clock.advance(600)
    assert s.tick() == [t["id"]]
    settle(s)
    assert len(runner.calls) == 1


def test_a_run_whose_process_vanished_shows_as_interrupted(tmp_path: Path, folder: Path) -> None:
    s, _, _ = make(tmp_path)
    t = s.add(interval(folder))
    s._append({"run": "r1", "task": t["id"], "name": "x", "status": "running", "started": 5.0})
    r = s.history(t["id"])[0]
    assert r["status"] == "interrupted" and "closed" in r["error"]


# ─── a real run through the SDK and the fake provider ────────────────────────

def test_a_task_runs_as_a_normal_session_through_the_sdk(tmp_path: Path, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fake_llm import FakeLLMServer
    home = tmp_path / "axon-home"
    monkeypatch.setenv("AXON_HOME", str(home))
    srv = FakeLLMServer(model_ids=("gpt-4o", "fake-model")).start()
    try:
        for k, v in dict(AXON_BASE_URL=srv.url, AXON_API_KEY="sk-e2e-test-key", AXON_MODEL="gpt-4o", AXON_OFFLINE="1", AXON_NO_UPDATE_CHECK="1",
                         AXON_TITLE_LLM="0", AXON_TIPS_LLM="0", AXON_PROMPT_SUGGESTIONS="0", AXON_NO_NOTIFY="1").items():
            monkeypatch.setenv(k, v)
        srv.enqueue([{"text": "The build looks fine."}])
        s = sch.Scheduler(home=home)                                       # the real runner
        t = s.add(interval(folder, name="Build"))
        s.run_now(t["id"], wait=True)
        r = s.history(t["id"])[0]
        assert r["status"] == "ok", r
        assert r["session_id"]
        from axon import storage
        assert storage.sessions_dir(folder).exists() and any(r["session_id"] in p.name for p in storage.sessions_dir(folder).iterdir())
        sent = json.dumps(srv.completion_requests()[0].body)
        assert "scheduled run" in sent and "check the build" in sent            # the unattended note and the prompt
    finally:
        srv.stop()


# ─── the app routes ──────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from test_dashboard import Client, env as _env  # noqa: F401
    from axon.features import dashboard as dash
    home = tmp_path / "home"
    monkeypatch.setenv("AXON_HOME", str(home))
    ws = tmp_path / "proj"
    ws.mkdir()
    runner = Runner()
    monkeypatch.setattr(sch, "_SCHEDULER", sch.Scheduler(home=home, runner=runner))
    srv = dash.make_server(0, ws)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    cl = Client(srv)
    cl.login()
    yield SimpleNamespace(c=cl, ws=ws, runner=runner, s=sch._SCHEDULER, srv=srv)
    srv.shutdown_runs()
    srv.shutdown()
    srv.server_close()


def test_routes_need_the_login_cookie_and_a_same_origin_post(app) -> None:
    from test_dashboard import Client
    anon = Client(app.srv)
    assert anon.get("/api/tasks")[0] in (401, 403)
    assert anon.post("/api/tasks/add", {"prompt": "x"}, cookie=False)[0] in (401, 403)
    st, _, _ = app.c.post("/api/tasks/add", {"prompt": "x", "kind": "interval", "every_s": 600}, origin="http://evil.example")
    assert st == 403
    st, _, _ = app.c.post("/api/tasks/add", {"prompt": "x", "kind": "interval", "every_s": 600}, origin=None)
    assert st == 403


def test_add_list_edit_pause_run_history_delete_over_http(app) -> None:
    c = app.c
    assert c.json("/api/tasks")["tasks"] == [] and c.json("/api/tasks")["default_mode"] == "readonly"
    st, _, body = c.post("/api/tasks/add", {"prompt": "summarise the repo", "kind": "daily", "at": "08:15", "name": "Morning"})
    assert st == 200, body
    t = json.loads(body)
    assert t["folder"] == str(app.ws.resolve()) and t["mode"] == "readonly" and t["when"] == "Every day at 08:15" and t["budget_usd"] == 1.0
    tid = t["id"]
    assert c.post("/api/tasks/update", {"id": tid, "name": "AM", "budget_usd": 0.25})[0] == 200
    assert c.json("/api/tasks")["tasks"][0]["name"] == "AM"
    assert json.loads(c.post("/api/tasks/pause", {"id": tid, "paused": True})[2])["state"] == "paused"
    assert json.loads(c.post("/api/tasks/pause", {"id": tid, "paused": False})[2])["state"] == "waiting"
    st, _, body = c.post("/api/tasks/run", {"id": tid})
    assert st == 200
    app.s.threads[tid].join(5) if tid in app.s.threads else None
    deadline = time.time() + 5
    while time.time() < deadline and c.json(f"/api/tasks/history?id={tid}")["runs"][0]["status"] == "running":
        time.sleep(0.05)
    runs = c.json(f"/api/tasks/history?id={tid}")["runs"]
    assert runs[0]["status"] == "ok" and runs[0]["session_id"] == "s-" + tid and runs[0]["cost_usd"] == 0.02
    recent = c.json("/api/tasks/recent?since=0")["runs"]
    assert recent and recent[0]["name"] == "AM" and recent[0]["status"] == "ok" and "session_id" not in recent[0]
    assert c.post("/api/tasks/delete", {"id": tid})[0] == 400                    # needs confirm
    assert json.loads(c.post("/api/tasks/delete", {"id": tid, "confirm": True})[2])["ok"] is True
    assert c.json("/api/tasks")["tasks"] == []


@pytest.mark.parametrize("body,expect", [
    ({"prompt": ""}, 400), ({"prompt": "x", "kind": "interval", "every_s": 5}, 400), ({"prompt": "x", "kind": "daily", "at": "nine"}, 400),
    ({"prompt": "x", "kind": "interval", "every_s": 600, "mode": "bypass"}, 400),
    ({"prompt": "x", "kind": "interval", "every_s": 600, "folder": "/nope/nope"}, 400),
    ({"prompt": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 go", "kind": "interval", "every_s": 600}, 400),
    ({"prompt": ["x"], "kind": "interval", "every_s": 600}, 400),
])
def test_bad_input_over_http_is_a_400_not_a_crash(app, body, expect) -> None:
    st, _, raw = app.c.post("/api/tasks/add", body)
    assert st == expect and "error" in json.loads(raw)
    assert app.c.json("/api/tasks")["tasks"] == []


def test_full_access_over_http_needs_the_acknowledgement(app) -> None:
    f = {"prompt": "x", "kind": "interval", "every_s": 600, "mode": "bypass"}
    assert app.c.post("/api/tasks/add", f)[0] == 400
    st, _, body = app.c.post("/api/tasks/add", {**f, "bypass_ack": True})
    assert st == 200 and json.loads(body)["mode"] == "bypass"


def test_unknown_ids_and_routes(app) -> None:
    assert app.c.post("/api/tasks/run", {"id": "zzzz"})[0] == 400
    assert app.c.post("/api/tasks/update", {"id": "zzzz", "name": "x"})[0] == 400
    assert app.c.post("/api/tasks/run", {})[0] == 400
    assert app.c.get("/api/tasks/nonsense")[0] == 404


def test_a_task_already_running_cannot_be_started_again_from_the_app(app) -> None:
    app.runner.gate.clear()
    tid = json.loads(app.c.post("/api/tasks/add", {"prompt": "x", "kind": "interval", "every_s": 600})[2])["id"]
    assert app.c.post("/api/tasks/run", {"id": tid})[0] == 200
    assert app.runner.entered.wait(5)
    assert json.loads(app.c.get("/api/tasks")[2])["tasks"][0]["running"] is True
    st, _, body = app.c.post("/api/tasks/run", {"id": tid})
    assert st == 400 and "already running" in json.loads(body)["error"]
    assert json.loads(app.c.post("/api/tasks/stop", {"id": tid})[2])["ok"] is True
    app.runner.gate.set()


def test_recent_notice_names_hide_secrets(app) -> None:
    assert sch.notice_name({"name": "Build the thing"}) == "Build the thing"
    assert sch.notice_name("password=hunter2hunter2") == "A task"
    assert sch.notice_name("  line one\nline two  ") == "line one line two"


# ─── the page: tasks panel, settings, notifications ──────────────────────────

def test_the_page_script_has_the_tasks_panel_and_the_notification_card(app) -> None:
    st, _, body = app.c.get("/static/app.js")
    js = body.decode()
    for needle in ("function tasksMount", "function axnSettingsCard", "tasks: { title: 'Tasks'", "body.append(axnSettingsCard())", "Open Tasks",
                   "Notification settings"):
        assert needle in js, needle
    # the English catalog (i18n.js, first in the bundle) still lists an old label; what matters is that the page code has no second name for the panel
    from axon.features.dashboard_web import i18n_asset
    assert "Background tasks" not in js.replace(i18n_asset("i18n.js")[0], "", 1)
    assert "Tasks run only while Axon is open" in js or "run only while Axon is open" in js
    # nothing of a question or an error message is put into a notification
    assert "ev.question" not in js[js.index("function axOnEvent"):js.index("function axOnEvent") + 900]
    assert "ev.message" not in js[js.index("function axOnEvent"):js.index("function axOnEvent") + 900]
    # the browser's permission prompt is only reached from the switch in Settings
    assert js.count("Notification.requestPermission") == 1
    assert js.index("Notification.requestPermission") > js.index("function axnSettingsCard")
    st, _, css = app.c.get("/static/app.css")
    assert b".tk{" in css


def test_the_tasks_script_never_uses_inner_html() -> None:
    assert "innerHTML" not in dash_js_tasks.JS_TASKS and "html:" not in dash_js_tasks.JS_TASKS


# ─── terminal notifications ──────────────────────────────────────────────────

def test_notice_text_is_one_clean_line_without_secrets_or_escape_codes() -> None:
    assert notify.safe_notice_text("a\x1b[31mb\x07\nc") == "a [31mb c"
    assert "\x1b" not in notify.safe_notice_text("\x1b]0;evil\x07 x") and "\x07" not in notify.safe_notice_text("\x1b]0;evil\x07 x")
    assert notify.safe_notice_text("here is sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 ok") == "Something needs your attention"
    assert len(notify.safe_notice_text("x" * 500)) == 120


def test_terminal_signal_forms() -> None:
    assert notify.terminal_signal("off", "Axon", "done") == ""
    assert notify.terminal_signal("bell", "Axon", "done") == "\a"
    assert notify.terminal_signal("osc9", "Axon", "done") == "\x1b]9;Axon: done\x07"
    assert notify.terminal_signal("osc777", "Axon", "a;b") == "\x1b]777;notify;Axon;a,b\x07"
    evil = notify.terminal_signal("osc9", "Axon", "x\x1b]52;c;ZGF0YQ==\x07y")
    assert evil.count("\x1b") == 1 and evil.count("\x07") == 1               # only our own opener and terminator


class FakeTty:
    def __init__(self, tty: bool) -> None:
        self.tty, self.out = tty, ""

    def isatty(self) -> bool:
        return self.tty

    def write(self, s: str) -> int:
        self.out += s
        return len(s)

    def flush(self) -> None:
        pass


def test_the_terminal_signal_goes_only_to_a_terminal_and_only_when_on() -> None:
    t, pipe = FakeTty(True), FakeTty(False)
    assert notify.emit_terminal_notice("Axon", "done", "bell", t) and t.out == "\a"
    assert not notify.emit_terminal_notice("Axon", "done", "bell", pipe) and pipe.out == ""
    assert not notify.emit_terminal_notice("Axon", "done", "off", t)


def test_it_is_off_by_default_and_notify_event_sends_it_when_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.config import Settings
    assert Settings.model_fields["terminal_notify"].default == "off"
    monkeypatch.setattr(notify, "_blocked_by_env", lambda: False)
    monkeypatch.setattr(notify.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(notify, "notify_if_unfocused", lambda title, msg: False)
    monkeypatch.setattr(notify, "_last_sent", {})
    tty = FakeTty(True)
    monkeypatch.setattr(notify.sys, "stdout", tty)
    notify.configure(SimpleNamespace(notifications="important", notify_after_seconds=30.0, terminal_notify="off"))
    assert notify.notify_event("permission_prompt", "Axon needs your permission to use Bash") is False and tty.out == ""
    notify._last_sent.clear()
    notify.configure(SimpleNamespace(notifications="important", notify_after_seconds=30.0, terminal_notify="osc9"))
    assert notify.notify_event("permission_prompt", "Axon needs your permission to use Bash") is True
    assert tty.out == "\x1b]9;Axon: Axon needs your permission to use Bash\x07"
    notify._last_sent.clear()
    tty.out = ""
    assert not notify.notify_event("turn_complete", "finished", elapsed=3.0) and tty.out == ""      # short turns stay quiet
    assert notify.notify_event("turn_complete", "finished", elapsed=45.0) and tty.out
    notify.configure(SimpleNamespace(notifications="off", notify_after_seconds=30.0, terminal_notify="osc9"))
    notify._last_sent.clear()
    tty.out = ""
    assert not notify.notify_event("permission_prompt", "x") and tty.out == ""
    notify.configure(SimpleNamespace())                                          # leave the module as it was


def test_questions_and_failures_notify_without_their_text() -> None:
    import inspect
    from axon import cli
    from axon.tools import interactive
    src = inspect.getsource(interactive.AskUserQuestionTool)
    assert "Axon is asking you a question" in src and "first[:150]" not in src
    assert "final_text" not in inspect.getsource(cli._run_agent_turn).split("notify_event(\"turn_failed\"")[1].split("else:")[0]


def test_the_notify_command_sets_the_terminal_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "h"))
    from axon.commands.system_cmds import handle_notify
    from axon.config import Settings
    s = Settings.load({"workspace": tmp_path}, require_api_key=False)
    agent = SimpleNamespace(settings=s, workspace=tmp_path)
    handle_notify(agent, "terminal sideways")
    assert "Usage" in capsys.readouterr().out and agent.settings.terminal_notify == "off"
    monkeypatch.setattr(notify.sys, "stdout", sys.stdout)
    handle_notify(agent, "terminal bell")
    assert agent.settings.terminal_notify == "bell" and notify._policy["terminal"] == "bell"
    saved = json.loads((tmp_path / "h" / "settings.json").read_text(encoding="utf-8"))
    assert saved["terminalNotify"] == "bell"
    handle_notify(agent, "terminal off")
    assert notify._policy["terminal"] == "off"


# ─── /schedule in the terminal ───────────────────────────────────────────────

@pytest.fixture
def term(tmp_path: Path, folder: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "h"
    monkeypatch.setenv("AXON_HOME", str(home))
    runner = Runner()
    monkeypatch.setattr(sch, "_SCHEDULER", sch.Scheduler(home=home, runner=runner))
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=folder), workspace=folder)
    return SimpleNamespace(agent=agent, runner=runner, s=sch._SCHEDULER, folder=folder)


def plain_out(capsys: pytest.CaptureFixture[str]) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", capsys.readouterr().out)


def test_schedule_is_a_command_with_a_menu_and_is_documented() -> None:
    from axon.commands import usage_menu
    from axon.commands.registry import resolve_builtin
    assert resolve_builtin("schedule") is not None and usage_menu.menu_kind("schedule") == "own"
    assert resolve_builtin("cron") is resolve_builtin("schedule")
    assert "only while Axon is open" in resolve_builtin("schedule").usage


@pytest.mark.parametrize("line,check", [
    ("every 30m run the tests", lambda f: f["kind"] == "interval" and f["every_s"] == 1800 and f["prompt"] == "run the tests"),
    ("daily 09:00 write the summary", lambda f: f["kind"] == "daily" and f["at"] == "09:00" and f["prompt"] == "write the summary"),
    ("weekly mon,wed 08:30 check mail", lambda f: f["days"] == [0, 2] and f["at"] == "08:30"),
    ("weekly weekdays 08:30 x", lambda f: f["days"] == [0, 1, 2, 3, 4]),
    ("once in 2h look", lambda f: f["once_at"] == NOON + 7200),
    ("once 2026-11-20 14:30 look", lambda f: f["once_at"] == at(2026, 11, 20, 14, 30)),
    ("once 13:00 look", lambda f: f["once_at"] == at(2026, 11, 9, 13, 0)),
    ("once 11:00 look", lambda f: f["once_at"] == at(2026, 11, 10, 11, 0)),
    ("every 1h --budget 0.5 --mode edits do it", lambda f: f["budget_usd"] == 0.5 and f["mode"] == "edits" and f["prompt"] == "do it"),
])
def test_typed_add_forms(term, line, check) -> None:
    from axon.commands.schedule_cmds import parse_add
    f = parse_add(term.agent, line, NOON)
    assert check(f), f
    assert f["mode"] in ("readonly", "edits") and f["folder"] == str(term.folder)


@pytest.mark.parametrize("line", ["", "sometimes do it", "every", "daily", "weekly mon", "weekly funday 09:00 x", "once soon x", "every 1h --budget lots x"])
def test_typed_add_mistakes_are_explained(term, line) -> None:
    from axon.commands.schedule_cmds import parse_add
    with pytest.raises(sch.ScheduleError):
        parse_add(term.agent, line, NOON)


def test_schedule_command_text_flow(term, capsys) -> None:
    from axon.commands.schedule_cmds import handle_schedule
    handle_schedule(term.agent, "add every 1h run the tests")
    out = plain_out(capsys)
    assert "Added task" in out and "only while Axon is open" in out
    tid = term.s.tasks()[0]["id"]
    handle_schedule(term.agent, "list")
    out = plain_out(capsys)
    assert tid in out and "Every hour" in out and "Read-only" in out and "$1.00" in out
    handle_schedule(term.agent, f"run {tid}")
    for th in list(term.s.threads.values()):
        th.join(5)
    plain_out(capsys)
    handle_schedule(term.agent, f"history {tid}")
    out = plain_out(capsys)
    assert "succeeded" in out and "s-" + tid in out
    handle_schedule(term.agent, f"pause {tid}")
    assert term.s.get(tid)["enabled"] is False
    handle_schedule(term.agent, f"resume {tid}")
    assert term.s.get(tid)["enabled"] is True
    handle_schedule(term.agent, f"delete {tid}")
    assert term.s.tasks() == []
    r = handle_schedule(term.agent, "add every 1h --mode bypass x")
    assert "confirmation" in plain_out(capsys) and r.success is False
    handle_schedule(term.agent, "add every 1h --mode bypass --i-understand x")
    assert term.s.tasks()[0]["mode"] == "bypass"
    handle_schedule(term.agent, "run nothing")
    assert "No such task" in plain_out(capsys)


def test_schedule_menu(term, picks, monkeypatch) -> None:
    from axon.commands import schedule_cmds
    from axon.ui import menus
    answers = iter(["write a haiku about the repo", "30m", "0.25", str(term.folder)])      # prompt, interval, budget, folder
    monkeypatch.setattr(menus, "ask_text", lambda label, default="", secret=False: next(answers))
    shown = picks(["New task…", "Every so often", "Read-only", None])
    schedule_cmds.handle_schedule(term.agent, "")
    t = term.s.tasks()
    assert len(t) == 1 and t[0]["every_s"] == 1800 and t[0]["budget_usd"] == 0.25 and t[0]["mode"] == "readonly"
    assert shown[0][0] == "Scheduled tasks" and "New task…" in shown[0][1]
    modes = next(labels for title, labels in shown if title == "What may the task do?")
    assert modes[0] == "Read-only" and modes[-1] == "Full access"                             # full access is last, never the default


def test_schedule_menu_task_actions(term, picks) -> None:
    from axon.commands import schedule_cmds
    t = term.s.add({"prompt": "x", "folder": str(term.folder), "kind": "interval", "every_s": 600, "name": "Build"})
    shown = picks(["Build", "Run it now", "Pause", "Delete…", "Yes", None, None])
    schedule_cmds.handle_schedule(term.agent, "")
    for th in list(term.s.threads.values()):
        th.join(5)
    assert term.s.tasks() == [] and len(term.runner.calls) == 1
    assert any(title == "Delete the task 'Build'? Its sessions stay." for title, _ in shown)
    assert t["id"]


def test_the_scheduler_starts_with_the_terminal_and_tells_the_person(term, monkeypatch) -> None:
    from axon.commands import schedule_cmds
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(sch, "ensure_started", lambda: term.s)
    monkeypatch.setattr(notify, "notify_event", lambda kind, msg, **kw: seen.append((kind, msg)) or True)
    schedule_cmds.start_in_terminal(term.agent)
    t = term.s.add({"prompt": "SECRET-LOOKING body text", "folder": str(term.folder), "kind": "interval", "every_s": 600, "name": "Build"})
    term.s.run_now(t["id"], wait=True)
    time.sleep(0.1)
    assert seen == [("background_task", "Build: succeeded")]               # the name and the outcome, never the prompt or the answer


def test_nothing_starts_in_tests_or_when_switched_off(monkeypatch) -> None:
    assert sch.ensure_started() is None                                      # PYTEST_CURRENT_TEST is set
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("AXON_NO_SCHEDULER", "1")
    assert sch.ensure_started() is None


def test_the_unattended_note_forbids_questions() -> None:
    assert "nobody" in sch.UNATTENDED_NOTE and "Do not ask questions" in sch.UNATTENDED_NOTE
    assert os.name  # the module imports on every platform: no POSIX-only module level imports
    import ast
    tree = ast.parse(Path(sch.__file__).read_text(encoding="utf-8"))
    top = {n.names[0].name for n in tree.body if isinstance(n, ast.Import)} | {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert not top & {"fcntl", "pwd", "grp", "termios", "resource"}
    assert 'encoding="utf-8"' in Path(sch.__file__).read_text(encoding="utf-8")
