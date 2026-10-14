"""Parity commands: output styles, preferences, /loop, /add-dir, /cd, ask rules, rewind, /context grid,
/cost cache line, /copy N, /export, /btw context, /tasks, purge, release notes, terminal setup, /config, /agents."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon.agent.context import ContextManager
from axon.agent.loop import Agent
from axon.commands import list_command_names
from axon.commands.builtin import dispatch_command
from axon.config import Settings
from axon.permissions.engine import PermissionEngine
from axon.providers.base import AssistantTurn, TextBlock, Usage
from axon.session.ledger import Ledger
from axon.session.store import SessionStore
from axon.tools import create_default_registry
from fakes import FakeProvider

NEW_COMMANDS = ("/output-style", "/statusline", "/theme", "/vim", "/color", "/fast", "/goal", "/loop", "/cd",
                "/add-dir", "/release-notes", "/terminal-setup", "/rewind", "/config", "/tasks", "/agents")


def _turn(text: str) -> AssistantTurn:
    return AssistantTurn(blocks=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage(input=10, output=5))


def _make(workspace: Path, texts: list[str] | None = None, **kw) -> Agent:
    s = Settings(workspace=workspace, mode="default", **kw)
    return Agent(provider=FakeProvider([_turn(t) for t in (texts or [f"answer {i}" for i in range(10)])]),
                 tools=create_default_registry(), permissions=PermissionEngine(s), context=ContextManager(s),
                 session=SessionStore(workspace), ledger=Ledger(), settings=s)


@pytest.fixture
def agent(workspace: Path) -> Agent:
    return _make(workspace)


def _user_settings(home: Path) -> dict:
    p = home / ".axon" / "settings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ─── registry / help ───────────────────────────────────────────────────────

def test_every_new_command_is_listed_and_in_help(agent: Agent, capsys):
    names = dict(list_command_names(None))
    for c in NEW_COMMANDS:
        assert c in names and names[c], c
    dispatch_command("/help", agent)
    out = capsys.readouterr().out
    for c in NEW_COMMANDS:
        assert c in out, c
    dispatch_command("/help loop", agent)
    assert "/loop <interval> <prompt>" in capsys.readouterr().out


# ─── settings.json helper ──────────────────────────────────────────────────

def test_update_settings_file_merges_deletes_and_refuses_malformed(isolate_user_home: Path, workspace: Path):
    from axon.commands.common import settings_file, update_settings_file
    p = settings_file("user")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"model": "keep-me", "theme": "dark"}), encoding="utf-8")
    assert update_settings_file({"theme": None, "permissions.additionalDirectories": ["/x"]}) == p
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"model": "keep-me", "permissions": {"additionalDirectories": ["/x"]}}
    p.write_text("{not json", encoding="utf-8")
    assert update_settings_file({"theme": "light"}) is None
    assert p.read_text(encoding="utf-8") == "{not json"
    assert settings_file("local", workspace) == workspace / ".axon" / "settings.local.json"


# ─── output styles ─────────────────────────────────────────────────────────

def test_output_styles_builtins_files_and_active_prompt(workspace: Path, isolate_user_home: Path):
    from axon.commands.output_styles import active_style_prompt, get_style, list_styles
    names = {s.name for s in list_styles(workspace)}
    assert {"default", "explanatory", "learning", "concise"} <= names
    d = workspace / ".axon" / "output-styles"
    d.mkdir(parents=True)
    (d / "pirate.md").write_text("---\nname: Pirate\ndescription: Talk like a pirate\nkeep-coding-instructions: false\n---\nArr, answer like a pirate.\n", encoding="utf-8")
    st = get_style("pirate", workspace)
    assert st is not None and st.description == "Talk like a pirate" and st.keep_coding_instructions is False
    assert st.source == "project"
    assert active_style_prompt(SimpleNamespace(output_style="pirate", workspace=workspace)) == "Arr, answer like a pirate."
    assert active_style_prompt(SimpleNamespace(output_style="default", workspace=workspace)) is None
    assert active_style_prompt(SimpleNamespace(output_style="missing", workspace=workspace)) is None
    assert "Explanatory" in (active_style_prompt(SimpleNamespace(output_style="explanatory", workspace=workspace)) or "")


def test_examples_output_style_parses():
    from axon.commands.output_styles import parse_style_file
    path = Path(__file__).resolve().parent.parent / "examples" / "output-styles" / "explanatory.md"
    if not path.exists():
        pytest.skip("examples not present")
    st = parse_style_file(path, "example")
    assert st is not None and st.name == "explanatory" and st.description and st.prompt and "---" not in st.prompt[:3]


def test_output_style_command_switches_and_creates(agent: Agent, isolate_user_home: Path, capsys):
    dispatch_command("/output-style concise", agent)
    assert agent.settings.output_style == "concise"
    assert _user_settings(isolate_user_home)["outputStyle"] == "concise"
    dispatch_command("/output-style nope", agent)
    assert "No output style" in capsys.readouterr().out
    dispatch_command("/output-style new reviewer Strict reviewer voice", agent)
    created = isolate_user_home / ".axon" / "output-styles" / "reviewer.md"
    assert created.exists() and "description: Strict reviewer voice" in created.read_text(encoding="utf-8")
    dispatch_command("/output-style", agent)
    assert "reviewer" in capsys.readouterr().out


# ─── preferences ───────────────────────────────────────────────────────────

def test_statusline_set_default_and_off(agent: Agent, isolate_user_home: Path):
    (isolate_user_home / ".axon").mkdir(parents=True, exist_ok=True)
    (isolate_user_home / ".axon" / "settings.json").write_text(json.dumps({"model": "m"}), encoding="utf-8")
    dispatch_command("/statusline echo hi", agent)
    data = _user_settings(isolate_user_home)
    assert data["statusLine"] == {"type": "command", "command": "echo hi", "padding": 0} and data["model"] == "m"
    assert agent.settings.status_line["command"] == "echo hi"
    dispatch_command("/statusline default", agent)
    script = isolate_user_home / ".axon" / "statusline.py"
    assert script.exists() and str(script) in _user_settings(isolate_user_home)["statusLine"]["command"]
    dispatch_command("/statusline off", agent)
    assert "statusLine" not in _user_settings(isolate_user_home) and agent.settings.status_line is None


def test_default_statusline_script_runs(isolate_user_home: Path):
    import subprocess
    import sys
    from axon.commands.prefs_cmds import default_statusline_command
    path, _cmd = default_statusline_command()
    payload = json.dumps({"model": {"display_name": "Opus"}, "workspace": {"current_dir": "/w/proj"},
                          "cost": {"total_cost_usd": 0.5}, "context_window": {"used_percentage": 42}})
    out = subprocess.run([sys.executable, str(path)], input=payload, capture_output=True, text=True, timeout=20).stdout
    assert out.strip() == "Opus | proj | ctx 42% | $0.5000"


def test_theme_vim_color_persist(agent: Agent, isolate_user_home: Path, monkeypatch, capsys):
    import axon.ui.theme as theme
    calls = []
    monkeypatch.setattr(theme, "set_theme", lambda n: calls.append(n), raising=False)
    dispatch_command("/theme light", agent)
    assert calls == ["light"] and agent.settings.theme == "light"
    dispatch_command("/theme purple-haze", agent)
    assert "Unknown theme" in capsys.readouterr().out
    dispatch_command("/vim", agent)
    assert agent.settings.vim_mode is True and _user_settings(isolate_user_home)["editorMode"] == "vim"
    dispatch_command("/vim off", agent)
    assert agent.settings.vim_mode is False and _user_settings(isolate_user_home)["editorMode"] == "normal"
    dispatch_command("/color green", agent)
    assert agent.prompt_color == "green" and _user_settings(isolate_user_home)["promptColor"] == "green"
    dispatch_command("/color default", agent)
    assert "promptColor" not in _user_settings(isolate_user_home)
    assert _user_settings(isolate_user_home)["theme"] == "light"


def test_fast_mode_is_session_only(workspace: Path, isolate_user_home: Path, monkeypatch, capsys):
    a = _make(workspace, small_model="tiny-model", model="big-model")
    import axon.providers.registry as reg
    monkeypatch.setattr(reg, "provider_for", lambda model, settings: FakeProvider([]))
    dispatch_command("/fast", a)
    assert a.settings.model == "tiny-model" and a.fast_mode is True
    dispatch_command("/fast off", a)
    assert a.settings.model == "big-model" and a.fast_mode is False
    assert not (isolate_user_home / ".axon" / "config.toml").exists()  # never persisted
    b = _make(workspace)
    dispatch_command("/fast on", b)
    assert "No small model configured" in capsys.readouterr().out


def test_goal_set_show_clear(agent: Agent, capsys):
    from axon.commands.prefs_cmds import goal_prompt_section
    dispatch_command("/goal all tests pass", agent)
    assert agent.goal == "all tests pass" and agent.settings.goal == "all tests pass"
    assert "all tests pass" in goal_prompt_section(agent)
    dispatch_command("/status", agent)
    assert "all tests pass" in capsys.readouterr().out
    dispatch_command("/goal clear", agent)
    assert agent.goal is None and goal_prompt_section(agent) is None


# ─── /loop ─────────────────────────────────────────────────────────────────

def test_loop_count_mode_queues_prompts(agent: Agent):
    dispatch_command("/loop 3x run the tests", agent)
    assert [it.text for it in agent.message_queue.items] == ["run the tests"] * 3


def test_loop_interval_never_stacks_and_stops(agent: Agent):
    from axon.commands.workspace_cmds import LOOP_WAKE, _tick, parse_interval, start_interval_loop, stop_loops
    assert parse_interval("5m") == 300 and parse_interval("30s") == 30 and parse_interval("abc") is None
    now = [1000.0]
    LOOP_WAKE.clear()
    job = start_interval_loop(agent, "check deploy", 60, max_runs=3, clock=lambda: now[0], start_thread=False)
    assert len(agent.message_queue) == 1 and job.runs == 1 and LOOP_WAKE.is_set()
    now[0] += 120
    assert _tick(agent, job, now[0]) is False      # previous run still queued: no stacking
    agent.message_queue.pop()
    assert _tick(agent, job, now[0]) is True and job.runs == 2
    agent.message_queue.pop()
    assert _tick(agent, job, now[0] + 10) is False  # not due yet
    assert _tick(agent, job, now[0] + 61) is True and job.runs == 3 and not job.active  # max_runs reached
    job2 = start_interval_loop(agent, "again", 5, clock=lambda: now[0], start_thread=False)
    assert job2.interval_s == 10  # minimum interval
    assert stop_loops(agent) == 1 and not job2.active and len(agent.message_queue) == 1


def test_loop_interval_thread_and_command(agent: Agent, capsys):
    dispatch_command("/loop 10s ping", agent)
    jobs = [j for j in agent.loops if j.active]
    assert len(jobs) == 1 and jobs[0].thread is not None and jobs[0].thread.daemon
    dispatch_command("/loop", agent)
    assert "ping" in capsys.readouterr().out
    dispatch_command("/loop stop", agent)
    assert not any(j.active for j in agent.loops)


# ─── /add-dir, /cd, ask rules ──────────────────────────────────────────────

def test_add_dir_allows_outside_paths(agent: Agent, tmp_path: Path):
    from axon.permissions.paths import remove_additional_dir, resolve_in_workspace
    from axon.errors import PermissionDenied
    other = tmp_path / "other_repo"
    other.mkdir()
    (other / "a.txt").write_text("x", encoding="utf-8")
    read = agent.registry.get("Read")
    assert agent.permissions.check(read, {"file_path": str(other / "a.txt")}).outcome == "ask"
    with pytest.raises(PermissionDenied):
        resolve_in_workspace(agent.settings.workspace, other / "a.txt", allow_outside=False)
    try:
        dispatch_command(f"/add-dir {other} --save", agent)
        assert agent.permissions.check(read, {"file_path": str(other / "a.txt")}).outcome == "allow"
        resolve_in_workspace(agent.settings.workspace, other / "a.txt", allow_outside=False)
        local = json.loads((agent.settings.workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))
        assert str(other.resolve()) in local["permissions"]["additionalDirectories"]
        dispatch_command(f"/add-dir remove {other}", agent)
        assert agent.permissions.check(read, {"file_path": str(other / "a.txt")}).outcome == "ask"
    finally:
        remove_additional_dir(other)


def test_cd_moves_workspace_and_transcript(agent: Agent, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(agent.settings.workspace)
    agent.run_turn("hello there")
    sid = agent.session.active_session_id
    new = tmp_path / "second"
    new.mkdir()
    dispatch_command(f"/cd {new}", agent)
    assert Path(agent.settings.workspace) == new.resolve()
    assert agent.permissions.workspace == new.resolve()
    assert agent.session.active_session_id == sid and agent.session.active_file.exists()
    assert Path.cwd() == new.resolve()
    assert any(m.get("content") == "hello there" for m in agent.session.read_conversation(sid).messages)


def test_ask_rules_beat_allow_and_bypass(workspace: Path):
    s = Settings(workspace=workspace, mode="bypass")
    eng = PermissionEngine(s)
    bash = SimpleNamespace(name="Bash", readonly=False, default_permission="ask")
    eng.add_rule("allow", "Bash(git push:*)")
    assert eng.check(bash, {"command": "git push origin"}).outcome == "allow"
    eng.add_rule("ask", "Bash(git push:*)")
    d = eng.check(bash, {"command": "git status && git push origin"})
    assert d.outcome == "ask" and "Ask rule" in d.reason
    data = json.loads((workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))
    assert data["permissions"]["ask"] == ["Bash(git push:*)"]
    eng2 = PermissionEngine(s)  # reloaded from disk
    assert eng2.check(bash, {"command": "git push"}).outcome == "ask"
    assert eng2.remove_rule("ask", "Bash(git push:*)") == ["local"]


def test_permissions_command_ask_and_list(agent: Agent, capsys):
    dispatch_command("/permissions ask Bash(npm publish:*)", agent)
    dispatch_command("/permissions", agent)
    out = capsys.readouterr().out
    assert "Ask rules (1)" in out and "npm publish" in out


def test_grant_for_persists_dont_ask_again(agent: Agent):
    bash = agent.registry.get("Bash")
    rule = agent.permissions.grant_for(bash, {"command": "npm test -- -x"}, agent.settings.workspace)
    assert str(rule) == "Bash(npm test:*)"
    local = json.loads((agent.settings.workspace / ".axon" / "settings.local.json").read_text(encoding="utf-8"))
    assert "Bash(npm test:*)" in local["permissions"]["allow"]


# ─── rewind ────────────────────────────────────────────────────────────────

def _edit(agent: Agent, f: Path, data: bytes) -> None:
    agent.checkpoints.capture_before_edit(f)
    f.write_bytes(data)


def test_rewind_points_and_code_and_conversation(agent: Agent, workspace: Path):
    from axon.commands.session_cmds import apply_rewind, rewind_points
    agent.checkpoints.set_session(agent.session.active_session_id)
    f = workspace / "app.py"
    f.write_bytes(b"v0")
    agent.run_turn("first prompt")
    _edit(agent, f, b"v1")
    agent.checkpoints.commit_turn_snapshot()
    time.sleep(0.02)
    agent.run_turn("second prompt")
    _edit(agent, f, b"v2")
    agent.checkpoints.commit_turn_snapshot()
    pts = rewind_points(agent)
    assert [p.prompt for p in pts] == ["first prompt", "second prompt"]
    assert all(p.ts for p in pts) and [p.files_changed for p in pts] == [1, 1]
    res = apply_rewind(agent, 2, "code_and_conversation")
    assert f.read_bytes() == b"v1"
    assert [m["content"] for m in agent.conversation.messages if m["role"] == "user"] == ["first prompt"]
    assert res["prompt"] == "second prompt" and agent.pending_input == "second prompt"
    # persisted: a resume sees the rewound history and the points stay consistent
    conv = agent.session.read_conversation(agent.session.active_session_id)
    assert len(conv.messages) == len(agent.conversation.messages)
    assert len(agent.session.user_message_times()) == 1
    with pytest.raises(ValueError):
        apply_rewind(agent, 5, "code")
    with pytest.raises(ValueError):
        apply_rewind(agent, 1, "bogus")


def test_rewind_conversation_only_and_code_only(agent: Agent, workspace: Path):
    from axon.commands.session_cmds import apply_rewind
    f = workspace / "a.txt"
    f.write_bytes(b"orig")
    agent.run_turn("only prompt")
    _edit(agent, f, b"new")
    agent.checkpoints.commit_turn_snapshot()
    apply_rewind(agent, 1, "conversation")
    assert f.read_bytes() == b"new" and agent.conversation.messages == []
    agent.run_turn("again")
    apply_rewind(agent, 1, "code")
    assert f.read_bytes() == b"new"  # the edit happened before "again": nothing after it to undo
    assert agent.conversation.messages  # code-only keeps the conversation


def test_rewind_summarize_from_here(workspace: Path):
    a = _make(workspace, ["a1", "a2", "a3", "SUMMARY TEXT"])
    from axon.commands.session_cmds import apply_rewind
    a.run_turn("p1")
    a.run_turn("p2")
    a.run_turn("p3")
    res = apply_rewind(a, 2, "summarize_from_here")
    assert res["summary"] == "SUMMARY TEXT"
    msgs = a.conversation.messages
    assert msgs[0]["content"] == "p1" and "SUMMARY TEXT" in msgs[-2]["content"] and msgs[-1]["role"] == "assistant"
    assert len(msgs) == 4


def test_rewind_command_non_tty_lists_points(agent: Agent, capsys):
    agent.run_turn("describe the repo")
    dispatch_command("/rewind", agent)
    out = capsys.readouterr().out
    assert "Rewind points" in out and "describe the repo" in out
    dispatch_command("/rewind to 1 conversation", agent)
    assert agent.conversation.messages == []
    assert dispatch_command("/checkpoint list", agent).handled


# ─── context / cost ────────────────────────────────────────────────────────

def test_context_grid_cells():
    from axon.commands.context_cmds import context_grid
    from axon.ui.theme import strip_ansi
    lines = context_grid([(20_000, ""), (1, "")], 100_000, 80_000, ["legend"])
    glyphs = "".join(strip_ansi(l) for l in lines)
    used = sum(glyphs.count(c) for c in "⛁#")
    buffer = sum(glyphs.count(c) for c in "⛝x")
    assert used == 21 and buffer == 20 and len(lines) == 10 and "legend" in lines[0]


def test_context_command_shows_grid(agent: Agent, capsys):
    agent.run_turn("hi")
    dispatch_command("/context", agent)
    out = capsys.readouterr().out
    assert "Free" in out and "Messages" in out and ("⛁" in out or "#" in out)


def test_prompt_cache_stats_and_cost_line(agent: Agent, capsys):
    from axon.commands.context_cmds import prompt_cache_stats

    def step(cr, inp, cause="", end=0.0):
        return SimpleNamespace(kind="llm", ended_at=end, data={"usage": {"input": inp, "cache_read": cr, "cache_write": 0},
                                                              "cache_miss_cause": cause})
    agent.traces = [SimpleNamespace(agent_name="main", steps=[step(0, 1000, "system prompt changed", 100.0), step(900, 1000, end=200.0)]),
                    SimpleNamespace(agent_name="sub", steps=[step(0, 5000)])]
    st = prompt_cache_stats(agent, now=250.0)
    assert st["calls"] == 2 and st["hits"] == 1 and st["misses"] == 1 and st["warm"]
    assert abs(st["hit_ratio"] - 0.45) < 1e-9 and st["last_miss_cause"] == "system prompt changed"
    assert not prompt_cache_stats(agent, now=1000.0)["warm"]
    agent.ledger.record("deepseek-v4-flash", Usage(input=1000, output=10, cache_read=900))
    dispatch_command("/cost", agent)
    assert "Prompt cache (main)" in capsys.readouterr().out


# ─── copy / export / btw / tasks ───────────────────────────────────────────

def test_copy_nth_answer_and_code(workspace: Path, monkeypatch, capsys):
    a = _make(workspace, ["first answer", "second ```py\nprint(1)\n``` and ```sh\nls\n```"])
    a.run_turn("q1")
    a.run_turn("q2")
    copied = []
    monkeypatch.setattr("axon.ui.clipboard.copy_to_clipboard", lambda t: copied.append(t) or True)
    dispatch_command("/copy 2", a)
    dispatch_command("/copy code", a)
    dispatch_command("/copy 9", a)
    assert copied == ["first answer", "print(1)\n\nls"]
    assert "No answer #9" in capsys.readouterr().out


def test_export_txt_and_clipboard(agent: Agent, monkeypatch, isolate_user_home: Path):
    agent.run_turn("export me")
    copied = []
    monkeypatch.setattr("axon.ui.clipboard.copy_to_clipboard", lambda t: copied.append(t) or True)
    dispatch_command("/export txt clipboard", agent)
    assert copied and "> export me" in copied[0]
    dispatch_command("/export txt", agent)
    out = isolate_user_home / ".axon" / "exports" / f"{agent.session.active_session_id}.txt"
    assert out.exists() and "> export me" in out.read_text(encoding="utf-8")


def test_btw_sends_session_context(agent: Agent, monkeypatch):
    agent.run_turn("we are refactoring the parser")
    seen = {}
    import axon.commands.code_cmds as cc
    monkeypatch.setattr(cc, "one_shot", lambda ag, system, prompt, **kw: seen.setdefault("prompt", prompt) or "ok")
    dispatch_command("/btw what file was that?", agent)
    assert "refactoring the parser" in seen["prompt"] and "what file was that?" in seen["prompt"]
    assert len(agent.conversation.messages) == 2  # the side question is not added


def test_tasks_lists_shells_and_background_turns(agent: Agent, monkeypatch, capsys):
    import threading
    import axon.commands.agent_cmds as ac
    stream = SimpleNamespace(lock=threading.Lock(), buf="building...\nstep 2 done\n")
    shell = SimpleNamespace(id="bash_1", command="npm run dev", status="running", started=time.time() - 5, stream=stream)
    monkeypatch.setattr(ac, "_background_shells", lambda: [shell])
    agent.background_tasks = {"bg1": SimpleNamespace(status="completed", prompt="long refactor", elapsed_s=3.0, log=["did it"])}
    dispatch_command("/tasks", agent)
    out = capsys.readouterr().out
    assert "bash_1" in out and "npm run dev" in out and "step 2 done" in out and "long refactor" in out
    dispatch_command("/bashes log bash_1", agent)
    assert "building..." in capsys.readouterr().out
    dispatch_command("/tasks log bg1", agent)
    assert "did it" in capsys.readouterr().out


# ─── purge / release notes / terminal setup ────────────────────────────────

def test_purge_project_requires_confirmation(workspace: Path, isolate_user_home: Path):
    from axon import storage
    from axon.commands.workspace_cmds import purge
    d = storage.project_dir(workspace)
    (d / "sessions").mkdir(parents=True, exist_ok=True)
    (isolate_user_home / ".axon" / "settings.json").write_text("{}", encoding="utf-8")
    assert purge("project", workspace, confirm_fn=lambda q: False) == [] and d.exists()
    assert purge("project", workspace, confirm_fn=lambda q: True) == [d] and not d.exists()
    assert (isolate_user_home / ".axon" / "settings.json").exists()
    storage.cache_dir("x")
    assert purge("caches", assume_yes=True) == [storage.axon_home() / "cache"]
    with pytest.raises(ValueError):
        purge("everything")


def test_release_notes(agent: Agent, capsys):
    from axon.commands.prefs_cmds import changelog_path, parse_changelog
    import axon
    entries = parse_changelog(changelog_path().read_text(encoding="utf-8"))
    assert entries[0][0] == axon.__version__ and "MCP" in entries[0][1]
    dispatch_command("/release-notes", agent)
    assert f"Axon {axon.__version__}" in capsys.readouterr().out
    dispatch_command("/release-notes 9.9", agent)
    assert "No release notes for 9.9" in capsys.readouterr().out


def test_terminal_setup_detection_and_vscode_binding(tmp_path: Path, agent: Agent, capsys):
    from axon.commands.prefs_cmds import detect_terminal, install_vscode_binding
    assert detect_terminal({"TERM_PROGRAM": "vscode"}) == "vscode"
    assert detect_terminal({"WT_SESSION": "1"}) == "windows-terminal"
    assert detect_terminal({"TERM_PROGRAM": "iTerm.app"}) == "iterm2"
    kb = tmp_path / "keybindings.json"
    assert install_vscode_binding(kb) == "added" and install_vscode_binding(kb) == "present"
    assert json.loads(kb.read_text(encoding="utf-8"))[0]["args"]["text"] == "\x1b\r"
    kb.write_text("// comment\n[]", encoding="utf-8")
    assert install_vscode_binding(kb) == "unparseable" and kb.read_text(encoding="utf-8").startswith("//")
    dispatch_command("/terminal-setup windows-terminal", agent)
    assert "sendInput" in capsys.readouterr().out


# ─── /config, /rename, /agents, /memory ────────────────────────────────────

def test_config_key_value_persists_panel_settings(agent: Agent, isolate_user_home: Path, capsys):
    from axon.commands.system_cmds import set_config_value
    dispatch_command("/config suggestions=off", agent)
    assert agent.settings.suggestions is False
    assert _user_settings(isolate_user_home)["promptSuggestionEnabled"] is False
    dispatch_command("/config trace_level detailed", agent)
    assert _user_settings(isolate_user_home)["traceLevel"] == "detailed"
    set_config_value(agent, "mode", "bypass")
    assert agent.settings.mode == "bypass" and _user_settings(isolate_user_home)["permissions"]["defaultMode"] == "bypassPermissions"
    with pytest.raises(ValueError):
        set_config_value(agent, "effort", "turbo")
    dispatch_command("/config", agent)
    out = capsys.readouterr().out
    assert "output_style" in out and "vim_mode" in out


def test_rename_without_title_generates_one(workspace: Path):
    a = _make(workspace, ["sure", "Parser Refactor Plan"])
    a.run_turn("help me refactor the parser")
    dispatch_command("/rename", a)
    metas = {m.session_id: m for m in a.session.list_recent()}
    assert metas[a.session.active_session_id].first_prompt == "Parser Refactor Plan"


def test_agent_file_creation(agent: Agent, workspace: Path, isolate_user_home: Path):
    from axon.commands.agent_cmds import create_agent_file
    from axon.agent.subagent import load_agent_definitions
    p = create_agent_file(workspace, "Test Runner", "Runs tests", ["Bash", "Read"], "small-model", "project", "Run the tests.")
    assert p == workspace / ".axon" / "agents" / "test-runner.md"
    d = next(x for x in load_agent_definitions(workspace) if x.name == "test-runner")
    assert d.tools == ["Bash", "Read"] and d.model == "small-model" and "Run the tests." in d.system_prompt
    with pytest.raises(FileExistsError):
        create_agent_file(workspace, "test-runner", "x", None, None)
    u = create_agent_file(workspace, "helper", "Helps", None, None, "user")
    assert u == isolate_user_home / ".axon" / "agents" / "helper.md" and "tools:" not in u.read_text(encoding="utf-8")
    dispatch_command("/agents create linter Lints code", agent)
    assert (workspace / ".axon" / "agents" / "linter.md").exists()


def test_editor_argv_uses_platform_editor(monkeypatch):
    from axon.commands.common import editor_argv
    monkeypatch.setenv("VISUAL", "code --wait")
    assert editor_argv() == ["code", "--wait"]


# ─── review / effort / autocompact / aliases / focus ───────────────────────

def test_review_prompt_flags_and_new_review_commands(agent: Agent):
    from axon.commands.code_cmds import build_review_prompt
    p, label = build_review_prompt("high --fix --max-findings 3 auth module")
    assert "fix every finding" in p and "do not modify files" not in p and "at most 3 findings" in p
    assert "Focus: auth module" in p and "high" in label
    p2, _ = build_review_prompt("42 --comment")
    assert "gh pr diff 42" in p2 and "inline review comment" in p2
    dispatch_command("/security-review", agent)
    dispatch_command("/code-review low", agent)
    dispatch_command("/simplify parser", agent)
    users = [m["content"] for m in agent.conversation.messages if m["role"] == "user"]
    assert "security review" in users[0] and "Report only a few findings" in users[1] and "Focus: parser" in users[2]


def test_effort_auto_status_and_autocompact_tokens(agent: Agent, capsys):
    from axon.commands.system_cmds import parse_compact_threshold
    dispatch_command("/effort reflex", agent)
    dispatch_command("/effort status", agent)
    assert "reflex" in capsys.readouterr().out
    dispatch_command("/effort auto", agent)
    assert agent.settings.effort == Settings.model_fields["effort"].default
    assert parse_compact_threshold("500k", 1_000_000) == 0.5
    assert parse_compact_threshold("70%", 1) == 0.7 and parse_compact_threshold("auto", 1) == Settings.model_fields["compact_at"].default
    with pytest.raises(ValueError):
        parse_compact_threshold("5k", 1_000_000)
    dispatch_command("/autocompact 60", agent)
    assert agent.settings.compact_at == 0.6


def test_feedback_alias_and_focus(agent: Agent, isolate_user_home: Path):
    dispatch_command("/feedback tool output was cut off", agent)
    assert list((isolate_user_home / ".axon" / "bug-reports").glob("bug-*.md"))
    dispatch_command("/focus", agent)
    assert agent.settings.trace_level == "compact"
    dispatch_command("/focus off", agent)
    assert agent.settings.trace_level == "normal"


# ─── skills frontmatter / plugins / bypass lock ────────────────────────────

def test_skill_frontmatter_named_args_and_invocation_flags(agent: Agent, workspace: Path):
    from axon.skills.manager import SkillManager, skill_content, skills_prompt_section, _managers
    d = workspace / ".axon" / "skills"
    (d / "deploy").mkdir(parents=True)
    (d / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy the app\ndisable-model-invocation: true\n"
                                           "arguments: [env, version]\n---\nDeploy $version to ${env}.\n", encoding="utf-8")
    (d / "helper").mkdir()
    (d / "helper" / "SKILL.md").write_text("---\nname: helper\ndescription: Background helper\nuser-invocable: false\n"
                                           "when_to_use: when the build breaks\npaths: ['src/**']\n---\nHelp.\n", encoding="utf-8")
    _managers.clear()
    mgr = SkillManager(workspace)
    agent.skills = mgr
    dep, hlp = mgr.get("deploy"), mgr.get("helper")
    assert dep.disable_model_invocation and dep.arguments == ["env", "version"] and hlp.user_invocable is False
    assert "Deploy 1.2.3 to staging." in mgr.execute_skill("deploy", "staging 1.2.3")
    section = skills_prompt_section([dep, hlp])
    assert "deploy" not in section and "Use when: when the build breaks" in section and "src/**" in section
    assert "only be run by the user" in skill_content("deploy", workspace)
    names = dict(list_command_names(workspace, agent))
    assert "/deploy" in names and "/helper" not in names


def test_enabled_plugins_setting(workspace: Path):
    from axon.commands.plugins import load_plugins
    root = workspace / ".axon" / "plugins" / "p1"
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"name": "p1"}), encoding="utf-8")
    assert load_plugins(workspace)[0].enabled
    (workspace / ".axon" / "settings.json").write_text(json.dumps({"enabledPlugins": {"p1@market": False}}), encoding="utf-8")
    assert not load_plugins(workspace)[0].enabled


def test_disable_bypass_permissions_mode(workspace: Path):
    (workspace / ".axon").mkdir(exist_ok=True)
    (workspace / ".axon" / "settings.json").write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}), encoding="utf-8")
    eng = PermissionEngine(Settings(workspace=workspace, mode="bypass"))
    bash = SimpleNamespace(name="Bash", readonly=False, default_permission="ask")
    assert eng.check(bash, {"command": "make"}).outcome == "ask"


# ─── insight: prompt audit / import / skill doctor / debug ────────────────

def test_prompt_audit_finds_conflicts_duplicates_and_stale_paths(agent: Agent, workspace: Path):
    from axon.commands.insight_cmds import prompt_audit
    (workspace / "src").mkdir()
    (workspace / "src" / "real.py").write_text("x", encoding="utf-8")
    srcs = [("AXON.md", "- Always use tabs for indentation.\n- See `src/real.py` and `src/gone.py`.\n- Run the full test suite before you commit.\n"),
            ("rules/style.md", "- Never use tabs for indentation.\n- Run the full test suite before you commit.\n")]
    kinds = {(f.kind, f.message.split('"')[1] if '"' in f.message else f.message) for f in prompt_audit(agent, srcs)}
    assert ("conflict", "use tabs for indentation") in kinds
    assert any(k == "duplicate" for k, _ in kinds)
    stale = [m for k, m in kinds if k == "stale-path"]
    assert len(stale) == 1 and "src/gone.py" in stale[0]


def test_doctor_prompt_audit_command(agent: Agent, workspace: Path, capsys):
    (workspace / "AXON.md").write_text("Always commit often.\nNever commit often.\n", encoding="utf-8")
    dispatch_command("/doctor prompt-audit", agent)
    out = capsys.readouterr().out
    assert "Prompt audit" in out and "conflict" in out


def test_import_cursor_and_codex(agent: Agent, workspace: Path, isolate_user_home: Path, monkeypatch):
    from axon.commands.insight_cmds import apply_import, import_plan
    (workspace / ".cursorrules").write_text("Use 4 spaces.", encoding="utf-8")
    rd = workspace / ".cursor" / "rules"
    rd.mkdir(parents=True)
    (rd / "react.mdc").write_text("---\ndescription: React\nglobs: src/**/*.tsx\nalwaysApply: false\n---\nUse hooks.\n", encoding="utf-8")
    codex = isolate_user_home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('[mcp_servers.docs]\ncommand = "npx"\nargs = ["-y", "docs-mcp"]\n', encoding="utf-8")
    monkeypatch.setattr("axon.mcp.config.Path.home", classmethod(lambda cls: isolate_user_home), raising=False)
    plan = import_plan(workspace, "all")
    assert any(i.target and i.target.name == "cursor-react.md" for i in plan) and any(i.mcp and i.mcp[0] == "docs" for i in plan)
    dispatch_command("/import cursor --dry-run", agent)
    assert not (workspace / ".axon" / "rules" / "cursor.md").exists()
    notes = apply_import(workspace, plan)
    rule = (workspace / ".axon" / "rules" / "cursor-react.md").read_text(encoding="utf-8")
    assert rule.startswith("---\npaths:") and "src/**/*.tsx" in rule and "Use hooks." in rule
    assert (workspace / ".axon" / "rules" / "cursor.md").exists()
    assert any("added MCP server docs" in n for n in notes)
    assert all("skipped" in n for n in apply_import(workspace, plan))  # never overwrites


def test_skill_doctor_and_debug_prompt(agent: Agent, workspace: Path, capsys):
    from axon.commands.insight_cmds import debug_prompt, skill_report
    agent.run_turn("hello")
    agent.traces[-1].steps.append(SimpleNamespace(kind="tool", data={"name": "Skill", "input": {"skill": "code-review"}}))
    rows = {r["name"]: r for r in skill_report(agent)}
    assert rows["code-review"]["uses"] == 1 and rows["code-review"]["body_tokens"] > 0
    dispatch_command("/skill-doctor", agent)
    out = capsys.readouterr().out
    assert "Skill doctor" in out and "code-review" in out
    p = debug_prompt(agent, "it ignored my rule")
    assert "it ignored my rule" in p and "Trace of the last turn" in p


def test_changed_files_lists_tracked_and_untracked(workspace: Path):
    import shutil as _sh
    import subprocess
    from axon.commands.code_cmds import changed_files
    if not _sh.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i"], cwd=workspace, check=True)
    (workspace / "t.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "t.txt"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "t"], cwd=workspace, check=True)
    (workspace / "t.txt").write_text("a\nb\n", encoding="utf-8")
    (workspace / "new.txt").write_text("n", encoding="utf-8")
    rows = dict(changed_files(workspace, ["diff"]))
    assert rows["t.txt"] == "+1 -0" and rows["new.txt"] == "untracked"


def test_keybindings_lists_effective_bindings(agent: Agent, capsys):
    dispatch_command("/keybindings", agent)
    out = capsys.readouterr().out
    assert "Keyboard shortcuts" in out
    try:
        from axon.ui.keybindings import effective_keybindings
    except ImportError:
        return
    first = sorted(effective_keybindings())[0]
    assert first in out


def test_rewind_menu_uses_ui_picker(agent: Agent, monkeypatch, capsys):
    import axon.commands.session_cmds as sc
    import axon.ui.picker as picker
    agent.run_turn("menu prompt")
    monkeypatch.setattr(sc, "is_tty", lambda: True)
    monkeypatch.setattr(picker, "run_rewind_menu", lambda pts: (pts[0], "conversation"))
    dispatch_command("/rewind", agent)
    assert agent.conversation.messages == []
    monkeypatch.setattr(picker, "run_rewind_menu", lambda pts: None)
    agent.run_turn("again")
    dispatch_command("/rewind", agent)
    assert "cancelled" in capsys.readouterr().out and agent.conversation.messages
