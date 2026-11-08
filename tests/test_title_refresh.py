"""The title follows the conversation: it is rewritten as the work moves on, never over a name the user chose."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.session.store import iter_records
from axon.ui.input import match_slash_commands


def _wait(predicate, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _titled_agent(workspace: Path, reply: str = "Payment Webhook Retries", **kw):
    agent = make_agent(FakeProvider([]), workspace, **kw)
    agent.title_provider = FakeProvider(scripted(reply))
    agent._apply_title("Fix the login bug", auto=True)
    return agent


@pytest.mark.parametrize("turn,due", [(1, False), (2, False), (3, True), (4, False), (5, True), (6, False), (10, True)])
def test_refresh_schedule(workspace: Path, turn: int, due: bool):
    agent = _titled_agent(workspace, title_refresh_turns=5)
    agent._title_turns = turn
    assert agent._title_refresh_due() is due


def test_refresh_rewrites_an_automatic_title(workspace: Path):
    agent = _titled_agent(workspace)
    agent._refresh_title("Fix the login bug", ["Fix the login bug", "now add webhook retries"], "Added retries.")
    assert agent._title == "Payment Webhook Retries"


def test_refresh_is_sent_the_latest_work_and_the_current_title(workspace: Path):
    agent = _titled_agent(workspace)
    agent._refresh_title("Fix the login bug", ["Fix the login bug", "now add webhook retries"], "Added retries.")
    sent = str(agent.title_provider.requests[-1])
    assert "now add webhook retries" in sent and "Added retries." in sent and "Fix the login bug" in sent


def test_refresh_input_stays_small_after_a_huge_paste(workspace: Path):
    agent = _titled_agent(workspace)
    agent._refresh_title("Fix the login bug", ["x" * 5_000_000], "y" * 5_000_000)
    assert len(str(agent.title_provider.requests[-1])) < 10_000


def test_a_name_the_user_chose_is_never_refreshed(workspace: Path):
    agent = _titled_agent(workspace)
    agent.mark_titled("My own name")
    agent._title_turns = 5
    assert agent._title_refresh_due() is False


def test_user_chosen_name_survives_a_restart(workspace: Path):
    agent = _titled_agent(workspace)
    agent.session.rename_session("My own name")
    agent._titled = agent._title = agent._title_auto = None
    agent._title_turns = 5
    assert agent.session_title == "My own name"
    assert agent._title_refresh_due() is False


def test_automatic_title_is_still_refreshed_after_a_restart(workspace: Path):
    agent = _titled_agent(workspace)
    agent._titled = agent._title = agent._title_auto = None
    agent._title_turns = 5
    assert agent._title_refresh_due() is True


def test_rename_during_the_call_wins(workspace: Path):
    agent = _titled_agent(workspace)
    original = agent._llm_title

    def rename_mid_call(prompt, **kw):
        agent.mark_titled("My own name")
        return original(prompt, **kw)

    agent._llm_title = rename_mid_call
    agent._refresh_title("Fix the login bug", ["more"], "done")
    assert agent._title == "My own name"


def test_failed_call_keeps_the_title(workspace: Path):
    agent = _titled_agent(workspace)
    agent._llm_title = lambda prompt, **kw: ""
    agent._refresh_title("Fix the login bug", ["more"], "done")
    assert agent._title == "Fix the login bug"


def test_unchanged_title_writes_no_record(workspace: Path):
    agent = _titled_agent(workspace, reply="Fix the login bug")
    path = agent.session.active_file
    before = sum(r.get("type") == "session_rename" for r in iter_records(path))
    agent._refresh_title("Fix the login bug", ["more"], "done")
    assert sum(r.get("type") == "session_rename" for r in iter_records(path)) == before


def test_can_be_turned_off(workspace: Path):
    agent = _titled_agent(workspace, title_refresh_turns=0)
    agent._title_turns = 5
    assert agent._title_refresh_due() is False


def test_titles_use_the_session_model(workspace: Path):
    agent = _titled_agent(workspace, model="big-model", small_model="tiny-model")
    agent._llm_title("hello")
    assert agent.title_provider.requests[-1]["model"] == "big-model"


def test_turns_trigger_the_refresh_in_the_background(workspace: Path):
    agent = _titled_agent(workspace)
    result = SimpleNamespace(final_text="Added retries.", stop_reason="end_turn")
    for _ in range(3):
        agent._after_turn_title("now add webhook retries", result)
    assert _wait(lambda: agent._title == "Payment Webhook Retries")


@pytest.mark.parametrize("typed,canonical_desc", [("ledger", "Cost by source")])
def test_typing_an_alias_selects_that_command(workspace: Path, typed: str, canonical_desc: str):
    first = match_slash_commands(typed, workspace)[0]
    assert first[0] == f"/{typed}"
    assert canonical_desc in first[3]


def test_exact_command_still_beats_its_prefix_matches(workspace: Path):
    assert match_slash_commands("cost", workspace)[0][0] == "/cost"


def test_resume_keeps_refreshing_an_automatic_title(workspace: Path):
    agent = _titled_agent(workspace)
    agent.reload_title()
    agent._title_turns = 5
    assert agent.session_title == "Fix the login bug" and agent._title_refresh_due() is True


def test_resume_keeps_a_user_chosen_name(workspace: Path):
    agent = _titled_agent(workspace)
    agent.session.rename_session("My own name")
    agent.reload_title()
    agent._title_turns = 5
    assert agent.session_title == "My own name" and agent._title_refresh_due() is False
