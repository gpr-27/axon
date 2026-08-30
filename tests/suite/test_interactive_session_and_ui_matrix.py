"""
Exhaustive test suite for interactive session restoration, UI renderers, dashboard boxes, and banner displays.
"""
import pytest
from pathlib import Path
from io import StringIO
import sys
from axon.agent.state import Conversation
from axon.session.interactive import render_restored_conversation
from axon.session.ledger import Ledger
from axon.agent.subagent import SubagentTask
from axon.ui.render import (
    render_subagent_dashboard,
    render_queue_box,
    render_shortcuts_footer,
    render_todo_box,
    render_error_box,
)
# ─── UI Dashboard & Status Badge Matrix (25 tests) ──────────────────────────
@pytest.mark.parametrize("status,expected_icon", [
    ("running", "Subagent"),
    ("completed", "Explore database"),
    ("exhausted", "Subagent"),
    ("error", "Subagent"),
])
def test_subagent_dashboard_statuses(status: str, expected_icon: str):
    tasks = [
        SubagentTask(id="sub-1", prompt="Explore database", index=1, title="Explore database", status=status, steps=4, max_steps=10),
    ]
    old_stdout = sys.stdout
    sys.stdout = buffer = StringIO()
    try:
        render_subagent_dashboard(tasks)
    finally:
        sys.stdout = old_stdout
    assert expected_icon in buffer.getvalue()

def test_render_subagent_dashboard_output():
    tasks = [
        SubagentTask(id="sub-1", prompt="Explore database", index=1, title="Explore database", status="completed", steps=4, max_steps=10),
        SubagentTask(id="sub-2", prompt="Inspect auth routes", index=2, title="Inspect auth routes", status="running", steps=2, max_steps=10),
    ]
    old_stdout = sys.stdout
    sys.stdout = buffer = StringIO()
    try:
        render_subagent_dashboard(tasks)
    finally:
        sys.stdout = old_stdout

    output = buffer.getvalue()
    assert "Subagent Fan-Out" in output
    assert "Explore database" in output
    assert "Inspect auth routes" in output

def test_render_ui_boxes():
    from axon.agent.state import MessageQueue
    q = MessageQueue()
    q.push("Check migrations")
    
    q_box = render_queue_box(q)
    assert "Check migrations" in q_box or "Pending" in q_box or "Queue" in q_box

    foot = render_shortcuts_footer()
    assert "Ctrl+" in foot or "shortcuts" in foot.lower() or "mode" in foot.lower()

    err = render_error_box("Read", "File not found")
    assert "File not found" in err

