"""Regression tests for the review items that were left open:

1. writers must not replace an unparsable settings.json with {}
2. /keys redaction of a bare key and of -u
3. a user-level base_url survives an ignored project base_url
4. HTTP hooks do not follow redirects (custom headers are secrets)
5. AXON_SUBAGENT_MODEL=inherit
6. Patch: mixed CRLF/LF files and a real directory called "a"
7. cancelling the custom-model prompt cancels the provider connection
8. vim counts are capped
9. an allow rule does not cover a redirect to a path outside the workspace
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from axon.config import Settings
from axon.errors import ToolError
from axon.permissions.engine import PermissionEngine
from axon.permissions.rules import patch_paths, redirect_targets, tool_paths
from axon.storage import axon_home

BROKEN = "{ this is not json, \"permissions\": ["


def _broken(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BROKEN, encoding="utf-8")
    return path


# ─── 1. unparsable settings.json is never replaced ───────────────────────────

def test_add_rule_refuses_unparsable_settings(workspace: Path) -> None:
    engine = PermissionEngine(Settings(workspace=workspace, mode="default", api_key="k"))
    path = _broken(engine.local_settings_path())
    with pytest.raises(ValueError, match="not valid JSON"):
        engine.add_rule("allow", "Bash(git status:*)")
    assert path.read_text(encoding="utf-8") == BROKEN
    assert not any(str(e.rule) == "Bash(git status:*)" for e in engine.entries)


def test_add_rule_still_works_on_a_missing_or_valid_file(workspace: Path) -> None:
    engine = PermissionEngine(Settings(workspace=workspace, mode="default", api_key="k"))
    engine.add_rule("allow", "Bash(git status:*)")
    engine.add_rule("deny", "Bash(rm:*)")
    data = json.loads(engine.local_settings_path().read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(git status:*)"], "deny": ["Bash(rm:*)"]}


def test_permissions_command_prints_the_error(workspace: Path, capsys: Any) -> None:
    from axon.commands.perm_cmds import handle_permissions
    s = Settings(workspace=workspace, mode="default", api_key="k")
    agent = SimpleNamespace(settings=s, permissions=PermissionEngine(s))
    path = _broken(agent.permissions.local_settings_path())
    handle_permissions(agent, "allow Bash(ls:*)")
    out = capsys.readouterr().out
    assert "not valid JSON" in out and "saved" not in out.lower()
    assert path.read_text(encoding="utf-8") == BROKEN


def test_skill_overrides_refuse_unparsable_settings(workspace: Path) -> None:
    from axon.skills import settings as skill_settings
    path = _broken(workspace / ".axon" / "settings.json")
    with pytest.raises(ValueError, match="not valid JSON"):
        skill_settings.disable_skill(workspace, "demo", "project")
    assert path.read_text(encoding="utf-8") == BROKEN


def test_set_skill_enabled_reports_unparsable_settings(workspace: Path) -> None:
    from axon.marketplace import control
    from axon.skills.manager import SkillManager
    skill_dir = workspace / ".axon" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: demo skill\n---\nbody\n", encoding="utf-8")
    assert SkillManager(workspace).get("demo") is not None
    path = _broken(axon_home() / "settings.json")
    done = control.set_skill_enabled(workspace, "demo", False)
    assert not done.ok and "not valid JSON" in done.message
    assert path.read_text(encoding="utf-8") == BROKEN


def test_save_automation_refuses_unparsable_settings() -> None:
    from axon.marketplace import automation
    path = _broken(automation.user_settings_path())
    with pytest.raises(ValueError, match="not valid JSON"):
        automation.save_automation({"enabled": True})
    assert path.read_text(encoding="utf-8") == BROKEN


def test_marketplace_auto_command_reports_unparsable_settings(workspace: Path) -> None:
    from axon.marketplace import automation, cli
    path = _broken(automation.user_settings_path())
    lines: list[str] = []
    outcome = cli.Marketplace(workspace, out=lines.append).run(["auto", "on"])
    assert outcome.code == 1 and "not valid JSON" in "\n".join(lines)
    assert path.read_text(encoding="utf-8") == BROKEN


def test_set_config_value_reports_a_failed_write(workspace: Path) -> None:
    from axon.commands.system_cmds import set_config_value
    s = Settings(workspace=workspace, mode="default", api_key="k")
    agent = SimpleNamespace(settings=s, permissions=None)
    path = _broken(axon_home() / "settings.json")
    with pytest.raises(ValueError, match="not saved"):
        set_config_value(agent, "tips", True)
    assert path.read_text(encoding="utf-8") == BROKEN


def test_config_command_does_not_claim_saved_on_failure(workspace: Path, capsys: Any) -> None:
    from axon.commands.system_cmds import handle_config
    s = Settings(workspace=workspace, mode="default", api_key="k")
    agent = SimpleNamespace(settings=s, permissions=None)
    _broken(axon_home() / "settings.json")
    handle_config(agent, "tips off")
    out = capsys.readouterr().out
    assert "saved to ~/.axon/settings.json" not in out and "not saved" in out


# ─── 2. /keys redaction ──────────────────────────────────────────────────────

KEY = "sk-live-BBBBBBBBBBBBBBBBBBBB2222"


@pytest.mark.parametrize("line,expected", [
    (f"/keys {KEY}", "/keys [REDACTED]"),
    (f"/key {KEY} --project", "/key [REDACTED] --project"),
    (f"/keys set {KEY}", "/keys set [REDACTED]"),
    (f"/keys set openai {KEY} -u", "/keys set openai [REDACTED] -u"),
    (f"/keys openai {KEY} -u", "/keys openai [REDACTED] -u"),
    (f"/keys set openai -u {KEY}", "/keys set openai -u [REDACTED]"),
    ("/keys openai", "/keys openai"),
    ("/keys set gemini", "/keys set gemini"),
])
def test_keys_redaction(line: str, expected: str) -> None:
    from axon.providers import keystore as ks
    assert ks.redact_command_line(line) == expected
    assert KEY not in ks.redact_command_line(line)


# ─── 3. user-level base_url survives an ignored project base_url ─────────────

def test_user_settings_json_base_url_is_kept_when_project_base_url_is_ignored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AXON_BASE_URL", "AXON_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    home = axon_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps({"baseUrl": "https://user.example/v1"}), encoding="utf-8")
    ws = tmp_path / "proj"
    (ws / ".axon").mkdir(parents=True)
    (ws / ".axon" / "config.toml").write_text('base_url = "https://evil.example/v1"\n', encoding="utf-8")
    settings = Settings.load({"workspace": ws, "api_key": "k"}, require_api_key=False)
    assert settings.base_url == "https://user.example/v1"


def test_user_config_toml_base_url_still_wins_over_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AXON_BASE_URL", "AXON_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    home = axon_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text('base_url = "https://toml.example/v1"\n', encoding="utf-8")
    ws = tmp_path / "proj"
    (ws / ".axon").mkdir(parents=True)
    (ws / ".axon" / "config.toml").write_text('base_url = "https://evil.example/v1"\n', encoding="utf-8")
    settings = Settings.load({"workspace": ws, "api_key": "k"}, require_api_key=False)
    assert settings.base_url == "https://toml.example/v1"


# ─── 4. HTTP hooks do not follow redirects ───────────────────────────────────

class _Target(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_POST(self) -> None:  # noqa: N802
        _Target.seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST  # noqa: N815

    def log_message(self, *a: Any) -> None:
        pass


def _redirector(location: str) -> type[BaseHTTPRequestHandler]:
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            pass
    return Redirect


def test_http_hook_does_not_send_its_secret_header_to_a_redirect_target(
        workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.hooks import HookManager
    from tests.test_core_helpers import make_settings
    _Target.seen = []
    target = HTTPServer(("127.0.0.1", 0), _Target)
    redirect = HTTPServer(("127.0.0.1", 0), _redirector(f"http://localhost:{target.server_port}/steal"))
    for srv in (target, redirect):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HOOK_TOKEN", "s3cret")
        d = workspace / ".axon"
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text(json.dumps({"hooks": {"Notification": [{"hooks": [{
            "type": "http", "url": f"http://127.0.0.1:{redirect.server_port}/h", "allowedEnvVars": ["HOOK_TOKEN"],
            "headers": {"Authorization": "Bearer $HOOK_TOKEN"}}]}]}}), encoding="utf-8")
        res = HookManager(make_settings(workspace), workspace).run("Notification", {})
        assert _Target.seen == []                         # the redirect was not followed
        assert res.warnings and "302" in " ".join(res.warnings)
    finally:
        target.shutdown()
        redirect.shutdown()


# ─── 5. AXON_SUBAGENT_MODEL ──────────────────────────────────────────────────

def test_subagent_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.agent.subagent import AgentDefinition, _resolve_model
    parent = SimpleNamespace(settings=SimpleNamespace(model="parent-model", small_model="small-model"))
    explore = AgentDefinition(name="Explore", description="x", fast_model=True, source="builtin")
    plain = AgentDefinition(name="p", description="x", source="user")
    monkeypatch.delenv("AXON_SUBAGENT_MODEL", raising=False)
    assert _resolve_model(parent, explore) == "small-model"
    assert _resolve_model(parent, plain) == "parent-model"
    monkeypatch.setenv("AXON_SUBAGENT_MODEL", "env-model")
    assert _resolve_model(parent, plain) == "env-model"
    assert _resolve_model(parent, explore) == "env-model"
    assert _resolve_model(parent, plain, "call-model") == "call-model"      # the Task call beats the env
    monkeypatch.setenv("AXON_SUBAGENT_MODEL", "inherit")
    assert _resolve_model(parent, plain) == "parent-model"                  # inherit: the parent's model
    assert _resolve_model(parent, explore) == "parent-model"                # also for the fast builtin agents
    assert _resolve_model(parent, plain, "call-model") == "call-model"


# ─── 6. Patch: mixed line endings, a real directory called "a" ───────────────

def _patch_ctx(workspace: Path) -> Any:
    from axon.agent.state import FileState, TodoState
    from axon.tools.base import ToolContext
    return ToolContext(workspace=workspace, file_state=FileState(), todos=TodoState(),
                       settings=Settings(workspace=workspace, api_key="k"))


def _patch(workspace: Path, text: str) -> str:
    from axon.tools.patch import PatchTool
    ctx = _patch_ctx(workspace)
    for p in workspace.rglob("*"):
        if p.is_file():
            ctx.file_state.record_read(p)       # the Patch tool wants the file read first
    return PatchTool().run({"patch": text}, ctx)


def test_patch_keeps_each_lines_ending_in_a_mixed_file(workspace: Path) -> None:
    f = workspace / "mixed.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\nfour\n")
    _patch(workspace, "--- a/mixed.txt\n+++ b/mixed.txt\n@@ -1,4 +1,4 @@\n one\n-two\n+TWO\n three\n four\n")
    assert f.read_bytes() == b"one\r\nTWO\r\nthree\nfour\n"
    _patch(workspace, "--- a/mixed.txt\n+++ b/mixed.txt\n@@ -3,2 +3,3 @@\n three\n-four\n+FOUR\n+five\n")
    assert f.read_bytes() == b"one\r\nTWO\r\nthree\nFOUR\nfive\n"


def test_patch_added_line_after_a_crlf_line_is_crlf(workspace: Path) -> None:
    f = workspace / "mixed.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\nfour\n")
    _patch(workspace, "--- a/mixed.txt\n+++ b/mixed.txt\n@@ -2,1 +2,2 @@\n two\n+two and a half\n")
    assert f.read_bytes() == b"one\r\ntwo\r\ntwo and a half\r\nthree\nfour\n"


def test_patch_uniform_crlf_file_still_works(workspace: Path) -> None:
    f = workspace / "crlf.txt"
    f.write_bytes(b"one\r\ntwo\r\n")
    _patch(workspace, "--- a/crlf.txt\n+++ b/crlf.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n")
    assert f.read_bytes() == b"one\r\nTWO\r\n"


def test_patch_git_prefix_is_stripped_when_no_such_directory(workspace: Path) -> None:
    (workspace / "x.py").write_text("a\n", encoding="utf-8")
    _patch(workspace, "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert (workspace / "x.py").read_text(encoding="utf-8") == "b\n"


def test_patch_keeps_the_prefix_for_a_real_directory_called_a(workspace: Path) -> None:
    (workspace / "a").mkdir()
    (workspace / "a" / "x.py").write_text("a\n", encoding="utf-8")
    _patch(workspace, "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert (workspace / "a" / "x.py").read_text(encoding="utf-8") == "b\n"
    assert not (workspace / "x.py").exists()


def test_patch_missing_file_in_a_directory_called_a_is_not_created(workspace: Path) -> None:
    (workspace / "a").mkdir()
    with pytest.raises(ToolError, match="non-existent"):
        _patch(workspace, "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n")


def test_permission_engine_sees_the_prefixed_patch_path_too() -> None:
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert patch_paths(diff) == ["src/a.py"]
    assert patch_paths(diff, keep_prefixed=True) == ["src/a.py", "a/src/a.py", "b/src/a.py"]
    assert tool_paths("Patch", {"patch": diff}) == ["src/a.py"]
    assert "a/src/a.py" in tool_paths("Patch", {"patch": diff}, keep_prefixed=True)


def test_deny_rule_on_a_real_directory_called_a_covers_a_prefixed_patch(workspace: Path) -> None:
    from axon.tools.patch import PatchTool
    e = PermissionEngine(Settings(workspace=workspace, mode="bypass", api_key="k"))
    e.add_session_rule("deny", "Edit(a/**)")
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert e.check(PatchTool(), {"patch": diff}, "bypass").outcome == "deny"


# ─── 7. cancelling the custom-model prompt cancels ───────────────────────────

def test_cancelling_the_custom_model_prompt_does_not_apply_the_default(
        monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from axon.providers import catalog
    from axon.ui import provider_picker as pp
    groq = next(p for p in catalog.PROVIDER_PRESETS if p.id == "groq")
    applied: list[str] = []
    picks: list[str] = []
    monkeypatch.setattr(pp, "find_key", lambda p: "gsk_existing")
    monkeypatch.setattr(pp.plat, "is_interactive", lambda: False)
    monkeypatch.setattr(pp, "_models_for", lambda preset, key: ["m1", "m2"])

    def fake_pick(items: list[str], **k: Any) -> str:
        picks.append("x")
        assert len(picks) < 5, "looped instead of cancelling"
        return pp.CUSTOM_MODEL_OPTION
    monkeypatch.setattr(pp, "pick", fake_pick)
    monkeypatch.setattr(pp, "_stdin_is_tty", lambda: False)         # no terminal: the prompt cannot be answered
    monkeypatch.setattr(pp, "apply_provider", lambda agent, preset, model, key: applied.append(model) or True)
    assert pp._configure_and_apply_preset(SimpleNamespace(), groq) is False
    assert applied == [] and len(picks) == 1
    assert "cancelled" in capsys.readouterr().out.lower()


def test_typed_custom_model_is_still_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    from axon.providers import catalog
    from axon.ui import provider_picker as pp
    groq = next(p for p in catalog.PROVIDER_PRESETS if p.id == "groq")
    applied: list[str] = []
    monkeypatch.setattr(pp, "find_key", lambda p: "gsk_existing")
    monkeypatch.setattr(pp.plat, "is_interactive", lambda: False)
    monkeypatch.setattr(pp, "_models_for", lambda preset, key: ["m1"])
    monkeypatch.setattr(pp, "pick", lambda items, **k: pp.CUSTOM_MODEL_OPTION)
    monkeypatch.setattr(pp, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "my-model")
    monkeypatch.setattr(pp, "apply_provider", lambda agent, preset, model, key: applied.append(model) or True)
    assert pp._configure_and_apply_preset(SimpleNamespace(), groq) is True
    assert applied == ["my-model"]


# ─── 8. vim counts are capped ────────────────────────────────────────────────

def test_huge_vim_count_is_capped() -> None:
    import time

    from axon.ui.input import EditorState, PromptEditor
    from axon.ui.keybindings import KeyResolver, default_bindings
    from axon.ui.vim import MAX_COUNT, VimState
    st = EditorState(history=[])
    st.vim = VimState()
    st.set_text("x")
    ed = PromptEditor(st, KeyResolver(default_bindings()))
    ed.handle("esc")
    for k in ("y", "y"):
        ed.handle(k)
    t0 = time.time()
    for k in "99999999p":
        ed.handle(k)
    assert time.time() - t0 < 5
    assert len(st.buf) <= MAX_COUNT * 2 + 2
    assert MAX_COUNT == 9999


# ─── 9. an allow rule does not cover a redirect outside the workspace ────────

@pytest.fixture
def engine(workspace: Path) -> PermissionEngine:
    e = PermissionEngine(Settings(workspace=workspace, mode="default", api_key="k"))
    e.add_session_rule("allow", "Bash(git diff:*)")
    return e


def _bash(engine: PermissionEngine, cmd: str) -> str:
    from axon.tools.shell import BashTool
    return engine.check(BashTool(), {"command": cmd}, "default").outcome


@pytest.mark.parametrize("cmd", [
    "git diff > ~/.bashrc",
    "git diff >> ~/.bashrc",
    "git diff >~/.bashrc",
    "git diff > /tmp/out.txt",
    "git diff > ../outside.txt",
    "git diff 2> /etc/motd",
    "git diff &> /tmp/all.txt",
    "git diff >| /tmp/x",
    "git diff > \"/tmp/with space\"",
    "git diff > $HOME/x",
])
def test_allow_rule_asks_for_a_redirect_outside_the_workspace(engine: PermissionEngine, cmd: str) -> None:
    assert _bash(engine, cmd) == "ask"


@pytest.mark.parametrize("cmd", [
    "git diff",
    "git diff --stat",
    "git diff > out.patch",
    "git diff >> sub/dir/out.patch",
    "git diff 2>&1",
    "git diff > /dev/null 2>&1",
    "git diff -- 'a > b'",
])
def test_allow_rule_still_allows_harmless_commands(engine: PermissionEngine, cmd: str) -> None:
    assert _bash(engine, cmd) == "allow"


def test_exact_command_rule_is_trusted_and_blanket_rules_are_untouched(workspace: Path) -> None:
    e = PermissionEngine(Settings(workspace=workspace, mode="default", api_key="k"))
    e.add_session_rule("allow", "Bash(git diff > /tmp/approved.txt)")
    assert _bash(e, "git diff > /tmp/approved.txt") == "allow"
    assert _bash(e, "git diff > /tmp/other.txt") == "ask"
    e.add_session_rule("allow", "Bash")
    assert _bash(e, "git diff > /tmp/other.txt") == "allow"


def test_redirect_targets_parser() -> None:
    assert redirect_targets("git diff > ~/.bashrc") == ["~/.bashrc"]
    assert redirect_targets("a >b; c >> d 2>&1") == ["b", "d"]
    assert redirect_targets("echo '>' \">\" x") == []
    assert redirect_targets("cmd >& f") == ["f"]
    assert redirect_targets("cmd 2>&1 >&2") == []
