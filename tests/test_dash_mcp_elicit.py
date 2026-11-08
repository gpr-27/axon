"""The web app answers MCP elicitation on the page (never on the terminal's stdin) and drops unknown fields."""
import threading
import time

from axon.features.dash_runtime import Runtime
from axon.mcp import state

PARAMS = {"message": "Pick a colour", "requestedSchema": {
    "type": "object", "properties": {"colour": {"type": "string", "enum": ["red", "teal"]}, "n": {"type": "integer"}},
    "required": ["colour"]}}


def _run(rt, params=PARAMS):
    box = {}
    t = threading.Thread(target=lambda: box.update(r=rt.elicit("srv", params)), daemon=True)
    t.start()
    for _ in range(100):
        if rt.pending:
            break
        time.sleep(0.02)
    return t, box


def test_accept_returns_only_declared_fields(tmp_path):
    rt = Runtime("", None, tmp_path)
    t, box = _run(rt)
    (eid,) = list(rt.pending)
    rt.answer_elicit(eid, "accept", {"colour": "teal", "evil": "x"})
    t.join(3)
    assert box["r"] == {"action": "accept", "content": {"colour": "teal"}}


def test_decline_and_unknown_id(tmp_path):
    import pytest
    from axon.features.dash_common import DashError
    rt = Runtime("", None, tmp_path)
    t, box = _run(rt)
    (eid,) = list(rt.pending)
    rt.answer_elicit(eid, "decline", None)
    t.join(3)
    assert box["r"] == {"action": "decline"}
    with pytest.raises(DashError):
        rt.answer_elicit("nope", "accept", {})


def test_page_mode_never_prompts_on_the_terminal():
    state.set_interactive(True)
    state.set_page_mode(False)
    try:
        assert state.can_prompt()
        state.set_page_mode(True)
        assert not state.can_prompt() and state.is_interactive()
    finally:
        state.set_page_mode(False)
        state.set_interactive(None)
