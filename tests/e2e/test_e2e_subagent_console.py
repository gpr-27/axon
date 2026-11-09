"""The agent list and transcript in a real REPL, in a PTY, against the scripted model: what is typed reaches the model."""
from __future__ import annotations

import re
import time
from typing import Any

import pytest

from fake_llm import FakeLLMServer, script_from_rules
from harness import create_axon_env
from test_e2e_d_helpers import open_repl, posix_only

pytestmark = posix_only
UP, DOWN, ESC = "\x1b[A", "\x1b[B", "\x1b"
SLEEP = 3


def _rule(req: Any) -> Any:
    is_main = "Task" in req.tool_names
    if is_main:
        if "fan out" in (req.last_user_text() or "") and not req.tool_results():
            return [{"tool": "Task", "args": {"description": "Survey the docs", "prompt": "job one", "subagent_type": "general-purpose"}},
                    {"tool": "Task", "args": {"description": "Check the tests", "prompt": "job two", "subagent_type": "general-purpose"}}]
        return [{"text": "main done"}]
    n = len(req.tool_results())
    if n < 2:
        return [{"text": f"step {n}. "}, {"tool": "Bash", "args": {"command": f"sleep {SLEEP} && echo done{n}", "description": "wait"}}]
    return [{"text": "Report written."}]


@pytest.fixture()
def repl(tmp_path):
    llm = FakeLLMServer(model_ids=("gpt-4o",))
    llm.default_script = script_from_rules(_rule)
    llm.start()
    env = create_axon_env(tmp_path, llm)
    s = open_repl(env, rows=40, cols=100)
    try:
        yield s, llm
    finally:
        s.close()
        llm.stop()


def _pump(s: Any, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        s._pump(0.05)


def _carrying(llm: FakeLLMServer, token: str) -> list[Any]:
    return [r for r in llm.completion_requests() if token in r.all_text()]


def _wait_for(llm: FakeLLMServer, s: Any, token: str, timeout: float = 15.0) -> list[Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = _carrying(llm, token)
        if found:
            return found
        s._pump(0.1)
    raise AssertionError(f"{token} never reached the model")


def _type(s: Any, text: str) -> None:
    for ch in text:
        s.send(ch)
        s._pump(0.01)


def test_a_message_typed_in_a_transcript_reaches_that_agents_next_request(repl) -> None:
    s, llm = repl
    s.send("fan out\r")
    s.expect(r"◆ Subagents", timeout=10)
    s.expect(r"Survey the docs", timeout=5)
    s.send(DOWN)
    _pump(s, 0.2)
    s.send(UP)
    _pump(s, 0.2)
    s.send("\r")
    s.expect(r"message 1", timeout=5)
    _type(s, "PING-ONE")
    s.send("\r")
    s.expect(r"Sent to 1", timeout=5)
    found = _wait_for(llm, s, "PING-ONE")
    assert all("job one" in r.all_text() for r in found)
    assert not any("job two" in r.all_text() for r in found), "the other agent must not see it"
    s.send(ESC)
    s.expect(r"esc stop the turn", timeout=5)
    s.expect(r"Done in", timeout=30)


def test_x_stops_the_selected_agent_and_a_finished_one_resumes_from_the_console(repl) -> None:
    s, llm = repl
    s.send("fan out\r")
    s.expect(r"◆ Subagents", timeout=10)
    s.expect(r"Check the tests", timeout=5)
    s.send(DOWN)
    _pump(s, 0.3)
    s.send("x")
    s.expect(r"stopped by you", timeout=8)
    s.expect(r"Done in", timeout=30)
    s.ready(timeout=10)
    m = s.mark()
    s.send("/subagents\r")
    s.expect(r"back to the prompt", timeout=5, since=m)
    assert re.search(r"⊘ +2", s.screen[m:])
    s.send("1")                                  # a digit opens that agent
    s.expect(r"resumes it with its history", timeout=5, since=m)
    _type(s, "RESUME-ONE")
    s.send("\r")
    s.expect(r"Resumed 1", timeout=5, since=m)
    found = _wait_for(llm, s, "RESUME-ONE")
    assert "job one" in found[0].all_text() and "step 0." in found[0].all_text()     # with its history
    s.send(ESC)
    _pump(s, 0.3)
    s.send(ESC)
    s.expect(r"Ask anything", timeout=10, since=m)


def test_subagents_send_addresses_by_title_and_says_when_it_cannot(repl) -> None:
    s, llm = repl
    m = s.mark()
    s.send("/subagents send ghost hello\r")
    s.expect(r"No subagent 'ghost'", timeout=5, since=m)
    s.ready(since=m)
    s.send("fan out\r")
    s.expect(r"Done in", timeout=30)
    s.ready(timeout=10)
    s.send("/subagents send check TITLE-MSG\r")
    found = _wait_for(llm, s, "TITLE-MSG")
    assert "job two" in found[0].all_text()


def _late_rule(req: Any) -> Any:
    if "Task" in req.tool_names:
        if "late please" in (req.last_user_text() or "") and not req.tool_results():
            return [{"tool": "Task", "args": {"description": "One call", "prompt": "job late", "subagent_type": "general-purpose"}}]
        return [{"text": "main done"}]
    if "LATE-MSG" in req.all_text():
        return [{"text": "Second answer."}]
    return [{"delay": 4}, {"text": "First answer."}]


def test_a_message_sent_during_the_last_model_call_is_not_lost(tmp_path) -> None:
    """No tool round follows the last call, so nothing would carry the message: it becomes one more turn."""
    llm = FakeLLMServer(model_ids=("gpt-4o",))
    llm.default_script = script_from_rules(_late_rule)
    llm.start()
    s = open_repl(create_axon_env(tmp_path, llm), rows=40, cols=100)
    try:
        s.send("late please\r")
        s.expect(r"◆ Subagents", timeout=10)
        s.expect(r"One call", timeout=5)
        s.send("\r")
        s.expect(r"message 1", timeout=5)
        _type(s, "LATE-MSG")
        s.send("\r")
        found = _wait_for(llm, s, "LATE-MSG", timeout=12)
        assert "job late" in found[0].all_text() and "First answer." in found[0].all_text()
        s.expect(r"Done in", timeout=20)
        main = [r for r in llm.completion_requests() if "Task" in r.tool_names]
        last = main[-1].all_text()
        assert "First answer." in last and "Second answer." in last     # the parent's report keeps both
    finally:
        s.close()
        llm.stop()


def _approval_rule(req: Any) -> Any:
    if "Task" in req.tool_names:
        if "approve please" in (req.last_user_text() or "") and not req.tool_results():
            return [{"tool": "Task", "args": {"description": "Writer", "prompt": "job write", "subagent_type": "general-purpose"}},
                    {"tool": "Task", "args": {"description": "Waiter", "prompt": "job wait", "subagent_type": "explore"}}]
        return [{"text": "main done"}]
    if "job wait" in req.all_text():
        return [{"delay": 4}, {"text": "waited"}]
    if req.tool_results():
        return [{"text": "wrote it"}]
    return [{"tool": "Write", "args": {"file_path": "notes.md", "content": "x\n"}}]


def test_an_approval_prompt_gets_the_terminal_while_the_list_steps_aside(tmp_path) -> None:
    llm = FakeLLMServer(model_ids=("gpt-4o",))
    llm.default_script = script_from_rules(_approval_rule)
    llm.start()
    env = create_axon_env(tmp_path, llm)
    s = open_repl(env, args=("--mode", "default"), rows=40, cols=100)
    try:
        m = s.mark()
        s.send("approve please\r")
        s.expect(r"Allow Write", timeout=10, since=m)
        s.expect(r"quick pick", timeout=5, since=m)
        _pump(s, 1.0)
        shown = s.screen[m:]
        assert "◆ Subagents" not in shown[shown.rindex("quick pick"):], "the list was drawn over the approval prompt"
        s.send("y")
        s.expect(r"◆ Subagents", timeout=8, since=m)
        s.expect(r"Done in", timeout=20, since=m)
        assert (env.workspace / "notes.md").exists()
    finally:
        s.close()
        llm.stop()


def _background_rule(req: Any) -> Any:
    if "Task" in req.tool_names:
        if "in the background" in (req.last_user_text() or "") and not req.tool_results():
            return [{"tool": "Task", "args": {"description": "Quiet survey", "prompt": "job three", "subagent_type": "general-purpose",
                                              "run_in_background": True}}]
        return [{"text": "main idle now"}]
    n = len(req.tool_results())
    if n < 3:
        return [{"text": f"step {n}. "}, {"tool": "Bash", "args": {"command": f"sleep {SLEEP} && echo done{n}", "description": "wait"}}]
    return [{"text": "Report written."}]


def test_down_at_the_idle_prompt_opens_the_list_and_esc_returns_to_the_prompt(tmp_path) -> None:
    llm = FakeLLMServer(model_ids=("gpt-4o",))
    llm.default_script = script_from_rules(_background_rule)
    llm.start()
    s = open_repl(create_axon_env(tmp_path, llm), rows=40, cols=100)
    try:
        mark = s.mark()
        s.send("work in the background\r")
        s.expect("main idle now", timeout=20, since=mark)
        s.ready(since=mark)
        mark = s.mark()
        s.send(DOWN)
        s.expect(r"◆ Subagents", timeout=10, since=mark)
        s.expect(r"Quiet survey", timeout=5, since=mark)
        s.send("\r")
        s.expect(r"message 1|Quiet survey", timeout=5, since=mark)
        _pump(s, 0.3)
        _type(s, "PING-IDLE")
        s.send("\r")
        found = _wait_for(llm, s, "PING-IDLE", timeout=25)
        assert all("job three" in r.all_text() for r in found)
        s.send(ESC)
        _pump(s, 0.3)
        s.send(ESC)
        s.ready(since=mark, timeout=15)
    finally:
        s.close()
        llm.stop()
