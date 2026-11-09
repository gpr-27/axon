"""A fan-out started from a real terminal must reach the web page while it runs, not after it ends.

The terminal's live subagent monitor owns the screen during a fan-out, so the agent holds its events back from the
terminal renderer. That hold must not delay the page, which draws nothing into the terminal.
"""
from __future__ import annotations

from concurrent.futures import wait
from pathlib import Path
from typing import Any

import pytest

from axon.features.dash_runtime import Runtime
from axon.providers.base import ToolExecutionStart, ToolUseBlock
from axon.ui import subagent_monitor
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "axon_home"
    home_dir.mkdir()
    monkeypatch.setenv("AXON_HOME", str(home_dir))
    return home_dir


def _page_events(rt: Runtime) -> list[dict[str, Any]]:
    return [e for _, e in rt.hub.read(0, 0.01)[0]]


def _fan_out(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run one Task under a stand-in monitor and record what each side had seen when the monitor closed."""
    terminal: list[Any] = []
    agent = make_agent(FakeProvider([]), workspace, events=terminal)
    agent.session.open("sess_live")
    rt = Runtime("live", agent, workspace, external=True)
    rt.attach()
    seen: dict[str, Any] = {"rt": rt, "terminal": terminal}

    def monitor(future_map: Any, *args: Any, **kwargs: Any) -> None:
        wait(list(future_map))
        seen["page_during"] = [e["type"] for e in _page_events(rt)]
        seen["terminal_during"] = [type(e).__name__ for e in terminal]

    monkeypatch.setattr(subagent_monitor, "run_live_subagent_monitor", monitor)
    monkeypatch.setattr(agent, "_interactive_monitor", lambda: True)
    agent._execute_batch([ToolUseBlock(id="t1", name="Task", input={"prompt": "say hi", "description": "Say hi"})])
    return seen


def test_page_gets_task_and_subagent_events_while_the_monitor_is_open(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    during = _fan_out(workspace, monkeypatch)["page_during"]
    assert "tool_use" in during
    assert "subagent" in during


def test_terminal_still_gets_nothing_until_the_monitor_closes(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fan_out(workspace, monkeypatch)
    assert "ToolExecutionStart" not in seen["terminal_during"]
    assert any(isinstance(e, ToolExecutionStart) for e in seen["terminal"])


def test_page_does_not_get_held_events_a_second_time(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events = _page_events(_fan_out(workspace, monkeypatch)["rt"])
    assert [e["type"] for e in events].count("tool_use") == 1
    assert [e["type"] for e in events].count("tool_result") == 1
