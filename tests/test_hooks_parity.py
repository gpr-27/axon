"""Claude Code hooks parity: matchers, `if`, handler types, output schema, exit codes, loop wiring."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from axon.hooks import HookManager
from axon.hooks import envfile
from axon.hooks.manager import EVENTS, cap_text, default_timeout, matcher_matches, parse_output, valid_terminal_sequence
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


def _py(tmp: Path, name: str, body: str) -> str:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{p}"'


def _cfg(ws: Path, hooks: dict[str, Any]) -> None:
    d = ws / ".axon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _emit(tmp: Path, name: str, obj: Any, code: int = 0) -> str:
    return _py(tmp, name, f"import json,sys\nprint(json.dumps({obj!r}))\nsys.exit({code})\n")


def _mgr(ws: Path) -> HookManager:
    return HookManager(make_settings(ws), ws)


# ── events, matchers, defaults ──────────────────────────────────────────────

def test_every_documented_event_is_known() -> None:
    documented = {
        "Setup", "SessionStart", "UserPromptSubmit", "UserPromptExpansion", "PreToolUse", "PermissionRequest",
        "PermissionDenied", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Notification", "MessageDisplay",
        "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "Stop", "StopFailure", "TeammateIdle",
        "InstructionsLoaded", "ConfigChange", "CwdChanged", "DirectoryAdded", "FileChanged", "WorktreeCreate",
        "WorktreeRemove", "PreCompact", "PostCompact", "PreModelSwitch", "PostModelSwitch", "Elicitation",
        "ElicitationResult", "SessionEnd"}
    assert documented <= set(EVENTS)


def test_matcher_semantics() -> None:
    assert matcher_matches("Edit|Write", "Write") and matcher_matches("Edit, Write", "Edit")
    assert not matcher_matches("Edit|Write", "NotebookEdit")           # exact list, not a substring
    assert matcher_matches("^Notebook", "NotebookEdit")               # regex path
    assert matcher_matches("Edit.*", "NotebookEdit")                  # unanchored, like JS RegExp.test
    assert matcher_matches("mcp__memory__.*", "mcp__memory__create_entities")
    assert not matcher_matches("mcp__memory", "mcp__memory__create_entities")   # exact characters only
    assert matcher_matches("*", "x") and matcher_matches("", "x") and matcher_matches(None, "x")
    # FileChanged / StopFailure only treat letters, digits, _ and | as exact
    assert matcher_matches(".envrc|.env", ".env", "FileChanged")    # '.' puts it on the regex path: still matches
    assert matcher_matches("rate_limit|overloaded", "overloaded", "StopFailure")
    assert not matcher_matches("rate-limit", "rate_limit", "StopFailure")


def test_matcher_fields_per_event(workspace: Path) -> None:
    _cfg(workspace, {"ConfigChange": [{"matcher": "project_settings", "hooks": [{"type": "command", "command": "exit 0"}]}],
                     "PreModelSwitch": [{"matcher": ".*opus.*", "hooks": [{"type": "command", "command": "exit 0"}]}],
                     "UserPromptExpansion": [{"matcher": "deploy", "hooks": [{"type": "command", "command": "exit 0"}]}],
                     "FileChanged": [{"matcher": ".envrc|.env", "hooks": [{"type": "command", "command": "exit 0"}]}]})
    hm = _mgr(workspace)
    assert hm.matching("ConfigChange", {"source": "project_settings"}) and not hm.matching("ConfigChange", {"source": "user_settings"})
    assert hm.matching("PreModelSwitch", {"to_model": "claude-opus-5"}) and not hm.matching("PreModelSwitch", {"to_model": "sonnet"})
    assert hm.matching("UserPromptExpansion", {"command_name": "deploy"})
    assert hm.matching("FileChanged", {"file_path": "/p/.env"})


def test_default_timeouts() -> None:
    assert default_timeout("command", "PreToolUse") == 600 and default_timeout("http", "Stop") == 600
    assert default_timeout("command", "UserPromptSubmit") == 30 and default_timeout("command", "MessageDisplay") == 10
    assert default_timeout("prompt", "Stop") == 30 and default_timeout("agent", "Stop") == 60
    assert default_timeout("command", "SessionEnd") == 1.5


def test_handler_fields_parse(workspace: Path) -> None:
    _cfg(workspace, {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "if": "Bash(git *)", "command": "node", "args": ["a.js"], "shell": "bash",
         "onFailure": "block", "statusMessage": "checking", "timeout": 7}]}]})
    row = _mgr(workspace).describe()[0]
    assert row["if"] == "Bash(git *)" and row["args"] == ["a.js"] and row["shell"] == "bash"
    assert row["onFailure"] == "block" and row["statusMessage"] == "checking" and row["timeout"] == 7


def test_unsupported_types_are_skipped_with_a_note(workspace: Path) -> None:
    _cfg(workspace, {"SessionStart": [{"hooks": [{"type": "http", "url": "http://x"}, {"type": "command", "command": "exit 0"}]}],
                     "Notification": [{"hooks": [{"type": "prompt", "prompt": "hi"}]}]})
    hm = _mgr(workspace)
    assert [h["type"] for h in hm.describe()] == ["command"]
    assert len(hm.load_errors) == 2


# ── `if` conditions ─────────────────────────────────────────────────────────

def test_if_condition_filters_tool_events(workspace: Path, tmp_path: Path) -> None:
    out = tmp_path / "ran.txt"
    cmd = _py(tmp_path, "r.py", f"open(r'{out}','a').write('x')\n")
    _cfg(workspace, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "if": "Bash(git *)", "command": cmd}]}],
                     "Stop": [{"hooks": [{"type": "command", "if": "Bash(git *)", "command": cmd}]}]})
    hm = _mgr(workspace)
    hm.run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "npm test"}})
    assert not out.exists()
    hm.run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "FOO=1 git push"}})
    hm.run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "npm i && git push"}})
    assert out.read_text() == "xx"
    hm.run("Stop", {})                      # `if` on a non-tool event: never runs
    assert out.read_text() == "xx"


def test_if_condition_file_patterns(workspace: Path, tmp_path: Path) -> None:
    out = tmp_path / "ran.txt"
    cmd = _py(tmp_path, "r.py", f"open(r'{out}','a').write('x')\n")
    _cfg(workspace, {"PostToolUse": [{"hooks": [{"type": "command", "if": "Edit(*.py)", "command": cmd}]}]})
    hm = _mgr(workspace)
    hm.run("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(workspace / "a.ts")}})
    assert not out.exists()
    hm.run("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": str(workspace / "a.py")}})
    assert out.exists()


# ── command handlers ────────────────────────────────────────────────────────

def test_exec_form_substitutes_placeholders_without_a_shell(workspace: Path, tmp_path: Path) -> None:
    script = tmp_path / "e.py"
    out = tmp_path / "args.json"
    script.write_text(f"import sys,json\nopen(r'{out}','w').write(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "command", "command": sys.executable,
                                                  "args": [str(script), "${CLAUDE_PROJECT_DIR}/x y", "a;b $HOME"]}]}]})
    _mgr(workspace).run("Notification", {"message": "m"})
    assert json.loads(out.read_text()) == [f"{workspace}/x y", "a;b $HOME"]


def test_env_vars_and_common_input(workspace: Path, tmp_path: Path) -> None:
    out = tmp_path / "env.json"
    cmd = _py(tmp_path, "env.py", f"import os,sys,json\nd=json.load(sys.stdin)\nopen(r'{out}','w').write(json.dumps([os.environ['CLAUDE_PROJECT_DIR'], d]))\n")
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "command", "command": cmd}]}]})
    hm = _mgr(workspace)
    hm.session_id, hm.prompt_id = "s9", "p-1"
    hm.run("Notification", {"message": "m"})
    proj, data = json.loads(out.read_text())
    assert proj == str(workspace)
    assert data["prompt_id"] == "p-1" and data["session_id"] == "s9" and data["permission_mode"]


def test_matching_hooks_run_in_parallel(workspace: Path, tmp_path: Path) -> None:
    slow = _py(tmp_path, "slow.py", "import time\ntime.sleep(1.2)\n")
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "command", "command": slow}, {"type": "command", "command": slow + " 1"},
                                                 {"type": "command", "command": slow + " 2"}]}]})
    t0 = time.monotonic()
    res = _mgr(workspace).run("Notification", {"message": "m"})
    assert res.ran == 3 and time.monotonic() - t0 < 3.0


def test_identical_handlers_across_files_run_once(workspace: Path, isolate_user_home: Path) -> None:
    (isolate_user_home / ".axon").mkdir(exist_ok=True)
    body = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo same"}]}]}}
    (isolate_user_home / ".axon" / "settings.json").write_text(json.dumps(body), encoding="utf-8")
    _cfg(workspace, body["hooks"])
    assert len(_mgr(workspace).describe()) == 1


def test_scoped_hooks_register_and_unregister(workspace: Path) -> None:
    hm = _mgr(workspace)
    assert not hm.has("Stop")
    assert hm.add_scoped({"Stop": [{"hooks": [{"type": "command", "command": "exit 0"}]}]}, "skill:deploy") == 1
    assert hm.has("Stop") and hm.describe()[0]["source"] == "scoped:skill:deploy"
    hm.remove_scoped("skill:deploy")
    assert not hm.has("Stop")


# ── exit codes ──────────────────────────────────────────────────────────────

def test_exit_2_only_blocks_events_that_can_block(workspace: Path, tmp_path: Path) -> None:
    cmd = _py(tmp_path, "x2.py", "import sys\nsys.stderr.write('stderr text')\nsys.exit(2)\n")
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "command", "command": cmd}]}],
                     "PermissionRequest": [{"hooks": [{"type": "command", "command": cmd}]}],
                     "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}],
                     "PostToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]})
    hm = _mgr(workspace)
    assert not hm.run("Notification", {"message": "m"}).blocked
    assert not hm.run("PermissionRequest", {"tool_name": "Bash"}).blocked      # exit 2 not honored there
    assert hm.run("UserPromptSubmit", {"prompt": "p"}).blocked
    post = hm.run("PostToolUse", {"tool_name": "Bash"})
    assert post.blocked and post.message == "stderr text"                      # feedback to the model


def test_json_wins_over_exit_code_except_exit_2(workspace: Path, tmp_path: Path) -> None:
    deny = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "no"}}
    _cfg(workspace, {"PreToolUse": [{"matcher": "A", "hooks": [{"type": "command", "command": _emit(tmp_path, "a.py", deny, 1)}]},
                                    {"matcher": "B", "hooks": [{"type": "command", "command": _emit(tmp_path, "b.py", {"hookSpecificOutput": {"permissionDecision": "allow"}}, 2)}]}]})
    hm = _mgr(workspace)
    a = hm.run("PreToolUse", {"tool_name": "A"})
    assert a.permission_decision == "deny" and not a.warnings                  # exit 1 + valid JSON: fields decide
    b = hm.run("PreToolUse", {"tool_name": "B"})
    assert b.blocked                                                           # exit 2 cannot be overridden by allow


def test_invalid_json_is_a_non_blocking_error(workspace: Path, tmp_path: Path) -> None:
    cmd = _py(tmp_path, "bad.py", "print('{not json}')\n")
    _cfg(workspace, {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]})
    res = _mgr(workspace).run("PreToolUse", {"tool_name": "Bash"})
    assert not res.blocked and res.warnings and "valid JSON" in res.warnings[0]


def test_wrong_field_types_fail_validation(workspace: Path, tmp_path: Path) -> None:
    cmd = _emit(tmp_path, "t.py", {"continue": "no", "systemMessage": 3})
    _cfg(workspace, {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]})
    res = _mgr(workspace).run("Stop", {})
    assert not res.stop and res.warnings and "validation" in res.warnings[0]
    assert parse_output('{"continue": false}')[0] == "json"
    assert parse_output("plain text")[0] == "text" and parse_output("")[0] == "empty"


def test_on_failure_block(workspace: Path, tmp_path: Path) -> None:
    crash = _py(tmp_path, "crash.py", "import sys\nsys.exit(1)\n")
    _cfg(workspace, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": crash, "onFailure": "block"}]},
                                    {"matcher": "Read", "hooks": [{"type": "command", "command": crash}]},
                                    {"matcher": "Missing", "hooks": [{"type": "command", "command": "no-such-binary-xyz", "args": [], "onFailure": "block"}]}]})
    hm = _mgr(workspace)
    res = hm.run("PreToolUse", {"tool_name": "Bash"})
    assert res.blocked and 'onFailure is "block"' in res.message
    assert not hm.run("PreToolUse", {"tool_name": "Read"}).blocked
    assert hm.run("PreToolUse", {"tool_name": "Missing"}).blocked              # cannot start


def test_on_failure_block_on_timeout(workspace: Path, tmp_path: Path) -> None:
    slow = _py(tmp_path, "slow.py", "import time\ntime.sleep(10)\n")
    _cfg(workspace, {"PreToolUse": [{"hooks": [{"type": "command", "command": slow, "timeout": 1, "onFailure": "block"}]}]})
    res = _mgr(workspace).run("PreToolUse", {"tool_name": "Bash"})
    assert res.blocked and "timed out" in res.message


def test_timeout_does_not_block_by_default(workspace: Path, tmp_path: Path) -> None:
    slow = _py(tmp_path, "slow.py", "import time\ntime.sleep(10)\n")
    _cfg(workspace, {"PreToolUse": [{"hooks": [{"type": "command", "command": slow, "timeout": 1}]}]})
    res = _mgr(workspace).run("PreToolUse", {"tool_name": "Bash"})
    assert not res.blocked and res.warnings


# ── output schema ───────────────────────────────────────────────────────────

def test_permission_precedence_and_updated_input_replaces(workspace: Path, tmp_path: Path) -> None:
    mk = lambda n, d, ui=None: _emit(tmp_path, n, {"hookSpecificOutput": {  # noqa: E731
        "permissionDecision": d, **({"updatedInput": ui} if ui else {})}})
    _cfg(workspace, {"PreToolUse": [{"hooks": [
        {"type": "command", "command": mk("a.py", "allow", {"command": "ls"})},
        {"type": "command", "command": mk("b.py", "ask")},
        {"type": "command", "command": mk("c.py", "defer")}]}]})
    res = _mgr(workspace).run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm", "x": 1}})
    assert res.permission_decision == "defer" and res.updated_input == {"command": "ls"}


def test_post_tool_use_fields(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "ctx",
                                  "updatedToolOutput": {"stdout": "[redacted]", "stderr": ""}},
           "continue": False, "stopReason": "enough", "systemMessage": "heads up"}
    _cfg(workspace, {"PostToolUse": [{"hooks": [{"type": "command", "command": _emit(tmp_path, "p.py", out)}]}]})
    res = _mgr(workspace).run("PostToolUse", {"tool_name": "Bash"})
    assert res.updated_tool_output == {"stdout": "[redacted]", "stderr": ""}
    assert res.additional_context == "ctx" and res.stop and res.stop_reason == "enough" and res.system_message == "heads up"


def test_permission_request_decision_object(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
        "behavior": "allow", "updatedInput": {"command": "npm run lint"},
        "updatedPermissions": [{"type": "addRules", "rules": [{"toolName": "Bash"}], "behavior": "allow", "destination": "session"}]}}}
    _cfg(workspace, {"PermissionRequest": [{"hooks": [{"type": "command", "command": _emit(tmp_path, "pr.py", out)}]}]})
    res = _mgr(workspace).run("PermissionRequest", {"tool_name": "Bash"})
    assert res.permission_decision == "allow" and res.updated_input == {"command": "npm run lint"}
    assert res.updated_permissions[0]["type"] == "addRules"
    deny = {"hookSpecificOutput": {"decision": {"behavior": "deny", "message": "nope", "interrupt": True}}}
    _cfg(workspace, {"PermissionRequest": [{"hooks": [{"type": "command", "command": _emit(tmp_path, "pd.py", deny)}]}]})
    res = _mgr(workspace).run("PermissionRequest", {"tool_name": "Bash"})
    assert res.permission_decision == "deny" and res.message == "nope" and res.interrupt


def test_session_and_prompt_fields(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"sessionTitle": "my title", "watchPaths": ["/a"], "reloadSkills": True,
                                  "initialUserMessage": "go", "suppressOriginalPrompt": True}}
    _cfg(workspace, {"SessionStart": [{"hooks": [{"type": "command", "command": _emit(tmp_path, "s.py", out)}]}]})
    res = _mgr(workspace).run("SessionStart", {"source": "startup"})
    assert (res.session_title, res.watch_paths, res.reload_skills, res.initial_user_message, res.suppress_original_prompt) == \
        ("my title", ["/a"], True, "go", True)


def test_event_specific_discards(workspace: Path, tmp_path: Path) -> None:
    out = {"continue": False, "systemMessage": "x", "decision": "block", "reason": "r"}
    cmd = _emit(tmp_path, "d.py", out)
    _cfg(workspace, {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev in
                     ("Notification", "SessionEnd", "InstructionsLoaded", "StopFailure", "CwdChanged", "TaskCreated")})
    hm = _mgr(workspace)
    for ev in ("Notification", "SessionEnd", "InstructionsLoaded", "StopFailure"):
        r = hm.run(ev, {})
        assert not r.stop and not r.blocked and not r.system_message, ev
    cwd = hm.run("CwdChanged", {})
    assert not cwd.stop and cwd.system_message == "x"                          # systemMessage kept, continue discarded
    task = hm.run("TaskCreated", {"task_id": "1"})
    assert task.blocked and not task.stop                                      # decision honored, continue ignored


def test_terminal_sequence_allowlist(workspace: Path, tmp_path: Path) -> None:
    assert valid_terminal_sequence("\x1b]777;notify;t;b\x07") and valid_terminal_sequence("\x07")
    assert valid_terminal_sequence("\x1b]0;title\x1b\\")
    assert not valid_terminal_sequence("\x1b[31m") and not valid_terminal_sequence("\x1b]52;c;AAAA\x07")
    assert not valid_terminal_sequence("\x1b]8;;http://x\x07")
    ok = _emit(tmp_path, "ts.py", {"terminalSequence": "\x1b]9;hello\x07"})
    bad = _emit(tmp_path, "tb.py", {"terminalSequence": "\x1b[2J"})
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "command", "command": ok}, {"type": "command", "command": bad}]}]})
    assert _mgr(workspace).run("Notification", {}).terminal_sequence == "\x1b]9;hello\x07"


def test_large_outputs_are_capped_to_a_preview(workspace: Path, tmp_path: Path) -> None:
    text = "y" * 25_000
    cmd = _emit(tmp_path, "big.py", {"hookSpecificOutput": {"additionalContext": text}})
    _cfg(workspace, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]})
    res = _mgr(workspace).run("UserPromptSubmit", {"prompt": "p"})
    assert len(res.additional_context) < 3_000 and "25,000 characters" in res.additional_context
    assert cap_text("short") == "short"


def test_plain_stdout_context_events(workspace: Path, tmp_path: Path) -> None:
    cmd = _py(tmp_path, "pl.py", "print('some context')\n")
    _cfg(workspace, {ev: [{"hooks": [{"type": "command", "command": cmd}]}] for ev in
                     ("UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PostModelSwitch", "PostToolUse")})
    hm = _mgr(workspace)
    for ev in ("UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PostModelSwitch"):
        assert hm.run(ev, {}).additional_context == "some context", ev
    assert hm.run("PostToolUse", {"tool_name": "Bash"}).additional_context == ""


def test_elicitation_actions(workspace: Path, tmp_path: Path) -> None:
    ans = {"hookSpecificOutput": {"hookEventName": "Elicitation", "action": "accept", "content": {"username": "alice"}}}
    dec = {"hookSpecificOutput": {"action": "decline"}}
    _cfg(workspace, {"Elicitation": [{"matcher": "srv", "hooks": [{"type": "command", "command": _emit(tmp_path, "e1.py", ans)}]},
                                     {"matcher": "other", "hooks": [{"type": "command", "command": _emit(tmp_path, "e2.py", dec)}]}]})
    hm = _mgr(workspace)
    r = hm.run("Elicitation", {"mcp_server_name": "srv"})
    assert r.action == "accept" and r.content == {"username": "alice"}
    assert hm.run("Elicitation", {"mcp_server_name": "other"}).action == "decline"


# ── disableAllHooks precedence ──────────────────────────────────────────────

def test_disable_all_hooks_follows_settings_precedence(workspace: Path, isolate_user_home: Path) -> None:
    (isolate_user_home / ".axon").mkdir(exist_ok=True)
    hook = {"Stop": [{"hooks": [{"type": "command", "command": "exit 0"}]}]}
    (isolate_user_home / ".axon" / "settings.json").write_text(json.dumps({"disableAllHooks": True, "hooks": hook}), encoding="utf-8")
    assert _mgr(workspace).describe() == []
    (workspace / ".axon").mkdir(exist_ok=True)
    (workspace / ".axon" / "settings.json").write_text(json.dumps({"disableAllHooks": False}), encoding="utf-8")
    assert len(_mgr(workspace).describe()) == 1                                # project overrides the user's true


# ── http ────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    body = b"{}"
    status = 200
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Handler.seen.append({"json": json.loads(self.rfile.read(n) or b"{}"), "auth": self.headers.get("Authorization")})
        self.send_response(_Handler.status)
        self.end_headers()
        self.wfile.write(_Handler.body)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def http_server() -> Any:
    _Handler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/h"
    srv.shutdown()


def test_http_hook_json_and_header_env(workspace: Path, http_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "tok")
    monkeypatch.setenv("OTHER", "leak")
    _Handler.body, _Handler.status = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "http no"}}).encode(), 200
    _cfg(workspace, {"PreToolUse": [{"hooks": [{"type": "http", "url": http_server, "allowedEnvVars": ["MY_TOKEN"],
                                                "headers": {"Authorization": "Bearer $MY_TOKEN$OTHER"}}]}]})
    res = _mgr(workspace).run("PreToolUse", {"tool_name": "Bash"})
    assert res.permission_decision == "deny" and "http no" in res.message
    assert _Handler.seen[0]["auth"] == "Bearer tok" and _Handler.seen[0]["json"]["hook_event_name"] == "PreToolUse"


def test_http_hook_response_handling(workspace: Path, http_server: str) -> None:
    _cfg(workspace, {"Notification": [{"hooks": [{"type": "http", "url": http_server}]}],
                     "PreToolUse": [{"hooks": [{"type": "http", "url": http_server, "onFailure": "block"}]}]})
    hm = _mgr(workspace)
    _Handler.body, _Handler.status = b"", 200
    assert not hm.run("Notification", {}).warnings                              # empty 2xx body: success
    _Handler.body = b"plain text"
    assert hm.run("Notification", {}).warnings                                  # non-JSON 2xx body: error
    _Handler.body, _Handler.status = b"", 500
    assert hm.run("Notification", {}).warnings
    assert hm.run("PreToolUse", {"tool_name": "Bash"}).blocked                  # onFailure block on HTTP error


def test_http_allowlist(workspace: Path, http_server: str) -> None:
    d = workspace / ".axon"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"allowedHttpHookUrls": ["https://hooks.example.com/*"], "hooks": {
        "Notification": [{"hooks": [{"type": "http", "url": http_server}]}]}}), encoding="utf-8")
    res = _mgr(workspace).run("Notification", {})
    assert _Handler.seen == [] and any("allowedHttpHookUrls" in w for w in res.warnings)


# ── prompt / agent / mcp_tool ───────────────────────────────────────────────

def test_prompt_hook_decisions(workspace: Path) -> None:
    _cfg(workspace, {"Stop": [{"hooks": [{"type": "prompt", "prompt": "Done? $ARGUMENTS"}]}],
                     "PreToolUse": [{"matcher": "A", "hooks": [{"type": "prompt", "prompt": "check"}]},
                                    {"matcher": "B", "hooks": [{"type": "prompt", "prompt": "check", "continueOnBlock": True}]}]})
    hm = _mgr(workspace)
    prompts: list[str] = []
    reply = ['{"ok": false, "reason": "tests missing"}']
    hm.llm = lambda p, m, t, a: (prompts.append(p), reply[0])[1]
    stop = hm.run("Stop", {"last_assistant_message": "hi"})
    assert stop.blocked and stop.message == "tests missing"
    assert "last_assistant_message" in prompts[0] and prompts[0].startswith("Done? {")
    reply[0] = '```json\n{"ok": false, "reason": "never", "impossible": true}\n```'
    assert not hm.run("Stop", {}).blocked                                       # impossible: let the turn end
    reply[0] = '{"ok": false, "reason": "bad cmd"}'
    a = hm.run("PreToolUse", {"tool_name": "A"})
    assert a.permission_decision == "deny" and a.stop                           # default: the turn ends
    b = hm.run("PreToolUse", {"tool_name": "B"})
    assert b.permission_decision == "deny" and not b.stop                       # continueOnBlock: error back to the model
    reply[0] = '{"ok": true}'
    assert not hm.run("Stop", {}).blocked


def test_prompt_hook_without_a_model_is_a_warning(workspace: Path) -> None:
    _cfg(workspace, {"Stop": [{"hooks": [{"type": "prompt", "prompt": "x"}]}]})
    res = _mgr(workspace).run("Stop", {})
    assert not res.blocked and res.warnings


def test_agent_hook_blocks_with_continue_semantics(workspace: Path) -> None:
    _cfg(workspace, {"PreToolUse": [{"hooks": [{"type": "agent", "prompt": "verify"}]}]})
    hm = _mgr(workspace)
    hm.llm = lambda p, m, t, a: '{"ok": false, "reason": "r"}' if a else "?"
    res = hm.run("PreToolUse", {"tool_name": "Bash"})
    assert res.permission_decision == "deny" and not res.stop


def test_mcp_tool_hook_fills_input_from_payload(workspace: Path) -> None:
    _cfg(workspace, {"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "mcp_tool", "server": "scan", "tool": "check",
                                                                    "input": {"file_path": "${tool_input.file_path}", "n": 1}}]}]})
    hm = _mgr(workspace)
    calls: list[tuple[str, str, dict[str, Any]]] = []
    hm.mcp_call = lambda s, t, a: (calls.append((s, t, a)), ('{"systemMessage": "scanned"}', False))[1]
    res = hm.run("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": "/x/y.py"}})
    assert calls == [("scan", "check", {"file_path": "/x/y.py", "n": 1})] and res.system_message == "scanned"
    hm.mcp_call = lambda s, t, a: ("boom", True)
    assert hm.run("PostToolUse", {"tool_name": "Write", "tool_input": {}}).warnings


# ── async ───────────────────────────────────────────────────────────────────

def _wait(cond: Any, secs: float = 8.0) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end and not cond():
        time.sleep(0.05)


def test_async_hook_output_is_delivered_later(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "tests passed"}, "systemMessage": "sm"}
    cmd = _py(tmp_path, "as.py", f"import json,time\ntime.sleep(0.3)\nprint('noise')\nprint(json.dumps({out!r}))\n")
    _cfg(workspace, {"PostToolUse": [{"hooks": [{"type": "command", "command": cmd, "async": True}]}]})
    hm = _mgr(workspace)
    t0 = time.monotonic()
    res = hm.run("PostToolUse", {"tool_name": "Write"})
    assert time.monotonic() - t0 < 5 and res.ran == 1 and not res.additional_context
    _wait(hm.has_async_output)
    msgs = hm.drain_async()
    assert len(msgs) == 1 and "tests passed" in msgs[0] and "sm" in msgs[0] and hm.drain_async() == []


def test_async_rewake_on_exit_2(workspace: Path, tmp_path: Path) -> None:
    cmd = _py(tmp_path, "rw.py", "import sys\nsys.stderr.write('build broke')\nsys.exit(2)\n")
    _cfg(workspace, {"PostToolUse": [{"hooks": [{"type": "command", "command": cmd, "asyncRewake": True}]}]})
    hm = _mgr(workspace)
    woke: list[bool] = []
    hm.on_async = woke.append
    hm.run("PostToolUse", {"tool_name": "Write"})
    _wait(hm.has_async_output)
    assert hm.drain_async() == ["build broke"] and woke == [True]


# ── CLAUDE_ENV_FILE ─────────────────────────────────────────────────────────

def test_env_file_variables_reach_bash_commands(workspace: Path, tmp_path: Path) -> None:
    envfile.reset()
    cmd = _py(tmp_path, "ef.py", "import os\nopen(os.environ['CLAUDE_ENV_FILE'],'a').write(\"export NODE_ENV=production\\n"
                                 "export NAME='two words'\\nexport P=\\\"$HOME/bin\\\"\\n$(rm -rf /)\\n\")\n")
    _cfg(workspace, {"SessionStart": [{"hooks": [{"type": "command", "command": cmd}]}]})
    _mgr(workspace).run("SessionStart", {"source": "startup"})
    from axon.tools.shell import _child_env
    env = _child_env(workspace)
    assert env["NODE_ENV"] == "production" and env["NAME"] == "two words" and env["P"].endswith("/bin")
    assert not any("rm" in v for v in env.values() if v.startswith("$("))
    cwd_cmd = _py(tmp_path, "cw.py", "import os\nopen(os.environ['CLAUDE_ENV_FILE'],'a').write('export ONLY_NOW=1\\n')\n")
    _cfg(workspace, {"CwdChanged": [{"hooks": [{"type": "command", "command": cwd_cmd}]}]})
    _mgr(workspace).run("CwdChanged", {"old_cwd": "a", "new_cwd": "b"})
    env = _child_env(workspace)
    assert env.get("ONLY_NOW") == "1" and "NODE_ENV" not in env                # CwdChanged cleared the earlier variables
    envfile.reset()


# ── agent loop wiring ───────────────────────────────────────────────────────

def test_loop_pre_tool_use_context_replaces_input_and_carries_tool_use_id(workspace: Path, tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    cmd = _py(tmp_path, "pre.py", f"import sys,json\nd=json.load(sys.stdin)\nopen(r'{seen}','w').write(json.dumps(d))\n"
              "print(json.dumps({'hookSpecificOutput':{'additionalContext':'branch is main','updatedInput':{'path':'.'}}}))\n")
    _cfg(workspace, {"PreToolUse": [{"matcher": "Ls", "hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Ls", {"path": "nonexistent-dir"}), "ok"))
    make_agent(prov, workspace).run_turn("ls")
    result = prov.requests[1]["messages"][-1]["content"][0]
    assert not result.get("is_error") and "branch is main" in result["content"]
    payload = json.loads(seen.read_text())
    assert payload["tool_use_id"] and payload["tool_name"] == "Ls" and payload["prompt_id"]


def test_loop_post_tool_use_updated_output_and_continue_false(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"updatedToolOutput": "[redacted]"}, "continue": False, "stopReason": "enough"}
    _cfg(workspace, {"PostToolUse": [{"matcher": "Ls", "hooks": [{"type": "command", "command": _emit(tmp_path, "po.py", out)}]}]})
    prov = FakeProvider(scripted(("Ls", {"path": "."}), "never reached"))
    agent = make_agent(prov, workspace)
    res = agent.run_turn("ls")
    assert res.stop_reason == "blocked" and "enough" in res.final_text
    assert len(prov.requests) == 1                                              # no model call after the stop
    assert "[redacted]" in json.dumps(agent.conversation.messages)


def test_loop_post_tool_batch_blocks_next_model_call(workspace: Path, tmp_path: Path) -> None:
    cmd = _emit(tmp_path, "pb.py", {"decision": "block", "reason": "stop here"})
    _cfg(workspace, {"PostToolBatch": [{"hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Ls", {"path": "."}), "never"))
    res = make_agent(prov, workspace).run_turn("ls")
    assert res.stop_reason == "blocked" and "stop here" in res.final_text and len(prov.requests) == 1


def test_loop_stop_hook_cap_and_additional_context(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "3")
    cmd = _emit(tmp_path, "st.py", {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "run the tests"}})
    _cfg(workspace, {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted("a", "b", "c", "d", "e"))
    res = make_agent(prov, workspace).run_turn("go")
    assert len(prov.requests) == 4 and res.final_text == "d"                    # 3 forced continuations, then the turn ends
    assert "run the tests" in json.dumps(prov.requests[1]["messages"][-1])


def test_loop_user_prompt_block_reports_original_prompt(workspace: Path, tmp_path: Path) -> None:
    cmd = _emit(tmp_path, "up.py", {"decision": "block", "reason": "not today"})
    other = _emit(tmp_path, "up2.py", {"decision": "block", "reason": "not today", "hookSpecificOutput": {"suppressOriginalPrompt": True}})
    _cfg(workspace, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]})
    res = make_agent(FakeProvider(scripted("x")), workspace).run_turn("my private prompt")
    assert "not today" in res.final_text and "my private prompt" in res.final_text
    _cfg(workspace, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": other}]}]})
    res = make_agent(FakeProvider(scripted("x")), workspace).run_turn("my private prompt")
    assert "my private prompt" not in res.final_text


def test_loop_permission_request_hook_updated_permissions(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedPermissions": [
        {"type": "addRules", "rules": [{"toolName": "Write"}], "behavior": "allow", "destination": "session"}]}}}
    _cfg(workspace, {"PermissionRequest": [{"hooks": [{"type": "command", "command": _emit(tmp_path, "pr.py", out)}]}]})
    prov = FakeProvider(scripted(("Write", {"path": "f.txt", "content": "x"}), "ok"))
    agent = make_agent(prov, workspace, mode="default")
    agent.run_turn("write")
    assert (workspace / "f.txt").exists()
    assert any(str(e.rule) == "Write" for e in agent.permissions.session_rules)


def test_loop_prompt_hook_uses_the_provider(workspace: Path) -> None:
    _cfg(workspace, {"Stop": [{"hooks": [{"type": "prompt", "prompt": "Is it done? $ARGUMENTS"}]}]})
    prov = FakeProvider(scripted("first answer", '{"ok": false, "reason": "add tests"}', "second answer", '{"ok": true}'))
    agent = make_agent(prov, workspace)
    res = agent.run_turn("do it")
    assert res.final_text == "second answer"
    assert "add tests" in json.dumps(prov.requests[2]["messages"])


def test_stop_failure_error_types() -> None:
    from axon.agent.loop import classify_stop_failure
    assert classify_stop_failure("API Error: Rate limit reached (429)") == "rate_limit"
    assert classify_stop_failure("HTTP 529 overloaded") == "overloaded"
    assert classify_stop_failure("401 invalid api key") == "authentication_failed"
    assert classify_stop_failure("something odd") == "unknown"


def test_user_prompt_expansion_and_model_switch_hooks(workspace: Path, tmp_path: Path) -> None:
    block = _emit(tmp_path, "ue.py", {"decision": "block", "reason": "no deploys"})
    ctx = _emit(tmp_path, "pm.py", {"hookSpecificOutput": {"additionalContext": "now on opus"}})
    deny = _py(tmp_path, "pre.py", "import sys\nsys.stderr.write('retired')\nsys.exit(2)\n")
    _cfg(workspace, {"UserPromptExpansion": [{"matcher": "deploy", "hooks": [{"type": "command", "command": block}]}],
                     "PreModelSwitch": [{"matcher": ".*old.*", "hooks": [{"type": "command", "command": deny}]}],
                     "PostModelSwitch": [{"hooks": [{"type": "command", "command": ctx}]}]})
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    assert agent.expansion_hook("deploy", "prod", "project", "/deploy prod")[0] is False
    assert agent.expansion_hook("review", "", "project", "/review")[0] is True
    assert agent.model_switch_hook("pre", "a", "very-old-model") == (False, "retired")
    assert agent.model_switch_hook("pre", "a", "new-model")[0] is True
    agent.model_switch_hook("post", "a", "new-model")
    assert "now on opus" in agent.take_pending_context()


# ── worktree hooks and /hooks ───────────────────────────────────────────────

def test_worktree_create_hook_replaces_git(workspace: Path, tmp_path: Path) -> None:
    from axon.agent.worktree import create_session_worktree, hook_remove_worktree
    target = tmp_path / "wt-made-by-hook"
    cmd = _py(tmp_path, "wc.py", f"import sys,json,os\nd=json.load(sys.stdin)\nos.makedirs(r'{target}', exist_ok=True)\n"
              f"print('banner line')\nprint(r'{target}')\n")
    rm = _py(tmp_path, "wr.py", "import sys,json,shutil\nshutil.rmtree(json.load(sys.stdin)['worktree_path'])\n")
    _cfg(workspace, {"WorktreeCreate": [{"hooks": [{"type": "command", "command": cmd}]}],
                     "WorktreeRemove": [{"hooks": [{"type": "command", "command": rm}]}]})
    info = create_session_worktree(workspace, "feature-x")
    assert info is not None and info.by_hook and info.worktree_path == target
    assert hook_remove_worktree(info) and not target.exists()


def test_worktree_create_hook_failure_and_bad_paths(workspace: Path, tmp_path: Path) -> None:
    from axon.agent.worktree import create_session_worktree
    _cfg(workspace, {"WorktreeCreate": [{"hooks": [{"type": "command", "command": _py(tmp_path, "f.py", "import sys\nsys.exit(1)\n")}]}]})
    assert create_session_worktree(workspace, "x") is None
    _cfg(workspace, {"WorktreeCreate": [{"hooks": [{"type": "command", "command": _py(tmp_path, "g.py", f"print(r'{tmp_path}/a/../b')\n")}]}]})
    assert create_session_worktree(workspace, "x") is None


def test_hooks_command_lists_details_and_events(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from axon.commands.agent_cmds import handle_hooks
    _cfg(workspace, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "if": "Bash(git *)", "command": "echo hi"}]}]})
    agent = make_agent(FakeProvider(scripted("x")), workspace)
    agent.hooks = None
    handle_hooks(agent, "")
    out = capsys.readouterr().out
    assert "PreToolUse" in out and "echo hi" in out and "Bash(git *)" in out and "project settings" in out
    handle_hooks(agent, "1")
    assert "matcher" in capsys.readouterr().out
    handle_hooks(agent, "events")
    assert "UserPromptExpansion" in capsys.readouterr().out
    handle_hooks(agent, "PreModelSwitch")
    assert "0 configured" in capsys.readouterr().out
