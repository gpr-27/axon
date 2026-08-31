"""Unit test for MessageQueue FIFO push and pop operations (queue_push_pop)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_queue_push_pop(workspace: Path):
    # Test MessageQueue FIFO push and pop operations
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for queue_push_pop")
    assert item.id is not None

    q = MessageQueue()
    q.push("prompt 1")
    assert len(q) == 1
    assert q.pop().text == "prompt 1"

    # Test /q and /queue via dispatch_command
    from unittest.mock import MagicMock
    from axon.commands.builtin import dispatch_command
    from axon.ui.input import ALL_SLASH_COMMANDS

    mock_agent = MagicMock()
    mock_agent.message_queue = MessageQueue()

    res_q = dispatch_command("/q Test follow-up task", mock_agent)
    assert res_q is not None and res_q.handled
    assert len(mock_agent.message_queue) == 1
    assert mock_agent.message_queue.items[0].text == "Test follow-up task"

    res_queue = dispatch_command("/queue Second task", mock_agent)
    assert res_queue is not None and res_queue.handled
    assert len(mock_agent.message_queue) == 2

    # Verify /q is present in ALL_SLASH_COMMANDS autocomplete list while redundant /queue is omitted
    slash_names = [c[0] for c in ALL_SLASH_COMMANDS]
    assert "/q" in slash_names
    assert "/queue" not in slash_names

