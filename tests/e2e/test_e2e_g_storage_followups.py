"""E2E-G: regressions for failed-turn persistence, /retry and /output after a resume, --debug mcp,
/tools exact match and JSON errors without an API key."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import AxonEnv
from test_e2e_b_util import no_crash, write_file
from test_e2e_c_support import env_without_keys, ext_llm, run_raw, xenv  # noqa: F401

pytestmark = pytest.mark.e2e

FAKE_MCP = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def _chat(req) -> list[dict]:  # noqa: ANN001
    return [m for m in req.messages if m.get("role") != "system"]


def _fail_first_turn(env: AxonEnv) -> None:
    env.llm.enqueue({"error": 500}, {"error": 500}, {"error": 500}, {"error": 500})
    r = env.run(["-p", "first q"])
    no_crash(r)
    assert "Provider error" in r.output, str(r)


def test_failed_first_turn_is_not_replayed_after_continue(axon_env: AxonEnv) -> None:
    _fail_first_turn(axon_env)
    axon_env.llm.enqueue([{"text": "second ok"}])
    r = axon_env.run(["-p", "--continue", "x"])
    assert r.code == 0 and "second ok" in r.stdout, str(r)
    msgs = _chat(axon_env.llm.completion_requests()[-1])
    assert len(msgs) == 1, msgs
    assert "Provider error" not in json.dumps(msgs) and "first q" not in json.dumps(msgs)
    # nothing is lost: the transcript still records the failed prompt
    text = "".join(p.read_text(encoding="utf-8") for p in axon_env.sessions_dir().glob("*.jsonl"))
    assert '"turn_failed"' in text and "first q" in text


def test_retry_after_continue_resends_failed_prompt(axon_env: AxonEnv) -> None:
    _fail_first_turn(axon_env)
    axon_env.llm.enqueue([{"text": "retried fine"}])
    r = axon_env.run(["-p", "--continue", "/retry"])
    no_crash(r)
    assert "Nothing to retry" not in r.output, str(r)
    assert "retried fine" in r.output, str(r)
    msgs = _chat(axon_env.llm.completion_requests()[-1])
    assert len(msgs) == 1 and "first q" in json.dumps(msgs), msgs


def test_output_after_continue_reads_recorded_tool_results(axon_env: AxonEnv) -> None:
    write_file(axon_env.workspace / "notes.txt", "alpha-line\nbeta-line\n")
    axon_env.llm.enqueue([{"tool": "Read", "args": {"path": "notes.txt"}}], [{"text": "read it"}])
    assert axon_env.run(["-p", "read notes"]).code == 0
    r = axon_env.run(["-p", "--continue", "/output list"])
    no_crash(r)
    assert "Read" in r.output and "notes.txt" in r.output, str(r)
    r = axon_env.run(["-p", "--continue", "/output 1"])
    assert "beta-line" in r.output, str(r)
    r = axon_env.run(["-p", "--continue", "/output"])
    assert "alpha-line" in r.output, str(r)


def test_debug_mcp_logs_server_lifecycle_and_calls(axon_env: AxonEnv) -> None:
    write_file(axon_env.workspace / ".mcp.json", json.dumps({"mcpServers": {
        "srv": {"command": sys.executable, "args": [FAKE_MCP, "--name", "srv", "--log", str(axon_env.root / "m.log")]}}}))
    axon_env.llm.enqueue([{"tool": "mcp__srv__echo", "args": {"text": "dbg-echo"}}], [{"text": "done"}])
    log = axon_env.root / "mcp-debug.log"
    r = axon_env.run(["-p", "--debug", "mcp", "--debug-file", str(log), "echo please"])
    assert r.code == 0, str(r)
    text = log.read_text(encoding="utf-8")
    assert "axon.mcp" in text, text
    assert "mcp srv: starting" in text and "connected" in text and "call echo" in text, text


def test_tools_exact_name_does_not_list_prefix_matches(axon_env: AxonEnv) -> None:
    def headers(out: str) -> list[str]:
        return [ln.split()[0] for ln in out.splitlines() if ln.startswith("  ") and not ln.startswith("   ")
                and ("default" in ln) and ln.split()]

    r = axon_env.run(["-p", "/tools Bash"])
    no_crash(r)
    assert headers(r.output) == ["Bash"], str(r)
    r = axon_env.run(["-p", "/tools BashO"])  # prefix match when no tool has that exact name
    assert headers(r.output) == ["BashOutput"], str(r)


@pytest.mark.parametrize("fmt", ["json", "stream-json"])
def test_missing_key_prints_json_error(xenv: AxonEnv, fmt: str) -> None:  # noqa: F811
    env = env_without_keys(xenv, AXON_PROVIDER="xai", AXON_MODEL="grok-4")
    env.pop("AXON_BASE_URL", None)
    r = run_raw(env, ["-p", "--output-format", fmt, "hi"], xenv.workspace, timeout=30)
    assert r.code == 2, str(r)
    data = json.loads(r.stdout.strip().splitlines()[0] if fmt == "stream-json" else r.stdout)
    assert data["type"] == "error" and data["is_error"] is True and "API key" in data["error"]
