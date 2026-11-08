"""/agents delete removes a project or personal agent file (its source label is 'project'/'user', not a path)."""
from types import SimpleNamespace

from axon.commands.agent_cmds import handle_agents


def test_delete_project_agent_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    d = tmp_path / "ws" / ".axon" / "agents"
    d.mkdir(parents=True)
    f = d / "myagent.md"
    f.write_text("---\nname: myagent\ndescription: x\n---\nhello\n")
    agent = SimpleNamespace(settings=SimpleNamespace(workspace=tmp_path / "ws"))
    monkeypatch.setattr("axon.commands.agent_cmds.confirm", lambda *a, **k: True)
    handle_agents(agent, "delete myagent")
    out = capsys.readouterr().out
    assert "built in" not in out
    assert not f.exists()
