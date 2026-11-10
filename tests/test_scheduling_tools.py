"""Self-paced /loop, the Cron* and ScheduleWakeup tools, and what a scheduled prompt may and may not do."""
from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.agent.subagent import _EXCLUDED_FOR_SUBAGENTS
from axon.commands import workspace_cmds as wc
from axon.commands.registry import dispatch_command
from axon.errors import ToolError
from axon.permissions.engine import PermissionEngine
from axon.tools import create_default_registry
from axon.tools.base import ToolContext
from axon.tools.cron import CronCreateTool, CronDeleteTool, CronError, CronListTool, CronSpec, ScheduleWakeupTool
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr("axon.storage.axon_home", lambda: tmp_path / "home" / ".axon")
    ws = tmp_path / "ws"
    ws.mkdir()
    a = make_agent(FakeProvider([]), ws)
    a.fires_scheduled_tasks = True
    yield a
    wc.stop_loops(a)


def _ctx(agent: Any) -> ToolContext:
    return ToolContext(workspace=agent.settings.workspace, file_state=agent.file_state, todos=agent.todos,
                       settings=agent.settings, agent=agent)


def _at(*args: int) -> float:
    return datetime(*args).timestamp()


# ─── the cron parser ───────────────────────────────────────────────────────

@pytest.mark.parametrize("expr, now, expected", [
    ("*/5 * * * *", (2026, 11, 9, 10, 2), (2026, 11, 9, 10, 5)),
    ("0 * * * *", (2026, 11, 9, 10, 0), (2026, 11, 9, 11, 0)),      # strictly after now
    ("7 * * * *", (2026, 11, 9, 10, 8), (2026, 11, 9, 11, 7)),
    ("0 9 * * *", (2026, 11, 9, 10, 0), (2026, 11, 10, 9, 0)),
    ("0 9 * * 1-5", (2026, 11, 13, 9, 0), (2026, 11, 16, 9, 0)),    # Friday -> Monday
    ("30 14 15 3 *", (2026, 11, 9, 0, 0), (2027, 3, 15, 14, 30)),
    ("0 0 * * 7", (2026, 11, 9, 0, 0), (2026, 11, 15, 0, 0)),       # 7 is Sunday
    ("0 0 * * 0", (2026, 11, 9, 0, 0), (2026, 11, 15, 0, 0)),       # and so is 0
    ("1,15,30 8 * * *", (2026, 11, 9, 8, 1), (2026, 11, 9, 8, 15)),
    ("10-20/5 8 * * *", (2026, 11, 9, 8, 11), (2026, 11, 9, 8, 15)),
    ("0 0 13 * 5", (2026, 11, 9, 0, 0), (2026, 11, 13, 0, 0)),      # both restricted: either matches (a Friday and the 13th)
    ("0 0 20 * 1", (2026, 11, 10, 0, 0), (2026, 11, 16, 0, 0)),     # the Monday comes before the 20th
    ("0 0 29 2 *", (2026, 11, 9, 0, 0), (2028, 2, 29, 0, 0)),
])
def test_cron_next_run(expr: str, now: tuple, expected: tuple) -> None:
    assert CronSpec(expr).next_after(_at(*now)) == _at(*expected)


@pytest.mark.parametrize("expr", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8",
                                  "*/0 * * * *", "5-1 * * * *", "* * * JAN *", "* * * * MON", "0 0 L * *", "? * * * *", "0 0 31 2 *"])
def test_cron_rejects_what_it_does_not_support(expr: str) -> None:
    with pytest.raises(CronError):
        CronSpec(expr)


# ─── CronCreate, CronList, CronDelete ──────────────────────────────────────

def test_cron_create_list_delete(agent: Any) -> None:
    ctx = _ctx(agent)
    out = CronCreateTool().run({"cron": "*/5 * * * *", "prompt": "check the deploy"}, ctx)
    job = wc.active_jobs(agent)[0]
    assert len(job.uid) == 8 and job.uid in out and "next run" in out and job.plain and job.recurring
    assert len(agent.message_queue) == 0            # nothing fires until the schedule says so
    listing = CronListTool().run({}, ctx)
    assert job.uid in listing and "*/5 * * * *" in listing and "check the deploy" in listing
    assert job.uid in CronDeleteTool().run({"id": job.uid}, ctx)
    assert CronListTool().run({}, ctx) == "No scheduled tasks."
    with pytest.raises(ToolError):
        CronDeleteTool().run({"id": job.uid}, ctx)


def test_cron_bad_input_is_an_error_the_model_can_read(agent: Any) -> None:
    ctx = _ctx(agent)
    for args in ({"cron": "nope", "prompt": "x"}, {"cron": "* * * * *", "prompt": "  "},
                 {"cron": "* * * * *", "prompt": "x", "recurring": "yes"}):
        with pytest.raises(ToolError):
            CronCreateTool().run(args, ctx)
    assert wc.active_jobs(agent) == []


def test_at_most_fifty_per_session_counting_loops(agent: Any) -> None:
    wc.start_interval_loop(agent, "typed", 60, start_thread=False)
    for i in range(wc.MAX_SCHEDULED - 1):
        wc.start_cron_job(agent, CronSpec("0 9 * * *"), f"p{i}", start_thread=False)
    with pytest.raises(ToolError, match="50"):
        CronCreateTool().run({"cron": "0 9 * * *", "prompt": "one too many"}, _ctx(agent))
    dispatch_command("/loop 5m one more loop", agent)
    assert len(wc.active_jobs(agent)) == wc.MAX_SCHEDULED
    wc.stop_loops(agent, wc.active_jobs(agent)[0].uid)
    CronCreateTool().run({"cron": "0 9 * * *", "prompt": "room again"}, _ctx(agent))


def test_recurring_fires_on_schedule_never_stacks_and_expires_after_seven_days(agent: Any) -> None:
    start = _at(2026, 11, 9, 10, 0)
    clock = [start]
    job = wc.start_cron_job(agent, CronSpec("*/5 * * * *"), "poll", clock=lambda: clock[0], start_thread=False)
    assert job.expires_at == start + 7 * 86400
    assert wc._tick(agent, job, start + 60) is False
    wc.LOOP_WAKE.clear()
    assert wc._tick(agent, job, start + 300) is True and wc.LOOP_WAKE.is_set()
    assert wc._tick(agent, job, start + 3000) is False          # the last run still waits: never stacked
    agent.message_queue.pop()
    assert wc._tick(agent, job, start + 3000) is True           # one run for all it missed ...
    agent.message_queue.pop()
    assert job.next_at == start + 3300                          # ... and the next is on the schedule from now
    late = start + 7 * 86400 + 60
    assert wc._tick(agent, job, late) is True and not job.active      # one final run, then gone
    assert wc.active_jobs(agent) == []


def test_one_shot_fires_once_and_deletes_itself(agent: Any) -> None:
    start = _at(2026, 11, 9, 10, 0)
    job = wc.start_cron_job(agent, CronSpec("31 10 * * *"), "remind me", recurring=False, clock=lambda: start, start_thread=False)
    assert job.expires_at is None and job.next_at == _at(2026, 11, 9, 10, 31)
    assert wc._tick(agent, job, job.next_at) is True and not job.active and wc.active_jobs(agent) == []


def test_jobs_fire_from_their_own_thread(agent: Any) -> None:
    job = wc.start_cron_job(agent, CronSpec("* * * * *"), "tick")
    job.next_at = 0.0                                           # due now
    assert job.thread is not None and job.thread.daemon
    job.thread.join(0.0)
    import time
    deadline = time.time() + 3
    while not len(agent.message_queue) and time.time() < deadline:
        time.sleep(0.05)
    assert "tick" in agent.message_queue.peek().text


def test_cron_create_refuses_where_nothing_would_ever_fire(agent: Any) -> None:
    agent.fires_scheduled_tasks = False                         # axon -p, the SDK, the app without a terminal
    with pytest.raises(ToolError, match="terminal"):
        CronCreateTool().run({"cron": "* * * * *", "prompt": "x"}, _ctx(agent))


def test_scheduling_stops_with_the_session(agent: Any) -> None:
    CronCreateTool().run({"cron": "0 9 * * *", "prompt": "x"}, _ctx(agent))
    agent.reset_for_new_session()
    assert wc.active_jobs(agent) == []


# ─── permissions ───────────────────────────────────────────────────────────

def _decide(agent: Any, mode: str, tool_name: str, args: dict[str, Any]) -> str:
    engine = PermissionEngine(agent.settings.model_copy(update={"mode": mode}))
    return engine.check(create_default_registry().get(tool_name), args).outcome


@pytest.mark.parametrize("name", ["CronCreate", "CronDelete", "CronList", "ScheduleWakeup"])
def test_default_mode_allows_like_task(agent: Any, name: str) -> None:
    assert _decide(agent, "default", name, {"cron": "* * * * *", "prompt": "x", "id": "x"}) == "allow"


def test_create_is_classified_like_task() -> None:
    reg = create_default_registry()
    task, create = reg.get("Task"), reg.get("CronCreate")
    assert (create.readonly, create.default_permission) == (task.readonly, task.default_permission) == (False, "allow")


def test_plan_mode_refuses_scheduling_but_may_list_and_pace(agent: Any) -> None:
    assert _decide(agent, "plan", "CronCreate", {"cron": "* * * * *", "prompt": "x"}) == "deny"
    assert _decide(agent, "plan", "CronDelete", {"id": "x"}) == "deny"
    assert _decide(agent, "plan", "CronList", {}) == "allow"
    assert _decide(agent, "plan", "ScheduleWakeup", {"delay_seconds": 60}) == "allow"


def test_a_deny_rule_blocks_cron_create(agent: Any) -> None:
    (agent.settings.workspace / ".axon").mkdir(exist_ok=True)
    (agent.settings.workspace / ".axon" / "settings.json").write_text('{"permissions": {"deny": ["CronCreate"]}}', encoding="utf-8")
    assert _decide(agent, "default", "CronCreate", {"cron": "* * * * *", "prompt": "x"}) == "deny"


def test_subagents_cannot_schedule(agent: Any) -> None:
    assert {"CronCreate", "CronList", "CronDelete", "ScheduleWakeup"} <= _EXCLUDED_FOR_SUBAGENTS
    agent.is_subagent = True
    for tool, args in ((CronCreateTool(), {"cron": "* * * * *", "prompt": "x"}), (CronListTool(), {}),
                       (CronDeleteTool(), {"id": "x"}), (ScheduleWakeupTool(), {"delay_seconds": 60})):
        with pytest.raises(ToolError, match="main conversation"):
            tool.run(args, _ctx(agent))
    agent.is_subagent = False


# ─── a scheduled prompt is plain text ──────────────────────────────────────

@pytest.mark.parametrize("prompt", ["!touch pwned", "/permissions", "#remember this", "@secret.txt tell me"])
def test_a_model_written_prompt_is_never_a_typed_line(agent: Any, tmp_path: Path, prompt: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon import cli
    (agent.settings.workspace / "secret.txt").write_text("HUSH", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(cli, "_run_agent_turn", lambda a, r, text, label: seen.append(text))
    for forbidden in ("_shell_into_context", "_quick_memory"):
        monkeypatch.setattr(cli, forbidden, lambda *a, **k: pytest.fail(f"{forbidden} ran"))
    monkeypatch.setattr("axon.commands.builtin.dispatch_command", lambda *a, **k: pytest.fail("a command ran"))
    start = _at(2026, 11, 9, 10, 0)
    job = wc.start_cron_job(agent, CronSpec("* * * * *"), prompt, clock=lambda: start, start_thread=False)
    wc._tick(agent, job, job.next_at)
    item = agent.message_queue.peek()
    assert item.plain and item.text.startswith(f"[Scheduled task {job.uid}") and item.text.endswith(prompt)
    assert cli.drain_queue(agent, None) is True
    assert seen == [item.text] and "HUSH" not in seen[0]
    assert not (agent.settings.workspace / "pwned").exists()


def test_a_typed_loop_prompt_still_runs_as_typed(agent: Any) -> None:
    job = wc.start_interval_loop(agent, "/review", 60, start_thread=False)
    item = agent.message_queue.peek()
    assert not item.plain and item.text == "/review" and not job.plain


# ─── self-paced /loop ──────────────────────────────────────────────────────

def _run_next(agent: Any, during: Any = None, clock: Any = None) -> str:
    item = agent.message_queue.pop()
    wc.before_queued_run(agent, item.id)
    if during is not None:
        during()
    wc.after_queued_run(agent, item.id, **({"clock": clock} if clock else {}))
    return item.text


def test_prompt_only_is_self_paced_and_the_model_picks_the_delay(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch_command("/loop check the deploy", agent)
    job = wc.self_paced_job(agent)
    assert job is not None and job.next_at == math.inf and agent.message_queue.peek().text == "check the deploy"
    assert wc.pending_wakeup(agent) is None                     # queued, not waiting
    tool, ctx = ScheduleWakeupTool(), _ctx(agent)

    def choose() -> None:
        assert agent.take_pending_context().count("ScheduleWakeup") >= 1       # the model is told what to do
        assert "2m" in tool.run({"delay_seconds": 120, "reason": "build still running"}, ctx)
    _run_next(agent, choose)
    assert "Next loop run in 2m: build still running" in capsys.readouterr().out
    assert wc.pending_wakeup(agent) is job and job.active and 118 < job.next_at - __import__("time").time() <= 120
    assert wc._tick(agent, job, job.next_at - 1) is False
    assert wc._tick(agent, job, job.next_at) is True and agent.message_queue.peek().text == "check the deploy"


@pytest.mark.parametrize("asked, used", [(5, 60), (60, 60), (3600, 3600), (99999, 3600)])
def test_the_delay_stays_between_one_minute_and_one_hour(agent: Any, asked: int, used: int) -> None:
    job = wc.start_self_paced_loop(agent, "p", start_thread=False)
    now = __import__("time").time()
    ScheduleWakeupTool().run({"delay_seconds": asked}, _ctx(agent))
    assert used - 1 <= job.next_at - now <= used + 5


def test_stop_true_ends_the_loop_and_needs_a_loop(agent: Any) -> None:
    tool, ctx = ScheduleWakeupTool(), _ctx(agent)
    with pytest.raises(ToolError):
        tool.run({"delay_seconds": 60}, ctx)
    job = wc.start_self_paced_loop(agent, "p", start_thread=False)
    with pytest.raises(ToolError):
        tool.run({}, ctx)
    assert "stopped" in tool.run({"stop": True}, ctx).lower() and not job.active and len(agent.message_queue) == 0


def test_no_choice_gets_one_fallback_then_the_loop_ends(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    job = wc.start_self_paced_loop(agent, "p", clock=lambda: 1000.0, start_thread=False)
    _run_next(agent, clock=lambda: 1000.0)
    assert job.active and job.next_at == 1000.0 + wc.FALLBACK_WAKE_S and "20m" in capsys.readouterr().out
    assert wc._tick(agent, job, job.next_at) is True
    _run_next(agent, clock=lambda: 3000.0)
    assert not job.active and "Loop ended" in capsys.readouterr().out


def test_a_choice_resets_the_fallback(agent: Any) -> None:
    job = wc.start_self_paced_loop(agent, "p", clock=lambda: 1000.0, start_thread=False)
    _run_next(agent, clock=lambda: 1000.0)
    wc._tick(agent, job, job.next_at)
    _run_next(agent, lambda: wc.schedule_wakeup(job, 60, clock=lambda: 3000.0), clock=lambda: 3000.0)
    assert job.active and not job.fallback_used
    wc._tick(agent, job, job.next_at)
    _run_next(agent, clock=lambda: 3100.0)
    assert job.active and job.next_at == 3100.0 + wc.FALLBACK_WAKE_S


def test_cancel_wakeup_only_during_the_wait(agent: Any) -> None:
    assert wc.cancel_wakeup(agent) is False
    job = wc.start_self_paced_loop(agent, "p", start_thread=False)
    assert wc.cancel_wakeup(agent) is False and job.active      # queued: not waiting yet
    item = agent.message_queue.pop()
    wc.before_queued_run(agent, item.id)
    assert wc.cancel_wakeup(agent) is False and job.active      # working on it
    wc.schedule_wakeup(job, 300)
    wc.after_queued_run(agent, item.id)
    assert any("esc stops it" in r for r in wc.loop_status_rows(agent))
    assert wc.cancel_wakeup(agent) is True and not job.active and wc.loop_status_rows(agent) == []


def test_only_one_self_paced_loop(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch_command("/loop first", agent)
    dispatch_command("/loop second", agent)
    assert [j.prompt for j in wc.active_jobs(agent)] == ["first"] and "already self-paced" in capsys.readouterr().out


# ─── /loop forms and loop.md ───────────────────────────────────────────────

def test_bare_loop_uses_the_builtin_maintenance_prompt_as_plain_text(agent: Any) -> None:
    dispatch_command("/loop", agent)
    job, item = wc.self_paced_job(agent), agent.message_queue.peek()
    assert job.default_prompt and item.plain and item.text == f"[/loop] {wc.MAINTENANCE_PROMPT}"
    assert "Continue any unfinished work" in item.text and "pull request" in item.text and "new initiatives" in item.text


def test_interval_only_runs_the_default_prompt_on_a_fixed_schedule(agent: Any) -> None:
    dispatch_command("/loop 15m", agent)
    job = wc.active_jobs(agent)[0]
    assert job.default_prompt and not job.self_paced and job.interval_s == 900 and agent.message_queue.peek().plain


def test_loop_md_order_limit_and_rereading(agent: Any, tmp_path: Path) -> None:
    ws = agent.settings.workspace
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "loop.md").write_text("user claude", encoding="utf-8")
    assert wc.load_default_prompt(ws)[0] == "user claude"
    (home / ".axon").mkdir(exist_ok=True)
    (home / ".axon" / "loop.md").write_text("user axon", encoding="utf-8")
    assert wc.load_default_prompt(ws)[0] == "user axon"
    (ws / ".claude").mkdir(exist_ok=True)
    (ws / ".claude" / "loop.md").write_text("project claude", encoding="utf-8")
    assert wc.load_default_prompt(ws)[0] == "project claude"
    (ws / ".axon").mkdir(exist_ok=True)
    path = ws / ".axon" / "loop.md"
    path.write_text("  \n", encoding="utf-8")                   # empty does not count
    assert wc.load_default_prompt(ws)[0] == "project claude"
    path.write_text("project axon", encoding="utf-8")
    assert wc.load_default_prompt(ws) == ("project axon", str(path))
    path.write_text("é" * 20_000, encoding="utf-8")             # 40,000 bytes, cut inside no character
    text = wc.load_default_prompt(ws)[0]
    assert text == "é" * 12_500 and len(text.encode()) == wc.LOOP_FILE_MAX_BYTES

    path.write_text("first version", encoding="utf-8")
    now = [1000.0]
    job = wc.start_self_paced_loop(agent, "", default_prompt=True, clock=lambda: now[0], start_thread=False)
    assert agent.message_queue.pop().text == "[/loop] first version"
    path.write_text("/permissions and !rm -rf", encoding="utf-8")
    wc.schedule_wakeup(job, 60, clock=lambda: now[0])
    wc._tick(agent, job, job.next_at)
    item = agent.message_queue.pop()                            # re-read, and a repository's file is never a command
    assert item.plain and item.text == "[/loop] /permissions and !rm -rf"


@pytest.mark.parametrize("arg, interval, prompt", [
    ("check the deploy every 2 hours", 7200, "check the deploy"), ("watch it every 90s", 120, "watch it"),
    ("5m check", 300, "check"), ("1d report", 86400, "report"),
])
def test_interval_forms(agent: Any, arg: str, interval: float, prompt: str) -> None:
    dispatch_command(f"/loop {arg}", agent)
    job = wc.active_jobs(agent)[0]
    assert (job.interval_s, job.prompt, job.self_paced) == (interval, prompt, False)


@pytest.mark.parametrize("arg", ["clear the old caches", "stop the build if it fails", "cancel stale jobs please", "check every 5 items"])
def test_words_that_start_a_prompt_are_not_subcommands(agent: Any, arg: str) -> None:
    dispatch_command(f"/loop {arg}", agent)
    assert wc.self_paced_job(agent).prompt == arg


def test_loop_stop_by_number_or_id_and_listing(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    a = wc.start_interval_loop(agent, "alpha", 60, start_thread=False)
    b = wc.start_cron_job(agent, CronSpec("0 9 * * *"), "beta", start_thread=False)
    dispatch_command("/loop list", agent)
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out and b.uid in out
    dispatch_command(f"/loop stop {b.uid}", agent)
    assert a.active and not b.active
    dispatch_command(f"/loop stop #{a.id}", agent)
    assert not a.active


def test_cron_can_be_turned_off(agent: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    job = wc.start_cron_job(agent, CronSpec("* * * * *"), "x", start_thread=False)
    monkeypatch.setenv("AXON_DISABLE_CRON", "1")
    assert wc._tick(agent, job, job.next_at) is False
    dispatch_command("/loop 5m x", agent)
    assert "turned off" in capsys.readouterr().out
    names = {t.name for t in create_default_registry().visible_tools()}
    assert not names & {"CronCreate", "CronList", "CronDelete", "ScheduleWakeup"}
    monkeypatch.delenv("AXON_DISABLE_CRON")
    assert {"CronCreate", "CronList", "CronDelete", "ScheduleWakeup"} <= {t.name for t in create_default_registry().visible_tools()}


def test_an_image_path_in_a_scheduled_prompt_is_not_attached(agent: Any, tmp_path: Path) -> None:
    from axon import cli
    from axon.providers.base import AssistantTurn, TextBlock
    agent.provider = FakeProvider([AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn")])
    picture = tmp_path / "secret.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    assert cli.process_line(agent, None, f"[Scheduled task x] look at {picture}", from_queue=True, plain=True)
    content = next(m for m in agent.conversation.messages if m["role"] == "user")["content"]
    assert isinstance(content, str) and str(picture) in content and not hasattr(agent, "_model_written_prompt")


def test_a_default_prompt_job_says_so_in_the_lists(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch_command("/loop", agent)
    dispatch_command("/loop list", agent)
    assert "loop.md or the built-in" in capsys.readouterr().out
    assert "loop.md or the built-in" in CronListTool().run({}, _ctx(agent))


# ─── schedule times ────────────────────────────────────────────────────────

def _cron_job(text: str, recurring: bool = True, uid: str = "abcd1234") -> wc.LoopJob:
    return wc.LoopJob(id=1, prompt="p", interval_s=60.0, cron=CronSpec(text), recurring=recurring, uid=uid)


def test_a_job_runs_exactly_on_its_cron_time() -> None:
    job = _cron_job("0 * * * *")
    assert wc._cron_run_after(job, _at(2026, 11, 9, 10, 1)) == _at(2026, 11, 9, 11, 0)
    assert wc._following_run(job, _at(2026, 11, 9, 11, 0)) == _at(2026, 11, 9, 12, 0)


def test_a_busy_session_does_not_catch_up() -> None:
    job = _cron_job("*/10 * * * *")
    wc._cron_run_after(job, _at(2026, 11, 9, 10, 1))
    wc._following_run(job, _at(2026, 11, 9, 10, 47))                   # four runs were missed
    assert job.nominal_at == _at(2026, 11, 9, 10, 50)


# ─── restored on resume ────────────────────────────────────────────────────

def _session_agent(tmp_path: Path, sid: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(session=SimpleNamespace(session_dir=tmp_path, active_session_id=sid), fires_scheduled_tasks=True,
                           settings=SimpleNamespace(workspace=tmp_path))


def test_a_resumed_session_gets_its_tasks_back_with_the_same_ids(tmp_path: Path) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    job = wc.start_cron_job(a, CronSpec("7 * * * *"), "poll", clock=lambda: start, start_thread=False)
    once = wc.start_cron_job(a, CronSpec("3 12 * * *"), "remind", recurring=False, clock=lambda: start, start_thread=False)
    b = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(b, clock=lambda: start + 60, start_thread=False) == 2
    got = {j.uid: j for j in wc.active_jobs(b)}
    assert set(got) == {job.uid, once.uid} and got[job.uid].plain and got[job.uid].expires_at == job.expires_at
    assert not got[once.uid].recurring and got[once.uid].next_at == _at(2026, 11, 9, 12, 3)


def test_expired_and_passed_tasks_stay_gone_and_missed_runs_are_not_caught_up(tmp_path: Path) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    wc.start_cron_job(a, CronSpec("7 * * * *"), "poll", clock=lambda: start, start_thread=False)
    wc.start_cron_job(a, CronSpec("3 12 * * *"), "remind", recurring=False, clock=lambda: start, start_thread=False)
    later = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(later, clock=lambda: start + 86400, start_thread=False) == 1      # the one-shot's time passed
    job = wc.active_jobs(later)[0]
    assert job.recurring and job.nominal_at == _at(2026, 11, 10, 10, 7) and job.next_at > start + 86400
    gone = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(gone, clock=lambda: start + wc.EXPIRY_S + 1, start_thread=False) == 0


def test_leaving_a_session_keeps_its_tasks_but_cancelling_one_does_not(tmp_path: Path) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    keep = wc.start_cron_job(a, CronSpec("7 * * * *"), "keep", clock=lambda: start, start_thread=False)
    drop = wc.start_cron_job(a, CronSpec("9 * * * *"), "drop", clock=lambda: start, start_thread=False)
    wc.stop_loops(a, drop.uid)
    wc.save_cron_jobs(a)
    wc.stop_loops(a)                                                   # /clear, /resume, exit: ended here, not forgotten
    b = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(b, clock=lambda: start, start_thread=False) == 1
    assert [j.uid for j in wc.active_jobs(b)] == [keep.uid]


def test_a_finished_one_shot_is_not_restored(tmp_path: Path) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    job = wc.start_cron_job(a, CronSpec("3 10 * * *"), "once", recurring=False, clock=lambda: start, start_thread=False)
    assert wc._tick(a, job, job.next_at) is True
    b = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(b, clock=lambda: start, start_thread=False) == 0


def test_only_a_terminal_session_restores_and_a_damaged_file_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    wc.start_cron_job(a, CronSpec("7 * * * *"), "poll", clock=lambda: start, start_thread=False)
    headless = _session_agent(tmp_path)
    headless.fires_scheduled_tasks = False
    assert wc.restore_cron_jobs(headless, clock=lambda: start, start_thread=False) == 0
    monkeypatch.setenv("AXON_DISABLE_CRON", "1")
    assert wc.restore_cron_jobs(_session_agent(tmp_path), clock=lambda: start, start_thread=False) == 0
    monkeypatch.delenv("AXON_DISABLE_CRON")
    path = wc._cron_file(a)
    if os.name != "nt":                 # Windows has no group/other bits: the file is covered by the profile folder's ACL
        assert path.stat().st_mode & 0o077 == 0
    path.write_text('[{"id": "x"}, 5, {"id": "y", "prompt": "p", "recurring": true, "cron": "not cron"}]')
    assert wc.restore_cron_jobs(_session_agent(tmp_path), clock=lambda: start, start_thread=False) == 0
    path.write_text("{broken")
    assert wc.restore_cron_jobs(_session_agent(tmp_path), clock=lambda: start, start_thread=False) == 0


# ─── a /loop interval is a cron step ───────────────────────────────────────

@pytest.mark.parametrize("seconds,expr,real", [
    (5, "* * * * *", 60), (60, "* * * * *", 60), (61, "*/2 * * * *", 120), (300, "*/5 * * * *", 300),
    (420, "*/6 * * * *", 360), (1800, "*/30 * * * *", 1800), (2700, "*/30 * * * *", 1800), (3600, "0 * * * *", 3600),
    (5400, "0 * * * *", 3600), (7200, "0 */2 * * *", 7200), (86400, "0 0 * * *", 86400), (172800, "0 0 */2 * *", 172800),
    (86400 * 90, "0 0 */30 * *", 86400 * 30),
])
def test_an_interval_becomes_the_nearest_cron_step(seconds: int, expr: str, real: int) -> None:
    assert wc.interval_cron(seconds) == (expr, real)
    CronSpec(expr)                                                     # every one of them parses


def test_a_fixed_loop_runs_at_once_then_on_the_next_5_minute_mark(agent: Any) -> None:
    now = [_at(2026, 11, 9, 10, 2)]
    job = wc.start_interval_loop(agent, "check", 300, clock=lambda: now[0], start_thread=False)
    assert job.runs == 1 and job.cron.text == "*/5 * * * *" and job.next_at == _at(2026, 11, 9, 10, 5)
    assert job.plain is False                                          # the person typed it


def test_a_rounded_interval_is_named(agent: Any, capsys: pytest.CaptureFixture[str]) -> None:
    dispatch_command("/loop 7m check", agent)
    out = capsys.readouterr().out
    assert "Rounded from 7m" in out and "6m" in out
    wc.stop_loops(agent)
    dispatch_command("/loop 5m check", agent)
    assert "Rounded" not in capsys.readouterr().out
    wc.stop_loops(agent)


def test_fixed_loops_are_restored_but_self_paced_ones_are_not(tmp_path: Path) -> None:
    start = _at(2026, 11, 9, 10, 1)
    a = _session_agent(tmp_path)
    typed = wc.start_interval_loop(a, "/review", 3600, max_runs=5, clock=lambda: start, start_thread=False)
    upkeep = wc.start_interval_loop(a, "", 3600, default_prompt=True, clock=lambda: start, start_thread=False)
    wc.start_self_paced_loop(a, "watch", clock=lambda: start, start_thread=False)
    wc.save_cron_jobs(a)
    b = _session_agent(tmp_path)
    assert wc.restore_cron_jobs(b, clock=lambda: start + 60, start_thread=False) == 2
    got = {j.uid: j for j in wc.active_jobs(b)}
    assert got[typed.uid].plain is False and got[typed.uid].max_runs == 5 and got[typed.uid].runs == 1
    assert got[upkeep.uid].default_prompt and got[upkeep.uid].plain
    assert not any(j.self_paced for j in got.values())


def test_a_file_without_the_plain_field_stays_plain_text(tmp_path: Path) -> None:
    a = _session_agent(tmp_path)
    path = wc._cron_file(a)
    path.parent.mkdir(parents=True)
    path.write_text('[{"id": "abcd1234", "cron": "7 * * * *", "prompt": "!touch x", "recurring": true}]')
    assert wc.restore_cron_jobs(a, clock=lambda: _at(2026, 11, 9, 10, 1), start_thread=False) == 1
    assert wc.active_jobs(a)[0].plain is True


# ─── Windows: a naive datetime within a day of the epoch has no .timestamp() ──

def test_next_after_survives_a_datetime_the_platform_cannot_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows ``datetime.timestamp()`` raises OSError 22 for local times near 1 Jan 1970 (it probes a negative
    time), which is where a test clock of 1000.0 lands. The next run is then worked out by plain arithmetic."""
    import axon.tools.cron as cron_mod

    class WindowsDatetime(datetime):
        def timestamp(self) -> float:
            if self.year == 1970:
                raise OSError(22, "Invalid argument")
            return super().timestamp()

    spec = CronSpec("*/5 * * * *")
    want = spec.next_after(1000.0)
    monkeypatch.setattr(cron_mod, "datetime", WindowsDatetime)
    assert spec.next_after(1000.0) == want
    assert want is not None and 0 < want - 1000.0 <= 300
