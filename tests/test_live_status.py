"""Live status under the spinner (what is happening now + a state-based tip) and the decision explainers."""
from __future__ import annotations

import io
import sys
from typing import Any

import pytest

from axon.agent.trace import CompactionEvent, LLMCallEnd, RetryEvent, ToolRationale
from axon.config import Settings, map_json_settings
from axon.providers.base import (
    LLMCallStart,
    TextDelta,
    ThinkingDelta,
    ToolBatchStart,
    ToolExecutionResult,
    ToolExecutionStart,
    ToolUseStart,
    TurnComplete,
    Usage,
)
from axon.ui import decision_help, live_status, live_tips
from axon.ui.live_status import LiveStatus, View, Wait, now_candidates
from axon.ui.live_tips import TipFacts, TipPicker
from axon.ui.theme import display_width, glyph, strip_ansi
from axon import waitmarks

T0 = 1000.0
DOT = glyph("dot")


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_live(clock: Clock | None = None, facts: TipFacts | None = None, tips: bool = True) -> tuple[LiveStatus, Clock]:
    clock = clock or Clock()
    live = LiveStatus(clock=clock, provider_of=lambda model: "AgentRouter")
    live.waits_of = lambda: []
    live.subagents_of = lambda: []
    live.facts_of = lambda: facts or TipFacts()
    live.tips_enabled_of = lambda: tips
    return live, clock


def start_step(live: LiveStatus, step: int = 1, model: str = "claude-sonnet-4-5") -> None:
    live.observe(LLMCallStart(step, 25, model, 4))


def now_line(live: LiveStatus, clock: Clock, width: int = 200) -> str:
    return live.rows(width, now=clock.t).now


# ─── state -> line ────────────────────────────────────────────────────────

def test_waiting_for_model_names_model_provider_and_seconds() -> None:
    live, clock = make_live()
    start_step(live)
    clock.t = T0 + 3
    line = now_line(live, clock)
    assert "waiting for claude-sonnet-4-5 via AgentRouter" in line and "3s" in line
    assert "buffering" not in line


def test_no_first_byte_after_five_seconds_mentions_gateway_and_esc() -> None:
    live, clock = make_live()
    start_step(live)
    clock.t = T0 + 12
    line = now_line(live, clock)
    assert "12s" in line and "the gateway may be buffering the answer; Esc stops it" in line


def test_first_byte_ends_the_waiting_message() -> None:
    live, clock = make_live()
    start_step(live)
    clock.t = T0 + 8
    live.observe(ThinkingDelta("x" * 400))
    clock.t = T0 + 9
    line = now_line(live, clock)
    assert "claude-sonnet-4-5 is thinking" in line and "~100 tokens so far" in line and "buffering" not in line


def test_writing_the_answer_and_a_tool_call() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(TextDelta("a" * 4000))
    clock.t = T0 + 2
    assert "is writing the answer" in now_line(live, clock) and "~1.0k tokens so far" in now_line(live, clock)
    live.observe(ToolUseStart("t1", "Bash"))
    clock.t += 1
    assert "is writing a Bash call" in now_line(live, clock)


def test_running_bash_shows_the_command_elapsed_and_step() -> None:
    live, clock = make_live()
    start_step(live, step=3)
    live.observe(ToolBatchStart(1, []))
    live.observe(ToolExecutionStart("b1", "Bash", {"command": "pytest -q", "description": "Run the unit tests"}))
    clock.t = T0 + 4
    line = now_line(live, clock)
    assert line.startswith("running Bash: pytest -q") and "4s" in line and "step 3" in line


def test_running_a_tool_without_description_falls_back_to_what_it_does() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(ToolExecutionStart("r1", "Read", {"path": "src/app.py"}))
    clock.t = T0 + 1
    assert "running Read: read src/app.py" in now_line(live, clock)


def test_several_tools_in_parallel() -> None:
    live, clock = make_live()
    start_step(live, step=2)
    for i, name in enumerate(("Read", "Grep", "Glob")):
        live.observe(ToolExecutionStart(f"p{i}", name, {"path": "a", "pattern": "x"}))
    clock.t = T0 + 2
    line = now_line(live, clock)
    assert "running 3 tools at once: Read, Grep, Glob" in line and "step 2" in line
    live.observe(ToolExecutionResult("p0", "Read", {}, "ok"))
    live.observe(ToolExecutionResult("p1", "Grep", {}, "ok"))
    assert "running Glob" in now_line(live, clock)


def test_between_steps_is_unknown_so_only_step_and_seconds() -> None:
    live, clock = make_live()
    start_step(live, step=4)
    live.observe(ToolExecutionStart("b1", "Bash", {"command": "ls"}))
    live.observe(ToolExecutionResult("b1", "Bash", {}, "ok"))
    clock.t = T0 + 7
    assert now_line(live, clock) == f"step 4 {DOT} 7s"


def test_no_turn_no_rows() -> None:
    live, clock = make_live()
    assert live.rows(80, now=T0).now == ""


def view(**kw: Any) -> View:
    base = dict(turn_t0=T0, step=2, model="m1", provider="P", phase="waiting", phase_t0=T0)
    base.update(kw)
    return View(**base)


def test_waiting_for_an_mcp_server_to_reply_and_to_connect() -> None:
    reply = view(waits=(Wait("mcp_call", T0, {"server": "github", "tool": "search_issues"}),))
    assert now_candidates(reply, T0 + 3)[0] == f"waiting for MCP server github to reply (search_issues) {DOT} 3s"
    connect = view(waits=(Wait("mcp_connect", T0, {"server": "github"}),))
    assert "waiting for MCP server github to connect" in now_candidates(connect, T0 + 2)[0]


def test_waiting_for_a_hook() -> None:
    v = view(waits=(Wait("hook", T0, {"event": "PreToolUse"}),))
    assert now_candidates(v, T0 + 2)[0] == f"waiting for a PreToolUse hook to finish {DOT} 2s"


def test_compaction_says_why_with_context_percent() -> None:
    v = view(context_pct=82.4, waits=(Wait("compact", T0, {"model": "small-model"}),))
    text = now_candidates(v, T0 + 9)[0]
    assert "compacting the conversation (context is at 82%)" in text and "summarising older turns with small-model" in text
    assert v.compacting


def test_titling_and_waiting_for_the_user() -> None:
    assert "naming this session" in now_candidates(view(waits=(Wait("title", T0),)), T0 + 1)[0]
    ask = view(waits=(Wait("user", T0, {"what": "approve Bash"}),))
    assert now_candidates(ask, T0 + 5)[0].startswith("waiting for you: approve Bash")


def test_user_wait_outranks_every_other_wait() -> None:
    v = view(waits=(Wait("hook", T0, {"event": "Stop"}), Wait("user", T0 + 1, {"what": "your answer"})))
    assert now_candidates(v, T0 + 3)[0].startswith("waiting for you")


def test_retry_after_429_counts_down_and_names_the_attempt() -> None:
    live, clock = make_live()
    start_step(live)
    clock.t = T0 + 2
    live.observe(RetryEvent(attempt=2, max_attempts=5, delay_s=4.0, error="HTTP 429 from AgentRouter: slow down"))
    assert now_line(live, clock) == f"retrying after HTTP 429 (rate limited) in 4s {DOT} retry 2 of 5"
    clock.t += 2.5
    assert "in 2s" in now_line(live, clock)
    clock.t += 1.6
    assert "retrying after HTTP 429 (rate limited) now" in now_line(live, clock)
    clock.t += 5
    assert now_line(live, clock).startswith("retry sent, waiting for claude-sonnet-4-5")


def test_retry_after_5xx_and_connection_errors() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(RetryEvent(1, 3, 1.0, "HTTP 503 from anthropic"))
    assert "HTTP 503 (server error) in 1s" in now_line(live, clock)
    live.observe(RetryEvent(2, 3, 2.0, "ConnectError: connection refused"))
    assert "retrying after ConnectError: connection refused in 2s" in now_line(live, clock)


def test_first_byte_after_a_retry_clears_the_retry_line() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(RetryEvent(1, 3, 1.0, "HTTP 429"))
    live.observe(TextDelta("hello"))
    assert "retry" not in now_line(live, clock)


def test_failover_is_not_called_a_retry() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(RetryEvent(1, 2, 0.0, "a:m HTTP 529 -> failover to b, +3.0s"))
    assert now_line(live, clock).startswith("switching to the fallback model")


def test_subagent_line_names_it_and_its_step() -> None:
    v = view(subagents=(live_status.SubView("sub-2", "#2 explorer", T0, steps=4, max_steps=15, activity="Read src/x.py"),))
    text = now_candidates(v, T0 + 12)[0]
    assert "subagent #2 explorer: step 4 of 15" in text and "Read src/x.py" in text and "12s" in text


def test_a_running_subagent_is_read_from_the_agent() -> None:
    task = type("T", (), dict(id="sub-1", index=1, agent_type="explorer", start_time=T0, steps=2, max_steps=9,
                               activity="Grep foo", status="running"))()
    done = type("T", (), dict(id="sub-2", index=2, agent_type="x", start_time=T0, steps=1, max_steps=9,
                               activity="", status="completed"))()
    manager = type("M", (), {"all_tasks": lambda self: [task, done]})()
    agent = type("A", (), {"subagents": manager, "settings": None, "registry": None})()
    live = LiveStatus(clock=Clock(), agent_of=lambda: agent)
    assert [s.label for s in live._running_subagents()] == ["#1 explorer"]


def test_lines_fit_50_and_80_columns() -> None:
    v = view(phase_t0=T0 - 30)
    for width in (50, 80):
        text = live_status.fit(now_candidates(v, T0), width)
        assert display_width(text) <= width and text
    long_tool = view(tools=(live_status.ToolView("1", "Bash", T0, "x" * 300),), phase="tools")
    assert display_width(live_status.fit(now_candidates(long_tool, T0 + 1), 50)) <= 50


def test_compaction_event_does_not_change_the_state() -> None:
    live, clock = make_live()
    start_step(live)
    live.observe(CompactionEvent("auto", 10, 4, 9000, 3000))
    assert "waiting for" in now_line(live, clock)


def test_context_percent_comes_from_the_last_call() -> None:
    live, _ = make_live()
    start_step(live)
    live.observe(LLMCallEnd(iteration=1, model="m", usage=None, cost=0.0, duration_s=1.0, stop_reason="tool_use",
                            started_at=0.0, ended_at=1.0, context_used=820, context_window=1000))
    assert live.view().context_pct == pytest.approx(82.0)


def test_observe_never_raises_on_odd_events() -> None:
    live, _ = make_live()
    live.observe(object())
    live.observe(ToolExecutionStart("x", "Weird", None))        # type: ignore[arg-type]
    live.observe(TurnComplete("end_turn", Usage()))
    assert live.view().phase == ""


def test_the_models_words_are_kept_for_the_prompt() -> None:
    live, _ = make_live()
    start_step(live)
    live.observe(ToolRationale("q1", "AskUserQuestion", "I need to know which database to use", "model-text"))
    live.observe(ToolExecutionStart("q1", "AskUserQuestion", {"questions": []}))
    assert live.current_why("AskUserQuestion") == "I need to know which database to use"
    live.observe(ToolRationale("q2", "Bash", "Axon's own guess", "heuristic"))
    live.observe(ToolExecutionStart("q2", "Bash", {"command": "ls"}))
    assert live.current_why("Bash") == ""


# ─── tips ─────────────────────────────────────────────────────────────────

def tip_at(live: LiveStatus, clock: Clock, dt: float) -> str:
    clock.t = T0 + dt
    return live.rows(200, now=clock.t).tip


def test_no_tip_in_the_first_seconds_of_a_turn() -> None:
    live, clock = make_live()
    start_step(live)
    assert tip_at(live, clock, 1) == ""


def test_long_wait_tip_explains_esc_and_queueing() -> None:
    live, clock = make_live()
    start_step(live)
    assert tip_at(live, clock, 7) == "Esc stops the turn; type to queue a message that is sent between steps"


def test_tool_running_tip_names_trace_and_transcript() -> None:
    live, clock = make_live(facts=TipFacts(used=frozenset({"queue"}), commands=frozenset({"transcript"})))
    start_step(live)
    live.observe(ToolExecutionStart("b", "Bash", {"command": "sleep 9"}))
    assert tip_at(live, clock, 4) == "ctrl+o opens the full transcript; alt+l changes trace detail"


def test_high_context_tip_uses_the_live_percent() -> None:
    live, clock = make_live(facts=TipFacts(context_pct=82.0))
    start_step(live)
    assert tip_at(live, clock, 4) == "ctx 82%: /compact frees space"


def test_cost_and_many_mcp_tools_tips() -> None:
    facts = TipFacts(session_cost=2.5, mcp_tools=40, used=frozenset({"transcript", "mode"}))
    live, clock = make_live(facts=facts)
    start_step(live)
    live.observe(ToolExecutionStart("b", "Bash", {"command": "x"}))
    first = tip_at(live, clock, 4)
    second = tip_at(live, clock, 13)
    assert first == "MCP tools load on demand via ToolSearch; /tools lists them"
    assert second == "/cost shows where the money went"


def test_retry_tip_comes_first_even_before_the_hold_ends() -> None:
    live, clock = make_live()
    start_step(live)
    assert tip_at(live, clock, 4)                                  # some tip is up since t=4
    live.observe(RetryEvent(1, 5, 4.0, "HTTP 429"))
    assert tip_at(live, clock, 5) != "Switch model with /model if this keeps failing"      # held, only 1s
    assert tip_at(live, clock, 8) == "Switch model with /model if this keeps failing"      # urgent cuts in after 3s


def test_a_tip_holds_for_eight_seconds_then_rotates_without_repeats() -> None:
    facts = TipFacts(session_cost=3.0)
    live, clock = make_live(facts=facts)
    start_step(live)
    live.observe(ToolExecutionStart("b", "Bash", {"command": "x"}))
    seen = []
    for dt in range(4, 60):
        tip = tip_at(live, clock, dt)
        if tip and (not seen or seen[-1][1] != tip):
            seen.append((dt, tip))
    assert [dt for dt, _ in seen][:2] == [4, 12]                   # the second tip waits out the 8 s hold
    texts = [t for _, t in seen]
    assert len(texts) == len(set(texts))                            # never repeated in a turn


def test_tips_never_repeat_across_a_long_turn_and_reset_next_turn() -> None:
    live, clock = make_live(facts=TipFacts(session_cost=3.0))
    start_step(live)
    shown = [t for t in (tip_at(live, clock, dt) for dt in range(4, 200)) if t]
    assert shown
    live.end_turn()
    start_step(live)                     # the clock keeps running: a new turn starts at T0 + 199
    clock.t = T0 + 199
    live.observe(LLMCallStart(1, 25, "m", 1), now=clock.t)
    first_again = live.rows(200, now=clock.t + 4).tip
    assert first_again == shown[0]       # a new turn starts with a clean slate


def test_tips_off_setting_hides_the_tip_but_not_the_line() -> None:
    live, clock = make_live(tips=False)
    start_step(live)
    clock.t = T0 + 8
    rows = live.rows(200, now=clock.t)
    assert rows.tip == "" and rows.now


def test_a_tip_about_a_feature_the_user_already_used_is_skipped() -> None:
    facts = TipFacts(context_pct=90.0, used=frozenset({"compact", "queue", "mode"}))
    live, clock = make_live(facts=facts)
    start_step(live)
    assert tip_at(live, clock, 7) == ""


def test_a_tip_that_names_a_missing_command_is_skipped() -> None:
    facts = TipFacts(context_pct=90.0, commands=frozenset({"model"}), used=frozenset({"queue", "mode"}))
    live, clock = make_live(facts=facts)
    start_step(live)
    assert tip_at(live, clock, 7) == ""


def test_picker_with_a_fake_clock_and_generic_fallback() -> None:
    picker = TipPicker(rules=[], fallback=lambda: "generic tip one")
    v = view(turn_t0=T0)
    f = TipFacts()
    assert picker.pick(v, f, T0 + 1) == ""                          # too early
    assert picker.pick(v, f, T0 + 4) == "generic tip one"
    picker.fallback = lambda: "generic tip two"
    assert picker.pick(v, f, T0 + 6) == "generic tip one"           # held
    assert picker.pick(v, f, T0 + 13) == "generic tip two"
    picker.fallback = lambda: "generic tip one"
    assert picker.pick(v, f, T0 + 30) == "generic tip two"          # one is already seen: keep what is up


def test_tip_facts_from_an_agent_use_history_and_the_registry(workspace: Any) -> None:
    from axon.agent.context import ContextManager
    from axon.agent.loop import Agent
    from axon.permissions.engine import PermissionEngine
    from axon.session.ledger import Ledger
    from axon.session.store import SessionStore
    from axon.tools import create_default_registry
    from fakes import FakeProvider

    s = Settings(workspace=workspace, mode="default")
    agent = Agent(provider=FakeProvider([]), tools=create_default_registry(), permissions=PermissionEngine(s),
                  context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)
    facts = live_tips.facts_from_agent(agent)
    assert facts.mode == "default" and facts.commands and "compact" in facts.commands
    assert live_tips.facts_from_agent(None) == TipFacts() or live_tips.facts_from_agent(None).commands is not None


# ─── settings, marks, non-TTY ─────────────────────────────────────────────

def test_setting_and_json_key(workspace: Any) -> None:
    assert Settings(workspace=workspace).live_status is True
    assert map_json_settings({"liveStatus": False}) == {"live_status": False}
    off = type("A", (), {"settings": Settings(workspace=workspace, live_status=False)})()
    assert LiveStatus(agent_of=lambda: off).enabled() is False


def test_wait_marks_open_and_close() -> None:
    before = len(waitmarks.active_waits())
    with waitmarks.waiting("hook", event="Stop"):
        marks = waitmarks.active_waits()
        assert len(marks) == before + 1 and marks[-1].kind == "hook" and marks[-1].info == {"event": "Stop"}
    assert len(waitmarks.active_waits()) == before


def test_compaction_summary_call_is_marked() -> None:
    from axon.agent.context import ContextManager
    from axon.agent.state import Conversation

    seen: list[list[str]] = []

    class Prov:
        def stream(self, **kw: Any):
            seen.append([w.kind for w in waitmarks.active_waits()])
            return iter(())

        def finalize(self) -> Any:
            return type("Turn", (), {"text": "summary", "usage": None})()

    conv = Conversation()
    for i in range(6):
        conv.add_user_text(f"question {i}") if hasattr(conv, "add_user_text") else conv.messages.append({"role": "user", "content": f"q{i}"})
        conv.messages.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
    ctx = ContextManager(Settings())
    ctx.generate_summary(conv, Prov(), "m", keep_turns=1)
    assert seen and "compact" in seen[0]
    assert "compact" not in [w.kind for w in waitmarks.active_waits()]


def test_spinner_prints_nothing_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.ui.render import Spinner

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    live, _ = make_live()
    start_step(live)
    spinner = Spinner()
    spinner.live = live
    spinner.start("Step 1 thinking")
    spinner.stop()
    assert buf.getvalue() == "" and not Spinner.enabled()


def test_explainers_print_nothing_when_nothing_is_asked(capsys: pytest.CaptureFixture[str]) -> None:
    from axon.ui.approve import ask_approval

    assert ask_approval("Bash", {"command": "ls"}, "needs approval") == "deny"       # no tty: denied, silently
    out = capsys.readouterr().out
    assert out == ""
    with decision_help.shown([]):
        pass
    assert capsys.readouterr().out == ""


def test_shown_erases_the_block_only_on_an_interactive_terminal(capsys: pytest.CaptureFixture[str]) -> None:
    with decision_help.shown(["  line one", "  line two"], erase=False):
        pass
    out = capsys.readouterr().out
    assert "line one" in out and "\x1b[2A" not in out
    with decision_help.shown(["  line one", "  line two"], erase=True):
        pass
    assert "\x1b[2A\r\x1b[J" in capsys.readouterr().out


def test_live_status_off_means_no_explainer(workspace: Any) -> None:
    off = type("A", (), {"settings": Settings(workspace=workspace, live_status=False)})()
    live_status.set_current(LiveStatus(agent_of=lambda: off))
    try:
        assert decision_help.approval_lines("Bash", {"command": "ls"}) == []
        assert decision_help.question_lines({"question": "q", "options": [{"label": "a"}]}) == []
    finally:
        live_status.set_current(None)


# ─── spinner rows ─────────────────────────────────────────────────────────

def test_spinner_row_says_now_a_tip_goes_under_it_and_they_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.ui import render

    live, clock = make_live()
    start_step(live)
    clock.t = T0 + 12
    monkeypatch.setattr(live, "clock", clock)
    monkeypatch.setattr(render, "term_width", lambda: 50)
    monkeypatch.setattr(render, "term_height", lambda: 40)
    spinner = render.Spinner()
    spinner.live = live
    rows = [strip_ansi(r) for r in spinner.status_rows()]
    assert len(rows) == 1 and "Tip: Esc stops" in rows[0]            # the status is on the spinner row itself
    line = strip_ansi(spinner.render_line("x"))
    assert "waiting for" in line and "Now:" not in line and "interrupt" in line
    assert all(display_width(r) <= 49 for r in rows + [line])


def test_spinner_rows_fall_back_to_the_plain_tip_row_when_off(monkeypatch: pytest.MonkeyPatch, workspace: Any) -> None:
    from axon.ui import render

    off = type("A", (), {"settings": Settings(workspace=workspace, live_status=False)})()
    live = LiveStatus(agent_of=lambda: off)
    monkeypatch.setattr(render, "term_height", lambda: 40)
    spinner = render.Spinner()
    spinner.live = live
    monkeypatch.setenv("AXON_NO_TIPS", "1")
    assert spinner.status_rows() == []


def test_spinner_erases_every_row_it_drew(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.ui import render

    spinner = render.Spinner()
    out = io.StringIO()
    spinner._real, spinner._visible, spinner._rows = out, True, 3
    spinner._erase_locked()
    assert out.getvalue() == "\r\x1b[2K" + "\x1b[A\r\x1b[2K" * 2


def test_renderer_feeds_the_live_status_and_clears_it_at_the_end() -> None:
    from axon.ui.render import Renderer

    r = Renderer()
    r.on_event(LLMCallStart(1, 25, "m-1", 3))
    assert r.live.view().phase == "waiting" and r.live.view().step == 1
    r.turn_footer(0, Usage(), 0.0, 1.0)
    assert r.live.view().turn_t0 == 0.0 and r.live.rows(80).now == ""


# ─── decision explainers ──────────────────────────────────────────────────

QUESTION = {"question": "Which database should the service use?", "header": "Database", "multiSelect": False,
            "options": [{"label": "Postgres", "description": "full SQL server, needs a running service"},
                        {"label": "SQLite", "description": "one local file, no setup"},
                        {"label": "Redis", "description": "in-memory store, data may be lost"}]}


def flat(lines: list[str]) -> str:
    """The block as running text (wrapped rows joined), so wording can be checked at any width."""
    return " ".join(line.strip() for line in lines)


def test_question_explainer_covers_what_why_options_keys_and_esc() -> None:
    lines = decision_help.explain_question(QUESTION, why="I need to pick a store before writing the schema", width=80)
    text = flat(lines)
    assert "Deciding: Which database should the service use? [Database]" in text
    assert 'Why asked: the model said "I need to pick a store before writing the schema" just before asking' in text
    assert "1  Postgres - full SQL server, needs a running service" in text
    assert "2  SQLite - one local file, no setup" in text and "3  Redis - in-memory store, data may be lost" in text
    assert "Other: last row: press Enter on it, then type your own answer" in text
    assert "Keys: 1-3 picks at once, or arrows + Enter (starts on 1)." in text
    assert "Esc skips: the model is told you did not answer and may ask in text or assume" in text
    assert len(lines) <= 10


def test_question_explainer_without_a_reason_says_so_and_handles_multi_select() -> None:
    q = dict(QUESTION, multiSelect=True)
    text = flat(decision_help.explain_question(q, index=2, total=3, width=80))
    assert "it gave no reason in words" in text and "(question 2 of 3)" in text
    assert "space ticks it, Enter confirms" in text


@pytest.mark.parametrize("width", [50, 80])
def test_question_explainer_wraps_to_the_width(width: int) -> None:
    lines = decision_help.explain_question(QUESTION, why="x " * 100, width=width)
    assert lines and all(display_width(line) <= width - 2 for line in lines)


def test_bash_approval_explainer() -> None:
    lines = decision_help.explain_approval(
        "Bash", {"command": "npm install lodash", "description": "Install lodash"},
        reason="Bash commands need approval in default mode.", rule="Bash(npm install:*)", width=80)
    text = flat(lines)
    assert "What: Bash runs this command in your shell: Install lodash" in text
    assert "Can: run programs, change files and use the network" in text
    assert "Why asked: Bash commands need approval in default mode Answers" in text
    assert "1 yes, this call only" in text and "2 yes, every Bash call until you exit" in text
    assert "3 no" in text and "4 always allow Bash(npm install:*) in this project" in text
    assert "Keys: 1-4, y / a / n, or arrows + Enter (starts on 1)." in text
    assert "Esc = No: the model is told you declined and may try another way; Axon waits for you, there is no timeout" in text


def test_edit_approval_explainer_names_the_file_and_has_no_rule_row() -> None:
    lines = decision_help.explain_approval("Edit", {"path": "src/app.py", "old_string": "a", "new_string": "b"},
                                           reason="Edits need approval in default mode.", why="Fix the typo in the title", width=80)
    text = flat(lines)
    assert "What: Edit edits this file: src/app.py" in text
    assert "Can: change the file in place" in text
    assert 'the model said "Fix the typo in the title"' in text
    assert "4 always allow" not in text and "Keys: 1-3," in text


@pytest.mark.parametrize("width", [50, 80])
def test_approval_explainer_fits_the_width(width: int) -> None:
    lines = decision_help.explain_approval("Bash", {"command": "x" * 200, "description": "d " * 80},
                                           reason="r " * 90, rule="Bash(" + "y" * 90 + ")", width=width)
    assert 5 <= len(lines) <= 22 and all(display_width(line) <= width - 2 for line in lines)


def test_mcp_and_unknown_tools_get_honest_wording() -> None:
    text = "\n".join(decision_help.explain_approval("mcp__github__create_issue", {"title": "Bug"}, width=80))
    assert "calls a tool on the MCP server github" in text and "decided by that server" in text
    text = "\n".join(decision_help.explain_approval("Frobnicate", {}, width=80))
    assert "runs the Frobnicate tool" in text


def test_control_characters_in_model_text_are_removed() -> None:
    q = {"question": "Pick\x1b[31m one", "options": [{"label": "A\x1b[2J", "description": "x\x07y"}]}
    text = "\n".join(decision_help.explain_question(q, width=80))
    assert "\x1b" not in text and "\x07" not in text
