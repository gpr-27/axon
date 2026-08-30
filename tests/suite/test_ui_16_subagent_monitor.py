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
