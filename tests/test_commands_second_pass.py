"""Slash commands, second pass: commands that accepted bad input, hid a failure, or said something untrue."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.ui.theme import strip_ansi
from fakes import FakeProvider


def _agent(workspace: Path, text: str = "ok", usage: Usage | None = None, **settings: object) -> Agent:
    s = Settings(workspace=workspace, mode="default", **settings)
    turn = AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=usage or Usage(input=10, output=5))
    return Agent(provider=FakeProvider([turn] * 8), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)


@pytest.fixture
def agent(workspace: Path) -> Agent:
    return _agent(workspace)


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return strip_ansi(capsys.readouterr().out)


# ─── /model ──────────────────────────────────────────────────────────────────

def test_model_refuses_a_name_no_provider_knows(agent: Agent, capsys) -> None:
    """'/model bogus-xyz' was accepted and saved as the default; the next turn then failed at the provider."""
    before = agent.settings.model
    dispatch_command("/model bogus-xyz", agent)
    out = _out(capsys)
    assert agent.settings.model == before
    assert "bogus-xyz" in out and "--force" in out and before in out


def test_model_suggests_the_close_name(agent: Agent, capsys) -> None:
    dispatch_command("/model deepseek-v4-flsh", agent)
    assert "deepseek-v4-flash" in _out(capsys)


def test_model_force_switches_to_an_unlisted_name(agent: Agent, capsys) -> None:
    dispatch_command("/model bogus-xyz --force", agent)
    assert agent.settings.model == "bogus-xyz"


def test_model_accepts_any_name_on_local_and_unregistered_endpoints(workspace: Path, capsys) -> None:
    """A local server or a gateway Axon has no list for decides its own model names."""
    local = _agent(workspace, base_url="http://localhost:11434/v1", provider="ollama")
    dispatch_command("/model my-finetune", local)
    assert local.settings.model == "my-finetune"
    gateway = _agent(workspace, base_url="http://127.0.0.1:9/v1")
    dispatch_command("/model house-model", gateway)
    assert gateway.settings.model == "house-model"


def test_model_switch_says_what_changes(agent: Agent, capsys) -> None:
    before = agent.settings.model
    dispatch_command("/model claude-sonnet-4-5", agent)
    out = _out(capsys)
    assert "claude-sonnet-4-5" in out and f"was {before}" in out and "next message" in out


# ─── /resume ─────────────────────────────────────────────────────────────────

def test_resume_finds_a_renamed_session_by_its_title(agent: Agent, capsys) -> None:
    agent.run_turn("hello one")
    sid = agent.session.active_session_id
    dispatch_command("/rename My Fancy Title", agent)
    dispatch_command("/clear", agent)
    assert agent.session.active_session_id != sid
    capsys.readouterr()
    dispatch_command("/resume fancy title", agent)
    assert agent.session.active_session_id == sid, _out(capsys)


def test_resume_lists_the_sessions_an_ambiguous_title_matches(agent: Agent, capsys) -> None:
    agent.run_turn("one")
    dispatch_command("/rename Parser work", agent)
    dispatch_command("/clear", agent)
    agent.run_turn("two")
    dispatch_command("/rename Parser tests", agent)
    dispatch_command("/clear", agent)
    current = agent.session.active_session_id
    capsys.readouterr()
    dispatch_command("/resume parser", agent)
    out = _out(capsys)
    assert agent.session.active_session_id == current
    assert "Parser work" in out and "Parser tests" in out


# ─── /compact ────────────────────────────────────────────────────────────────

def test_compact_keeps_the_conversation_when_the_summary_is_not_smaller(workspace: Path, capsys) -> None:
    """A tiny chat was replaced by a longer summary, in memory only, and the command then said 'Nothing to compact yet'."""
    agent = _agent(workspace, text="a summary that is much longer than the two short messages it replaces " * 3)
    agent.run_turn("hi")
    agent.run_turn("yo")
    before = [dict(m) for m in agent.conversation.messages]
    capsys.readouterr()
    dispatch_command("/compact", agent)
    out = _out(capsys)
    assert agent.conversation.messages == before
    assert "Nothing to compact" not in out
    assert "not smaller" in out and "unchanged" in out
    assert agent.context.last_receipt is None            # /compact --last must not describe a compaction that was undone


def test_compact_says_what_went_in_and_what_came_out(workspace: Path, capsys) -> None:
    agent = _agent(workspace, text="short")
    for i in range(3):
        agent.run_turn(f"question {i} " + "with plenty of words to make the older turns worth summarising " * 6)
    capsys.readouterr()
    dispatch_command("/compact", agent)
    out = _out(capsys)
    assert "Compacted 6 → 4 messages" in out
    assert "4 older messages" in out and "kept the latest 2" in out
    assert "Summary call" in out and "10 in" in out and "5 out" in out


# ─── /cost ───────────────────────────────────────────────────────────────────

def test_cost_calls_n_shows_that_call(agent: Agent, capsys) -> None:
    """'/cost calls 1' ignored the number and printed the session breakdown."""
    agent.run_turn("one")
    agent.run_turn("two")
    capsys.readouterr()
    dispatch_command("/cost calls 2", agent)
    out = _out(capsys)
    assert "Call #2" in out and "Session cost" not in out and "#01" not in out
    dispatch_command("/cost calls 9", agent)
    assert "No call #9" in _out(capsys)


# ─── /export ─────────────────────────────────────────────────────────────────

def test_export_rejects_an_unknown_format(agent: Agent, workspace: Path, capsys) -> None:
    """'/export bogus' wrote a Markdown file named 'bogus' into the workspace."""
    agent.run_turn("one")
    capsys.readouterr()
    res = dispatch_command("/export bogus", agent)
    out = _out(capsys)
    assert not (workspace / "bogus").exists()
    assert res.success is False and "bogus" in out and "md" in out and "json" in out


def test_export_still_takes_a_file_name(agent: Agent, workspace: Path, capsys) -> None:
    agent.run_turn("one")
    dispatch_command("/export notes.md", agent)
    dispatch_command("/export out/chat.json", agent)
    assert (workspace / "notes.md").exists() and (workspace / "out" / "chat.json").exists()


# ─── /rewind ─────────────────────────────────────────────────────────────────

def test_rewind_that_cannot_be_saved_changes_nothing(agent: Agent, capsys, monkeypatch) -> None:
    """The write of the rewind record failed silently: the chat looked rewound, and came back whole on resume."""
    agent.run_turn("one")
    agent.run_turn("two")
    before = [dict(m) for m in agent.conversation.messages]

    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(agent.session, "append_rewind", boom)
    capsys.readouterr()
    dispatch_command("/rewind to 2 conversation", agent)
    out = _out(capsys)
    assert agent.conversation.messages == before
    assert "disk full" in out and "Nothing was changed" in out and "✓" not in out


def test_rewind_says_what_it_undid_and_survives_resume(agent: Agent, capsys) -> None:
    agent.run_turn("one")
    agent.run_turn("two")
    sid = agent.session.active_session_id
    capsys.readouterr()
    dispatch_command("/rewind to 2 conversation", agent)
    out = _out(capsys)
    assert "2 message(s) removed" in out and "2 left" in out and "Prompt was: two" in out
    assert len(SessionStore(agent.settings.workspace).load(sid).messages) == 2


# ─── transcripts ─────────────────────────────────────────────────────────────

def test_one_hour_cache_writes_survive_a_transcript_round_trip(workspace: Path) -> None:
    """cache_write_1h was not saved, so a ledger rebuilt on resume priced 1-hour cache writes at the 5-minute rate."""
    from axon.providers.pricing import cost_for
    usage = Usage(input=120_000, output=10, cache_write=100_000, cache_write_1h=100_000)
    agent = _agent(workspace, usage=usage, model="claude-sonnet-4-5")
    agent.run_turn("one")
    rebuilt = agent.session.load_ledger(agent.session.active_session_id, agent.settings.model)
    assert rebuilt.entries[0].usage.cache_write_1h == 100_000
    assert float(rebuilt.entries[0].cost) == pytest.approx(float(cost_for("claude-sonnet-4-5", usage)))
