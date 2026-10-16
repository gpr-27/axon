"""Command registry, dispatch, custom commands, plugins, prompts, memory and trace commands."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands import list_command_names
from axon.commands.builtin import ALIASES, COMMANDS, CommandResult, dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from fakes import FakeProvider


def _turn(text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input=10, output=5))


@pytest.fixture
def agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    return Agent(provider=FakeProvider([_turn(f"answer {i}") for i in range(10)]), tools=create_default_registry(),
                 permissions=PermissionEngine(s), context=ContextManager(s), session=SessionStore(workspace),
                 ledger=Ledger(), settings=s)


def _last_user(agent: Agent) -> str:
    return next(m["content"] for m in reversed(agent.conversation.messages) if m["role"] == "user" and isinstance(m["content"], str))


def test_registry_has_unique_names_and_aliases():
    assert not set(COMMANDS) & set(ALIASES)
    for required in ("help", "clear", "compact", "context", "cost", "model", "permissions", "init", "memory",
                     "agents", "hooks", "mcp", "skills", "resume", "rewind", "diff", "review", "export",
                     "todos", "tasks", "queue", "trace", "inspect", "explain", "branch", "exit"):
        assert required in COMMANDS, required


def test_help_lists_categories(agent: Agent, capsys):
    dispatch_command("/help", agent)
    out = capsys.readouterr().out
    assert "Session" in out and "Context & Cost" in out and "/permissions" in out
    dispatch_command("/help cost", agent)
    assert "/cost calls" in capsys.readouterr().out


def test_non_commands_and_paths_are_not_handled(agent: Agent):
    assert dispatch_command("hello", agent) is None
    assert dispatch_command("/Users/me/pic.png what is this", agent) is None


def test_unknown_command_suggests(agent: Agent, capsys):
    res = dispatch_command("/sesions", agent)
    assert isinstance(res, CommandResult) and res.handled
    assert "/sessions" in capsys.readouterr().out


def test_exit(agent: Agent):
    assert dispatch_command("/exit", agent).should_exit is True


def test_custom_command_frontmatter_args_namespace_and_include(agent: Agent, workspace: Path, capsys):
    cmd_dir = workspace / ".axon" / "commands" / "frontend"
    cmd_dir.mkdir(parents=True)
    (workspace / "notes.txt").write_text("INCLUDED-CONTENT", encoding="utf-8")
    (cmd_dir / "component.md").write_text(
        "---\ndescription: Make a component\nargument-hint: <name> <style>\n---\n"
        "Create component $1 in $2 style. All: $ARGUMENTS. See @notes.txt\n", encoding="utf-8")
    names = dict(list_command_names(workspace))
    assert names.get("/frontend:component") == "Make a component"
    dispatch_command("/frontend:component Button flat", agent)
    sent = _last_user(agent)
    assert "Create component Button in flat style" in sent
    assert "All: Button flat" in sent
    assert "INCLUDED-CONTENT" in sent


def test_custom_command_shell_injection_needs_permission(agent: Agent, workspace: Path):
    d = workspace / ".claude" / "commands"
    d.mkdir(parents=True)
    (d / "ctx.md").write_text("Status: !`echo injected-ok`\n", encoding="utf-8")
    (d / "ctx2.md").write_text("---\nallowed-tools: Bash(echo:*)\n---\nStatus: !`echo injected-ok`\n", encoding="utf-8")
    dispatch_command("/ctx", agent)  # default mode, no TTY -> not approved
    assert "not permitted" in _last_user(agent)
    dispatch_command("/ctx2", agent)
    assert "injected-ok" in _last_user(agent)


def test_skills_reachable_with_prefix_and_args(agent: Agent, workspace: Path):
    sk = workspace / ".claude" / "skills" / "deploy"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy it\nallowed-tools: [Read, Grep]\n---\nDeploy to $ARGUMENTS\n", encoding="utf-8")
    agent.skills.discover()
    assert agent.skills.skills["deploy"].allowed_tools == ["Read", "Grep"]
    dispatch_command("/deploy staging", agent)
    assert "Deploy to staging" in _last_user(agent)
    dispatch_command("/skill:optimize hot loop", agent)  # shadowed by the /optimize built-in
    assert "hot loop" in _last_user(agent)


def test_plugin_create_exposes_namespaced_command(agent: Agent, workspace: Path):
    dispatch_command("/plugin create demo", agent)
    from axon.commands.plugins import load_plugins, plugin_dirs
    assert [p.name for p in load_plugins(workspace)] == ["demo"]
    assert plugin_dirs(workspace)[0].name == "demo"
    from axon.commands.custom import invalidate_cache
    invalidate_cache()
    dispatch_command("/demo:hello Ada", agent)
    assert "Greet Ada" in _last_user(agent)


def test_prompt_library_placeholders(agent: Agent):
    dispatch_command("/prompt tests path=src/app.py", agent)
    assert "Write tests for src/app.py" in _last_user(agent)
    before = len(agent.conversation.messages)
    dispatch_command("/prompt impact", agent)  # missing placeholder, no TTY -> nothing sent
    assert len(agent.conversation.messages) == before


def test_quick_memory_and_memory_commands(agent: Agent, workspace: Path):
    from axon.agent.memory import MemoryStore
    dispatch_command("#use tabs for indentation", agent)
    items = MemoryStore(workspace).list_all()
    assert any("tabs" in it.content for it in items)
    dispatch_command("/memory edit 1 use spaces instead", agent)
    assert any("spaces" in it.content for it in MemoryStore(workspace).list_all())
    dispatch_command("/memory remove 1", agent)
    assert MemoryStore(workspace).list_all() == []


def test_init_creates_but_never_overwrites(agent: Agent, workspace: Path, capsys):
    (workspace / "pyproject.toml").write_text("[project]\nname='x'\n[tool.pytest.ini_options]\n", encoding="utf-8")
    (workspace / ".cursorrules").write_text("Always use type hints.", encoding="utf-8")
    dispatch_command("/init", agent)
    text = (workspace / "AXON.md").read_text(encoding="utf-8")
    assert "python -m pytest" in text and "Always use type hints." in text
    (workspace / "AXON.md").write_text("# mine\n", encoding="utf-8")
    dispatch_command("/init", agent)
    assert (workspace / "AXON.md").read_text(encoding="utf-8") == "# mine\n"
    assert "suggestions" in capsys.readouterr().out
    dispatch_command("/init --force", agent)  # no TTY -> confirmation defaults to no
    assert (workspace / "AXON.md").read_text(encoding="utf-8") == "# mine\n"


def test_permissions_command_persists_local_settings(agent: Agent, workspace: Path):
    dispatch_command("/permissions allow Bash(git status:*)", agent)
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == ["Bash(git status:*)"]
    bash = agent.registry.get("Bash")
    assert agent.permissions.check(bash, {"command": "git status -s"}).outcome == "allow"
    dispatch_command("/permissions remove allow Bash(git status:*)", agent)
    assert agent.permissions.check(bash, {"command": "git status -s"}).outcome == "ask"


def test_trace_inspect_explain_with_trace(agent: Agent, capsys):
    agent.run_turn("hi")
    dispatch_command("/explain", agent)
    out = capsys.readouterr().out
    assert "Turn 1" in out and "model call" in out
    from axon.commands.agent_cmds import explain_trace
    from axon.agent.trace import Step, TurnTrace
    t = TurnTrace(turn=2, prompt="fix", model="m", mode="default")
    t.steps = [Step(1, "tool", 0.0, 1.0, {"name": "Read", "input": {"path": "src/app.py"}, "est_tokens": 1200}),
               Step(2, "tool", 1.0, 2.0, {"name": "Edit", "input": {"path": "src/app.py"}}),
               Step(3, "tool", 2.0, 3.0, {"name": "Bash", "input": {"command": "pytest -q"}, "is_error": True})]
    text = explain_trace(t)
    assert "read 1 file(s) (app.py" in text and "edited app.py" in text and "ran 1 command(s) (pytest -q)" in text
    assert "1 tool call(s) failed" in text


def test_compact_persists_and_reloads(agent: Agent):
    for i in range(4):
        agent.run_turn(f"question {i}")
    n = len(agent.conversation.messages)
    dispatch_command("/compact keep decisions", agent)
    assert len(agent.conversation.messages) < n
    reloaded = agent.session.read_conversation(agent.session.active_session_id)
    assert len(reloaded.messages) == len(agent.conversation.messages)


def test_rewind_and_diff_checkpoint(agent: Agent, workspace: Path, capsys):
    f = workspace / "x.bin"
    f.write_bytes(b"\x00\x01orig\r\n")
    agent.checkpoints.capture_before_edit(f)
    f.write_bytes(b"changed")
    agent.checkpoints.commit_turn_snapshot()
    dispatch_command("/rewind list", agent)
    assert "1 file(s)" in capsys.readouterr().out
    dispatch_command("/rewind 1", agent)
    assert f.read_bytes() == b"\x00\x01orig\r\n"


def test_feature_commands_override_builtins(monkeypatch, agent: Agent):
    import axon.commands.registry as reg
    from axon.features import FeatureCommand
    calls = []
    monkeypatch.setattr("axon.features.feature_commands",
                        lambda: [FeatureCommand(name="/changes", handler=lambda a, t: calls.append(t), description="x")])
    reg.reload_features()
    try:
        dispatch_command("/changes now", agent)
        assert calls == ["now"]
        assert ("/changes", "x") in list_command_names(agent=agent)
    finally:
        reg.reload_features()


def test_multiline_usage_is_aligned_under_first_line(agent: Agent, capsys):
    """An invalid /rewind argument prints the multi-line usage with every line indented (not at column 0)."""
    from axon.ui.theme import strip_ansi
    dispatch_command("/rewind 1 code", agent)
    lines = [ln for ln in strip_ansi(capsys.readouterr().out).splitlines() if "/rewind" in ln]
    assert len(lines) >= 5 and lines[0].startswith("  Usage: /rewind ")
    assert all(ln.startswith(" " * 9 + "/rewind") for ln in lines[1:]), lines


def test_analytics_is_an_alias_of_stats_and_unknown_commands_suggest_by_meaning(capsys):
    from axon.commands.registry import dispatch_command, suggest_commands
    from axon.ui.theme import strip_ansi
    # /analytics now reaches the analytics command instead of "Unknown command"
    from axon.commands import registry
    table = registry.feature_table()
    assert "stats" in table and "analytics" in table["stats"].aliases
    # typos and words that describe a command both find it
    assert [n for n, _ in suggest_commands("modle")][:2] == ["mode", "model"]
    assert "stats" in [n for n, _ in suggest_commands("analytic")]
    assert suggest_commands("xyzzy") == []
    res = dispatch_command("/memry", None)
    out = strip_ansi(capsys.readouterr().out)
    assert res is not None and res.handled
    assert "Unknown command /memry." in out and "Did you mean:" in out and "/memory" in out
