"""Prompt suggestions: the guessed next message shown as ghost text in the empty prompt."""
from __future__ import annotations

import contextlib
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import pytest

from axon import platform as plat
from axon.agent import suggest
from axon.agent.loop import TurnResult
from axon.config import Settings, map_json_settings
from axon.providers.base import Usage
from axon.ui import input as ui_input
from axon.ui.input import EditorState, PromptChrome, build_frame, read_input
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
USER_MESSAGE = "Why does the payload show every tool twice?"
ANSWER = "The system prompt lists the tools again, so they appear once in the tools field and once in the text."


def plain(text: str) -> str:
    return ANSI.sub("", text)


def wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def suggesting_agent(workspace: Path, reply: str = "run the tests", answer: str = ANSWER, **kw: Any):
    """An agent whose main model answers ``answer`` and whose small model replies ``reply``."""
    agent = make_agent(FakeProvider(scripted(answer, answer, answer)), workspace, **kw)
    agent.suggest_provider = FakeProvider(scripted(reply, reply, reply))
    agent.suggestions.live = True
    return agent


def finished_turn(text: str = ANSWER, stop_reason: str = "end_turn") -> TurnResult:
    return TurnResult(final_text=text, stop_reason=stop_reason, iterations=1, tool_calls_count=0, usage=Usage())


# ─── generation ─────────────────────────────────────────────────────────────

def test_suggestion_is_generated_after_a_finished_turn(workspace: Path) -> None:
    agent = suggesting_agent(workspace, reply='"Run the tests."')
    agent.run_turn(USER_MESSAGE)
    assert wait_until(lambda: agent.next_suggestion != "")
    assert agent.next_suggestion == "Run the tests"
    request = agent.suggest_provider.requests[0]
    assert request["tools"] == []
    asked = request["messages"][0]["content"]
    assert USER_MESSAGE in asked and "system prompt lists the tools again" in asked
    assert "exactly NONE" in request["system"][0]["text"]


def test_suggestion_usage_is_recorded_with_source_suggest(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent.run_turn(USER_MESSAGE)
    assert wait_until(lambda: agent.next_suggestion != "")
    assert [e.source for e in agent.ledger.entries].count("suggest") == 1
    entry = next(e for e in agent.ledger.entries if e.source == "suggest")
    assert entry.usage.output == 50


def test_small_model_is_used_when_set(workspace: Path) -> None:
    agent = suggesting_agent(workspace, small_model="claude-haiku-4-5")
    agent.run_turn(USER_MESSAGE)
    assert wait_until(lambda: agent.next_suggestion != "")
    assert agent.suggest_provider.requests[0]["model"] == "claude-haiku-4-5"
    assert next(e for e in agent.ledger.entries if e.source == "suggest").model == "claude-haiku-4-5"


@pytest.mark.parametrize("stop_reason", ["interrupted", "error", "blocked", "budget_exceeded", "max_tokens"])
def test_no_suggestion_for_turns_that_did_not_finish_normally(workspace: Path, stop_reason: str) -> None:
    agent = suggesting_agent(workspace)
    agent._maybe_suggest(USER_MESSAGE, finished_turn(stop_reason=stop_reason))
    time.sleep(0.05)
    assert agent.next_suggestion == "" and agent.suggest_provider.requests == []


def test_no_suggestion_for_an_empty_answer(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent._maybe_suggest(USER_MESSAGE, finished_turn(text="  "))
    assert agent.suggest_provider.requests == [] and not agent.suggestions.pending


def test_no_suggestion_after_a_slash_command_turn(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent.run_turn("/review the diff")
    time.sleep(0.05)
    assert agent.next_suggestion == "" and agent.suggest_provider.requests == []


def test_no_suggestion_for_a_subagent(workspace: Path) -> None:
    agent = suggesting_agent(workspace, agent_name="reviewer")
    assert agent.is_subagent
    agent._maybe_suggest(USER_MESSAGE, finished_turn())
    time.sleep(0.05)
    assert agent.next_suggestion == "" and agent.suggest_provider.requests == []


def test_no_suggestion_when_disabled(workspace: Path) -> None:
    agent = suggesting_agent(workspace, prompt_suggestions=False)
    agent.run_turn(USER_MESSAGE)
    time.sleep(0.05)
    assert agent.next_suggestion == "" and agent.suggest_provider.requests == []


def test_no_suggestion_outside_an_interactive_session(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent.suggestions.live = False          # -p print mode, a pipe: the REPL never switched it on
    agent.run_turn(USER_MESSAGE)
    time.sleep(0.05)
    assert agent.next_suggestion == "" and agent.suggest_provider.requests == []


def test_a_failing_small_model_never_raises(workspace: Path) -> None:
    class Broken(FakeProvider):
        def stream(self, **kwargs: Any):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    agent = suggesting_agent(workspace)
    agent.suggest_provider = Broken([])
    agent.run_turn(USER_MESSAGE)
    assert wait_until(lambda: not agent.suggestions.pending)
    assert agent.next_suggestion == ""


def test_stale_suggestion_is_ignored_when_a_new_turn_started(workspace: Path) -> None:
    gate = threading.Event()

    class Slow(FakeProvider):
        def stream(self, **kwargs: Any):
            gate.wait(3)
            yield from super().stream(**kwargs)

    agent = suggesting_agent(workspace)
    agent.suggest_provider = Slow(scripted("explain the first answer"))
    agent.run_turn(USER_MESSAGE)
    assert agent.suggestions.pending
    agent.run_turn("and the second thing?")        # a new turn: the first guess is stale
    assert not agent.suggestions.pending or agent.suggestions.snapshot()[0] == ""
    gate.set()
    assert wait_until(lambda: len(agent.suggest_provider.requests) >= 1)
    time.sleep(0.1)
    assert "explain the first answer" not in agent.next_suggestion


def test_new_turn_clears_the_old_suggestion(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent.suggestions.start()
    agent.suggestions.finish(agent.suggestions._generation, "old guess")
    agent.suggestions.live = False
    agent.run_turn("something else")
    assert agent.next_suggestion == ""


def test_earlier_user_messages_are_sent_as_context(workspace: Path) -> None:
    agent = suggesting_agent(workspace)
    agent.run_turn("first question")
    assert wait_until(lambda: agent.next_suggestion != "")
    agent.run_turn("second question")
    assert wait_until(lambda: len(agent.suggest_provider.requests) == 2)
    asked = agent.suggest_provider.requests[1]["messages"][0]["content"]
    assert "Earlier user messages" in asked and "- first question" in asked
    assert asked.index("first question") < asked.index("second question")


# ─── pure functions ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply, expected", [
    ("run the tests", "run the tests"),
    ('"Run the tests."', "Run the tests"),
    ("`git diff`", "git diff"),
    ("- commit this", "commit this"),
    ("why did it fail?", "why did it fail?"),
    ("NONE", ""),
    ("none.", ""),
    ("", ""),
    ("   ", ""),
    ("x" * 101, ""),
    ("I think you should run the tests", ""),
    ("You might want to run the tests", ""),
    ("I can't predict that", ""),
    ("run the tests\nthen commit", ""),
    ("...", ""),
    ("/exit", ""),
])
def test_clean_reply(reply: str, expected: str) -> None:
    assert suggest.clean_reply(reply) == expected


def test_clean_reply_keeps_a_real_slash_command_only() -> None:
    assert suggest.clean_reply("/commit", {"commit"}) == "/commit"
    assert suggest.clean_reply("/commit now", {"commit"}) == "/commit now"
    assert suggest.clean_reply("/nonsense", {"commit"}) == ""


def test_build_request_limits_the_text() -> None:
    system, messages = suggest.build_request("§" * 900, "¶" * 3000, ["ø" * 500, "æ" * 500, "å" * 500])
    text = messages[0]["content"]
    assert text.count("§") == 600 and text.count("¶") == 1200
    assert text.count("ø") == 0                      # only the last two earlier messages are sent
    assert text.count("æ") == 200 and text.count("å") == 200
    assert "NONE" in system[0]["text"] and messages[0]["role"] == "user"


def test_earlier_user_messages_skip_tool_results_and_the_latest_message() -> None:
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
        {"role": "user", "content": [{"type": "text", "text": "two"}]},
        {"role": "user", "content": "three"},
        {"role": "user", "content": "four"},
    ]
    assert suggest.earlier_user_messages(messages) == ["two", "three"]


def test_suggestion_box_versions_and_staleness() -> None:
    box = suggest.SuggestionBox()
    assert box.snapshot() == ("", 0) and not box.pending
    ticket = box.start()
    assert box.pending
    assert box.finish(ticket, "go on") and box.snapshot() == ("go on", 1) and not box.pending
    box.clear()
    assert box.snapshot() == ("", 2)
    old = box.start()
    box.clear()                                    # a newer event makes the ticket stale
    assert not box.finish(old, "late") and box.snapshot()[0] == "" and not box.pending


# ─── the frame ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    monkeypatch.setattr(plat, "term_size", lambda: (100, 30))


def prompt_row(st: EditorState) -> str:
    lines, _ = build_frame(st, PromptChrome())
    return plain(lines[1])


def test_frame_shows_ghost_text_with_an_empty_buffer() -> None:
    st = EditorState()
    st.ghost = "run the tests"
    row = prompt_row(st)
    assert "run the tests" in row and "tab to accept" in row
    assert "Ask anything" not in row


def test_frame_without_a_suggestion_shows_the_usual_hint() -> None:
    assert "Ask anything" in prompt_row(EditorState())


def test_frame_hides_the_ghost_when_typing() -> None:
    st = EditorState()
    st.ghost = "run the tests"
    st.set_text("fix")
    row = prompt_row(st)
    assert "run the tests" not in row and "tab to accept" not in row and "fix" in row


def test_frame_drops_the_key_hint_when_the_suggestion_is_wide() -> None:
    monkey_width = 40
    row = plain(ui_input.ghost_text("word " * 7, monkey_width))
    assert "tab to accept" not in row and row.startswith("word")


def test_ghost_text_is_not_part_of_the_buffer() -> None:
    st = EditorState()
    st.ghost = "run the tests"
    assert st.buf == "" and st.expanded() == ""


# ─── the editor loop ────────────────────────────────────────────────────────

@pytest.fixture
def editor(monkeypatch, tmp_path):
    """Drive read_input with scripted keys. A callable in the script runs and reads as 'no key yet'."""
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    monkeypatch.setattr(ui_input, "_loop_wake_event", lambda: None)
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(plat, "setup_console", lambda: None)
    monkeypatch.setattr(ui_input, "_status_rows", lambda agent, idle=False: [])
    monkeypatch.setattr(ui_input.ClickMap, "for_prompt", classmethod(lambda cls, agent=None: None))
    rows: list[str] = []
    timeouts: list[Any] = []
    real_build = ui_input.build_frame

    def spy(st, chrome):
        result = real_build(st, chrome)
        rows.append(plain(result[0][1]))
        return result
    monkeypatch.setattr(ui_input, "build_frame", spy)

    def run(script: list[Any], box: suggest.SuggestionBox, **kwargs: Any):
        queue = deque(script)

        def fake_read_key(timeout=None, report_resize=True):
            if not queue:
                raise AssertionError("ran out of scripted keys")
            item = queue.popleft()
            timeouts.append(timeout)
            if callable(item):
                item()
                return None
            return item
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        agent = type("A", (), {"suggestions": box, "settings": None})()
        return read_input(agent=agent, cwd=str(tmp_path), **kwargs)

    run.rows = rows
    run.timeouts = timeouts
    return run


def box_with(text: str) -> suggest.SuggestionBox:
    box = suggest.SuggestionBox()
    box.finish(box.start(), text)
    return box


def deliver(box: suggest.SuggestionBox, text: str) -> Callable[[], None]:
    def put() -> None:
        box.finish(box._generation, text)
    return put


def test_tab_accepts_the_suggestion_into_the_buffer(editor) -> None:
    box = box_with("run the tests")
    text = editor(["tab", "enter"], box)[0]
    assert text == "run the tests"
    assert box.text == ""                           # accepted: gone
    assert any("run the tests" in r and "tab to accept" in r for r in editor.rows[:1])


def test_accepted_text_is_editable_with_the_cursor_at_the_end(editor) -> None:
    box = box_with("run the tests")
    assert editor(["tab", " ", "n", "o", "w", "enter"], box)[0] == "run the tests now"


@pytest.mark.parametrize("key", ["right", "end"])
def test_right_and_end_accept_at_an_empty_bar(editor, key: str) -> None:
    box = box_with("commit this")
    assert editor([key, "enter"], box)[0] == "commit this"


@pytest.mark.parametrize("key, expected", [("x", "x"), ("esc", ""), ("up", ""), ("paste:hello", "hello")])
def test_any_dismissing_key_removes_the_suggestion(editor, key: str, expected: str) -> None:
    box = box_with("run the tests")
    assert editor([key, "enter"], box)[0] == expected
    assert box.text == ""
    assert not any("run the tests" in r for r in editor.rows[1:])


def test_ctrl_c_dismisses_the_suggestion_and_leaves_the_prompt(editor) -> None:
    box = box_with("run the tests")
    with pytest.raises(KeyboardInterrupt):
        editor(["ctrl-c"], box)
    assert box.text == ""


def test_enter_on_the_empty_bar_does_not_send_the_suggestion(editor, tmp_path) -> None:
    box = box_with("run the tests")
    assert editor(["enter"], box)[0] == ""
    assert box.text == "run the tests"              # untouched: still offered at the next prompt
    assert "run the tests" not in ui_input.history_entries(cwd=str(tmp_path))[0]


def test_an_unsent_suggestion_never_reaches_the_history(editor, tmp_path) -> None:
    box = box_with("run the tests")
    editor(["x", "backspace", "enter"], box)          # dismissed, then Enter on an empty bar
    assert "run the tests" not in ui_input.history_entries(cwd=str(tmp_path))[0]
    box = box_with("run the tests")
    editor(["tab", "enter"], box)                    # accepted and sent: now it is the user's own prompt
    assert "run the tests" in ui_input.history_entries(cwd=str(tmp_path))[0]


def test_suggestion_that_arrives_while_waiting_is_picked_up_without_a_keypress(editor) -> None:
    box = suggest.SuggestionBox()
    box.start()                                      # generation in flight
    text = editor([None, deliver(box, "run the tests"), None, "tab", "enter"], box)[0]
    assert text == "run the tests"
    assert any("Ask anything" in r for r in editor.rows[:1])
    assert any("run the tests" in r for r in editor.rows[1:])
    assert editor.timeouts[0] == 0.25                # polls while a suggestion is pending


def test_the_idle_prompt_does_not_poll_when_nothing_is_pending(editor) -> None:
    box = suggest.SuggestionBox()
    editor(["enter"], box)
    assert editor.timeouts == [None]


def test_late_suggestion_is_dropped_when_the_user_already_typed(editor) -> None:
    box = suggest.SuggestionBox()
    box.start()
    text = editor(["h", "i", deliver(box, "run the tests"), None, "enter"], box)[0]
    assert text == "hi"
    assert box.text == ""
    assert not any("run the tests" in r for r in editor.rows)


def test_a_suggestion_waiting_before_the_prompt_opened_is_shown_at_once(editor) -> None:
    box = box_with("run the tests")
    editor(["enter"], box)
    assert "run the tests" in editor.rows[0]


def test_type_ahead_text_drops_the_suggestion(editor) -> None:
    box = box_with("run the tests")
    text = editor(["enter"], box, initial="already typed")[0]
    assert text == "already typed" and box.text == ""


def test_subagent_view_shows_no_suggestion(editor) -> None:
    box = box_with("run the tests")
    editor(["enter"], box, subagent_label="Subagent #1")
    assert not any("run the tests" in r for r in editor.rows)


def test_prompt_without_a_suggestion_box_still_works(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(plat, "is_interactive", lambda: True)
    monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(plat, "setup_console", lambda: None)
    queue = deque(["o", "k", "enter"])
    monkeypatch.setattr(plat, "read_key", lambda timeout=None, report_resize=True: queue.popleft())
    assert read_input(agent=object(), cwd=str(tmp_path))[0] == "ok"


# ─── settings and the command ───────────────────────────────────────────────

def test_setting_default_and_json_key(workspace: Path) -> None:
    assert Settings(workspace=workspace, api_key="k" * 12).prompt_suggestions is True
    assert map_json_settings({"promptSuggestions": False}) == {"prompt_suggestions": False}


def test_suggestions_command_toggles_the_setting_and_clears_the_ghost(workspace: Path, monkeypatch, capsys) -> None:
    from axon.commands import prefs_cmds
    saved: list[dict] = []
    monkeypatch.setattr(prefs_cmds, "update_settings_file", lambda changes: saved.append(changes) or Path("settings.json"))
    agent = suggesting_agent(workspace)
    agent.suggestions.finish(agent.suggestions.start(), "run the tests")
    prefs_cmds.handle_suggestions(agent, "off")
    assert agent.settings.prompt_suggestions is False and agent.next_suggestion == ""
    prefs_cmds.handle_suggestions(agent, "on")
    assert agent.settings.prompt_suggestions is True
    prefs_cmds.handle_suggestions(agent, "toggle")
    assert agent.settings.prompt_suggestions is False
    assert saved == [{"promptSuggestions": False}, {"promptSuggestions": True}, {"promptSuggestions": False}]
    prefs_cmds.handle_suggestions(agent, "maybe")
    assert "/suggestions [on|off]" in plain(capsys.readouterr().out)
    assert agent.settings.prompt_suggestions is False


def test_suggestions_command_is_registered_and_listed_in_config(workspace: Path) -> None:
    from axon.commands.registry import resolve_builtin
    from axon.commands.system_cmds import _panel_keys
    assert resolve_builtin("suggestions") is not None
    agent = suggesting_agent(workspace)
    assert "prompt_suggestions" in [k for k, *_ in _panel_keys(agent)]
