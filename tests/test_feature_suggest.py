"""Tests for axon.features.suggest (next-step suggestions, /next)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.agent.state import TodoState
from axon.agent.trace import Step, TurnTrace
from axon.features import suggest as sg
from axon.ui.theme import glyph, strip_ansi

ALL = {"/fix", "/autofix", "/verify", "/test", "/commit", "/compact", "/stats", "/cost", "/init"}


def avail(names=ALL):
    return lambda n: n in names


def _trace(steps: list[tuple[str, dict]], turn: int = 1, cost: float = 0.0) -> TurnTrace:
    t = TurnTrace(turn=turn, prompt="p", model="m", mode="default")
    for i, (kind, data) in enumerate(steps, 1):
        t.steps.append(Step(index=i, kind=kind, started_at=0.0, data=data))
    t.cost = cost
    return t


def edit(path: str = "src/app.py", error: bool = False) -> tuple[str, dict]:
    return "tool", {"name": "Edit", "input": {"path": path}, "is_error": error}


def bash(cmd: str, result: str, error: bool = False) -> tuple[str, dict]:
    return "tool", {"name": "Bash", "input": {"command": cmd}, "result_preview": result, "is_error": error}


def agent_for(traces, ws: Path, todos=None):
    state = TodoState()
    if todos:
        state.replace(todos)
    return SimpleNamespace(traces=list(traces), todos=state, settings=SimpleNamespace(workspace=ws))


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    (d / "AXON.md").write_text("# memory\n", encoding="utf-8")
    return d


def cmds(sugs):
    return [s.command for s in sugs]


def test_no_trace_no_suggestions(ws: Path) -> None:
    assert sg.suggest_next(agent_for([], ws), available=avail(), check_git=False) == []


def test_edit_without_tests_suggests_verify(ws: Path) -> None:
    a = agent_for([_trace([edit()])], ws)
    sugs = sg.suggest_next(a, available=avail(), check_git=False)
    assert cmds(sugs) == ["/verify"]
    assert "1 file(s) edited" in sugs[0].reason


def test_verify_falls_back_to_test_command(ws: Path) -> None:
    a = agent_for([_trace([edit()])], ws)
    assert cmds(sg.suggest_next(a, available=avail(ALL - {"/verify"}), check_git=False)) == ["/test"]


def test_failed_tests_suggest_fix_not_verify(ws: Path) -> None:
    t = _trace([edit(), bash("python -m pytest -q", "Command exited with non-zero code 1.\nOutput:\n1 failed, 3 passed")])
    sugs = sg.suggest_next(agent_for([t], ws), available=avail(), check_git=False)
    assert cmds(sugs)[0] == "/fix"
    assert "/verify" not in cmds(sugs)


def test_passing_tests_do_not_suggest_verify(ws: Path) -> None:
    t = _trace([edit(), bash("npm test", "ℹ pass 4\nℹ fail 0")])
    assert sg.suggest_next(agent_for([t], ws), available=avail(), check_git=False) == []


def test_fix_falls_back_to_plain_prompt(ws: Path) -> None:
    t = _trace([bash("go test ./...", "--- FAIL: TestX")])
    sugs = sg.suggest_next(agent_for([t], ws), available=avail(set()), check_git=False)
    assert sugs[0].is_prompt and "failing tests" in sugs[0].command


def test_recovered_tool_error_is_ignored_but_final_error_is_not(ws: Path) -> None:
    recovered = _trace([("tool", {"name": "Read", "is_error": True}), ("tool", {"name": "Read", "is_error": False})])
    assert sg.suggest_next(agent_for([recovered], ws), available=avail(), check_git=False) == []
    final = _trace([("tool", {"name": "Bash", "input": {"command": "make"}, "is_error": True})])
    sugs = sg.suggest_next(agent_for([final], ws), available=avail(), check_git=False)
    assert cmds(sugs) == ["/fix"] and "Bash" in sugs[0].reason


def test_failed_turn_result_suggests_fix(ws: Path) -> None:
    result = SimpleNamespace(stop_reason="error", error="boom")
    sugs = sg.suggest_next(agent_for([_trace([])], ws), result, available=avail(), check_git=False)
    assert cmds(sugs) == ["/fix"]


def test_pending_todos_suggest_continue(ws: Path) -> None:
    todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"},
             {"content": "c", "status": "in_progress"}]
    sugs = sg.suggest_next(agent_for([_trace([])], ws, todos), available=avail(), check_git=False)
    assert sugs[0].label == "continue" and sugs[0].is_prompt and "2" in sugs[0].command


def test_high_context_suggests_compact(ws: Path) -> None:
    t = _trace([("llm", {"context_used": 170_000, "context_window": 200_000})])
    sugs = sg.suggest_next(agent_for([t], ws), available=avail(), check_git=False)
    assert cmds(sugs) == ["/compact"] and "85%" in sugs[0].reason


def test_cost_spike_suggests_stats_or_cost(ws: Path) -> None:
    traces = [_trace([], 1, 0.01), _trace([], 2, 0.02), _trace([], 3, 0.30)]
    assert cmds(sg.suggest_next(agent_for(traces, ws), available=avail(), check_git=False)) == ["/stats"]
    assert cmds(sg.suggest_next(agent_for(traces, ws), available=avail(ALL - {"/stats"}), check_git=False)) == ["/cost"]
    cheap = [_trace([], 1, 0.01), _trace([], 2, 0.02)]
    assert sg.suggest_next(agent_for(cheap, ws), available=avail(), check_git=False) == []


def test_first_turn_without_axon_md_suggests_init(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    sugs = sg.suggest_next(agent_for([_trace([])], bare), available=avail(), check_git=False)
    assert cmds(sugs) == ["/init"]
    second = agent_for([_trace([]), _trace([], 2)], bare)
    assert sg.suggest_next(second, available=avail(), check_git=False) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_dirty_git_after_edit_suggests_commit(ws: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "app.py").write_text("x = 1\n", encoding="utf-8")
    sugs = sg.suggest_next(agent_for([_trace([edit("app.py")])], ws), available=avail())
    assert cmds(sugs) == ["/verify", "/commit"]


def test_max_three_and_deterministic(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    t = _trace([edit(), ("llm", {"context_used": 90, "context_window": 100}),
                ("tool", {"name": "Bash", "input": {"command": "ls"}, "is_error": True})])
    a = agent_for([t], bare, [{"content": "b", "status": "pending"}])
    first = sg.suggest_next(a, available=avail(), check_git=False)
    assert len(first) == 3
    assert first == sg.suggest_next(a, available=avail(), check_git=False)
    assert cmds(first)[:2] == ["/fix", "Continue with the remaining 1 todo item(s)."]


def test_render_suggestions() -> None:
    assert sg.render_suggestions([]) == ""
    line = strip_ansi(sg.render_suggestions([sg.Suggestion("/verify", "/verify", "r"),
                                             sg.Suggestion("/commit", "/commit", "r")]))
    assert line.strip() == f"Next: [1] /verify {glyph('dot')} [2] /commit"


def test_run_suggestion_prompt_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    agent = SimpleNamespace(run_turn=lambda text: seen.append(("turn", text)) or "done")
    assert sg.run_suggestion(agent, sg.Suggestion("continue", "Keep going.", "r")) == "done"
    import axon.commands.builtin as builtin
    monkeypatch.setattr(builtin, "dispatch_command", lambda line, ag: seen.append(("cmd", line)) or "ok")
    assert sg.run_suggestion(agent, sg.Suggestion("/compact", "/compact", "r")) == "ok"
    assert seen == [("turn", "Keep going."), ("cmd", "/compact")]


def test_next_command_runs_numbered_choice(ws: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    ran = []
    monkeypatch.setattr(sg, "run_suggestion", lambda agent, s: ran.append(s.command))
    monkeypatch.setattr(sg, "command_available", avail())
    monkeypatch.setattr(sg, "git_dirty", lambda ws, timeout=2.0: False)
    a = agent_for([_trace([edit()])], ws)
    sg.handle_next(a, "1")
    assert ran == ["/verify"]
    sg.handle_next(a, "")  # non-interactive: lists with reasons, runs nothing
    out = strip_ansi(capsys.readouterr().out)
    assert "Suggested next steps" in out and "no tests were run" in out and ran == ["/verify"]


def test_feature_contract() -> None:
    assert [c.name for c in sg.COMMANDS] == ["/next"]
