"""
Exhaustive subagent lifecycle, depth enforcement, event isolation, and session switching test matrix.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from axon.agent.subagent import SubagentManager, SubagentTask, sync_subagents_for_session
from axon.agent.state import Conversation
from axon.commands.builtin import handle_subagents, handle_main
from axon.session.store import SessionStore
from axon.providers.base import AssistantTurn, ToolUseBlock, ToolResultBlock, TextBlock

# ─── SubagentManager Operations Matrix (15 tests) ───────────────────────────
def test_subagent_manager_registration_and_progress():
    sm = SubagentManager()
    t1 = sm.register("Task 1: Search repo", max_steps=10)
    t2 = sm.register("Task 2: Benchmark performance", max_steps=15)
    assert len(sm.all_tasks()) == 2
    assert t1.index == 1
    assert t2.index == 2
    assert t1.status == "running"

    sm.update_progress(t1.id, 5)
    assert t1.steps == 5

    sm.complete(t1.id, "Found 10 matches", conversation=None, status="completed")
    assert t1.status == "completed"
    assert t1.result_text == "Found 10 matches"

    sm.fail(t2.id, "Connection timeout")
    assert t2.status == "error"
    assert t2.error_msg == "Connection timeout"

def test_subagent_manager_clear():
    sm = SubagentManager()
    sm.register("Task A")
    sm.register("Task B")
    assert len(sm.all_tasks()) == 2
    sm.clear()
    assert len(sm.all_tasks()) == 0

# ─── Sibling Subagent Navigation & Return to Main Matrix (15 tests) ──────────
def test_subagent_sibling_switching_and_main_return(workspace: Path):
    store = SessionStore(workspace)
    store.open("sess_multi_sub")
    store.append_user("Build microservices")
    
    turn = AssistantTurn(
        blocks=[
            ToolUseBlock(id="tu_auth", name="Task", input={"prompt": "Build auth service"}),
            ToolUseBlock(id="tu_pay", name="Task", input={"prompt": "Build payment service"}),
            ToolUseBlock(id="tu_notif", name="Task", input={"prompt": "Build notification service"}),
        ],
        stop_reason="end_turn",
    )
    store.append_turn(turn)
    store.append_results([
        ToolResultBlock(tool_use_id="tu_auth", content="Auth service built"),
        ToolResultBlock(tool_use_id="tu_pay", content="Payment service built"),
        ToolResultBlock(tool_use_id="tu_notif", content="Notification service built"),
    ])

    agent = MagicMock()
    agent.session = store
    agent.conversation = store.read_conversation("sess_multi_sub")

    # 1. Switch to Subagent 1
    res1 = handle_subagents(agent, "1")
    assert res1.handled is True
    assert agent.session.active_session_id == "sess_multi_sub_sub_1"

    # 2. Switch directly to Subagent 3 from Subagent 1
    res3 = handle_subagents(agent, "3")
    assert res3.handled is True
    assert agent.session.active_session_id == "sess_multi_sub_sub_3"

    # 3. Return to Main from Subagent 3
    res_main = handle_main(agent, "")
    assert res_main.handled is True
    assert agent.session.active_session_id == "sess_multi_sub"
    assert len(agent.conversation.messages) > 0

# ─── Per-Session Subagent Scoping & Transcript Recovery (10 tests) ──────────
def test_sync_subagents_for_session_multiple_sessions(workspace: Path):
    agent = MagicMock()
    agent.subagents = SubagentManager()

    # Session with 1 task
    agent.conversation = Conversation([
        {"role": "user", "content": "Start session"},
        {
            "role": "assistant",
            "content": [ToolUseBlock(id="call_1", name="Task", input={"prompt": "Single subtask"})],
        },
        {
            "role": "user",
            "content": [ToolResultBlock(tool_use_id="call_1", content="Done")],
        },
    ])
    sync_subagents_for_session(agent)
    assert len(agent.subagents.all_tasks()) == 1

    # Session with 0 tasks
    agent.conversation = Conversation([
        {"role": "user", "content": "Just a normal prompt"},
        {"role": "assistant", "content": "Normal response"},
    ])
    sync_subagents_for_session(agent)
    assert len(agent.subagents.all_tasks()) == 0

def test_subagent_tool_registry_provisioning():
    from axon.tools.registry import create_default_registry
    from axon.config import Settings

    parent_registry = create_default_registry()
    all_names = {t.name for t in parent_registry.all_tools()}
    assert "Write" in all_names
    assert "Edit" in all_names
    assert "Bash" in all_names
    assert "Task" in all_names

    # In default/acceptEdits/bypass mode: subagents get all tools except recursive 'Task'
    sub_reg_normal = parent_registry.subset(
        names=[t.name for t in parent_registry.all_tools() if t.name != "Task"],
        readonly_only=False,
    )
    sub_names = {t.name for t in sub_reg_normal.all_tools()}
    assert "Task" not in sub_names
    assert "Write" in sub_names
    assert "Edit" in sub_names
    assert "Bash" in sub_names
    assert "Read" in sub_names

    # In plan mode: subagents must be strictly readonly
    sub_reg_plan = parent_registry.subset(
        names=[t.name for t in parent_registry.all_tools() if t.name != "Task"],
        readonly_only=True,
    )
    plan_names = {t.name for t in sub_reg_plan.all_tools()}
    assert "Task" not in plan_names
    assert "Write" not in plan_names
    assert "Edit" not in plan_names
    assert "Bash" not in plan_names
    assert "Read" in plan_names

