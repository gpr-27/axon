"""Claude-Code-compatible hooks: config loading, exit codes, JSON output, loop wiring."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any

from axon.hooks import HookManager, HookRunner, normalize_event
from axon.providers.base import AssistantTurn, TextBlock
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent, make_settings


def _script(tmp: Path, name: str, body: str) -> str:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{p}"'


def _write_settings(ws: Path, hooks: dict[str, Any], local: bool = False) -> None:
    d = ws / ".axon"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("settings.local.json" if local else "settings.json")).write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def test_loads_all_sources_and_describes(workspace: Path, isolate_user_home: Path) -> None:
    (isolate_user_home / ".axon").mkdir(exist_ok=True)
    (isolate_user_home / ".axon" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}]}}), encoding="utf-8")
    _write_settings(workspace, {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "echo proj", "timeout": 5}]}]})
    _write_settings(workspace, {"PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "echo local"}]}]}, local=True)
    settings = make_settings(workspace, hooks={"pre_tool": [{"tool": "Write", "command": "echo toml"}]})
    rows = HookManager(settings, workspace).describe()
    by_cmd = {r["command"]: r for r in rows}
    assert set(by_cmd) == {"echo user", "echo proj", "echo local", "echo toml"}
    assert by_cmd["echo toml"]["event"] == "PreToolUse" and by_cmd["echo toml"]["matcher"] == "Write"
    assert by_cmd["echo proj"]["timeout"] == 5
    assert normalize_event("post_tool") == "PostToolUse"


def test_matcher_regex(workspace: Path) -> None:
    _write_settings(workspace, {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "exit 0"}]}]})
    hm = HookManager(make_settings(workspace), workspace)
    assert len(hm.matching("PreToolUse", {"tool_name": "Bash"})) == 1
    assert len(hm.matching("PreToolUse", {"tool_name": "Read"})) == 0
    assert len(hm.matching("PreToolUse", {"tool_name": "Bashful"})) == 0  # full match only


def test_exit_code_2_blocks_with_stderr_and_payload_on_stdin(workspace: Path, tmp_path: Path) -> None:
    out = tmp_path / "payload.json"
    cmd = _script(tmp_path, "h.py", f"import sys,json\ndata=sys.stdin.read()\nopen(r'{out}','w').write(data)\nsys.stderr.write('nope: dangerous')\nsys.exit(2)\n")
    _write_settings(workspace, {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]})
    hm = HookManager(make_settings(workspace), workspace)
    hm.session_id = "s1"
    res = hm.run("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf x"}})
    assert res.blocked and res.message == "nope: dangerous"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == "PreToolUse" and payload["session_id"] == "s1"
    assert payload["tool_input"]["command"] == "rm -rf x" and payload["cwd"] == str(workspace)


def test_other_exit_codes_are_warnings(workspace: Path) -> None:
    _write_settings(workspace, {"Stop": [{"hooks": [{"type": "command", "command": "exit 1"}]}]})
    res = HookManager(make_settings(workspace), workspace).run("Stop", {})
    assert not res.blocked and res.warnings


def test_json_output_fields(workspace: Path, tmp_path: Path) -> None:
    out = {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "policy",
                                  "additionalContext": "ctx here", "updatedInput": {"command": "ls"}}}
    cmd = _script(tmp_path, "j.py", f"print({json.dumps(json.dumps(out))})\n")
    _write_settings(workspace, {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]})
    res = HookManager(make_settings(workspace), workspace).run("PreToolUse", {"tool_name": "Bash"})
    assert res.permission_decision == "deny" and "policy" in res.message
    assert res.additional_context == "ctx here" and res.updated_input == {"command": "ls"}


def test_timeout_is_reported(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "slow.py", "import time\ntime.sleep(10)\n")
    _write_settings(workspace, {"Notification": [{"hooks": [{"type": "command", "command": cmd, "timeout": 1}]}]})
    res = HookManager(make_settings(workspace), workspace).run("Notification", {"message": "x"})
    assert any("timed out" in w for w in res.warnings)


def test_legacy_hook_runner_shim(workspace: Path) -> None:
    settings = make_settings(workspace, hooks={"pre_tool": [{"tool": "*", "command": "exit 2"}]})
    assert HookRunner(settings).run("pre_tool", {"tool": "Read"}).proceed is False


def test_pre_tool_use_hook_blocks_tool_in_loop(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "b.py", "import sys\nsys.stderr.write('writes are frozen')\nsys.exit(2)\n")
    _write_settings(workspace, {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Write", {"path": "f.txt", "content": "x"}), "ok"))
    agent = make_agent(prov, workspace)
    agent.run_turn("write")
    result = prov.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "writes are frozen" in result["content"]
    assert not (workspace / "f.txt").exists()


def test_post_tool_use_feedback_is_appended(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "p.py", "import json\nprint(json.dumps({'decision':'block','reason':'lint failed: E501'}))\n")
    _write_settings(workspace, {"PostToolUse": [{"matcher": "Ls", "hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Ls", {"path": "."}), "ok"))
    make_agent(prov, workspace).run_turn("ls")
    result = prov.requests[1]["messages"][-1]["content"][0]
    assert "lint failed: E501" in result["content"]


def test_user_prompt_submit_block_and_context(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "u.py", "import sys,json\nd=json.load(sys.stdin)\n"
                  "if 'secret' in d['prompt']:\n    sys.stderr.write('no secrets'); sys.exit(2)\nprint('Today is release day')\n")
    _write_settings(workspace, {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted("fine"))
    agent = make_agent(prov, workspace)
    res = agent.run_turn("tell me the secret")
    assert res.stop_reason == "blocked" and "no secrets" in res.final_text
    assert prov.requests == [] and agent.conversation.messages == []
    agent.run_turn("hello")
    assert "Today is release day" in json.dumps(prov.requests[0]["messages"][-1])


def test_stop_hook_forces_one_continuation(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "s.py", "import sys,json\nd=json.load(sys.stdin)\n"
                  "if not d['stop_hook_active']:\n    print(json.dumps({'decision':'block','reason':'run the tests first'}))\n")
    _write_settings(workspace, {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted("first stop", "second stop"))
    res = make_agent(prov, workspace).run_turn("do it")
    assert res.final_text == "second stop"
    assert "run the tests first" in json.dumps(prov.requests[1]["messages"][-1])
    assert len(prov.requests) == 2


def test_session_start_context_enters_system_prompt(workspace: Path, tmp_path: Path) -> None:
    cmd = _script(tmp_path, "ss.py", "print('Branch policy: never push to main')\n")
    _write_settings(workspace, {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted("ok"))
    agent = make_agent(prov, workspace)
    agent.start_session("startup")
    agent.run_turn("hi")
    assert "never push to main" in json.dumps(prov.requests[0]["system"])


def test_subagent_stop_fires_for_subagents(workspace: Path, tmp_path: Path) -> None:
    marker = tmp_path / "sub_stop.txt"
    cmd = _script(tmp_path, "ss2.py", f"import sys,json\nd=json.load(sys.stdin)\nopen(r'{marker}','a').write(d['hook_event_name']+'\\n')\n")
    _write_settings(workspace, {"SubagentStop": [{"hooks": [{"type": "command", "command": cmd}]}],
                                "Stop": [{"hooks": [{"type": "command", "command": cmd}]}]})
    prov = FakeProvider(scripted(("Task", {"prompt": "look", "subagent_type": "explore"}), "sub report", "main done"))
    make_agent(prov, workspace).run_turn("delegate")
    assert marker.read_text(encoding="utf-8").split() == ["SubagentStop", "Stop"]


def test_hook_does_not_run_for_unrelated_events(workspace: Path, tmp_path: Path) -> None:
    prov = FakeProvider([AssistantTurn(blocks=[TextBlock(text="x")])])
    agent = make_agent(prov, workspace)
    assert agent.hooks is not None and agent.hooks.describe() == []
    agent.run_turn("x")


def test_powershell_hook_command_calls_quoted_path_and_keeps_exit_code() -> None:
    from axon.hooks.manager import _powershell_hook_command
    quoted = _powershell_hook_command('"C:\\py\\python.exe" "h.py"')
    assert quoted.startswith('& "C:\\py\\python.exe" "h.py"\n')
    assert quoted.endswith("if ($LASTEXITCODE) { exit $LASTEXITCODE }")
    assert _powershell_hook_command("python h.py").startswith("python h.py\n")
