"""Unit test for Saving persistent memory item to disk (memory_store_learn)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_memory_store_learn(workspace: Path):
    # Test Saving persistent memory item to disk
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for memory_store_learn")
    assert item.id is not None

    q = MessageQueue()
    q.push("prompt 1")
    assert len(q) == 1
    assert q.pop().text == "prompt 1"
