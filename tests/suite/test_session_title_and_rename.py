"""
Unit tests for intelligent session titles, /rename command, and categorized time headers.
"""
from __future__ import annotations
import time
from datetime import datetime, timedelta
from pathlib import Path
from axon.session.store import SessionStore
from axon.ui.switcher import _clean_session_topic, categorize_session_time, load_dashboard_sessions

def test_clean_session_topic():
    # Markdown stripping
    assert _clean_session_topic("```python\ndef foo(): pass\n```\nFix authentication bug") == "Fix authentication bug"
    assert _clean_session_topic("`router.py` - add new endpoint") == "Add new endpoint"
    assert _clean_session_topic("# Header\n**Bold Text**") == "Header Bold Text"
    assert _clean_session_topic("!pytest tests/") == "Pytest tests/"
    assert _clean_session_topic("@src/axon/cli.py review this file") == "Src/axon/cli.py review this file"
    assert _clean_session_topic("1. Setup database migrations") == "Setup database migrations"

def test_categorize_session_time():
    now = time.time()
    assert categorize_session_time(now) == "Today"
    
    yesterday = (datetime.now() - timedelta(days=1)).timestamp()
    assert categorize_session_time(yesterday) == "Yesterday"
    
    four_days_ago = (datetime.now() - timedelta(days=4)).timestamp()
    assert categorize_session_time(four_days_ago) == "Previous 7 Days"
    
    two_weeks_ago = (datetime.now() - timedelta(days=14)).timestamp()
    assert categorize_session_time(two_weeks_ago) == "Older"

def test_session_rename_and_persistence(tmp_path: Path):
    s_dir = tmp_path / "sessions"
    store = SessionStore(workspace=tmp_path, session_dir=s_dir)
    
    # Append a prompt and turn
    store.append_user("Refactor database models to use SQLModel")
    
    # Check title before rename
    recent = store.list_recent()
    assert len(recent) == 1
    assert "Refactor database models" in recent[0].first_prompt
    
    # Rename session
    renamed = store.rename_session("Core DB Refactoring")
    assert renamed == "Core DB Refactoring"
    
    # Check title after rename
    recent_renamed = store.list_recent()
    assert recent_renamed[0].first_prompt == "Core DB Refactoring"
    
    # Test switcher loader
    dashboard_sessions = load_dashboard_sessions(tmp_path, store.active_session_id, session_dir=s_dir)
    assert len(dashboard_sessions) == 1
    assert dashboard_sessions[0].title == "Core DB Refactoring"

def test_empty_session_fallback_name(tmp_path: Path):
    s_dir = tmp_path / "sessions"
    store = SessionStore(workspace=tmp_path, session_dir=s_dir)
    
    # Create empty session
    store.append("session_start", {"model": "deepseek-v4-flash"})
    
    dashboard_sessions = load_dashboard_sessions(tmp_path, store.active_session_id, session_dir=s_dir)
    assert len(dashboard_sessions) == 1
    # Fallback should not contain raw ISO date format like 2026-08-31T
    assert "New Session (" in dashboard_sessions[0].title
    assert "T" not in dashboard_sessions[0].title.split("(")[1]
