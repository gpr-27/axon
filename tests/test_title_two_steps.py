"""A session gets its first-words title at once; the model-written title replaces it in the background."""
import time
from pathlib import Path

from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.agent.loop import heuristic_title


def _wait(predicate, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_first_words_title_is_saved_immediately_then_upgraded(workspace: Path):
    agent = make_agent(FakeProvider([]), workspace)
    agent.title_provider = FakeProvider(scripted("Context Window Basics"))
    prompt = "Hi, can you explain how the context window limit works in this project?"
    agent._title_in_two_steps(prompt)
    assert agent._title in (heuristic_title(prompt), "Context Window Basics")     # present right away
    assert _wait(lambda: agent._title == "Context Window Basics")


def test_a_rename_by_the_user_is_not_overwritten(workspace: Path):
    agent = make_agent(FakeProvider([]), workspace)
    agent.title_provider = FakeProvider(scripted("AI Title"))
    first = agent._apply_title(heuristic_title("Fix the login bug please"))
    agent._apply_title("My own name")                                  # the user renames the session
    agent._upgrade_title("Fix the login bug please", first)
    assert agent._title == "My own name"


def test_failed_model_call_keeps_the_first_title(workspace: Path):
    agent = make_agent(FakeProvider([]), workspace)
    agent.title_provider = None                                        # no model: _llm_title falls back to ""
    first = agent._apply_title(heuristic_title("Explain decorators"))
    agent._llm_title = lambda prompt: ""                               # type: ignore[method-assign]
    agent._upgrade_title("Explain decorators", first)
    assert agent._title == first
