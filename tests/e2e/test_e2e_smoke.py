"""
End-to-end smoke tests: the real `axon` CLI (subprocess) against a local fake LLM server.

Run only these:  .venv/bin/python -m pytest -q -p no:cacheprovider -m e2e tests/e2e
"""
from __future__ import annotations

import json
import os

import pytest

from fake_llm import Recorded, script_from_rules
from harness import IS_WINDOWS, AxonEnv

pytestmark = pytest.mark.e2e

TRACEBACK = "Traceback (most recent call last)"


def _no_traceback(r) -> None:
    assert TRACEBACK not in r.stdout and TRACEBACK not in r.stderr, str(r)


def _has_tool_result(substr: str = ""):
    def pred(req: Recorded):
        res = req.tool_results()
        if not res:
            return "no tool result in request"
        if substr and not any(substr in t for t in res):
            return f"no tool result containing {substr!r}: {res!r}"
        return True
    pred.__name__ = f"has_tool_result({substr!r})"
    return pred


# ─── CLI basics (no LLM) ────────────────────────────────────────────────────

def test_version(axon_env: AxonEnv) -> None:
    import axon
    r = axon_env.run(["--version"])
    assert r.code == 0, str(r)
    assert axon.__version__ in r.output
    assert axon_env.llm.requests == []


def test_help(axon_env: AxonEnv) -> None:
    r = axon_env.run(["--help"])
    assert r.code == 0, str(r)
    for flag in ("--print", "--output-format", "--continue", "--mode"):
        assert flag in r.output
    _no_traceback(r)


def test_doctor(axon_env: AxonEnv) -> None:
    r = axon_env.run(["doctor", "--no-agent"])
    _no_traceback(r)
    assert "Axon doctor" in r.output, str(r)
    assert str(axon_env.llm.url) in r.output  # base_url from env is reported
    assert "API key" in r.output and "set" in r.output


# ─── print mode, both wire formats ──────────────────────────────────────────

def test_print_simple_answer_openai_wire(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue({"steps": [{"text": "The answer is forty-two."}],
                          "expect": [lambda q: "What is the answer" in q.last_user_text() or "prompt missing"]})
    r = axon_env.run(["-p", "What is the answer?"])
    assert r.code == 0, str(r)
    assert "The answer is forty-two." in r.output
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 1 and reqs[0].wire == "openai"
    assert reqs[0].body["stream"] is True
    assert reqs[0].headers.get("authorization") == "Bearer sk-e2e-test-key"
    assert "Axon" in reqs[0].system_text
    assert {"Read", "Write", "Bash"} <= set(reqs[0].tool_names)
    assert not axon_env.llm.failures


def test_print_simple_answer_anthropic_wire(axon_env: AxonEnv) -> None:
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"text": "Hello from the Anthropic wire."}])
    r = axon_env.run(["-p", "hi"])
    assert r.code == 0, str(r)
    assert "Hello from the Anthropic wire." in r.output
    reqs = axon_env.llm.completion_requests()
    assert reqs and all(q.wire == "anthropic" for q in reqs), [q.path for q in axon_env.llm.requests]
    assert reqs[0].body["model"] == "claude-sonnet-4-6"
    assert "Axon" in reqs[0].system_text
    assert reqs[0].headers.get("x-api-key") == "sk-e2e-test-key" or \
        reqs[0].headers.get("authorization") == "Bearer sk-e2e-test-key"


def test_append_system_prompt_reaches_provider(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue({"steps": [{"text": "ack"}],
                          "expect": [lambda q: "ZEBRA-MARKER-7" in q.system_text or "marker not in system prompt"]})
    r = axon_env.run(["-p", "--append-system-prompt", "ZEBRA-MARKER-7", "hello"])
    assert r.code == 0, str(r)
    assert not axon_env.llm.failures, axon_env.llm.failures


# ─── tools ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_tool_round_trip_write_then_read(axon_env: AxonEnv, wire: str) -> None:
    if wire == "anthropic":
        axon_env.use_anthropic_wire()
    axon_env.llm.enqueue(
        [{"text": "Creating the file."}, {"tool": "Write", "args": {"path": "notes/hello.txt", "content": "e2e content 123\n"}}],
        {"steps": [{"tool": "Read", "args": {"path": "notes/hello.txt"}}], "expect": [_has_tool_result()]},
        {"steps": [{"text": "Done: file written and verified."}], "expect": [_has_tool_result("e2e content 123")]},
    )
    r = axon_env.run(["-p", "create notes/hello.txt"])
    assert r.code == 0, str(r)
    target = axon_env.workspace / "notes" / "hello.txt"
    assert target.is_file(), str(r)
    assert target.read_text(encoding="utf-8") == "e2e content 123\n"
    assert "Done: file written and verified." in r.output
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert len(axon_env.llm.completion_requests()) == 3


def test_output_format_json(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo json-e2e", "description": "echo"}},
         {"usage": {"input": 1200, "output": 30, "cached": 1000}}],
        {"steps": [{"text": "final json answer"}], "expect": [_has_tool_result("json-e2e")]},
    )
    r = axon_env.run(["-p", "--output-format", "json", "run echo"])
    assert r.code == 0, str(r)
    data = r.json()
    assert data["type"] == "result"
    assert data["result"].strip() == "final json answer"
    assert data["is_error"] is False
    assert data["tool_calls"] == 1
    assert data["iterations"] == 2
    assert isinstance(data["cost_usd"], (int, float))
    u = data["usage"]
    assert u["input_tokens"] >= 1200 and u["output_tokens"] >= 30
    assert u["cache_read_tokens"] >= 1000
    assert data["session_id"]


def test_output_format_stream_json(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(
        [{"tool": "Bash", "args": {"command": "echo streamed", "description": "echo"}}],
        [{"text": "stream done"}],
    )
    r = axon_env.run(["-p", "--output-format", "stream-json", "go"])
    assert r.code == 0, str(r)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]  # every non-empty line must be JSON
    types = [e.get("type") for e in events]
    assert types[-1] == "result", types
    assert events[-1]["result"].strip() == "stream done"
    assert any(t in ("tool_use", "tool_call", "tool_start") for t in types), types
    assert any(t in ("tool_result",) for t in types), types


# ─── provider errors ────────────────────────────────────────────────────────

def test_429_is_retried_then_succeeds(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"error": 429, "retry_after": 0}], [{"text": "recovered after retry"}])
    r = axon_env.run(["-p", "hi"], timeout=30)
    assert r.code == 0, str(r)
    assert "recovered after retry" in r.output
    assert len(axon_env.llm.completion_requests()) == 2
    assert r.duration < 15


def test_401_gives_friendly_error(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"error": 401, "message": "invalid api key"}])
    r = axon_env.run(["-p", "hi"], timeout=30)
    assert r.code != 0, str(r)
    _no_traceback(r)
    low = r.output.lower()
    assert "401" in low or "api key" in low or "auth" in low, str(r)
    assert len(axon_env.llm.completion_requests()) == 1  # 401 must not be retried


# ─── permissions ────────────────────────────────────────────────────────────

def test_plan_mode_blocks_write(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(
        [{"tool": "Write", "args": {"path": "blocked.txt", "content": "nope"}}],
        [{"text": "ok, not writing"}],
    )
    r = axon_env.run(["-p", "--mode", "plan", "write blocked.txt"])
    _no_traceback(r)
    assert not (axon_env.workspace / "blocked.txt").exists(), str(r)
    reqs = axon_env.llm.completion_requests()
    if len(reqs) >= 2:  # the refusal must be reported back to the model as a tool result
        assert reqs[1].tool_results(), reqs[1].messages


def test_bypass_mode_allows_bash(axon_env: AxonEnv) -> None:
    cmd = "echo bypass-ok > bash_out.txt" if not IS_WINDOWS else "echo bypass-ok | Out-File -Encoding utf8 bash_out.txt"
    axon_env.llm.enqueue(
        [{"tool": "Bash", "args": {"command": cmd, "description": "write a file"}}],
        [{"text": "bash ran"}],
    )
    r = axon_env.run(["-p", "--mode", "bypass", "run it"])
    assert r.code == 0, str(r)
    out = axon_env.workspace / "bash_out.txt"
    assert out.is_file(), str(r)
    assert "bypass-ok" in out.read_text(encoding="utf-8", errors="replace")


# ─── sessions ───────────────────────────────────────────────────────────────

def test_session_persisted_under_axon_home_not_workspace(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "remember me"}])
    r = axon_env.run(["-p", "--output-format", "json", "persist this"])
    assert r.code == 0, str(r)
    sid = r.json()["session_id"]
    sdir = axon_env.sessions_dir()
    assert sdir.is_dir(), f"missing {sdir}; axon_home tree: {sorted(str(p) for p in axon_env.axon_home.rglob('*'))[:40]}"
    files = [p for p in sdir.rglob("*") if p.is_file()]
    assert files, f"no session files in {sdir}"
    assert any(sid in str(p) for p in files), (sid, files)
    blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files if p.suffix in (".jsonl", ".json"))
    assert "persist this" in blob and "remember me" in blob
    leaked = [str(p.relative_to(axon_env.workspace)) for p in axon_env.workspace.rglob("*")]
    assert leaked == [], f"axon wrote into the workspace: {leaked}"


def test_continue_resumes_previous_conversation(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "First answer: the code word is PELICAN."}])
    r1 = axon_env.run(["-p", "first question"])
    assert r1.code == 0, str(r1)
    axon_env.llm.enqueue({"steps": [{"text": "You said PELICAN."}],
                          "expect": [lambda q: ("first question" in q.all_text() and "PELICAN" in q.all_text())
                                     or "prior messages missing from resumed request"]})
    r2 = axon_env.run(["-p", "--continue", "what was the code word?"])
    assert r2.code == 0, str(r2)
    assert not axon_env.llm.failures, axon_env.llm.failures
    assert "You said PELICAN." in r2.output
    last = axon_env.llm.completion_requests()[-1]
    roles = [m.get("role") for m in last.messages if m.get("role") != "system"]
    assert roles[:3] == ["user", "assistant", "user"], roles


def test_dynamic_rules_script(axon_env: AxonEnv) -> None:
    @script_from_rules
    def echo(req: Recorded):
        return [{"text": f"ECHO<{req.last_user_text().strip()}>"}]

    axon_env.llm.default_script = echo
    r = axon_env.run(["-p", "dynamic-12345"])
    assert r.code == 0, str(r)
    assert "ECHO<dynamic-12345>" in r.output


# ─── interactive REPL via pty ───────────────────────────────────────────────

@pytest.mark.skipif(IS_WINDOWS, reason="pty is POSIX-only")
def test_repl_prompt_help_exit(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"text": "REPL-ANSWER-OK"}])
    with axon_env.pty() as s:
        s.expect(r"(?i)(axon|❯|>)", timeout=15)
        s.drain(0.5)
        m = s.mark()
        s.sendline("hello from pty")
        s.expect("REPL-ANSWER-OK", timeout=20, since=m)
        s.drain(0.5)
        m = s.mark()
        s.sendline("/help")
        s.expect(r"/exit|/clear|/model", timeout=10, since=m)
        s.drain(0.5)
        s.sendline("/exit")
        code = s.wait(10)
        assert code is not None, "REPL did not exit on /exit:\n" + s.screen[-2000:]
        assert code == 0, s.screen[-2000:]
        assert TRACEBACK not in s.screen
    assert axon_env.llm.completion_requests(), "REPL never called the LLM"


# ─── additional scenarios (retry exhaustion, limits, stdin, cache usage) ────

def test_persistent_500_exhausts_retries(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(*[[{"error": 500, "retry_after": 0}]] * 4)
    r = axon_env.run(["-p", "--output-format", "json", "hi"], timeout=30)
    assert r.code == 1, str(r)
    data = r.json()
    assert data["is_error"] is True and data["stop_reason"] == "error"
    assert "500" in data["result"]
    assert len(axon_env.llm.completion_requests()) == 4  # 1 try + 3 retries


def test_max_turns_stops_tool_loop(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue(*[[{"tool": "Bash", "args": {"command": "echo loop", "description": "loop"}}]] * 5)
    r = axon_env.run(["-p", "--max-turns", "2", "--output-format", "json", "loop forever"])
    assert r.code == 1, str(r)
    data = r.json()
    assert data["stop_reason"] == "max_iterations" and data["iterations"] == 2
    assert len(axon_env.llm.completion_requests()) == 2


def test_prompt_from_piped_stdin(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue({"steps": [{"text": "got stdin"}],
                          "expect": [lambda q: "PIPED-INPUT-99" in q.last_user_text() or "stdin not forwarded"]})
    r = axon_env.run(["-p"], input="PIPED-INPUT-99")
    assert r.code == 0, str(r)
    assert "got stdin" in r.output
    assert not axon_env.llm.failures, axon_env.llm.failures


def test_anthropic_cache_usage_is_accounted(axon_env: AxonEnv) -> None:
    axon_env.use_anthropic_wire()
    axon_env.llm.enqueue([{"text": "cached answer"}, {"usage": {"input": 5000, "output": 20, "cached": 4000}}])
    r = axon_env.run(["-p", "--output-format", "json", "hi"])
    assert r.code == 0, str(r)
    u = r.json()["usage"]
    assert u["input_tokens"] == 5000 and u["cache_read_tokens"] == 4000 and u["output_tokens"] == 20, u
    assert r.json()["cost_usd"] > 0


# ─── regressions for bugs found by the harness ─────────────────────────────

def test_failed_turn_footer_is_not_marked_done(axon_env: AxonEnv) -> None:
    axon_env.llm.enqueue([{"error": 401}])
    r = axon_env.run(["-p", "hi"])
    assert r.code == 1, str(r)
    assert "✓ Done" not in r.output, str(r)


def test_doctor_honours_axon_home(axon_env: AxonEnv, tmp_path) -> None:
    alt = tmp_path / "alt-axon-home"
    alt.mkdir()
    (alt / "config.toml").write_text('model = "gpt-4o"\n', encoding="utf-8")
    r = axon_env.run(["doctor", "--no-agent"], env={"AXON_HOME": str(alt)})
    line = next((ln for ln in r.output.splitlines() if "User config" in ln), "")
    assert str(alt) in line and "found" in line, line
