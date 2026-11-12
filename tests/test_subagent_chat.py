"""Talking to a subagent: a finished one is resumed with its history, a running one is steered, and the web endpoint and the
/agent command reach the same code as the model's SendMessage."""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.agent.subagent import run_subagent
from axon.features import dash_subagents
from axon.features.dash_common import DashError
from axon.providers.base import AssistantTurn, TextBlock, Usage
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _wait(pred: Any, secs: float = 8.0) -> None:
    end = time.time() + secs
    while time.time() < end and not pred():
        time.sleep(0.05)
    assert pred()


@pytest.fixture
def parent(workspace: Path):
    prov = FakeProvider(scripted("first answer") + scripted("second answer"))
    p = make_agent(prov, workspace)
    p.session.open("sess_chat")
    run_subagent("count the files", p, description="count files", name="counter")
    return p


def _srv(agent: Any) -> Any:
    rt = SimpleNamespace(agent=agent, session_id="sess_chat", event_log=lambda: None)
    idx = SimpleNamespace(lookup=lambda sid: (_ for _ in ()).throw(DashError("session not found", 404)))
    return SimpleNamespace(runtime=lambda sid, create=True: rt, index=idx)


def test_a_finished_subagent_is_resumed_from_the_web_endpoint(parent: Any) -> None:
    task = parent.subagents.all_tasks()[0]
    assert task.status == "completed"
    res = dash_subagents.say(_srv(parent), "sess_chat", task.index, "now count the tests")
    assert res["kind"] == "resumed" and res["task"] == task.id
    _wait(lambda: task.status != "running" and task.runs >= 2)
    text = " ".join(str(m.get("content")) for m in task.conversation.messages)
    assert "now count the tests" in text and "second answer" in text      # the same history grew; nothing started over
    sub = Path(parent.session._file("sess_chat_sub_%d" % task.index))
    assert "second answer" in sub.read_text(encoding="utf-8")             # and the transcript the page tails has it


def test_an_empty_message_or_unknown_subagent_is_refused(parent: Any) -> None:
    with pytest.raises(DashError):
        dash_subagents.say(_srv(parent), "sess_chat", 1, "   ")
    with pytest.raises(DashError) as e:
        dash_subagents.say(_srv(parent), "sess_chat", 99, "hello")
    assert e.value.status == 404


def test_agent_command_shows_everything_and_sends(parent: Any, capsys: Any) -> None:
    from axon.commands.parity_cmds import handle_agent, handle_list_agents
    handle_list_agents(parent, "")
    assert "sub-1" in capsys.readouterr().out
    handle_agent(parent, "counter")
    out = capsys.readouterr().out
    assert "count the files" in out and "first answer" in out
    handle_agent(parent, "1 and the tests please")
    assert "Resumed" in capsys.readouterr().out
    _wait(lambda: parent.subagents.all_tasks()[0].runs >= 2)
