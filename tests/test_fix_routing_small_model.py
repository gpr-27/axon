"""Side models (hooks, compaction, title, subagents, budget downgrade) go to their own provider; subagent bookkeeping."""
from __future__ import annotations

import gc
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from axon.agent import loop as loop_mod
from axon.agent.loop import make_provider
from axon.agent.state import Conversation
from axon.agent.subagent import SubagentManager, sync_subagents_for_session
from axon.config import Settings
from axon.providers import endpoints as ep
from axon.providers.catalog import PROVIDER_PRESETS
from axon.providers.registry import resolve_route, settings_for_model
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "axon_home"
    home_dir.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home_dir))
    for preset in PROVIDER_PRESETS:
        for var in preset.env_vars:
            monkeypatch.setenv(var, "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-1234567890")
    ep.apply_endpoint_overrides({})
    yield home_dir
    ep.apply_endpoint_overrides({})


def anthropic_settings(workspace: Path, **extra: Any) -> Settings:
    return Settings(workspace=workspace, mode="bypass", provider="anthropic", base_url="https://api.anthropic.com",
                    api_key=SecretStr("sk-ant-1234567890"), model="claude-opus-4-8", **extra)


def anthropic_agent(workspace: Path, **extra: Any) -> Any:
    settings = anthropic_settings(workspace, **extra)
    agent = make_agent(make_provider(settings.model, settings), workspace)
    agent.settings = settings
    return agent


# ─── 1. side models route to their own provider ──────────────────────────

def test_other_providers_model_leaves_the_main_provider(workspace: Path) -> None:
    settings = anthropic_settings(workspace)
    routed = settings_for_model("gpt-5", settings)
    assert resolve_route("gpt-5", routed).preset.id == "openai"
    assert resolve_route("gpt-5", routed).api_key == "sk-openai-1234567890"
    assert resolve_route("gpt-5", settings).preset.id == "anthropic"      # the bug: the main provider won


def test_model_of_the_active_provider_and_the_main_model_stay_put(workspace: Path) -> None:
    settings = anthropic_settings(workspace)
    assert settings_for_model("claude-haiku-4-5", settings) is settings
    assert settings_for_model(settings.model, settings) is settings


def test_make_provider_builds_the_side_model_on_its_own_endpoint(workspace: Path) -> None:
    settings = anthropic_settings(workspace)
    provider = make_provider("gpt-5", settings, role="hook")
    assert "openai.com" in str(provider.settings.base_url)
    assert provider.settings.model == "gpt-5"
    assert "anthropic" in str(make_provider("claude-haiku-4-5", settings).settings.base_url)


def test_compaction_provider_follows_the_small_model(workspace: Path) -> None:
    agent = anthropic_agent(workspace, small_model="gpt-5")
    provider, model = agent._compaction_provider()
    assert model == "gpt-5"
    assert "openai.com" in str(provider.settings.base_url)


def test_budget_downgrade_moves_to_the_small_models_provider(workspace: Path) -> None:
    from axon.providers.base import Usage
    agent = anthropic_agent(workspace, small_model="gpt-5", budget_turn_usd=0.0000001, budget_action="downgrade")
    agent._budget_guard([{"type": "text", "text": "x" * 4000}], [], 1, 0, Usage(), "")
    assert agent.settings.model == "gpt-5" and agent.settings.provider == "openai"
    assert "openai.com" in str(agent.provider.settings.base_url)


def test_title_provider_is_rebuilt_for_the_current_provider(workspace: Path) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    agent.settings = Settings(workspace=workspace, mode="bypass", provider="openai", base_url="https://api.openai.com/v1",
                              api_key=SecretStr("sk-openai-1234567890"), model="gpt-5", small_model="claude-haiku-4-5")
    stale = make_provider("gpt-5", agent.settings)
    fresh = agent._current_title_provider("claude-haiku-4-5", stale)
    assert fresh is not stale
    assert "anthropic" in str(fresh.settings.base_url)


def test_subagent_with_another_providers_model_gets_that_provider(workspace: Path) -> None:
    from axon.agent.subagent import _make_provider
    parent = anthropic_agent(workspace)
    routed = settings_for_model("gpt-5", parent.settings).model_copy(update={"model": "gpt-5"})
    assert "openai.com" in str(_make_provider(parent, "gpt-5", routed).settings.base_url)


# ─── 2. subagent numbers come from the result header ─────────────────────

def task_call(call_id: str, prompt: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": "Task", "input": {"prompt": prompt}}


def test_sync_numbers_subagents_from_the_result_header() -> None:
    from unittest.mock import MagicMock
    agent = MagicMock()
    agent.subagents = SubagentManager()
    # Subagents 1 and 2 were compacted away: only the calls for #3 and #4 are left.
    agent.conversation = Conversation([
        {"role": "assistant", "content": [task_call("c3", "third"), task_call("c4", "fourth")]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c3", "content": "[Subagent #3 Result (third) | 10 tokens]:\nok"},
            {"type": "tool_result", "tool_use_id": "c4", "content": "[Subagent #4 (fourth) failed: boom]"},
        ]},
    ])
    sync_subagents_for_session(agent)
    tasks = {t.title: t for t in agent.subagents.all_tasks()}
    assert (tasks["third"].index, tasks["third"].id) == (3, "sub-3")
    assert (tasks["fourth"].index, tasks["fourth"].status) == (4, "error")
    assert agent.subagents.register("next").index == 5


def test_sync_falls_back_to_counting_without_a_header() -> None:
    from unittest.mock import MagicMock
    agent = MagicMock()
    agent.subagents = SubagentManager()
    agent.conversation = Conversation([
        {"role": "assistant", "content": [task_call("a", "first"), task_call("b", "second")]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "plain text"},
            {"type": "tool_result", "tool_use_id": "b", "content": "more text"},
        ]},
    ])
    sync_subagents_for_session(agent)
    assert [t.index for t in agent.subagents.all_tasks()] == [1, 2]


# ─── 3. cancelled stays cancelled ────────────────────────────────────────

def test_fail_does_not_overwrite_cancelled() -> None:
    manager = SubagentManager()
    task = manager.register("work")
    task.status = "cancelled"
    manager.fail(task.id, "boom")
    assert task.status == "cancelled"
    other = manager.register("other")
    manager.fail(other.id, "boom")
    assert other.status == "error" and other.error_msg == "boom"


# ─── 4. a broken live monitor must not cancel the batch ──────────────────

def test_monitor_failure_does_not_cancel_the_task_batch(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.providers.base import ToolUseBlock
    from axon.ui import subagent_monitor

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("monitor exploded")

    monkeypatch.setattr(subagent_monitor, "run_live_subagent_monitor", broken)
    agent = make_agent(FakeProvider([]), workspace, events=[])
    agent.session.open("sess_monitor")
    monkeypatch.setattr(agent, "_interactive_monitor", lambda: True)
    journal: list[tuple[str, Any]] = []
    monkeypatch.setattr(agent, "journal", lambda kind, data=None, sync=False: journal.append((kind, data)))

    results = agent._execute_batch([ToolUseBlock(id="t1", name="Task", input={"prompt": "say hi"})])

    assert len(results) == 1 and not results[0].is_error
    assert any("monitor exploded" in str(data) for _, data in journal)


# ─── 5. provider locks are pruned ────────────────────────────────────────

def test_provider_locks_are_dropped_with_the_provider() -> None:
    provider = FakeProvider([])
    key = id(provider)
    lock = loop_mod._provider_lock(provider)
    assert loop_mod._provider_lock(provider) is lock
    assert key in loop_mod._PROVIDER_LOCKS
    del provider
    gc.collect()
    assert key not in loop_mod._PROVIDER_LOCKS


def test_provider_lock_works_for_objects_that_cannot_be_weakly_referenced() -> None:
    class Slotted:
        __slots__ = ()

    assert isinstance(loop_mod._provider_lock(Slotted()), type(threading.RLock()))
