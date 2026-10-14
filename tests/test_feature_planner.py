"""Tests for axon.features.planner (plan parsing, planning call, execution, storage, resume)."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.features import planner
from axon.features.planner import Plan, PlanStep
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from fakes import FakeProvider


def shell_py(code: str) -> str:
    """A shell command string running this interpreter (quoted path; PowerShell needs `&`)."""
    exe = f'"{sys.executable}"'
    try:
        from axon.platform import shell_info
        if shell_info().is_powershell:
            exe = "& " + exe
    except Exception:
        pass
    return f"{exe} -c \"{code}\""


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "axon_home"))
    monkeypatch.setattr(planner, "_interactive", lambda: False)


class FakeAgent:
    def __init__(self, ws: Path, plan_text: str = "", results: list[Any] | None = None) -> None:
        turn = AssistantTurn(blocks=[TextBlock(text=plan_text)], usage=Usage(input=100, output=50))
        self.provider = FakeProvider([turn])
        self.settings = SimpleNamespace(workspace=ws, model="fake-model", effort=None)
        self.ledger = Ledger()
        self.conversation = SimpleNamespace(messages=[])
        self.traces: list[Any] = []
        self.prompts: list[str] = []
        self.results = list(results or [])

    def run_turn(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        self.ledger.record("fake-model", Usage(input=1, output=1), source="main", cost=Decimal("0.005"))
        step = SimpleNamespace(kind="tool", data={"name": "Edit"})
        self.traces.append(SimpleNamespace(steps=[step]))
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        return ok()


def ok(text: str = "did it") -> Any:
    return SimpleNamespace(final_text=text, stop_reason="end_turn", tool_calls_count=1, error=None)


PLAN_JSON = {"goal": "Add feature", "steps": [
    {"title": "Inspect code", "detail": "read src", "verify": ""},
    {"title": "Implement", "detail": "edit src/app.py", "verify": "`%s`"},
]}


# ── parsing ────────────────────────────────────────────────────────────────
def test_extract_json_from_fence_and_trailing_text() -> None:
    text = "Here you go:\n```json\n" + json.dumps(PLAN_JSON) + "\n```\nLet me know!"
    goal, steps = planner.extract_plan_json(text)
    assert goal == "Add feature" and [s.title for s in steps] == ["Inspect code", "Implement"]


def test_extract_json_bare_with_prose_and_trailing_comma() -> None:
    text = 'Sure. {"goal": "G", "steps": [{"title": "A {x}", "detail": "d"},]} trailing'
    goal, steps = planner.extract_plan_json(text)
    assert goal == "G" and steps[0].title == "A {x}"


def test_numbered_list_fallback() -> None:
    text = "Goal: fix login\n1. Reproduce the bug - run the test\n   Verify: `pytest -q`\n2) Patch auth.py\n   handle None user\n"
    goal, steps = planner.parse_plan_text(text)
    assert goal == "fix login"
    assert steps[0].title == "Reproduce the bug" and steps[0].detail == "run the test"
    assert steps[0].verify == "`pytest -q`"
    assert steps[1].title == "Patch auth.py" and "None user" in steps[1].detail


def test_unparseable_returns_none() -> None:
    assert planner.parse_plan_text("I cannot help with that.") is None


def test_verify_command_extraction() -> None:
    assert planner.verify_command("`pytest -q tests/test_a.py`") == "pytest -q tests/test_a.py"
    assert planner.verify_command("run npm test.") == "npm test"
    assert planner.verify_command("the page renders correctly") is None
    assert planner.verify_command("") is None


def test_apply_edit() -> None:
    p = Plan("x", "t", "g", [PlanStep("a"), PlanStep("b")])
    assert planner.apply_edit(p, "edit", 0, "A2 -- new detail") and p.steps[0].title == "A2" and p.steps[0].detail == "new detail"
    assert planner.apply_edit(p, "add", 1, "mid") and [s.title for s in p.steps] == ["A2", "mid", "b"]
    assert planner.apply_edit(p, "delete", 2) and len(p.steps) == 2
    assert not planner.apply_edit(p, "delete", 9)


# ── planning call ──────────────────────────────────────────────────────────
def test_create_plan_uses_toolless_call_and_records_cost(tmp_path: Path) -> None:
    agent = FakeAgent(tmp_path, json.dumps(PLAN_JSON))
    plan = planner.create_plan(agent, "add feature")
    req = agent.provider.requests[0]
    assert req["tools"] == [] and "STRICT JSON" in req["system"][0]["text"]
    assert "add feature" in req["messages"][0]["content"]
    assert len(plan.steps) == 2 and agent.ledger.breakdown()[0]["source"] == "planner"


def test_create_plan_bad_output_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        planner.create_plan(FakeAgent(tmp_path, "nope"), "x")


# ── execution ──────────────────────────────────────────────────────────────
def test_plan_run_non_tty_without_yes_saves_draft(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = FakeAgent(tmp_path, json.dumps(PLAN_JSON))
    planner.cmd_plan_run(agent, "add feature")
    out = capsys.readouterr().out
    assert "Re-run with --yes" in out and agent.prompts == []
    plans = planner.list_plans(tmp_path)
    assert len(plans) == 1 and plans[0].status == "draft"
    assert planner.plans_dir(tmp_path).is_relative_to(tmp_path / "axon_home")
    assert not (tmp_path / ".axon").exists()


def test_plan_run_yes_executes_with_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = json.loads(json.dumps(PLAN_JSON))
    data["steps"][1]["verify"] = f"`{shell_py('print(1)')}`"
    agent = FakeAgent(tmp_path, json.dumps(data))
    planner.cmd_plan_run(agent, "add feature --yes")
    out = capsys.readouterr().out
    assert len(agent.prompts) == 2
    assert "CURRENT STEP 2/2: Implement" in agent.prompts[1] and "COMPLETED STEPS" in agent.prompts[1]
    assert "LATER STEPS" in agent.prompts[0]
    assert "verify passed" in out and "Plan done" in out
    saved = planner.list_plans(tmp_path)[0]
    assert saved.status == "done" and all(s.status == "done" for s in saved.steps)
    assert saved.steps[0].tools == ["Edit"] and saved.steps[0].cost == pytest.approx(0.005)


def _plan(ws: Path, verify: str = "") -> Plan:
    p = Plan(planner.new_plan_id(), "task", "goal", [PlanStep("one"), PlanStep("two", verify=verify), PlanStep("three")],
             workspace=str(ws))
    planner.save_plan(ws, p)
    return p


def test_failed_verify_retry_then_success(tmp_path: Path) -> None:
    flag = tmp_path / "flag"
    code = "import sys,os; sys.exit(0 if os.path.exists('flag') else 1)"
    cmd = shell_py(code)
    plan = _plan(tmp_path, verify=f"`{cmd}`")
    agent = FakeAgent(tmp_path)
    calls: list[int] = []

    def choose(step: PlanStep, i: int) -> str:
        calls.append(i)
        flag.write_text("x", encoding="utf-8")
        return "retry"

    result = planner.execute_plan(agent, plan, tmp_path, choose=choose)
    assert calls == [1] and result.status == "done"
    assert "PREVIOUS ATTEMPT FAILED" in agent.prompts[2]
    assert result.steps[1].attempts == 2


def test_failed_step_skip_and_abort(tmp_path: Path) -> None:
    fail = SimpleNamespace(final_text="", stop_reason="error", tool_calls_count=0, error="boom")
    plan = _plan(tmp_path)
    agent = FakeAgent(tmp_path, results=[ok(), fail, ok()])
    res = planner.execute_plan(agent, plan, tmp_path, choose=lambda s, i: "skip")
    assert [s.status for s in res.steps] == ["done", "skipped", "done"] and res.status == "done"

    plan2 = _plan(tmp_path)
    agent2 = FakeAgent(tmp_path, results=[fail])
    res2 = planner.execute_plan(agent2, plan2, tmp_path, choose=lambda s, i: "abort")
    assert res2.status == "aborted" and res2.steps[0].status == "failed" and len(agent2.prompts) == 1


def test_retry_limit(tmp_path: Path) -> None:
    fail = SimpleNamespace(final_text="", stop_reason="error", tool_calls_count=0, error="boom")
    plan = _plan(tmp_path)
    agent = FakeAgent(tmp_path, results=[fail] * 10)
    res = planner.execute_plan(agent, plan, tmp_path, choose=lambda s, i: "retry", max_retries=2)
    assert len(agent.prompts) == 3 and res.status == "aborted"


def test_ctrl_c_then_resume(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _plan(tmp_path)
    agent = FakeAgent(tmp_path, results=[ok(), KeyboardInterrupt()])
    res = planner.execute_plan(agent, plan, tmp_path)
    assert res.status == "aborted" and [s.status for s in res.steps] == ["done", "pending", "pending"]
    assert "Stopped by Ctrl+C" in capsys.readouterr().out

    agent2 = FakeAgent(tmp_path)
    planner.cmd_plan_resume(agent2, plan.id[:15])
    assert len(agent2.prompts) == 2 and "CURRENT STEP 2/3" in agent2.prompts[0]
    assert planner.load_plan(tmp_path, plan.id).status == "done"


def test_resume_latest_unfinished_and_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planner.cmd_plan_resume(FakeAgent(tmp_path), "")
    assert "No unfinished plans" in capsys.readouterr().out
    planner.cmd_plan_resume(FakeAgent(tmp_path), "missing")
    assert "No plan" in capsys.readouterr().out
    _plan(tmp_path)
    agent = FakeAgent(tmp_path)
    planner.cmd_plan_resume(agent, "")
    assert len(agent.prompts) == 3


def test_plans_listing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planner.cmd_plans(FakeAgent(tmp_path), "")
    assert "No saved plans" in capsys.readouterr().out
    p = _plan(tmp_path)
    planner.cmd_plans(FakeAgent(tmp_path), "")
    out = capsys.readouterr().out
    assert p.id in out and "0/3" in out and "draft" in out


def test_plan_run_usage_and_provider_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    planner.cmd_plan_run(FakeAgent(tmp_path), "")
    assert "Usage" in capsys.readouterr().out
    agent = FakeAgent(tmp_path)
    agent.provider = None
    planner.cmd_plan_run(agent, "do x --yes")
    assert "Could not create a plan" in capsys.readouterr().out


def test_commands_registered() -> None:
    cmds = {c.name: c for c in planner.COMMANDS}
    assert set(cmds) == {"/plan-run", "/plans", "/plan-resume"} and "/solve" in cmds["/plan-run"].aliases
