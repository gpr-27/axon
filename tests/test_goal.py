"""Done-when goals: the grader, the revise loop inside one run, limits, persistence, the usage row and /goal."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axon.agent import goal as G
from axon.agent.trace import GoalCheckEvent
from axon.commands.registry import dispatch_command
from axon.providers.base import AssistantTurn, TextBlock, Usage
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

GOAL = "x.txt exists and the tests pass"


def verdict(kind: str, *, gap: str = "tests were not run", evidence: str = "x.txt written") -> AssistantTurn:
    met = kind == "satisfied"
    body = {"per_criterion": [
        {"criterion": "x.txt exists", "met": True, "evidence": evidence, "gap": ""},
        {"criterion": "the tests pass", "met": met, "evidence": "ran pytest" if met else "", "gap": "" if met else gap}],
        "verdict": kind, "summary": "all good" if met else "tests missing"}
    return AssistantTurn(blocks=[TextBlock(text=json.dumps(body))], stop_reason="end_turn", usage=Usage(input=700, output=90))


def text_turn(text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input=60, output=10))


def goal_agent(workspace: Path, turns: list[AssistantTurn], rounds: int | None = None):
    events: list[Any] = []
    prov = FakeProvider(turns)
    agent = make_agent(prov, workspace, events=events)
    dispatch_command(f"/goal {GOAL}", agent)
    if rounds is not None:
        dispatch_command(f"/goal budget {rounds}", agent)
    return agent, prov, events


def goal_events(events: list[Any], phase: str = "verdict") -> list[GoalCheckEvent]:
    return [e for e in events if isinstance(e, GoalCheckEvent) and e.phase == phase]


def work() -> list[Any]:
    return scripted(("Write", {"path": "x.txt", "content": "hi\n"}), "I wrote x.txt and ran the tests.")


def test_satisfied_first_time(workspace: Path) -> None:
    agent, prov, events = goal_agent(workspace, [*work(), verdict("satisfied")])
    res = agent.run_turn("make x.txt")
    assert res.final_text == "I wrote x.txt and ran the tests."
    assert len(prov.requests) == 3                         # tool call, answer, grader
    assert G.goal_text(agent) == "" and agent.settings.goal is None   # met: the goal is cleared
    ev = goal_events(events)
    assert len(ev) == 1 and ev[0].verdict == "satisfied" and ev[0].note == "goal met" and ev[0].round == 1
    assert ev[0].cost >= 0 and ev[0].usage.input == 700    # the grader's own usage rides on the event


def test_grader_runs_in_a_fresh_context_with_objective_evidence(workspace: Path) -> None:
    agent, prov, _ = goal_agent(workspace, [*work(), verdict("satisfied")])
    agent.run_turn("make x.txt")
    req = prov.requests[2]
    assert req["tools"] == [] and len(req["messages"]) == 1          # no shared history, no tools
    sys_text = repr(req["system"])
    assert "strict" in sys_text and "per_criterion" in sys_text and "failed_unrecoverable" in sys_text
    body = req["messages"][0]["content"]
    assert GOAL in body and "make x.txt" in body and "I wrote x.txt" in body
    assert "x.txt" in body.split("Objective evidence")[1] and "hi" in body.split("Objective evidence")[1]


def test_needs_revision_then_satisfied_in_the_same_turn(workspace: Path) -> None:
    turns = [*work(), verdict("needs_revision", gap="GAP-ZQ tests were not run"), text_turn("Ran the tests, they pass."),
             verdict("satisfied")]
    agent, prov, events = goal_agent(workspace, turns)
    res = agent.run_turn("make x.txt")
    assert res.final_text == "Ran the tests, they pass."
    assert len(agent.traces) == 1                                   # one turn, not two
    follow = prov.requests[3]["messages"][-1]["content"]            # the next MAIN request carries the gaps
    assert follow.startswith(G.FOLLOWUP_MARK) and "GAP-ZQ tests were not run" in follow and "round 1 of 3" in follow
    assert "x.txt exists" not in follow.split("not met yet")[1]      # met criteria are not repeated
    ev = goal_events(events)
    assert [e.verdict for e in ev] == ["needs_revision", "satisfied"] and [e.round for e in ev] == [1, 2]
    assert G.goal_text(agent) == ""


def test_round_limit_stops_and_names_unmet_criteria(workspace: Path) -> None:
    turns = [*work(), verdict("needs_revision"), text_turn("second try"), verdict("needs_revision", gap="still no tests"),
             text_turn("must not be reached")]
    agent, prov, events = goal_agent(workspace, turns, rounds=2)
    res = agent.run_turn("make x.txt")
    assert res.final_text == "second try" and len(prov.requests) == 5   # 2 main + 2 grader + 1 tool call
    last = goal_events(events)[-1]
    assert last.round == 2 and last.max_rounds == 2 and "round limit" in last.note
    unmet = [c for c in last.criteria if not c["met"]]
    assert [c["criterion"] for c in unmet] == ["the tests pass"] and unmet[0]["gap"] == "still no tests"
    assert G.goal_text(agent) == GOAL and agent.goal_last["verdict"] == "needs_revision"   # kept for the next turn


def test_invalid_grader_json_retries_once_then_needs_revision(workspace: Path) -> None:
    turns = [*work(), text_turn("not json at all"), text_turn("{still: broken"), text_turn("Checked again."), verdict("satisfied")]
    agent, prov, events = goal_agent(workspace, turns)
    res = agent.run_turn("make x.txt")
    assert "RESPONSE_NOT_NEEDED" not in res.final_text and res.final_text == "Checked again."
    retry_body = prov.requests[3]["messages"][0]["content"]
    assert "not valid JSON" in retry_body                           # the single retry says why
    ev = goal_events(events)
    assert ev[0].verdict == "needs_revision" and "valid JSON" in ev[0].summary and ev[0].criteria == ()
    follow = prov.requests[4]["messages"][-1]["content"]
    assert follow.startswith(G.FOLLOWUP_MARK) and "could not verify" in follow
    assert ev[0].usage.input == 120                                 # both attempts are paid for and counted


def test_valid_json_in_a_code_fence_is_accepted(workspace: Path) -> None:
    fenced = text_turn("```json\n" + verdict("satisfied").text + "\n```")
    agent, prov, events = goal_agent(workspace, [*work(), fenced])
    agent.run_turn("make x.txt")
    assert len(prov.requests) == 3 and goal_events(events)[0].verdict == "satisfied"


class InterruptedGrader(FakeProvider):
    def stream(self, **kw: Any):
        if len(self.requests) == 2:                                  # the third call is the grader
            self.requests.append(kw)
            raise KeyboardInterrupt()
        yield from super().stream(**kw)


def test_esc_during_the_check_cancels_it(workspace: Path) -> None:
    events: list[Any] = []
    prov = InterruptedGrader(work())
    agent = make_agent(prov, workspace, events=events)
    dispatch_command(f"/goal {GOAL}", agent)
    res = agent.run_turn("make x.txt")
    assert res.stop_reason == "interrupted" and res.final_text == "I wrote x.txt and ran the tests."
    assert len(prov.requests) == 3                                    # no revise round
    assert goal_events(events)[0].verdict == "interrupted"
    assert G.goal_text(agent) == GOAL                                 # an interrupted check does not clear the goal


def test_read_only_turn_is_not_graded(workspace: Path) -> None:
    prov = FakeProvider(scripted("Python 3.12 is in use."))
    events: list[Any] = []
    agent = make_agent(prov, workspace, events=events)
    dispatch_command(f"/goal {GOAL}", agent)
    res = agent.run_turn("which python?")
    assert res.final_text == "Python 3.12 is in use." and len(prov.requests) == 1
    assert goal_events(events, "skipped")[0].note.startswith("read-only")


def test_goal_that_asks_for_an_answer_is_graded_on_a_read_only_turn(workspace: Path) -> None:
    prov = FakeProvider([text_turn("It is 3.12."), verdict("satisfied")])
    agent = make_agent(prov, workspace)
    dispatch_command("/goal explain which python is used", agent)
    agent.run_turn("which python?")
    assert len(prov.requests) == 2
    body = prov.requests[1]["messages"][0]["content"]
    assert "no files were written and no commands were run" in body    # the grader is told there was nothing to check


def test_grader_never_sees_secrets(workspace: Path) -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    turns = scripted(("Write", {"path": "x.txt", "content": f"API_KEY={secret}\n"}), f"Wrote it with {secret}.")
    agent, prov, _ = goal_agent(workspace, [*turns, verdict("satisfied")])
    agent.run_turn(f"use {secret}")
    assert secret not in json.dumps(prov.requests[2]["messages"])


def test_evidence_is_bounded(workspace: Path) -> None:
    big = "line of output\n" * 5000
    (workspace / "big.txt").write_text(big)
    turns = scripted(("Write", {"path": "big.txt", "content": big}), ("Bash", {"command": "echo hi"}), "done")
    agent, prov, _ = goal_agent(workspace, [*turns, verdict("satisfied")])
    agent.run_turn("make it")
    assert len(prov.requests[-1]["messages"][0]["content"]) < G.EVIDENCE_CHARS + 9_000


def test_subagent_is_never_graded(workspace: Path) -> None:
    prov = FakeProvider([*work()])
    agent = make_agent(prov, workspace, agent_name="worker", extra_system="sub")
    agent.is_subagent = True
    agent.goal = GOAL
    agent.run_turn("make x.txt")
    assert len(prov.requests) == 2


def test_goal_persists_across_resume(workspace: Path) -> None:
    turns = [*work(), verdict("needs_revision", gap="no tests"), text_turn("trying"), verdict("needs_revision", gap="no tests")]
    agent, _, _ = goal_agent(workspace, turns, rounds=2)
    agent.run_turn("make x.txt")
    sid = agent.session.active_session_id
    fresh = make_agent(FakeProvider([]), workspace)
    assert G.goal_text(fresh) == ""
    from axon.session.store import restore_retry_state
    restore_retry_state(fresh, sid)
    assert G.goal_text(fresh) == GOAL and G.goal_rounds(fresh) == 2
    assert fresh.settings.goal == GOAL
    assert fresh.goal_last["verdict"] == "needs_revision" and "no tests" in "\n".join(G.describe(fresh))


def test_cleared_goal_stays_cleared_after_resume(workspace: Path) -> None:
    agent, _, _ = goal_agent(workspace, [])
    dispatch_command("/goal clear", agent)
    fresh = make_agent(FakeProvider([]), workspace)
    fresh.goal = "stale"
    G.restore_goal(fresh, agent.session.active_session_id)
    assert G.goal_text(fresh) == ""


def test_usage_row_is_its_own_row_in_the_turn_table(workspace: Path) -> None:
    from axon.ui.goal_block import goal_block_rows, goal_usage_row
    from axon.ui.render import step_usage, turn_summary_rows
    agent, _, events = goal_agent(workspace, [*work(), verdict("satisfied")])
    agent.run_turn("make x.txt")
    ev = goal_events(events)[0]
    row = goal_usage_row(ev)
    assert row and row["step"] == "goal check" and row["input"] == 700 and row["output"] == 90
    rows = turn_summary_rows([step_usage(1, "m", 1.0, Usage(input=100, output=50), 0.001), row], 120)
    assert sum("goal check" in r for r in rows) == 1
    block = "\n".join(goal_block_rows(ev, 100))
    assert "GOAL" in block and "goal met" in block and "round 1/3" in block and "x.txt exists" in block


def test_block_rows_show_gap_for_unmet_criteria(workspace: Path) -> None:
    from axon.ui.goal_block import goal_block_rows
    ev = GoalCheckEvent(goal=GOAL, phase="verdict", round=1, max_rounds=3, verdict="needs_revision",
                        criteria=({"criterion": "the tests pass", "met": False, "evidence": "", "gap": "GAP-ROW"},))
    text = "\n".join(goal_block_rows(ev, 80))
    assert "not done yet" in text and "GAP-ROW" in text and "round 1/3" in text
    assert goal_block_rows(GoalCheckEvent(goal=GOAL, phase="start")) == []


def test_goal_command_parsing() -> None:
    p = G.parse_goal_arg
    assert p("") == ("show", None) and p("  clear ") == ("clear", None) and p("off") == ("clear", None)
    assert p("budget 5") == ("budget", 5) and p("budget") == ("budget", None) and p("budget abc") == ("budget", None)
    assert p("budget=4") == ("budget", 4)
    assert p("all tests pass") == ("set", "all tests pass")
    assert p("- a passes\n- b passes") == ("set", "- a passes - b passes")


def test_goal_command_budget_and_show(workspace: Path, capsys) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/goal tests pass", agent)
    assert agent.goal == "tests pass" and agent.settings.goal == "tests pass"
    assert G.goal_rounds(agent) == 3
    dispatch_command("/goal budget 99", agent)
    assert G.goal_rounds(agent) == G.MAX_ROUNDS
    dispatch_command("/goal budget 0", agent)
    assert G.goal_rounds(agent) == G.MAX_ROUNDS                    # invalid: unchanged
    dispatch_command("/goal budget 2", agent)
    capsys.readouterr()
    dispatch_command("/goal", agent)
    out = capsys.readouterr().out
    assert "tests pass" in out and "up to 2" in out and "Not checked yet" in out
    dispatch_command("/goal clear", agent)
    assert agent.goal is None and G.goal_text(agent) == ""


def test_parse_verdict_is_robust() -> None:
    assert G.parse_verdict("") is None and G.parse_verdict("nope") is None and G.parse_verdict("[1,2]") is None
    v = G.parse_verdict('Sure! {"per_criterion":[{"criterion":"a","met":false,"gap":"g"}],"verdict":"satisfied"} done')
    assert v and v["verdict"] == "needs_revision"                      # "satisfied" next to an unmet criterion is not trusted
    v = G.parse_verdict('{"per_criterion":[{"criterion":"a","met":true}]}')
    assert v and v["verdict"] == "satisfied"                           # missing verdict is derived from the criteria
    assert G.parse_verdict('{"verdict":"needs_revision","per_criterion":[]}')["verdict"] == "needs_revision"


def test_web_state_carries_the_goal(workspace: Path) -> None:
    from axon.features.dash_js_chatx import JS_CHATX
    assert "goalCheckEvent" in JS_CHATX and "goalChipDraw" in JS_CHATX
    from axon.features.sdk import event_to_dict
    d = event_to_dict(GoalCheckEvent(goal=GOAL, phase="verdict", round=1, max_rounds=3, verdict="satisfied",
                                     usage=Usage(input=5, output=2), criteria=({"criterion": "a", "met": True},)))
    assert d["type"] == "goal_check" and d["usage"]["input_tokens"] == 5 and d["criteria"][0]["met"] is True
