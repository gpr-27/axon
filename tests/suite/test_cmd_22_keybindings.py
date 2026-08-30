"""Unit test for /keybindings command."""
import pytest
from pathlib import Path
from axon.commands.builtin import dispatch_command
from axon.agent.loop import Agent
from axon.tools import create_default_registry
from axon.agent.context import ContextManager
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.permissions.engine import PermissionEngine
from axon.config import Settings
from tests.fakes import FakeProvider, scripted

def test_cmd_keybindings(workspace: Path):
    settings = Settings(workspace=workspace)
    agent = Agent(
        provider=FakeProvider(scripted([], "ok")),
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(workspace),
        ledger=Ledger(),
        settings=settings,
    )
    res = dispatch_command("/keybindings", agent)
    assert res is not None and res.handled is True
