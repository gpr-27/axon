"""Cross-area contracts: read_input prefill + /loop wake, failed-turn footer, mode changes via
agent.set_mode, /rename -> mark_titled, /tasks background-turn log."""
from __future__ import annotations

import contextlib
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.agent.loop import BackgroundTurn
from axon.commands.workspace_cmds import LOOP_WAKE
from axon.providers.base import Usage
from axon.ui import input as ui_input
from axon.ui.input import read_input
from axon.ui.render import Renderer
from axon.ui.theme import strip_ansi

from tests.fakes import FakeProvider
from tests.test_core_helpers import make_agent


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ui_input, "_SESSION_HISTORY", [])
    monkeypatch.setattr(ui_input, "registered_slash_commands", lambda cwd=None: [])
    LOOP_WAKE.clear()
    yield
    LOOP_WAKE.clear()


@pytest.fixture
def keys(monkeypatch):
    def install(seq, on_none=None):
        q = deque(seq)

        def fake_read_key(timeout=None, report_resize=True):
            if not q:
                raise AssertionError("ran out of scripted keys")
            k = q.popleft()
            if k is None and on_none:
                on_none()
            return k
        monkeypatch.setattr(plat, "read_key", fake_read_key)
        monkeypatch.setattr(plat, "is_interactive", lambda: True)
        monkeypatch.setattr(plat, "raw_terminal", lambda *a, **k: contextlib.nullcontext(True))
        monkeypatch.setattr(plat, "setup_console", lambda: None)
        monkeypatch.setattr(plat, "term_size", lambda: (100, 30))
    return install


def test_read_input_initial_prefills_editor(keys, tmp_path):
    keys(["!", "enter"])
    assert read_input(initial="queued prompt", cwd=str(tmp_path))[0] == "queued prompt!"


def test_loop_wake_returns_empty_line_while_idle(keys, tmp_path):
    keys([None, None], on_none=LOOP_WAKE.set)
    assert read_input(cwd=str(tmp_path))[0] == ""
    assert not LOOP_WAKE.is_set()


def test_loop_wake_does_not_discard_a_draft(keys, tmp_path):
    keys(["h", "i", None, "enter"], on_none=LOOP_WAKE.set)
    assert read_input(cwd=str(tmp_path))[0] == "hi"
    assert LOOP_WAKE.is_set()  # still pending: the REPL drains the queue after this prompt


def test_renderer_footer_marks_failed_turns(capsys):
    r = Renderer()
    r.turn_footer(0, Usage(input=10, output=2), 0.0, 1.2, 1, stop_reason="error", error="401 unauthorized")
    out = strip_ansi(capsys.readouterr().out)
    assert "✗ Failed" in out and "401 unauthorized" in out and "Done" not in out
    r.turn_footer(0, Usage(), 0.0, 1.0, 1, stop_reason="end_turn")
    out = strip_ansi(capsys.readouterr().out)
    assert "✓ Done" in out and "✗" not in out


def test_cli_footer_does_not_duplicate_the_failure(workspace: Path, capsys):
    from axon import cli
    from axon.agent.loop import TurnResult
    agent = make_agent(FakeProvider([]), workspace)
    res = TurnResult(final_text="Provider error", stop_reason="error", iterations=1, tool_calls_count=0,
                     usage=Usage(), error="boom")
    assert cli.render_turn_footer(Renderer(), agent, res, 0.3) is True
    out = strip_ansi(capsys.readouterr().out)
    assert out.count("✗") == 1 and "boom" in out


def test_mode_commands_go_through_set_mode(workspace: Path):
    from axon.commands import dispatch_command
    agent = make_agent(FakeProvider([]), workspace)
    seen: list[str] = []
    real = agent.set_mode
    agent.set_mode = lambda m: (seen.append(m), real(m))[1]  # type: ignore[method-assign]
    dispatch_command("/mode plan", agent)
    dispatch_command("/plan off", agent)
    dispatch_command("/mode acceptEdits", agent)
    assert seen == ["plan", "default", "acceptEdits"]
    assert agent.settings.mode == "acceptEdits" and agent.permissions.settings.mode == "acceptEdits"


def test_rename_marks_session_titled(workspace: Path):
    from axon.commands import dispatch_command
    agent = make_agent(FakeProvider([]), workspace)
    dispatch_command("/rename  My   session ", agent)
    assert agent.session_title == "My session"
    assert agent._titled is True


def test_tasks_log_shows_background_turn_result(workspace: Path, capsys):
    from axon.commands import dispatch_command
    agent = make_agent(FakeProvider([]), workspace)
    rec = BackgroundTurn(id="bg1", prompt="do it", started_at=0.0, status="completed", ended_at=2.0,
                         result=SimpleNamespace(final_text="all finished"))
    rec.add_log("Read README.md")
    agent.background_tasks["bg1"] = rec
    dispatch_command("/tasks", agent)
    out = strip_ansi(capsys.readouterr().out)
    assert "bg1" in out and "completed" in out and "do it" in out
    dispatch_command("/tasks log bg1", agent)
    out = strip_ansi(capsys.readouterr().out)
    assert "Read README.md" in out and "all finished" in out


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_shell_substitutions_and_credential_paths_are_guarded(workspace: Path, tmp_path: Path, monkeypatch):
    from axon.config import Settings
    from axon.permissions.engine import PermissionEngine
    from axon.permissions.rules import all_subcommands
    from axon.tools import create_default_registry
    monkeypatch.setenv("HOME", str(tmp_path))
    assert all_subcommands("echo $(rm -rf v) `ls x` $((1+2)) '$(no)' diff <(cat a) b")[1:] == ["rm -rf v", "ls x", "cat a"]
    eng = PermissionEngine(Settings(workspace=workspace, api_key="k", permissions={"deny": ["Bash(rm:*)"]}))
    bash = create_default_registry().get("Bash")
    def outcome(cmd: str) -> str:
        return eng.check(bash, {"command": cmd}, "bypass").outcome
    assert outcome("echo $(rm -rf victim)") == "deny"
    assert outcome("cat ~/.ssh/id_rsa") == "deny"
    assert outcome("cat < $HOME/.aws/credentials") == "deny"
    assert outcome("echo $(cat ~/.ssh/k)") == "deny"
    assert outcome("ls -la src/ && echo hi > out.txt") == "allow"


def test_offline_mode_fails_web_tools_fast(workspace: Path, monkeypatch):
    from axon.errors import ToolError
    from axon.tools import _http, create_default_registry
    monkeypatch.setenv("AXON_OFFLINE", "1")
    monkeypatch.setattr(_http, "_TRANSPORT", None)
    def boom(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(_http.httpx, "Client", boom)
    reg = create_default_registry()
    for name, args in (("WebFetch", {"url": "https://example.org"}), ("WebSearch", {"query": "axon"})):
        with pytest.raises(ToolError, match="offline mode"):
            reg.get(name).run(args, SimpleNamespace(workspace=workspace, settings=None))


def test_add_dir_takes_one_value_per_flag():
    from axon import cli
    a = cli.build_parser().parse_args(["--add-dir", "A", "--add-dir", "B", "do the thing"])
    assert a.add_dirs == ["A", "B"] and a.prompt == ["do the thing"]


def test_max_retries_setting_and_command_success_flag(workspace: Path):
    from axon.commands.registry import _run_handler
    from axon.config import Settings
    from axon.providers.transport import max_retries_for
    assert max_retries_for(Settings(workspace=workspace, api_key="k")) == 3
    assert max_retries_for(Settings(workspace=workspace, api_key="k", max_retries=7)) == 7
    assert _run_handler(lambda a, t: False, None, "", "/x").success is False
    assert _run_handler(lambda a, t: None, None, "", "/x").success is True
    assert _run_handler(lambda a, t: 1 / 0, None, "", "/x").success is False


def test_none_cli_overrides_do_not_block_env(workspace: Path, monkeypatch, tmp_path: Path):
    from axon.config import Settings
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text('model = "gpt-4o-mini"\n', encoding="utf-8")
    monkeypatch.setenv("AXON_HOME", str(home))
    monkeypatch.setenv("AXON_MODEL", "gpt-4o")
    s = Settings.load({"workspace": workspace, "model": None, "base_url": None}, require_api_key=False)
    assert s.model == "gpt-4o"
