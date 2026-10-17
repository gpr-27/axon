"""Step limit: the advance warning, the wrap-up report at the limit, Task max_steps and the exhausted report."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from axon.agent.subagent import AgentDefinition, SubagentManager, _step_limit, run_subagent
from axon.providers.base import AssistantTurn, TextBlock, ToolResultBlock, ToolUseBlock, Usage
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _ls(n: int, prefix: str = "c") -> list[AssistantTurn]:
    return [AssistantTurn(blocks=[ToolUseBlock(id=f"{prefix}{i}", name="Ls", input={"path": "."})], stop_reason="tool_use",
                          usage=Usage(input=10, output=5)) for i in range(n)]


def _report(text: str = "REPORT: found x in a.py:3; left: tests") -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], usage=Usage(input=10, output=20))


def _no_consecutive_users(messages: list[dict[str, Any]]) -> bool:
    roles = [m.get("role") for m in messages]
    return all(not (a == b == "user") for a, b in zip(roles, roles[1:]))


class _BoomProvider(FakeProvider):
    """Raises on the request numbers in *fail_on* (1-based)."""

    def __init__(self, turns: list[AssistantTurn], fail_on: set[int]) -> None:
        super().__init__(turns)
        self.fail_on = fail_on

    def stream(self, **kw: Any):  # type: ignore[override]
        if len(self.requests) + 1 in self.fail_on:
            self.requests.append({"messages": list(kw["messages"]), "tools": kw["tools"]})
            raise RuntimeError("upstream exploded")
        return super().stream(**kw)


class _OpenAIStyleProvider(FakeProvider):
    def encode_tool_results(self, results: list[ToolResultBlock]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": r.tool_use_id, "content": r.content} for r in results]


# ── warning ───────────────────────────────────────────────────────────────

def test_main_agent_is_warned_once_three_steps_before_the_limit(workspace: Path) -> None:
    prov = FakeProvider(_ls(6) + [_report()])
    agent = make_agent(prov, workspace, max_iterations=6)
    agent.run_turn("go")
    last = [json.dumps(r["messages"][-1]) for r in prov.requests]
    warned = [i for i, m in enumerate(last) if "steps left before the step limit" in m]
    assert warned == [3]                                  # results of step 3 go into call 4: steps 4, 5, 6 remain
    assert "3 steps left before the step limit (6)" in last[3]
    assert "1 step left" not in "".join(last)            # only once


def test_subagent_warning_asks_for_its_final_report(workspace: Path) -> None:
    prov = FakeProvider(_ls(4) + [_report()])
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    run_subagent("look around", parent, max_iterations=4, subagent_type="explore", description="look")
    assert any("3 steps left before your step limit (4). Stop exploring soon and write your final report."
               in json.dumps(r["messages"][-1]) for r in prov.requests)


# ── wrap-up (subagents) ───────────────────────────────────────────────────

def test_main_agent_stops_at_exactly_max_iterations_calls(workspace: Path) -> None:
    prov = FakeProvider(_ls(3) + [_report()])
    agent = make_agent(prov, workspace, max_iterations=3)
    res = agent.run_turn("go")
    assert res.stop_reason == "max_iterations" and len(prov.requests) == 3   # --max-turns N means N calls
    assert "maximum iteration ceiling" in res.final_text
    agent.conversation.validate()


def test_step_limit_ends_with_a_wrap_up_report_and_a_valid_history(workspace: Path) -> None:
    prov = FakeProvider(_ls(3) + [_report()])
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=3)
    res = agent.run_turn("go")
    assert res.stop_reason == "max_iterations" and res.iterations == 3 and res.tool_calls_count == 3
    assert res.final_text == "REPORT: found x in a.py:3; left: tests"
    assert res.usage.output == 3 * 5 + 20                 # the wrap-up call is counted
    wrap = prov.requests[3]
    assert wrap["tools"]                                  # tool definitions still sent (tool_use history needs them)
    last = wrap["messages"][-1]
    assert last["role"] == "user" and [b["type"] for b in last["content"]] == ["tool_result", "text"]
    assert "reached the step limit (3 steps)" in last["content"][-1]["text"]
    agent.conversation.validate()
    assert _no_consecutive_users(agent.conversation.messages)
    assert agent.conversation.messages[-1]["role"] == "assistant"


def test_wrap_up_history_is_valid_for_anthropic_and_openai_encodings(workspace: Path) -> None:
    from axon.providers.anthropic import to_anthropic_messages
    from axon.providers.openai_compat import sanitize_openai_messages
    prov = _OpenAIStyleProvider(_ls(2) + [_report()])
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=2)
    agent.run_turn("go")
    sent = prov.requests[2]["messages"]
    assert [m["role"] for m in sent[-2:]] == ["tool", "user"]   # note after the role=tool results
    oa = sanitize_openai_messages(sent)
    assert oa[-1]["role"] == "user" and oa[-2]["role"] == "tool" and "step limit" in oa[-1]["content"]
    an = to_anthropic_messages(sent)
    assert an[-1]["role"] == "user" and [b["type"] for b in an[-2]["content"]] == ["tool_result"]


def test_wrap_up_that_calls_a_tool_falls_back_and_runs_nothing(workspace: Path) -> None:
    prov = FakeProvider(_ls(3) + _ls(1, prefix="extra"))
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=3)
    res = agent.run_turn("go")
    assert res.stop_reason == "max_iterations" and "maximum iteration ceiling" in res.final_text
    assert res.tool_calls_count == 3
    agent.conversation.validate()
    assert "reached the step limit" not in json.dumps(agent.conversation.messages)   # the note was taken back
    assert "extra0" not in json.dumps(agent.conversation.messages)


def test_wrap_up_tool_calls_are_dropped_but_its_text_is_kept(workspace: Path) -> None:
    mixed = AssistantTurn(blocks=[TextBlock(text="REPORT with a stray call"),
                                  ToolUseBlock(id="stray", name="Ls", input={"path": "."})], stop_reason="tool_use")
    prov = FakeProvider(_ls(2) + [mixed])
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=2)
    res = agent.run_turn("go")
    assert res.final_text.startswith("REPORT with a stray call")
    assert "tool_use" not in json.dumps(agent.conversation.messages[-1])
    agent.conversation.validate()


def test_wrap_up_provider_error_falls_back_to_the_cut_off(workspace: Path) -> None:
    prov = _BoomProvider(_ls(3), fail_on={4})
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=3)
    res = agent.run_turn("go")
    assert res.stop_reason == "max_iterations" and "maximum iteration ceiling" in res.final_text
    assert "reached the step limit" not in json.dumps(agent.conversation.messages)
    agent.conversation.validate()


def test_wrap_up_is_skipped_once_the_turn_token_budget_is_spent(workspace: Path) -> None:
    prov = FakeProvider(_ls(2) + [_report()])
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=2, turn_token_budget=30)
    res = agent.run_turn("go")
    assert res.stop_reason == "max_iterations" and len(prov.requests) == 2


def test_resumed_session_after_a_wrap_up_replays_a_valid_history(workspace: Path) -> None:
    prov = FakeProvider(_ls(2) + [_report()])
    agent = make_agent(prov, workspace, agent_name="explore", max_iterations=2)
    sid = agent.session.open("sess_wrap")
    agent.run_turn("go")
    conv = agent.session.read_conversation(sid)
    conv.validate()
    assert [m["role"] for m in conv.messages[-2:]] == ["user", "assistant"]
    assert "REPORT" in json.dumps(conv.messages[-1]) and "reached the step limit" not in json.dumps(conv.messages)


# ── subagents ─────────────────────────────────────────────────────────────

def test_exhausted_subagent_returns_its_report_and_progress(workspace: Path) -> None:
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    prov = FakeProvider(scripted(("Read", {"path": "a.py"}), ("Grep", {"pattern": "x"}), ("Ls", {"path": "."}))
                        + [_report("Found x at a.py:1. Not done: callers.")])
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("find x", parent, max_iterations=3, subagent_type="explore", description="find x")
    lines = out.splitlines()
    assert lines[0].startswith("[Subagent #1 Result (find x) | ") and lines[0].endswith("]:")
    assert lines[1] == "Stopped at the step limit (3 steps) before finishing."
    assert "Found x at a.py:1. Not done: callers." in out
    assert "Progress before the step limit:\n- Read a.py\n- Grep x\n- Ls ." in out
    assert "You have at most 3 steps" in json.dumps(prov.requests[0]["messages"][0])
    task = parent.subagents.all_tasks()[0]
    assert task.status == "exhausted" and task.steps == 3 and task.max_steps == 3
    assert task.timeline[-1]["kind"] != "step" or "writing the final report" in task.timeline[-1]["text"]
    assert any("Step limit (3) reached" in e["text"] for e in task.timeline)


def test_exhausted_subagent_without_a_report_still_lists_its_progress(workspace: Path) -> None:
    prov = FakeProvider(_ls(2) + _ls(1, prefix="more"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    out = run_subagent("look", parent, max_iterations=2, subagent_type="explore", description="look")
    assert "Stopped at the step limit (2 steps) before finishing." in out
    assert "Progress before the step limit:\n- Ls .\n- Ls ." in out


def test_step_limit_resolution_is_clamped_to_the_session_limit(workspace: Path) -> None:
    parent = make_agent(FakeProvider([]), workspace, max_iterations=40)
    d = AgentDefinition(name="x", description="x")
    assert _step_limit(parent, d, None) == 25
    assert _step_limit(parent, d, 7) == 7
    assert _step_limit(parent, d, 999) == 40
    assert _step_limit(parent, d, -3) == 1
    assert _step_limit(parent, AgentDefinition(name="y", description="y", max_turns=100), None) == 40
    assert _step_limit(parent, AgentDefinition(name="y", description="y", max_turns=9), None) == 9
    small = make_agent(FakeProvider([]), workspace, max_iterations=10)
    assert _step_limit(small, d, None) == 10


def test_task_tool_max_steps_sets_the_subagent_limit(workspace: Path) -> None:
    prov = FakeProvider(scripted(("Task", {"prompt": "look", "subagent_type": "explore", "max_steps": 2}))
                        + _ls(2) + [_report("sub report")] + scripted("main done"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    res = parent.run_turn("delegate")
    assert res.final_text == "main done"
    task = parent.subagents.all_tasks()[0]
    assert task.max_steps == 2 and task.status == "exhausted"
    fed = json.dumps(prov.requests[-1]["messages"][-1])
    assert "Stopped at the step limit (2 steps)" in fed and "sub report" in fed


def test_task_tool_schema_and_bad_max_steps(workspace: Path) -> None:
    from axon.errors import ToolError
    from axon.tools.base import ToolContext
    from axon.tools.task import TaskTool
    props = TaskTool.schema["properties"]
    assert props["max_steps"]["type"] == "integer" and "max_steps" not in TaskTool.schema["required"]
    assert "max_steps" in TaskTool.description and "split" in TaskTool.description
    parent = make_agent(FakeProvider([]), workspace)
    ctx = ToolContext(workspace=workspace, file_state=parent.file_state, todos=parent.todos, settings=parent.settings,
                      ledger=parent.ledger, agent=parent)
    with pytest.raises(ToolError):
        TaskTool().run({"prompt": "x", "max_steps": "lots"}, ctx)


def test_restored_session_marks_an_exhausted_subagent(workspace: Path) -> None:
    from axon.agent.subagent import sync_subagents_for_session
    agent = make_agent(FakeProvider([]), workspace)
    agent.session.open("sess_r")
    agent.conversation.messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Task", "input": {"prompt": "look"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content":
            "[Subagent #1 Result (look) | 9 tokens (in: 5 · out: 4)]:\nStopped at the step limit (2 steps) before finishing.\n\nr"}]},
    ]
    sync_subagents_for_session(agent)
    assert agent.subagents.all_tasks()[0].status == "exhausted"


# ── review fixes ──────────────────────────────────────────────────────────

def test_finished_subagent_thread_is_unbound_and_cannot_be_cancelled(workspace: Path) -> None:
    prov = FakeProvider(scripted("done"))
    parent = make_agent(prov, workspace)
    parent.session.open("sess_p")
    run_subagent("x", parent, subagent_type="explore", description="x")
    task = parent.subagents.all_tasks()[0]
    assert parent.subagents._threads == {} and parent.subagents.cancel(task.id) is False


def test_interrupted_fan_out_cancels_its_subagents(workspace: Path, monkeypatch: Any) -> None:
    prov = FakeProvider(scripted([("Task", {"prompt": "a"}), ("Task", {"prompt": "b"})]))
    agent = make_agent(prov, workspace)
    called: list[int] = []
    monkeypatch.setattr(agent.subagents, "cancel_all", lambda: called.append(1) or 0)

    def boom(_tu: Any) -> Any:
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "_run_one", boom)
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn("fan out")
    assert called == [1]
    agent.conversation.validate()


def test_dot_directory_globs_match() -> None:
    from axon.agent.memory import path_matches
    assert path_matches(".github/workflows/ci.yml", [".github/workflows/*.yml"])
    assert path_matches("src/a.py", ["./src/*.py"])
    assert not path_matches("github/workflows/ci.yml", [".github/workflows/*.yml"])


def test_rule_in_a_rules_subfolder_matches_relative_to_the_project(workspace: Path) -> None:
    from axon.agent.memory import MemoryLoader
    rule = workspace / ".axon" / "rules" / "frontend" / "ui.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("---\npaths: \"web/**/*.ts\"\n---\nUI-RULE", encoding="utf-8")
    (workspace / "web").mkdir()
    (workspace / "web" / "app.ts").write_text("x", encoding="utf-8")
    loader = MemoryLoader(workspace, auto_memory=False)
    loader.load_startup(include_user=False, include_managed=False)
    new = loader.on_file_access(workspace / "web" / "app.ts")
    assert [n.reason for n in new] == ["path-rule"] and "UI-RULE" in loader.render(new)


def test_slow_stat_after_a_command_reports_no_changes(workspace: Path, monkeypatch: Any) -> None:
    from axon.agent import filechanges
    (workspace / "a.txt").write_text("a\n", encoding="utf-8")
    snap = filechanges.take_snapshot(workspace)
    assert snap is not None and "a.txt" in snap.stats
    monkeypatch.setattr(filechanges, "_stats", lambda *a, **k: None)
    assert filechanges.changes_since(snap) == []          # not "a.txt deleted"


def test_subagent_worktrees_with_the_same_task_id_do_not_collide(tmp_path: Path) -> None:
    from axon.agent.worktree import WorktreeManager
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        for cmd in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                                             "--allow-empty", "-m", "init"]):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable")
    a = WorktreeManager.create_worktree(repo, "sub-1")
    if a is None and sys.platform == "win32":
        pytest.skip("git worktree add fails on the Windows CI runner (reason not reported by the runner)")
    b = WorktreeManager.create_worktree(repo, "sub-1")
    try:
        assert a is not None and b is not None and a.worktree_path != b.worktree_path
        assert a.worktree_path.exists() and b.worktree_path.exists()
    finally:
        for info in (a, b):
            if info is not None:
                WorktreeManager.cleanup(info)
