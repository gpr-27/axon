"""
Test suite for InFlightInputListener, /btw side inquiry, and message queuing.
"""
from pathlib import Path
import pytest
from pydantic import SecretStr

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import handle_btw
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools.registry import create_default_registry
from axon.ui.in_flight import InFlightInputListener


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
    listener._buffer = list("Next prompt in queue")
    with listener:
        pass  # on exit, buffer is auto-pushed if non-empty

    assert len(mock_agent.message_queue) == 1
    assert mock_agent.message_queue.items[0].text == "Next prompt in queue"
