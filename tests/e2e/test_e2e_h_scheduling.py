"""
End-to-end, in a real pseudo-terminal against the scripted fake LLM: self-paced /loop, the Cron* tools, the status
line's refreshInterval, vimInsertModeRemaps and the comment field of the permission prompt.

The scheduling tests wait for the real clock (a wakeup is at least 60 seconds out, cron has one-minute steps).
"""
from __future__ import annotations

import json
import time

import pytest

from harness import AxonEnv
from test_e2e_d_helpers import TRACEBACK, open_repl, posix_only

pytestmark = [pytest.mark.e2e, posix_only]

ESC, TAB, DOWN = "\x1b", "\t", "\x1b[B"
CTRL_C = "\x03"


def _wait(s: object, seconds: float) -> None:
    """Keep reading the terminal while time passes: a redraw blocks once nobody reads the pty."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        s._pump(0.1)


def _user_settings(env: AxonEnv, data: dict) -> None:
    (env.axon_home / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def _user_texts(env: AxonEnv) -> list[str]:
    return [r.last_user_text() for r in env.llm.completion_requests()]


def _wait_requests(env: AxonEnv, n: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while len(env.llm.completion_requests()) < n and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(env.llm.completion_requests()) >= n, f"only {len(env.llm.completion_requests())} of {n} requests after {timeout}s"


# ─── self-paced /loop ──────────────────────────────────────────────────────

def test_self_paced_loop_waits_what_the_model_chose_then_stops_when_told(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(
        [{"tool": "ScheduleWakeup", "args": {"delay_seconds": 60, "reason": "build still running"}}], [{"text": "FIRST-ITERATION"}],
        [{"tool": "ScheduleWakeup", "args": {"stop": True}}], [{"text": "SECOND-ITERATION"}])
    s = open_repl(axon_env)
    try:
        start = time.monotonic()
        m = s.submit("/loop check the deploy")
        s.expect("self-paced", timeout=10, since=m)
        s.expect("FIRST-ITERATION", timeout=20, since=m)
        s.expect("Next loop run in 60s: build still running", timeout=10, since=m)
        s.expect("next loop run in .* esc stops it", timeout=5, since=m)
        first = axon_env.llm.completion_requests()[0]
        assert "check the deploy" in first.last_user_text() and "ScheduleWakeup" in first.last_user_text()
        assert "ScheduleWakeup" in first.tool_names
        _wait(s, 45)
        assert len(axon_env.llm.completion_requests()) == 2     # both requests of the first iteration, and nothing yet
        s.expect("SECOND-ITERATION", timeout=30, since=m)
        assert time.monotonic() - start >= 58
        assert "check the deploy" in axon_env.llm.completion_requests()[2].last_user_text()
        s.drain(2)
        assert len(axon_env.llm.completion_requests()) == 4 and not axon_env.llm.pending
        m2 = s.submit("/loop list")
        s.expect("No active loops", timeout=5, since=m2)
        assert TRACEBACK not in s.screen
    finally:
        s.close()


@pytest.mark.parametrize("key", [ESC, CTRL_C], ids=["esc", "ctrl-c"])
def test_esc_or_ctrl_c_during_the_wait_stops_the_loop(axon_env: AxonEnv, key: str) -> None:
    axon_env.llm.enqueue([{"tool": "ScheduleWakeup", "args": {"delay_seconds": 60, "reason": "waiting"}}], [{"text": "RAN-ONCE"}])
    s = open_repl(axon_env)
    try:
        m = s.submit("/loop keep an eye on it")
        s.expect("esc stops it", timeout=20, since=m)
        s.drain(0.4)
        m = s.mark()
        s.send(key)
        s.expect("Pending wakeup cancelled", timeout=5, since=m)
        m = s.submit("/loop list")
        s.expect("No active loops", timeout=5, since=m)
        assert "Goodbye" not in s.screen
    finally:
        s.close()


def test_esc_with_no_loop_waiting_keeps_its_meaning(axon_env: AxonEnv) -> None:
    s = open_repl(axon_env)
    try:
        m = s.mark()
        s.send(ESC)
        s.expect("esc again to rewind", timeout=5, since=m)
    finally:
        s.close()


def test_bare_loop_sends_loop_md_as_plain_text_and_never_runs_it(axon_env: AxonEnv) -> None:
    (axon_env.workspace / ".axon").mkdir()
    (axon_env.workspace / ".axon" / "loop.md").write_text("!touch from-loop-md", encoding="utf-8")
    axon_env.llm.enqueue([{"tool": "ScheduleWakeup", "args": {"stop": True}}], [{"text": "LOOP-MD-READ"}])
    s = open_repl(axon_env)
    try:
        m = s.submit("/loop")
        s.expect("LOOP-MD-READ", timeout=20, since=m)
        assert "[/loop] !touch from-loop-md" in _user_texts(axon_env)[0]
        assert not (axon_env.workspace / "from-loop-md").exists()
    finally:
        s.close()


def test_bare_loop_runs_the_builtin_prompt(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"tool": "ScheduleWakeup", "args": {"stop": True}}], [{"text": "MAINTENANCE-DONE"}])
    s = open_repl(axon_env)
    try:
        m = s.submit("/loop")
        s.expect("MAINTENANCE-DONE", timeout=20, since=m)
        assert "Continue any unfinished work" in _user_texts(axon_env)[0]
    finally:
        s.close()


# ─── CronCreate, CronList, CronDelete ──────────────────────────────────────

def test_one_shot_cron_fires_between_turns_as_plain_text_without_a_prompt(axon_env: AxonEnv) -> None:
    """Default mode: no permission prompt to schedule. What fires reaches the model as text; its `!` runs nothing."""
    axon_env.llm.enqueue(
        [{"tool": "CronCreate", "args": {"cron": "* * * * *", "prompt": "!touch from-cron", "recurring": False}}], [{"text": "SCHEDULED-IT"}],
        [{"tool": "CronList", "args": {}}], [{"text": "LISTED-NONE"}])
    s = open_repl(axon_env, ("--mode", "default"))
    try:
        m = s.submit("remind me in a minute")
        s.expect("SCHEDULED-IT", timeout=20, since=m)
        assert "Allow CronCreate" not in s.screen[m:]
        results = axon_env.llm.completion_requests()[1].tool_results()
        assert any("Scheduled " in r and "fires once" in r and "next run" in r for r in results)
        s.expect("LISTED-NONE", timeout=70, since=m)            # the job fired by itself at the next minute
        fired = axon_env.llm.completion_requests()[2].last_user_text()
        assert "[Scheduled task " in fired and "!touch from-cron" in fired
        assert not (axon_env.workspace / "from-cron").exists()
        assert "No scheduled tasks" in " ".join(axon_env.llm.completion_requests()[3].tool_results())   # one-shot: gone
    finally:
        s.close()


def test_cron_list_and_delete_through_the_model(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"tool": "CronCreate", "args": {"cron": "0 9 * * 1-5", "prompt": "morning check"}}],
                         [{"tool": "CronList", "args": {}}], [{"text": "CREATED-AND-LISTED"}])
    s = open_repl(axon_env, ("--mode", "default"))
    try:
        m = s.submit("schedule the morning check")
        s.expect("CREATED-AND-LISTED", timeout=20, since=m)
        listing = axon_env.llm.completion_requests()[2].tool_results()[-1]
        assert "0 9 * * 1-5 (recurring)" in listing and "morning check" in listing
        uid = listing.split()[0]
        axon_env.llm.enqueue([{"tool": "CronDelete", "args": {"id": uid}}], [{"tool": "CronList", "args": {}}], [{"text": "DELETED-IT"}])
        m = s.submit("cancel it")
        s.expect("DELETED-IT", timeout=20, since=m)
        results = axon_env.llm.completion_requests()[-1].tool_results()
        assert f"Cancelled {uid}" in results[-2] and "No scheduled tasks" in results[-1]
    finally:
        s.close()


def test_plan_mode_refuses_cron_create(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"tool": "CronCreate", "args": {"cron": "* * * * *", "prompt": "x"}}], [{"text": "AFTER-REFUSAL"}])
    s = open_repl(axon_env, ("--mode", "plan"))
    try:
        m = s.submit("schedule it")
        s.expect("AFTER-REFUSAL", timeout=20, since=m)
        result = axon_env.llm.completion_requests()[1].tool_results()[-1]
        assert "Scheduled " not in result
        m = s.submit("/loop list")
        s.expect("No active loops", timeout=5, since=m)
    finally:
        s.close()


def test_print_mode_says_a_job_would_never_fire(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"tool": "CronCreate", "args": {"cron": "* * * * *", "prompt": "x"}}], [{"text": "PRINT-DONE"}])
    res = axon_env.run(["-p", "schedule it", "--mode", "bypass"])
    assert "PRINT-DONE" in res.stdout
    assert any("terminal" in r for r in axon_env.llm.completion_requests()[1].tool_results())


# ─── status line refreshInterval ───────────────────────────────────────────

def _counting_status(env: AxonEnv, refresh: int | None) -> tuple[str, dict]:
    log = env.root / "status-runs.log"
    cfg: dict = {"type": "command", "command": f"echo x >> '{log}'; echo COUNT-$(wc -l < '{log}' | tr -d ' ')"}
    if refresh is not None:
        cfg["refreshInterval"] = refresh
    return str(log), {"statusLine": cfg}


def _runs(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        return len(f.readlines())


def test_refresh_interval_reruns_and_redraws_while_idle(axon_env: AxonEnv) -> None:
    log, settings = _counting_status(axon_env, 1)
    _user_settings(axon_env, settings)
    s = open_repl(axon_env)
    try:
        before = _runs(log)
        m = s.mark()
        s.expect(f"COUNT-{before + 3}", timeout=8, since=m)     # the new value reaches the screen with no key pressed
        _wait(s, 5)
        assert 6 <= _runs(log) - before <= 10
    finally:
        s.close()


def test_slower_refresh_interval_is_respected(axon_env: AxonEnv) -> None:
    log, settings = _counting_status(axon_env, 3)
    _user_settings(axon_env, settings)
    s = open_repl(axon_env)
    try:
        before = _runs(log)
        _wait(s, 7)
        assert 1 <= _runs(log) - before <= 3
    finally:
        s.close()


def test_without_refresh_interval_idle_runs_nothing_but_a_turn_still_does(axon_env: AxonEnv) -> None:
    log, settings = _counting_status(axon_env, None)
    _user_settings(axon_env, settings)
    axon_env.llm.enqueue([{"text": "A-TURN"}])
    s = open_repl(axon_env)
    try:
        s.drain(0.5)
        before = _runs(log)
        _wait(s, 4)
        assert _runs(log) == before
        s.turn("hello")
        assert _runs(log) > before
    finally:
        s.close()


# ─── vimInsertModeRemaps ───────────────────────────────────────────────────

def test_vim_remap_leaves_insert_mode_and_the_keys_never_reach_the_model(axon_env: AxonEnv) -> None:
    _user_settings(axon_env, {"editorMode": "vim", "vimInsertModeRemaps": {"jk": "<Esc>"}})
    axon_env.llm.enqueue([{"text": "GOT-IT"}])
    s = open_repl(axon_env)
    try:
        m = s.mark()
        s.send("hello ")
        s.expect("hello", timeout=5, since=m)
        s.expect("-- INSERT --", timeout=5, since=m)
        m = s.mark()
        s.send("jk")
        s.expect("-- NORMAL --", timeout=5, since=m)
        s.send("x")                                             # NORMAL mode: deletes the trailing space
        s.drain(0.3)
        s.send("\r")
        s.expect("GOT-IT", timeout=15, since=m)
        assert _user_texts(axon_env)[0] == "hello"
    finally:
        s.close()


def test_vim_remap_after_the_window_stays_text(axon_env: AxonEnv) -> None:
    _user_settings(axon_env, {"editorMode": "vim", "vimInsertModeRemaps": {"jk": "<Esc>"}})
    axon_env.llm.enqueue([{"text": "GOT-LITERAL"}])
    s = open_repl(axon_env)
    try:
        s.send("j")
        time.sleep(1.3)
        s.send("k")
        s.drain(0.3)
        m = s.mark()
        s.send("\r")
        s.expect("GOT-LITERAL", timeout=15, since=m)
        assert _user_texts(axon_env)[0] == "jk"
    finally:
        s.close()


def test_a_repository_cannot_remap_your_keys(axon_env: AxonEnv) -> None:
    _user_settings(axon_env, {"editorMode": "vim"})
    (axon_env.workspace / ".axon").mkdir()
    (axon_env.workspace / ".axon" / "settings.json").write_text(json.dumps({"vimInsertModeRemaps": {"jk": "<Esc>"}}), encoding="utf-8")
    axon_env.llm.enqueue([{"text": "STILL-TEXT"}])
    s = open_repl(axon_env)
    try:
        m = s.mark()
        s.send("jk")
        s.drain(0.4)
        assert "-- NORMAL --" not in s.screen[m:]
        s.send("\r")
        s.expect("STILL-TEXT", timeout=15, since=m)
        assert _user_texts(axon_env)[0] == "jk"
    finally:
        s.close()


# ─── a comment on a permission answer ──────────────────────────────────────

def _bash_prompt(env: AxonEnv, target: str) -> tuple:
    env.llm.enqueue([{"tool": "Bash", "args": {"command": f"echo ran > {target}", "description": "write"}}], [{"text": "AFTER-ANSWER"}])
    s = open_repl(env, ("--mode", "default"))
    m = s.submit("run bash")
    s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
    s.expect(r"tab comment", timeout=5, since=m)
    s.drain(0.3)
    return s, m


def test_yes_with_a_comment_runs_and_the_model_gets_the_note(axon_env: AxonEnv) -> None:
    s, m = _bash_prompt(axon_env, "yes.txt")
    try:
        s.send(TAB)
        s.expect("Comment on Yes", timeout=5, since=m)
        s.send("yes but use staging")                           # y, e, s... are text here, not answers
        s.expect("yes but use staging", timeout=5, since=m)
        assert not (axon_env.workspace / "yes.txt").exists()
        s.send("\r")
        s.expect("AFTER-ANSWER", timeout=15, since=m)
        assert (axon_env.workspace / "yes.txt").read_text().strip() == "ran"
        result = axon_env.llm.completion_requests()[1].tool_results()[-1]
        assert "yes but use staging" in result and "approved" in result
    finally:
        s.close()


def test_no_with_a_comment_tells_the_model_why(axon_env: AxonEnv) -> None:
    s, m = _bash_prompt(axon_env, "no.txt")
    try:
        s.send(DOWN + DOWN)
        s.drain(0.2)
        s.send(TAB)
        s.expect("Comment on No", timeout=5, since=m)
        s.send("not on this branch\r")
        s.expect("AFTER-ANSWER", timeout=15, since=m)
        assert not (axon_env.workspace / "no.txt").exists()
        result = axon_env.llm.completion_requests()[1].tool_results()[-1]
        assert "declined" in result and "not on this branch" in result
    finally:
        s.close()


def test_tab_closes_the_field_and_the_kept_comment_goes_with_the_answer(axon_env: AxonEnv) -> None:
    s, m = _bash_prompt(axon_env, "kept.txt")
    try:
        s.send(TAB)
        s.expect("Comment on Yes", timeout=5, since=m)
        s.send("keep me")
        s.drain(0.2)
        s.send(TAB)
        s.drain(0.3)
        assert not (axon_env.workspace / "kept.txt").exists()
        s.send("y")                                             # the field is closed: the shortcut answers again
        s.expect("AFTER-ANSWER", timeout=15, since=m)
        assert "keep me" in axon_env.llm.completion_requests()[1].tool_results()[-1]
    finally:
        s.close()


def test_type_ahead_can_neither_open_the_field_nor_answer(axon_env: AxonEnv) -> None:
    """The keys arrive as soon as the question is on the screen, inside the 0.35 s settle time."""
    axon_env.llm.enqueue([{"tool": "Bash", "args": {"command": "echo ran > ahead.txt", "description": "write"}}], [{"text": "AFTER-ANSWER"}])
    s = open_repl(axon_env, ("--mode", "default"))
    try:
        m = s.submit("run bash")
        s.expect(r"⚠ Allow Bash\?", timeout=15, since=m)
        s.send(TAB + "y\ry\r")
        s.expect(r"tab comment", timeout=5, since=m)
        s.drain(0.8)
        shown = s.screen[m:]
        assert "Comment on Yes" not in shown and "Allowed" not in shown and "Denied" not in shown
        assert not (axon_env.workspace / "ahead.txt").exists()
        s.send("n")                                             # the prompt is still open and answers normally
        s.expect("Declined", timeout=15, since=m)
        assert not (axon_env.workspace / "ahead.txt").exists()
    finally:
        s.close()
