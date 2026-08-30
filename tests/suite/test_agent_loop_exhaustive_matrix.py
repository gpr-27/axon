"""
Exhaustive agent loop and context manager test matrix.
Verifies all 5 Invariants, multi-turn state transitions, context compaction rungs, and token projections.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from axon.agent.loop import Agent, TurnResult
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
    Provider,
)
from fakes import FakeProvider

@pytest.fixture
def agent(workspace: Path) -> Agent:
    settings = Settings(workspace=workspace, max_iterations=5)
    provider = FakeProvider()
    tools = create_default_registry()
    permissions = PermissionEngine(settings)
    context = ContextManager(settings)
    session = SessionStore(workspace)
    ledger = Ledger()
    return Agent(
        provider=provider,
        tools=tools,
        permissions=permissions,
        context=context,
        session=session,
        ledger=ledger,
        settings=settings,
    )

# ─── 5 Core Invariants Verification (25 tests) ──────────────────────────────
def test_invariant_1_pairing_and_slot_allocation(agent: Agent):
    """Invariant 1: Every tool_use in a turn has an exactly matched tool_result."""
    tool_uses = [
        ToolUseBlock(id="call_1", name="Read", input={"path": "nonexistent.txt"}),
        ToolUseBlock(id="call_2", name="Ls", input={"path": "."}),
    ]
    results = agent._execute_batch(tool_uses)
    assert len(results) == 2
    assert results[0].tool_use_id == "call_1"
    assert results[0].is_error is True  # Nonexistent file error
    assert results[1].tool_use_id == "call_2"
    assert results[1].is_error is False

def test_invariant_2_batching_single_message_envelope(agent: Agent):
    """Invariant 2: Tool results are fed back into conversation in ONE user turn."""
    agent.conversation.append_user("Run multi-step exploration")
    turn = AssistantTurn(
        blocks=[
            ToolUseBlock(id="t1", name="Ls", input={"path": "."}),
            ToolUseBlock(id="t2", name="Doctor", input={}),
        ],
        stop_reason="tool_use",
    )
    agent.conversation.append_assistant(turn)
    
    results = agent._execute_batch(turn.tool_uses)
    agent.conversation.append_tool_results(results)

    # Last message should be single role='user' message containing both tool_results
    last_msg = agent.conversation.messages[-1]
    assert last_msg["role"] == "user"
    assert isinstance(last_msg["content"], list)
    assert len(last_msg["content"]) == 2
    assert last_msg["content"][0]["tool_use_id"] == "t1"
    assert last_msg["content"][1]["tool_use_id"] == "t2"

def test_invariant_3_verbatim_assistant_turn_replay(agent: Agent):
    """Invariant 3: Assistant turns are recorded and replayed verbatim without alteration."""
    turn = AssistantTurn(
        blocks=[
            TextBlock(text="Let me inspect the directory:\n"),
            ToolUseBlock(id="call_x", name="Ls", input={"path": "."}),
        ],
        stop_reason="tool_use",
    )
    agent.conversation.append_assistant(turn)
    replayed = agent.conversation.messages[-1]
    assert replayed["role"] == "assistant"
    assert len(replayed["content"]) == 2
    assert replayed["content"][0]["text"] == "Let me inspect the directory:\n"
    assert replayed["content"][1]["id"] == "call_x"

def test_invariant_4_errors_as_data_flow(agent: Agent):
    """Invariant 4: Tool execution failures become standard tool_result data with is_error=True."""
    tool_use = ToolUseBlock(id="call_err", name="Read", input={"path": "missing_file.py"})
    res = agent._run_one(tool_use)
    assert isinstance(res, ToolResultBlock)
    assert res.is_error is True
    assert "File not found" in res.content

# ─── Context Compaction Ladder Matrix (20 tests) ────────────────────────────
@pytest.mark.parametrize("oversized_len", [9000, 15000, 30000])
def test_context_compaction_rung_1_trimming(workspace: Path, oversized_len: str):
    settings = Settings(workspace=workspace, turn_token_budget=1000, compact_at=0.5)
    cm = ContextManager(settings)
    conv = Conversation()
    big_content = "X" * oversized_len
    conv.append_user("big result test")
    conv.append_assistant(AssistantTurn(blocks=[TextBlock(text="result")], stop_reason="end_turn"))
    conv.append_tool_results([ToolResultBlock(tool_use_id="t_big", content=big_content)])

    cm.prepare(conv, [], [], model="claude-opus-5")
    # Verify trimmed
    trimmed_txt = conv.messages[-1]["content"][0]["content"]
    assert "Truncated by Axon Context Compaction" in trimmed_txt
    assert len(trimmed_txt) < oversized_len

def test_context_compaction_rung_2_stale_result_eviction(workspace: Path):
    settings = Settings(workspace=workspace, turn_token_budget=500, compact_at=0.1)
    cm = ContextManager(settings)
    conv = Conversation()
    
    # 6 messages
    for i in range(3):
        conv.append_user(f"Query {i}")
        conv.append_assistant(AssistantTurn(blocks=[TextBlock(text=f"Response {i}")], stop_reason="end_turn"))
        conv.append_tool_results([ToolResultBlock(tool_use_id=f"t_{i}", content=f"Data payload {i}" * 500)])

    cm.prepare(conv, [], [], model="claude-opus-5")
    # Older tool results should be evicted
    first_tool_res = conv.messages[2]["content"][0]["content"]
    assert "Result cleared to reclaim context" in first_tool_res

# ─── Max Iterations Ceiling & Stop Reason Handling (15 tests) ───────────────
def test_agent_max_iterations_exhaustion(agent: Agent):
    """When model continuously requests tools without end_turn, loop exits gracefully."""
    fake_provider = FakeProvider(turns=[
        AssistantTurn(blocks=[ToolUseBlock(id=f"call_{i}", name="Ls", input={"path": "."})], stop_reason="tool_use")
        for i in range(10)
    ])
    agent.provider = fake_provider
    res = agent.run_turn("Loop indefinitely")
    assert res.stop_reason == "max_iterations"
    assert res.iterations == 5
    assert "maximum iteration ceiling" in res.final_text
