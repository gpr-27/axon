"""Parity round (P-CORE): CLI flags, footer/suggestions, resume, doctor, debug log, worktree, mentions."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from axon import cli
from axon.agent.loop import TurnResult
from axon.config import ConfigError, Settings
from axon.providers.base import Usage
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


def test_new_flags_parse() -> None:
    a = cli.build_parser().parse_args([
        "--add-dir", "x", "--add-dir", "y", "--system-prompt", "S", "--agent", "explore", "--agents", "{}", "--tools", "Bash,Read",
        "--mcp-config", "m.json", "--strict-mcp-config", "--fallback-model", "a,b", "--debug", "api,hooks",
        "-w", "feat", "--permission-mode", "bypassPermissions", "hello",
    ])
    assert a.add_dirs == ["x", "y"] and a.system_prompt == "S" and a.agent_name == "explore" and a.tools == "Bash,Read"
    assert a.mcp_config == ["m.json"] and a.strict_mcp_config and a.debug == "api,hooks" and a.worktree == "feat"
    assert a.mode == "bypass" and cli._overrides_from_args(a)["fallback_model"] == "a,b"
    b = cli.build_parser().parse_args(["--debug", "-w", "--mode", "manual"])
    assert b.debug == "all" and b.worktree == "" and b.mode == "default"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--mode", "yolo"])


def test_apply_prompt_flags(workspace: Path, tmp_path: Path) -> None:
    sp = tmp_path / "sys.txt"
    sp.write_text("Custom core prompt", encoding="utf-8")
    ap = tmp_path / "app.txt"
    ap.write_text("From file", encoding="utf-8")
    extra = tmp_path / "lib"
    extra.mkdir()
    args = cli.build_parser().parse_args(["--system-prompt-file", str(sp), "--append-system-prompt-file", str(ap),
                                          "--append-system-prompt", "From flag", "--add-dir", str(extra)])
    s = cli._apply_prompt_flags(make_settings(workspace, append_system_prompt="From flag"), args)
    assert s.system_prompt_override == "Custom core prompt"
    assert s.append_system_prompt == "From file\n\nFrom flag" and s.additional_dirs == [extra.resolve()]
    bad = cli.build_parser().parse_args(["--add-dir", str(tmp_path / "nope")])
    with pytest.raises(ConfigError):
        cli._apply_prompt_flags(make_settings(workspace), bad)


def test_agent_flag_sets_prompt_model_and_tools(workspace: Path) -> None:
    d = workspace / ".axon" / "agents"
    d.mkdir(parents=True)
    (d / "auditor.md").write_text("---\nname: auditor\ndescription: audits\ntools: Read, Grep\nmodel: gpt-4o\nmaxTurns: 9\n---\n"
                                  "You audit code.\n", encoding="utf-8")
    s, ad = cli._apply_agent_flag(make_settings(workspace), "auditor")
    assert s.model == "gpt-4o" and s.max_iterations == 9
    agent = cli.build_agent(s, provider=FakeProvider([]), connect_mcp=False, agent_def=ad)
    assert set(agent.registry.visible_names()) == {"Read", "Grep", "TodoWrite"}
    assert "You audit code." in "\n".join(b["text"] for b in agent.system_blocks())
    with pytest.raises(ConfigError, match="unknown agent"):
        cli._apply_agent_flag(make_settings(workspace), "ghost")


def test_tools_flag_restricts_registry(workspace: Path) -> None:
    agent = cli.build_agent(make_settings(workspace), provider=FakeProvider([]), connect_mcp=False, tools_spec="Read,Bash")
    assert set(agent.registry.visible_names()) == {"Read", "Bash"}


def test_mcp_config_parsing(workspace: Path) -> None:
    f = workspace / "servers.json"
    f.write_text(json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["-y", "srv"]}}}), encoding="utf-8")
    specs = cli._mcp_specs_from_args(["servers.json", '{"mcpServers": {"web": {"type": "http", "url": "http://x/mcp"}}}'], workspace)
    assert set(specs) == {"files", "web"} and specs["files"].command == "npx" and specs["web"].url == "http://x/mcp"
    with pytest.raises(ConfigError):
        cli._mcp_specs_from_args(["{not json"], workspace)


def _res(stop: str, text: str = "", error: str | None = None) -> TurnResult:
    return TurnResult(final_text=text, stop_reason=stop, iterations=1, tool_calls_count=0, usage=Usage(), error=error)


def test_failed_footer_never_says_done(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = make_agent(FakeProvider([]), workspace)
    calls: list[dict[str, Any]] = []

    class Old:
        def turn_footer(self, tool_count: int, usage: Any, cost: float, elapsed: float, llm_calls: int = 1) -> None:
            calls.append({"old": True})

    class New:
        def turn_footer(self, tool_count: int, usage: Any, cost: float, elapsed: float, llm_calls: int = 1,
                        stop_reason: str = "", error: str = "") -> None:
            calls.append({"stop": stop_reason, "error": error})

    cli.render_turn_footer(Old(), agent, _res("error", "Provider error: 401", "401 unauthorized"), 0.5)
    out = capsys.readouterr().out
    assert "✗ Failed" in out and "401" in out and calls == []
    cli.render_turn_footer(New(), agent, _res("error", "x", "boom"), 0.5)
    assert calls == [{"stop": "error", "error": "boom"}]
    cli.render_turn_footer(Old(), agent, _res("end_turn", "fine"), 0.5)
    assert calls[-1] == {"old": True}


def test_suggestions_after_footer(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = make_agent(FakeProvider(scripted("hi")), workspace)
    res = agent.run_turn("hello")
    line = cli.show_suggestions(agent, res)
    assert "/init" in line and "/init" in capsys.readouterr().out
    agent.settings = agent.settings.model_copy(update={"suggestions": False})
    assert cli.show_suggestions(agent, res) == ""


def test_resume_restores_mode_and_recaps(workspace: Path) -> None:
    a1 = make_agent(FakeProvider(scripted("the first answer")), workspace)
    a1.start_session()
    a1.set_mode("plan")
    a1.run_turn("plan the refactor")
    sid = a1.session.active_session_id
    a2 = make_agent(FakeProvider([]), workspace, mode="bypass")
    lines: list[str] = []
    assert cli._resume(a2, sid, lines.append)
    assert a2.settings.mode == "plan" and a2.permissions.settings.mode == "plan"
    joined = "\n".join(lines)
    assert "plan the refactor" in joined and "the first answer" in joined and "Permission mode restored: plan" in joined
    a3 = make_agent(FakeProvider([]), workspace, mode="bypass")
    assert cli._resume(a3, sid, lines.append, restore_mode=False) and a3.settings.mode == "bypass"
    assert a3.checkpoints.session_id == sid


def test_shell_output_goes_into_context(workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prov = FakeProvider(scripted("I see the files"))
    agent = make_agent(prov, workspace)

    class R:
        def turn_footer(self, **kw: Any) -> None:
            pass

    assert cli.process_line(agent, R(), "!echo hello-from-shell") is True
    assert "hello-from-shell" in capsys.readouterr().out
    assert prov.requests == []  # respond_to_bash_commands is off: no model call yet
    cli.process_line(agent, R(), "what did it print?")
    first = prov.requests[0]["messages"][0]["content"]
    assert "<bash-input>echo hello-from-shell</bash-input>" in first and "hello-from-shell" in first


def test_shell_respond_runs_a_turn(workspace: Path) -> None:
    prov = FakeProvider(scripted("noted"))
    agent = make_agent(prov, workspace, respond_to_bash_commands=True)

    class R:
        def turn_footer(self, **kw: Any) -> None:
            pass

    cli.process_line(agent, R(), "!echo hi")
    assert len(prov.requests) == 1 and "<bash-stdout>hi" in prov.requests[0]["messages"][0]["content"]


def test_busy_agent_queues_prompts(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = make_agent(FakeProvider([]), workspace)

    class Busy:
        def is_alive(self) -> bool:
            return True

    agent._turn_thread = Busy()  # type: ignore[assignment]
    assert agent.busy
    assert cli.process_line(agent, object(), "next question") is True
    assert [it.text for it in agent.message_queue.items] == ["next question"]
    assert cli.drain_queue(agent, object()) is True and len(agent.message_queue) == 1
    agent._turn_thread = None


def test_doctor_single_report(capsys: pytest.CaptureFixture[str], isolate_user_home: Path) -> None:
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert out.count("Axon doctor") == 1 and "Axon System Diagnostics" not in out
    assert out.count("Python Version") == 1 and "User config" in out and "Hooks" in out
    assert code in (0, 1)


def test_debug_logging_writes_file(workspace: Path, tmp_path: Path) -> None:
    import logging
    path = cli.setup_debug_logging("hooks", str(tmp_path / "dbg.log"), "sid")
    try:
        logging.getLogger("axon.hooks").debug("hook-line")
        logging.getLogger("axon.mcp").debug("mcp-line")
        for h in logging.getLogger("axon").handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert "hook-line" in text and "mcp-line" not in text
    finally:
        root = logging.getLogger("axon")
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        root.setLevel(logging.WARNING)
    assert cli.setup_debug_logging(None, None) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_session_worktree_created_and_reused(tmp_path: Path, isolate_user_home: Path) -> None:
    from axon.agent.worktree import create_session_worktree
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, env={**os.environ, **env}, check=True)  # noqa: E731
    run("init", "-q")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")
    info = create_session_worktree(repo, "feat one")
    assert info is not None and info.created and (info.worktree_path / "f.txt").is_file()
    assert info.branch_name == "axon/feat-one"
    again = create_session_worktree(repo, "feat one")
    assert again is not None and not again.created and again.worktree_path == info.worktree_path
    assert create_session_worktree(tmp_path / "not-a-repo-dir", "x") is None


def test_mentions_dirs_size_limit_and_disable(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "mod.py").write_text("x = 1", encoding="utf-8")
    (workspace / "big.txt").write_text("y" * (cli.MENTION_MAX_BYTES + 10), encoding="utf-8")
    out = cli.expand_file_mentions("see @pkg and @big.txt", workspace)
    assert "[Directory: pkg]" in out and "mod.py" in out and "@big.txt" in out
    monkeypatch.setenv("AXON_DISABLE_FILE_MENTIONS", "1")
    assert cli.expand_file_mentions("see @pkg", workspace) == "see @pkg"


def test_print_mode_failure_footer_and_auto_title(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = make_agent(FakeProvider(scripted("answer")), workspace)

    class R:
        def __init__(self) -> None:
            self.kw: dict[str, Any] = {}

        def turn_footer(self, **kw: Any) -> None:
            self.kw = kw

    r = R()
    assert cli.run_print_mode(agent, "Explain the cache layer", "text", r) == 0
    assert "stop_reason" in r.kw or r.kw.get("tool_count") == 0
    assert agent._has_title()


def test_fallback_model_reaches_provider(workspace: Path) -> None:
    from axon.providers.failover import FailoverProvider
    from axon.providers.registry import provider_for
    s = make_settings(workspace, fallback_model="gpt-4o,claude-sonnet-4-5", base_url="https://openrouter.ai/api/v1")
    p = provider_for(s.model, s)
    assert isinstance(p, FailoverProvider) and p.chain[:1] == ["gpt-4o"]


def test_purge_subcommand_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.commands.workspace_cmds as wc
    seen: list[list[str]] = []
    monkeypatch.setattr(wc, "purge_main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["purge", "--dry-run"]) == 0 and seen == [["--dry-run"]]


def test_repl_prefills_pending_input(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import axon.ui.input as ui_input
    agent = make_agent(FakeProvider([]), workspace)
    agent.pending_input = "restored prompt"
    seen: list[str] = []

    def fake_read_input(**kw: Any) -> Any:
        seen.append(kw.get("initial", ""))
        raise EOFError

    monkeypatch.setattr(ui_input, "read_input", fake_read_input)

    class R:
        def print_banner(self, **kw: Any) -> None:
            pass

    assert cli.run_repl(agent, R()) == 0
    assert seen == ["restored prompt"] and agent.pending_input is None
