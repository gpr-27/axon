"""
Comprehensive tests for human-readable observation tracking, numbering,
pipeline routing, and the /ptw side inquiry interface.
"""
from pathlib import Path
import pytest
from pydantic import SecretStr

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools.registry import create_default_registry
from axon.ui.render import Renderer, render_side_question_box
from axon.ui.theme import strip_ansi
from axon.providers.base import (
    LLMCallStart,
    ToolBatchStart,
    ToolUseBlock,
    ToolExecutionStart,
    ToolExecutionResult,
)
from axon.commands.builtin import dispatch_command, handle_btw
from axon.ui.input import ALL_SLASH_COMMANDS
from axon.ui.in_flight import InFlightInputListener


@pytest.fixture
def mock_agent(tmp_path: Path) -> Agent:
    settings = Settings(workspace=tmp_path, api_key=SecretStr("mock-key"), model="deepseek-v4-flash")
    return Agent(
        provider=None,
        tools=create_default_registry(),
        permissions=PermissionEngine(settings),
        context=ContextManager(settings),
        session=SessionStore(tmp_path),
        ledger=Ledger(),
        settings=settings,
    )


def test_renderer_human_readable_observation_tracking(capsys):
    """The timeline shows each step, what each tool returned and what is fed back to the next call."""
    renderer = Renderer(trace_level="detailed")
    renderer.on_event(
        LLMCallStart(iteration=1, max_iterations=10, model="deepseek-v4-flash", message_count=1,
                     tokens_in=1200, tools_ingested=[], user_prompt="show the tree")
    )
    plain0 = strip_ansi(capsys.readouterr().out)
    assert 'Turn 1 · "show the tree"' in plain0
    assert "Step 1  LLM call · deepseek-v4-flash · reads 1 message (~1.2k tokens)" in plain0

    renderer.on_event(ToolExecutionStart(id="call_00_H1bn", name="FileTree", input={"max_depth": 2}))
    renderer.on_event(
        ToolExecutionResult(id="call_00_H1bn", name="FileTree", input={"max_depth": 2}, content="axon/\n  dist/\n  src/\n")
    )
    plain1 = strip_ansi(capsys.readouterr().out)
    assert "● FileTree" in plain1
    assert "max_depth  2" in plain1                         # detailed: every input argument as key  value
    assert "to step 2" in plain1                           # where the result goes
    assert "axon/" in plain1                               # detailed: result preview
    assert "result #1 [call_00_H1bn] goes to the model in step 2" in plain1

    renderer.on_event(
        LLMCallStart(
            iteration=2, max_iterations=10, model="deepseek-v4-flash", message_count=3, tokens_in=4500,
            tools_ingested=[{"id": "call_00_H1bn", "name": "FileTree", "input": {"max_depth": 2}, "target": ".",
                             "lines": 3, "bytes": 24, "tokens": 8, "preview": "axon/", "is_error": False}],
        )
    )
    plain2 = strip_ansi(capsys.readouterr().out)
    assert "Step 2  LLM call · deepseek-v4-flash" in plain2
    assert "includes 1 tool result from step 1 (24 B · ~8 tokens)" in plain2
    assert "FileTree . · 3 lines · 24 bytes · ~8 tok · [call_00_H1bn]" in plain2

def test_renderer_multi_observation_numbering(capsys):
    """Observations are numbered per step and routed to the next LLM call."""
    renderer = Renderer(trace_level="detailed")
    renderer.on_event(LLMCallStart(iteration=2, max_iterations=10, model="deepseek-v4-flash", message_count=3, tokens_in=5000))
    capsys.readouterr()
    renderer.on_event(ToolExecutionStart(id="call_01", name="Ls", input={"path": "src/axon"}))
    renderer.on_event(ToolExecutionResult(id="call_01", name="Ls", input={"path": "src/axon"}, content="agent/\nui/\n"))
    out1 = strip_ansi(capsys.readouterr().out)
    assert "result #1 [call_01] goes to the model in step 3" in out1
    renderer.on_event(ToolExecutionStart(id="call_02", name="Read", input={"path": "pyproject.toml"}))
    renderer.on_event(ToolExecutionResult(id="call_02", name="Read", input={"path": "pyproject.toml"}, content="[project]\nversion = '0.27.1'\n"))
    out2 = strip_ansi(capsys.readouterr().out)
    assert "result #2 [call_02] goes to the model in step 3" in out2
    assert "to step 3" in out2

def test_render_side_question_box_format():
    """Verify high-clarity text field box format for /btw side questions."""
    q = "How does context compaction work?"
    ans = "It uses a sliding-window and multi-rung pruning strategy.\nOld turns are pruned when token budget exceeds limit."
    rendered = render_side_question_box(q, ans, max_width=80)
    plain = strip_ansi(rendered)

    assert "Side Question (/btw)" in plain
    assert "Question Asked:" in plain
    assert "How does context compaction work?" in plain
    assert "Direct Answer (Factual & Non-Inferential):" in plain
    assert "It uses a sliding-window and multi-rung pruning strategy." in plain
    assert "Scratchpad Isolation:" in plain
    assert "Zero pollution to main conversation flow" in plain


def test_ptw_slash_command_registered():
    """Verify /btw is registered as canonical side question command and redundant duplicates are omitted."""
    cmds = [cmd[0] for cmd in ALL_SLASH_COMMANDS]
    assert "/btw" in cmds
    assert "/ptw" not in cmds
    assert "/ask" not in cmds


def test_ptw_dispatch_command(mock_agent: Agent, capsys):
    """Verify dispatch_command routes /ptw fallback to handle_btw."""
    res = dispatch_command("/ptw", mock_agent)
    assert res.handled is True
    out = strip_ansi(capsys.readouterr().out)
    assert "Usage: /btw <question>" in out

    res_btw = dispatch_command("/btw", mock_agent)
    assert res_btw.handled is True
    out_btw = strip_ansi(capsys.readouterr().out)
    assert "Usage: /btw <question>" in out_btw


def test_in_flight_ptw_detection(mock_agent: Agent):
    """Verify in-flight listener intercepts /ptw as side inquiry rather than queuing."""
    listener = InFlightInputListener(mock_agent)
    assert hasattr(listener, "_submit_line")
    # Empty question does not crash or queue
    listener._submit_line("/ptw")
    assert len(mock_agent.message_queue) == 0


def test_renderer_tool_plan_and_invocation(capsys):
    """Normal level: the plan line, each call with its arguments, and permission decisions."""
    from axon.agent.trace import PermissionDecisionEvent

    renderer = Renderer()
    assert renderer.trace_level == "normal"
    renderer.on_event(LLMCallStart(iteration=1, max_iterations=10, model="deepseek-v4-flash", message_count=1,
                                   tokens_in=500, user_prompt="Test tool calling", mode="bypass"))
    capsys.readouterr()
    tools = [
        ToolUseBlock(id="call_00_doc", name="Doctor", input={}),
        ToolUseBlock(id="call_01_ls", name="Ls", input={"path": "."}),
        ToolUseBlock(id="call_03_bash", name="Bash", input={"command": "echo 'hello from bash'"}),
    ]
    renderer.on_event(ToolBatchStart(total_count=3, tools=tools, user_prompt="Test tool calling", mode="bypass"))
    plan_out = strip_ansi(capsys.readouterr().out)
    assert "runs 3 tools one after another" in plan_out

    renderer.on_event(ToolExecutionStart(id="call_03_bash", name="Bash", input={"command": "echo 'hello from bash'"}, mode="bypass"))
    renderer.on_event(PermissionDecisionEvent(id="call_03_bash", tool="Bash", outcome="allow", reason="bypass mode", source="mode"))
    bash_out = strip_ansi(capsys.readouterr().out)
    assert "● Bash  $ echo 'hello from bash'" in bash_out
    assert "auto-allowed" not in bash_out          # routine bypass-mode approvals are hidden at normal level

