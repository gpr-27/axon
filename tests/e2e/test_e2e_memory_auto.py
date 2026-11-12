"""Automatic project memory end to end: state a durable fact in one session, start a new session in the same
folder, and the fake LLM receives it in the request. Then /memory edit, forget and auto toggle."""
from __future__ import annotations

from harness import AxonEnv


def _auto_dir(env: AxonEnv):
    from harness import _project_key
    return env.axon_home / "projects" / _project_key(env.workspace) / "memory"


def test_session_one_saves_session_two_reads_back(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "noted"}])
    r1 = axon_env.run(["-p", "We use pytest, not unittest. Never push to main without asking."])
    assert r1.code == 0, str(r1)
    notes = _auto_dir(axon_env) / "conventions.md"
    assert notes.is_file(), list(_auto_dir(axon_env).glob("*"))
    text = notes.read_text(encoding="utf-8")
    assert "pytest, not unittest" in text and "Never push to main" in text
    assert "conventions.md" in (_auto_dir(axon_env) / "MEMORY.md").read_text(encoding="utf-8")
    assert "memory saved" in r1.output, r1.output

    axon_env.llm.enqueue([{"text": "hello"}])
    r2 = axon_env.run(["-p", "hello there"])
    assert r2.code == 0, str(r2)
    last = axon_env.llm.completion_requests()[-1]
    assert "pytest, not unittest" in last.all_text() and "Never push to main" in last.all_text()
    assert "Auto memory" in last.all_text()


def test_other_folder_does_not_see_it_and_off_switch_saves_nothing(axon_env: AxonEnv, tmp_path) -> None:
    axon_env.llm.enqueue([{"text": "ok"}])
    axon_env.run(["-p", "Remember that deploys go through make release"], env={"AXON_DISABLE_AUTO_MEMORY": "1"})
    assert not (_auto_dir(axon_env) / "project-facts.md").exists()
    axon_env.llm.enqueue([{"text": "ok"}])
    axon_env.run(["-p", "Remember that deploys go through make release"])
    assert (_auto_dir(axon_env) / "project-facts.md").is_file()
    other = tmp_path / "elsewhere"
    other.mkdir()
    axon_env.llm.enqueue([{"text": "ok"}])
    axon_env.run(["-p", "hi"], cwd=other)
    assert "make release" not in axon_env.llm.completion_requests()[-1].all_text()


def test_memory_commands_non_interactive(axon_env: AxonEnv) -> None:
    r = axon_env.run(["-p", "/memory add project we format with ruff"])
    assert "Remembered in Project" in r.output, str(r)
    assert "ruff" in (axon_env.workspace / "AXON.md").read_text(encoding="utf-8")
    r = axon_env.run(["-p", "/memory add local my token budget is small"])
    assert "AXON.local.md" in (axon_env.workspace / ".gitignore").read_text(encoding="utf-8")
    r = axon_env.run(["-p", "/memory forget token budget"])
    assert "Forgot 1 line" in r.output, str(r)
    r = axon_env.run(["-p", "/memory auto off"])
    assert "Auto memory off" in r.output
    axon_env.llm.enqueue([{"text": "ok"}])
    axon_env.run(["-p", "We use pytest, not unittest."])
    assert not (_auto_dir(axon_env) / "conventions.md").exists()
