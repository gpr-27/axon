"""Parity round (P-CORE): agent loop, hooks, prompt, tools, subagents, settings and storage."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from axon.agent.loop import detect_thinking_keyword, heuristic_title
from axon.agent.prompt import build_system
from axon.agent.trace import SteerEvent, SubagentStep, ThinkingBoost
from axon.config import Settings, map_json_settings, normalize_mode
from axon.errors import ToolError
from axon.providers.base import AssistantTurn, TextBlock, ToolUseBlock, Usage
from axon.tools import create_default_registry, restrict_tools
from axon.tools.base import Tool, ToolContext
from axon.tools.interactive import AskUserQuestionTool, SkillTool, ToolSearchTool, defer_mcp_tools
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


def _hooks_file(ws: Path, hooks: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    d = ws / ".axon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"hooks": hooks, **(extra or {})}), encoding="utf-8")


def _py_hook(script: str) -> str:
    import sys
    return f'"{sys.executable}" -c "{script}"'


def _json_hook(tmp: Path, payload: dict[str, Any]) -> str:
    """A hook command that prints ``payload`` as JSON (script file: no shell-quoting trouble)."""
    import sys
    script = tmp / f"hook_{abs(hash(json.dumps(payload, sort_keys=True)))}.py"
    script.write_text(f"import json\nprint(json.dumps({payload!r}))\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


# ─── settings ───────────────────────────────────────────────────────────────

def test_new_settings_fields_and_json_mapping(workspace: Path) -> None:
    s = make_settings(workspace)
    for name, default in (("fallback_model", None), ("additional_dirs", []), ("output_style", "default"),
                          ("status_line", None), ("vim_mode", False), ("cleanup_period_days", 0),
                          ("verify_commands", []), ("verify_timeout", 900), ("repo_map", False),
                          ("repo_map_tokens", 1500), ("theme", "auto"), ("suggestions", True), ("auto_title", True)):
        assert getattr(s, name) == default, name
    assert s.permissions.ask == []
    m = map_json_settings({"fallbackModel": "a,b", "editorMode": "vim", "cleanupPeriodDays": 7, "outputStyle": "concise",
                           "statusLine": {"type": "command", "command": "echo"}, "verifyCommands": ["pytest"],
                           "permissions": {"ask": ["Bash(git push:*)"], "additionalDirectories": ["/tmp"],
                                           "defaultMode": "bypassPermissions"}})
    assert m["fallback_model"] == "a,b" and m["vim_mode"] is True and m["cleanup_period_days"] == 7
    assert m["permissions"]["ask"] == ["Bash(git push:*)"] and m["mode"] == "bypass"
    assert m["additional_dirs"] == ["/tmp"] and m["verify_commands"] == ["pytest"]
    assert Settings(workspace=workspace, cleanup_period_days=-5).cleanup_period_days == 0  # 0 = keep forever
    assert normalize_mode("bypassPermissions") == "bypass" and normalize_mode("MANUAL") == "default"
    assert normalize_mode("nonsense") is None


def test_settings_json_layers_merge_ask_and_dirs(workspace: Path, isolate_user_home: Path, tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    (isolate_user_home / ".axon").mkdir(parents=True, exist_ok=True)
    (isolate_user_home / ".axon" / "settings.json").write_text(json.dumps({"permissions": {"ask": ["Bash"]}}), encoding="utf-8")
    (workspace / ".axon").mkdir()
    (workspace / ".axon" / "settings.json").write_text(json.dumps({
        "permissions": {"ask": ["Write"], "additionalDirectories": [str(extra)]}, "fallbackModel": "gpt-4o"}), encoding="utf-8")
    s = Settings.load({"workspace": workspace, "api_key": "k-123456789"}, require_api_key=False)
    assert s.permissions.ask == ["Bash", "Write"]
    assert s.additional_dirs == [extra.resolve()] and s.fallback_model == "gpt-4o"


def test_preset_key_found_in_user_env_file(workspace: Path, isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AXON_API_KEY", "XAI_API_KEY", "AXON_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    (isolate_user_home / ".axon").mkdir(parents=True, exist_ok=True)
    (isolate_user_home / ".axon" / ".env").write_text("XAI_API_KEY=xai-real-key-123\n", encoding="utf-8")
    s = Settings.load({"workspace": workspace, "provider": "xai", "base_url": "https://api.x.ai/v1"}, require_api_key=False)
    assert s.api_key.get_secret_value() == "xai-real-key-123"
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def test_missing_key_message_points_at_setup(workspace: Path) -> None:
    from axon.config import missing_key_message
    s = Settings(workspace=workspace, api_key="", base_url="https://api.openai.com/v1", provider="openai")
    msg = missing_key_message(s)
    assert "axon setup" in msg and "AXON_API_KEY" in msg


# ─── storage ────────────────────────────────────────────────────────────────

def test_prune_old_and_once_per_day(isolate_user_home: Path, workspace: Path) -> None:
    from axon import storage
    sess = storage.sessions_dir(workspace) / "old.jsonl"
    sess.write_text("{}\n", encoding="utf-8")
    keep = storage.sessions_dir(workspace) / "new.jsonl"
    keep.write_text("{}\n", encoding="utf-8")
    cp = storage.checkpoints_dir(workspace, "old") / "1"
    cp.mkdir(parents=True)
    (cp / "f.txt").write_text("x", encoding="utf-8")
    old = time.time() - 40 * 86400
    for f in (sess, cp / "f.txt", cp, cp.parent):
        os.utime(f, (old, old))
    assert storage.prune_old(30) >= 2
    assert not sess.exists() and keep.exists() and not cp.parent.exists()
    assert storage.prune_on_startup(30) == 0  # nothing left to prune
    sess.write_text("{}\n", encoding="utf-8")
    os.utime(sess, (old, old))
    assert storage.prune_on_startup(30) == 0 and sess.exists()  # stamped: skipped within a day


# ─── hooks ──────────────────────────────────────────────────────────────────

def test_post_tool_use_failure_and_batch_hooks(workspace: Path, tmp_path: Path) -> None:
    log = tmp_path / "events.log"
    cmd = _py_hook("import sys,json; d=json.load(sys.stdin); open(r'%s','a').write(d['hook_event_name']+chr(10))" % log)
    _hooks_file(workspace, {"PostToolUseFailure": [{"hooks": [{"type": "command", "command": cmd}]}],
                            "PostToolBatch": [{"hooks": [{"type": "command", "command": cmd}]}]})
    agent = make_agent(FakeProvider(scripted(("Read", {"path": "missing.txt"}), "done")), workspace)
    agent.run_turn("read it")
    assert log.read_text(encoding="utf-8").split() == ["PostToolUseFailure", "PostToolBatch"]


def test_permission_request_hook_allows_without_prompt(workspace: Path, tmp_path: Path) -> None:
    cmd = _json_hook(tmp_path, {"hookSpecificOutput": {"decision": {"behavior": "allow"}}})
    _hooks_file(workspace, {"PermissionRequest": [{"matcher": "Write", "hooks": [{"type": "command", "command": cmd}]}]})
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace, mode="default")
    agent.run_turn("write")
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "hi"


def test_http_hook_posts_payload_and_can_block(workspace: Path) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen: list[dict[str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"payload": json.loads(body), "auth": self.headers.get("Authorization")})
            out = json.dumps({"decision": "block", "reason": "nope from http"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a: Any) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["HOOK_TOKEN_T"] = "s3cret"
    try:
        _hooks_file(workspace, {"UserPromptSubmit": [{"hooks": [{
            "type": "http", "url": f"http://127.0.0.1:{srv.server_port}/hook",
            "headers": {"Authorization": "Bearer $HOOK_TOKEN_T", "X-Other": "$HOME"}, "allowedEnvVars": ["HOOK_TOKEN_T"]}]}]})
        from axon.hooks import HookManager
        res = HookManager(make_settings(workspace), workspace).run("UserPromptSubmit", {"prompt": "hi"})
        assert res.blocked and "nope from http" in res.message
        assert seen[0]["payload"]["prompt"] == "hi" and seen[0]["payload"]["hook_event_name"] == "UserPromptSubmit"
        assert seen[0]["auth"] == "Bearer s3cret"
    finally:
        srv.shutdown()
        os.environ.pop("HOOK_TOKEN_T", None)


def test_disable_all_hooks(workspace: Path) -> None:
    _hooks_file(workspace, {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}, {"disableAllHooks": True})
    from axon.hooks import HookManager
    assert HookManager(make_settings(workspace), workspace).describe() == []
    _hooks_file(workspace, {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]})
    assert len(HookManager(make_settings(workspace), workspace).describe()) == 1
    assert HookManager(make_settings(workspace, disable_all_hooks=True), workspace).describe() == []


def test_post_compact_hook_and_reread_recent_files(workspace: Path, tmp_path: Path) -> None:
    log = tmp_path / "pc.log"
    cmd = _py_hook("import sys,json; d=json.load(sys.stdin); open(r'%s','a').write(d['trigger'])" % log)
    _hooks_file(workspace, {"PostCompact": [{"hooks": [{"type": "command", "command": cmd}]}]})
    turns = scripted(("Write", {"path": "app.py", "content": "print('v1')\n"}), "wrote", "two", "three")
    agent = make_agent(FakeProvider(turns), workspace)
    agent.run_turn("create app.py")
    agent.run_turn("second")
    agent.run_turn("third")
    (workspace / "app.py").write_text("print('v2')\n", encoding="utf-8")
    agent.compact(trigger="manual", summary="summary of work")
    assert log.read_text(encoding="utf-8") == "manual"
    sys_text = "\n".join(b["text"] for b in agent.system_blocks())
    assert "re-read from disk after context compaction" in sys_text and "print('v2')" in sys_text


# ─── loop: steer, thinking boost, shell context, title, persistence ─────────

def test_steer_messages_delivered_between_tool_rounds(workspace: Path) -> None:
    events: list[Any] = []
    prov = FakeProvider(scripted(("Ls", {"path": "."}), "final"))
    agent = make_agent(prov, workspace, events=events)
    agent.message_queue.push("also check the README", steer=True)
    agent.message_queue.push("a later prompt")
    agent.run_turn("list files")
    second = json.dumps(prov.requests[1]["messages"][-1])
    assert "[User added while you were working]: also check the README" in second
    assert [it.text for it in agent.message_queue.items] == ["a later prompt"]
    assert any(isinstance(e, SteerEvent) and e.count == 1 for e in events)
    assert any(s.kind == "steer" for s in agent.last_trace.steps)


def test_thinking_keyword_boosts_effort_for_one_turn(workspace: Path) -> None:
    seen: list[tuple[str, bool]] = []

    class P(FakeProvider):
        def stream(self, **kw: Any):  # type: ignore[override]
            seen.append((kw.get("effort"), kw.get("thinking")))
            return super().stream(**kw)

    agent = make_agent(P(scripted("a", "b")), workspace, effort="balanced", thinking=False)
    agent.run_turn("ultrathink about the design")
    agent.run_turn("normal question")
    assert seen == [("quantum", True), ("balanced", False)]
    assert agent.settings.effort == "balanced" and agent.settings.thinking is False
    assert any(s.kind == "thinking_boost" and s.data["keyword"] == "ultrathink" for s in agent.traces[0].steps)
    assert detect_thinking_keyword("think hard please") == ("think hard", "synapse")
    assert detect_thinking_keyword("I was rethinking this") is None


def test_shell_context_is_prepended_to_next_prompt(workspace: Path) -> None:
    prov = FakeProvider(scripted("ok"))
    agent = make_agent(prov, workspace)
    agent.add_shell_context("ls", "a.txt\nb.txt\n", 0)
    agent.run_turn("what files are there?")
    first_user = prov.requests[0]["messages"][0]["content"]
    assert "<bash-input>ls</bash-input>" in first_user and "a.txt" in first_user
    assert first_user.rstrip().endswith("what files are there?")
    assert agent.take_pending_context() == ""


def test_auto_title_and_session_records(workspace: Path) -> None:
    from axon.session.store import iter_records
    agent = make_agent(FakeProvider(scripted(("Ls", {"path": "."}), "done")), workspace)
    agent.start_session()
    agent.run_turn("Fix the flaky login test in auth.py. Then run the suite.")
    recs = list(iter_records(agent.session.active_file))
    kinds = [r["type"] for r in recs]
    assert "permission_mode" in kinds and "permission" in kinds
    title = next(r["data"]["title"] for r in recs if r["type"] == "session_rename")
    assert title == "Fix the flaky login test in auth.py"
    turn = next(r["data"] for r in recs if r["type"] == "assistant_turn")
    assert turn["model"] and turn["duration_s"] is not None
    results = next(r["data"] for r in recs if r["type"] == "tool_results")
    assert "duration_s" in results[0]
    agent2 = make_agent(FakeProvider(scripted("x")), workspace)
    agent2.session.open(agent.session.active_session_id)
    assert agent2.maybe_auto_title("other") is None  # already titled
    assert heuristic_title("```code``` " + "word " * 30).endswith("…")


def test_background_current_turn_detaches_and_completes(workspace: Path) -> None:
    gate = threading.Event()

    class SlowTool(Tool):
        name = "Slow"
        description = "waits"
        schema = {"type": "object", "properties": {}}
        readonly = True
        default_permission = "allow"

        def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
            gate.wait(5)
            return "slow done"

    reg = create_default_registry()
    reg.register(SlowTool())
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Slow", {}), "all finished")), workspace, registry=reg, events=events)
    out: dict[str, Any] = {}

    def trigger() -> None:
        for _ in range(100):
            if agent.busy:
                break
            time.sleep(0.01)
        time.sleep(0.05)
        out["rec"] = agent.background_current_turn()

    threading.Thread(target=trigger).start()
    assert agent.run_turn_threaded("do slow work") is None
    rec = out["rec"]
    assert rec.id in agent.background_tasks and rec.status == "running" and agent.busy
    n_events = len(events)
    gate.set()
    rec.thread.join(5)
    assert rec.status == "completed" and rec.result.final_text == "all finished"
    assert len(events) == n_events  # live output was captured, not rendered
    assert any(line.startswith("step 2") for line in rec.log)
    assert agent.on_event == events.append and not agent.busy


def test_cancel_current_turn_interrupts_worker(workspace: Path) -> None:
    class Spin(Tool):
        name = "Spin"
        description = "spins"
        schema = {"type": "object", "properties": {}}
        readonly = True
        default_permission = "allow"

        def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
            end = time.time() + 5
            while time.time() < end:
                time.sleep(0.01)
            return "never"

    reg = create_default_registry()
    reg.register(Spin())
    agent = make_agent(FakeProvider(scripted(("Spin", {}), "x")), workspace, registry=reg)

    def cancel() -> None:
        time.sleep(0.2)
        agent.cancel_current_turn()

    threading.Thread(target=cancel).start()
    t0 = time.time()
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn_threaded("spin")
    assert time.time() - t0 < 3
    agent.conversation.validate()  # pairing invariant kept


# ─── prompt ─────────────────────────────────────────────────────────────────

def test_prompt_override_style_repo_map_and_dirs(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.commands.output_styles as os_mod
    import axon.features.repo_map as rm
    monkeypatch.setattr(os_mod, "active_style_prompt", lambda settings, ws=None: "Explain your reasoning with insights.")
    monkeypatch.setattr(rm, "repo_map_for_prompt", lambda ws, settings=None, max_tokens=None: "src/app.py: main()")
    extra = tmp_path / "shared"
    extra.mkdir()
    s = make_settings(workspace, system_prompt_override="You are a pirate coder.", output_style="explanatory",
                      repo_map=True, additional_dirs=[extra])
    text = "\n".join(b["text"] for b in build_system(s, create_default_registry(), git_status=""))
    assert text.startswith("You are a pirate coder.") and "You are Axon" not in text
    assert "## Output style: explanatory" in text and "insights" in text
    assert "## Repository map" in text and "src/app.py: main()" in text
    assert f"Additional directory (readable/editable): {extra.resolve()}" in text
    plain = "\n".join(b["text"] for b in build_system(make_settings(workspace), create_default_registry(), git_status=""))
    assert "Output style" not in plain and "Repository map" not in plain and "You are Axon" in plain


def test_agent_caches_repo_map_per_session(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.features.repo_map as rm
    calls: list[int] = []
    monkeypatch.setattr(rm, "repo_map_for_prompt", lambda ws, settings=None, max_tokens=None: calls.append(1) or "map")
    agent = make_agent(FakeProvider(scripted("a", "b")), workspace, repo_map=True)
    agent.run_turn("one")
    agent.run_turn("two")
    assert len(calls) == 1


# ─── tools ──────────────────────────────────────────────────────────────────

def _ctx(agent: Any) -> ToolContext:
    return ToolContext(workspace=agent.settings.workspace, file_state=agent.file_state, todos=agent.todos,
                       settings=agent.settings, agent=agent)


def test_skill_tool_loads_body(workspace: Path) -> None:
    d = workspace / ".axon" / "skills" / "deploy"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy the app\n---\nRun make deploy with $ARGUMENTS\n",
                                encoding="utf-8")
    agent = make_agent(FakeProvider([]), workspace)
    out = SkillTool().run({"skill": "deploy", "args": "staging"}, _ctx(agent))
    assert "make deploy with staging" in out and '<skill name="deploy">' in out
    with pytest.raises(ToolError):
        SkillTool().run({"skill": "nope"}, _ctx(agent))
    text = "\n".join(b["text"] for b in agent.system_blocks())
    assert "**/deploy**" in text and "Skill tool" in text


def test_ask_user_question_tool(workspace: Path) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    args = {"questions": [{"question": "Which DB?", "header": "DB", "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
                          {"question": "Extras?", "options": ["auth", "cache"], "multiSelect": True}]}
    answers = iter([["SQLite"], ["auth", "cache"]])
    tool = AskUserQuestionTool(ask=lambda q, i, n: next(answers), interactive=lambda: True)
    out = tool.run(args, _ctx(agent))
    assert '"Which DB?" -> SQLite' in out and '"Extras?" -> auth, cache' in out
    with pytest.raises(ToolError, match="interactive terminal"):
        AskUserQuestionTool(interactive=lambda: False).run(args, _ctx(agent))
    with pytest.raises(ToolError, match="dismissed"):
        AskUserQuestionTool(ask=lambda q, i, n: None, interactive=lambda: True).run(args, _ctx(agent))
    with pytest.raises(ToolError):
        tool.run({"questions": []}, _ctx(agent))
    assert AskUserQuestionTool.readonly


class _FakeMcp(Tool):
    readonly = True
    default_permission = "allow"

    def __init__(self, name: str, desc: str) -> None:
        self._n, self._d = name, desc

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._n

    @property
    def description(self) -> str:  # type: ignore[override]
        return self._d

    @property
    def schema(self) -> dict[str, Any]:  # type: ignore[override]
        return {"type": "object", "properties": {"q": {"type": "string", "description": "x" * 400}}}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        return f"{self._n} ran with {args}"


def test_tool_search_defers_mcp_schemas(workspace: Path) -> None:
    reg = create_default_registry()
    for i in range(30):
        reg.register(_FakeMcp(f"mcp__gh__tool_{i}", f"GitHub operation number {i}"))
    reg.register(_FakeMcp("mcp__slack__send_message", "Send a Slack message"))
    search = defer_mcp_tools(reg, context_window=20_000)
    assert isinstance(search, ToolSearchTool)
    assert not any(n.startswith("mcp__") for n in reg.visible_names()) and "ToolSearch" in reg.visible_names()
    assert "mcp__slack__send_message" in search.description
    agent = make_agent(FakeProvider([]), workspace, registry=reg)
    before = json.dumps(reg.schemas())
    out = search.run({"query": "slack message"}, _ctx(agent))
    assert "mcp__slack__send_message" in out.splitlines()[2]
    assert reg.get("mcp__slack__send_message").run({"q": "hi"}, _ctx(agent)).startswith("mcp__slack__send_message ran")
    assert json.dumps(reg.schemas()) == before  # revealed tools stay hidden: cache-stable tool list
    assert [t.name for t in search.search("select:mcp__gh__tool_3,MCP__GH__TOOL_4")] == ["mcp__gh__tool_3", "mcp__gh__tool_4"]
    small = create_default_registry()
    small.register(_FakeMcp("mcp__x__y", "tiny"))
    assert defer_mcp_tools(small, context_window=1_000_000) is None and "mcp__x__y" in small.visible_names()


def test_restrict_tools_and_feature_tools() -> None:
    reg = create_default_registry()
    assert "UsageStats" in reg.visible_names()
    only = restrict_tools(reg, "Bash,Read")
    assert set(only.visible_names()) == {"Bash", "Read"}
    assert restrict_tools(reg, "") .visible_names() == []
    assert len(restrict_tools(reg, "default").visible_names()) == len(reg.visible_names())


# ─── subagents ──────────────────────────────────────────────────────────────

def test_feature_and_session_agents(workspace: Path) -> None:
    from axon.agent.subagent import (clear_session_agents, get_agent_definition, load_agent_definitions,
                                     parse_agents_json, register_session_agents)
    assert "analytics" in {d.name for d in load_agent_definitions(workspace)}
    defs = parse_agents_json('{"reviewer": {"description": "Reviews", "prompt": "Be strict", "tools": ["Read"], "model": "gpt-4o"},'
                             ' "explore": {"description": "Custom explore", "prompt": "mine"}}')
    register_session_agents(defs)
    try:
        r = get_agent_definition(workspace, "reviewer")
        assert r is not None and r.tools == ["Read"] and r.model == "gpt-4o" and r.source == "cli"
        assert get_agent_definition(workspace, "explore").system_prompt == "mine"
    finally:
        clear_session_agents()
    with pytest.raises(ValueError):
        parse_agents_json('{"x": {"description": "no prompt"}}')


def test_subagent_steps_nested_in_parent_trace(workspace: Path) -> None:
    turns = scripted(("Task", {"prompt": "look around", "subagent_type": "explore"}), ("Ls", {"path": "."}), "found it", "parent done")
    events: list[Any] = []
    agent = make_agent(FakeProvider(turns), workspace, events=events)
    agent.run_turn("delegate")
    sub = next(s for s in agent.last_trace.steps if s.kind == "subagent")
    kinds = [x["kind"] for x in sub.data["steps"]]
    assert kinds.count("llm") == 2 and kinds.count("tool") == 1
    assert sub.data["status"] == "completed" and sub.data["phase"] == "end"
    assert any(isinstance(e, SubagentStep) for e in events)
    tool_step = next(x for x in sub.data["steps"] if x["kind"] == "tool")
    assert tool_step["name"] == "Ls" and tool_step["summary"].startswith(".")


def test_subagent_start_hook_adds_context(workspace: Path, tmp_path: Path) -> None:
    cmd = _json_hook(tmp_path, {"hookSpecificOutput": {"additionalContext": "USE-TABS"}})
    _hooks_file(workspace, {"SubagentStart": [{"matcher": "explore", "hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Task", {"prompt": "go", "subagent_type": "explore"}), "sub done", "parent done"))
    agent = make_agent(prov, workspace)
    agent.run_turn("delegate")
    sub_first = prov.requests[1]["messages"][0]["content"]
    assert "USE-TABS" in sub_first


def test_thinking_boost_event_dataclass() -> None:
    e = ThinkingBoost(keyword="ultrathink", effort="quantum", previous_effort="balanced")
    assert e.thinking is True


def test_session_title_property_and_cancel_all(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("ok")), workspace)
    assert agent.session_title == ""
    agent.run_turn("Refactor the parser module")
    assert agent.session_title == "Refactor the parser module"
    fresh = make_agent(FakeProvider([]), workspace)
    fresh.session.open(agent.session.active_session_id)
    assert fresh.session_title == "Refactor the parser module"

    from axon.agent.subagent import SubagentManager
    mgr = SubagentManager()
    task = mgr.register("spin forever")
    hit = threading.Event()
    caught: list[str] = []

    def worker() -> None:
        mgr.bind_thread(task.id, threading.get_ident())
        try:
            end = time.time() + 5
            hit.set()
            while time.time() < end:
                time.sleep(0.01)
        except KeyboardInterrupt:
            caught.append("cancelled")

    th = threading.Thread(target=worker)
    th.start()
    hit.wait(2)
    assert mgr.cancel_all() == 1
    th.join(3)
    assert caught == ["cancelled"]


def test_goal_prompt_and_single_stop_check(workspace: Path) -> None:
    events: list[Any] = []
    prov = FakeProvider(scripted("did part", "goal confirmed", "next turn"))
    agent = make_agent(prov, workspace, events=events)
    agent.goal = "all tests pass"
    res = agent.run_turn("work on it")
    assert res.final_text == "goal confirmed" and len(prov.requests) == 2
    assert "all tests pass" in "\n".join(b["text"] for b in prov.requests[0]["system"])
    assert "[Goal check]" in json.dumps(prov.requests[1]["messages"][-1])
    assert any(s.kind == "goal_check" for s in agent.last_trace.steps)
    agent.goal = None
    agent.run_turn("again")
    assert len(prov.requests) == 3


def test_prompt_color_setting_maps() -> None:
    assert map_json_settings({"promptColor": "cyan"})["prompt_color"] == "cyan"
