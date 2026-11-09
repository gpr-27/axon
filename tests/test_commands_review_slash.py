"""Slash-command review: options the usage text offers must run, and /config must refuse values that break the session."""
from __future__ import annotations

from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands import usage_menu
from axon.commands.builtin import dispatch_command
from axon.commands.registry import resolve_builtin
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.ui.theme import strip_ansi
from fakes import FakeProvider


def _agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    turn = AssistantTurn(blocks=[TextBlock(text="ok")], stop_reason="end_turn", usage=Usage(input=1, output=1))
    return Agent(provider=FakeProvider([turn] * 5), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)


@pytest.fixture
def agent(workspace: Path) -> Agent:
    return _agent(workspace)


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return strip_ansi(capsys.readouterr().out)


def test_memory_choices_keep_the_scope_flag(workspace: Path) -> None:
    """'[--scope global|project|repo]' made '/memory project' and '/memory repo' choices; both only print usage."""
    cmd = resolve_builtin("memory")
    words = [c.words for c in usage_menu.usage_choices("memory", cmd.usage)]
    assert "--scope project" in words and "--scope repo" in words
    assert "project" not in words and "repo" not in words


def test_model_choices_do_not_offer_the_literal_usage_text(workspace: Path) -> None:
    """/model random[:small] became a choice that switched to a model named 'random[:small]'."""
    cmd = resolve_builtin("model")
    words = [c.words for c in usage_menu.usage_choices("model", cmd.usage)]
    assert not any("[" in w for w in words)
    assert "random" in words and "random:small" in words


def test_config_small_model_can_be_cleared(agent: Agent, capsys) -> None:
    """'/config small_model none' stored the text 'none', and compaction and titles then asked for a model called none."""
    dispatch_command("/config small_model gpt-mini", agent)
    assert agent.settings.small_model == "gpt-mini"
    capsys.readouterr()
    dispatch_command("/config small_model none", agent)
    assert agent.settings.small_model is None
    assert "main model" in _out(capsys)


def test_config_base_url_must_look_like_a_url(agent: Agent, capsys) -> None:
    before = agent.settings.base_url
    dispatch_command("/config base_url notaurl", agent)
    assert agent.settings.base_url == before
    assert "http" in _out(capsys)
    dispatch_command("/config base_url https://example.test/v1", agent)
    assert agent.settings.base_url == "https://example.test/v1"


def test_config_number_error_is_readable(agent: Agent, capsys) -> None:
    dispatch_command("/config max_tokens abc", agent)
    out = _out(capsys)
    assert "int()" not in out and "invalid literal" not in out
    assert "whole number" in out


def test_diff_staged_outside_a_repo_says_so(agent: Agent, capsys) -> None:
    """Outside git, 'git diff --cached' printed git's own 'unknown option `cached'' error."""
    dispatch_command("/diff staged", agent)
    out = _out(capsys)
    assert "unknown option" not in out
    assert "not a git repository" in out.lower()
