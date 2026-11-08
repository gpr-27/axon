"""/ctxdiff: what entered and left the prompt between two model calls."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from axon.agent.trace import CacheTracker
from axon.features import insights
from axon.providers.base import Usage
from axon.ui.theme import strip_ansi
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

SYS = [{"type": "text", "text": "You are Axon. " * 20}]
TOOLS = [{"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}]


def _msgs(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"role": "user", "content": "fix the bug in app.py"}, *extra]


def test_diff_lists_entered_and_left_blocks_with_token_deltas() -> None:
    a = insights.build_manifest(SYS, TOOLS, _msgs())
    big = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 4000}]}
    use = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "app.py"}}]}
    b = insights.build_manifest([{"type": "text", "text": "different system"}], TOOLS, _msgs(use, big))
    d = insights.diff_manifests(a, b)
    kinds = {(e["kind"], e["label"][:13]) for e in d["entered"]}
    assert ("message", "tool_result R") in kinds and any(e["kind"] == "system" for e in d["entered"])
    assert [e["kind"] for e in d["left"]] == ["system"]
    assert d["entered_tokens"] > 1000 and d["left_tokens"] > 0
    assert d["unchanged"] == 2           # the tools and the first user message


def test_identical_prompts_have_an_empty_diff() -> None:
    a = insights.build_manifest(SYS, TOOLS, _msgs())
    d = insights.diff_manifests(a, insights.build_manifest(SYS, TOOLS, _msgs()))
    assert d["entered"] == [] and d["left"] == []


def test_cache_control_markers_do_not_count_as_changes() -> None:
    marked = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
    plain = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    d = insights.diff_manifests(insights.build_manifest(SYS, TOOLS, marked), insights.build_manifest(SYS, TOOLS, plain))
    assert d["entered"] == [] and d["left"] == []


def test_tracker_records_one_manifest_per_call_with_cache_outcome() -> None:
    tr = CacheTracker()
    for read in (0, 900):
        fp = tr.fingerprint("m", "", SYS, TOOLS, _msgs())
        tr.observe(fp, Usage(input=1000, cache_read=read))
    assert [m["n"] for m in tr.manifests] == [1, 2]
    assert tr.manifests[1]["usage"]["cache_read"] == 900 and tr.manifests[0]["miss"] is True


def _agent(workspace: Path) -> Any:
    return make_agent(FakeProvider(scripted(("Ls", {"path": "."}), "done")), workspace, model="claude-sonnet-4-5")


def test_command_reports_the_last_two_calls(workspace: Path, capsys: Any) -> None:
    agent = _agent(workspace)
    agent.run_turn("list files")
    capsys.readouterr()
    insights.handle_ctxdiff(agent, "")
    out = strip_ansi(capsys.readouterr().out)
    assert "call 2 vs call 1" in out and "entered" in out and "tool_result" in out
    assert "estimates" in out


def test_command_with_n_and_without_history(workspace: Path, capsys: Any) -> None:
    agent = _agent(workspace)
    insights.handle_ctxdiff(agent, "")
    assert "No model call has been recorded" in strip_ansi(capsys.readouterr().out)
    agent.run_turn("list files")
    capsys.readouterr()
    insights.handle_ctxdiff(agent, "1")
    assert "first call of the session" in strip_ansi(capsys.readouterr().out)
    insights.handle_ctxdiff(agent, "9")
    assert "only 2 calls" in strip_ansi(capsys.readouterr().out)
