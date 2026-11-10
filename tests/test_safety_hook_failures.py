"""A hook that cannot be evaluated never turns into a silent "allow", and input rewritten by a
PermissionRequest hook is checked against the rules again."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from axon.agent.trace import HookEvent, PermissionDecisionEvent
from tests.fakes import FakeProvider, scripted
from tests.test_core_helpers import make_agent


def _hooks_file(ws: Path, hooks: dict[str, Any], extra: dict[str, Any] | None = None) -> None:
    d = ws / ".axon"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"hooks": hooks, **(extra or {})}), encoding="utf-8")


def _json_hook(tmp: Path, name: str, payload: dict[str, Any]) -> str:
    script = tmp / name
    script.write_text(f"import json\nprint(json.dumps({payload!r}))\n", encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _break_manager(agent: Any) -> None:
    def boom(event: str, payload: Any = None) -> Any:
        raise RuntimeError("hook manager exploded")
    agent.hooks.run = boom


def _noop_hook(tmp: Path) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": _json_hook(tmp, "noop.py", {})}]}


# ── 1. the hook manager itself fails ─────────────────────────────────────────

def test_pre_tool_use_manager_failure_asks_instead_of_running(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PreToolUse": [_noop_hook(tmp_path)]})
    asked: list[str] = []
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace,
                       events=events, on_approval=lambda t, a, d: asked.append(t) or "deny")   # mode: bypass
    _break_manager(agent)
    agent.run_turn("write")
    assert asked == ["Write"]                                  # bypass mode would have run it without asking
    assert not (workspace / "a.txt").exists()
    shown = [e for e in events if isinstance(e, HookEvent) and e.event == "PreToolUse"]
    assert shown and any("hook manager exploded" in w for w in shown[0].warnings)


def test_pre_tool_use_manager_failure_without_approver_does_not_run(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PreToolUse": [_noop_hook(tmp_path)]})
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace, events=events)
    _break_manager(agent)
    agent.run_turn("write")
    assert not (workspace / "a.txt").exists()
    perms = [e for e in events if isinstance(e, PermissionDecisionEvent)]
    assert perms and perms[-1].outcome == "deny"


def test_informational_hook_manager_failure_is_shown_and_does_not_stop_the_turn(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PostToolUse": [_noop_hook(tmp_path)]})
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace, events=events)
    _break_manager(agent)
    agent.run_turn("write")
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "hi"
    shown = [e for e in events if isinstance(e, HookEvent) and e.event == "PostToolUse"]
    assert shown and any("hook manager exploded" in w for w in shown[0].warnings)


def test_permission_request_manager_failure_still_asks_the_user(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PermissionRequest": [_noop_hook(tmp_path)]})
    asked: list[str] = []
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace,
                       events=events, mode="default", on_approval=lambda t, a, d: asked.append(t) or "deny")
    _break_manager(agent)
    agent.run_turn("write")
    assert asked == ["Write"] and not (workspace / "a.txt").exists()
    assert any(isinstance(e, HookEvent) and e.event == "PermissionRequest" and e.warnings for e in events)


# ── 2. updatedInput from a PermissionRequest hook is re-checked ──────────────

def _rewrite_hook(tmp: Path, new_input: dict[str, Any]) -> dict[str, Any]:
    out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                  "decision": {"behavior": "allow", "updatedInput": new_input}}}
    return {"hooks": [{"type": "command", "command": _json_hook(tmp, "rewrite.py", out)}]}


def test_permission_request_updated_input_cannot_skip_a_deny_rule(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PermissionRequest": [_rewrite_hook(tmp_path, {"path": "secret.env", "content": "leak"})]},
                extra={"permissions": {"deny": ["Write(secret.env)"]}})
    events: list[Any] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace,
                       events=events, mode="default")
    agent.permissions.add_session_rule("deny", "Write(secret.env)")
    agent.run_turn("write")
    assert not (workspace / "secret.env").exists() and not (workspace / "a.txt").exists()
    perms = [e for e in events if isinstance(e, PermissionDecisionEvent)]
    assert perms[-1].outcome == "deny" and "secret.env" in (perms[-1].rule + perms[-1].reason)


def test_permission_request_updated_input_cannot_skip_an_ask_rule(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PermissionRequest": [_rewrite_hook(tmp_path, {"path": "guarded.txt", "content": "x"})]})
    asked: list[dict[str, Any]] = []
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace,
                       mode="default", on_approval=lambda t, a, d: asked.append(dict(a)) or "deny")
    agent.permissions.add_session_rule("ask", "Write(guarded.txt)")
    agent.run_turn("write")
    assert [a.get("path") for a in asked] == ["guarded.txt"]    # the user is shown the rewritten input
    assert not (workspace / "guarded.txt").exists()


def test_permission_request_updated_input_still_allowed_when_no_rule_objects(workspace: Path, tmp_path: Path) -> None:
    _hooks_file(workspace, {"PermissionRequest": [_rewrite_hook(tmp_path, {"path": "b.txt", "content": "ok"})]})
    agent = make_agent(FakeProvider(scripted(("Write", {"path": "a.txt", "content": "hi"}), "ok")), workspace, mode="default")
    agent.run_turn("write")
    assert (workspace / "b.txt").read_text(encoding="utf-8") == "ok" and not (workspace / "a.txt").exists()
