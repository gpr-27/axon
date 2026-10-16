"""Settings precedence/security, CLI parsing, print mode and session handling."""
from __future__ import annotations
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from axon import cli
from axon.config import ConfigError, Settings, decode_config_bytes
from axon.agent.prompt import build_system, detect_shell
from axon.tools import create_default_registry
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AXON_API_KEY", "AXON_BASE_URL", "AXON_MODEL", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
              "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def _w(p: Path, text: str | bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")


def test_env_var_beats_toml(workspace: Path, isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _w(isolate_user_home / ".axon" / "config.toml", 'model = "from-toml"\napi_key = "k-user"\n')
    monkeypatch.setenv("AXON_MODEL", "from-env")
    s = Settings.load({"workspace": workspace})
    assert s.model == "from-env"
    assert Settings.load({"workspace": workspace, "model": "from-cli"}).model == "from-cli"


def test_project_base_url_cannot_redirect_user_key(workspace: Path, isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_API_KEY", "user-secret")
    _w(workspace / ".axon" / "config.toml", 'base_url = "https://evil.example.com"\n')
    s = Settings.load({"workspace": workspace})
    assert "evil" not in s.base_url
    _w(workspace / ".axon" / "config.toml", 'base_url = "http://localhost:11434/v1"\n')
    assert Settings.load({"workspace": workspace}).base_url.startswith("http://localhost")


def test_parent_dir_env_files_are_not_read(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _w(workspace.parent / ".env", "AXON_API_KEY=parent-key\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(ConfigError):
        Settings.load({"workspace": workspace})
    _w(workspace / ".env", "AXON_API_KEY=ws-key\n")
    assert Settings.load({"workspace": workspace}).api_key.get_secret_value() == "ws-key"
    monkeypatch.delenv("AXON_API_KEY", raising=False)


def test_bom_and_utf16_config_files(workspace: Path, isolate_user_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _w(isolate_user_home / ".axon" / ".env", "﻿AXON_API_KEY=bom-key\n".encode("utf-8"))
    assert Settings.load({"workspace": workspace}).api_key.get_secret_value() == "bom-key"
    monkeypatch.delenv("AXON_API_KEY", raising=False)
    _w(isolate_user_home / ".axon" / "config.toml", 'model = "utf16-model"\napi_key = "k"\n'.encode("utf-16"))
    assert Settings.load({"workspace": workspace}).model == "utf16-model"
    assert decode_config_bytes(b"\xef\xbb\xbfa=1") == "a=1"


def test_project_permissions_merge_even_without_global(workspace: Path, isolate_user_home: Path) -> None:
    _w(workspace / ".axon" / "config.toml", '[permissions]\nallow = ["Bash(git *)"]\n')
    _w(workspace / ".axon" / "settings.json", json.dumps({"permissions": {"deny": ["Bash(rm *)"], "defaultMode": "acceptEdits"}}))
    s = Settings.load({"workspace": workspace, "api_key": "k"})
    assert "Bash(git *)" in s.permissions.allow and "Bash(rm *)" in s.permissions.deny
    assert s.mode == "acceptEdits"


def test_settings_json_cannot_set_base_url_from_project(workspace: Path) -> None:
    _w(workspace / ".axon" / "settings.local.json", json.dumps({"baseUrl": "https://evil.example.com", "model": "m-local"}))
    s = Settings.load({"workspace": workspace, "api_key": "k"})
    assert "evil" not in s.base_url and s.model == "m-local"


def test_dangerously_skip_permissions_forces_bypass(workspace: Path) -> None:
    s = Settings.load({"workspace": workspace, "api_key": "k", "mode": "default", "dangerously_skip_permissions": True})
    assert s.mode == "bypass"
    assert Settings.load({"workspace": workspace, "api_key": "k", "mode": "plan"}).mode == "plan"


def test_budget_table_in_toml(workspace: Path, isolate_user_home: Path) -> None:
    _w(isolate_user_home / ".axon" / "config.toml", 'api_key = "k"\n[budget]\nturn_usd = 0.5\nsession_usd = 3\naction = "warn"\n')
    s = Settings.load({"workspace": workspace})
    assert s.budget_turn_usd == 0.5 and s.budget_session_usd == 3 and s.budget_action == "warn"


def test_parser_flags() -> None:
    args = cli.build_parser().parse_args([
        "-p", "fix", "it", "--provider", "openai", "--base-url", "http://x", "--max-turns", "7",
        "--allowedTools", "Read,Bash(git log *)", "--disallowedTools", "WebFetch", "--append-system-prompt", "Be terse",
        "--output-format", "stream-json", "--verbose", "--trace", "debug", "--max-budget-usd", "2", "-r", "abc", "--fork-session",
    ])
    ov = cli._overrides_from_args(args)
    assert " ".join(args.prompt) == "fix it" and ov["provider"] == "openai" and ov["max_iterations"] == 7
    assert ov["trace_level"] == "debug" and ov["budget_session_usd"] == 2 and ov["verbose"] is True
    assert cli._split_tool_list(args.allowed_tools) == ["Read", "Bash(git log *)"]
    assert args.resume_id == "abc" and args.fork_session
    assert cli.build_parser().parse_args(["-r"]).resume_id == ""


def test_main_config_error_is_friendly(workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = cli.main(["--workspace", str(workspace), "-p", "hi"])
    err = capsys.readouterr().err
    assert code == 2 and "Missing API key" in err and "Traceback" not in err


def test_read_piped_stdin_ignores_tty_and_reads_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    class TTY(io.StringIO):
        def isatty(self) -> bool:
            return True
    monkeypatch.setattr(sys, "stdin", TTY("should not read"))
    assert cli.read_piped_stdin() == ""


def test_print_mode_json_and_stream_json(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = make_agent(FakeProvider(scripted("the answer")), workspace)
    assert cli.run_print_mode(agent, "question", "json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["result"] == "the answer" and data["is_error"] is False and "session_id" in data
    emitter = cli._StreamJsonEmitter(io.StringIO())
    from axon.providers.base import TextDelta
    emitter.on_event(TextDelta("hi"))
    assert emitter.out.getvalue().strip()


def test_process_line_shell_memory_and_queue(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(cli, "run_shell_shortcut", lambda c, sink=None: ran.append(c) or (sink.append("hi\n") if sink is not None else None) or 0)
    prov = FakeProvider(scripted("a1", "a2"))
    agent = make_agent(prov, workspace, respond_to_bash_commands=False)   # shell context only, no reply turn

    class R:
        def turn_footer(self, **kw: Any) -> None:
            pass
    assert cli.process_line(agent, R(), "!echo hi") is True and ran == ["echo hi"]
    assert cli.process_line(agent, R(), "# use tabs") is True
    from axon.agent.memory import MemoryStore
    assert any("use tabs" in m.content for m in MemoryStore(workspace).list_all())
    assert cli.process_line(agent, R(), "first ;; second") is True
    assert len(agent.message_queue) == 1
    assert cli.drain_queue(agent, R()) is True
    assert len(prov.requests) == 2


def test_file_mentions_expand(workspace: Path) -> None:
    (workspace / "notes.txt").write_text("hello notes", encoding="utf-8")
    out = cli.expand_file_mentions("see @notes.txt and email a@b.com", workspace)
    assert "hello notes" in out and "a@b.com" in out


def test_find_and_fork_session(workspace: Path) -> None:
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    sid = agent.session.open(None)
    agent.run_turn("unique first prompt")
    found, _ = cli.find_session(agent, sid)
    assert found == sid
    new_id = cli.fork_session(agent, lambda m: None)
    assert new_id != sid and (agent.session.session_dir / f"{new_id}.jsonl").is_file()
    assert cli.find_session(agent, "zzz-no-such-session")[0] is None


def test_system_prompt_env_and_stable_order(workspace: Path) -> None:
    settings = make_settings(workspace)
    reg = create_default_registry()
    a = build_system(settings, reg, git_status="")
    b = build_system(settings, reg, git_status="")
    assert a == b
    env = a[3]["text"]
    assert "Operating system:" in env and f"Shell: {detect_shell()}" in env and "Date:" in env
    assert "Permission mode: bypass" in env
    joined = "\n".join(x["text"] for x in a)
    assert "Subagents" in joined and "explore" in joined


def test_doctor_runs_without_api_key(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["doctor", "--no-agent"])
    out = capsys.readouterr().out
    assert "Axon doctor" in out and "API key" in out
    assert code in (0, 1)


def test_claude_code_settings_never_switch_axons_model(tmp_path, monkeypatch) -> None:
    """Opened in the home folder, ~/.claude/settings.json is Claude Code's own config ("model": "opus");
    it must not override the model in ~/.axon/config.toml (it silently moved a DeepSeek user onto Opus)."""
    import json
    from axon.config import Settings
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "effortLevel": "low"}), encoding="utf-8")
    axon_home = home / ".axon"
    axon_home.mkdir()
    (axon_home / "config.toml").write_text('model = "deepseek-v4-flash"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXON_HOME", str(axon_home))
    for var in ("AXON_MODEL", "AXON_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.load({"workspace": str(home)}, require_api_key=False)
    assert s.model == "deepseek-v4-flash"
    # a real project's .claude/settings.json still contributes permissions, but not the model
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet", "permissions": {"allow": ["Bash(git status)"]}}), encoding="utf-8")
    s2 = Settings.load({"workspace": str(proj)}, require_api_key=False)
    assert s2.model == "deepseek-v4-flash"
    assert "Bash(git status)" in (s2.permissions.allow if hasattr(s2.permissions, "allow") else s2.permissions["allow"])


def test_context_limit_defaults_to_128k_and_caps_the_model_window(tmp_path) -> None:
    from types import SimpleNamespace
    from axon.config import Settings
    from axon.agent.context import ContextManager
    from axon.providers.registry import effective_context_window, parse_token_count
    s = Settings(workspace=tmp_path, model="deepseek-v4-flash")
    assert s.context_limit == 128_000
    ctx = ContextManager(s)
    assert ctx.context_window() == 128_000                       # 1M model, 128k used by default
    assert 0.70 <= ctx.threshold() / ctx.context_window() <= 0.80  # auto-compact near 75%
    assert ContextManager(s.model_copy(update={"context_limit": 256_000})).context_window() == 256_000
    assert ContextManager(s.model_copy(update={"context_limit": 0})).context_window() == 1_000_000
    assert effective_context_window("gpt-4o", SimpleNamespace(context_limit=1_000_000)) == 128_000  # never above the model
    assert [parse_token_count(x) for x in ("128k", "256K", "1m", "max")] == [128_000, 256_000, 1_000_000, 0]
