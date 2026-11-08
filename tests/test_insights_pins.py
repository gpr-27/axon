"""Context pins: a pinned tool result is never trimmed, evicted or summarised away."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from axon.agent.context import STALE_PLACEHOLDER, TRIM_THRESHOLD_CHARS, ContextManager
from axon.agent.state import Conversation
from axon.config import Settings
from axon.features import insights
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

BIG = "line of source code\n" * 1200            # well over the trim threshold


def _call(i: str, name: str, path: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": name, "input": {"file_path": path}}]}


def _result(i: str, text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": text}]}


def _conv() -> Conversation:
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "look at both files"}]
    msgs += [_call("a1", "Read", "src/a.py"), _result("a1", BIG), _call("b1", "Read", "src/b.py"), _result("b1", BIG)]
    for i in range(3):
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": f"note {i}"}]}, {"role": "user", "content": f"go on {i}"}]
    return Conversation(msgs)


def _cm(workspace: Path) -> ContextManager:
    return ContextManager(Settings(workspace=workspace, api_key="k"))


def _text(conv: Conversation, tid: str) -> str:
    for m in conv.messages:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "tool_result" and b.get("tool_use_id") == tid:
                return b["content"]
    raise KeyError(tid)


def test_pinned_result_survives_trim_and_eviction(workspace: Path) -> None:
    cm, conv = _cm(workspace), _conv()
    cm.pins["a1"] = {"label": "Read src/a.py", "tokens": 100}
    cm._trim_large_results(conv)
    assert _text(conv, "a1") == BIG and "truncated" in _text(conv, "b1")
    cm._evict_stale_results(conv)
    assert _text(conv, "a1") == BIG and _text(conv, "b1") == STALE_PLACEHOLDER
    assert len(BIG) > TRIM_THRESHOLD_CHARS


def test_pinned_result_is_carried_through_the_summary_split(workspace: Path) -> None:
    cm, conv = _cm(workspace), _conv()
    cm.pins["a1"] = {"label": "Read src/a.py", "tokens": 100}
    assert cm._summarize_older_turns(conv, summary_text="did things", keep_turns=1)
    first = conv.messages[0]["content"]
    assert "did things" in first and '<pinned id="a1"' in first and BIG.strip() in first
    assert "b1" not in first                                    # unpinned results are summarised away


def test_a_second_summary_keeps_the_pin_and_drops_an_unpinned_one(workspace: Path) -> None:
    cm, conv = _cm(workspace), _conv()
    cm.pins["a1"] = {"label": "x", "tokens": 1}
    cm.pins["b1"] = {"label": "y", "tokens": 1}
    cm._summarize_older_turns(conv, summary_text="one", keep_turns=1)
    conv.messages += [{"role": "assistant", "content": [{"type": "text", "text": "more"}]}, {"role": "user", "content": "again"},
                      {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, {"role": "user", "content": "last"}]
    del cm.pins["b1"]
    cm._summarize_older_turns(conv, summary_text="two", keep_turns=1)
    first = conv.messages[0]["content"]
    assert "two" in first and '<pinned id="a1"' in first and '<pinned id="b1"' not in first


def test_summary_without_pins_is_unchanged(workspace: Path) -> None:
    cm, conv = _cm(workspace), _conv()
    cm._summarize_older_turns(conv, summary_text="plain", keep_turns=1)
    assert "pinned" not in conv.messages[0]["content"]


# ─── the commands ────────────────────────────────────────────────────────────

def _agent(workspace: Path) -> Any:
    (workspace / "a.py").write_text("print('hi')\n" * 50, encoding="utf-8")
    prov = FakeProvider(scripted(("Read", {"file_path": str(workspace / "a.py")}), "done"))
    agent = make_agent(prov, workspace)
    agent.run_turn("read a.py")
    return agent


def _tool_id(agent: Any) -> str:
    return next(b["id"] for m in agent.conversation.messages if isinstance(m["content"], list)
                for b in m["content"] if b.get("type") == "tool_use")


def test_pin_by_path_and_by_id_then_list_with_token_total(workspace: Path, capsys: Any) -> None:
    agent = _agent(workspace)
    capsys.readouterr()
    insights.handle_pin(agent, "a.py")
    out = strip_ansi(capsys.readouterr().out)
    assert "Pinned" in out and "Read" in out
    tid = _tool_id(agent)
    assert tid in agent.context.pins
    insights.handle_pin(agent, "")
    listing = strip_ansi(capsys.readouterr().out)
    assert tid in listing and "pinned total" in listing and "1 pin" in listing
    insights.handle_pin(agent, tid)                              # idempotent
    assert list(agent.context.pins) == [tid]


def test_pin_unknown_target_says_so(workspace: Path, capsys: Any) -> None:
    agent = _agent(workspace)
    capsys.readouterr()
    assert insights.handle_pin(agent, "nope.py") is False
    assert "nothing to pin" in strip_ansi(capsys.readouterr().out).lower()
    assert not agent.context.pins


def test_unpin_one_or_all(workspace: Path, capsys: Any) -> None:
    agent = _agent(workspace)
    insights.handle_pin(agent, "a.py")
    tid = _tool_id(agent)
    capsys.readouterr()
    insights.handle_unpin(agent, tid)
    assert not agent.context.pins and "Unpinned" in strip_ansi(capsys.readouterr().out)
    insights.handle_unpin(agent, "")
    assert "no pins" in strip_ansi(capsys.readouterr().out).lower()


def test_pins_come_back_on_resume_and_not_in_a_new_session(workspace: Path) -> None:
    agent = _agent(workspace)
    insights.handle_pin(agent, "a.py")
    sid, tid = agent.session.active_session_id, _tool_id(agent)

    other = make_agent(FakeProvider([]), workspace)
    other.session.open(sid)
    other.conversation = other.session.load(sid)
    insights.sync_pins(other)
    assert list(other.context.pins) == [tid]
    assert other.context.pins[tid]["label"].startswith("Read")

    other.session.open(None)                                     # a fresh session starts with no pins
    insights.sync_pins(other)
    assert not other.context.pins


def test_unpinning_is_remembered_across_resume(workspace: Path) -> None:
    agent = _agent(workspace)
    insights.handle_pin(agent, "a.py")
    insights.handle_unpin(agent, "")
    sid = agent.session.active_session_id
    other = make_agent(FakeProvider([]), workspace)
    other.session.open(sid)
    other.conversation = other.session.load(sid)
    insights.sync_pins(other)
    assert not other.context.pins


def test_the_turn_loop_restores_pins_before_trimming(workspace: Path) -> None:
    agent = _agent(workspace)
    insights.handle_pin(agent, "a.py")
    sid, tid = agent.session.active_session_id, _tool_id(agent)
    other = make_agent(FakeProvider(scripted("hello")), workspace)
    other.session.open(sid)
    other.conversation = other.session.load(sid)
    other.run_turn("continue")
    assert tid in other.context.pins
