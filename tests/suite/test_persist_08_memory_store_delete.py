"""Unit test for Deleting memory items from disk (memory_store_delete)."""
import pytest
from pathlib import Path
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.agent.memory import MemoryStore
from axon.agent.state import MessageQueue, QueuedMessage, TodoState

def test_memory_store_delete(workspace: Path):
    # Test Deleting memory items from disk
    store = SessionStore(workspace)
    assert store.active_session_id is not None
    ledger = Ledger()
    assert ledger.total() == 0

    mem = MemoryStore(workspace)
    item = mem.learn("Test convention for memory_store_delete")
    assert item.id is not None

    assert len(mem.list_all()) >= 1
    deleted = mem.delete(item.id)
    assert deleted is True

    from unittest.mock import MagicMock
    from axon.commands.builtin import handle_memory
    agent = MagicMock()
    agent.settings.workspace = workspace
    agent.provider = None

    # Test /memory add
    res_add = handle_memory(agent, "add Always run unit tests")
    assert res_add.handled is True
    assert len(mem.list_all()) == 1

    # Test /memory view
    res_view = handle_memory(agent, "view 1")
    assert res_view.handled is True

    # Test /memory list
    res_list = handle_memory(agent, "")
    assert res_list.handled is True

    # Test /memory delete
    res_del = handle_memory(agent, "delete 1")
    assert res_del.handled is True
    assert len(mem.list_all()) == 0
