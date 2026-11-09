"""Tab at a permission prompt: a comment for the model. Keys typed before the prompt can be read still answer nothing."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axon import platform as plat
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock
from axon.ui import approve
from axon.ui.approve import Answer, ask_approval
from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


@pytest.fixture
def prompt(monkeypatch: pytest.MonkeyPatch) -> Any:
    """ask_approval on a scripted keyboard. Records the order of the settle delay, the flush and the first key read."""
    def run(keys: list[str], tool: str = "Bash", args: dict[str, Any] | None = None) -> Any:
        script = iter(keys)
        monkeypatch.setattr(approve.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "read_key", lambda timeout=None: next(script))
        monkeypatch.setattr(approve.time, "sleep", lambda s: None)
        return ask_approval(tool, args or {"command": "ls"})
    return run


def _chars(text: str) -> list[str]:
    return list(text)


def test_tab_on_yes_then_enter_answers_with_the_comment(prompt: Any) -> None:
    res = prompt(["tab", *_chars("use -la"), "enter"])
    assert res == "once" and res.comment == "use -la"


def test_tab_on_no_then_enter_declines_with_the_reason(prompt: Any) -> None:
    res = prompt(["down", "down", "tab", *_chars("wrong dir"), "enter"])
    assert res == "deny" and res.comment == "wrong dir"


def test_empty_field_answers_without_a_comment(prompt: Any) -> None:
    res = prompt(["tab", "enter"])
    assert res == "once" and not isinstance(res, Answer)


def test_keys_in_the_field_are_text_never_answers(prompt: Any) -> None:
    res = prompt(["down", "down", "tab", "y", "a", "n", "1", "2", "3", "j", "k", "up", "down", "enter"])
    assert res == "deny" and res.comment == "yan123jk"          # focus stayed on No; arrows did nothing


def test_tab_closes_keeps_the_text_and_answering_that_option_sends_it(prompt: Any) -> None:
    res = prompt(["tab", *_chars("note"), "tab", "enter"])
    assert res == "once" and res.comment == "note"


def test_shift_tab_closes_the_field_like_tab(prompt: Any) -> None:
    res = prompt(["tab", "x", "shift-tab", "y"])                # with the field closed, y answers again
    assert res == "once" and res.comment == "x"


def test_a_comment_stays_with_its_option(prompt: Any) -> None:
    res = prompt(["tab", *_chars("for yes"), "tab", "down", "down", "enter"])
    assert res == "deny" and not isinstance(res, Answer)


def test_each_option_keeps_its_own_comment(prompt: Any) -> None:
    res = prompt(["tab", "a", "tab", "down", "down", "tab", "b", "tab", "up", "up", "enter"])
    assert res == "once" and res.comment == "a"


def test_backspace_and_clear_edit_the_comment(prompt: Any) -> None:
    res = prompt(["tab", "a", "b", "backspace", "ctrl-u", "c", "d", "backspace", "enter"])
    assert res.comment == "c"


def test_a_pasted_comment_is_one_clean_line(prompt: Any) -> None:
    res = prompt(["tab", f"{plat.PASTE_PREFIX}line one\nline two\x1b[31m", "enter"])
    assert res == "once" and res.comment.startswith("line one line two") and "\n" not in res.comment and "\x1b" not in res.comment


def test_esc_in_the_field_declines_without_a_comment(prompt: Any) -> None:
    res = prompt(["tab", *_chars("half a note"), "esc"])
    assert res == "deny" and not isinstance(res, Answer)


def test_esc_with_a_saved_comment_declines_without_it(prompt: Any) -> None:
    res = prompt(["down", "down", "tab", "x", "tab", "esc"])
    assert res == "deny" and not isinstance(res, Answer)


def test_session_option_takes_no_comment(prompt: Any) -> None:
    res = prompt(["down", "tab", "enter"])
    assert res == "always" and not isinstance(res, Answer)


def test_rule_option_takes_no_comment(prompt: Any) -> None:
    res = prompt(["down", "down", "down", "tab", "enter"], args={"command": "git status"})
    assert res == "rule" and not isinstance(res, Answer)


@pytest.mark.parametrize("tool, args", [("WebFetch", {"url": "https://example.com"}), ("Http", {"url": "https://example.com"})])
def test_web_prompts_offer_no_field(prompt: Any, tool: str, args: dict[str, Any]) -> None:
    res = prompt(["tab", "y"], tool=tool, args=args)            # Tab does nothing, so y still answers
    assert res == "once" and not isinstance(res, Answer)


def test_the_settle_delay_and_flush_still_come_before_any_key(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(approve.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(approve.time, "sleep", lambda s: order.append(f"sleep {s}"))
    monkeypatch.setattr(plat, "flush_input", lambda: order.append("flush"))

    def read_key(timeout: Any = None) -> str:
        order.append("key")
        return "esc"
    monkeypatch.setattr(plat, "read_key", read_key)
    assert ask_approval("Bash", {"command": "ls"}) == "deny"
    assert approve._SETTLE_SECONDS == 0.35 and order[:3] == ["sleep 0.35", "flush", "key"]


# ─── what the model receives ───────────────────────────────────────────────

def _turn_with_bash(workspace: Path, answer: Any) -> tuple[Any, list[str]]:
    seen: list[str] = []
    provider = FakeProvider([
        AssistantTurn(blocks=[ToolUseBlock(id="b1", name="Bash", input={"command": "echo ran-it > out.txt"})], stop_reason="tool_use"),
        AssistantTurn(blocks=[TextBlock(text="done")], stop_reason="end_turn"),
    ])
    agent = make_agent(provider, workspace, on_approval=lambda tool, args, decision: answer, mode="default")
    agent.run_turn("go")
    for m in agent.conversation.messages:
        if isinstance(m.get("content"), list):
            seen += [str(b.get("content")) for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        elif m.get("role") == "tool":
            seen.append(str(m.get("content")))
    return agent, seen


def test_yes_runs_then_the_comment_follows_the_result(tmp_path: Path) -> None:
    _, results = _turn_with_bash(tmp_path, Answer("once", "use the staging db"))
    assert (tmp_path / "out.txt").exists()
    assert len(results) == 1 and "use the staging db" in results[0] and "approved" in results[0]


def test_no_sends_the_comment_as_the_reason(tmp_path: Path) -> None:
    _, results = _turn_with_bash(tmp_path, Answer("deny", "not on main"))
    assert not (tmp_path / "out.txt").exists()
    assert len(results) == 1 and "declined" in results[0] and "not on main" in results[0]


def _declined_turn(workspace: Path, answer: Any, blocks: list[Any] | None = None, **attrs: Any) -> tuple[Any, Any, FakeProvider]:
    provider = FakeProvider([
        AssistantTurn(blocks=blocks or [ToolUseBlock(id="b1", name="Bash", input={"command": "echo ran-it > out.txt"})], stop_reason="tool_use"),
        AssistantTurn(blocks=[TextBlock(text="carried on")], stop_reason="end_turn"),
    ])
    agent = make_agent(provider, workspace, on_approval=lambda tool, args, decision: answer, mode="default")
    agent.decline_ends_turn = True
    for k, v in attrs.items():
        setattr(agent, k, v)
    return agent, agent.run_turn("go"), provider


def test_a_plain_no_stops_the_turn_and_keeps_the_conversation_valid(tmp_path: Path) -> None:
    agent, res, _ = _declined_turn(tmp_path, "deny")
    assert res.stop_reason == "declined" and "carried on" not in res.final_text and not (tmp_path / "out.txt").exists()
    results = [b for m in agent.conversation.messages if isinstance(m.get("content"), list) for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(results) == 1 and "declined" in str(results[0].get("content"))


def test_no_with_a_comment_lets_the_model_carry_on(tmp_path: Path) -> None:
    _, res, _ = _declined_turn(tmp_path, Answer("deny", "use staging"))
    assert res.stop_reason == "end_turn" and res.final_text == "carried on"


def test_where_no_person_answers_a_plain_no_does_not_stop_the_turn(tmp_path: Path) -> None:
    _, res, _ = _declined_turn(tmp_path, "deny", decline_ends_turn=False)        # the SDK's callback
    assert res.stop_reason == "end_turn" and res.final_text == "carried on"


def test_a_subagents_plain_no_does_not_stop_its_turn(tmp_path: Path) -> None:
    _, res, _ = _declined_turn(tmp_path, "deny", is_subagent=True)
    assert res.stop_reason != "declined"


def test_a_plain_no_also_stops_the_rest_of_that_step(tmp_path: Path) -> None:
    calls = [ToolUseBlock(id="b1", name="Bash", input={"command": "echo a > a.txt"}),
             ToolUseBlock(id="b2", name="Bash", input={"command": "echo b > b.txt"})]
    asked: list[str] = []

    def answer(tool: str, args: dict[str, Any], decision: Any) -> str:
        asked.append(args["command"])
        return "deny"
    provider = FakeProvider([AssistantTurn(blocks=calls, stop_reason="tool_use"), AssistantTurn(blocks=[TextBlock(text="no")], stop_reason="end_turn")])
    agent = make_agent(provider, tmp_path, on_approval=answer, mode="default")
    agent.decline_ends_turn = True
    res = agent.run_turn("go")
    assert res.stop_reason == "declined" and len(asked) == 1 and not (tmp_path / "b.txt").exists() and not (tmp_path / "a.txt").exists()


def test_the_next_turn_after_a_plain_no_runs_normally(tmp_path: Path) -> None:
    agent, res, provider = _declined_turn(tmp_path, "deny")
    assert res.stop_reason == "declined"
    assert agent.run_turn("do something else").final_text == "carried on"


def test_a_plain_answer_adds_nothing(tmp_path: Path) -> None:
    _, results = _turn_with_bash(tmp_path, "once")
    assert "comment" not in results[0]


# ─── the plain-text prompt ─────────────────────────────────────────────────

@pytest.mark.parametrize("typed,result,comment", [
    ("y", "once", ""), ("y: use staging", "once", "use staging"), ("YES : a  b", "once", "a b"),
    ("n", "deny", ""), ("n: not on main", "deny", "not on main"), ("no: why", "deny", "why"),
    ("a", "always", ""), ("a: ignored", "always", ""), ("", "deny", ""), ("maybe: yes", "deny", "yes"),
])
def test_the_plain_prompt_takes_a_note_after_a_colon(monkeypatch: pytest.MonkeyPatch, typed: str, result: str, comment: str) -> None:
    monkeypatch.setattr(approve.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(plat, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: typed)
    got = ask_approval("Bash", {"command": "ls"})
    assert got == result and getattr(got, "comment", "") == comment


def test_the_plain_prompt_takes_no_note_on_web_tools(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(approve.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(plat, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: "y: note")
    got = ask_approval("WebFetch", {"url": "https://example.com"})
    assert got == "once" and not isinstance(got, Answer) and "add a note" not in capsys.readouterr().out
