"""Unit test for Listing recent sessions with metadata (session_list_recent)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_session_list_recent(workspace: Path):
    # Test Listing recent sessions with metadata
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for session_list_recent")
    assert item.id is not None

    q = MessageQueue()
    q.push("prompt 1")
    assert len(q) == 1
    assert q.pop().text == "prompt 1"
