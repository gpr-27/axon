"""Regression tests for the commands / session / hooks / MCP / skills review pass."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from axon.ui.theme import strip_ansi
from fakes import FakeProvider


def _agent(workspace: Path) -> Agent:
    s = Settings(workspace=workspace, mode="default")
    turns = [AssistantTurn(blocks=[TextBlock(text=f"a{i}")], stop_reason="end_turn", usage=Usage(input=10, output=5))
             for i in range(5)]
    return Agent(provider=FakeProvider(turns), tools=create_default_registry(), permissions=PermissionEngine(s),
                 context=ContextManager(s), session=SessionStore(workspace), ledger=Ledger(), settings=s)


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return strip_ansi(capsys.readouterr().out)


# ─── /help <word> searches, as the "Unknown command" hint promises ─────────

def test_help_word_searches_commands(workspace: Path, capsys) -> None:
    agent = _agent(workspace)
    dispatch_command("/help checkpoint", agent)  # not a command name: an alias of /rewind and a description word
    out = _out(capsys)
    assert "No command named" not in out
    assert "/rewind" in out or "/diff" in out
    dispatch_command("/help zzqqxx", agent)
    assert "No command named /zzqqxx" in _out(capsys)


# ─── session store ─────────────────────────────────────────────────────────

def test_user_message_times_skip_tool_result_carriers(workspace: Path) -> None:
    store = SessionStore(workspace)
    store.append_user("first prompt")
    store.append("user_message", {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]})
    store.append_user("second prompt")
    assert len(store.user_message_times()) == 2


def test_branch_keeps_tool_results_out_of_the_prompts(workspace: Path, capsys) -> None:
    from axon.session.interactive import handle_branch
    agent = _agent(workspace)
    agent.conversation.messages[:] = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "data", "is_error": True}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    handle_branch(agent, "try2")
    store = agent.session
    assert len(store.user_message_times()) == 1
    conv = store.read_conversation(store.active_session_id)
    tr = conv.messages[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1" and tr.get("is_error") is True


def test_compaction_usage_counts_in_resumed_ledger(workspace: Path) -> None:
    store = SessionStore(workspace)
    store.append_user("hi")
    store.append("compaction", {"messages": [{"role": "user", "content": "summary"}], "model": "claude-sonnet-4-5",
                                "usage": {"input": 1000, "output": 200}})
    ledger = store.load_ledger(store.active_session_id, "claude-sonnet-4-5")
    assert ledger.total_input_tokens == 1000 and ledger.total_output_tokens == 200
    assert any(e.source == "compaction" for e in ledger.entries)
    meta = next(m for m in store.list_recent() if m.session_id == store.active_session_id)
    assert meta.total_tokens == 1200


def test_subagent_trace_files_are_not_transcripts(workspace: Path) -> None:
    store = SessionStore(workspace)
    sid = store.active_session_id
    store.append_user("hi")
    (store.session_dir / f"{sid}_sub_1.jsonl").write_text("", encoding="utf-8")
    (store.session_dir / f"{sid}_sub_1.traces.jsonl").write_text("{}\n", encoding="utf-8")
    assert [p.name for p in store._subagent_files(sid)] == [f"{sid}_sub_1.jsonl"]
    assert store.delete_session(sid)
    assert not (store.session_dir / f"{sid}_sub_1.traces.jsonl").exists()  # still moved to the trash


def test_git_info_branch_with_slash(tmp_path: Path) -> None:
    from axon.session.layout import git_info
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature/login-form\n", encoding="utf-8")
    assert git_info(tmp_path)["branch"] == "feature/login-form"


# ─── skills / custom commands ──────────────────────────────────────────────

def test_substituted_arguments_are_not_rescanned() -> None:
    from axon.skills.manager import substitute_arguments
    assert substitute_arguments("Fix: $ARGUMENTS", "price shows $5 not $1") == "Fix: price shows $5 not $1"
    assert substitute_arguments("A=$1 B=$2", "x '$2'") == "A=x B=$2"
    assert substitute_arguments("Env ${env}, ref $ref", "prod $1 main", ["env", "ref"]) == "Env prod, ref $1 main"
    assert substitute_arguments("no placeholder", "hello") == "no placeholder\n\nARGUMENTS: hello"


def test_frontmatter_with_bom() -> None:
    from axon.skills.manager import parse_frontmatter
    fm, body = parse_frontmatter("﻿---\nname: x\ndescription: d\n---\nbody")
    assert fm["name"] == "x" and body == "body"


def test_skill_delete_keeps_a_library_copy(workspace: Path, capsys) -> None:
    from axon.session.library import library_dir
    from axon.skills.interactive import handle_skills_command
    from axon.skills.manager import SkillManager
    f = workspace / ".axon" / "skills" / "mine" / "SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("---\nname: mine\ndescription: d\n---\nbody\n", encoding="utf-8")
    mgr = SkillManager(workspace)
    handle_skills_command(mgr, workspace, "delete mine")  # non-interactive: confirm defaults to yes
    assert not f.exists()
    copies = list((library_dir(workspace) / "skill" / "mine").glob("*SKILL.md"))
    assert copies and "body" in copies[-1].read_text(encoding="utf-8")


# ─── commands ──────────────────────────────────────────────────────────────

def test_memory_edit_project_opens_axon_md_not_claude_md(workspace: Path, capsys) -> None:
    agent = _agent(workspace)
    (workspace / "AXON.md").write_text("# a\n", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("# c\n", encoding="utf-8")
    dispatch_command("/memory edit project", agent)  # non-interactive: prints the path
    out = _out(capsys)
    assert "AXON.md" in out and "CLAUDE.md" not in out


def test_memory_add_bare_global_flag_is_usage(workspace: Path, capsys) -> None:
    agent = _agent(workspace)
    dispatch_command("/memory add --global", agent)
    assert "Usage" in _out(capsys)


def test_test_command_keeps_quoted_args(workspace: Path) -> None:
    from axon.commands.code_cmds import _detect_tests
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    argv, _ = _detect_tests(workspace, '-k "login and not slow"')
    assert argv[-2:] == ["-k", "login and not slow"]


def test_tools_command_handles_list_typed_schema(workspace: Path, capsys) -> None:
    from axon.tools.base import Tool

    class T(Tool):
        name = "ListTyped"
        description = "x"
        schema = {"type": "object", "properties": {"q": {"type": ["string", "null"], "description": "d"}}}

        def run(self, args, ctx):  # pragma: no cover
            return ""

    agent = _agent(workspace)
    agent.registry.register(T())
    res = dispatch_command("/tools ListTyped", agent)
    assert res.success and "string|null" in _out(capsys)


def test_config_compact_at_accepts_percent(workspace: Path, capsys) -> None:
    agent = _agent(workspace)
    dispatch_command("/config compact_at 80", agent)
    assert abs(float(agent.settings.compact_at) - 0.8) < 1e-9


def test_endpoint_partial_name_must_be_unambiguous() -> None:
    from axon.commands.endpoint_cmds import _find, _presets
    ids = [p.id for p in _presets()]
    assert _find("ai") is None or sum("ai" in i or "ai" in p.name.lower() for i, p in zip(ids, _presets())) == 1
    assert _find(ids[0]).id == ids[0]


def test_inspect_reads_saved_traces_after_resume(workspace: Path, monkeypatch) -> None:
    from axon.commands import agent_cmds
    seen = {}

    def fake_load(ws, sid, legacy_session_dir=None):
        seen["ws"], seen["sid"] = ws, sid
        return []

    monkeypatch.setattr("axon.agent.trace.load_traces", fake_load)
    agent = _agent(workspace)
    agent.traces = []
    agent_cmds._traces(agent)
    assert Path(seen["ws"]) == workspace and seen["sid"] == agent.session.active_session_id


# ─── hooks ─────────────────────────────────────────────────────────────────

def test_snake_case_event_aliases() -> None:
    from axon.hooks.manager import normalize_event
    assert normalize_event("task_created") == "TaskCreated"
    assert normalize_event("instructions_loaded") == "InstructionsLoaded"
    assert normalize_event("cwd_changed") == "CwdChanged"


# ─── MCP ───────────────────────────────────────────────────────────────────

def test_mcp_args_may_reference_the_servers_own_env(monkeypatch) -> None:
    from axon.mcp.config import ServerSpec
    monkeypatch.delenv("MY_TOKEN", raising=False)
    spec = ServerSpec.from_dict("s", {"command": "srv", "args": ["--token", "${MY_TOKEN}"], "env": {"MY_TOKEN": "abc"}})
    assert spec.unresolved_problems() == []
    assert spec.resolved().args == ["--token", "abc"]
    bad = ServerSpec.from_dict("s", {"command": "srv", "args": ["${NOPE_NOT_SET}"]})
    assert bad.unresolved_problems() == ["${NOPE_NOT_SET} is not set"]


def test_malformed_mcp_spec_does_not_break_loading(workspace: Path) -> None:
    from axon.mcp.config import MCPConfig
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "bad": {"command": "x", "env": ["A=1"]},
        "good": {"command": "y"},
    }}), encoding="utf-8")
    specs = MCPConfig(workspace).load_specs()
    assert "good" in specs and "bad" not in specs


def test_mcp_remove_accepts_scope_before_name(workspace: Path, capsys) -> None:
    from axon.mcp.commands import handle_mcp_command
    from axon.mcp.config import MCPConfig
    agent = _agent(workspace)
    cfg = MCPConfig(workspace)
    cfg.add_raw("srv", {"command": "echo"}, "project")
    handle_mcp_command(agent, "remove --scope project srv")
    assert "srv" not in MCPConfig(workspace).load_specs()
    assert "Removed MCP server 'srv'" in _out(capsys)
