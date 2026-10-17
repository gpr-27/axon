"""Parallel Task fan-out: numbering, failure reporting, summary table and the live monitor."""
import threading
from types import SimpleNamespace

from axon.agent.subagent import SubagentManager, SubagentTask
from axon.ui.render import render_subagent_dashboard, render_subagent_result_card
from axon.ui.subagent_monitor import build_monitor_frame
from axon.ui.theme import strip_ansi


def test_reserved_numbers_follow_call_order_even_when_threads_start_late():
    mgr = SubagentManager()
    mgr.reserve("call-a")  # #1: the first Task call in the batch
    mgr.reserve("call-b")  # #2

    done = threading.Event()

    def later_thread():  # the second call's thread starts first
        mgr.register("count docs", title="Count docs", call_id="call-b")
        done.set()

    th = threading.Thread(target=later_thread)
    th.start()
    done.wait(2)
    mgr.register("scan repo", title="Scan repo", call_id="call-a")
    th.join()
    assert [(t.index, t.title) for t in mgr.all_tasks()] == [(1, "Scan repo"), (2, "Count docs")]


def test_failed_subagent_is_marked_failed_not_completed():
    line = "[Subagent #1 (Count docs) failed: HTTP 400 Bad Request from 127.0.0.1]"
    card = strip_ansi(render_subagent_result_card(line, width=90))
    assert "Failed" in card and "Completed" not in card
    assert "Subagent #1 · Count docs" in card
    assert "HTTP 400 Bad Request" in card
    assert "Your message was not answered" not in card


def test_successful_result_still_shows_completed():
    line = "[Subagent #2 Result (Scan repo) | 4,659 tokens (in: 4,655 · out: 4)]:\nlisted 12 files"
    card = strip_ansi(render_subagent_result_card(line, width=90))
    assert "Completed" in card and "listed 12 files" in card


def _task(index, title, status, **kw):
    t = SubagentTask(id=f"sub-{index}", index=index, title=title, prompt="p", status=status, steps=1, max_steps=25)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def test_summary_counts_failures_separately(capsys):
    tasks = [
        _task(1, "Scan repo", "completed", elapsed_s=0.4, tokens_consumed=4659),
        _task(2, "Count docs", "error", error_msg="HTTP 400 Bad Request", elapsed_s=0.1),
    ]
    for t in tasks:
        t.agent_type = "explore"
    render_subagent_dashboard(tasks)
    out = strip_ansi(capsys.readouterr().out)
    assert "1 done" in out and "1 failed" in out
    assert "2/2 completed" not in out
    assert "✗ HTTP 400 Bad Request" in out
    row = next(ln for ln in out.splitlines() if "Scan repo" in ln)
    assert "explore" in row and "✓ done" in row     # agent type has its own column; status is glyph + word


def test_monitor_shows_what_was_asked_and_why_it_failed():
    tasks = [_task(1, "Count docs", "error", error_msg="HTTP 400 Bad Request", prompt="count docs in docs/",
                   model="gpt-4o")]
    tasks[0].agent_type = "explore"
    agent = SimpleNamespace(message_queue=None)
    frame = strip_ansi("\n".join(build_monitor_frame(1, tasks, agent, "", 90)))
    rows = {ln.split()[0]: " ".join(ln.split()[1:]) for ln in frame.splitlines() if ln.strip()}
    assert rows["asked"] == "count docs in docs/"
    assert rows["model"] == "gpt-4o"
    assert rows["reason"] == "HTTP 400 Bad Request"


def test_events_are_held_while_the_monitor_owns_the_screen():
    from axon.agent.trace import SubagentEvent
    from axon.agent.loop import Agent

    delivered: list[object] = []
    agent = SimpleNamespace(_held_events=None, _trace=None, on_event=delivered.append)
    agent._emit = Agent._emit.__get__(agent)
    agent._deliver = Agent._deliver.__get__(agent)
    agent._emit(SubagentEvent(phase="start", task_id="sub-1", agent_type="explore", title="a", model="m"))
    assert len(delivered) == 1

    agent._held_events = []
    agent._emit(SubagentEvent(phase="end", task_id="sub-1", agent_type="explore", title="a", model="m"))
    assert len(delivered) == 1 and len(agent._held_events) == 1


_LONG = ("AgentRouter rejected the request as invalid (HTTP 500). This is a rejection of the request itself, "
         "not an outage. Check the model name and the request size, then retry.")


def test_dashboard_shows_every_failure_in_full_and_fits_the_width(capsys, monkeypatch):
    import re as _re
    from axon.ui import render
    from axon.ui.theme import display_width
    for width in (60, 88, 120):
        monkeypatch.setattr(render, "term_width", lambda w=width: w)
        tasks = [_task(1, "Scan repo", "completed", elapsed_s=0.4, tokens_consumed=4659),
                 _task(2, "Inspect result card rendering in detail", "error", error_msg=_LONG, elapsed_s=80.9,
                       retries=3, resumes=1, tool_calls=14),
                 _task(3, "Trace events", "exhausted", steps=25, elapsed_s=140.0)]
        for t in tasks:
            t.agent_type = "explore"
        render_subagent_dashboard(tasks)
        out = strip_ansi(capsys.readouterr().out)
        assert all(display_width(ln) <= width - 2 for ln in out.splitlines()), (width, out)
        assert "1 done" in out and "1 failed" in out and "1 hit the step limit" in out
        flat = _re.sub(r"\s+", " ", out)
        assert _LONG in flat                                  # never cut with an ellipsis
        assert "3 provider retries" in flat and "1 resume after an error" in flat and "14 tool calls" in flat
        assert "step limit (25/25 steps)" in flat
        assert "/subagents <#>" in flat
        assert [ln.split()[0] for ln in out.splitlines() if _re.match(r"^  \d  \S", ln)] == ["1", "2", "3"]


def test_failure_card_accepts_steps_and_progress_section():
    import re as _re
    line = ("[Subagent #3 (Inspect docs) failed after 7 steps: " + _LONG + "]\n\n"
            "Progress before the failure:\n- Read a.py\n- Grep foo")
    card = strip_ansi(render_subagent_result_card(line, width=70))
    flat = _re.sub(r"[\s│]+", " ", card)
    assert "Subagent #3 · Inspect docs · ✗ Failed" in card and "Completed" not in card
    assert "7 steps before the failure" in card
    assert _LONG in flat
    assert "Progress before the failure" in card and "• Read a.py" in card and "• Grep foo" in card
    assert "the parent agent sees this failure and can retry" in card
    assert "]" not in card.replace("[", "")                 # bracket of the header never leaks into the card
    # progress section inside the bracket works too
    inside = strip_ansi(render_subagent_result_card(
        "[Subagent #4 failed: boom\n\nProgress before the failure:\n- Read b.py]", width=70))
    assert "boom" in inside and "• Read b.py" in inside and "b.py]" not in inside


def test_subagent_card_takes_the_timing_in_its_border():
    from axon.ui import render
    from axon.ui.theme import display_width
    rows = render.render_subagent_result_card("[Subagent #2 Result (Scan repo) | 9 tokens]: ok", width=60).splitlines()
    folded = render._append_box_footer(rows, "took 3.10s")
    assert len(folded) == len(rows) and strip_ansi(folded[-1]).strip().startswith("╰─ took 3.10s ─")
    assert display_width(folded[-1]) == display_width(rows[-1])


def test_plan_shows_every_item_numbered_with_the_active_one_tagged():
    from axon.ui.render import _todo_summary
    items = [f"  [{'✓' if i < 2 else ('▶' if i == 2 else ' ')}] {i + 1}. step number {i + 1} " + "word " * 12
             for i in range(12)]
    rows = [strip_ansi(r) for r in _todo_summary("Updated todos (12 items):\n" + "\n".join(items), width=60)]
    assert rows[0].startswith("Plan · 2/12 done") and "16%" in rows[0] and "1 in progress" in rows[0]
    text = "\n".join(rows)
    for i in range(12):
        assert f"{i + 1}. step number {i + 1}" in text            # nothing collapsed into "+N more"
    assert "more" not in text and "in progress" in text
    assert all(len(r) <= 60 for r in rows)


def test_transcript_falls_back_to_the_activity_timeline(capsys, monkeypatch):
    from axon.ui import render
    monkeypatch.setattr(render, "term_width", lambda: 88)
    t = _task(2, "Count docs", "error", error_msg=_LONG, prompt="count docs in docs/", model="gpt-4o", retries=2)
    t.agent_type = "explore"
    t.add_entry("tool", "Read docs/index.md")
    t.add_entry("retry", "provider rejected the request; retrying")
    t.add_entry("error", "HTTP 500", is_error=True)
    render.render_subagent_transcript(t)
    out = strip_ansi(capsys.readouterr().out)
    assert "Subagent #2 · Count docs" in out and "✗ failed" in out and "gpt-4o" in out and "2 provider retries" in out
    assert "Prompt" in out and "count docs in docs/" in out
    assert "Activity · 3 events" in out and "Read docs/index.md" in out and "HTTP 500" in out


def test_after_the_monitor_only_this_batch_is_summarized_and_steps_are_not_replayed(workspace, monkeypatch):
    from axon.agent.trace import SubagentStep
    from axon.providers.base import ToolExecutionResult, ToolUseBlock
    from tests.fakes import FakeProvider
    from tests.test_core_helpers import make_agent
    import axon.ui.render as render

    events = []
    agent = make_agent(FakeProvider([]), workspace, events=events)
    for n in (1, 2, 3):
        agent.subagents.register(f"p{n}", title=f"Task {n}")
    shown = []
    monkeypatch.setattr(render, "render_subagent_dashboard", lambda tasks: shown.append([t.index for t in tasks]))
    calls = [ToolUseBlock(id="a", name="Task", input={}), ToolUseBlock(id="b", name="Task", input={})]
    held = [SubagentStep(task_id="sub-3", agent_type="explore", kind="tool", name="Read"),
            ToolExecutionResult(id="b", name="Task", input={}, content="second", is_error=False),
            ToolExecutionResult(id="a", name="Task", input={}, content="first", is_error=False)]
    agent._deliver_after_monitor(held, calls, {2, 3})
    assert shown == [[2, 3]]                                      # #1 belongs to an earlier batch
    printed = [e for e in events if type(e).__name__ != "SubagentStep"]
    assert [e.content for e in printed] == ["first", "second"]    # cards in call order, not finish order
    assert not any(type(e).__name__ == "SubagentStep" for e in events)


def test_a_note_typed_in_the_monitor_says_it_goes_to_the_main_agent():
    from axon.ui.subagent_monitor import queued_notes_line
    tasks = [_task(1, "Count docs", "running")]
    frame = strip_ansi("\n".join(build_monitor_frame(0, tasks, None, "", 100,
                                                     notice="✓ Note #1 queued for the main agent · runs after")))
    assert "Note #1 queued for the main agent" in frame
    assert "type a note for the main agent" in frame              # the empty input explains where notes go
    line = strip_ansi(queued_notes_line([SimpleNamespace(id=1, text="ok"), SimpleNamespace(id=2, text="also check docs")]))
    assert "2 notes for the main agent" in line and '#1 "ok"' in line and '#2 "also check docs"' in line


def test_steps_read_against_the_limit_only_while_running():
    done = _task(1, "Read docs", "completed", steps=2, max_steps=25, result_text="ok")
    running = _task(2, "Read more", "running", steps=12, max_steps=25)
    frame = strip_ansi("\n".join(build_monitor_frame(0, [done, running], None, "", 120)))
    row1 = next(l for l in frame.splitlines() if "2 steps" in l)
    assert "/25" not in row1 and "▱" not in row1                  # finished: a count, no half-empty bar
    assert "step 12 · limit 25" in frame


def test_queue_commands_typed_in_the_monitor_answer_inside_it():
    from axon.agent.state import MessageQueue
    from axon.ui.subagent_monitor import _queue_command
    agent = SimpleNamespace(message_queue=MessageQueue())
    agent.message_queue.push("first")
    msg = _queue_command("/q drop 7", agent)
    assert msg and "\n" not in msg and "\x1b" not in msg
