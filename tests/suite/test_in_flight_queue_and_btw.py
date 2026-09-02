"""
Test suite for InFlightInputListener, /btw side inquiry, message queuing,
fixed bottom bar scroll region, and queue management during execution.
"""
from pathlib import Path
import pytest
from pydantic import SecretStr

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.agent.state import MessageQueue
from axon.commands.builtin import handle_btw
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools.registry import create_default_registry
from axon.ui.in_flight import InFlightInputListener
from axon.ui.scroll_region import FixedBottomBar


@pytest.fixture
def mock_agent(tmp_path: Path) -> Agent:
    settings = Settings(workspace=tmp_path, api_key=SecretStr("mock-key"), model="gpt-4o")
    return Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )


def test_handle_btw_empty(mock_agent: Agent):
    res = handle_btw(mock_agent, "")
    assert res.handled is True


def test_in_flight_listener_lifecycle(mock_agent: Agent):
    listener = InFlightInputListener(mock_agent)
    with listener:
        assert listener._stop_event.is_set() is False

    assert listener._stop_event.is_set() is True


def test_in_flight_queue_push(mock_agent: Agent):
    listener = InFlightInputListener(mock_agent)
    # 1. Explicit submission enqueues to message_queue
    item = mock_agent.message_queue.push("Next prompt in queue")
    assert len(mock_agent.message_queue) == 1
    assert item.text == "Next prompt in queue"

    # 2. Unsubmitted in-flight draft is safely discarded on exit to prevent ghost turn execution
    listener._buffer = list("unsubmitted draft")
    with listener:
        pass
    assert len(mock_agent.message_queue) == 1
    assert listener._buffer == []


def test_message_queue_remove_by_index():
    q = MessageQueue()
    q.push("First")
    q.push("Second")
    q.push("Third")
    assert len(q) == 3

    # Remove by 1-based index
    removed = q.remove_by_index(2)
    assert removed is not None
    assert removed.text == "Second"
    assert len(q) == 2

    # Invalid index returns None
    assert q.remove_by_index(0) is None
    assert q.remove_by_index(5) is None
    assert len(q) == 2


def test_message_queue_list_summary():
    q = MessageQueue()
    # Empty queue
    assert q.list_summary() == ""

    # Single item
    q.push("Fix the bug")
    assert "1 pending" in q.list_summary()
    assert "Fix the bug" in q.list_summary()

    # Multiple items
    q.push("Run tests")
    summary = q.list_summary()
    assert "2 pending" in summary
    assert "Fix the bug" in summary  # Shows next item

    # Long text truncation
    q2 = MessageQueue()
    q2.push("A" * 100)
    summary2 = q2.list_summary()
    assert "..." in summary2


def test_message_queue_remove_by_id():
    q = MessageQueue()
    item1 = q.push("First")
    item2 = q.push("Second")
    item3 = q.push("Third")

    # Remove by id
    assert q.remove(item2.id) is True
    assert len(q) == 2
    assert q.items[0].text == "First"
    assert q.items[1].text == "Third"

    # Remove non-existent id
    assert q.remove(999) is False
    assert len(q) == 2


def test_fixed_bottom_bar_non_tty():
    """FixedBottomBar works in non-TTY mode (no rendering, no crash)."""
    bar = FixedBottomBar(queue_summary="📥 Queue: 2 pending")
    # In non-TTY mode (e.g. CI), __enter__ still sets _active and
    # the bar methods execute without errors
    with bar:
        bar.set_content(status_text="test", prompt_text="test")
        bar.temporarily_clear()
        bar.restore()
    # Should not raise any exceptions


def test_fixed_bottom_bar_properties():
    """Test basic FixedBottomBar properties without terminal interaction."""
    bar = FixedBottomBar(queue_summary="📥 Queue: 5 pending")
    assert bar.is_active is False
    assert bar._queue_summary == "📥 Queue: 5 pending"

    bar.update_queue_summary("📥 Queue: 3 pending")
    assert bar._queue_summary == "📥 Queue: 3 pending"


def test_in_flight_queue_command_handling(mock_agent: Agent):
    """Test that queue commands are correctly parsed during in-flight execution."""
    listener = InFlightInputListener(mock_agent)

    # Setup queue
    mock_agent.message_queue.push("Question 1")
    mock_agent.message_queue.push("Question 2")
    assert len(mock_agent.message_queue) == 2

    # Test drop command
    item_id = mock_agent.message_queue.items[0].id
    listener._handle_queue_command(f"/q drop {item_id}")
    assert len(mock_agent.message_queue) == 1
    assert mock_agent.message_queue.items[0].text == "Question 2"

    # Test clear command
    mock_agent.message_queue.push("Question 3")
    listener._handle_queue_command("/q clear")
    assert len(mock_agent.message_queue) == 0

    # Test enqueue via /q <text>
    listener._handle_queue_command("/q New question from bar")
    assert len(mock_agent.message_queue) == 1
    assert mock_agent.message_queue.items[0].text == "New question from bar"


def test_in_flight_submit_line(mock_agent: Agent):
    """Test that _submit_line enqueues to message queue."""
    listener = InFlightInputListener(mock_agent)
    listener._submit_line("fast typed follow-up")
    assert len(mock_agent.message_queue) == 1
    assert mock_agent.message_queue.items[0].text == "fast typed follow-up"


def test_queued_slash_and_shell_commands(mock_agent: Agent):
    """Verify that queued commands like /cost, /clear, or !cmd dispatch correctly."""
    from axon.commands.builtin import handle_queue

    # 1. Queued /cost
    mock_agent.message_queue.push("/cost")
    res = handle_queue(mock_agent, "pop")
    assert res.handled is True
    assert len(mock_agent.message_queue) == 0

    # 2. Queued !echo hello
    mock_agent.message_queue.push("!echo test_queued_shell")
    res = handle_queue(mock_agent, "run")
    assert res.handled is True
    assert len(mock_agent.message_queue) == 0


def test_notify_focus_detection(monkeypatch):
    """Test is_terminal_window_focused and notify_if_unfocused behavior."""
    from axon.ui.notify import is_terminal_window_focused, notify_if_unfocused

    # Test when window is focused
    monkeypatch.setattr("axon.ui.notify.is_terminal_window_focused", lambda: True)
    assert notify_if_unfocused("Title", "Msg") is False

    # Test when window is unfocused (user in another window/app)
    monkeypatch.setattr("axon.ui.notify.is_terminal_window_focused", lambda: False)
    monkeypatch.setattr("axon.ui.notify.send_desktop_notification", lambda title, msg: True)
    assert notify_if_unfocused("Title", "Msg") is True

