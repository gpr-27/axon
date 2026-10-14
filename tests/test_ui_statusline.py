"""Custom status-line command (JSON on stdin, cache, failures), built-in line, git branch, prompt rows."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from axon import platform as plat
from axon.ui import statusline as sl
from axon.ui.input import EditorState, PromptChrome, build_frame
from axon.ui.theme import strip_ansi

posix_only = pytest.mark.skipif(plat.IS_WINDOWS, reason="command quoting differs on Windows shells")


@pytest.fixture(autouse=True)
def _clear_cache():
    sl._cache.clear()
    sl._last_good.clear()
    yield
    sl._cache.clear()
    sl._last_good.clear()


def make_agent(ws: Path, status_line=None, model="claude-opus-5"):
    settings = SimpleNamespace(model=model, workspace=ws, mode="default", effort="high", thinking=False,
                               status_line=status_line, output_style="default", additional_dirs=[])
    ledger = SimpleNamespace(total=lambda: 0.1234, totals=lambda: {"input": 1200, "output": 300})
    conv = SimpleNamespace(token_estimate=lambda: 20_000)
    session = SimpleNamespace(active_session_id="session_x", active_file=ws / "s.jsonl")
    renderer = SimpleNamespace(trace_level="normal", last_context=(50_000, 200_000))
    return SimpleNamespace(settings=settings, ledger=ledger, conversation=conv, session=session, renderer=renderer)


def script_cmd(tmp_path: Path, body: str) -> str:
    script = tmp_path / "status.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_config_parsing():
    assert sl.status_line_config(SimpleNamespace(status_line=None)) is None
    assert sl.status_line_config(SimpleNamespace(status_line={"type": "command", "command": " "})) is None
    assert sl.status_line_config(SimpleNamespace(status_line="echo hi"))["command"] == "echo hi"
    assert sl.status_line_config(SimpleNamespace(statusLine={"command": "x"}))["command"] == "x"
    assert sl.status_line_config(SimpleNamespace(status_line={"type": "static", "command": "x"})) is None


def test_payload_schema(tmp_path):
    agent = make_agent(tmp_path)
    p = sl.build_payload(agent.settings, agent)
    assert p["model"]["id"] == "claude-opus-5"
    assert p["workspace"]["current_dir"] == str(tmp_path)
    assert p["session_id"] == "session_x" and p["transcript_path"].endswith("s.jsonl")
    assert p["context_window"]["context_window_size"] == 200_000
    assert p["context_window"]["used_percentage"] == 25.0
    assert p["cost"]["total_cost_usd"] == pytest.approx(0.1234)
    assert p["trace_level"] == "normal"
    json.dumps(p)


@posix_only
def test_custom_command_receives_json_and_output_is_cached(tmp_path):
    log = tmp_path / "runs.txt"
    cmd = script_cmd(tmp_path, (
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write('x')\n"
        "d = json.load(sys.stdin)\n"
        "print(d['model']['display_name'] + ' | ' + str(d['context_window']['used_percentage']) + '%')\n"
        "print('\\033[32mline two\\033[0m')\n"))
    agent = make_agent(tmp_path, {"type": "command", "command": cmd, "padding": 1})
    out = sl.render_custom_status(agent.settings, agent)
    assert out == " claude-opus-5 | 25.0%\n \033[32mline two\033[0m"
    assert sl.render_custom_status(agent.settings, agent) == out     # within 300 ms: cached
    assert log.read_text() == "x"


@posix_only
def test_failing_command_keeps_last_good_output(tmp_path):
    flag = tmp_path / "fail"
    cmd = script_cmd(tmp_path, f"import os, sys\nif os.path.exists({str(flag)!r}): sys.exit(3)\nprint('ok')\n")
    agent = make_agent(tmp_path, {"command": cmd})
    assert sl.render_custom_status(agent.settings, agent) == "ok"
    flag.write_text("1")
    sl._cache.clear()
    assert sl.render_custom_status(agent.settings, agent) == "ok"
    sl._last_good.clear()
    sl._cache.clear()
    assert sl.render_custom_status(agent.settings, agent) is None


@posix_only
def test_timeout_returns_none(tmp_path):
    cmd = script_cmd(tmp_path, "import time\ntime.sleep(5)\n")
    assert sl.run_status_command(cmd, {}, timeout=0.3) is None


def test_git_branch_variants(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert sl.git_branch(sub) == "feature/x"
    (repo / ".git" / "HEAD").write_text("0123456789abcdef\n")
    import os
    os.utime(repo / ".git" / "HEAD", (1, 1))
    assert sl.git_branch(repo) == "0123456"
    wt = tmp_path / "wt"
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/wt-branch\n")
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")
    assert sl.git_branch(wt) == "wt-branch"
    assert sl.git_branch(None) == ""


def test_default_line_and_prompt_rows(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    agent = make_agent(tmp_path)
    line = strip_ansi(sl.default_status_line(agent, width=200))
    for part in ("claude-opus-5", "ctx 25%", "$0.1234", "trace normal", "main"):
        assert part in line
    assert "default" not in line                 # the mode row above the status line already shows it
    assert "default" in strip_ansi(sl.default_status_line(agent, width=200, include_mode=True))
    rows = sl.prompt_status_rows(agent, width=200)
    assert len(rows) == 1 and "claude-opus-5" in strip_ansi(rows[0])
    assert sl.prompt_status_rows(None) == []


def test_status_rows_render_under_prompt(monkeypatch):
    monkeypatch.setattr(plat, "term_size", lambda: (80, 24))
    st = EditorState()
    st.status_rows = ["  my custom status"]
    lines, _ = build_frame(st, PromptChrome())
    plain = [strip_ansi(l) for l in lines]
    assert plain.index("  my custom status") == plain.index(next(l for l in plain if "shift+tab to cycle" in l)) + 1
