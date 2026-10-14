"""
E2E-C: feature commands end to end (real CLI subprocess, fake LLM).

axon setup (scripted), axon new <template> (+ running the generated tests), /verify, /autofix
(the fake model's Edit really fixes a failing pytest), /fix (pasted traceback / command), /solve
(JSON plan + --yes), /commit and /pr (no gh) in a temp git repo, /map, /index, /next, /insights
and `axon report` (HTML KPIs match the usage log).
"""
from __future__ import annotations

import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fake_llm import Recorded, script_from_rules
from harness import IS_WINDOWS, AxonEnv
from test_e2e_c_support import (  # noqa: F401
    ext_llm, has_tool_result, no_gh_path, no_traceback, usage_records, venv_path_env, xenv, xgit_env,
)

pytestmark = pytest.mark.e2e

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# ─── axon setup ─────────────────────────────────────────────────────────────

def _cfg(env: AxonEnv) -> dict:
    return tomllib.loads((env.axon_home / "config.toml").read_text(encoding="utf-8"))


def test_setup_scripted_writes_config_and_keeps_other_keys(xenv: AxonEnv) -> None:
    """`axon setup --provider xai --model grok-4 --mode plan --trace detailed --skip-verify` merges into config.toml."""
    (xenv.axon_home / "config.toml").write_text(
        'verbose = true\nrepo_map_tokens = 999\n\n[permissions]\nallow = ["Bash(ls *)"]\n', encoding="utf-8")
    r = xenv.run(["setup", "--provider", "xai", "--model", "grok-4", "--mode", "plan", "--trace", "detailed",
                  "--skip-verify"], env={"XAI_API_KEY": "xai-setup-key"})
    assert r.code == 0, str(r)
    no_traceback(r)
    cfg = _cfg(xenv)
    assert cfg["provider"] == "xai" and cfg["model"] == "grok-4" and cfg["mode"] == "plan"
    assert cfg["trace_level"] == "detailed" and cfg["base_url"] == "https://api.x.ai/v1"
    assert cfg["verbose"] is True and cfg["repo_map_tokens"] == 999
    assert cfg["permissions"] == {"allow": ["Bash(ls *)"]}


def test_setup_result_drives_the_next_run(xenv: AxonEnv) -> None:
    """After setup, `axon -p` uses the configured provider + model (endpoint pointed at the fake via --base-url)."""
    r = xenv.run(["setup", "--provider", "xai", "--model", "grok-code-fast-1", "--mode", "bypass", "--trace", "compact",
                  "--skip-verify"], env={"XAI_API_KEY": "xai-run-key"})
    assert r.code == 0, str(r)
    env = {k: v for k, v in xenv.env.items() if k not in ("AXON_MODEL", "AXON_API_KEY", "AXON_BASE_URL")}
    env["XAI_API_KEY"] = "xai-run-key"
    xenv.llm.enqueue([{"text": "configured"}])
    from harness import run_axon
    r2 = run_axon(["-p", "--base-url", xenv.llm.url, "hi"], env=env, cwd=xenv.workspace)
    assert r2.code == 0, str(r2)
    req = xenv.llm.completion_requests()[0]
    assert req.body["model"] == "grok-code-fast-1"
    assert req.headers.get("authorization") == "Bearer xai-run-key"


def _closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_env_base_url_beats_config_toml_base_url(xenv: AxonEnv) -> None:
    """Documented precedence env > config.toml: AXON_BASE_URL wins over base_url written by `axon setup`."""
    (xenv.axon_home / "config.toml").write_text(f'base_url = "http://127.0.0.1:{_closed_port()}/v1"\n', encoding="utf-8")
    xenv.llm.enqueue([{"text": "env wins"}])
    r = xenv.run(["-p", "--output-format", "json", "hi"], timeout=60)
    assert r.code == 0, str(r)
    assert len(xenv.llm.completion_requests()) == 1


def test_env_model_beats_config_toml_model(xenv: AxonEnv) -> None:
    """AXON_MODEL=gpt-4o must win over model = "gpt-4o-mini" in config.toml."""
    (xenv.axon_home / "config.toml").write_text('model = "gpt-4o-mini"\n', encoding="utf-8")
    xenv.llm.enqueue([{"text": "which model"}])
    r = xenv.run(["-p", "hi"])
    assert r.code == 0, str(r)
    assert xenv.llm.completion_requests()[0].body["model"] == "gpt-4o"


def test_cli_flag_beats_config_toml(xenv: AxonEnv) -> None:
    """--model on the command line wins over config.toml (control for the env-precedence bug)."""
    (xenv.axon_home / "config.toml").write_text('model = "gpt-4o-mini"\n', encoding="utf-8")
    xenv.llm.enqueue([{"text": "flag"}])
    r = xenv.run(["-p", "--model", "gpt-4.1", "hi"])
    assert r.code == 0, str(r)
    assert xenv.llm.completion_requests()[0].body["model"] == "gpt-4.1"


def test_setup_local_provider_needs_no_key(xenv: AxonEnv) -> None:
    """`axon setup --provider ollama --model llama3.1:8b --skip-verify` works without any key."""
    env = {k: v for k, v in xenv.env.items() if not k.endswith("_API_KEY")}
    from harness import run_axon
    r = run_axon(["setup", "--provider", "ollama", "--model", "llama3.1:8b", "--mode", "default", "--trace", "normal",
                  "--skip-verify"], env=env, cwd=xenv.workspace)
    assert r.code == 0, str(r)
    cfg = _cfg(xenv)
    assert cfg["provider"] == "ollama" and cfg["base_url"] == "http://localhost:11434/v1" and cfg["model"] == "llama3.1:8b"


def test_setup_without_key_non_interactive_fails_and_writes_nothing(xenv: AxonEnv) -> None:
    """Scripted setup for a keyed provider with no key: exit 2, config.toml not created."""
    env = {k: v for k, v in xenv.env.items() if not k.endswith("_API_KEY")}
    from harness import run_axon
    r = run_axon(["setup", "--provider", "groq", "--model", "llama-3.3-70b-versatile", "--skip-verify"], env=env,
                 cwd=xenv.workspace)
    assert r.code == 2, str(r)
    assert "GROQ_API_KEY" in r.output
    assert not (xenv.axon_home / "config.toml").exists()


def test_setup_unknown_provider_and_missing_provider(xenv: AxonEnv) -> None:
    """Unknown --provider -> exit 2; no --provider on a non-TTY -> exit 1 with manual instructions."""
    r = xenv.run(["setup", "--provider", "nope-ai", "--skip-verify"])
    assert r.code == 2 and "nope-ai" in r.output, str(r)
    r2 = xenv.run(["setup"])
    assert r2.code == 1, str(r2)
    assert "config.toml" in r2.output or "axon setup" in r2.output


def test_setup_refuses_to_clobber_malformed_config(xenv: AxonEnv) -> None:
    """A malformed config.toml is left untouched (exit 4) instead of being overwritten."""
    bad = "model = 'unterminated\n[[[\n"
    (xenv.axon_home / "config.toml").write_text(bad, encoding="utf-8")
    r = xenv.run(["setup", "--provider", "xai", "--model", "grok-4", "--skip-verify"], env={"XAI_API_KEY": "k"})
    assert r.code == 4, str(r)
    assert (xenv.axon_home / "config.toml").read_text(encoding="utf-8") == bad


# ─── axon new ───────────────────────────────────────────────────────────────

from axon.features.scaffold import list_templates  # noqa: E402

TEMPLATES = [t.name for t in list_templates()]


def _run(cmd: list[str], cwd: Path, timeout: float = 120, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


@pytest.mark.parametrize("template", TEMPLATES)
def test_new_template_generates_complete_project(xenv: AxonEnv, template: str) -> None:
    """`axon new <template> <dir>`: exit 0, every .tmpl rendered, placeholders replaced, dotfiles renamed."""
    r = xenv.run(["new", template, "proj-e2e"])
    assert r.code == 0, str(r)
    dest = xenv.workspace / "proj-e2e"
    files = [p for p in dest.rglob("*") if p.is_file()]
    assert files, f"nothing generated for {template}"
    assert not [p for p in files if p.name.endswith(".tmpl")]
    from axon.features.scaffold import get_template
    if (get_template(template).path / "dot-gitignore.tmpl").is_file():
        assert (dest / ".gitignore").is_file() and not (dest / "dot-gitignore").exists()
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        assert "{{" not in text or "{{ " in text, f"unrendered placeholder in {p}"
        assert "{{" not in str(p.relative_to(dest))
    if (dest / "package.json").is_file():
        pkg = json.loads((dest / "package.json").read_text(encoding="utf-8"))
        assert pkg["name"] == "proj-e2e"


def _pytest_generated(dest: Path) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], dest)


@pytest.mark.parametrize("template", ["python-cli", "python-lib"])
def test_new_python_template_tests_pass(xenv: AxonEnv, template: str) -> None:
    """The generated Python project's own pytest suite passes out of the box."""
    assert xenv.run(["new", template, "pyproj"]).code == 0
    p = _pytest_generated(xenv.workspace / "pyproj")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "passed" in p.stdout


@pytest.mark.parametrize("template,module", [("fastapi", "fastapi"), ("flask", "flask")])
def test_new_web_template_tests_pass(xenv: AxonEnv, template: str, module: str) -> None:
    """fastapi / flask starters: their pytest suites pass (skipped when the framework is not installed)."""
    pytest.importorskip(module)
    if module == "fastapi":
        pytest.importorskip("httpx")
    assert xenv.run(["new", template, "webproj"]).code == 0
    p = _pytest_generated(xenv.workspace / "webproj")
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_new_node_cli_template_tests_pass(xenv: AxonEnv) -> None:
    """node-cli (zero dependencies): `node --test test/` passes."""
    assert xenv.run(["new", "node-cli", "nodeproj"]).code == 0
    p = _run(["node", "--test", "test/"], xenv.workspace / "nodeproj")
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_new_go_cli_template_tests_pass(xenv: AxonEnv) -> None:
    """go-cli: `go test ./...` passes (std library only, works offline)."""
    assert xenv.run(["new", "go-cli", "goproj"]).code == 0
    p = _run(["go", "test", "./..."], xenv.workspace / "goproj", timeout=180, env={"GOFLAGS": "-mod=mod", "GOPROXY": "off"})
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_new_rust_cli_template_tests_pass(xenv: AxonEnv) -> None:
    """rust-cli: `cargo test --offline` passes."""
    assert xenv.run(["new", "rust-cli", "rsproj"]).code == 0
    p = _run(["cargo", "test", "--offline", "-q"], xenv.workspace / "rsproj", timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr


def test_new_refuses_nonempty_dir_without_force(xenv: AxonEnv) -> None:
    """A non-empty target is not written non-interactively; --force writes into it."""
    dest = xenv.workspace / "busy"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    r = xenv.run(["new", "python-lib", "busy"])
    assert r.code == 1, str(r)
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    r2 = xenv.run(["new", "python-lib", "busy", "--force"])
    assert r2.code == 0, str(r2)
    assert (dest / "pyproject.toml").is_file() and (dest / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_new_list_and_unknown_template(xenv: AxonEnv) -> None:
    """`axon new --list` lists every template; an unknown template exits 2."""
    r = xenv.run(["new", "--list"])
    assert r.code == 0
    for t in TEMPLATES:
        assert t in r.output
    r2 = xenv.run(["new", "cobol-mainframe", "x"])
    assert r2.code == 2 and not (xenv.workspace / "x").exists()


def test_new_name_flag_renders_module_name(xenv: AxonEnv) -> None:
    """--name My-Tool -> slug used in pyproject and module directory src/my_tool."""
    r = xenv.run(["new", "python-lib", "libdir", "--name", "my-tool"])
    assert r.code == 0, str(r)
    dest = xenv.workspace / "libdir"
    assert (dest / "src" / "my_tool" / "__init__.py").is_file()
    assert 'name = "my-tool"' in (dest / "pyproject.toml").read_text(encoding="utf-8")


# ─── solver: /verify /autofix /fix ─────────────────────────────────────────

BUGGY = "def add(a, b):\n    return a - b\n"


def _buggy_project(ws: Path) -> None:
    (ws / "calc.py").write_text(BUGGY, encoding="utf-8")
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                                               encoding="utf-8")
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")


def _fixing_model(env: AxonEnv, must_contain: tuple[str, ...] = ("calc.py",)) -> None:
    """Script: Read calc.py, Edit the bug, then summarise. The first request must contain `must_contain`."""
    def first(req: Recorded):
        txt = req.last_user_text()
        for s in must_contain:
            if s not in txt:
                return f"fix prompt lacks {s!r}: {txt[:400]!r}"
        return True
    env.llm.enqueue(
        {"steps": [{"tool": "Read", "args": {"path": "calc.py"}}], "expect": [first]},
        [{"tool": "Edit", "args": {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}}],
        [{"text": "Fixed add(): it subtracted instead of adding."}],
    )


def test_verify_reports_failing_pytest_with_location(xenv: AxonEnv) -> None:
    """/verify detects pytest from pytest.ini, runs it and lists the failing test file:line."""
    _buggy_project(xenv.workspace)
    r = xenv.run(["-p", "/verify"], env=venv_path_env(xenv))
    assert r.code == 1, str(r)  # a failing check makes `axon -p /verify` exit non-zero
    assert "pytest" in r.output and "1 failing" in r.output, r.output
    assert "tests/test_calc.py:5" in r.output
    assert xenv.llm.completion_requests() == []  # verify itself never calls the model


def test_verify_passing_project(xenv: AxonEnv) -> None:
    """/verify on a green project reports 0 failing."""
    _buggy_project(xenv.workspace)
    (xenv.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    r = xenv.run(["-p", "/verify"], env=venv_path_env(xenv))
    assert r.code == 0
    assert "All 1 checks passed" in r.output, r.output


def test_verify_uses_configured_commands(xenv: AxonEnv) -> None:
    """.axon/settings.json "verify" overrides detection (custom command, custom name)."""
    (xenv.workspace / ".axon").mkdir()
    (xenv.workspace / ".axon" / "settings.json").write_text(json.dumps(
        {"verify": [{"name": "lint-e2e", "command": f'"{sys.executable}" -c "import sys; sys.exit(3)"', "kind": "lint"}]}),
        encoding="utf-8")
    r = xenv.run(["-p", "/verify"])
    assert "lint-e2e" in r.output and "exit 3" in r.output, r.output


def test_autofix_scripted_edit_turns_pytest_green(xenv: AxonEnv) -> None:
    """/autofix: verify fails -> fix prompt -> fake model Reads+Edits calc.py -> re-verify passes ("Green")."""
    _buggy_project(xenv.workspace)
    _fixing_model(xenv, ("calc.py", "test_calc.py", "assert"))
    r = xenv.run(["-p", "/autofix"], env=venv_path_env(xenv), timeout=120)
    assert r.code == 0, str(r)
    assert not xenv.llm.failures, xenv.llm.failures
    assert (xenv.workspace / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert "Green after 1 attempt" in r.output, r.output
    p = _pytest_generated(xenv.workspace)
    assert p.returncode == 0, p.stdout


def test_autofix_gives_up_when_model_makes_no_progress(xenv: AxonEnv) -> None:
    """A model that never edits: /autofix --max 3 stops with 'no progress' and the file is unchanged."""
    _buggy_project(xenv.workspace)
    xenv.llm.default_script = [{"text": "I am not sure how to fix this."}]
    r = xenv.run(["-p", "/autofix --max 3"], env=venv_path_env(xenv), timeout=120)
    assert r.code == 0, str(r)
    assert "Still failing" in r.output and "stopped decreasing" in r.output, r.output
    assert (xenv.workspace / "calc.py").read_text(encoding="utf-8") == BUGGY
    assert len(xenv.llm.completion_requests()) == 2


def test_autofix_on_green_project_does_nothing(xenv: AxonEnv) -> None:
    """Already passing: no model call, 'nothing to fix'."""
    _buggy_project(xenv.workspace)
    (xenv.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    r = xenv.run(["-p", "/autofix"], env=venv_path_env(xenv))
    assert "nothing to fix" in r.output.lower(), r.output
    assert xenv.llm.completion_requests() == []


def test_fix_with_pasted_traceback_sends_snippet_and_fixes(xenv: AxonEnv) -> None:
    """/fix <pasted traceback>: the prompt carries the file:line + source snippet; the scripted Edit fixes it."""
    _buggy_project(xenv.workspace)
    tb = ("Traceback (most recent call last):\n"
          f'  File "{xenv.workspace / "calc.py"}", line 2, in add\n'
          "    return a - b\n"
          "TypeError: unsupported operand type(s) for -: 'str' and 'str'\n")
    _fixing_model(xenv, ("calc.py", "return a - b", "TypeError"))
    r = xenv.run(["-p", "/fix " + tb], env=venv_path_env(xenv), timeout=90)
    assert r.code == 0, str(r)
    assert not xenv.llm.failures, xenv.llm.failures
    assert "Error Doctor" in r.output and "1 located failure" in r.output, r.output
    assert "return a + b" in (xenv.workspace / "calc.py").read_text(encoding="utf-8")


def test_fix_with_command_runs_it_then_fixes(xenv: AxonEnv) -> None:
    """/fix <command>: runs the failing command, parses failures, hands them to the agent."""
    _buggy_project(xenv.workspace)
    _fixing_model(xenv, ("calc.py", "test_add"))
    r = xenv.run(["-p", f"/fix {sys.executable} -m pytest -q -p no:cacheprovider"], env=venv_path_env(xenv), timeout=90)
    assert r.code == 0, str(r)
    assert not xenv.llm.failures, xenv.llm.failures
    assert "return a + b" in (xenv.workspace / "calc.py").read_text(encoding="utf-8")


def test_fix_with_nothing_to_fix(xenv: AxonEnv) -> None:
    """/fix with no argument and no failing history explains usage and makes no model call."""
    r = xenv.run(["-p", "/fix"])
    assert "Nothing to fix" in r.output, r.output
    assert xenv.llm.completion_requests() == []


# ─── planner: /solve ────────────────────────────────────────────────────────

PLAN = {"goal": "Add a multiply helper with a test",
        "steps": [{"title": "Create mathx.py", "detail": "def mul(a, b)", "verify": ""},
                  {"title": "Add a test", "detail": "tests/test_mathx.py", "verify": "`python -m pytest -q -p no:cacheprovider`"}]}


def _plan_project(ws: Path) -> None:
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = .\n", encoding="utf-8")


def test_solve_yes_executes_json_plan_steps(xenv: AxonEnv) -> None:
    """/solve <task> --yes: tool-less plan call -> JSON plan -> each step runs as a turn -> verify passes."""
    _plan_project(xenv.workspace)

    def plan_req(req: Recorded):
        if req.body.get("tools"):
            return "planning call must not offer tools"
        return "STRICT JSON" in req.system_text or "planning prompt missing"

    xenv.llm.enqueue(
        {"steps": [{"text": json.dumps(PLAN)}], "expect": [plan_req]},
        {"steps": [{"tool": "Write", "args": {"path": "mathx.py", "content": "def mul(a, b):\n    return a * b\n"}}],
         "expect": [lambda q: "CURRENT STEP 1/2" in q.last_user_text() or "step 1 prompt missing"]},
        [{"text": "Created mathx.py."}],
        {"steps": [{"tool": "Write", "args": {"path": "tests/test_mathx.py",
                                               "content": "from mathx import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n"}}],
         "expect": [lambda q: "CURRENT STEP 2/2" in q.last_user_text() or "step 2 prompt missing"]},
        [{"text": "Added the test."}],
    )
    r = xenv.run(["-p", "/solve add a multiply helper --yes"], env=venv_path_env(xenv), timeout=120)
    assert r.code == 0, str(r)
    assert not xenv.llm.failures, xenv.llm.failures
    assert (xenv.workspace / "mathx.py").is_file() and (xenv.workspace / "tests" / "test_mathx.py").is_file()
    assert "verify passed" in r.output, r.output
    plans = list(xenv.axon_home.rglob("plans/*.json"))
    assert len(plans) == 1, plans
    saved = json.loads(plans[0].read_text(encoding="utf-8"))
    assert saved["status"] == "done" and [s["status"] for s in saved["steps"]] == ["done", "done"]
    assert not list(xenv.workspace.rglob("plans"))


def test_solve_without_yes_saves_draft_only(xenv: AxonEnv) -> None:
    """Non-interactive /solve without --yes: plan saved as draft, no step executed."""
    xenv.llm.enqueue([{"text": json.dumps(PLAN)}])
    r = xenv.run(["-p", "/solve add a multiply helper"])
    assert "draft" in r.output, r.output
    assert len(xenv.llm.completion_requests()) == 1
    assert not (xenv.workspace / "mathx.py").exists()
    r2 = xenv.run(["-p", "/plans"])
    assert "multiply" in r2.output.lower(), r2.output


def test_solve_with_unparseable_plan_reports_error(xenv: AxonEnv) -> None:
    """A non-plan answer from the model -> 'Could not create a plan', nothing executed."""
    xenv.llm.enqueue([{"text": "I would rather chat about the weather."}])
    r = xenv.run(["-p", "/solve do something --yes"])
    assert "Could not create a plan" in r.output, r.output
    assert len(xenv.llm.completion_requests()) == 1


def test_solve_step_failure_aborts_non_interactively(xenv: AxonEnv) -> None:
    """A step whose verify command fails is marked failed and the plan aborts (no TTY to ask retry)."""
    plan = {"goal": "g", "steps": [{"title": "Do it", "detail": "", "verify": f"`{sys.executable} -c \"import sys; sys.exit(1)\"`"},
                                    {"title": "Never", "detail": "", "verify": ""}]}
    xenv.llm.enqueue([{"text": json.dumps(plan)}], [{"text": "did it"}])
    r = xenv.run(["-p", "/solve g --yes"], timeout=60)
    assert "step 1 failed" in r.output, r.output
    saved = json.loads(next(xenv.axon_home.rglob("plans/*.json")).read_text(encoding="utf-8"))
    assert saved["status"] == "aborted"
    assert [s["status"] for s in saved["steps"]] == ["failed", "pending"]


# ─── git: /commit /pr ───────────────────────────────────────────────────────

def _git(ws: Path, *args: str, env: AxonEnv) -> str:
    p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True, env={**os.environ, **{
        k: v for k, v in env.env.items() if k.startswith("GIT_")}})
    assert p.returncode == 0, p.stderr
    return p.stdout


def _repo_with_change(env: AxonEnv) -> None:
    ws = env.workspace
    (ws / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    _git(ws, "add", "-A", env=env)
    _git(ws, "commit", "-qm", "chore: init", env=env)
    (ws / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
    _git(ws, "add", "hello.py", env=env)


def test_commit_non_interactive_shows_llm_message_but_commits_nothing(xgit_env: AxonEnv, tmp_path: Path) -> None:
    """/commit without a TTY: the model writes the message from the staged diff, then the commit is aborted."""
    _repo_with_change(xgit_env)
    xgit_env.llm.enqueue({"steps": [{"text": "feat(hello): greet the whole world\n\n- widen the greeting"}],
                          "expect": [lambda q: "hello world" in q.last_user_text() or "staged diff missing"]})
    r = xgit_env.run(["-p", "/commit"], env={"PATH": no_gh_path(tmp_path)})
    assert not xgit_env.llm.failures, xgit_env.llm.failures
    assert "feat(hello): greet the whole world" in r.output and "generated by model" in r.output, r.output
    assert "Nothing committed" in r.output
    assert _git(xgit_env.workspace, "log", "--format=%s", env=xgit_env).split() == ["chore:", "init"]


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_commit_interactive_confirm_creates_commit(xgit_env: AxonEnv, tmp_path: Path) -> None:
    """REPL /commit + 'y': the fake model's Conventional Commit message becomes the real commit."""
    _repo_with_change(xgit_env)
    xgit_env.llm.enqueue([{"text": "feat(hello): greet the whole world\n\n- widen the greeting"}])
    with xgit_env.pty(env={"PATH": no_gh_path(tmp_path)}) as s:
        s.expect(r"(?i)(axon|❯|>)", timeout=20)
        s.drain(0.5)
        m = s.mark()
        s.sendline("/commit")
        s.expect(r"Commit with this message\?", timeout=30, since=m)
        s.send("y\r")
        s.expect(r"Committed [0-9a-f]+", timeout=20, since=m)
        s.drain(0.3)
        s.sendline("/exit")
        s.wait(10)
    log = _git(xgit_env.workspace, "log", "-1", "--format=%B", env=xgit_env)
    assert log.startswith("feat(hello): greet the whole world"), log
    assert "- widen the greeting" in log


def test_pr_without_gh_prints_generated_title_and_body(xgit_env: AxonEnv, tmp_path: Path) -> None:
    """/pr main on a feature branch with no gh: the model's title/body are shown, no push attempted."""
    _repo_with_change(xgit_env)
    ws = xgit_env.workspace
    _git(ws, "branch", "-M", "main", env=xgit_env)
    _git(ws, "commit", "-qm", "chore: base", "--allow-empty", env=xgit_env)
    _git(ws, "checkout", "-qb", "feature/greet", env=xgit_env)
    _git(ws, "commit", "-qam", "feat: greet world", "--allow-empty", env=xgit_env)
    xgit_env.llm.enqueue({"steps": [{"text": "feat: greet the world\n\n## Summary\n- greets everyone\n\n## Changes\n- hello.py"}],
                          "expect": [lambda q: "feature/greet" in q.last_user_text() or "branch missing from prompt"]})
    r = xgit_env.run(["-p", "/pr main"], env={"PATH": no_gh_path(tmp_path)})
    assert not xgit_env.llm.failures, xgit_env.llm.failures
    assert "feat: greet the world" in r.output and "## Summary" in r.output, r.output
    assert "gh) not found" in r.output


def test_pr_with_no_commits_ahead(xgit_env: AxonEnv, tmp_path: Path) -> None:
    """/pr on a branch with nothing ahead of base: explains and makes no model call."""
    _repo_with_change(xgit_env)
    _git(xgit_env.workspace, "commit", "-qm", "feat: x", env=xgit_env)
    _git(xgit_env.workspace, "branch", "-M", "main", env=xgit_env)
    r = xgit_env.run(["-p", "/pr main"], env={"PATH": no_gh_path(tmp_path)})
    assert "No commits" in r.output, r.output
    assert xgit_env.llm.completion_requests() == []


# ─── /map /index ────────────────────────────────────────────────────────────

def _code_project(ws: Path) -> None:
    (ws / "pkg").mkdir()
    (ws / "pkg" / "core.py").write_text("class Engine:\n    def start(self, speed):\n        return speed\n\n\n"
                                        "def build_engine():\n    return Engine()\n", encoding="utf-8")
    (ws / "app.js").write_text("export function renderPage(title) { return title; }\n", encoding="utf-8")


def test_map_lists_files_and_symbols(xenv: AxonEnv) -> None:
    """/map shows the ranked files with their classes / functions."""
    _code_project(xenv.workspace)
    r = xenv.run(["-p", "/map"])
    assert r.code == 0, str(r)
    for s in ("core.py", "class Engine", "build_engine", "app.js", "renderPage"):
        assert s in r.output, (s, r.output)


def test_index_stores_under_axon_home_and_is_incremental(xenv: AxonEnv) -> None:
    """/index writes index.json under AXON_HOME (never the workspace); a second run reuses cached files."""
    _code_project(xenv.workspace)
    r1 = xenv.run(["-p", "/index"])
    assert r1.code == 0, str(r1)
    m = re.search(r"stored at\s+(\S+)", r1.output)
    assert m, r1.output
    stored = Path(m.group(1)).resolve()
    assert stored.is_file() and str(xenv.axon_home.resolve()) in str(stored)
    assert re.search(r"files scanned\s+2", r1.output) and re.search(r"parsed / cached\s+2 / 0", r1.output), r1.output
    r2 = xenv.run(["-p", "/index"])
    assert re.search(r"parsed / cached\s+0 / 2", r2.output), r2.output
    assert not [p for p in xenv.workspace.rglob("*") if "index" in p.name]


def test_axon_map_subcommand(xenv: AxonEnv) -> None:
    """`axon map pkg` works without an LLM and limits the map to the path."""
    _code_project(xenv.workspace)
    r = xenv.run(["map", "pkg"])
    assert r.code == 0, str(r)
    assert "Engine" in r.output and "renderPage" not in r.output


# ─── /next /insights ────────────────────────────────────────────────────────

def test_next_suggests_verify_and_commit_after_edit_turn(xgit_env: AxonEnv) -> None:
    """After a turn that wrote a file (untested, git dirty) /next suggests /verify and /commit."""
    xgit_env.llm.enqueue([{"tool": "Write", "args": {"path": "feature.py", "content": "X = 1\n"}}], [{"text": "wrote it"}])
    assert xgit_env.run(["-p", "add feature.py"]).code == 0
    r = xgit_env.run(["-p", "--continue", "/next"])
    assert r.code == 0, str(r)
    assert "/verify" in r.output and "edited" in r.output, r.output
    assert "/commit" in r.output and "uncommitted" in r.output, r.output


def test_next_suggests_fix_after_failing_bash(xenv: AxonEnv) -> None:
    """A turn whose Bash test command failed -> /next proposes /fix (or /autofix)."""
    cmd = f'{sys.executable} -m pytest -q nonexistent_test_file.py'
    xenv.llm.enqueue([{"tool": "Bash", "args": {"command": cmd, "description": "run tests"}}], [{"text": "tests failed"}])
    assert xenv.run(["-p", "run the tests"]).code == 0
    r = xenv.run(["-p", "--continue", "/next"])
    assert "/fix" in r.output or "/autofix" in r.output, r.output


def test_insights_runs_analytics_subagent_on_real_usage(xenv: AxonEnv) -> None:
    """/insights hands the real usage numbers to an analytics subagent and prints its answer."""
    xenv.llm.enqueue([{"text": "hi"}, {"usage": {"input": 100_000, "output": 20_000, "cached": 40_000}}])
    assert xenv.run(["-p", "hello"]).code == 0

    @script_from_rules
    def analyst(req: Recorded):
        blob = req.all_text()
        if "0.400" in blob or "0.40" in blob:
            return [{"text": "INSIGHT: gpt-4o dominated spend at $0.40."}]
        return [{"text": "INSIGHT-MISSING-DATA"}]

    xenv.llm.default_script = analyst
    r = xenv.run(["-p", "--continue", "/insights"], timeout=90)
    assert r.code == 0, str(r)
    assert "INSIGHT: gpt-4o dominated spend" in r.output, r.output
    assert "analytics" in r.output.lower()


# ─── axon report (HTML) ─────────────────────────────────────────────────────

class _KPIParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.kpis: dict[str, str] = {}
        self._cls: list[str] = []
        self._label: str | None = None
        self._field: str | None = None

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "") or ""
        self._cls.append(cls)
        if cls in ("l", "v"):
            self._field = cls

    def handle_endtag(self, tag):
        if self._cls:
            self._cls.pop()
        self._field = None

    def handle_data(self, data):
        if not any("kpi" in c for c in self._cls):
            return
        if self._field == "l":
            self._label = data.strip()
        elif self._field == "v" and self._label:
            self.kpis.setdefault(self._label, data.strip())


def test_report_html_kpis_match_usage(xenv: AxonEnv) -> None:
    """`axon report --out f.html --days 0`: Total cost / LLM calls / Sessions KPIs equal the recorded usage."""
    from axon.features.analytics import fmt_usd
    xenv.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo r", "description": "r"}}, {"usage": {"input": 100_000, "output": 10_000}}],
        [{"text": "a"}, {"usage": {"input": 100_000, "output": 10_000, "cached": 50_000}}],
    )
    assert xenv.run(["-p", "first"]).code == 0
    xenv.llm.enqueue([{"text": "b"}, {"usage": {"input": 200_000, "output": 0}}])
    assert xenv.run(["-p", "second"]).code == 0
    recs = usage_records(xenv)
    total = sum(r["cost"] for r in recs)
    out = xenv.root / "report.html"
    r = xenv.run(["report", "--out", str(out), "--days", "0"])
    assert r.code == 0, str(r)
    assert "Report written" in r.output and f"{len(recs)} calls" in r.output
    p = _KPIParser()
    p.feed(out.read_text(encoding="utf-8"))
    assert p.kpis.get("Total cost") == fmt_usd(total), (p.kpis, total)
    assert p.kpis.get("LLM calls") == str(len(recs)) == "3"
    assert p.kpis.get("Sessions") == "2"
    assert p.kpis.get("Cache hit rate", "").endswith("%")


def test_report_with_no_usage_still_writes_html(xenv: AxonEnv) -> None:
    """With an empty usage log the report is still written and explains how to get data."""
    out = xenv.root / "empty.html"
    r = xenv.run(["report", "--out", str(out)])
    assert r.code == 0, str(r)
    text = out.read_text(encoding="utf-8")
    assert text.lstrip().lower().startswith("<!doctype html") and "usage.jsonl" in text
