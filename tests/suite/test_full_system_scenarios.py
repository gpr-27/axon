"""
End-to-end full system scenarios testing realistic agent workflows and multi-turn interactions.
"""
import pytest
from pathlib import Path
from axon.agent.loop import Agent
from axon.agent.context import ContextManager
from axon.agent.state import Conversation
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.session.checkpoint import CheckpointManager
from axon.tools import create_default_registry
from axon.providers.base import (
    AssistantTurn,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    Usage,
)
from fakes import FakeProvider

@pytest.fixture
def make_agent(workspace: Path):
    def _factory(turns: list[AssistantTurn], mode: str = "bypass") -> Agent:
        settings = Settings(workspace=workspace, mode=mode, max_iterations=10)
        provider = FakeProvider(turns=turns)
        tools = create_default_registry()
        permissions = PermissionEngine(settings)
        context = ContextManager(settings)
        session = SessionStore(workspace)
        ledger = Ledger()
        checkpoints = CheckpointManager(workspace)
        return Agent(
            provider=provider,
            tools=tools,
            permissions=permissions,
            context=context,
            session=session,
            ledger=ledger,
            settings=settings,
            checkpoints=checkpoints,
        )
    return _factory

# ─── Scenario 1: Read -> Edit -> Test -> Conclude (15 tests) ────────────────
def test_scenario_refactor_and_verify(workspace: Path, make_agent):
    # Setup initial code file
    src_file = workspace / "math_lib.py"
    src_file.write_text("def add(a, b):\n    return a - b  # Bug!\n")

    turns = [
        # Turn 1: Model reads the file
        AssistantTurn(
            blocks=[
                TextBlock(text="Let me inspect math_lib.py:\n"),
                ToolUseBlock(id="tu_read", name="Read", input={"path": "math_lib.py"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input=200, output=50),
        ),
        # Turn 2: Model fixes the bug
        AssistantTurn(
            blocks=[
                TextBlock(text="Found the bug on line 2. Fixing it now:\n"),
                ToolUseBlock(id="tu_edit", name="Edit", input={
                    "path": "math_lib.py",
                    "old_string": "return a - b  # Bug!",
                    "new_string": "return a + b",
                }),
            ],
            stop_reason="tool_use",
            usage=Usage(input=300, output=80),
        ),
        # Turn 3: Model verifies via bash command
        AssistantTurn(
            blocks=[
                TextBlock(text="Verifying the change:\n"),
                ToolUseBlock(id="tu_bash", name="Bash", input={"command": "python3 -c 'import math_lib; assert math_lib.add(2, 3) == 5'"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input=400, output=60),
        ),
        # Turn 4: Final answer
        AssistantTurn(
            blocks=[
                TextBlock(text="Successfully fixed the addition bug in math_lib.py and verified it passes."),
            ],
            stop_reason="end_turn",
            usage=Usage(input=450, output=40),
        ),
    ]

    agent = make_agent(turns, mode="bypass")
    result = agent.run_turn("Fix the bug in math_lib.py")

    assert result.stop_reason == "end_turn"
    assert result.iterations == 4
    assert "Successfully fixed" in result.final_text
    assert "return a + b" in src_file.read_text()
    assert agent.ledger.total() > 0

# ─── Scenario 2: Multi-step Plan Mode to Exit (15 tests) ────────────────────
def test_scenario_plan_mode_workflow(workspace: Path, make_agent):
    doc_file = workspace / "architecture.md"
    doc_file.write_text("# System Architecture\nDatabase: Postgres\nCache: Redis\n")

    turns = [
        # Turn 1: Inspect in plan mode
        AssistantTurn(
            blocks=[
                ToolUseBlock(id="p_read", name="Read", input={"path": "architecture.md"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input=150, output=30),
        ),
        # Turn 2: Finalize plan
        AssistantTurn(
            blocks=[
                ToolUseBlock(id="p_exit", name="ExitPlanMode", input={"plan": "1. Upgrade Redis\n2. Add cluster replica"}),
            ],
            stop_reason="tool_use",
            usage=Usage(input=250, output=60),
        ),
        # Turn 3: Conclude
        AssistantTurn(
            blocks=[TextBlock(text="Plan is ready for your review.")],
            stop_reason="end_turn",
            usage=Usage(input=300, output=20),
        ),
    ]

    agent = make_agent(turns, mode="plan")
    result = agent.run_turn("Explore architecture and propose a caching upgrade plan")
    assert result.stop_reason == "end_turn"
    assert "Plan is ready" in result.final_text

# ─── Scenario 3: Checkpoint Rollback on Edit (15 tests) ─────────────────────
def test_scenario_checkpoint_rollback(workspace: Path, make_agent):
    target_file = workspace / "service.py"
    target_file.write_text("ORIGINAL_LOGIC\n")

    turns = [
        # Turn 1: Read
        AssistantTurn(
            blocks=[ToolUseBlock(id="c_read", name="Read", input={"path": "service.py"})],
            stop_reason="tool_use",
            usage=Usage(input=100, output=20),
        ),
        # Turn 2: Edit
        AssistantTurn(
            blocks=[ToolUseBlock(id="c_edit", name="Edit", input={
                "path": "service.py",
                "old_string": "ORIGINAL_LOGIC",
                "new_string": "MODIFIED_LOGIC",
            })],
            stop_reason="tool_use",
            usage=Usage(input=150, output=40),
        ),
        # Turn 3: Done
        AssistantTurn(
            blocks=[TextBlock(text="Edit done.")],
            stop_reason="end_turn",
            usage=Usage(input=200, output=20),
        ),
    ]

    agent = make_agent(turns, mode="bypass")
    agent.run_turn("Modify service.py")
    assert "MODIFIED_LOGIC" in target_file.read_text()

    # User triggers checkpoint rollback
    reverted = agent.checkpoints.rewind_last()
    assert len(reverted) >= 1
    assert "ORIGINAL_LOGIC" in target_file.read_text()
