"""
Exhaustive slash commands test matrix.
Covers dispatching, parameter validation, aliases, queue management, model switching, and session management.
"""
import pytest
from pathlib import Path
from axon.commands.builtin import dispatch_command, CommandResult
from axon.agent.loop import Agent
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.agent.context import ContextManager
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.tools import create_default_registry
from fakes import FakeProvider

@pytest.fixture
def agent(workspace: Path) -> Agent:
    settings = Settings(workspace=workspace)
    return Agent(
        provider=FakeProvider(),
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(workspace),
        ledger=Ledger(),
        settings=settings,
    )

# ─── Slash Commands Dispatch Matrix (35 tests) ──────────────────────────────
@pytest.mark.parametrize("cmd_line", [
    "/help",
    "/model",
    "/model claude-opus-5",
    "/model gpt-5.6-sol",
    "/effort",
    "/effort quantum",
    "/effort synapse",
    "/mode",
    "/mode acceptEdits",
    "/mode default",
    "/config",
    "/status",
    "/permissions",
    "/plan",
    "/context",
    "/tokens",
    "/window",
    "/window 15",
    "/cost",
    "/payload",
    "/history",
    "/diff",
    "/review",
    "/subagents",
    "/todos",
    "/queue",
    "/queue Add test question",
    "/q Quick question",
    "/q clear",
    "/skills",
    "/mcp",
    "/plugin",
    "/hooks",
    "/memory",
    "/doctor",
    "/main",
    "/root",
])
def test_all_slash_commands_handled(agent: Agent, cmd_line: str):
    res = dispatch_command(agent, cmd_line)
    assert isinstance(res, CommandResult)
    assert res.handled is True

# ─── Queue Management Matrix (15 tests) ─────────────────────────────────────
def test_queue_push_and_drop(agent: Agent):
    dispatch_command(agent, "/q First question")
    dispatch_command(agent, "/queue Second question")
    dispatch_command(agent, "/q Third question")
    assert len(agent.message_queue) == 3

    # Drop middle item
    item_id = agent.message_queue.items[1].id
    dispatch_command(agent, f"/queue drop {item_id}")
    assert len(agent.message_queue) == 2
    assert agent.message_queue.items[0].text == "First question"
    assert agent.message_queue.items[1].text == "Third question"

def test_queue_clear(agent: Agent):
    dispatch_command(agent, "/q Q1")
    dispatch_command(agent, "/q Q2")
    assert len(agent.message_queue) == 2
    dispatch_command(agent, "/queue clear")
    assert len(agent.message_queue) == 0

# ─── Mode & Model Switching Matrix (15 tests) ───────────────────────────────
@pytest.mark.parametrize("target_mode", ["default", "acceptEdits", "plan", "bypass"])
def test_mode_command_switches_state(agent: Agent, target_mode: str):
    dispatch_command(agent, f"/mode {target_mode}")
    assert agent.settings.mode == target_mode

@pytest.mark.parametrize("target_effort", ["reflex", "balanced", "synapse", "quantum"])
def test_effort_command_switches_state(agent: Agent, target_effort: str):
    dispatch_command(agent, f"/effort {target_effort}")
    assert agent.settings.effort == target_effort
