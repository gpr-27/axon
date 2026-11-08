"""The insight commands are registered, catalogued and reachable through the dispatcher."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from axon.commands.registry import dispatch_command, resolve_builtin
from axon.commands.usage_menu import menu_kind
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def test_every_insight_command_is_registered_and_catalogued() -> None:
    for name in ("ctxdiff", "whatif", "pin", "unpin", "receipts"):
        assert resolve_builtin(name) is not None, name
        assert menu_kind(name), name


def test_dispatch_runs_ctxdiff_and_whatif(workspace: Path, capsys: Any) -> None:
    agent = make_agent(FakeProvider(scripted(("Ls", {"path": "."}), "done")), workspace, model="claude-sonnet-4-5")
    agent.run_turn("list")
    capsys.readouterr()
    assert dispatch_command("/ctxdiff", agent).handled
    assert "Prompt diff" in strip_ansi(capsys.readouterr().out)
    assert dispatch_command("/whatif claude-haiku-4-5", agent).handled
    assert "pricing calculation" in strip_ansi(capsys.readouterr().out)
