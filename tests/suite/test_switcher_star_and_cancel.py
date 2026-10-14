"""
Unit tests for Switcher star visibility, star all, prompt cancellation, and /faq command registration.
"""
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from axon.session.store import SessionStore
from axon.ui.switcher import load_dashboard_sessions
from axon.ui.input import ALL_SLASH_COMMANDS
from axon.commands.builtin import dispatch_command, handle_faq


def test_session_store_star_and_star_all(tmp_path: Path):
    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    f1 = s_dir / "sess_1.jsonl"
    f1.write_text('{"type": "user_message", "data": {"content": "Task 1"}}\n')
    f2 = s_dir / "sess_2.jsonl"
    f2.write_text('{"type": "user_message", "data": {"content": "Task 2"}}\n')

    store = SessionStore(workspace=tmp_path, session_dir=s_dir)

    # Initially unstarred
    recent = store.list_recent()
    assert len(recent) == 2
    assert recent[0].is_starred is False
    assert recent[1].is_starred is False

    # Star single session
    st = store.star_session("sess_1")
    assert st is True
    recent = store.list_recent()
    s1 = next(s for s in recent if s.session_id == "sess_1")
    s2 = next(s for s in recent if s.session_id == "sess_2")
    assert s1.is_starred is True
    assert s2.is_starred is False

    # Star all sessions
    all_st = store.star_all_sessions()
    assert all_st is True
    recent = store.list_recent()
    assert all(s.is_starred for s in recent)

    # Star all again toggles to unstarred
    all_st_off = store.star_all_sessions()
    assert all_st_off is False
    recent = store.list_recent()
    assert all(not s.is_starred for s in recent)


def test_dashboard_sessions_star_loaded(tmp_path: Path):
    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    f1 = s_dir / "sess_star.jsonl"
    f1.write_text(
        '{"type": "user_message", "data": {"content": "Starred task"}}\n'
        '{"type": "session_star", "data": {"starred": true}}\n'
    )
    f2 = s_dir / "sess_plain.jsonl"
    f2.write_text('{"type": "user_message", "data": {"content": "Plain task"}}\n')

    dash_sessions = load_dashboard_sessions(tmp_path, active_id="sess_star", session_dir=s_dir)
    assert len(dash_sessions) == 2
    s_star = next(s for s in dash_sessions if s.id == "sess_star")
    s_plain = next(s for s in dash_sessions if s.id == "sess_plain")
    assert s_star.is_starred is True
    assert s_plain.is_starred is False


def test_slash_commands_include_faq_and_utilities():
    cmd_names = [cmd[0] for cmd in ALL_SLASH_COMMANDS]
    assert "/faq" in cmd_names
    assert "/pricing" in cmd_names
    assert "/delete" in cmd_names
    assert "/thinking" in cmd_names
    assert "/paste" in cmd_names


def test_faq_command_dispatch():
    mock_agent = MagicMock()
    # /faq overview
    res1 = dispatch_command(mock_agent, "/faq")
    assert res1.handled is True

    # /faq 402 topic
    res2 = dispatch_command(mock_agent, "/faq 402")
    assert res2.handled is True

    # /pricing route
    res3 = dispatch_command(mock_agent, "/pricing")
    assert res3.handled is True


def test_session_tag_update_and_removal(tmp_path: Path):
    """Verify tags can be changed, updated, and removed cleanly."""
    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    f1 = s_dir / "sess_tag.jsonl"
    f1.write_text('{"type": "user_message", "data": {"content": "Tagged work"}}\n')

    store = SessionStore(workspace=tmp_path, session_dir=s_dir)

    # 1. Tag session with 'work'
    store.tag_session("work", "sess_tag")
    recent = store.list_recent()
    assert recent[0].tags == ["work"]

    sessions = load_dashboard_sessions(tmp_path, active_id="sess_tag", session_dir=s_dir)
    assert sessions[0].tags == ["work"]

    # 2. Change tag to 'urgent'
    store.tag_session("urgent", "sess_tag")
    recent = store.list_recent()
    assert recent[0].tags == ["urgent"]

    sessions = load_dashboard_sessions(tmp_path, active_id="sess_tag", session_dir=s_dir)
    assert sessions[0].tags == ["urgent"]

    # 3. Remove tag using empty string or '-'
    store.tag_session("-", "sess_tag")
    recent = store.list_recent()
    assert recent[0].tags == []

    sessions = load_dashboard_sessions(tmp_path, active_id="sess_tag", session_dir=s_dir)
    assert sessions[0].tags == []


def test_active_session_preserves_tag_and_star_in_switcher(tmp_path: Path):
    """Verify that selecting a session does not cause its tags or star to disappear."""
    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    f1 = s_dir / "sess_active_tag.jsonl"
    f1.write_text(
        '{"type": "user_message", "data": {"content": "Tagged active chat"}}\n'
        '{"type": "session_tag", "data": {"tag": "SULLI"}}\n'
        '{"type": "session_star", "data": {"starred": true}}\n'
    )

    # When sess_active_tag is the active session
    sessions = load_dashboard_sessions(tmp_path, active_id="sess_active_tag", session_dir=s_dir)
    active_s = next(s for s in sessions if s.id == "sess_active_tag")
    assert active_s.is_current is True
    assert active_s.tags == ["SULLI"]
    assert active_s.is_starred is True

