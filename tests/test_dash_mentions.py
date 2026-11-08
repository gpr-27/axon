"""The page expands @file and @agent-name mentions like the terminal (the model sees the content, the transcript the typed text)."""
from axon.features.dash_api import expand_mentions


def test_file_and_agent_mentions_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "notes.txt").write_text("FILE-BODY-9911\n")
    out = expand_mentions("see @notes.txt", tmp_path)
    assert "[File: notes.txt]" in out and "FILE-BODY-9911" in out
    out = expand_mentions("ask @agent-explore to look", tmp_path)
    assert 'subagent_type="explore"' in out and "the explore subagent" in out
    assert expand_mentions("no mention here", tmp_path) == "no mention here"
    assert expand_mentions("/help @notes.txt", tmp_path) == "/help @notes.txt"
