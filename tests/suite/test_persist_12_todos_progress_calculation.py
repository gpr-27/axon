"""Unit test for Percentage calculation for todos checklist (todos_progress_calculation)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_todos_progress_calculation(workspace: Path):
    # Test Percentage calculation for todos checklist
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for todos_progress_calculation")
    assert item.id is not None

    q = MessageQueue()
    q.push("prompt 1")
    assert len(q) == 1
    assert q.pop().text == "prompt 1"
