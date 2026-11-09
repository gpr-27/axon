"""The agent list and transcript: layout at each width, keys, addressing, and delivery of typed messages."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.agent.state import Conversation, MessageQueue
from axon.agent.subagent import ScreenGate, SubagentManager, SubagentTask, user_message
from axon.errors import ToolError
from axon.ui import agent_panel
from axon.ui.subagent_monitor import (CalmFrame, MonitorKeys, agent_table, build_monitor_frame, can_message,
                                      conversation_entries, transcript_lines)
from axon.ui.theme import strip_ansi


def _mk(i: int, title: str, status: str = "running", **kw) -> SubagentTask:
    t = SubagentTask(id=f"sub-{i}", index=i, title=title, prompt=f"job {i}", status=status, steps=2, max_steps=25,
                     agent_type="explore", model="gpt-4o", start_time=time.time() - 12, elapsed_s=61.0)
    t.tool_calls, t.tokens_consumed = 3, 12_345
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _fleet() -> list[SubagentTask]:
    return [_mk(1, "Survey the docs folder for stale pages", activity="Read a very long path/" + "x" * 80),
            _mk(2, "Check the tests", "completed", result_text="# Report\nAll green."),
            _mk(3, "界面 audit", "error", error_msg="HTTP 500 upstream"),
            _mk(4, "Stopped one", "cancelled", name="scout")]


@pytest.mark.parametrize("width", [50, 64, 80, 110, 130])
def test_table_is_aligned_and_fits_at_every_width(width: int) -> None:
    lines = agent_table(_fleet(), width, selected=0, now=time.time())
    assert all(plat.str_width(ln) <= width for ln in lines)
    head, rows = strip_ansi(lines[0]), [strip_ansi(ln) for ln in lines[1:]]
    agent_rows = [r for r in rows if "tok" not in r and r[4:5] in "▶✓✗⊘"]
    assert len(agent_rows) == 4
    # the numbers are right-aligned to the same column on every agent row, and under the headings
    assert {plat.str_width(r.rstrip()) for r in agent_rows} == {plat.str_width(head.rstrip())}
    assert all(r.rstrip().endswith("12.3k") for r in agent_rows)
    text = "\n".join(rows)
    assert "12s" in text and "1m 01s" in text and "    3" in text
    assert "All green." in text and "stopped by you" in text and "scout" in text
    assert "HTTP 500" in text


def test_table_shows_a_dash_for_what_a_shell_does_not_have() -> None:
    row = SimpleNamespace(index=10_000, label="bash_1", id="bash_1", title="sleep 9", status="running", kind="shell",
                          tool_calls=None, tokens_consumed=0, start_time=time.time() - 3, elapsed_s=0, activity="tick")
    line = strip_ansi(agent_table([row], 80)[1])
    assert "bash_1" in line and "tick" in line and line.rstrip().endswith("-")


def test_frame_never_gets_taller_than_the_window() -> None:
    tasks = [_mk(i, f"Task {i}") for i in range(1, 40)]
    for t in tasks:
        for j in range(60):
            t.add_entry("tool", f"Read file{j}.py")
    for height in (8, 12, 24, 40):
        for view in (0, 5, "Q"):
            for width in (50, 80, 130):
                lines = build_monitor_frame(view, tasks, None, "", width, "✓ note", height=height)
                assert len(lines) <= height, (height, view, width, len(lines))


def test_transcript_follows_the_end_and_scrolls_back_to_earlier_lines() -> None:
    t = _mk(1, "Long")
    for j in range(100):
        t.add_entry("tool", f"Read file{j}.py")
    keys = MonitorKeys(None)
    tasks = [t]
    keys.handle("enter", tasks)
    assert keys.view == 1
    text = "\n".join(strip_ansi(ln) for ln in keys.frame(tasks, None, 80, 20))
    assert "file99.py" in text and "earlier lines" in text and "file0.py" not in text
    keys.handle("pageup", tasks)
    keys.handle("up", tasks)
    text = "\n".join(strip_ansi(ln) for ln in keys.frame(tasks, None, 80, 20))
    assert "file99.py" not in text and "newer line" in text
    before = keys.scroll
    t.add_entry("tool", "Read fresh.py")          # new lines must not move what is being read
    keys.frame(tasks, None, 80, 20)
    assert keys.scroll == before + 1
    keys.handle("end", tasks)
    assert keys.scroll == 0
    keys.handle("home", tasks)
    text = "\n".join(strip_ansi(ln) for ln in keys.frame(tasks, None, 80, 20))
    assert "file0.py" in text


def test_list_keys_select_open_and_go_back() -> None:
    tasks = _fleet()
    keys = MonitorKeys(SimpleNamespace(message_queue=MessageQueue()))
    keys.handle("down", tasks)
    keys.handle("down", tasks)
    assert keys.selected == 2
    keys.handle("up", tasks)
    assert keys.selected == 1 and keys.view == 0
    keys.handle("enter", tasks)
    assert keys.view == 2
    assert keys.handle("esc", tasks) is None and keys.view == 0
    assert keys.handle("esc", tasks) == "interrupt"         # a fan-out: Esc on the list stops the turn
    console = MonitorKeys(None, console=True)
    assert console.handle("esc", tasks) == "close"           # the console: back to the prompt


def test_typing_in_a_transcript_never_triggers_list_shortcuts() -> None:
    mgr = SubagentManager()
    task = mgr.register("job", title="One")
    agent = SimpleNamespace(subagents=mgr, message_queue=MessageQueue())
    keys = MonitorKeys(agent)
    keys.handle("enter", [task])
    for k in "x1q":                                          # shortcuts on the list, plain text here
        keys.handle(k, [task])
    assert keys.buf == "x1q" and keys.view == 1 and not task.stopped


def test_x_stops_the_selected_agent_and_clears_a_finished_row() -> None:
    cancelled: list[str] = []
    mgr = SimpleNamespace(cancel=lambda tid: cancelled.append(tid) or True)
    tasks = _fleet()
    keys = MonitorKeys(SimpleNamespace(subagents=mgr))
    keys.handle("x", tasks)
    assert cancelled == ["sub-1"] and keys.notice.startswith("✓ Stopping 1")
    keys.handle("down", tasks)
    keys.handle("x", tasks)
    assert tasks[1].dismissed and cancelled == ["sub-1"]     # finished: cleared, not "stopped"


def test_ctrl_x_stops_from_the_transcript_and_arrows_change_agent() -> None:
    cancelled: list[str] = []
    tasks = _fleet()
    keys = MonitorKeys(SimpleNamespace(subagents=SimpleNamespace(cancel=lambda tid: cancelled.append(tid) or True)))
    keys.handle("enter", tasks)
    keys.handle("ctrl-x", tasks)
    assert cancelled == ["sub-1"]
    keys.handle("right", tasks)
    assert keys.view == 2 and keys.selected == 1
    keys.handle("left", tasks)
    keys.handle("left", tasks)
    assert keys.view == 4                                    # wraps round


def test_console_list_does_not_collect_text_and_its_list_has_no_input_row() -> None:
    tasks = _fleet()
    keys = MonitorKeys(None, console=True)
    keys.handle("h", tasks)
    assert keys.buf == ""
    lines = [strip_ansi(ln) for ln in keys.frame(tasks, None, 80, 30)]
    assert not lines[-1].lstrip().startswith("›") and "back to the prompt" in "\n".join(lines)


def test_ctrl_o_expands_tool_output_kept_in_the_entry() -> None:
    t = _mk(1, "One")
    t.add_entry("tool", "Bash ls")
    t.add_entry("result", "alpha  (3 lines)", full="alpha\nbeta\ngamma")
    short = "\n".join(strip_ansi(ln) for ln in transcript_lines(t, 80))
    assert "beta" not in short and "3 lines" in short
    full = "\n".join(strip_ansi(ln) for ln in transcript_lines(t, 80, expanded=True))
    assert "beta" in full and "gamma" in full


def test_transcript_shows_text_your_messages_and_the_report() -> None:
    t = _mk(1, "One", "completed", result_text="Final words.")
    t.add_entry("text", "I am looking.")
    t.add_entry("note", "you: hello there")
    t.add_entry("delivered", "1 message from you sent to the model after step 2")
    text = "\n".join(strip_ansi(ln) for ln in transcript_lines(t, 80))
    assert "I am looking." in text and "› you: hello there" in text and "✓ 1 message" in text
    assert "Report" in text and "Final words." in text


def test_a_subagent_from_an_earlier_run_has_a_transcript_from_its_conversation() -> None:
    entries = conversation_entries([
        {"role": "user", "content": "the task"},
        {"role": "assistant", "content": [{"type": "text", "text": "Looking."},
                                          {"type": "tool_use", "id": "a", "name": "Read", "input": {"path": "x.py"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "line1\nline2"}]},
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": "Done."},
    ])
    assert [e["kind"] for e in entries] == ["text", "tool", "result", "note", "text"]
    t = _mk(1, "Old", "completed", conversation=Conversation([{"role": "user", "content": "the task"},
                                                              {"role": "assistant", "content": "Done."}]))
    assert "Done." in "\n".join(strip_ansi(ln) for ln in transcript_lines(t, 80))
    assert can_message(t) == "resume"


def test_shell_row_opens_its_output_and_takes_no_message() -> None:
    shell = SimpleNamespace(id="bash_1", command="echo hi", status="running", started=time.time() - 2,
                            stream=SimpleNamespace(lock=_NoLock(), buf="one\ntwo\n"))
    row = agent_panel.ShellRow(shell, 0)
    assert row.status == "running" and row.activity == "two" and can_message(row) == ""
    keys = MonitorKeys(SimpleNamespace(subagents=None), console=True)
    keys.handle("enter", [row])
    assert keys.view == row.index
    text = "\n".join(strip_ansi(ln) for ln in keys.frame([row], None, 80, 20))
    assert "one" in text and "two" in text and "takes no messages" in text
    keys.buf = "hello"
    keys.handle("enter", [row])
    assert keys.notice.startswith("✗ bash_1 takes no messages")
    done = SimpleNamespace(**{**shell.__dict__, "status": "exited with code 3"})
    assert agent_panel.ShellRow(done, 0).status == "error"


class _NoLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_a) -> None:
        return None


def test_calm_frame_rewrites_only_the_rows_that_changed(monkeypatch) -> None:
    written: list[str] = []
    from axon.ui import subagent_monitor as mon
    monkeypatch.setattr(mon, "_write", lambda parts: written.append("".join(parts)))
    monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    frame = CalmFrame()
    frame.render(["alpha", "beta", "gamma"], (2, 2))
    assert all(x in written[-1] for x in ("alpha", "beta", "gamma"))
    frame.render(["alpha", "BETA", "gamma"], (2, 2))
    second = written[-1]
    assert "BETA" in second and "alpha" not in second and "gamma" not in second and "\033[J" not in second
    frame.render(["alpha", "BETA"], (1, 0))                   # shrinking erases what is left below
    assert "\033[J" in written[-1]
    frame.invalidate()
    frame.render(["alpha", "BETA"], (1, 0))
    assert "alpha" in written[-1]


def test_screen_gate_waits_for_the_view_only_when_there_is_one() -> None:
    gate = ScreenGate()
    start = time.monotonic()
    with gate.borrow():
        assert not gate.wanted.is_set()
    assert time.monotonic() - start < 0.5
    gate.attach()
    gate.granted.set()
    assert gate.attached()
    gate.detach()
    assert not gate.attached()


# ── delivery ────────────────────────────────────────────────────────────────

def _live(mgr: SubagentManager, title: str = "One", **kw) -> SubagentTask:
    task = mgr.register("job", title=title, **kw)
    task.live_agent = SimpleNamespace(message_queue=MessageQueue())
    return task


def test_send_queues_a_steer_message_and_names_and_numbers_both_address() -> None:
    mgr = SubagentManager()
    task = _live(mgr, name="scout")
    agent = SimpleNamespace(subagents=mgr)
    for query in ("scout", "1", "#1", "sub-1", "@scout"):
        kind, found = user_message(agent, query, f"hello {query}")
        assert kind == "queued" and found is task
    assert [it.steer for it in task.live_agent.message_queue.items] == [True] * 5
    with pytest.raises(ToolError, match="No subagent 'nobody'"):
        user_message(agent, "nobody", "x")
    with pytest.raises(ToolError, match="empty"):
        user_message(agent, "1", "   ")


def test_take_messages_closes_the_queue_so_a_late_message_is_not_stranded() -> None:
    mgr = SubagentManager()
    task = _live(mgr)
    assert mgr.send("1", "first") is task
    assert mgr.take_messages(task.id) == ["first"]
    assert not task.closing                                  # it took one: another turn runs, still open
    assert mgr.take_messages(task.id) == []
    assert task.closing
    assert mgr.send("1", "too late") is None                 # refused: the caller resumes it instead


def test_user_message_resumes_a_finished_or_stopped_subagent(monkeypatch) -> None:
    from axon.agent import subagent as sa
    mgr = SubagentManager()
    task = _live(mgr)
    task.status, task.stopped = "cancelled", True
    task.conversation = Conversation([{"role": "user", "content": "job"}, {"role": "assistant", "content": "ok"}])
    started: list[tuple[str, object]] = []
    monkeypatch.setattr(sa, "spawn_background", lambda text, main, resume=None: started.append((text, resume)))
    kind, found = user_message(SimpleNamespace(subagents=mgr), "1", "carry on")
    assert kind == "resumed" and found is task and started == [("carry on", task)]
    assert any(e["text"] == "you: carry on" for e in task.timeline)


def test_user_message_says_when_there_is_nothing_to_resume() -> None:
    mgr = SubagentManager()
    task = _live(mgr)
    task.status = "error"
    with pytest.raises(ToolError, match="no saved history"):
        user_message(SimpleNamespace(subagents=mgr), "1", "x")
    keys = MonitorKeys(SimpleNamespace(subagents=mgr))
    keys.handle("enter", [task])
    keys.buf = "hello"
    keys.handle("enter", [task])
    assert keys.notice.startswith("✗") and "no saved history" in keys.notice


def test_console_refuses_to_open_beside_a_view_that_owns_the_screen(monkeypatch) -> None:
    from axon.agent.subagent import SCREEN_GATE
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    SCREEN_GATE.attach()
    try:
        assert not agent_panel.available()
    finally:
        SCREEN_GATE.detach()


def test_tasks_table_and_subagents_commands_without_a_terminal(capsys) -> None:
    from axon.commands.agent_cmds import _subagent_send, _subagent_stop
    mgr = SubagentManager()
    task = _live(mgr, name="scout")
    agent = SimpleNamespace(subagents=mgr)
    assert _subagent_send(agent, "scout", "hi").success is not False
    assert "Sent to sub-1" in capsys.readouterr().out
    assert _subagent_send(agent, "ghost", "hi").success is False
    task.status = "completed"
    _subagent_stop(agent, "scout")
    assert "not running" in strip_ansi(capsys.readouterr().out)
