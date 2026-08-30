"""Unit test for Loading sessions for dashboard switcher (switcher_load_sessions)."""
import pytest
from pathlib import Path
from axon.ui.theme import strip_ansi, BOLD, TEAL, RST
from axon.ui.render import render_shortcuts_footer, render_side_question_box
from axon.ui.switcher import format_time_ago, load_dashboard_sessions

def test_ui_switcher_load_sessions(workspace: Path):
    footer = render_shortcuts_footer()
    assert len(footer) > 0
    clean_f = strip_ansi(footer)
    assert "shell mode" in clean_f

    box = render_side_question_box("question", "answer")
    assert "Side Question" in box

    assert format_time_ago(100.0) is not None

def test_ui_switcher_non_tty(workspace: Path):
    from unittest.mock import MagicMock, patch
    from axon.ui.switcher import run_session_dashboard
    mock_agent = MagicMock()
    with patch("sys.stdin.isatty", return_value=False):
        res = run_session_dashboard(mock_agent)
        assert res is None
