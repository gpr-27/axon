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

def test_ui_switcher_total_cost_and_deletion(tmp_path: Path):
    from axon.session.store import SessionStore
    from axon.ui.switcher import load_dashboard_sessions
    from axon.session.interactive import handle_session_delete
    from axon.commands.builtin import dispatch_command
    from unittest.mock import MagicMock

    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)

    f1 = s_dir / "test_switch_cost_1.jsonl"
    f1.write_text(
        '{"type": "user_message", "data": {"content": "Fix login bug"}}\n'
        '{"type": "assistant_turn", "data": {"text": "Fixed", "usage": {"input": 5000, "output": 1000}}}\n'
    )
    f2 = s_dir / "test_switch_cost_2.jsonl"
    f2.write_text(
        '{"type": "user_message", "data": {"content": "Optimize queries"}}\n'
        '{"type": "assistant_turn", "data": {"text": "Done", "usage": {"input": 2000, "output": 500}}}\n'
    )

    sessions = load_dashboard_sessions(tmp_path, active_id="test_switch_cost_1", session_dir=s_dir)
    assert len(sessions) == 2
    for s in sessions:
        assert s.total_cost > 0.0

    store = SessionStore(workspace=tmp_path, session_dir=s_dir)
    mock_agent = MagicMock()
    mock_agent.session = store

    # Test /sessions delete <id> command dispatch
    res = dispatch_command("/sessions delete test_switch_cost_2", mock_agent)
    assert res is not None and res.handled is True
    assert not f2.exists()
    assert len(store.list_recent()) == 1

    # Test /delete --all command dispatch
    res2 = dispatch_command("/delete --all", mock_agent)
    assert res2 is not None and res2.handled is True
    assert len(store.list_recent()) == 0
