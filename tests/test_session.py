"""
Tests for Session store and Ledger accounting.
"""
from pathlib import Path
from decimal import Decimal
from axon.session.store import SessionStore
from axon.session.ledger import Ledger
from axon.providers.base import Usage

def test_session_store_append_and_load(workspace: Path):
    store = SessionStore(workspace=workspace)
    sid = store.open("test_session_123")

    store.append_user("hello agent")
    from axon.providers.base import AssistantTurn, TextBlock
    turn = AssistantTurn(blocks=[TextBlock(text="hello human")], stop_reason="end_turn")
    store.append_turn(turn)

    # Reconstruct
    loaded_conv = store.load("test_session_123")
    assert len(loaded_conv.messages) == 2
    assert loaded_conv.messages[0]["content"] == "hello agent"
    # Assistant turn reconstructed as structured blocks or text
    asst_content = loaded_conv.messages[1]["content"]
    assert asst_content == [{"type": "text", "text": "hello human"}] or asst_content == "hello human"

def test_ledger_cost_and_counterfactual():
    ledger = Ledger()
    usage = Usage(input=10000, output=1000, cache_read=8000)
    cost = ledger.record("claude-opus-5", usage)
    assert cost > Decimal("0.0")

    counterfactual = ledger.uncached_counterfactual("claude-opus-5")
    assert counterfactual > cost
    assert ledger.total() == cost

def test_dashboard_sessions_and_time_formatting(workspace: Path):
    from axon.ui.switcher import format_time_ago, load_dashboard_sessions
    import time

    # Time formatting tests
    assert format_time_ago(time.time() - 20) == "20s"
    assert format_time_ago(time.time() - 180) == "3m"
    assert format_time_ago(time.time() - 7200) == "2h"
    assert format_time_ago(time.time() - 86400 * 5) == "5d"

    # Create dummy sessions
    store = SessionStore(workspace=workspace)
    store.open("sess_a")
    store.append_user("Build a REST API in FastAPI")
    
    store.open("sess_b")
    store.append_user("Binary search FIA in C++")

    sessions = load_dashboard_sessions(workspace, active_id="sess_b")
    assert len(sessions) >= 2
    titles = [s.title for s in sessions]
    assert any("REST API" in t or "sess_a" in t for t in titles)
    assert any("Binary search" in t for t in titles)

def test_unlimited_session_loading_and_token_metrics(workspace: Path):
    from axon.ui.switcher import load_dashboard_sessions
    from axon.session.interactive import render_restored_conversation
    from axon.providers.base import AssistantTurn, TextBlock
    import io
    import sys

    store = SessionStore(workspace=workspace)
    # Create 20 distinct sessions
    for i in range(20):
        sid = f"test_bulk_sess_{i:02d}"
        store.open(sid)
        store.append_user(f"Task number {i}")
        turn = AssistantTurn(
            blocks=[TextBlock(text=f"Response for task {i}")],
            stop_reason="end_turn",
            usage=Usage(input=100 * (i + 1), output=50 * (i + 1)),
        )
        store.append_turn(turn)

    # Verify list_recent loads all 20 sessions and computes total_tokens
    recent = store.list_recent(limit=None)
    assert len(recent) >= 20
    assert any(m.total_tokens > 0 for m in recent)

    # Verify load_dashboard_sessions loads all sessions without truncating at 15
    dashboard_sessions = load_dashboard_sessions(workspace, active_id="test_bulk_sess_19")
    assert len(dashboard_sessions) >= 20
    assert any(s.total_tokens > 0 for s in dashboard_sessions)

    # Verify render_restored_conversation includes tokens and cost
    conv = store.load("test_bulk_sess_05")
    ledger = store.load_ledger("test_bulk_sess_05", "claude-3-7-sonnet-20250219")
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        render_restored_conversation(conv, "test_bulk_sess_05", ledger=ledger)
    finally:
        sys.stdout = old_stdout

    rendered_text = buf.getvalue()
    assert "Restored Chat Session" in rendered_text
    assert "tokens" in rendered_text
    assert "Total Cost" in rendered_text

def test_active_session_synchronization_and_re_render(workspace: Path):
    from unittest.mock import MagicMock
    from axon.session.interactive import render_restored_conversation
    from axon.providers.base import AssistantTurn, TextBlock
    import io
    import sys

    store = SessionStore(workspace=workspace)
    s1 = store.open("session_active_test_1")
    store.append_user("Original chat message in active session")
    turn = AssistantTurn(blocks=[TextBlock(text="Original assistant response")], stop_reason="end_turn")
    store.append_turn(turn)

    # When session is loaded and restored via handle_resume or switcher, open() is invoked
    assert store.active_session_id == "session_active_test_1"
    store.open("session_active_test_2")
    assert store.active_session_id == "session_active_test_2"
    assert store.active_file.name == "session_active_test_2.jsonl"

    # Verify that returning to the current active session properly renders without disappearing
    conv = store.load("session_active_test_1")
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        render_restored_conversation(conv, "session_active_test_1")
    finally:
        sys.stdout = old_stdout

    out = buf.getvalue()
    assert "Original chat message in active session" in out
    assert "Original assistant response" in out

def test_switcher_new_session_flow(workspace: Path):
    store = SessionStore(workspace=workspace)
    s1 = store.open()
    store.append_user("Existing task")
    assert store.active_session_id == s1

    # Starting a new session from switcher opens a fresh session
    new_s = store.open()
    assert new_s != s1
    assert store.active_session_id == new_s
    assert store.active_file.name == f"{new_s}.jsonl"

def test_subagent_per_session_synchronization(workspace: Path):
    from unittest.mock import MagicMock
    from axon.agent.state import Conversation
    from axon.agent.subagent import SubagentManager, sync_subagents_for_session

    mock_agent = MagicMock()
    mock_agent.subagents = SubagentManager()
    
    # Session 1 with 2 subagent tasks recorded in conversation
    mock_agent.conversation = Conversation([
        {"role": "user", "content": "Analyze project architecture"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "Task", "input": {"prompt": "Inspect database models"}},
                {"type": "tool_use", "id": "call_2", "name": "Task", "input": {"prompt": "Review auth flow"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "Found 5 models"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "Auth uses JWT"},
            ],
        },
    ])

    sync_subagents_for_session(mock_agent)
    assert len(mock_agent.subagents.all_tasks()) == 2
    assert mock_agent.subagents.all_tasks()[0].title == "Inspect database models"
    assert mock_agent.subagents.all_tasks()[1].title == "Review auth flow"

    # Switching to Session 2 (fresh or empty) clears subagents
    mock_agent.conversation = Conversation([
        {"role": "user", "content": "Hello in session 2"},
        {"role": "assistant", "content": "Hello! How can I help?"},
    ])
    sync_subagents_for_session(mock_agent)
    assert len(mock_agent.subagents.all_tasks()) == 0

def test_subagent_command_launches_chat(workspace: Path):
    from unittest.mock import MagicMock, patch
    from axon.agent.state import Conversation
    from axon.agent.subagent import SubagentManager
    from axon.commands.builtin import handle_subagents
    from axon.session.store import SessionStore

    store = SessionStore(workspace=workspace)
    store.open("parent_sess_100")
    store.append_user("Parent message")

    mock_agent = MagicMock()
    mock_agent.session = store
    mock_agent.subagents = SubagentManager()
    task = mock_agent.subagents.register("Investigate memory leak")
    task.status = "completed"
    task.result_text = "Found 2 cyclic references"
    mock_agent.conversation = Conversation([{"role": "user", "content": "Parent message"}])

    # Run handle_subagents with argument "1"
    res = handle_subagents(mock_agent, "1")
    assert res.handled is True
    # Verify subagent session was opened
    assert "parent_sess_100_sub_1" in mock_agent.session.active_session_id
    assert len(mock_agent.conversation.messages) >= 2
    assert mock_agent.conversation.messages[0]["content"] == "Investigate memory leak"
    assert mock_agent.conversation.messages[1]["content"] == "Found 2 cyclic references"

def test_handle_main_and_nested_subagents(workspace: Path):
    from unittest.mock import MagicMock
    from axon.agent.state import Conversation
    from axon.commands.builtin import handle_main, handle_subagents
    from axon.session.store import SessionStore

    store = SessionStore(workspace=workspace)
    # Create parent session
    store.open("parent_sess_200")
    store.append_user("Analyze distributed cache")
    from axon.providers.base import AssistantTurn, ToolUseBlock, ToolResultBlock, TextBlock
    turn = AssistantTurn(
        blocks=[
            ToolUseBlock(id="call_t1", name="Task", input={"prompt": "Benchmark Redis"}),
            ToolUseBlock(id="call_t2", name="Task", input={"prompt": "Benchmark Memcached"}),
        ],
        stop_reason="end_turn",
    )
    store.append_turn(turn)
    store.append_results([
        ToolResultBlock(tool_use_id="call_t1", content="Redis throughput 120k ops/sec"),
        ToolResultBlock(tool_use_id="call_t2", content="Memcached throughput 140k ops/sec"),
    ])

    mock_agent = MagicMock()
    mock_agent.session = store
    mock_agent.conversation = store.load("parent_sess_200")

    # 1. Open subagent 1 chat
    res_sub1 = handle_subagents(mock_agent, "1")
    assert res_sub1.handled is True
    assert mock_agent.session.active_session_id == "parent_sess_200_sub_1"

    # 2. While in subagent 1, switch directly to sibling subagent 2
    res_sub2 = handle_subagents(mock_agent, "2")
    assert res_sub2.handled is True
    assert mock_agent.session.active_session_id == "parent_sess_200_sub_2"

    # 3. While in subagent 2, run /main to return to parent session
    res_main = handle_main(mock_agent, "")
    assert res_main.handled is True
    assert mock_agent.session.active_session_id == "parent_sess_200"
    assert len(mock_agent.conversation.messages) > 0

    # Verify both subagents are preserved and discoverable after returning to main
    assert len(mock_agent.subagents.all_tasks()) == 2
    assert mock_agent.subagents.all_tasks()[0].index == 1
    assert mock_agent.subagents.all_tasks()[1].index == 2

def test_openai_tool_calls_subagent_sync_and_disk_discovery(workspace: Path):
    from unittest.mock import MagicMock
    from axon.agent.state import Conversation
    from axon.agent.subagent import sync_subagents_for_session
    from axon.commands.builtin import handle_main, handle_subagents
    from axon.providers.base import ToolResultBlock
    from axon.session.store import SessionStore

    store = SessionStore(workspace=workspace)
    store.open("sess_openai_multi")
    store.append_user("Run multi-agent exploration")

    # Native OpenAI tool call format in conversation messages
    turn_native = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_sub1",
                "type": "function",
                "function": {"name": "Task", "arguments": '{"prompt": "Audit database indexes"}'},
            },
            {
                "id": "call_sub2",
                "type": "function",
                "function": {"name": "Task", "arguments": '{"prompt": "Inspect connection pool"}'},
            },
        ],
    }
    store.append("assistant_turn", {"native": turn_native, "tool_uses": [], "text": ""})
    store.append_results([
        ToolResultBlock(tool_use_id="call_sub1", content="3 unused indexes found"),
        ToolResultBlock(tool_use_id="call_sub2", content="Pool size optimal"),
    ])
    mock_agent = MagicMock()
    mock_agent.session = store
    mock_agent.conversation = store.load("sess_openai_multi")

    # Sync subagents from OpenAI conversation structure
    sync_subagents_for_session(mock_agent)
    tasks = mock_agent.subagents.all_tasks()
    assert len(tasks) == 2
    assert tasks[0].index == 1
    assert "Audit database indexes" in tasks[0].prompt
    assert tasks[1].index == 2
    assert "Inspect connection pool" in tasks[1].prompt

    # Open subagent 1, then subagent 2, then /main
    handle_subagents(mock_agent, "1")
    assert mock_agent.session.active_session_id == "sess_openai_multi_sub_1"

    handle_subagents(mock_agent, "2")
    assert mock_agent.session.active_session_id == "sess_openai_multi_sub_2"

    handle_main(mock_agent, "")
    assert mock_agent.session.active_session_id == "sess_openai_multi"
    assert len(mock_agent.subagents.all_tasks()) == 2

def test_subagent_separate_and_combined_cost_accounting(workspace: Path):
    from unittest.mock import MagicMock
    from axon.agent.state import Conversation
    from axon.agent.subagent import sync_subagents_for_session
    from axon.commands.builtin import handle_cost, handle_main, handle_subagents
    from axon.providers.base import AssistantTurn, TextBlock, ToolResultBlock, ToolUseBlock, Usage
    from axon.session.store import SessionStore

    store = SessionStore(workspace=workspace)
    parent_sess = "sess_cost_demo"
    store.open(parent_sess)
    store.append_user("Optimize database performance")

    # Main agent turn with 2 subtasks
    turn_main = AssistantTurn(
        blocks=[
            ToolUseBlock(id="tu_1", name="Task", input={"prompt": "Profile slow queries"}),
            ToolUseBlock(id="tu_2", name="Task", input={"prompt": "Analyze table locks"}),
        ],
        usage=Usage(input=5000, output=200),
    )
    store.append_turn(turn_main)
    store.append_results([
        ToolResultBlock(tool_use_id="tu_1", content="Found 3 unindexed queries"),
        ToolResultBlock(tool_use_id="tu_2", content="Zero deadlocks observed"),
    ])

    # Record independent subagent 1 session
    store.open(f"{parent_sess}_sub_1")
    store.append_user("[Subagent Research Task]: Profile slow queries")
    store.append_turn(AssistantTurn(
        blocks=[TextBlock(text="Found 3 unindexed queries")],
        usage=Usage(input=14000, output=500),
    ))

    # Record independent subagent 2 session
    store.open(f"{parent_sess}_sub_2")
    store.append_user("[Subagent Research Task]: Analyze table locks")
    store.append_turn(AssistantTurn(
        blocks=[TextBlock(text="Zero deadlocks observed")],
        usage=Usage(input=21000, output=600),
    ))

    # Reopen parent session
    store.open(parent_sess)
    mock_agent = MagicMock()
    mock_agent.session = store
    mock_agent.settings.model = "claude-opus-5"
    mock_agent.conversation = store.load(parent_sess)

    # Sync subagents
    sync_subagents_for_session(mock_agent)
    tasks = mock_agent.subagents.all_tasks()
    assert len(tasks) == 2
    assert tasks[0].input_tokens == 14000
    assert tasks[0].output_tokens == 500
    assert tasks[1].input_tokens == 21000
    assert tasks[1].output_tokens == 600

    # 1. Switch to Subagent 1: should show Subagent 1's isolated usage (14,500 tokens)
    res1 = handle_subagents(mock_agent, "1")
    assert res1.handled is True
    assert mock_agent.session.active_session_id == f"{parent_sess}_sub_1"
    assert mock_agent.ledger.total_input_tokens == 14000
    assert mock_agent.ledger.total_output_tokens == 500
    assert float(mock_agent.ledger.total_cost) > 0.0

    # 2. Switch to Subagent 2: should show Subagent 2's isolated usage (21,600 tokens)
    res2 = handle_subagents(mock_agent, "2")
    assert res2.handled is True
    assert mock_agent.session.active_session_id == f"{parent_sess}_sub_2"
    assert mock_agent.ledger.total_input_tokens == 21000
    assert mock_agent.ledger.total_output_tokens == 600
    assert float(mock_agent.ledger.total_cost) > 0.0

    # 3. Return to Main: combined ledger includes Main (5,200) + Sub1 (14,500) + Sub2 (21,600) = 41,300 tokens
    res_m = handle_main(mock_agent, "")
    assert res_m.handled is True
    assert mock_agent.session.active_session_id == parent_sess
    total_tokens = mock_agent.ledger.total_input_tokens + mock_agent.ledger.total_output_tokens
    assert total_tokens == (5000 + 200) + (14000 + 500) + (21000 + 600)
    assert float(mock_agent.ledger.total_cost) > 0.0

    # 4. Running handle_cost outputs main + subagent breakdown + combined total
    res_cost = handle_cost(mock_agent, "")
    assert res_cost.handled is True

def test_dashboard_and_store_filter_out_subagent_sessions(tmp_path: Path) -> None:
    """Verify that sub-agent session files (_sub_*) are excluded from dashboard and list_recent."""
    from axon.session.store import SessionStore
    from axon.ui.switcher import load_dashboard_sessions

    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)

    # Create main sessions
    (s_dir / "session_1001.jsonl").write_text('{"type": "user_message", "data": {"content": "Main task 1"}}\n')
    (s_dir / "session_1002.jsonl").write_text('{"type": "user_message", "data": {"content": "Main task 2"}}\n')
    # Create subagent sessions
    (s_dir / "session_1001_sub_1.jsonl").write_text('{"type": "user_message", "data": {"content": "Sub task 1"}}\n')
    (s_dir / "session_1001_sub_2.jsonl").write_text('{"type": "user_message", "data": {"content": "Sub task 2"}}\n')
    (s_dir / "session_1002_sub_1.jsonl").write_text('{"type": "user_message", "data": {"content": "Sub task 3"}}\n')

    # 1. SessionStore list_recent should only return the 2 main sessions
    store = SessionStore(workspace=tmp_path, session_dir=s_dir)
    recent = store.list_recent()
    recent_ids = [m.session_id for m in recent]
    assert len(recent_ids) == 2
    assert "session_1001" in recent_ids
    assert "session_1002" in recent_ids
    assert not any("_sub_" in sid for sid in recent_ids)

    # 2. load_dashboard_sessions should only return the 2 main sessions
    dash_sessions = load_dashboard_sessions(tmp_path, "session_1001", session_dir=s_dir)
    dash_ids = [s.id for s in dash_sessions]
    assert len(dash_ids) == 2
    assert "session_1001" in dash_ids
    assert "session_1002" in dash_ids
    assert not any("_sub_" in sid for sid in dash_ids)

def test_workspace_lifetime_ledger_incorporates_subagents_and_counts_normal_chats(tmp_path: Path) -> None:
    """Verify that load_workspace_ledger incorporates subagent tokens/costs and counts only normal chats."""
    from axon.session.store import SessionStore

    s_dir = tmp_path / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)

    # Chat 1: Main agent (1,000 in, 100 out) + Subagent 1 (2,000 in, 200 out)
    f1 = s_dir / "session_1.jsonl"
    f1.write_text(
        '{"type": "user_message", "data": {"content": "Task 1"}}\n'
        '{"type": "assistant_turn", "data": {"text": "Done", "usage": {"input": 1000, "output": 100}}}\n'
    )
    f1_sub = s_dir / "session_1_sub_1.jsonl"
    f1_sub.write_text(
        '{"type": "user_message", "data": {"content": "Sub 1"}}\n'
        '{"type": "assistant_turn", "data": {"text": "Sub done", "usage": {"input": 2000, "output": 200}}}\n'
    )

    # Chat 2: Main agent only (3,000 in, 300 out)
    f2 = s_dir / "session_2.jsonl"
    f2.write_text(
        '{"type": "user_message", "data": {"content": "Task 2"}}\n'
        '{"type": "assistant_turn", "data": {"text": "Done 2", "usage": {"input": 3000, "output": 300}}}\n'
    )

    store = SessionStore(workspace=tmp_path, session_dir=s_dir)
    ws_ledger = store.load_workspace_ledger("claude-opus-5")

    # Chat count should equal normal chats (2), not including subagents
    assert ws_ledger.chat_count == 2

    # Total tokens = Chat 1 (1100) + Sub 1 (2200) + Chat 2 (3300) = 6600
    expected_input = 1000 + 2000 + 3000
    expected_output = 100 + 200 + 300
    assert ws_ledger.total_input_tokens == expected_input
    assert ws_ledger.total_output_tokens == expected_output
    assert float(ws_ledger.total_cost) > 0.0








