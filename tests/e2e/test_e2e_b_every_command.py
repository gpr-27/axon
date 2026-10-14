"""E2E-B: "every command works".

The list of slash commands is built in-process from ``list_command_names`` (built-ins, feature
commands, bundled skills - exactly what /help and autocomplete show). Each command is then run
with no arguments through the real CLI as ``axon -p "/cmd"``:

* on a fresh, empty session, and
* on a populated session (``--continue`` after a turn that wrote a file via a tool call),

and must finish without a Python traceback, a swallowed handler exception (``✗ /x failed:``),
or an 'Error during' message. The runs are executed concurrently by a module fixture (each in
its own isolated AXON_HOME + workspace) so the sweep stays fast; one test per command reports.
A REPL (pty) sweep runs a representative subset inside a single interactive session.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fake_llm import FakeLLMServer
from harness import IS_WINDOWS, SRC, AxonEnv, _project_key, create_axon_env
from test_e2e_b_util import CMD_FAILED_RE, TRACEBACK, no_crash, repl_cmd, start_repl

pytestmark = pytest.mark.e2e

# Commands with side effects outside the sandbox (desktop notification, long-running server).
SKIP = {"/notify": "posts a real desktop notification", "/serve": "starts a blocking HTTP server"}


def _command_names() -> list[str]:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from axon.commands.registry import list_command_names
    ws = Path(tempfile.mkdtemp(prefix="axon-cmdlist-"))
    try:
        return [n for n, _ in list_command_names(ws)]
    finally:
        shutil.rmtree(ws, ignore_errors=True)


ALL_COMMANDS = _command_names()
BUILTIN_COMMANDS = [n for n in ALL_COMMANDS if n not in SKIP and ":" not in n]


def _bad(text: str) -> str | None:
    if TRACEBACK in text:
        return "traceback"
    if "Error during" in text:
        return "'Error during'"
    m = CMD_FAILED_RE.search(text)
    return f"handler raised ({m.group(0)})" if m else None


def _seed(root: Path, llm: FakeLLMServer) -> AxonEnv:
    env = create_axon_env(root, llm)
    llm.enqueue([{"text": "Seeding."}, {"tool": "Write", "args": {"path": "seed.py", "content": "print('seed')\n"}}],
                [{"text": "Seed done. ```python\nprint('seed')\n```"}])
    r = env.run(["-p", "create seed.py"])
    assert r.code == 0, str(r)
    return env


def _clone(seed: AxonEnv, root: Path, llm: FakeLLMServer) -> AxonEnv:
    """Copy the seeded home/workspace to ``root``, rewriting stored absolute paths."""
    env = create_axon_env(root, llm)
    shutil.rmtree(env.home)
    shutil.rmtree(env.workspace)
    shutil.copytree(seed.home, env.home)
    shutil.copytree(seed.workspace, env.workspace)
    projects = env.axon_home / "projects"
    old_key, new_key = _project_key(seed.workspace), _project_key(env.workspace)
    if (projects / old_key).exists():
        (projects / old_key).rename(projects / new_key)
    old_ws, new_ws = str(seed.workspace.resolve()), str(env.workspace.resolve())
    old_ws2, new_ws2 = str(seed.workspace), str(env.workspace)
    for p in env.home.rglob("*"):
        if p.is_file() and p.suffix in (".jsonl", ".json"):
            t = p.read_text(encoding="utf-8", errors="replace")
            t2 = t.replace(old_ws, new_ws).replace(old_ws2, new_ws2)
            if t2 != t:
                p.write_text(t2, encoding="utf-8")
    return env


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], object]:
    llm = FakeLLMServer(model_ids=("gpt-4o",)).start()
    base = tmp_path_factory.mktemp("sweep")
    results: dict[tuple[str, str], object] = {}
    try:
        seed = _seed(base / "_seed", llm)
        jobs: list[tuple[str, str]] = [("fresh", n) for n in ALL_COMMANDS if n not in SKIP]
        jobs += [("populated", n) for n in BUILTIN_COMMANDS]

        def work(job: tuple[str, str]):
            kind, name = job
            slug = f"{kind}-{name.strip('/').replace(':', '_')}"
            if kind == "fresh":
                env = create_axon_env(base / slug, llm)
                args = ["-p", name]
            else:
                env = _clone(seed, base / slug, llm)
                args = ["-p", "--continue", name]
            return job, env.run(args, timeout=60)

        with ThreadPoolExecutor(max_workers=max(4, min(10, (os.cpu_count() or 4)))) as ex:
            for job, res in ex.map(work, jobs):
                results[job] = res
        yield results
    finally:
        llm.stop()


def test_command_list_is_complete() -> None:
    """The in-process command list includes the core built-ins and bundled skills."""
    for must in ("/help", "/status", "/rewind", "/export", "/mcp", "/memory", "/init", "/config", "/model"):
        assert must in ALL_COMMANDS, must
    assert len(ALL_COMMANDS) >= 100, len(ALL_COMMANDS)
    assert len(set(ALL_COMMANDS)) == len(ALL_COMMANDS), "duplicate names in list_command_names"


def test_listed_skill_name_does_not_resolve_to_a_different_builtin() -> None:
    """Every non-built-in entry offered by autocomplete must not be captured by a built-in alias."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from axon.commands.registry import resolve_builtin, visible_commands
    builtin_names = {c.name for c in visible_commands()}
    clashes = [n for n in ALL_COMMANDS
               if n.strip("/") not in builtin_names and ":" not in n and resolve_builtin(n.strip("/")) is not None]
    assert clashes == [], clashes


@pytest.mark.parametrize("name", [n for n in ALL_COMMANDS if n not in SKIP])
def test_every_command_runs_fresh(sweep, name: str) -> None:
    """`axon -p "/<cmd>"` on an empty session finishes without a crash."""
    r = sweep[("fresh", name)]
    bad = _bad(r.output)
    assert bad is None, f"{name}: {bad}\n{r}"
    assert r.code == 0, str(r)


@pytest.mark.parametrize("name", BUILTIN_COMMANDS)
def test_every_command_runs_on_populated_session(sweep, name: str) -> None:
    """`axon -p --continue "/<cmd>"` after a tool-using turn finishes without a crash."""
    r = sweep[("populated", name)]
    bad = _bad(r.output)
    assert bad is None, f"{name}: {bad}\n{r}"
    assert r.code == 0, str(r)


# Commands that do not open a full-screen picker on a terminal (pickers are covered elsewhere).
REPL_SUBSET = ["/help", "/status", "/context", "/cost", "/trace", "/inspect", "/explain", "/stats", "/budget",
               "/tools", "/tasks", "/todos", "/keybindings", "/history", "/rewind points", "/rewind list",
               "/cost calls", "/payload", "/goal", "/queue"]
PROMPT_READY = r"Ask anything"


@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_command_sweep(axon_env: AxonEnv) -> None:
    """Inside one interactive REPL, a turn followed by 20 commands: each returns to the prompt
    without a crash."""
    axon_env.llm.enqueue([{"tool": "Write", "args": {"path": "r.txt", "content": "r\n"}}], [{"text": "SWEEP-READY"}])
    with start_repl(axon_env) as s:
        repl_cmd(s, "make r.txt", "SWEEP-READY", 25)
        for cmd in REPL_SUBSET:
            m = s.mark()
            s.sendline(cmd)
            try:
                s.expect(PROMPT_READY, timeout=6, since=m)
            except TimeoutError:  # a picker opened: Esc must cancel it and give the prompt back
                m2 = s.mark()
                s.send("\x1b")
                try:
                    s.expect(PROMPT_READY, timeout=10, since=m2)
                except TimeoutError:
                    raise AssertionError(f"{cmd} did not return to the prompt:\n{s.screen[m:][-2500:]}")
            s.drain(0.2)
            out = s.screen[m:]
            assert _bad(out) is None, f"{cmd}: {_bad(out)}\n{out[-2500:]}"
        s.sendline("/exit")
        assert s.wait(10) == 0, s.screen[-2000:]
    no_crash(s.screen)
