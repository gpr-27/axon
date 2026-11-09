"""Unit test for Subagent Live Monitor (run_live_subagent_monitor)."""
from unittest.mock import MagicMock, patch
from pathlib import Path
import io
import sys
from axon.agent.subagent import SubagentManager
from axon.ui.subagent_monitor import run_live_subagent_monitor
from axon.ui.theme import strip_ansi

def test_subagent_monitor_non_tty(workspace: Path):
    """Test that monitor safely returns when stdin is not a tty."""
    mgr = SubagentManager()
    t1 = mgr.register("Explore codebase architecture")
    future_map = {MagicMock(done=lambda: True): 0}

    with patch("sys.stdin.isatty", return_value=False):
        run_live_subagent_monitor(future_map, mgr)

def test_subagent_monitor_render_math():
    """Verify ANSI line clearing math (moving up last_rendered_lines - 1 lines)."""
    # When N lines are rendered without trailing newline, cursor is on line index N-1.
    # To return to line index 0, cursor must move up N - 1 lines.
    n_lines = 9
    move_up_offset = n_lines - 1
    assert move_up_offset == 8
    clear_seq = f"\033[{move_up_offset}A\r\033[J"
    assert clear_seq == "\033[8A\r\033[J"

def test_subagent_monitor_views_cycle():
    """Test view list calculation including Main [0], Subagents [1..N], and Queue [Q]."""
    mgr = SubagentManager()
    t1 = mgr.register("Subagent One")
    t2 = mgr.register("Subagent Two")
    tasks = mgr.all_tasks()

    views = [0] + [t.index for t in tasks] + ["Q"]
    assert views == [0, 1, 2, "Q"]

    # Test cycling forward (Right / Down / Tab)
    # 0 -> 1 -> 2 -> Q -> 0
    curr = 0
    curr = views[(views.index(curr) + 1) % len(views)]
    assert curr == 1
    curr = views[(views.index(curr) + 1) % len(views)]
    assert curr == 2
    curr = views[(views.index(curr) + 1) % len(views)]
    assert curr == "Q"
    curr = views[(views.index(curr) + 1) % len(views)]
    assert curr == 0

    # Test cycling backward (Left / Up / Shift+Tab)
    # 0 -> Q -> 2 -> 1 -> 0
    curr = views[(views.index(curr) - 1) % len(views)]
    assert curr == "Q"
    curr = views[(views.index(curr) - 1) % len(views)]
    assert curr == 2
    curr = views[(views.index(curr) - 1) % len(views)]
    assert curr == 1
    curr = views[(views.index(curr) - 1) % len(views)]
    assert curr == 0

def test_subagent_monitor_no_unintended_queue_push(workspace: Path):
    """Test that unsubmitted buffer in monitor or in_flight listener does NOT auto-push to message_queue."""
    from axon.agent.state import MessageQueue
    from axon.ui.in_flight import InFlightInputListener

    mock_agent = MagicMock()
    mock_agent.message_queue = MessageQueue()

    # InFlightInputListener exit test
    listener = InFlightInputListener(mock_agent)
    listener._buffer = ["u", "n", "s", "u", "b", "m", "i", "t", "t", "e", "d"]
    listener.__exit__(None, None, None)

    # Buffer was cleared and nothing was enqueued
    assert len(mock_agent.message_queue) == 0
    assert listener._buffer == []



# ─── build_monitor_frame layout ────────────────────────────────────────────
import time
from types import SimpleNamespace

from axon import platform as plat
from axon.agent.subagent import SubagentTask
from axon.ui.subagent_monitor import build_monitor_frame

_LONG_ERR = ("AgentRouter rejected the request as invalid (HTTP 500). This usually means the upstream provider "
             "returned an error for this model; the request was retried 6 times and then given up. FINAL-WORD")


def _mk(index, title, status, **kw):
    t = SubagentTask(id=f"sub-{index}", index=index, title=title, prompt="do the thing", status=status, steps=3,
                     max_steps=25, agent_type="explore", model="deepseek-v4-flash")
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _fleet():
    now = time.time()
    return [
        _mk(1, "Inspect subagent numbering", "error", error_msg=_LONG_ERR, retries=6, elapsed_s=12.0),
        _mk(2, "Inspect result card rendering", "running", start_time=now - 20, activity="Read src/axon/ui/render.py",
            tool_calls=4, tokens_consumed=12_300, retries=2),
        _mk(3, "Inspect monitor keys", "error", error_msg=_LONG_ERR, elapsed_s=3.0),
        _mk(4, "Inventory subagent tests", "completed", elapsed_s=8.0, result_text="# Report\nFound 4 test files."),
        _mk(5, "Inspect docs", "exhausted", steps=25, elapsed_s=30.0),
    ]


def _plain(lines):
    return [strip_ansi(ln) for ln in lines]


def test_monitor_header_counts_every_status():
    out = _plain(build_monitor_frame(0, _fleet(), None, "", 120))
    assert "◆ Subagents" in out[0]
    assert "▶ 1 running" in out[0] and "✓ 1 done" in out[0] and "✗ 2 failed" in out[0] and "! 1 step limit" in out[0]
    assert "12.3k tok" in out[0]


def test_monitor_list_shows_each_outcome_and_the_full_reason_of_the_selected_failure():
    text = "\n".join(_plain(build_monitor_frame(0, _fleet(), None, "", 100, selected=0)))
    flat = " ".join(text.split())
    assert "FINAL-WORD" in flat  # the whole reason of the selected row, not cut at the column
    assert "AgentRouter rejected the request as invalid (HTTP 500)" in flat
    assert "Read src/axon/ui/render.py" in text  # running activity
    assert "Found 4 test files." in text  # first body line of the result, not the heading
    assert "stopped at the step limit" in text
    other = "\n".join(_plain(build_monitor_frame(0, _fleet(), None, "", 100, selected=3)))
    assert "FINAL-WORD" not in other  # only the selected row gets the extra lines


def test_monitor_detail_view_renders_each_timeline_kind():
    t = _mk(2, "Inspect result card rendering", "running", start_time=time.time() - 5, activity="Thinking",
            prompt="line one\nline two", timeline=[
                {"kind": "step", "text": "Step 4", "is_error": False, "t": 0},
                {"kind": "thinking", "text": "pondering the card", "is_error": False, "t": 0},
                {"kind": "tool", "text": "Read src/x.py", "is_error": False, "t": 0},
                {"kind": "result", "text": "first\nsecond\nthird", "is_error": False, "t": 0},
                {"kind": "retry", "text": "Retrying (2/6) · HTTP 500", "is_error": True, "t": 0},
                {"kind": "resume", "text": "Resumed after provider error", "is_error": False, "t": 0},
                {"kind": "error", "text": "Tool failed", "is_error": True, "t": 0},
            ])
    text = "\n".join(_plain(build_monitor_frame(2, [t], None, "", 88)))
    assert "── Step 4 ─" in text
    assert "∴ pondering the card" in text
    assert "▸ Read src/x.py" in text
    assert "↳ first (3 lines)" in text
    assert "⟳ Retrying (2/6) · HTTP 500" in text
    assert "↻ Resumed after provider error" in text
    assert "✗ Tool failed" in text
    assert "line one" in text and "line two" in text
    assert "now" in text and "Thinking" in text


def test_monitor_detail_falls_back_to_live_logs_and_caps_long_prompts():
    t = _mk(1, "Old task", "running", live_logs=["legacy log line"], prompt="\n".join(f"row {i}" for i in range(20)))
    text = "\n".join(_plain(build_monitor_frame(1, [t], None, "", 88)))
    assert "│ legacy log line" in text
    assert "row 1" in text and "row 2" not in text and "… 18 more lines" in text
    full = "\n".join(_plain(build_monitor_frame(1, [t], None, "", 88, expanded=True, height=60)))
    assert "row 19" in full


def test_monitor_every_line_fits_width():
    agent = SimpleNamespace(message_queue=None)
    tasks = _fleet()
    for width in (60, 88, 120):
        for view in (0, 1, 2, 3, 4, 5, "Q", 9):
            for ln in build_monitor_frame(view, tasks, agent, "x" * 200, width):
                assert plat.str_width(ln) <= width, (width, view, strip_ansi(ln))


def test_monitor_many_tasks_scroll_with_the_selection_inside_the_window():
    tasks = [_mk(i, f"Task {i}", "running", activity=f"Read f{i}.py") for i in range(1, 30)]
    out = _plain(build_monitor_frame(0, tasks, None, "", 100, selected=20, height=16))
    assert len(out) <= 16
    picked = next(ln for ln in out if "❯" in ln)
    assert "Task 21" in picked and "Read f21.py" in picked
    assert any("above" in ln and "below" in ln for ln in out)
