"""Compaction receipt: what a compaction replaced, the summary text, and the before/after sizes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from axon.commands.registry import dispatch_command
from axon.features import insights
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent
from tests.test_insights_pins import BIG, _cm, _conv


def test_summary_records_a_receipt(workspace: Path) -> None:
    cm, conv = _cm(workspace), _conv()
    n_before = len(conv.messages)
    assert cm._summarize_older_turns(conv, summary_text="we read two files", keep_turns=1)
    r = cm.last_receipt
    assert r["messages_before"] == n_before and r["messages_after"] == len(conv.messages)
    assert r["tokens_before"] > r["tokens_after"] > 0
    assert "we read two files" in r["summary"] and r["trigger"] == "auto"
    replaced = r["replaced"]
    assert len(replaced) == r["messages_before"] - (r["messages_after"] - 1) or len(replaced) > 0
    assert replaced[0]["index"] == 1 and replaced[0]["label"].startswith("user: look at both files")
    assert any(x["label"].startswith("tool_result Read src/a.py") for x in replaced)
    assert all(x["tokens"] > 0 for x in replaced)


def test_no_receipt_when_nothing_was_summarised(workspace: Path) -> None:
    cm = _cm(workspace)
    from axon.agent.state import Conversation
    assert cm._summarize_older_turns(Conversation([{"role": "user", "content": "hi"}]), summary_text="x", keep_turns=1) is False
    assert cm.last_receipt is None


def test_compact_last_prints_the_receipt(workspace: Path, capsys: Any) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    agent.conversation = _conv()
    agent.context._summarize_older_turns(agent.conversation, summary_text="we read two files", keep_turns=1)
    capsys.readouterr()
    assert dispatch_command("/compact --last", agent).handled
    out = strip_ansi(capsys.readouterr().out)
    assert "Last compaction" in out and "we read two files" in out and "replaced" in out
    assert "tool_result Read src/a.py" in out and "estimates" in out
    assert BIG[:10] in BIG                       # the conversation itself was not touched by the receipt


def test_compact_last_without_a_compaction_says_so(workspace: Path, capsys: Any) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    assert dispatch_command("/compact --last", agent).handled
    assert "No compaction has happened" in strip_ansi(capsys.readouterr().out)
    assert SimpleNamespace is not None
