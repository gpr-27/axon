"""/receipts: claims in the last answer, each matched to a tool call of that turn (heuristic)."""
from __future__ import annotations

from typing import Any

from axon.features import insights
from axon.ui.theme import strip_ansi


def _use(i: str, name: str, **inp: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}


def _res(i: str, text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": text}]}


def _answer(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _agent(*msgs: dict[str, Any]) -> Any:
    conv = type("C", (), {"messages": [{"role": "user", "content": "do it"}, *msgs]})()
    return type("A", (), {"conversation": conv})()


def test_tests_pass_claim_is_supported_by_a_test_command() -> None:
    a = _agent(_use("1", "Bash", command="python -m pytest -q"), _res("1", "12 passed"), _answer("Done. All tests pass."))
    d = insights.receipts_data(a)
    assert [(c["claim"], c["supported"], c["call_id"]) for c in d["claims"]] == [("All tests pass.", True, "1")]


def test_claim_without_a_supporting_call_is_flagged() -> None:
    a = _agent(_use("1", "Read", file_path="a.py"), _res("1", "x"), _answer("The tests pass and there are no other callers."))
    d = insights.receipts_data(a)
    assert {c["kind"]: c["supported"] for c in d["claims"]} == {"tests": False, "callers": False}


def test_no_other_callers_needs_a_search() -> None:
    a = _agent(_use("1", "Grep", pattern="foo"), _res("1", "a.py:3"), _answer("There are no other callers of foo."))
    assert insights.receipts_data(a)["claims"][0]["supported"] is True


def test_edit_claim_matches_the_edited_path() -> None:
    a = _agent(_use("1", "Edit", file_path="/w/src/app.py", old_string="a", new_string="b"), _res("1", "ok"),
               _answer("I updated `src/app.py` and fixed `other.py`."))
    got = {c["claim"]: c["supported"] for c in insights.receipts_data(a)["claims"]}
    assert list(got.values()) == [True, False]


def test_only_the_last_turn_counts() -> None:
    old = [_use("1", "Bash", command="pytest"), _res("1", "ok"), _answer("tests pass")]
    a = _agent(*old, {"role": "user", "content": "now something else"}, _answer("All tests pass."))
    assert insights.receipts_data(a)["claims"][0]["supported"] is False


def test_failing_run_does_not_support_a_pass_claim() -> None:
    a = _agent(_use("1", "Bash", command="pytest"), _res("1", "3 failed, 9 passed"), _answer("All tests pass."))
    c = insights.receipts_data(a)["claims"][0]
    assert c["supported"] is False and "failures" in c["note"]


def test_no_claims_and_command_output(capsys: Any) -> None:
    a = _agent(_answer("Here is the explanation of the design."))
    assert insights.receipts_data(a)["claims"] == []
    insights.handle_receipts(a, "")
    assert "No checkable claims" in strip_ansi(capsys.readouterr().out)
    b = _agent(_answer("All tests pass."))
    insights.handle_receipts(b, "")
    out = strip_ansi(capsys.readouterr().out)
    assert "heuristic" in out and "no supporting tool call" in out
