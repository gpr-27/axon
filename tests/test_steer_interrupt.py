"""A message typed while the model is answering cuts that call short and is read by the next call of the same turn
(Claude Code style); during a tool call it waits for the tool boundary; nothing is lost or delivered twice."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from axon.agent.loop import STEER_PREFIX, STEER_STATE_TEXT
from axon.agent.trace import SteerEvent
from axon.providers.base import (AssistantTurn, TextBlock, TextDelta, ToolArgsDelta, ToolUseBlock, ToolUseComplete,
                                 ToolUseStart, TurnComplete, Usage)
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


class SlowProvider(FakeProvider):
    """Streams its text in chunks; ``on_chunk(call_no, chunk_no)`` runs between chunks (the user types there)."""

    def __init__(self, turns: list[AssistantTurn], on_chunk: Callable[[int, int], None] | None = None) -> None:
        super().__init__(turns)
        self.on_chunk = on_chunk
        self.calls = 0
        self.consumed: list[int] = []

    def stream(self, **kw: Any) -> Iterator[Any]:
        self.calls += 1
        call = self.calls
        self.requests.append({"model": kw["model"], "system": kw["system"], "messages": [dict(m) for m in kw["messages"]], "tools": kw["tools"]})
        turn = self._turns.pop(0) if self._turns else AssistantTurn(blocks=[TextBlock(text="done")], stop_reason="end_turn")
        n = 0
        for b in turn.blocks:
            if isinstance(b, TextBlock):
                for i in range(0, len(b.text), 5):
                    n += 1
                    yield TextDelta(b.text[i:i + 5])
                    if self.on_chunk:
                        self.on_chunk(call, n)
            elif isinstance(b, ToolUseBlock):
                n += 1
                yield ToolUseStart(b.id, b.name)
                if self.on_chunk:
                    self.on_chunk(call, n)
                yield ToolArgsDelta(b.id, json.dumps(b.input))
                yield ToolUseComplete(b.id)
        self.consumed.append(n)
        yield TurnComplete(turn.stop_reason, turn.usage)
        self._last = turn


def _text(turn_text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=turn_text)], stop_reason="end_turn", usage=Usage(input=10, output=5))


def _user_texts(messages: list[dict[str, Any]]) -> str:
    return json.dumps([m for m in messages if m.get("role") == "user"])


def test_steer_mid_stream_cuts_the_call_and_the_next_call_reads_it(workspace: Path) -> None:
    events: list[Any] = []
    ls = scripted(("Ls", {"path": "."}))[0]

    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        if call == 2 and chunk == 2:
            holder["agent"].message_queue.push("wrong file, use b.py", steer=True)

    prov = SlowProvider([ls, _text("I will now read a.py and explain it in great detail"), _text("ok, b.py")], typed)
    agent = make_agent(prov, workspace, events=events)
    holder["agent"] = agent
    res = agent.run_turn("explain a.py")
    assert res.stop_reason == "end_turn" and res.final_text == "ok, b.py"
    assert prov.calls == 3 and res.iterations == 3                 # the cut call counts as a step
    assert prov.consumed[1] == 2                                   # the stream was abandoned after two chunks
    req = prov.requests[2]["messages"]
    # earlier tool round stays well formed: assistant tool_use then a user message holding its tool_result
    assert req[1]["role"] == "assistant" and req[1]["content"][0]["type"] == "tool_use"
    assert req[2]["role"] == "user" and req[2]["content"][0]["type"] == "tool_result"
    # the streamed text is kept as an assistant message, then the correction comes as one user turn
    assert req[3]["role"] == "assistant" and req[3]["content"][0]["text"] .startswith("I will now")
    assert req[4]["role"] == "user" and f"{STEER_PREFIX} wrong file, use b.py" in json.dumps(req[4])
    assert "do not repeat" in json.dumps(req[4])
    roles = [m["role"] for m in req]
    assert all(a != b for a, b in zip(roles, roles[1:]))            # strict alternation
    steer = [e for e in events if isinstance(e, SteerEvent)]
    assert len(steer) == 1 and steer[0].mode == "interrupt" and steer[0].texts == ("wrong file, use b.py",)
    assert len(agent.message_queue) == 0                            # delivered exactly once


def test_steer_before_any_text_drops_the_empty_reply_and_merges_into_the_user_turn(workspace: Path) -> None:
    holder: dict[str, Any] = {}
    sent = []

    def typed(call: int, chunk: int) -> None:
        pass

    class Thinking(SlowProvider):
        def stream(self, **kw: Any) -> Iterator[Any]:
            if self.calls == 0:
                from axon.providers.base import ThinkingDelta
                self.calls += 1
                self.requests.append({"messages": [dict(m) for m in kw["messages"]]})
                yield ThinkingDelta("hmm")
                holder["agent"].message_queue.push("actually do Y", steer=True)
                yield ThinkingDelta("still thinking")
                sent.append(1)
                self._last = _text("never used")
                yield TurnComplete("end_turn", Usage())
                return
            yield from super().stream(**kw)

    prov = Thinking([_text("did Y")], typed)
    agent = make_agent(prov, workspace)
    holder["agent"] = agent
    res = agent.run_turn("do X")
    assert res.final_text == "did Y" and not sent               # the first stream stopped at the thinking chunk
    msgs = prov.requests[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user"]                # one user turn: prompt and correction together
    body = json.dumps(msgs[0])
    assert "do X" in body and f"{STEER_PREFIX} actually do Y" in body and "discarded" in body


def test_blocked_stream_is_aborted_through_the_provider_cancel(workspace: Path) -> None:
    gate = threading.Event()

    class Silent(FakeProvider):
        def __init__(self) -> None:
            super().__init__([_text("fine, second")])
            self.calls = 0
            self.cancelled = False

        def stream(self, **kw: Any) -> Iterator[Any]:
            self.calls += 1
            self.requests.append({"messages": [dict(m) for m in kw["messages"]]})
            if self.calls == 1:
                yield TextDelta("start ")
                gate.wait(5)                                  # the server goes silent
                if self.cancelled:
                    raise KeyboardInterrupt
                yield TurnComplete("end_turn", Usage())
                return
            yield from super().stream(**kw)

        def cancel(self, thread_ident: int | None = None) -> bool:
            self.cancelled = True
            gate.set()
            return True

    prov = Silent()
    agent = make_agent(prov, workspace)
    threading.Timer(0.3, lambda: agent.message_queue.push("change of plan", steer=True)).start()
    t0 = time.time()
    res = agent.run_turn("go")
    assert time.time() - t0 < 4 and res.final_text == "fine, second"
    assert prov.calls == 2 and f"{STEER_PREFIX} change of plan" in json.dumps(prov.requests[1]["messages"])


def test_steer_while_the_model_writes_a_tool_call_waits_for_the_tool_boundary(workspace: Path) -> None:
    events: list[Any] = []
    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        if call == 1:                                          # right after ToolUseStart
            holder["agent"].message_queue.push("and mention the date", steer=True)
            holder["state"] = holder["agent"].steer_state()

    prov = SlowProvider(scripted(("Ls", {"path": "."}), "final"), typed)
    agent = make_agent(prov, workspace, events=events)
    holder["agent"] = agent
    res = agent.run_turn("list")
    assert holder["state"] == "tool" and res.final_text == "final" and prov.calls == 2
    last = prov.requests[1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
    assert f"{STEER_PREFIX} and mention the date" in json.dumps(last)       # rode on the tool result, as before
    assert [e.mode for e in events if isinstance(e, SteerEvent)] == ["next_step"]


def test_a_running_tool_is_never_killed_by_steer(workspace: Path) -> None:
    prov = SlowProvider(scripted(("Ls", {"path": "."}), "final"))
    agent = make_agent(prov, workspace)
    real = agent._execute_batch
    seen: dict[str, Any] = {}

    def slow(tool_uses: Any) -> Any:
        agent.message_queue.push("hurry up", steer=True)
        seen["state"] = agent.steer_state()
        time.sleep(0.3)                                        # the watcher polls during this time
        seen["finished"] = True
        return real(tool_uses)

    agent._execute_batch = slow                                 # type: ignore[method-assign]
    res = agent.run_turn("list")
    assert seen == {"state": "tool", "finished": True} and prov.calls == 2 and res.final_text == "final"
    assert f"{STEER_PREFIX} hurry up" in json.dumps(prov.requests[1]["messages"][-1])


def test_two_quick_messages_become_one_injected_turn_in_order(workspace: Path) -> None:
    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        if call == 1 and chunk == 1:
            q = holder["agent"].message_queue
            q.push("first fix", steer=True)
            q.push("second fix", steer=True)

    prov = SlowProvider([_text("a long first answer that will be cut"), _text("both read")], typed)
    agent = make_agent(prov, workspace)
    holder["agent"] = agent
    agent.run_turn("question")
    assert prov.calls == 2                                       # one extra call for both messages, not two
    body = json.dumps(prov.requests[1]["messages"][-1])
    assert body.index("first fix") < body.index("second fix")


def test_setting_off_keeps_the_old_read_at_next_step_behaviour(workspace: Path) -> None:
    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        if call == 1 and chunk == 1:
            holder["agent"].message_queue.push("late fix", steer=True)

    prov = SlowProvider([_text("a long first answer that is allowed to finish")], typed)
    agent = make_agent(prov, workspace, steer_interrupts=False)
    holder["agent"] = agent
    res = agent.run_turn("question")
    assert prov.calls == 1 and res.final_text == "a long first answer that is allowed to finish"
    assert [it.text for it in agent.message_queue.items] == ["late fix"]     # left for the follow-up turn, as before
    assert agent.steer_state() == "next_step"


def test_setting_is_declared_and_mapped() -> None:
    from axon.config import Settings, map_json_settings
    assert Settings.model_fields["steer_interrupts"].default is True
    assert map_json_settings({"steerInterrupts": False})["steer_interrupts"] is False
    from axon.commands.system_cmds import _CONFIG_KEYS
    assert "steer_interrupts" in _CONFIG_KEYS


def test_a_message_is_not_lost_when_the_run_ends_first(workspace: Path) -> None:
    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        pass

    prov = SlowProvider([_text("short")], typed)
    agent = make_agent(prov, workspace)
    holder["agent"] = agent
    orig = agent._stop_result

    def late(*a: Any, **k: Any) -> Any:
        agent.message_queue.push("too late", steer=True)       # lands after the last model call finished
        return orig(*a, **k)

    agent._stop_result = late                                    # type: ignore[method-assign]
    agent.run_turn("hi")
    assert [it.text for it in agent.message_queue.items] == ["too late"]       # still queued for the follow-up turn


def test_state_lines_and_terminal_text_sequence(workspace: Path) -> None:
    from axon.ui.render import Renderer
    assert STEER_STATE_TEXT["interrupt"] == "interrupting the current answer to include it"
    assert STEER_STATE_TEXT["tool"] == "queued — read when the current tool finishes"
    assert STEER_STATE_TEXT["next_step"] == "included in the model's next step"
    import io
    import re
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = Renderer(trace_level="normal")
        r.on_event(SteerEvent(count=1, texts=("use b.py instead",), iteration=2, mode="interrupt", shown=False))
        r.on_event(SteerEvent(count=1, texts=("x",), iteration=3, mode="next_step", shown=True))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    assert lines[0].endswith("you: use b.py instead")
    assert lines[1] == STEER_STATE_TEXT["interrupt"]
    assert lines[2] == STEER_STATE_TEXT["next_step"]
    assert "x" not in lines[2].split()                           # a message already shown is not printed twice


def test_subagent_path_uses_the_same_loop_and_reports_the_state(workspace: Path) -> None:
    from axon.agent.subagent import SubagentManager, SubagentTask
    holder: dict[str, Any] = {}

    def typed(call: int, chunk: int) -> None:
        if call == 1 and chunk == 1:
            holder["agent"].message_queue.push("sub fix", steer=True)

    prov = SlowProvider([_text("a long first subagent answer"), _text("fixed")], typed)
    sub = make_agent(prov, workspace, agent_name="worker")
    holder["agent"] = sub
    res = sub.run_turn("task")
    assert sub.is_subagent and prov.calls == 2 and res.final_text == "fixed"
    assert f"{STEER_PREFIX} sub fix" in json.dumps(prov.requests[1]["messages"][-1])

    # SubagentManager.send writes the one state line under the "you:" entry
    mgr = SubagentManager()
    task = SubagentTask(id="s1", index=1, title="d", prompt="p", status="running")
    task.live_agent = SimpleNamespace(message_queue=sub.message_queue, steer_state=lambda: "tool")
    mgr._tasks.append(task)
    assert mgr.send("s1", "hello") is task
    texts = [e["text"] for e in task.timeline if e["kind"] == "note"]
    assert texts[-2:] == ["you: hello", STEER_STATE_TEXT["tool"]]
    assert sub.message_queue.items[-1].shown is True
