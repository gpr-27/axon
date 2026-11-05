"""AI chat titles: on by default (main model when no small model), and /retitle all for old chats."""
from pathlib import Path

from fakes import FakeProvider, scripted
from test_core_helpers import make_agent

from axon.agent.loop import heuristic_title
from axon.commands import dispatch_command
from axon.commands.session_cmds import _same_title, auto_named_sessions


def _write_chat(agent, prompt: str, answer: str, session_id: str, title: str = "") -> None:
    store = agent.session
    store.active_session_id = session_id
    store._activate(session_id)
    store.append_user(prompt)
    store.append("assistant_message", {"text": answer})
    if title:
        store.rename_session(title, session_id=session_id)


def test_same_title_matches_only_first_word_titles():
    prompt = "Use the ToolSearch tool with query select:mcp__swiggy-food__get_addresses and tell me"
    assert _same_title(heuristic_title(prompt), prompt)
    assert not _same_title("Swiggy Tool Lookup", prompt)


def test_auto_named_sessions_skip_chats_you_or_the_ai_titled(workspace: Path):
    agent = make_agent(FakeProvider([]), workspace)
    _write_chat(agent, "Hi there", "Hello!", "session_a")
    _write_chat(agent, "Fix the login bug", "Done.", "session_b", title="Login Bug Fix")
    _write_chat(agent, "Explain decorators", "They wrap functions.", "session_c", title=heuristic_title("Explain decorators"))
    names = {m.session_id for m, _ in auto_named_sessions(agent)}
    assert names == {"session_a", "session_c"}


def test_retitle_all_renames_with_the_models_title(workspace: Path, capsys):
    agent = make_agent(FakeProvider(scripted("Greeting Chat", "Decorators Explained")), workspace)
    _write_chat(agent, "Hi there", "Hello!", "session_a")
    _write_chat(agent, "Explain decorators", "They wrap functions.", "session_c")
    dispatch_command("/retitle all", agent)
    out = capsys.readouterr().out
    assert "Titled 2 of 2 chats" in out
    assert not auto_named_sessions(agent)        # both now carry an AI title


def test_retitle_all_with_nothing_to_do(workspace: Path, capsys):
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/retitle all", agent)
    assert "already has a title" in capsys.readouterr().out
