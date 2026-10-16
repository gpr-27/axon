"""Behaviour of slash commands that are awkward to drive end to end (stdin-driven /paste,
in-session /focus, /find's query, non-interactive /delete, /prompt arguments, status-bar gauge)."""
from __future__ import annotations

import re
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


def _turn(text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input=10, output=5))


def _make(workspace: Path, texts: list[str] | None = None) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    return Agent(provider=FakeProvider([_turn(t) for t in (texts or [f"answer {i}" for i in range(10)])]),
                 tools=create_default_registry(), permissions=PermissionEngine(s), context=ContextManager(s),
                 session=SessionStore(workspace), ledger=Ledger(), settings=s)


@pytest.fixture
def agent(workspace: Path) -> Agent:
    return _make(workspace)


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return strip_ansi(capsys.readouterr().out)


def _user_prompts(agent: Agent) -> list[str]:
    return [m["content"] for m in agent.conversation.messages
            if m.get("role") == "user" and isinstance(m.get("content"), str)]


# ─── /find ─────────────────────────────────────────────────────────────────

def test_find_uses_the_query(agent: Agent, workspace: Path, capsys) -> None:
    """`/find <query>` used to ignore its argument (non-interactively it returned the first file)."""
    (workspace / "aaa.txt").write_text("a\n", encoding="utf-8")
    (workspace / "src" / "deep").mkdir(parents=True)
    (workspace / "src" / "deep" / "widget_factory.py").write_text("x = 1\n", encoding="utf-8")
    from axon.ui import fuzzy_picker
    fuzzy_picker._cache.clear()
    dispatch_command("/find widfac", agent)
    assert "@src/deep/widget_factory.py" in _out(capsys)
    dispatch_command("/find zzzqqq", agent)
    assert "No workspace file matches 'zzzqqq'" in _out(capsys)


def test_fuzzy_finder_prefills_query(workspace: Path) -> None:
    (workspace / "a.txt").write_text("", encoding="utf-8")
    (workspace / "zeta_module.py").write_text("", encoding="utf-8")
    from axon.ui.fuzzy_picker import run_fuzzy_file_finder
    assert run_fuzzy_file_finder(workspace, "zetmod") == "zeta_module.py"
    assert run_fuzzy_file_finder(workspace, "qqqq-nothing") is None


# ─── /delete ───────────────────────────────────────────────────────────────

def test_delete_without_id_is_refused_without_a_terminal(agent: Agent, capsys) -> None:
    """Non-interactively the picker returns its first entry, so a bare /delete silently deleted
    the latest session. It must print usage and keep every session."""
    agent.run_turn("hello")
    before = [m.session_id for m in agent.session.list_recent(limit=None)]
    assert before
    capsys.readouterr()
    dispatch_command("/delete", agent)
    assert "Usage: /delete" in _out(capsys)
    assert [m.session_id for m in agent.session.list_recent(limit=None)] == before


# ─── /prompt ───────────────────────────────────────────────────────────────

def test_prompt_template_args_keep_spaces_and_quotes() -> None:
    from axon.commands.memory_cmds import parse_template_args as p
    assert p("description=login fails", ["description"]) == ({"description": "login fails"}, [])
    assert p('description="login fails" now', ["description"]) == ({"description": "login fails now"}, [])
    assert p("target=src/a.py goal=make it fast", ["target", "goal"]) == (
        {"target": "src/a.py", "goal": "make it fast"}, [])
    assert p("login fails", ["description"]) == ({}, ["login", "fails"])
    assert p(r"target=C:\src\a.py", ["target"]) == ({"target": r"C:\src\a.py"}, [])


def test_prompt_fills_template_with_multi_word_value(agent: Agent) -> None:
    dispatch_command("/prompt bugfix description=login fails on Safari", agent)
    prompts = _user_prompts(agent)
    assert prompts and "Investigate and fix this bug: login fails on Safari." in prompts[-1], prompts


def test_prompt_single_placeholder_takes_free_words(agent: Agent) -> None:
    dispatch_command("/prompt bugfix crash on save", agent)
    assert "Investigate and fix this bug: crash on save." in _user_prompts(agent)[-1]


# ─── /paste ────────────────────────────────────────────────────────────────

def test_paste_sends_lines_until_end(agent: Agent, monkeypatch, capsys) -> None:
    lines = iter(["first line", "  second line", "END", "not sent"])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    dispatch_command("/paste", agent)
    assert _user_prompts(agent)[-1] == "first line\n  second line"
    assert "Sending 24 chars" in _out(capsys)


def test_paste_eof_cancels_and_empty_is_ignored(agent: Agent, monkeypatch, capsys) -> None:
    def eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    dispatch_command("/paste", agent)
    assert "Paste cancelled" in _out(capsys)
    lines = iter(["   ", "END"])
    monkeypatch.setattr("builtins.input", lambda *a: next(lines))
    dispatch_command("/paste", agent)
    assert "Empty paste ignored" in _out(capsys)
    assert _user_prompts(agent) == []


# ─── /focus ────────────────────────────────────────────────────────────────

def test_focus_switches_to_compact_and_restores_previous_trace(agent: Agent, capsys) -> None:
    from axon.commands.model_cmds import current_trace_level
    dispatch_command("/trace detailed", agent)
    dispatch_command("/focus", agent)
    assert current_trace_level(agent) == "compact"
    assert "Focus view on" in _out(capsys)
    dispatch_command("/focus on", agent)
    assert "already on" in _out(capsys)
    dispatch_command("/focus", agent)
    assert current_trace_level(agent) == "detailed"
    assert "Focus view off: trace detailed" in _out(capsys)


# ─── /statusbar ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("used", [9_600, 9_700, 64_000, 127_999, 0])
def test_status_bar_context_percentages_agree(used: int) -> None:
    """The bar used to print 'ctx 8%' next to '(7%)' for the same usage (round vs truncate)."""
    from axon.ui.statusbar import StatusBar
    bar = strip_ansi(StatusBar.format_bar("gpt-4o", "high", "bypass", context_tokens=used, context_capacity=128_000))
    a = re.search(r"ctx (\d+)%", bar)
    b = re.search(r"\((\d+)%\)", bar)
    assert a and b, bar
    assert a.group(1) == b.group(1), bar


def test_statusbar_command_prints_model_mode_and_capacity(agent: Agent, capsys) -> None:
    dispatch_command("/statusbar", agent)
    out = _out(capsys)
    assert agent.settings.model in out and "default" in out, out


# ─── /goal ─────────────────────────────────────────────────────────────────

def test_goal_reaches_the_model_request(agent: Agent) -> None:
    dispatch_command("/goal all tests pass ZQ-GOAL", agent)
    agent.run_turn("go")
    req = agent.provider.requests[-1]
    text = repr(req["system"]) + repr(req["messages"])
    assert "ZQ-GOAL" in text, "goal is not sent to the model"
    dispatch_command("/goal clear", agent)
    agent.run_turn("again")
    req = agent.provider.requests[-1]
    assert "ZQ-GOAL" not in repr(req["system"]) + repr(req["messages"][-1:])
