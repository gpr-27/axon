"""Extensions say where a step went and why one failed: MCP servers at startup and per call, a crashed server's
reason, skills with broken frontmatter or a shadowed name, plugins with a broken manifest, ToolSearch misses."""
from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from axon.errors import ToolError
from axon.mcp import bridge
from axon.mcp.config import ServerSpec

SERVER = textwrap.dedent('''
    import json, os, sys, time
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": msg["params"].get("protocolVersion"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "t", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": [{"name": n, "description": "d", "inputSchema": {"type": "object"}}
                             for n in ("echo", "die", "hang")]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name == "die":
                sys.stderr.write("fatal: database file is locked\\n")
                sys.stderr.flush()
                os._exit(1)
            if name == "hang":
                time.sleep(60)
            res = {"content": [{"type": "text", "text": "ok"}]}
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
        sys.stdout.flush()
''')


class Ctx:
    agent = None
    images: list = []


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home" / ".axon"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def stdio(tmp: Path, name: str = "good") -> dict:
    script = tmp / "srv.py"
    script.write_text(SERVER)
    return {"command": sys.executable, "args": [str(script)], "timeout": 5.0}


def exits_at_once() -> dict:
    code = "import sys; sys.stderr.write('config file missing: /etc/foo.toml\\n'); sys.exit(3)"
    return {"command": sys.executable, "args": ["-c", code]}


# ── MCP ─────────────────────────────────────────────────────────────────────

def test_startup_report_names_each_server_its_tools_and_why_one_failed(tmp_path):
    b = bridge.MCPToolBridge(tmp_path)
    try:
        b.connect_all({"good": stdio(tmp_path), "exits": exits_at_once(),
                       "nocmd": {"command": "/nonexistent/binary-xyz", "args": []}})
        report = b.startup_report()
        lines = report.splitlines()
        assert lines[0] == "MCP: 1 of 3 servers connected, 3 tools"
        assert any("good" in ln and "3 tools" in ln and "stdio" in ln for ln in lines)
        assert any("nocmd" in ln and "Command not found" in ln for ln in lines)
        # the reason the process gave on stderr, not only "see the log"
        assert any("exits" in ln and "config file missing: /etc/foo.toml" in ln for ln in lines), report
    finally:
        b.disconnect_all()


def test_a_call_right_after_a_crash_waits_for_the_restart_and_the_crash_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LOCAL_RECONNECT_DELAYS", (0.4, 0.4, 0.4))
    b = bridge.MCPToolBridge(tmp_path)
    try:
        tools = {t.name: t for t in b.connect_all({"good": stdio(tmp_path)})}
        with pytest.raises(ToolError) as crash:
            tools["mcp__good__die"].run({}, Ctx())
        assert "database file is locked" in str(crash.value)          # what the server said as it died
        conn = b.get_connection("good")
        end = time.time() + 5
        while conn.status != "reconnecting" and time.time() < end:
            time.sleep(0.01)
        assert conn.status == "reconnecting"
        current = {t.name: t for t in b.active_tools} or tools
        assert current.get("mcp__good__echo", tools["mcp__good__echo"]).run({}, Ctx()) == "ok"
    finally:
        b.disconnect_all()


def test_a_server_that_never_answers_a_call_times_out_with_a_reason(tmp_path):
    b = bridge.MCPToolBridge(tmp_path)
    try:
        tools = {t.name: t for t in b.connect_all({"good": {**stdio(tmp_path), "timeout": 1.5}})}
        t0 = time.time()
        with pytest.raises(ToolError) as e:
            tools["mcp__good__hang"].run({}, Ctx())
        assert time.time() - t0 < 10
        assert "MCP tool 'hang' on 'good' failed" in str(e.value) and "did not answer" in str(e.value)
    finally:
        b.disconnect_all()


# ── what the events carry ───────────────────────────────────────────────────

def _mcp_turns(tool_name: str):
    from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock
    return [AssistantTurn(blocks=[ToolUseBlock(id="t1", name=tool_name, input={})], stop_reason="tool_use"),
            AssistantTurn(blocks=[TextBlock(text="done")], stop_reason="end_turn")]


def test_an_mcp_tool_call_event_names_the_server_and_how_long_it_took(tmp_path):
    from fakes import FakeProvider
    from test_core_helpers import make_agent
    from axon.agent.trace import TurnTrace
    from axon.features.sdk import event_to_dict
    from axon.providers.base import ToolExecutionResult, ToolExecutionStart
    from axon.tools import create_default_registry

    b = bridge.MCPToolBridge(tmp_path)
    try:
        registry = create_default_registry()
        for t in b.connect_all({"good": stdio(tmp_path)}):
            registry.register(t)
        events: list = []
        agent = make_agent(FakeProvider(_mcp_turns("mcp__good__echo")), tmp_path, registry=registry, events=events)
        agent.run_turn("call it")
    finally:
        b.disconnect_all()
    start = next(e for e in events if isinstance(e, ToolExecutionStart))
    result = next(e for e in events if isinstance(e, ToolExecutionResult))
    assert start.server == "good" and result.server == "good"
    assert result.content == "ok" and result.duration_ms >= 0 and isinstance(result.duration_ms, int)
    assert event_to_dict(start)["server"] == "good"
    wire = event_to_dict(result)
    assert wire["server"] == "good" and "duration_ms" in wire
    trace = TurnTrace(turn=1, prompt="call it", model="m", mode="bypass")
    for e in events:
        trace.record(e)
    steps = [s for s in trace.steps if s.kind == "tool"]
    assert steps and steps[0].data.get("server") == "good"


def test_a_builtin_tool_event_has_no_server(tmp_path):
    from fakes import FakeProvider
    from test_core_helpers import make_agent
    from axon.features.sdk import event_to_dict
    from axon.providers.base import ToolExecutionStart

    events: list = []
    agent = make_agent(FakeProvider(_mcp_turns("Glob")), tmp_path, events=events)
    agent.run_turn("x")
    start = next(e for e in events if isinstance(e, ToolExecutionStart))
    assert start.server == "" and "server" not in event_to_dict(start)


# ── ToolSearch ──────────────────────────────────────────────────────────────

def _search_tool(n: int = 120):
    from axon.tools.base import Tool
    from axon.tools.interactive import ToolSearchTool

    class Plain(Tool):
        def __init__(self, name: str) -> None:
            self.name, self.description = name, f"does {name}"          # type: ignore[misc]
            self.schema = {"type": "object", "properties": {}}          # type: ignore[misc]

        def run(self, args, ctx):
            return "ok"

    make = Plain
    return ToolSearchTool([make(f"mcp__big__tool_{i}") for i in range(n)] + [make("mcp__gh__create_issue")])


class _Reg:
    def __init__(self):
        self.added = {}

    def has(self, name):
        return name in self.added

    def register(self, tool):
        self.added[tool.name] = tool


class _SearchCtx:
    def __init__(self):
        self.agent = type("A", (), {})()
        self.agent.registry = _Reg()


def test_tool_search_loads_a_deferred_tool_among_many_and_says_which_names_were_wrong():
    search = _search_tool()
    ctx = _SearchCtx()
    out = search.run({"query": "select:mcp__big__tool_7,mcp__gh__create_isue"}, ctx)
    assert '"name": "mcp__big__tool_7"' in out and "mcp__big__tool_7" in ctx.agent.registry.added
    assert "Not found: mcp__gh__create_isue" in out and "mcp__gh__create_issue" in out   # the close name is offered


def test_tool_search_with_a_wrong_name_suggests_close_names_not_the_whole_list():
    search = _search_tool()
    out = search.run({"query": "select:mcp__gh__create_isue"}, _SearchCtx())
    assert "No deferred tool matches" in out and "Did you mean: mcp__gh__create_issue" in out
    assert len(out) < 2500 and "mcp__big__ (120)" in out     # servers and counts, not 121 full names twice


# ── skills ──────────────────────────────────────────────────────────────────

def _skill(root: Path, name: str, text: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


def test_a_skill_with_unclosed_frontmatter_is_reported_and_keeps_a_usable_name(tmp_path):
    from axon.skills.manager import SkillManager
    ws = tmp_path / "ws"
    path = _skill(ws / ".axon" / "skills", "broken", "---\nname: broken\ndescription: never closed\n\nDo the thing\n")
    mgr = SkillManager(ws)
    skill = mgr.discover()["broken"]
    assert skill.description != "---"
    assert any(str(path) in p and "frontmatter" in p and "---" in p for p in mgr.problems), mgr.problems


def test_a_personal_skill_that_hides_a_project_skill_of_the_same_name_is_reported(tmp_path):
    from axon.skills.manager import SkillManager
    ws = tmp_path / "ws"
    home = tmp_path / "home"
    project = _skill(ws / ".axon" / "skills", "deploy", "---\nname: deploy\ndescription: PROJECT\n---\nbody\n")
    personal = _skill(home / ".axon" / "skills", "deploy", "---\nname: deploy\ndescription: USER\n---\nbody\n")
    mgr = SkillManager(ws)
    assert mgr.discover()["deploy"].description == "USER"            # discovery order is unchanged
    assert any(str(project) in p and str(personal) in p and "deploy" in p for p in mgr.problems), mgr.problems


def test_skills_list_shows_the_problems(tmp_path, capsys):
    from axon.skills.interactive import handle_skills_command
    from axon.skills.manager import SkillManager
    ws = tmp_path / "ws"
    _skill(ws / ".axon" / "skills", "broken", "---\nname: broken\ndescription: never closed\n\nDo the thing\n")
    mgr = SkillManager(ws)
    mgr.discover()
    handle_skills_command(mgr, ws, "list")
    out = capsys.readouterr().out
    assert "Problems" in out and "frontmatter" in out


def test_the_skill_tool_result_says_which_skill_and_where_it_came_from(tmp_path):
    from fakes import FakeProvider
    from test_core_helpers import make_agent
    from axon.tools.base import ToolContext
    from axon.tools.interactive import SkillTool
    ws = tmp_path / "ws"
    path = _skill(ws / ".axon" / "skills", "deploy", "---\nname: deploy\ndescription: Deploy\n---\nRun make deploy\n")
    agent = make_agent(FakeProvider([]), ws)
    ctx = ToolContext(workspace=ws, file_state=agent.file_state, todos=agent.todos, settings=agent.settings,
                      ledger=agent.ledger, agent=agent)
    out = SkillTool().run({"skill": "deploy"}, ctx)
    assert out.startswith(f'<skill name="deploy" source="project" path="{path}">'), out[:200]


# ── plugins ─────────────────────────────────────────────────────────────────

def _plugin_src(root: Path, name: str, manifest: str) -> Path:
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    (d / "commands").mkdir()
    (d / "commands" / "yo.md").write_text("---\ndescription: yo\n---\nSay yo\n", encoding="utf-8")
    return d


def _plugin_cmd(ws: Path, arg: str) -> None:
    from axon.commands import plugin_cmds
    agent = type("A", (), {})()
    agent.settings = type("S", (), {"workspace": ws})()
    agent.workspace = ws
    plugin_cmds.run_plugin_command(agent, arg, lambda a: None, lambda a, s: None)


def test_an_installed_plugin_with_a_broken_manifest_is_listed_with_the_reason(tmp_path, capsys):
    from axon.commands import plugins as pl
    ws = tmp_path / "ws"
    ws.mkdir()
    _plugin_cmd(ws, f"install {_plugin_src(tmp_path / 'src', 'good', json.dumps({'name': 'good', 'version': '1.0.0'}))}")
    assert [p.name for p in pl.load_plugins(ws)] == ["good"]
    manifest = pl._manifest_path(tmp_path / "home" / ".axon" / "plugins" / "good")
    manifest.write_text('{"name": "good", ', encoding="utf-8")
    assert pl.load_plugins(ws) == []                               # it cannot load ...
    problems = pl.plugin_problems(ws)
    assert len(problems) == 1 and problems[0][0] == manifest.parent.parent and "not valid JSON" in problems[0][1]
    capsys.readouterr()
    _plugin_cmd(ws, "list")
    out = capsys.readouterr().out
    assert "good" in out and "not valid JSON" in out and str(manifest) in out      # ... and the list says why


def test_installing_a_plugin_with_a_bad_manifest_names_the_file_and_never_crashes(tmp_path, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = _plugin_src(tmp_path / "src", "badjson", '{"name": "badjson", "version": }')
    as_list = _plugin_src(tmp_path / "src", "aslist", '["x"]')
    _plugin_cmd(ws, f"install {bad}")
    out = capsys.readouterr().out
    assert "plugin.json" in out and "not valid JSON" in out
    _plugin_cmd(ws, f"install {as_list}")                           # was an AttributeError
    out = capsys.readouterr().out
    assert "plugin.json" in out and "JSON object" in out
    assert not (tmp_path / "home" / ".axon" / "plugins" / "aslist").exists()
