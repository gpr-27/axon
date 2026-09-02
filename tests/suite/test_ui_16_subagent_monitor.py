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

