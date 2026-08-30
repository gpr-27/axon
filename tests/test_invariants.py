"""
The 5 Law Invariant Tests (02-AGENT-LOOP.md & 08-TESTING.md).
"""
import pytest
from pathlib import Path
from axon.agent.loop import Agent
from axon.tools.fs_read import ReadTool
from axon.tools.shell import BashTool
from tests.fakes import FakeProvider, scripted

def test_law1_every_tool_use_gets_a_result(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger):
    """Law 1: Every tool_use block gets a matching tool_result even if unknown or failing."""
    (workspace / "a.py").write_text("print('hello')", encoding="utf-8")
    fake = FakeProvider(scripted(
        [
            ("Read", {"path": "a.py"}),
            ("NoSuchTool", {}),
            ("Bash", {"command": "rm -rf /", "description": "dangerous"}),
            ("Read", {"path": "/etc/passwd"}),
        ],
        "Done with tasks.",
    ))
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    agent.run_turn("execute tools")
    assert len(fake.requests) == 2
    tool_results = fake.requests[1]["messages"][-1]["content"]
    assert len(tool_results) == 4
    assert {r["tool_use_id"] for r in tool_results} == {"t0", "t1", "t2", "t3"}
    # 3 of them should be error results
    assert sum(1 for r in tool_results if r.get("is_error")) == 3

def test_law2_results_batched_into_one_message(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger):
    """Law 2: All tool_result blocks from one assistant turn go into ONE single user message."""
    fake = FakeProvider(scripted(
        [("Glob", {"pattern": "*"}), ("Ls", {"path": "."})],
        "Batch complete.",
    ))
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    agent.run_turn("list files")
    msgs = fake.requests[1]["messages"]
    assert msgs[-1]["role"] == "user"
    assert len(msgs[-1]["content"]) == 2  # Both results in one message
    assert msgs[-2]["role"] == "assistant"

def test_law3_native_content_replayed_verbatim(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger):
    """Law 3: Provider-native content is replayed verbatim."""
    sentinel = [{"type": "thinking", "text": "..."}, {"type": "tool_use", "id": "t0", "name": "Ls", "input": {"path": "."}}]
    turn = scripted(("Ls", {"path": "."}))[0]
    turn.native = sentinel
    fake = FakeProvider([turn, scripted("final")[0]])
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    agent.run_turn("native test")
    assert len(fake.requests) == 2
    replayed = fake.requests[1]["messages"][-2]["content"]
    assert replayed == sentinel

def test_law4_tool_crash_becomes_error_result_not_exception(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger, monkeypatch):
    """Law 4: Tool crashes become error results instead of escaping."""
    monkeypatch.setattr(ReadTool, "run", lambda *a, **k: 1 / 0)
    fake = FakeProvider(scripted(("Read", {"path": "dummy.py"}), "handled error"))
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    # Must not raise out of run_turn
    res = agent.run_turn("read dummy")
    assert res.stop_reason == "end_turn"
    result_block = fake.requests[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] is True
    assert "ZeroDivisionError" in result_block["content"]

def test_law5_interrupt_still_closes_the_turn(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger, monkeypatch):
    """Law 5: KeyboardInterrupt still records tool_results and leaves valid conversation state."""
    def _raise_interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(BashTool, "run", _raise_interrupt)
    fake = FakeProvider(scripted(("Bash", {"command": "sleep 10", "description": "sleep"}), "done"))
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    with pytest.raises(KeyboardInterrupt):
        agent.run_turn("run sleep")

    # Validate that conversation has matching tool result
    agent.conversation.validate()
    last_msg = agent.conversation.messages[-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"][0]["is_error"] is True
    assert "Interrupted" in last_msg["content"][0]["content"]

def test_dispatch_slash_commands(workspace: Path, settings, registry, permissions, context_manager, session_store, ledger):
    """Verify built-in slash command dispatchers (/cost, /todos, /permissions, /context, /clear)."""
    from axon.commands.builtin import dispatch_command
    fake = FakeProvider(scripted([], "done"))
    agent = Agent(
        provider=fake,
        tools=registry,
        permissions=permissions,
        context=context_manager,
        session=session_store,
        ledger=ledger,
        settings=settings,
    )

    # /cost
    res = dispatch_command("/cost", agent)
    assert res is not None and res.handled is True

    # /todos
    res = dispatch_command("/todos", agent)
    assert res is not None and res.handled is True

    # /permissions
    res = dispatch_command("/permissions", agent)
    assert res is not None and res.handled is True

    # /context
    res = dispatch_command("/context", agent)
    assert res is not None and res.handled is True

    # /tools
    res = dispatch_command("/tools", agent)
    assert res is not None and res.handled is True

    # /mode bypass
    res = dispatch_command("/mode bypass", agent)
    assert res is not None and res.handled is True
    assert agent.settings.mode == "bypass"

    # /history and /whistory
    res = dispatch_command("/history", agent)
    assert res is not None and res.handled is True

    res = dispatch_command("/whistory", agent)
    assert res is not None and res.handled is True

    # /payload
    res = dispatch_command("/payload", agent)
    assert res is not None and res.handled is True

    # /window
    res = dispatch_command("/window", agent)
    assert res is not None and res.handled is True

    res = dispatch_command("/window 10", agent)
    assert res is not None and res.handled is True
    assert agent.settings.max_history_turns == 10

    # /output
    res = dispatch_command("/output", agent)
    assert res is not None and res.handled is True

    # /help and ?
    res = dispatch_command("/help", agent)
    assert res is not None and res.handled is True

    res = dispatch_command("?", agent)
    assert res is not None and res.handled is True

    # /doctor
    res = dispatch_command("/doctor", agent)
    assert res is not None and res.handled is True

    # /sessions
    res = dispatch_command("/sessions", agent)
    assert res is not None and res.handled is True

    # /thinking
    res = dispatch_command("/thinking", agent)
    assert res is not None and res.handled is True

    # /subagents and /main
    res = dispatch_command("/subagents", agent)
    assert res is not None and res.handled is True

    res = dispatch_command("/main", agent)
    assert res is not None and res.handled is True

    # /clear
    res = dispatch_command("/clear", agent)
    assert res is not None and res.handled is True
    assert len(agent.conversation.messages) == 0

def test_openai_message_sanitization():
    """Verify that sanitize_openai_messages prevents HTTP 400 errors from unpaired tool_calls."""
    from axon.providers.openai_compat import sanitize_openai_messages

    # Unpaired tool_calls (assistant has tool calls but next message is user)
    broken_msgs = [
        {"role": "user", "content": "run task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
            ],
        },
        {"role": "user", "content": "How are you doing?"},
    ]

    sanitized = sanitize_openai_messages(broken_msgs)
    # The assistant message must be followed by tool messages for call_1 and call_2 before the user message
    assert sanitized[0]["role"] == "user"
    assert sanitized[1]["role"] == "assistant"
    assert sanitized[2]["role"] == "tool" and sanitized[2]["tool_call_id"] == "call_1"
    assert sanitized[3]["role"] == "tool" and sanitized[3]["tool_call_id"] == "call_2"
    assert sanitized[4]["role"] == "user" and sanitized[4]["content"] == "How are you doing?"

    # Orphaned tool message
    orphaned_msgs = [
        {"role": "tool", "tool_call_id": "orphan_call", "content": "result"},
        {"role": "user", "content": "hello"},
    ]
    sanitized_orphans = sanitize_openai_messages(orphaned_msgs)
    assert len(sanitized_orphans) == 1
    assert sanitized_orphans[0]["role"] == "user"

def test_message_queue():
    """Verify MessageQueue operations and /queue command handling."""
    from axon.agent.state import MessageQueue
    from axon.commands.builtin import dispatch_command

    q = MessageQueue()
    assert len(q) == 0

    # Push items
    m1 = q.push("Question 1")
    m2 = q.push("Question 2")
    m3 = q.push("Question 3")
    assert len(q) == 3
    assert m1.id == 1 and m2.id == 2 and m3.id == 3

    # Render
    rendered = q.render()
    assert "3 pending" in rendered
    assert "#1 [Next]: Question 1" in rendered

    # Remove item
    assert q.remove(2) is True
    assert len(q) == 2
    assert q.remove(999) is False

    # Pop item
    popped = q.pop()
    assert popped is not None and popped.id == 1
    assert len(q) == 1

    # Clear
    q.clear()
    assert len(q) == 0

def test_btw_and_keybindings():
    """Verify /btw side question and /keybindings command dispatch and rendering."""
    from axon.ui.render import render_shortcuts_footer, render_side_question_box
    from axon.ui.theme import strip_ansi
    from axon.commands.builtin import dispatch_command
    from axon.agent.loop import Agent
    from axon.tools import create_default_registry
    from axon.agent.context import ContextManager
    from axon.session.store import SessionStore
    from axon.session.ledger import Ledger
    from axon.permissions.engine import PermissionEngine
    from axon.config import Settings
    from pathlib import Path

    footer = render_shortcuts_footer()
    footer_plain = strip_ansi(footer)
    assert "! for shell mode" in footer_plain
    assert "/btw for side question" in footer_plain
    assert "ctrl + s to stash prompt" in footer_plain
    assert "ctrl + g to edit in $EDITOR" in footer_plain

    side_box = render_side_question_box("What is DFA?", "A deterministic finite automaton.")
    assert "Side Question" in side_box
    assert "deterministic finite automaton" in side_box

    settings = Settings(workspace=Path.cwd())
    agent = Agent(
        provider=FakeProvider(scripted([], "4")),
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(settings.workspace),
        ledger=Ledger(),
        settings=settings,
    )

    res_keys = dispatch_command("/keybindings", agent)
    assert res_keys is not None and res_keys.handled is True

    res_btw = dispatch_command("/btw What is 2+2?", agent)
    assert res_btw is not None and res_btw.handled is True

    # Test new command suite
    for cmd in [
        "/compact",
        "/plan",
        "/plan mode",
        "/plan default",
        "/mcp",
        "/plugin",
        "/hooks",
        "/memory",
        "/status",
        "/diff",
        "/config",
        "/config effort medium",
        "/tasks",
        "/agents",
    ]:
        res = dispatch_command(cmd, agent)
        assert res is not None and res.handled is True

