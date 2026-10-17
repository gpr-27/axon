"""Tests for axon.features.solver (detection, running, parsing, /verify, /fix, /autofix)."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.features import solver
from axon.features.solver import Check, Failure, detect_checks, parse_failures, run_check
from axon.providers.base import Usage
from axon.session.ledger import Ledger


def py_cmd(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class FakeAgent:
    """Duck-typed agent: run_turn calls `on_turn(prompt)` and records a priced ledger entry."""

    def __init__(self, ws: Path, on_turn: Any = None, cost: str = "0.01") -> None:
        self.settings = SimpleNamespace(workspace=ws, model="fake-model", effort=None)
        self.ledger = Ledger()
        self.conversation = SimpleNamespace(messages=[])
        self.traces: list[Any] = []
        self.prompts: list[str] = []
        self.on_turn = on_turn
        self.cost = cost

    def run_turn(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self.on_turn:
            self.on_turn(prompt)
        self.ledger.record("fake-model", Usage(input=10, output=5), source="main", cost=Decimal(self.cost))
        return SimpleNamespace(final_text="done", stop_reason="end_turn", iterations=1,
                               tool_calls_count=1, usage=Usage(), error=None)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXON_HOME", str(tmp_path / "axon_home"))


# ── detection ──────────────────────────────────────────────────────────────
def test_detect_python_and_js(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "lint": "eslint .", "typecheck": "tsc"}}), encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    names = {c.name: c for c in detect_checks(tmp_path)}
    assert {"pytest", "ruff", "mypy", "pnpm test", "pnpm lint", "pnpm typecheck"} <= set(names)
    assert names["ruff"].kind == "lint" and names["mypy"].kind == "typecheck"
    assert names["pnpm lint"].command == "pnpm lint"


def test_detect_other_ecosystems(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
    names = {c.name for c in detect_checks(tmp_path)}
    assert {"go test", "cargo test", "mvn test", "dotnet test"} <= names


def test_unittest_fallback_and_npm_default_test_ignored(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}), encoding="utf-8")
    names = [c.name for c in detect_checks(tmp_path)]
    assert "unittest" in names and "npm test" not in names


def test_overrides_from_settings_and_workspace(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / ".axon").mkdir()
    (tmp_path / ".axon" / "settings.json").write_text(json.dumps(
        {"verify": ["make check", {"name": "lint", "command": "ruff check .", "kind": "lint"}]}), encoding="utf-8")
    checks = detect_checks(tmp_path)
    assert [c.name for c in checks] == ["make check", "lint"] and checks[1].kind == "lint"
    s = SimpleNamespace(verify_commands=["pytest -q"])
    assert [c.command for c in detect_checks(tmp_path, s)] == ["pytest -q"]


# ── running ────────────────────────────────────────────────────────────────
def test_run_check_pass_fail_and_missing(tmp_path: Path) -> None:
    ok = run_check(Check("ok", py_cmd("print('hi')")), tmp_path)
    assert ok.ok and "hi" in ok.output and ok.duration >= 0
    bad = run_check(Check("bad", py_cmd("import sys; print('x.py:3: boom'); sys.exit(2)")), tmp_path)
    assert not bad.ok and bad.exit_code == 2 and bad.failures[0].location == "x.py:3"
    missing = run_check(Check("nope", ["definitely-not-a-real-exe-axon"]), tmp_path)
    assert missing.exit_code == 127 and not missing.ok


def test_run_check_shell_string_and_timeout(tmp_path: Path) -> None:
    exe = sys.executable
    res = run_check(Check("shell", f'"{exe}" -c "print(40+2)"' if sys.platform != "win32" else 'python -c "print(40+2)"'), tmp_path)
    assert "42" in res.output
    slow = run_check(Check("slow", py_cmd("import time; time.sleep(30)")), tmp_path, timeout=0.5)
    assert slow.timed_out and not slow.ok


# ── parsing ────────────────────────────────────────────────────────────────
def test_parse_python_traceback_prefers_project_frame() -> None:
    out = ('Traceback (most recent call last):\n'
           '  File "/ws/app/main.py", line 12, in run\n    foo()\n'
           '  File "/usr/lib/python3.11/site-packages/lib.py", line 9, in foo\n'
           'ValueError: bad value\n')
    f = parse_failures(out, Path("/ws"))
    assert f == [Failure("app/main.py", 12, "ValueError: bad value", "python")]


def test_parse_pytest_summary_merges_location() -> None:
    out = "tests/test_a.py:7: AssertionError\nFAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py - ImportError: x"
    f = parse_failures(out)
    assert f[0].location == "tests/test_a.py:7" and "test_x" in f[0].message
    assert f[1].file == "tests/test_b.py" and f[1].line is None


@pytest.mark.parametrize("text,loc,kind", [
    ("src/app.ts(12,5): error TS2322: Type 'x'", "src/app.ts:12:5", "tsc"),
    ("src/b.ts:3:1 - error TS1005: ';' expected", "src/b.ts:3:1", "tsc"),
    ("TypeError: oops\n    at foo (/ws/src/x.js:10:5)\n    at bar (/ws/node_modules/z/i.js:1:1)", "src/x.js:10:5", "js"),
    ("./main.go:12:5: undefined: x", "main.go:12:5", "go"),
    ("error[E0308]: mismatched types\n  --> src/main.rs:4:5", "src/main.rs:4:5", "rust"),
    ("thread 't' panicked at src/lib.rs:10:5:\nassertion failed", "src/lib.rs:10:5", "rust"),
    (r"C:\ws\Foo.cs(12,5): error CS1002: ; expected", "Foo.cs:12:5", "csharp"),
    ("[ERROR] /ws/src/A.java:[12,5] cannot find symbol", "src/A.java:12:5", "java"),
    ("src/a.py:1:8: F401 `os` imported but unused", "src/a.py:1:8", "generic"),
])
def test_parse_many_ecosystems(text: str, loc: str, kind: str) -> None:
    ws = Path(r"C:\ws") if "C:" in text else Path("/ws")
    f = parse_failures(text, ws)
    assert f and f[0].location == loc and f[0].kind == kind and f[0].message


def test_normalize_windows_paths() -> None:
    assert solver.normalize_path(r"C:\Users\me\proj\app\mod.py", Path(r"C:\Users\me\proj")) == "app/mod.py"
    assert solver.normalize_path(r".\src\x.py") == "src/x.py"


def test_snippet_and_fix_prompt(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 31)), encoding="utf-8")
    f = Failure("m.py", 15, "NameError: x", "python")
    snip = solver.snippet(tmp_path, f)
    assert "> 15 | line15" in snip and "7 | line7" in snip and "line24" not in snip
    prompt = solver.build_fix_prompt(tmp_path, "pytest -q", "boom", [f])
    assert "m.py:15" in prompt and "line15" in prompt and "`pytest -q`" in prompt


def test_looks_like_command() -> None:
    assert solver.looks_like_command("pytest -q tests")
    assert solver.looks_like_command("npm run build")
    assert not solver.looks_like_command("TypeError: x is undefined")
    assert not solver.looks_like_command("Traceback (most recent call last):\n  File")


def test_last_bash_error_from_conversation(tmp_path: Path) -> None:
    agent = FakeAgent(tmp_path)
    agent.conversation.messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "pytest"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x.py:2: E\nexit code 1", "is_error": True}]},
        {"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": "Bash", "arguments": json.dumps({"command": "make"})}}]},
        {"role": "tool", "tool_call_id": "b", "content": "ok\nexit code 0"},
    ]
    assert solver.last_bash_error(agent) == ("pytest", "x.py:2: E\nexit code 1")


# ── commands ──────────────────────────────────────────────────────────────
def _workspace_with_flag_check(tmp_path: Path, fail_lines: int = 1) -> tuple[Path, Path]:
    flag = tmp_path / "count.txt"
    flag.write_text(str(fail_lines), encoding="utf-8")
    code = ("import sys,pathlib; n=int(pathlib.Path('count.txt').read_text());"
            "[print(f'mod.py:{i+1}: error E{i}') for i in range(n)]; sys.exit(1 if n else 0)")
    (tmp_path / ".axon").mkdir(exist_ok=True)
    (tmp_path / ".axon" / "settings.json").write_text(json.dumps(
        {"verify": [{"name": "flag", "command": py_cmd(code), "kind": "test"}]}), encoding="utf-8")
    (tmp_path / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
    return tmp_path, flag


def test_verify_command_prints_table_and_remembers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _ = _workspace_with_flag_check(tmp_path, 2)
    agent = FakeAgent(ws)
    solver.cmd_verify(agent, "")
    out = capsys.readouterr().out
    assert "flag" in out and "mod.py:1" in out and "1 failing" in out
    assert not agent._axon_solver_state["last_verify"][0].ok


def test_verify_no_checks_hint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    solver.cmd_verify(FakeAgent(tmp_path), "")
    assert "No checks detected" in capsys.readouterr().out


def test_fix_uses_last_verify_and_calls_agent(tmp_path: Path) -> None:
    ws, _ = _workspace_with_flag_check(tmp_path, 1)
    agent = FakeAgent(ws)
    solver.cmd_verify(agent, "")
    solver.cmd_fix(agent, "")
    assert len(agent.prompts) == 1
    p = agent.prompts[0]
    assert "mod.py:1" in p and "> 1 | a" in p


def test_fix_with_pasted_error_text(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("one\ntwo\n", encoding="utf-8")
    agent = FakeAgent(tmp_path)
    solver.cmd_fix(agent, 'Traceback (most recent call last):\n  File "x.py", line 2, in <module>\nKeyError: 1')
    assert "x.py:2" in agent.prompts[0] and "KeyError" in agent.prompts[0]


def test_fix_with_passing_command_does_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = FakeAgent(tmp_path)
    exe = Path(sys.executable).name
    solver.cmd_fix(agent, f'{sys.executable} -c "print(1)"' if " " not in sys.executable else f"{exe} -V")
    assert agent.prompts == []
    assert "nothing to fix" in capsys.readouterr().out


def test_fix_nothing_available(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = FakeAgent(tmp_path)
    solver.cmd_fix(agent, "")
    assert agent.prompts == [] and "Nothing to fix" in capsys.readouterr().out


def test_autofix_loops_until_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, flag = _workspace_with_flag_check(tmp_path, 3)

    def fix_one(prompt: str) -> None:
        flag.write_text(str(max(0, int(flag.read_text()) - 2)), encoding="utf-8")

    agent = FakeAgent(ws, fix_one, cost="0.02")
    solver.cmd_autofix(agent, "--max 5")
    out = capsys.readouterr().out
    assert len(agent.prompts) == 2
    assert "3 → 1" in out or "3 -> 1" in out
    assert "Green after 2" in out and "$0.0200" in out
    assert "Previous attempt" in agent.prompts[1]


def test_autofix_stops_when_no_progress(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _ = _workspace_with_flag_check(tmp_path, 2)
    agent = FakeAgent(ws)
    solver.cmd_autofix(agent, "--max 10")
    out = capsys.readouterr().out
    assert len(agent.prompts) == 2 and "no progress" in out


def test_autofix_respects_max_and_command_arg(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agent = FakeAgent(tmp_path)
    rows = solver.autofix(agent, [Check("c", py_cmd("import sys; print('a.py:1: x'); sys.exit(1)"))], tmp_path, 1)
    assert len(rows) == 1 and rows[0]["status"] == "fail" and rows[0]["cost"] == Decimal("0.01")


def test_autofix_already_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _ = _workspace_with_flag_check(tmp_path, 0)
    agent = FakeAgent(ws)
    solver.cmd_autofix(agent, "")
    assert agent.prompts == [] and "Already passing" in capsys.readouterr().out


def test_autofix_ctrl_c_stops_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, _ = _workspace_with_flag_check(tmp_path, 1)

    def interrupt(prompt: str) -> None:
        raise KeyboardInterrupt

    solver.cmd_autofix(FakeAgent(ws, interrupt), "")
    assert "Stopped by Ctrl+C" in capsys.readouterr().out


def test_commands_registered() -> None:
    names = {c.name for c in solver.COMMANDS}
    assert names == {"/verify", "/fix", "/autofix"}


def test_fix_does_not_resend_consumed_verify_results(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, flag = _workspace_with_flag_check(tmp_path, 1)
    agent = FakeAgent(ws, lambda p: flag.write_text("0", encoding="utf-8"))
    solver.cmd_verify(agent, "")
    solver.cmd_fix(agent, "")
    solver.cmd_fix(agent, "")  # the first /fix fixed it: stale failures must not be sent again
    assert len(agent.prompts) == 1
    assert "Nothing to fix" in capsys.readouterr().out


def test_autofix_updates_last_verify(tmp_path: Path) -> None:
    ws, flag = _workspace_with_flag_check(tmp_path, 2)
    solver_agent = FakeAgent(ws, lambda p: flag.write_text("1", encoding="utf-8"))
    solver.cmd_verify(solver_agent, "")
    solver.cmd_autofix(solver_agent, "--max 1")
    last = solver_agent._axon_solver_state["last_verify"]
    assert [f.location for f in last[0].failures] == ["mod.py:1"]


def test_git_stat_from_repo_subdirectory(tmp_path: Path) -> None:
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("git"):
        pytest.skip("git not installed")
    _sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x\n", encoding="utf-8")
    _sp.run(["git", "add", "."], cwd=tmp_path, check=True)
    _sp.run(["git", "-c", "user.email=t@e", "-c", "user.name=t", "-c", "commit.gpgsign=false",
             "commit", "-qm", "i"], cwd=tmp_path, check=True)
    (tmp_path / "pkg" / "a.py").write_text("y\n", encoding="utf-8")
    assert "a.py" in solver._git_stat(tmp_path / "pkg")


def test_python_exe_skips_windows_store_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\python3.exe"
    monkeypatch.setattr(solver.shutil, "which", lambda name: stub)
    assert solver._python_exe(tmp_path) == sys.executable
    monkeypatch.setattr(solver.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    assert solver._python_exe(tmp_path) == "/usr/bin/python3"
