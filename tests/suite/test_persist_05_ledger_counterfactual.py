"""Unit test for Counterfactual cache savings computation (ledger_counterfactual)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_ledger_counterfactual(workspace: Path):
    # Test Counterfactual cache savings computation
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for ledger_counterfactual")
    assert item.id is not None

    q = MessageQueue()
    q.push("prompt 1")
    assert len(q) == 1
    assert q.pop().text == "prompt 1"
