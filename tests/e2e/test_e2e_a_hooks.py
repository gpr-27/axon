"""
E2E (agent A): lifecycle hooks configured in <ws>/.axon/settings.json and run as real python scripts
(or a local HTTP endpoint): PreToolUse (exit 2, JSON permissionDecision, updatedInput, matchers),
PostToolUse additionalContext, UserPromptSubmit block/context, Stop continuation, SessionStart context,
http hooks, failure tolerance, disableAllHooks.
"""
from __future__ import annotations

import json

import pytest

from harness import IS_WINDOWS, AxonEnv
from test_e2e_a_common import final, local_http, no_traceback, perms, py_hook, results, run_tools, write_settings

pytestmark = pytest.mark.e2e
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX shell commands in tool calls")

LOG_PAYLOAD = "d = json.load(sys.stdin)\nopen(sys.argv[1], 'a', encoding='utf-8').write(json.dumps(d) + '\\n')\n"


def _ok(r) -> None:
    assert r.code == 0, str(r)
    no_traceback(r)


def _hooks(env: AxonEnv, cfg: dict) -> None:
    write_settings(env, {"hooks": cfg})


def _log(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


# ─── PreToolUse ─────────────────────────────────────────────────────────────

@posix_only
def test_pretooluse_exit2_blocks_and_feeds_stderr_to_model(axon_env: AxonEnv) -> None:
    """A PreToolUse hook exiting 2 blocks the Bash call (not executed) and its stderr reaches the model."""
    h = py_hook(axon_env, "pre.py", "d = json.load(sys.stdin)\nsys.stderr.write('HOOK-DENIED-XYZ')\nsys.exit(2)\n")
    _hooks(axon_env, {"PreToolUse": [{"matcher": "Bash", "hooks": [h]}]})
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch hooked.txt", "description": "x"})])
    _ok(r)
    res = results(ev, "Bash")[0]
    assert res["is_error"] and "HOOK-DENIED-XYZ" in res["content"]
    assert not (axon_env.workspace / "hooked.txt").exists()
    assert any("HOOK-DENIED-XYZ" in t for t in axon_env.llm.completion_requests()[1].tool_results())


@posix_only
def test_pretooluse_json_permission_decision_deny(axon_env: AxonEnv) -> None:
    """hookSpecificOutput.permissionDecision=deny (exit 0) denies the call with the given reason, even in bypass."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'deny', 'permissionDecisionReason': 'JSON-DENY-REASON'}}))\n")
    _hooks(axon_env, {"PreToolUse": [{"matcher": "Bash", "hooks": [py_hook(axon_env, "pre.py", body)]}]})
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch j.txt", "description": "x"})], "--mode", "bypass")
    _ok(r)
    assert "JSON-DENY-REASON" in results(ev, "Bash")[0]["content"]
    assert not (axon_env.workspace / "j.txt").exists()


@posix_only
def test_pretooluse_json_permission_decision_allow_in_default_mode(axon_env: AxonEnv) -> None:
    """permissionDecision=allow approves a Bash call that default mode would otherwise deny without a TTY."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'allow'}}))\n")
    _hooks(axon_env, {"PreToolUse": [{"matcher": "Bash", "hooks": [py_hook(axon_env, "pre.py", body)]}]})
    r, ev = run_tools(axon_env, [("Bash", {"command": "touch allowed.txt", "description": "x"})], "--mode", "default")
    _ok(r)
    assert perms(ev, "Bash")[0]["outcome"] == "allow"
    assert (axon_env.workspace / "allowed.txt").exists()


def test_pretooluse_allow_cannot_override_deny_rule(axon_env: AxonEnv) -> None:
    """A hook 'allow' must not override an explicit deny rule from settings."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'allow'}}))\n")
    write_settings(axon_env, {"permissions": {"deny": ["Write(locked.txt)"]},
                              "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [py_hook(axon_env, "pre.py", body)]}]}})
    r, ev = run_tools(axon_env, [("Write", {"path": "locked.txt", "content": "x"})], "--mode", "default")
    _ok(r)
    assert not (axon_env.workspace / "locked.txt").exists()


@posix_only
def test_pretooluse_updated_input_rewrites_command(axon_env: AxonEnv) -> None:
    """updatedInput from a PreToolUse hook replaces the tool arguments before execution."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', "
            "'permissionDecision': 'allow', 'updatedInput': {'command': 'echo REWRITTEN-BY-HOOK', 'description': 'x'}}}))\n")
    _hooks(axon_env, {"PreToolUse": [{"matcher": "Bash", "hooks": [py_hook(axon_env, "pre.py", body)]}]})
    r, ev = run_tools(axon_env, [("Bash", {"command": "echo original", "description": "x"})])
    _ok(r)
    c = results(ev, "Bash")[0]["content"]
    assert "REWRITTEN-BY-HOOK" in c and "original" not in c


def test_pretooluse_payload_and_matcher(axon_env: AxonEnv) -> None:
    """The hook receives the documented JSON payload, and a 'Write|Edit' matcher does not fire for Read."""
    log = axon_env.root / "pre.jsonl"
    _hooks(axon_env, {"PreToolUse": [{"matcher": "Write|Edit", "hooks": [py_hook(axon_env, "pre.py", LOG_PAYLOAD, str(log))]}]})
    (axon_env.workspace / "r.txt").write_text("r\n", encoding="utf-8")
    r, ev = run_tools(axon_env, [("Read", {"path": "r.txt"}), ("Write", {"path": "w.txt", "content": "W\n"})])
    _ok(r)
    entries = _log(log)
    assert [e["tool_name"] for e in entries] == ["Write"]
    e = entries[0]
    assert e["hook_event_name"] == "PreToolUse" and e["tool_input"] == {"path": "w.txt", "content": "W\n"}
    assert e["session_id"] and e["cwd"] in (str(axon_env.workspace.resolve()), str(axon_env.workspace))
    assert e["permission_mode"] == "bypassPermissions"
    assert (axon_env.workspace / "w.txt").exists()


def test_hook_env_has_project_dir(axon_env: AxonEnv) -> None:
    """Hook commands get CLAUDE_PROJECT_DIR / AXON_PROJECT_DIR pointing at the workspace."""
    log = axon_env.root / "env.txt"
    body = ("sys.stdin.read()\nopen(sys.argv[1], 'w').write(os.environ.get('CLAUDE_PROJECT_DIR', '') + '|' + "
            "os.environ.get('AXON_PROJECT_DIR', ''))\n")
    _hooks(axon_env, {"PreToolUse": [{"hooks": [py_hook(axon_env, "env.py", body, str(log))]}]})
    r, ev = run_tools(axon_env, [("Ls", {})])
    _ok(r)
    a, b = log.read_text().split("|")
    assert a and a == b and a.endswith("workspace")


# ─── PostToolUse ────────────────────────────────────────────────────────────

def test_posttooluse_additional_context_reaches_model(axon_env: AxonEnv) -> None:
    """PostToolUse hookSpecificOutput.additionalContext is appended to the tool result sent to the model."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', "
            "'additionalContext': 'POST-CTX-123'}}))\n")
    _hooks(axon_env, {"PostToolUse": [{"matcher": "Ls", "hooks": [py_hook(axon_env, "post.py", body)]}]})
    r, ev = run_tools(axon_env, [("Ls", {})])
    _ok(r)
    assert any("POST-CTX-123" in t for t in axon_env.llm.completion_requests()[1].tool_results())


def test_posttooluse_receives_tool_response(axon_env: AxonEnv) -> None:
    """PostToolUse payload includes the tool's input and its response."""
    log = axon_env.root / "post.jsonl"
    (axon_env.workspace / "f.txt").write_text("POST-PAYLOAD-CONTENT\n", encoding="utf-8")
    _hooks(axon_env, {"PostToolUse": [{"matcher": "Read", "hooks": [py_hook(axon_env, "post.py", LOG_PAYLOAD, str(log))]}]})
    r, ev = run_tools(axon_env, [("Read", {"path": "f.txt"})])
    _ok(r)
    e = _log(log)[0]
    assert e["hook_event_name"] == "PostToolUse" and e["tool_name"] == "Read"
    assert "POST-PAYLOAD-CONTENT" in json.dumps(e.get("tool_response"))


def test_posttooluse_exit2_feedback_reaches_model(axon_env: AxonEnv) -> None:
    """A PostToolUse hook exiting 2 (e.g. a failing linter) feeds its stderr back to the model."""
    body = "json.load(sys.stdin)\nsys.stderr.write('LINT-FAILED-E501')\nsys.exit(2)\n"
    _hooks(axon_env, {"PostToolUse": [{"matcher": "Write", "hooks": [py_hook(axon_env, "post.py", body)]}]})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.py", "content": "x=1\n"})])
    _ok(r)
    assert any("LINT-FAILED-E501" in t for t in axon_env.llm.completion_requests()[1].tool_results())
    assert (axon_env.workspace / "w.py").exists()  # PostToolUse cannot undo the write


# ─── UserPromptSubmit ───────────────────────────────────────────────────────

def test_user_prompt_submit_block_prevents_llm_call(axon_env: AxonEnv) -> None:
    """UserPromptSubmit exit 2 blocks the prompt: no LLM request is made and the run reports 'blocked'."""
    body = "d = json.load(sys.stdin)\nif 'forbidden' in d['prompt']:\n    sys.stderr.write('PROMPT-POLICY'); sys.exit(2)\n"
    _hooks(axon_env, {"UserPromptSubmit": [{"hooks": [py_hook(axon_env, "ups.py", body)]}]})
    r = axon_env.run(["-p", "--output-format", "json", "a forbidden request"])
    no_traceback(r)
    d = r.json()
    assert d["stop_reason"] == "blocked" and "PROMPT-POLICY" in d["result"]
    assert axon_env.llm.completion_requests() == []


def test_user_prompt_submit_json_block(axon_env: AxonEnv) -> None:
    """UserPromptSubmit JSON {decision: block, reason} also blocks."""
    body = "json.load(sys.stdin)\nprint(json.dumps({'decision': 'block', 'reason': 'JSON-BLOCK-R'}))\n"
    _hooks(axon_env, {"UserPromptSubmit": [{"hooks": [py_hook(axon_env, "ups.py", body)]}]})
    r = axon_env.run(["-p", "--output-format", "json", "anything"])
    assert r.json()["stop_reason"] == "blocked" and "JSON-BLOCK-R" in r.json()["result"]
    assert axon_env.llm.completion_requests() == []


def test_user_prompt_submit_plain_stdout_is_context(axon_env: AxonEnv) -> None:
    """Plain stdout of a UserPromptSubmit hook is added to the user's message as context."""
    _hooks(axon_env, {"UserPromptSubmit": [{"hooks": [py_hook(axon_env, "ups.py", "json.load(sys.stdin)\nprint('UPS-CTX-456')\n")]}]})
    axon_env.llm.enqueue([{"text": "ok"}])
    r = axon_env.run(["-p", "my question"])
    _ok(r)
    u = axon_env.llm.completion_requests()[0].last_user_text()
    assert "my question" in u and "UPS-CTX-456" in u


def test_user_prompt_submit_additional_context_json(axon_env: AxonEnv) -> None:
    """hookSpecificOutput.additionalContext from UserPromptSubmit reaches the model."""
    body = ("json.load(sys.stdin)\nprint(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', "
            "'additionalContext': 'UPS-JSON-CTX'}}))\n")
    _hooks(axon_env, {"UserPromptSubmit": [{"hooks": [py_hook(axon_env, "ups.py", body)]}]})
    axon_env.llm.enqueue([{"text": "ok"}])
    _ok(axon_env.run(["-p", "q"]))
    assert "UPS-JSON-CTX" in axon_env.llm.completion_requests()[0].all_text()


# ─── Stop / SessionStart / SessionEnd ───────────────────────────────────────

def test_stop_hook_forces_one_continuation(axon_env: AxonEnv) -> None:
    """A Stop hook exiting 2 makes the agent continue once with the feedback; the second Stop
    (stop_hook_active=true) is respected so the loop cannot spin forever."""
    log = axon_env.root / "stop.jsonl"
    body = LOG_PAYLOAD + "sys.stderr.write('KEEP-GOING-STOP')\nsys.exit(2)\n"  # always blocks
    _hooks(axon_env, {"Stop": [{"hooks": [py_hook(axon_env, "stop.py", body, str(log))]}]})
    axon_env.llm.enqueue([{"text": "first stop"}], [{"text": "second stop"}], [{"text": "third (must not happen)"}])
    r = axon_env.run(["-p", "--output-format", "json", "go"])
    _ok(r)
    reqs = axon_env.llm.completion_requests()
    assert len(reqs) == 2
    assert "KEEP-GOING-STOP" in reqs[1].last_user_text()
    assert r.json()["result"].strip() == "second stop"
    entries = _log(log)
    assert [e["stop_hook_active"] for e in entries] == [False, True]
    assert entries[0]["last_assistant_message"] == "first stop"


def test_session_start_context_in_system_prompt(axon_env: AxonEnv) -> None:
    """SessionStart stdout is injected into the system prompt as session context."""
    _hooks(axon_env, {"SessionStart": [{"hooks": [py_hook(axon_env, "ss.py", "json.load(sys.stdin)\nprint('SESSION-CTX-789')\n")]}]})
    axon_env.llm.enqueue([{"text": "ok"}])
    _ok(axon_env.run(["-p", "hi"]))
    assert "SESSION-CTX-789" in axon_env.llm.completion_requests()[0].system_text


def test_session_start_source_startup_vs_resume(axon_env: AxonEnv) -> None:
    """SessionStart reports source=startup for a new session and resume for --continue."""
    log = axon_env.root / "ss.jsonl"
    _hooks(axon_env, {"SessionStart": [{"hooks": [py_hook(axon_env, "ss.py", LOG_PAYLOAD, str(log))]}]})
    axon_env.llm.enqueue([{"text": "a"}], [{"text": "b"}])
    _ok(axon_env.run(["-p", "one"]))
    _ok(axon_env.run(["-p", "--continue", "two"]))
    assert [e.get("source") for e in _log(log)] == ["startup", "resume"]


def test_session_end_hook_runs_on_exit(axon_env: AxonEnv) -> None:
    """A SessionEnd hook runs when the print-mode process exits."""
    log = axon_env.root / "se.jsonl"
    _hooks(axon_env, {"SessionEnd": [{"hooks": [py_hook(axon_env, "se.py", LOG_PAYLOAD, str(log))]}]})
    axon_env.llm.enqueue([{"text": "a"}])
    _ok(axon_env.run(["-p", "bye"]))
    entries = _log(log)
    assert entries and entries[0]["hook_event_name"] == "SessionEnd"


# ─── http hooks ─────────────────────────────────────────────────────────────

def test_http_hook_receives_payload_and_can_deny(axon_env: AxonEnv) -> None:
    """An http PreToolUse hook gets the JSON payload via POST; its JSON reply denies the call."""
    def handler(h, method, body):
        return 200, "application/json", json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "HTTP-HOOK-DENY"}})
    with local_http(handler) as srv:
        _hooks(axon_env, {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "http", "url": srv.url + "/hook"}]}]})
        r, ev = run_tools(axon_env, [("Write", {"path": "h.txt", "content": "x"})])
    _ok(r)
    assert srv.requests and srv.requests[0]["method"] == "POST"
    payload = json.loads(srv.requests[0]["body"])
    assert payload["tool_name"] == "Write" and payload["hook_event_name"] == "PreToolUse"
    assert "HTTP-HOOK-DENY" in results(ev, "Write")[0]["content"]
    assert not (axon_env.workspace / "h.txt").exists()


def test_http_hook_error_status_is_non_blocking(axon_env: AxonEnv) -> None:
    """An http hook answering 500 is only a warning: the tool still runs."""
    with local_http(lambda h, m, b: (500, "text/plain", "boom")) as srv:
        _hooks(axon_env, {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "http", "url": srv.url}]}]})
        r, ev = run_tools(axon_env, [("Write", {"path": "ok.txt", "content": "x"})])
    _ok(r)
    assert srv.requests
    assert (axon_env.workspace / "ok.txt").exists()


def test_http_hook_header_env_expansion_only_for_allowed_vars(axon_env: AxonEnv) -> None:
    """Header values expand $VAR only for variables listed in allowedEnvVars."""
    with local_http(lambda h, m, b: (200, "application/json", "{}")) as srv:
        _hooks(axon_env, {"PreToolUse": [{"hooks": [{"type": "http", "url": srv.url, "allowedEnvVars": ["E2E_TOKEN"],
                                                     "headers": {"X-Token": "$E2E_TOKEN", "X-Other": "${E2E_SECRET}"}}]}]})
        r, ev = run_tools(axon_env, [("Ls", {})], run_env={"E2E_TOKEN": "tok-123", "E2E_SECRET": "must-not-leak"})
    _ok(r)
    hdr = srv.requests[0]["headers"]
    assert hdr.get("x-token") == "tok-123" and "must-not-leak" not in json.dumps(hdr)


# ─── robustness ─────────────────────────────────────────────────────────────

def test_hook_nonzero_exit_is_non_blocking(axon_env: AxonEnv) -> None:
    """A hook exiting 1 is a warning only; the tool runs and the turn completes."""
    _hooks(axon_env, {"PreToolUse": [{"hooks": [py_hook(axon_env, "bad.py", "sys.stdin.read()\nsys.exit(1)\n")]}]})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})])
    _ok(r)
    assert (axon_env.workspace / "w.txt").exists()
    assert final(ev)["result"].strip() == "FINAL-ANSWER"


def test_hook_timeout_is_non_blocking(axon_env: AxonEnv) -> None:
    """A hook exceeding its timeout is killed and does not block the tool."""
    h = py_hook(axon_env, "slow.py", "import time\nsys.stdin.read()\ntime.sleep(30)\n")
    h["timeout"] = 1
    _hooks(axon_env, {"PreToolUse": [{"hooks": [h]}]})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})], timeout=40)
    _ok(r)
    assert (axon_env.workspace / "w.txt").exists() and r.duration < 20


def test_hook_garbage_stdout_is_ignored(axon_env: AxonEnv) -> None:
    """A PreToolUse hook printing invalid JSON with exit 0 does not block or crash anything."""
    _hooks(axon_env, {"PreToolUse": [{"hooks": [py_hook(axon_env, "junk.py", "sys.stdin.read()\nprint('{not json')\n")]}]})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})])
    _ok(r)
    assert (axon_env.workspace / "w.txt").exists()


def test_disable_all_hooks(axon_env: AxonEnv) -> None:
    """disableAllHooks=true in settings turns every hook off."""
    h = py_hook(axon_env, "pre.py", "sys.stdin.read()\nsys.stderr.write('SHOULD-NOT-RUN')\nsys.exit(2)\n")
    write_settings(axon_env, {"disableAllHooks": True, "hooks": {"PreToolUse": [{"hooks": [h]}]}})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})])
    _ok(r)
    assert (axon_env.workspace / "w.txt").exists()


def test_legacy_flat_hook_form(axon_env: AxonEnv) -> None:
    """The legacy flat {tool, command} hook form is still honoured."""
    h = py_hook(axon_env, "pre.py", "sys.stdin.read()\nsys.stderr.write('FLAT-FORM-BLOCK')\nsys.exit(2)\n")
    _hooks(axon_env, {"PreToolUse": [{"tool": "Write", "command": h["command"]}]})
    r, ev = run_tools(axon_env, [("Write", {"path": "w.txt", "content": "x"})])
    _ok(r)
    assert "FLAT-FORM-BLOCK" in results(ev, "Write")[0]["content"]
    assert not (axon_env.workspace / "w.txt").exists()


def test_post_tool_use_failure_hook_fires_on_error(axon_env: AxonEnv) -> None:
    """PostToolUseFailure fires for a failing tool (and PostToolUse does not claim success for it)."""
    log = axon_env.root / "fail.jsonl"
    _hooks(axon_env, {"PostToolUseFailure": [{"hooks": [py_hook(axon_env, "f.py", LOG_PAYLOAD, str(log))]}]})
    r, ev = run_tools(axon_env, [("Read", {"path": "missing.txt"})])
    _ok(r)
    entries = _log(log)
    assert entries and entries[0]["tool_name"] == "Read"
